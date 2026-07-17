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
            "excerpt": "This source maps a bounded outcome-specific finding with heterogeneous direction.",
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


def test_evidence_map_rejects_multiple_off_topic_rows() -> None:
    sections = {
        "Evidence Landscape": (
            "Exercise evidence is mapped across populations, comparators, endpoints, source types, and effect "
            "directions so readers can inspect heterogeneous findings without pretending the corpus converges "
            "to one universal clinical or policy claim."
        ),
        "Findings Map": """
| population | comparator | finding | source |
|---|---|---|---|
| older adults | control | exercise training improved fitness and strength | doi:10.1000/ex1 |
| adults with obesity | placebo | average losses of 9.6-17.4% of initial body weight at week 68 | doi:10.1000/off1 |
| aged adults | sham | this method is impractical and may reduce arterial compliance by about 20% | doi:10.1000/off2 |
| colon-cancer risk adults | usual care | physical activity may prevent approximately 15% of colon cancers | doi:10.1000/ex2 |
""",
    }
    results = run_submission_template_checks(
        title="Exercise: evidence map - 18 findings across 18 sources",
        sections=sections,
        source_bundle=[
            {
                "title": f"Exercise source {i}",
                "doi": f"10.1000/ex{i}",
                "year": 2024,
                "evidence_type": "primary",
            }
            for i in range(10)
        ],
        article_type=ArticleType.EVIDENCE_MAP.value,
    )

    gate = next(result for result in results if result.name == "topic_coherence")
    assert not gate.passed
    assert "doi:10.1000/off1" in gate.reason
    assert "doi:10.1000/off2" in gate.reason


def test_evidence_map_accepts_topic_synonyms() -> None:
    sections = {
        "Evidence Landscape": (
            "Exercise evidence is mapped across populations, comparators, endpoints, source types, and effect "
            "directions so readers can inspect heterogeneous findings without pretending the corpus converges "
            "to one universal clinical or policy claim."
        ),
        "Findings Map": """
| population | comparator | finding | source |
|---|---|---|---|
| older adults | control | physical activity improved functional capacity | doi:10.1000/ex1 |
| adults | control | resistance training improved muscle strength | doi:10.1000/ex2 |
| older adults | sham | aerobic training improved fitness | doi:10.1000/ex3 |
""",
    }
    results = run_submission_template_checks(
        title="Exercise: evidence map - 18 findings across 18 sources",
        sections=sections,
        source_bundle=[
            {
                "title": f"Exercise source {i}",
                "doi": f"10.1000/ex{i}",
                "year": 2024,
                "evidence_type": "primary",
            }
            for i in range(10)
        ],
        article_type=ArticleType.EVIDENCE_MAP.value,
    )

    gate = next(result for result in results if result.name == "topic_coherence")
    assert gate.passed, gate.reason


def test_evidence_map_accepts_generic_label_before_topic_title() -> None:
    sections = {
        "Evidence Landscape": """
| Evidence domain | Corpus slice | Strongest signal | Directness | Main limitation |
|---|---|---|---|---|
| TORC1 inhibitor / Skeletal, Fracture, and Bone | n=3 | null | 3 review | bounded |
| TORC1 inhibitor / Immune and Inflammation | n=2 | null | 2 review | bounded |
| TORC1 inhibitor / Cardiometabolic | n=1 | unclear | 1 review | bounded |
""",
    }
    results = run_submission_template_checks(
        title="Adjacent Evidence Brief: TORC1 inhibitor — full paper",
        sections=sections,
        source_bundle=[
            {
                "title": f"TORC1 inhibitor source {i}",
                "doi": f"10.1000/torc{i}",
                "year": 2024,
                "evidence_type": "primary",
            }
            for i in range(10)
        ],
        article_type=ArticleType.EVIDENCE_MAP.value,
    )

    gate = next(result for result in results if result.name == "topic_coherence")
    assert gate.passed, gate.reason
