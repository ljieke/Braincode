from __future__ import annotations

import asyncio

import pytest

from braincode.tools.recent_calls import (
    RecentToolCallTracker,
    RepeatPolicy,
    canonical_arguments_json,
    tool_call_fingerprint,
)


def _call(
    tracker: RecentToolCallTracker,
    tool_id: str,
    arguments: dict,
    output: str = "same",
    is_error: bool = False,
):
    context = tracker.before_call("Bash", arguments, RepeatPolicy.OBSERVE)
    return tracker.after_call(context, tool_id, output, is_error)


def test_argument_order_does_not_change_fingerprint():
    first = canonical_arguments_json({"file_path": "a.py", "offset": 0})
    second = canonical_arguments_json({"offset": 0, "file_path": "a.py"})
    assert first == second
    assert tool_call_fingerprint("ReadFile", first) == tool_call_fingerprint(
        "ReadFile", second
    )


def test_different_tool_or_arguments_are_not_repeated():
    tracker = RecentToolCallTracker()
    tracker.begin_run()
    first = tracker.before_call("ReadFile", {"file_path": "a.py"}, RepeatPolicy.OBSERVE)
    assert not tracker.after_call(first, "one", "same", False).repeated
    second = tracker.before_call("Grep", {"file_path": "a.py"}, RepeatPolicy.OBSERVE)
    assert not tracker.after_call(second, "two", "same", False).repeated


def test_same_result_is_compacted_then_warned():
    tracker = RecentToolCallTracker()
    tracker.begin_run()
    first = _call(tracker, "one", {"command": "pytest"})
    second = _call(tracker, "two", {"command": "pytest"})
    third = _call(tracker, "three", {"command": "pytest"})

    assert first.same_result_count == 1
    assert second.repeated is True
    assert second.compact_output is not None
    assert "one" in second.compact_output
    assert third.warning is not None
    assert third.compact_output is None
    assert "3 times" in third.warning
    assert third.block_next is False


def test_result_change_resets_consecutive_count_and_error_is_part_of_hash():
    tracker = RecentToolCallTracker()
    tracker.begin_run()
    _call(tracker, "one", {"command": "pytest"}, "same", False)
    changed = _call(tracker, "two", {"command": "pytest"}, "changed", False)
    error_changed = _call(tracker, "three", {"command": "pytest"}, "changed", True)

    assert changed.same_result_count == 1
    assert changed.compact_output is None
    assert error_changed.same_result_count == 1


def test_begin_run_clears_state_and_tracker_is_bounded_lru():
    tracker = RecentToolCallTracker(max_entries=2)
    tracker.begin_run()
    one = tracker.before_call("Bash", {"value": 1}, RepeatPolicy.OBSERVE)
    tracker.after_call(one, "one", "same", False)
    two = tracker.before_call("Bash", {"value": 2}, RepeatPolicy.OBSERVE)
    tracker.after_call(two, "two", "same", False)
    tracker.get(one.fingerprint)
    three = tracker.before_call("Bash", {"value": 3}, RepeatPolicy.OBSERVE)
    tracker.after_call(three, "three", "same", False)
    assert len(tracker) == 2
    assert tracker.get(one.fingerprint) is not None
    assert tracker.get(two.fingerprint) is None

    tracker.begin_run()
    assert len(tracker) == 0
    assert tracker.in_flight == 0


def test_non_json_arguments_use_stable_fallback():
    class Value:
        def __init__(self, value: str):
            self.value = value

    first = canonical_arguments_json({"value": Value("x")})
    second = canonical_arguments_json({"value": Value("x")})
    assert first == second


def test_concurrent_same_calls_leave_no_in_flight_state():
    async def run() -> None:
        tracker = RecentToolCallTracker()
        tracker.begin_run()
        first = tracker.before_call("ReadFile", {"file_path": "a.py"}, RepeatPolicy.OBSERVE)
        second = tracker.before_call("ReadFile", {"file_path": "a.py"}, RepeatPolicy.OBSERVE)
        assert tracker.in_flight == 2

        async def finish(context, tool_id):
            await asyncio.sleep(0)
            return tracker.after_call(context, tool_id, "same", False)

        await asyncio.gather(finish(first, "one"), finish(second, "two"))
        assert tracker.in_flight == 0
        entry = tracker.get(first.fingerprint)
        assert entry is not None
        assert entry.call_count == 2
        assert entry.same_result_count == 2

    asyncio.run(run())


def test_abandon_call_does_not_leak_in_flight():
    tracker = RecentToolCallTracker()
    tracker.begin_run()
    context = tracker.before_call("Bash", {"command": "pytest"}, RepeatPolicy.OBSERVE)
    tracker.abandon_call(context)
    assert tracker.in_flight == 0


def test_active_entries_are_not_evicted_when_capacity_is_full():
    tracker = RecentToolCallTracker(max_entries=1)
    tracker.begin_run()
    first = tracker.before_call("Bash", {"value": 1}, RepeatPolicy.OBSERVE)
    second = tracker.before_call("Bash", {"value": 2}, RepeatPolicy.OBSERVE)
    assert tracker.in_flight == 2
    tracker.after_call(first, "one", "same", False)
    assert tracker.in_flight == 1
    tracker.after_call(second, "two", "same", False)
    assert tracker.in_flight == 0
    assert len(tracker) <= 1


