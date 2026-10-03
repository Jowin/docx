"""Postgres for the runtime's state: jobs, deliveries, review, audit chain, spool, checkpoints.

Everything lives in one Postgres schema (``DB_SCHEMA``, default ``extractor``),
so the runtime can share a database with design-time's registry without its
tables mixing in. Connections come from a pool shared by the API, its worker
threads and the LangGraph checkpointer; every connection is in autocommit
mode with its ``search_path`` set to the schema, and multi-statement changes
use explicit transactions.

    DATABASE_URL   postgresql://user:password@host:5432/db   (a SQLAlchemy-style
                   postgresql+psycopg2:// URL is accepted too)
    DB_SCHEMA      extractor
"""
from __future__ import annotations

import re
import threading
from typing import Any

_SCHEMA = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
_POOLS: list[Any] = []
_LOCK = threading.Lock()


def conninfo(url: str) -> str:
    """postgresql+psycopg2://... -> postgresql://... (what psycopg 3 takes)."""
    scheme, sep, rest = url.partition("://")
    if not sep or not scheme.startswith("postgres"):
        raise ValueError("DATABASE_URL must be a postgresql:// URL")
    return f"postgresql://{rest}"


def check_schema(schema: str) -> str:
    if not _SCHEMA.match(schema):
        raise ValueError(f"DB_SCHEMA {schema!r}: lower-case letters, digits and _ only")
    return schema


def open_pool(url: str, schema: str, *, max_size: int = 10):
    """A connection pool whose connections work inside ``schema`` (created if missing)."""
    import psycopg
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    info = conninfo(url)
    check_schema(schema)
    with psycopg.connect(info, autocommit=True) as conn:
        conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
    pool = ConnectionPool(info, min_size=1, max_size=max_size, open=True, name=f"extractor:{schema}",
                          kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row,
                                  "options": f"-c search_path={schema}"})
    pool.wait(timeout=30)
    with _LOCK:
        _POOLS.append(pool)
    return pool


def close_all() -> None:
    """Close every pool this process opened (shutdown, and between tests)."""
    with _LOCK:
        pools, _POOLS[:] = list(_POOLS), []
    for p in pools:
        try:
            p.close()
        except Exception:                                      # noqa: BLE001 - closing is best-effort
            pass
