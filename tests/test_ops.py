from datetime import datetime, timedelta, timezone

from contracts import Decision, EventType, ObjectType, ResearchObject, RuntimeEvent, RuntimeJob, Stage
from runtime_core.ops import operational_alerts, reconcile_stalled_submissions, submission_lifecycle
from runtime_core.repos import InMemoryRuntimeRepository


def test_reconciler_restores_missing_review_job() -> None:
    repo = InMemoryRuntimeRepository()
    now = datetime.now(timezone.utc)
    submission = repo.create_object(
        ResearchObject(
            object_type=ObjectType.SUBMISSION,
            title="Interrupted submission",
            created_at=now - timedelta(minutes=10),
        )
    )
    intake = repo.enqueue_job(RuntimeJob(target_object_id=submission.id, stage=Stage.INTAKE))
    repo.complete_job(intake.id)
    repo.record_event(
        RuntimeEvent(
            event_type=EventType.JOB_COMPLETED,
            target_object_id=submission.id,
            job_id=intake.id,
            payload={"stage": Stage.INTAKE.value},
            ts=now,
        )
    )

    repaired = reconcile_stalled_submissions(repo, now=now + timedelta(minutes=3))

    assert len(repaired) == 1
    assert repaired[0].stage == Stage.REVIEW
    assert repaired[0].payload["reconciled"] is True


def test_reconciler_does_not_restart_terminal_failure() -> None:
    repo = InMemoryRuntimeRepository()
    now = datetime.now(timezone.utc)
    submission = repo.create_object(
        ResearchObject(
            object_type=ObjectType.SUBMISSION,
            title="Terminal submission",
            created_at=now - timedelta(minutes=10),
        )
    )
    repo.record_event(
        RuntimeEvent(
            event_type=EventType.JOB_FAILED,
            target_object_id=submission.id,
            payload={"stage": Stage.REVIEW.value, "terminal": True},
            ts=now - timedelta(minutes=5),
        )
    )

    assert reconcile_stalled_submissions(repo, now=now) == []
    assert repo.queued_jobs() == []


def test_reconciler_restores_missing_editorial_job() -> None:
    repo = InMemoryRuntimeRepository()
    now = datetime.now(timezone.utc)
    submission = repo.create_object(
        ResearchObject(
            object_type=ObjectType.SUBMISSION,
            title="Reviewed submission",
            created_at=now - timedelta(minutes=10),
        )
    )
    review = repo.create_object(
        ResearchObject(
            object_type=ObjectType.REVIEW,
            parent_object_id=submission.id,
            title="Completed review",
            created_at=now - timedelta(minutes=5),
        )
    )

    repaired = reconcile_stalled_submissions(repo, now=now)

    assert repaired[0].stage == Stage.EDITORIAL
    assert repaired[0].payload["review_id"] == review.id


def test_reconciler_restores_missing_publish_job() -> None:
    repo = InMemoryRuntimeRepository()
    now = datetime.now(timezone.utc)
    submission = repo.create_object(
        ResearchObject(
            object_type=ObjectType.SUBMISSION,
            title="Accepted submission",
            created_at=now - timedelta(minutes=10),
        )
    )
    repo.create_object(
        ResearchObject(
            object_type=ObjectType.DECISION,
            parent_object_id=submission.id,
            title="Accept",
            metadata={"decision": Decision.ACCEPT.value},
            created_at=now - timedelta(minutes=5),
        )
    )

    repaired = reconcile_stalled_submissions(repo, now=now)

    assert repaired[0].stage == Stage.PUBLISH


def test_reconciler_does_not_duplicate_active_lease() -> None:
    repo = InMemoryRuntimeRepository()
    now = datetime.now(timezone.utc)
    submission = repo.create_object(
        ResearchObject(
            object_type=ObjectType.SUBMISSION,
            title="Actively processing",
            created_at=now - timedelta(minutes=10),
        )
    )
    job = repo.enqueue_job(RuntimeJob(target_object_id=submission.id, stage=Stage.INTAKE))
    assert repo.claim_next_job(target_object_id=submission.id) == job

    assert reconcile_stalled_submissions(repo, now=now) == []
    assert len(repo.jobs) == 1


