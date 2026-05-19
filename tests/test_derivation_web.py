from __future__ import annotations

import io
import urllib.error
from email.message import Message

from contracts import ObjectType, ResearchObject
from runtime_core.derivation_web import _post, backfill_missing_publication_chains, emit_decision_to_derivation_web, emit_publication_to_derivation_web
from runtime_core.repos import InMemoryRuntimeRepository


def test_emit_decision_to_derivation_web_posts_actor_artifacts_and_step(monkeypatch):
    calls = []

    def fake_post(path, payload, *, api_key):
        calls.append((path, payload, api_key))
        if path == "/api/artifacts" and payload["kind"] == "source":
            return 201, {"id": "art_submission"}
        if path == "/api/artifacts" and payload["kind"] == "claim":
            return 201, {"id": "art_decision"}
        return 201, {"id": "ok"}

    monkeypatch.setenv("RESEARKA_DW_API_KEY", "dwk_test")
    monkeypatch.setattr("runtime_core.derivation_web._post", fake_post)
    submission = ResearchObject(
        object_type=ObjectType.SUBMISSION,
        title="Submission",
        body_markdown="Submission body",
        metadata={"domain_slug": "longevity", "article_type": "rapid_evidence_synthesis"},
    )
    review = ResearchObject(
        object_type=ObjectType.REVIEW,
        parent_object_id=submission.id,
        title="Review",
        metadata={"recommendation": "revise"},
    )
    decision = ResearchObject(
        object_type=ObjectType.DECISION,
        parent_object_id=submission.id,
        title="Decision",
        metadata={"decision": "revise", "notes": ["revise"], "model": "panel"},
    )

    result = emit_decision_to_derivation_web(submission=submission, review=review, decision=decision)

    assert result["ok"] is True
    assert [path for path, _, _ in calls] == ["/api/actors", "/api/artifacts", "/api/artifacts", "/api/steps"]
    assert calls[-1][1]["input_artifact_ids"] == ["art_submission"]
    assert calls[-1][1]["output_artifact_id"] == "art_decision"
    assert all(api_key == "dwk_test" for _, _, api_key in calls)


def test_emit_decision_to_derivation_web_skips_without_key(monkeypatch):
    monkeypatch.delenv("RESEARKA_DW_API_KEY", raising=False)
    monkeypatch.setenv("RESEARKA_DW_KEY_FILE", "/tmp/missing-researka-dw-key")
    submission = ResearchObject(object_type=ObjectType.SUBMISSION, title="Submission")
    decision = ResearchObject(object_type=ObjectType.DECISION, parent_object_id=submission.id, title="Decision")

    result = emit_decision_to_derivation_web(submission=submission, decision=decision)

    assert result == {"ok": False, "skipped": "missing_key"}


def test_emit_decision_to_derivation_web_includes_fallback_flags(monkeypatch):
    """The DW claim artifact metadata should carry the per-slot fallback flags
    (sourced from the review object) so external auditors can reconstruct
    safety-net usage without database access."""
    captured: list[dict] = []

    def fake_post(path, payload, *, api_key):  # noqa: ARG001
        if path == "/api/artifacts" and payload["kind"] == "claim":
            captured.append(payload["metadata"])
            return 201, {"id": "art_decision"}
        if path == "/api/artifacts" and payload["kind"] == "source":
            return 201, {"id": "art_submission"}
        return 201, {"id": "ok"}

    monkeypatch.setenv("RESEARKA_DW_API_KEY", "dwk_test")
    monkeypatch.setattr("runtime_core.derivation_web._post", fake_post)
    submission = ResearchObject(object_type=ObjectType.SUBMISSION, title="Submission", body_markdown="body")
    review = ResearchObject(
        object_type=ObjectType.REVIEW,
        parent_object_id=submission.id,
        title="Review",
        metadata={
            "recommendation": "revise",
            "primary_fallback_used": True,
            "primary_fallback_reason": "timeout",
            "sparring_fallback_used": False,
            "route": "consensus",
        },
    )
    decision = ResearchObject(
        object_type=ObjectType.DECISION,
        parent_object_id=submission.id,
        title="Decision",
        metadata={"decision": "revise", "notes": ["revise"], "model": "panel"},
    )

    emit_decision_to_derivation_web(submission=submission, review=review, decision=decision)

    assert len(captured) == 1
    metadata = captured[0]
    assert metadata["primary_fallback_used"] is True
    assert metadata["primary_fallback_reason"] == "timeout"
    assert metadata["sparring_fallback_used"] is False
    assert metadata["sparring_fallback_reason"] is None
    assert metadata["panel_route"] == "consensus"


