"""Tests for headjack.store — the index connection.

Always a real temporary SQLite file, never `:memory:` and never a mock: this is a
library wrapping a file format, and mocking it would hide exactly the bugs that
matter (trigger wiring, cascade behaviour, pragma persistence).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from headjack.store import SCHEMA_VERSION, SchemaError, connect, migrate


def _insert_artifact(conn: sqlite3.Connection, name: str = "a") -> int:
    cur = conn.execute(
        "INSERT INTO artifact (kind, name, content_hash, scope, body, updated_at) "
        "VALUES ('skill', ?, 'hash', 'personal', 'body text', '2026-01-01T00:00:00Z')",
        (name,),
    )
    conn.commit()
    assert cur.lastrowid is not None
    return cur.lastrowid


def _insert_chunk(conn: sqlite3.Connection, artifact_id: int, text: str = "hello world") -> int:
    cur = conn.execute(
        "INSERT INTO chunk (artifact_id, heading, text, symptoms) VALUES (?, 'H', ?, 'sym')",
        (artifact_id, text),
    )
    conn.commit()
    chunk_id = cur.lastrowid
    assert chunk_id is not None
    conn.execute(
        "INSERT INTO chunk_vec (chunk_id, embedding) VALUES (?, ?)",
        (chunk_id, b"\x00\x00\x80?" * 384),  # 384 float32(1.0) bytes, content irrelevant here
    )
    conn.commit()
    return chunk_id


def test_connect_on_missing_path_creates_valid_database(tmp_path: Path) -> None:
    db_path = tmp_path / "new.db"
    assert not db_path.exists()

    conn = connect(db_path)

    assert db_path.exists()
    tables = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'view')")
    }
    assert {"artifact", "chunk", "meta"} <= tables
    row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    assert row["value"] == str(SCHEMA_VERSION)


def test_connect_on_existing_database_does_not_destroy_data(tmp_path: Path) -> None:
    db_path = tmp_path / "existing.db"
    conn = connect(db_path)
    artifact_id = _insert_artifact(conn)
    conn.close()

    conn2 = connect(db_path)
    row = conn2.execute("SELECT name FROM artifact WHERE id = ?", (artifact_id,)).fetchone()
    assert row["name"] == "a"


def test_migrate_is_idempotent(tmp_path: Path) -> None:
    conn = connect(tmp_path / "idempotent.db")
    _insert_artifact(conn)
    migrate(conn)  # second call must not raise or wipe anything
    migrate(conn)
    count = conn.execute("SELECT COUNT(*) AS n FROM artifact").fetchone()["n"]
    assert count == 1


def test_foreign_keys_pragma_is_on(tmp_path: Path) -> None:
    conn = connect(tmp_path / "fk.db")
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_delete_artifact_cascades_chunks_fts_and_vectors(tmp_path: Path) -> None:
    conn = connect(tmp_path / "cascade.db")
    artifact_id = _insert_artifact(conn)
    chunk_id = _insert_chunk(conn, artifact_id, text="BackoffLimitExceeded on dbp42")

    fts_before = conn.execute(
        "SELECT COUNT(*) AS n FROM chunk_fts WHERE chunk_fts MATCH 'BackoffLimitExceeded'"
    ).fetchone()["n"]
    assert fts_before == 1
    vec_before = conn.execute(
        "SELECT COUNT(*) AS n FROM chunk_vec WHERE chunk_id = ?", (chunk_id,)
    ).fetchone()["n"]
    assert vec_before == 1

    conn.execute("DELETE FROM artifact WHERE id = ?", (artifact_id,))
    conn.commit()

    assert conn.execute("SELECT COUNT(*) AS n FROM chunk").fetchone()["n"] == 0
    fts_after = conn.execute(
        "SELECT COUNT(*) AS n FROM chunk_fts WHERE chunk_fts MATCH 'BackoffLimitExceeded'"
    ).fetchone()["n"]
    assert fts_after == 0
    vec_after = conn.execute(
        "SELECT COUNT(*) AS n FROM chunk_vec WHERE chunk_id = ?", (chunk_id,)
    ).fetchone()["n"]
    assert vec_after == 0


def test_direct_chunk_delete_also_cleans_fts_and_vec(tmp_path: Path) -> None:
    """Not just the cascade path — a bare DELETE FROM chunk must stay consistent too."""
    conn = connect(tmp_path / "direct-delete.db")
    artifact_id = _insert_artifact(conn)
    chunk_id = _insert_chunk(conn, artifact_id)

    conn.execute("DELETE FROM chunk WHERE id = ?", (chunk_id,))
    conn.commit()

    assert conn.execute("SELECT COUNT(*) AS n FROM chunk_fts").fetchone()["n"] == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM chunk_vec").fetchone()["n"] == 0


def test_old_database_migrates_forward_without_losing_data(tmp_path: Path) -> None:
    """A schema_version=1 database (missing every column added since) must gain
    them all on the next connect, in place, without touching existing rows."""
    db_path = tmp_path / "old.db"
    conn = connect(db_path)
    artifact_id = _insert_artifact(conn)
    conn.execute("UPDATE meta SET value = '1' WHERE key = 'schema_version'")
    conn.execute("ALTER TABLE artifact DROP COLUMN description")
    conn.execute("ALTER TABLE artifact DROP COLUMN previous_scope")
    conn.execute("ALTER TABLE artifact DROP COLUMN signature")
    conn.execute("DROP TABLE artifact_file")
    conn.commit()
    conn.close()

    migrated = connect(db_path)
    row = migrated.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    assert row["value"] == str(SCHEMA_VERSION)

    cols = {r["name"] for r in migrated.execute("PRAGMA table_info(artifact)")}
    assert "description" in cols
    assert "previous_scope" in cols
    assert "signature" in cols
    tables = {
        r["name"] for r in migrated.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert "artifact_file" in tables

    kept = migrated.execute("SELECT name FROM artifact WHERE id = ?", (artifact_id,)).fetchone()
    assert kept["name"] == "a"


def test_migrating_forward_backs_up_the_pre_migration_file(tmp_path: Path) -> None:
    """The user's local index is their only copy - a real pending migration must
    leave a restorable snapshot of what it started from, not just hope the ALTER
    goes well."""
    db_path = tmp_path / "old.db"
    conn = connect(db_path)
    _insert_artifact(conn)
    conn.execute("UPDATE meta SET value = '1' WHERE key = 'schema_version'")
    conn.execute("ALTER TABLE artifact DROP COLUMN description")
    conn.execute("ALTER TABLE artifact DROP COLUMN previous_scope")
    conn.execute("ALTER TABLE artifact DROP COLUMN signature")
    conn.execute("DROP TABLE artifact_file")
    conn.commit()
    conn.close()

    connect(db_path).close()

    backup_path = db_path.with_name(f"{db_path.name}.bak-v1")
    assert backup_path.exists()
    backup_conn = sqlite3.connect(backup_path)
    backup_conn.row_factory = sqlite3.Row
    row = backup_conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    assert row["value"] == "1"  # the backup is the pre-migration snapshot, not the result
    cols = {r["name"] for r in backup_conn.execute("PRAGMA table_info(artifact)")}
    assert "description" not in cols  # exactly the state being migrated away from
    backup_conn.close()


def test_no_pending_migration_takes_no_backup(tmp_path: Path) -> None:
    db_path = tmp_path / "current.db"
    connect(db_path).close()
    connect(db_path).close()  # second connect: nothing pending, must not back up

    assert not any(tmp_path.glob("*.bak-*"))


def test_read_only_connect_to_a_stale_database_raises_clearly(tmp_path: Path) -> None:
    """A read-only connection cannot write the migration itself - it must fail
    loudly naming the cause, not crash with a raw sqlite3.OperationalError or
    silently proceed against a database missing a column later code expects."""
    db_path = tmp_path / "stale.db"
    conn = connect(db_path)
    conn.execute("UPDATE meta SET value = '1' WHERE key = 'schema_version'")
    conn.commit()
    conn.close()

    with pytest.raises(SchemaError, match="read-only"):
        connect(db_path, read_only=True)


def test_read_only_connect_to_a_version_gap_with_no_sqlite_migration_works(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A version bump that's a no-op for SQLite (`_MIGRATIONS` has no entry)
    must not block read-only connections just because the version number moved -
    only a real pending `_MIGRATIONS` entry should do that. Simulated with a
    monkeypatched next version rather than today's real migration numbers."""
    import headjack.store as store_module

    db_path = tmp_path / "version-gap.db"
    connect(db_path).close()  # created at today's real SCHEMA_VERSION
    monkeypatch.setattr(store_module, "SCHEMA_VERSION", SCHEMA_VERSION + 1)

    read_only_conn = connect(db_path, read_only=True)
    row = read_only_conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    assert row["value"] == str(SCHEMA_VERSION)  # a read-only connection never writes the bump


