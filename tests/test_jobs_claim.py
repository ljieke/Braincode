from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier

import pytest

from braincode.jobs import (
    JobKind,
    JobOwnershipError,
    JobSpec,
    JobStatus,
    SQLiteJobStore,
)


class MutableClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 1, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


def test_concurrent_claim_has_exactly_one_winner(tmp_path: Path) -> None:
    store = SQLiteJobStore(tmp_path / "runtime.db")
    job = store.create(JobSpec(kind=JobKind.AGENT, name="one winner"))
    workers = 12
    barrier = Barrier(workers)

    def claim(index: int):
        barrier.wait()
        return store.claim(job.id, f"worker-{index}", 30)

    with ThreadPoolExecutor(max_workers=workers) as executor:
        results = list(executor.map(claim, range(workers)))

    winners = [result for result in results if result is not None]
    assert len(winners) == 1
    assert winners[0].status == JobStatus.RUNNING
    assert winners[0].attempts == 1


def test_owner_and_live_lease_are_required_to_complete(tmp_path: Path) -> None:
    clock = MutableClock()
    store = SQLiteJobStore(tmp_path / "runtime.db", clock=clock)
    job = store.create(JobSpec(kind=JobKind.TOOL, name="owned"))
    assert store.claim(job.id, "alice", 10) is not None

    with pytest.raises(JobOwnershipError):
        store.complete(job.id, "bob", "wrong owner")

    clock.advance(11)
    with pytest.raises(JobOwnershipError, match="expired"):
        store.complete(job.id, "alice", "late")


def test_heartbeat_extends_lease_and_recovery_requeues_job(tmp_path: Path) -> None:
    clock = MutableClock()
    store = SQLiteJobStore(tmp_path / "runtime.db", clock=clock)
    job = store.create(JobSpec(kind=JobKind.AGENT, name="lease", max_attempts=3))
    claimed = store.claim(job.id, "worker", 10)
    assert claimed is not None
    original_lease = claimed.lease_until

    clock.advance(5)
    assert store.heartbeat(job.id, "worker", 20) is True
    heartbeat_job = store.get(job.id)
    assert heartbeat_job is not None
    assert heartbeat_job.lease_until > original_lease  # type: ignore[operator]

    clock.advance(11)
    assert store.recover_expired() == []
    clock.advance(10)
    recovered = store.recover_expired()
    assert len(recovered) == 1
    assert recovered[0].status == JobStatus.PENDING
    assert recovered[0].owner is None
    assert recovered[0].attempts == 1


def test_expired_job_is_recovered_after_store_reopen(tmp_path: Path) -> None:
    clock = MutableClock()
    path = tmp_path / "runtime.db"
    first_process = SQLiteJobStore(path, clock=clock)
    job = first_process.create(JobSpec(kind=JobKind.AGENT, name="restart"))
    assert first_process.claim(job.id, "worker", 5) is not None

    clock.advance(6)
    restarted_process = SQLiteJobStore(path, clock=clock)
    recovered = restarted_process.recover_expired()
    assert len(recovered) == 1
    assert recovered[0].id == job.id
    assert recovered[0].status == JobStatus.PENDING
    assert recovered[0].owner is None


def test_expired_job_fails_after_max_attempts(tmp_path: Path) -> None:
    clock = MutableClock()
    store = SQLiteJobStore(tmp_path / "runtime.db", clock=clock)
    job = store.create(JobSpec(kind=JobKind.AGENT, name="bounded", max_attempts=2))

    assert store.claim(job.id, "first", 5) is not None
    clock.advance(6)
    assert store.recover_expired()[0].status == JobStatus.PENDING

    second = store.claim(job.id, "second", 5)
    assert second is not None
    assert second.attempts == 2
    clock.advance(6)
    failed = store.recover_expired()[0]
    assert failed.status == JobStatus.FAILED
    assert failed.owner is None
    assert "max attempts" in failed.error_text
    assert store.claim(job.id, "third", 5) is None


def test_complete_and_fail_record_terminal_result(tmp_path: Path) -> None:
    store = SQLiteJobStore(tmp_path / "runtime.db")
    completed = store.create(JobSpec(kind=JobKind.TOOL, name="complete"))
    assert store.claim(completed.id, "worker", 30) is not None
    completed = store.complete(completed.id, "worker", "result")
    assert completed.status == JobStatus.COMPLETED
    assert completed.result_text == "result"
    assert completed.finished_at is not None

    failed = store.create(JobSpec(kind=JobKind.TOOL, name="fail"))
    assert store.claim(failed.id, "worker", 30) is not None
    failed = store.fail(failed.id, "worker", "boom")
    assert failed.status == JobStatus.FAILED
    assert failed.error_text == "boom"