def test_emit_decision_to_derivation_web_defaults_fallback_flags_when_review_missing(monkeypatch):
    """When a review object isn't passed (e.g. early reject path), the flags
    must default to False/None — not blow up."""
    captured: list[dict] = []

    def fake_post(path, payload, *, api_key):  # noqa: ARG001
        if path == "/api/artifacts" and payload["kind"] == "claim":
            captured.append(payload["metadata"])
        if path == "/api/artifacts":
            return 201, {"id": "art_x"}
        return 201, {"id": "ok"}

    monkeypatch.setenv("RESEARKA_DW_API_KEY", "dwk_test")
    monkeypatch.setattr("runtime_core.derivation_web._post", fake_post)
    submission = ResearchObject(object_type=ObjectType.SUBMISSION, title="S", body_markdown="b")
    decision = ResearchObject(
        object_type=ObjectType.DECISION,
        parent_object_id=submission.id,
        title="D",
        metadata={"decision": "reject"},
    )

    emit_decision_to_derivation_web(submission=submission, decision=decision)  # no review arg

    assert len(captured) == 1
    metadata = captured[0]
    assert metadata["primary_fallback_used"] is False
    assert metadata["sparring_fallback_used"] is False
    assert metadata["primary_fallback_reason"] is None
    assert metadata["sparring_fallback_reason"] is None
    assert metadata["panel_route"] is None


def test_emit_decision_to_derivation_web_non_blocking_when_dw_down(monkeypatch):
    """If DW is unreachable, the emit should swallow the error and return ok=False
    with an error message — never raise. Researka's review path must not block on DW."""
    calls = []

    def fake_post_dw_down(path, payload, *, api_key):
        calls.append(path)
        raise urllib.error.URLError("Connection refused: dw.domlynch.com unreachable")

    monkeypatch.setenv("RESEARKA_DW_API_KEY", "dwk_test")
    monkeypatch.setattr("runtime_core.derivation_web._post", fake_post_dw_down)
    submission = ResearchObject(
        object_type=ObjectType.SUBMISSION,
        title="Submission",
        body_markdown="Submission body",
        metadata={"domain_slug": "longevity", "article_type": "rapid_evidence_synthesis"},
    )
    decision = ResearchObject(
        object_type=ObjectType.DECISION,
        parent_object_id=submission.id,
        title="Decision",
        metadata={"decision": "revise", "notes": ["revise"], "model": "panel"},
    )

    # Must not raise.
    result = emit_decision_to_derivation_web(submission=submission, decision=decision)

    assert result["ok"] is False
    assert "error" in result
    assert "Connection refused" in result["error"]
    # The emit should bail on the very first POST (actors), not silently retry the rest.
    assert calls == ["/api/actors"]


def test_dw_post_retries_rate_limits(monkeypatch) -> None:
    calls = []

    class Response:
        status = 201

        def read(self) -> bytes:
            return b'{"id":"ok"}'

        def __enter__(self):
            return self

        def __exit__(self, *args: object) -> None:
            return None

    def fake_urlopen(*_: object, **__: object) -> Response:
        calls.append(1)
        if len(calls) == 1:
            headers = Message()
            headers["Retry-After"] = "0"
            raise urllib.error.HTTPError("url", 429, "Too Many Requests", headers, io.BytesIO())
        return Response()

    monkeypatch.setattr("runtime_core.derivation_web.time.sleep", lambda _: None)
    monkeypatch.setattr("runtime_core.derivation_web.urllib.request.urlopen", fake_urlopen)

    status, body = _post("/api/actors", {"id": "a"}, api_key="dwk_test")

    assert status == 201
    assert body == {"id": "ok"}
    assert len(calls) == 2


