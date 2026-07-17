from __future__ import annotations

import re
from typing import Any

from contracts import ArticleType, ContradictionStatus

WEAK_PATTERN = re.compile(
    r"(\d+)\s*/\s*(\d+)\s+retained sources are (?:coded as null|indirect)",
    re.IGNORECASE,
)
DIRECT_PATTERN = re.compile(r"contains\s+(\d+)\s+direct clinical sources", re.IGNORECASE)
BUNDLE_REFERENCE_PATTERN = re.compile(r"\[bundle:(\d+)\]", re.IGNORECASE)
UNASSESSED_VALUES = {"", "unknown", "not appraised", "not_appraised", "not extracted"}
GENERIC_EVIDENCE_WORDS = {
    "about", "across", "evidence", "finding", "findings", "reported", "results",
    "review", "source", "study", "studies", "support", "supports", "suggests", "trial",
}


def claim_candidates(text: str) -> list[str]:
    candidates = []
    for line in text.splitlines():
        clean = line.strip(" -*")
        if len(clean) < 80:
            continue
        if any(marker in clean.lower() for marker in ("support", "suggest", "risk", "increase", "decrease", "null", "evidence")):
            candidates.append(clean)
    if not candidates:
        candidates = [part.strip() for part in re.split(r"\n+|(?<=[.!?])\s+", text) if len(part.strip()) >= 80]
    return candidates[:30]


def evidence_profile(*, text: str, source_bundle: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    source_bundle = source_bundle or []
    weak_ratio = 0.0
    for weak, total in WEAK_PATTERN.findall(text):
        denom = max(1, int(total))
        weak_ratio = max(weak_ratio, int(weak) / denom)
    direct_match = DIRECT_PATTERN.search(text)
    direct_count = int(direct_match.group(1)) if direct_match else None
    selected_count = len(source_bundle)
    primary_sources = [item for item in source_bundle if item.get("evidence_type") == "primary"]
    primary_count = len(primary_sources)
    directness_count = sum(
        1 for item in source_bundle if str(item.get("directness") or "").strip().lower() not in UNASSESSED_VALUES
    )
    appraised_count = sum(
        1
        for item in primary_sources
        if str(item.get("risk_of_bias") or "").strip().lower() not in UNASSESSED_VALUES
    )
    bundle_direct_count = sum(
        1 for item in source_bundle if str(item.get("directness") or "").strip().lower().startswith("direct")
    )
    if direct_count is None and directness_count:
        direct_count = bundle_direct_count
    lower = text.lower()
    claims = claim_candidates(text)
    exact_traces = sum(1 for claim in claims if support_for_claim(claim, source_bundle))
    return {
        "weak_evidence_ratio": round(weak_ratio, 4),
        "direct_clinical_sources": direct_count,
        "source_count": selected_count,
        "primary_source_ratio": round(primary_count / selected_count, 4) if selected_count else None,
        "directness_coverage": round(directness_count / selected_count, 4) if selected_count else None,
        "risk_of_bias_coverage": round(appraised_count / primary_count, 4) if primary_count else None,
        "claim_trace_count": len(claims),
        "exact_claim_trace_count": exact_traces,
        "exact_claim_trace_ratio": round(exact_traces / len(claims), 4) if claims else None,
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
    trace_count = int(profile.get("claim_trace_count") or 0)
    exact_traces = int(profile.get("exact_claim_trace_count") or 0)
    if title.lower().startswith("research synthesis:") and trace_count and exact_traces / trace_count < 0.8:
        return "adjacent_evidence_brief"
    if title.lower().startswith("research synthesis:") and (
        float(profile.get("directness_coverage") or 0) < 0.8
        or float(profile.get("risk_of_bias_coverage") or 0) < 0.8
    ):
        return "adjacent_evidence_brief"
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


def _evidence_aligns(claim: str, source: dict[str, Any]) -> bool:
    claim_words = {
        word for word in re.findall(r"[a-z0-9]+", claim.lower())
        if len(word) >= 5 and word not in GENERIC_EVIDENCE_WORDS
    }
    for value in (source.get("quote"), source.get("evidence_span"), source.get("excerpt")):
        evidence = " ".join(str(value or "").lower().split())
        if len(evidence) < 20:
            continue
        if evidence in claim.lower() or claim.lower() in evidence:
            return True
        evidence_words = {
            word for word in re.findall(r"[a-z0-9]+", evidence)
            if len(word) >= 5 and word not in GENERIC_EVIDENCE_WORDS
        }
        required = min(4, max(2, (len(claim_words) + 4) // 5))
        if len(claim_words & evidence_words) >= required:
            return True
    return False


def support_for_claim(text: str, sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    claim = text.lower()
    bundle_indexes = {int(value) - 1 for value in BUNDLE_REFERENCE_PATTERN.findall(text)}
    bundle_indexes = {index for index in bundle_indexes if 0 <= index < len(sources)}
    doi_indexes = {
        index
        for index, source in enumerate(sources)
        if source.get("doi") and str(source["doi"]).lower().rstrip(".,") in claim
    }
    cited_as_indexes = {
        index
        for index, source in enumerate(sources)
        if len(str(source.get("cited_as") or "").strip()) >= 4
        and str(source["cited_as"]).strip().lower() in claim
    }
    span_indexes = {
        index
        for index, source in enumerate(sources)
        if any(
            len(span) >= 8 and span in claim
            for span in (
                str(source.get("quote") or "").strip().lower(),
                str(source.get("evidence_span") or "").strip().lower(),
            )
        )
    }
    support: list[dict[str, Any]] = []
    for index in sorted(bundle_indexes | doi_indexes | cited_as_indexes | span_indexes):
        source = sources[index]
        if not _evidence_aligns(text, source):
            continue
        row = {
            "source_id": str(source.get("source_id") or f"source_{index + 1}"),
            "study": source.get("study") or source.get("title"),
            "doi": source.get("doi"),
            "url": source.get("url"),
            "support_kind": (
                "bundle_reference"
                if index in bundle_indexes
                else "direct_doi_match"
                if index in doi_indexes
                else "cited_as_match"
                if index in cited_as_indexes
                else "evidence_span_match"
            ),
        }
        for key in ("cited_as", "population", "endpoint", "effect", "directness", "quote", "evidence_span", "excerpt", "dw_chain_ref"):
            if source.get(key):
                row[key] = source[key]
        support.append(row)
    return support
