"""
FastAPI app. One process: JSON API plus the static SPA.

Every endpoint is read-only over persisted results except POST /api/datasets
and POST /api/runs. No LLM call anywhere -- P0 is deterministic by design.
"""
from __future__ import annotations

import csv
import io
import json
import os
import pathlib

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from api.schemas import DISCLAIMER, money, waterfall
from engine import runner
from engine.calculation import compute_d1
from engine.db import fetch, fetch_one, tx
from engine.invariants import check as check_invariants
from engine.lineage import trace
from engine.loader import load
from engine.money import rupees
from engine.policy import load_policy

STATIC = pathlib.Path(__file__).parent / "static"
app = FastAPI(title="AI Finance Controller (P0)", version=runner.ENGINE_VERSION)


def _rows(sql, params=()):
    with tx() as conn:
        return fetch(conn, sql, params)


def _one(sql, params=()):
    with tx() as conn:
        return fetch_one(conn, sql, params)


# ------------------------------------------------------------------- meta ---
@app.get("/api/health")
def health():
    p = load_policy()
    return {"status": "ok", "engine_version": runner.ENGINE_VERSION,
            "policy_version": p.version, "config_hash": p.config_hash,
            "demo_policy_label": DISCLAIMER}


@app.get("/api/policy")
def policy():
    p = load_policy()
    return {"version": p.version, "config_hash": p.config_hash, "label": DISCLAIMER,
            "raw": p.raw}


# --------------------------------------------------------------- datasets ---
class GenerateRequest(BaseModel):
    seed: int = 42
    settlements: int = 100
    label: str | None = "demo"
    clean: bool = False


@app.get("/api/datasets")
def datasets():
    return _rows("SELECT d.*, (SELECT count(*) FROM ground_truth_anomalies g "
                 "WHERE g.dataset_id=d.dataset_id) AS planted "
                 "FROM datasets d ORDER BY generated_at DESC")


@app.post("/api/datasets")
def generate(req: GenerateRequest):
    from generator.generate import build, persist
    ds = build(req.seed, req.settlements, load_policy(), req.label, with_anomalies=not req.clean)
    with tx() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM datasets WHERE dataset_id=%s", (ds.dataset_id,))
        counts = persist(ds, conn)
    return {"dataset_id": ds.dataset_id, "seed": req.seed, "row_counts": counts,
            "planted_anomalies": len(ds.ground_truth)}


# ------------------------------------------------------------------- runs ---
class RunRequest(BaseModel):
    dataset_id: str


@app.get("/api/runs")
def runs():
    return _rows("""SELECT r.run_id, r.dataset_id, r.policy_version, r.engine_version,
                           r.config_hash, r.started_at, r.finished_at, r.status, d.label, d.seed
                    FROM reconciliation_runs r JOIN datasets d USING (dataset_id)
                    ORDER BY r.started_at DESC""")


@app.post("/api/runs")
def create_run(req: RunRequest):
    with tx() as conn:
        m = runner.run(conn, req.dataset_id)
    return m


class TickRequest(BaseModel):
    settlements: int = 10
    clean: bool = False
    reconcile: bool = True


@app.post("/api/datasets/{dataset_id}/tick")
def tick(dataset_id: str, req: TickRequest):
    """Advance the book by one settlement cycle, then re-reconcile all of it.

    This is the difference between a snapshot and a ledger that is still being
    written. New payments, refunds, allocations and bank lines are appended to
    the SAME dataset -- including refunds against payments that settled cycles
    ago, and the bank credit for last cycle's final settlement, which was still
    in flight when that cycle closed. The engine then re-runs over everything
    and mints a new immutable run.
    """
    from generator.append import tick as run_tick
    if req.settlements < 1 or req.settlements > 200:
        raise HTTPException(400, "settlements must be between 1 and 200")
    try:
        with tx() as conn:
            return run_tick(conn, dataset_id, req.settlements, req.clean, req.reconcile)
    except ValueError as e:
        raise HTTPException(404, str(e))


