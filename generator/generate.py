"""
Seeded, reproducible dataset generator.

Given a seed, this produces byte-identical data. Generation is strictly
dependency-ordered (spec 4.2) and every fee, tax and commission figure is read
from the policy registry -- the same registry the engine reads. Anomalies are
applied as a separate MUTATION PASS at step 12 over clean data, so we know
exactly what was broken and by how much.

    python -m generator.generate --seed 42 --settlements 100 --label demo
"""
from __future__ import annotations

import argparse
import json
import random
import uuid
from datetime import date, datetime, time, timedelta, timezone

from engine.db import connect, copy_rows, fetch_one
from engine.money import bps
from engine.policy import Policy, load_policy
from generator.calendar import add_working_days, is_working_day

IST = timezone(timedelta(hours=5, minutes=30))

FIRST_NAMES = ["Aarav","Diya","Vihaan","Ananya","Arjun","Ishita","Kabir","Meera","Rohan","Saanvi",
               "Aditya","Nisha","Vikram","Priya","Karan","Tara","Rahul","Sneha","Manish","Kavya",
               "Devansh","Riya","Aryan","Pooja","Siddharth","Anika","Yash","Neha","Om","Zara"]
LAST_NAMES = ["Sharma","Verma","Iyer","Nair","Reddy","Patel","Singh","Gupta","Menon","Bose",
              "Rao","Joshi","Kulkarni","Desai","Chopra","Malhotra","Banerjee","Pillai","Shetty","Ghosh"]
SELLER_STEMS = ["Kanha","Urban","Nimbus","Saffron","Trellis","Copperleaf","Marigold","Bluejay","Ashwin",
                "Peepal","Kitewind","Sunder","Tamarind","Nilgiri","Coral","Banyan","Chinar","Mandala",
                "Zephyr","Kesar","Palash","Ivory","Neelam","Rangoli","Gulmohar","Sitara","Amber","Cove",
                "Kaveri","Lotus","Indigo","Terra","Vayu","Mrig","Anantha","Bela","Cirrus","Dhruva",
                "Ekam","Falgun"]
SELLER_SUFFIX = ["Traders","Exports","Retail","Crafts","Foods","Textiles","Electronics","Organics",
                 "Supplies","Studio"]
REFUND_REASONS = ["Customer requested cancellation","Item damaged in transit","Wrong size delivered",
                  "Duplicate order","Delivery delayed beyond SLA","Quality complaint"]
FAILURE_REASONS = ["Insufficient funds","Bank declined","3DS authentication timeout",
                   "Card expired","Issuer unavailable","UPI collect expired"]
METHOD_WEIGHTS = [("UPI", 52), ("CARD", 24), ("NETBANKING", 12), ("WALLET", 9), ("CARD_INTL", 3)]


class Dataset:
    """In-memory dataset. Lists of dicts, mutated by the anomaly pass, then COPY'd."""

    def __init__(self, dataset_id: str, seed: int, policy: Policy, label: str | None):
        self.dataset_id = dataset_id
        self.seed = seed
        self.policy = policy
        self.label = label
        self.customers: list[dict] = []
        self.sellers: list[dict] = []
        self.orders: list[dict] = []
        self.payments: list[dict] = []
        self.refunds: list[dict] = []
        self.allocations: list[dict] = []
        self.transfers: list[dict] = []
        self.adjustments: list[dict] = []
        self.settlements: list[dict] = []
        self.settlement_items: list[dict] = []
        self.bank_transactions: list[dict] = []
        self.ledger_entries: list[dict] = []
        self.money_edges: list[dict] = []
        self.ground_truth: list[dict] = []
        # counters used by the anomaly pass to mint new ids without collision
        self._seq = {"si": 0, "bank": 0, "ledger": 0, "adj": 0, "refund": 0, "transfer": 0,
                     "grp": 0, "gt": 0}
        # Payments owned by an EARLIER batch, loaded read-only so a late refund
        # can reference them. Never persisted again -- they are already rows.
        self.external_payments: list[dict] = []
        # Sellers owned by an earlier batch. Same idea: readable, never re-written.
        self.carried_sellers: list[dict] = []

    # --- id helpers -------------------------------------------------------
    def next_id(self, kind: str, prefix: str, width: int = 5) -> str:
        self._seq[kind] += 1
        return f"{prefix}{self._seq[kind]:0{width}d}"

    # --- lookup indexes (rebuilt on demand; datasets are small) -----------
    def index(self) -> None:
        self.by_payment = {p["payment_id"]: p
                           for p in (self.external_payments + self.payments)}
        self.by_settlement = {s["settlement_id"]: s for s in self.settlements}
        self.by_refund = {r["refund_id"]: r for r in self.refunds}
        self.by_allocation = {a["allocation_id"]: a for a in self.allocations}
        # On an append `sellers` is empty -- the population carried over and its
        # rows already exist. The index still has to resolve them.
        self.by_seller = {s["seller_id"]: s
                          for s in (getattr(self, "carried_sellers", []) + self.sellers)}
        self.items_by_settlement: dict[str, list[dict]] = {}
        for it in self.settlement_items:
            self.items_by_settlement.setdefault(it["settlement_id"], []).append(it)


def weighted_choice(rng: random.Random, weighted: list[tuple[str, int]]) -> str:
    total = sum(w for _, w in weighted)
    x = rng.randrange(total)
    acc = 0
    for value, w in weighted:
        acc += w
        if x < acc:
            return value
    return weighted[-1][0]


def utr_for(index: int, d: date) -> str:
    """Deterministic 16-char UTR, shaped like an NEFT reference."""
    return f"N{d.strftime('%y%m%d')}{index:09d}"


def ts(d: date, rng: random.Random) -> datetime:
    return datetime.combine(d, time(rng.randrange(6, 22), rng.randrange(60), rng.randrange(60)), IST)