def test_emit_publication_to_derivation_web_returns_metadata(monkeypatch):
    calls = []

    def fake_post(path, payload, *, api_key):
        calls.append((path, payload, api_key))
        if path == "/api/artifacts" and payload["metadata"]["researka_object_type"] == ObjectType.SUBMISSION.value:
            return 201, {"id": "art_submission"}
        if path == "/api/artifacts" and payload["metadata"]["researka_object_type"] == ObjectType.PUBLICATION.value:
            return 201, {"id": "art_publication", "content_hash": "abc123"}
        if path == "/api/steps":
            return 201, {"id": "step_publication", "step_hash": "step123"}
        return 201, {"id": "ok"}

    monkeypatch.setenv("RESEARKA_DW_API_KEY", "dwk_test")
    monkeypatch.delenv("RESEARKA_DW_URL", raising=False)
    monkeypatch.setattr("runtime_core.derivation_web._post", fake_post)
    submission = ResearchObject(
        object_type=ObjectType.SUBMISSION,
        title="Submission",
        body_markdown="Submission body",
        metadata={"domain_slug": "general", "article_type": "research_synthesis"},
    )
    publication = ResearchObject(
        object_type=ObjectType.PUBLICATION,
        parent_object_id=submission.id,
        title="Publication",
        body_markdown="Publication body",
        metadata={"article_type": "research_synthesis", "author_agent_id": "publisher-agent"},
    )
    review = ResearchObject(object_type=ObjectType.REVIEW, parent_object_id=submission.id, title="Review")
    decision = ResearchObject(
        object_type=ObjectType.DECISION,
        parent_object_id=submission.id,
        title="Decision",
        metadata={"decision": "accept", "review_id": review.id},
    )

    result = emit_publication_to_derivation_web(
        submission=submission,
        publication=publication,
        review=review,
        decision=decision,
    )

    assert result["dw_status"] == "registered"
    assert result["dw_artifact_id"] == "art_publication"
    assert result["sha256"] == "sha256:abc123"
    assert result["dw_chain_url"] == "https://provenance.researka.org/artifacts/art_publication/chain"
    assert [path for path, _, _ in calls] == ["/api/actors", "/api/artifacts", "/api/artifacts", "/api/steps"]
    assert calls[-1][1]["output_artifact_id"] == "art_publication"


def test_dw_backfill_dry_run_does_not_call_emitter() -> None:
    repo = InMemoryRuntimeRepository()
    submission = repo.create_object(
        ResearchObject(
            object_type=ObjectType.SUBMISSION,
            title="Backfill candidate",
            body_markdown="Submission body",
            metadata={"author_agent_id": "publisher-agent"},
        )
    )
    repo.create_object(
        ResearchObject(
            object_type=ObjectType.PUBLICATION,
            parent_object_id=submission.id,
            title="Backfill candidate",
            body_markdown="Publication body",
            metadata={"author_agent_id": "publisher-agent"},
        )
    )

    def fail_emit(**_: object) -> dict[str, object]:
        raise AssertionError("dry-run must not call Derivation Web")

    summary = backfill_missing_publication_chains(repo, emit_fn=fail_emit)

    assert summary["mode"] == "dry_run"
    assert summary["eligible"] == 1
    assert summary["registered"] == 0
    assert summary["items"][0]["status"] == "would_register"


def test_dw_backfill_apply_persists_returned_metadata() -> None:
    repo = InMemoryRuntimeRepository()
    submission = repo.create_object(ResearchObject(object_type=ObjectType.SUBMISSION, title="Submission"))
    publication = repo.create_object(
        ResearchObject(
            object_type=ObjectType.PUBLICATION,
            parent_object_id=submission.id,
            title="Publication",
            body_markdown="Publication body",
        )
    )

    def fake_emit(**_: object) -> dict[str, object]:
        return {
            "dw_artifact_id": "art_backfilled",
            "dw_chain_url": "https://provenance.researka.org/artifacts/art_backfilled/chain",
            "dw_status": "registered",
            "sha256": "sha256:abc",
        }

    summary = backfill_missing_publication_chains(repo, apply=True, emit_fn=fake_emit)
    updated = repo.get_object(publication.id)

    assert summary["registered"] == 1
    assert updated is not None
    assert updated.metadata["dw_artifact_id"] == "art_backfilled"
    assert updated.metadata["sha256"] == "sha256:abc"


def test_dw_backfill_skips_already_registered_publication() -> None:
    repo = InMemoryRuntimeRepository()
    submission = repo.create_object(ResearchObject(object_type=ObjectType.SUBMISSION, title="Submission"))
    repo.create_object(
        ResearchObject(
            object_type=ObjectType.PUBLICATION,
            parent_object_id=submission.id,
            title="Publication",
            metadata={"dw_artifact_id": "art_existing"},
        )
    )

    summary = backfill_missing_publication_chains(repo, apply=True)

    assert summary["already_registered"] == 1
    assert summary["items"][0]["status"] == "already_registered"


def test_dw_backfill_apply_failure_does_not_mark_registered() -> None:
    repo = InMemoryRuntimeRepository()
    submission = repo.create_object(ResearchObject(object_type=ObjectType.SUBMISSION, title="Submission"))
    publication = repo.create_object(
        ResearchObject(
            object_type=ObjectType.PUBLICATION,
            parent_object_id=submission.id,
            title="Publication",
            body_markdown="Publication body",
        )
    )

    def fail_emit(**_: object) -> dict[str, object]:
        return {"dw_status": "failed", "dw_error": "dw unavailable"}

    summary = backfill_missing_publication_chains(repo, apply=True, emit_fn=fail_emit)
    unchanged = repo.get_object(publication.id)

    assert summary["registered"] == 0
    assert summary["failed"] == 1
    assert unchanged is not None
    assert "dw_artifact_id" not in unchanged.metadata
