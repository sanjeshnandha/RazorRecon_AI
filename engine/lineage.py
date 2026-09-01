"""
Trace Money. One recursive CTE over money_edges, rendered as an expandable
timeline -- never as a node-link diagram, which looks impressive and tells a
finance person nothing.
"""
from __future__ import annotations

from engine.db import fetch

TRACE_SQL = """
WITH RECURSIVE lineage AS (
    SELECT src_type, src_id, dst_type, dst_id, edge_kind, amount_paise, 1 AS depth,
           ARRAY[src_type || ':' || src_id] AS path
    FROM money_edges
    WHERE dataset_id = %(ds)s AND src_type = %(t)s AND src_id = %(i)s
  UNION ALL
    SELECT e.src_type, e.src_id, e.dst_type, e.dst_id, e.edge_kind, e.amount_paise, l.depth + 1,
           l.path || (e.src_type || ':' || e.src_id)
    FROM money_edges e
    JOIN lineage l ON e.dataset_id = %(ds)s
                  AND e.src_type = l.dst_type AND e.src_id = l.dst_id
    WHERE l.depth < 8
      AND NOT (e.src_type || ':' || e.src_id) = ANY(l.path)
)
SELECT DISTINCT src_type, src_id, dst_type, dst_id, edge_kind, amount_paise, depth
FROM lineage ORDER BY depth, dst_type, dst_id
"""

UPSTREAM_SQL = """
WITH RECURSIVE up AS (
    SELECT src_type, src_id, dst_type, dst_id, edge_kind, amount_paise, 1 AS depth
    FROM money_edges
    WHERE dataset_id = %(ds)s AND dst_type = %(t)s AND dst_id = %(i)s
  UNION ALL
    SELECT e.src_type, e.src_id, e.dst_type, e.dst_id, e.edge_kind, e.amount_paise, u.depth + 1
    FROM money_edges e
    JOIN up u ON e.dataset_id = %(ds)s AND e.dst_type = u.src_type AND e.dst_id = u.src_id
    WHERE u.depth < 6
)
SELECT DISTINCT src_type, src_id, dst_type, dst_id, edge_kind, amount_paise, depth
FROM up ORDER BY depth
"""


def trace(conn, dataset_id: str, node_type: str, node_id: str) -> dict:
    down = fetch(conn, TRACE_SQL, {"ds": dataset_id, "t": node_type, "i": node_id})
    up = fetch(conn, UPSTREAM_SQL, {"ds": dataset_id, "t": node_type, "i": node_id})
    return {"root": {"type": node_type, "id": node_id},
            "downstream": down, "upstream": list(reversed(up))}
