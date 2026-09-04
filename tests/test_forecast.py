"""
The forward cash position.

The forecaster's one job is to be *derived* rather than predicted: every date
comes from the working-day calendar and the policy registry, and every line
points back at a record the engine already persisted. So the tests here are
mostly about arithmetic identity and provenance, not about plausibility.

The one behavioural claim worth stating loudly: the forecast reads the matcher's
D2 verdict instead of re-deciding what was matched. A naive "settlement with no
bank line joined on UTR" query calls Rs 1.2 Cr in flight on the demo dataset,
when the true figure is Rs 3.8 L -- because 20 of those 21 settlements were
matched on evidence other than the UTR. test_reads_the_matcher_verdict pins that.
"""
from datetime import date, timedelta

import pytest

from engine import forecast as fc
from engine.db import fetch, fetch_one
from engine.policy import load_policy
from generator.calendar import is_working_day, working_days_between

POLICY = load_policy()


@pytest.fixture(autouse=True)
def _clean_tx(db):
    """One bad statement must not poison every test after it."""
    yield
    db.rollback()


@pytest.fixture(scope="module")
def f(db, demo_run):
    return fc.build(db, demo_run["run_id"], demo_run["dataset_id"])


# --------------------------------------------------------------- due dates ---
def test_credit_due_date_is_lag_plus_tolerance_in_working_days():
    d = date(2026, 3, 2)                       # a Monday
    due = fc.credit_due_date(d, POLICY)
    assert working_days_between(d, due, POLICY) == (
        POLICY.expected_lag_days + POLICY.bank_tolerance_days)
    assert is_working_day(due, POLICY)


def test_due_date_skips_non_working_days():
    """A settlement dated just before a weekend must not come due inside it."""
    for offset in range(0, 21):
        d = date(2026, 3, 2) + timedelta(days=offset)
        if not is_working_day(d, POLICY):
            continue
        assert is_working_day(fc.credit_due_date(d, POLICY), POLICY)


def test_awaited_before_due_and_overdue_after():
    settled = date(2026, 3, 2)
    due = fc.credit_due_date(settled, POLICY)
    assert fc.settlement_status(settled, settled, POLICY)["state"] == "AWAITED"
    assert fc.settlement_status(settled, due, POLICY)["state"] == "AWAITED"
    late = fc.settlement_status(settled, due + timedelta(days=30), POLICY)
    assert late["state"] == "OVERDUE"
    assert late["working_days_overdue"] > 0
    assert late["due_date"] == due.isoformat()


def test_status_carries_the_rule_that_dated_it():
    s = fc.settlement_status(date(2026, 3, 2), date(2026, 3, 2), POLICY)
    assert "POLICY.BANK" in s["rule"]


def test_projected_settlement_date_uses_the_cycle():
    cap = date(2026, 3, 2)
    proj = fc.projected_settlement_date(cap, POLICY)
    assert working_days_between(cap, proj, POLICY) == POLICY.cycle_working_days


# ------------------------------------------------------------ the roll-up ---
def test_as_of_is_the_day_after_the_book_closed(db, demo_run, f):
    last = fetch_one(db, """SELECT MAX(settlement_period_end) AS e FROM settlements
                            WHERE dataset_id=%s""", (demo_run["dataset_id"],))["e"]
    assert date.fromisoformat(f.as_of) > last


def test_every_day_is_a_working_day(f):
    assert f.days, "the demo run should schedule something"
    for d in f.days:
        assert is_working_day(date.fromisoformat(d["date"]), POLICY), d["date"]


def test_horizon_is_honoured(db, demo_run):
    for h in (5, 15, 30):
        n = len(fc.build(db, demo_run["run_id"], demo_run["dataset_id"], horizon=h).days)
        # as_of itself counts when it is a working day, hence h or h+1
        assert h <= n <= h + 1, (h, n)


def test_running_balance_is_the_cumulative_net(f):
    running = 0
    for d in f.days:
        assert d["net_paise"] == d["in_paise"] - d["out_paise"]
        running += d["net_paise"]
        assert d["running_paise"] == running


