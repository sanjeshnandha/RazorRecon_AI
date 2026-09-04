"""
The agent's bounded, read-only view of a finished run.

Three rules hold for every tool in this file, and they are the whole security
argument for letting a language model near financial data:

  1. No SQL reaches the database that the model wrote. The model picks a tool
     name and a few typed arguments; the SQL is fixed here.
  2. Every query is scoped to the one run_id (and its dataset_id) the caller
     opened the session with, so a question about one run can never read another.
  3. Every query is LIMITed, and every limit is clamped here rather than trusted
     from the arguments.

Only SELECTs exist in this module. There is no code path by which the agent can
write, and therefore none by which it can change a number, a tier, or a status.

Money is returned as integer paise AND as a preformatted rupee string. The model
is never asked to do arithmetic -- it reports figures the engine already computed.
"""
from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from engine.db import fetch, fetch_one
from engine.lineage import trace
from engine.policy import load_policy

MAX_ROWS = 60          # hard ceiling on any list a tool may return
DEFAULT_ROWS = 20


def _clamp(n, lo: int, hi: int, default: int) -> int:
    try:
        n = int(n)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def rupees(paise: int | None) -> str:
    """Indian-format rupee string. Presentation only -- paise stays the truth."""
    if paise is None:
        return "n/a"
    neg, p = paise < 0, abs(int(paise))
    whole, frac = divmod(p, 100)
    s = str(whole)
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        s = ",".join(parts + [tail])
    return f"{'-' if neg else ''}Rs {s}.{frac:02d}"


