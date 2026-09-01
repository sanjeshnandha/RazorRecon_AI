"""Response shaping. Money crosses the API as integer paise, always. The only
place a rupee string appears is alongside it, for display."""
from __future__ import annotations

from engine.money import rupees
from engine.policy import DEMO_POLICY_LABEL


def money(paise: int | None) -> dict:
    if paise is None:
        return {"paise": None, "display": ""}
    return {"paise": int(paise), "display": rupees(int(paise))}


def waterfall(row: dict) -> list[dict]:
    """gross -> -refunds -> -fees -> -taxes -> +/-adjustments -> expected net
    -> actual settled -> bank credit -> residual."""
    steps = [
        {"label": "Gross captured", "paise": row["gross_paise"], "kind": "base",
         "policy_derived": False},
        {"label": "Refunds", "paise": -row["refunds_paise"], "kind": "delta",
         "policy_derived": True, "rule": "POLICY.REFUND.WINDOW"},
        {"label": "Gateway fees", "paise": -row["computed_fee_paise"], "kind": "delta",
         "policy_derived": True, "rule": "POLICY.MDR.*"},
        {"label": "GST on fees", "paise": -row["computed_tax_paise"], "kind": "delta",
         "policy_derived": True, "rule": "POLICY.TAX.GST_ON_FEE"},
        {"label": "Adjustments", "paise": row["adjustments_paise"], "kind": "delta",
         "policy_derived": False},
        {"label": "Expected net", "paise": row["expected_net_paise"], "kind": "subtotal",
         "policy_derived": True},
        {"label": "Actual settled", "paise": row["actual_net_paise"], "kind": "actual",
         "policy_derived": False},
        {"label": "Bank credit", "paise": row["bank_paise"], "kind": "actual",
         "policy_derived": False},
        {"label": "Unexplained residual", "paise": row["residual_paise"], "kind": "residual",
         "policy_derived": False},
    ]
    for s in steps:
        s["display"] = rupees(s["paise"])
    return steps


DISCLAIMER = DEMO_POLICY_LABEL
