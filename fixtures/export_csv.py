"""
Exports the static evaluation batch to CSV, one file per table.

    python -m fixtures.export_csv          # writes fixtures/csv/
    make evaluation-csv

The CSVs are dumped from the database AFTER the batch is loaded, so they are
provably the same rows the engine reconciles -- not a second rendering of the
JSON that could quietly drift from it. Loading is idempotent, so running this is
safe at any time.

Money stays in integer paise, exactly as stored. Converting to rupees here would
introduce a second representation of the same number and invite someone to
reconcile against the rounded one; 976400 means Rs 9,764.00.
"""
from __future__ import annotations

import argparse
import csv
import json
import pathlib

from engine.db import connect, fetch, fetch_one
from fixtures.loader import load, load_batch

OUT = pathlib.Path(__file__).resolve().parent / "csv"

# Dependency order, so the files read top-down the way the data was built.
TABLES = [
    ("customers", "customer_id"),
    ("sellers", "seller_id"),
    ("orders", "order_id"),
    ("payments", "payment_id"),
    ("refunds", "refund_id"),
    ("seller_allocations", "allocation_id"),
    ("transfers", "transfer_id"),
    ("adjustments", "adjustment_id"),
    ("settlements", "settlement_id"),
    ("settlement_items", "settlement_item_id"),
    ("bank_transactions", "bank_transaction_id"),
    ("ledger_entries", "ledger_entry_id"),
    ("money_edges", "src_id, dst_id, edge_kind"),
    ("ground_truth_anomalies", "anomaly_id"),
]


def _write(path: pathlib.Path, rows: list[dict], columns: list[str] | None = None) -> int:
    cols = columns or (list(rows[0]) if rows else [])
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({c: ("" if r.get(c) is None else r.get(c)) for c in cols})
    return len(rows)


def export(conn, out_dir: pathlib.Path = OUT) -> dict:
    batch = load_batch()
    ds = batch["dataset_id"]
    if fetch_one(conn, "SELECT 1 AS ok FROM datasets WHERE dataset_id=%s", (ds,)) is None:
        load(conn, batch)

    out_dir.mkdir(parents=True, exist_ok=True)
    written: dict[str, int] = {}

    for table, order in TABLES:
        rows = fetch(conn, f"SELECT * FROM {table} WHERE dataset_id=%s ORDER BY {order}", (ds,))
        # dataset_id is the same constant on every row; it is noise in a flat file
        for r in rows:
            r.pop("dataset_id", None)
        written[f"{table}.csv"] = _write(out_dir / f"{table}.csv", rows)

    # The scenario manifest: what each settlement is FOR, and what the engine is
    # expected to conclude about it. This is the file an evaluator opens first.
    manifest = []
    for s in batch["scenarios"]:
        e = s["expected"]
        manifest.append({
            "scenario_id": s["scenario_id"],
            "settlement_id": s["settlement"]["settlement_id"],
            "family": s["family"],
            "title": s["title"],
            "expected_d1_paise": e.get("d1_paise", ""),
            "expected_d2_paise": e.get("d2_paise", ""),
            "expected_d3_paise": e.get("d3_paise", ""),
            "expected_d4_paise": e.get("d4_paise", ""),
            "expected_worst_tier": e.get("worst_tier", ""),
            "expected_exception_types": "|".join(e.get("exception_types") or []),
            "period_start": s["settlement"]["settlement_period_start"],
            "period_end": s["settlement"]["settlement_period_end"],
            "settlement_date": s["settlement"]["settlement_date"],
            "note": " ".join(s["note"].split()),
        })
    written["scenarios.csv"] = _write(out_dir / "scenarios.csv", manifest, list(manifest[0]))

    counts = fetch_one(conn, "SELECT row_counts FROM datasets WHERE dataset_id=%s", (ds,))["row_counts"]
    if isinstance(counts, str):
        counts = json.loads(counts)
    return {"dataset_id": ds, "out_dir": str(out_dir), "files": written,
            "total_financial_records": counts.get("total_financial_records")}


def main() -> None:
    ap = argparse.ArgumentParser(description="Export the static evaluation batch as CSV.")
    ap.add_argument("--out", type=str, default=str(OUT))
    args = ap.parse_args()
    with connect() as conn:
        r = export(conn, pathlib.Path(args.out))
    print(f"dataset {r['dataset_id']}  ->  {r['out_dir']}")
    total = 0
    for name, n in r["files"].items():
        total += n
        print(f"  {name:28s} {n:>5,d} rows")
    print(f"  {'':28s} {'':>5s}")
    print(f"  {len(r['files'])} files, {total:,} rows "
          f"({r['total_financial_records']:,} financial records)")


if __name__ == "__main__":
    main()
