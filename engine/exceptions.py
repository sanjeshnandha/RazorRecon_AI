"""Exception rows: what is wrong, how much, what was proven, what remains, and
what to do about it. Every row carries explained / unexplained / evidence."""
from __future__ import annotations

from engine.money import rupees

HIGH_WATERMARK_PAISE = 100000     # Rs 1,000

ACTIONS = {
    "FEE_RATE_MISMATCH": "Raise a fee dispute with the gateway citing the payment id and the policy "
                         "MDR rule; recover the excess fee and its GST.",
    "TAX_ROUNDING_MISMATCH": "Ask the gateway to compute GST per item as the policy specifies; the "
                            "amount is sub-rupee but the method is wrong on every settlement.",
    "REFUND_NOT_DEDUCTED": "Confirm the refund reached the customer, then expect it to be netted "
                           "from the next settlement; do not recognise it as revenue.",
    "HEADER_ROLLUP_MISMATCH": "Re-request the settlement report; the header and the itemised lines "
                              "disagree, and the items are authoritative.",
    "ADJUSTMENT_UNEXPLAINED": "Match the adjustment to its source event (chargeback, reserve) and "
                              "have it itemised on the settlement report.",
    "UNEXPLAINED_SHORTFALL": "ESCALATE. No source record accounts for this. Open a ticket with the "
                             "gateway quoting the settlement id and the exact rupee figure.",
    "MISSING_BANK_CREDIT": "Confirm the settlement status with the gateway; if not ON_HOLD, chase "
                           "the missing credit with the UTR.",
    "UTR_MISSING": "Matched without UTR evidence. A human should confirm the credit before the "
                   "match is treated as final.",
    "UTR_MISMATCH": "The UTR on the bank line does not belong to this settlement. Re-derive the "
                    "match before posting.",
    "TIMING_DIFFERENCE": "No action -- the credit arrived outside the expected day but inside "
                         "policy tolerance. Recorded so the pattern is visible.",
    "MERGED_BANK_CREDIT": "One credit covers several settlements; the split is proven by subset-sum "
                          "and can be posted as-is.",
    "SPLIT_BANK_CREDIT": "The settlement arrived in parts; the parts are proven by subset-sum and "
                         "can be posted as-is.",
    "AMBIGUOUS_BANK_MATCH": "ESCALATE. Two or more credits fit equally well. Ask the bank for the "
                            "UTR on each credit rather than guessing.",
    "AMOUNT_MISMATCH": "The credit does not equal the settlement net. Reconcile the difference "
                       "before posting to the ledger.",
    "DUPLICATE_LEDGER_ENTRY": "Reverse the duplicate posting group; the clearing account is "
                              "over-credited until you do.",
    "MISSING_LEDGER_ENTRY": "Post the missing settlement entry; money is stranded in clearing.",
    "MISPOSTED_ACCOUNT": "Reclassify the entry from SALES to GATEWAY_FEES; revenue is overstated "
                         "and input GST understated until you do.",
    "UNBALANCED_ENTRY_GROUP": "The double-entry group does not balance. Fix at source before any "
                             "downstream reporting.",
    "CLEARING_NOT_ZERO": "ESCALATE. The clearing account does not net to zero and no single posting "
                         "explains it.",
    "ALLOCATION_EXCEEDS_PAYMENT": "Allocations promise more than the customer paid. Correct the "
                                  "split before the next payout run.",
    "ALLOCATION_TRANSFER_DIVERGENCE": "A reversed transfer explains the gap; confirm whether the "
                                      "seller is due a re-attempt.",
    "TRANSFER_MISSING": "Seller is owed money that never moved. Queue the payout and notify the "
                        "seller.",
    "PHANTOM_PAYOUT_GAP": "ESCALATE. Seller underpaid with nothing explaining it. Do not close the "
                          "period until this is answered.",
    "FAILED_PAYMENT_IN_SETTLEMENT": "A FAILED payment was settled. Reverse it and investigate the "
                                    "capture pipeline.",
    "UNKNOWN_REFERENCE": "A settlement line points at a record that does not exist. Treat the line "
                         "as unusable and re-request the report.",
    "HEADER_SELF_INCONSISTENT": "The settlement header does not equal its own components. Re-request "
                                "the report.",
}


def severity_for(status: str, unexplained_paise: int) -> str:
    if status == "UNRESOLVED" or abs(unexplained_paise) > HIGH_WATERMARK_PAISE:
        return "HIGH"
    if status == "NEEDS_REVIEW":
        return "MEDIUM"
    return "LOW"


def status_for(tier: str) -> str:
    return {"A": "AUTO_RESOLVED", "B": "NEEDS_REVIEW", "C": "UNRESOLVED"}[tier]


def build_exception(exception_id, settlement_id, subject_id, delta_kind, exception_type,
                    delta_paise, explained_paise, residual_paise, tier, notes=None) -> dict:
    status = status_for(tier)
    action = ACTIONS.get(exception_type, "Review manually.")
    if notes:
        action = action + "  Context: " + "; ".join(notes)
    return {
        "exception_id": exception_id, "settlement_id": settlement_id, "subject_id": subject_id,
        "delta_kind": delta_kind, "exception_type": exception_type,
        "severity": severity_for(status, residual_paise),
        "amount_paise": abs(delta_paise), "explained_paise": abs(explained_paise),
        "unexplained_paise": abs(residual_paise), "tier": tier, "status": status,
        "recommended_action": action}