def test_totals_equal_the_sum_of_the_days(f):
    t = f.totals
    assert t["inflow_paise"] == sum(d["in_paise"] for d in f.days)
    assert t["outflow_paise"] == sum(d["out_paise"] for d in f.days)
    assert t["net_paise"] == t["inflow_paise"] - t["outflow_paise"]
    if f.days:
        assert f.days[-1]["running_paise"] == t["net_paise"]


def test_buckets_partition_the_window(f):
    t = f.totals
    assert (t["settlement_awaited_paise"] + t["pipeline_paise"]) == t["inflow_paise"]
    assert t["seller_payout_paise"] == t["outflow_paise"]


def test_money_is_integer_paise(f):
    for d in f.days:
        for k in ("in_paise", "out_paise", "net_paise", "running_paise"):
            assert isinstance(d[k], int), (d["date"], k)
    for l in f.lines:
        assert isinstance(l.amount_paise, int)
        assert l.amount_paise > 0, "direction carries the sign, never the amount"


def test_every_line_cites_a_record_and_a_rule(f):
    for l in f.lines:
        assert l.direction in ("IN", "OUT")
        assert l.bucket in ("settlement_awaited", "pipeline", "seller_payout")
        assert l.subject_type and l.subject_id
        assert l.basis
        assert l.rule.startswith("POLICY."), l.rule


def test_nothing_inside_the_window_is_dated_before_as_of(f):
    for d in f.days:
        assert d["date"] >= f.as_of


def test_overdue_is_dated_before_as_of_and_never_in_the_window(f):
    for o in f.overdue + f.overdue_payouts:
        assert o["due_date"] < f.as_of, o
    window = {d["date"] for d in f.days}
    for o in f.overdue + f.overdue_payouts:
        assert o["due_date"] not in window


def test_assumptions_are_stated(f):
    assert f.assumptions and all(isinstance(a, str) for a in f.assumptions)


# ------------------------------------------------------------- provenance ---
def test_reads_the_matcher_verdict_not_a_naive_utr_join(db, demo_run, f):
    """The whole design rule, as an assertion.

    A settlement the engine matched on EXACT_AMOUNT_DATE rather than the UTR is
    money that has already landed. Counting it as awaited would overstate the
    in-flight position by orders of magnitude.
    """
    naive = fetch_one(db, """
        SELECT COALESCE(SUM(s.net_settlement_amount_paise), 0) AS p
        FROM settlements s
        WHERE s.dataset_id = %s
          AND NOT EXISTS (SELECT 1 FROM bank_transactions b
                          WHERE b.dataset_id = s.dataset_id
                            AND b.settlement_utr = s.settlement_utr)""",
                      (demo_run["dataset_id"],))["p"]
    awaited = f.totals["settlement_awaited_paise"] + sum(
        o["amount_paise"] for o in f.overdue)
    assert awaited < naive, (
        "the forecast must not re-derive 'unmatched' from the UTR: "
        f"naive={naive} forecast={awaited}")


def test_awaited_settlements_all_carry_a_d2_residue(db, demo_run, f):
    ids = {l.subject_id for l in f.lines if l.bucket == "settlement_awaited"}
    ids |= {o["settlement_id"] for o in f.overdue}
    for sid in ids:
        r = fetch_one(db, """SELECT SUM(ABS(delta_paise)) AS d
                             FROM reconciliation_deltas
                             WHERE run_id=%s AND settlement_id=%s AND delta_kind='D2_BANK'""",
                      (demo_run["run_id"], sid))
        assert r and r["d"], f"{sid} is awaited but the matcher raised no D2 residue"