def test_reconciler_respects_latest_non_accept_decision() -> None:
    repo = InMemoryRuntimeRepository()
    now = datetime.now(timezone.utc)
    submission = repo.create_object(
        ResearchObject(
            object_type=ObjectType.SUBMISSION,
            title="Revised after provisional accept",
            created_at=now - timedelta(minutes=10),
        )
    )
    repo.create_object(
        ResearchObject(
            object_type=ObjectType.DECISION,
            parent_object_id=submission.id,
            title="Provisional accept",
            metadata={"decision": Decision.ACCEPT.value},
            created_at=now - timedelta(minutes=6),
        )
    )
    repo.create_object(
        ResearchObject(
            object_type=ObjectType.DECISION,
            parent_object_id=submission.id,
            title="Final revise",
            metadata={"decision": Decision.REVISE.value},
            created_at=now - timedelta(minutes=5),
        )
    )

    assert reconcile_stalled_submissions(repo, now=now) == []
    assert repo.queued_jobs() == []


def test_reconciler_does_not_republish_completed_deduped_job() -> None:
    repo = InMemoryRuntimeRepository()
    now = datetime.now(timezone.utc)
    submission = repo.create_object(
        ResearchObject(
            object_type=ObjectType.SUBMISSION,
            title="Deduped publish",
            created_at=now - timedelta(minutes=10),
        )
    )
    repo.create_object(
        ResearchObject(
            object_type=ObjectType.DECISION,
            parent_object_id=submission.id,
            title="Accept",
            metadata={"decision": Decision.ACCEPT.value},
            created_at=now - timedelta(minutes=5),
        )
    )
    repo.record_event(
        RuntimeEvent(
            event_type=EventType.JOB_COMPLETED,
            target_object_id=submission.id,
            payload={"stage": Stage.PUBLISH.value, "deduped": True},
            ts=now - timedelta(minutes=4),
        )
    )

    assert reconcile_stalled_submissions(repo, now=now) == []
    assert repo.queued_jobs() == []


def test_operational_alerts_cover_queue_failures_and_publication_stall() -> None:
    repo = InMemoryRuntimeRepository()
    now = datetime.now(timezone.utc)
    submission = repo.create_object(
        ResearchObject(
            object_type=ObjectType.SUBMISSION,
            title="Stalled submission",
            created_at=now - timedelta(days=2),
        )
    )
    repo.enqueue_job(
        RuntimeJob(
            target_object_id=submission.id,
            stage=Stage.REVIEW,
            created_at=now - timedelta(hours=1),
        )
    )
    for index in range(3):
        repo.record_event(
            RuntimeEvent(
                event_type=EventType.JOB_FAILED,
                target_object_id=submission.id,
                job_id=f"failed-{index}",
                payload={"stage": Stage.REVIEW.value, "terminal": True},
                ts=now - timedelta(minutes=5),
            )
        )

    alerts = operational_alerts(repo, now=now)

    assert {alert["code"] for alert in alerts} == {
        "queue_age",
        "repeated_terminal_failures",
        "publication_stall",
    }


def test_submission_lifecycle_reports_attempt_timestamps() -> None:
    repo = InMemoryRuntimeRepository()
    submission = ResearchObject(object_type=ObjectType.SUBMISSION, title="Visible lifecycle")
    job = RuntimeJob(target_object_id=submission.id, stage=Stage.INTAKE)
    repo.create_object_and_enqueue_job(submission, job)

    lifecycle = submission_lifecycle(repo, submission.id)

    assert lifecycle["submitted_at"] == submission.created_at.isoformat()
    assert lifecycle["current_stage"] == Stage.INTAKE.value
    assert lifecycle["attempt_count"] == 1
    assert lifecycle["attempts"][0]["event"] == EventType.JOB_QUEUED.value
    assert lifecycle["attempts"][0]["at"]
