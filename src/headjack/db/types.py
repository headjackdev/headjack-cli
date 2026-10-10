"""Shared types for index storage — SQLite (dev) and Postgres (staging)."""

from __future__ import annotations

import sqlite3
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class IndexCursor(Protocol):
    rowcount: int
    lastrowid: int | None

    def fetchone(self) -> Any: ...
    def fetchall(self) -> list[Any]: ...


@runtime_checkable
class IndexConnection(Protocol):
    """The subset of sqlite3.Connection the Headjack index code relies on."""

    def execute(self, query: str, params: Any = ()) -> IndexCursor: ...
    def commit(self) -> None: ...
    def close(self) -> None: ...
    def __enter__(self) -> Any: ...
    def __exit__(self, *exc: Any) -> Any: ...


def is_postgres(conn: IndexConnection) -> bool:
    """True when `conn` is the Postgres adapter, not a raw sqlite3 connection."""
    return getattr(conn, "dialect", "sqlite") == "postgres"


def as_sqlite(conn: IndexConnection) -> sqlite3.Connection:
    """Narrow to sqlite3 for code paths that are SQLite-only (local CLI index)."""
    if not isinstance(conn, sqlite3.Connection):
        raise TypeError("this operation requires a SQLite index file")
    return conn
