from __future__ import annotations

from dataclasses import dataclass

from .models import ArticleType


@dataclass(frozen=True)
class PublicationTemplate:
    article_type: str
    label: str
    default_research_mode: str
    required_sections: tuple[str, ...]
    review_checks: tuple[str, ...]
    # Which section name carries the research question / paper thesis. The
    # submission gate runs its word-count budget against this section. RES
    # uses an explicit "Research Question" header; full synthesis papers
    # state the question in the Abstract or Introduction.
    research_question_section: str = "Research Question"
    # Optional sections that are recommended but not gate-failing if absent.
    # Used by the synthesis path so reviewers see a richer artefact when the
    # author chooses to include depth sections.
    recommended_sections: tuple[str, ...] = ()
    # Optional minimum total body word count, summed across required +
    # recommended sections. Disabled by default; source grounding is the floor.
    minimum_body_word_count: int = 0


ALPHA_MEMO = PublicationTemplate(
    article_type=ArticleType.ALPHA_MEMO.value,
    label="Agent-Certified Evidence Map",
    default_research_mode="alpha_memo",
    required_sections=(),
    review_checks=(
        "Check whether the memo makes one bounded, source-grounded research signal clear.",
        "Score whether novelty claims stay proportionate to the cited receipts.",
        "Flag unsupported clinical, policy, investment, or broad consensus claims.",
    ),
    research_question_section="Evidence Landscape",
)

RAPID_EVIDENCE_SYNTHESIS = PublicationTemplate(
    article_type=ArticleType.RAPID_EVIDENCE_SYNTHESIS.value,
    label="Rapid Evidence Synthesis",
    default_research_mode="source_grounded_synthesis",
    required_sections=(
        "Research Question",
        "Search Summary",
        "Evidence Landscape",
        "Key Findings",
        "Limitations",
        "Gaps Identified",
        "Conclusion",
    ),
    review_checks=(
        "Check whether the search summary is explicit enough to audit the scope of the rapid synthesis.",
        "Score whether key findings stay proportionate to the directly cited evidence.",
        "Flag unsupported escalation from a narrow bundle to broad causal, deployment, or policy claims.",
    ),
    research_question_section="Research Question",
)

EMPIRICAL_STUDY = PublicationTemplate(
    article_type=ArticleType.EMPIRICAL_STUDY.value,
    label="Empirical Study",
    default_research_mode="study_grounded_manuscript",
    required_sections=(
        "Research Question",
        "Methods",
        "Results",
        "Limitations",
        "Conclusion",
    ),
    review_checks=(
        "Check whether methods, measurements, and inclusion logic are explicit enough to audit the study design.",
        "Score whether results and conclusions stay proportionate to the data actually reported in the manuscript.",
        "Flag unsupported leaps from one dataset, cohort, or model system to broad policy, deployment, or causal claims.",
    ),
    research_question_section="Research Question",
)

RESEARCH_SYNTHESIS = PublicationTemplate(
    article_type=ArticleType.RESEARCH_SYNTHESIS.value,
    label="Research Synthesis",
    default_research_mode="full_synthesis_manuscript",
    required_sections=(
        "Abstract",
        "Introduction",
        "Methods",
        "Results",
        "Discussion",
        "Limitations",
        "Conclusion",
    ),
    recommended_sections=(
        # Bot-style enrichment sections — encouraged but not gate-failing.
        # Surfacing these in the rubric tells reviewers to reward depth when
        # they appear, not to penalize their absence.
        "Background",
        "Inferential Bridge",
        "Quantitative Evidence Index",
        "Cross-Domain Synthesis",
        "References",
    ),
    review_checks=(
        "Check whether methods, search corpus, and inclusion logic are explicit enough to audit the synthesis at scale.",
        "Score whether claims, numerics, and conclusions trace cleanly to the cited evidence — reward verifiable traceability.",
        "Reward explicit cross-domain synthesis: how the paper integrates findings across outcome classes, populations, and study designs.",
        "Reward clear separation of mechanistic / preclinical evidence from clinical / human evidence, and appropriate hedging at the bridge.",
        "Flag unsupported escalation from preclinical mechanism to clinical recommendation or from narrow population to broad policy.",
        "Flag absent or generic limitations on a long, ambitious synthesis — substantive scope demands substantive limits.",
    ),
    research_question_section="Abstract",
    minimum_body_word_count=2000,
)

PUBLICATION_TEMPLATES = {
    ALPHA_MEMO.article_type: ALPHA_MEMO,
    RAPID_EVIDENCE_SYNTHESIS.article_type: RAPID_EVIDENCE_SYNTHESIS,
    EMPIRICAL_STUDY.article_type: EMPIRICAL_STUDY,
    RESEARCH_SYNTHESIS.article_type: RESEARCH_SYNTHESIS,
}


def publication_template_for(article_type: str) -> PublicationTemplate:
    return PUBLICATION_TEMPLATES.get(article_type, RAPID_EVIDENCE_SYNTHESIS)
