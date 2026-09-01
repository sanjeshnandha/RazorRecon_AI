"""
The 19 golden scenarios from the build spec, run against the real seeded batch.

Tests 12, 13, 16, 17 and 19 are the ones that prove the system has judgment
rather than pattern-matching the happy path: they all assert that the engine
REFUSES to produce an answer.
"""
import pytest

from engine.db import fetch


# --------------------------------------------------------------------- helpers
def gt_rows(db, dataset_id, anomaly_type):
    return fetch(db, "SELECT * FROM ground_truth_anomalies WHERE dataset_id=%s AND anomaly_type=%s "
                     "ORDER BY anomaly_id", (dataset_id, anomaly_type))


def delta(db, run_id, settlement_id, kind, subject_id=None):
    if subject_id:
        rows = fetch(db, "SELECT * FROM reconciliation_deltas WHERE run_id=%s AND delta_kind=%s "
                         "AND subject_id=%s", (run_id, kind, subject_id))
    else:
        rows = fetch(db, "SELECT * FROM reconciliation_deltas WHERE run_id=%s AND settlement_id=%s "
                         "AND delta_kind=%s AND subject_id IS NULL", (run_id, settlement_id, kind))
    return rows[0] if rows else None


def attrs(db, run_id, delta_id):
    return fetch(db, "SELECT * FROM attributions WHERE run_id=%s AND delta_id=%s ORDER BY attribution_id",
                 (run_id, delta_id))


def excs(db, run_id, settlement_id, kind=None, subject_id=None):
    sql = "SELECT * FROM exceptions WHERE run_id=%s AND settlement_id=%s"
    params = [run_id, settlement_id]
    if kind:
        sql += " AND delta_kind=%s"; params.append(kind)
    if subject_id:
        sql += " AND subject_id=%s"; params.append(subject_id)
    return fetch(db, sql + " ORDER BY exception_id", tuple(params))


def cands(db, run_id, settlement_id):
    return fetch(db, "SELECT * FROM match_candidates WHERE run_id=%s AND settlement_id=%s "
                     "ORDER BY pass_name", (run_id, settlement_id))


@pytest.fixture(scope="module")
def R(demo_run):
    return demo_run


# ------------------------------------------------------------------ 1. clean
def test_01_clean_settlements_have_every_delta_at_zero(db, R):
    dirty = {g["settlement_id"] for g in fetch(
        db, "SELECT DISTINCT settlement_id FROM ground_truth_anomalies WHERE dataset_id=%s",
        (R["dataset_id"],)) if g["settlement_id"]}
    rows = fetch(db, "SELECT * FROM reconciliation_deltas WHERE run_id=%s AND subject_id IS NULL",
                 (R["run_id"],))
    clean = [r for r in rows if r["settlement_id"] not in dirty]
    assert len(clean) >= 120, "expected a large clean majority to verify against"
    offenders = [(r["settlement_id"], r["delta_kind"], r["delta_paise"]) for r in clean
                 if r["delta_paise"] != 0 or r["tier"] != "A"]
    assert offenders == [], f"clean settlements must be exactly zero: {offenders[:5]}"


# --------------------------------------------------------- 2. fee rate drift
def test_02_fee_rate_drift(db, R):
    for g in gt_rows(db, R["dataset_id"], "D1_FEE_RATE_DRIFT"):
        d = delta(db, R["run_id"], g["settlement_id"], "D1_COMPUTE")
        assert d["delta_paise"] == g["planted_amount_paise"]
        assert d["residual_paise"] == 0 and d["tier"] == "A"
        a = attrs(db, R["run_id"], d["delta_id"])
        assert {x["evidence_record_id"] for x in a} == {g["subject_id"]}
        assert {x["evidence_type"] for x in a} == {"fee", "tax_on_fee"}
        assert all(x["derivation"] == "DETERMINISTIC" for x in a)
        assert any("POLICY.MDR." in r for x in a for r in x["rule_ids"])
        assert any(e["exception_type"] == "FEE_RATE_MISMATCH"
                   for e in excs(db, R["run_id"], g["settlement_id"], "D1_COMPUTE"))


