# 来源：公众号@小林coding
# 后端八股网站：xiaolincoding.com
# Agent网站：xiaolinnote.com
# 简历模版：jianli.xiaolinnote.com
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from braincode.jobs.models import JobKind, JobSpec, JobStatus

if TYPE_CHECKING:
    from braincode.jobs.manager import JobManager


@dataclass
class SharedTask:
    id: str
    title: str
    description: str = ""
    status: str = "pending"  # pending | in_progress | completed | blocked
    assignee: str = ""
    blocks: list[str] = field(default_factory=list)
    blocked_by: list[str] = field(default_factory=list)
    created_by: str = ""


    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SharedTask:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


class SharedTaskStore:


    def __init__(
        self,
        path: str | Path,
        job_manager: JobManager | None = None,
        team_name: str = "",
    ) -> None:
        self._path = Path(path)
        self._job_manager = job_manager
        self._team_name = team_name
        self._next_id = 1
        self._tasks: dict[str, SharedTask] = {}
        self._load()
        self._ensure_durable_tasks()

    def _load(self) -> None:
        if not self._path.exists():
            return
        data = json.loads(self._path.read_text(encoding="utf-8"))
        self._next_id = data.get("next_id", 1)
        for t in data.get("tasks", []):
            task = SharedTask.from_dict(t)
            self._tasks[task.id] = task

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "next_id": self._next_id,
            "tasks": [t.to_dict() for t in self._tasks.values()],
        }
        self._path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    def create(
        self,
        title: str,
        description: str = "",
        assignee: str = "",
        blocks: list[str] | None = None,
        blocked_by: list[str] | None = None,
        created_by: str = "",
    ) -> SharedTask:
        task_id = str(self._next_id)
        self._next_id += 1
        task = SharedTask(
            id=task_id,
            title=title,
            description=description,
            assignee=assignee,
            blocks=blocks or [],
            blocked_by=blocked_by or [],
            created_by=created_by,
        )
        if self._job_manager is not None:
            try:
                self._create_durable_task(task)
            except Exception:
                self._next_id -= 1
                raise
        self._tasks[task_id] = task
        self._save()
        return task

    def get(self, task_id: str) -> SharedTask | None:
        self._load()
        task = self._tasks.get(task_id)
        if task is not None:
            self._sync_from_job(task)
        return task


    def list_tasks(
        self,
        status: str | None = None,
        assignee: str | None = None,
    ) -> list[SharedTask]:
        self._load()
        result = list(self._tasks.values())
        for task in result:
            self._sync_from_job(task)
        if status:
            result = [t for t in result if t.status == status]
        if assignee:
            result = [t for t in result if t.assignee == assignee]
        return result


    def update(
        self,
        task_id: str,
        status: str | None = None,
        assignee: str | None = None,
        description: str | None = None,
        add_blocks: list[str] | None = None,
        add_blocked_by: list[str] | None = None,
    ) -> SharedTask | None:
        self._load()
        task = self._tasks.get(task_id)
        if task is None:
            return None
        if status is not None:
            task.status = status
        if assignee is not None:
            task.assignee = assignee
        if description is not None:
            task.description = description
        if add_blocks:
            for bid in add_blocks:
                if bid not in task.blocks:
                    task.blocks.append(bid)
        if add_blocked_by:
            for bid in add_blocked_by:
                if bid not in task.blocked_by:
                    task.blocked_by.append(bid)
        self._sync_to_job(task, status=status)
        if add_blocks:
            for blocked_id in add_blocks:
                blocked_task = self._tasks.get(blocked_id)
                if blocked_task is not None:
                    self._sync_to_job(blocked_task, status=None)
        self._save()
        return task

    def init_empty(self) -> None:
        self._tasks.clear()
        self._next_id = 1
        self._save()

    def _job_id(self, task_id: str) -> str:
        return f"team:{self._team_name}:task:{task_id}"

    def _create_durable_task(self, task: SharedTask) -> None:
        if self._job_manager is None:
            return
        dependencies = self._dependency_ids(task)
        self._job_manager.create(
            JobSpec(
                id=self._job_id(task.id),
                kind=JobKind.TEAM,
                name=task.title,
                description=task.description,
                payload_json=json.dumps(
                    {
                        "legacy_task_id": task.id,
                        "assignee": task.assignee,
                        "created_by": task.created_by,
                    },
                    ensure_ascii=False,
                ),
                team_name=self._team_name,
                dependencies=dependencies,
            )
        )

    def _ensure_durable_tasks(self) -> None:
        if self._job_manager is None or not self._tasks:
            return
        for task in self._tasks.values():
            if self._job_manager.store.get(self._job_id(task.id)) is None:
                self._job_manager.create(
                    JobSpec(
                        id=self._job_id(task.id),
                        kind=JobKind.TEAM,
                        name=task.title,
                        description=task.description,
                        payload_json=json.dumps(
                            {
                                "legacy_task_id": task.id,
                                "assignee": task.assignee,
                                "created_by": task.created_by,
                            },
                            ensure_ascii=False,
                        ),
                        team_name=self._team_name,
                    )
                )
        for task in self._tasks.values():
            dependencies = self._dependency_ids(task)
            self._job_manager.store.set_dependencies(self._job_id(task.id), dependencies)
            self._sync_to_job(task, status=task.status)

    def _sync_to_job(self, task: SharedTask, status: str | None) -> None:
        if self._job_manager is None:
            return
        job_id = self._job_id(task.id)
        dependencies = self._dependency_ids(task)
        job = self._job_manager.store.get(job_id)
        if job is None:
            self._create_durable_task(task)
            job = self._job_manager.store.get(job_id)
        if job is None:
            return
        if job.status in {JobStatus.PENDING, JobStatus.BLOCKED}:
            self._job_manager.store.set_dependencies(job_id, dependencies)
        owner = f"team:{self._team_name}:{task.assignee or task.created_by or 'unassigned'}"
        if status == "in_progress":
            self._job_manager.ensure_team_task_running(job_id, owner)
        elif status == "completed":
            self._job_manager.complete_team_task(job_id, owner, task.description)

    def _sync_from_job(self, task: SharedTask) -> None:
        if self._job_manager is None:
            return
        job = self._job_manager.store.get(self._job_id(task.id))
        if job is None:
            return
        task.status = {
            JobStatus.PENDING: "pending",
            JobStatus.BLOCKED: "blocked",
            JobStatus.RUNNING: "in_progress",
            JobStatus.COMPLETED: "completed",
            JobStatus.FAILED: "blocked",
            JobStatus.CANCELLED: "blocked",
        }[job.status]

    def _dependency_ids(self, task: SharedTask) -> tuple[str, ...]:
        legacy_ids = set(task.blocked_by)
        legacy_ids.update(
            other.id for other in self._tasks.values() if task.id in other.blocks
        )
        return tuple(self._job_id(value) for value in sorted(legacy_ids))
