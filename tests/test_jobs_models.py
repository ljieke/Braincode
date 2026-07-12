from __future__ import annotations

from braincode.jobs import JobKind, JobQuery, JobSpec, JobStatus


def test_job_enums_match_persisted_values() -> None:
    assert {kind.value for kind in JobKind} == {"agent", "tool", "prompt", "team"}
    assert {status.value for status in JobStatus} == {
        "pending",
        "running",
        "completed",
        "failed",
        "cancelled",
        "blocked",
    }


def test_job_spec_and_query_have_safe_defaults() -> None:
    spec = JobSpec(kind=JobKind.AGENT, name="analyze")
    assert spec.payload_json == "{}"
    assert spec.dependencies == ()
    assert spec.max_attempts == 3

    query = JobQuery()
    assert query.statuses == ()
    assert query.kinds == ()
    assert query.limit == 100