# =============================================================================
# Clean generation
# =============================================================================
def generate_clean(seed: int, n_settlements: int, policy: Policy, label: str | None,
                   origin=None, withhold_tail_credit: bool = True) -> Dataset:
    """Build one batch of clean data.

    With `origin` None this is a fresh dataset and behaves exactly as it always
    has. With an origin it builds the NEXT batch of an existing dataset: the
    calendar resumes where the last period ended, every id sequence continues
    from its high-water mark, and the customer and seller population carries
    over instead of being re-minted.
    """
    rng = random.Random(seed if origin is None else f"{origin.seed}:{origin.batch}")
    ds = Dataset(str(uuid.UUID(int=random.Random(seed ^ 0x5EED).getrandbits(128), version=4))
                 if origin is None else origin.dataset_id,
                 seed, policy, label)
    if origin is not None:
        # Every sequence continues from its high-water mark, including the ones
        # the fresh path never needed to track (orders, payments, allocations).
        ds._seq.update(origin.seq)

    # --- 1. customers & sellers -----------------------------------------
    # On an append the existing population keeps trading; only a small cohort of
    # new customers joins. Re-minting sellers each cycle would make every seller
    # payout look like a first-time relationship, which is not how a marketplace
    # behaves and would make Delta-4 trends meaningless.
    n_customers = 800
    first_customer = 1
    if origin is not None:
        ds.external_customers = origin.customers
        ds.sellers_carried = origin.sellers
        first_customer = origin.seq.get("customer", 0) + 1
        n_customers = first_customer + max(20, n_settlements) - 1
    for i in range(first_customer, n_customers + 1):
        fn, ln = rng.choice(FIRST_NAMES), rng.choice(LAST_NAMES)
        ds.customers.append({
            "customer_id": f"C_{i:05d}", "name": f"{fn} {ln}",
            "email": f"{fn.lower()}.{ln.lower()}{i}@example.in",
            "created_at": ts(date(2025, 11, 1) + timedelta(days=rng.randrange(60)), rng)})

    seller_range = range(1, 41) if origin is None else range(0)
    if origin is not None:
        ds.carried_sellers[:] = [dict(x) for x in origin.sellers]
    for i in seller_range:
        stype = weighted_choice(rng, [("INDIVIDUAL", 35), ("SMB", 45), ("ENTERPRISE", 20)])
        ds.sellers.append({
            "seller_id": f"S_{i:03d}",
            "seller_name": f"{SELLER_STEMS[(i - 1) % len(SELLER_STEMS)]} {rng.choice(SELLER_SUFFIX)}",
            "seller_type": stype,
            "commission_bps": policy.commission_bps(stype),
            "status": "SUSPENDED" if rng.random() < 0.05 else "ACTIVE"})
    seller_pool = ds.sellers if origin is None else ds.carried_sellers
    active_sellers = [s for s in seller_pool if s["status"] == "ACTIVE"]

    # --- settlement calendar --------------------------------------------
    # C_1..C_n are consecutive working days. Settlement i covers the calendar
    # days (C_{i-1}, C_i]; every capture date inside that window rolls forward
    # to C_i and therefore settles on the same T+2 date. Periods tile the
    # calendar with no gaps and no overlaps, so a refund date belongs to
    # exactly one settlement period -- which is what makes the refund-period
    # gate in the engine unambiguous.
    cutoffs: list[date] = []
    d = date(2026, 1, 5) if origin is None else origin.first_period_start
    while len(cutoffs) < n_settlements:
        if is_working_day(d, policy):
            cutoffs.append(d)
        d += timedelta(days=1)

    periods: list[tuple[date, date, date]] = []   # (period_start, period_end, settlement_date)
    for i, c in enumerate(cutoffs):
        if i:
            start = cutoffs[i - 1] + timedelta(days=1)
        elif origin is not None:
            start = origin.first_period_start
        else:
            start = c - timedelta(days=2)
        periods.append((start, c, add_working_days(c, policy.cycle_working_days, policy)))

    def period_index_for(day: date) -> int | None:
        for i, (ps, pe, _) in enumerate(periods):
            if ps <= day <= pe:
                return i
        return None

    # --- 2/3. orders and payments ---------------------------------------
    n_orders = 3000 if origin is None else max(200, n_settlements * 30)
    order_seq = ds._seq.get("order", 0)
    payment_seq = ds._seq.get("payment", 0)
    customer_pool = ds.customers if origin is None else (
        [{"customer_id": c["customer_id"]} for c in origin.customers] + ds.customers)
    # every period gets at least one captured payment, then the rest spread out
    period_targets = [1] * n_settlements
    for _ in range(n_orders - n_settlements):
        period_targets[rng.randrange(n_settlements)] += 1

    for pi, target in enumerate(period_targets):
        ps, pe, _ = periods[pi]
        span = (pe - ps).days
        for _ in range(target):
            order_seq += 1
            oid = f"O_{order_seq:05d}"
            cust = rng.choice(customer_pool)
            amount = rng.choice([
                rng.randrange(19900, 500000, 100),        # Rs 199 - 5,000
                rng.randrange(50000, 2500000, 100),       # Rs 500 - 25,000
                rng.randrange(2500000, 9000000, 100)])    # Rs 25,000 - 90,000
            capture_day = ps + timedelta(days=rng.randrange(span + 1)) if span else ps
            order_date = capture_day - timedelta(days=rng.randrange(0, 2))
            ds.orders.append({"order_id": oid, "customer_id": cust["customer_id"],
                              "order_amount_paise": amount, "order_date": order_date,
                              "order_status": "PAID"})
            method = weighted_choice(rng, METHOD_WEIGHTS)
            all_fail = rng.random() < 0.10
            n_attempts = 1 if rng.random() < 0.86 else rng.choice([2, 3])
            for attempt in range(n_attempts):
                payment_seq += 1
                pid = f"P_{payment_seq:05d}"
                last = attempt == n_attempts - 1
                if all_fail or not last:
                    ds.payments.append({
                        "payment_id": pid, "order_id": oid, "customer_id": cust["customer_id"],
                        "amount_paise": amount, "payment_status": "FAILED",
                        "payment_method": method, "created_at": ts(capture_day, rng),
                        "captured_at": None, "failure_reason": rng.choice(FAILURE_REASONS)})
                else:
                    ds.payments.append({
                        "payment_id": pid, "order_id": oid, "customer_id": cust["customer_id"],
                        "amount_paise": amount, "payment_status": "CAPTURED",
                        "payment_method": method, "created_at": ts(capture_day, rng),
                        "captured_at": ts(capture_day, rng), "failure_reason": None,
                        "_capture_day": capture_day, "_period": pi})
            if all_fail:
                ds.orders[-1]["order_status"] = "CANCELLED"

    captured = [p for p in ds.payments if p["payment_status"] == "CAPTURED"]

    # --- 4. refunds ------------------------------------------------------
    # refund_date is 1-20 days after capture, so a refund usually lands in a
    # LATER settlement period than its payment. That is realistic and it is
    # what makes the period gate load-bearing rather than decorative.
    refund_seq = ds._seq.get("refund", 0)
    for p in rng.sample(captured, k=min(300, len(captured))):
        refund_seq += 1
        frac_bps = rng.choice([2500, 3300, 5000, 10000])
        amt = bps(p["amount_paise"], frac_bps)
        if amt <= 0:
            continue
        rday = p["_capture_day"] + timedelta(days=rng.randrange(1, 21))
        if period_index_for(rday) is None:
            continue
        status = "PROCESSED" if rng.random() < 0.90 else rng.choice(["PENDING", "FAILED"])
        ds.refunds.append({"refund_id": f"R_{refund_seq:05d}", "payment_id": p["payment_id"],
                           "refund_amount_paise": amt, "refund_status": status,
                           "refund_date": rday, "refund_reason": rng.choice(REFUND_REASONS)})
        if status == "PROCESSED":
            p["_refunded_paise"] = p.get("_refunded_paise", 0) + amt
            ds.orders_status_touch = True
    ds._seq["refund"] = refund_seq

    # --- 4b. late-arriving refunds (append only) -------------------------
    # A customer who paid in January can be refunded in March. The payment was
    # settled two batches ago and its books are closed, so this refund is netted
    # off the CURRENT settlement -- exactly the timing difference Delta-2 exists
    # to explain. Refund headroom is respected so INV-B2 can never be violated.
    if origin is not None and origin.prior_payments:
        window_start, window_end = periods[0][0], periods[-1][1]
        n_late = max(1, int(len(origin.prior_payments) * 0.012))
        for src in rng.sample(origin.prior_payments, k=min(n_late, len(origin.prior_payments))):
            headroom = src["amount_paise"] - src["refunded"]
            amt = min(bps(src["amount_paise"], rng.choice([2500, 3300, 5000])), headroom)
            if amt <= 0:
                continue
            span = (window_end - window_start).days
            rday = window_start + timedelta(days=rng.randrange(span + 1))
            if period_index_for(rday) is None:
                continue
            refund_seq += 1
            ext = {"payment_id": src["payment_id"], "order_id": src["order_id"],
                   "customer_id": src["customer_id"], "amount_paise": src["amount_paise"],
                   "payment_status": "CAPTURED", "payment_method": src["payment_method"],
                   "_capture_day": src["capture_day"], "_settlement_id": src["settlement_id"],
                   "_external": True}
            ds.external_payments.append(ext)
            ds.refunds.append({"refund_id": f"R_{refund_seq:05d}", "payment_id": src["payment_id"],
                               "refund_amount_paise": amt, "refund_status": "PROCESSED",
                               "refund_date": rday, "refund_reason": rng.choice(REFUND_REASONS),
                               "_late": True})
        ds._seq["refund"] = refund_seq

    # --- 5. seller allocations -------------------------------------------
    alloc_seq = ds._seq.get("alloc", 0)
    for p in captured:
        n_sellers = 1 if rng.random() < 0.72 else 2
        chosen = rng.sample(active_sellers, k=n_sellers)
        remaining = p["amount_paise"]
        for j, seller in enumerate(chosen):
            if j == len(chosen) - 1:
                gross = remaining
            else:
                gross = bps(p["amount_paise"], rng.randrange(3000, 7000))
                gross = max(100, min(gross, remaining - 100))
            remaining -= gross
            commission = bps(gross, seller["commission_bps"])
            alloc_seq += 1
            status = "SETTLED" if rng.random() < 0.93 else ("PENDING" if rng.random() < 0.7 else "REVERSED")
            ds.allocations.append({
                "allocation_id": f"A_{alloc_seq:05d}", "payment_id": p["payment_id"],
                "seller_id": seller["seller_id"], "gross_allocated_paise": gross,
                "commission_paise": commission, "net_seller_paise": gross - commission,
                "allocation_status": status,
                "allocation_date": p["_capture_day"] + timedelta(days=1)})

    # --- 6. transfers (mirror SETTLED allocations exactly) ----------------
    transfer_seq = ds._seq.get("transfer", 0)
    for a in ds.allocations:
        if a["allocation_status"] != "SETTLED":
            continue
        transfer_seq += 1
        ds.transfers.append({
            "transfer_id": f"T_{transfer_seq:05d}", "payment_id": a["payment_id"],
            "seller_id": a["seller_id"], "amount_paise": a["net_seller_paise"],
            "transfer_status": "PROCESSED",
            "transfer_date": a["allocation_date"] + timedelta(days=1),
            "transfer_reference": f"RTF{transfer_seq:07d}",
            "_allocation_id": a["allocation_id"]})
    ds._seq["transfer"] = transfer_seq

    # --- 7/8/9. settlements, items, headers ------------------------------
    payments_by_period: dict[int, list[dict]] = {}
    for p in captured:
        payments_by_period.setdefault(p["_period"], []).append(p)
    refunds_by_period: dict[int, list[dict]] = {}
    for r in ds.refunds:
        if r["refund_status"] != "PROCESSED":
            continue
        pi = period_index_for(r["refund_date"])
        if pi is not None:
            refunds_by_period.setdefault(pi, []).append(r)

    si_seq = ds._seq.get("si", 0)
    set_base = 0 if origin is None else origin.settlement_offset
    for i, (ps, pe, sd) in enumerate(periods):
        sid = f"SET_{set_base + i + 1:04d}"
        gross = fee = tax = refund_total = 0
        for p in sorted(payments_by_period.get(i, []), key=lambda x: x["payment_id"]):
            f = bps(p["amount_paise"], policy.mdr_bps(p["payment_method"]))
            t = bps(f, policy.gst_on_fee_bps)
            si_seq += 1
            ds.settlement_items.append({
                "settlement_item_id": f"SI_{si_seq:06d}", "settlement_id": sid,
                "transaction_type": "PAYMENT", "payment_id": p["payment_id"],
                "refund_id": None, "adjustment_id": None, "transfer_id": None,
                "amount_paise": p["amount_paise"], "fee_paise": f, "tax_paise": t,
                "transaction_date": p["_capture_day"]})
            gross += p["amount_paise"]; fee += f; tax += t
            p["_settlement_id"] = sid
        for r in sorted(refunds_by_period.get(i, []), key=lambda x: x["refund_id"]):
            si_seq += 1
            ds.settlement_items.append({
                "settlement_item_id": f"SI_{si_seq:06d}", "settlement_id": sid,
                "transaction_type": "REFUND", "payment_id": None,
                "refund_id": r["refund_id"], "adjustment_id": None, "transfer_id": None,
                "amount_paise": -r["refund_amount_paise"], "fee_paise": 0, "tax_paise": 0,
                "transaction_date": r["refund_date"]})
            refund_total += r["refund_amount_paise"]
            r["_settlement_id"] = sid
        ds.settlements.append({
            "settlement_id": sid, "settlement_date": sd,
            "settlement_period_start": ps, "settlement_period_end": pe,
            "gross_amount_paise": gross, "refund_amount_paise": refund_total,
            "fee_amount_paise": fee, "tax_amount_paise": tax,
            "adjustment_amount_paise": 0,
            "net_settlement_amount_paise": gross - refund_total - fee - tax,
            "settlement_status": "PROCESSED", "settlement_utr": utr_for(set_base + i + 1, sd)})
    ds._seq["si"] = si_seq

    # --- adjustments (clean ones ARE itemised; the anomaly pass adds the
    #     unitemised kind later) -----------------------------------------
    adj_seq = ds._seq.get("adj", 0)
    for s in rng.sample(ds.settlements, k=min(70, len(ds.settlements))):
        adj_seq += 1
        atype = weighted_choice(rng, [("ROLLING_RESERVE_HOLD", 30), ("ROLLING_RESERVE_RELEASE", 25),
                                      ("CHARGEBACK", 20), ("CHARGEBACK_REVERSAL", 15), ("MANUAL", 10)])
        magnitude = rng.randrange(5000, 400000, 100)
        signed = -magnitude if atype in ("ROLLING_RESERVE_HOLD", "CHARGEBACK") else magnitude
        aid = f"ADJ_{adj_seq:05d}"
        ds.adjustments.append({
            "adjustment_id": aid, "settlement_id": s["settlement_id"], "adjustment_type": atype,
            "amount_paise": signed, "reason": f"{atype.replace('_',' ').title()} for {s['settlement_id']}",
            "created_at": ts(s["settlement_date"], rng), "status": "APPLIED", "ref_payment_id": None})
        si_seq += 1
        ds.settlement_items.append({
            "settlement_item_id": f"SI_{si_seq:06d}", "settlement_id": s["settlement_id"],
            "transaction_type": "ADJUSTMENT", "payment_id": None, "refund_id": None,
            "adjustment_id": aid, "transfer_id": None, "amount_paise": signed,
            "fee_paise": 0, "tax_paise": 0, "transaction_date": s["settlement_date"]})
        s["adjustment_amount_paise"] += signed
        s["net_settlement_amount_paise"] += signed
    ds._seq["si"] = si_seq
    ds._seq["adj"] = adj_seq

    # TRANSFER items -- lineage only, never summed into Delta-1 (spec 3.3)
    transfers_by_date: dict[date, list[dict]] = {}
    for t in ds.transfers:
        transfers_by_date.setdefault(t["transfer_date"], []).append(t)
    for i, (ps, pe, sd) in enumerate(periods):
        sid = f"SET_{set_base + i + 1:04d}"
        pool = [t for day, lst in transfers_by_date.items() if ps <= day <= pe for t in lst]
        for t in rng.sample(pool, k=min(8, len(pool))) if pool else []:
            si_seq += 1
            ds.settlement_items.append({
                "settlement_item_id": f"SI_{si_seq:06d}", "settlement_id": sid,
                "transaction_type": "TRANSFER", "payment_id": None, "refund_id": None,
                "adjustment_id": None, "transfer_id": t["transfer_id"],
                "amount_paise": t["amount_paise"], "fee_paise": 0, "tax_paise": 0,
                "transaction_date": t["transfer_date"]})
    ds._seq["si"] = si_seq

    # --- 10. bank transactions -------------------------------------------
    bank_seq = ds._seq.get("bank", 0)

    # Money in flight from the previous batch lands first. Its settlement was
    # reported unmatched last cycle; this is the credit that closes it.
    if origin is not None:
        for pend in origin.pending_bank:
            if pend["net_settlement_amount_paise"] <= 0:
                continue
            bank_seq += 1
            ds.bank_transactions.append({
                "bank_transaction_id": f"B_{bank_seq:05d}",
                "transaction_date": add_working_days(pend["settlement_date"],
                                                     policy.expected_lag_days, policy),
                "description": f"NEFT CR-RAZORPAY SOFTWARE-{pend['settlement_utr']}",
                "credit_paise": pend["net_settlement_amount_paise"], "debit_paise": 0,
                "bank_reference": f"BREF{bank_seq:08d}",
                "settlement_utr": pend["settlement_utr"],
                "_settlement_id": pend["settlement_id"]})

    # The last settlement of a batch has not been credited yet at the moment the
    # batch closes -- T+2 lands after the cutoff. Withholding it is what makes a
    # tick produce an exception that the NEXT tick resolves, rather than a book
    # that is implausibly complete the instant it is written.
    # Only a settlement that actually owes the merchant money can have a credit
    # in flight. Where refunds swallowed the net there is nothing to wait for.
    withhold = None
    if origin is not None and withhold_tail_credit:
        for cand in reversed(ds.settlements):
            if cand["net_settlement_amount_paise"] > 0:
                withhold = cand["settlement_id"]
                break
    ds.withheld_bank = [withhold] if withhold else []

    for s in ds.settlements:
        if s["settlement_id"] == withhold:
            continue
        # A settlement whose refunds swallowed the net owes money rather than
        # receiving it, so there is no incoming credit to find. The matcher
        # already skips these (matcher.py guards net > 0); emitting a negative
        # "credit" would be a lie about the bank statement -- and the DDL, which
        # requires credit_paise >= 0, refuses it outright.
        if s["net_settlement_amount_paise"] <= 0:
            continue
        bank_seq += 1
        utr = s["settlement_utr"]
        roll = rng.random()
        if roll < 0.60:
            desc = f"NEFT CR-RAZORPAY SOFTWARE-{utr}"
            carried = utr
        elif roll < 0.80:
            desc = f"NEFT CR RZRPAY {utr[-8:]}"
            carried = utr
        else:
            desc = "NEFT-RAZORPAYSOFTWAREPVTLTD-SETTLEMENT"
            carried = utr
        ds.bank_transactions.append({
            "bank_transaction_id": f"B_{bank_seq:05d}",
            "transaction_date": s["settlement_date"] + timedelta(days=policy.expected_lag_days),
            "description": desc, "credit_paise": s["net_settlement_amount_paise"],
            "debit_paise": 0, "bank_reference": f"BREF{bank_seq:08d}", "settlement_utr": carried,
            "_settlement_id": s["settlement_id"]})
    # noise lines: unrelated debits and credits that must never be matched
    for _ in range(40):
        bank_seq += 1
        s = rng.choice(ds.settlements)
        is_credit = rng.random() < 0.35
        amt = rng.randrange(10000, 900000, 100)
        ds.bank_transactions.append({
            "bank_transaction_id": f"B_{bank_seq:05d}",
            "transaction_date": s["settlement_date"] + timedelta(days=rng.randrange(-2, 3)),
            "description": rng.choice([
                "NEFT DR-VENDOR PAYOUT-INFRA", "IMPS DR-OFFICE RENT", "UPI CR-MISC RECEIPT",
                "NEFT DR-GST CHALLAN", "CHQ DEP-CUSTOMER", "ACH DR-PAYROLL"]),
            "credit_paise": amt if is_credit else 0, "debit_paise": 0 if is_credit else amt,
            "bank_reference": f"BREF{bank_seq:08d}", "settlement_utr": None,
            "_settlement_id": None})
    ds._seq["bank"] = bank_seq

    # --- 11. the pipeline: captured, not yet settled ----------------------
    # Money taken in the days AFTER the last period closed, which no settlement
    # has picked up. Added AFTER the settlement items are built, so it is never
    # itemised and never enters a delta -- it is not reconcilable yet. This is
    # what the cash forecast is made of.
    #
    # Additive: the settlements above are untouched, so the batch's reported
    # figures and every planted anomaly stay exactly as they were.
    last_end = periods[-1][1]
    pipeline_days = 4
    for d_off in range(1, pipeline_days + 1):
        day = last_end + timedelta(days=d_off)
        if not is_working_day(day, policy):
            continue
        for _ in range(rng.randrange(8, 18)):
            order_seq += 1
            payment_seq += 1
            oid, pid = f"O_{order_seq:05d}", f"P_{payment_seq:05d}"
            cust = rng.choice(customer_pool)
            amount = rng.choice([rng.randrange(19900, 500000, 100),
                                 rng.randrange(50000, 2500000, 100)])
            ds.orders.append({"order_id": oid, "customer_id": cust["customer_id"],
                              "order_amount_paise": amount, "order_date": day,
                              "order_status": "PAID"})
            ds.payments.append({
                "payment_id": pid, "order_id": oid, "customer_id": cust["customer_id"],
                "amount_paise": amount, "payment_status": "CAPTURED",
                "payment_method": weighted_choice(rng, METHOD_WEIGHTS),
                "created_at": ts(day, rng), "captured_at": ts(day, rng),
                "failure_reason": None, "_capture_day": day, "_pipeline": True})
            # a share is owed to sellers but the payout has not run: PENDING, so
            # Delta-4 leaves it alone and the forecast can count it as outflow
            if rng.random() < 0.7:
                seller = rng.choice(active_sellers)
                gross = bps(amount, rng.randrange(4000, 9000))
                commission = bps(gross, seller["commission_bps"])
                alloc_seq += 1
                ds.allocations.append({
                    "allocation_id": f"A_{alloc_seq:05d}", "payment_id": pid,
                    "seller_id": seller["seller_id"], "gross_allocated_paise": gross,
                    "commission_paise": commission, "net_seller_paise": gross - commission,
                    "allocation_status": "PENDING", "allocation_date": day})

    ds._grp_seq = 0
    ds._periods = periods
    ds.pipeline_from = last_end + timedelta(days=1)
    ds.index()
    return ds


