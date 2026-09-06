from __future__ import annotations

import re
import urllib.parse
from typing import Literal

from pydantic import BaseModel, ValidationError, model_validator

from .models import ArticleType, GateResult
from .templates import RAPID_EVIDENCE_SYNTHESIS, publication_template_for

SUBMISSION_POLICY_VERSION = "submission-policy-v2"

_DOI_PATTERN = re.compile(r"^10\.\d{4,}/\S+$")
# Prose-citation patterns: identifiers an author cites inside section text.
# Every one must be a member of the submitted source bundle — citing receipts
# the bundle does not carry is the canonical fabrication/slip vector.
_PROSE_DOI_PATTERN = re.compile(r"\b10\.\d{4,9}/", re.IGNORECASE)
_PROSE_PMID_PATTERN = re.compile(r"\bPMID[:\s#-]*(\d{4,12})\b", re.IGNORECASE)
_TABLE_SEPARATOR_PATTERN = re.compile(r"^:?-{3,}:?$")
_TABLE_HEADER_CELLS = {
    "comparator",
    "corpus slice",
    "directness",
    "evidence domain",
    "finding",
    "main limitation",
    "outcome class",
    "population",
    "source",
    "strongest signal",
}
_TOPIC_STOPWORDS = {
    "across",
    "adjacent",
    "brief",
    "evidence",
    "findings",
    "full",
    "generating",
    "hypothesis",
    "map",
    "memo",
    "mechanistic",
    "paper",
    "research",
    "review",
    "scoping",
    "signal",
    "signals",
    "sources",
    "synthesis",
}
_TOPIC_ALIASES = {
    "exercise": (
        "physical activity",
        "training",
        "fitness",
        "aerobic",
        "resistance",
        "sedentary",
        "muscle",
        "strength",
        "imst",
    ),
    "ai": ("artificial intelligence", "llm", "language model", "model"),
    "models": ("model",),
}
_ALPHA_NOVELTY_TERMS = {
    "bound",
    "bounded",
    "boundary",
    "context",
    "falsifiable",
    "gap",
    "mixed",
    "novel",
    "receipt",
    "signal",
    "tension",
}

# Versioned extension surface observed in the live corpus. Unknown nested keys
# are rejected instead of being silently ignored, while established producer
# metadata remains forward-compatible with the v2 gate.
_SOURCE_BUNDLE_FIELDS = frozenset({
    "added_in_normalization", "card", "cited_as", "claim_span", "directness",
    "doi", "effect_direction", "endpoint", "evidence_context", "evidence_origin",
    "evidence_recency", "evidence_span", "evidence_tier", "evidence_type", "excerpt",
    "id", "intervention", "is_retracted", "journal", "journal_name", "openalex_id",
    "outcome_class", "payload", "pmid", "population", "publication_type", "quote",
    "quote_verified", "registry_id", "relevance", "risk_of_bias", "setting", "source",
    "source_content_hash", "source_fact", "source_identity_hash", "source_index",
    "source_record_hash", "source_record_locator", "source_role", "source_type", "title",
    "type", "url", "year",
})


def _clean_doi(value: str) -> str:
    return value.strip().rstrip(".,;").lower()


def _prose_doi_end(prose: str, start: int) -> int:
    # Scan once: nested suffix brackets belong to the DOI, outer closers do not.
    stack: list[str] = []
    pairs = {")": "(", "]": "[", "}": "{", ">": "<"}
    for index in range(start, len(prose)):
        char = prose[index]
        if char.isspace() or char in "\"',;":
            return index
        if char in "([{<":
            stack.append(char)
        elif char in pairs:
            if not stack:
                return index
            if stack[-1] == pairs[char]:
                stack.pop()
    return len(prose)


def _trusted_host(host: str, expected: str) -> bool:
    normalized = host.strip().lower().rstrip(".")
    return normalized == expected or normalized.endswith(f".{expected}")


