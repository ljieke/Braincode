from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Callable, Iterator, Protocol

from braincode.jobs.models import (
    TERMINAL_JOB_STATUSES,
    Job,
    JobEvent,
    JobKind,
    JobQuery,
    JobSpec,
    JobStatus,
)


CURRENT_SCHEMA_VERSION = 2
DEFAULT_BUSY_TIMEOUT_MS = 5_000


class JobStoreError(Exception):
    pass


class JobNotFoundError(JobStoreError):
    pass


class JobStateError(JobStoreError):
    pass


class JobOwnershipError(JobStoreError):
    pass


class JobDependencyError(JobStoreError):
    pass


class JobCycleError(JobDependencyError):
    pass


class SchemaError(JobStoreError):
    pass


class UnsupportedSchemaVersionError(SchemaError):
    pass


class JobStore(Protocol):
    def create(self, spec: JobSpec) -> Job: ...

    def get(self, job_id: str) -> Job | None: ...

    def list(self, query: JobQuery | None = None) -> list[Job]: ...

    def set_dependencies(
        self, job_id: str, dependencies: tuple[str, ...] | list[str]
    ) -> Job: ...

    def claim(self, job_id: str, owner: str, lease_seconds: int) -> Job | None: ...

    def heartbeat(self, job_id: str, owner: str, lease_seconds: int) -> bool: ...

    def complete(self, job_id: str, owner: str, result: str) -> Job: ...

    def fail(self, job_id: str, owner: str, error: str) -> Job: ...

    def cancel(self, job_id: str) -> Job: ...

    def recover_expired(self, now: datetime | None = None) -> list[Job]: ...

    def update_progress(self, job_id: str, progress_json: str) -> Job: ...

    def append_event(
        self, job_id: str, event_type: str, payload_json: str = "{}"
    ) -> JobEvent: ...

    def consume_events(
        self,
        event_types: tuple[str, ...] = (),
        job_kinds: tuple[JobKind, ...] = (),
        limit: int = 100,
    ) -> list[JobEvent]: ...

    def mark_failed(self, job_id: str, error: str) -> Job: ...


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _require_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime values must be timezone-aware")
    return value.astimezone(UTC)


def _to_epoch_us(value: datetime) -> int:
    return int(_require_aware_utc(value).timestamp() * 1_000_000)


def _from_epoch_us(value: int | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value / 1_000_000, UTC)


