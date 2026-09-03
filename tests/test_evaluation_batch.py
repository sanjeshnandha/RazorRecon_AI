"""
The static evaluation batch.

A fixed batch is only worth something if the answers it claims are the answers
the engine actually produces. Every scenario in evaluation_batch.json states the
delta it expects on each of the four axes, the worst tier, and the exception
types -- and every one of those claims is asserted here against a real run.

If the engine changes behaviour, these fail. If the batch is edited without
re-checking, these fail. That is the point: the file cannot quietly become a
description of something the system no longer does.
"""
import json

import pytest

from engine import runner
from engine.db import fetch, fetch_one
from fixtures.loader import BATCH, load, load_batch

BATCH_DOC = load_batch()
SCENARIOS = BATCH_DOC["scenarios"]
DELTA_KINDS = (("d1_paise", "D1_COMPUTE"), ("d2_paise", "D2_BANK"),
               ("d3_paise", "D3_LEDGER"), ("d4_paise", "D4_PAYOUT"))


@pytest.fixture(scope="module")
def batch_run(db):
    out = load(db, BATCH_DOC)
    m = runner.run(db, out["dataset_id"])
    return {"dataset_id": out["dataset_id"], "run_id": m["run_id"], "metrics": m,
            "row_counts": out["row_counts"]}


def _actual(db, run_id, settlement_id):
    rows = fetch(db, """SELECT delta_kind, SUM(delta_paise) AS delta_paise, MAX(tier) AS tier
                        FROM reconciliation_deltas WHERE run_id=%s AND settlement_id=%s
                        GROUP BY delta_kind""", (run_id, settlement_id))
    by = {r["delta_kind"]: r for r in rows}
    return {
        "deltas": {k: int(by[k]["delta_paise"]) if k in by else 0
                   for _, k in DELTA_KINDS},
        "worst_tier": max((r["tier"] or "A") for r in rows) if rows else "A",
        "exceptions": sorted({r["exception_type"] for r in fetch(
            db, "SELECT exception_type FROM exceptions WHERE run_id=%s AND settlement_id=%s",
            (run_id, settlement_id))}),
    }


# ------------------------------------------------------- the batch itself ---
def test_the_batch_is_big_enough_and_covers_every_family(batch_run):
    counts = batch_run["row_counts"]
    assert counts["total_financial_records"] >= 50, "the brief asks for a 50+ record batch"
    assert counts["settlements"] == len(SCENARIOS)
    families = {s["family"] for s in SCENARIOS}
    for required in ("CLEAN", "D1", "D2", "D3", "D4", "TRAP"):
        assert required in families, f"no {required} scenario in the batch"


def _reload_and_restore(db, batch_run):
    """Reloading deletes the dataset, which CASCADEs the run with it. Any test
    that reloads has to put the run back, or it pulls the module fixture out from
    under every test that follows."""
    out = load(db, BATCH_DOC)
    batch_run["run_id"] = runner.run(db, out["dataset_id"])["run_id"]
    return out


def test_the_batch_is_static(db, batch_run):
    """No seed, no sampling, no clock. Loading it twice must produce byte-identical
    rows, or it cannot be used to compare one version of the engine against another."""
    sql = """SELECT settlement_id, gross_amount_paise, net_settlement_amount_paise
             FROM settlements WHERE dataset_id=%s ORDER BY settlement_id"""
    first = _reload_and_restore(db, batch_run)
    snap1 = fetch(db, sql, (first["dataset_id"],))
    second = _reload_and_restore(db, batch_run)
    snap2 = fetch(db, sql, (second["dataset_id"],))
    assert first["dataset_id"] == second["dataset_id"], "the dataset_id must be a constant"
    assert snap1 == snap2


def test_loading_twice_replaces_rather_than_accumulates(db, batch_run):
    _reload_and_restore(db, batch_run)
    n = fetch_one(db, "SELECT count(*) c FROM settlements WHERE dataset_id=%s",
                  (batch_run["dataset_id"],))["c"]
    assert n == len(SCENARIOS), "a second load duplicated the batch"


def test_the_json_carries_its_own_documentation(batch_run):
    """An evaluator reads this file without reading the code, so every scenario
    has to say what was done to it and what should therefore happen."""
    for s in SCENARIOS:
        assert s["note"].strip(), f"{s['scenario_id']} has no explanation"
        assert len(s["note"]) > 80, f"{s['scenario_id']}'s note is too thin to audit"
        assert s["expected"], f"{s['scenario_id']} states no expected outcome"


def test_the_calendar_is_a_closed_tiling(db, batch_run):
    """Settlement periods must tile with no gaps and no overlaps, or the refund
    period gate is ambiguous and the EV07 trap means nothing."""
    rows = fetch(db, """SELECT settlement_id, settlement_period_start s, settlement_period_end e
                        FROM settlements WHERE dataset_id=%s ORDER BY settlement_period_start""",
                 (batch_run["dataset_id"],))
    for a, b in zip(rows, rows[1:]):
        assert a["e"] < b["s"], f"periods overlap at {a['settlement_id']}"
        assert (b["s"] - a["e"]).days == 1, f"gap after {a['settlement_id']}"


