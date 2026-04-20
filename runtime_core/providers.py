from __future__ import annotations

import json
import os
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
    timeout_sec: int = 30
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
        retry_backoff_seconds: float = 0.25,
    ) -> None:
        self.provider = provider
        self.model = model
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.input_cost_per_million = input_cost_per_million
        self.output_cost_per_million = output_cost_per_million
        self.max_attempts = max_attempts
        self.retry_backoff_seconds = retry_backoff_seconds

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
        for attempt in range(self.max_attempts):
            try:
                with urllib.request.urlopen(req, timeout=request.timeout_sec) as response:
                    raw = json.loads(response.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as exc:
                error_class = self._classify_status(int(getattr(exc, "code", 0) or 0))
                body = exc.read().decode("utf-8", errors="ignore")
                if self._should_retry(error_class, attempt):
                    time.sleep(self.retry_backoff_seconds * (2**attempt))
                    continue
                return ProviderResult(ok=False, error=ProviderError(error_class=error_class, message=body or str(exc)))
            except Exception as exc:
                message = str(exc).lower()
                error_class = ProviderErrorClass.TIMEOUT if "timed out" in message else ProviderErrorClass.PROVIDER_UNAVAILABLE
                if self._should_retry(error_class, attempt):
                    time.sleep(self.retry_backoff_seconds * (2**attempt))
                    continue
                return ProviderResult(ok=False, error=ProviderError(error_class=error_class, message=str(exc)))
        else:
            return ProviderResult(
                ok=False,
                error=ProviderError(
                    error_class=ProviderErrorClass.PROVIDER_UNAVAILABLE,
                    message=f"{self.provider}_retry_exhausted",
                ),
            )

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

    def _should_retry(self, error_class: ProviderErrorClass, attempt: int) -> bool:
        return attempt < self.max_attempts - 1 and error_class in {
            ProviderErrorClass.TIMEOUT,
            ProviderErrorClass.PROVIDER_UNAVAILABLE,
        }


class MiniMaxProvider(OpenAICompatibleProvider):
    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str = "MiniMax-M2.7-highspeed",
        base_url: str = "https://api.minimax.io/v1",
    ) -> None:
        super().__init__(
            provider="minimax",
            model=model,
            api_key=api_key or os.getenv("MINIMAX_API_KEY", ""),
            base_url=base_url,
            input_cost_per_million=0.6,
            output_cost_per_million=2.4,
        )


class MimoProvider(OpenAICompatibleProvider):
    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str = "mimo-v2-pro",
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


class DeepSeekProvider(OpenAICompatibleProvider):
    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str = "deepseek-reasoner",
        base_url: str = "https://api.deepseek.com/v1",
    ) -> None:
        super().__init__(
            provider="deepseek",
            model=model,
            api_key=api_key or os.getenv("DEEPSEEK_API_KEY", ""),
            base_url=base_url,
            input_cost_per_million=0.27,
            output_cost_per_million=1.10,
        )


def provider_from_env() -> LanguageModelProvider:
    selected = os.getenv("RESEARKA_V2_PROVIDER", "deterministic").strip().lower()
    if selected == "minimax":
        return MiniMaxProvider(
            model=os.getenv("RESEARKA_V2_MINIMAX_MODEL", "MiniMax-M2.7-highspeed"),
            base_url=os.getenv("RESEARKA_V2_MINIMAX_BASE_URL", "https://api.minimax.io/v1"),
        )
    if selected == "mimo":
        return MimoProvider(
            model=os.getenv("RESEARKA_V2_MIMO_MODEL", "mimo-v2-pro"),
            base_url=os.getenv("RESEARKA_V2_MIMO_BASE_URL", "https://token-plan-sgp.xiaomimimo.com/v1"),
        )
    if selected == "deepseek":
        return DeepSeekProvider(
            model=os.getenv("RESEARKA_V2_DEEPSEEK_MODEL", "deepseek-reasoner"),
            base_url=os.getenv("RESEARKA_V2_DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"),
        )
    return DeterministicProvider()
