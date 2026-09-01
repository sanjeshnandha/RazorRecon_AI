"""
Delta-1 (compute), Delta-3 (ledger) and Delta-4 (seller payout).

Delta-1 computes fee and tax FROM POLICY, never by reading fee_paise off the
item. Reading their number and comparing it to their number proves nothing.

Refunds are SOURCE-derived, adjustments are ITEM-derived. That asymmetry is
deliberate:
  * a refund's settlement is derivable from policy -- a PROCESSED refund dated
    inside [period_start, period_end] must be deducted from that settlement, so
    its absence from settlement_items is itself the defect;
  * an adjustment has no policy-derivable settlement assignment. adjustments.
    settlement_id is a CLAIM, and the claim is what we are auditing, so it
    cannot also be an input to the expectation. An adjustment present in the
    source but missing from items becomes discoverable evidence instead.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from engine.money import bps
from engine.policy import Policy


@dataclass
class D1Result:
    settlement_id: str
    gross_paise: int = 0
    source_refunds_paise: int = 0
    item_refunds_paise: int = 0
    computed_fee_paise: int = 0
    computed_tax_paise: int = 0
    charged_fee_paise: int = 0
    charged_tax_paise: int = 0
    item_adjustments_paise: int = 0
    expected_net_paise: int = 0
    actual_net_paise: int = 0
    delta_paise: int = 0
    # evidence carried forward to the attribution layer
    fee_by_payment: dict = field(default_factory=dict)     # pid -> (computed, charged)
    aggregate_tax_paise: int = 0
    in_period_refunds: list = field(default_factory=list)
    itemised_refund_ids: set = field(default_factory=set)
    itemised_adjustment_ids: set = field(default_factory=set)


def compute_d1(snap, s: dict, policy: Policy, excluded_items: set) -> D1Result:
    sid = s["settlement_id"]
    r = D1Result(settlement_id=sid)
    items = [i for i in snap.items_by_settlement.get(sid, [])
             if i["transaction_type"] != "TRANSFER"          # TRANSFER rows belong to Delta-4
             and i["settlement_item_id"] not in excluded_items]

    for it in items:
        if it["transaction_type"] == "PAYMENT":
            p = snap.payments.get(it["payment_id"])
            if p is None:
                continue
            r.gross_paise += it["amount_paise"]
            cf = bps(p["amount_paise"], policy.mdr_bps(p["payment_method"]))
            ct = bps(cf, policy.gst_on_fee_bps)
            r.computed_fee_paise += cf
            r.computed_tax_paise += ct
            r.fee_by_payment[p["payment_id"]] = (cf, it["fee_paise"], ct, it["tax_paise"],
                                                 p["payment_method"], p["amount_paise"])
        elif it["transaction_type"] == "REFUND":
            r.item_refunds_paise += -it["amount_paise"]
            r.itemised_refund_ids.add(it["refund_id"])
        elif it["transaction_type"] == "ADJUSTMENT":
            r.item_adjustments_paise += it["amount_paise"]
            r.itemised_adjustment_ids.add(it["adjustment_id"])
        r.charged_fee_paise += it["fee_paise"]
        r.charged_tax_paise += it["tax_paise"]

    # what the aggregate (wrong) GST method would have produced -- kept so the
    # attribution layer can prove a tax gap IS the rounding method, not a guess
    r.aggregate_tax_paise = bps(r.computed_fee_paise, policy.gst_on_fee_bps)

    # --- the period gate. A refund dated after period_end belongs to a LATER
    # settlement, however close it sits to this settlement_date.
    ps, pe = s["settlement_period_start"], s["settlement_period_end"]
    for ref in snap.refunds:
        if ref["refund_status"] != "PROCESSED":
            continue
        if ps <= ref["refund_date"] <= pe:
            r.source_refunds_paise += ref["refund_amount_paise"]
            r.in_period_refunds.append(ref)

    r.expected_net_paise = (r.gross_paise - r.source_refunds_paise - r.computed_fee_paise
                            - r.computed_tax_paise + r.item_adjustments_paise)
    r.actual_net_paise = s["net_settlement_amount_paise"]
    r.delta_paise = r.expected_net_paise - r.actual_net_paise
    return r


@dataclass
class D3Result:
    settlement_id: str
    mode: str                       # CLEARING | ACCOUNT_INTEGRITY | CLEAN
    expected_paise: int = 0
    actual_paise: int = 0
    delta_paise: int = 0
    duplicate_groups: list = field(default_factory=list)
    missing_payments: list = field(default_factory=list)
    misposted_entries: list = field(default_factory=list)


def compute_d3(snap, s: dict, policy: Policy, excluded_items: set) -> D3Result:
    """Delta-3 checks the merchant's own books.

    A fully settled payment leaves RAZORPAY_CLEARING at exactly zero, so any
    non-zero clearing balance across a settlement's payments is a duplicate,
    missing or misdirected posting. Where clearing IS zero we still check
    account integrity: a fee posted to SALES instead of GATEWAY_FEES leaves a
    balanced group and a zero clearing balance while overstating revenue.
    """
    sid = s["settlement_id"]
    pids = [i["payment_id"] for i in snap.items_by_settlement.get(sid, [])
            if i["transaction_type"] == "PAYMENT" and i["payment_id"]
            and i["settlement_item_id"] not in excluded_items]
    res = D3Result(settlement_id=sid, mode="CLEAN")
    dr = cr = 0
    for pid in pids:
        for le in snap.ledger_by_payment.get(pid, []):
            if le["account"] != "RAZORPAY_CLEARING":
                continue
            if le["direction"] == "DR":
                dr += le["amount_paise"]
            else:
                cr += le["amount_paise"]
    balance = dr - cr
    res.expected_paise, res.actual_paise = dr, cr

    if balance != 0:
        res.mode = "CLEARING"
        res.delta_paise = balance
        # duplicate settlement postings: the same (payment, settlement) posted twice
        seen: dict[str, list[str]] = {}
        for pid in pids:
            for le in snap.ledger_by_payment.get(pid, []):
                if le["account"] == "RAZORPAY_CLEARING" and le["direction"] == "CR" \
                        and le["settlement_id"] == sid:
                    seen.setdefault(pid, []).append(le["entry_group_id"])
        for pid, groups in seen.items():
            if len(groups) > 1:
                for g in groups[1:]:
                    amt = sum(l["amount_paise"] for l in snap.ledger_by_group.get(g, [])
                              if l["account"] == "RAZORPAY_CLEARING" and l["direction"] == "CR")
                    res.duplicate_groups.append((g, pid, amt))
        for pid in pids:
            if pid not in seen:
                captured = sum(l["amount_paise"] for l in snap.ledger_by_payment.get(pid, [])
                               if l["account"] == "RAZORPAY_CLEARING" and l["direction"] == "DR")
                refunded = sum(l["amount_paise"] for l in snap.ledger_by_payment.get(pid, [])
                               if l["account"] == "RAZORPAY_CLEARING" and l["direction"] == "CR")
                if captured - refunded != 0:
                    res.missing_payments.append((pid, captured - refunded))
        return res

    # clearing nets to zero -- now check that fees landed in the right account
    expected_fee = sum(i["fee_paise"] for i in snap.items_by_settlement.get(sid, [])
                       if i["transaction_type"] != "TRANSFER"
                       and i["settlement_item_id"] not in excluded_items)
    posted_fee = sum(le["amount_paise"] for le in snap.ledger_by_settlement.get(sid, [])
                     if le["account"] == "GATEWAY_FEES" and le["direction"] == "DR")
    if expected_fee != posted_fee:
        res.mode = "ACCOUNT_INTEGRITY"
        res.expected_paise, res.actual_paise = expected_fee, posted_fee
        res.delta_paise = expected_fee - posted_fee
        for le in snap.ledger_by_settlement.get(sid, []):
            if le["account"] == "SALES" and le["direction"] == "DR":
                res.misposted_entries.append((le["ledger_entry_id"], le["amount_paise"],
                                              le["payment_id"]))
    return res


@dataclass
class D4Result:
    subject_type: str               # 'allocation' | 'payment'
    subject_id: str
    settlement_id: str | None
    seller_id: str | None
    expected_paise: int
    actual_paise: int
    delta_paise: int
    kind: str                       # PAYOUT | TRANSFER_MISSING | ALLOC_EXCEEDS
    reversed_transfers: list = field(default_factory=list)
    extra: dict = field(default_factory=dict)


def compute_d4(snap, policy: Policy, settlement_of_payment) -> list[D4Result]:
    """Delta-1..3 can all reconcile perfectly while a specific seller is short.
    For a marketplace that is arguably the more consequential failure, so it is
    computed and reported on its own axis."""
    out: list[D4Result] = []

    for a in snap.allocations:
        if a["allocation_status"] != "SETTLED":
            continue
        key = (a["payment_id"], a["seller_id"])
        moved = [t for t in snap.transfers_by_ps.get(key, [])]
        paid = sum(t["amount_paise"] for t in moved if t["transfer_status"] == "PROCESSED")
        expected = a["net_seller_paise"]
        delta = expected - paid
        has_processed = any(t["transfer_status"] == "PROCESSED" for t in moved)
        out.append(D4Result(
            subject_type="allocation", subject_id=a["allocation_id"],
            settlement_id=settlement_of_payment(a["payment_id"]), seller_id=a["seller_id"],
            expected_paise=expected, actual_paise=paid, delta_paise=delta,
            kind="PAYOUT" if has_processed else "TRANSFER_MISSING",
            reversed_transfers=[t for t in moved if t["transfer_status"] in ("REVERSED", "FAILED")]))

    # allocations exceeding the payment: a payment-level invariant, so its own row
    for pid, allocs in snap.allocations_by_payment.items():
        p = snap.payments.get(pid)
        if not p:
            continue
        total = sum(x["gross_allocated_paise"] for x in allocs
                    if x["allocation_status"] in ("SETTLED", "PENDING"))
        if total > p["amount_paise"]:
            out.append(D4Result(
                subject_type="payment", subject_id=pid,
                settlement_id=settlement_of_payment(pid), seller_id=None,
                expected_paise=p["amount_paise"], actual_paise=total,
                delta_paise=p["amount_paise"] - total, kind="ALLOC_EXCEEDS",
                extra={"allocation_ids": [x["allocation_id"] for x in allocs]}))
    return out