def test_guard_blocks_only_the_next_call_after_four_identical_results():
    tracker = RecentToolCallTracker(guard_after=4)
    tracker.begin_run()

    decisions = []
    for index in range(4):
        context = tracker.before_call(
            "ReadFile",
            {"file_path": "a.py"},
            RepeatPolicy.GUARD,
        )
        assert context.blocked is False
        decisions.append(
            tracker.after_call(context, f"tool-{index + 1}", "same", False)
        )

    assert decisions[-1].block_next is True
    guarded = tracker.before_call(
        "ReadFile",
        {"file_path": "a.py"},
        RepeatPolicy.GUARD,
    )
    assert guarded.blocked is True
    assert guarded.call_number == 5
    assert guarded.same_result_count == 4
    assert guarded.last_tool_use_id == "tool-4"
    assert guarded.policy == RepeatPolicy.GUARD
    assert tracker.in_flight == 0

    # A guard is deliberately one-shot so a later call can observe changed state.
    next_call = tracker.before_call(
        "ReadFile",
        {"file_path": "a.py"},
        RepeatPolicy.GUARD,
    )
    assert next_call.blocked is False
    changed = tracker.after_call(next_call, "tool-6", "changed", False)
    assert changed.same_result_count == 1
    assert changed.block_next is False


@pytest.mark.parametrize("policy", [RepeatPolicy.OBSERVE, RepeatPolicy.WARN])
def test_non_guard_policies_always_execute(policy: RepeatPolicy):
    tracker = RecentToolCallTracker(guard_after=2)
    tracker.begin_run()

    for index in range(6):
        context = tracker.before_call("Count", {"value": "x"}, policy)
        assert context.blocked is False
        decision = tracker.after_call(context, f"tool-{index}", "same", False)
        assert decision.block_next is False

    assert tracker.in_flight == 0


def test_guard_can_be_disabled_without_changing_phase_one_behavior():
    tracker = RecentToolCallTracker(guard_after=None)
    tracker.begin_run()

    for index in range(6):
        context = tracker.before_call(
            "ReadFile", {"file_path": "a.py"}, RepeatPolicy.GUARD
        )
        assert context.blocked is False
        decision = tracker.after_call(context, f"tool-{index}", "same", False)
        assert decision.block_next is False


def test_argument_change_and_new_run_do_not_inherit_guard():
    tracker = RecentToolCallTracker(guard_after=2)
    tracker.begin_run()
    for index in range(2):
        context = tracker.before_call(
            "ReadFile", {"file_path": "a.py"}, RepeatPolicy.GUARD
        )
        tracker.after_call(context, f"a-{index}", "same", False)

    changed_arguments = tracker.before_call(
        "ReadFile", {"file_path": "b.py"}, RepeatPolicy.GUARD
    )
    assert changed_arguments.blocked is False
    tracker.abandon_call(changed_arguments)

    tracker.begin_run()
    same_arguments_new_run = tracker.before_call(
        "ReadFile", {"file_path": "a.py"}, RepeatPolicy.GUARD
    )
    assert same_arguments_new_run.blocked is False
    tracker.abandon_call(same_arguments_new_run)


def test_unfinished_guard_calls_never_arm_the_guard():
    tracker = RecentToolCallTracker(guard_after=2)
    tracker.begin_run()
    contexts = [
        tracker.before_call(
            "ReadFile", {"file_path": "a.py"}, RepeatPolicy.GUARD
        )
        for _ in range(5)
    ]

    assert all(context.blocked is False for context in contexts)
    assert tracker.in_flight == 5
    for index, context in enumerate(contexts):
        tracker.after_call(context, f"tool-{index}", "same", False)
    assert tracker.in_flight == 0


def test_guard_snapshot_is_consumed_once_per_fingerprint():
    tracker = RecentToolCallTracker(guard_after=2)
    tracker.begin_run()
    for index in range(2):
        context = tracker.before_call(
            "ReadFile", {"file_path": "a.py"}, RepeatPolicy.GUARD
        )
        tracker.after_call(context, f"initial-{index}", "same", False)

    snapshot = tracker.guard_snapshot()
    guarded = tracker.before_call(
        "ReadFile",
        {"file_path": "a.py"},
        RepeatPolicy.GUARD,
        snapshot,
    )
    assert guarded.blocked is True

    second = tracker.before_call(
        "ReadFile",
        {"file_path": "a.py"},
        RepeatPolicy.GUARD,
        snapshot,
    )
    assert second.blocked is False
    tracker.after_call(second, "second", "same", False)

    third = tracker.before_call(
        "ReadFile",
        {"file_path": "a.py"},
        RepeatPolicy.GUARD,
        snapshot,
    )
    assert third.blocked is False
    tracker.after_call(third, "third", "same", False)