class SQLiteJobStore:
    """File-backed SQLite implementation of the durable Job data layer.

    Every public operation uses its own connection. This keeps the store safe to
    call from multiple threads or processes while SQLite serializes writers.
    Runtime scheduling and asyncio task ownership deliberately live outside this
    Phase 1 data layer.
    """

    def __init__(
        self,
        database: str | Path,
        *,
        clock: Callable[[], datetime] | None = None,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
    ) -> None:
        database_text = str(database)
        if database_text == ":memory:":
            raise ValueError("SQLiteJobStore requires a file-backed database")
        if busy_timeout_ms <= 0:
            raise ValueError("busy_timeout_ms must be positive")

        self.database = Path(database)
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock or _utc_now
        self._busy_timeout_ms = busy_timeout_ms
        self._initialize()

    def _now(self) -> datetime:
        return _require_aware_utc(self._clock())

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database,
            timeout=self._busy_timeout_ms / 1_000,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms}")
        return connection

    @contextmanager
    def _transaction(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        connection = self._connect()
        try:
            journal_mode = connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
            if str(journal_mode).lower() != "wal":
                raise SchemaError(
                    f"Job database must support WAL mode, got {journal_mode!r}"
                )
            connection.execute("PRAGMA synchronous = NORMAL")
            connection.execute("BEGIN IMMEDIATE")
            version = self._read_schema_version(connection)
            if version > CURRENT_SCHEMA_VERSION:
                raise UnsupportedSchemaVersionError(
                    f"Job database schema version {version} is newer than "
                    f"supported version {CURRENT_SCHEMA_VERSION}"
                )
            while version < CURRENT_SCHEMA_VERSION:
                migrator = getattr(self, f"_migrate_{version}_to_{version + 1}", None)
                if migrator is None:
                    raise UnsupportedSchemaVersionError(
                        f"No migration path from schema version {version}"
                    )
                migrator(connection)
                version += 1
            connection.commit()
        except JobStoreError:
            connection.rollback()
            raise
        except sqlite3.DatabaseError as exc:
            connection.rollback()
            raise SchemaError(f"Failed to initialize job database schema: {exc}") from exc
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _read_schema_version(connection: sqlite3.Connection) -> int:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_version'"
        ).fetchone()
        if exists is None:
            return 0

        try:
            rows = connection.execute("SELECT version FROM schema_version").fetchall()
        except sqlite3.DatabaseError as exc:
            raise SchemaError(f"Invalid schema_version table: {exc}") from exc
        if len(rows) != 1:
            raise SchemaError("schema_version must contain exactly one row")
        version = rows[0]["version"]
        if not isinstance(version, int) or version < 0:
            raise SchemaError(f"Invalid schema version: {version!r}")
        return version

    @staticmethod
    def _migrate_0_to_1(connection: sqlite3.Connection) -> None:
        statements = (
            """
            CREATE TABLE IF NOT EXISTS schema_version (
                version INTEGER NOT NULL
            )
            """,
            """
            CREATE TABLE jobs (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL CHECK (kind IN ('agent', 'tool', 'prompt', 'team')),
                name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL CHECK (
                    status IN ('pending', 'running', 'completed', 'failed', 'cancelled', 'blocked')
                ),
                payload_json TEXT NOT NULL DEFAULT '{}',
                result_text TEXT NOT NULL DEFAULT '',
                error_text TEXT NOT NULL DEFAULT '',
                owner TEXT,
                team_name TEXT NOT NULL DEFAULT '',
                worktree_path TEXT NOT NULL DEFAULT '',
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                started_at INTEGER,
                finished_at INTEGER,
                lease_until INTEGER,
                attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
                max_attempts INTEGER NOT NULL CHECK (max_attempts > 0),
                priority INTEGER NOT NULL DEFAULT 0,
                parent_job_id TEXT REFERENCES jobs(id) ON DELETE SET NULL,
                schedule_id TEXT
            )
            """,
            """
            CREATE TABLE job_dependencies (
                job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                depends_on_job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE RESTRICT,
                PRIMARY KEY (job_id, depends_on_job_id),
                CHECK (job_id <> depends_on_job_id)
            )
            """,
            """
            CREATE INDEX idx_jobs_status_priority
                ON jobs(status, priority DESC, created_at ASC)
            """,
            """
            CREATE INDEX idx_jobs_owner_lease
                ON jobs(owner, lease_until)
            """,
            """
            CREATE INDEX idx_job_dependencies_reverse
                ON job_dependencies(depends_on_job_id, job_id)
            """,
        )
        for statement in statements:
            connection.execute(statement)
        connection.execute("DELETE FROM schema_version")
        connection.execute(
            "INSERT INTO schema_version(version) VALUES (?)",
            (1,),
        )
        connection.execute("PRAGMA user_version = 1")

    @staticmethod
    def _migrate_1_to_2(connection: sqlite3.Connection) -> None:
        connection.execute(
            "ALTER TABLE jobs ADD COLUMN progress_json TEXT NOT NULL DEFAULT '{}'"
        )
        connection.execute(
            """
            CREATE TABLE job_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}',
                created_at INTEGER NOT NULL,
                consumed_at INTEGER
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX idx_job_events_unconsumed
                ON job_events(consumed_at, id)
            """
        )
        connection.execute("UPDATE schema_version SET version = 2")
        connection.execute("PRAGMA user_version = 2")

    def schema_version(self) -> int:
        connection = self._connect()
        try:
            return self._read_schema_version(connection)
        finally:
            connection.close()

    @staticmethod
    def _normalize_dependencies(
        job_id: str, dependencies: tuple[str, ...] | list[str]
    ) -> tuple[str, ...]:
        normalized = tuple(str(value).strip() for value in dependencies)
        if any(not value for value in normalized):
            raise JobDependencyError("Dependency IDs must not be empty")
        if len(set(normalized)) != len(normalized):
            raise JobDependencyError("Duplicate dependencies are not allowed")
        if job_id in normalized:
            raise JobDependencyError("A job cannot depend on itself")
        return normalized

    @staticmethod
    def _validate_payload(payload_json: str) -> None:
        try:
            json.loads(payload_json)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("payload_json must contain valid JSON") from exc

    @staticmethod
    def _require_job_row(
        connection: sqlite3.Connection, job_id: str
    ) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise JobNotFoundError(f"Job '{job_id}' not found")
        return row

    @staticmethod
    def _validate_dependency_rows(
        connection: sqlite3.Connection, dependencies: tuple[str, ...]
    ) -> None:
        for dependency_id in dependencies:
            found = connection.execute(
                "SELECT 1 FROM jobs WHERE id = ?", (dependency_id,)
            ).fetchone()
            if found is None:
                raise JobDependencyError(
                    f"Dependency job '{dependency_id}' does not exist"
                )

    @staticmethod
    def _dependencies_satisfied(connection: sqlite3.Connection, job_id: str) -> bool:
        incomplete = connection.execute(
            """
            SELECT 1
            FROM job_dependencies AS dependency
            JOIN jobs AS required ON required.id = dependency.depends_on_job_id
            WHERE dependency.job_id = ? AND required.status <> ?
            LIMIT 1
            """,
            (job_id, JobStatus.COMPLETED.value),
        ).fetchone()
        return incomplete is None

    @staticmethod
    def _would_create_cycle(
        connection: sqlite3.Connection, job_id: str, dependency_id: str
    ) -> bool:
        row = connection.execute(
            """
            WITH RECURSIVE reachable(id) AS (
                SELECT depends_on_job_id
                FROM job_dependencies
                WHERE job_id = ?
                UNION
                SELECT dependency.depends_on_job_id
                FROM job_dependencies AS dependency
                JOIN reachable ON dependency.job_id = reachable.id
            )
            SELECT 1 FROM reachable WHERE id = ? LIMIT 1
            """,
            (dependency_id, job_id),
        ).fetchone()
        return row is not None

    def create(self, spec: JobSpec) -> Job:
        job_id = spec.id or uuid.uuid4().hex
        name = spec.name.strip()
        if not name:
            raise ValueError("Job name must not be empty")
        if spec.max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        self._validate_payload(spec.payload_json)
        dependencies = self._normalize_dependencies(job_id, spec.dependencies)
        kind = JobKind(spec.kind)
        now = _to_epoch_us(self._now())

        with self._transaction(immediate=True) as connection:
            existing = connection.execute(
                "SELECT 1 FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if existing is not None:
                raise JobStateError(f"Job '{job_id}' already exists")
            self._validate_dependency_rows(connection, dependencies)
            if spec.parent_job_id is not None:
                parent = connection.execute(
                    "SELECT 1 FROM jobs WHERE id = ?", (spec.parent_job_id,)
                ).fetchone()
                if parent is None:
                    raise JobDependencyError(
                        f"Parent job '{spec.parent_job_id}' does not exist"
                    )

            status = JobStatus.PENDING
            if dependencies:
                completed = connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM jobs
                    WHERE id IN ({}) AND status = ?
                    """.format(",".join("?" for _ in dependencies)),
                    (*dependencies, JobStatus.COMPLETED.value),
                ).fetchone()[0]
                if completed != len(dependencies):
                    status = JobStatus.BLOCKED

            connection.execute(
                """
                INSERT INTO jobs(
                    id, kind, name, description, status, payload_json,
                    result_text, error_text, owner, team_name, worktree_path,
                    created_at, updated_at, started_at, finished_at, lease_until,
                    attempts, max_attempts, priority, parent_job_id, schedule_id
                ) VALUES (?, ?, ?, ?, ?, ?, '', '', NULL, ?, ?, ?, ?, NULL, NULL, NULL,
                          0, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    kind.value,
                    name,
                    spec.description,
                    status.value,
                    spec.payload_json,
                    spec.team_name,
                    spec.worktree_path,
                    now,
                    now,
                    spec.max_attempts,
                    spec.priority,
                    spec.parent_job_id,
                    spec.schedule_id,
                ),
            )
            connection.executemany(
                "INSERT INTO job_dependencies(job_id, depends_on_job_id) VALUES (?, ?)",
                ((job_id, dependency_id) for dependency_id in dependencies),
            )
            self._insert_event(connection, job_id, "created", "{}", now)
            return self._row_to_job(connection, self._require_job_row(connection, job_id))

    def get(self, job_id: str) -> Job | None:
        connection = self._connect()
        try:
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            return self._row_to_job(connection, row) if row is not None else None
        finally:
            connection.close()

    def list(self, query: JobQuery | None = None) -> list[Job]:
        query = query or JobQuery()
        if query.limit <= 0:
            raise ValueError("query.limit must be positive")
        if query.offset < 0:
            raise ValueError("query.offset must not be negative")

        clauses: list[str] = []
        params: list[object] = []
        if query.statuses:
            values = [JobStatus(status).value for status in query.statuses]
            clauses.append(f"status IN ({','.join('?' for _ in values)})")
            params.extend(values)
        if query.kinds:
            values = [JobKind(kind).value for kind in query.kinds]
            clauses.append(f"kind IN ({','.join('?' for _ in values)})")
            params.extend(values)
        if query.owner is not None:
            clauses.append("owner = ?")
            params.append(query.owner)
        if query.team_name is not None:
            clauses.append("team_name = ?")
            params.append(query.team_name)

        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        params.extend((query.limit, query.offset))
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT * FROM jobs"
                + where
                + " ORDER BY priority DESC, created_at ASC, id ASC LIMIT ? OFFSET ?",
                params,
            ).fetchall()
            return [self._row_to_job(connection, row) for row in rows]
        finally:
            connection.close()

    def set_dependencies(
        self, job_id: str, dependencies: tuple[str, ...] | list[str]
    ) -> Job:
        normalized = self._normalize_dependencies(job_id, dependencies)
        now = _to_epoch_us(self._now())
        with self._transaction(immediate=True) as connection:
            row = self._require_job_row(connection, job_id)
            status = JobStatus(row["status"])
            if status not in {JobStatus.PENDING, JobStatus.BLOCKED}:
                raise JobStateError(
                    f"Dependencies cannot be changed while job is {status.value}"
                )
            self._validate_dependency_rows(connection, normalized)
            connection.execute("DELETE FROM job_dependencies WHERE job_id = ?", (job_id,))
            for dependency_id in normalized:
                if self._would_create_cycle(connection, job_id, dependency_id):
                    raise JobCycleError(
                        f"Dependency '{dependency_id}' would create a cycle for job '{job_id}'"
                    )
                connection.execute(
                    "INSERT INTO job_dependencies(job_id, depends_on_job_id) VALUES (?, ?)",
                    (job_id, dependency_id),
                )

            new_status = (
                JobStatus.PENDING
                if self._dependencies_satisfied(connection, job_id)
                else JobStatus.BLOCKED
            )
            connection.execute(
                "UPDATE jobs SET status = ?, updated_at = ? WHERE id = ?",
                (new_status.value, now, job_id),
            )
            return self._row_to_job(connection, self._require_job_row(connection, job_id))

    def claim(self, job_id: str, owner: str, lease_seconds: int) -> Job | None:
        owner = owner.strip()
        if not owner:
            raise ValueError("owner must not be empty")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        now_dt = self._now()
        now = _to_epoch_us(now_dt)
        lease_until = _to_epoch_us(now_dt + timedelta(seconds=lease_seconds))

        with self._transaction(immediate=True) as connection:
            updated = connection.execute(
                """
                UPDATE jobs
                SET status = ?, owner = ?, lease_until = ?,
                    attempts = attempts + 1,
                    started_at = COALESCE(started_at, ?),
                    updated_at = ?, finished_at = NULL, error_text = ''
                WHERE id = ?
                  AND status = ?
                  AND attempts < max_attempts
                  AND NOT EXISTS (
                      SELECT 1
                      FROM job_dependencies AS dependency
                      JOIN jobs AS required ON required.id = dependency.depends_on_job_id
                      WHERE dependency.job_id = jobs.id AND required.status <> ?
                  )
                """,
                (
                    JobStatus.RUNNING.value,
                    owner,
                    lease_until,
                    now,
                    now,
                    job_id,
                    JobStatus.PENDING.value,
                    JobStatus.COMPLETED.value,
                ),
            )
            if updated.rowcount != 1:
                return None
            self._insert_event(
                connection,
                job_id,
                "claimed",
                json.dumps({"owner": owner}, ensure_ascii=False),
                now,
            )
            return self._row_to_job(connection, self._require_job_row(connection, job_id))

    def heartbeat(self, job_id: str, owner: str, lease_seconds: int) -> bool:
        owner = owner.strip()
        if not owner:
            raise ValueError("owner must not be empty")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        now_dt = self._now()
        now = _to_epoch_us(now_dt)
        lease_until = _to_epoch_us(now_dt + timedelta(seconds=lease_seconds))
        with self._transaction(immediate=True) as connection:
            updated = connection.execute(
                """
                UPDATE jobs
                SET lease_until = ?, updated_at = ?
                WHERE id = ? AND status = ? AND owner = ? AND lease_until > ?
                """,
                (
                    lease_until,
                    now,
                    job_id,
                    JobStatus.RUNNING.value,
                    owner,
                    now,
                ),
            )
            return updated.rowcount == 1

    def _require_owned_running_job(
        self,
        connection: sqlite3.Connection,
        job_id: str,
        owner: str,
        now: int,
    ) -> sqlite3.Row:
        row = self._require_job_row(connection, job_id)
        if row["status"] != JobStatus.RUNNING.value:
            raise JobStateError(f"Job '{job_id}' is {row['status']}, not running")
        if row["owner"] != owner:
            raise JobOwnershipError(
                f"Job '{job_id}' is owned by {row['owner']!r}, not {owner!r}"
            )
        if row["lease_until"] is None or row["lease_until"] <= now:
            raise JobOwnershipError(f"Lease for job '{job_id}' has expired")
        return row

    def complete(self, job_id: str, owner: str, result: str) -> Job:
        now = _to_epoch_us(self._now())
        with self._transaction(immediate=True) as connection:
            self._require_owned_running_job(connection, job_id, owner, now)
            connection.execute(
                """
                UPDATE jobs
                SET status = ?, result_text = ?, error_text = '', owner = NULL,
                    lease_until = NULL, finished_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (JobStatus.COMPLETED.value, result, now, now, job_id),
            )
            self._insert_event(
                connection,
                job_id,
                "completed",
                json.dumps({"result": result}, ensure_ascii=False),
                now,
            )
            self._refresh_blocked_jobs(connection, now)
            return self._row_to_job(connection, self._require_job_row(connection, job_id))

    def fail(self, job_id: str, owner: str, error: str) -> Job:
        now = _to_epoch_us(self._now())
        with self._transaction(immediate=True) as connection:
            self._require_owned_running_job(connection, job_id, owner, now)
            connection.execute(
                """
                UPDATE jobs
                SET status = ?, error_text = ?, owner = NULL,
                    lease_until = NULL, finished_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (JobStatus.FAILED.value, error, now, now, job_id),
            )
            self._insert_event(
                connection,
                job_id,
                "failed",
                json.dumps({"error": error}, ensure_ascii=False),
                now,
            )
            return self._row_to_job(connection, self._require_job_row(connection, job_id))

    def cancel(self, job_id: str) -> Job:
        now = _to_epoch_us(self._now())
        with self._transaction(immediate=True) as connection:
            row = self._require_job_row(connection, job_id)
            status = JobStatus(row["status"])
            if status == JobStatus.CANCELLED:
                return self._row_to_job(connection, row)
            if status in TERMINAL_JOB_STATUSES:
                raise JobStateError(f"Job '{job_id}' is already {status.value}")
            connection.execute(
                """
                UPDATE jobs
                SET status = ?, owner = NULL, lease_until = NULL,
                    finished_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (JobStatus.CANCELLED.value, now, now, job_id),
            )
            self._insert_event(connection, job_id, "cancelled", "{}", now)
            return self._row_to_job(connection, self._require_job_row(connection, job_id))

    def recover_expired(self, now: datetime | None = None) -> list[Job]:
        now_value = _to_epoch_us(now if now is not None else self._now())
        recovered_ids: list[str] = []
        with self._transaction(immediate=True) as connection:
            rows = connection.execute(
                """
                SELECT id, attempts, max_attempts
                FROM jobs
                WHERE status = ? AND (lease_until IS NULL OR lease_until <= ?)
                ORDER BY created_at ASC, id ASC
                """,
                (JobStatus.RUNNING.value, now_value),
            ).fetchall()
            for row in rows:
                job_id = row["id"]
                if row["attempts"] >= row["max_attempts"]:
                    connection.execute(
                        """
                        UPDATE jobs
                        SET status = ?, owner = NULL, lease_until = NULL,
                            error_text = ?, finished_at = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            JobStatus.FAILED.value,
                            "Job lease expired after reaching max attempts",
                            now_value,
                            now_value,
                            job_id,
                        ),
                    )
                    self._insert_event(
                        connection,
                        job_id,
                        "failed",
                        json.dumps(
                            {"error": "Job lease expired after reaching max attempts"},
                            ensure_ascii=False,
                        ),
                        now_value,
                    )
                else:
                    next_status = (
                        JobStatus.PENDING
                        if self._dependencies_satisfied(connection, job_id)
                        else JobStatus.BLOCKED
                    )
                    connection.execute(
                        """
                        UPDATE jobs
                        SET status = ?, owner = NULL, lease_until = NULL,
                            error_text = '', finished_at = NULL, updated_at = ?
                        WHERE id = ?
                        """,
                        (next_status.value, now_value, job_id),
                    )
                    self._insert_event(
                        connection,
                        job_id,
                        "recovered",
                        json.dumps({"status": next_status.value}),
                        now_value,
                    )
                recovered_ids.append(job_id)
            return [
                self._row_to_job(connection, self._require_job_row(connection, job_id))
                for job_id in recovered_ids
            ]

    def update_progress(self, job_id: str, progress_json: str) -> Job:
        self._validate_payload(progress_json)
        now = _to_epoch_us(self._now())
        with self._transaction(immediate=True) as connection:
            self._require_job_row(connection, job_id)
            connection.execute(
                "UPDATE jobs SET progress_json = ?, updated_at = ? WHERE id = ?",
                (progress_json, now, job_id),
            )
            self._insert_event(connection, job_id, "progress", progress_json, now)
            return self._row_to_job(connection, self._require_job_row(connection, job_id))

    def append_event(
        self, job_id: str, event_type: str, payload_json: str = "{}"
    ) -> JobEvent:
        event_type = event_type.strip()
        if not event_type:
            raise ValueError("event_type must not be empty")
        self._validate_payload(payload_json)
        now = _to_epoch_us(self._now())
        with self._transaction(immediate=True) as connection:
            self._require_job_row(connection, job_id)
            event_id = self._insert_event(
                connection, job_id, event_type, payload_json, now
            )
            row = connection.execute(
                "SELECT * FROM job_events WHERE id = ?", (event_id,)
            ).fetchone()
            return self._row_to_event(row)

    def consume_events(
        self,
        event_types: tuple[str, ...] = (),
        job_kinds: tuple[JobKind, ...] = (),
        limit: int = 100,
    ) -> list[JobEvent]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        normalized = tuple(value.strip() for value in event_types)
        if any(not value for value in normalized):
            raise ValueError("event_types must not contain empty values")
        normalized_kinds = tuple(JobKind(value).value for value in job_kinds)
        now = _to_epoch_us(self._now())
        with self._transaction(immediate=True) as connection:
            params: list[object] = []
            event_filter = ""
            if normalized:
                event_filter = f" AND event_type IN ({','.join('?' for _ in normalized)})"
                params.extend(normalized)
            kind_filter = ""
            if normalized_kinds:
                kind_filter = f" AND jobs.kind IN ({','.join('?' for _ in normalized_kinds)})"
                params.extend(normalized_kinds)
            params.append(limit)
            rows = connection.execute(
                "SELECT job_events.* FROM job_events "
                "JOIN jobs ON jobs.id = job_events.job_id "
                "WHERE job_events.consumed_at IS NULL"
                + event_filter
                + kind_filter
                + " ORDER BY id ASC LIMIT ?",
                params,
            ).fetchall()
            if rows:
                connection.executemany(
                    "UPDATE job_events SET consumed_at = ? WHERE id = ?",
                    ((now, row["id"]) for row in rows),
                )
            return [
                JobEvent(
                    id=row["id"],
                    job_id=row["job_id"],
                    event_type=row["event_type"],
                    payload_json=row["payload_json"],
                    created_at=_from_epoch_us(row["created_at"]),
                    consumed_at=_from_epoch_us(now),
                )
                for row in rows
            ]

    def mark_failed(self, job_id: str, error: str) -> Job:
        """Fail a non-terminal job during recovery when no runtime owner exists."""
        now = _to_epoch_us(self._now())
        with self._transaction(immediate=True) as connection:
            row = self._require_job_row(connection, job_id)
            status = JobStatus(row["status"])
            if status in TERMINAL_JOB_STATUSES:
                return self._row_to_job(connection, row)
            connection.execute(
                """
                UPDATE jobs
                SET status = ?, error_text = ?, owner = NULL, lease_until = NULL,
                    finished_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (JobStatus.FAILED.value, error, now, now, job_id),
            )
            self._insert_event(
                connection,
                job_id,
                "failed",
                json.dumps({"error": error}, ensure_ascii=False),
                now,
            )
            return self._row_to_job(connection, self._require_job_row(connection, job_id))

    @staticmethod
    def _insert_event(
        connection: sqlite3.Connection,
        job_id: str,
        event_type: str,
        payload_json: str,
        created_at: int,
    ) -> int:
        cursor = connection.execute(
            """
            INSERT INTO job_events(job_id, event_type, payload_json, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (job_id, event_type, payload_json, created_at),
        )
        return int(cursor.lastrowid)

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> JobEvent:
        return JobEvent(
            id=row["id"],
            job_id=row["job_id"],
            event_type=row["event_type"],
            payload_json=row["payload_json"],
            created_at=_from_epoch_us(row["created_at"]),
            consumed_at=_from_epoch_us(row["consumed_at"]),
        )

    @staticmethod
    def _refresh_blocked_jobs(connection: sqlite3.Connection, now: int) -> None:
        connection.execute(
            """
            UPDATE jobs
            SET status = ?, updated_at = ?
            WHERE status = ?
              AND NOT EXISTS (
                  SELECT 1
                  FROM job_dependencies AS dependency
                  JOIN jobs AS required ON required.id = dependency.depends_on_job_id
                  WHERE dependency.job_id = jobs.id AND required.status <> ?
              )
            """,
            (
                JobStatus.PENDING.value,
                now,
                JobStatus.BLOCKED.value,
                JobStatus.COMPLETED.value,
            ),
        )

    def _row_to_job(self, connection: sqlite3.Connection, row: sqlite3.Row) -> Job:
        dependencies = tuple(
            dependency["depends_on_job_id"]
            for dependency in connection.execute(
                """
                SELECT depends_on_job_id
                FROM job_dependencies
                WHERE job_id = ?
                ORDER BY depends_on_job_id ASC
                """,
                (row["id"],),
            ).fetchall()
        )
        return Job(
            id=row["id"],
            kind=JobKind(row["kind"]),
            name=row["name"],
            description=row["description"],
            status=JobStatus(row["status"]),
            payload_json=row["payload_json"],
            progress_json=row["progress_json"],
            result_text=row["result_text"],
            error_text=row["error_text"],
            owner=row["owner"],
            team_name=row["team_name"],
            worktree_path=row["worktree_path"],
            created_at=_from_epoch_us(row["created_at"]),
            updated_at=_from_epoch_us(row["updated_at"]),
            started_at=_from_epoch_us(row["started_at"]),
            finished_at=_from_epoch_us(row["finished_at"]),
            lease_until=_from_epoch_us(row["lease_until"]),
            attempts=row["attempts"],
            max_attempts=row["max_attempts"],
            priority=row["priority"],
            parent_job_id=row["parent_job_id"],
            schedule_id=row["schedule_id"],
            dependencies=dependencies,
        )
