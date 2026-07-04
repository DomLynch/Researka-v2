from __future__ import annotations

import re
from typing import Any

from contracts import ArticleType, ContradictionStatus

WEAK_PATTERN = re.compile(
    r"(\d+)\s*/\s*(\d+)\s+retained sources are (?:coded as null|indirect)",
    re.IGNORECASE,
)
DIRECT_PATTERN = re.compile(r"contains\s+(\d+)\s+direct clinical sources", re.IGNORECASE)


def evidence_profile(*, text: str, source_bundle: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    source_bundle = source_bundle or []
    weak_ratio = 0.0
    for weak, total in WEAK_PATTERN.findall(text):
        denom = max(1, int(total))
        weak_ratio = max(weak_ratio, int(weak) / denom)
    direct_match = DIRECT_PATTERN.search(text)
    direct_count = int(direct_match.group(1)) if direct_match else None
    selected_count = len(source_bundle)
    primary_count = sum(1 for item in source_bundle if item.get("evidence_type") == "primary")
    lower = text.lower()
    return {
        "weak_evidence_ratio": round(weak_ratio, 4),
        "direct_clinical_sources": direct_count,
        "source_count": selected_count,
        "primary_source_ratio": round(primary_count / selected_count, 4) if selected_count else None,
        "mixed_signal": any(term in lower for term in ("mixed", "heterogeneous", "disagreement", "tension")),
        "non_supportive_signal": any(term in lower for term in ("non-supportive", "does not support", "null or no extracted")),
        "indirect_signal": any(term in lower for term in ("indirect", "adjacent", "mechanistic")),
    }


def publication_class(*, article_type: str, title: str, profile: dict[str, Any]) -> str:
    if article_type == ArticleType.ALPHA_MEMO.value:
        return "alpha_memo"
    if article_type == ArticleType.EVIDENCE_MAP.value:
        return "evidence_map"
    weak_ratio = float(profile.get("weak_evidence_ratio") or 0.0)
    direct_count = profile.get("direct_clinical_sources")
    low_direct = isinstance(direct_count, int) and direct_count <= 2
    if weak_ratio >= 0.80 or (low_direct and profile.get("non_supportive_signal")):
        return "hypothesis_generating_brief"
    if (
        title.lower().startswith("research synthesis:")
        and isinstance(direct_count, int)
        and direct_count > 2
    ):
        return "research_synthesis"
    if weak_ratio >= 0.60 or profile.get("indirect_signal"):
        return "adjacent_evidence_brief"
    if title.lower().startswith("research synthesis:"):
        return "research_synthesis"
    return "living_evidence_brief"


def classified_title(title: str, publication_class: str) -> str:
    prefixes = {
        "evidence_map": "Evidence Map",
        "hypothesis_generating_brief": "Hypothesis-Generating Brief",
        "adjacent_evidence_brief": "Adjacent Evidence Brief",
        "research_synthesis": "Research Synthesis",
    }
    prefix = prefixes.get(publication_class)
    if not prefix:
        return title
    body = re.sub(
        r"^(Research Synthesis|Evidence Map|Hypothesis-Generating Brief|Adjacent Evidence Brief):\s*",
        "",
        title,
        flags=re.IGNORECASE,
    )
    return f"{prefix}: {body.strip()}"


def contradiction_status_for_text(text: str, profile: dict[str, Any]) -> ContradictionStatus:
    lower = text.lower()
    if any(term in lower for term in ("non-supportive", "does not support", "no extracted directional")):
        return ContradictionStatus.NON_SUPPORTIVE
    if any(term in lower for term in ("insufficient", "sparse", "indirect", "adjacent")):
        return ContradictionStatus.INSUFFICIENT
    if any(term in lower for term in ("contradict", "disagree", "disagreement")):
        return ContradictionStatus.CONTESTED
    if any(term in lower for term in ("mixed", "heterogeneous", "tension", "while", "but")):
        return ContradictionStatus.MIXED
    if profile.get("non_supportive_signal"):
        return ContradictionStatus.NON_SUPPORTIVE
    if profile.get("mixed_signal"):
        return ContradictionStatus.MIXED
    if profile.get("indirect_signal"):
        return ContradictionStatus.INSUFFICIENT
    return ContradictionStatus.NONE


def support_for_claim(text: str, sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    claim = text.lower()
    doi_hits = [
        source for source in sources
        if source.get("doi") and str(source["doi"]).lower().rstrip(".,") in claim
    ]
    selected = doi_hits or sources[:5]
    support: list[dict[str, Any]] = []
    for index, source in enumerate(selected, start=1):
        row = {
            "source_id": str(source.get("source_id") or f"source_{index}"),
            "study": source.get("study") or source.get("title"),
            "doi": source.get("doi"),
            "url": source.get("url"),
            "support_kind": "direct_doi_match" if source in doi_hits else "candidate_source_row",
        }
        for key in ("population", "endpoint", "effect", "directness"):
            if source.get(key):
                row[key] = source[key]
        support.append(row)
    return support
