"""Tests for the v2 RESEARCH_SYNTHESIS article type.

The synthesis path is the gatekeeper-tier publishing path. It is calibrated
against the Research Agent Bot's natural output shape — long-form papers
 (2k+ words is acceptable) with 12+ sources, full IMRaD-style sections, optional
depth sections, and numeric traceability — so that the bot's best work
clears Researka without being destructively compressed into the rapid path.

These tests pin down:
  - The new ArticleType enum value resolves
  - The PublicationTemplate has the right required + recommended sections
  - submission_template_for() applies full-paper thresholds
    (12 citations, 75-word abstract, 2k counted-body floor)
  - The intake gates use the Abstract (not Research Question) for the
    word-count check on synthesis papers
  - Body-word-count gate keeps 2-3k papers eligible but blocks thin stubs
  - RES path is unchanged (regression check)
"""
from __future__ import annotations

from contracts import (
    PUBLICATION_TEMPLATES,
    RESEARCH_SYNTHESIS,
    ArticleType,
    publication_template_for,
    run_submission_template_checks,
    submission_template_for,
)


def _valid_synthesis_bundle(n: int = 12) -> list[dict[str, object]]:
    """Build a source bundle that clears citations, recency, and DOI gates."""
    bundle: list[dict[str, object]] = []
    for i in range(n):
        bundle.append(
            {
                "title": f"Synthesis source {i + 1}",
                "doi": f"10.1000/synthesis-{i + 1}",
                # Half recent (>=2020), half foundational (<2020) → meets the
                # 0.5 recency ratio comfortably.
                "year": 2024 if i % 2 == 0 else 2018,
                "evidence_type": "primary" if i % 2 == 0 else "review",
            }
        )
    return bundle


# ---------------------------------------------------------------------------
# 1. Article type + template wiring
# ---------------------------------------------------------------------------


def test_research_synthesis_article_type_resolves() -> None:
    assert ArticleType.RESEARCH_SYNTHESIS.value == "research_synthesis"
    assert "research_synthesis" in PUBLICATION_TEMPLATES
    assert publication_template_for("research_synthesis") is RESEARCH_SYNTHESIS


def test_research_synthesis_template_has_imrad_required_sections() -> None:
    required = RESEARCH_SYNTHESIS.required_sections
    # Core IMRaD-style structure, plus Abstract, Limitations, and Conclusion.
    assert "Abstract" in required
    assert "Introduction" in required
    assert "Methods" in required
    assert "Results" in required
    assert "Discussion" in required
    assert "Limitations" in required
    assert "Conclusion" in required
    # Should NOT require RES-specific sections.
    assert "Search Summary" not in required
    assert "Key Findings" not in required
    assert "Gaps Identified" not in required


def test_research_synthesis_template_recommends_depth_sections() -> None:
    recommended = RESEARCH_SYNTHESIS.recommended_sections
    # Bot-style enrichment sections — recommended (not gate-failing).
    assert "Background" in recommended
    assert "Inferential Bridge" in recommended
    assert "Quantitative Evidence Index" in recommended
    assert "Cross-Domain Synthesis" in recommended


def test_research_synthesis_template_uses_abstract_for_research_question() -> None:
    # Synthesis papers state the question in the Abstract, not a separate header.
    assert RESEARCH_SYNTHESIS.research_question_section == "Abstract"


def test_research_synthesis_keeps_live_source_floor_with_low_body_gate() -> None:
    synthesis_t = submission_template_for(ArticleType.RESEARCH_SYNTHESIS.value)
    res_t = submission_template_for(ArticleType.RAPID_EVIDENCE_SYNTHESIS.value)
    # V3 full papers keep the live 12-source floor and accept 2-3k word bodies.
    assert synthesis_t.minimum_citations == 12
    assert synthesis_t.minimum_citations == res_t.minimum_citations
    assert synthesis_t.minimum_research_question_words > res_t.minimum_research_question_words
    assert synthesis_t.minimum_body_word_count == 2000
    assert res_t.minimum_body_word_count == 0


# ---------------------------------------------------------------------------
# 2. Gate behaviour for synthesis paths
# ---------------------------------------------------------------------------


