from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ValidationError

from .models import ArticleType, GateResult
from .templates import RAPID_EVIDENCE_SYNTHESIS, publication_template_for

_DOI_PATTERN = re.compile(r"^10\.\d{4,}/\S+$")


class SourceBundleEntry(BaseModel):
    title: str
    url: str | None = None
    doi: str | None = None
    year: int | None = None
    evidence_type: Literal["primary", "review"]
    relevance: float | None = None


class SubmissionTemplateV1(BaseModel):
    article_type: str = ArticleType.RAPID_EVIDENCE_SYNTHESIS.value
    required_sections: tuple[str, ...] = RAPID_EVIDENCE_SYNTHESIS.required_sections
    recommended_sections: tuple[str, ...] = ()
    review_checks: tuple[str, ...] = RAPID_EVIDENCE_SYNTHESIS.review_checks
    minimum_citations: int = 12
    minimum_recency_ratio: float = 0.5
    minimum_research_question_words: int = 50
    research_question_section: str = "Research Question"
    minimum_body_word_count: int = 0


SUBMISSION_TEMPLATE_V1 = SubmissionTemplateV1()
RECENT_PUBLICATION_YEAR_FLOOR = 2020

# Per-article-type intake thresholds. Alpha memos are shorter than papers but
# still need enough independent receipts for public auto-acceptance.
_TYPE_THRESHOLDS: dict[str, dict[str, object]] = {
    ArticleType.ALPHA_MEMO.value: {
        "minimum_citations": 5,
        "minimum_recency_ratio": 0.0,
        "minimum_research_question_words": 0,
    },
    ArticleType.RAPID_EVIDENCE_SYNTHESIS.value: {
        "minimum_citations": 12,
        "minimum_recency_ratio": 0.5,
        "minimum_research_question_words": 50,
    },
    ArticleType.EMPIRICAL_STUDY.value: {
        "minimum_citations": 12,
        "minimum_recency_ratio": 0.5,
        "minimum_research_question_words": 50,
    },
    ArticleType.RESEARCH_SYNTHESIS.value: {
        # V3 full-paper lane keeps the live Researka source floor at 12 while
        # using the full-manuscript section/body gates.
        "minimum_citations": 12,
        # Synthesis papers must engage with foundational mechanism work
        # (often pre-2020 for established pathways like mTOR / autophagy),
        # so the recency floor is lower than RES. 40% means at least 10 of
        # 25 citations must be recent — current evidence is still required,
        # but the corpus is allowed to be majority-foundational.
        "minimum_recency_ratio": 0.4,
        # Abstract for a synthesis paper must convey the question, scope, and
        # headline findings — needs more words than a one-line research
        # question prompt.
        "minimum_research_question_words": 75,
    },
}


def submission_template_for(article_type: str) -> SubmissionTemplateV1:
    publication_template = publication_template_for(article_type)
    thresholds = _TYPE_THRESHOLDS.get(publication_template.article_type, {})
    return SubmissionTemplateV1(
        article_type=publication_template.article_type,
        required_sections=publication_template.required_sections,
        recommended_sections=publication_template.recommended_sections,
        review_checks=publication_template.review_checks,
        research_question_section=publication_template.research_question_section,
        minimum_body_word_count=publication_template.minimum_body_word_count,
        **thresholds,  # type: ignore[arg-type]
    )


def _normalize_source_bundle(source_bundle: list[dict]) -> list[SourceBundleEntry]:
    normalized: list[SourceBundleEntry] = []
    for index, entry in enumerate(source_bundle):
        try:
            normalized.append(SourceBundleEntry.model_validate(entry))
        except ValidationError as exc:
            raise ValueError(f"submission_template:source_bundle_entry_invalid:{index}:{exc.errors()[0]['type']}") from exc
    return normalized


def _int_value(value: object) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return 0


