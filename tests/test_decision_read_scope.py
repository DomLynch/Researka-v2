"""Decision polling must not load unrelated submissions' event payloads."""
from typing import Any, cast

import pytest

from contracts import EventType, ObjectType, ResearchObject, RuntimeEvent, Stage


@pytest.mark.parametrize("outcome", ["pending", "failed", "revise", "reject", "published", "deduped", "blocked"])
def test_decision_read_uses_target_events(client, monkeypatch, outcome):
    repo = cast(Any, client.app).state.repository
    submission = repo.create_object(ResearchObject(object_type=ObjectType.SUBMISSION, title="Scoped decision"))
    other = repo.create_object(ResearchObject(object_type=ObjectType.SUBMISSION, title="Unrelated"))
    decision = None
    if outcome not in {"pending", "failed"}:
        verdict = outcome if outcome in {"revise", "reject"} else "accept"
        decision = repo.create_object(ResearchObject(
            object_type=ObjectType.DECISION, parent_object_id=submission.id, title="Decision",
            metadata={"decision": verdict, "publication_state": "NOT_PUBLISHED"},
        ))
        for artifact in ("old", "current"):
            repo.record_event(RuntimeEvent(
                event_type=EventType.JOB_COMPLETED, target_object_id=submission.id,
                payload={"stage": Stage.EDITORIAL.value, "created_object_id": decision.id,
                         "derivation_web": {"decision_artifact_id": artifact}},
            ))
    if outcome in {"published", "deduped"}:
        publication = repo.create_object(ResearchObject(
            object_type=ObjectType.PUBLICATION, title="Publication",
            parent_object_id=other.id if outcome == "deduped" else submission.id,
            metadata={"publication_state": "PUBLISHED", "public_visibility": "listed"},
        ))
        repo.record_event(RuntimeEvent(
            event_type=EventType.JOB_COMPLETED, target_object_id=submission.id,
            payload={"stage": Stage.PUBLISH.value, "deduped": outcome == "deduped",
                     "publication_id": publication.id},
        ))
    if outcome in {"failed", "blocked"}:
        repo.record_event(RuntimeEvent(
            event_type=EventType.JOB_FAILED, target_object_id=submission.id,
            payload={"stage": Stage.PUBLISH.value if outcome == "blocked" else Stage.REVIEW.value,
                     "terminal": True, "reason": "provider unavailable", "failure_class": "provider_unavailable"},
        ))
    # A later unrelated success must not hide this submission's terminal failure.
    repo.record_event(RuntimeEvent(event_type=EventType.JOB_COMPLETED, target_object_id=other.id,
                                   payload={"stage": Stage.PUBLISH.value}))

    def reject_global_scan():
        pytest.fail("Decision polling loaded the entire event table")

    monkeypatch.setattr(repo, "list_events", reject_global_scan)
    response = client.get(f"/submissions/{submission.id}/decision")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == (outcome if outcome in {"pending", "failed"} else "complete")
    if decision is not None:
        assert body["decision"] == decision.metadata["decision"]
        assert body["dw_artifact_id"] == "current"
        assert body["resubmission"]["allowed"] == (outcome == "revise")
        assert body["publication_status"] == (outcome if outcome in {"published", "deduped", "blocked"} else "not_applicable")
    if outcome in {"failed", "blocked"}:
        assert body["failed_checks"] == ["provider unavailable"]
