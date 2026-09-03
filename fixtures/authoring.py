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


def pay(pid, amount, method, day, *, charged_fee=None, charged_tax=None,
        status="CAPTURED", order=None):
    f = fee(amount, method)
    return {"payment_id": pid, "order_id": order or f"O_{pid}", "amount_paise": amount,
            "payment_method": method, "captured_at": day, "payment_status": status,
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


SCENARIOS: list[dict] = []
_cursor = {"day": START, "n": 0}


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
_SELLERS = [{"seller_id": "EV_S1", "seller_name": "Kanha Traders",
             "seller_type": "SMB", "commission_bps": P._commission["SMB"]},
            {"seller_id": "EV_S2", "seller_name": "Nilgiri Organics",
             "seller_type": "ENTERPRISE", "commission_bps": P._commission["ENTERPRISE"]}]


def alloc(aid, pid, seller_id, gross):
    c = bps(gross, P._commission[next(s["seller_type"] for s in _SELLERS
                                      if s["seller_id"] == seller_id)])
    return {"allocation_id": aid, "payment_id": pid, "seller_id": seller_id,
            "gross_allocated_paise": gross, "commission_paise": c,
            "net_seller_paise": gross - c, "allocation_status": "SETTLED"}


_a1 = alloc("EV18_A1", "EV18_P1", "EV_S1", 800000)
_a2 = alloc("EV18_A2", "EV18_P1", "EV_S2", 400000)
_SHORT = 30000
scenario("EV18", "Seller paid less than the allocation says they were owed", "D4",
         f"EV_S1 was allocated 800000 gross at 1200 bps commission, so "
         f"{_a1['net_seller_paise']} was owed. Only "
         f"{_a1['net_seller_paise'] - _SHORT} was transferred. The settlement "
         f"itself reconciles perfectly on D1, D2 and D3 -- a marketplace can "
         f"balance to the paise at the top and still be short-paying a seller "
         f"underneath, which is the entire argument for reporting D4 separately.",
         payments=[pay("EV18_P1", 1200000, "CARD", "2026-03-08")],
         sellers=_SELLERS, allocations=[_a1, _a2],
         transfers=[{"transfer_id": "EV18_T1", "payment_id": "EV18_P1",
                     "seller_id": "EV_S1", "seller_amount_paise":
                         _a1["net_seller_paise"] - _SHORT, "transfer_status": "PROCESSED"},
                    {"transfer_id": "EV18_T2", "payment_id": "EV18_P1",
                     "seller_id": "EV_S2", "seller_amount_paise": _a2["net_seller_paise"],
                     "transfer_status": "PROCESSED"},
                    # the reversal that accounts for the shortfall: explainable,
                    # not a mystery. Without this row the engine is right to
                    # refuse to resolve it.
                    {"transfer_id": "EV18_T3", "payment_id": "EV18_P1",
                     "seller_id": "EV_S1", "seller_amount_paise": _SHORT,
                     "transfer_status": "REVERSED"}],
         ground_truth=[gt("D4_ALLOC_TRANSFER_DIVERGENCE", "seller_allocation", "EV18_A1",
                          "D4_PAYOUT", "ALLOCATION_TRANSFER_DIVERGENCE", _SHORT, True,
                          f"{_SHORT} paise short against a SETTLED allocation.")],
         expected={"d1_paise": 0, "d2_paise": 0, "d3_paise": 0, "d4_paise": _SHORT,
                   "worst_tier": "A", "exception_types": ["ALLOCATION_TRANSFER_DIVERGENCE"]})

_a3 = alloc("EV19_A1", "EV19_P1", "EV_S2", 900000)
scenario("EV19", "Allocation marked settled with no transfer at all", "D4",
         f"The allocation says SETTLED and {_a3['net_seller_paise']} is owed, but "
         f"no transfer row exists. Not a rounding difference and not a timing "
         f"difference -- the payout never happened.",
         payments=[pay("EV19_P1", 950000, "CARD", "2026-03-10")],
         sellers=_SELLERS, allocations=[_a3], transfers=[],
         ground_truth=[gt("D4_TRANSFER_MISSING", "seller_allocation", "EV19_A1",
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


def main() -> None:
    doc = {
        "batch_id": "EVALUATION_BATCH_V1",
        "dataset_id": DATASET_ID,
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
    print(f"wrote {OUT} -- {len(SCENARIOS)} scenarios {fams}")


if __name__ == "__main__":
    main()