def _citation_membership_failures(sections: dict[str, str], source_bundle: list[dict]) -> list[str]:
    prose = "\n".join(str(value) for value in sections.values())
    bundle_dois = {_clean_doi(str(entry.get("doi") or "")) for entry in source_bundle}
    bundle_pmids = {str(entry.get("pmid") or entry.get("id") or "").strip() for entry in source_bundle}
    cited_dois = set()
    cursor = 0
    while match := _PROSE_DOI_PATTERN.search(prose, cursor):
        end = _prose_doi_end(prose, match.end())
        cited_dois.add(_clean_doi(prose[match.start():end]))
        cursor = end
    cited_pmids = set(_PROSE_PMID_PATTERN.findall(prose))
    missing = [f"doi:{doi}" for doi in sorted(cited_dois - bundle_dois)]
    missing.extend(f"pmid:{pmid}" for pmid in sorted(cited_pmids - bundle_pmids))
    return missing


def _topic_anchors(title: str) -> set[str]:
    anchors: set[str] = set()
    for segment in re.split(r":|\s+[—-]\s+", title.lower()):
        tokens = {
            token for token in re.findall(r"[a-z0-9]+", segment)
            if len(token) > 2 or token == "ai"  # nosec B105 - topic acronym, not a credential
        }
        anchors = {token for token in tokens if token not in _TOPIC_STOPWORDS}
        if anchors:
            break
    for token in tuple(anchors):
        anchors.update(_TOPIC_ALIASES.get(token, ()))
    return anchors


def _markdown_table_rows(sections: dict[str, str]) -> list[str]:
    rows: list[str] = []
    for section in sections.values():
        for raw_line in str(section).splitlines():
            line = raw_line.strip()
            if not (line.startswith("|") and line.endswith("|")):
                continue
            cells = [cell.strip() for cell in line.strip("|").split("|")]
            if len(cells) < 3 or all(_TABLE_SEPARATOR_PATTERN.fullmatch(cell) for cell in cells):
                continue
            if sum(cell.lower() in _TABLE_HEADER_CELLS for cell in cells) >= 2:
                continue
            lower = " ".join(cells).lower()
            if "finding" in lower and ("source" in lower or "population" in lower):
                continue
            rows.append(" ".join(cell for cell in cells if cell))
    return rows


def _matches_topic(row: str, anchors: set[str]) -> bool:
    normalized = " ".join(re.findall(r"[a-z0-9]+", row.lower()))
    tokens = set(normalized.split())
    return any(anchor in normalized if " " in anchor else anchor in tokens for anchor in anchors)


def _topic_coherence_failures(*, title: str, sections: dict[str, str]) -> list[str]:
    anchors = _topic_anchors(title)
    rows = _markdown_table_rows(sections)
    if not anchors or not rows:
        return []
    weak = [row[:160] for row in rows if not _matches_topic(row, anchors)]
    if len(weak) >= 2 or (len(weak) / len(rows)) > 0.15:
        return weak[:5]
    return []


def _alpha_title_novelty_failures(*, title: str, sections: dict[str, str]) -> list[str]:
    clean_title = " ".join(str(title or "").split())
    if not clean_title:
        return []
    tokens = re.findall(r"[a-z0-9]+", clean_title.lower())
    failures: list[str] = []
    if len(tokens) < 4 or ("/" in clean_title and len(tokens) <= 5):
        failures.append("alpha memo title must be human-readable and specific, not a raw topic/query")
    prose = " ".join(str(value or "").lower() for value in sections.values())
    if not any(term in prose for term in _ALPHA_NOVELTY_TERMS):
        failures.append("alpha memo must state a bounded signal, tension, gap, or falsifiable novelty angle")
    return failures


