"""
Authors fixtures/evaluation_batch.json -- the static evaluation batch.

Run once; the JSON it writes is the artifact that ships and the thing evaluators
read. There is NO randomness here and no seed: every amount, rate and date below
is written deliberately, and every fee and tax is derived from policy.yaml by the
same bps() the engine uses, so the batch cannot drift from the policy it claims
to follow.

    python -m fixtures.authoring          # rewrites fixtures/evaluation_batch.json

The batch is deliberately small enough to audit by hand. Each scenario states
what was done to it, what the engine should therefore conclude, and why -- and
tests/test_evaluation_batch.py asserts the engine actually agrees.
"""
from __future__ import annotations

import json
import pathlib
from datetime import date, timedelta

from engine.money import bps
from engine.policy import load_policy
from generator.calendar import add_working_days

OUT = pathlib.Path(__file__).resolve().parent / "evaluation_batch.json"

# A constant id, so loading the batch twice replaces it rather than accumulating
# copies, and so an evaluator can always find it.
DATASET_ID = "e0a1f5c2-0000-4000-8000-000000000001"

P = load_policy()
MDR, GST = P._mdr, P.gst_on_fee_bps
START = date(2026, 2, 2)


def fee(amount: int, method: str) -> int:
    return bps(amount, MDR[method])


def tax(f: int) -> int:
    return bps(f, GST)


def utr(n: int, d: str) -> str:
    return f"N{d.replace('-', '')[2:]}{n:08d}"


_order_customer: dict[str, str] = {}


def pay(pid, amount, method, day, *, charged_fee=None, charged_tax=None,
        status="CAPTURED", order=None):
    """One payment. `order` groups retry attempts: they share an order and,
    because a person does not change between attempts, a customer."""
    f = fee(amount, method)
    oid = order_for(order or pid)
    if oid not in _order_customer:
        _order_customer[oid] = next_customer()
    return {"payment_id": pid, "order_id": oid, "customer_id": _order_customer[oid],
            "amount_paise": amount, "payment_method": method, "captured_at": day,
            "payment_status": status,
            "policy_fee_paise": f, "policy_tax_paise": tax(f),
            "charged_fee_paise": f if charged_fee is None else charged_fee,
            "charged_tax_paise": tax(f) if charged_tax is None else charged_tax}


def refund(rid, pid, amount, day, *, status="PROCESSED", itemised=True, itemise_in=None):
    """`itemise_in` names a DIFFERENT settlement to carry the REFUND line. That is
    the period-gate trap: a refund dated after one period closes belongs to the
    next one, and must be itemised there rather than here."""
    return {"refund_id": rid, "payment_id": pid, "refund_amount_paise": amount,
            "refund_status": status, "refund_date": day, "itemised": itemised,
            "itemise_in": itemise_in}


def adjustment(aid, atype, amount, *, itemised=True, ref_payment=None):
    return {"adjustment_id": aid, "adjustment_type": atype, "amount_paise": amount,
            "status": "APPLIED", "itemised": itemised, "ref_payment_id": ref_payment}


def gt(anomaly_type, subject_type, subject_id, delta_kind, exception_type,
       amount, resolvable, notes):
    return {"anomaly_type": anomaly_type, "subject_type": subject_type,
            "subject_id": subject_id, "expected_delta_kind": delta_kind,
            "expected_exception_type": exception_type, "planted_amount_paise": amount,
            "is_resolvable": resolvable, "notes": notes}


# =============================================================================
# The population.
#
# A batch with one customer and two sellers reconciles just as well, but it does
# not look like a book anyone kept -- and an evaluator reading it should be
# looking at the anomalies, not wondering why 35 payments came from the same
# person. Names are ordinary Indian retail names; the emails are example.in, so
# nothing here can be mistaken for a real address.
# =============================================================================
CUSTOMERS = [
    ("Aarav Sharma", "aarav.sharma"), ("Diya Iyer", "diya.iyer"),
    ("Vihaan Reddy", "vihaan.reddy"), ("Ananya Nair", "ananya.nair"),
    ("Arjun Menon", "arjun.menon"), ("Ishita Bose", "ishita.bose"),
    ("Kabir Singh", "kabir.singh"), ("Meera Pillai", "meera.pillai"),
    ("Rohan Gupta", "rohan.gupta"), ("Saanvi Rao", "saanvi.rao"),
    ("Aditya Joshi", "aditya.joshi"), ("Nisha Kulkarni", "nisha.kulkarni"),
    ("Vikram Desai", "vikram.desai"), ("Priya Chopra", "priya.chopra"),
    ("Karan Malhotra", "karan.malhotra"), ("Tara Banerjee", "tara.banerjee"),
    ("Rahul Shetty", "rahul.shetty"), ("Sneha Ghosh", "sneha.ghosh"),
    ("Manish Verma", "manish.verma"), ("Kavya Patel", "kavya.patel"),
    ("Devansh Kapoor", "devansh.kapoor"), ("Riya Krishnan", "riya.krishnan"),
    ("Yash Agarwal", "yash.agarwal"), ("Neha Subramanian", "neha.subramanian"),
    ("Siddharth Rana", "siddharth.rana"), ("Anika Deshpande", "anika.deshpande"),
]
CUSTOMER_ROWS = [
    {"customer_id": f"CUST_{i:03d}", "name": name, "email": f"{handle}@example.in",
     # signup dates spread across the two months before the batch opens
     "created_at": (date(2025, 12, 1) + timedelta(days=(i * 7) % 60)).isoformat()}
    for i, (name, handle) in enumerate(CUSTOMERS, start=1)
]