def test_synthesis_gate_reads_word_budget_from_abstract_not_research_question() -> None:
    """Critical: synthesis papers don't have a 'Research Question' section. The
    gate must look in Abstract instead. If we left it pointing at Research
    Question, every bot-format paper would fail intake on the wrong reason."""
    sections = {
        "Abstract": " ".join(["abstract_word"] * 80),
        "Introduction": "intro " * 200,
        "Methods": "methods " * 200,
        "Results": "results " * 5000,
        "Discussion": "discussion " * 1500,
        "Limitations": "limitations " * 200,
        "Conclusion": "conclusion " * 50,
    }
    results = run_submission_template_checks(
        sections=sections,
        source_bundle=_valid_synthesis_bundle(12),
        article_type="research_synthesis",
    )
    rq_gate = next(g for g in results if g.name == "research_question_word_budget")
    assert rq_gate.passed is True
    # The reason string should make it explicit which section was checked.
    assert "Abstract" in rq_gate.reason


def test_synthesis_gate_allows_2k_body_when_sources_clear_floor() -> None:
    """The live v3 path should accept 2-3k papers when evidence gates pass."""
    concise_sections = {
        "Abstract": " ".join(["abstract"] * 80),
        "Introduction": " ".join(["i"] * 350),
        "Methods": " ".join(["m"] * 300),
        "Results": " ".join(["r"] * 850),
        "Discussion": " ".join(["d"] * 450),
        "Limitations": " ".join(["l"] * 200),
        "Conclusion": " ".join(["c"] * 80),
    }
    results = run_submission_template_checks(
        sections=concise_sections,
        source_bundle=_valid_synthesis_bundle(12),
        article_type="research_synthesis",
    )
    body_gate = next(g for g in results if g.name == "minimum_body_word_count")
    assert body_gate.passed is True
    assert all(g.passed for g in results)


def test_synthesis_gate_blocks_stub_body_even_with_sources() -> None:
    stub_sections = {
        "Abstract": " ".join(["abstract"] * 80),
        "Introduction": " ".join(["i"] * 80),
        "Methods": " ".join(["m"] * 80),
        "Results": " ".join(["r"] * 80),
        "Discussion": " ".join(["d"] * 80),
        "Limitations": " ".join(["l"] * 40),
        "Conclusion": " ".join(["c"] * 40),
    }
    results = run_submission_template_checks(
        sections=stub_sections,
        source_bundle=_valid_synthesis_bundle(12),
        article_type="research_synthesis",
    )
    body_gate = next(g for g in results if g.name == "minimum_body_word_count")
    assert body_gate.passed is False
    assert "2000" in body_gate.reason


def test_synthesis_gate_minimum_citations_demands_12() -> None:
    fat_sections = {
        "Abstract": " ".join(["a"] * 100),
        "Introduction": " ".join(["i"] * 2000),
        "Methods": " ".join(["m"] * 2000),
        "Results": " ".join(["r"] * 4000),
        "Discussion": " ".join(["d"] * 2000),
        "Limitations": " ".join(["l"] * 500),
        "Conclusion": " ".join(["c"] * 100),
    }
    results = run_submission_template_checks(
        sections=fat_sections,
        source_bundle=_valid_synthesis_bundle(11),
        article_type="research_synthesis",
    )
    cite_gate = next(g for g in results if g.name == "minimum_citations")
    assert cite_gate.passed is False
    assert "12" in cite_gate.reason


# ---------------------------------------------------------------------------
# 3. Regression: RES path unchanged
# ---------------------------------------------------------------------------


def test_rapid_evidence_synthesis_still_uses_research_question_section() -> None:
    """Adding the synthesis path must not break the rapid path."""
    sections = {
        "Research Question": " ".join(["w"] * 60),
        "Search Summary": "x " * 30,
        "Evidence Landscape": "x " * 30,
        "Key Findings": "x " * 30,
        "Limitations": "x " * 30,
        "Gaps Identified": "x " * 30,
        "Conclusion": "x " * 30,
    }
    results = run_submission_template_checks(
        sections=sections,
        source_bundle=_valid_synthesis_bundle(12),
        article_type="rapid_evidence_synthesis",
    )
    rq_gate = next(g for g in results if g.name == "research_question_word_budget")
    assert rq_gate.passed is True
    assert "Research Question" in rq_gate.reason
    # No body-word-count gate on RES (minimum is 0).
    assert not any(g.name == "minimum_body_word_count" for g in results)
