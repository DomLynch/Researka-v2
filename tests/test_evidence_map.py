from __future__ import annotations

from contracts import (
    EVIDENCE_MAP,
    ArticleType,
    SubmissionPayload,
    publication_template_for,
    run_submission_template_checks,
    submission_template_for,
)
from runtime_core import WorkflowEngine


def test_evidence_map_template_registered() -> None:
    template = publication_template_for(ArticleType.EVIDENCE_MAP.value)
    assert template is EVIDENCE_MAP
    assert template.label == "Evidence Map"
    # Memo-tier: structured sections are recommended, never gate-failing —
    # evidence maps arrive markdown-only through the alpha artifact pipe.
    assert template.required_sections == ()
    assert "Findings Map" in template.recommended_sections
    assert template.research_question_section == "Evidence Landscape"


def test_evidence_map_thresholds_relax_single_claim_budget() -> None:
    template = submission_template_for(ArticleType.EVIDENCE_MAP.value)
    # Source-rich floor, but the single-thesis word budget is relaxed vs RES (50) / synthesis (75).
    assert template.minimum_citations == 10
    assert template.minimum_research_question_words == 30
    assert template.research_question_section == "Evidence Landscape"


def test_artifact_pipe_does_not_clobber_declared_evidence_map() -> None:
    """Regression: v4 sends artifact_type=alpha_memo (its pipe identity) with
    article_type=evidence_map. The declared type must survive — this clobber
    sent live evidence maps to the alpha single-claim rubric and got them rejected."""
    # The bot sends raw JSON (strings) — exercise that exact wire path.
    payload = SubmissionPayload.model_validate({
        "title": "Metformin: evidence map",
        "abstract": "x",
        "markdown": "## Findings\nten heterogeneous findings across outcome classes",
        "artifact_type": "alpha_memo",
        "article_type": "evidence_map",
        "author_agent_id": "bot",
    })
    assert payload.article_type is ArticleType.EVIDENCE_MAP
    assert "evidence-map reviewer" in WorkflowEngine()._review_system_prompt(payload.article_type.value)


def test_plain_alpha_memo_still_normalizes() -> None:
    payload = SubmissionPayload(
        title="Memo", abstract="", markdown="## E\nsignal", artifact_type="alpha_memo", author_agent_id="bot"
    )
    assert payload.article_type is ArticleType.ALPHA_MEMO
    assert payload.sections == {"Evidence Landscape": "## E\nsignal"}
    assert payload.abstract


def test_evidence_map_reviewer_prompt_does_not_demand_one_claim() -> None:
    prompt = WorkflowEngine()._review_system_prompt(ArticleType.EVIDENCE_MAP.value).lower()
    assert "evidence-map reviewer" in prompt
    assert "do not require a single bounded thesis" in prompt
    # The strict single-claim acceptance language must NOT govern this type.
    assert "one bounded, source-grounded research signal" not in prompt


def test_valid_evidence_map_passes_template_checks() -> None:
    landscape = (
        "Findings cluster across cardiometabolic, longevity, immune, oncology, and renal "
        "outcomes; effects range from protective to null to harmful depending on the cohort, "
        "dose, and endpoint, so no single converging direction is asserted across the corpus."
    )  # >= 30 words for the relaxed research-question budget (now the Evidence Landscape section)
    sections = {
        "Evidence Landscape": landscape,
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
