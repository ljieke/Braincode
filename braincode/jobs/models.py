from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


class JobKind(StrEnum):
    AGENT = "agent"
    TOOL = "tool"
    PROMPT = "prompt"
    TEAM = "team"


class JobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    BLOCKED = "blocked"


TERMINAL_JOB_STATUSES = frozenset(
    {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}
)


@dataclass(frozen=True)
class JobSpec:
    kind: JobKind
    name: str
    description: str = ""
    payload_json: str = "{}"
    team_name: str = ""
    worktree_path: str = ""
    max_attempts: int = 3
    priority: int = 0
    parent_job_id: str | None = None
    schedule_id: str | None = None
    dependencies: tuple[str, ...] = field(default_factory=tuple)
    id: str | None = None


@dataclass(frozen=True)
class Job:
    id: str
    kind: JobKind
    name: str
    description: str
    status: JobStatus
    payload_json: str
    progress_json: str
    result_text: str
    error_text: str
    owner: str | None
    team_name: str
    worktree_path: str
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    lease_until: datetime | None
    attempts: int
    max_attempts: int
    priority: int
    parent_job_id: str | None
    schedule_id: str | None
    dependencies: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_JOB_STATUSES


@dataclass(frozen=True)
class JobQuery:
    statuses: tuple[JobStatus, ...] = field(default_factory=tuple)
    kinds: tuple[JobKind, ...] = field(default_factory=tuple)
    owner: str | None = None
    team_name: str | None = None
    limit: int = 100
    offset: int = 0


@dataclass(frozen=True)
class JobEvent:
    id: int
    job_id: str
    event_type: str
    payload_json: str
    created_at: datetime
    consumed_at: datetime | None
