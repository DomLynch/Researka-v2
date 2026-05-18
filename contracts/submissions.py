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
    review_checks: tuple[str, ...] = RAPID_EVIDENCE_SYNTHESIS.review_checks
    minimum_citations: int = 12
    minimum_recency_ratio: float = 0.5
    minimum_research_question_words: int = 50


SUBMISSION_TEMPLATE_V1 = SubmissionTemplateV1()
RECENT_PUBLICATION_YEAR_FLOOR = 2020


def submission_template_for(article_type: str) -> SubmissionTemplateV1:
    publication_template = publication_template_for(article_type)
    return SubmissionTemplateV1(
        article_type=publication_template.article_type,
        required_sections=publication_template.required_sections,
        review_checks=publication_template.review_checks,
    )


def _normalize_source_bundle(source_bundle: list[dict]) -> list[SourceBundleEntry]:
    normalized: list[SourceBundleEntry] = []
    for index, entry in enumerate(source_bundle):
        try:
            normalized.append(SourceBundleEntry.model_validate(entry))
        except ValidationError as exc:
            raise ValueError(f"submission_template:source_bundle_entry_invalid:{index}:{exc.errors()[0]['type']}") from exc
    return normalized


def run_submission_template_checks(
    *,
    sections: dict[str, str],
    source_bundle: list[dict],
    article_type: str = ArticleType.RAPID_EVIDENCE_SYNTHESIS.value,
    template: SubmissionTemplateV1 | None = None,
) -> list[GateResult]:
    active_template = template or submission_template_for(article_type)
    results: list[GateResult] = []

    research_question = str(sections.get("Research Question", "")).strip()
    question_words = len(research_question.split())
    results.append(
        GateResult(
            name="research_question_word_budget",
            passed=question_words >= active_template.minimum_research_question_words,
            reason=(
                f"Research Question must contain at least {active_template.minimum_research_question_words} words; "
                f"received {question_words}"
            ),
        )
    )

    try:
        normalized_bundle = _normalize_source_bundle(source_bundle)
    except ValueError as exc:
        return [GateResult(name="source_bundle_schema", passed=False, reason=str(exc))]

    citation_count = len(normalized_bundle)
    results.append(
        GateResult(
            name="minimum_citations",
            passed=citation_count >= active_template.minimum_citations,
            reason=f"source bundle must contain at least {active_template.minimum_citations} citations",
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
