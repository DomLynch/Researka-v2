from __future__ import annotations

from typing import Any

from contracts import Decision, ObjectType, ResearchObject
from runtime_core.derivation_web import backfill_missing_publication_chains
from runtime_core.repos import InMemoryRuntimeRepository


def _seed_publication(repo: InMemoryRuntimeRepository) -> tuple[ResearchObject, ResearchObject]:
    submission = repo.create_object(
        ResearchObject(
            object_type=ObjectType.SUBMISSION,
            title="Agent v4 alpha memo",
            body_markdown="Submission body",
            metadata={"author_agent_id": "agent-v4-alpha-memo"},
        )
    )
    review = repo.create_object(
        ResearchObject(
            object_type=ObjectType.REVIEW,
            parent_object_id=submission.id,
            title="Review",
            body_markdown="Review body",
            metadata={"recommendation": "accept"},
        )
    )
    repo.create_object(
        ResearchObject(
            object_type=ObjectType.DECISION,
            parent_object_id=submission.id,
            title="Decision",
            body_markdown="Decision body",
            metadata={"decision": Decision.ACCEPT.value, "review_id": review.id},
        )
    )
    publication = repo.create_object(
        ResearchObject(
            object_type=ObjectType.PUBLICATION,
            parent_object_id=submission.id,
            title="Agent v4 alpha memo",
            body_markdown="Publication body",
            metadata={"author_agent_id": "agent-v4-alpha-memo"},
        )
    )
    return submission, publication


def test_dw_backfill_dry_run_does_not_call_emitter() -> None:
    repo = InMemoryRuntimeRepository()
    _, publication = _seed_publication(repo)

    def fail_emit(**_: Any) -> dict[str, Any]:
        raise AssertionError("dry-run must not call Derivation Web")

    summary = backfill_missing_publication_chains(repo, emit_fn=fail_emit)

    assert summary["mode"] == "dry_run"
    assert summary["eligible"] == 1
    assert summary["registered"] == 0
    assert summary["items"][0]["status"] == "would_register"
    unchanged = repo.get_object(publication.id)
    assert unchanged is not None
    assert unchanged.metadata == {"author_agent_id": "agent-v4-alpha-memo"}


def test_dw_backfill_apply_persists_returned_metadata() -> None:
    repo = InMemoryRuntimeRepository()
    submission, publication = _seed_publication(repo)
    captured: dict[str, Any] = {}

    def fake_emit(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {
            "dw_artifact_id": "art_backfilled",
            "dw_chain_url": "https://provenance.researka.org/artifacts/art_backfilled/chain",
            "dw_api_chain_url": "https://provenance.researka.org/api/artifacts/art_backfilled/chain",
            "dw_source_artifact_id": "art_source",
            "dw_step_id": "step_backfilled",
            "dw_step_hash": "step-hash",
            "dw_status": "registered",
            "content_hash": "sha256:abc",
            "sha256": "sha256:abc",
        }

    summary = backfill_missing_publication_chains(repo, apply=True, emit_fn=fake_emit)
    updated = repo.get_object(publication.id)

    assert summary["registered"] == 1
    assert summary["failed"] == 0
    assert updated is not None
    assert updated.metadata["dw_artifact_id"] == "art_backfilled"
    assert updated.metadata["sha256"] == "sha256:abc"
    assert captured["submission"].id == submission.id
    assert captured["publication"].id == publication.id
    assert captured["review"] is not None
    assert captured["decision"] is not None


def test_dw_backfill_skips_already_registered_publications() -> None:
    repo = InMemoryRuntimeRepository()
    _, publication = _seed_publication(repo)
    repo.update_object_metadata(
        publication.id,
        {**publication.metadata, "dw_artifact_id": "art_existing"},
    )

    summary = backfill_missing_publication_chains(repo, apply=True)

    assert summary["eligible"] == 0
    assert summary["already_registered"] == 1
    assert summary["items"][0]["status"] == "already_registered"


def test_dw_backfill_skips_publication_without_parent() -> None:
    repo = InMemoryRuntimeRepository()
    repo.create_object(
        ResearchObject(
            object_type=ObjectType.PUBLICATION,
            title="Orphan publication",
            body_markdown="Publication body",
            metadata={},
        )
    )

    summary = backfill_missing_publication_chains(repo, apply=True)

    assert summary["eligible"] == 0
    assert summary["skipped"] == 1
    assert summary["items"][0]["status"] == "missing_parent_submission"


def test_dw_backfill_apply_failure_does_not_mark_registered() -> None:
    repo = InMemoryRuntimeRepository()
    _, publication = _seed_publication(repo)

    def fail_emit(**_: Any) -> dict[str, Any]:
        raise RuntimeError("dw unavailable")

    summary = backfill_missing_publication_chains(repo, apply=True, emit_fn=fail_emit)
    unchanged = repo.get_object(publication.id)

    assert summary["registered"] == 0
    assert summary["failed"] == 1
    assert summary["items"][0]["status"] == "failed"
    assert unchanged is not None
    assert "dw_artifact_id" not in unchanged.metadata