# =============================================================================
# Ledger postings (step 11) -- built AFTER the non-ledger anomaly pass, from the
# final state of payments and refunds. Building it earlier would leave every
# refund the anomaly pass introduces without its double-entry, manufacturing a
# fake Delta-3 on settlements that are supposed to be clean.
# =============================================================================
def build_ledger(ds: Dataset) -> None:
    policy = ds.policy
    captured = [p for p in ds.payments if p["payment_status"] == "CAPTURED"]
    # fee/tax as ACTUALLY CHARGED on the settlement line, not as policy says it
    # should have been -- the merchant's books record what happened, and Delta-1
    # is what compares that against policy.
    charged: dict[str, tuple[int, int]] = {}
    for it in ds.settlement_items:
        if it["transaction_type"] == "PAYMENT" and it["payment_id"]:
            charged[it["payment_id"]] = (it["fee_paise"], it["tax_paise"])
    # --- postings ---------------------------------------------
    # Three posting events (spec 4.4). The settlement posting nets the payment's
    # FULL lifetime refunds, so a fully settled payment's RAZORPAY_CLEARING
    # balance is exactly zero -- which is what makes Delta-3 a pure detector of
    # duplicate / missing / misposted entries rather than a timing artefact.
    led_seq = ds._seq.get("ledger", 0)
    grp_seq = ds._seq.get("grp", 0)

    def post(group: str, account: str, direction: str, amount: int, day: date, **refs) -> None:
        nonlocal led_seq
        if amount <= 0:
            return
        led_seq += 1
        ds.ledger_entries.append({
            "ledger_entry_id": f"L_{led_seq:06d}", "entry_group_id": group, "account": account,
            "direction": direction, "amount_paise": amount, "ledger_date": day,
            "order_id": refs.get("order_id"), "payment_id": refs.get("payment_id"),
            "refund_id": refs.get("refund_id"), "settlement_id": refs.get("settlement_id"),
            "seller_id": refs.get("seller_id"),
            "description": refs.get("description", f"{account} {direction}")})

    refunds_by_payment: dict[str, list[dict]] = {}
    for r in ds.refunds:
        if r["refund_status"] == "PROCESSED":
            refunds_by_payment.setdefault(r["payment_id"], []).append(r)

    for p in captured:
        g = p["amount_paise"]
        grp_seq += 1
        gcap = f"G_{grp_seq:06d}"
        post(gcap, "RAZORPAY_CLEARING", "DR", g, p["_capture_day"],
             payment_id=p["payment_id"], order_id=p["order_id"], description="capture")
        post(gcap, "SALES", "CR", g, p["_capture_day"],
             payment_id=p["payment_id"], order_id=p["order_id"], description="capture")
        r_total = 0
        for r in refunds_by_payment.get(p["payment_id"], []):
            grp_seq += 1
            gref = f"G_{grp_seq:06d}"
            post(gref, "REFUNDS", "DR", r["refund_amount_paise"], r["refund_date"],
                 payment_id=p["payment_id"], refund_id=r["refund_id"], description="refund")
            post(gref, "RAZORPAY_CLEARING", "CR", r["refund_amount_paise"], r["refund_date"],
                 payment_id=p["payment_id"], refund_id=r["refund_id"], description="refund")
            r_total += r["refund_amount_paise"]
        sid = p.get("_settlement_id")
        if not sid:
            continue
        s = next(x for x in ds.settlements if x["settlement_id"] == sid)
        f, t = charged.get(p["payment_id"],
                           (bps(g, policy.mdr_bps(p["payment_method"])), 0))
        if p["payment_id"] not in charged:
            t = bps(f, policy.gst_on_fee_bps)
        c = g - r_total
        n = c - f - t
        grp_seq += 1
        gset = f"G_{grp_seq:06d}"
        # The gateway fee is charged even when the payment was fully refunded
        # (POLICY.REFUND.mdr_refunded = false), so the fee and GST legs are
        # posted unconditionally. When refunds swallow the net, the bank leg
        # flips to a credit -- money leaving the account rather than arriving.
        # Either way DR(f + t) +/- bank == CR(c), so the group balances and
        # RAZORPAY_CLEARING still nets to exactly zero per payment.
        if n >= 0:
            post(gset, "BANK", "DR", n, s["settlement_date"],
                 payment_id=p["payment_id"], settlement_id=sid, description="settlement")
        else:
            post(gset, "BANK", "CR", -n, s["settlement_date"],
                 payment_id=p["payment_id"], settlement_id=sid,
                 description="settlement (fees exceed net after refunds)")
        post(gset, "GATEWAY_FEES", "DR", f, s["settlement_date"],
             payment_id=p["payment_id"], settlement_id=sid, description="settlement fee")
        post(gset, "INPUT_GST", "DR", t, s["settlement_date"],
             payment_id=p["payment_id"], settlement_id=sid, description="settlement gst")
        post(gset, "RAZORPAY_CLEARING", "CR", c, s["settlement_date"],
             payment_id=p["payment_id"], settlement_id=sid, description="settlement")
        p["_ledger_settlement_group"] = gset
    settled_elsewhere = {p["payment_id"] for p in ds.external_payments}
    by_sid = {x["settlement_id"]: x for x in ds.settlements}
    for r in ds.refunds:
        if not r.get("_late") or r["payment_id"] not in settled_elsewhere:
            continue
        if r["refund_status"] != "PROCESSED" or not r.get("_settlement_id"):
            continue
        grp_seq += 1
        glate = f"G_{grp_seq:06d}"
        day = by_sid[r["_settlement_id"]]["settlement_date"]
        post(glate, "REFUNDS", "DR", r["refund_amount_paise"], day,
             payment_id=r["payment_id"], refund_id=r["refund_id"],
             settlement_id=r["_settlement_id"], description="late refund on closed settlement")
        post(glate, "BANK", "CR", r["refund_amount_paise"], day,
             payment_id=r["payment_id"], refund_id=r["refund_id"],
             settlement_id=r["_settlement_id"], description="late refund on closed settlement")

    ds._seq["ledger"] = led_seq
    ds._seq["grp"] = grp_seq
    ds._grp_seq = grp_seq


