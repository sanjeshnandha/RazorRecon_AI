"""
Read-only table browser for the Data tab.

The registry is built from PostgreSQL's own catalog rather than a hand-written
list, so it cannot drift when a column or a table is added -- the schema is the
single source of truth for what exists and how to scope it.

Two rules, the same ones the agent's tools follow:

  * the table name is validated against the catalog-derived registry before it
    reaches any SQL string. A name that is not a real table in this schema can
    never be interpolated.
  * every query is scoped to the run (or its dataset) the caller opened, and
    every limit is clamped here rather than trusted from the query string.

Nothing in this module writes.
"""
from __future__ import annotations

from engine.db import fetch, fetch_one

MAX_LIMIT = 200
DEFAULT_LIMIT = 50

# Tables that hold engine OUTPUT rather than generated data. Worth separating in
# the UI: one group is the book, the other is what the engine concluded about it.
RESULT_TABLES = {"reconciliation_runs", "reconciliation_deltas", "attributions",
                 "match_candidates", "exceptions", "audit_log", "agent_transcripts"}

_registry: dict | None = None


def registry(conn) -> dict:
    """{table: {scope, key_columns, columns}} straight from the catalog."""
    global _registry
    if _registry is not None:
        return _registry

    cols = fetch(conn, """
        SELECT table_name, column_name, data_type, ordinal_position
        FROM information_schema.columns
        WHERE table_schema='public'
        ORDER BY table_name, ordinal_position""")
    keys = fetch(conn, """
        SELECT tc.table_name, kcu.column_name, kcu.ordinal_position
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
          ON kcu.constraint_name=tc.constraint_name AND kcu.table_schema=tc.table_schema
        WHERE tc.constraint_type='PRIMARY KEY' AND tc.table_schema='public'
        ORDER BY tc.table_name, kcu.ordinal_position""")

    by_table: dict[str, dict] = {}
    for c in cols:
        t = by_table.setdefault(c["table_name"], {"columns": [], "key_columns": []})
        t["columns"].append({"name": c["column_name"], "type": c["data_type"],
                             # every money column in this schema is BIGINT paise
                             "money": c["column_name"].endswith("_paise")})
    for k in keys:
        if k["table_name"] in by_table:
            by_table[k["table_name"]]["key_columns"].append(k["column_name"])

    out = {}
    for name, t in by_table.items():
        names = {c["name"] for c in t["columns"]}
        # dataset_id wins where both exist, so reconciliation_runs lists every run
        # of the current dataset rather than only the one being viewed.
        scope = "dataset_id" if "dataset_id" in names else (
                "run_id" if "run_id" in names else None)
        if scope is None:
            continue
        out[name] = {
            "scope": scope,
            "key_columns": t["key_columns"] or [scope],
            "columns": t["columns"],
            "group": "results" if name in RESULT_TABLES else "data",
        }
    _registry = out
    return _registry


def _scope_value(table_meta: dict, run_id: str, dataset_id: str) -> str:
    return dataset_id if table_meta["scope"] == "dataset_id" else run_id


def summary(conn, run_id: str, dataset_id: str) -> list[dict]:
    """Every table with its live row count for this run. This is what makes the
    tab answer 'did my last Simulate actually land'."""
    reg = registry(conn)
    rows = []
    for name in sorted(reg):
        meta = reg[name]
        n = fetch_one(conn, f"SELECT count(*) AS c FROM {name} WHERE {meta['scope']} = %s",
                      (_scope_value(meta, run_id, dataset_id),))["c"]
        rows.append({"table": name, "rows": int(n), "scope": meta["scope"],
                     "group": meta["group"], "columns": len(meta["columns"])})
    return rows


def page(conn, run_id: str, dataset_id: str, table: str,
         limit: int = DEFAULT_LIMIT, offset: int = 0) -> dict:
    """One page of real rows. `table` is checked against the registry before it
    is ever put into a SQL string -- that check is the whole defence here."""
    reg = registry(conn)
    meta = reg.get(table)
    if meta is None:
        return {"error": f"unknown table {table!r}", "available": sorted(reg)}

    limit = max(1, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT))
    offset = max(0, int(offset or 0))
    value = _scope_value(meta, run_id, dataset_id)
    # newest first for the append-ordered tables, so a tick's rows are on page 1
    order = ", ".join(f"{c} DESC" for c in meta["key_columns"])

    total = fetch_one(conn, f"SELECT count(*) AS c FROM {table} WHERE {meta['scope']} = %s",
                      (value,))["c"]
    rows = fetch(conn, f"SELECT * FROM {table} WHERE {meta['scope']} = %s "
                       f"ORDER BY {order} LIMIT %s OFFSET %s", (value, limit, offset))
    # the scope column is the same on every row; it is noise in a table view
    visible = [c for c in meta["columns"] if c["name"] not in ("dataset_id", "run_id")]
    keep = {c["name"] for c in visible}
    return {
        "table": table, "scope": meta["scope"], "group": meta["group"],
        "total_rows": int(total), "limit": limit, "offset": offset,
        "columns": visible,
        "rows": [{k: v for k, v in r.items() if k in keep} for r in rows],
    }
