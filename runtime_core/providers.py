from __future__ import annotations

import json
import os
import random
import time
import urllib.error
import urllib.request
from typing import Protocol

from pydantic import BaseModel, Field

from contracts import ProviderErrorClass, ProviderUsage


class ProviderRequest(BaseModel):
    system_prompt: str
    user_prompt: str
    prompt_version: str
    timeout_sec: int = 60
    max_input_tokens: int = 12000
    max_output_tokens: int = 1200
    response_format: str | None = None


class ProviderResponse(BaseModel):
    text: str
    provider: str
    model: str
    usage: ProviderUsage
    metadata: dict[str, object] = Field(default_factory=dict)


class ProviderError(BaseModel):
    error_class: ProviderErrorClass
    message: str


class ProviderResult(BaseModel):
    ok: bool
    response: ProviderResponse | None = None
    error: ProviderError | None = None


class LanguageModelProvider(Protocol):
    provider: str
    model: str

    def complete(self, request: ProviderRequest) -> ProviderResult: ...


class DeterministicProvider:
    def __init__(self, *, provider: str = "deterministic-mvp", model: str = "deterministic-mvp") -> None:
        self.provider = provider
        self.model = model

    def complete(self, request: ProviderRequest) -> ProviderResult:
        return ProviderResult(
            ok=True,
            response=ProviderResponse(
                text=json.dumps(
                    {
                        "recommendation": "accept",
                        "rubric_scores": {
                            "research_question_quality": 5,
                            "synthesis_quality": 4,
                            "claim_evidence_alignment": 5,
                            "limitations_quality": 4,
                            "gaps_quality": 4,
                            "source_grounding": 5,
                        },
                        "major_issues": [],
                        "minor_issues": [],
                        "required_revisions": [],
                        "claim_support_verdict": "supported",
                        "overclaim_verdict": "none",
                        "synthesis_quality_verdict": "strong",
                        "review_markdown": "The submission is structurally complete, source-grounded, and acceptable for the MVP gatekeeper path.",
                    }
                ),
                provider=self.provider,
                model=self.model,
                usage=ProviderUsage(input_tokens=0, output_tokens=0, cost_usd=0.0),
            ),
        )