# =============================================================================
# money_edges (step 13 -- built from FINAL state, after anomalies)
# =============================================================================
def build_money_edges(ds: Dataset) -> None:
    ds.money_edges.clear()
    seen = set()

    def edge(st, si, dt, di, kind, amt=None):
        key = (st, si, dt, di, kind)
        if si is None or di is None or key in seen:
            return
        seen.add(key)
        ds.money_edges.append({"src_type": st, "src_id": si, "dst_type": dt, "dst_id": di,
                               "edge_kind": kind, "amount_paise": amt})

    for o in ds.orders:
        edge("customer", o["customer_id"], "order", o["order_id"], "PLACED", o["order_amount_paise"])
    for p in ds.payments:
        edge("order", p["order_id"], "payment", p["payment_id"], "PAID_BY", p["amount_paise"])
    for r in ds.refunds:
        edge("payment", r["payment_id"], "refund", r["refund_id"], "REFUNDED_BY", r["refund_amount_paise"])
    for a in ds.allocations:
        edge("payment", a["payment_id"], "seller_allocation", a["allocation_id"],
             "ALLOCATED_TO", a["gross_allocated_paise"])
    alloc_by_ps = {}
    for a in ds.allocations:
        alloc_by_ps.setdefault((a["payment_id"], a["seller_id"]), a)
    for t in ds.transfers:
        edge("payment", t["payment_id"], "transfer", t["transfer_id"], "TRANSFERRED_BY", t["amount_paise"])
        a = alloc_by_ps.get((t["payment_id"], t["seller_id"]))
        if a:
            edge("seller_allocation", a["allocation_id"], "transfer", t["transfer_id"],
                 "PAID_OUT_AS", t["amount_paise"])
    for it in ds.settlement_items:
        src = (("payment", it["payment_id"]) if it["payment_id"] else
               ("refund", it["refund_id"]) if it["refund_id"] else
               ("adjustment", it["adjustment_id"]) if it["adjustment_id"] else
               ("transfer", it["transfer_id"]))
        edge(src[0], src[1], "settlement_item", it["settlement_item_id"], "SETTLED_AS", it["amount_paise"])
        edge("settlement_item", it["settlement_item_id"], "settlement", it["settlement_id"],
             "PART_OF", it["amount_paise"])
    for b in ds.bank_transactions:
        if b.get("_settlement_id"):
            edge("settlement", b["_settlement_id"], "bank_transaction", b["bank_transaction_id"],
                 "CREDITED_AS", b["credit_paise"])
    for le in ds.ledger_entries:
        if le["payment_id"]:
            edge("payment", le["payment_id"], "ledger_entry", le["ledger_entry_id"],
                 "POSTED_AS", le["amount_paise"])
        if le["settlement_id"]:
            edge("settlement", le["settlement_id"], "ledger_entry", le["ledger_entry_id"],
                 "POSTED_AS", le["amount_paise"])


