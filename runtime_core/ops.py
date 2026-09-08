from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone

from contracts import Decision, EventType, ObjectType, RuntimeEvent, RuntimeJob, Stage

from .failure_classifier import classify_failure_reason, effective_failure_class
from .repos import RuntimeRepository
from .workflow import recover_publication_delivery


def classify_failure(reason: str):
    return classify_failure_reason(reason)


def summarize_events(events: list[RuntimeEvent]) -> dict[str, int]:
    return dict(Counter(event.event_type.value for event in events))


def submission_lifecycle(repo: RuntimeRepository, submission_id: str) -> dict:
    submission = repo.get_object(submission_id)
    events = repo.events_for_target(submission_id)
    attempts_by_job: dict[str, int] = {}
    stage_counts: Counter[str] = Counter()
    history = []
    for event in events:
        stage = str(event.payload.get("stage") or "")
        job_id = str(event.job_id or "")
        if job_id and job_id not in attempts_by_job:
            stage_counts[stage] += 1
            attempts_by_job[job_id] = stage_counts[stage]
        reason = str(event.payload.get("reason") or "")
        history.append(
            {
                "event": event.event_type.value,
                "stage": stage or None,
                "attempt": attempts_by_job.get(job_id),
                "job_id": job_id or None,
                "at": event.ts.isoformat(),
                "terminal": event.payload.get("terminal"),
                "failure_category": event.payload.get("failure_class"),
                "reason": reason[:500] or None,
            }
        )
    submitted_at = submission.created_at if submission else None
    updated_at = events[-1].ts if events else submitted_at
    return {
        "submitted_at": submitted_at.isoformat() if submitted_at else None,
        "updated_at": updated_at.isoformat() if updated_at else None,
        "current_stage": history[-1]["stage"] if history else None,
        "attempt_count": len(attempts_by_job),
        "attempts": history,
    }


def reconcile_stalled_submissions(
    repo: RuntimeRepository,
    *,
    now: datetime | None = None,
    stale_after_seconds: float = 120.0,
    max_publication_recoveries: int = 3,
) -> list[RuntimeJob]:
    now = now or datetime.now(timezone.utc)
    events = repo.list_events()
    events_by_target: dict[str, list[RuntimeEvent]] = {}
    for event in events:
        events_by_target.setdefault(event.target_object_id, []).append(event)
    active_targets = {job.target_object_id for job in repo.active_jobs()}
    repaired = []

    for submission in repo.list_objects(ObjectType.SUBMISSION, summaries_only=True):
        target_events = sorted(events_by_target.get(submission.id, []), key=lambda event: event.ts)
        last_activity = target_events[-1].ts if target_events else submission.created_at
        if (now - last_activity).total_seconds() < stale_after_seconds:
            continue
        if submission.id in active_targets:
            continue
        if target_events and target_events[-1].event_type == EventType.JOB_FAILED:
            if target_events[-1].payload.get("terminal") is not False:
                continue
        if target_events and target_events[-1].event_type == EventType.JOB_COMPLETED:
            last_payload = target_events[-1].payload
            if last_payload.get("terminal_decision") in {
                Decision.REVISE.value,
                Decision.REJECT.value,
            }:
                continue
        if any(
            event.event_type == EventType.JOB_COMPLETED
            and event.payload.get("stage") == Stage.PUBLISH.value
            for event in target_events
        ) or repo.publication_for_target(submission.id) is not None:
            continue

        decisions = repo.children_of(submission.id, ObjectType.DECISION)
        accepted = decisions[-1] if decisions and decisions[-1].metadata.get("decision") == Decision.ACCEPT.value else None
        if decisions and accepted is None:
            continue
        payload: dict[str, object]
        if accepted is not None:
            stage = Stage.PUBLISH
            payload = {"reconciled": True, "decision_id": accepted.id,
                       "canonical_package_hash": accepted.metadata.get("canonical_package_hash")}
        else:
            reviews = repo.children_of(submission.id, ObjectType.REVIEW)
            if reviews:
                stage = Stage.EDITORIAL
                payload = {"review_id": reviews[-1].id, "reconciled": True}
            elif any(
                event.event_type == EventType.JOB_COMPLETED
                and event.payload.get("stage") == Stage.INTAKE.value
                for event in target_events
            ):
                stage, payload = Stage.REVIEW, {"reconciled": True}
            else:
                stage, payload = Stage.INTAKE, {"reconciled": True}
        repaired.append(repo.enqueue_job(RuntimeJob(target_object_id=submission.id, stage=stage, payload=payload)))
    for publication in repo.list_objects(ObjectType.PUBLICATION, summaries_only=True):
        if publication.id in active_targets:
            continue
        state = publication.metadata.get("publication_state")
        if state not in {"ACCEPTED_QUARANTINED", "PUBLISH_BLOCKED_EXTERNAL", "PUBLISHING"}:
            continue
        target_events = sorted(
            events_by_target.get(publication.id, []), key=lambda event: event.ts
        )
        last_activity = target_events[-1].ts if target_events else publication.created_at
        if (now - last_activity).total_seconds() < stale_after_seconds:
            continue
        try:
            recovered = recover_publication_delivery(
                repo, publication, max_recoveries=max_publication_recoveries
            )
        except (RuntimeError, ValueError):
            continue
        if recovered is not None:
            repaired.append(recovered)
    return repaired


