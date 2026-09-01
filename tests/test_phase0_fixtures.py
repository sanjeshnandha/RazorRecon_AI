"""
PHASE 0 GATE.

The real engine runs against ten settlements whose every rupee was computed by
hand from policy.yaml before any code existed. This is the only test that can
catch the generator and the engine sharing the same misunderstanding of the
policy, because the fixture is independent of both.

If this file disagrees with the engine, the ENGINE is wrong.
"""
import pytest

from engine.attribution import attribute_d1, attribute_d3, attribute_d4
from engine.calculation import compute_d1, compute_d3, compute_d4
from engine.invariants import check as check_invariants
from engine.loader import load
from engine.policy import load_policy
from tests.fixture_loader import load_fixture

D1_FIXTURES = ["M01_clean", "M02_fee_rate_drift", "M03_tax_aggregate_rounding",
               "M04_refund_not_deducted", "M05_refund_outside_period",
               "M06_chargeback_adjustment", "M07_header_rollup_mismatch", "M08_phantom_debit"]


def _run_d1(db, fx):
    policy = load_policy()
    ds_id = load_fixture(db, fx)
    snap = load(db, ds_id)
    inv = check_invariants(snap)
    s = snap.settlements[0]
    d1 = compute_d1(snap, s, policy, inv.excluded_items)
    diag = attribute_d1(snap, s, d1, policy, inv.by_settlement(s["settlement_id"]))
    return d1, diag


@pytest.mark.parametrize("name", D1_FIXTURES)
def test_delta1_matches_the_hand_worked_figures(db, fixtures, name):
    fx = fixtures[name]
    e = fx["expected"]
    d1, diag = _run_d1(db, fx)

    assert d1.gross_paise == e["gross_paise"], "gross"
    assert d1.source_refunds_paise == e["refunds_paise"], "in-period refunds"
    assert d1.computed_fee_paise == e["computed_fee_paise"], "policy fee"
    assert d1.computed_tax_paise == e["computed_tax_paise"], "policy tax (PER_ITEM)"
    assert d1.expected_net_paise == e["expected_net_paise"], "expected net"
    assert d1.actual_net_paise == e["actual_net_paise"], "actual net"
    assert d1.delta_paise == e["d1_paise"], "delta"

    assert diag.residual_paise == e["residual_paise"], "residual"
    assert diag.tier == e["tier"], "tier"
    assert len(diag.attributions) == len(e["attributions"]), (
        f"attribution count: got {[(a.evidence_type, a.evidence_record_id, a.signed_amount_paise) for a in diag.attributions]}")
    for got, want in zip(sorted(diag.attributions, key=lambda a: (a.evidence_type, a.evidence_record_id)),
                         sorted(e["attributions"], key=lambda a: (a["evidence_type"], a["evidence_record_id"]))):
        assert got.evidence_type == want["evidence_type"]
        assert got.evidence_record_id == want["evidence_record_id"]
        assert got.signed_amount_paise == want["signed_amount_paise"]
        assert got.derivation == "DETERMINISTIC"
        assert got.rule_ids, "every attribution must cite at least one rule id"
    if e.get("exception_type"):
        assert diag.exception_type == e["exception_type"]


def test_M05_refund_outside_period_produces_no_attribution(db, fixtures):
    """Golden test 14. A refund sitting between period_end and settlement_date
    is NOT evidence. Fabricating an explanation from it is the failure mode."""
    fx = fixtures["M05_refund_outside_period"]
    d1, diag = _run_d1(db, fx)
    assert d1.source_refunds_paise == 0
    assert d1.delta_paise == 0
    assert diag.attributions == []
    assert diag.exception_type is None
    assert diag.tier == "A" and diag.status == "MATCHED"


def test_M08_phantom_debit_escalates_with_zero_attributions(db, fixtures):
    """Golden test 12. Reporting Rs 473.00 as unexplained beats inventing a cause."""
    fx = fixtures["M08_phantom_debit"]
    d1, diag = _run_d1(db, fx)
    assert diag.attributions == []
    assert diag.residual_paise == 47300
    assert diag.tier == "C" and diag.status == "UNRESOLVED"
    assert diag.exception_type == "UNEXPLAINED_SHORTFALL"


def test_M09_seller_payout_delta4(db, fixtures):
    """Golden test 18. Delta-1..3 are clean while a seller is short Rs 3,520."""
    fx = fixtures["M09_seller_payout"]
    policy = load_policy()
    ds_id = load_fixture(db, fx)
    snap = load(db, ds_id)
    inv = check_invariants(snap)
    s = snap.settlements[0]
    assert compute_d1(snap, s, policy, inv.excluded_items).delta_paise == fx["expected"]["d1_paise"]
    assert compute_d3(snap, s, policy, inv.excluded_items).delta_paise == fx["expected"]["d3_paise"]

    by_alloc = {r.subject_id: r for r in compute_d4(snap, policy, lambda pid: s["settlement_id"])}
    for want in fx["expected"]["d4_by_allocation"]:
        got = by_alloc[want["allocation_id"]]
        assert got.expected_paise == want["expected_payout_paise"]
        assert got.actual_paise == want["actual_payout_paise"]
        assert got.delta_paise == want["d4_paise"]
        diag = attribute_d4(snap, got, policy)
        assert diag.tier == want["tier"]
        assert diag.residual_paise == want.get("residual_paise", 0)
        if want.get("exception_type"):
            assert diag.exception_type == want["exception_type"]
            assert [a.evidence_record_id for a in diag.attributions] == \
                   [a["evidence_record_id"] for a in want["attributions"]]


def test_M10_duplicate_ledger_delta3(db, fixtures):
    """Golden test 10. The duplicate group is balanced; only the clearing net exposes it."""
    fx = fixtures["M10_duplicate_ledger"]
    e = fx["expected"]
    policy = load_policy()
    ds_id = load_fixture(db, fx)
    snap = load(db, ds_id)
    inv = check_invariants(snap)
    s = snap.settlements[0]
    assert compute_d1(snap, s, policy, inv.excluded_items).delta_paise == e["d1_paise"]
    d3 = compute_d3(snap, s, policy, inv.excluded_items)
    assert d3.expected_paise == e["clearing_dr_paise"]
    assert d3.actual_paise == e["clearing_cr_paise"]
    assert d3.delta_paise == e["d3_paise"]
    diag = attribute_d3(snap, s, d3, policy)
    assert diag.residual_paise == 0 and diag.tier == "A"
    assert diag.exception_type == e["exception_type"]
    assert [a.evidence_record_id for a in diag.attributions] == \
           [a["evidence_record_id"] for a in e["attributions"]]
    # every entry group must still balance -- a duplicate is a copy, not a corruption
    assert not [v for v in inv.business if v["id"] == "INV-B1"]
