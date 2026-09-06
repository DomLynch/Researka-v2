import json

import pytest

from contracts import ProviderErrorClass
from runtime_core.judge_release import build_judge_release
from runtime_core.providers import ProviderError, ProviderRequest, ProviderResult
from runtime_core.review_contract import MODEL_QUORUM_POLICY
from runtime_core.reviewer_panel import ReviewerPanel, reviewer_from_env
from tests.test_runtime_core import _ReviewPayloadProvider, _review_payload


def panel(primary="accept", secondary="accept", backup="accept"):
    slots = [
        _ReviewPayloadProvider(model, _review_payload(vote), provider=provider)
        for model, provider, vote in (
            ("gpt-5.6-sol", "codex", primary),
            ("gpt-5.6-terra", "codex", secondary),
            ("z-ai/glm-5.3-flash", "openrouter", backup),
        )
    ]
    return ReviewerPanel(primary=slots[0], sparring=slots[1], fallback=slots[2], quorum_policy=MODEL_QUORUM_POLICY)


def run(subject):
    return subject.complete(ProviderRequest(system_prompt="system", user_prompt="user", prompt_version="test"))


def test_default_is_two_subscription_models_and_glm_backup(monkeypatch):
    monkeypatch.setenv("RESEARKA_V2_PROVIDER", "judge_panel")
    monkeypatch.delenv("RESEARKA_V2_REVIEWER_PRIMARY_PROVIDER", raising=False)
    monkeypatch.setenv("RESEARKA_V2_REVIEW_ATTESTATION_SECRET", "test-key")
    subject = reviewer_from_env()
    assert isinstance(subject, ReviewerPanel)
    assert (subject.primary.model, subject.primary.reasoning_effort) == ("gpt-5.6-sol", "high")
    assert (subject.sparring.model, subject.sparring.reasoning_effort) == ("gpt-5.6-terra", "medium")
    assert subject.fallback.model == "z-ai/glm-5.3-flash"
    assert not subject.allow_sparring_billing_skip


def test_two_valid_gpt_votes_do_not_call_openrouter():
    subject = panel()
    result = run(subject)
    assert result.ok and result.response
    assert subject.fallback.calls == 0
    assert result.response.metadata["accept_quorum_count"] == 2
    assert result.response.metadata["accept_quorum_providers"] == ["codex"]


@pytest.mark.parametrize("failure", [ProviderErrorClass.BILLING, ProviderErrorClass.TIMEOUT, ProviderErrorClass.BAD_REQUEST])
def test_one_failed_gpt_uses_one_validated_backup(monkeypatch, failure):
    subject = panel()
    monkeypatch.setattr(subject.primary, "complete", lambda _: ProviderResult(ok=False, error=ProviderError(error_class=failure, message="codex:failure")))
    result = run(subject)
    assert result.ok and result.response
    assert subject.fallback.calls == 1
    assert result.response.metadata["accept_quorum_models"] == ["gpt-5.6-terra", "z-ai/glm-5.3-flash"]
    assert result.response.metadata["primary_fallback_used"] is True


def test_two_failed_gpt_votes_cannot_be_replaced_by_one_model(monkeypatch):
    subject = panel()
    for slot in (subject.primary, subject.sparring):
        monkeypatch.setattr(slot, "complete", lambda _: ProviderResult(ok=False, error=ProviderError(error_class=ProviderErrorClass.BILLING, message="quota")))
    result = run(subject)
    assert not result.ok
    assert result.error.error_class is ProviderErrorClass.BILLING
    assert subject.fallback.calls == 0


@pytest.mark.parametrize("other", ["revise", "reject"])
def test_disagreement_never_calls_paid_backup(other):
    subject = panel(secondary=other)
    result = run(subject)
    assert subject.fallback.calls == 0
    assert not result.ok or json.loads(result.response.text)["recommendation"] != "accept"


@pytest.mark.parametrize("model", ["gpt-5.6-sol", "unapproved-model"])
def test_duplicate_or_unknown_model_cannot_supply_quorum(model):
    subject = panel()
    subject.sparring.model = model
    assert not run(subject).ok


def test_malformed_gpt_and_malformed_backup_cannot_accept():
    subject = panel()
    subject.primary.payload = {"recommendation": "accept"}
    subject.fallback.payload = {"recommendation": "accept"}
    result = run(subject)
    assert not result.ok and result.error
    assert subject.fallback.calls == 1
    assert "invalid_panel_response:openrouter:z-ai/glm-5.3-flash" in result.error.message


def test_release_binds_policy_and_reasoning_settings():
    subject = panel()
    metadata = run(subject).response.metadata
    args = dict(system_prompt="review", provider=subject.provider, model=subject.model)
    first = build_judge_release(**args, response_metadata=metadata)
    assert first["settings"]["quorum_policy"] == MODEL_QUORUM_POLICY
    assert first["settings"]["fallback_on_disagreement"] is False
    assert first["settings"]["max_output_tokens"] == 12000
    assert first["settings"]["max_input_tokens"] == 200000
    metadata["reviewer_settings"]["primary"]["reasoning_effort"] = "low"
    assert build_judge_release(**args, response_metadata=metadata)["id"] != first["id"]


def test_backup_disagreement_preserves_original_provider_failure(monkeypatch):
    subject = panel(backup="reject")
    monkeypatch.setattr(subject.primary, "complete", lambda _: ProviderResult(
        ok=False, error=ProviderError(error_class=ProviderErrorClass.BILLING, message="codex_subscription_quota_exhausted")
    ))
    result = run(subject)
    assert not result.ok and result.error
    assert "codex_subscription_quota_exhausted" in result.error.message
    assert "openrouter:z-ai/glm-5.3-flash" in result.error.message
    assert subject.fallback.calls == 1