# Six sellers spanning all three commission tiers the policy defines, and one
# SUSPENDED -- a marketplace always has a few.
SELLER_ROWS = [
    {"seller_id": "SELL_01", "seller_name": "Kanha Traders", "seller_type": "SMB"},
    {"seller_id": "SELL_02", "seller_name": "Nilgiri Organics", "seller_type": "ENTERPRISE"},
    {"seller_id": "SELL_03", "seller_name": "Marigold Crafts", "seller_type": "INDIVIDUAL"},
    {"seller_id": "SELL_04", "seller_name": "Banyan Textiles", "seller_type": "SMB"},
    {"seller_id": "SELL_05", "seller_name": "Coral Electronics", "seller_type": "ENTERPRISE"},
    {"seller_id": "SELL_06", "seller_name": "Peepal Supplies", "seller_type": "INDIVIDUAL",
     "status": "SUSPENDED"},
]
for _s in SELLER_ROWS:
    _s["commission_bps"] = P._commission[_s["seller_type"]]
    _s.setdefault("status", "ACTIVE")

ACTIVE_SELLERS = [s for s in SELLER_ROWS if s["status"] == "ACTIVE"]


def commission_for(seller_id: str) -> int:
    return next(s["commission_bps"] for s in SELLER_ROWS if s["seller_id"] == seller_id)


SCENARIOS: list[dict] = []
_cursor = {"day": START, "n": 0}
_seq = {"customer": 0, "order": 0, "alloc": 0, "transfer": 0}
_orders: dict[str, str] = {}


def next_customer() -> str:
    """Walk the population in order. Deterministic, and every customer is used
    before any is reused."""
    _seq["customer"] += 1
    return CUSTOMER_ROWS[(_seq["customer"] - 1) % len(CUSTOMER_ROWS)]["customer_id"]


def order_for(key: str) -> str:
    """Orders have their own identity. Two attempts at the same order share a
    key, and therefore share an order_id and a customer."""
    if key not in _orders:
        _seq["order"] += 1
        _orders[key] = f"ORD_{_seq['order']:04d}"
    return _orders[key]


def scenario(sid, title, family, note, *, span=2, **kw):
    """One settlement. Periods tile the calendar with no gaps and no overlaps,
    exactly as the real generator does -- the refund period gate depends on it."""
    _cursor["n"] += 1
    n = _cursor["n"]
    pstart = _cursor["day"]
    pend = pstart + timedelta(days=span - 1)
    sdate = add_working_days(pend, P.cycle_working_days, P)
    _cursor["day"] = pend + timedelta(days=1)

    s = {
        "scenario_id": sid, "title": title, "family": family, "note": note,
        "settlement": {
            "settlement_id": f"EV_{n:02d}",
            "settlement_period_start": pstart.isoformat(),
            "settlement_period_end": pend.isoformat(),
            "settlement_date": sdate.isoformat(),
            "settlement_status": kw.pop("settlement_status", "PROCESSED"),
            "settlement_utr": utr(n, sdate.isoformat()),
        },
        "payments": [], "refunds": [], "adjustments": [],
        "sellers": [], "allocations": [], "transfers": [],
        "bank": None, "ledger_mutation": None, "header_override": None,
        "ground_truth": [], "expected": {},
    }
    s.update(kw)
    SCENARIOS.append(s)
    return s


def clean_header(sc):
    """The header a settlement would carry if nothing were wrong with it."""
    cap = [p for p in sc["payments"] if p["payment_status"] == "CAPTURED"]
    gross = sum(p["amount_paise"] for p in cap)
    f = sum(p["charged_fee_paise"] for p in cap)
    t = sum(p["charged_tax_paise"] for p in cap)
    ref = sum(r["refund_amount_paise"] for r in sc["refunds"]
              if r["refund_status"] == "PROCESSED" and r["itemised"])
    adj = sum(a["amount_paise"] for a in sc["adjustments"] if a["itemised"])
    return {"gross_paise": gross, "fee_paise": f, "tax_paise": t,
            "refund_paise": ref, "adjustment_paise": adj,
            "net_paise": gross - ref - f - t + adj}


# =============================================================================
# CLEAN CONTROLS -- every delta must be exactly zero. Without these, a batch
# only proves the engine finds problems, not that it stays quiet when there
# are none.
# =============================================================================
scenario("EV01", "Clean settlement across three payment methods", "CLEAN",
         "UPI at 0 bps, CARD at 200, NETBANKING at 175, each fee and its 18% GST "
         "taken straight from the policy registry. Nothing is wrong here.",
         payments=[pay("EV01_P1", 250000, "UPI", "2026-02-02"),
                   pay("EV01_P2", 1000000, "CARD", "2026-02-03"),
                   pay("EV01_P3", 480000, "NETBANKING", "2026-02-02")],
         expected={"d1_paise": 0, "d2_paise": 0, "d3_paise": 0, "d4_paise": 0,
                   "worst_tier": "A", "exception_types": []})

scenario("EV02", "Retried order: two failed attempts, then a capture", "CLEAN",
         "EV02_P1, P2 and P3 are three attempts at the SAME order O_EV02_A. Only "
         "the captured attempt may reach a settlement -- a FAILED payment in a "
         "settlement is a hard structural error (INV-B5), so this is the control "
         "that proves the engine excludes them rather than summing them.",
         payments=[pay("EV02_P1", 620000, "CARD", "2026-02-04",
                       status="FAILED", order="O_EV02_A"),
                   pay("EV02_P2", 620000, "CARD", "2026-02-04",
                       status="FAILED", order="O_EV02_A"),
                   pay("EV02_P3", 620000, "CARD", "2026-02-04", order="O_EV02_A"),
                   pay("EV02_P4", 310000, "WALLET", "2026-02-05")],
         expected={"d1_paise": 0, "d2_paise": 0, "d3_paise": 0, "d4_paise": 0,
                   "worst_tier": "A", "exception_types": []})

scenario("EV03", "Refund inside the period, correctly deducted", "CLEAN",
         "300000 refunded against a 900000 CARD payment, dated inside the period "
         "and itemised. The gateway fee is NOT returned on a refund "
         "(POLICY.REFUND.mdr_refunded = false), so the fee still stands on the "
         "full 900000. Getting that backwards would show up as a Delta-1.",
         payments=[pay("EV03_P1", 900000, "CARD", "2026-02-06")],
         refunds=[refund("EV03_R1", "EV03_P1", 300000, "2026-02-07")],
         expected={"d1_paise": 0, "d2_paise": 0, "d3_paise": 0, "d4_paise": 0,
                   "worst_tier": "A", "exception_types": []})

