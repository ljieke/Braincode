from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from braincode.jobs import (
    BackgroundToolRunner,
    CronExpression,
    CronExpressionError,
    JobKind,
    JobManager,
    JobSpec,
    JobStatus,
    MisfirePolicy,
    OverlapPolicy,
    PromptJobRunner,
    ScheduleSpec,
    SchedulerService,
    SQLiteJobStore,
)
from braincode.permissions import (
    DangerousCommandDetector,
    PathSandbox,
    PermissionChecker,
    PermissionMode,
    RuleEngine,
)
from braincode.tools import create_default_registry
from braincode.tools.job_tools import CronCreateParams, CronCreateTool
from braincode.validator import ConfigError, validate_config_structure


class Clock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


def make_checker(tmp_path: Path, mode: PermissionMode = PermissionMode.BYPASS):
    return PermissionChecker(
        detector=DangerousCommandDetector(),
        sandbox=PathSandbox(str(tmp_path)),
        rule_engine=RuleEngine(),
        mode=mode,
    )


async def wait_terminal(store: SQLiteJobStore, job_id: str, timeout: float = 3.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        job = store.get(job_id)
        if job is not None and job.is_terminal:
            return job
        await asyncio.sleep(0.02)
    raise AssertionError(f"Job {job_id} did not become terminal")


def test_cron_validation_and_timezone_conversion() -> None:
    cron = CronExpression.parse("0 9 * * 1-5")
    after = datetime(2026, 7, 13, 0, 0, tzinfo=UTC)
    assert cron.next_after(after, "Asia/Shanghai") == datetime(
        2026, 7, 13, 1, 0, tzinfo=UTC
    )

    sunday = CronExpression.parse("0 0 * * 1-7")
    assert 0 in sunday.weekdays and 6 in sunday.weekdays
    with pytest.raises(CronExpressionError):
        CronExpression.parse("* * *")
    with pytest.raises(CronExpressionError):
        CronExpression.parse("60 * * * *")
    with pytest.raises(CronExpressionError, match="timezone"):
        cron.next_after(after, "Not/A_Zone")


def test_scheduler_config_validation() -> None:
    validated = validate_config_structure(
        {
            "providers": [
                {
                    "name": "primary",
                    "protocol": "anthropic",
                    "base_url": "https://example.invalid",
                    "model": "model",
                }
            ],
            "scheduler": {
                "enabled": True,
                "timezone": "Asia/Shanghai",
                "poll_interval_seconds": 0.5,
                "default_misfire_policy": "run_once",
                "default_overlap_policy": "parallel",
            },
        }
    )
    assert validated["scheduler"]["enabled"] is True
    assert validated["scheduler"]["timezone"] == "Asia/Shanghai"
    with pytest.raises(ConfigError, match="timezone"):
        validate_config_structure(
            {
                "providers": [
                    {
                        "name": "primary",
                        "protocol": "anthropic",
                        "base_url": "https://example.invalid",
                        "model": "model",
                    }
                ],
                "scheduler": {"timezone": "Missing/Zone"},
            }
        )


def test_scheduler_fires_once_and_restart_does_not_duplicate(tmp_path: Path) -> None:
    start = datetime(2026, 7, 12, 0, 0, tzinfo=UTC)
    clock = Clock(start)
    store = SQLiteJobStore(tmp_path / "runtime.db", clock=clock)
    scheduler = SchedulerService(store, clock=clock, poll_interval_seconds=1)
    schedule = scheduler.create(
        ScheduleSpec(
            name="minute prompt",
            cron_expression="* * * * *",
            timezone="UTC",
            job_kind=JobKind.PROMPT,
            payload_json=json.dumps({"prompt": "hello"}),
        )
    )

    clock.value = datetime(2026, 7, 12, 0, 1, tzinfo=UTC)
    first = scheduler.process_due()
    assert len(first) == 1
    assert first[0].schedule_id == schedule.id

    restarted = SchedulerService(store, clock=clock, poll_interval_seconds=1)
    assert restarted.process_due() == []
    assert len(store.list_schedules()) == 1


def test_misfire_skip_and_run_once(tmp_path: Path) -> None:
    start = datetime(2026, 7, 12, 0, 0, tzinfo=UTC)
    clock = Clock(start)
    store = SQLiteJobStore(tmp_path / "runtime.db", clock=clock)
    scheduler = SchedulerService(store, clock=clock, poll_interval_seconds=1)
    skipped = scheduler.create(
        ScheduleSpec(
            name="skip",
            cron_expression="* * * * *",
            timezone="UTC",
            job_kind=JobKind.PROMPT,
            payload_json='{"prompt":"skip"}',
            misfire_policy=MisfirePolicy.SKIP,
        )
    )
    run_once = scheduler.create(
        ScheduleSpec(
            name="once",
            cron_expression="* * * * *",
            timezone="UTC",
            job_kind=JobKind.PROMPT,
            payload_json='{"prompt":"once"}',
            misfire_policy=MisfirePolicy.RUN_ONCE,
        )
    )
    clock.value = datetime(2026, 7, 12, 0, 5, tzinfo=UTC)
    jobs = scheduler.process_due()

    assert [job.schedule_id for job in jobs] == [run_once.id]
    assert store.get_schedule(skipped.id).next_run_at == datetime(
        2026, 7, 12, 0, 6, tzinfo=UTC
    )


def test_overlap_skip_and_parallel(tmp_path: Path) -> None:
    start = datetime(2026, 7, 12, 0, 0, tzinfo=UTC)
    clock = Clock(start)
    store = SQLiteJobStore(tmp_path / "runtime.db", clock=clock)
    scheduler = SchedulerService(store, clock=clock, poll_interval_seconds=1)
    skipped = scheduler.create(
        ScheduleSpec(
            name="skip overlap",
            cron_expression="* * * * *",
            timezone="UTC",
            job_kind=JobKind.PROMPT,
            payload_json='{"prompt":"skip"}',
            overlap_policy=OverlapPolicy.SKIP,
        )
    )
    parallel = scheduler.create(
        ScheduleSpec(
            name="parallel overlap",
            cron_expression="* * * * *",
            timezone="UTC",
            job_kind=JobKind.PROMPT,
            payload_json='{"prompt":"parallel"}',
            overlap_policy=OverlapPolicy.PARALLEL,
        )
    )
    clock.value = datetime(2026, 7, 12, 0, 1, tzinfo=UTC)
    assert len(scheduler.process_due()) == 2
    clock.value = datetime(2026, 7, 12, 0, 2, tzinfo=UTC)
    jobs = scheduler.process_due()
    assert [job.schedule_id for job in jobs] == [parallel.id]
    assert store.has_active_schedule_job(skipped.id)


@pytest.mark.asyncio
async def test_background_bash_completes_and_persists_output(
    tmp_path: Path, python_command
) -> None:
    store = SQLiteJobStore(tmp_path / "runtime.db")
    manager = JobManager(store, owner="tool-runtime")
    registry = create_default_registry()
    runner = BackgroundToolRunner(
        manager, registry, make_checker(tmp_path), work_dir=str(tmp_path)
    )
    command = python_command("print('background-ok')")
    job = runner.enqueue("Bash", {"command": command, "timeout": 5})

    completed = await wait_terminal(store, job.id)
    assert completed.status == JobStatus.COMPLETED
    assert "background-ok" in completed.result_text
    payload = json.loads(completed.payload_json)
    assert payload["cwd"] == str(tmp_path.resolve())
    assert payload["permission_mode"] == PermissionMode.BYPASS.value
    await runner.stop()


@pytest.mark.asyncio
async def test_permission_ask_creates_explicit_failed_job(
    tmp_path: Path, python_command
) -> None:
    store = SQLiteJobStore(tmp_path / "runtime.db")
    manager = JobManager(store, owner="tool-runtime")
    runner = BackgroundToolRunner(
        manager,
        create_default_registry(),
        make_checker(tmp_path, PermissionMode.DEFAULT),
        work_dir=str(tmp_path),
    )
    command = python_command("print('requires-confirmation')")
    job = runner.enqueue("Bash", {"command": command, "timeout": 5})
    assert job.status == JobStatus.FAILED
    assert "permission ask" in job.error_text


@pytest.mark.asyncio
async def test_background_bash_timeout_and_cancel(
    tmp_path: Path, python_command
) -> None:
    store = SQLiteJobStore(tmp_path / "runtime.db")
    manager = JobManager(store, owner="tool-runtime")
    runner = BackgroundToolRunner(
        manager,
        create_default_registry(),
        make_checker(tmp_path),
        work_dir=str(tmp_path),
    )
    slow = python_command("import time; time.sleep(10)")
    timed = runner.enqueue("Bash", {"command": slow, "timeout": 1})
    failed = await wait_terminal(store, timed.id, timeout=8)
    assert failed.status == JobStatus.FAILED
    assert "timed out" in failed.error_text

    cancelled = runner.enqueue("Bash", {"command": slow, "timeout": 30})
    while store.get(cancelled.id).status != JobStatus.RUNNING:
        await asyncio.sleep(0.01)
    assert runner.cancel(cancelled.id)
    result = await wait_terminal(store, cancelled.id)
    assert result.status == JobStatus.CANCELLED
    await runner.stop()


@pytest.mark.asyncio
async def test_background_bash_rechecks_permission_and_uses_sandbox(
    tmp_path: Path, python_command
) -> None:
    class FlippingChecker:
        mode = PermissionMode.BYPASS

        def __init__(self) -> None:
            self.calls = 0

        def check(self, tool, arguments):
            self.calls += 1
            effect = "allow" if self.calls == 1 else "deny"
            return type("Decision", (), {"effect": effect, "reason": "flip"})()

    checker = FlippingChecker()
    store = SQLiteJobStore(tmp_path / "runtime.db")
    manager = JobManager(store, owner="tool-runtime")
    registry = create_default_registry()
    runner = BackgroundToolRunner(
        manager, registry, checker, work_dir=str(tmp_path)  # type: ignore[arg-type]
    )
    job = runner.enqueue(
        "Bash", {"command": python_command("print('must-not-run')"), "timeout": 5}
    )
    failed = await wait_terminal(store, job.id)
    assert failed.status == JobStatus.FAILED
    assert "permission deny" in failed.error_text

    class FakeSandbox:
        def __init__(self) -> None:
            self.commands: list[str] = []

        def available(self) -> bool:
            return True

        def wrap(self, command, config):
            self.commands.append(command)
            return command

    sandbox = FakeSandbox()
    registry = create_default_registry()
    bash = registry.get("Bash")
    bash.sandbox = sandbox
    bash.sandbox_config = object()
    runner2 = BackgroundToolRunner(
        manager, registry, make_checker(tmp_path), work_dir=str(tmp_path)
    )
    sandboxed = runner2.enqueue(
        "Bash", {"command": python_command("print('sandboxed')"), "timeout": 5}
    )
    completed = await wait_terminal(store, sandboxed.id)
    assert completed.status == JobStatus.COMPLETED
    assert sandbox.commands
    await runner.stop()
    await runner2.stop()


@pytest.mark.asyncio
async def test_large_background_output_is_persisted(tmp_path: Path, python_command) -> None:
    store = SQLiteJobStore(tmp_path / "runtime.db")
    manager = JobManager(store, owner="tool-runtime")
    runner = BackgroundToolRunner(
        manager,
        create_default_registry(),
        make_checker(tmp_path),
        work_dir=str(tmp_path),
    )
    job = runner.enqueue(
        "Bash", {"command": python_command("print('x' * 12000)"), "timeout": 5}
    )
    completed = await wait_terminal(store, job.id)
    assert completed.status == JobStatus.COMPLETED
    assert "full output persisted" in completed.result_text
    output_path = tmp_path / ".braincode" / "job-output" / f"{job.id}.txt"
    assert output_path.exists()
    assert len(output_path.read_text(encoding="utf-8")) > 10_000
    await runner.stop()


@pytest.mark.asyncio
async def test_prompt_job_runner_uses_handler_and_finishes(tmp_path: Path) -> None:
    store = SQLiteJobStore(tmp_path / "runtime.db")
    manager = JobManager(store, owner="prompt-runtime")
    seen: list[str] = []

    async def handler(job, prompt: str) -> str:
        seen.append(prompt)
        return f"independent: {prompt}"

    runner = PromptJobRunner(
        manager, handler, output_dir=tmp_path / ".braincode" / "job-output"
    )
    job = manager.create(
        JobSpec(
            kind=JobKind.PROMPT,
            name="scheduled prompt",
            payload_json='{"prompt":"hello"}',
        )
    )
    runner.submit_job(job)
    completed = await wait_terminal(store, job.id)
    assert completed.status == JobStatus.COMPLETED
    assert completed.result_text == "independent: hello"
    assert seen == ["hello"]
    await runner.stop()


@pytest.mark.asyncio
async def test_prompt_job_runner_cancels_runtime_task(tmp_path: Path) -> None:
    store = SQLiteJobStore(tmp_path / "runtime.db")
    manager = JobManager(store, owner="prompt-runtime")

    async def handler(job, prompt: str) -> str:
        await asyncio.sleep(30)
        return "late"

    runner = PromptJobRunner(
        manager, handler, output_dir=tmp_path / ".braincode" / "job-output"
    )
    job = manager.create(
        JobSpec(
            kind=JobKind.PROMPT,
            name="cancel prompt",
            payload_json='{"prompt":"wait"}',
        )
    )
    runner.submit_job(job)
    while store.get(job.id).status != JobStatus.RUNNING:
        await asyncio.sleep(0.01)
    assert runner.cancel(job.id)
    cancelled = await wait_terminal(store, job.id)
    assert cancelled.status == JobStatus.CANCELLED
    await runner.stop()


@pytest.mark.asyncio
async def test_cron_tool_job_runs_through_shared_job_manager(
    tmp_path: Path, python_command
) -> None:
    start = datetime(2026, 7, 12, 0, 0, tzinfo=UTC)
    clock = Clock(start)
    store = SQLiteJobStore(tmp_path / "runtime.db", clock=clock)
    manager = JobManager(store, owner="tool-runtime")
    registry = create_default_registry()
    runner = BackgroundToolRunner(
        manager, registry, make_checker(tmp_path), work_dir=str(tmp_path)
    )
    scheduler = SchedulerService(
        store,
        clock=clock,
        on_job_created=runner.submit_job,
        poll_interval_seconds=1,
    )
    tool = CronCreateTool(scheduler, runner)
    result = await tool.execute(
        CronCreateParams(
            name="scheduled bash",
            cron_expression="* * * * *",
            timezone="UTC",
            job_kind=JobKind.TOOL,
            payload={
                "tool_name": "Bash",
                "arguments": {
                    "command": python_command("print('cron-bash')"),
                    "timeout": 5,
                },
            },
        )
    )
    assert result.is_error is False
    clock.value = datetime(2026, 7, 12, 0, 1, tzinfo=UTC)
    jobs = scheduler.process_due()
    assert len(jobs) == 1
    completed = await wait_terminal(store, jobs[0].id)
    assert completed.status == JobStatus.COMPLETED
    assert "cron-bash" in completed.result_text
    payload = json.loads(completed.payload_json)
    assert payload["permission_mode"] == PermissionMode.BYPASS.value
    await runner.stop()
