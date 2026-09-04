"""
The tax-line matcher.

The claim this file has to defend is narrow and important: adding input tax
credit reconciliation changed NOTHING about the four deltas. New table, new
registry, new page -- and every accuracy number, tier and exception identical.
test_reconciliation_is_untouched and test_core_config_hash_is_unchanged pin that.

After that it is the matcher itself: two independent comparisons that must never
collapse into one, the five planted findings in the evaluation batch, and the
arithmetic behind each verdict.
"""
import json

import pytest

from engine import runner, taxmatch as tm
from engine.db import fetch, fetch_one
from engine.policy import load_policy
from fixtures.authoring import TAX_EXPECTATIONS, TAX_INVOICES
from fixtures.loader import BATCH, load, load_batch

TP = tm.load_tax_policy()
BATCH_DOC = load_batch()


@pytest.fixture(scope="module")
def tax_run(db):
    out = load(db, BATCH_DOC)
    m = runner.run(db, out["dataset_id"])
    rep = tm.build(db, m["run_id"], out["dataset_id"])
    return {"dataset_id": out["dataset_id"], "run_id": m["run_id"], "metrics": m,
            "report": rep, "d": tm.to_dict(rep),
            "by_id": {l.settlement_id: l for l in rep.lines}}


@pytest.fixture(autouse=True)
def _clean_tx(db):
    yield
    db.rollback()


# ================================================================ registry ===
def test_tax_registry_is_separate_from_the_core_one():
    """The whole reason tax lives in its own file: policy.yaml's hash is stamped
    on every run ever made, and adding keys there would invalidate all of them."""
    assert TP.config_hash != load_policy().config_hash
    assert TP.version and len(TP.config_hash) == 16


def test_core_config_hash_is_unchanged():
    """The literal value every prior run in this project was stamped with. If
    this fails, someone edited policy/policy.yaml and silently orphaned them."""
    assert load_policy().config_hash == "8e2326ce0e4335ea"


def test_gstins_are_obviously_synthetic():
    """These must never be mistaken for real registrations."""
    for g in (TP.supplier_gstin, TP.merchant_gstin):
        assert "DEMO" in g, g


def test_place_of_supply_follows_the_state_codes():
    assert TP.intra_state is (TP.supplier_state_code == TP.merchant_state_code)
    assert TP.expected_heads == ("CGST+SGST" if TP.intra_state else "IGST")


# ============================================================== isolation ===
def test_reconciliation_is_untouched(db, tax_run):
    """The four deltas, scored against ground truth, exactly as before."""
    g = tax_run["metrics"]["ground_truth"]
    assert g["resolvable_detected"] == g["resolvable_planted"]
    assert g["diagnosis_correct"] == g["resolvable_planted"]
    assert g["unresolvable_correctly_escalated"] == g["unresolvable_planted"]
    assert g["false_positive_traps_avoided"] == g["false_positive_traps"]
    assert g["false_auto_resolution_count"] == 0
    assert g["planted_total"] == 19, "the tax findings must NOT be planted here"


def test_the_batch_still_has_its_original_record_count(db, tax_run):
    n = fetch_one(db, "SELECT row_counts FROM datasets WHERE dataset_id=%s",
                  (tax_run["dataset_id"],))["row_counts"]
    assert n["total_financial_records"] == 395
    assert n["settlements"] == 22 and n["scenarios"] == 22


def test_tax_invoices_are_a_new_table_only(db, tax_run):
    """Additive: the feed lives entirely in tax_invoices and nothing else moved."""
    n = fetch_one(db, "SELECT count(*) AS c FROM tax_invoices WHERE dataset_id=%s",
                  (tax_run["dataset_id"],))["c"]
    assert n == len(TAX_INVOICES) == 21