# =============================================================================
# DELTA 1 -- the settlement's own arithmetic against the policy registry
# =============================================================================
_f_drift = 25000            # 250 bps charged
_t_drift = tax(_f_drift)    # GST on the inflated fee
scenario("EV04", "Gateway fee charged at 250 bps instead of the policy 200", "D1",
         f"Policy CARD MDR is 200 bps: 1000000 * 200 / 10000 = 20000, GST "
         f"{tax(20000)}. The settlement charged {_f_drift} and taxed its own "
         f"inflated fee at {_t_drift}. Short by "
         f"{(_f_drift - 20000) + (_t_drift - tax(20000))} paise, of which "
         f"{_f_drift - 20000} is fee and {_t_drift - tax(20000)} is the tax "
         f"consequence. Both legs must be attributed, leaving residual zero.",
         payments=[pay("EV04_P1", 1000000, "CARD", "2026-02-08",
                       charged_fee=_f_drift, charged_tax=_t_drift)],
         ground_truth=[gt("D1_FEE_RATE_DRIFT", "payment", "EV04_P1", "D1_COMPUTE",
                          "FEE_RATE_MISMATCH", (_f_drift - 20000) + (_t_drift - tax(20000)),
                          True, "Fee charged at 250 bps against a policy of 200.")],
         expected={"d1_paise": (_f_drift - 20000) + (_t_drift - tax(20000)),
                   "d2_paise": 0, "d3_paise": 0, "d4_paise": 0,
                   "worst_tier": "A", "exception_types": ["FEE_RATE_MISMATCH"]})

# Per-item GST summed, versus GST computed once on the summed fee. Three CARD
# payments whose individual fees each round half-up.
# Six CARD amounts whose individual GST each rounds half-up. Summed per item
# they come to two paise more than a single GST on the aggregate fee.
_amts = [120200, 180500, 240700, 300800, 361000, 421100]
_fees = [fee(a, "CARD") for a in _amts]
_per_item = sum(tax(f) for f in _fees)
_aggregate = tax(sum(_fees))
scenario("EV05", "GST computed on the aggregate fee instead of per item", "D1",
         f"Fees {_fees} sum to {sum(_fees)}. Policy says PER_ITEM: "
         f"{' + '.join(str(tax(f)) for f in _fees)} = {_per_item}. The settlement "
         f"taxed the total instead: GST({sum(_fees)}) = {_aggregate}. The "
         f"difference is {_per_item - _aggregate} paise -- small, systematic, and "
         f"invisible unless the engine recomputes per item.",
         payments=[pay(f"EV05_P{i+1}", a, "CARD", "2026-02-10") for i, a in enumerate(_amts)],
         aggregate_tax_paise=_aggregate,
         ground_truth=[gt("D1_TAX_AGGREGATE_ROUNDING", "settlement", "EV_05", "D1_COMPUTE",
                          "TAX_ROUNDING_MISMATCH", abs(_per_item - _aggregate), True,
                          f"GST taken on the aggregate fee; {_per_item - _aggregate} paise apart "
                          f"from the per-item method the policy mandates.")],
         expected={"d1_paise": _aggregate - _per_item,
                   "d2_paise": 0, "d3_paise": 0, "d4_paise": 0,
                   "worst_tier": "A", "exception_types": ["TAX_ROUNDING_MISMATCH"]})

scenario("EV06", "Processed refund inside the period, never deducted", "D1",
         "EV06_R1 is PROCESSED and dated inside the period, so the settlement "
         "owes 400000 less than it paid. The refund exists in the refunds table "
         "but no REFUND settlement item was written, so the header overstates the "
         "net by exactly the refund.",
         payments=[pay("EV06_P1", 1200000, "CARD", "2026-02-12")],
         refunds=[refund("EV06_R1", "EV06_P1", 400000, "2026-02-13", itemised=False)],
         ground_truth=[gt("D1_REFUND_NOT_DEDUCTED", "refund", "EV06_R1", "D1_COMPUTE",
                          "REFUND_NOT_DEDUCTED", 400000, True,
                          "A processed in-period refund that never reached the settlement.")],
         expected={"d1_paise": -400000, "d2_paise": 0, "d3_paise": 0, "d4_paise": 0,
                   "worst_tier": "A", "exception_types": ["REFUND_NOT_DEDUCTED"]})

scenario("EV07", "Refund after period close belongs to the next settlement", "TRAP",
         "EV07_R1 is dated 2026-02-16 -- after this settlement's period closes on "
         "2026-02-15 but before it pays out on 2026-02-18. Periods tile the "
         "calendar, so the refund belongs to EV_08, and it is correctly itemised "
         "there. Deducting it HERE would be a fabricated explanation, and "
         "flagging either settlement is a false positive. The correct answer is "
         "silence on both.",
         payments=[pay("EV07_P1", 800000, "CARD", "2026-02-14")],
         refunds=[refund("EV07_R1", "EV07_P1", 150000, "2026-02-16",
                         itemised=True, itemise_in="EV_08")],
         ground_truth=[gt("TRAP_REFUND_OUTSIDE_PERIOD", "refund", "EV07_R1", "D1_COMPUTE",
                          "NONE", 150000, True,
                          "Refund falls in EV_08's period, not this one. Neither settlement "
                          "may raise an exception.")],
         expected={"d1_paise": 0, "d2_paise": 0, "d3_paise": 0, "d4_paise": 0,
                   "worst_tier": "A", "exception_types": []})

scenario("EV08", "Header gross understates the sum of its own items", "D1",
         "The line items are correct; the header claims 75000 paise less gross "
         "than they add up to. settlement_items is the source of truth, so the "
         "header is what is wrong. It also receives EV07_R1, the refund whose "
         "period falls here -- correctly itemised, and therefore not an anomaly.",
         payments=[pay("EV08_P1", 700000, "CARD", "2026-02-16"),
                   pay("EV08_P2", 250000, "UPI", "2026-02-17")],
         header_override={"gross_delta_paise": -75000, "net_delta_paise": -75000},
         ground_truth=[gt("D1_HEADER_ROLLUP_MISMATCH", "settlement", "EV_08", "D1_COMPUTE",
                          "HEADER_ROLLUP_MISMATCH", 75000, True,
                          "Header gross is 75000 paise below the sum of its items.")],
         expected={"d1_paise": 75000, "d2_paise": 0, "d3_paise": 0, "d4_paise": 0,
                   "worst_tier": "A", "exception_types": ["HEADER_ROLLUP_MISMATCH"]})

