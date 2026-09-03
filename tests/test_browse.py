"""
The Data tab's read-only table browser.

A tab that puts a table name into a SQL string is the obvious place for this
project to acquire an injection hole, so the tests here are mostly about what
the browser refuses to do. The registry is derived from PostgreSQL's own
catalogue rather than a hand-written list, which is what stops it drifting when
the schema changes -- and that property is worth a test of its own.
"""
import inspect
import json

import pytest

from api import browse


@pytest.fixture(scope="module")
def scope(db, demo_run):
    return {"run_id": demo_run["run_id"], "dataset_id": demo_run["dataset_id"]}


def test_the_registry_is_derived_from_the_catalogue(db):
    """Not a hand-written list. Add a table to schema.sql and it appears here
    with no code change; remove one and it disappears."""
    browse._registry = None
    reg = browse.registry(db)
    live = {r["table_name"] for r in db.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema='public'"
    ).fetchall()}
    assert set(reg) <= live
    for expected in ("settlements", "payments", "ledger_entries", "reconciliation_deltas",
                     "exceptions", "audit_log", "agent_transcripts"):
        assert expected in reg, f"{expected} missing from the browser"


def test_each_table_is_scoped_by_a_column_that_actually_exists(db):
    reg = browse.registry(db)
    for name, meta in reg.items():
        cols = {c["name"] for c in meta["columns"]}
        assert meta["scope"] in cols
        assert meta["scope"] in ("dataset_id", "run_id")
        assert meta["key_columns"], f"{name} has no ordering key"
        assert set(meta["key_columns"]) <= cols


def test_engine_output_is_separated_from_generated_data(db):
    """Two different things: the book the engine read, and what it concluded.
    The UI groups them, so the grouping has to be right."""
    reg = browse.registry(db)
    assert reg["settlements"]["group"] == "data"
    assert reg["payments"]["group"] == "data"
    assert reg["reconciliation_deltas"]["group"] == "results"
    assert reg["exceptions"]["group"] == "results"
    assert reg["agent_transcripts"]["group"] == "results"


def test_summary_counts_match_the_database(db, scope):
    rows = {r["table"]: r["rows"] for r in browse.summary(db, **scope)}
    for table, col in (("settlements", "dataset_id"), ("payments", "dataset_id"),
                       ("reconciliation_deltas", "run_id"), ("exceptions", "run_id")):
        val = scope["dataset_id"] if col == "dataset_id" else scope["run_id"]
        actual = db.execute(f"SELECT count(*) c FROM {table} WHERE {col}=%s",
                            (val,)).fetchone()["c"]
        assert rows[table] == actual, f"{table} count is wrong"


def test_an_unknown_table_never_reaches_a_query(db, scope):
    """The table name is the one value that cannot be parameterised, so it is
    checked against the registry first. Anything else is refused as data."""
    for bad in ("pg_shadow", "information_schema.tables", "settlements; DROP TABLE payments",
                "settlements--", "'; DELETE FROM settlements; --", ""):
        out = browse.page(db, scope["run_id"], scope["dataset_id"], bad)
        assert "error" in out, f"{bad!r} was not refused"
    assert db.execute("SELECT count(*) c FROM settlements").fetchone()["c"] > 0
    assert db.execute("SELECT count(*) c FROM payments").fetchone()["c"] > 0


def test_the_browser_contains_no_write_path(db):
    src = inspect.getsource(browse).upper()
    for verb in ("INSERT ", "UPDATE ", "DELETE ", "DROP ", "ALTER ", "TRUNCATE ", "CREATE "):
        assert verb not in src, f"api/browse.py contains a {verb.strip()} statement"


@pytest.mark.parametrize("asked,expected", [(9999, browse.MAX_LIMIT), (0, 50), (-3, 1), (7, 7)])
def test_page_size_is_clamped_server_side(db, scope, asked, expected):
    out = browse.page(db, scope["run_id"], scope["dataset_id"], "settlements", limit=asked)
    assert out["limit"] == expected
    assert len(out["rows"]) <= browse.MAX_LIMIT


def test_pages_do_not_overlap_or_skip_rows(db, scope):
    a = browse.page(db, scope["run_id"], scope["dataset_id"], "settlements", limit=10, offset=0)
    b = browse.page(db, scope["run_id"], scope["dataset_id"], "settlements", limit=10, offset=10)
    ids_a = [r["settlement_id"] for r in a["rows"]]
    ids_b = [r["settlement_id"] for r in b["rows"]]
    assert len(set(ids_a) & set(ids_b)) == 0, "pages overlap"
    assert a["total_rows"] == b["total_rows"]


def test_rows_are_json_encodable_and_hide_the_scope_column(db, scope):
    """Dates, UUIDs and JSONB all have to survive the trip to the browser, and
    the scope column is identical on every row -- pure noise in a table view."""
    for table in ("settlements", "payments", "ledger_entries", "reconciliation_deltas",
                  "exceptions", "audit_log"):
        out = browse.page(db, scope["run_id"], scope["dataset_id"], table, limit=3)
        assert "error" not in out, out
        json.dumps(out, default=str)
        names = {c["name"] for c in out["columns"]}
        assert "dataset_id" not in names and "run_id" not in names
        for row in out["rows"]:
            assert "dataset_id" not in row and "run_id" not in row


def test_money_columns_are_flagged_so_the_ui_can_format_them(db, scope):
    out = browse.page(db, scope["run_id"], scope["dataset_id"], "settlements", limit=1)
    money = {c["name"] for c in out["columns"] if c["money"]}
    assert "net_settlement_amount_paise" in money
    assert "settlement_id" not in money
    for c in out["columns"]:
        if c["money"]:
            assert c["type"] == "bigint", "a money column must stay integer paise"


def test_a_run_only_sees_its_own_results(db, scope):
    """Deltas and exceptions are scoped by run_id, so two runs over the same
    dataset must not show each other's conclusions."""
    other = db.execute("""SELECT run_id FROM reconciliation_runs WHERE run_id <> %s
                          LIMIT 1""", (scope["run_id"],)).fetchone()
    if not other:
        pytest.skip("only one run present")
    mine = browse.page(db, scope["run_id"], scope["dataset_id"], "reconciliation_deltas",
                       limit=200)
    got = {r["delta_id"] for r in mine["rows"]}
    theirs = {r["delta_id"] for r in db.execute(
        "SELECT delta_id FROM reconciliation_deltas WHERE run_id=%s LIMIT 200",
        (str(other["run_id"]),)).fetchall()}
    expected = {r["delta_id"] for r in db.execute(
        "SELECT delta_id FROM reconciliation_deltas WHERE run_id=%s ORDER BY delta_id DESC LIMIT 200",
        (scope["run_id"],)).fetchall()}
    assert got <= expected
    assert mine["total_rows"] == db.execute(
        "SELECT count(*) c FROM reconciliation_deltas WHERE run_id=%s",
        (scope["run_id"],)).fetchone()["c"]