# =============================================================================
# Pipeline
# =============================================================================
def build(seed: int, n_settlements: int, policy: Policy, label: str | None,
          with_anomalies: bool = True, origin=None) -> Dataset:
    """clean data -> non-ledger anomalies -> ledger -> ledger anomalies ->
    lineage. The ordering is load-bearing; see build_ledger().

    `origin` turns this into an append: the same pipeline, run over a slice that
    continues an existing dataset rather than starting one. Using one pipeline
    for both is deliberate -- a separate append generator would drift away from
    the real one and the appended data would stop being a fair test."""
    from generator.anomalies import apply_anomalies_ledger, apply_anomalies_pre_ledger

    salt = 0 if origin is None else origin.batch * 0x9E3779B1
    ds = generate_clean(seed, n_settlements, policy, label, origin=origin,
                        withhold_tail_credit=with_anomalies)
    if with_anomalies:
        apply_anomalies_pre_ledger(ds, random.Random(seed ^ 0xA1B2C3 ^ salt))
    build_ledger(ds)
    ds.index()
    if with_anomalies:
        apply_anomalies_ledger(ds, random.Random(seed ^ 0xD3D3D3 ^ salt))
    build_money_edges(ds)
    ds.index()
    return ds


# =============================================================================
# Persistence
# =============================================================================
def row_counts(ds: Dataset) -> dict:
    counts = {
        "customers": len(ds.customers), "sellers": len(ds.sellers), "orders": len(ds.orders),
        "payments": len(ds.payments), "refunds": len(ds.refunds),
        "seller_allocations": len(ds.allocations), "transfers": len(ds.transfers),
        "adjustments": len(ds.adjustments), "settlements": len(ds.settlements),
        "settlement_items": len(ds.settlement_items),
        "bank_transactions": len(ds.bank_transactions), "ledger_entries": len(ds.ledger_entries),
        "money_edges": len(ds.money_edges), "ground_truth_anomalies": len(ds.ground_truth)}
    counts["total_financial_records"] = sum(
        counts[k] for k in ("payments", "refunds", "seller_allocations", "transfers",
                            "adjustments", "settlement_items", "bank_transactions",
                            "ledger_entries"))
    return counts