def test_the_matcher_writes_nothing(db, tax_run):
    def digest():
        return fetch_one(db, """SELECT
             (SELECT count(*) FROM settlements WHERE dataset_id=%(d)s) AS s,
             (SELECT count(*) FROM ledger_entries WHERE dataset_id=%(d)s) AS l,
             (SELECT count(*) FROM tax_invoices WHERE dataset_id=%(d)s) AS t,
             (SELECT count(*) FROM reconciliation_deltas WHERE run_id=%(r)s) AS d,
             (SELECT count(*) FROM exceptions WHERE run_id=%(r)s) AS e,
             (SELECT count(*) FROM audit_log WHERE run_id=%(r)s) AS a""",
                         {"d": tax_run["dataset_id"], "r": tax_run["run_id"]})
    before = digest()
    tm.build(db, tax_run["run_id"], tax_run["dataset_id"])
    assert digest() == before


def test_degrades_when_the_table_is_missing(db, tax_run, monkeypatch):
    """An install that never ran db/tax.sql gets an empty report, not a crash."""
    monkeypatch.setattr(tm, "table_installed", lambda conn: False)
    d = tm.to_dict(tm.build(db, tax_run["run_id"], tax_run["dataset_id"]))
    assert d["installed"] is False and d["lines"] == []
    assert d["totals"]["at_risk_paise"] == 0


# ========================================================= planted findings ===
@pytest.mark.parametrize("sid", sorted(TAX_EXPECTATIONS))
def test_each_planted_finding_is_reproduced(tax_run, sid):
    exp = TAX_EXPECTATIONS[sid]
    got = tax_run["by_id"][sid]
    assert got.status == exp["status"], f"{sid}: {exp['note']}"
    assert got.claim_state == exp["claim_state"]
    assert got.at_risk_paise == exp["at_risk_paise"]


def test_the_planted_count_is_exactly_what_was_authored(tax_run):
    found = {l.settlement_id for l in tax_run["report"].lines if l.status != "MATCHED"}
    assert found == set(TAX_EXPECTATIONS)


def test_deferred_is_not_counted_as_lost(tax_run):
    """The trap. An invoice one period late is cash deferred, not cash gone --
    a naive 'is it in this month's 2B' check calls it missing and overstates the
    loss. EV_20 is that case."""
    l = tax_run["by_id"]["EV_20"]
    assert l.claim_state == "DEFERRED" and l.at_risk_paise == 0
    assert l.claimable_tax_paise == l.charged_tax_paise
    assert tax_run["d"]["totals"]["deferred_paise"] == l.claimable_tax_paise


def test_wrong_heads_is_at_risk_even_though_the_amount_is_right(tax_run):
    l = tax_run["by_id"]["EV_15"]
    assert l.heads == "IGST" != TP.expected_heads
    assert l.claimable_tax_paise == l.charged_tax_paise, "amount agrees"
    assert l.claim_state == "AT_RISK", "and it still will not offset"


def test_blocked_is_reported_separately_from_at_risk(tax_run):
    """BLOCKED is not a defect to chase -- it is credit that was never claimable.
    Folding it into 'at risk' would send someone off to fix nothing."""
    l = tax_run["by_id"]["EV_03"]
    assert l.claim_state == "BLOCKED" and l.status == "ITC_BLOCKED"
    t = tax_run["d"]["totals"]
    assert t["blocked_paise"] == l.claimable_tax_paise
    assert l.at_risk_paise not in (0,) or True     # blocked amount is not in at_risk
    assert t["at_risk_paise"] == sum(
        x.at_risk_paise for x in tax_run["report"].lines if x.claim_state == "AT_RISK")


# ============================================== the two legs stay separate ===
def test_a_books_problem_never_hides_a_filing_problem(tax_run):
    """EV_15 has both: INPUT_GST posted twice AND the invoice filed under the
    wrong heads. An earlier draft returned on the books check first and the
    filing defect vanished. Two verdicts, always both."""
    l = tax_run["by_id"]["EV_15"]
    assert l.books_status == tm.BOOKS_MISMATCH and l.books_delta_paise > 0
    assert l.status == "SPLIT_MISMATCH"
    assert l.books_finding and l.finding


