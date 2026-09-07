from __future__ import annotations

import re
import hashlib
from decimal import Decimal, InvalidOperation
from typing import Any

from contracts import ArticleType, ContradictionStatus

WEAK_PATTERN = re.compile(
    r"(\d+)\s*/\s*(\d+)\s+retained sources are (?:coded as null|indirect)",
    re.IGNORECASE,
)
DIRECT_PATTERN = re.compile(r"contains\s+(\d+)\s+direct clinical sources", re.IGNORECASE)
BUNDLE_REFERENCE_PATTERN = re.compile(r"\[bundle:(\d+)\]", re.IGNORECASE)
DOI_PATTERN = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.IGNORECASE)
NUMERIC_CITATION_PATTERN = re.compile(r"\[(?:\d+[\s,;-]*)+\]|\b(?:source|ref(?:erence)?)\s*#?\d+\b", re.IGNORECASE)
PMID_PATTERN = re.compile(r"\bPMID\s*:?\s*\d+\b", re.IGNORECASE)
BRACKETED_CITATION_PATTERN = re.compile(r"\[((?:\d+[\s,;-]*)+)\]")
PMID_VALUE_PATTERN = re.compile(r"\bPMID\s*:?\s*(\d+)\b", re.IGNORECASE)
QUANTITY_PATTERN = re.compile(
    r"(?<![\w./])(?P<number>[+-]?(?:(?:\d{1,3}(?:[,\s]\d{3})+|\d+)(?:\.\d+)?|\.\d+))(?:\s*-\s*|\s*)"
    r"(?P<unit>(?:%|percent(?:age)?(?:\s+points?)?|pp|mmol|mol|mmhg|bpm|hz|"
    r"mg|kg|ug|µg|μg|ng|ml|km|cm|mm|g|l|m|seconds?|minutes?|hours?|days?|weeks?|months?|years?)"
    r"(?:/[A-Za-zµμ]+)?)?(?![A-Za-z])",
    re.IGNORECASE,
)
BUNDLE_COUNT_PATTERN = re.compile(r"^\s*(?:sources?|papers?|studies|findings|receipts|claims)\b", re.IGNORECASE)
UNASSESSED_VALUES = {"", "unknown", "not appraised", "not_appraised", "not extracted"}
GENERIC_EVIDENCE_WORDS = {
    "about", "across", "evidence", "finding", "findings", "reported", "results",
    "review", "source", "study", "studies", "support", "supports", "suggests", "trial",
}
EFFECT_PATTERN = r"\b(?:(?:reduc|increas|decreas|improv|rais)(?:e[sd]?|ing)|(?:lower|inhibit)(?:s|ed|ing)?)\b"
NEGATED_EFFECT_PATTERN = re.compile(r"\b(?:not|never|no)\b(?:\W+\w+){0,3}\W+" + EFFECT_PATTERN, re.IGNORECASE)
UNIT_SCALES = {
    "kg": ("g", "1000"), "mg": ("g", ".001"), "ug": ("g", ".000001"), "ng": ("g", ".000000001"),
    "km": ("m", "1000"), "cm": ("m", ".01"), "mm": ("m", ".001"), "ml": ("l", ".001"),
    "minute": ("second", "60"), "hour": ("second", "3600"), "day": ("second", "86400"),
}


