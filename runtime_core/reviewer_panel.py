from __future__ import annotations

import json
import os

from contracts import ProviderErrorClass, ProviderUsage

from .providers import (
    DeepSeekProvider,
    DeterministicProvider,
    LanguageModelProvider,
    MimoProvider,
    ProviderError,
    ProviderRequest,
    ProviderResponse,
    ProviderResult,
)

REVIEW_RUBRIC_KEYS = (
    "research_question_quality",
    "synthesis_quality",
    "claim_evidence_alignment",
    "limitations_quality",
    "gaps_quality",
    "source_grounding",
)
CLAIM_SUPPORT_VERDICTS = {"supported", "partially_supported", "unsupported"}
OVERCLAIM_VERDICTS = {"none", "mild", "significant"}
SYNTHESIS_QUALITY_VERDICTS = {"strong", "adequate", "weak", "empty"}


class ReviewerPanel:
    provider = "reviewer-panel"

    def __init__(
        self,
        *,
        primary: LanguageModelProvider,
        sparring: LanguageModelProvider,
        fallback: LanguageModelProvider,
    ) -> None:
        self.primary = primary
        self.sparring = sparring
        self.fallback = fallback
        self.model = f"{getattr(primary, 'model', 'primary')}|{getattr(sparring, 'model', 'sparring')}|{getattr(fallback, 'model', 'fallback')}"

    def complete(self, request: ProviderRequest) -> ProviderResult:
        primary = self._validated_result(self.primary.complete(request))
        sparring = self._validated_result(self.sparring.complete(request))

        if primary.ok and sparring.ok:
            primary_rec = self._recommendation_from(primary)
            sparring_rec = self._recommendation_from(sparring)
            if primary_rec == sparring_rec:
                return self._panel_response(
                    winner=primary,
                    route="consensus",
                    used=[primary, sparring],
                    metadata={
                        "primary_recommendation": primary_rec,
                        "sparring_recommendation": sparring_rec,
                        "consensus": True,
                    },
                )
            fallback = self._validated_result(self.fallback.complete(request))
            if not fallback.ok:
                return self._combined_error("panel_disagreement_fallback_failed", primary, sparring, fallback)
            return self._panel_response(
                winner=fallback,
                route="fallback_tiebreak",
                used=[primary, sparring, fallback],
                metadata={
                    "primary_recommendation": primary_rec,
                    "sparring_recommendation": sparring_rec,
                    "consensus": False,
                    "escalated_to_fallback": True,
                },
            )

        if primary.ok and not sparring.ok:
            return self._panel_response(
                winner=primary,
                route="sparring_failed_primary_used",
                used=[primary],
                metadata={
                    "ops_flag": "sparring_failed",
                    "sparring_error": self._error_text(sparring),
                },
            )

        if sparring.ok and not primary.ok:
            return self._panel_response(
                winner=sparring,
                route="primary_failed_sparring_used",
                used=[sparring],
                metadata={
                    "ops_flag": "primary_failed",
                    "primary_error": self._error_text(primary),
                },
            )

        fallback = self._validated_result(self.fallback.complete(request))
        if not fallback.ok:
            return self._combined_error("panel_all_failed", primary, sparring, fallback)
        return self._panel_response(
            winner=fallback,
            route="fallback_after_primary_and_sparring_failure",
            used=[fallback],
            metadata={
                "ops_flag": "both_reviewers_failed",
                "primary_error": self._error_text(primary),
                "sparring_error": self._error_text(sparring),
                "escalated_to_fallback": True,
            },
        )

    def _panel_response(
        self,
        *,
        winner: ProviderResult,
        route: str,
        used: list[ProviderResult],
        metadata: dict[str, object],
    ) -> ProviderResult:
        assert winner.response is not None
        usage = ProviderUsage(
            input_tokens=sum(result.response.usage.input_tokens for result in used if result.response),
            output_tokens=sum(result.response.usage.output_tokens for result in used if result.response),
            cost_usd=round(sum(result.response.usage.cost_usd for result in used if result.response), 6),
        )
        route_metadata = {
            "route": route,
            "winner_provider": winner.response.provider,
            "winner_model": winner.response.model,
            **metadata,
        }
        return ProviderResult(
            ok=True,
            response=ProviderResponse(
                text=winner.response.text,
                provider=self.provider,
                model=self.model,
                usage=usage,
                metadata=route_metadata,
            ),
        )

    def _combined_error(self, reason: str, *results: ProviderResult) -> ProviderResult:
        return ProviderResult(
            ok=False,
            error=ProviderError(
                error_class=ProviderErrorClass.PROVIDER_UNAVAILABLE,
                message=f"{reason}:{' | '.join(self._error_text(result) for result in results)}",
            ),
        )

    def _validated_result(self, result: ProviderResult) -> ProviderResult:
        if not result.ok or result.response is None:
            return result
        try:
            payload = self._payload_from_result(result)
            self._validate_payload_contract(payload)
        except Exception as exc:
            return ProviderResult(
                ok=False,
                error=ProviderError(
                    error_class=ProviderErrorClass.BAD_REQUEST,
                    message=f"invalid_panel_response:{exc}",
                ),
            )
        return result

    def _recommendation_from(self, result: ProviderResult) -> str:
        payload = self._payload_from_result(result)
        recommendation = str(payload.get("recommendation", "")).strip().lower()
        if recommendation not in {"accept", "revise", "reject"}:
            raise ValueError("invalid_review_recommendation")
        return recommendation

    def _payload_from_result(self, result: ProviderResult) -> dict[str, object]:
        if not result.ok or result.response is None:
            raise ValueError("provider_result_missing_response")
        raw = result.response.text.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw
            raw = raw.rsplit("```", 1)[0].strip()
        start = raw.find("{")
        if start == -1:
            raise ValueError("review_json_missing")
        depth = 0
        end = -1
        in_string = False
        escape = False
        for i in range(start, len(raw)):
            ch = raw[i]
            if escape:
                escape = False
                continue
            if ch == "\\":
                escape = True
                continue
            if ch == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i
                    break
        if end == -1:
            raise ValueError("review_json_missing")
        try:
            payload = json.loads(raw[start : end + 1])
        except json.JSONDecodeError:
            raise ValueError("review_json_parse_failed")
        if not isinstance(payload, dict):
            raise ValueError("review_json_not_object")
        return payload

    def _validate_payload_contract(self, payload: dict[str, object]) -> None:
        recommendation = str(payload.get("recommendation", "")).strip().lower()
        if recommendation not in {"accept", "revise", "reject"}:
            raise ValueError("invalid_review_recommendation")

        review_markdown = str(payload.get("review_markdown", "")).strip()
        if not review_markdown:
            raise ValueError("missing_review_markdown")

        rubric_scores = payload.get("rubric_scores")
        if not isinstance(rubric_scores, dict):
            raise ValueError("missing_rubric_scores")
        normalized_scores: dict[str, int] = {}
        for key in REVIEW_RUBRIC_KEYS:
            value = rubric_scores.get(key)
            if not isinstance(value, int) or value < 1 or value > 5:
                raise ValueError(f"invalid_rubric_score:{key}")
            normalized_scores[key] = value

        def _list_field(name: str) -> list[str]:
            value = payload.get(name)
            if not isinstance(value, list):
                raise ValueError(f"missing_{name}")
            return [str(item).strip() for item in value if str(item).strip()]

        major_issues = _list_field("major_issues")
        _list_field("minor_issues")
        required_revisions = _list_field("required_revisions")

        claim_support = str(payload.get("claim_support_verdict", "")).strip().lower()
        if claim_support not in CLAIM_SUPPORT_VERDICTS:
            raise ValueError("invalid_claim_support_verdict")
        overclaim = str(payload.get("overclaim_verdict", "")).strip().lower()
        if overclaim not in OVERCLAIM_VERDICTS:
            raise ValueError("invalid_overclaim_verdict")
        synthesis_quality = str(payload.get("synthesis_quality_verdict", "")).strip().lower()
        if synthesis_quality not in SYNTHESIS_QUALITY_VERDICTS:
            raise ValueError("invalid_synthesis_quality_verdict")

        if recommendation == "accept":
            # Threshold (tuned 2026-04-21): allow one dimension to dip to 3/5 if
            # the other five are >= 4/5 and none fall below 3. Calibrated against
            # elite_benchmark_v2 to lift elite-agreement from 0% baseline while
            # keeping contested-revise rate intact.
            weak_scores = sum(1 for score in normalized_scores.values() if score < 4)
            if weak_scores > 1:
                raise ValueError("accept_rubric_too_weak")
            if min(normalized_scores.values()) < 3:
                raise ValueError("accept_rubric_score_below_floor")
            if major_issues:
                raise ValueError("accept_has_major_issues")
            if required_revisions:
                raise ValueError("accept_has_required_revisions")
            if claim_support != "supported":
                raise ValueError("accept_claim_support_not_supported")
            if overclaim != "none":
                raise ValueError("accept_has_overclaim")
            if synthesis_quality not in {"strong", "adequate"}:
                raise ValueError("accept_synthesis_quality_invalid")

    def _error_text(self, result: ProviderResult) -> str:
        if result.error is None:
            return "unknown"
        return f"{result.error.error_class.value}:{result.error.message}"


def reviewer_from_env() -> LanguageModelProvider:
    selected = os.getenv("RESEARKA_V2_PROVIDER", "deterministic").strip().lower()
    if selected in {"judge_panel", "panel", "reviewer_panel"}:
        return ReviewerPanel(
            primary=MimoProvider(
                model=os.getenv("RESEARKA_V2_MIMO_MODEL", "mimo-v2-pro"),
                base_url=os.getenv("RESEARKA_V2_MIMO_BASE_URL", "https://token-plan-sgp.xiaomimimo.com/v1"),
            ),
            sparring=DeepSeekProvider(
                model=os.getenv("RESEARKA_V2_DEEPSEEK_SPARRING_MODEL", "deepseek-chat"),
                base_url=os.getenv("RESEARKA_V2_DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"),
            ),
            fallback=DeepSeekProvider(
                model=os.getenv("RESEARKA_V2_DEEPSEEK_MODEL", "deepseek-reasoner"),
                base_url=os.getenv("RESEARKA_V2_DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"),
            ),
        )
    if selected == "deterministic":
        return DeterministicProvider()
    from .providers import provider_from_env

    return provider_from_env()
