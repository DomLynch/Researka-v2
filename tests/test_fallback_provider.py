"""Tests for FallbackProvider — the per-slot reviewer backup.

Use case: MiMo and Gemma each get wrapped with Mistral as a backup so a
transient failure on either reviewer (timeout, 429, 5xx) doesn't drop a
panel slot entirely. A 4xx (auth/quota/bad request) does NOT trigger the
fallback because retrying on a different provider won't fix it.

Observability: when the fallback fires, the response carries
`fallback_used=True` and `fallback_reason=<error_class>` in metadata.
The reviewer panel reads these and surfaces them as
`primary_fallback_used` / `sparring_fallback_used` in the panel
metadata so DW chains record when MiMo or Gemma was actually saved.
"""
from __future__ import annotations

import json

from contracts import ProviderErrorClass, ProviderUsage
from runtime_core.providers import (
    FallbackProvider,
    ProviderError,
    ProviderRequest,
    ProviderResponse,
    ProviderResult,
)
from runtime_core.reviewer_panel import ReviewerPanel


class _StubProvider:
    def __init__(self, *, provider: str, model: str, result: ProviderResult, calls: list[str] | None = None) -> None:
        self.provider = provider
        self.model = model
        self._result = result
        self._calls = calls if calls is not None else []

    def complete(self, request: ProviderRequest) -> ProviderResult:  # noqa: ARG002
        self._calls.append(self.provider)
        return self._result


def _ok_result(model: str, text: str = '{"recommendation":"accept"}') -> ProviderResult:
    return ProviderResult(
        ok=True,
        response=ProviderResponse(
            text=text,
            provider="stub",
            model=model,
            usage=ProviderUsage(input_tokens=1, output_tokens=1, cost_usd=0.0),
        ),
    )


def _err_result(error_class: ProviderErrorClass, message: str = "boom") -> ProviderResult:
    return ProviderResult(
        ok=False,
        error=ProviderError(error_class=error_class, message=message),
    )


def _request() -> ProviderRequest:
    return ProviderRequest(system_prompt="s", user_prompt="u", prompt_version="v")


def test_primary_success_does_not_call_fallback() -> None:
    calls: list[str] = []
    primary = _StubProvider(provider="mimo", model="m", result=_ok_result("m"), calls=calls)
    fallback = _StubProvider(provider="mistral", model="ml", result=_ok_result("ml"), calls=calls)
    wrapper = FallbackProvider(primary=primary, fallback=fallback)
    result = wrapper.complete(_request())
    assert result.ok is True
    assert result.response is not None
    assert result.response.model == "m"
    # Observability tag: primary served the request, fallback_used must be False.
    assert result.response.metadata.get("fallback_used") is False
    assert "fallback_reason" not in result.response.metadata
    assert calls == ["mimo"]


def test_primary_timeout_falls_back() -> None:
    calls: list[str] = []
    primary = _StubProvider(provider="mimo", model="m", result=_err_result(ProviderErrorClass.TIMEOUT), calls=calls)
    fallback = _StubProvider(provider="mistral", model="ml", result=_ok_result("ml"), calls=calls)
    wrapper = FallbackProvider(primary=primary, fallback=fallback)
    result = wrapper.complete(_request())
    assert result.ok is True
    assert result.response is not None
    assert result.response.model == "ml"
    # Observability tag: fallback served the request, reason carries the inner error class.
    assert result.response.metadata.get("fallback_used") is True
    assert result.response.metadata.get("fallback_reason") == "timeout"
    assert calls == ["mimo", "mistral"]


def test_primary_rate_limit_falls_back() -> None:
    calls: list[str] = []
    primary = _StubProvider(provider="gemma", model="g", result=_err_result(ProviderErrorClass.RATE_LIMIT), calls=calls)
    fallback = _StubProvider(provider="mistral", model="ml", result=_ok_result("ml"), calls=calls)
    wrapper = FallbackProvider(primary=primary, fallback=fallback)
    result = wrapper.complete(_request())
    assert result.ok is True
    assert result.response is not None
    assert result.response.model == "ml"
    assert calls == ["gemma", "mistral"]


def test_primary_provider_unavailable_falls_back() -> None:
    calls: list[str] = []
    primary = _StubProvider(
        provider="mimo",
        model="m",
        result=_err_result(ProviderErrorClass.PROVIDER_UNAVAILABLE),
        calls=calls,
    )
    fallback = _StubProvider(provider="mistral", model="ml", result=_ok_result("ml"), calls=calls)
    wrapper = FallbackProvider(primary=primary, fallback=fallback)
    result = wrapper.complete(_request())
    assert result.ok is True
    assert calls == ["mimo", "mistral"]


def test_primary_bad_request_does_NOT_fall_back() -> None:
    """4xx errors (auth, missing key, malformed payload) won't be fixed by trying
    a different provider — short-circuit and surface the original error."""
    calls: list[str] = []
    primary = _StubProvider(
        provider="mimo",
        model="m",
        result=_err_result(ProviderErrorClass.BAD_REQUEST, "mimo_api_key_missing"),
        calls=calls,
    )
    fallback = _StubProvider(provider="mistral", model="ml", result=_ok_result("ml"), calls=calls)
    wrapper = FallbackProvider(primary=primary, fallback=fallback)
    result = wrapper.complete(_request())
    assert result.ok is False
    assert result.error is not None
    assert result.error.error_class is ProviderErrorClass.BAD_REQUEST
    assert calls == ["mimo"]  # fallback NOT called


