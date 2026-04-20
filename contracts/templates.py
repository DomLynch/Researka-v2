from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PublicationTemplate:
    article_type: str
    label: str
    default_research_mode: str
    required_sections: tuple[str, ...]
    review_checks: tuple[str, ...]


RAPID_EVIDENCE_SYNTHESIS = PublicationTemplate(
    article_type="rapid_evidence_synthesis",
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