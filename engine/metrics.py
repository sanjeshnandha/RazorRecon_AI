"""
Metrics. Reported separately, never blended -- "throughput plus measured
accuracy plus an honest exception list" is three things, not one.

The two metrics that keep the others honest:
  * false auto-resolution count -- tier A cases contradicting ground truth.
    Must be 0.
  * resolvable-anomaly detection rate -- of the planted anomalies that CAN be
    diagnosed, the fraction actually flagged. A "flag nothing" engine scores a
    perfect 0 false auto-resolutions and 0% here, which is the point: being
    conservative is only virtuous when paired with actually finding things.
"""
from __future__ import annotations

import statistics

from engine.db import fetch
from engine.money import rupees


def _rate(num: int, den: int) -> float:
    return round(100.0 * num / den, 2) if den else 0.0


def compute(conn, snap, run_id, dataset_id, d_index, exceptions, outcome,
            latencies, elapsed, policy) -> dict:
    gt = fetch(conn, "SELECT * FROM ground_truth_anomalies WHERE dataset_id=%s ORDER BY anomaly_id",
               (dataset_id,))
    counts = fetch(conn, "SELECT row_counts FROM datasets WHERE dataset_id=%s", (dataset_id,))[0]["row_counts"]

    by_kind: dict[str, list[dict]] = {}
    for d in d_index.values():
        by_kind.setdefault(d["delta_kind"], []).append(d)

    settlements = {s["settlement_id"] for s in snap.settlements}
    resolved_by_settlement: dict[str, bool] = {sid: True for sid in settlements}
    for d in d_index.values():
        sid = d["settlement_id"]
        if sid in resolved_by_settlement and d["tier"] == "C":
            resolved_by_settlement[sid] = False

    total_settled_value = sum(max(s["net_settlement_amount_paise"], 0) for s in snap.settlements)
    open_exceptions = [e for e in exceptions if e["status"] != "AUTO_RESOLVED"]
    at_risk_paise = sum(e["unexplained_paise"] for e in exceptions)
    unreconciled_value = sum(
        abs(d["residual_paise"]) for d in d_index.values()
        if d["delta_kind"] in ("D1_COMPUTE", "D2_BANK") and d["tier"] == "C")

    # ---- ground-truth scoring -------------------------------------------
    exc_by_settlement: dict[str, list[dict]] = {}
    for e in exceptions:
        exc_by_settlement.setdefault(e["settlement_id"], []).append(e)
    exc_by_subject: dict[str, list[dict]] = {}
    for e in exceptions:
        if e["subject_id"]:
            exc_by_subject.setdefault(e["subject_id"], []).append(e)

    # A settlement can legitimately carry more than one planted anomaly on
    # different axes -- a ledger duplicate on a settlement that is also a
    # Delta-1 trap, say. When scoring a trap, an exception another anomaly on
    # the same settlement genuinely expects is NOT noise; only an exception no
    # planted anomaly accounts for is a fabrication.
    expected_here: dict[str, set] = {}
    for g in gt:
        if g["expected_exception_type"] and g["expected_exception_type"] != "NONE":
            expected_here.setdefault(g["settlement_id"], set()).add(g["expected_exception_type"])

    def _noise(g, rows):
        """Exceptions on this settlement that no planted anomaly accounts for,
        scoped to the delta the trap is about."""
        legit = expected_here.get(g["settlement_id"], set()) | {"TIMING_DIFFERENCE"}
        scope = g["expected_delta_kind"]
        return [e for e in rows
                if e["exception_type"] not in legit
                and (scope is None or e["delta_kind"] == scope)]

    detection_hits = detection_total = 0
    diagnosis_hits = diagnosis_total = 0
    trap_pass = trap_total = 0
    escalation_hits = escalation_total = 0
    gt_detail: list[dict] = []

    for g in gt:
        sid = g["settlement_id"]
        subject = g["subject_id"]
        relevant = list(exc_by_settlement.get(sid, []))
        if subject in exc_by_subject:
            relevant += [e for e in exc_by_subject[subject] if e not in relevant]
        kinds = {e["exception_type"] for e in relevant}
        tiers = {e["tier"] for e in relevant}
        record = {"anomaly_id": g["anomaly_id"], "anomaly_type": g["anomaly_type"],
                  "settlement_id": sid, "subject_id": subject,
                  "planted_amount_paise": g["planted_amount_paise"],
                  "expected_exception_type": g["expected_exception_type"],
                  "observed_exception_types": sorted(kinds), "observed_tiers": sorted(tiers),
                  "is_resolvable": g["is_resolvable"]}

        if not g["is_resolvable"]:
            escalation_total += 1
            ok = "C" in tiers
            escalation_hits += ok
            record["outcome"] = "CORRECTLY_ESCALATED" if ok else "MISSED_ESCALATION"
        elif g["expected_exception_type"] == "NONE":
            # false-positive trap: the correct behaviour is to raise nothing here
            trap_total += 1
            noise = _noise(g, relevant)
            ok = not noise
            trap_pass += ok
            record["outcome"] = "TRAP_AVOIDED" if ok else "FALSE_POSITIVE"
            record["fabricated"] = [e["exception_id"] for e in noise]
        else:
            detection_total += 1
            detected = bool(relevant)
            detection_hits += detected
            diagnosis_total += 1
            correct = g["expected_exception_type"] in kinds
            diagnosis_hits += correct
            record["outcome"] = ("DIAGNOSED" if correct else
                                 "DETECTED_WRONG_TYPE" if detected else "MISSED")
        gt_detail.append(record)

    # ---- false auto-resolution: a tier A verdict that contradicts truth --
    false_auto: list[dict] = []
    for g in gt:
        if g["expected_exception_type"] == "NONE":
            # a trap: an exception here that NO planted anomaly accounts for is
            # an invented diagnosis, which is exactly a false auto-resolution
            for e in _noise(g, exc_by_settlement.get(g["settlement_id"], [])):
                false_auto.append({"anomaly_id": g["anomaly_id"], "reason":
                                   f"fabricated {e['exception_type']} with nothing to support it",
                                   "exception_id": e["exception_id"]})
        elif not g["is_resolvable"]:
            for e in exc_by_settlement.get(g["settlement_id"], []):
                if e["tier"] == "A" and e["exception_type"] == g["expected_exception_type"]:
                    false_auto.append({"anomaly_id": g["anomaly_id"], "reason":
                                       "undiagnosable case auto-resolved",
                                       "exception_id": e["exception_id"]})
    # a matcher guard that fired wrongly is also a false auto-resolution
    selected_passes = {(c["settlement_id"], c["pass_name"]) for c in outcome.candidates if c["is_selected"]}
    guard_checks = []
    for g in gt:
        if g["anomaly_type"] == "D2_SUFFIX_COLLISION":
            bad = (g["settlement_id"], "UTR_SUFFIX") in selected_passes
            guard_checks.append({"anomaly_id": g["anomaly_id"], "guard": "UTR_SUFFIX uniqueness",
                                 "settlement_id": g["settlement_id"],
                                 "passed": not bad,
                                 "matched_by": outcome.results[g["settlement_id"]].pass_name})
            if bad:
                false_auto.append({"anomaly_id": g["anomaly_id"],
                                   "reason": "UTR_SUFFIX selected despite a colliding suffix"})
        if g["anomaly_type"] == "D2_SAME_AMOUNT_SAME_DAY":
            bad = (g["settlement_id"], "EXACT_AMOUNT_DATE") in selected_passes
            guard_checks.append({"anomaly_id": g["anomaly_id"], "guard": "amount+date ambiguity",
                                 "settlement_id": g["settlement_id"], "passed": not bad,
                                 "matched_by": outcome.results[g["settlement_id"]].pass_name})
            if bad:
                false_auto.append({"anomaly_id": g["anomaly_id"],
                                   "reason": "EXACT_AMOUNT_DATE selected with a competing settlement"})

    all_deltas = list(d_index.values())
    tier_a = sum(1 for d in all_deltas if d["tier"] == "A")
    tier_b = sum(1 for d in all_deltas if d["tier"] == "B")
    tier_c = sum(1 for d in all_deltas if d["tier"] == "C")

    d4 = by_kind.get("D4_PAYOUT", [])
    d4_alloc = [d for d in d4 if d["delta_kind"] == "D4_PAYOUT"]

    lat = sorted(latencies) or [0.0]
    def pct(p):
        return round(lat[min(int(len(lat) * p), len(lat) - 1)] * 1000, 3)

    m = {
        "run_id": run_id, "dataset_id": dataset_id,
        "policy_version": policy.version, "config_hash": policy.config_hash,
        "throughput": {
            "settlements": len(snap.settlements),
            "total_financial_records": counts.get("total_financial_records"),
            "ledger_postings": counts.get("ledger_entries"),
            "record_counts": counts,
            "elapsed_seconds": round(elapsed, 3),
            "records_per_second": int(counts.get("total_financial_records", 0) / elapsed) if elapsed else 0,
            "p50_settlement_ms": pct(0.50), "p95_settlement_ms": pct(0.95),
            "headline": (
                f"{len(snap.settlements)} settlements backed by "
                f"{counts.get('total_financial_records', 0):,} financial records across payments, "
                f"refunds, seller allocations, transfers, bank credits and accounting ledger "
                f"entries -- of which {counts.get('ledger_entries', 0):,} are individual ledger "
                f"postings, not standalone transactions"),
        },
        "accuracy": {
            "record_match_rate_pct": _rate(sum(1 for v in resolved_by_settlement.values() if v),
                                           len(settlements)),
            "settlements_fully_resolved": sum(1 for v in resolved_by_settlement.values() if v),
            "d1_match_rate_pct": _rate(sum(1 for d in by_kind.get("D1_COMPUTE", []) if d["tier"] != "C"),
                                       len(by_kind.get("D1_COMPUTE", []))),
            "d2_match_rate_pct": _rate(sum(1 for d in by_kind.get("D2_BANK", []) if d["tier"] != "C"),
                                       len(by_kind.get("D2_BANK", []))),
            "d3_match_rate_pct": _rate(sum(1 for d in by_kind.get("D3_LEDGER", []) if d["tier"] != "C"),
                                       len(by_kind.get("D3_LEDGER", []))),
            "d4_match_rate_pct": _rate(sum(1 for d in d4_alloc if d["tier"] != "C"), len(d4_alloc)),
            "monetary_reconciliation_rate_pct": _rate(total_settled_value - unreconciled_value,
                                                      total_settled_value),
            "seller_payout_reconciliation_rate_pct": _rate(
                sum(1 for d in d4_alloc if d["tier"] != "C"), len(d4_alloc)),
            "total_settled_value_paise": total_settled_value,
            "unreconciled_value_paise": unreconciled_value,
            "amount_at_risk_paise": at_risk_paise,
            "amount_at_risk_display": rupees(at_risk_paise),
        },
        "tiers": {
            "total_deltas": len(all_deltas),
            "tier_a": tier_a, "tier_b": tier_b, "tier_c": tier_c,
            "auto_resolution_rate_pct": _rate(tier_a, len(all_deltas)),
            "human_review_rate_pct": _rate(tier_b, len(all_deltas)),
            "unresolved_rate_pct": _rate(tier_c, len(all_deltas)),
        },
        "ground_truth": {
            "planted_total": len(gt),
            "resolvable_planted": detection_total,
            "resolvable_detected": detection_hits,
            "resolvable_detection_rate_pct": _rate(detection_hits, detection_total),
            "diagnosis_correct": diagnosis_hits,
            "diagnosis_accuracy_pct": _rate(diagnosis_hits, diagnosis_total),
            "unresolvable_planted": escalation_total,
            "unresolvable_correctly_escalated": escalation_hits,
            "correct_escalation_rate_pct": _rate(escalation_hits, escalation_total),
            "false_positive_traps": trap_total,
            "false_positive_traps_avoided": trap_pass,
            "false_auto_resolution_count": len(false_auto),
            "false_auto_resolutions": false_auto,
            "matcher_guard_checks": guard_checks,
            "detail": gt_detail,
        },
        "exceptions": {
            "total": len(exceptions),
            "open": len(open_exceptions),
            "by_severity": {sev: sum(1 for e in exceptions if e["severity"] == sev)
                            for sev in ("HIGH", "MEDIUM", "LOW")},
            "by_status": {st: sum(1 for e in exceptions if e["status"] == st)
                          for st in ("AUTO_RESOLVED", "NEEDS_REVIEW", "UNRESOLVED")},
            "by_type": {},
        },
        "demo_policy_disclaimer": (
            "Every fee, tax, settlement-cycle and bank-lag figure here comes from a synthetic "
            "Demo Merchant Policy authored for this project. It is not a claim about Razorpay's "
            "actual terms, which vary by merchant category, risk grade and commercial agreement."),
    }
    for e in exceptions:
        m["exceptions"]["by_type"][e["exception_type"]] = \
            m["exceptions"]["by_type"].get(e["exception_type"], 0) + 1
    return m
