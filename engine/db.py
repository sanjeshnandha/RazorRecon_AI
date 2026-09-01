"""Postgres connection helper. One env var, no ORM, no magic."""
from __future__ import annotations

import os
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row

DEFAULT_DSN = "postgresql://finctl:finctl@localhost:5433/finctl"


def dsn() -> str:
    return os.environ.get("DATABASE_URL", DEFAULT_DSN)


def connect(autocommit: bool = False) -> psycopg.Connection:
    return psycopg.connect(dsn(), row_factory=dict_row, autocommit=autocommit)


@contextmanager
def tx():
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def fetch(conn, sql: str, params: tuple = ()) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def fetch_one(conn, sql: str, params: tuple = ()) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def copy_rows(conn, table: str, columns: list[str], rows: list[tuple]) -> None:
    """Bulk load via COPY. This is what keeps generation and persistence fast."""
    if not rows:
        return
    cols = ", ".join(columns)
    with conn.cursor() as cur:
        with cur.copy(f"COPY {table} ({cols}) FROM STDIN") as cp:
            for r in rows:
                cp.write_row(r)
