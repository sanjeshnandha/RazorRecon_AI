"""
Batch runner. Loads a dataset, computes all four deltas for every settlement,
matches the bank, attributes, tiers, raises exceptions, scores itself against
ground truth, and persists an immutable run.

Every derived row carries run_id. Re-running never mutates a prior run.
"""
from __future__ import annotations

import json
import time
import uuid
from datetime import date, datetime

from engine import metrics as metrics_mod
from engine.attribution import attribute_d1, attribute_d2, attribute_d3, attribute_d4
from engine.calculation import compute_d1, compute_d3, compute_d4
from engine.db import copy_rows, fetch
from engine.exceptions import build_exception, status_for
from engine.invariants import check as check_invariants
from engine.loader import load
from engine.matcher import run_matcher
from engine.money import rupees
from engine.policy import load_policy

ENGINE_VERSION = "P0.1.0"


class Audit:
    def __init__(self):
        self.rows: list[tuple] = []
        self.n = 0

    def log(self, action, subject_type, subject_id, inputs=None, rule_ids=None,
            outputs=None, decision=None, tier=None):
        self.n += 1
        self.rows.append((self.n, "ENGINE", action, subject_type, subject_id,
                          json.dumps(inputs or {}, default=str), rule_ids or [],
                          json.dumps(outputs or {}, default=str), decision, tier))


