"""Retry/backoff tests for OpenAICompatibleProvider.

Pilot day-1 hit a class of failure where 3 of 5 review jobs died at
autonomous_review when fired concurrently — OpenRouter free-tier 429s
that the previous 3-attempt / 0.25s backoff couldn't ride out.
These tests cover the new behaviour:

- Rate limits get a separate, larger attempt budget
- `Retry-After` header is honoured when present
- Other transient errors still use the standard 3-attempt budget
- Non-transient errors (e.g. 400) fail fast with no retries
"""
from __future__ import annotations

from email.message import Message
import json
import urllib.error

import pytest

from contracts import ProviderErrorClass
from runtime_core.providers import MiniMaxProvider, OpenAICompatibleProvider, ProviderRequest


class _RecordingHttpError(urllib.error.HTTPError):
    def __init__(self, code: int, retry_after: str | None = None) -> None:
        headers = Message()
        if retry_after:
            headers["Retry-After"] = retry_after
        super().__init__(
            url="http://example.test/v1/chat/completions",
            code=code,
            msg=f"http {code}",
            hdrs=headers,
            fp=None,
        )

    def read(self, *_args: object, **_kwargs: object) -> bytes:
        return b'{"error":"forced"}'


def _make_provider(**overrides: object) -> OpenAICompatibleProvider:
    defaults: dict[str, object] = {
        "provider": "test",
        "model": "test-model",
        "api_key": "test-key",
        "base_url": "http://example.test/v1",
        "max_attempts": 3,
        "max_attempts_on_rate_limit": 5,
        "retry_backoff_seconds": 0.0,
        "rate_limit_backoff_seconds": 0.0,
        "rate_limit_backoff_cap_seconds": 0.0,
    }
    defaults.update(overrides)
    return OpenAICompatibleProvider(**defaults)  # type: ignore[arg-type]


def _patch_urlopen(monkeypatch: pytest.MonkeyPatch, side_effects: list[object]) -> list[int]:
    """Monkey-patch urllib.request.urlopen to raise the next side effect each call.

    Returns a counter list so tests can assert call count.
    """
    counter = [0]

    def fake_urlopen(*_args: object, **_kwargs: object) -> object:
        idx = counter[0]
        counter[0] += 1
        effect = side_effects[idx]
        if isinstance(effect, BaseException):
            raise effect
        return effect

    monkeypatch.setattr("runtime_core.providers.urllib.request.urlopen", fake_urlopen)
    return counter


def _request() -> ProviderRequest:
    return ProviderRequest(system_prompt="s", user_prompt="u", prompt_version="v")


class _JsonResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def __enter__(self) -> "_JsonResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


def test_rate_limit_uses_dedicated_budget_separate_from_other_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """A burst of 4 consecutive 429s should still retry — old 3-attempt budget would have given up after 2."""
    counter = _patch_urlopen(
        monkeypatch,
        [
            _RecordingHttpError(429),
            _RecordingHttpError(429),
            _RecordingHttpError(429),
            _RecordingHttpError(429),
            _RecordingHttpError(429),
        ],
    )
    provider = _make_provider(max_attempts=3, max_attempts_on_rate_limit=5)
    result = provider.complete(_request())
    assert result.ok is False
    assert result.error is not None
    assert result.error.error_class is ProviderErrorClass.RATE_LIMIT
    # 5 attempts on rate limit budget, all consumed
    assert counter[0] == 5


def test_retry_after_header_is_honoured(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the server sends `Retry-After: <seconds>`, we sleep that long (capped)."""
    sleeps: list[float] = []
    monkeypatch.setattr("runtime_core.providers.time.sleep", lambda s: sleeps.append(s))
    _patch_urlopen(monkeypatch, [_RecordingHttpError(429, retry_after="7"), _RecordingHttpError(429, retry_after="3")])
    provider = _make_provider(max_attempts_on_rate_limit=2, rate_limit_backoff_cap_seconds=10.0)
    provider.complete(_request())
    assert sleeps == [7.0]  # Only one sleep before the second attempt fails out


def test_retry_after_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    """A wildly large `Retry-After` value is clamped to the configured cap."""
    sleeps: list[float] = []
    monkeypatch.setattr("runtime_core.providers.time.sleep", lambda s: sleeps.append(s))
    _patch_urlopen(monkeypatch, [_RecordingHttpError(429, retry_after="3600"), _RecordingHttpError(429)])
    provider = _make_provider(max_attempts_on_rate_limit=2, rate_limit_backoff_cap_seconds=5.0)
    provider.complete(_request())
    assert sleeps == [5.0]


def test_bad_request_does_not_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    """400/401/403/404 are caller errors — never retry."""
    counter = _patch_urlopen(monkeypatch, [_RecordingHttpError(400)])
    provider = _make_provider()
    result = provider.complete(_request())
    assert result.ok is False
    assert result.error is not None
    assert result.error.error_class is ProviderErrorClass.BAD_REQUEST
    assert counter[0] == 1


def test_payment_required_is_billing_and_does_not_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    counter = _patch_urlopen(monkeypatch, [_RecordingHttpError(402)])

    result = _make_provider().complete(_request())

    assert result.ok is False
    assert result.error is not None
    assert result.error.error_class is ProviderErrorClass.BILLING
    assert result.error.status_code == 402
    assert counter[0] == 1


def test_5xx_uses_standard_attempt_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """500-class errors retry up to max_attempts, NOT max_attempts_on_rate_limit."""
    counter = _patch_urlopen(monkeypatch, [_RecordingHttpError(503), _RecordingHttpError(503), _RecordingHttpError(503)])
    provider = _make_provider(max_attempts=3, max_attempts_on_rate_limit=10)
    result = provider.complete(_request())
    assert result.ok is False
    assert result.error is not None
    assert result.error.error_class is ProviderErrorClass.PROVIDER_UNAVAILABLE
    assert counter[0] == 3


def test_minimax_provider_uses_anthropic_messages_api(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def fake_urlopen(req: object, **_kwargs: object) -> _JsonResponse:
        request = req  # urllib Request, kept as object to avoid private typing.
        seen["url"] = getattr(request, "full_url")
        seen["payload"] = json.loads(getattr(request, "data").decode("utf-8"))
        seen["api_key"] = request.get_header("X-api-key")  # type: ignore[attr-defined]
        return _JsonResponse(
            {
                "model": "MiniMax-M3",
                "content": [{"type": "text", "text": "{\"recommendation\":\"accept\"}"}],
                "usage": {"input_tokens": 12, "output_tokens": 5},
            }
        )

    monkeypatch.setattr("runtime_core.providers.urllib.request.urlopen", fake_urlopen)

    result = MiniMaxProvider(api_key="test-key").complete(_request())

    assert result.ok is True
    assert result.response is not None
    assert seen["url"] == "https://api.minimax.io/anthropic/v1/messages"
    assert seen["api_key"] == "test-key"
    assert seen["payload"] == {
        "model": "MiniMax-M3",
        "system": "s",
        "messages": [{"role": "user", "content": [{"type": "text", "text": "u"}]}],
        "temperature": 0.1,
        "max_tokens": 1200,
        "thinking": {"type": "disabled"},
    }
    assert result.response.provider == "minimax"
    assert result.response.model == "MiniMax-M3"
    assert result.response.text == "{\"recommendation\":\"accept\"}"
    assert result.response.usage.input_tokens == 12
    assert result.response.usage.output_tokens == 5
