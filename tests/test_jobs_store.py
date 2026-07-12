from __future__ import annotations

import json
from pathlib import Path

import pytest

from braincode.jobs import (
    JobKind,
    JobQuery,
    JobSpec,
    JobStateError,
    JobStatus,
    SQLiteJobStore,
)


@pytest.fixture
def store(tmp_path: Path) -> SQLiteJobStore:
    return SQLiteJobStore(tmp_path / "runtime.db")


def test_job_crud_and_persistence(store: SQLiteJobStore) -> None:
    created = store.create(
        JobSpec(
            id="job-1",
            kind=JobKind.TOOL,
            name="run tests",
            description="Execute the suite",
            payload_json=json.dumps({"command": "pytest"}),
            team_name="core",
            worktree_path="/repo/wt",
            priority=7,
        )
    )

    assert created.id == "job-1"
    assert created.status == JobStatus.PENDING
    assert created.created_at.tzinfo is not None
    assert json.loads(created.payload_json) == {"command": "pytest"}

    reopened = SQLiteJobStore(store.database)
    fetched = reopened.get("job-1")
    assert fetched is not None
    assert fetched.name == "run tests"
    assert fetched.team_name == "core"
    assert fetched.worktree_path == "/repo/wt"


def test_list_filters_and_priority_order(store: SQLiteJobStore) -> None:
    store.create(JobSpec(id="low", kind=JobKind.AGENT, name="low", priority=1))
    store.create(
        JobSpec(id="high", kind=JobKind.TOOL, name="high", priority=10, team_name="a")
    )
    store.create(
        JobSpec(id="mid", kind=JobKind.TOOL, name="mid", priority=5, team_name="a")
    )

    assert [job.id for job in store.list()] == ["high", "mid", "low"]
    filtered = store.list(
        JobQuery(kinds=(JobKind.TOOL,), team_name="a", limit=10)
    )
    assert [job.id for job in filtered] == ["high", "mid"]


def test_cancel_is_idempotent_but_terminal_jobs_cannot_be_cancelled(
    store: SQLiteJobStore,
) -> None:
    job = store.create(JobSpec(kind=JobKind.PROMPT, name="later"))
    cancelled = store.cancel(job.id)
    assert cancelled.status == JobStatus.CANCELLED
    assert store.cancel(job.id).status == JobStatus.CANCELLED

    completed = store.create(JobSpec(kind=JobKind.TOOL, name="done"))
    claimed = store.claim(completed.id, "worker", 30)
    assert claimed is not None
    store.complete(completed.id, "worker", "ok")
    with pytest.raises(JobStateError):
        store.cancel(completed.id)


def test_invalid_payload_and_limits_are_rejected(store: SQLiteJobStore) -> None:
    with pytest.raises(ValueError, match="valid JSON"):
        store.create(
            JobSpec(kind=JobKind.AGENT, name="bad payload", payload_json="{")
        )
    with pytest.raises(ValueError, match="max_attempts"):
        store.create(JobSpec(kind=JobKind.AGENT, name="bad attempts", max_attempts=0))
    with pytest.raises(ValueError, match="limit"):
        store.list(JobQuery(limit=0))