# ------------------------------------------------------ 3. GST aggregate GST
def test_03_gst_aggregate_rounding(db, R):
    for g in gt_rows(db, R["dataset_id"], "D1_TAX_AGGREGATE_ROUNDING"):
        d = delta(db, R["run_id"], g["settlement_id"], "D1_COMPUTE")
        assert abs(d["delta_paise"]) == g["planted_amount_paise"]
        assert d["residual_paise"] == 0 and d["tier"] == "A"
        a = attrs(db, R["run_id"], d["delta_id"])
        assert [x["evidence_type"] for x in a] == ["tax"]
        assert a[0]["evidence_record_id"] == g["settlement_id"]
        assert any(e["exception_type"] == "TAX_ROUNDING_MISMATCH"
                   for e in excs(db, R["run_id"], g["settlement_id"], "D1_COMPUTE"))


# ------------------------------------------------------- 4. refund not taken
def test_04_refund_omitted_cites_the_refund_id(db, R):
    for g in gt_rows(db, R["dataset_id"], "D1_REFUND_NOT_DEDUCTED"):
        d = delta(db, R["run_id"], g["settlement_id"], "D1_COMPUTE")
        assert d["residual_paise"] == 0 and d["tier"] == "A"
        a = attrs(db, R["run_id"], d["delta_id"])
        assert g["subject_id"] in {x["evidence_record_id"] for x in a}
        assert abs(next(x for x in a if x["evidence_record_id"] == g["subject_id"]
                        )["signed_amount_paise"]) == g["planted_amount_paise"]


# ------------------------------------------------------ 5. adjustment explains
def test_05_chargeback_adjustment_explains_the_gap(db, R):
    for g in gt_rows(db, R["dataset_id"], "D1_ADJUSTMENT_APPLIED"):
        d = delta(db, R["run_id"], g["settlement_id"], "D1_COMPUTE")
        assert d["delta_paise"] == g["planted_amount_paise"]
        assert d["residual_paise"] == 0 and d["tier"] == "A"
        a = attrs(db, R["run_id"], d["delta_id"])
        assert g["subject_id"] in {x["evidence_record_id"] for x in a}


# --------------------------------- 6. refund already itemised -> no attribution
def test_06_refund_present_in_items_creates_no_attribution(db, R):
    """The double-count guard: a refund that IS itemised is already inside
    expected_net and must never be attributed a second time."""
    itemised = fetch(db, """
        SELECT DISTINCT si.refund_id FROM settlement_items si
        WHERE si.dataset_id=%s AND si.transaction_type='REFUND'""", (R["dataset_id"],))
    ids = {r["refund_id"] for r in itemised}
    assert len(ids) > 50, "need a meaningful population of correctly-itemised refunds"
    used = fetch(db, "SELECT DISTINCT evidence_record_id FROM attributions "
                     "WHERE run_id=%s AND evidence_type='refund'", (R["run_id"],))
    assert {u["evidence_record_id"] for u in used} & ids == set(), \
        "an itemised refund was attributed -- that is a double count"


# ----------------------------------------------------------------- 7. timing
def test_07_timing_next_business_day(db, R):
    for g in gt_rows(db, R["dataset_id"], "D2_TIMING_NEXT_DAY"):
        d = delta(db, R["run_id"], g["settlement_id"], "D2_BANK")
        assert d["delta_paise"] == 0, "the money did arrive"
        assert d["tier"] == "A"
        e = [x for x in excs(db, R["run_id"], g["settlement_id"], "D2_BANK")]
        assert any(x["exception_type"] == "TIMING_DIFFERENCE" for x in e)
        t = next(x for x in e if x["exception_type"] == "TIMING_DIFFERENCE")
        assert t["unexplained_paise"] == 0 and t["status"] == "AUTO_RESOLVED"
        assert "tolerance" in t["recommended_action"] or "policy tolerance" in t["recommended_action"]