@app.get("/api/datasets/{dataset_id}/batches")
def batches(dataset_id: str):
    """The batch log: what each tick added, and what it left in flight."""
    row = _one("SELECT row_counts FROM datasets WHERE dataset_id=%s", (dataset_id,))
    if not row:
        raise HTTPException(404, "no such dataset")
    counts = row["row_counts"] or {}
    if isinstance(counts, str):
        import json as _json
        counts = _json.loads(counts)
    return {"dataset_id": dataset_id, "batches": counts.get("batches", []),
            "cumulative": {k: v for k, v in counts.items() if k != "batches"}}


@app.get("/api/runs/{run_id}/metrics")
def metrics(run_id: str):
    row = _one("SELECT metrics FROM reconciliation_runs WHERE run_id=%s", (run_id,))
    if not row or not row["metrics"]:
        raise HTTPException(404, "run not found or still running")
    return row["metrics"]


def _dataset_of(run_id: str) -> str:
    row = _one("SELECT dataset_id FROM reconciliation_runs WHERE run_id=%s", (run_id,))
    if not row:
        raise HTTPException(404, "run not found")
    return str(row["dataset_id"])


# ------------------------------------------------------------ settlements ---
SETTLEMENT_SQL = """
SELECT s.settlement_id, s.settlement_date, s.settlement_period_start, s.settlement_period_end,
       s.gross_amount_paise, s.refund_amount_paise, s.fee_amount_paise, s.tax_amount_paise,
       s.adjustment_amount_paise, s.net_settlement_amount_paise, s.settlement_status,
       s.settlement_utr,
       d1.expected_paise AS expected_net_paise, d1.delta_paise AS d1_paise, d1.tier AS d1_tier,
       d1.residual_paise AS d1_residual, d1.status AS d1_status,
       d2.actual_paise AS bank_paise, d2.delta_paise AS d2_paise, d2.tier AS d2_tier,
       d2.residual_paise AS d2_residual, d2.status AS d2_status,
       d3.delta_paise AS d3_paise, d3.tier AS d3_tier, d3.residual_paise AS d3_residual,
       COALESCE(d4.worst_tier, 'A') AS d4_tier, COALESCE(d4.d4_residual, 0) AS d4_residual,
       COALESCE(d4.d4_paise, 0) AS d4_paise, COALESCE(d4.n_allocations, 0) AS d4_allocations,
       sp.payment_id AS sample_payment_id
FROM settlements s
LEFT JOIN LATERAL (
    SELECT si.payment_id FROM settlement_items si
    WHERE si.dataset_id = s.dataset_id AND si.settlement_id = s.settlement_id
      AND si.transaction_type = 'PAYMENT'
    ORDER BY si.amount_paise DESC LIMIT 1) sp ON TRUE
LEFT JOIN reconciliation_deltas d1 ON d1.run_id=%(run)s AND d1.settlement_id=s.settlement_id
     AND d1.delta_kind='D1_COMPUTE'
LEFT JOIN reconciliation_deltas d2 ON d2.run_id=%(run)s AND d2.settlement_id=s.settlement_id
     AND d2.delta_kind='D2_BANK'
LEFT JOIN reconciliation_deltas d3 ON d3.run_id=%(run)s AND d3.settlement_id=s.settlement_id
     AND d3.delta_kind='D3_LEDGER'
LEFT JOIN (
    SELECT settlement_id, MAX(tier) AS worst_tier, SUM(ABS(residual_paise)) AS d4_residual,
           SUM(ABS(delta_paise)) AS d4_paise, COUNT(*) AS n_allocations
    FROM reconciliation_deltas WHERE run_id=%(run)s AND delta_kind='D4_PAYOUT'
    GROUP BY settlement_id) d4 ON d4.settlement_id = s.settlement_id
WHERE s.dataset_id=%(ds)s
ORDER BY s.settlement_id
"""


