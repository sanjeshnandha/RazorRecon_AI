"""
The investigation agent.

The agent is the one component that is not deterministic, so what gets tested is
everything around it: that its tools cannot reach outside the run they were
opened on, cannot be talked into arbitrary SQL, cannot write, and that a claim
it makes about a record it never read is caught rather than shipped.

The model itself is stubbed. That is deliberate -- these tests must pass in CI
with no API key and no network, or the agent is untested exactly where it
matters.
"""
import json

import pytest

from agent import store, tools
from agent.investigator import investigate
from agent.llm import LLMClient, LLMConfig, LLMUnavailable

ALL_TOOLS = sorted(tools.HANDLERS)


# --------------------------------------------------------------------- stub ---
class StubClient(LLMClient):
    """Replays a scripted list of assistant messages. Signature-compatible with
    the real client so the loop under test is the real loop."""

    def __init__(self, script):
        self.config = LLMConfig(provider="stub", label="Stub", model="stub-1", base_url="",
                                api_key="stub")
        self.script = list(script)
        self.seen = []
        self.calls = 0

    def complete(self, messages, tools=None, temperature=0.0, max_tokens=1500):
        self.seen.append(messages[-1])
        self.calls += 1
        return self.script.pop(0) if self.script else {"content": "done"}


def tool_call(name, args, cid="c1"):
    return {"content": "", "tool_calls": [
        {"id": cid, "type": "function",
         "function": {"name": name, "arguments": json.dumps(args)}}]}


@pytest.fixture(scope="module")
def ctx(db, demo_run):
    return {"run_id": demo_run["run_id"], "dataset_id": demo_run["dataset_id"]}


@pytest.fixture(scope="module")
def other_run(db):
    """A second, independent run. Scoping is the security property that matters
    most here, so it gets tested against a real neighbour rather than skipped
    when the database happens to hold only one run."""
    from engine import runner
    from engine.policy import load_policy
    from generator.generate import build, persist
    ds = build(31337, 14, load_policy(), "agent-scope-tests")
    with db.cursor() as cur:
        cur.execute("DELETE FROM datasets WHERE dataset_id=%s", (ds.dataset_id,))
    db.commit()
    persist(ds, db)
    m = runner.run(db, ds.dataset_id)
    return {"run_id": m["run_id"], "dataset_id": ds.dataset_id}


@pytest.fixture(scope="module")
def worst(db, demo_run, ctx):
    rows = tools.list_settlements(db, ctx, limit=1)["settlements"]
    assert rows, "the demo run is supposed to contain unexplained residue"
    return rows[0]["settlement_id"]


# --------------------------------------------------------------- the tools ---
@pytest.mark.parametrize("name", ALL_TOOLS)
def test_every_tool_returns_json_encodable_output(db, ctx, worst, name):
    """Tool results are json.dumps'd before the model sees them. A UUID or a date
    that will not encode is a hard failure mid-conversation, not a cosmetic one."""
    args = {}
    if name in ("get_settlement", "get_evidence", "get_matcher_trail", "get_payments",
                "get_ledger", "get_audit"):
        args = {"settlement_id": worst}
    if name == "trace_money":
        args = {"node_type": "settlement", "node_id": worst}
    out = tools.dispatch(db, ctx, name, args)
    assert "error" not in out, out
    json.dumps(out)


def test_tools_contain_no_write_path(db):
    """The security argument for pointing a language model at financial data is
    that there is nothing here for it to write with. Assert that, don't assume it."""
    import inspect
    src = inspect.getsource(tools).upper()
    for verb in ("INSERT ", "UPDATE ", "DELETE ", "DROP ", "ALTER ", "TRUNCATE ", "CREATE "):
        assert verb not in src, f"agent/tools.py contains a {verb.strip()} statement"


