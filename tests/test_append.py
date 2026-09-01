"""
Append mode: a dataset that keeps growing.

The engine was always able to re-reconcile a dataset. What these tests defend is
that a SECOND batch bolted onto a first one is still a coherent book -- the
calendar stays a closed tiling across the seam, no id is minted twice, refund
headroom survives, and the ledger of a payment settled two cycles ago is not
disturbed by a refund that arrives today.
"""
import pytest

from engine import runner
from engine.calculation import compute_d3
from engine.invariants import check
from engine.loader import load
from engine.policy import load_policy
from generator.append import tick
from generator.generate import build, persist

SEQUENCES = [
    ("orders", "order_id"), ("payments", "payment_id"), ("refunds", "refund_id"),
    ("seller_allocations", "allocation_id"), ("transfers", "transfer_id"),
    ("adjustments", "adjustment_id"), ("settlements", "settlement_id"),
    ("settlement_items", "settlement_item_id"), ("bank_transactions", "bank_transaction_id"),
    ("ledger_entries", "ledger_entry_id"), ("ground_truth_anomalies", "anomaly_id"),
]


def _fresh(db, seed, label, settlements=12, clean=False):
    ds = build(seed, settlements, load_policy(), label, with_anomalies=not clean)
    with db.cursor() as cur:
        cur.execute("DELETE FROM datasets WHERE dataset_id=%s", (ds.dataset_id,))
    db.commit()
    persist(ds, db)
    return ds.dataset_id


@pytest.fixture(scope="module")
def grown(db):
    """One dataset, ticked twice. Two ticks, not one -- a single append can pass
    by accident where the second reveals a counter that never advanced."""
    dataset_id = _fresh(db, 4242, "append-tests")
    first = tick(db, dataset_id, settlements=6)
    second = tick(db, dataset_id, settlements=6)
    return {"dataset_id": dataset_id, "ticks": [first, second]}


def test_append_keeps_the_calendar_a_closed_tiling(db, grown):
    """The seam between two batches is the one place a gap or an overlap can
    appear, and either would break the refund period gate batch-wide."""
    rows = db.execute("""SELECT settlement_id, settlement_period_start s, settlement_period_end e
                         FROM settlements WHERE dataset_id=%s ORDER BY settlement_period_start""",
                      (grown["dataset_id"],)).fetchall()
    assert len(rows) == 24, "two ticks of 6 on a 12-settlement base"
    for a, b in zip(rows, rows[1:]):
        assert a["e"] < b["s"], f"periods overlap at {a['settlement_id']}/{b['settlement_id']}"
        assert (b["s"] - a["e"]).days == 1, f"gap at {a['settlement_id']}/{b['settlement_id']}"


@pytest.mark.parametrize("table,column", SEQUENCES)
def test_append_never_mints_an_id_twice(db, grown, table, column):
    """Every id sequence has to resume from its high-water mark. Restarting at 1
    is the single most likely append bug and it is a primary-key violation, so
    it must be impossible rather than merely unlikely."""
    dupes = db.execute(f"""SELECT {column} FROM {table} WHERE dataset_id=%s
                           GROUP BY {column} HAVING count(*) > 1 LIMIT 5""",
                       (grown["dataset_id"],)).fetchall()
    assert dupes == [], f"duplicate {column} after append: {dupes}"


def test_late_refunds_land_on_a_later_settlement_than_their_payment(db, grown):
    """The point of appending rather than regenerating: a refund can arrive for a
    payment whose settlement closed cycles ago. That is a Delta-2 timing
    difference, and until now the engine could handle one but never saw one."""
    assert sum(t["late_refunds"] for t in grown["ticks"]) > 0, "no late refunds were generated"
    crossed = db.execute("""
        SELECT count(*) c FROM refunds r
        JOIN settlement_items ri ON ri.dataset_id=r.dataset_id AND ri.refund_id=r.refund_id
        JOIN settlement_items pi ON pi.dataset_id=r.dataset_id AND pi.payment_id=r.payment_id
                                AND pi.transaction_type='PAYMENT'
        WHERE r.dataset_id=%s AND ri.settlement_id <> pi.settlement_id""",
        (grown["dataset_id"],)).fetchone()["c"]
    assert crossed > 0, "no refund was netted off a settlement later than its payment's"


