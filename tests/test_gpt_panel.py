import json

import pytest

from contracts import ArticleType, ProviderErrorClass
from runtime_core.judge_release import build_judge_release
from runtime_core.providers import OpenRouterProvider, ProviderError, ProviderRequest, ProviderResult
from runtime_core.review_contract import MODEL_QUORUM_POLICY
from runtime_core.reviewer_panel import ReviewerPanel, reviewer_from_env
from runtime_core.workflow import WorkflowEngine
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


@pytest.mark.parametrize("cost", [0.0132, 0, None, "0.01", -1, float("nan"), float("inf"), True, 10**400])
def test_backup_cost_comes_from_provider_receipt(cost):
    subject = panel()
    raw = {"id": "test-generation", "model": "z-ai/glm-5.3-flash",
           "choices": [{"message": {"content": json.dumps(_review_payload("revise"))}}],
           "usage": {"prompt_tokens": 74206, "completion_tokens": 10459, "cost": cost}}
    result = OpenRouterProvider(model="z-ai/glm-5.3-flash")._result_from_raw(raw)
    receipt = subject._reviewer_receipt(result)
    assert receipt["billing"] == "openrouter_credits"
    assert receipt["generation_id"] == "test-generation"
    if type(cost) in (float, int) and cost in (0.0132, 0):
        assert receipt["usage"]["cost_usd"] == cost
        assert receipt["cost_source"] == "provider_reported"
    else:
        assert receipt["cost_source"] == "unreported"


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


@pytest.mark.parametrize("article_type", list(ArticleType))
def test_every_article_type_uses_the_same_repairability_and_grounding_rules(article_type):
    prompt = WorkflowEngine()._review_system_prompt(article_type.value)
    assert prompt.count("Apply one repairability rule to every section and table") == 1
    assert "Reject when at least one demonstrated material defect requires new evidence" in prompt
    assert "A title correction or reclassification is not itself a scope reset" in prompt
    assert "verbatim quote for an incorrect statement" in prompt
    assert "use kind=omission for missing content, without inventing a quote" in prompt
    assert "Hedging does not excuse contradicted claims" in prompt
    assert "3 if claims are supported but hedged" not in prompt
    assert "accept = all scores >= 4, zero major_issues" in prompt
    assert "Reject when the memo is source-free, hype-framed" not in prompt
    assert "Reject when findings are unsourced, fabricated" not in prompt
    if article_type in {ArticleType.ALPHA_MEMO, ArticleType.EVIDENCE_MAP}:
        assert "shared repairability rule" in prompt


@pytest.mark.parametrize("scenario", ["agree", "primary_failed", "secondary_failed", "swap_verdicts"])
def test_review_slots_receive_identical_section_briefs_and_bounded_adjudication(monkeypatch, scenario):
    subject = panel(primary="revise", secondary="reject") if scenario == "swap_verdicts" else panel()
    captured = {name: [] for name in ("primary", "sparring", "fallback")}
    for name in captured:
        slot = getattr(subject, name)
        original = slot.complete

        def complete(request, name=name, slot=slot, original=original):
            captured[name].append(request.model_dump())
            if (name, scenario) in {("primary", "primary_failed"), ("sparring", "secondary_failed")}:
                return ProviderResult(ok=False, error=ProviderError(error_class=ProviderErrorClass.TIMEOUT, message="timeout"))
            if scenario == "swap_verdicts" and len(captured[name]) == 2:
                slot.payload = _review_payload("reject" if name == "primary" else "revise")
            return original(request)

        monkeypatch.setattr(slot, "complete", complete)
    sections = {name: f"{name} evidence [1]." for name in (
        "Abstract", "Introduction", "Methods", "Results", "Discussion", "Limitations", "Conclusion",
        "Background", "Inferential Bridge", "Quantitative Evidence Index", "Cross-Domain Synthesis",
    )}
    request = ProviderRequest(system_prompt=WorkflowEngine()._review_system_prompt("research_synthesis"),
                              user_prompt=json.dumps({"sections": sections}), prompt_version="test")
    result = subject.complete(request)
    first = captured["primary"][0]
    assert first == captured["sparring"][0]
    assert json.loads(first["user_prompt"])["sections"] == sections
    assert first["system_prompt"] == request.system_prompt
    if scenario.endswith("failed"):
        assert captured["fallback"] == [first]
    else:
        assert captured["fallback"] == []
    if scenario == "swap_verdicts":
        assert len(captured["primary"]) == len(captured["sparring"]) == 2
        focused = captured["primary"][1]
        assert focused == captured["sparring"][1]
        assert focused["user_prompt"].startswith(request.user_prompt)
        assert "Compare their material_findings item by item" in focused["system_prompt"]
        assert "explain any change from your prior decision" in focused["system_prompt"]
        assert not result.ok and result.error.message.startswith("review_disagreement:")
        trail = json.loads(result.error.message.split(":", 1)[1])
        assert [row["recommendation"] for row in trail["prior_reviews"]] == ["revise", "reject"]
        assert [row["recommendation"] for row in trail["final_reviews"]] == ["reject", "revise"]
    else:
        assert result.ok and result.response.metadata["decision_quorum_count"] == 2