scenario("EV09", "Chargeback applied to the header but never itemised", "D1",
         "A 90000 paise chargeback reduced the header net, but no ADJUSTMENT line "
         "item was written for it. The money moved and the adjustment is real -- "
         "the settlement simply does not show its working.",
         payments=[pay("EV09_P1", 950000, "CARD", "2026-02-18")],
         adjustments=[adjustment("EV09_A1", "CHARGEBACK", -90000, itemised=False,
                                 ref_payment="EV09_P1")],
         header_override={"net_delta_paise": -90000},
         ground_truth=[gt("D1_ADJUSTMENT_UNITEMISED", "adjustment", "EV09_A1", "D1_COMPUTE",
                          "ADJUSTMENT_UNEXPLAINED", 90000, True,
                          "Chargeback hit the header with no matching settlement item.")],
         expected={"d1_paise": 90000, "d2_paise": 0, "d3_paise": 0, "d4_paise": 0,
                   "worst_tier": "A", "exception_types": ["ADJUSTMENT_UNEXPLAINED"]})


# =============================================================================
# DELTA 2 -- did the money actually arrive in the bank
# =============================================================================
scenario("EV10", "No bank credit for this settlement at all", "D2",
         "The settlement is arithmetically perfect and the books agree, but no "
         "credit carrying this UTR, amount or date exists in the statement. "
         "Nothing was offered to any matching pass -- money absent, not "
         "mismatched.",
         payments=[pay("EV10_P1", 1400000, "CARD", "2026-02-20")],
         bank=[],
         ground_truth=[gt("D2_MISSING_CREDIT", "settlement", "EV_10", "D2_BANK",
                          "MISSING_BANK_CREDIT", 1349480, True,
                          "The settlement's net never arrived in the bank statement.")],
         expected={"d1_paise": 0, "d3_paise": 0, "d4_paise": 0,
                   "worst_tier": "C", "exception_types": ["MISSING_BANK_CREDIT"]})

scenario("EV11", "One settlement arrives as two unlabelled credits", "D2",
         "The bank paid it in two parts, neither carrying a UTR and neither "
         "matching the net on its own. Bounded subset-sum over unmatched credits "
         "resolves it -- and because the enumeration is bounded, the resolution "
         "is provable rather than a guess.",
         payments=[pay("EV11_P1", 900000, "CARD", "2026-02-22"),
                   pay("EV11_P2", 600000, "NETBANKING", "2026-02-23")],
         bank="SPLIT",
         ground_truth=[gt("D2_SPLIT_CREDIT", "settlement", "EV_11", "D2_BANK",
                          "SPLIT_BANK_CREDIT", 0, True,
                          "Arrived as two credits, neither carrying a UTR.")],
         expected={"d1_paise": 0, "d3_paise": 0, "d4_paise": 0,
                   "worst_tier": "A", "exception_types": ["SPLIT_BANK_CREDIT"]})

scenario("EV12", "Two settlements paid by one bulk credit (first leg)", "D2",
         "EV_12 and EV_13 were paid together as a single bank line with no UTR. "
         "Subset-sum has to work out that one credit covers exactly these two "
         "settlements and no other combination.",
         span=1, payments=[pay("EV12_P1", 700000, "CARD", "2026-02-24")],
         bank="MERGE_HEAD",
         ground_truth=[gt("D2_MERGED_CREDIT", "settlement", "EV_12", "D2_BANK",
                          "MERGED_BANK_CREDIT", 0, True,
                          "First leg of a bulk credit covering EV_12 and EV_13.")],
         expected={"d1_paise": 0, "d3_paise": 0, "d4_paise": 0,
                   "worst_tier": "A", "exception_types": ["MERGED_BANK_CREDIT"]})

scenario("EV13", "Two settlements paid by one bulk credit (second leg)", "D2",
         "The partner of EV_12. It has no bank line of its own; its money is "
         "inside EV_12's bulk credit.",
         span=1, payments=[pay("EV13_P1", 550000, "NETBANKING", "2026-02-26")],
         bank="MERGE_TAIL",
         ground_truth=[gt("D2_MERGED_CREDIT", "settlement", "EV_13", "D2_BANK",
                          "MERGED_BANK_CREDIT", 0, True,
                          "Second leg of the same bulk credit.")],
         expected={"d1_paise": 0, "d3_paise": 0, "d4_paise": 0,
                   "worst_tier": "A", "exception_types": ["MERGED_BANK_CREDIT"]})

scenario("EV14", "Bank narration carries only the last 8 digits of the UTR", "D2",
         "The credit is the right amount on the right day, but the narration was "
         "truncated to the UTR's tail. A suffix match is weaker evidence than a "
         "full reference, so the engine must resolve it on amount and date and "
         "record that it did -- not claim a UTR match it does not have.",
         payments=[pay("EV14_P1", 1100000, "CARD", "2026-02-28")],
         bank="SUFFIX",
         ground_truth=[gt("D2_NARRATION_NO_UTR", "settlement", "EV_14", "D2_BANK",
                          "NONE", 0, True,
                          "No UTR on the bank line and only a suffix in the narration. The credit "
                          "is inside the tolerance window, so exact amount + date resolves it at "
                          "tier A and the correct outcome is no exception at all.")],
         expected={"d1_paise": 0, "d3_paise": 0, "d4_paise": 0,
                   "worst_tier": "A", "exception_types": []})

