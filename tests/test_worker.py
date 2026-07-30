from __future__ import annotations

from contracts import EventType, JobStatus, RuntimeJob, Stage
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
    monkeypatch.setenv("RESEARKA_V2_REVIEW_JOB_RETRY_BACKOFF_SEC", "0")
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
    monkeypatch.setenv("RESEARKA_V2_REVIEW_JOB_RETRY_BACKOFF_SEC", "0")
    repo = InMemoryRuntimeRepository()
    repo.enqueue_job(RuntimeJob(target_object_id="submission-1", stage=Stage.REVIEW))
    worker = WorkerApp(repo, engine=_FlakyReviewEngine(failures=99))

    assert worker.run_once()["retried"] == 1
    exhausted = worker.run_once()

    assert exhausted["retried"] == 0
    assert exhausted["retry_job_id"] is None
    assert worker.run_once() == {"claimed": 0, "completed": 0, "failed": 0}


def test_provider_review_retry_enqueue_failure_is_terminal(monkeypatch) -> None:
    repo = InMemoryRuntimeRepository()
    first = repo.enqueue_job(RuntimeJob(target_object_id="submission-1", stage=Stage.REVIEW))

    def fail_enqueue(job: RuntimeJob) -> RuntimeJob:
        raise RuntimeError("queue unavailable")

    monkeypatch.setattr(repo, "enqueue_job", fail_enqueue)

    result = WorkerApp(repo, engine=_FlakyReviewEngine(failures=1)).run_once()

    assert result["retried"] == 0
    failed = repo.get_job(first.id)
    assert failed is not None
    assert failed.payload["failure_reason"].endswith("retry_enqueue_failed: queue unavailable")
    failure_event = [event for event in repo.list_events() if event.event_type == EventType.JOB_FAILED][-1]
    assert failure_event.payload["terminal"] is True
    assert failure_event.payload["reason"].endswith("retry_enqueue_failed: queue unavailable")


def test_provider_review_retry_waits_for_backoff(monkeypatch) -> None:
    monkeypatch.setenv("RESEARKA_V2_REVIEW_JOB_RETRY_BACKOFF_SEC", "3600")
    repo = InMemoryRuntimeRepository()
    repo.enqueue_job(RuntimeJob(target_object_id="submission-1", stage=Stage.REVIEW))

    result = WorkerApp(repo, engine=_FlakyReviewEngine(failures=1)).run_once()

    assert result["retried"] == 1
    retry = repo.get_job(result["retry_job_id"])
    assert retry is not None and retry.payload["retry_not_before"]
    assert repo.claim_next_job() is None
    failure = next(event for event in repo.list_events() if event.event_type == EventType.JOB_FAILED)
    retry_queued = next(
        event
        for event in repo.list_events()
        if event.event_type == EventType.JOB_QUEUED and event.job_id == retry.id
    )
    assert failure.ts <= retry_queued.ts


def test_non_provider_failure_is_not_retried() -> None:
    repo = InMemoryRuntimeRepository()
    repo.enqueue_job(RuntimeJob(target_object_id="submission-1", stage=Stage.INTAKE))

    result = WorkerApp(repo, engine=_InvalidSubmissionEngine()).run_once()

    assert result["retried"] == 0
    assert result["retry_job_id"] is None
    assert repo.queued_jobs() == []