@app.get("/api/runs/{run_id}/settlements")
def settlements(run_id: str):
    ds = _dataset_of(run_id)
    rows = _rows(SETTLEMENT_SQL, {"run": run_id, "ds": ds})
    for r in rows:
        r["worst_tier"] = max(r["d1_tier"] or "A", r["d2_tier"] or "A",
                              r["d3_tier"] or "A", r["d4_tier"] or "A")
        r["unexplained_paise"] = sum(abs(r[k] or 0) for k in
                                     ("d1_residual", "d2_residual", "d3_residual", "d4_residual"))
    return {"demo_policy_label": DISCLAIMER, "settlements": rows}


@app.get("/api/runs/{run_id}/settlements/{settlement_id}")
def settlement_detail(run_id: str, settlement_id: str):
    ds = _dataset_of(run_id)
    policy = load_policy()
    with tx() as conn:
        snap = load(conn, ds)
        if settlement_id not in snap.settlement_by_id:
            raise HTTPException(404, "settlement not found")
        s = snap.settlement_by_id[settlement_id]
        inv = check_invariants(snap)
        d1 = compute_d1(snap, s, policy, inv.excluded_items)
        deltas = fetch(conn, "SELECT * FROM reconciliation_deltas WHERE run_id=%s AND "
                             "settlement_id=%s ORDER BY delta_kind, delta_id",
                       (run_id, settlement_id))
        ids = [d["delta_id"] for d in deltas]
        attrs = fetch(conn, "SELECT * FROM attributions WHERE run_id=%s AND delta_id = ANY(%s) "
                            "ORDER BY attribution_id", (run_id, ids)) if ids else []
        excs = fetch(conn, "SELECT * FROM exceptions WHERE run_id=%s AND settlement_id=%s "
                           "ORDER BY exception_id", (run_id, settlement_id))
        cands = fetch(conn, "SELECT * FROM match_candidates WHERE run_id=%s AND settlement_id=%s "
                            "ORDER BY pass_name", (run_id, settlement_id))
        audit = fetch(conn, "SELECT * FROM audit_log WHERE run_id=%s AND subject_id=%s "
                            "ORDER BY audit_id", (run_id, settlement_id))
        gt = fetch(conn, "SELECT * FROM ground_truth_anomalies WHERE dataset_id=%s AND "
                         "settlement_id=%s ORDER BY anomaly_id", (ds, settlement_id))

        items = snap.items_by_settlement.get(settlement_id, [])
        pay_ids = [i["payment_id"] for i in items if i["transaction_type"] == "PAYMENT"]
        payments = []
        for i in items:
            if i["transaction_type"] != "PAYMENT":
                continue
            p = snap.payments.get(i["payment_id"], {})
            cf, chf, ct, cht, method, amt = d1.fee_by_payment.get(
                i["payment_id"], (0, i["fee_paise"], 0, i["tax_paise"],
                                  p.get("payment_method"), i["amount_paise"]))
            payments.append({**{k: p.get(k) for k in
                                ("payment_id", "order_id", "customer_id", "amount_paise",
                                 "payment_method", "payment_status", "captured_at")},
                             "policy_fee_paise": cf, "charged_fee_paise": chf,
                             "policy_tax_paise": ct, "charged_tax_paise": cht,
                             "mdr_bps": policy.mdr_bps(p["payment_method"]) if p else None,
                             "settlement_item_id": i["settlement_item_id"]})
        allocs = []
        d4_by_alloc = {d["subject_id"]: d for d in deltas if d["delta_kind"] == "D4_PAYOUT"}
        for pid in pay_ids:
            for a in snap.allocations_by_payment.get(pid, []):
                moved = snap.transfers_by_ps.get((a["payment_id"], a["seller_id"]), [])
                d = d4_by_alloc.get(a["allocation_id"])
                allocs.append({**a, "seller_name": snap.sellers.get(a["seller_id"], {}).get("seller_name"),
                               "seller_type": snap.sellers.get(a["seller_id"], {}).get("seller_type"),
                               "transfers": moved,
                               "paid_paise": sum(t["amount_paise"] for t in moved
                                                 if t["transfer_status"] == "PROCESSED"),
                               "d4_paise": d["delta_paise"] if d else 0,
                               "d4_tier": d["tier"] if d else "A",
                               "d4_residual": d["residual_paise"] if d else 0,
                               "delta_id": d["delta_id"] if d else None})
        bank_ids = [c["bank_transaction_id"] for c in cands if c["is_selected"]]
        bank = [snap.bank_by_id[b] for b in bank_ids if b in snap.bank_by_id]
        d2row = next((d for d in deltas if d["delta_kind"] == "D2_BANK"), None)

        wf = waterfall({
            "gross_paise": d1.gross_paise, "refunds_paise": d1.source_refunds_paise,
            "computed_fee_paise": d1.computed_fee_paise, "computed_tax_paise": d1.computed_tax_paise,
            "adjustments_paise": d1.item_adjustments_paise,
            "expected_net_paise": d1.expected_net_paise, "actual_net_paise": d1.actual_net_paise,
            "bank_paise": (d2row["actual_paise"] if d2row else 0),
            "residual_paise": sum(abs(d["residual_paise"]) for d in deltas)})

        ledger = snap.ledger_by_settlement.get(settlement_id, [])
        return {
            "demo_policy_label": DISCLAIMER,
            "settlement": s, "waterfall": wf, "deltas": deltas, "attributions": attrs,
            "exceptions": excs, "match_candidates": cands, "audit": audit,
            "ground_truth": gt,
            "d1": {"gross_paise": d1.gross_paise, "source_refunds_paise": d1.source_refunds_paise,
                   "item_refunds_paise": d1.item_refunds_paise,
                   "computed_fee_paise": d1.computed_fee_paise,
                   "charged_fee_paise": d1.charged_fee_paise,
                   "computed_tax_paise": d1.computed_tax_paise,
                   "charged_tax_paise": d1.charged_tax_paise,
                   "aggregate_tax_paise": d1.aggregate_tax_paise,
                   "item_adjustments_paise": d1.item_adjustments_paise,
                   "expected_net_paise": d1.expected_net_paise,
                   "actual_net_paise": d1.actual_net_paise, "delta_paise": d1.delta_paise},
            "payments": payments,
            "refunds": [r for r in snap.refunds
                        if s["settlement_period_start"] <= r["refund_date"] <= s["settlement_period_end"]
                        or r["payment_id"] in pay_ids],
            "itemised_refund_ids": sorted(d1.itemised_refund_ids),
            "allocations": allocs,
            "adjustments": snap.adjustments_by_settlement.get(settlement_id, []),
            "itemised_adjustment_ids": sorted(d1.itemised_adjustment_ids),
            "bank": bank,
            "items": items,
            "ledger": ledger,
        }