class SourceBundleEntry(BaseModel):
    title: str
    url: str | None = None
    doi: str | None = None
    pmid: str | None = None
    openalex_id: str | None = None
    registry_id: str | None = None
    year: int | None = None
    evidence_type: Literal["primary", "review"]
    publication_type: str | None = None
    relevance: float | None = None
    # The in-text citation token the manuscript uses for this source (e.g.
    # "Zufry 2025"). Lets reviewers cross-walk author-year prose citations to
    # bundle entries instead of flagging present sources as ungrounded.
    cited_as: str | None = None
    directness: str | None = None
    risk_of_bias: str | None = None
    quote: str | None = None
    evidence_span: str | None = None
    excerpt: str | None = None

    @model_validator(mode="before")
    @classmethod
    def preserve_source_fact(cls, data: object) -> object:
        if not isinstance(data, dict):
            return data
        values = dict(data)
        unknown = sorted(str(key) for key in values if str(key) not in _SOURCE_BUNDLE_FIELDS)
        if unknown:
            raise ValueError(f"unknown source fields: {', '.join(unknown)}")
        parsed = urllib.parse.urlparse(str(values.get("url") or ""))
        host = parsed.hostname or ""
        path = urllib.parse.unquote(parsed.path).strip("/")
        if _trusted_host(host, "doi.org") and not values.get("doi") and _DOI_PATTERN.match(path):
            values["doi"] = path
        if _trusted_host(host, "pubmed.ncbi.nlm.nih.gov") and not values.get("pmid") and path.isdigit():
            values["pmid"] = path
        if _trusted_host(host, "openalex.org") and not values.get("openalex_id") and re.fullmatch(r"W\d+", path, re.I):
            values["openalex_id"] = path
        if any(values.get(key) for key in ("quote", "evidence_span", "excerpt")):
            return values
        fact = data.get("source_fact")
        if not isinstance(fact, dict):
            return values
        values["excerpt"] = next(
            (fact.get(key) for key in ("canonical_phrase", "finding", "source_excerpt") if fact.get(key)),
            None,
        )
        return values


class SubmissionTemplateV1(BaseModel):
    article_type: str = ArticleType.RAPID_EVIDENCE_SYNTHESIS.value
    required_sections: tuple[str, ...] = RAPID_EVIDENCE_SYNTHESIS.required_sections
    recommended_sections: tuple[str, ...] = ()
    review_checks: tuple[str, ...] = RAPID_EVIDENCE_SYNTHESIS.review_checks
    minimum_citations: int = 12
    minimum_recency_ratio: float = 0.5
    minimum_research_question_words: int = 50
    research_question_section: str = "Research Question"
    minimum_body_word_count: int = 0


SUBMISSION_TEMPLATE_V1 = SubmissionTemplateV1()
RECENT_PUBLICATION_YEAR_FLOOR = 2020

# Per-article-type intake thresholds. Alpha memos are shorter than papers but
# still need enough independent receipts for public auto-acceptance.
_TYPE_THRESHOLDS: dict[str, dict[str, object]] = {
    ArticleType.ALPHA_MEMO.value: {
        "minimum_citations": 5,
        "minimum_recency_ratio": 0.0,
        "minimum_research_question_words": 0,
    },
    ArticleType.RAPID_EVIDENCE_SYNTHESIS.value: {
        "minimum_citations": 12,
        "minimum_recency_ratio": 0.5,
        "minimum_research_question_words": 50,
    },
    ArticleType.EMPIRICAL_STUDY.value: {
        "minimum_citations": 12,
        "minimum_recency_ratio": 0.5,
        "minimum_research_question_words": 50,
    },
    ArticleType.EVIDENCE_MAP.value: {
        # An evidence map only earns its keep on source-rich topics — it must
        # survey a real spread of findings, so it needs a meaningful citation
        # floor. But it deliberately does not converge to one claim, so the
        # research-question word budget is relaxed (the Scope section is short).
        "minimum_citations": 10,
        "minimum_recency_ratio": 0.3,
        "minimum_research_question_words": 30,
    },
    ArticleType.RESEARCH_SYNTHESIS.value: {
        # V3 full-paper lane keeps the live Researka source floor at 12.
        # Keep the original low body floor: 2-3k word papers may pass.
        "minimum_citations": 12,
        # Synthesis papers must engage with foundational mechanism work
        # (often pre-2020 for established pathways like mTOR / autophagy),
        # so the recency floor is lower than RES. Current evidence is still required,
        # but the corpus is allowed to be majority-foundational.
        "minimum_recency_ratio": 0.4,
        # Abstract for a synthesis paper must convey the question, scope, and
        # headline findings — needs more words than a one-line research
        # question prompt.
        "minimum_research_question_words": 75,
    },
}


def submission_template_for(article_type: str) -> SubmissionTemplateV1:
    publication_template = publication_template_for(article_type)
    thresholds = _TYPE_THRESHOLDS.get(publication_template.article_type, {})
    return SubmissionTemplateV1(
        article_type=publication_template.article_type,
        required_sections=publication_template.required_sections,
        recommended_sections=publication_template.recommended_sections,
        review_checks=publication_template.review_checks,
        research_question_section=publication_template.research_question_section,
        minimum_body_word_count=publication_template.minimum_body_word_count,
        **thresholds,  # type: ignore[arg-type]
    )


