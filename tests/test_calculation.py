"""
Calculation-engine properties that must hold regardless of which anomalies
happen to land where.
"""
import pytest

from engine.calculation import compute_d1, compute_d3
from engine.invariants import check
from engine.loader import load
from engine.money import bps
from engine.policy import load_policy
from engine.subset_sum import find_unique_subset


def test_fee_is_computed_from_policy_not_read_off_the_item(db, demo_run):
    """The entire point of D1. If the engine ever reads fee_paise as its own
    expectation, a fee drift becomes invisible and this test catches it."""
    policy = load_policy()
    snap = load(db, demo_run["dataset_id"])
    inv = check(snap)
    drifted = 0
    for s in snap.settlements:
        d1 = compute_d1(snap, s, policy, inv.excluded_items)
        for pid, (cf, chf, ct, cht, method, amt) in d1.fee_by_payment.items():
            assert cf == bps(amt, policy.mdr_bps(method)), "computed fee must come from policy"
            assert ct == bps(cf, policy.gst_on_fee_bps), "GST must be computed on the policy fee"
            if cf != chf:
                drifted += 1
    assert drifted > 0, "the batch is supposed to contain fee drifts; none were seen"


def test_transfer_items_are_never_summed_into_d1(db, demo_run):
    """A TRANSFER row is a separate money movement. Counting it in D1 would
    double-count every marketplace payout."""
    policy = load_policy()
    snap = load(db, demo_run["dataset_id"])
    inv = check(snap)
    checked = 0
    for s in snap.settlements:
        items = snap.items_by_settlement.get(s["settlement_id"], [])
        transfers = [i for i in items if i["transaction_type"] == "TRANSFER"]
        if not transfers:
            continue
        checked += 1
        d1 = compute_d1(snap, s, policy, inv.excluded_items)
        payments_only = sum(i["amount_paise"] for i in items if i["transaction_type"] == "PAYMENT")
        assert d1.gross_paise == payments_only
    assert checked > 10, "expected TRANSFER lineage rows in the batch"


def test_refund_period_gate_is_a_closed_tiling(db, demo_run):
    """Settlement periods tile the calendar with no gaps and no overlaps, so a
    PROCESSED refund is deducted from exactly one settlement -- never zero, and
    never two. Overlapping periods would double-count refunds batch-wide."""
    rows = db.execute("""SELECT settlement_id, settlement_period_start, settlement_period_end
                         FROM settlements WHERE dataset_id=%s ORDER BY settlement_period_start""",
                      (demo_run["dataset_id"],)).fetchall()
    for a, b in zip(rows, rows[1:]):
        assert a["settlement_period_end"] < b["settlement_period_start"], "periods overlap"
        assert (b["settlement_period_start"] - a["settlement_period_end"]).days == 1, "gap in tiling"
    dupes = db.execute("""
        SELECT r.refund_id, count(*) c FROM refunds r JOIN settlements s
          ON s.dataset_id=r.dataset_id
         AND r.refund_date BETWEEN s.settlement_period_start AND s.settlement_period_end
        WHERE r.dataset_id=%s AND r.refund_status='PROCESSED'
        GROUP BY r.refund_id HAVING count(*) > 1""", (demo_run["dataset_id"],)).fetchall()
    assert dupes == [], "a refund fell into two settlement periods"


def test_clearing_account_nets_to_zero_except_where_ledger_anomalies_were_planted(db, demo_run):
    policy = load_policy()
    snap = load(db, demo_run["dataset_id"])
    inv = check(snap)
    planted = {g["settlement_id"] for g in db.execute(
        "SELECT settlement_id FROM ground_truth_anomalies WHERE dataset_id=%s "
        "AND anomaly_type LIKE 'D3_%%'", (demo_run["dataset_id"],)).fetchall()}
    for s in snap.settlements:
        d3 = compute_d3(snap, s, policy, inv.excluded_items)
        if s["settlement_id"] not in planted:
            assert d3.delta_paise == 0, f"{s['settlement_id']} has an unplanted ledger imbalance"


@pytest.mark.parametrize("cands,target,expect", [
    ([("a", 100), ("b", 200), ("c", 300)], 300, ["a", "b"]),         # exactly one subset
    ([("a", 100), ("b", 200), ("c", 100), ("d", 200)], 300, None),   # two subsets -> refuse
    ([("a", 100), ("b", 200)], 999, None),                           # no subset
    ([("a", 300)], 300, None),                                       # size-1 is not a "split"
])
def test_subset_sum_refuses_ambiguity(cands, target, expect):
    got, n = find_unique_subset(cands, target, 10, 4)
    if expect is None:
        assert got is None
        assert n != 1
    else:
        assert sorted(got) == sorted(expect)
        assert n == 1


def test_subset_sum_stays_inside_its_bound():
    """C(10,4) = 210 subsets worst case. The bound is what makes this provable
    rather than heuristic, so it must actually be enforced."""
    cands = [(f"x{i}", i + 1) for i in range(50)]
    got, n = find_unique_subset(cands, 10_000_000, max_candidates=10, max_size=4)
    assert got is None and n == 0