# ------------------------------------------------------------- exceptions ---
@app.get("/api/runs/{run_id}/exceptions")
def exceptions(run_id: str, status: str | None = None, severity: str | None = None,
               delta_kind: str | None = None, tier: str | None = None):
    sql = "SELECT * FROM exceptions WHERE run_id=%s"
    params: list = [run_id]
    for col, val in (("status", status), ("severity", severity),
                     ("delta_kind", delta_kind), ("tier", tier)):
        if val:
            sql += f" AND {col}=%s"
            params.append(val)
    rows = _rows(sql + " ORDER BY (severity='HIGH') DESC, unexplained_paise DESC, exception_id",
                 tuple(params))
    ids = [r["exception_id"] for r in rows]
    return {"demo_policy_label": DISCLAIMER, "exceptions": rows,
            "total_unexplained_paise": sum(r["unexplained_paise"] for r in rows)}


@app.get("/api/runs/{run_id}/exceptions/{exception_id}/evidence")
def exception_evidence(run_id: str, exception_id: str):
    e = _one("SELECT * FROM exceptions WHERE run_id=%s AND exception_id=%s", (run_id, exception_id))
    if not e:
        raise HTTPException(404, "exception not found")
    key = e["subject_id"] or e["settlement_id"]
    attrs = _rows("""SELECT a.* FROM attributions a JOIN reconciliation_deltas d
                     ON d.run_id=a.run_id AND d.delta_id=a.delta_id
                     WHERE a.run_id=%s AND d.settlement_id=%s AND d.delta_kind=%s
                     AND (d.subject_id IS NOT DISTINCT FROM %s)""",
                  (run_id, e["settlement_id"], e["delta_kind"], e["subject_id"]))
    return {"exception": e, "attributions": attrs}