# ---------------------------------------------------- 8. narration without UTR
def test_08_narration_without_utr_is_tier_B_when_only_amount_reaches_it(db, R):
    wide = [g for g in gt_rows(db, R["dataset_id"], "D2_NARRATION_NO_UTR")
            if g["expected_exception_type"] == "UTR_MISSING"]
    assert wide, "expected some no-UTR credits outside the tight tolerance window"
    for g in wide:
        d = delta(db, R["run_id"], g["settlement_id"], "D2_BANK")
        assert d["delta_paise"] == 0
        assert d["tier"] == "B", "amount-only evidence must never reach tier A"
        assert d["status"] == "REVIEW"
        a = attrs(db, R["run_id"], d["delta_id"])
        assert any(x["derivation"] == "FUZZY" for x in a)
        assert any(x["pass_name"] == "AMOUNT_WIDE_WINDOW" and x["is_selected"]
                   for x in cands(db, R["run_id"], g["settlement_id"]))


# ---------------------------------------------------------- 9. merged credit
def test_09_merged_credit_resolved_by_subset_sum(db, R):
    rows = gt_rows(db, R["dataset_id"], "D2_MERGED_CREDIT")
    assert rows
    for g in rows:
        d = delta(db, R["run_id"], g["settlement_id"], "D2_BANK")
        assert d["delta_paise"] == 0 and d["tier"] == "A", g["settlement_id"]
        c = [x for x in cands(db, R["run_id"], g["settlement_id"])
             if x["pass_name"] == "SUBSET_SUM_MERGED" and x["is_selected"]]
        assert c, "must be resolved by subset-sum, not guessed"
        assert any(e["exception_type"] == "MERGED_BANK_CREDIT"
                   for e in excs(db, R["run_id"], g["settlement_id"], "D2_BANK"))


def test_09b_split_credit_resolved_by_subset_sum(db, R):
    for g in gt_rows(db, R["dataset_id"], "D2_SPLIT_CREDIT"):
        d = delta(db, R["run_id"], g["settlement_id"], "D2_BANK")
        assert d["delta_paise"] == 0 and d["tier"] == "A"
        sel = [x for x in cands(db, R["run_id"], g["settlement_id"])
               if x["pass_name"] == "SUBSET_SUM_SPLIT" and x["is_selected"]]
        assert len(sel) >= 2, "a split credit is two or more bank lines"


# --------------------------------------------------------- 10. duplicate ledger
def test_10_duplicate_ledger_cites_the_entry_group(db, R):
    for g in gt_rows(db, R["dataset_id"], "D3_DUPLICATE_LEDGER"):
        d = delta(db, R["run_id"], g["settlement_id"], "D3_LEDGER")
        assert d["delta_paise"] != 0
        assert d["residual_paise"] == 0 and d["tier"] == "A"
        a = attrs(db, R["run_id"], d["delta_id"])
        assert a and a[0]["evidence_type"] == "duplicate_ledger"
        assert a[0]["evidence_record_id"] == g["subject_id"], "the entry_group_id must be cited"


def test_10b_missing_and_misposted_ledger(db, R):
    for g in gt_rows(db, R["dataset_id"], "D3_MISSING_LEDGER"):
        d = delta(db, R["run_id"], g["settlement_id"], "D3_LEDGER")
        assert d["residual_paise"] == 0 and d["tier"] == "A"
        assert any(e["exception_type"] == "MISSING_LEDGER_ENTRY"
                   for e in excs(db, R["run_id"], g["settlement_id"], "D3_LEDGER"))
    for g in gt_rows(db, R["dataset_id"], "D3_WRONG_ACCOUNT"):
        d = delta(db, R["run_id"], g["settlement_id"], "D3_LEDGER")
        assert d["residual_paise"] == 0 and d["tier"] == "A"
        a = attrs(db, R["run_id"], d["delta_id"])
        assert g["subject_id"] in {x["evidence_record_id"] for x in a}
        assert any(e["exception_type"] == "MISPOSTED_ACCOUNT"
                   for e in excs(db, R["run_id"], g["settlement_id"], "D3_LEDGER"))


