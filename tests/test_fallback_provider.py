"""Tests for FallbackProvider — the per-slot reviewer backup.

Use case: MiMo and Gemma each get wrapped with Mistral as a backup so a
transient failure on either reviewer (timeout, 429, 5xx) doesn't drop a
panel slot entirely. A 4xx (auth/quota/bad request) does NOT trigger the
fallback because retrying on a different provider won't fix it.
"""
from __future__ import annotations


from contracts import ProviderErrorClass, ProviderUsage
from runtime_core.providers import (
    FallbackProvider,
    ProviderError,
    ProviderRequest,
    ProviderResponse,
    ProviderResult,
)


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