# =============================================================================
# DELTA 3 -- the merchant's own double-entry books
# =============================================================================
scenario("EV15", "Settlement posting written twice", "D3",
         "The whole settlement entry group was posted a second time. Both copies "
         "balance internally, so an unbalanced-group check finds nothing -- only "
         "the clearing balance for the payment gives it away.",
         payments=[pay("EV15_P1", 1000000, "CARD", "2026-03-02")],
         ledger_mutation={"kind": "DUPLICATE_SETTLEMENT_GROUP", "payment_id": "EV15_P1"},
         ground_truth=[gt("D3_DUPLICATE_LEDGER", "payment", "EV15_P1", "D3_LEDGER",
                          "DUPLICATE_LEDGER_ENTRY", 976400, True,
                          "The settlement group was posted twice; clearing no longer nets to zero.")],
         expected={"d1_paise": 0, "d2_paise": 0, "d4_paise": 0,
                   "worst_tier": "A", "exception_types": ["DUPLICATE_LEDGER_ENTRY"]})

scenario("EV16", "Settlement posting missing entirely", "D3",
         "The capture was posted but the settlement never was, so the payment's "
         "clearing balance is still sitting at the full captured amount. The "
         "mirror image of EV15.",
         payments=[pay("EV16_P1", 850000, "CARD", "2026-03-04")],
         ledger_mutation={"kind": "DROP_SETTLEMENT_GROUP", "payment_id": "EV16_P1"},
         ground_truth=[gt("D3_MISSING_LEDGER", "payment", "EV16_P1", "D3_LEDGER",
                          "MISSING_LEDGER_ENTRY", 850000, True,
                          "No settlement posting; clearing still holds the captured amount.")],
         expected={"d1_paise": 0, "d2_paise": 0, "d4_paise": 0,
                   "worst_tier": "A", "exception_types": ["MISSING_LEDGER_ENTRY"]})

scenario("EV17", "Gateway fee posted to SALES instead of GATEWAY_FEES", "D3",
         "The entry group still balances and the clearing account still nets to "
         "exactly zero, so every arithmetic check passes. Only an account-level "
         "integrity check finds it -- and it matters, because revenue is "
         "overstated and input GST understated.",
         payments=[pay("EV17_P1", 1250000, "CARD", "2026-03-06")],
         ledger_mutation={"kind": "MISPOST_FEE_TO_SALES", "payment_id": "EV17_P1"},
         ground_truth=[gt("D3_WRONG_ACCOUNT", "payment", "EV17_P1", "D3_LEDGER",
                          "MISPOSTED_ACCOUNT", 25000, True,
                          "25000 paise of gateway fee posted to SALES; group still balances.")],
         expected={"d1_paise": 0, "d2_paise": 0, "d4_paise": 0,
                   "worst_tier": "A", "exception_types": ["MISPOSTED_ACCOUNT"]})


# =============================================================================
# DELTA 4 -- what each seller was owed against what actually moved
# =============================================================================
def alloc(aid, pid, seller_id, gross, *, status="SETTLED"):
    """One allocation. Commission comes from the seller's tier in policy.yaml,
    so INV-B4 (net = gross - commission) holds by construction."""
    c = bps(gross, commission_for(seller_id))
    return {"allocation_id": aid, "payment_id": pid, "seller_id": seller_id,
            "gross_allocated_paise": gross, "commission_paise": c,
            "net_seller_paise": gross - c, "allocation_status": status}


def paid_out(scenario, splits):
    """Allocate a settlement's payments to sellers and pay every one of them
    exactly what is owed.

    Most settlements in a marketplace move seller money, and almost all of it
    moves correctly. Without these the batch would contain seller payouts only
    where something is wrong with them, which is not a book -- and Delta-4 would
    have two data points and no controls.

    `splits` is [(payment_id, seller_id, share_bps), ...].
    """
    by_id = {p["payment_id"]: p for p in scenario["payments"]}
    for pid, seller_id, share_bps in splits:
        p = by_id[pid]
        if p["payment_status"] != "CAPTURED":
            continue
        gross = bps(p["amount_paise"], share_bps)
        _seq["alloc"] += 1
        a = alloc(f"ALLOC_{_seq['alloc']:04d}", pid, seller_id, gross)
        scenario["allocations"].append(a)
        _seq["transfer"] += 1
        scenario["transfers"].append({
            "transfer_id": f"TRF_{_seq['transfer']:04d}", "payment_id": pid,
            "seller_id": seller_id, "seller_amount_paise": a["net_seller_paise"],
            "transfer_status": "PROCESSED"})
    scenario["sellers"] = SELLER_ROWS
    return scenario


_a1 = alloc("ALLOC_D4A", "EV18_P1", "SELL_01", 800000)
_a2 = alloc("ALLOC_D4B", "EV18_P1", "SELL_05", 400000)
_SHORT = 30000
scenario("EV18", "Seller paid less than the allocation says they were owed", "D4",
         f"SELL_01 (Kanha Traders, SMB) was allocated 800000 gross at 1200 bps, so "
         f"{_a1['net_seller_paise']} was owed. Only "
         f"{_a1['net_seller_paise'] - _SHORT} was transferred. The settlement "
         f"itself reconciles perfectly on D1, D2 and D3 -- a marketplace can "
         f"balance to the paise at the top and still be short-paying a seller "
         f"underneath, which is the entire argument for reporting D4 separately.",
         payments=[pay("EV18_P1", 1200000, "CARD", "2026-03-08")],
         sellers=SELLER_ROWS, allocations=[_a1, _a2],
         transfers=[{"transfer_id": "EV18_T1", "payment_id": "EV18_P1",
                     "seller_id": "SELL_01", "seller_amount_paise":
                         _a1["net_seller_paise"] - _SHORT, "transfer_status": "PROCESSED"},
                    {"transfer_id": "EV18_T2", "payment_id": "EV18_P1",
                     "seller_id": "SELL_05", "seller_amount_paise": _a2["net_seller_paise"],
                     "transfer_status": "PROCESSED"},
                    # the reversal that accounts for the shortfall: explainable,
                    # not a mystery. Without this row the engine is right to
                    # refuse to resolve it.
                    {"transfer_id": "EV18_T3", "payment_id": "EV18_P1",
                     "seller_id": "SELL_01", "seller_amount_paise": _SHORT,
                     "transfer_status": "REVERSED"}],
         ground_truth=[gt("D4_ALLOC_TRANSFER_DIVERGENCE", "seller_allocation", "ALLOC_D4A",
                          "D4_PAYOUT", "ALLOCATION_TRANSFER_DIVERGENCE", _SHORT, True,
                          f"{_SHORT} paise short against a SETTLED allocation.")],
         expected={"d1_paise": 0, "d2_paise": 0, "d3_paise": 0, "d4_paise": _SHORT,
                   "worst_tier": "A", "exception_types": ["ALLOCATION_TRANSFER_DIVERGENCE"]})