def _normalize_source_bundle(source_bundle: list[dict]) -> list[SourceBundleEntry]:
    normalized: list[SourceBundleEntry] = []
    for index, entry in enumerate(source_bundle):
        try:
            normalized.append(SourceBundleEntry.model_validate(entry))
        except ValidationError as exc:
            error = exc.errors()[0]
            raise ValueError(
                f"submission_template:source_bundle_entry_invalid:{index}:"
                f"{error['type']}:{error.get('msg', '')}"
            ) from exc
    return normalized


_NON_LOAD_BEARING_PUBLICATION_TYPE = re.compile(
    r"\b(?:correction|corrigendum|erratum|retraction|withdrawn|expression of concern|study protocol|protocol for)\b",
    re.IGNORECASE,
)
_NON_LOAD_BEARING_TITLE = re.compile(
    r"^\s*(?:\[(?:retracted|withdrawn)\]\s*)?(?:(?:correction|corrigendum|erratum|retraction|withdrawal|expression of concern)(?::|\s+(?:of|to)\b)|(?:a\s+)?study protocol\b|protocol for\b)",
    re.IGNORECASE,
)


def _has_stable_locator(entry: SourceBundleEntry) -> bool:
    doi = str(entry.doi or "").strip()
    pmid = str(entry.pmid or "").strip()
    openalex = str(entry.openalex_id or "").strip()
    registry = str(entry.registry_id or "").strip()
    return bool(
        _DOI_PATTERN.match(doi)
        or re.fullmatch(r"\d{4,12}", pmid)
        or re.fullmatch(r"(?:https?://openalex\.org/)?W\d+", openalex, re.IGNORECASE)
        or re.fullmatch(r"[A-Za-z][A-Za-z0-9._/-]{3,127}", registry)
        or re.match(r"^https?://\S+$", str(entry.url or "").strip(), re.IGNORECASE)
    )


def _stable_locator_keys(entry: SourceBundleEntry) -> set[str]:
    values = (
        ("doi", _clean_doi(str(entry.doi or ""))),
        ("pmid", str(entry.pmid or "").strip()),
        ("openalex", str(entry.openalex_id or "").strip().lower().removeprefix("https://openalex.org/")),
        ("registry", str(entry.registry_id or "").strip().lower()),
        ("url", str(entry.url or "").strip().lower().rstrip("/")),
    )
    return {f"{kind}:{value}" for kind, value in values if value}


def _has_registered_locator(entry: SourceBundleEntry) -> bool:
    return any((entry.doi, entry.pmid, entry.openalex_id, entry.registry_id))


def _is_non_load_bearing(entry: SourceBundleEntry) -> bool:
    return bool(
        _NON_LOAD_BEARING_TITLE.search(entry.title)
        or _NON_LOAD_BEARING_PUBLICATION_TYPE.search(entry.publication_type or "")
    )


def _int_value(value: object) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return 0


def _alpha_source_exception(
    article_type: str, citation_count: int, evidence_bundle: dict | None, *, trusted: bool
) -> bool:
    if not trusted or article_type != ArticleType.ALPHA_MEMO.value or not 2 <= citation_count <= 4:
        return False
    verdict = (evidence_bundle or {}).get("publish_verdict")
    if not isinstance(verdict, dict):
        return False
    axes_raw = verdict.get("axes")
    axes = axes_raw if isinstance(axes_raw, dict) else {}
    if verdict.get("decision") != "ready_to_publish" or verdict.get("publish_tier") != "TIER_1":
        return False
    if verdict.get("maturity_level") != "L5" and verdict.get("confidence_label") != "evidence_backed_signal":
        return False
    if verdict.get("blockers") or axes.get("cross_domain_forced") or axes.get("feed_scope_mismatch"):
        return False
    bound = max(
        _int_value(axes.get("bound_receipts")),
        _int_value(axes.get("a_core_receipts")),
        _int_value(axes.get("available_bound_receipts")),
    )
    return bound >= 2