# ---------------------------------------------------------------- sellers ---
@app.get("/api/runs/{run_id}/sellers")
def sellers(run_id: str):
    ds = _dataset_of(run_id)
    rows = _rows("""
        SELECT sl.seller_id, sl.seller_name, sl.seller_type, sl.commission_bps, sl.status,
               COUNT(*) FILTER (WHERE a.allocation_status='SETTLED') AS settled_allocations,
               COALESCE(SUM(a.net_seller_paise) FILTER (WHERE a.allocation_status='SETTLED'),0)
                   AS owed_paise,
               COALESCE(SUM(d.actual_paise),0) AS paid_paise,
               COALESCE(SUM(ABS(d.residual_paise)),0) AS unexplained_paise,
               COUNT(*) FILTER (WHERE d.tier='C') AS unresolved_count
        FROM sellers sl
        LEFT JOIN seller_allocations a ON a.dataset_id=sl.dataset_id AND a.seller_id=sl.seller_id
        LEFT JOIN reconciliation_deltas d ON d.run_id=%s AND d.subject_id=a.allocation_id
                                        AND d.delta_kind='D4_PAYOUT'
        WHERE sl.dataset_id=%s
        GROUP BY sl.seller_id, sl.seller_name, sl.seller_type, sl.commission_bps, sl.status
        ORDER BY unexplained_paise DESC, sl.seller_id""", (run_id, ds))
    return {"demo_policy_label": DISCLAIMER, "sellers": rows}


# ------------------------------------------------------------------ trace ---
@app.get("/api/runs/{run_id}/trace")
def trace_money(run_id: str, node_type: str = Query(...), node_id: str = Query(...)):
    ds = _dataset_of(run_id)
    with tx() as conn:
        result = trace(conn, ds, node_type, node_id)
        snap_rows = {}
        wanted: dict[str, set] = {}
        for e in result["downstream"] + result["upstream"]:
            wanted.setdefault(e["src_type"], set()).add(e["src_id"])
            wanted.setdefault(e["dst_type"], set()).add(e["dst_id"])
        wanted.setdefault(node_type, set()).add(node_id)
        TABLES = {"customer": ("customers", "customer_id"), "order": ("orders", "order_id"),
                  "payment": ("payments", "payment_id"), "refund": ("refunds", "refund_id"),
                  "seller_allocation": ("seller_allocations", "allocation_id"),
                  "transfer": ("transfers", "transfer_id"),
                  "adjustment": ("adjustments", "adjustment_id"),
                  "settlement_item": ("settlement_items", "settlement_item_id"),
                  "settlement": ("settlements", "settlement_id"),
                  "bank_transaction": ("bank_transactions", "bank_transaction_id"),
                  "ledger_entry": ("ledger_entries", "ledger_entry_id")}
        for t, ids in wanted.items():
            if t not in TABLES or not ids:
                continue
            table, col = TABLES[t]
            for row in fetch(conn, f"SELECT * FROM {table} WHERE dataset_id=%s AND {col} = ANY(%s)",
                             (ds, list(ids))):
                snap_rows[f"{t}:{row[col]}"] = row
    result["nodes"] = snap_rows
    result["demo_policy_label"] = DISCLAIMER
    return result


# --------------------------------------------------------------- fixtures ---
@app.get("/api/fixtures/evaluation-batch")
def evaluation_batch_info():
    """What the static batch contains, without loading it."""
    from fixtures.loader import load_batch
    b = load_batch()
    return {"batch_id": b["batch_id"], "dataset_id": b["dataset_id"], "title": b["title"],
            "policy_version": b["policy_version"], "how_to_read": b["how_to_read"],
            "scenario_count": b["scenario_count"],
            "families": sorted({s["family"] for s in b["scenarios"]}),
            "scenarios": [{k: s[k] for k in ("scenario_id", "title", "family", "note")}
                          | {"settlement_id": s["settlement"]["settlement_id"],
                             "expected": s["expected"]}
                          for s in b["scenarios"]]}