# ------------------------------------------------- 11. allocation != transfer
def test_11_allocation_transfer_divergence(db, R):
    for g in gt_rows(db, R["dataset_id"], "D4_ALLOC_TRANSFER_DIVERGENCE"):
        d = delta(db, R["run_id"], None, "D4_PAYOUT", subject_id=g["subject_id"])
        assert d["delta_paise"] == g["planted_amount_paise"]
        assert d["residual_paise"] == 0 and d["tier"] == "A"
        a = attrs(db, R["run_id"], d["delta_id"])
        assert a[0]["evidence_type"] == "transfer", "a reversed transfer is the evidence"


def test_11b_allocation_exceeds_payment(db, R):
    for g in gt_rows(db, R["dataset_id"], "D4_ALLOC_EXCEEDS_PAYMENT"):
        alloc = fetch(db, "SELECT payment_id FROM seller_allocations WHERE dataset_id=%s AND "
                          "allocation_id=%s", (R["dataset_id"], g["subject_id"]))[0]
        d = delta(db, R["run_id"], None, "D4_PAYOUT", subject_id=alloc["payment_id"])
        assert d is not None and abs(d["delta_paise"]) == g["planted_amount_paise"]
        assert d["residual_paise"] == 0 and d["tier"] == "A"


# ----------------------------------------------------------- 12. phantom debit
def test_12_phantom_debit_is_tier_C_with_zero_attributions(db, R):
    rows = gt_rows(db, R["dataset_id"], "UNRESOLVABLE_PHANTOM_DEBIT")
    assert len(rows) == 3
    for g in rows:
        d = delta(db, R["run_id"], g["settlement_id"], "D1_COMPUTE")
        assert d["delta_paise"] == g["planted_amount_paise"]
        assert attrs(db, R["run_id"], d["delta_id"]) == [], "zero attributions is the answer"
        assert d["residual_paise"] == d["delta_paise"]
        assert d["tier"] == "C" and d["status"] == "UNRESOLVED"
        e = next(x for x in excs(db, R["run_id"], g["settlement_id"], "D1_COMPUTE"))
        assert e["exception_type"] == "UNEXPLAINED_SHORTFALL"
        assert e["unexplained_paise"] == g["planted_amount_paise"]
        assert e["severity"] == "HIGH" and "ESCALATE" in e["recommended_action"]


# ----------------------------------------------------- 13. ambiguous credits
def test_13_two_equally_matching_credits_are_never_auto_matched(db, R):
    rows = gt_rows(db, R["dataset_id"], "UNRESOLVABLE_AMBIGUOUS_CREDIT")
    assert len(rows) == 2
    for g in rows:
        d = delta(db, R["run_id"], g["settlement_id"], "D2_BANK")
        assert d["tier"] == "C" and d["status"] == "UNRESOLVED"
        assert attrs(db, R["run_id"], d["delta_id"]) == []
        assert any(e["exception_type"] == "AMBIGUOUS_BANK_MATCH"
                   for e in excs(db, R["run_id"], g["settlement_id"], "D2_BANK"))
        assert not [c for c in cands(db, R["run_id"], g["settlement_id"]) if c["is_selected"]]


# -------------------------------------------- 14. refund outside the period
def test_14_out_of_period_refund_is_never_attributed(db, R):
    rows = gt_rows(db, R["dataset_id"], "D1_REFUND_OUTSIDE_PERIOD")
    assert rows
    for g in rows:
        d = delta(db, R["run_id"], g["settlement_id"], "D1_COMPUTE")
        assert d["delta_paise"] == 0, "the settlement was correct and must stay correct"
        assert attrs(db, R["run_id"], d["delta_id"]) == []
        assert [e for e in excs(db, R["run_id"], g["settlement_id"], "D1_COMPUTE")] == []
    # and the refund must not be attributed anywhere at all -- it is correctly
    # itemised in the settlement whose period actually contains it
    refund_ids = {g["subject_id"] for g in rows if g["subject_type"] == "refund"}
    for rid in refund_ids:
        used = fetch(db, "SELECT delta_id FROM attributions WHERE run_id=%s AND evidence_record_id=%s",
                     (R["run_id"], rid))
        assert used == [], f"{rid} sits between period_end and settlement_date; it is not evidence"


