from __future__ import annotations

from contracts import JobStatus, RuntimeJob, Stage
from apps.worker.main import WorkerApp
from runtime_core.repos import InMemoryRuntimeRepository, RuntimeRepository


class _FlakyReviewEngine:
    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.calls = 0

    def handle_job(self, job: RuntimeJob, repository: RuntimeRepository) -> dict:
        self.calls += 1
        if self.calls <= self.failures:
            raise ValueError("provider_error:provider_unavailable:panel_accept_quorum_unavailable")
        return {"reviewed": True}


class _InvalidSubmissionEngine:
    def handle_job(self, job: RuntimeJob, repository: RuntimeRepository) -> dict:
        raise ValueError("structure_gate:missing_conclusion")


def test_provider_review_failure_retries_to_success(monkeypatch) -> None:
    monkeypatch.setenv("RESEARKA_V2_REVIEW_JOB_MAX_RETRIES", "2")
    repo = InMemoryRuntimeRepository()
    first = repo.enqueue_job(RuntimeJob(target_object_id="submission-1", stage=Stage.REVIEW))
    worker = WorkerApp(repo, engine=_FlakyReviewEngine(failures=2))

    first_result = worker.run_once()
    second_result = worker.run_once()
    third_result = worker.run_once()

    assert first_result["retried"] == second_result["retried"] == 1
    assert third_result == {
        "claimed": 1,
        "completed": 1,
        "failed": 0,
        "target_object_id": "submission-1",
        "stage": Stage.REVIEW.value,
        "job_id": second_result["retry_job_id"],
    }
    failed_first = repo.get_job(first.id)
    failed_retry = repo.get_job(first_result["retry_job_id"])
    completed_retry = repo.get_job(second_result["retry_job_id"])
    assert failed_first is not None and failed_first.status == JobStatus.FAILED
    assert failed_retry is not None and failed_retry.status == JobStatus.FAILED
    assert completed_retry is not None and completed_retry.status == JobStatus.COMPLETED
    assert repo.queued_jobs() == []


def test_provider_review_retry_is_bounded(monkeypatch) -> None:
    monkeypatch.setenv("RESEARKA_V2_REVIEW_JOB_MAX_RETRIES", "1")
    repo = InMemoryRuntimeRepository()
    repo.enqueue_job(RuntimeJob(target_object_id="submission-1", stage=Stage.REVIEW))
    worker = WorkerApp(repo, engine=_FlakyReviewEngine(failures=99))

    assert worker.run_once()["retried"] == 1
    exhausted = worker.run_once()

    assert exhausted["retried"] == 0
    assert exhausted["retry_job_id"] is None
    assert worker.run_once() == {"claimed": 0, "completed": 0, "failed": 0}


def test_non_provider_failure_is_not_retried() -> None:
    repo = InMemoryRuntimeRepository()
    repo.enqueue_job(RuntimeJob(target_object_id="submission-1", stage=Stage.INTAKE))

    result = WorkerApp(repo, engine=_InvalidSubmissionEngine()).run_once()

    assert result["retried"] == 0
    assert result["retry_job_id"] is None
    assert repo.queued_jobs() == []