@app.post("/api/fixtures/evaluation-batch")
def load_evaluation_batch():
    """Load the static evaluation batch and reconcile it.

    Fixed, hand-authored, no seed and no randomness: the same rows every time,
    with each scenario's expected outcome stated in the file. Loading it twice
    replaces it rather than accumulating copies, because its dataset_id is a
    constant.
    """
    from fixtures.loader import load
    with tx() as conn:
        out = load(conn)
        m = runner.run(conn, out["dataset_id"])
    return {"dataset_id": out["dataset_id"], "row_counts": out["row_counts"], "run": m}


# ------------------------------------------------------------------- data ---
@app.get("/api/runs/{run_id}/tables")
def tables(run_id: str):
    """Live row counts for every table, scoped to this run and its dataset."""
    from api import browse
    ds = _dataset_of(run_id)
    with tx() as conn:
        return {"run_id": run_id, "dataset_id": ds,
                "tables": browse.summary(conn, run_id, ds)}


@app.get("/api/runs/{run_id}/tables/{table}")
def table_rows(run_id: str, table: str, limit: int = 50, offset: int = 0):
    """One page of real rows from one table. Read-only."""
    from api import browse
    ds = _dataset_of(run_id)
    with tx() as conn:
        out = browse.page(conn, run_id, ds, table, limit, offset)
    if "error" in out:
        raise HTTPException(404, out["error"])
    return out


# ------------------------------------------------------------------ agent ---
class AskRequest(BaseModel):
    question: str
    use_history: bool = True


@app.get("/api/agent/status")
def agent_status():
    """Whether a provider key is configured, and the suggested questions. The UI
    calls this to decide between offering the panel and explaining what is missing."""
    from agent.investigator import SUGGESTED_QUESTIONS
    from agent.llm import status as llm_status
    return {**llm_status(), "suggested_questions": SUGGESTED_QUESTIONS,
            "scope": "read-only over persisted results; the agent cannot change any "
                     "number, tier or status"}


@app.post("/api/runs/{run_id}/ask")
def ask(run_id: str, req: AskRequest):
    """Ask the investigation agent about this run.

    The agent gets bounded read-only tools over results the engine already
    computed. It never recomputes anything, and there is no code path from here
    to a write on any reconciliation table.
    """
    from agent import store
    from agent.investigator import investigate
    from agent.llm import LLMUnavailable

    question = (req.question or "").strip()
    if not question:
        raise HTTPException(400, "question is required")
    if len(question) > 2000:
        raise HTTPException(400, "question is too long (2000 characters max)")
    ds = _dataset_of(run_id)

    with tx() as conn:
        history = store.history_for_prompt(conn, run_id) if req.use_history else []
        try:
            result = investigate(conn, run_id, ds, question, history)
        except LLMUnavailable as e:
            # A missing or unreachable key must never look like a broken engine.
            raise HTTPException(503, str(e))
        turn = store.record(conn, run_id, question, result)
    return {"turn_id": turn, "question": question, **result}


@app.get("/api/runs/{run_id}/conversation")
def conversation(run_id: str):
    """The persisted transcript for this run.

    Never fails the request. The agent is additive to a system that already
    works, and this endpoint is called every time a run is selected -- so a
    problem in the agent's own storage must degrade to an empty conversation,
    not take a reconciliation screen down with it.
    """
    from agent import store
    try:
        with tx() as conn:
            return {"run_id": run_id, "turns": store.conversation(conn, run_id),
                    "storage_ready": store.table_ready(conn)}
    except Exception:                               # noqa: BLE001 - degraded, not fatal
        return {"run_id": run_id, "turns": [], "storage_ready": False}


