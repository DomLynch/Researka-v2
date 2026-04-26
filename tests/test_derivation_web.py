from __future__ import annotations

import urllib.error

from contracts import ObjectType, ResearchObject
from runtime_core.derivation_web import emit_decision_to_derivation_web


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
