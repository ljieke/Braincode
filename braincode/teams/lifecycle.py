from __future__ import annotations

from enum import Enum


class TeammateState(str, Enum):
    """Authoritative lifecycle states for a team member."""

    CREATED = "created"
    RUNNING = "running"
    IDLE = "idle"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"


class LifecycleTransitionError(ValueError):
    """Raised when a teammate attempts an invalid lifecycle transition."""


_ALLOWED_TRANSITIONS: dict[TeammateState, frozenset[TeammateState]] = {
    TeammateState.CREATED: frozenset({
        TeammateState.RUNNING,
        TeammateState.STOPPING,
        TeammateState.FAILED,
    }),
    TeammateState.RUNNING: frozenset({
        TeammateState.IDLE,
        TeammateState.STOPPING,
        TeammateState.FAILED,
    }),
    TeammateState.IDLE: frozenset({
        TeammateState.RUNNING,
        TeammateState.STOPPING,
        TeammateState.FAILED,
    }),
    TeammateState.STOPPING: frozenset({
        TeammateState.STOPPED,
        TeammateState.FAILED,
    }),
    TeammateState.STOPPED: frozenset(),
    TeammateState.FAILED: frozenset(),
}


def coerce_teammate_state(value: TeammateState | str) -> TeammateState:
    if isinstance(value, TeammateState):
        return value
    try:
        return TeammateState(value)
    except ValueError as exc:
        valid = ", ".join(state.value for state in TeammateState)
        raise LifecycleTransitionError(
            f"Unknown teammate state '{value}'. Expected one of: {valid}"
        ) from exc


def validate_transition(
    current: TeammateState | str,
    target: TeammateState | str,
) -> tuple[TeammateState, TeammateState]:
    """Validate and normalize a teammate state transition.

    Re-applying the current state is intentionally idempotent. Runtime shutdown
    and task cancellation can observe the same transition from different
    callbacks, and persisting the same state twice must remain safe.
    """

    current_state = coerce_teammate_state(current)
    target_state = coerce_teammate_state(target)
    if current_state == target_state:
        return current_state, target_state
    if target_state not in _ALLOWED_TRANSITIONS[current_state]:
        raise LifecycleTransitionError(
            f"Invalid teammate lifecycle transition: "
            f"{current_state.value} -> {target_state.value}"
        )
    return current_state, target_state


def is_executing(state: TeammateState | str) -> bool:
    return coerce_teammate_state(state) in {
        TeammateState.CREATED,
        TeammateState.RUNNING,
        TeammateState.STOPPING,
    }


def is_terminal(state: TeammateState | str) -> bool:
    return coerce_teammate_state(state) in {
        TeammateState.STOPPED,
        TeammateState.FAILED,
    }
