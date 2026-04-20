from __future__ import annotations

from dataclasses import dataclass

from .models import GateResult
from .templates import RAPID_EVIDENCE_SYNTHESIS
from .submissions import (
    RECENT_PUBLICATION_YEAR_FLOOR,
    SUBMISSION_TEMPLATE_V1,
    SourceBundleEntry,
    SubmissionTemplateV1,
    run_submission_template_checks,
)


@dataclass(frozen=True)
class IntakeRejectReason:
    gate: str
    code: str
    message: str


INTAKE_REJECT_REASONS = (
    IntakeRejectReason(
        gate="research_question_word_budget",
        code="rq_too_short",
        message=(
            f"Research Question must contain at least "
            f"{SUBMISSION_TEMPLATE_V1.minimum_research_question_words} words."
        ),
    ),
    IntakeRejectReason(
        gate="source_bundle_schema",
        code="bundle_entry_invalid",
        message=(
            "Each source_bundle entry must have: title (required), evidence_type (required, "
            "'primary' or 'review'). Optional: url, doi, year (int), relevance (float)."
        ),
    ),
    IntakeRejectReason(
        gate="minimum_citations",
        code="too_few_citations",
        message=(
            f"source_bundle must contain at least "
            f"{SUBMISSION_TEMPLATE_V1.minimum_citations} entries."
        ),
    ),
    IntakeRejectReason(
        gate="recency_ratio",
        code="too_few_recent",
        message=(
            f"At least {SUBMISSION_TEMPLATE_V1.minimum_recency_ratio:.0%} of citations "
            f"must be from {RECENT_PUBLICATION_YEAR_FLOOR}+."
        ),
    ),
    IntakeRejectReason(
        gate="leakage_blocker",
        code="pipeline_leakage",
        message=(
            "Body must not contain reviewer notes, revision briefs, "
            "placeholder tokens, or pipeline leakage."
        ),
    ),
    IntakeRejectReason(
        gate="count_reconciliation",
        code="count_mismatch",
        message="Selected citation count must equal review-like + primary-like counts.",
    ),
    IntakeRejectReason(
        gate="core_claims_resolved",
        code="unresolved_claims",
        message="Title/abstract/conclusion claims must not remain unresolved.",
    ),
    IntakeRejectReason(
        gate="doi_sanity",
        code="malformed_doi",
        message="Provided DOIs must match format 10.XXXX/suffix.",
    ),
)

SUBMISSION_CONTRACT = {
    "version": "v1",
    "article_type": RAPID_EVIDENCE_SYNTHESIS.article_type,
    "label": RAPID_EVIDENCE_SYNTHESIS.label,
    "required_sections": RAPID_EVIDENCE_SYNTHESIS.required_sections,
    "section_count": len(RAPID_EVIDENCE_SYNTHESIS.required_sections),
    "minimum_research_question_words": SUBMISSION_TEMPLATE_V1.minimum_research_question_words,
    "minimum_citations": SUBMISSION_TEMPLATE_V1.minimum_citations,
    "minimum_recency_ratio": SUBMISSION_TEMPLATE_V1.minimum_recency_ratio,
    "recency_year_floor": RECENT_PUBLICATION_YEAR_FLOOR,
    "source_bundle_schema": {
        "required": ["title", "evidence_type"],
        "optional": ["url", "doi", "year", "relevance"],
        "evidence_type_values": ["primary", "review"],
        "year_type": "int or null",
        "relevance_type": "float or null",
    },
    "reject_reasons": [
        {"gate": r.gate, "code": r.code, "message": r.message}
        for r in INTAKE_REJECT_REASONS
    ],
    "review_checks": RAPID_EVIDENCE_SYNTHESIS.review_checks,
}

__all__ = [
    "IntakeRejectReason",
    "INTAKE_REJECT_REASONS",
    "SUBMISSION_CONTRACT",
]
