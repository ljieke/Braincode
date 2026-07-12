from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from braincode.jobs import (
    CURRENT_SCHEMA_VERSION,
    JobKind,
    JobSpec,
    SQLiteJobStore,
    SchemaError,
    UnsupportedSchemaVersionError,
)


def test_fresh_database_uses_wal_and_current_schema(tmp_path: Path) -> None:
    path = tmp_path / "runtime.db"
    store = SQLiteJobStore(path)
    assert store.schema_version() == CURRENT_SCHEMA_VERSION

    with sqlite3.connect(path) as connection:
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert journal_mode.lower() == "wal"
    assert {"schema_version", "jobs", "job_dependencies", "job_events"} <= tables
    with sqlite3.connect(path) as connection:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(jobs)")
        }
    assert "progress_json" in columns


def test_version_zero_database_migrates_to_current_version(tmp_path: Path) -> None:
    path = tmp_path / "runtime.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE schema_version(version INTEGER NOT NULL)")
        connection.execute("INSERT INTO schema_version(version) VALUES (0)")

    store = SQLiteJobStore(path)
    assert store.schema_version() == CURRENT_SCHEMA_VERSION
    assert store.create(JobSpec(kind=JobKind.AGENT, name="after migration"))


def test_version_one_database_migrates_without_losing_jobs(tmp_path: Path) -> None:
    path = tmp_path / "runtime.db"
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        SQLiteJobStore._migrate_0_to_1(connection)
        connection.execute(
            """
            INSERT INTO jobs(
                id, kind, name, description, status, payload_json,
                result_text, error_text, owner, team_name, worktree_path,
                created_at, updated_at, started_at, finished_at, lease_until,
                attempts, max_attempts, priority, parent_job_id, schedule_id
            ) VALUES ('legacy', 'agent', 'legacy job', '', 'pending', '{}',
                      '', '', NULL, '', '', 1, 1, NULL, NULL, NULL,
                      0, 3, 0, NULL, NULL)
            """
        )

    store = SQLiteJobStore(path)
    assert store.schema_version() == CURRENT_SCHEMA_VERSION
    job = store.get("legacy")
    assert job is not None
    assert job.progress_json == "{}"


def test_future_schema_version_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "runtime.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE schema_version(version INTEGER NOT NULL)")
        connection.execute(
            "INSERT INTO schema_version(version) VALUES (?)",
            (CURRENT_SCHEMA_VERSION + 1,),
        )

    with pytest.raises(UnsupportedSchemaVersionError, match="newer"):
        SQLiteJobStore(path)


def test_corrupt_schema_version_table_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "runtime.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE schema_version(version INTEGER NOT NULL)")
        connection.executemany(
            "INSERT INTO schema_version(version) VALUES (?)", [(0,), (0,)]
        )

    with pytest.raises(SchemaError, match="exactly one row"):
        SQLiteJobStore(path)