# ------------------------------------------- every scenario, one at a time ---
@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s["scenario_id"] for s in SCENARIOS])
def test_the_engine_produces_the_stated_deltas(db, batch_run, scenario):
    got = _actual(db, batch_run["run_id"], scenario["settlement"]["settlement_id"])
    for key, kind in DELTA_KINDS:
        want = scenario["expected"].get(key)
        if want is None:
            continue
        assert got["deltas"][kind] == want, (
            f"{scenario['scenario_id']} {kind}: the batch says {want} paise, the engine "
            f"produced {got['deltas'][kind]}. {scenario['note'][:120]}")


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s["scenario_id"] for s in SCENARIOS])
def test_the_engine_reaches_the_stated_tier_and_exceptions(db, batch_run, scenario):
    got = _actual(db, batch_run["run_id"], scenario["settlement"]["settlement_id"])
    exp = scenario["expected"]
    if exp.get("worst_tier"):
        assert got["worst_tier"] == exp["worst_tier"], (
            f"{scenario['scenario_id']}: expected tier {exp['worst_tier']}, got "
            f"{got['worst_tier']}")
    if exp.get("exception_types") is not None:
        assert got["exceptions"] == sorted(exp["exception_types"]), (
            f"{scenario['scenario_id']}: expected {sorted(exp['exception_types'])}, "
            f"got {got['exceptions']}")


def test_clean_scenarios_raise_nothing_at_all(db, batch_run):
    """Without these the batch would only prove the engine finds problems, not
    that it stays quiet when there are none."""
    for s in SCENARIOS:
        if s["family"] != "CLEAN":
            continue
        got = _actual(db, batch_run["run_id"], s["settlement"]["settlement_id"])
        assert all(v == 0 for v in got["deltas"].values()), f"{s['scenario_id']} is not clean"
        assert got["exceptions"] == []


def test_a_failed_payment_never_reaches_a_settlement(db, batch_run):
    """EV02 puts two FAILED attempts and one capture on the same order. INV-B5."""
    bad = fetch(db, """SELECT si.settlement_item_id FROM settlement_items si
                       JOIN payments p ON p.dataset_id=si.dataset_id AND p.payment_id=si.payment_id
                       WHERE si.dataset_id=%s AND p.payment_status='FAILED'""",
                (batch_run["dataset_id"],))
    assert bad == []
    retried = fetch(db, """SELECT order_id, count(*) c FROM payments WHERE dataset_id=%s
                           GROUP BY order_id HAVING count(*) > 1""", (batch_run["dataset_id"],))
    assert retried, "the batch is supposed to contain a retried order"


# -------------------------------------------------------- honesty scoring ---
def test_the_engine_scores_perfectly_against_this_batch(batch_run):
    """Every planted anomaly detected and diagnosed, every undiagnosable case
    escalated, every trap avoided, and no false auto-resolution. This is the
    number an evaluator is really here for."""
    g = batch_run["metrics"]["ground_truth"]
    assert g["resolvable_detection_rate_pct"] == 100.0, g
    assert g["diagnosis_accuracy_pct"] == 100.0, g
    assert g["correct_escalation_rate_pct"] == 100.0, g
    assert g["false_positive_traps_avoided"] == g["false_positive_traps"], g
    assert g["false_auto_resolution_count"] == 0, g


def test_every_ground_truth_row_belongs_to_a_scenario(db, batch_run):
    rows = fetch(db, "SELECT settlement_id, anomaly_type FROM ground_truth_anomalies "
                     "WHERE dataset_id=%s", (batch_run["dataset_id"],))
    known = {s["settlement"]["settlement_id"] for s in SCENARIOS}
    assert rows, "the batch plants nothing to detect"
    for r in rows:
        assert r["settlement_id"] in known


def test_traps_are_labelled_as_traps_not_as_escalations(batch_run):
    """is_resolvable=False means 'must escalate to tier C'; a false-positive trap
    is is_resolvable=True with an expected type of NONE. Confusing the two makes
    the honesty score measure the wrong thing."""
    for s in SCENARIOS:
        for g in s["ground_truth"]:
            if g["expected_exception_type"] == "NONE":
                assert g["is_resolvable"] is True, (
                    f"{s['scenario_id']}: a trap must be is_resolvable=True, or it is scored "
                    f"as an escalation that should reach tier C")


def test_the_batch_matches_the_policy_it_claims(batch_run):
    from engine.policy import load_policy
    p = load_policy()
    assert BATCH_DOC["policy_version"] == p.version
    assert BATCH_DOC["config_hash"] == p.config_hash, (
        "policy.yaml changed since the batch was authored -- re-run "
        "`python -m fixtures.authoring` and re-check the expected figures")


def test_the_batch_file_is_valid_json_and_committed():
    assert BATCH.exists(), "fixtures/evaluation_batch.json must ship with the repo"
    json.loads(BATCH.read_text())
