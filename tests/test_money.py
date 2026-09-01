"""Unit tests for the money primitives, checked against the Phase 0 fixtures.

If bps() and the hand-worked figures ever disagree, the arithmetic is wrong
before any of the rest of the system gets a chance to be.
"""
from engine.money import bps, rupees
from engine.policy import load_policy


def test_bps_is_round_half_up_not_bankers():
    # Python's round() would give 2 here (banker's rounding). We need 3.
    assert bps(25, 10000) == 25
    assert bps(1, 5000) == 1          # 0.5 -> 1, away from zero
    assert bps(3, 5000) == 2          # 1.5 -> 2
    assert bps(-1, 5000) == -1        # symmetric for negatives
    assert bps(0, 1800) == 0


def test_bps_rejects_floats():
    import pytest
    with pytest.raises(TypeError):
        bps(100.0, 200)
    with pytest.raises(TypeError):
        bps(100, 2.0)


def test_rupees_uses_indian_grouping():
    assert rupees(84231700) == "Rs 8,42,317.00"
    assert rupees(-200000) == "-Rs 2,000.00"
    assert rupees(1) == "Rs 0.01"


def test_bps_reproduces_every_hand_worked_fee_and_tax(fixtures):
    policy = load_policy()
    checked = 0
    for name, fx in fixtures.items():
        for p in fx.get("payments", []):
            fee = bps(p["amount_paise"], policy.mdr_bps(p["payment_method"]))
            assert fee == p["policy_fee_paise"], f"{name}/{p['payment_id']} fee"
            tax = bps(fee, policy.gst_on_fee_bps)
            assert tax == p["policy_tax_paise"], f"{name}/{p['payment_id']} tax"
            if "charged_fee_paise" in p:
                assert bps(p["amount_paise"], 250) == p["charged_fee_paise"]
                assert bps(p["charged_fee_paise"], policy.gst_on_fee_bps) == p["charged_tax_paise"]
            checked += 1
        for a in fx.get("allocations", []):
            seller = next(s for s in fx["sellers"] if s["seller_id"] == a["seller_id"])
            assert bps(a["gross_allocated_paise"], seller["commission_bps"]) == a["commission_paise"]
            assert a["net_seller_paise"] == a["gross_allocated_paise"] - a["commission_paise"]
            checked += 1
    assert checked >= 15


def test_hand_worked_arithmetic_is_internally_consistent(fixtures):
    """The fixtures must add up on their own terms before we trust them."""
    for name, fx in fixtures.items():
        e = fx["expected"]
        if "expected_net_paise" not in e:
            continue
        assert e["expected_net_paise"] == (
            e["gross_paise"] - e["refunds_paise"] - e["computed_fee_paise"]
            - e["computed_tax_paise"] + e.get("adjustments_paise", 0)), name
        assert e["d1_paise"] == e["expected_net_paise"] - e["actual_net_paise"], name
        attributed = sum(a["signed_amount_paise"] for a in e.get("attributions", []))
        assert attributed + e["residual_paise"] == e["d1_paise"], name
