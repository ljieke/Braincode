# 来源：公众号@小林coding
# 后端八股网站：xiaolincoding.com
# Agent网站：xiaolinnote.com
# 简历模版：jianli.xiaolinnote.com
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

from braincode.teams.lifecycle import (
    TeammateState,
    coerce_teammate_state,
    is_executing,
    validate_transition,
)
from braincode.teams.progress import TeammateProgress


class BackendType(str, Enum):
    TMUX = "tmux"
    ITERM2 = "iterm2"
    IN_PROCESS = "in-process"


@dataclass
class TeammateInfo:
    name: str
    agent_id: str
    agent_type: str
    model: str
    worktree_path: str
    backend_type: str  # BackendType value
    is_active: bool | None = None
    lifecycle_state: str | None = None
    progress: Optional[TeammateProgress] = None

    def __post_init__(self) -> None:
        # Migrate legacy configs that only persisted ``is_active``.  Before the
        # lifecycle state machine, True meant executing and False meant idle.
        if self.lifecycle_state is None:
            if self.is_active is True:
                state = TeammateState.RUNNING
            elif self.is_active is False:
                state = TeammateState.IDLE
            else:
                state = TeammateState.CREATED
        else:
            state = coerce_teammate_state(self.lifecycle_state)
        self._apply_state(state)

    @property
    def state(self) -> TeammateState:
        return coerce_teammate_state(self.lifecycle_state or TeammateState.CREATED)

    def transition_to(self, target: TeammateState | str) -> bool:
        current, target_state = validate_transition(self.state, target)
        if current == target_state:
            return False
        self._apply_state(target_state)
        return True

    def _apply_state(self, state: TeammateState) -> None:
        self.lifecycle_state = state.value
        # Compatibility field for older clients. It describes whether the
        # teammate is currently executing, not whether its process is alive.
        self.is_active = is_executing(state)
        if self.progress is not None:
            self.progress.status = state.value

    def to_dict(self) -> dict:
        # Exclude progress (runtime-only, contains threading.Lock)
        return {
            "name": self.name,
            "agent_id": self.agent_id,
            "agent_type": self.agent_type,
            "model": self.model,
            "worktree_path": self.worktree_path,
            "backend_type": self.backend_type,
            "is_active": self.is_active,
            "lifecycle_state": self.lifecycle_state,
        }

    @classmethod
    def from_dict(cls, data: dict) -> TeammateInfo:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


def _sanitize_name(name: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_-]", "-", name.strip().lower())
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug or "team"


@dataclass
class AgentTeam:
    name: str
    lead_agent_id: str
    members: list[TeammateInfo] = field(default_factory=list)
    config_path: str = ""
    description: str = ""

    def get_member(self, name: str) -> TeammateInfo | None:
        for m in self.members:
            if m.name == name or m.agent_id == name:
                return m
        return None


    def add_member(self, member: TeammateInfo) -> None:
        self.members.append(member)

    def remove_member(self, name: str) -> bool:
        for i, m in enumerate(self.members):
            if m.name == name or m.agent_id == name:
                self.members.pop(i)
                return True
        return False


    def set_member_active(self, name: str, is_active: bool | None) -> bool:
        member = self.get_member(name)
        if member is None:
            return False
        if is_active is True:
            member.transition_to(TeammateState.RUNNING)
        elif is_active is False:
            member.transition_to(TeammateState.IDLE)
        return True

    def all_idle(self) -> bool:
        return all(m.state == TeammateState.IDLE for m in self.members)


    def active_members(self) -> list[TeammateInfo]:
        return [m for m in self.members if is_executing(m.state)]

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "lead_agent_id": self.lead_agent_id,
            "members": [m.to_dict() for m in self.members],
            "config_path": self.config_path,
            "description": self.description,
        }


    @classmethod
    def from_dict(cls, data: dict) -> AgentTeam:
        members = [TeammateInfo.from_dict(m) for m in data.get("members", [])]
        return cls(
            name=data["name"],
            lead_agent_id=data["lead_agent_id"],
            members=members,
            config_path=data.get("config_path", ""),
            description=data.get("description", ""),
        )

    def save(self) -> None:
        path = Path(self.config_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")

    @classmethod
    def load(cls, config_path: str) -> AgentTeam:
        data = json.loads(Path(config_path).read_text(encoding="utf-8"))
        team = cls.from_dict(data)
        team.config_path = config_path
        return team


def resolve_team_dir(team_name: str) -> Path:
    slug = _sanitize_name(team_name)
    return Path.home() / ".braincode" / "teams" / slug


def unique_team_name(team_name: str) -> str:
    slug = _sanitize_name(team_name)
    base_dir = Path.home() / ".braincode" / "teams"
    if not (base_dir / slug).exists():
        return slug
    counter = 2
    while (base_dir / f"{slug}-{counter}").exists():
        counter += 1
    return f"{slug}-{counter}"
