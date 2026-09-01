"""
Append a new settlement cycle onto an existing dataset, then reconcile it.

This is what turns the project from a fixed snapshot into a book that grows.
A tick does three things:

  1. reads the continuation state out of Postgres (generator/origin.py),
  2. runs the SAME generation pipeline over a slice that continues it,
  3. persists only the new rows and re-runs the engine over everything.

Step 3 re-reconciles the WHOLE dataset rather than just the new settlements.
That is deliberate. A run is an immutable, complete picture of the book at a
point in time -- that property is what makes the audit trail worth trusting,
and loader.load() issues a fixed 10 queries regardless of size, so a full
re-run stays cheap far past the point where this matters.

    python -m generator.append --dataset <uuid> --settlements 10
"""
from __future__ import annotations

import argparse
import json
import time

from engine import runner
from engine.db import connect
from engine.policy import load_policy
from generator.generate import build, copy_entities, row_counts
from generator.origin import load_origin

# Money columns that are cumulative across batches; everything else in
# row_counts is a simple sum too, but these are the ones the UI reads.
_SUMMED = ("customers", "sellers", "orders", "payments", "refunds", "seller_allocations",
           "transfers", "adjustments", "settlements", "settlement_items",
           "bank_transactions", "ledger_entries", "money_edges", "ground_truth_anomalies",
           "total_financial_records")


def persist_append(ds, conn, origin) -> dict:
    """COPY the new slice and roll the dataset's row_counts forward.

    Sellers are NOT re-inserted -- the population carried over from the previous
    batch and the rows already exist. Everything else in `ds` is new by
    construction, because generate_clean only ever appends to empty lists.
    """
    batch_counts = row_counts(ds)
    copy_entities(ds, conn)

    prior = dict(origin.row_counts or {})
    batches = list(prior.get("batches", []))
    if not batches:
        # the dataset predates append mode: batch 1 is everything already there
        batches = [{"batch": 1, "settlements": prior.get("settlements", 0),
                    "records": prior.get("total_financial_records", 0),
                    "withheld_bank": []}]
    batches.append({
        "batch": origin.batch,
        "settlements": batch_counts["settlements"],
        "records": batch_counts["total_financial_records"],
        "anomalies": batch_counts["ground_truth_anomalies"],
        "withheld_bank": list(getattr(ds, "withheld_bank", [])),
        "late_refunds": sum(1 for r in ds.refunds if r.get("_late")),
        "period_start": str(ds.settlements[0]["settlement_period_start"]),
        "period_end": str(ds.settlements[-1]["settlement_period_end"]),
    })

    merged = {k: prior.get(k, 0) + batch_counts.get(k, 0) for k in _SUMMED}
    merged["batches"] = batches
    with conn.cursor() as cur:
        cur.execute("UPDATE datasets SET row_counts=%s WHERE dataset_id=%s",
                    (json.dumps(merged), ds.dataset_id))
    conn.commit()
    return {"batch": batch_counts, "cumulative": merged}


def tick(conn, dataset_id: str, settlements: int = 10, clean: bool = False,
         reconcile: bool = True) -> dict:
    """One cycle: append `settlements` new settlement periods, then re-reconcile."""
    t0 = time.perf_counter()
    policy = load_policy()
    origin = load_origin(conn, dataset_id)
    ds = build(origin.seed, settlements, policy, origin.label,
               with_anomalies=not clean, origin=origin)
    counts = persist_append(ds, conn, origin)
    gen_s = time.perf_counter() - t0

    out = {
        "dataset_id": dataset_id,
        "batch": origin.batch,
        "appended": counts["batch"],
        "cumulative": {k: v for k, v in counts["cumulative"].items() if k != "batches"},
        "period_start": str(ds.settlements[0]["settlement_period_start"]),
        "period_end": str(ds.settlements[-1]["settlement_period_end"]),
        "late_refunds": sum(1 for r in ds.refunds if r.get("_late")),
        "bank_credits_resolved": len(origin.pending_bank),
        "bank_credits_in_flight": len(getattr(ds, "withheld_bank", [])),
        "generate_seconds": round(gen_s, 3),
    }
    if reconcile:
        t1 = time.perf_counter()
        out["run"] = runner.run(conn, dataset_id)
        out["reconcile_seconds"] = round(time.perf_counter() - t1, 3)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Append a settlement cycle and re-reconcile.")
    ap.add_argument("--dataset", type=str, default=None,
                    help="dataset_id to grow; defaults to the most recent one")
    ap.add_argument("--settlements", type=int, default=10)
    ap.add_argument("--clean", action="store_true", help="append data with no planted anomalies")
    ap.add_argument("--no-reconcile", action="store_true")
    args = ap.parse_args()

    with connect() as conn:
        ds_id = args.dataset
        if not ds_id:
            from engine.db import fetch_one
            # Newest GENERATED dataset. The Phase 0 fixtures live in these same
            # tables and are newer than the demo data right after a test run, so
            # "newest" alone would pick a three-row fixture.
            row = fetch_one(conn, """
                SELECT d.dataset_id FROM datasets d
                WHERE (SELECT count(*) FROM settlements x
                        WHERE x.dataset_id=d.dataset_id) > 1
                  AND (SELECT count(*) FROM sellers s
                        WHERE s.dataset_id=d.dataset_id AND s.status='ACTIVE') >= 2
                ORDER BY d.generated_at DESC LIMIT 1""")
            if not row:
                raise SystemExit("no generated dataset to append to -- "
                                 "run `make generate` (or generator.generate) first")
            ds_id = str(row["dataset_id"])
        out = tick(conn, ds_id, args.settlements, args.clean, not args.no_reconcile)

    b, c = out["appended"], out["cumulative"]
    print(f"dataset {out['dataset_id']}   batch {out['batch']}")
    print(f"  period            {out['period_start']} -> {out['period_end']}")
    print(f"  appended          {b['settlements']:,} settlements, "
          f"{b['total_financial_records']:,} records, {b['ground_truth_anomalies']} anomalies")
    print(f"  late refunds      {out['late_refunds']} against earlier settlements")
    print(f"  bank credits      {out['bank_credits_resolved']} landed, "
          f"{out['bank_credits_in_flight']} still in flight")
    print(f"  dataset now       {c['settlements']:,} settlements, "
          f"{c['total_financial_records']:,} records")
    print(f"  generated in      {out['generate_seconds']}s")
    if "run" in out:
        m = out["run"]
        print(f"  run               {m['run_id']}  reconciled in {out['reconcile_seconds']}s")


if __name__ == "__main__":
    main()