def _jsonable(v):
    """Everything a tool returns is json.dumps'd before the model sees it, so an
    un-encodable value is a hard runtime failure mid-conversation rather than a
    cosmetic problem. Unknown types degrade to str() instead of raising."""
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, (Decimal, UUID)):
        return int(v) if isinstance(v, Decimal) else str(v)
    if isinstance(v, (list, tuple, set)):
        return [_jsonable(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    return str(v)


# run_id and dataset_id are already pinned by the session; echoing them on every
# row wastes context the model could spend on evidence.
_NOISE = ("run_id", "dataset_id")


def _row(r: dict, money_keys=()) -> dict:
    out = {k: _jsonable(v) for k, v in r.items() if k not in _NOISE}
    for k in money_keys:
        if out.get(k) is not None:
            out[f"{k}_display"] = rupees(r[k])
    return out


# =============================================================================
# Tools. Each takes (conn, ctx, **kwargs) and returns a JSON-safe dict.
# ctx carries the run_id and dataset_id the session is pinned to.
# =============================================================================

def _settlement_exists(conn, ctx, settlement_id: str) -> bool:
    return fetch_one(conn, "SELECT 1 AS ok FROM settlements WHERE dataset_id=%s "
                           "AND settlement_id=%s", (ctx["dataset_id"], settlement_id)) is not None


def run_overview(conn, ctx) -> dict:
    """Headline metrics for the run: the four delta rates, amount at risk, tiers."""
    row = fetch_one(conn, "SELECT metrics, policy_version, engine_version, config_hash, "
                          "started_at, finished_at FROM reconciliation_runs WHERE run_id=%s",
                    (ctx["run_id"],))
    if not row:
        return {"error": "run not found"}
    m = row["metrics"] or {}
    if isinstance(m, str):
        m = json.loads(m)
    n = fetch_one(conn, "SELECT count(*) c FROM settlements WHERE dataset_id=%s",
                  (ctx["dataset_id"],))["c"]
    return {
        "run_id": ctx["run_id"], "settlements_in_dataset": n,
        "policy_version": row["policy_version"], "engine_version": row["engine_version"],
        "config_hash": row["config_hash"],
        "accuracy": m.get("accuracy", {}), "tiers": m.get("tiers", {}),
        "throughput": m.get("throughput", {}), "exceptions": m.get("exceptions", {}),
        "ground_truth_scoring": {k: v for k, v in (m.get("ground_truth") or {}).items()
                                 if not isinstance(v, list)},
        "note": "All figures were computed deterministically by the engine before this "
                "conversation began. They are read here, never recalculated.",
    }


def list_settlements(conn, ctx, tier: str | None = None, delta_kind: str | None = None,
                     min_unexplained_paise: int = 1, limit: int = DEFAULT_ROWS) -> dict:
    """Settlements ranked by unexplained residual. The default surfaces only
    settlements that actually have a residue -- the agent's job is the exceptions."""
    limit = _clamp(limit, 1, MAX_ROWS, DEFAULT_ROWS)
    sql = """
        SELECT d.settlement_id,
               SUM(ABS(d.residual_paise)) AS unexplained_paise,
               SUM(ABS(d.delta_paise))    AS delta_paise,
               MAX(d.tier)                AS worst_tier,
               array_agg(DISTINCT d.delta_kind ORDER BY d.delta_kind)
                 FILTER (WHERE d.residual_paise <> 0) AS kinds_with_residue,
               s.net_settlement_amount_paise, s.settlement_date, s.settlement_utr
        FROM reconciliation_deltas d
        JOIN settlements s ON s.dataset_id=%(ds)s AND s.settlement_id=d.settlement_id
        WHERE d.run_id=%(run)s
    """
    p = {"run": ctx["run_id"], "ds": ctx["dataset_id"], "lim": limit}
    if tier in ("A", "B", "C"):
        sql += " AND d.tier=%(tier)s"
        p["tier"] = tier
    if delta_kind in ("D1_COMPUTE", "D2_BANK", "D3_LEDGER", "D4_PAYOUT"):
        sql += " AND d.delta_kind=%(kind)s"
        p["kind"] = delta_kind
    sql += """ GROUP BY d.settlement_id, s.net_settlement_amount_paise, s.settlement_date,
                        s.settlement_utr
               HAVING SUM(ABS(d.residual_paise)) >= %(minres)s
               ORDER BY unexplained_paise DESC, d.settlement_id
               LIMIT %(lim)s"""
    p["minres"] = max(0, int(min_unexplained_paise or 0))
    rows = fetch(conn, sql, p)
    return {"count": len(rows), "limit": limit,
            "settlements": [_row(r, ("unexplained_paise", "delta_paise",
                                     "net_settlement_amount_paise")) for r in rows]}


def get_settlement(conn, ctx, settlement_id: str) -> dict:
    """Header, all four deltas, and the reported-vs-expected arithmetic."""
    s = fetch_one(conn, "SELECT * FROM settlements WHERE dataset_id=%s AND settlement_id=%s",
                  (ctx["dataset_id"], settlement_id))
    if not s:
        return {"error": f"no settlement {settlement_id} in this run's dataset"}
    deltas = fetch(conn, "SELECT * FROM reconciliation_deltas WHERE run_id=%s AND settlement_id=%s "
                         "ORDER BY delta_kind, delta_id",
                   (ctx["run_id"], settlement_id))
    money = ("gross_amount_paise", "refund_amount_paise", "fee_amount_paise", "tax_amount_paise",
             "adjustment_amount_paise", "net_settlement_amount_paise")
    dmoney = ("expected_paise", "actual_paise", "delta_paise", "explained_paise", "residual_paise")

    # D4 is per seller allocation, so a busy settlement carries dozens of rows that
    # are almost all exactly zero. Returning them whole would bury the one delta
    # that actually has a residue under 40 lines of noise, and the model reasons
    # over what it can see. Settlement-level deltas in full; D4 summarised, with
    # only the non-zero allocations listed.
    level = [d for d in deltas if d["delta_kind"] != "D4_PAYOUT"]
    d4 = [d for d in deltas if d["delta_kind"] == "D4_PAYOUT"]
    d4_nonzero = [d for d in d4 if d["residual_paise"] != 0 or d["delta_paise"] != 0]
    d4_summary = {
        "allocation_count": len(d4),
        "allocations_with_a_difference": len(d4_nonzero),
        "total_residual_paise": sum(abs(d["residual_paise"]) for d in d4),
        "total_residual_display": rupees(sum(abs(d["residual_paise"]) for d in d4)),
        "worst_tier": max((d["tier"] for d in d4), default="A"),
        "non_zero": [_row(d, dmoney) for d in d4_nonzero[:MAX_ROWS]],
        "note": "Call get_seller_payouts for the full per-seller breakdown.",
    } if d4 else None

    return {
        "settlement": _row(s, money),
        "deltas": [_row(d, dmoney) for d in level],
        "d4_payout_summary": d4_summary,
        "delta_meanings": {
            "D1_COMPUTE": "expected net recomputed from the policy registry vs the net the "
                          "settlement report claims",
            "D2_BANK": "the settlement's net vs the bank credit that actually arrived",
            "D3_LEDGER": "the merchant's own double-entry books for these payments",
            "D4_PAYOUT": "what each seller was owed vs what was transferred",
        },
    }


def get_evidence(conn, ctx, settlement_id: str, delta_kind: str | None = None) -> dict:
    """The attribution ledger: every rupee the engine could explain, what evidence
    explained it, and which policy rule authorised that. This is the tool to reach
    for when the question is 'why'."""
    sql = """SELECT a.attribution_id, a.delta_id, a.evidence_type, a.evidence_record_id,
                    a.signed_amount_paise, a.derivation, a.rule_ids, a.rationale,
                    d.delta_kind, d.residual_paise, d.tier
             FROM attributions a
             JOIN reconciliation_deltas d ON d.run_id=a.run_id AND d.delta_id=a.delta_id
             WHERE a.run_id=%s AND d.settlement_id=%s"""
    params = [ctx["run_id"], settlement_id]
    if delta_kind:
        sql += " AND d.delta_kind=%s"
        params.append(delta_kind)
    sql += " ORDER BY d.delta_kind, a.attribution_id LIMIT %s"
    params.append(MAX_ROWS)
    if not _settlement_exists(conn, ctx, settlement_id):
        return {"error": f"no settlement {settlement_id} in this run's dataset"}
    rows = fetch(conn, sql, tuple(params))
    note = ("derivation=DETERMINISTIC means the link is provable; FUZZY means the engine "
            "matched on weaker evidence and capped the tier accordingly.")
    if not rows:
        note = ("This settlement exists but the engine attributed nothing on it: either it "
                "reconciled cleanly, or the difference is one it could not explain at all "
                "(check the residual and tier from get_settlement).")
    # Echo the subject. A settlement the engine could explain nothing about still
    # has to count as a record the agent legitimately read, or the citation guard
    # reports a false unsupported reference on the very case being investigated.
    return {"settlement_id": settlement_id, "count": len(rows),
            "attributions": [_row(r, ("signed_amount_paise", "residual_paise")) for r in rows],
            "note": note}


def list_exceptions(conn, ctx, status: str | None = None, severity: str | None = None,
                    delta_kind: str | None = None, limit: int = DEFAULT_ROWS) -> dict:
    """Open items, worst first."""
    limit = _clamp(limit, 1, MAX_ROWS, DEFAULT_ROWS)
    sql = "SELECT * FROM exceptions WHERE run_id=%s"
    params = [ctx["run_id"]]
    for col, val, allowed in (
            ("status", status, ("AUTO_RESOLVED", "NEEDS_REVIEW", "UNRESOLVED")),
            ("severity", severity, ("LOW", "MEDIUM", "HIGH")),
            ("delta_kind", delta_kind, ("D1_COMPUTE", "D2_BANK", "D3_LEDGER", "D4_PAYOUT"))):
        if val in allowed:
            sql += f" AND {col}=%s"
            params.append(val)
    sql += (" ORDER BY (severity='HIGH') DESC, unexplained_paise DESC, exception_id LIMIT %s")
    params.append(limit)
    rows = fetch(conn, sql, tuple(params))
    total = fetch_one(conn, "SELECT COALESCE(SUM(unexplained_paise),0) t FROM exceptions "
                            "WHERE run_id=%s AND status <> 'AUTO_RESOLVED'", (ctx["run_id"],))["t"]
    return {"count": len(rows), "limit": limit,
            "total_open_unexplained_paise": int(total),
            "total_open_unexplained_display": rupees(int(total)),
            "exceptions": [_row(r, ("amount_paise", "explained_paise", "unexplained_paise"))
                           for r in rows]}


def get_matcher_trail(conn, ctx, settlement_id: str) -> dict:
    """Every bank-matching pass that was tried for this settlement and what it
    found. A D2 residue is almost always explained here."""
    cands = fetch(conn, """SELECT c.pass_name, c.bank_transaction_id, c.score_bps,
                                  c.is_selected, c.is_ambiguous,
                                  b.transaction_date, b.description, b.credit_paise,
                                  b.debit_paise, b.settlement_utr
                           FROM match_candidates c
                           LEFT JOIN bank_transactions b ON b.dataset_id=%s
                                 AND b.bank_transaction_id=c.bank_transaction_id
                           WHERE c.run_id=%s AND c.settlement_id=%s
                           ORDER BY c.is_selected DESC, c.pass_name LIMIT %s""",
                  (ctx["dataset_id"], ctx["run_id"], settlement_id, MAX_ROWS))
    s = fetch_one(conn, "SELECT settlement_utr, net_settlement_amount_paise, settlement_date "
                        "FROM settlements WHERE dataset_id=%s AND settlement_id=%s",
                  (ctx["dataset_id"], settlement_id))
    if not s:
        return {"error": f"no settlement {settlement_id} in this run's dataset"}
    note = ("is_selected marks the match the engine accepted. is_ambiguous means two or more "
            "candidates fit equally well, so the engine refused to pick and left the case "
            "unresolved rather than guessing.")
    if not cands:
        # No candidates at all is a finding in its own right, and a different one
        # from "candidates existed but none matched". Say so, or the model will
        # reach for an explanation the evidence does not support.
        note = ("No bank line was offered to any matching pass for this settlement. That means "
                "no credit carrying this UTR, amount or date window exists in the statement at "
                "all -- the money has not arrived, as opposed to arriving and failing to match. "
                "Common causes: the credit is still in flight (settlement dated within the "
                "expected bank lag), or the credit is genuinely missing. Compare "
                "settlement_date against expected_bank_lag_days from get_policy before "
                "concluding which.")
    return {"settlement_id": settlement_id,
            "settlement": _row(s, ("net_settlement_amount_paise",)),
            "candidate_count": len(cands),
            "candidates": [_row(c, ("credit_paise", "debit_paise")) for c in cands],
            "note": note}


def get_payments(conn, ctx, settlement_id: str, limit: int = DEFAULT_ROWS) -> dict:
    """Payment lines with the fee and tax the report charged. Compare against the
    policy rate from get_policy to see a D1 fee drift."""
    limit = _clamp(limit, 1, MAX_ROWS, DEFAULT_ROWS)
    rows = fetch(conn, """SELECT si.settlement_item_id, si.payment_id, si.amount_paise,
                                 si.fee_paise, si.tax_paise, si.transaction_date,
                                 p.payment_method, p.payment_status
                          FROM settlement_items si
                          LEFT JOIN payments p ON p.dataset_id=si.dataset_id
                                AND p.payment_id=si.payment_id
                          WHERE si.dataset_id=%s AND si.settlement_id=%s
                            AND si.transaction_type='PAYMENT'
                          ORDER BY si.amount_paise DESC LIMIT %s""",
                  (ctx["dataset_id"], settlement_id, limit))
    if not rows and not _settlement_exists(conn, ctx, settlement_id):
        return {"error": f"no settlement {settlement_id} in this run's dataset"}
    return {"settlement_id": settlement_id, "count": len(rows),
            "payments": [_row(r, ("amount_paise", "fee_paise", "tax_paise")) for r in rows]}


def get_ledger(conn, ctx, settlement_id: str, limit: int = 40) -> dict:
    """Double-entry postings touching this settlement, plus the clearing balance
    per payment. A non-zero clearing balance is what a D3 residue means."""
    limit = _clamp(limit, 1, MAX_ROWS, 40)
    rows = fetch(conn, """SELECT ledger_entry_id, entry_group_id, account, direction,
                                 amount_paise, payment_id, refund_id, ledger_date, description
                          FROM ledger_entries
                          WHERE dataset_id=%s AND settlement_id=%s
                          ORDER BY entry_group_id, ledger_entry_id LIMIT %s""",
                  (ctx["dataset_id"], settlement_id, limit))
    bal = fetch(conn, """SELECT le.payment_id,
                                SUM(CASE WHEN le.direction='DR' THEN le.amount_paise
                                         ELSE -le.amount_paise END) AS clearing_paise
                         FROM ledger_entries le
                         JOIN settlement_items si ON si.dataset_id=le.dataset_id
                              AND si.payment_id=le.payment_id
                              AND si.transaction_type='PAYMENT'
                              AND si.settlement_id=%s
                         WHERE le.dataset_id=%s AND le.account='RAZORPAY_CLEARING'
                         GROUP BY le.payment_id
                         HAVING SUM(CASE WHEN le.direction='DR' THEN le.amount_paise
                                         ELSE -le.amount_paise END) <> 0
                         LIMIT %s""", (settlement_id, ctx["dataset_id"], MAX_ROWS))
    if not rows and not _settlement_exists(conn, ctx, settlement_id):
        return {"error": f"no settlement {settlement_id} in this run's dataset"}
    return {"settlement_id": settlement_id,
            "postings": [_row(r, ("amount_paise",)) for r in rows],
            "nonzero_clearing_balances": [_row(r, ("clearing_paise",)) for r in bal],
            "note": "A fully settled payment leaves RAZORPAY_CLEARING at exactly zero. Any "
                    "payment listed under nonzero_clearing_balances has a duplicate, missing "
                    "or misdirected posting."}


def get_seller_payouts(conn, ctx, settlement_id: str | None = None,
                       seller_id: str | None = None, limit: int = DEFAULT_ROWS) -> dict:
    """D4: what each seller was owed on this settlement's payments vs what moved."""
    limit = _clamp(limit, 1, MAX_ROWS, DEFAULT_ROWS)
    sql = """SELECT d.subject_id AS allocation_id, d.expected_paise, d.actual_paise,
                    d.delta_paise, d.residual_paise, d.tier, d.status, d.settlement_id,
                    a.seller_id, sl.seller_name, sl.commission_bps, a.allocation_status
             FROM reconciliation_deltas d
             JOIN seller_allocations a ON a.dataset_id=%(ds)s AND a.allocation_id=d.subject_id
             JOIN sellers sl ON sl.dataset_id=%(ds)s AND sl.seller_id=a.seller_id
             WHERE d.run_id=%(run)s AND d.delta_kind='D4_PAYOUT'"""
    p = {"run": ctx["run_id"], "ds": ctx["dataset_id"], "lim": limit}
    if settlement_id:
        sql += " AND d.settlement_id=%(sid)s"
        p["sid"] = settlement_id
    if seller_id:
        sql += " AND a.seller_id=%(sel)s"
        p["sel"] = seller_id
    sql += " ORDER BY ABS(d.residual_paise) DESC, d.subject_id LIMIT %(lim)s"
    rows = fetch(conn, sql, p)
    return {"count": len(rows),
            "payouts": [_row(r, ("expected_paise", "actual_paise", "delta_paise",
                                 "residual_paise")) for r in rows]}


def trace_money(conn, ctx, node_type: str, node_id: str) -> dict:
    """Follow the money edges up and down from any record: customer, order,
    payment, refund, allocation, transfer, settlement, bank line, ledger entry."""
    allowed = {"customer", "order", "payment", "refund", "seller_allocation", "transfer",
               "adjustment", "settlement_item", "settlement", "bank_transaction", "ledger_entry"}
    if node_type not in allowed:
        return {"error": f"node_type must be one of {sorted(allowed)}"}
    r = trace(conn, ctx["dataset_id"], node_type, node_id)
    up, down = r.get("upstream", []), r.get("downstream", [])
    cap = 30   # lineage fans out fast; the model needs the shape, not every edge
    return {"upstream_total": len(up), "downstream_total": len(down),
            "upstream": [_jsonable(e) for e in up[:cap]],
            "downstream": [_jsonable(e) for e in down[:cap]],
            "truncated": len(up) > cap or len(down) > cap}


def get_policy(conn, ctx) -> dict:
    """The policy registry the engine computed against. Every rate cited in an
    answer must come from here, not from general knowledge of Indian payments."""
    p = load_policy()
    return {
        "version": p.version, "config_hash": p.config_hash,
        "mdr_bps_by_method": dict(p._mdr),
        "gst_on_fee_bps": p.gst_on_fee_bps,
        "tax_computation": p.tax_computation,
        "rounding_mode": p.rounding_mode,
        "refund_window_days": p.refund_window_days,
        "mdr_refunded_on_refund": p.mdr_refunded,
        "settlement_cycle_working_days": p.cycle_working_days,
        "expected_bank_lag_days": p.expected_lag_days,
        "bank_tolerance_days": p.bank_tolerance_days,
        "amount_tolerance_paise": p.amount_tolerance_paise,
        "commission_bps_by_seller_type": dict(p._commission),
        "disclaimer": "Demo Merchant Policy -- a synthetic policy file authored for this "
                      "project. NOT Razorpay's actual commercial terms. Never present these "
                      "rates as real Razorpay pricing.",
    }


def get_audit(conn, ctx, settlement_id: str, limit: int = 30) -> dict:
    """The engine's own decision log for this settlement: what it did, on which
    rule, and what it concluded."""
    limit = _clamp(limit, 1, MAX_ROWS, 30)
    if not _settlement_exists(conn, ctx, settlement_id):
        return {"error": f"no settlement {settlement_id} in this run's dataset"}
    rows = fetch(conn, """SELECT audit_id, ts, actor, action, subject_type, subject_id,
                                 rule_ids, decision, tier, inputs, outputs
                          FROM audit_log WHERE run_id=%s AND subject_id=%s
                          ORDER BY audit_id LIMIT %s""",
                 (ctx["run_id"], settlement_id, limit))
    return {"settlement_id": settlement_id, "count": len(rows),
            "audit": [_jsonable(r) for r in rows]}


def get_cash_forecast(conn, ctx, horizon_working_days: int = 15,
                      bucket: str | None = None, limit: int = DEFAULT_ROWS) -> dict:
    """The forward cash position for this run: what is due in, what is due out,
    day by day over the next working days, plus everything already overdue.

    Every date is derived from the working-day calendar and the policy registry,
    never predicted. The forecast reads the matcher's D2 verdict, so a settlement
    the engine matched on non-UTR evidence is NOT counted as money in flight.
    Use `bucket` to see the individual dated lines behind a total:
    settlement_awaited (credits due from Razorpay), pipeline (captured payments
    not yet itemised into a settlement), seller_payout (marketplace transfers due).
    """
    from datetime import date as _date

    from engine import forecast as fcast

    horizon = _clamp(horizon_working_days, 1, 60, 15)
    limit = _clamp(limit, 1, MAX_ROWS, DEFAULT_ROWS)
    f = fcast.build(conn, ctx["run_id"], ctx["dataset_id"], None, horizon)
    d = fcast.to_dict(f)
    t = d["totals"]

    money = ("inflow_paise", "outflow_paise", "net_paise", "settlement_awaited_paise",
             "pipeline_paise", "seller_payout_paise", "overdue_paise",
             "overdue_payout_paise")
    totals = dict(t)
    totals.update({k.replace("_paise", "_rupees"): rupees(t[k]) for k in money if k in t})

    days = [{**day, "in_rupees": rupees(day["in_paise"]),
             "out_rupees": rupees(day["out_paise"]),
             "running_rupees": rupees(day["running_paise"])} for day in d["days"]]

    lines = d["lines"]
    if bucket:
        lines = [l for l in lines if l["bucket"] == bucket]
    matching = len(lines)
    lines = [{**l, "amount_rupees": rupees(l["amount_paise"])} for l in lines[:limit]]

    cap = min(limit, 20)
    od = [{**o, "amount_rupees": rupees(o["amount_paise"])} for o in d["overdue"][:cap]]
    odp = [{**o, "amount_rupees": rupees(o["amount_paise"])}
           for o in d["overdue_payouts"][:cap]]

    return {
        "as_of": d["as_of"],
        "as_of_basis": "the day after the last settlement period in this dataset closed",
        "horizon_working_days": d["horizon_working_days"],
        "totals": totals,
        "days": days,
        "lines": lines,
        "lines_shown": len(lines),
        "lines_matching": matching,
        "lines_in_window": t.get("lines_in_window", 0),
        "lines_truncated": matching > len(lines),
        "overdue_credits": od,
        "overdue_payouts": odp,
        "overdue_truncated": len(d["overdue"]) > len(od) or len(d["overdue_payouts"]) > len(odp),
        "assumptions": d["assumptions"],
        "caveat": "A derived schedule of money already owed under the policy, not a "
                  "prediction of future trading. It contains no forecast of sales.",
    }


# =============================================================================
# Registry. The schema list is what the model sees; HANDLERS is what runs.
# A name absent from HANDLERS can never execute, whatever the model asks for.
# =============================================================================
HANDLERS = {
    "run_overview": run_overview,
    "list_settlements": list_settlements,
    "get_settlement": get_settlement,
    "get_evidence": get_evidence,
    "list_exceptions": list_exceptions,
    "get_matcher_trail": get_matcher_trail,
    "get_payments": get_payments,
    "get_ledger": get_ledger,
    "get_seller_payouts": get_seller_payouts,
    "trace_money": trace_money,
    "get_policy": get_policy,
    "get_audit": get_audit,
    "get_cash_forecast": get_cash_forecast,
}

_S = lambda **kw: {"type": "string", **kw}
_I = lambda **kw: {"type": "integer", **kw}


def _tool(name: str, props: dict | None = None, required: list | None = None) -> dict:
    return {"type": "function", "function": {
        "name": name,
        "description": (HANDLERS[name].__doc__ or "").strip(),
        "parameters": {"type": "object", "properties": props or {},
                       "required": required or []}}}


SCHEMAS = [
    _tool("run_overview"),
    _tool("list_settlements", {
        "tier": _S(enum=["A", "B", "C"], description="A auto-resolved, B needs review, C unresolved"),
        "delta_kind": _S(enum=["D1_COMPUTE", "D2_BANK", "D3_LEDGER", "D4_PAYOUT"]),
        "min_unexplained_paise": _I(description="default 1, i.e. only settlements with a residue"),
        "limit": _I(description="max 60")}),
    _tool("get_settlement", {"settlement_id": _S(description="e.g. SET_0099")}, ["settlement_id"]),
    _tool("get_evidence", {"settlement_id": _S(),
                           "delta_kind": _S(enum=["D1_COMPUTE", "D2_BANK", "D3_LEDGER",
                                                  "D4_PAYOUT"])}, ["settlement_id"]),
    _tool("list_exceptions", {
        "status": _S(enum=["AUTO_RESOLVED", "NEEDS_REVIEW", "UNRESOLVED"]),
        "severity": _S(enum=["LOW", "MEDIUM", "HIGH"]),
        "delta_kind": _S(enum=["D1_COMPUTE", "D2_BANK", "D3_LEDGER", "D4_PAYOUT"]),
        "limit": _I()}),
    _tool("get_matcher_trail", {"settlement_id": _S()}, ["settlement_id"]),
    _tool("get_payments", {"settlement_id": _S(), "limit": _I()}, ["settlement_id"]),
    _tool("get_ledger", {"settlement_id": _S(), "limit": _I()}, ["settlement_id"]),
    _tool("get_seller_payouts", {"settlement_id": _S(), "seller_id": _S(), "limit": _I()}),
    _tool("trace_money", {"node_type": _S(), "node_id": _S()}, ["node_type", "node_id"]),
    _tool("get_policy"),
    _tool("get_audit", {"settlement_id": _S(), "limit": _I()}, ["settlement_id"]),
    _tool("get_cash_forecast", {
        "horizon_working_days": _I(description="1-60, default 15"),
        "bucket": _S(enum=["settlement_awaited", "pipeline", "seller_payout"],
                     description="restrict the returned lines to one bucket"),
        "limit": _I(description="max lines to return, max 60")}),
]


def dispatch(conn, ctx: dict, name: str, arguments: dict) -> dict:
    """Run one tool. Unknown names and bad arguments come back as data the model
    can react to, never as an exception that kills the conversation."""
    fn = HANDLERS.get(name)
    if fn is None:
        return {"error": f"unknown tool {name!r}. Available: {sorted(HANDLERS)}"}
    if not isinstance(arguments, dict):
        return {"error": "arguments must be an object"}
    clean = {k: v for k, v in arguments.items() if isinstance(k, str)}
    try:
        return fn(conn, ctx, **clean)
    except TypeError as e:
        return {"error": f"bad arguments for {name}: {e}"}
    except Exception as e:                      # noqa: BLE001 - surfaced to the model
        return {"error": f"{name} failed: {type(e).__name__}: {e}"}