class OpenAICompatibleProvider:
    def __init__(
        self,
        *,
        provider: str,
        model: str,
        api_key: str,
        base_url: str,
        input_cost_per_million: float = 0.0,
        output_cost_per_million: float = 0.0,
        max_attempts: int = 3,
        max_attempts_on_rate_limit: int = 6,
        retry_backoff_seconds: float = 0.25,
        rate_limit_backoff_seconds: float = 2.0,
        rate_limit_backoff_cap_seconds: float = 30.0,
    ) -> None:
        self.provider = provider
        self.model = model
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.input_cost_per_million = input_cost_per_million
        self.output_cost_per_million = output_cost_per_million
        self.max_attempts = max_attempts
        self.max_attempts_on_rate_limit = max_attempts_on_rate_limit
        self.retry_backoff_seconds = retry_backoff_seconds
        self.rate_limit_backoff_seconds = rate_limit_backoff_seconds
        self.rate_limit_backoff_cap_seconds = rate_limit_backoff_cap_seconds

    def complete(self, request: ProviderRequest) -> ProviderResult:
        if not self.api_key:
            return ProviderResult(
                ok=False,
                error=ProviderError(
                    error_class=ProviderErrorClass.BAD_REQUEST,
                    message=f"{self.provider}_api_key_missing",
                ),
            )

        req = urllib.request.Request(
            url=f"{self.base_url}/chat/completions",
            data=json.dumps(self._payload_for(request)).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        # Rate-limit retries get their own (larger) attempt budget so a single
        # 429 burst doesn't spend all retries before the rate-limit window opens.
        attempt = 0
        rate_limit_attempt = 0
        last_error: ProviderError | None = None
        while True:
            try:
                with urllib.request.urlopen(req, timeout=request.timeout_sec) as response:
                    raw = json.loads(response.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as exc:
                error_class = self._classify_status(int(getattr(exc, "code", 0) or 0))
                body = exc.read().decode("utf-8", errors="ignore")
                last_error = ProviderError(error_class=error_class, message=body or str(exc))
                if error_class is ProviderErrorClass.RATE_LIMIT:
                    if rate_limit_attempt >= self.max_attempts_on_rate_limit - 1:
                        return ProviderResult(ok=False, error=last_error)
                    sleep_for = self._rate_limit_sleep(exc, rate_limit_attempt)
                    rate_limit_attempt += 1
                elif self._is_transient(error_class):
                    if attempt >= self.max_attempts - 1:
                        return ProviderResult(ok=False, error=last_error)
                    sleep_for = self._jittered_backoff(self.retry_backoff_seconds, attempt)
                    attempt += 1
                else:
                    return ProviderResult(ok=False, error=last_error)
                time.sleep(sleep_for)
                continue
            except Exception as exc:
                message = str(exc).lower()
                error_class = ProviderErrorClass.TIMEOUT if "timed out" in message else ProviderErrorClass.PROVIDER_UNAVAILABLE
                last_error = ProviderError(error_class=error_class, message=str(exc))
                if not self._is_transient(error_class) or attempt >= self.max_attempts - 1:
                    return ProviderResult(ok=False, error=last_error)
                time.sleep(self._jittered_backoff(self.retry_backoff_seconds, attempt))
                attempt += 1
                continue

        message = (((raw.get("choices") or [{}])[0]).get("message") or {})
        text = str(message.get("content") or message.get("reasoning_content") or "").strip()
        usage = raw.get("usage") or {}
        prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
        completion_tokens = int(usage.get("completion_tokens", 0) or 0)
        cost_usd = round(
            (prompt_tokens / 1_000_000 * self.input_cost_per_million)
            + (completion_tokens / 1_000_000 * self.output_cost_per_million),
            6,
        )
        return ProviderResult(
            ok=True,
            response=ProviderResponse(
                text=text,
                provider=self.provider,
                model=str(raw.get("model") or self.model),
                usage=ProviderUsage(
                    input_tokens=prompt_tokens,
                    output_tokens=completion_tokens,
                    cost_usd=cost_usd,
                ),
            ),
        )

    def _payload_for(self, request: ProviderRequest) -> dict[str, object]:
        payload: dict[str, object] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": request.system_prompt},
                {"role": "user", "content": request.user_prompt},
            ],
            "temperature": 0.1,
            "max_tokens": request.max_output_tokens,
        }
        if request.response_format == "json_object":
            payload["response_format"] = {"type": "json_object"}
        return payload

    def _classify_status(self, status: int) -> ProviderErrorClass:
        if status == 429:
            return ProviderErrorClass.RATE_LIMIT
        if status in {400, 401, 403, 404}:
            return ProviderErrorClass.BAD_REQUEST
        if status in {408, 504}:
            return ProviderErrorClass.TIMEOUT
        if status >= 500:
            return ProviderErrorClass.PROVIDER_UNAVAILABLE
        return ProviderErrorClass.OTHER

    def _is_transient(self, error_class: ProviderErrorClass) -> bool:
        return error_class in {ProviderErrorClass.TIMEOUT, ProviderErrorClass.PROVIDER_UNAVAILABLE}

    def _jittered_backoff(self, base_seconds: float, attempt: int) -> float:
        """Exponential backoff with ±25% jitter to avoid thundering-herd retries."""
        raw = base_seconds * (2**attempt)
        jitter = raw * 0.25 * (2 * random.random() - 1)
        return max(0.0, raw + jitter)

    def _rate_limit_sleep(self, exc: urllib.error.HTTPError, attempt: int) -> float:
        """Sleep duration for a 429.

        Order:
        1. `Retry-After` header (seconds or HTTP-date) if present and parseable
        2. Exponential backoff with jitter, capped at rate_limit_backoff_cap_seconds

        Capped to keep single requests from blocking the worker indefinitely.
        """
        retry_after = getattr(exc, "headers", None)
        if retry_after is not None:
            value = retry_after.get("Retry-After", "").strip()
            if value:
                try:
                    seconds = float(value)
                    return min(max(seconds, 0.0), self.rate_limit_backoff_cap_seconds)
                except ValueError:
                    pass
        backoff = self._jittered_backoff(self.rate_limit_backoff_seconds, attempt)
        return min(backoff, self.rate_limit_backoff_cap_seconds)