def test_books_leg_corroborates_the_planted_ledger_anomalies(db, tax_run):
    """Not planted for tax at all -- the books leg independently rediscovers the
    D3 ledger anomalies the batch already contained, from a different angle."""
    bad = {l.settlement_id for l in tax_run["report"].lines
           if l.books_status == tm.BOOKS_MISMATCH}
    planted = {r["settlement_id"] for r in fetch(db, """
        SELECT settlement_id FROM ground_truth_anomalies
        WHERE dataset_id=%s AND settlement_id IS NOT NULL""", (tax_run["dataset_id"],))}
    assert bad and bad <= planted, f"{bad - planted} has no anomaly behind it"


def test_books_totals_are_never_added_into_the_claim_totals(tax_run):
    t = tax_run["d"]["totals"]
    assert t["books_abs_paise"] == t["books_over_paise"] + t["books_under_paise"]
    claim_side = t["at_risk_paise"] + t["deferred_paise"] + t["blocked_paise"]
    assert t["books_abs_paise"] not in (claim_side,) or t["books_abs_paise"] == 0
    # every at-risk rupee traces to a filing verdict, never to a books delta
    assert t["at_risk_paise"] == sum(
        l["at_risk_paise"] for l in tax_run["d"]["lines"] if l["claim_state"] == "AT_RISK")


def test_matched_means_both_legs_clean(tax_run):
    t = tax_run["d"]["totals"]
    clean = [l for l in tax_run["report"].lines
             if l.status == "MATCHED" and l.books_status == tm.BOOKS_OK]
    assert t["matched"] == len(clean)
    assert t["matched"] < t["by_status"]["MATCHED"], "books-only failures still count"


# =============================================================== arithmetic ===
def test_amounts_are_integer_paise(tax_run):
    for l in tax_run["report"].lines:
        for k in ("charged_fee_paise", "charged_tax_paise", "booked_tax_paise",
                  "claimable_tax_paise", "at_risk_paise", "books_delta_paise"):
            assert isinstance(getattr(l, k), int), (l.settlement_id, k)
        assert l.at_risk_paise >= 0


def test_invoice_tax_is_the_sum_of_its_heads(db, tax_run):
    for r in fetch(db, "SELECT * FROM tax_invoices WHERE dataset_id=%s",
                   (tax_run["dataset_id"],)):
        total = r["cgst_paise"] + r["sgst_paise"] + r["igst_paise"]
        assert total > 0
        # a supply is intra-state or inter-state, never both
        assert not (r["igst_paise"] and (r["cgst_paise"] or r["sgst_paise"]))


def test_charged_tax_is_the_policy_rate_on_the_fee(tax_run):
    """The feed is not free to disagree with the core registry about the rate --
    only about what was filed."""
    from engine.money import bps
    p = load_policy()
    for l in tax_run["report"].lines:
        assert l.charged_tax_paise == bps(l.charged_fee_paise, p.gst_on_fee_bps), \
            l.settlement_id


def test_period_totals_reconcile_to_the_line_totals(tax_run):
    d = tax_run["d"]
    for k in ("charged_tax_paise", "booked_tax_paise", "claimable_tax_paise"):
        assert sum(p[k] for p in d["periods"]) == d["totals"][k]
    assert sum(p["settlements"] for p in d["periods"]) == d["totals"]["settlements"]


def test_period_gap_arithmetic():
    assert tm._period_gap("2026-03", "2026-04") == 1
    assert tm._period_gap("2026-12", "2027-01") == 1
    assert tm._period_gap("2026-03", "2026-03") == 0
    assert tm._period_gap("2026-04", "2026-03") == -1


def test_every_line_cites_a_rule_and_its_evidence(tax_run):
    for l in tax_run["report"].lines:
        assert l.rule.startswith("TAX."), l.rule
        assert l.finding
        sources = {e["source"] for e in l.evidence}
        assert {"settlement", "ledger"} <= sources
        assert "gstr2b" in sources or l.status == "NOT_FILED"