def operational_alerts(
    repo: RuntimeRepository,
    *,
    now: datetime | None = None,
    queue_age_seconds: float = 900.0,
    failure_window_seconds: float = 3600.0,
    failure_threshold: int = 3,
    publication_stall_seconds: float = 86400.0,
) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    alerts: list[dict] = []
    old_jobs = [
        job for job in repo.queued_jobs()
        if (now - job.created_at).total_seconds() >= queue_age_seconds
    ]
    if old_jobs:
        alerts.append({"code": "queue_age", "count": len(old_jobs), "oldest_job_id": old_jobs[0].id})

    cutoff = now - timedelta(seconds=failure_window_seconds)
    events = repo.list_events()
    terminal_failures = [
        event for event in events
        if event.event_type == EventType.JOB_FAILED
        and event.ts >= cutoff
        and event.payload.get("terminal") is not False
    ]
    if len(terminal_failures) >= failure_threshold:
        alerts.append({"code": "repeated_terminal_failures", "count": len(terminal_failures)})
    disagreements = [event.target_object_id for event in terminal_failures
                     if event.payload.get("failure_class") == "review_disagreement"]
    if disagreements:
        alerts.append({"code": "review_adjudication_required", "submission_ids": sorted(set(disagreements))})
    latest_outcomes = {
        (event.target_object_id, event.payload.get("stage")): event
        for event in sorted(events, key=lambda event: event.ts)
        if event.event_type in {EventType.JOB_FAILED, EventType.JOB_COMPLETED}
    }
    login_failures = [event.target_object_id for event in latest_outcomes.values()
                      if event.event_type == EventType.JOB_FAILED
                      and effective_failure_class(event.payload) == "authentication_required"]
    if login_failures:
        alerts.append({"code": "reviewer_authentication_required", "submission_ids": sorted(set(login_failures))})

    decisions = repo.list_objects(ObjectType.DECISION, summaries_only=True)
    publications = repo.list_objects(ObjectType.PUBLICATION, summaries_only=True)
    published_targets = {
        item.parent_object_id
        for item in publications
    } | {
        event.target_object_id
        for event in events
        if event.event_type == EventType.JOB_COMPLETED
        and event.payload.get("stage") == Stage.PUBLISH.value
    }
    latest_decisions = {item.parent_object_id: item for item in decisions}
    stalled_accepts = [
        item
        for target_id, item in latest_decisions.items()
        if target_id not in published_targets
        and item.metadata.get("decision") == Decision.ACCEPT.value
        and (now - item.created_at).total_seconds() >= publication_stall_seconds
    ]
    if stalled_accepts:
        oldest = min(stalled_accepts, key=lambda item: item.created_at)
        alerts.append({"code": "publication_stall", "count": len(stalled_accepts), "since": oldest.created_at.isoformat()})
    stalled_deliveries = [
        item
        for item in publications
        if item.metadata.get("publication_state")
        in {"ACCEPTED_QUARANTINED", "PUBLISH_BLOCKED_EXTERNAL", "PUBLISHING"}
        and (now - item.created_at).total_seconds() >= publication_stall_seconds
    ]
    if stalled_deliveries:
        oldest = min(stalled_deliveries, key=lambda item: item.created_at)
        alerts.append(
            {
                "code": "publication_delivery_stall",
                "count": len(stalled_deliveries),
                "since": oldest.created_at.isoformat(),
            }
        )
    return alerts
