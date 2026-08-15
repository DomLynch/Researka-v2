from datetime import datetime, timedelta, timezone

import pytest

from contracts import Decision, EventType, ObjectType, ResearchObject, RuntimeEvent, RuntimeJob, Stage
import runtime_core.ops as ops
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


def test_reconciler_skips_completed_terminal_decision() -> None:
    class _NoChildLookupRepo(InMemoryRuntimeRepository):
        def publication_for_target(self, target_object_id: str) -> ResearchObject | None:
            raise AssertionError(f"unexpected child lookup for {target_object_id}")

    repo = _NoChildLookupRepo()
    now = datetime.now(timezone.utc)
    submission = repo.create_object(
        ResearchObject(
            object_type=ObjectType.SUBMISSION,
            title="Terminal revise",
            created_at=now - timedelta(minutes=10),
        )
    )
    repo.record_event(
        RuntimeEvent(
            event_type=EventType.JOB_COMPLETED,
            target_object_id=submission.id,
            payload={"stage": Stage.INTAKE.value, "terminal_decision": Decision.REVISE.value},
            ts=now - timedelta(minutes=5),
        )
    )

    assert reconcile_stalled_submissions(repo, now=now) == []


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
    class _NoPublicationLookupRepo(InMemoryRuntimeRepository):
        def publication_for_target(self, target_object_id: str) -> ResearchObject | None:
            raise AssertionError(f"unexpected publication lookup for {target_object_id}")

    repo = _NoPublicationLookupRepo()
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


def test_reconciler_isolates_one_corrupt_publication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = InMemoryRuntimeRepository()
    now = datetime.now(timezone.utc)
    corrupt = repo.create_object(
        ResearchObject(
            object_type=ObjectType.PUBLICATION,
            title="Corrupt lineage",
            metadata={"publication_state": "PUBLISH_BLOCKED_EXTERNAL"},
            created_at=now - timedelta(minutes=10),
        )
    )
    healthy = repo.create_object(
        ResearchObject(
            object_type=ObjectType.PUBLICATION,
            title="Recoverable delivery",
            metadata={"publication_state": "PUBLISH_BLOCKED_EXTERNAL"},
            created_at=now - timedelta(minutes=10),
        )
    )

    def recover(_repo, publication, *, max_recoveries):  # noqa: ANN001
        assert max_recoveries == 3
        if publication.id == corrupt.id:
            raise ValueError("publication_lineage_invalid")
        return _repo.enqueue_job(
            RuntimeJob(target_object_id=publication.id, stage=Stage.OSF_DEPOSIT)
        )

    monkeypatch.setattr(ops, "recover_publication_delivery", recover)

    repaired = reconcile_stalled_submissions(repo, now=now)

    assert [job.target_object_id for job in repaired] == [healthy.id]


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
    repo.create_object(
        ResearchObject(
            object_type=ObjectType.DECISION,
            parent_object_id=submission.id,
            title="Accept decision",
            metadata={"decision": Decision.ACCEPT.value},
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


def test_operational_alerts_ignore_rejected_and_completed_submissions() -> None:
    repo = InMemoryRuntimeRepository()
    now = datetime.now(timezone.utc)
    submission = repo.create_object(
        ResearchObject(
            object_type=ObjectType.SUBMISSION,
            title="Rejected submission",
            created_at=now - timedelta(days=2),
        )
    )
    repo.create_object(
        ResearchObject(
            object_type=ObjectType.DECISION,
            parent_object_id=submission.id,
            title="Reject decision",
            metadata={"decision": Decision.REJECT.value},
            created_at=now - timedelta(days=2),
        )
    )
    accepted = repo.create_object(
        ResearchObject(
            object_type=ObjectType.SUBMISSION,
            title="Completed accepted submission",
            created_at=now - timedelta(days=2),
        )
    )
    repo.create_object(
        ResearchObject(
            object_type=ObjectType.DECISION,
            parent_object_id=accepted.id,
            title="Accept decision",
            metadata={"decision": Decision.ACCEPT.value},
            created_at=now - timedelta(days=2),
        )
    )
    repo.record_event(
        RuntimeEvent(
            event_type=EventType.JOB_COMPLETED,
            target_object_id=accepted.id,
            payload={"stage": Stage.PUBLISH.value, "deduped": True},
            ts=now - timedelta(days=2),
        )
    )

    assert operational_alerts(repo, now=now) == []


def test_operational_alerts_expose_stalled_publication_delivery() -> None:
    repo = InMemoryRuntimeRepository()
    now = datetime.now(timezone.utc)
    repo.create_object(
        ResearchObject(
            object_type=ObjectType.PUBLICATION,
            title="Visible delivery failure",
            metadata={"publication_state": "PUBLISH_BLOCKED_EXTERNAL"},
            created_at=now - timedelta(days=2),
        )
    )

    alerts = operational_alerts(repo, now=now)

    assert alerts == [
        {
            "code": "publication_delivery_stall",
            "count": 1,
            "since": (now - timedelta(days=2)).isoformat(),
        }
    ]


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