# The supplier GSTIN on the synthetic tax invoices. Deliberately a placeholder
# containing "DEMO" so it can never be read as a real registration.
TAX_SUPPLIER_GSTIN = "29DEMOP0000D1Z5"


def persist(ds: Dataset, conn) -> dict:
    d = ds.dataset_id
    counts = row_counts(ds)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO datasets (dataset_id, seed, policy_version, row_counts, label) "
                    "VALUES (%s,%s,%s,%s,%s)",
                    (d, ds.seed, ds.policy.version, json.dumps(counts), ds.label))
    copy_entities(ds, conn)
    conn.commit()
    return counts



def _copy_tax_invoices(ds: Dataset, conn) -> int:
    """The GSTR-2B feed: one gateway tax invoice per settlement.

    Additive and optional. `tax_invoices` lives in db/tax.sql, not the destructive
    schema.sql, so an installation that has not run it just gets a dataset with no
    tax feed and everything else behaves identically.

    The invoices are DERIVED from settlements that already exist -- the taxable
    value is the fee and the tax is what the settlement charged. No new randomness
    and no new anomaly pass: the interesting tax findings on a seeded dataset come
    for free from the ledger anomalies already planted (a missing or duplicated
    entry group takes INPUT_GST with it), which is a more honest source of signal
    than defects invented specially for this table.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('public.tax_invoices') AS t")
        if not cur.fetchone()["t"]:
            return 0
        # copy_entities is shared with append mode, which hands us a dataset that
        # may include settlements already persisted. COPY cannot ON CONFLICT, so
        # the existing invoice numbers are read once and skipped.
        cur.execute("SELECT invoice_no FROM tax_invoices WHERE dataset_id=%s",
                    (ds.dataset_id,))
        seen = {r["invoice_no"] for r in cur.fetchall()}

    rows = []
    for st in sorted(ds.settlements, key=lambda x: (x["settlement_date"], x["settlement_id"])):
        tax = int(st["tax_amount_paise"])
        if not tax:
            continue
        day = st["settlement_date"]
        # The serial is DERIVED from the settlement id, never from a counter.
        # A counter restarts on every append and collides on the second tick --
        # which is exactly the bug this replaced.
        digits = "".join(ch for ch in st["settlement_id"] if ch.isdigit()) or "0"
        no = f"DGS/{day.year}/{int(digits):05d}"
        if no in seen:
            continue
        seen.add(no)
        # Intra-state supply, so the total splits CGST/SGST with any odd paise
        # landing on CGST -- the way a supplier's own billing system rounds it.
        cgst, sgst = tax - tax // 2, tax // 2
        rows.append((ds.dataset_id, no, day,
                     f"{day.year:04d}-{day.month:02d}", TAX_SUPPLIER_GSTIN, "INVOICE",
                     st["settlement_id"], int(st["fee_amount_paise"]), cgst, sgst, 0,
                     True, None, day))
    if not rows:
        return 0
    copy_rows(conn, "tax_invoices",
              ["dataset_id","invoice_no","invoice_date","return_period","supplier_gstin",
               "document_type","settlement_id","taxable_value_paise","cgst_paise",
               "sgst_paise","igst_paise","itc_eligible","ineligible_reason","filed_at"],
              rows)
    return len(rows)


def copy_entities(ds: Dataset, conn) -> None:
    """Every COPY in the project, in dependency order. Shared by a fresh
    generation and by an append so the two can never disagree about columns."""
    d = ds.dataset_id
    copy_rows(conn, "customers", ["dataset_id","customer_id","name","email","created_at"],
              [(d, c["customer_id"], c["name"], c["email"], c["created_at"]) for c in ds.customers])
    copy_rows(conn, "sellers", ["dataset_id","seller_id","seller_name","seller_type","commission_bps","status"],
              [(d, s["seller_id"], s["seller_name"], s["seller_type"], s["commission_bps"], s["status"])
               for s in ds.sellers])
    copy_rows(conn, "orders", ["dataset_id","order_id","customer_id","order_amount_paise","order_date","order_status"],
              [(d, o["order_id"], o["customer_id"], o["order_amount_paise"], o["order_date"], o["order_status"])
               for o in ds.orders])
    copy_rows(conn, "payments", ["dataset_id","payment_id","order_id","customer_id","amount_paise",
                                 "payment_status","payment_method","created_at","captured_at","failure_reason"],
              [(d, p["payment_id"], p["order_id"], p["customer_id"], p["amount_paise"], p["payment_status"],
                p["payment_method"], p["created_at"], p["captured_at"], p["failure_reason"]) for p in ds.payments])
    copy_rows(conn, "refunds", ["dataset_id","refund_id","payment_id","refund_amount_paise",
                                "refund_status","refund_date","refund_reason"],
              [(d, r["refund_id"], r["payment_id"], r["refund_amount_paise"], r["refund_status"],
                r["refund_date"], r["refund_reason"]) for r in ds.refunds])
    copy_rows(conn, "seller_allocations", ["dataset_id","allocation_id","payment_id","seller_id",
                                           "gross_allocated_paise","commission_paise","net_seller_paise",
                                           "allocation_status","allocation_date"],
              [(d, a["allocation_id"], a["payment_id"], a["seller_id"], a["gross_allocated_paise"],
                a["commission_paise"], a["net_seller_paise"], a["allocation_status"], a["allocation_date"])
               for a in ds.allocations])
    copy_rows(conn, "transfers", ["dataset_id","transfer_id","payment_id","seller_id","amount_paise",
                                  "transfer_status","transfer_date","transfer_reference"],
              [(d, t["transfer_id"], t["payment_id"], t["seller_id"], t["amount_paise"],
                t["transfer_status"], t["transfer_date"], t["transfer_reference"]) for t in ds.transfers])
    copy_rows(conn, "adjustments", ["dataset_id","adjustment_id","settlement_id","adjustment_type",
                                    "amount_paise","reason","created_at","status","ref_payment_id"],
              [(d, a["adjustment_id"], a["settlement_id"], a["adjustment_type"], a["amount_paise"],
                a["reason"], a["created_at"], a["status"], a["ref_payment_id"]) for a in ds.adjustments])
    copy_rows(conn, "settlements", ["dataset_id","settlement_id","settlement_date","settlement_period_start",
                                    "settlement_period_end","gross_amount_paise","refund_amount_paise",
                                    "fee_amount_paise","tax_amount_paise","adjustment_amount_paise",
                                    "net_settlement_amount_paise","settlement_status","settlement_utr"],
              [(d, s["settlement_id"], s["settlement_date"], s["settlement_period_start"],
                s["settlement_period_end"], s["gross_amount_paise"], s["refund_amount_paise"],
                s["fee_amount_paise"], s["tax_amount_paise"], s["adjustment_amount_paise"],
                s["net_settlement_amount_paise"], s["settlement_status"], s["settlement_utr"])
               for s in ds.settlements])
    copy_rows(conn, "settlement_items", ["dataset_id","settlement_item_id","settlement_id","transaction_type",
                                         "payment_id","refund_id","adjustment_id","transfer_id",
                                         "amount_paise","fee_paise","tax_paise","transaction_date"],
              [(d, i["settlement_item_id"], i["settlement_id"], i["transaction_type"], i["payment_id"],
                i["refund_id"], i["adjustment_id"], i["transfer_id"], i["amount_paise"], i["fee_paise"],
                i["tax_paise"], i["transaction_date"]) for i in ds.settlement_items])
    copy_rows(conn, "bank_transactions", ["dataset_id","bank_transaction_id","transaction_date","description",
                                          "credit_paise","debit_paise","bank_reference","settlement_utr"],
              [(d, b["bank_transaction_id"], b["transaction_date"], b["description"], b["credit_paise"],
                b["debit_paise"], b["bank_reference"], b["settlement_utr"]) for b in ds.bank_transactions])
    copy_rows(conn, "ledger_entries", ["dataset_id","ledger_entry_id","entry_group_id","account","direction",
                                       "amount_paise","order_id","payment_id","refund_id","settlement_id",
                                       "seller_id","ledger_date","description"],
              [(d, l["ledger_entry_id"], l["entry_group_id"], l["account"], l["direction"], l["amount_paise"],
                l["order_id"], l["payment_id"], l["refund_id"], l["settlement_id"], l["seller_id"],
                l["ledger_date"], l["description"]) for l in ds.ledger_entries])
    _copy_tax_invoices(ds, conn)
    copy_rows(conn, "money_edges", ["dataset_id","src_type","src_id","dst_type","dst_id","edge_kind","amount_paise"],
              [(d, e["src_type"], e["src_id"], e["dst_type"], e["dst_id"], e["edge_kind"], e["amount_paise"])
               for e in ds.money_edges])
    copy_rows(conn, "ground_truth_anomalies",
              ["dataset_id","anomaly_id","anomaly_type","subject_type","subject_id","settlement_id",
               "expected_delta_kind","expected_exception_type","original_field","original_value_paise",
               "mutated_value_paise","planted_amount_paise","is_resolvable","notes"],
              [(d, g["anomaly_id"], g["anomaly_type"], g["subject_type"], g["subject_id"], g["settlement_id"],
                g["expected_delta_kind"], g["expected_exception_type"], g["original_field"],
                g["original_value_paise"], g["mutated_value_paise"], g["planted_amount_paise"],
                g["is_resolvable"], g["notes"]) for g in ds.ground_truth])


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate a seeded reconciliation dataset.")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--settlements", type=int, default=100)
    ap.add_argument("--label", type=str, default=None)
    ap.add_argument("--clean", action="store_true", help="skip the anomaly mutation pass")
    args = ap.parse_args()

    policy = load_policy()
    ds = build(args.seed, args.settlements, policy, args.label, with_anomalies=not args.clean)
    with connect() as conn:
        # The dataset_id is derived from the seed, so regenerating a seed means
        # replacing that dataset rather than adding a second one. The API has
        # always done this; the CLI did not, so a second `make generate` on the
        # same seed failed on the primary key instead of doing the obvious thing.
        prior = fetch_one(conn, "SELECT label, generated_at FROM datasets WHERE dataset_id=%s",
                          (ds.dataset_id,))
        if prior:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM datasets WHERE dataset_id=%s", (ds.dataset_id,))
            conn.commit()
            print(f"replacing the existing seed-{args.seed} dataset "
                  f"(label {prior['label']!r}, generated {prior['generated_at']:%Y-%m-%d %H:%M}) "
                  f"-- its runs and appended cycles go with it")
        counts = persist(ds, conn)
    print(f"dataset_id = {ds.dataset_id}")
    print(f"seed       = {args.seed}   policy = {policy.version}   label = {args.label}")
    for k, v in counts.items():
        print(f"  {k:26s} {v:>8,d}")
    print(f"  planted anomalies          {len(ds.ground_truth):>8,d}")


if __name__ == "__main__":
    main()