def _alpha_source_exception(article_type: str, citation_count: int, evidence_bundle: dict | None) -> bool:
    if article_type != ArticleType.ALPHA_MEMO.value or not 2 <= citation_count <= 4:
        return False
    verdict = (evidence_bundle or {}).get("publish_verdict")
    if not isinstance(verdict, dict):
        return False
    axes_raw = verdict.get("axes")
    axes = axes_raw if isinstance(axes_raw, dict) else {}
    if verdict.get("decision") != "ready_to_publish" or verdict.get("publish_tier") != "TIER_1":
        return False
    if verdict.get("maturity_level") != "L5" and verdict.get("confidence_label") != "evidence_backed_signal":
        return False
    if verdict.get("blockers") or axes.get("cross_domain_forced") or axes.get("feed_scope_mismatch"):
        return False
    bound = max(
        _int_value(axes.get("bound_receipts")),
        _int_value(axes.get("a_core_receipts")),
        _int_value(axes.get("available_bound_receipts")),
    )
    return bound >= 2


def run_submission_template_checks(
    *,
    sections: dict[str, str],
    source_bundle: list[dict],
    article_type: str = ArticleType.RAPID_EVIDENCE_SYNTHESIS.value,
    template: SubmissionTemplateV1 | None = None,
    evidence_bundle: dict | None = None,
) -> list[GateResult]:
    active_template = template or submission_template_for(article_type)
    results: list[GateResult] = []

    # Research question gate: which section to inspect depends on article type.
    # RES uses an explicit "Research Question" section; synthesis papers state
    # the question in the Abstract or Introduction.
    rq_section = active_template.research_question_section
    research_question = str(sections.get(rq_section, "")).strip()
    question_words = len(research_question.split())
    results.append(
        GateResult(
            name="research_question_word_budget",
            passed=question_words >= active_template.minimum_research_question_words,
            reason=(
                f"`{rq_section}` must contain at least {active_template.minimum_research_question_words} words; "
                f"received {question_words}"
            ),
        )
    )

    # Body word count gate (synthesis-tier only — minimum=0 for RES disables it).
    if active_template.minimum_body_word_count > 0:
        body_words = sum(
            len(str(sections.get(name, "")).split())
            for name in (*active_template.required_sections, *active_template.recommended_sections)
        )
        results.append(
            GateResult(
                name="minimum_body_word_count",
                passed=body_words >= active_template.minimum_body_word_count,
                reason=(
                    f"synthesis-paper body (required + recommended sections) must contain at least "
                    f"{active_template.minimum_body_word_count} words; received {body_words}"
                ),
            )
        )

    try:
        normalized_bundle = _normalize_source_bundle(source_bundle)
    except ValueError as exc:
        return [GateResult(name="source_bundle_schema", passed=False, reason=str(exc))]

    citation_count = len(normalized_bundle)
    citation_floor_exception = _alpha_source_exception(article_type, citation_count, evidence_bundle)
    citation_reason = f"source bundle must contain at least {active_template.minimum_citations} citations"
    if article_type == ArticleType.ALPHA_MEMO.value:
        citation_reason += " or qualify for the 2-4 source alpha exception"
    results.append(
        GateResult(
            name="minimum_citations",
            passed=citation_count >= active_template.minimum_citations or citation_floor_exception,
            reason=citation_reason,
        )
    )

    recent_count = sum(
        1
        for entry in normalized_bundle
        if isinstance(entry.year, int) and entry.year >= RECENT_PUBLICATION_YEAR_FLOOR
    )
    recent_ratio = recent_count / citation_count if citation_count else 0.0
    results.append(
        GateResult(
            name="recency_ratio",
            passed=recent_ratio >= active_template.minimum_recency_ratio,
            reason=(
                f"at least {active_template.minimum_recency_ratio:.0%} of citations must be from "
                f"{RECENT_PUBLICATION_YEAR_FLOOR}+; received {recent_ratio:.0%}"
            ),
        )
    )

    malformed_dois = [
        index
        for index, entry in enumerate(normalized_bundle)
        if entry.doi is not None and not _DOI_PATTERN.match(str(entry.doi).strip())
    ]
    if malformed_dois:
        results.append(
            GateResult(
                name="doi_sanity",
                passed=False,
                reason=f"malformed DOI at indices {malformed_dois}; expected format 10.XXXX/suffix",
            )
        )
    else:
        results.append(
            GateResult(name="doi_sanity", passed=True, reason="all provided DOIs are syntactically valid")
        )

    return results
