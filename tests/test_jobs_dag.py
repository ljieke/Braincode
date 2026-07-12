from __future__ import annotations

from pathlib import Path

import pytest

from braincode.jobs import (
    JobCycleError,
    JobDependencyError,
    JobKind,
    JobSpec,
    JobStatus,
    SQLiteJobStore,
)


@pytest.fixture
def store(tmp_path: Path) -> SQLiteJobStore:
    return SQLiteJobStore(tmp_path / "runtime.db")


def test_missing_duplicate_and_self_dependencies_are_rejected(
    store: SQLiteJobStore,
) -> None:
    with pytest.raises(JobDependencyError, match="does not exist"):
        store.create(
            JobSpec(kind=JobKind.AGENT, name="missing", dependencies=("unknown",))
        )

    dependency = store.create(JobSpec(id="dep", kind=JobKind.AGENT, name="dep"))
    with pytest.raises(JobDependencyError, match="Duplicate"):
        store.create(
            JobSpec(
                kind=JobKind.AGENT,
                name="duplicate",
                dependencies=(dependency.id, dependency.id),
            )
        )
    with pytest.raises(JobDependencyError, match="itself"):
        store.create(
            JobSpec(
                id="self",
                kind=JobKind.AGENT,
                name="self",
                dependencies=("self",),
            )
        )


def test_blocked_job_becomes_pending_when_all_dependencies_complete(
    store: SQLiteJobStore,
) -> None:
    first = store.create(JobSpec(id="first", kind=JobKind.AGENT, name="first"))
    second = store.create(
        JobSpec(
            id="second",
            kind=JobKind.AGENT,
            name="second",
            dependencies=(first.id,),
        )
    )
    assert second.status == JobStatus.BLOCKED
    assert store.claim(second.id, "early", 30) is None

    assert store.claim(first.id, "worker", 30) is not None
    store.complete(first.id, "worker", "done")

    unblocked = store.get(second.id)
    assert unblocked is not None
    assert unblocked.status == JobStatus.PENDING
    assert store.claim(second.id, "worker", 30) is not None


def test_two_node_cycle_is_rejected_without_losing_old_dependencies(
    store: SQLiteJobStore,
) -> None:
    first = store.create(JobSpec(id="first", kind=JobKind.AGENT, name="first"))
    second = store.create(
        JobSpec(
            id="second",
            kind=JobKind.AGENT,
            name="second",
            dependencies=(first.id,),
        )
    )

    with pytest.raises(JobCycleError):
        store.set_dependencies(first.id, (second.id,))

    unchanged = store.get(second.id)
    assert unchanged is not None
    assert unchanged.dependencies == (first.id,)
    assert store.get(first.id).dependencies == ()  # type: ignore[union-attr]


def test_multi_node_cycle_is_rejected(store: SQLiteJobStore) -> None:
    first = store.create(JobSpec(id="first", kind=JobKind.AGENT, name="first"))
    second = store.create(
        JobSpec(
            id="second",
            kind=JobKind.AGENT,
            name="second",
            dependencies=(first.id,),
        )
    )
    third = store.create(
        JobSpec(
            id="third",
            kind=JobKind.AGENT,
            name="third",
            dependencies=(second.id,),
        )
    )

    with pytest.raises(JobCycleError):
        store.set_dependencies(first.id, (third.id,))


def test_dependencies_can_be_replaced_for_non_running_job(
    store: SQLiteJobStore,
) -> None:
    dependency = store.create(JobSpec(id="dep", kind=JobKind.AGENT, name="dep"))
    job = store.create(JobSpec(id="job", kind=JobKind.AGENT, name="job"))

    blocked = store.set_dependencies(job.id, [dependency.id])
    assert blocked.status == JobStatus.BLOCKED
    assert blocked.dependencies == (dependency.id,)

    pending = store.set_dependencies(job.id, [])
    assert pending.status == JobStatus.PENDING
    assert pending.dependencies == ()