def test_a_tool_reads_only_the_run_it_was_opened_on(db, ctx, other_run):
    """Settlement ids restart per dataset, so SET_0005 exists in both runs with
    different numbers behind it. The context decides which one a tool sees --
    never an argument the model supplies. If scoping leaked, this is where a
    question about one merchant's run would quietly answer with another's."""
    theirs = {"run_id": other_run["run_id"], "dataset_id": other_run["dataset_id"]}
    shared = "SET_0005"

    mine_rows = db.execute("""SELECT delta_kind, delta_paise FROM reconciliation_deltas
                              WHERE run_id=%s AND settlement_id=%s AND subject_id IS NULL
                              ORDER BY delta_kind""", (ctx["run_id"], shared)).fetchall()
    their_rows = db.execute("""SELECT delta_kind, delta_paise FROM reconciliation_deltas
                               WHERE run_id=%s AND settlement_id=%s AND subject_id IS NULL
                               ORDER BY delta_kind""", (theirs["run_id"], shared)).fetchall()
    assert mine_rows and their_rows, "both runs should carry a SET_0005"

    got = {(d["delta_kind"], d["delta_paise"]) for d in
           tools.get_settlement(db, ctx, settlement_id=shared)["deltas"]}
    assert got == {(r["delta_kind"], r["delta_paise"]) for r in mine_rows}

    # and the neighbour's context returns the neighbour's numbers, not ours
    got_theirs = {(d["delta_kind"], d["delta_paise"]) for d in
                  tools.get_settlement(db, theirs, settlement_id=shared)["deltas"]}
    assert got_theirs == {(r["delta_kind"], r["delta_paise"]) for r in their_rows}


def test_a_settlement_outside_the_dataset_is_not_reachable(db, ctx):
    """The dataset boundary holds even for a well-formed id that simply is not
    part of this run."""
    for tool in ("get_settlement", "get_matcher_trail", "get_evidence", "get_audit",
                 "get_payments", "get_ledger"):
        out = tools.dispatch(db, ctx, tool, {"settlement_id": "SET_9999"})
        assert "error" in out, f"{tool} answered for a settlement outside the dataset: {out}"


def test_a_real_subject_is_echoed_back_even_when_it_has_no_evidence(db, ctx):
    """The citation guard treats an id as supported only if it appeared in a tool
    result. A settlement the engine could attribute nothing to would otherwise be
    reported as an unsupported reference on the very case being investigated --
    so a tool that finds the record must say so, even when it returns no rows."""
    empty = db.execute("""SELECT s.settlement_id FROM settlements s
                          WHERE s.dataset_id=%s AND NOT EXISTS (
                            SELECT 1 FROM attributions a
                              JOIN reconciliation_deltas d ON d.run_id=a.run_id
                                                          AND d.delta_id=a.delta_id
                             WHERE a.run_id=%s AND d.settlement_id=s.settlement_id)
                          LIMIT 1""", (ctx["dataset_id"], ctx["run_id"])).fetchone()
    if not empty:
        pytest.skip("every settlement in this run carries attribution evidence")
    sid = empty["settlement_id"]
    out = tools.get_evidence(db, ctx, settlement_id=sid)
    assert out["count"] == 0
    assert out["settlement_id"] == sid
    assert sid in json.dumps(out)


@pytest.mark.parametrize("asked,expected", [(9999, tools.MAX_ROWS), (0, 1), (-5, 1), ("x", 20)])
def test_row_limits_are_clamped_here_not_trusted_from_arguments(db, ctx, asked, expected):
    out = tools.list_settlements(db, ctx, min_unexplained_paise=0, limit=asked)
    assert out["limit"] == expected
    assert len(out["settlements"]) <= tools.MAX_ROWS


def test_unknown_tool_is_refused_as_data_not_an_exception(db, ctx):
    """A confused model must get something it can react to, not a 500."""
    out = tools.dispatch(db, ctx, "'; DROP TABLE settlements; --", {})
    assert "error" in out and "unknown tool" in out["error"]
    assert db.execute("SELECT count(*) c FROM settlements").fetchone()["c"] > 0


def test_bad_arguments_come_back_as_data(db, ctx):
    assert "error" in tools.dispatch(db, ctx, "get_settlement", {"wrong_arg": 1})
    assert "error" in tools.dispatch(db, ctx, "get_settlement", {})
    assert "error" in tools.dispatch(db, ctx, "trace_money",
                                     {"node_type": "secrets", "node_id": "x"})


def test_money_is_returned_as_paise_and_a_display_string(db, ctx, worst):
    d = tools.get_settlement(db, ctx, settlement_id=worst)
    for delta in d["deltas"]:
        assert isinstance(delta["residual_paise"], int)
        assert delta["residual_paise_display"].startswith(("Rs", "-Rs"))
    assert tools.rupees(-102334579) == "-Rs 10,23,345.79"
    assert tools.rupees(0) == "Rs 0.00"
    assert tools.rupees(100) == "Rs 1.00"