def test_both_primary_and_fallback_fail_returns_fallback_error() -> None:
    """If the fallback also fails, surface the fallback's error (most recent)."""
    calls: list[str] = []
    primary = _StubProvider(provider="mimo", model="m", result=_err_result(ProviderErrorClass.TIMEOUT), calls=calls)
    fallback = _StubProvider(
        provider="mistral",
        model="ml",
        result=_err_result(ProviderErrorClass.PROVIDER_UNAVAILABLE, "mistral_down"),
        calls=calls,
    )
    wrapper = FallbackProvider(primary=primary, fallback=fallback)
    result = wrapper.complete(_request())
    assert result.ok is False
    assert result.error is not None
    assert result.error.error_class is ProviderErrorClass.PROVIDER_UNAVAILABLE
    assert "mistral_down" in result.error.message
    assert calls == ["mimo", "mistral"]


def test_wrapper_exposes_primary_model_for_panel_string() -> None:
    """The panel composes a `model` string from each slot's `.model`. The wrapper
    should report the primary's model so the panel string stays readable when
    fallback isn't actually used."""
    primary = _StubProvider(provider="mimo", model="mimo-v2.5-pro", result=_ok_result("mimo-v2.5-pro"))
    fallback = _StubProvider(provider="mistral", model="mistralai/mistral-small-2603", result=_ok_result("mistral"))
    wrapper = FallbackProvider(primary=primary, fallback=fallback)
    assert wrapper.model == "mimo-v2.5-pro"


# ---------------------------------------------------------------------------
# Panel-level observability: fallback usage flows into panel metadata
# ---------------------------------------------------------------------------


def _ok_review(recommendation: str) -> ProviderResult:
    """Build a passing reviewer payload (passes the panel's _validate_payload_contract)."""
    payload = {
        "recommendation": recommendation,
        "rubric_scores": {
            "research_question_quality": 4,
            "synthesis_quality": 4,
            "claim_evidence_alignment": 4,
            "limitations_quality": 4,
            "gaps_quality": 4,
            "source_grounding": 4,
        },
        "major_issues": [],
        "minor_issues": [],
        "required_revisions": ["Clarify the bounded claim."] if recommendation == "revise" else [],
        "claim_support_verdict": "supported",
        "overclaim_verdict": "none",
        "synthesis_quality_verdict": "strong",
        "review_markdown": "stub review",
    }
    return ProviderResult(
        ok=True,
        response=ProviderResponse(
            text=json.dumps(payload),
            provider="stub",
            model="stub",
            usage=ProviderUsage(input_tokens=1, output_tokens=1, cost_usd=0.0),
        ),
    )


def test_panel_metadata_marks_primary_fallback_used_when_mimo_times_out() -> None:
    """End-to-end: wrap MiMo with Mistral fallback, force MiMo to timeout,
    verify the panel's metadata exposes primary_fallback_used=True."""
    mimo = _StubProvider(provider="mimo", model="mimo-v2.5-pro", result=_err_result(ProviderErrorClass.TIMEOUT))
    mistral = _StubProvider(
        provider="openrouter",
        model="mistralai/mistral-small-2603",
        result=_ok_review("revise"),
    )
    primary_slot = FallbackProvider(primary=mimo, fallback=mistral)
    sparring_slot = _StubProvider(
        provider="openrouter",
        model="google/gemma-4-31b-it",
        result=_ok_review("revise"),
    )
    fallback_slot = _StubProvider(
        provider="openrouter",
        model="mistralai/mistral-small-2603",
        result=_ok_review("revise"),
    )

    panel = ReviewerPanel(primary=primary_slot, sparring=sparring_slot, fallback=fallback_slot)
    result = panel.complete(_request())

    assert result.ok is True
    assert result.response is not None
    metadata = result.response.metadata
    assert metadata.get("primary_fallback_used") is True
    assert metadata.get("primary_fallback_reason") == "timeout"
    assert metadata.get("sparring_fallback_used") is False


def test_panel_metadata_flags_default_false_when_no_fallback_fires() -> None:
    """Happy path: both slots succeed without fallback. Both flags should be False."""
    mimo_inner = _StubProvider(provider="mimo", model="mimo-v2.5-pro", result=_ok_review("revise"))
    gemma_inner = _StubProvider(
        provider="openrouter",
        model="google/gemma-4-31b-it",
        result=_ok_review("revise"),
    )
    mistral_backup = _StubProvider(
        provider="openrouter",
        model="mistralai/mistral-small-2603",
        result=_ok_review("revise"),
    )
    primary_slot = FallbackProvider(primary=mimo_inner, fallback=mistral_backup)
    sparring_slot = FallbackProvider(primary=gemma_inner, fallback=mistral_backup)
    fallback_slot = _StubProvider(
        provider="openrouter",
        model="mistralai/mistral-small-2603",
        result=_ok_review("revise"),
    )

    panel = ReviewerPanel(primary=primary_slot, sparring=sparring_slot, fallback=fallback_slot)
    result = panel.complete(_request())

    assert result.ok is True
    assert result.response is not None
    metadata = result.response.metadata
    assert metadata.get("primary_fallback_used") is False
    assert metadata.get("sparring_fallback_used") is False
    assert "primary_fallback_reason" not in metadata
    assert "sparring_fallback_reason" not in metadata