def test_read_only_connect_to_an_up_to_date_database_works(tmp_path: Path) -> None:
    db_path = tmp_path / "current.db"
    connect(db_path).close()

    conn = connect(db_path, read_only=True)
    row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    assert row["value"] == str(SCHEMA_VERSION)


def test_newer_schema_version_raises(tmp_path: Path) -> None:
    db_path = tmp_path / "future.db"
    conn = connect(db_path)
    conn.execute("UPDATE meta SET value = '999' WHERE key = 'schema_version'")
    conn.commit()
    conn.close()

    with pytest.raises(SchemaError, match="999"):
        connect(db_path)


def test_missing_sqlite_vec_package_names_install_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import builtins

    real_import = builtins.__import__

    def fake_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "sqlite_vec":
            raise ImportError("no module named sqlite_vec")
        return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(builtins, "__import__", fake_import)

    with pytest.raises(SchemaError, match="uv sync --extra index"):
        connect(tmp_path / "no-vec.db")


def test_unique_artifact_identity_constraint(tmp_path: Path) -> None:
    conn = connect(tmp_path / "unique.db")
    conn.execute(
        "INSERT INTO artifact (kind, name, version, content_hash, scope, body, updated_at) "
        "VALUES ('skill', 'dup', '1.0', 'h1', 'personal', 'b', '2026-01-01T00:00:00Z')"
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO artifact (kind, name, version, content_hash, scope, body, updated_at) "
            "VALUES ('skill', 'dup', '1.0', 'h2', 'personal', 'b2', '2026-01-02T00:00:00Z')"
        )