def claim_candidates(text: str) -> list[str]:
    candidates = []
    for line in text.splitlines():
        clean = line.strip(" -*")
        if len(clean) < 80 and not (re.search(r"[A-Za-z]{3}", clean) and _quantity_tokens(clean)):
            continue
        if _quantity_tokens(clean) or any(marker in clean.lower() for marker in ("support", "suggest", "risk", "increase", "decrease", "null", "evidence")):
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
    primary_sources = [item for item in source_bundle if item.get("evidence_type") == "primary"
                       and str(item.get("directness") or "").lower() != "protocol"
                       and item.get("evidence_context") != "context"]
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
    citation_traces = sum(
        1 for claim in claims if support_for_claim(claim, source_bundle, require_evidence_alignment=False)
    )
    exact_traces = sum(1 for claim in claims if support_for_claim(claim, source_bundle))
    quantitative_claims = quantitative_claim_candidates(text)
    quantitative_traces = sum(
        1
        for claim in quantitative_claims
        if support_for_claim(claim, source_bundle, require_quantitative_agreement=True)
    )
    return {
        "weak_evidence_ratio": round(weak_ratio, 4),
        "direct_clinical_sources": direct_count,
        "source_count": selected_count,
        "primary_source_ratio": round(primary_count / selected_count, 4) if selected_count else None,
        "directness_coverage": round(directness_count / selected_count, 4) if selected_count else None,
        "risk_of_bias_coverage": round(appraised_count / primary_count, 4) if primary_count else None,
        "claim_trace_count": len(claims),
        "citation_trace_count": citation_traces,
        "exact_claim_trace_count": exact_traces,
        "exact_claim_trace_ratio": round(exact_traces / len(claims), 4) if claims else None,
        "quantitative_claim_count": len(quantitative_claims),
        "quantitative_claim_trace_count": quantitative_traces,
        "quantitative_claim_trace_ratio": (
            round(quantitative_traces / len(quantitative_claims), 4) if quantitative_claims else None
        ),
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


def _effect_subjects(claim: str) -> list[set[str]]:
    # A bounded lexical guard, not semantic entailment or a drug-name dictionary.
    effect = re.search(EFFECT_PATTERN, claim.lower())
    prefix = claim[:effect.start()] if effect else ""
    prefix = re.split(r"\b(?:that|reported|showed|found)\b", prefix.lower())[-1]
    if re.search(r"\b(?:was|were|is|been|be)\s+(?:not\s+)?$", prefix):
        return []  # Passive voice has no intervention subject before the verb.
    subjects = [
        {
            word for word in re.findall(r"[a-z]+", part)
            if len(word) >= 4 and word not in GENERIC_EVIDENCE_WORDS | {"this", "that", "with", "after", "were", "have", "been"}
        }
        for part in re.split(r"\band\b", prefix)
    ]
    return [subject for subject in subjects if subject]


def _subject_in_passage(subject: set[str], evidence: str) -> bool:
    targets = _effect_subjects(evidence) or [set(re.findall(r"[a-z0-9]+", evidence.lower()))]
    return any(subject <= target for target in targets)


def _effect_context(text: str) -> tuple[set[str], set[str]]:
    directions = set()
    endpoints: set[str] = set()
    for effect in re.finditer(EFFECT_PATTERN, text.lower()):
        verb = effect.group()
        directions.add("up" if re.match(r"increas|rais", verb) else "down" if re.match(r"reduc|decreas|lower|inhibit", verb) else "other")
        tail = re.split(r"\b(?:by|from|to|compared)\b|[\d.!?;]", text[effect.end():].lower(), maxsplit=1)[0]
        endpoints.update(word for word in re.findall(r"[a-z]+", tail)
                         if word not in {"the", "a", "an", "of", "in", "and", "was", "were", "with"})
    if not directions and (match := re.match(r"([A-Za-z -]+?)\s+(?:was|were|is|are)\s+[+-]?\d", text)):
        endpoints.update(re.findall(r"[a-z]+", match[1].lower()))
    return directions, endpoints


def _evidence_passages(source: dict[str, Any]) -> list[str]:
    passages = []
    for field in ("quote", "evidence_span", "excerpt"):
        sentences = [s.strip().lower() for s in re.split(r"\n+|(?<=[.!?;])\s+", str(source.get(field) or "")) if s.strip()]
        for index, sentence in enumerate(sentences):
            passages.append(sentence)
            if index + 1 < len(sentences) and not (
                re.search(EFFECT_PATTERN, sentence) and re.search(EFFECT_PATTERN, sentences[index + 1])
            ):
                passages.append(" ".join(sentences[index:index + 2]))
    return passages


def _passage_aligns(claim: str, evidence: str) -> bool:
    subjects = _effect_subjects(claim)
    claim_quantities = _quantity_tokens(claim)
    directions, endpoints = _effect_context(claim)
    evidence_directions, _ = _effect_context(evidence)
    if directions and evidence_directions and directions != evidence_directions:
        return False
    if endpoints and not endpoints <= set(re.findall(r"[a-z]+", evidence.lower())):
        return False
    if subjects and (
        not any(_subject_in_passage(subject, evidence) for subject in subjects)
        or (claim_quantities and not _quantity_tokens(evidence))
        or bool(NEGATED_EFFECT_PATTERN.search(claim)) != bool(NEGATED_EFFECT_PATTERN.search(evidence))
    ):
        return False
    claim_words = {
        word for word in re.findall(r"[a-z0-9]+", claim.lower())
        if len(word) >= 5 and word not in GENERIC_EVIDENCE_WORDS
    }
    if evidence in claim.lower() or claim.lower() in evidence:
        return True
    if claim_quantities and claim_quantities <= _quantity_tokens(evidence):
        return True
    evidence_words = {
        word for word in re.findall(r"[a-z0-9]+", evidence)
        if len(word) >= 5 and word not in GENERIC_EVIDENCE_WORDS
    }
    required = min(4, max(2, (len(claim_words) + 4) // 5))
    return len(claim_words & evidence_words) >= required


def _aligned_passages(claim: str, source: dict[str, Any]) -> list[str]:
    return [
        passage.strip().lower()
        for passage in _evidence_passages(source)
        if _passage_aligns(claim, passage)
    ]


def _evidence_aligns(claim: str, source: dict[str, Any]) -> bool:
    return bool(_aligned_passages(claim, source))


def _subjects_covered(claim: str, sources: list[dict[str, Any]]) -> bool:
    passages = [passage for source in sources for passage in _aligned_passages(claim, source)]
    return all(
        any(_subject_in_passage(subject, passage) for passage in passages)
        for subject in _effect_subjects(claim)
    )


def _quantity_tokens(text: str, sources: list[dict[str, Any]] | None = None) -> set[tuple[str, str]]:
    cleaned = BUNDLE_REFERENCE_PATTERN.sub(
        " ",
        DOI_PATTERN.sub(" ", PMID_PATTERN.sub(" ", NUMERIC_CITATION_PATTERN.sub(" ", text))),
    )
    for source in sources or []:
        for field in ("doi", "cited_as"):
            value = str(source.get(field) or "").strip()
            if value:
                cleaned = re.sub(re.escape(value), " ", cleaned, flags=re.IGNORECASE)
    tokens: set[tuple[str, str]] = set()
    for match in QUANTITY_PATTERN.finditer(cleaned):
        raw_number = re.sub(r"[,\s]", "", match.group("number"))
        raw_unit = (match.group("unit") or "").strip().lower()
        if not raw_unit and BUNDLE_COUNT_PATTERN.match(cleaned[match.end() :]):
            continue
        try:
            number = format(Decimal(raw_number).normalize(), "f")
        except InvalidOperation:
            continue
        if not raw_unit and Decimal(raw_number) == int(Decimal(raw_number)) and 1900 <= int(Decimal(raw_number)) <= 2100:
            continue
        unit = raw_unit.replace("μ", "u").replace("µ", "u")
        if unit.startswith("percent"):
            unit = "pp" if "point" in unit else "%"
        elif unit.endswith("s") and unit not in {"mmhg"}:
            unit = unit[:-1]
        unit, scale = UNIT_SCALES.get(unit, (unit, "1"))
        number = format((Decimal(number) * Decimal(scale)).normalize(), "f")
        tokens.add((number, unit))
    return tokens


def quantity_tokens(text: str, sources: list[dict[str, Any]] | None = None) -> set[tuple[str, str]]:
    return _quantity_tokens(text, sources)


def quantitative_claim_candidates(text: str) -> list[str]:
    return [
        part.strip()
        for part in re.split(r"\n+|(?<=[.!?])\s+", text)
        if re.search(r"[A-Za-z]{3}", part) and _quantity_tokens(part)
    ][:30]


def quantitative_table_rows(sections: dict[str, str]) -> list[dict[str, str]]:
    rows = []
    for section, text in sections.items():
        header: list[str] = []
        for number, line in enumerate(text.splitlines(), 1):
            if not line.strip().startswith("|"):
                header = []
                continue
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            names = [cell.lower() for cell in cells]
            if "endpoint" in names and "value" in names:
                header = names
            elif header and len(cells) == len(header):
                fields = dict(zip(header, cells))
                if _quantity_tokens(fields["value"]):
                    rows.append({"location": f"{section}, line {number}", "text": line,
                                 "endpoint": fields["endpoint"], "value": fields["value"]})
    return rows


def table_row_support(row: dict[str, str], sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    references = support_for_claim(row["text"], sources, require_evidence_alignment=False)
    endpoint = set(re.findall(r"[a-z]+", row["endpoint"].lower())) - {"of", "the", "and", "in"}
    quantities = _quantity_tokens(row["value"])
    return [source for source in references if endpoint and any(
        endpoint <= set(re.findall(r"[a-z]+", passage))
        and quantities <= _quantity_tokens(passage)
        for passage in _evidence_passages(source)
    )]


def _passage_differences(claim: str, passage: str) -> list[str]:
    differences = []
    directions, endpoints = _effect_context(claim)
    other_directions, _ = _effect_context(passage)
    if directions and other_directions and directions != other_directions:
        differences.append("direction")
    if endpoints and not endpoints <= set(re.findall(r"[a-z]+", passage.lower())):
        differences.append("endpoint")
    if any(not _subject_in_passage(subject, passage) for subject in _effect_subjects(claim)):
        differences.append("intervention_or_population")
    if not _quantity_tokens(claim) <= _quantity_tokens(passage):
        differences.append("number_or_unit")
    if bool(NEGATED_EFFECT_PATTERN.search(claim)) != bool(NEGATED_EFFECT_PATTERN.search(passage)):
        differences.append("negation")
    return differences


def claim_assessment(claim: str, sources: list[dict[str, Any]]) -> dict[str, Any]:
    references = support_for_claim(claim, sources, require_evidence_alignment=False)
    supported = support_for_claim(claim, sources, require_quantitative_agreement=True)
    passages = [passage for source in references for passage in _evidence_passages(source)]
    comparisons = [{"source_id": source["source_id"], "passage": passage[:2000], "truncated": len(passage) > 2000,
                    "mismatch_axes": _passage_differences(claim, passage)}
                   for source in references for field in ("quote", "evidence_span", "excerpt")
                   if (passage := str(source.get(field) or "").strip())]
    status = "SUPPORTED" if supported else "NEEDS_SEMANTIC_REVIEW" if passages else "INSUFFICIENT_SOURCE_TEXT"
    # Conflicting lexical comparisons need context, not a fabricated contradiction verdict.
    if supported and any(row["mismatch_axes"] for row in comparisons):
        status = "NEEDS_SEMANTIC_REVIEW"
    return {
        "claim_id": "claim_" + hashlib.sha256(claim.encode()).hexdigest()[:16],
        "claim": claim,
        "status": status,
        "sources": [str(source.get("doi") or source.get("pmid") or source.get("cited_as") or "") for source in references],
        "passages_considered": passages[:6],
        "comparisons": comparisons[:6],
        "required_check": "Check intervention, endpoint, direction, population and number/unit against these source-owned passages; lexical failure alone is not a proven contradiction.",
    }


def _quantities_agree(claim: str, sources: list[dict[str, Any]]) -> bool:
    claim_tokens = _quantity_tokens(claim, sources)
    if not claim_tokens:
        return True
    evidence_tokens: set[tuple[str, str]] = set()
    for source in sources:
        evidence = " ".join(_aligned_passages(claim, source))
        evidence_tokens.update(_quantity_tokens(evidence))
    evidence_tokens.update((number, "") for number, _ in tuple(evidence_tokens))
    return claim_tokens <= evidence_tokens


def support_for_claim(
    text: str,
    sources: list[dict[str, Any]],
    *,
    require_quantitative_agreement: bool = False,
    require_evidence_alignment: bool = True,
) -> list[dict[str, Any]]:
    claim = text.lower()
    bundle_indexes = {int(value) - 1 for value in BUNDLE_REFERENCE_PATTERN.findall(text)}
    bundle_indexes = {index for index in bundle_indexes if 0 <= index < len(sources)}
    numeric_indexes = {
        int(value) - 1
        for group in BRACKETED_CITATION_PATTERN.findall(text)
        for value in re.findall(r"\d+", group)
        if 0 < int(value) <= len(sources)
    }
    doi_indexes = {
        index
        for index, source in enumerate(sources)
        if source.get("doi") and str(source["doi"]).lower().rstrip(".,") in claim
    }
    pmids = set(PMID_VALUE_PATTERN.findall(text))
    pmid_indexes = {
        index
        for index, source in enumerate(sources)
        if re.sub(r"\D", "", str(source.get("pmid") or "")) in pmids
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
    aligned_indexes = [
        index
        for index in sorted(
            bundle_indexes | numeric_indexes | doi_indexes | pmid_indexes | cited_as_indexes | span_indexes
        )
        if not require_evidence_alignment or _evidence_aligns(text, sources[index])
    ]
    aligned_sources = [sources[index] for index in aligned_indexes]
    if (require_evidence_alignment and not _subjects_covered(text, aligned_sources)) or (
        require_quantitative_agreement and not _quantities_agree(text, aligned_sources)
    ):
        return []
    support: list[dict[str, Any]] = []
    for index in aligned_indexes:
        source = sources[index]
        row = {
            "source_id": str(source.get("source_id") or f"source_{index + 1}"),
            "study": source.get("study") or source.get("title"),
            "doi": source.get("doi"),
            "url": source.get("url"),
            "support_kind": (
                "bundle_reference"
                if index in bundle_indexes
                else "numeric_citation"
                if index in numeric_indexes
                else "direct_doi_match"
                if index in doi_indexes
                else "direct_pmid_match"
                if index in pmid_indexes
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