_a3 = alloc("ALLOC_D4C", "EV19_P1", "SELL_03", 900000)
scenario("EV19", "Allocation marked settled with no transfer at all", "D4",
         f"The allocation says SETTLED and {_a3['net_seller_paise']} is owed, but "
         f"no transfer row exists. Not a rounding difference and not a timing "
         f"difference -- the payout never happened.",
         payments=[pay("EV19_P1", 950000, "CARD", "2026-03-10")],
         sellers=SELLER_ROWS, allocations=[_a3], transfers=[],
         ground_truth=[gt("D4_TRANSFER_MISSING", "seller_allocation", "ALLOC_D4C",
                          "D4_PAYOUT", "TRANSFER_MISSING", _a3["net_seller_paise"], True,
                          "SETTLED allocation with no transfer record.")],
         expected={"d1_paise": 0, "d2_paise": 0, "d3_paise": 0,
                   "d4_paise": _a3["net_seller_paise"],
                   "worst_tier": "A", "exception_types": ["TRANSFER_MISSING"]})

# =============================================================================
# FALSE-POSITIVE TRAPS -- correct data that LOOKS wrong. An engine that flags
# these is not being thorough, it is being unreliable, and a batch that only
# contains defects cannot tell the difference.
# =============================================================================
scenario("EV20", "Two genuinely separate payments, identical amount, same day", "TRAP",
         "EV20_P1 and EV20_P2 are 450000 on CARD, captured on the same day by two "
         "different customers on two different orders. They are not duplicates. "
         "Anything that flags them is producing a false positive, and the cost of "
         "that in production is a controller chasing money that is not missing.",
         payments=[pay("EV20_P1", 450000, "CARD", "2026-03-12", order="O_EV20_A"),
                   pay("EV20_P2", 450000, "CARD", "2026-03-12", order="O_EV20_B")],
         ground_truth=[gt("TRAP_SAME_AMOUNT_SAME_DAY", "settlement", "EV_20", "D1_COMPUTE",
                          "NONE", 0, True,
                          "Two legitimate identical payments. The engine must NOT raise "
                          "anything here.")],
         expected={"d1_paise": 0, "d2_paise": 0, "d3_paise": 0, "d4_paise": 0,
                   "worst_tier": "A", "exception_types": []})

scenario("EV21", "Ambiguous bank match, first of an identical pair", "TRAP",
         "EV_21 and EV_22 settle for exactly the same net on the same day, and "
         "two unlabelled credits of that amount are in the statement. There is no "
         "evidence that assigns a credit to a settlement. The engine must leave "
         "both unresolved rather than pick one -- refusing to guess is the "
         "behaviour that makes every other match believable.",
         payments=[pay("EV21_P1", 500000, "CARD", "2026-03-14")],
         bank="AMBIGUOUS_HEAD",
         ground_truth=[gt("TRAP_AMBIGUOUS_MATCH", "settlement", "EV_21", "D2_BANK",
                          "AMBIGUOUS_BANK_MATCH", 0, False,
                          "Two identical credits, two identical settlements, no "
                          "distinguishing evidence. Must not be auto-resolved.")],
         expected={"d1_paise": 0, "d3_paise": 0, "d4_paise": 0,
                   "worst_tier": "C", "exception_types": ["AMBIGUOUS_BANK_MATCH"]})

scenario("EV22", "Ambiguous bank match, second of the identical pair", "TRAP",
         "The partner of EV_21: same net, same settlement date, same unlabelled "
         "credit sitting in the statement. Identical in every respect an "
         "automated matcher can see, which is exactly why neither may be "
         "auto-resolved. Both must land at tier C and wait for a human with "
         "information the statement does not contain.",
         payments=[pay("EV22_P1", 500000, "CARD", "2026-03-16")],
         bank="AMBIGUOUS_TAIL",
         ground_truth=[gt("TRAP_AMBIGUOUS_MATCH", "settlement", "EV_22", "D2_BANK",
                          "AMBIGUOUS_BANK_MATCH", 0, False,
                          "The other half of the ambiguity.")],
         expected={"d1_paise": 0, "d3_paise": 0, "d4_paise": 0,
                   "worst_tier": "C", "exception_types": ["AMBIGUOUS_BANK_MATCH"]})


# =============================================================================
# Marketplace payouts across the rest of the batch.
#
# Applied AFTER the scenarios are defined, so each one still reads as a single
# statement about the one thing it is testing. Every payout here is correct, so
# Delta-4 stays zero on all of them -- they are the controls that make EV18 and
# EV19 mean something.
# =============================================================================
_BY_ID = {sc["scenario_id"]: sc for sc in SCENARIOS}

for _sid, _splits in {
    "EV01": [("EV01_P2", "SELL_01", 6000), ("EV01_P2", "SELL_04", 3500),
             ("EV01_P3", "SELL_02", 9000)],
    "EV02": [("EV02_P3", "SELL_03", 8000), ("EV02_P4", "SELL_05", 7500)],
    "EV03": [("EV03_P1", "SELL_02", 5000)],
    "EV04": [("EV04_P1", "SELL_04", 7000)],
    "EV05": [("EV05_P1", "SELL_03", 9500), ("EV05_P4", "SELL_01", 6500)],
    "EV06": [("EV06_P1", "SELL_05", 4000), ("EV06_P1", "SELL_02", 4500)],
    "EV08": [("EV08_P1", "SELL_01", 8000)],
    "EV10": [("EV10_P1", "SELL_04", 7000), ("EV10_P1", "SELL_03", 2000)],
    "EV11": [("EV11_P1", "SELL_02", 6000), ("EV11_P2", "SELL_05", 8500)],
    "EV14": [("EV14_P1", "SELL_01", 5500)],
    "EV15": [("EV15_P1", "SELL_03", 7000)],
    "EV17": [("EV17_P1", "SELL_04", 6000)],
    "EV20": [("EV20_P1", "SELL_02", 7000), ("EV20_P2", "SELL_05", 7000)],
    "EV21": [("EV21_P1", "SELL_01", 9000)],
}.items():
    paid_out(_BY_ID[_sid], _splits)