def test_get_settlement_summarises_payout_deltas_instead_of_dumping_them(db, ctx, worst):
    """A busy settlement has dozens of D4 rows that are almost all zero. Returning
    them whole buries the delta that actually has a residue."""
    d = tools.get_settlement(db, ctx, settlement_id=worst)
    assert all(x["delta_kind"] != "D4_PAYOUT" for x in d["deltas"])
    if d["d4_payout_summary"]:
        s = d["d4_payout_summary"]
        assert s["allocations_with_a_difference"] <= s["allocation_count"]
    assert len(json.dumps(d)) < 20_000


def test_an_empty_matcher_trail_explains_itself(db, ctx):
    """No candidate at all means something different from candidates that failed
    to match, and the model will invent a reason if the tool does not say which."""
    rows = db.execute("""SELECT s.settlement_id FROM settlements s
                         WHERE s.dataset_id=%s AND NOT EXISTS (
                           SELECT 1 FROM match_candidates c WHERE c.run_id=%s
                            AND c.settlement_id=s.settlement_id) LIMIT 1""",
                      (ctx["dataset_id"], ctx["run_id"])).fetchall()
    if not rows:
        pytest.skip("every settlement in this run had a matcher candidate")
    out = tools.get_matcher_trail(db, ctx, settlement_id=rows[0]["settlement_id"])
    assert out["candidate_count"] == 0
    assert "has not arrived" in out["note"]


def test_tool_schemas_and_handlers_cannot_drift_apart(db):
    assert {s["function"]["name"] for s in tools.SCHEMAS} == set(tools.HANDLERS)
    for s in tools.SCHEMAS:
        assert s["function"]["description"], f"{s['function']['name']} has no description"


# ---------------------------------------------------------------- the loop ---
def test_the_agent_calls_a_tool_and_answers_from_the_result(db, demo_run, worst):
    client = StubClient([
        tool_call("get_settlement", {"settlement_id": worst}),
        {"content": f"The residue sits on {worst}."},
    ])
    out = investigate(db, demo_run["run_id"], demo_run["dataset_id"],
                      "What is wrong here?", client=client)
    assert out["tool_call_count"] == 1
    assert out["tool_calls"][0]["tool"] == "get_settlement"
    assert worst in out["citations"]
    assert out["grounded"] is True
    assert out["stop_reason"] == "answered"


def test_a_cited_record_the_agent_never_read_is_reported_as_unsupported(db, demo_run, worst):
    """The failure mode that matters: a confident sentence naming a record that
    appeared in no tool result. It must be surfaced, never quietly shipped."""
    client = StubClient([
        tool_call("get_settlement", {"settlement_id": worst}),
        {"content": f"{worst} is short because SET_9999 absorbed it, per EXC_99999."},
    ])
    out = investigate(db, demo_run["run_id"], demo_run["dataset_id"], "why?", client=client)
    assert out["grounded"] is False
    assert "SET_9999" in out["unsupported_references"]
    assert "EXC_99999" in out["unsupported_references"]
    assert worst in out["citations"]


def test_the_tool_budget_is_enforced(db, demo_run, worst):
    """A model that keeps calling tools has to be stopped, or one question can
    walk the whole dataset."""
    client = StubClient([tool_call("run_overview", {}, cid=f"c{i}") for i in range(40)])
    out = investigate(db, demo_run["run_id"], demo_run["dataset_id"], "loop", client=client)
    assert out["tool_call_count"] <= 20
    assert out["stop_reason"] in ("tool budget exhausted", "iteration limit reached")
    assert out["answer"], "the agent must still say something when it runs out of budget"


def test_malformed_tool_arguments_do_not_kill_the_conversation(db, demo_run):
    client = StubClient([
        {"content": "", "tool_calls": [{"id": "c1", "type": "function",
                                        "function": {"name": "get_settlement",
                                                     "arguments": "{not json"}}]},
        {"content": "I could not read that settlement."},
    ])
    out = investigate(db, demo_run["run_id"], demo_run["dataset_id"], "x", client=client)
    assert out["answer"]
    assert out["tool_call_count"] == 1


def test_the_answer_is_persisted_with_its_evidence(db, demo_run, worst):
    client = StubClient([tool_call("get_evidence", {"settlement_id": worst}),
                         {"content": f"See {worst}."}])
    out = investigate(db, demo_run["run_id"], demo_run["dataset_id"],
                      "explain the evidence", client=client)
    turn = store.record(db, demo_run["run_id"], "explain the evidence", out)
    rows = store.conversation(db, demo_run["run_id"])
    saved = next(r for r in rows if r["turn_id"] == turn)
    assert saved["question"] == "explain the evidence"
    assert saved["tool_call_count"] == out["tool_call_count"]
    assert saved["tool_calls"][0]["tool"] == "get_evidence"
    assert saved["grounded"] == out["grounded"]


