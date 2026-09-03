"""Transcript persistence. Every answer the agent gives is recorded next to the
run it describes, with the tools it called and whether its citations held up --
so an answer can be audited later exactly like an engine decision can.

The agent is an ADDITIVE feature on a system that already works. An install
predating it has no agent_transcripts table, and the fix for that must never be
"reload schema.sql" -- that drops every table and takes the dataset and the run
history with it. So the table is applied from db/agent.sql, which is idempotent,
and this module applies it on demand. If it cannot (no CREATE right, say), the
conversation degrades to empty rather than failing the request: nothing about
the agent should be able to break a reconciliation screen."""
from __future__ import annotations

import json
import pathlib

from engine.db import fetch, fetch_one

DDL_PATH = pathlib.Path(__file__).resolve().parent.parent / "db" / "agent.sql"

# Applying idempotent DDL costs a round trip, so the outcome is cached per
# process. Failure is cached too -- retrying a CREATE that is never going to
# succeed on every request would turn one misconfiguration into a slow API.
_schema_state: bool | None = None


def ensure_schema(conn) -> bool:
    """Make sure agent_transcripts exists. Idempotent and safe on a live
    database: db/agent.sql only ever CREATEs IF NOT EXISTS."""
    global _schema_state
    if _schema_state is not None:
        return _schema_state
    try:
        with conn.cursor() as cur:
            cur.execute(DDL_PATH.read_text())
        conn.commit()
        _schema_state = True
    except Exception:                              # noqa: BLE001 - degraded, not fatal
        conn.rollback()
        _schema_state = False
    return _schema_state


def table_ready(conn) -> bool:
    return ensure_schema(conn)


def record(conn, run_id: str, question: str, result: dict) -> int:
    ensure_schema(conn)
    turn = (fetch_one(conn, "SELECT COALESCE(MAX(turn_id), 0) + 1 AS n "
                            "FROM agent_transcripts WHERE run_id=%s", (run_id,)) or {}).get("n", 1)
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO agent_transcripts
              (run_id, turn_id, question, answer, provider, model, tool_calls,
               tool_call_count, citations, unsupported_references, grounded,
               stop_reason, elapsed_seconds)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (run_id, turn, question, result.get("answer", ""), result.get("provider"),
             result.get("model"), json.dumps(result.get("tool_calls", [])),
             result.get("tool_call_count", 0), result.get("citations", []),
             result.get("unsupported_references", []), bool(result.get("grounded", True)),
             result.get("stop_reason"), result.get("elapsed_seconds")))
    conn.commit()
    return int(turn)


def conversation(conn, run_id: str, limit: int = 50) -> list[dict]:
    if not ensure_schema(conn):
        return []
    return fetch(conn, """SELECT turn_id, asked_at, question, answer, provider, model,
                                 tool_calls, tool_call_count, citations,
                                 unsupported_references, grounded, stop_reason,
                                 elapsed_seconds
                          FROM agent_transcripts WHERE run_id=%s
                          ORDER BY turn_id LIMIT %s""", (run_id, max(1, min(limit, 200))))


def history_for_prompt(conn, run_id: str, turns: int = 3) -> list[dict]:
    """Recent exchanges, oldest first, as chat messages."""
    if not ensure_schema(conn):
        return []
    rows = fetch(conn, "SELECT question, answer FROM agent_transcripts WHERE run_id=%s "
                       "ORDER BY turn_id DESC LIMIT %s", (run_id, max(0, turns)))
    out: list[dict] = []
    for r in reversed(rows):
        out.append({"role": "user", "content": r["question"]})
        out.append({"role": "assistant", "content": r["answer"]})
    return out
