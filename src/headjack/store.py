"""Headjack's local index: one SQLite file, opened here and nowhere else.

Every other module in `headjack` goes through `connect()`. That is what keeps the
FTS5/sqlite-vec setup, the foreign-key pragma and the schema check in exactly one
place. This package is SQLite-only — remote catalog traffic goes over HTTPS to the
public Headjack API, not through this module.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import cast

from headjack.db.types import IndexConnection

SCHEMA_VERSION = 6

_SCHEMA_PATH = Path(__file__).parent / "schema.sql"

# Upward migrations for a database already at an older schema_version - schema.sql
# itself is only ever applied once, to a brand-new file (see migrate() below), so an
# existing database needs its own ALTER statement per version bump. Keyed by the
# version the statement moves *to*.
_MIGRATIONS: dict[int, str] = {
    2: "ALTER TABLE artifact ADD COLUMN description TEXT",
    # Promotion records the scope an artifact held before it moved into a
    # reviewed shared scope, so a later issue can reverse it. NULL means "never
    # promoted" - the common case for every row that predates this column.
    3: "ALTER TABLE artifact ADD COLUMN previous_scope TEXT",
    # ed25519:<keyid>:<base64sig> over body, NULL for anything published
    # before a signing key was configured or this column existed.
    5: "ALTER TABLE artifact ADD COLUMN signature TEXT",
    # What gets shipped for a skill, as opposed to `artifact.body`
    # (what gets indexed - SKILL.md only). A row-less artifact here just means it
    # predates this column; it is not re-populated retroactively.
    6: "CREATE TABLE artifact_file ("
    "artifact_id INTEGER NOT NULL REFERENCES artifact(id) ON DELETE CASCADE, "
    "path TEXT NOT NULL, content BLOB NOT NULL, PRIMARY KEY (artifact_id, path))",
}


class SchemaError(RuntimeError):
    """Raised when the opened database is missing a required SQLite feature,
    or was built with an incompatible schema version."""


def connect(path: Path | str, *, read_only: bool = False) -> IndexConnection:
    """Open the index at `path`, loading sqlite-vec and applying the schema if the
    file is new or empty. Raises `SchemaError` if the SQLite build lacks FTS5 or
    sqlite-vec, or if the file was built with a newer schema than this code knows.
    """
    uri = f"file:{Path(path)}?mode=ro" if read_only else str(path)
    conn = sqlite3.connect(uri, uri=read_only)
    conn.row_factory = sqlite3.Row

    _load_vec_extension(conn)
    _require_fts5(conn)

    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")

    migrate(conn, read_only=read_only, path=Path(path))
    return cast(IndexConnection, conn)


def _backup_before_migrating(conn: sqlite3.Connection, path: Path, from_version: int) -> None:
    """A consistent snapshot of `path` taken via SQLite's own backup API - not a
    raw file copy, which could land mid-write against a WAL-mode database and
    produce a torn, unopenable backup. This is the user's only copy of their local
    index; a schema change that goes wrong should be a `cp` away from undone, not
    a lost afternoon of `headjack index`."""
    backup_path = path.with_name(f"{path.name}.bak-v{from_version}")
    backup_conn = sqlite3.connect(backup_path)
    try:
        conn.backup(backup_conn)
    finally:
        backup_conn.close()


def migrate(conn: sqlite3.Connection, *, read_only: bool = False, path: Path | None = None) -> None:
    """Apply schema.sql to a brand-new file, or step an existing one forward through
    `_MIGRATIONS`; raise on a version newer than this code knows, or on a read-only
    connection to a database that needs migrating (it cannot write the result, and
    proceeding silently would let a later query fail confusingly on a column that
    was never added).

    `schema.sql` itself is only ever executed once, against an empty file — it is
    not safe to re-run against an already-migrated database (its `CREATE TABLE`
    statements aren't all `IF NOT EXISTS`). Each schema change after the first needs
    its own entry in `_MIGRATIONS`, applied in order and tracked via
    `meta.schema_version`, the same way any other project's migrations work.

    `path`, when given, names a real file to back up (`<path>.bak-v<N>`) before any
    pending `_MIGRATIONS` statement runs - skipped when there is nothing to apply,
    so a database already current or a version gap with no SQLite-side change (see
    the comment below) never gets a needless backup.
    """
    existing = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='meta'"
    ).fetchone()

    if existing is None:
        if read_only:
            raise SchemaError("no index exists yet at this path to open read-only")
        conn.executescript(_SCHEMA_PATH.read_text())
        conn.execute(
            "INSERT INTO meta(key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        conn.commit()
        return

    row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    stored_version = int(row["value"]) if row else 0
    if stored_version > SCHEMA_VERSION:
        raise SchemaError(
            f"database schema_version={stored_version} is newer than this code "
            f"supports (SCHEMA_VERSION={SCHEMA_VERSION}); upgrade headjack-cli"
        )

    if stored_version < SCHEMA_VERSION:
        # Not every version bump touches SQLite - some gaps have no entry in
        # `_MIGRATIONS` on purpose. A read-only connection should only be refused
        # when there's a real statement pending that it can't write; a version gap
        # with no matching entry needs nothing but the counter moving forward.
        pending = [v for v in range(stored_version + 1, SCHEMA_VERSION + 1) if v in _MIGRATIONS]
        if pending and read_only:
            raise SchemaError(
                f"database schema_version={stored_version} needs migrating to "
                f"{SCHEMA_VERSION}, but this connection is read-only; connect "
                "read-write once first (e.g. run `headjack index`) so the "
                "migration can be written"
            )
        if read_only:
            return
        if pending and path is not None:
            _backup_before_migrating(conn, path, stored_version)
        for version in pending:
            conn.execute(_MIGRATIONS[version])
        conn.execute(
            "UPDATE meta SET value = ? WHERE key = 'schema_version'", (str(SCHEMA_VERSION),)
        )
        conn.commit()


def _load_vec_extension(conn: sqlite3.Connection) -> None:
    try:
        import sqlite_vec
    except ImportError as exc:
        raise SchemaError(
            "the sqlite-vec package is not installed — run `uv sync --extra index`"
        ) from exc

    try:
        conn.enable_load_extension(True)
    except AttributeError as exc:
        raise SchemaError(
            "this Python's sqlite3 module was built without extension-loading "
            "support (common for uv-managed Python downloads: `uv python install` "
            "ships this disabled deliberately). Point uv at a system or Homebrew "
            "Python instead — see docs/install.md."
        ) from exc
    try:
        sqlite_vec.load(conn)
    finally:
        conn.enable_load_extension(False)

    try:
        conn.execute("SELECT vec_version()").fetchone()
    except sqlite3.OperationalError as exc:
        raise SchemaError(f"sqlite-vec loaded but is not functioning: {exc}") from exc


def _require_fts5(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        "SELECT 1 FROM pragma_compile_options WHERE compile_options = 'ENABLE_FTS5'"
    ).fetchone()
    if row is None:
        raise SchemaError(
            "this SQLite build lacks FTS5 (ENABLE_FTS5 compile option not set); "
            "Headjack's sparse retrieval depends on it"
        )
