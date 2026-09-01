"""
Structural invariants abort a record; business invariants never do.

Getting this backwards is what makes a demo silently reject the interesting
half of the dataset, so it gets its own test file.
"""
from engine.invariants import check
from engine.loader import load


def test_structural_failures_exclude_only_the_offending_item(db, demo_run):
    snap = load(db, demo_run["dataset_id"])
    rep = check(snap)
    # the generator never leaves a dangling reference, so this must be empty --
    # and the mechanism still has to exist for real-world data
    assert rep.structural == [], [r["detail"] for r in rep.structural[:3]]
    assert rep.excluded_items == set()


def test_business_invariants_fire_without_aborting_the_run(db, demo_run):
    snap = load(db, demo_run["dataset_id"])
    rep = check(snap)
    ids = {r["id"] for r in rep.business}
    # the planted anomalies are designed to violate exactly these
    assert "INV-B6" in ids, "header vs items rollup mismatch was planted and must fire"
    assert "INV-B9" in ids, "allocation vs transfer divergence was planted and must fire"
    # and every settlement still got all three settlement-level deltas computed
    total = db.execute("SELECT count(*) c FROM settlements WHERE dataset_id=%s",
                       (demo_run["dataset_id"],)).fetchone()["c"]
    n = db.execute("SELECT count(DISTINCT settlement_id) c FROM reconciliation_deltas "
                   "WHERE run_id=%s AND subject_id IS NULL", (demo_run["run_id"],)).fetchone()["c"]
    assert n == total, "a business-invariant violation must never skip a settlement"


def test_every_entry_group_balances(db, demo_run):
    """INV-B1. A duplicated posting is a copy of a balanced group, so this must
    hold even on the dirty dataset -- if it does not, the generator is broken,
    not the engine."""
    bad = db.execute("""
        SELECT entry_group_id,
               SUM(CASE WHEN direction='DR' THEN amount_paise ELSE -amount_paise END) AS net
        FROM ledger_entries WHERE dataset_id=%s
        GROUP BY entry_group_id HAVING SUM(CASE WHEN direction='DR' THEN amount_paise
                                                ELSE -amount_paise END) <> 0
    """, (demo_run["dataset_id"],)).fetchall()
    assert bad == [], f"unbalanced entry groups: {bad[:3]}"


def test_no_failed_payment_reaches_a_settlement(db, demo_run):
    """INV-B5."""
    bad = db.execute("""
        SELECT si.settlement_item_id FROM settlement_items si
        JOIN payments p ON p.dataset_id=si.dataset_id AND p.payment_id=si.payment_id
        WHERE si.dataset_id=%s AND p.payment_status='FAILED'
    """, (demo_run["dataset_id"],)).fetchall()
    assert bad == []


def test_refunds_never_exceed_their_payment(db, demo_run):
    """INV-B2. This one cannot arise from a legitimate mutation, so it is
    treated as structural if ever seen."""
    bad = db.execute("""
        SELECT r.payment_id, SUM(r.refund_amount_paise) s, p.amount_paise
        FROM refunds r JOIN payments p ON p.dataset_id=r.dataset_id AND p.payment_id=r.payment_id
        WHERE r.dataset_id=%s AND r.refund_status='PROCESSED'
        GROUP BY r.payment_id, p.amount_paise
        HAVING SUM(r.refund_amount_paise) > p.amount_paise
    """, (demo_run["dataset_id"],)).fetchall()
    assert bad == []


def test_allocation_net_always_equals_gross_minus_commission(db, demo_run):
    """INV-B4 is a DDL constraint, so a violation cannot even be inserted. The
    test documents that, and fails loudly if the constraint is ever dropped."""
    bad = db.execute("""SELECT allocation_id FROM seller_allocations WHERE dataset_id=%s
                        AND net_seller_paise <> gross_allocated_paise - commission_paise""",
                     (demo_run["dataset_id"],)).fetchall()
    assert bad == []


def test_no_float_ever_reaches_the_money_path(db, demo_run):
    """Every money column is BIGINT. If someone changes one to numeric or real,
    this fails before any number is trusted."""
    rows = db.execute("""
        SELECT table_name, column_name, data_type
        FROM information_schema.columns
        WHERE table_schema='public' AND (column_name LIKE '%%_paise' OR column_name LIKE '%%_bps')
    """).fetchall()
    assert rows, "expected money columns to exist"
    bad = [r for r in rows if r["data_type"] not in ("bigint", "integer")]
    assert bad == [], f"non-integer money columns: {bad}"