# Intake gates the author can clear by correcting the submission itself — the
# evidence exists, its presentation or identification is wrong. Failing only
# these earns a revise (resubmittable) rather than a terminal reject. Gates
# absent from this set signal insufficient or mismatched evidence, where the
# corpus itself is inadequate, and stay terminal.
REVISABLE_INTAKE_GATES = frozenset({
    "minimum_citations",
    "recency_ratio",
    "alpha_title_novelty",
    "topic_coherence",
    "source_bundle_schema",
    "source_identity",
    "primary_source_identity",
    "source_uniqueness",
    "source_role",
    "source_evidence_receipt",
    "doi_sanity",
    "citation_membership",
    "research_question_word_budget",
    "minimum_body_word_count",
    "structure_gate",
    # Publish-gate defects evaluated during intake (runtime_core.gates): stray
    # pipeline text in the body and count bookkeeping are both presentation
    # errors the author can strip or correct. "core_claims_resolved" is
    # deliberately absent — unresolved title/abstract/conclusion claims mean
    # the work itself is unfinished, not mispresented, so it stays terminal.
    "leakage_blocker",
    "count_reconciliation",
})


def intake_failures_are_revisable(gate_names: list[str]) -> bool:
    """True when every failed intake gate is author-correctable."""
    return bool(gate_names) and all(name in REVISABLE_INTAKE_GATES for name in gate_names)