# =============================================================================
# THE PIPELINE -- captured, not yet settled.
#
# Everything above is history: money that has already been through a settlement
# and can be reconciled. This is the other half of a real book -- payments taken
# in the days AFTER the last period closed, which no settlement has picked up
# yet. They are not reconcilable and never appear in a delta; they are what the
# cash forecast is made of.
#
# Strictly ADDITIVE. No row above is touched, so all 22 scenarios keep exactly
# the outcomes they state. These payments simply did not exist before.
#
# Each one carries its capture double-entry (DR RAZORPAY_CLEARING / CR SALES)
# and nothing else -- there is no settlement posting because there has been no
# settlement. Delta-3 only scopes to payments itemised in a settlement, so an
# open clearing balance here is correct rather than an imbalance.
# =============================================================================
def pipeline_day(day, entries):
    """One trading day's captures. `entries` is [(amount, method, [(seller, bps)]), ...]."""
    out = []
    for amount, method, splits in entries:
        _seq["pipeline"] = _seq.get("pipeline", 0) + 1
        pid = f"PIPE_P{_seq['pipeline']:03d}"
        p = pay(pid, amount, method, day)
        p["settled"] = False
        allocs = []
        for seller_id, share_bps in splits:
            _seq["alloc"] += 1
            # PENDING: the seller is owed this, but the payout has not run.
            # Delta-4 scores only SETTLED allocations, so this is future cash,
            # not an unexplained gap.
            allocs.append(alloc(f"ALLOC_{_seq['alloc']:04d}", pid, seller_id,
                                bps(amount, share_bps), status="PENDING"))
        out.append({"payment": p, "allocations": allocs})
    return {"capture_date": day, "captures": out}


# Four trading days after the last settlement period closed on 2026-03-15.
# Volumes and method mix follow the settled history, because a pipeline that
# looks nothing like the book behind it is not a pipeline.
PIPELINE = [
    pipeline_day("2026-03-16", [
        (485000, "CARD", [("SELL_01", 6000), ("SELL_04", 3000)]),
        (250000, "UPI", [("SELL_02", 8000)]),
        (1120000, "CARD", [("SELL_05", 7500)]),
        (318000, "NETBANKING", []),
    ]),
    pipeline_day("2026-03-17", [
        (742000, "CARD", [("SELL_03", 9000)]),
        (196000, "UPI", []),
        (655000, "WALLET", [("SELL_01", 5000), ("SELL_02", 4000)]),
    ]),
    pipeline_day("2026-03-18", [
        (1480000, "CARD_INTL", [("SELL_05", 8000)]),
        (409000, "UPI", [("SELL_04", 7000)]),
        (275000, "CARD", []),
        (890000, "NETBANKING", [("SELL_03", 6500)]),
    ]),
    pipeline_day("2026-03-19", [
        (533000, "CARD", [("SELL_01", 7000)]),
        (167000, "UPI", []),
    ]),
]



# =============================================================================
# GSTR-2B: the third source of truth for input tax credit
# =============================================================================
# STRICTLY ADDITIVE. Not one settled row above is touched -- these are new rows
# in a new table (tax_invoices, db/tax.sql), and nothing in the reconciliation
# path reads them. The 22 scenarios score exactly as they did before.
#
# One gateway tax invoice per settlement, which is how a payment gateway
# actually bills: the fee is the taxable value and the GST on it is the credit
# the merchant wants back. The invoice then has to appear in the merchant's
# GSTR-2B -- the statement the GST portal auto-drafts each month from what
# SUPPLIERS filed -- before a rupee of it is claimable.
#
# Five defects are planted among the 22, one per failure mode a real ITC
# reconciliation hits. They live here rather than in ground_truth_anomalies on
# purpose: that table feeds the engine's honesty metrics and the "19 planted
# anomalies" the dashboard reports, and tax findings are a different question
# scored on a different axis. Adding them there would silently move numbers the
# whole evaluation rests on.
#
# EVERY VALUE IS SYNTHETIC. The GSTINs are placeholders containing "DEMO".

TAX_SUPPLIER_GSTIN = "29DEMOP0000D1Z5"


def _tax_invoice(no, settlement_id, invoice_date, return_period, taxable, tax,
                 heads="CGST+SGST", eligible=True, reason=None, filed=None):
    """One GSTR-2B line. `tax` is the total; intra-state splits it CGST/SGST."""
    cgst = sgst = igst = 0
    if heads == "IGST":
        igst = tax
    else:
        # An odd total splits with the extra paise on CGST, which is how a
        # supplier's own system would round it.
        cgst, sgst = tax - tax // 2, tax // 2
    return {"invoice_no": no, "settlement_id": settlement_id,
            "invoice_date": invoice_date, "return_period": return_period,
            "supplier_gstin": TAX_SUPPLIER_GSTIN, "document_type": "INVOICE",
            "taxable_value_paise": taxable, "cgst_paise": cgst, "sgst_paise": sgst,
            "igst_paise": igst, "itc_eligible": eligible,
            "ineligible_reason": reason, "filed_at": filed or return_period + "-11"}


