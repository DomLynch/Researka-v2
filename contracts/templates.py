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
)

PUBLICATION_TEMPLATES = {
    RAPID_EVIDENCE_SYNTHESIS.article_type: RAPID_EVIDENCE_SYNTHESIS,
    EMPIRICAL_STUDY.article_type: EMPIRICAL_STUDY,
}


def publication_template_for(article_type: str) -> PublicationTemplate:
    return PUBLICATION_TEMPLATES.get(article_type, RAPID_EVIDENCE_SYNTHESIS)
