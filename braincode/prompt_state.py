from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Hashable
from dataclasses import dataclass
from typing import Any, Protocol

from braincode.jobs.models import JobQuery, JobStatus

log = logging.getLogger(__name__)


class PromptStateProvider(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def priority(self) -> int: ...

    def signature(self) -> Hashable: ...

    def render(self) -> str: ...


@dataclass(frozen=True)
class PromptStateStats:
    cache_hits: int
    cache_misses: int
    rebuild_reasons: dict[str, int]


@dataclass
class _CachedSection:
    signature: Hashable
    content: str


class PromptStateRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, PromptStateProvider] = {}
        self._cache: dict[str, _CachedSection] = {}
        self._cache_hits = 0
        self._cache_misses = 0
        self._rebuild_reasons: Counter[str] = Counter()

    def register(self, provider: PromptStateProvider) -> None:
        previous = self._providers.get(provider.name)
        self._providers[provider.name] = provider
        if previous is not provider:
            self._cache.pop(provider.name, None)

    def unregister(self, name: str) -> None:
        self._providers.pop(name, None)
        self._cache.pop(name, None)

    def render(self) -> str:
        sections: list[str] = []
        providers = sorted(
            (
                provider
                for provider in self._providers.values()
                if not bool(getattr(provider, "volatile", False))
            ),
            key=lambda provider: (provider.priority, provider.name),
        )
        for provider in providers:
            signature = provider.signature()
            cached = self._cache.get(provider.name)
            if cached is not None and cached.signature == signature:
                self._cache_hits += 1
                content = cached.content
            else:
                reason = "new_provider" if cached is None else "signature_changed"
                content = provider.render().strip()
                self._cache[provider.name] = _CachedSection(signature, content)
                self._cache_misses += 1
                self._rebuild_reasons[reason] += 1
                log.debug(
                    "Prompt state section rebuilt: name=%s reason=%s",
                    provider.name,
                    reason,
                )
            if content:
                sections.append(content)
        return "\n\n".join(sections)

    def render_volatile(self) -> str:
        sections = [
            provider.render().strip()
            for provider in sorted(
                (
                    provider
                    for provider in self._providers.values()
                    if bool(getattr(provider, "volatile", False))
                ),
                key=lambda provider: (provider.priority, provider.name),
            )
        ]
        return "\n\n".join(section for section in sections if section)

    def stats(self) -> PromptStateStats:
        return PromptStateStats(
            cache_hits=self._cache_hits,
            cache_misses=self._cache_misses,
            rebuild_reasons=dict(self._rebuild_reasons),
        )


class TextPromptStateProvider:
    def __init__(self, name: str, priority: int, heading: str) -> None:
        self._name = name
        self._priority = priority
        self._heading = heading
        self._content = ""

    @property
    def name(self) -> str:
        return self._name

    @property
    def priority(self) -> int:
        return self._priority

    def set_content(self, content: str) -> None:
        self._content = content.strip()

    def signature(self) -> Hashable:
        return self._content

    def render(self) -> str:
        if not self._content:
            return ""
        return f"# {self._heading}\n\n{self._content}"


class MCPPromptStateProvider(TextPromptStateProvider):
    def __init__(self) -> None:
        super().__init__("mcp", 150, "MCP Server State")


class SkillsPromptStateProvider(TextPromptStateProvider):
    def __init__(self) -> None:
        super().__init__("skills", 160, "Skills Directory")


class JobPromptStateProvider:
    name = "jobs"
    priority = 110

    def __init__(self, job_manager: Any) -> None:
        self._job_manager = job_manager

    def _jobs(self) -> tuple[Any, ...]:
        statuses = (JobStatus.PENDING, JobStatus.RUNNING, JobStatus.BLOCKED)
        jobs = self._job_manager.store.list(
            JobQuery(statuses=statuses, limit=50)
        )
        return tuple(sorted(jobs, key=lambda job: job.id))

    def signature(self) -> Hashable:
        return tuple(
            (
                job.id,
                job.kind.value,
                job.name,
                job.status.value,
                job.team_name,
                job.worktree_path,
            )
            for job in self._jobs()
        )

    def render(self) -> str:
        jobs = self._jobs()
        if not jobs:
            return ""
        lines = ["# Background Jobs", ""]
        for job in jobs:
            lines.append(
                f"- `{job.id}` [{job.status.value}] {job.kind.value}: {job.name}"
            )
        return "\n".join(lines)