def run_submission_template_checks(
    *,
    title: str = "",
    sections: dict[str, str],
    source_bundle: list[dict],
    article_type: str = ArticleType.RAPID_EVIDENCE_SYNTHESIS.value,
    template: SubmissionTemplateV1 | None = None,
    evidence_bundle: dict | None = None,
    alpha_exception_trusted: bool = False,
) -> list[GateResult]:
    active_template = template or submission_template_for(article_type)
    results: list[GateResult] = []

    # Research question gate: which section to inspect depends on article type.
    # RES uses an explicit "Research Question" section; synthesis papers state
    # the question in the Abstract or Introduction.
    rq_section = active_template.research_question_section
    research_question = str(sections.get(rq_section, "")).strip()
    question_words = len(research_question.split())
    results.append(
        GateResult(
            name="research_question_word_budget",
            passed=question_words >= active_template.minimum_research_question_words,
            reason=(
                f"`{rq_section}` must contain at least {active_template.minimum_research_question_words} words; "
                f"received {question_words}"
            ),
        )
    )

    # Body word count gate (synthesis-tier only — minimum=0 for RES disables it).
    if active_template.minimum_body_word_count > 0:
        body_words = sum(
            len(str(sections.get(name, "")).split())
            for name in (*active_template.required_sections, *active_template.recommended_sections)
        )
        results.append(
            GateResult(
                name="minimum_body_word_count",
                passed=body_words >= active_template.minimum_body_word_count,
                reason=(
                    f"synthesis-paper body (required + recommended sections) must contain at least "
                    f"{active_template.minimum_body_word_count} words; received {body_words}"
                ),
            )
        )

    try:
        normalized_bundle = _normalize_source_bundle(source_bundle)
    except ValueError as exc:
        return [GateResult(name="source_bundle_schema", passed=False, reason=str(exc))]

    missing_locators = [index for index, entry in enumerate(normalized_bundle) if not _has_stable_locator(entry)]
    results.append(
        GateResult(
            name="source_identity",
            passed=not missing_locators,
            reason=(
                "every source must carry a stable DOI, PMID, OpenAlex, registry, or canonical URL"
                + (f"; missing at indices {missing_locators}" if missing_locators else "")
            ),
        )
    )
    duplicate_sources: list[int] = []
    seen_sources: set[str] = set()
    for index, entry in enumerate(normalized_bundle):
        keys = _stable_locator_keys(entry)
        if keys & seen_sources:
            duplicate_sources.append(index)
        else:
            seen_sources.update(keys)
    results.append(
        GateResult(
            name="source_uniqueness",
            passed=not duplicate_sources,
            reason=(
                "source floors count unique registered records only"
                + (f"; duplicates at indices {duplicate_sources}" if duplicate_sources else "")
            ),
        )
    )
    invalid_primary_roles = [
        index
        for index, entry in enumerate(normalized_bundle)
        if entry.evidence_type == "primary" and _is_non_load_bearing(entry)
    ]
    results.append(
        GateResult(
            name="source_role",
            passed=not invalid_primary_roles,
            reason=(
                "corrections, retractions, expressions of concern, and protocols cannot be primary evidence"
                + (f"; invalid at indices {invalid_primary_roles}" if invalid_primary_roles else "")
            ),
        )
    )
    unregistered_primary = [
        index
        for index, entry in enumerate(normalized_bundle)
        if entry.evidence_type == "primary" and not _has_registered_locator(entry)
    ]
    results.append(
        GateResult(
            name="primary_source_identity",
            passed=not unregistered_primary,
            reason=(
                "primary evidence requires a DOI, PMID, OpenAlex, or registry identifier"
                + (f"; URL-only primary sources at indices {unregistered_primary}" if unregistered_primary else "")
            ),
        )
    )
    missing_evidence = [
        index
        for index, entry in enumerate(normalized_bundle)
        if not _is_non_load_bearing(entry)
        and not any(len(str(value or "").strip()) >= 20 for value in (entry.quote, entry.evidence_span, entry.excerpt))
    ]
    results.append(
        GateResult(
            name="source_evidence_receipt",
            passed=not missing_evidence,
            reason=(
                "every load-bearing source requires a quote, evidence span, or excerpt"
                + (f"; missing at indices {missing_evidence}" if missing_evidence else "")
            ),
        )
    )
    eligible_bundle: list[SourceBundleEntry] = []
    eligible_keys: set[str] = set()
    for entry in normalized_bundle:
        keys = _stable_locator_keys(entry)
        if _is_non_load_bearing(entry) or not keys or keys & eligible_keys:
            continue
        eligible_keys.update(keys)
        eligible_bundle.append(entry)
    citation_count = len(eligible_bundle)
    citation_floor_exception = _alpha_source_exception(
        article_type, citation_count, evidence_bundle, trusted=alpha_exception_trusted
    )
    citation_reason = f"source bundle must contain at least {active_template.minimum_citations} citations"
    if article_type == ArticleType.ALPHA_MEMO.value:
        citation_reason += " or qualify for the 2-4 source alpha exception"
    results.append(
        GateResult(
            name="minimum_citations",
            passed=citation_count >= active_template.minimum_citations or citation_floor_exception,
            reason=citation_reason,
        )
    )

    if active_template.article_type == ArticleType.ALPHA_MEMO.value:
        alpha_failures = _alpha_title_novelty_failures(title=title, sections=sections)
        results.append(
            GateResult(
                name="alpha_title_novelty",
                passed=not alpha_failures,
                reason="; ".join(alpha_failures) or "alpha memo title and novelty signal are public-ready",
            )
        )

    recent_count = sum(
        1
        for entry in eligible_bundle
        if isinstance(entry.year, int) and entry.year >= RECENT_PUBLICATION_YEAR_FLOOR
    )
    recent_ratio = recent_count / citation_count if citation_count else 0.0
    results.append(
        GateResult(
            name="recency_ratio",
            passed=recent_ratio >= active_template.minimum_recency_ratio,
            reason=(
                f"at least {active_template.minimum_recency_ratio:.0%} of citations must be from "
                f"{RECENT_PUBLICATION_YEAR_FLOOR}+; received {recent_ratio:.0%}"
            ),
        )
    )

    malformed_dois = [
        index
        for index, entry in enumerate(normalized_bundle)
        if entry.doi is not None and not _DOI_PATTERN.match(str(entry.doi).strip())
    ]
    if malformed_dois:
        results.append(
            GateResult(
                name="doi_sanity",
                passed=False,
                reason=f"malformed DOI at indices {malformed_dois}; expected format 10.XXXX/suffix",
            )
        )
    else:
        results.append(
            GateResult(name="doi_sanity", passed=True, reason="all provided DOIs are syntactically valid")
        )

    missing_citations = _citation_membership_failures(sections, source_bundle)
    results.append(
        GateResult(
            name="citation_membership",
            passed=not missing_citations,
            reason=(
                "every DOI/PMID cited in the manuscript must appear in the source bundle"
                + (f"; missing: {', '.join(missing_citations[:10])}" if missing_citations else "")
            ),
        )
    )

    if active_template.article_type == ArticleType.EVIDENCE_MAP.value:
        weak_rows = _topic_coherence_failures(title=title, sections=sections)
        results.append(
            GateResult(
                name="topic_coherence",
                passed=not weak_rows,
                reason=(
                    "evidence-map rows must stay anchored to the title topic"
                    + (f"; weak rows: {'; '.join(weak_rows)}" if weak_rows else "")
                ),
            )
        )

    return results
