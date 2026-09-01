"""Loads one dataset into memory. ~30k rows -- a single round trip per table
beats a per-settlement query storm by an order of magnitude, and it is what
keeps the whole batch inside the 10-second budget."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from engine.db import fetch


@dataclass
class Snapshot:
    dataset_id: str
    settlements: list[dict] = field(default_factory=list)
    items: list[dict] = field(default_factory=list)
    payments: dict[str, dict] = field(default_factory=dict)
    refunds: list[dict] = field(default_factory=list)
    adjustments: list[dict] = field(default_factory=list)
    allocations: list[dict] = field(default_factory=list)
    transfers: list[dict] = field(default_factory=list)
    bank: list[dict] = field(default_factory=list)
    ledger: list[dict] = field(default_factory=list)
    sellers: dict[str, dict] = field(default_factory=dict)

    # indexes
    items_by_settlement: dict[str, list[dict]] = field(default_factory=dict)
    settlement_by_id: dict[str, dict] = field(default_factory=dict)
    refunds_by_payment: dict[str, list[dict]] = field(default_factory=dict)
    adjustments_by_settlement: dict[str, list[dict]] = field(default_factory=dict)
    transfers_by_ps: dict[tuple, list[dict]] = field(default_factory=dict)
    allocations_by_payment: dict[str, list[dict]] = field(default_factory=dict)
    ledger_by_settlement: dict[str, list[dict]] = field(default_factory=dict)
    ledger_by_payment: dict[str, list[dict]] = field(default_factory=dict)
    ledger_by_group: dict[str, list[dict]] = field(default_factory=dict)
    refund_by_id: dict[str, dict] = field(default_factory=dict)
    allocation_by_id: dict[str, dict] = field(default_factory=dict)
    bank_by_id: dict[str, dict] = field(default_factory=dict)


def load(conn, dataset_id: str) -> Snapshot:
    d = (dataset_id,)
    s = Snapshot(dataset_id=dataset_id)
    s.settlements = fetch(conn, "SELECT * FROM settlements WHERE dataset_id=%s ORDER BY settlement_id", d)
    s.items = fetch(conn, "SELECT * FROM settlement_items WHERE dataset_id=%s ORDER BY settlement_item_id", d)
    s.payments = {p["payment_id"]: p for p in
                  fetch(conn, "SELECT * FROM payments WHERE dataset_id=%s", d)}
    s.refunds = fetch(conn, "SELECT * FROM refunds WHERE dataset_id=%s ORDER BY refund_id", d)
    s.adjustments = fetch(conn, "SELECT * FROM adjustments WHERE dataset_id=%s ORDER BY adjustment_id", d)
    s.allocations = fetch(conn, "SELECT * FROM seller_allocations WHERE dataset_id=%s ORDER BY allocation_id", d)
    s.transfers = fetch(conn, "SELECT * FROM transfers WHERE dataset_id=%s ORDER BY transfer_id", d)
    s.bank = fetch(conn, "SELECT * FROM bank_transactions WHERE dataset_id=%s ORDER BY bank_transaction_id", d)
    s.ledger = fetch(conn, "SELECT * FROM ledger_entries WHERE dataset_id=%s ORDER BY ledger_entry_id", d)
    s.sellers = {x["seller_id"]: x for x in fetch(conn, "SELECT * FROM sellers WHERE dataset_id=%s", d)}

    s.settlement_by_id = {x["settlement_id"]: x for x in s.settlements}
    s.items_by_settlement = defaultdict(list)
    for it in s.items:
        s.items_by_settlement[it["settlement_id"]].append(it)
    s.refunds_by_payment = defaultdict(list)
    s.refund_by_id = {}
    for r in s.refunds:
        s.refunds_by_payment[r["payment_id"]].append(r)
        s.refund_by_id[r["refund_id"]] = r
    s.adjustments_by_settlement = defaultdict(list)
    for a in s.adjustments:
        s.adjustments_by_settlement[a["settlement_id"]].append(a)
    s.transfers_by_ps = defaultdict(list)
    for t in s.transfers:
        s.transfers_by_ps[(t["payment_id"], t["seller_id"])].append(t)
    s.allocations_by_payment = defaultdict(list)
    s.allocation_by_id = {}
    for a in s.allocations:
        s.allocations_by_payment[a["payment_id"]].append(a)
        s.allocation_by_id[a["allocation_id"]] = a
    s.ledger_by_settlement = defaultdict(list)
    s.ledger_by_payment = defaultdict(list)
    s.ledger_by_group = defaultdict(list)
    for le in s.ledger:
        if le["settlement_id"]:
            s.ledger_by_settlement[le["settlement_id"]].append(le)
        if le["payment_id"]:
            s.ledger_by_payment[le["payment_id"]].append(le)
        s.ledger_by_group[le["entry_group_id"]].append(le)
    s.bank_by_id = {b["bank_transaction_id"]: b for b in s.bank}
    return s