class MimoProvider(OpenAICompatibleProvider):
    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str = "mimo-v2.5-pro",
        base_url: str = "https://token-plan-sgp.xiaomimimo.com/v1",
    ) -> None:
        super().__init__(
            provider="mimo",
            model=model,
            api_key=api_key or os.getenv("MIMO_API_KEY", ""),
            base_url=base_url,
            input_cost_per_million=float(os.getenv("RESEARKA_V2_MIMO_INPUT_USD_PER_MILLION", "0")),
            output_cost_per_million=float(os.getenv("RESEARKA_V2_MIMO_OUTPUT_USD_PER_MILLION", "0")),
        )


class OpenRouterProvider(OpenAICompatibleProvider):
    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str = "google/gemma-4-31b-it",
        base_url: str = "https://openrouter.ai/api/v1",
    ) -> None:
        super().__init__(
            provider="openrouter",
            model=model,
            api_key=api_key or os.getenv("OPENROUTER_API_KEY", ""),
            base_url=base_url,
        )


class FallbackProvider:
    """Wraps a primary provider with a fallback that runs only on transient failure.

    Use this in the reviewer panel to add resilience to individual reviewer slots:
    if MiMo or Gemma times out / is rate-limited / returns 5xx, the wrapped fallback
    (typically Mistral) takes over so the panel doesn't lose that slot entirely.

    A 4xx (BAD_REQUEST — including missing API key) is NOT considered transient and
    short-circuits without trying the fallback. A response that's structurally bad
    after parsing (caught by the panel's _validated_result, not us) is also not
    treated as transient — the fallback is for network/availability issues, not for
    "the model said something weird".
    """

    def __init__(self, *, primary: LanguageModelProvider, fallback: LanguageModelProvider) -> None:
        self.primary = primary
        self.fallback = fallback
        self.provider = f"{getattr(primary, 'provider', 'primary')}+fallback"
        self.model = getattr(primary, "model", "primary")

    def complete(self, request: ProviderRequest) -> ProviderResult:
        result = self.primary.complete(request)
        if result.ok:
            return self._tag(result, fallback_used=False)
        if not self._is_transient(result):
            return result
        fallback_result = self.fallback.complete(request)
        return self._tag(fallback_result, fallback_used=True, primary_error=result.error)

    @staticmethod
    def _tag(result: ProviderResult, *, fallback_used: bool, primary_error: ProviderError | None = None) -> ProviderResult:
        """Stamp the response metadata so the panel can surface fallback usage in DW.

        We tag the actual ProviderResponse rather than wrapping the result so the
        existing ResponseValidator / panel logic doesn't need to change shape.
        """
        if not result.ok or result.response is None:
            return result
        new_metadata = {**result.response.metadata, "fallback_used": fallback_used}
        if fallback_used and primary_error is not None:
            new_metadata["fallback_reason"] = primary_error.error_class.value
        # ProviderResponse is a frozen-ish pydantic model; rebuild it.
        result.response.metadata = new_metadata
        return result

    @staticmethod
    def _is_transient(result: ProviderResult) -> bool:
        if result.error is None:
            return False
        return result.error.error_class in {
            ProviderErrorClass.TIMEOUT,
            ProviderErrorClass.PROVIDER_UNAVAILABLE,
            ProviderErrorClass.RATE_LIMIT,
        }


def provider_from_env() -> LanguageModelProvider:
    selected = os.getenv("RESEARKA_V2_PROVIDER", "deterministic").strip().lower()
    if selected == "mimo":
        return MimoProvider(
            model=os.getenv("RESEARKA_V2_MIMO_MODEL", "mimo-v2.5-pro"),
            base_url=os.getenv("RESEARKA_V2_MIMO_BASE_URL", "https://token-plan-sgp.xiaomimimo.com/v1"),
        )
    if selected == "openrouter":
        return OpenRouterProvider(
            model=os.getenv("RESEARKA_V2_OPENROUTER_MODEL", "google/gemma-4-31b-it"),
            base_url=os.getenv("RESEARKA_V2_OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
        )
    return DeterministicProvider()
