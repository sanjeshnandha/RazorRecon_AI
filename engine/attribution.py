"""
Attribution ledger and tier gate.

Hard invariant, enforced in code after every pass:

    sum(attributions.signed_amount_paise) + residual_paise == delta_paise

residual_paise is COMPUTED, never asserted. Every attribution carries an
evidence_record_id that must exist in a source table; attributions failing that
check are dropped before persistence.

Tier gate:
  A  residual == 0 AND every attribution DETERMINISTIC AND no ambiguity flag
  B  residual == 0 AND at least one FUZZY attribution
  C  residual != 0 OR two or more equally-scoring match candidates

Ambiguity always beats confidence. There is no numeric threshold in P0.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from engine.money import bps, rupees
from engine.policy import Policy


# Delta-2 exception priority: the most consequential fact wins the label, the
# rest survive as notes on the same exception.
_D2_PRIORITY = ["AMBIGUOUS_BANK_MATCH", "MISSING_BANK_CREDIT", "AMOUNT_MISMATCH",
                "MERGED_BANK_CREDIT", "SPLIT_BANK_CREDIT", "UTR_MISSING", "TIMING_DIFFERENCE"]


def _pick(types: list[str]) -> str | None:
    for t in _D2_PRIORITY:
        if t in types:
            return t
    return types[0] if types else None


@dataclass
class Attribution:
    evidence_type: str
    evidence_record_id: str
    signed_amount_paise: int
    derivation: str                # DETERMINISTIC | FUZZY
    rule_ids: list[str]
    rationale: str


@dataclass
class Diagnosis:
    attributions: list[Attribution] = field(default_factory=list)
    residual_paise: int = 0
    tier: str = "A"
    status: str = "MATCHED"
    exception_type: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def explained_paise(self) -> int:
        return sum(a.signed_amount_paise for a in self.attributions)


def _finalise(diag: Diagnosis, delta: int, ambiguous: bool) -> Diagnosis:
    """Compute the residual and grade. The residual is DERIVED here and nowhere
    else, so an attribution layer can never assert its own success."""
    diag.residual_paise = delta - diag.explained_paise
    if diag.residual_paise != 0 or ambiguous:
        diag.tier, diag.status = "C", "UNRESOLVED"
    elif any(a.derivation == "FUZZY" for a in diag.attributions):
        diag.tier, diag.status = "B", "REVIEW"
    else:
        diag.tier = "A"
        diag.status = "MATCHED" if not diag.attributions else "EXPLAINED"
    return diag


# =============================================================================
# Delta-1
# =============================================================================
def attribute_d1(snap, s: dict, d1, policy: Policy, inv_rows: list[dict]) -> Diagnosis:
    diag = Diagnosis()
    delta = d1.delta_paise
    sid = s["settlement_id"]
    v = policy.version

    # ---- 1. rollup mismatch on the gross component ----------------------
    # The header lost rupees the items still show. Attribute only the gross and
    # adjustment components; fee/tax discrepancies are the fee rules' business.
    for row in inv_rows:
        if row["id"] != "INV-B6":
            continue
        comps = row.get("components", {})
        impact = 0
        detail = []
        for name in ("gross", "adjustment"):
            if name in comps:
                items_v, header_v = comps[name]
                impact += items_v - header_v
                detail.append(f"{name}: items {rupees(items_v)} vs header {rupees(header_v)}")
        if impact:
            diag.attributions.append(Attribution(
                "rollup", sid, impact, "DETERMINISTIC", [f"ATTR.ROLLUP_MISMATCH@{v}"],
                "settlement_items is the source of truth; the header rollup disagrees -- "
                + "; ".join(detail)))
            diag.exception_type = "HEADER_ROLLUP_MISMATCH"

    # ---- 2. GST computed on the aggregate instead of PER_ITEM ----------
    # Tested BEFORE per-payment fee drift: when the fees themselves are correct
    # and the whole tax gap is exactly what the aggregate method would produce,
    # the defect is the METHOD, and it belongs on the settlement, not on some
    # arbitrary payment that happens to carry the rounding remainder.
    fee_gap_total = d1.charged_fee_paise - d1.computed_fee_paise
    tax_gap_total = d1.charged_tax_paise - d1.computed_tax_paise
    aggregate_gap = d1.aggregate_tax_paise - d1.computed_tax_paise
    tax_is_method_error = (fee_gap_total == 0 and tax_gap_total != 0
                           and tax_gap_total == aggregate_gap)
    if tax_is_method_error:
        diag.attributions.append(Attribution(
            "tax", sid, tax_gap_total, "DETERMINISTIC",
            [policy.gst_rule, f"ATTR.TAX_ROUNDING@{v}"],
            f"Gateway fees match policy exactly, but GST was charged on the AGGREGATED fee "
            f"({rupees(d1.aggregate_tax_paise)}) rather than PER_ITEM "
            f"({rupees(d1.computed_tax_paise)}) as rounding.tax_computation requires. The gap of "
            f"{rupees(abs(tax_gap_total))} is exactly the difference between the two methods, which "
            f"is what proves it is the method and not an arbitrary error."))
        diag.exception_type = diag.exception_type or "TAX_ROUNDING_MISMATCH"

    # ---- 3. fee rate drift, net of any FEE_CORRECTION already returned ---
    # DOUBLE-COUNT GUARD: a FEE_CORRECTION adjustment referencing this payment
    # has already handed part of the drift back. Only the RESIDUAL fee error
    # (charged - corrected - policy) may be attributed here.
    corrections: dict[str, int] = {}
    for adj in snap.adjustments_by_settlement.get(sid, []):
        if adj["adjustment_type"] == "FEE_CORRECTION" and adj["status"] == "APPLIED" \
                and adj["ref_payment_id"] and adj["adjustment_id"] not in d1.itemised_adjustment_ids:
            corrections[adj["ref_payment_id"]] = corrections.get(adj["ref_payment_id"], 0) + \
                adj["amount_paise"]
    if not tax_is_method_error:
        for pid, (cf, chf, ct, cht, method, amt) in sorted(d1.fee_by_payment.items()):
            fee_gap, tax_gap = chf - cf, cht - ct
            if fee_gap == 0 and tax_gap == 0:
                continue
            credited = corrections.pop(pid, 0)
            note = ""
            if credited:
                note = (f" FEE_CORRECTION already returned {rupees(credited)}, so only the residual "
                        f"is attributed here -- attributing the full drift would count the same "
                        f"rupees twice.")
            if fee_gap:
                diag.attributions.append(Attribution(
                    "fee", pid, fee_gap - credited, "DETERMINISTIC",
                    [policy.mdr_rule(method), f"ATTR.FEE_RATE@{v}"],
                    f"{method} payment of {rupees(amt)}: policy fee {rupees(cf)} at "
                    f"{policy.mdr_bps(method)}bps, charged {rupees(chf)}.{note}"))
            if tax_gap:
                diag.attributions.append(Attribution(
                    "tax_on_fee", pid, tax_gap, "DETERMINISTIC",
                    [policy.gst_rule, f"ATTR.FEE_RATE@{v}"],
                    f"GST follows the fee: policy {rupees(ct)} on the policy fee, charged "
                    f"{rupees(cht)} on the inflated fee."))
            diag.exception_type = diag.exception_type or "FEE_RATE_MISMATCH"

    # ---- 4. in-period refunds present in the source but not in items ----
    # A payment with no refund is not evidence of anything. Only a refund that
    # EXISTS, is IN PERIOD, and is MISSING from items counts -- and every such
    # refund counts, because refunds is a sum, not a lookup.
    for ref in sorted(d1.in_period_refunds, key=lambda r: r["refund_id"]):
        if ref["refund_id"] in d1.itemised_refund_ids:
            continue
        diag.attributions.append(Attribution(
            "refund", ref["refund_id"], -ref["refund_amount_paise"], "DETERMINISTIC",
            [policy.refund_rule, f"ATTR.REFUND_OMITTED@{v}"],
            f"Refund {ref['refund_id']} of {rupees(ref['refund_amount_paise'])} against payment "
            f"{ref['payment_id']} is PROCESSED and dated {ref['refund_date']}, inside "
            f"[{s['settlement_period_start']}, {s['settlement_period_end']}], but no REFUND item "
            f"exists -- the merchant was overpaid by that amount."))
        diag.exception_type = diag.exception_type or "REFUND_NOT_DEDUCTED"

    # ---- 5. adjustments claimed against this settlement but not itemised -
    for adj in sorted(snap.adjustments_by_settlement.get(sid, []), key=lambda a: a["adjustment_id"]):
        if adj["status"] != "APPLIED" or adj["adjustment_id"] in d1.itemised_adjustment_ids:
            continue
        if adj["adjustment_type"] == "FEE_CORRECTION" and adj["ref_payment_id"]:
            continue          # handled inside the fee rule above -- never both
        diag.attributions.append(Attribution(
            "adjustment", adj["adjustment_id"], -adj["amount_paise"], "DETERMINISTIC",
            [f"POLICY.ADJUSTMENT.{adj['adjustment_type']}@{v}", f"ATTR.ADJUSTMENT@{v}"],
            f"{adj['adjustment_type'].replace('_',' ').title()} {adj['adjustment_id']} of "
            f"{rupees(adj['amount_paise'])} names this settlement in the adjustments table but "
            f"never appears as an ADJUSTMENT item. The money moved; the itemisation did not."))
        diag.exception_type = diag.exception_type or "ADJUSTMENT_UNEXPLAINED"

    _finalise(diag, delta, ambiguous=False)
    if diag.tier == "C" and delta != 0:
        diag.exception_type = diag.exception_type or "UNEXPLAINED_SHORTFALL"
        # An internally inconsistent header explains nothing about where the
        # money went -- it is a symptom, recorded as a note, never an attribution.
        for row in inv_rows:
            if row["id"] == "INV-B7":
                diag.notes.append("header is internally inconsistent: " + row["detail"])
    return diag


# =============================================================================
# Delta-2
# =============================================================================
def attribute_d2(snap, s: dict, match, policy: Policy) -> tuple[Diagnosis, int, int]:
    """Returns (diagnosis, expected_paise, actual_paise)."""
    diag = Diagnosis()
    sid = s["settlement_id"]
    expected = s["net_settlement_amount_paise"]
    actual = match.matched_paise
    delta = expected - actual
    v = policy.version

    if s["settlement_status"] == "ON_HOLD":
        if delta:
            diag.attributions.append(Attribution(
                "settlement_status", sid, delta, "DETERMINISTIC",
                [f"ATTR.ON_HOLD@{v}", policy.rule("SETTLEMENT", "STATUS")],
                f"Settlement is ON_HOLD: no UTR was issued and no credit was expected, so the "
                f"full {rupees(delta)} is accounted for by the hold rather than missing."))
        diag.exception_type = "MISSING_BANK_CREDIT"
        _finalise(diag, delta, ambiguous=False)
        return diag, expected, actual

    if match.is_ambiguous and not match.bank_ids:
        diag.exception_type = "AMBIGUOUS_BANK_MATCH"
        diag.notes.append(match.note or "two or more bank credits match equally well")
        _finalise(diag, delta, ambiguous=True)
        return diag, expected, actual

    if not match.bank_ids:
        diag.exception_type = "MISSING_BANK_CREDIT"
        diag.notes.append(match.note or "no bank credit could be matched to this settlement")
        _finalise(diag, delta, ambiguous=False)
        return diag, expected, actual

    # matched. Several observations can be true at once, so collect them and
    # report the most consequential -- a timing note must never mask the fact
    # that a credit was merged, split or short.
    types: list[str] = []
    for bid in match.bank_ids:
        b = snap.bank_by_id[bid]
        lag = (b["transaction_date"] - s["settlement_date"]).days
        if lag != policy.expected_lag_days:
            within = abs(lag) <= policy.bank_tolerance_days
            types.append("TIMING_DIFFERENCE")
            diag.notes.append(
                f"credit {bid} landed {b['transaction_date']} against a settlement date of "
                f"{s['settlement_date']} ({lag:+d} calendar days), "
                + (f"inside POLICY.BANK.tolerance_days@{v} = {policy.bank_tolerance_days}; "
                   f"no money is missing" if within else
                   f"OUTSIDE the {policy.bank_tolerance_days}-day tolerance"))
    if match.pass_name == "SUBSET_SUM_MERGED":
        types.append("MERGED_BANK_CREDIT")
        diag.notes.append(match.note)
    if match.pass_name == "SUBSET_SUM_SPLIT":
        types.append("SPLIT_BANK_CREDIT")
        diag.notes.append(match.note)
    if match.pass_name in ("AMOUNT_WIDE_WINDOW", "FUZZY_REFERENCE"):
        diag.attributions.append(Attribution(
            "bank_match", match.bank_ids[0], 0, "FUZZY",
            [f"ATTR.{match.pass_name}@{v}"],
            f"Matched by {match.pass_name} -- {match.note}. No UTR evidence, so this is human "
            f"review by design; string similarity never promotes to tier A."))
        types.append("UTR_MISSING")
    if delta:
        types.append("AMOUNT_MISMATCH")
        diag.notes.append(f"matched credit(s) total {rupees(actual)} against a net of {rupees(expected)}")
    diag.exception_type = _pick(types)
    _finalise(diag, delta, ambiguous=False)
    if diag.tier == "A" and match.tier == "B":
        diag.tier, diag.status = "B", "REVIEW"
    return diag, expected, actual

# =============================================================================
# Delta-3
# =============================================================================
def attribute_d3(snap, s: dict, d3, policy: Policy) -> Diagnosis:
    diag = Diagnosis()
    delta = d3.delta_paise
    v = policy.version
    for gid, pid, amt in d3.duplicate_groups:
        diag.attributions.append(Attribution(
            "duplicate_ledger", gid, -amt, "DETERMINISTIC", [f"ATTR.DUPLICATE_LEDGER@{v}"],
            f"Entry group {gid} is a second, balanced posting of the settlement event for payment "
            f"{pid}. It balances internally, so only the RAZORPAY_CLEARING net exposes it: the "
            f"account is over-credited by {rupees(amt)}."))
        diag.exception_type = "DUPLICATE_LEDGER_ENTRY"
    for pid, amt in d3.missing_payments:
        diag.attributions.append(Attribution(
            "missing_ledger", pid, amt, "DETERMINISTIC", [f"ATTR.MISSING_LEDGER@{v}"],
            f"Payment {pid} was captured and settled but has no settlement posting: "
            f"{rupees(amt)} is stranded in RAZORPAY_CLEARING."))
        diag.exception_type = diag.exception_type or "MISSING_LEDGER_ENTRY"
    for eid, amt, pid in d3.misposted_entries:
        diag.attributions.append(Attribution(
            "misposted_ledger", eid, amt, "DETERMINISTIC", [f"ATTR.MISPOSTED_LEDGER@{v}"],
            f"Ledger entry {eid} posts {rupees(amt)} of gateway fee to SALES instead of "
            f"GATEWAY_FEES. The group balances and clearing still nets to zero, so only an "
            f"account-level check finds it -- revenue overstated, input GST understated."))
        diag.exception_type = diag.exception_type or "MISPOSTED_ACCOUNT"
    _finalise(diag, delta, ambiguous=False)
    if diag.tier == "C" and delta:
        diag.exception_type = diag.exception_type or "CLEARING_NOT_ZERO"
    return diag


# =============================================================================
# Delta-4
# =============================================================================
def attribute_d4(snap, r, policy: Policy) -> Diagnosis:
    diag = Diagnosis()
    delta = r.delta_paise
    v = policy.version

    if r.kind == "ALLOC_EXCEEDS":
        diag.attributions.append(Attribution(
            "allocation_excess", r.subject_id, delta, "DETERMINISTIC",
            [f"ATTR.ALLOC_EXCEEDS@{v}", "INV-B3"],
            f"Allocations against payment {r.subject_id} total {rupees(r.actual_paise)} against a "
            f"payment of {rupees(r.expected_paise)} -- {rupees(abs(delta))} more than the customer "
            f"ever paid. Allocation ids: {', '.join(r.extra.get('allocation_ids', []))}."))
        diag.exception_type = "ALLOCATION_EXCEEDS_PAYMENT"
        return _finalise(diag, delta, ambiguous=False)

    if delta == 0:
        return _finalise(diag, delta, ambiguous=False)

    if r.kind == "TRANSFER_MISSING":
        diag.attributions.append(Attribution(
            "allocation", r.subject_id, delta, "DETERMINISTIC",
            [f"ATTR.TRANSFER_MISSING@{v}", policy.commission_rule(
                snap.sellers[r.seller_id]["seller_type"]) if r.seller_id in snap.sellers else "INV-B9"],
            f"Allocation {r.subject_id} is SETTLED and owes seller {r.seller_id} "
            f"{rupees(r.expected_paise)}, and no transfer row exists at all. The allocation itself "
            f"is the complete diagnosis: what is owed, to whom, and that nothing moved."))
        diag.exception_type = "TRANSFER_MISSING"
        return _finalise(diag, delta, ambiguous=False)

    # a REVERSED or FAILED transfer of exactly the missing amount explains the gap
    for t in sorted(r.reversed_transfers, key=lambda x: x["transfer_id"]):
        if t["amount_paise"] == delta:
            diag.attributions.append(Attribution(
                "transfer", t["transfer_id"], delta, "DETERMINISTIC",
                [f"ATTR.TRANSFER_REVERSED@{v}", "INV-B9"],
                f"Transfer {t['transfer_id']} for {rupees(t['amount_paise'])} to seller "
                f"{r.seller_id} is {t['transfer_status']}, which accounts for the whole gap between "
                f"the allocation's {rupees(r.expected_paise)} and the {rupees(r.actual_paise)} that "
                f"actually moved."))
            diag.exception_type = "ALLOCATION_TRANSFER_DIVERGENCE"
            break
    _finalise(diag, delta, ambiguous=False)
    if diag.tier == "C":
        diag.exception_type = "PHANTOM_PAYOUT_GAP"
        diag.notes.append(
            f"seller {r.seller_id} is {rupees(abs(diag.residual_paise))} short and there is no "
            f"reversed transfer, no adjustment and no second transfer explaining it")
    return diag