class CronPromptStateProvider:
    name = "cron"
    priority = 120

    def __init__(self, scheduler: Any) -> None:
        self._scheduler = scheduler

    def _schedules(self) -> tuple[Any, ...]:
        return tuple(sorted(self._scheduler.list(), key=lambda item: item.id))

    def signature(self) -> Hashable:
        return tuple(
            (
                schedule.id,
                schedule.name,
                schedule.cron_expression,
                schedule.timezone,
                schedule.job_kind.value,
                schedule.enabled,
                schedule.misfire_policy.value,
                schedule.overlap_policy.value,
            )
            for schedule in self._schedules()
        )

    def render(self) -> str:
        schedules = self._schedules()
        if not schedules:
            return ""
        lines = ["# Cron Schedules", ""]
        for schedule in schedules:
            state = "enabled" if schedule.enabled else "disabled"
            lines.append(
                f"- `{schedule.id}` {schedule.name}: `{schedule.cron_expression}` "
                f"({schedule.timezone}, {state})"
            )
        return "\n".join(lines)


class TeamPromptStateProvider:
    name = "teams"
    priority = 130

    def __init__(self, team_manager: Any) -> None:
        self._team_manager = team_manager

    def _snapshot(self) -> tuple[tuple[Any, ...], ...]:
        teams: list[tuple[Any, ...]] = []
        for team_name in sorted(self._team_manager.list_teams()):
            team = self._team_manager.get_team(team_name)
            if team is None:
                continue
            members = tuple(
                sorted(
                    (
                        member.name,
                        member.agent_type,
                        member.backend_type,
                        member.worktree_path,
                    )
                    for member in team.members
                )
            )
            teams.append((team.name, team.description, members))
        return tuple(teams)

    def signature(self) -> Hashable:
        return self._snapshot()

    def render(self) -> str:
        snapshot = self._snapshot()
        if not snapshot:
            return ""
        lines = ["# Agent Teams", ""]
        for team_name, description, members in snapshot:
            suffix = f" — {description}" if description else ""
            lines.append(f"- **{team_name}**{suffix}")
            for member_name, agent_type, backend, worktree_path in members:
                location = f", worktree={worktree_path}" if worktree_path else ""
                lines.append(
                    f"  - {member_name} ({agent_type}, {backend}{location})"
                )
        return "\n".join(lines)


class WorktreePromptStateProvider:
    name = "worktrees"
    priority = 140

    def __init__(self, worktree_manager: Any) -> None:
        self._worktree_manager = worktree_manager

    def _snapshot(self) -> tuple[Any, ...]:
        session = self._worktree_manager.get_current_session()
        current = None
        if session is not None:
            current = (
                session.worktree_name,
                session.worktree_path,
                session.original_branch,
            )
        worktrees = tuple(
            sorted(
                (worktree.name, worktree.path, worktree.branch)
                for worktree in self._worktree_manager.list_worktrees()
            )
        )
        return current, worktrees

    def signature(self) -> Hashable:
        return self._snapshot()

    def render(self) -> str:
        current, worktrees = self._snapshot()
        if current is None and not worktrees:
            return ""
        lines = ["# Worktrees", ""]
        if current is not None:
            lines.append(
                f"Current: {current[0]} at `{current[1]}` (from {current[2]})"
            )
        for name, path, branch in worktrees:
            lines.append(f"- {name}: `{path}` on `{branch}`")
        return "\n".join(lines)


class MemoryPromptStateProvider:
    name = "memory"
    priority = 170

    def __init__(self, memory_manager: Any) -> None:
        self._memory_manager = memory_manager

    def _manifest(self) -> tuple[tuple[str, int, int], ...]:
        entries: list[tuple[str, int, int]] = []
        roots = (
            self._memory_manager.user_mem_dir,
            self._memory_manager.project_mem_dir,
        )
        for root in roots:
            if not root.exists():
                continue
            for path in sorted(root.glob("*.md")):
                try:
                    stat = path.stat()
                except OSError:
                    continue
                entries.append((str(path), stat.st_mtime_ns, stat.st_size))
        return tuple(entries)

    def signature(self) -> Hashable:
        return self._manifest()

    def render(self) -> str:
        if not self._manifest():
            return ""
        return self._memory_manager.load()


class RecoveryPromptStateProvider:
    name = "recovery"
    priority = 180

    def __init__(self, recovery_controller: Any) -> None:
        self._recovery_controller = recovery_controller

    def _snapshot(self) -> tuple[Any, ...]:
        clients = tuple(
            (
                str(getattr(client, "provider_name", "")),
                str(getattr(client, "model", "")),
            )
            for client in self._recovery_controller.clients
        )
        policy = self._recovery_controller.policy
        return (
            self._recovery_controller.current_provider_name,
            clients,
            policy.max_retries,
            policy.max_output_continuations,
        )

    def signature(self) -> Hashable:
        return self._snapshot()

    def render(self) -> str:
        current, clients, max_retries, max_continuations = self._snapshot()
        providers = [
            "/".join(part for part in (provider, model) if part)
            for provider, model in clients
            if provider or model
        ]
        if not current and not providers:
            return ""
        return (
            "# Provider Recovery\n\n"
            f"Current provider: {current}\n"
            f"Fallback chain: {', '.join(providers)}\n"
            f"Retry limit: {max_retries}; output continuations: {max_continuations}"
        )