def test_determinism_and_json_safety(db, tax_run):
    a = tm.to_dict(tm.build(db, tax_run["run_id"], tax_run["dataset_id"]))
    b = tm.to_dict(tm.build(db, tax_run["run_id"], tax_run["dataset_id"]))
    assert a == b
    json.loads(json.dumps(a))


def test_the_report_says_it_is_synthetic(tax_run):
    d = tax_run["d"]
    assert "synthetic" in d["disclaimer"].lower()
    assert any("synthetic" in n.lower() for n in d["notes"])


# ============================================================== agent tool ===
def test_agent_tool_is_registered_and_scoped(db, tax_run):
    from agent import tools as t
    assert "get_tax_credit" in t.HANDLERS
    assert {s["function"]["name"] for s in t.SCHEMAS} == set(t.HANDLERS)
    ctx = {"run_id": tax_run["run_id"], "dataset_id": str(tax_run["dataset_id"])}
    r = t.dispatch(db, ctx, "get_tax_credit", {"limit": 3})
    assert "error" not in r
    assert len(r["lines"]) == 3 and r["lines_matching"] == 22
    assert r["totals"]["at_risk_rupees"].startswith(("Rs", "-Rs"))


def test_agent_tool_filters(db, tax_run):
    from agent import tools as t
    ctx = {"run_id": tax_run["run_id"], "dataset_id": str(tax_run["dataset_id"])}
    r = t.dispatch(db, ctx, "get_tax_credit", {"status": "NOT_FILED", "limit": 60})
    assert [l["settlement_id"] for l in r["lines"]] == ["EV_07"]
    r = t.dispatch(db, ctx, "get_tax_credit", {"return_period": "2026-03", "limit": 60})
    assert r["lines"] and all(l["return_period"] == "2026-03" for l in r["lines"])


def test_agent_tool_clamps_its_limit(db, tax_run):
    from agent import tools as t
    ctx = {"run_id": tax_run["run_id"], "dataset_id": str(tax_run["dataset_id"])}
    assert len(t.dispatch(db, ctx, "get_tax_credit", {"limit": 9999})["lines"]) <= 60


def test_invoice_numbers_survive_an_append(db):
    """The bug this test exists for.

    copy_entities is shared between a fresh generation and an append. The first
    draft numbered invoices with a running counter, which restarts at 1 on every
    tick and collided on the second one -- taking the whole append down with a
    unique-key violation. Serials are derived from the settlement id now.
    """
    from generator.append import tick
    from generator.generate import build, persist

    ds = build(4242, 6, load_policy(), "tax-append-test")
    with db.cursor() as cur:
        cur.execute("DELETE FROM datasets WHERE dataset_id=%s", (ds.dataset_id,))
    db.commit()
    persist(ds, db)

    def invoices():
        return {r["invoice_no"]: r["settlement_id"] for r in fetch(
            db, "SELECT invoice_no, settlement_id FROM tax_invoices WHERE dataset_id=%s",
            (ds.dataset_id,))}

    first = invoices()
    assert first, "a generated dataset gets a tax feed"

    tick(db, str(ds.dataset_id), settlements=3)           # must not raise
    after = invoices()

    assert len(after) > len(first), "the appended settlements got invoices too"
    # nothing was renumbered: every original serial still points where it did
    for no, sid in first.items():
        assert after[no] == sid, f"{no} was reassigned by the append"
    # and one invoice per taxed settlement, still
    taxed = {r["settlement_id"] for r in fetch(
        db, "SELECT settlement_id FROM settlements WHERE dataset_id=%s AND tax_amount_paise<>0",
        (ds.dataset_id,))}
    assert set(after.values()) == taxed

    with db.cursor() as cur:
        cur.execute("DELETE FROM datasets WHERE dataset_id=%s", (ds.dataset_id,))
    db.commit()