# --------------------------------- 15. fee already corrected by an adjustment
def test_15_fee_correction_is_not_double_counted(db, R):
    rows = gt_rows(db, R["dataset_id"], "D1_FEE_CORRECTED_BY_ADJUSTMENT")
    assert rows
    for g in rows:
        d = delta(db, R["run_id"], g["settlement_id"], "D1_COMPUTE")
        assert d["residual_paise"] == 0 and d["tier"] == "A"
        a = attrs(db, R["run_id"], d["delta_id"])
        fee = [x for x in a if x["evidence_type"] == "fee"]
        assert len(fee) == 1
        # the adjustment must NOT also be attributed on its own
        assert not [x for x in a if x["evidence_type"] == "adjustment"], \
            "the corrected rupees must be counted once, not once per evidence type"
        assert sum(x["signed_amount_paise"] for x in a) == d["delta_paise"]
        assert "FEE_CORRECTION" in fee[0]["rationale"]


# ------------------------------------------------------- 16. suffix collision
def test_16_utr_suffix_collision_refuses_to_select(db, R):
    rows = gt_rows(db, R["dataset_id"], "D2_SUFFIX_COLLISION")
    assert rows
    for g in rows:
        c = cands(db, R["run_id"], g["settlement_id"])
        selected = [x for x in c if x["is_selected"]]
        assert not [x for x in selected if x["pass_name"] == "UTR_SUFFIX"], \
            "a shared suffix must never win -- that is a coin flip, not a match"
        suffix_rows = [x for x in c if x["pass_name"] == "UTR_SUFFIX"]
        assert all(x["is_ambiguous"] for x in suffix_rows), "the refusal must be recorded"


# --------------------------------------------------- 17. same amount, same day
def test_17_same_amount_same_day_refuses_to_select(db, R):
    rows = gt_rows(db, R["dataset_id"], "D2_SAME_AMOUNT_SAME_DAY")
    assert rows
    for g in rows:
        c = cands(db, R["run_id"], g["settlement_id"])
        assert not [x for x in c if x["pass_name"] == "EXACT_AMOUNT_DATE" and x["is_selected"]], \
            "amount+date is not identity when a sibling settlement shares both"
        d = delta(db, R["run_id"], g["settlement_id"], "D2_BANK")
        assert d["tier"] == "C"


# --------------------------------------------------------- 18. missing transfer
def test_18_settled_allocation_with_no_transfer(db, R):
    rows = gt_rows(db, R["dataset_id"], "D4_TRANSFER_MISSING")
    assert rows
    for g in rows:
        d = delta(db, R["run_id"], None, "D4_PAYOUT", subject_id=g["subject_id"])
        assert d["expected_paise"] == g["planted_amount_paise"]
        assert d["actual_paise"] == 0
        assert d["delta_paise"] == g["planted_amount_paise"]
        assert d["residual_paise"] == 0 and d["tier"] == "A"
        a = attrs(db, R["run_id"], d["delta_id"])
        assert a[0]["evidence_record_id"] == g["subject_id"]


# ------------------------------------------------------ 19. phantom payout gap
def test_19_phantom_payout_gap_is_tier_C(db, R):
    rows = gt_rows(db, R["dataset_id"], "UNRESOLVABLE_PHANTOM_PAYOUT_GAP")
    assert len(rows) == 2
    for g in rows:
        d = delta(db, R["run_id"], None, "D4_PAYOUT", subject_id=g["subject_id"])
        assert d["delta_paise"] == g["planted_amount_paise"]
        assert attrs(db, R["run_id"], d["delta_id"]) == []
        assert d["residual_paise"] == d["delta_paise"]
        assert d["tier"] == "C" and d["status"] == "UNRESOLVED"
        e = fetch(db, "SELECT * FROM exceptions WHERE run_id=%s AND subject_id=%s",
                  (R["run_id"], g["subject_id"]))
        assert e and e[0]["exception_type"] == "PHANTOM_PAYOUT_GAP"