def test_payout_lines_are_pending_allocations(db, demo_run, f):
    ids = [l.subject_id for l in f.lines if l.bucket == "seller_payout"]
    assert ids, "the demo dataset has pending allocations"
    rows = fetch(db, """SELECT allocation_id, allocation_status FROM seller_allocations
                        WHERE dataset_id=%s AND allocation_id = ANY(%s)""",
                 (demo_run["dataset_id"], list(set(ids))))
    assert len(rows) == len(set(ids))
    assert {r["allocation_status"] for r in rows} == {"PENDING"}


def test_pipeline_lines_are_captured_but_unsettled(db, demo_run, f):
    ids = [l.subject_id for l in f.lines if l.bucket == "pipeline"]
    if not ids:
        pytest.skip("no pipeline captures in this dataset")
    rows = fetch(db, """SELECT p.payment_id, p.payment_status, si.settlement_id
                        FROM payments p
                        LEFT JOIN settlement_items si
                               ON si.dataset_id = p.dataset_id
                              AND si.payment_id = p.payment_id
                        WHERE p.dataset_id=%s AND p.payment_id = ANY(%s)""",
                 (demo_run["dataset_id"], list(set(ids))))
    assert rows
    for r in rows:
        assert r["payment_status"] == "CAPTURED", r
        assert r["settlement_id"] is None, f"{r['payment_id']} is already itemised"


def test_it_is_a_pure_read(db, demo_run):
    """The forecaster must never write. If it did, the digest would move."""
    def digest():
        return fetch_one(db, """SELECT
              (SELECT count(*) FROM settlements WHERE dataset_id=%(d)s) AS s,
              (SELECT count(*) FROM payments WHERE dataset_id=%(d)s) AS p,
              (SELECT count(*) FROM seller_allocations WHERE dataset_id=%(d)s) AS a,
              (SELECT count(*) FROM reconciliation_deltas WHERE run_id=%(r)s) AS d,
              (SELECT count(*) FROM exceptions WHERE run_id=%(r)s) AS e,
              (SELECT count(*) FROM audit_log WHERE run_id=%(r)s) AS l""",
                         {"d": demo_run["dataset_id"], "r": demo_run["run_id"]})
    before = digest()
    fc.build(db, demo_run["run_id"], demo_run["dataset_id"])
    assert digest() == before


def test_determinism(db, demo_run):
    a = fc.to_dict(fc.build(db, demo_run["run_id"], demo_run["dataset_id"]))
    b = fc.to_dict(fc.build(db, demo_run["run_id"], demo_run["dataset_id"]))
    assert a == b


def test_to_dict_is_json_serialisable(f):
    import json
    json.loads(json.dumps(fc.to_dict(f)))


# ------------------------------------------------------------- agent tool ---
def test_agent_tool_is_registered():
    from agent import tools as t
    assert "get_cash_forecast" in t.HANDLERS
    assert {s["function"]["name"] for s in t.SCHEMAS} == set(t.HANDLERS)


def test_agent_tool_returns_rupee_strings_and_bounds(db, demo_run):
    from agent import tools as t
    ctx = {"run_id": demo_run["run_id"], "dataset_id": str(demo_run["dataset_id"])}
    r = t.dispatch(db, ctx, "get_cash_forecast", {"horizon_working_days": 10, "limit": 3})
    assert "error" not in r
    assert r["horizon_working_days"] == 10 and 10 <= len(r["days"]) <= 11
    assert r["totals"]["inflow_rupees"].startswith(("Rs", "-Rs", "−Rs"))
    assert len(r["lines"]) <= 3
    assert r["lines_shown"] == len(r["lines"])
    assert len(r["overdue_credits"]) <= 20 and len(r["overdue_payouts"]) <= 20
    assert "prediction" in r["caveat"]


def test_agent_tool_clamps_a_hostile_horizon(db, demo_run):
    from agent import tools as t
    ctx = {"run_id": demo_run["run_id"], "dataset_id": str(demo_run["dataset_id"])}
    for bad, expect in ((100000, 60), (-5, 1), ("abc", 15), (None, 15)):
        r = t.dispatch(db, ctx, "get_cash_forecast", {"horizon_working_days": bad})
        assert r["horizon_working_days"] == expect, (bad, r["horizon_working_days"])