@app.get("/api/runs/{run_id}/audit")
def audit(run_id: str, limit: int = 500, offset: int = 0):
    return _rows("SELECT * FROM audit_log WHERE run_id=%s ORDER BY audit_id LIMIT %s OFFSET %s",
                 (run_id, limit, offset))


# ----------------------------------------------------------------- export ---
EXPORT_COLUMNS = ["settlement_id", "delta_kind", "subject_id", "expected_paise", "actual_paise",
                  "delta_paise", "explained_paise", "residual_paise", "tier", "status"]


@app.get("/api/runs/{run_id}/export.csv")
def export_csv(run_id: str):
    rows = _rows("SELECT * FROM reconciliation_deltas WHERE run_id=%s ORDER BY settlement_id, "
                 "delta_kind, delta_id", (run_id,))
    excs = _rows("SELECT * FROM exceptions WHERE run_id=%s ORDER BY exception_id", (run_id,))
    m = _one("SELECT metrics FROM reconciliation_runs WHERE run_id=%s", (run_id,))["metrics"]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["# AI Finance Controller reconciliation report"])
    w.writerow([f"# {DISCLAIMER}"])
    w.writerow([f"# run_id={run_id} policy={m['policy_version']} engine_config={m['config_hash']}"])
    w.writerow([f"# {m['throughput']['headline']}"])
    w.writerow([f"# amount at risk = {m['accuracy']['amount_at_risk_display']}"])
    w.writerow([])
    w.writerow(["SECTION", "DELTAS"])
    w.writerow(EXPORT_COLUMNS + ["delta_rupees", "residual_rupees"])
    for r in rows:
        w.writerow([r[c] for c in EXPORT_COLUMNS] +
                   [rupees(r["delta_paise"]), rupees(r["residual_paise"])])
    w.writerow([])
    w.writerow(["SECTION", "EXCEPTIONS"])
    cols = ["exception_id", "settlement_id", "subject_id", "delta_kind", "exception_type",
            "severity", "amount_paise", "explained_paise", "unexplained_paise", "tier", "status",
            "recommended_action"]
    w.writerow(cols + ["unexplained_rupees"])
    for e in excs:
        w.writerow([e[c] for c in cols] + [rupees(e["unexplained_paise"])])
    buf.seek(0)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition":
                                      f'attachment; filename="reconciliation_{run_id[:8]}.csv"'})


@app.get("/api/runs/{run_id}/export.json")
def export_json(run_id: str):
    m = _one("SELECT * FROM reconciliation_runs WHERE run_id=%s", (run_id,))
    if not m:
        raise HTTPException(404, "run not found")
    payload = {
        "demo_policy_label": DISCLAIMER,
        "run": {k: m[k] for k in ("run_id", "dataset_id", "policy_version", "engine_version",
                                  "config_hash", "started_at", "finished_at", "status")},
        "metrics": m["metrics"],
        "deltas": _rows("SELECT * FROM reconciliation_deltas WHERE run_id=%s ORDER BY delta_id",
                        (run_id,)),
        "attributions": _rows("SELECT * FROM attributions WHERE run_id=%s ORDER BY attribution_id",
                              (run_id,)),
        "exceptions": _rows("SELECT * FROM exceptions WHERE run_id=%s ORDER BY exception_id",
                            (run_id,)),
    }
    return JSONResponse(json.loads(json.dumps(payload, default=str)),
                        headers={"Content-Disposition":
                                 f'attachment; filename="reconciliation_{run_id[:8]}.json"'})


# ------------------------------------------------------------------- SPA ----
if STATIC.exists():
    app.mount("/assets", StaticFiles(directory=STATIC), name="assets")


@app.get("/")
def index():
    f = STATIC / "index.html"
    if not f.exists():
        raise HTTPException(500, "SPA not built -- run web/build.sh")
    return FileResponse(f)


@app.get("/{path:path}")
def spa(path: str):
    f = STATIC / "index.html"
    if not f.exists():
        raise HTTPException(404, "not found")
    return FileResponse(f)
