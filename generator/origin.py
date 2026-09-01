"""
Continuation state for append mode.

A dataset is a growing book, not a fixed snapshot. To bolt a new settlement
cycle onto one that already exists we need four things back out of the
database: where the calendar stopped, how far every id sequence got, who the
existing customers and sellers are, and which older payments are still
eligible for a late refund or chargeback.

Reading these from the database rather than carrying them in memory is what
lets a tick be issued by a fresh process -- an API worker, a cron job, a
terminal -- with no shared state beyond Postgres itself.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, timedelta

from engine.db import fetch, fetch_one

# id prefix -> (table, column, _seq key). The numeric tail of every id is the
# sequence value, so the high-water mark is a max() over that tail.
_SEQUENCES = [
    ("orders",            "order_id",         "order"),
    ("payments",          "payment_id",       "payment"),
    ("refunds",           "refund_id",        "refund"),
    ("seller_allocations","allocation_id",    "alloc"),
    ("transfers",         "transfer_id",      "transfer"),
    ("adjustments",       "adjustment_id",    "adj"),
    ("settlement_items",  "settlement_item_id","si"),
    ("bank_transactions", "bank_transaction_id","bank"),
    ("ledger_entries",    "ledger_entry_id",  "ledger"),
    ("ledger_entries",    "entry_group_id",   "grp"),
    ("ground_truth_anomalies", "anomaly_id",  "gt"),
    ("customers",         "customer_id",      "customer"),
    ("sellers",           "seller_id",        "seller"),
    ("settlements",       "settlement_id",    "settlement"),
]


@dataclass
class Origin:
    """Everything a new batch needs to know about the batches before it."""
    dataset_id: str
    seed: int
    label: str | None
    batch: int                      # 1-based index of the batch being built
    first_period_start: date        # day after the last period ended
    settlement_offset: int          # highest existing SET_ number
    seq: dict                       # high-water mark per id sequence
    customers: list[dict] = field(default_factory=list)
    sellers: list[dict] = field(default_factory=list)
    prior_payments: list[dict] = field(default_factory=list)
    pending_bank: list[dict] = field(default_factory=list)
    row_counts: dict = field(default_factory=dict)


def _max_seq(conn, table: str, column: str, dataset_id: str) -> int:
    row = fetch_one(conn, f"""
        SELECT COALESCE(MAX(CAST(SUBSTRING({column} FROM '[0-9]+$') AS BIGINT)), 0) AS n
        FROM {table} WHERE dataset_id=%s""", (dataset_id,))
    return int(row["n"]) if row else 0


def load_origin(conn, dataset_id: str, late_pool: int = 4000) -> Origin:
    head = fetch_one(conn, "SELECT seed, label, row_counts FROM datasets WHERE dataset_id=%s",
                     (dataset_id,))
    if head is None:
        raise ValueError(f"no such dataset: {dataset_id}")

    bounds = fetch_one(conn, """
        SELECT MAX(settlement_period_end) AS last_end, COUNT(*) AS n
        FROM settlements WHERE dataset_id=%s""", (dataset_id,))
    if not bounds or bounds["last_end"] is None:
        raise ValueError(f"dataset {dataset_id} has no settlements to append to")

    # The hand-worked Phase 0 fixtures are real datasets in the same tables, but
    # they are three rows written by hand to prove an arithmetic case -- there is
    # no seller population to keep trading and nothing sensible to append. Say so
    # here rather than failing deep inside the generator with an opaque
    # "sample larger than population".
    active = fetch_one(conn, "SELECT count(*) AS n FROM sellers WHERE dataset_id=%s "
                             "AND status='ACTIVE'", (dataset_id,))["n"]
    if active < 2 or bounds["n"] < 2:
        raise ValueError(
            f"dataset {dataset_id} has {bounds['n']} settlement(s) and {active} active "
            "seller(s), so it cannot be grown -- it looks like a hand-worked Phase 0 "
            "fixture rather than a generated dataset. Pass --dataset with a generated "
            "one, or run generator.generate first.")

    seq = {key: _max_seq(conn, tbl, col, dataset_id) for tbl, col, key in _SEQUENCES}

    counts = head["row_counts"]
    if isinstance(counts, str):
        counts = json.loads(counts)
    counts = counts or {}
    # A dataset generated before append mode has no batch log, but its rows are
    # nonetheless batch 1 -- so the batch being built now is always at least 2.
    batch = max(len(counts.get("batches", [])), 1) + 1

    customers = fetch(conn, "SELECT customer_id FROM customers WHERE dataset_id=%s "
                            "ORDER BY customer_id", (dataset_id,))
    sellers = fetch(conn, "SELECT seller_id, seller_name, seller_type, commission_bps, status "
                          "FROM sellers WHERE dataset_id=%s ORDER BY seller_id", (dataset_id,))

    # Payments that already belong to a closed settlement and still have refund
    # headroom. INV-B2 says lifetime refunds may never exceed the payment, so
    # the headroom is computed here rather than trusted to the caller.
    prior = fetch(conn, """
        SELECT p.payment_id, p.order_id, p.customer_id, p.amount_paise, p.payment_method,
               p.captured_at::date AS capture_day, si.settlement_id,
               COALESCE(r.refunded, 0) AS refunded
        FROM payments p
        JOIN settlement_items si ON si.dataset_id = p.dataset_id
                               AND si.payment_id = p.payment_id
                               AND si.transaction_type = 'PAYMENT'
        LEFT JOIN (SELECT payment_id, SUM(refund_amount_paise) AS refunded
                     FROM refunds WHERE dataset_id=%s AND refund_status='PROCESSED'
                    GROUP BY payment_id) r ON r.payment_id = p.payment_id
        WHERE p.dataset_id=%s AND p.payment_status='CAPTURED'
          AND p.amount_paise - COALESCE(r.refunded, 0) > 20000
        ORDER BY p.payment_id DESC
        LIMIT %s""", (dataset_id, dataset_id, late_pool))

    # Settlements whose bank credit was deliberately withheld at the end of a
    # previous batch -- the money is in flight, not missing. Recorded by id so
    # this is never confused with a planted MISSING_CREDIT anomaly.
    withheld = [sid for b in counts.get("batches", []) for sid in b.get("withheld_bank", [])]
    pending = []
    if withheld:
        pending = fetch(conn, """
            SELECT settlement_id, settlement_date, settlement_utr, net_settlement_amount_paise
            FROM settlements WHERE dataset_id=%s AND settlement_id = ANY(%s)
              AND settlement_utr NOT IN (SELECT COALESCE(settlement_utr,'') FROM bank_transactions
                                          WHERE dataset_id=%s)
            ORDER BY settlement_id""", (dataset_id, withheld, dataset_id))

    return Origin(
        dataset_id=dataset_id,
        seed=int(head["seed"]),
        label=head["label"],
        batch=batch,
        first_period_start=bounds["last_end"] + timedelta(days=1),
        settlement_offset=seq["settlement"],
        seq=seq,
        customers=customers,
        sellers=sellers,
        prior_payments=prior,
        pending_bank=pending,
        row_counts=counts,
    )