def run(conn, dataset_id: str, label: str | None = None) -> dict:
    policy = load_policy()
    run_id = str(uuid.uuid4())
    t0 = time.perf_counter()

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO reconciliation_runs (run_id, dataset_id, policy_version, engine_version, "
            "config_hash) VALUES (%s,%s,%s,%s,%s)",
            (run_id, dataset_id, policy.version, ENGINE_VERSION, policy.config_hash))
    conn.commit()

    snap = load(conn, dataset_id)
    audit = Audit()
    inv = check_invariants(snap)
    audit.log("INVARIANTS_CHECKED", "dataset", dataset_id,
              outputs={"structural": len(inv.structural), "business": len(inv.business),
                       "excluded_items": len(inv.excluded_items)},
              decision="structural failures exclude the record; business failures are inputs")

    settlement_of_payment_map = {}
    for it in snap.items:
        if it["transaction_type"] == "PAYMENT" and it["payment_id"]:
            settlement_of_payment_map[it["payment_id"]] = it["settlement_id"]

    def settlement_of_payment(pid):
        return settlement_of_payment_map.get(pid)

    deltas: list[tuple] = []
    attributions: list[tuple] = []
    exceptions: list[dict] = []
    per_settlement_latency: list[float] = []
    d_index: dict[str, dict] = {}          # delta_id -> summary, for metrics
    attr_seq = 0
    exc_seq = 0

    def emit(delta_id, sid, kind, subject_id, expected, actual, delta, diag):
        nonlocal attr_seq, exc_seq
        explained = diag.explained_paise
        deltas.append((run_id, delta_id, sid, kind, subject_id, expected, actual, delta,
                       explained, diag.residual_paise, diag.tier, diag.status))
        d_index[delta_id] = {"settlement_id": sid, "delta_kind": kind, "subject_id": subject_id,
                             "delta_paise": delta, "residual_paise": diag.residual_paise,
                             "tier": diag.tier, "status": diag.status,
                             "exception_type": diag.exception_type,
                             "n_attributions": len(diag.attributions)}
        for a in diag.attributions:
            attr_seq += 1
            attributions.append((run_id, f"ATT_{attr_seq:06d}", delta_id, a.evidence_type,
                                 a.evidence_record_id, a.signed_amount_paise, a.derivation,
                                 a.rule_ids, a.rationale))
        # HARD INVARIANT: attributions + residual must reconstruct the delta exactly
        assert explained + diag.residual_paise == delta, (
            f"attribution invariant broken on {delta_id}: {explained} + "
            f"{diag.residual_paise} != {delta}")
        if diag.exception_type:
            exc_seq += 1
            exceptions.append(build_exception(
                f"EXC_{exc_seq:05d}", sid, subject_id, kind, diag.exception_type,
                delta, explained, diag.residual_paise, diag.tier, diag.notes))
        audit.log(f"{kind}_DIAGNOSED", "settlement" if subject_id is None else "record",
                  subject_id or sid,
                  inputs={"expected_paise": expected, "actual_paise": actual},
                  rule_ids=sorted({r for a in diag.attributions for r in a.rule_ids}),
                  outputs={"delta_paise": delta, "explained_paise": explained,
                           "residual_paise": diag.residual_paise,
                           "exception_type": diag.exception_type},
                  decision=diag.status, tier=diag.tier)

    # ---- Delta-2 matcher runs once over the whole batch ------------------
    outcome = run_matcher(snap, policy)
    match_rows = [(run_id, c["settlement_id"], c["bank_transaction_id"], c["pass_name"],
                   c["score_bps"], c["is_selected"], c["is_ambiguous"])
                  for c in outcome.candidates]
    for c in outcome.candidates:
        if c["is_selected"] or c["is_ambiguous"]:
            audit.log("BANK_MATCH_CANDIDATE", "settlement", c["settlement_id"],
                      inputs={"bank_transaction_id": c["bank_transaction_id"],
                              "pass": c["pass_name"], "score_bps": c["score_bps"]},
                      decision="SELECTED" if c["is_selected"] else "AMBIGUOUS_REFUSED")

    # ---- per-settlement Delta-1 / Delta-2 / Delta-3 ---------------------
    for s in snap.settlements:
        st = time.perf_counter()
        sid = s["settlement_id"]
        inv_rows = inv.by_settlement(sid)

        d1 = compute_d1(snap, s, policy, inv.excluded_items)
        diag1 = attribute_d1(snap, s, d1, policy, inv_rows)
        emit(f"{sid}:D1", sid, "D1_COMPUTE", None, d1.expected_net_paise, d1.actual_net_paise,
             d1.delta_paise, diag1)

        match = outcome.results[sid]
        diag2, exp2, act2 = attribute_d2(snap, s, match, policy)
        emit(f"{sid}:D2", sid, "D2_BANK", None, exp2, act2, exp2 - act2, diag2)

        d3 = compute_d3(snap, s, policy, inv.excluded_items)
        diag3 = attribute_d3(snap, s, d3, policy)
        emit(f"{sid}:D3", sid, "D3_LEDGER", None, d3.expected_paise, d3.actual_paise,
             d3.delta_paise, diag3)
        per_settlement_latency.append(time.perf_counter() - st)

    # ---- Delta-4, per allocation ----------------------------------------
    for r4 in compute_d4(snap, policy, settlement_of_payment):
        diag4 = attribute_d4(snap, r4, policy)
        emit(f"{r4.subject_id}:D4", r4.settlement_id or "UNASSIGNED", "D4_PAYOUT", r4.subject_id,
             r4.expected_paise, r4.actual_paise, r4.delta_paise, diag4)

    # ---- cross-cutting invariant exceptions ------------------------------
    for row in inv.structural + [r for r in inv.business if r["id"] in ("INV-B1", "INV-B5")]:
        exc_seq += 1
        tier = "C" if row in inv.structural else "A"
        exceptions.append(build_exception(
            f"EXC_{exc_seq:05d}", row["settlement_id"] or "UNASSIGNED", row["subject_id"],
            "D3_LEDGER" if row["id"] == "INV-B1" else "D1_COMPUTE",
            row["exception_type"], row["amount_paise"], 0 if tier == "C" else row["amount_paise"],
            row["amount_paise"] if tier == "C" else 0, tier, [row["detail"]]))

    # ---- persist ---------------------------------------------------------
    copy_rows(conn, "reconciliation_deltas",
              ["run_id","delta_id","settlement_id","delta_kind","subject_id","expected_paise",
               "actual_paise","delta_paise","explained_paise","residual_paise","tier","status"], deltas)
    copy_rows(conn, "attributions",
              ["run_id","attribution_id","delta_id","evidence_type","evidence_record_id",
               "signed_amount_paise","derivation","rule_ids","rationale"], attributions)
    copy_rows(conn, "match_candidates",
              ["run_id","settlement_id","bank_transaction_id","pass_name","score_bps",
               "is_selected","is_ambiguous"], match_rows)
    copy_rows(conn, "exceptions",
              ["run_id","exception_id","settlement_id","subject_id","delta_kind","exception_type",
               "severity","amount_paise","explained_paise","unexplained_paise","tier","status",
               "recommended_action"],
              [(run_id, e["exception_id"], e["settlement_id"], e["subject_id"], e["delta_kind"],
                e["exception_type"], e["severity"], e["amount_paise"], e["explained_paise"],
                e["unexplained_paise"], e["tier"], e["status"], e["recommended_action"])
               for e in exceptions])
    copy_rows(conn, "audit_log",
              ["run_id","audit_id","actor","action","subject_type","subject_id","inputs",
               "rule_ids","outputs","decision","tier"],
              [(run_id,) + r for r in audit.rows])
    conn.commit()

    elapsed = time.perf_counter() - t0
    m = metrics_mod.compute(conn, snap, run_id, dataset_id, d_index, exceptions, outcome,
                            per_settlement_latency, elapsed, policy)
    with conn.cursor() as cur:
        cur.execute("UPDATE reconciliation_runs SET finished_at=now(), status='COMPLETED', "
                    "metrics=%s WHERE run_id=%s", (json.dumps(m, default=str), run_id))
    conn.commit()
    m["run_id"] = run_id
    return m