# ------------------------------------------------------ batch-level guarantees
def test_batch_false_auto_resolution_is_zero(db, R):
    assert R["metrics"]["ground_truth"]["false_auto_resolution_count"] == 0, \
        R["metrics"]["ground_truth"]["false_auto_resolutions"]


def test_batch_all_seven_unresolvable_cases_escalate(db, R):
    g = R["metrics"]["ground_truth"]
    assert g["unresolvable_planted"] == 7
    assert g["unresolvable_correctly_escalated"] == 7


def test_batch_every_resolvable_anomaly_was_detected(db, R):
    g = R["metrics"]["ground_truth"]
    assert g["resolvable_detection_rate_pct"] == 100.0
    assert g["diagnosis_accuracy_pct"] == 100.0


def test_batch_false_positive_traps_all_avoided(db, R):
    g = R["metrics"]["ground_truth"]
    assert g["false_positive_traps_avoided"] == g["false_positive_traps"]


def test_batch_completes_under_ten_seconds(db, R):
    assert R["metrics"]["throughput"]["elapsed_seconds"] < 10.0


def test_attribution_residual_invariant_holds_on_every_delta(db, R):
    bad = fetch(db, """
        SELECT d.delta_id, d.delta_paise, d.explained_paise, d.residual_paise,
               COALESCE(SUM(a.signed_amount_paise),0) AS attributed
        FROM reconciliation_deltas d
        LEFT JOIN attributions a ON a.run_id=d.run_id AND a.delta_id=d.delta_id
        WHERE d.run_id=%s
        GROUP BY d.delta_id, d.delta_paise, d.explained_paise, d.residual_paise
        HAVING COALESCE(SUM(a.signed_amount_paise),0) + d.residual_paise <> d.delta_paise
            OR COALESCE(SUM(a.signed_amount_paise),0) <> d.explained_paise
    """, (R["run_id"],))
    assert bad == [], f"sum(attributions) + residual must equal delta: {bad[:3]}"


def test_every_attribution_points_at_a_real_record(db, R):
    orphans = fetch(db, """
        SELECT a.evidence_type, a.evidence_record_id FROM attributions a
        WHERE a.run_id=%s AND NOT EXISTS (
            SELECT 1 FROM payments p WHERE p.dataset_id=%s AND p.payment_id=a.evidence_record_id
            UNION ALL SELECT 1 FROM refunds r WHERE r.dataset_id=%s AND r.refund_id=a.evidence_record_id
            UNION ALL SELECT 1 FROM adjustments j WHERE j.dataset_id=%s AND j.adjustment_id=a.evidence_record_id
            UNION ALL SELECT 1 FROM settlements s WHERE s.dataset_id=%s AND s.settlement_id=a.evidence_record_id
            UNION ALL SELECT 1 FROM seller_allocations l WHERE l.dataset_id=%s AND l.allocation_id=a.evidence_record_id
            UNION ALL SELECT 1 FROM transfers t WHERE t.dataset_id=%s AND t.transfer_id=a.evidence_record_id
            UNION ALL SELECT 1 FROM bank_transactions b WHERE b.dataset_id=%s AND b.bank_transaction_id=a.evidence_record_id
            UNION ALL SELECT 1 FROM ledger_entries e WHERE e.dataset_id=%s AND
                      (e.ledger_entry_id=a.evidence_record_id OR e.entry_group_id=a.evidence_record_id)
        )""", (R["run_id"],) + (R["dataset_id"],) * 8)
    assert orphans == [], f"attributions must cite records that exist: {orphans[:5]}"


def test_no_tier_A_delta_carries_a_residual(db, R):
    bad = fetch(db, "SELECT delta_id FROM reconciliation_deltas WHERE run_id=%s AND tier='A' "
                    "AND residual_paise <> 0", (R["run_id"],))
    assert bad == []