# (settlement_id, invoice_date, fee_paise, tax_paise) -- the clean case, taken
# from the settlements the scenarios already author. Kept as a literal rather
# than derived so this file stays readable as data.
_TAX_CLEAN = [
    ("EV_01", "2026-02-05", 28400, 5112), ("EV_02", "2026-02-07", 18600, 3348),
    ("EV_03", "2026-02-10", 18000, 3240), ("EV_04", "2026-02-11", 25000, 4500),
    ("EV_05", "2026-02-13", 32486, 5847), ("EV_06", "2026-02-17", 24000, 4320),
    ("EV_07", "2026-02-18", 16000, 2880), ("EV_08", "2026-02-19", 14000, 2520),
    ("EV_09", "2026-02-21", 19000, 3420), ("EV_10", "2026-02-24", 28000, 5040),
    ("EV_11", "2026-02-25", 28500, 5130), ("EV_12", "2026-02-26", 14000, 2520),
    ("EV_13", "2026-02-27",  9625, 1733), ("EV_14", "2026-03-03", 22000, 3960),
    ("EV_15", "2026-03-05", 20000, 3600), ("EV_16", "2026-03-06", 17000, 3060),
    ("EV_17", "2026-03-07", 25000, 4500), ("EV_18", "2026-03-10", 24000, 4320),
    ("EV_19", "2026-03-11", 19000, 3420), ("EV_20", "2026-03-13", 18000, 3240),
    ("EV_21", "2026-03-17", 10000, 1800), ("EV_22", "2026-03-18", 10000, 1800),
]

# settlement_id -> what the matcher must conclude, and why. Asserted in
# tests/test_taxmatch.py, exported to CSV for evaluators.
TAX_EXPECTATIONS = {
    "EV_07": {"status": "NOT_FILED", "claim_state": "AT_RISK", "at_risk_paise": 2880,
              "note": "The supplier never filed this invoice, so it appears in no "
                      "return period at all. Rs 28.80 sits in INPUT_GST that the "
                      "merchant cannot claim until they chase it."},
    "EV_11": {"status": "AMOUNT_MISMATCH", "claim_state": "AT_RISK", "at_risk_paise": 3,
              "note": "The supplier rounds GST once per invoice; the settlement "
                      "rounds it per line. Three paise, every invoice, forever."},
    "EV_15": {"status": "SPLIT_MISMATCH", "claim_state": "AT_RISK", "at_risk_paise": 3600,
              "note": "Filed as IGST when both parties are in state 29. The amount "
                      "is correct and the credit still will not offset, because it "
                      "is sitting under the wrong heads. Only the supplier can amend."},
    "EV_20": {"status": "PERIOD_MISMATCH", "claim_state": "DEFERRED", "at_risk_paise": 0,
              "note": "Filed late: a March settlement whose invoice lands in the "
                      "April GSTR-2B. Inside the claim window, so this is cash "
                      "deferred by a month, not cash lost. A naive check calls it "
                      "missing -- this is the trap in the tax batch."},
    "EV_03": {"status": "ITC_BLOCKED", "claim_state": "BLOCKED", "at_risk_paise": 3240,
              "note": "Every rupee matches and the portal still marks the line "
                      "ineligible. Nothing to fix and nothing to chase: it should "
                      "never have been booked as recoverable in the first place."},
}


def _build_tax_invoices():
    out, n = [], 0
    for sid, day, fee, tax in _TAX_CLEAN:
        n += 1
        no = f"DGS/2026/{n:04d}"
        period = day[:7]
        if sid == "EV_07":
            continue                                   # never filed
        if sid == "EV_11":
            out.append(_tax_invoice(no, sid, day, period, fee, tax - 3))
        elif sid == "EV_15":
            out.append(_tax_invoice(no, sid, day, period, fee, tax, heads="IGST"))
        elif sid == "EV_20":
            out.append(_tax_invoice(no, sid, day, "2026-04", fee, tax,
                                    filed="2026-04-14"))
        elif sid == "EV_03":
            out.append(_tax_invoice(no, sid, day, period, fee, tax, eligible=False,
                                    reason="Blocked credit under section 17(5)"))
        else:
            out.append(_tax_invoice(no, sid, day, period, fee, tax))
    return out


TAX_INVOICES = _build_tax_invoices()


def main() -> None:
    doc = {
        "batch_id": "EVALUATION_BATCH_V1",
        "dataset_id": DATASET_ID,
        "customers": CUSTOMER_ROWS,
        "sellers": SELLER_ROWS,
        "pipeline": PIPELINE,
        "title": "Static evaluation batch -- hand-authored, fixed, no randomness",
        "policy_version": P.version,
        "config_hash": P.config_hash,
        "how_to_read": (
            "Every scenario is one settlement. `note` states what was done and why, "
            "`expected` states what the engine must therefore conclude, and "
            "`ground_truth` is scored by the engine's own honesty metrics. Nothing "
            "here is sampled or seeded: change policy.yaml and this file must be "
            "re-authored, which is the point."),
        "scenario_count": len(SCENARIOS),
        "scenarios": SCENARIOS,
    }
    OUT.write_text(json.dumps(doc, indent=2) + "\n")
    fams: dict[str, int] = {}
    for s in SCENARIOS:
        fams[s["family"]] = fams.get(s["family"], 0) + 1
    allocs = sum(len(s["allocations"]) for s in SCENARIOS)
    used = {p["customer_id"] for s in SCENARIOS for p in s["payments"]}
    print(f"wrote {OUT} -- {len(SCENARIOS)} scenarios {fams}")
    pipe_pay = sum(len(d["captures"]) for d in PIPELINE)
    pipe_amt = sum(c["payment"]["amount_paise"] for d in PIPELINE for c in d["captures"])
    pipe_alloc = sum(len(c["allocations"]) for d in PIPELINE for c in d["captures"])
    print(f"  {len(used)} customers used of {len(CUSTOMER_ROWS)} · "
          f"{len(SELLER_ROWS)} sellers · {allocs} allocations across "
          f"{sum(1 for s in SCENARIOS if s['allocations'])} settlements")
    print(f"  pipeline: {pipe_pay} captures over {len(PIPELINE)} days "
          f"({pipe_amt / 100:,.2f} rupees), {pipe_alloc} allocations pending payout")
    periods = sorted({i["return_period"] for i in TAX_INVOICES})
    print(f"  GSTR-2B: {len(TAX_INVOICES)} invoices across {len(periods)} return "
          f"periods {periods}, {len(TAX_EXPECTATIONS)} planted tax findings")


if __name__ == "__main__":
    main()
