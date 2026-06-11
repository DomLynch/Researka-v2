from __future__ import annotations

from contracts import (
    EVIDENCE_MAP,
    ArticleType,
    publication_template_for,
    run_submission_template_checks,
    submission_template_for,
)
from runtime_core import WorkflowEngine


def test_evidence_map_template_registered() -> None:
    template = publication_template_for(ArticleType.EVIDENCE_MAP.value)
    assert template is EVIDENCE_MAP
    assert template.label == "Evidence Map"
    assert "Findings Map" in template.required_sections
    assert template.research_question_section == "Scope"


def test_evidence_map_thresholds_relax_single_claim_budget() -> None:
    template = submission_template_for(ArticleType.EVIDENCE_MAP.value)
    # Source-rich floor, but the single-thesis word budget is relaxed vs RES (50) / synthesis (75).
    assert template.minimum_citations == 10
    assert template.minimum_research_question_words == 30
    assert template.research_question_section == "Scope"


def test_evidence_map_reviewer_prompt_does_not_demand_one_claim() -> None:
    prompt = WorkflowEngine()._review_system_prompt(ArticleType.EVIDENCE_MAP.value).lower()
    assert "evidence-map reviewer" in prompt
    assert "do not require a single bounded thesis" in prompt
    # The strict single-claim acceptance language must NOT govern this type.
    assert "one bounded, source-grounded research signal" not in prompt


def test_valid_evidence_map_passes_template_checks() -> None:
    scope = " ".join(["metformin"] * 35)  # >= 30 words for the relaxed Scope budget
    sections = {
        "Scope": scope,
        "Search Summary": "PubMed + reviews, 2018-2026, inclusion logic stated.",
        "Evidence Landscape": "Findings cluster across cardiometabolic, longevity, immune, and oncology outcomes.",
        "Findings Map": "Each finding is attributed to its source with effect direction.",
        "Tensions and Gaps": "Cardiometabolic positive vs oncology null; no head-to-head trials.",
        "Limitations": "Heterogeneous designs; no single converging conclusion is claimed.",
    }
    source_bundle = [
        {
            "title": f"Source {i}",
            "doi": f"10.1000/m{i}",
            "year": 2024,
            "evidence_type": "review" if i % 2 == 0 else "primary",
        }
        for i in range(10)
    ]

    results = run_submission_template_checks(
        sections=sections,
        source_bundle=source_bundle,
        article_type=ArticleType.EVIDENCE_MAP.value,
    )

    failed = [r.name for r in results if not r.passed]
    assert failed == [], failed