def test_agent_tool_bucket_filter(db, demo_run):
    from agent import tools as t
    ctx = {"run_id": demo_run["run_id"], "dataset_id": str(demo_run["dataset_id"])}
    r = t.dispatch(db, ctx, "get_cash_forecast", {"bucket": "seller_payout", "limit": 60})
    assert r["lines"] and all(l["bucket"] == "seller_payout" for l in r["lines"])


def test_agent_tool_cannot_reach_another_run(db, demo_run):
    """Scoping is the security argument -- a bad run_id must not leak a forecast."""
    from agent import tools as t
    import uuid
    ctx = {"run_id": str(uuid.uuid4()), "dataset_id": str(uuid.uuid4())}
    r = t.dispatch(db, ctx, "get_cash_forecast", {})
    assert r.get("error") or (r["totals"]["inflow_paise"] == 0
                              and r["totals"]["outflow_paise"] == 0
                              and not r["overdue_credits"])


# ------------------------------------------- structured detail for the UI ---
def test_pipeline_lines_carry_the_dates_the_ui_shows(f):
    """The Cash position page builds columns from `detail`, not from prose.

    A capture's row has to answer three separate questions -- when it was taken,
    when it settles, when the cash actually lands -- and they are three different
    dates. Collapsing them into one is how a forecast starts lying.
    """
    pipe = [l for l in f.lines if l.bucket == "pipeline"]
    if not pipe:
        pytest.skip("no pipeline captures in this dataset")
    for l in pipe:
        d = l.detail
        assert set(d) >= {"capture_date", "settles_on", "credit_due", "method",
                          "gross_paise", "fee_paise", "cycle_working_days",
                          "working_days_until_credit"}
        assert d["capture_date"] <= d["settles_on"] <= d["credit_due"]
        assert d["credit_due"] == l.date
        assert d["gross_paise"] - d["fee_paise"] == l.amount_paise
        assert d["fee_paise"] >= 0
        assert isinstance(d["working_days_until_credit"], int)


def test_pipeline_fee_matches_the_policy_for_the_method(f):
    """The net is the gross less MDR and GST from the registry -- not a guess."""
    from engine.money import bps
    for l in (l for l in f.lines if l.bucket == "pipeline"):
        method = l.detail["method"]
        mdr = bps(l.detail["gross_paise"], POLICY._mdr[method])
        expected = mdr + bps(mdr, POLICY.gst_on_fee_bps)
        assert l.detail["fee_paise"] == expected, (l.subject_id, method)
        assert l.rule == f"POLICY.MDR.{method}@{POLICY.version}"


def test_zero_rated_method_has_no_fee(f):
    """UPI is 0 bps here. A zero fee must be a real zero, not a rounding smudge."""
    upi = [l for l in f.lines if l.bucket == "pipeline" and l.detail["method"] == "UPI"]
    if not upi:
        pytest.skip("no UPI captures in the pipeline")
    assert POLICY._mdr["UPI"] == 0
    for l in upi:
        assert l.detail["fee_paise"] == 0
        assert l.amount_paise == l.detail["gross_paise"]


def test_awaited_lines_carry_settlement_and_due_dates(f):
    for l in (l for l in f.lines if l.bucket == "settlement_awaited"):
        d = l.detail
        assert d["settlement_date"] < d["due_date"] == l.date
        assert isinstance(d["working_days_until_due"], int)


def test_payout_lines_name_the_seller(f):
    for l in (l for l in f.lines if l.bucket == "seller_payout"):
        d = l.detail
        assert d["seller_id"] and d["seller_name"]
        assert d["due_date"] == l.date
        assert d["capture_date"] <= l.date


def test_detail_is_json_safe(f):
    """It crosses the API. A date object here would 500 the endpoint."""
    import json
    for l in f.lines:
        json.loads(json.dumps(l.detail))
        for v in l.detail.values():
            assert isinstance(v, (str, int, float, bool, type(None))), (l.subject_id, v)
