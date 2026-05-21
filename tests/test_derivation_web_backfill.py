from __future__ import annotations

from typing import Any

from contracts import Decision, ObjectType, ResearchObject
from runtime_core.derivation_web import DerivationWebConfig, backfill_missing_publication_chains, emit_publication_chain
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


def test_emit_publication_chain_uses_service_actor_and_metadata(monkeypatch) -> None:
    calls: dict[str, list[dict[str, Any]]] = {"actors": [], "artifacts": [], "steps": []}

    class FakeClient:
        def __init__(self, config: DerivationWebConfig) -> None:
            self.config = config

        def ensure_actor(self, actor_id: str, *, kind: str, name: str) -> None:
            calls["actors"].append({"actor_id": actor_id, "kind": kind, "name": name})

        def create_artifact(
            self,
            *,
            kind: str,
            content_type: str,
            body_text: str,
            metadata: dict[str, Any],
            actor_id: str,
        ) -> dict[str, Any]:
            calls["artifacts"].append(
                {
                    "kind": kind,
                    "content_type": content_type,
                    "body_text": body_text,
                    "metadata": metadata,
                    "actor_id": actor_id,
                }
            )
            return {"id": f"art_{len(calls['artifacts'])}", "content_hash": f"hash_{len(calls['artifacts'])}"}

        def create_step(
            self,
            *,
            step_type: str,
            input_artifact_ids: list[str],
            output_artifact_id: str,
            actor_id: str,
            method: dict[str, Any],
        ) -> dict[str, Any]:
            calls["steps"].append(
                {
                    "step_type": step_type,
                    "input_artifact_ids": input_artifact_ids,
                    "output_artifact_id": output_artifact_id,
                    "actor_id": actor_id,
                    "method": method,
                }
            )
            return {"id": "step_1", "step_hash": "step_hash"}

    monkeypatch.setattr("runtime_core.derivation_web.DerivationWebClient", FakeClient)
    submission = ResearchObject(
        object_type=ObjectType.SUBMISSION,
        title="Memo",
        body_markdown="Submission",
        metadata={
            "author_agent_id": "agent-v4-alpha-memo",
            "human_owner_id": "orcid:0000-0002-1825-0097",
            "human_owner_name": "Dominic Lynch",
            "orcid": "0000-0002-1825-0097",
            "orcid_attribution": "researka_admin_assigned",
            "orcid_verified_at": "2026-05-21T00:00:00+00:00",
        },
    )
    publication = ResearchObject(
        object_type=ObjectType.PUBLICATION,
        parent_object_id=submission.id,
        title="Memo",
        body_markdown="Publication",
        metadata={
            "author_agent_id": "agent-v4-alpha-memo",
            "human_owner_id": "orcid:0000-0002-1825-0097",
            "human_owner_name": "Dominic Lynch",
            "orcid": "0000-0002-1825-0097",
            "orcid_at_publication": "0000-0002-1825-0097",
            "orcid_attribution": "researka_admin_assigned",
            "orcid_verified_at": "2026-05-21T00:00:00+00:00",
            "authors": [{"name": "Dominic Lynch", "orcid": "0000-0002-1825-0097", "role": "author"}],
        },
    )

    result = emit_publication_chain(
        submission=submission,
        publication=publication,
        review=None,
        decision=None,
        config=DerivationWebConfig(base_url="https://dw.test", api_key="test"),
    )

    assert result["dw_artifact_id"] == "art_2"
    assert calls["actors"] == [{"actor_id": "researka:v2", "kind": "system", "name": "Researka v2 gatekeeper"}]
    assert {artifact["actor_id"] for artifact in calls["artifacts"]} == {"researka:v2"}
    assert calls["artifacts"][0]["metadata"]["author_agent_id"] == "agent-v4-alpha-memo"
    assert calls["artifacts"][1]["metadata"]["orcid_at_publication"] == "0000-0002-1825-0097"
    assert calls["steps"][0]["actor_id"] == "researka:v2"