# ------------------------------------------------------------ the storage ---
def test_a_missing_transcript_table_is_created_rather_than_raising(db):
    """The agent shipped after installs already existed. Those databases have no
    agent_transcripts table, and the fix must never be "reload schema.sql" --
    that drops every table and takes the dataset and run history with it. The
    table is applied from db/agent.sql, which is idempotent and safe on a live
    database."""
    db.execute("DROP TABLE IF EXISTS agent_transcripts CASCADE")
    db.commit()
    store._schema_state = None
    assert db.execute("SELECT to_regclass('agent_transcripts') t").fetchone()["t"] is None

    assert store.conversation(db, "00000000-0000-0000-0000-000000000000") == []
    assert db.execute("SELECT to_regclass('agent_transcripts') t").fetchone()["t"] is not None


def test_applying_the_agent_ddl_twice_is_harmless(db, demo_run):
    """Idempotent, so `make agent-schema` can be run without thinking about it
    and the API can apply it on demand."""
    store._schema_state = None
    assert store.ensure_schema(db) is True
    store._schema_state = None
    assert store.ensure_schema(db) is True
    ddl = store.DDL_PATH.read_text().upper()
    assert "CREATE TABLE IF NOT EXISTS" in ddl
    assert "DROP TABLE" not in ddl, "the live-database migration must never drop anything"


def test_storage_that_cannot_be_created_degrades_instead_of_failing(db, monkeypatch):
    """A conversation endpoint is hit every time a run is selected. If the agent's
    own storage is unavailable -- no CREATE right, say -- it must return an empty
    conversation, not take a reconciliation screen down with it."""
    store._schema_state = None
    monkeypatch.setattr(store, "ensure_schema", lambda conn: False)
    assert store.conversation(db, "00000000-0000-0000-0000-000000000000") == []
    assert store.history_for_prompt(db, "00000000-0000-0000-0000-000000000000") == []


def test_the_agent_table_is_absent_from_no_install_path(db):
    """Both routes to the table must define it: schema.sql for a fresh load,
    db/agent.sql for an existing one. If they drift, one kind of install breaks."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent
    schema = (root / "db" / "schema.sql").read_text()
    agent = (root / "db" / "agent.sql").read_text()
    assert "agent_transcripts" in schema
    assert "agent_transcripts" in agent
    for col in ("tool_calls", "citations", "unsupported_references", "grounded",
                "stop_reason", "elapsed_seconds", "turn_id"):
        assert col in schema and col in agent, f"{col} missing from one of the two definitions"


# -------------------------------------------------------------- the client ---
def test_a_missing_api_key_raises_rather_than_calling_anything(monkeypatch):
    """The reconciliation demo must stay fully usable with no key configured."""
    for k in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "XAI_API_KEY", "GROK_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("FINCTL_LLM_PROVIDER", "gemini")
    client = LLMClient()
    assert client.config.configured is False
    with pytest.raises(LLMUnavailable, match="No API key"):
        client.complete([{"role": "user", "content": "hi"}])


@pytest.mark.parametrize("env,provider", [
    ("GEMINI_API_KEY", "gemini"), ("GOOGLE_API_KEY", "gemini"),
    ("XAI_API_KEY", "grok"), ("GROK_API_KEY", "grok")])
def test_the_provider_is_chosen_from_whichever_key_is_present(monkeypatch, env, provider):
    for k in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "XAI_API_KEY", "GROK_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.delenv("FINCTL_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("FINCTL_LLM_MODEL", raising=False)
    monkeypatch.setenv(env, "test-key")
    from agent.llm import resolve_config
    c = resolve_config()
    assert c.provider == provider and c.configured


def test_model_and_endpoint_can_be_overridden(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "k")
    monkeypatch.setenv("FINCTL_LLM_PROVIDER", "grok")
    monkeypatch.setenv("FINCTL_LLM_MODEL", "grok-custom")
    monkeypatch.setenv("FINCTL_LLM_BASE_URL", "https://example.test/v1/")
    from agent.llm import resolve_config
    c = resolve_config()
    assert c.model == "grok-custom" and c.base_url == "https://example.test/v1"