def test_a_late_refund_does_not_disturb_a_closed_settlements_ledger(db, grown):
    """A refund arriving after its payment settled takes the money out of BANK,
    not out of RAZORPAY_CLEARING -- the clearing balance for that payment was
    closed by its own settlement posting. Crediting clearing again would
    manufacture a Delta-3 imbalance on a settlement nobody touched."""
    policy = load_policy()
    snap = load(db, grown["dataset_id"])
    inv = check(snap)
    planted = {g["settlement_id"] for g in db.execute(
        "SELECT settlement_id FROM ground_truth_anomalies WHERE dataset_id=%s "
        "AND anomaly_type LIKE 'D3_%%'", (grown["dataset_id"],)).fetchall()}
    for s in snap.settlements:
        d3 = compute_d3(snap, s, policy, inv.excluded_items)
        if s["settlement_id"] not in planted:
            assert d3.delta_paise == 0, \
                f"{s['settlement_id']} picked up an unplanted ledger imbalance after append"


def test_refund_headroom_survives_appending(db, grown):
    """INV-B2. A late refund is chosen against a payment that already has
    refunds, so the headroom calculation is load-bearing, not decorative."""
    bad = db.execute("""
        SELECT r.payment_id FROM refunds r
        JOIN payments p ON p.dataset_id=r.dataset_id AND p.payment_id=r.payment_id
        WHERE r.dataset_id=%s AND r.refund_status='PROCESSED'
        GROUP BY r.payment_id, p.amount_paise
        HAVING SUM(r.refund_amount_paise) > p.amount_paise""",
        (grown["dataset_id"],)).fetchall()
    assert bad == []


def test_every_appended_entry_group_still_balances(db, grown):
    """INV-B1 across the seam, including the late-refund groups."""
    bad = db.execute("""
        SELECT entry_group_id FROM ledger_entries WHERE dataset_id=%s GROUP BY entry_group_id
        HAVING SUM(CASE WHEN direction='DR' THEN amount_paise ELSE -amount_paise END) <> 0""",
        (grown["dataset_id"],)).fetchall()
    assert bad == []


def test_ground_truth_continues_across_batches(db, grown):
    """Detection rate is scored against planted anomalies for the whole dataset.
    If an appended batch restarted the GT series it would silently overwrite the
    first batch's ground truth and the score would become a fiction."""
    total = db.execute("SELECT count(*) c FROM ground_truth_anomalies WHERE dataset_id=%s",
                       (grown["dataset_id"],)).fetchone()["c"]
    appended = sum(t["appended"]["ground_truth_anomalies"] for t in grown["ticks"])
    assert appended > 0, "appended batches planted nothing to detect"
    assert total > appended, "the base batch's ground truth was lost"

    gt = grown["ticks"][-1]["run"]["ground_truth"]
    assert gt["planted_total"] == total, "the run scored against a different ground truth"
    assert gt["false_auto_resolution_count"] == 0, \
        "an appended anomaly was auto-resolved on evidence that does not support it"
    assert gt["resolvable_detection_rate_pct"] == 100.0, "an appended anomaly went undetected"
    assert gt["diagnosis_accuracy_pct"] == 100.0, "an appended anomaly was misdiagnosed"


def test_bank_credit_in_flight_is_resolved_by_the_next_cycle(db, grown):
    """The last settlement of a batch has not been credited when the batch
    closes -- T+2 lands after the cutoff. It must show as open, then close on the
    next tick. An exception that heals itself is the clearest possible proof
    that the run is a picture of a moment rather than a fixed verdict."""
    assert grown["ticks"][0]["bank_credits_in_flight"] == 1
    assert grown["ticks"][1]["bank_credits_resolved"] == 1


def test_clean_append_still_reconciles_to_exactly_zero(db):
    """The phase-4 gate, extended across a batch boundary. If appending broke
    any arithmetic, a clean dataset would stop netting to zero and this is where
    it shows up."""
    dataset_id = _fresh(db, 909, "append-clean", settlements=10, clean=True)
    tick(db, dataset_id, settlements=6, clean=True, reconcile=False)
    m = runner.run(db, dataset_id)
    bad = db.execute("""SELECT delta_id, delta_paise FROM reconciliation_deltas
                        WHERE run_id=%s AND delta_paise <> 0 LIMIT 5""",
                     (m["run_id"],)).fetchall()
    assert bad == [], f"clean data stopped reconciling after append: {bad}"
    assert m["tiers"]["tier_c"] == 0
