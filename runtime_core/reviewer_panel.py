from __future__ import annotations

import json
import hashlib
import os

from contracts import ProviderErrorClass, ProviderUsage

from .providers import (
    DeterministicProvider,
    FallbackProvider,
    LanguageModelProvider,
    MimoProvider,
    MiniMaxProvider,
    OpenRouterProvider,
    ProviderError,
    ProviderRequest,
    ProviderResponse,
    ProviderResult,
)
from .review_contract import (
    CLAIM_SUPPORT_VERDICTS,
    OVERCLAIM_VERDICTS,
    REVIEW_RUBRIC_KEYS,
    SYNTHESIS_QUALITY_VERDICTS,
    accept_contract_failure,
    billing_waiver_receipt_valid,
    review_grounding_failure,
    review_attestation_secret,
)


class ReviewerPanel:
    provider = "reviewer-panel"
    enforces_accept_quorum = True

    def __init__(
        self,
        *,
        primary: LanguageModelProvider,
        sparring: LanguageModelProvider,
        fallback: LanguageModelProvider,
        allow_sparring_billing_skip: bool = False,
    ) -> None:
        self.primary = primary
        self.sparring = sparring
        self.fallback = fallback
        self.allow_sparring_billing_skip = allow_sparring_billing_skip
        self.model = f"{getattr(primary, 'model', 'primary')}|{getattr(sparring, 'model', 'sparring')}|{getattr(fallback, 'model', 'fallback')}"

    def complete(self, request: ProviderRequest) -> ProviderResult:
        primary = self._validated_slot(self.primary, request=request)
        sparring = self._validated_slot(self.sparring, request=request)
        slot_flags = self._slot_fallback_flags(primary, sparring)

        if primary.ok and sparring.ok:
            primary_rec = self._recommendation_from(primary)
            sparring_rec = self._recommendation_from(sparring)
            if primary_rec == sparring_rec:
                decision_quorum = self._decision_quorum_count(primary_rec, primary, sparring)
                if decision_quorum < 2:
                    reason = (
                        "panel_accept_quorum_unavailable"
                        if primary_rec == "accept"
                        else "panel_decision_quorum_unavailable"
                    )
                    return self._combined_error(reason, primary, sparring)
                return self._panel_response(
                    winner=primary,
                    route="consensus",
                    used=[primary, sparring],
                    metadata={
                        "primary_recommendation": primary_rec,
                        "sparring_recommendation": sparring_rec,
                        "accept_quorum_count": self._accept_quorum_count(primary, sparring),
                        "decision_quorum_count": decision_quorum,
                        "consensus": True,
                        **slot_flags,
                    },
                )
            fallback, fallback_attempts = self._validated_fallback(request)
            if not fallback.ok:
                return self._conservative_disagreement_response(
                    primary=primary,
                    primary_rec=primary_rec,
                    sparring=sparring,
                    sparring_rec=sparring_rec,
                    fallback=fallback,
                    fallback_attempts=fallback_attempts,
                    slot_flags=slot_flags,
                )
            fallback_rec = self._recommendation_from(fallback)
            decision_quorum = self._decision_quorum_count(fallback_rec, primary, sparring, fallback)
            if decision_quorum < 2:
                reason = (
                    "panel_accept_quorum_unavailable"
                    if fallback_rec == "accept"
                    else "panel_decision_quorum_unavailable"
                )
                return self._combined_error(reason, primary, sparring, fallback)
            return self._panel_response(
                winner=fallback,
                route="fallback_tiebreak",
                used=[primary, sparring, fallback],
                metadata={
                    "primary_recommendation": primary_rec,
                    "sparring_recommendation": sparring_rec,
                    "fallback_recommendation": fallback_rec,
                    "accept_quorum_count": self._accept_quorum_count(primary, sparring, fallback),
                    "decision_quorum_count": decision_quorum,
                    "consensus": False,
                    "escalated_to_fallback": True,
                    "fallback_tiebreak_attempts": fallback_attempts,
                    **slot_flags,
                },
            )

        if primary.ok and not sparring.ok:
            if (
                self.allow_sparring_billing_skip
                and self._openrouter_sparring()
                and sparring.error is not None
                and sparring.error.error_class is ProviderErrorClass.BILLING
                and sparring.error.status_code == 402
                and slot_flags.get("primary_fallback_used") is False
                and primary.response is not None
                and primary.response.provider in {"minimax", "mimo"}
                and self._recommendation_from(primary) == "accept"
            ):
                return self._panel_response(
                    winner=primary,
                    route="sparring_billing_skipped_primary_used",
                    used=[primary],
                    metadata={
                        "ops_flag": "sparring_billing_skipped",
                        "sparring_error": self._error_text(sparring),
                        "sparring_provider": "openrouter",
                        "sparring_http_status": 402,
                        "secondary_review_skipped": True,
                        "accept_quorum_count": self._accept_quorum_count(primary),
                        "accept_quorum_waiver": "sparring_billing_unavailable",
                        "consensus": False,
                        **slot_flags,
                    },
                )
            return self._single_valid_response(
                request,
                valid=primary,
                failed=sparring,
                failed_slot="sparring",
                route="sparring_failed_primary_used",
                slot_flags=slot_flags,
            )

        if sparring.ok and not primary.ok:
            return self._single_valid_response(
                request,
                valid=sparring,
                failed=primary,
                failed_slot="primary",
                route="primary_failed_sparring_used",
                slot_flags=slot_flags,
            )

        fallback, fallback_attempts = self._validated_fallback(request)
        if not fallback.ok:
            return self._combined_error("panel_all_failed", primary, sparring, fallback)
        return self._combined_error(
            "panel_single_fallback_cannot_decide",
            primary,
            sparring,
            fallback,
        )

    def billing_skip_receipt_valid(self, metadata: dict[str, object]) -> bool:
        return self.allow_sparring_billing_skip and billing_waiver_receipt_valid(metadata, provider=self.provider)

    def _openrouter_sparring(self) -> bool:
        if isinstance(self.sparring, FallbackProvider):
            return isinstance(self.sparring.primary, OpenRouterProvider) and isinstance(
                self.sparring.fallback,
                OpenRouterProvider,
            )
        return isinstance(self.sparring, OpenRouterProvider)

    def _validated_fallback(self, request: ProviderRequest) -> tuple[ProviderResult, int]:
        attempts = 0
        last = self._validated_result(self.fallback.complete(request), request=request)
        attempts += 1
        if last.ok:
            return last, attempts
        last = self._validated_result(self.fallback.complete(request), request=request)
        attempts += 1
        return last, attempts

    def _single_valid_response(
        self,
        request: ProviderRequest,
        *,
        valid: ProviderResult,
        failed: ProviderResult,
        failed_slot: str,
        route: str,
        slot_flags: dict[str, object],
    ) -> ProviderResult:
        recommendation = self._recommendation_from(valid)
        metadata = {
            "ops_flag": f"{failed_slot}_failed",
            f"{failed_slot}_error": self._error_text(failed),
            **slot_flags,
        }
        fallback, attempts = self._validated_fallback(request)
        if not fallback.ok:
            return self._combined_error("panel_decision_quorum_unavailable", valid, failed, fallback)
        fallback_rec = self._recommendation_from(fallback)
        if fallback_rec != recommendation:
            return self._combined_error("panel_single_reviewer_disagreement", valid, failed, fallback)
        decision_quorum = self._decision_quorum_count(recommendation, valid, fallback)
        if decision_quorum < 2:
            reason = (
                "panel_accept_quorum_unavailable"
                if recommendation == "accept"
                else "panel_decision_quorum_unavailable"
            )
            return self._combined_error(reason, valid, failed, fallback)
        return self._panel_response(
            winner=valid,
            route=f"{route}_confirmed",
            used=[valid, failed, fallback],
            metadata={
                **metadata,
                "surviving_recommendation": recommendation,
                "fallback_recommendation": fallback_rec,
                "accept_quorum_count": self._accept_quorum_count(valid, fallback),
                "decision_quorum_count": decision_quorum,
                "escalated_to_fallback": True,
                "fallback_tiebreak_attempts": attempts,
            },
        )

    def _accept_quorum_count(self, *results: ProviderResult) -> int:
        return self._decision_quorum_count("accept", *results)

    def _decision_quorum_count(self, recommendation: str, *results: ProviderResult) -> int:
        return min(
            len(self._recommendation_identities(recommendation, *results)),
            len(self._recommendation_providers(recommendation, *results)),
        )

    def _recommendation_identities(
        self,
        recommendation: str,
        *results: ProviderResult,
    ) -> list[str]:
        return sorted({
            f"{result.response.provider}:{result.response.model}"
            for result in results
            if result.ok
            and result.response is not None
            and result.response.model.strip()
            and self._recommendation_from(result) == recommendation
        })

    def _recommendation_providers(
        self,
        recommendation: str,
        *results: ProviderResult,
    ) -> list[str]:
        return sorted({
            result.response.provider
            for result in results
            if result.ok
            and result.response is not None
            and result.response.provider.strip()
            and self._recommendation_from(result) == recommendation
        })

    def _accept_quorum_identities(self, *results: ProviderResult) -> list[str]:
        return self._recommendation_identities("accept", *results)

    def _accept_quorum_models(self, *results: ProviderResult) -> list[str]:
        return sorted({
            result.response.model
            for result in results
            if result.ok
            and result.response is not None
            and result.response.model.strip()
            and self._recommendation_from(result) == "accept"
        })

    def _accept_quorum_providers(self, *results: ProviderResult) -> list[str]:
        return self._recommendation_providers("accept", *results)

    def _conservative_disagreement_response(
        self,
        *,
        primary: ProviderResult,
        primary_rec: str,
        sparring: ProviderResult,
        sparring_rec: str,
        fallback: ProviderResult,
        fallback_attempts: int,
        slot_flags: dict[str, object],
    ) -> ProviderResult:
        return self._combined_error(
            "panel_disagreement_unresolved",
            primary,
            sparring,
            fallback,
        )

    def _slot_fallback_flags(self, primary: ProviderResult, sparring: ProviderResult) -> dict[str, object]:
        """Read the FallbackProvider observability tags off each slot's response.

        FallbackProvider stamps `fallback_used` (and `fallback_reason` when true)
        into ProviderResponse.metadata. Surface those at the panel level as
        primary_fallback_used / sparring_fallback_used so the DW chain shows
        when a primary reviewer or Gemma was actually replaced by Mistral. Bare (un-wrapped)
        slots and failed slots default to False — the flag is only true when we
        have positive evidence the safety net fired.
        """
        flags: dict[str, object] = {}
        for slot_label, result in (("primary", primary), ("sparring", sparring)):
            metadata = result.response.metadata if (result.ok and result.response is not None) else {}
            flags[f"{slot_label}_fallback_used"] = bool(metadata.get("fallback_used", False))
            reason = metadata.get("fallback_reason")
            if reason:
                flags[f"{slot_label}_fallback_reason"] = reason
        return flags

    def _panel_response(
        self,
        *,
        winner: ProviderResult,
        route: str,
        used: list[ProviderResult],
        metadata: dict[str, object],
    ) -> ProviderResult:
        if winner.response is None:
            raise RuntimeError("panel_winner_response_missing")
        recommendation = self._recommendation_from(winner)
        usage = ProviderUsage(
            input_tokens=sum(result.response.usage.input_tokens for result in used if result.response),
            output_tokens=sum(result.response.usage.output_tokens for result in used if result.response),
            cost_usd=round(sum(result.response.usage.cost_usd for result in used if result.response), 6),
        )
        route_metadata = {
            "route": route,
            "winner_provider": winner.response.provider,
            "winner_model": winner.response.model,
            "review_markdown_recovered": bool(winner.response.metadata.get("review_markdown_recovered")),
            "panel_models": sorted({
                result.response.model for result in used if result.response and result.response.model.strip()
            }),
            "accept_quorum_models": self._accept_quorum_models(*used),
            "accept_quorum_identities": self._accept_quorum_identities(*used),
            "accept_quorum_providers": self._accept_quorum_providers(*used),
            "decision_quorum_count": self._decision_quorum_count(recommendation, *used),
            "decision_quorum_identities": self._recommendation_identities(recommendation, *used),
            "decision_quorum_providers": self._recommendation_providers(recommendation, *used),
            "reviewer_receipts": [self._reviewer_receipt(result) for result in used],
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

    def _reviewer_receipt(self, result: ProviderResult) -> dict[str, object]:
        if not result.ok or result.response is None:
            return {"ok": False, "error": self._error_text(result)}
        receipt: dict[str, object] = {
            "ok": True,
            "provider": result.response.provider,
            "model": result.response.model,
            "recommendation": self._recommendation_from(result),
            "response_sha256": hashlib.sha256(result.response.text.encode()).hexdigest(),
            "usage": result.response.usage.model_dump(mode="json"),
        }
        try:
            payload = self._payload_from_result(result)
        except (TypeError, ValueError):
            payload = None
        if isinstance(payload, dict):
            receipt["response"] = payload
        return receipt

    def _combined_error(self, reason: str, *results: ProviderResult) -> ProviderResult:
        return ProviderResult(
            ok=False,
            error=ProviderError(
                error_class=ProviderErrorClass.PROVIDER_UNAVAILABLE,
                message=f"{reason}:{' | '.join(self._error_text(result) for result in results)}",
            ),
        )

    def _validated_result(self, result: ProviderResult, *, request: ProviderRequest) -> ProviderResult:
        if not result.ok or result.response is None:
            return result
        try:
            payload = self._payload_from_result(result)
            result, payload = self._recover_review_markdown(result, payload)
            self._validate_payload_contract(payload)
            source_verification = request.context.get("source_verification")
            grounding_failure = review_grounding_failure(
                payload,
                user_prompt=request.user_prompt,
                source_verification=source_verification if isinstance(source_verification, dict) else None,
            )
            if grounding_failure:
                raise ValueError(grounding_failure)
        except Exception as exc:
            return ProviderResult(
                ok=False,
                error=ProviderError(
                    error_class=ProviderErrorClass.BAD_REQUEST,
                    message=f"invalid_panel_response:{exc}",
                ),
            )
        return result

    def _validated_slot(self, provider: LanguageModelProvider, *, request: ProviderRequest) -> ProviderResult:
        result = self._validated_result(provider.complete(request), request=request)
        if (
            result.ok
            or not isinstance(provider, FallbackProvider)
            or result.error is None
            or result.error.error_class is not ProviderErrorClass.BAD_REQUEST
            or not result.error.message.startswith("invalid_panel_response:")
        ):
            return result
        fallback = self._validated_result(provider.fallback.complete(request), request=request)
        if not fallback.ok:
            return result
        return FallbackProvider._tag(fallback, fallback_used=True, primary_error=result.error)

    def _recover_review_markdown(
        self,
        result: ProviderResult,
        payload: dict[str, object],
    ) -> tuple[ProviderResult, dict[str, object]]:
        if str(payload.get("review_markdown") or "").strip():
            return result, payload
        recommendation = str(payload.get("recommendation") or "").strip().lower()
        if recommendation not in {"revise", "reject"}:
            return result, payload
        sections: list[str] = []
        for label, field in (
            ("Major issues", "major_issues"),
            ("Minor issues", "minor_issues"),
            ("Required revisions", "required_revisions"),
        ):
            values = payload.get(field)
            items = [str(item).strip() for item in values if str(item).strip()] if isinstance(values, list) else []
            if items:
                sections.append(f"{label}:\n" + "\n".join(f"- {item}" for item in items))
        if not sections or result.response is None:
            return result, payload
        repaired = {**payload, "review_markdown": "\n\n".join(sections)}
        response = result.response.model_copy(
            update={
                "text": json.dumps(repaired),
                "metadata": {**result.response.metadata, "review_markdown_recovered": True},
            }
        )
        return result.model_copy(update={"response": response}), repaired

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
            failure = accept_contract_failure(
                normalized_scores,
                major_issues=major_issues,
                required_revisions=required_revisions,
                claim_support=claim_support,
                overclaim=overclaim,
                synthesis_quality=synthesis_quality,
            )
            if failure:
                raise ValueError(failure)
        if recommendation == "revise" and not required_revisions:
            raise ValueError("revise_missing_required_revisions")

    def _error_text(self, result: ProviderResult) -> str:
        if result.error is None:
            if result.ok and result.response is not None:
                return f"ok:{result.response.provider}:{result.response.model}"
            return "unknown"
        return f"{result.error.error_class.value}:{result.error.message}"


def reviewer_from_env() -> LanguageModelProvider:
    selected = os.getenv("RESEARKA_V2_PROVIDER", "deterministic").strip().lower()
    if selected in {"judge_panel", "panel", "reviewer_panel"}:
        or_base_url = os.getenv("RESEARKA_V2_OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
        # Backup model — Mistral by default. Used three ways:
        #   (1) wraps the primary reviewer so a transient failure still yields a primary review,
        #   (2) wraps Gemma so a transient Gemma failure still yields a sparring review,
        #   (3) is the panel-level tiebreaker on disagreement / both-failed.
        # Toggle off via RESEARKA_V2_REVIEWER_FALLBACK_ENABLED=0 if you want to study
        # raw primary/Gemma failure rates without the safety net.
        fallback_model = os.getenv("RESEARKA_V2_FALLBACK_MODEL", "mistralai/mistral-small-2603")
        fallback_enabled = os.getenv("RESEARKA_V2_REVIEWER_FALLBACK_ENABLED", "1").strip().lower() not in {"0", "false", "no"}

        def _make_fallback() -> OpenRouterProvider:
            return OpenRouterProvider(model=fallback_model, base_url=or_base_url)

        primary_provider = os.getenv("RESEARKA_V2_REVIEWER_PRIMARY_PROVIDER", "mimo").strip().lower()
        if primary_provider == "mimo":
            primary_inner: LanguageModelProvider = MimoProvider(
                model=os.getenv("RESEARKA_V2_MIMO_MODEL", "mimo-v2.5-pro"),
                base_url=os.getenv("RESEARKA_V2_MIMO_BASE_URL", "https://token-plan-sgp.xiaomimimo.com/v1"),
            )
        elif primary_provider == "minimax":
            primary_inner = MiniMaxProvider(
                model=os.getenv("RESEARKA_V2_MINIMAX_MODEL", "MiniMax-M3"),
                base_url=os.getenv("RESEARKA_V2_MINIMAX_BASE_URL", "https://api.minimax.io/anthropic"),
            )
        else:
            raise RuntimeError(f"unknown_primary_reviewer_provider:{primary_provider}")
        if os.getenv("RESEARKA_V2_ENV", "development").strip().lower() == "production":
            primary_key = "MIMO_API_KEY" if primary_provider == "mimo" else "MINIMAX_API_KEY"
            if not os.getenv(primary_key):
                raise RuntimeError(f"reviewer_credential_missing:{primary_key}")
            if not os.getenv("OPENROUTER_API_KEY"):
                raise RuntimeError("reviewer_credential_missing:OPENROUTER_API_KEY")
        sparring_inner = OpenRouterProvider(
            model=os.getenv("RESEARKA_V2_REVIEWER_MODEL", "google/gemma-4-31b-it"),
            base_url=or_base_url,
        )
        primary: LanguageModelProvider = (
            FallbackProvider(primary=primary_inner, fallback=_make_fallback()) if fallback_enabled else primary_inner
        )
        sparring: LanguageModelProvider = (
            FallbackProvider(primary=sparring_inner, fallback=_make_fallback()) if fallback_enabled else sparring_inner
        )
        allow_billing_skip = os.getenv(
            "RESEARKA_V2_SKIP_SPARRING_ON_BILLING_ERROR", "0"
        ).strip().lower() in {"1", "true", "yes"}
        if allow_billing_skip:
            review_attestation_secret(required=True)
        return ReviewerPanel(
            primary=primary,
            sparring=sparring,
            fallback=OpenRouterProvider(
                model=os.getenv("RESEARKA_V2_JUDGE_MODEL", "mistralai/mistral-small-2603"),
                base_url=or_base_url,
            ),
            allow_sparring_billing_skip=allow_billing_skip,
        )
    if selected == "deterministic":
        if os.getenv("RESEARKA_V2_ENV", "development").strip().lower() == "production":
            raise RuntimeError("deterministic_reviewer_forbidden_in_production")
        return DeterministicProvider()
    from .providers import provider_from_env

    return provider_from_env()
