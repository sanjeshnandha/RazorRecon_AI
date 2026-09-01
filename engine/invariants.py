"""
Structural (fatal-for-that-record) vs business (recorded, non-blocking) invariants.

Getting this distinction backwards is what causes a demo to silently reject the
interesting half of the dataset. Structural failures mean the record cannot be
reasoned about at all; the record is excluded and can never reach tier A. A
business invariant is EXPECTED to fail sometimes -- several planted anomalies
exist precisely to violate one -- and a violation is recorded as an exception
input while reconciliation continues.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class InvariantReport:
    structural: list[dict] = field(default_factory=list)   # fatal for that record
    business: list[dict] = field(default_factory=list)     # feeds the delta layers
    excluded_items: set = field(default_factory=set)
    excluded_settlements: set = field(default_factory=set)

    def by_settlement(self, sid: str, ids: tuple[str, ...] | None = None) -> list[dict]:
        out = [v for v in self.business if v["settlement_id"] == sid]
        if ids:
            out = [v for v in out if v["id"] in ids]
        return out


def _add(bucket, inv_id, sid, subject_type, subject_id, detail, amount=0, **extra):
    row = {"id": inv_id, "settlement_id": sid, "subject_type": subject_type,
           "subject_id": subject_id, "detail": detail, "amount_paise": amount}
    row.update(extra)
    bucket.append(row)


def check(snap) -> InvariantReport:
    rep = InvariantReport()

    # ---- INV-S1: every settlement_items reference resolves --------------
    known = {
        "payment_id": set(snap.payments),
        "refund_id": {r["refund_id"] for r in snap.refunds},
        "adjustment_id": {a["adjustment_id"] for a in snap.adjustments},
        "transfer_id": {t["transfer_id"] for t in snap.transfers},
    }
    for it in snap.items:
        for col, universe in known.items():
            if it[col] is not None and it[col] not in universe:
                rep.excluded_items.add(it["settlement_item_id"])
                _add(rep.structural, "INV-S1", it["settlement_id"], "settlement_item",
                     it["settlement_item_id"],
                     f"{col}={it[col]} does not resolve to any source record",
                     abs(it["amount_paise"]), exception_type="UNKNOWN_REFERENCE")

    # ---- INV-B1: every entry_group balances -----------------------------
    for gid, legs in snap.ledger_by_group.items():
        dr = sum(l["amount_paise"] for l in legs if l["direction"] == "DR")
        cr = sum(l["amount_paise"] for l in legs if l["direction"] == "CR")
        if dr != cr:
            sid = next((l["settlement_id"] for l in legs if l["settlement_id"]), None)
            _add(rep.business, "INV-B1", sid, "entry_group", gid,
                 f"DR {dr} != CR {cr} paise", abs(dr - cr),
                 exception_type="UNBALANCED_ENTRY_GROUP")

    # ---- INV-B2: refunds never exceed the payment (structural if seen) --
    for pid, refs in snap.refunds_by_payment.items():
        p = snap.payments.get(pid)
        if not p:
            continue
        total = sum(r["refund_amount_paise"] for r in refs if r["refund_status"] == "PROCESSED")
        if total > p["amount_paise"]:
            _add(rep.structural, "INV-B2", None, "payment", pid,
                 f"PROCESSED refunds {total} exceed payment {p['amount_paise']} paise",
                 total - p["amount_paise"], exception_type="UNKNOWN_REFERENCE")

    # ---- INV-B3: allocations never exceed the payment -------------------
    for pid, allocs in snap.allocations_by_payment.items():
        p = snap.payments.get(pid)
        if not p:
            continue
        total = sum(a["gross_allocated_paise"] for a in allocs
                    if a["allocation_status"] in ("SETTLED", "PENDING"))
        if total > p["amount_paise"]:
            sid = _settlement_of_payment(snap, pid)
            _add(rep.business, "INV-B3", sid, "payment", pid,
                 f"allocations {total} exceed payment {p['amount_paise']} paise",
                 total - p["amount_paise"], exception_type="ALLOCATION_EXCEEDS_PAYMENT",
                 allocation_ids=[a["allocation_id"] for a in allocs])

    # ---- INV-B5: no FAILED payment in settlement_items ------------------
    for it in snap.items:
        if it["transaction_type"] != "PAYMENT" or not it["payment_id"]:
            continue
        p = snap.payments.get(it["payment_id"])
        if p and p["payment_status"] == "FAILED":
            _add(rep.business, "INV-B5", it["settlement_id"], "payment", it["payment_id"],
                 "FAILED payment appears in settlement_items", abs(it["amount_paise"]),
                 exception_type="FAILED_PAYMENT_IN_SETTLEMENT")

    # ---- INV-B6 / INV-B7: header vs items, header self-consistency ------
    for s in snap.settlements:
        its = [i for i in snap.items_by_settlement.get(s["settlement_id"], [])
               if i["transaction_type"] != "TRANSFER"
               and i["settlement_item_id"] not in rep.excluded_items]
        g = sum(i["amount_paise"] for i in its if i["transaction_type"] == "PAYMENT")
        r = sum(-i["amount_paise"] for i in its if i["transaction_type"] == "REFUND")
        f = sum(i["fee_paise"] for i in its)
        t = sum(i["tax_paise"] for i in its)
        a = sum(i["amount_paise"] for i in its if i["transaction_type"] == "ADJUSTMENT")
        parts = {"gross": (g, s["gross_amount_paise"]), "refund": (r, s["refund_amount_paise"]),
                 "fee": (f, s["fee_amount_paise"]), "tax": (t, s["tax_amount_paise"]),
                 "adjustment": (a, s["adjustment_amount_paise"])}
        mismatched = {k: v for k, v in parts.items() if v[0] != v[1]}
        if mismatched:
            detail = "; ".join(f"{k}: items {v[0]} vs header {v[1]}" for k, v in mismatched.items())
            impact = sum(v[0] - v[1] for k, v in mismatched.items()
                         if k in ("gross", "adjustment")) - sum(
                         v[0] - v[1] for k, v in mismatched.items()
                         if k in ("refund", "fee", "tax"))
            _add(rep.business, "INV-B6", s["settlement_id"], "settlement", s["settlement_id"],
                 detail, impact, exception_type="HEADER_ROLLUP_MISMATCH", components=mismatched)
        implied = (s["gross_amount_paise"] - s["refund_amount_paise"] - s["fee_amount_paise"]
                   - s["tax_amount_paise"] + s["adjustment_amount_paise"])
        if implied != s["net_settlement_amount_paise"]:
            _add(rep.business, "INV-B7", s["settlement_id"], "settlement", s["settlement_id"],
                 f"header net {s['net_settlement_amount_paise']} != gross-refund-fee-tax+adj "
                 f"({implied}) paise", implied - s["net_settlement_amount_paise"],
                 exception_type="HEADER_SELF_INCONSISTENT")

        # ---- INV-B8: PROCESSED settlement must carry a UTR --------------
        if s["settlement_status"] == "PROCESSED" and not s["settlement_utr"]:
            _add(rep.business, "INV-B8", s["settlement_id"], "settlement", s["settlement_id"],
                 "PROCESSED settlement has no UTR", 0, exception_type="UTR_MISSING")

    # ---- INV-B9: allocation net vs linked transfer (detail lives in D4) --
    for a in snap.allocations:
        if a["allocation_status"] != "SETTLED":
            continue
        paid = sum(t["amount_paise"] for t in snap.transfers_by_ps.get((a["payment_id"], a["seller_id"]), [])
                   if t["transfer_status"] == "PROCESSED")
        if paid != a["net_seller_paise"]:
            _add(rep.business, "INV-B9", _settlement_of_payment(snap, a["payment_id"]),
                 "allocation", a["allocation_id"],
                 f"net_seller {a['net_seller_paise']} vs transferred {paid} paise",
                 a["net_seller_paise"] - paid, exception_type="ALLOCATION_TRANSFER_DIVERGENCE")
    return rep


_PAY_SETTLEMENT_CACHE: dict[int, dict] = {}


def _settlement_of_payment(snap, payment_id: str) -> str | None:
    cache = _PAY_SETTLEMENT_CACHE.get(id(snap))
    if cache is None:
        cache = {}
        for it in snap.items:
            if it["transaction_type"] == "PAYMENT" and it["payment_id"]:
                cache[it["payment_id"]] = it["settlement_id"]
        _PAY_SETTLEMENT_CACHE.clear()
        _PAY_SETTLEMENT_CACHE[id(snap)] = cache
    return cache.get(payment_id)
