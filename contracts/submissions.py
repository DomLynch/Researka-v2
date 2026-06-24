from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ValidationError

from .models import ArticleType, GateResult
from .templates import RAPID_EVIDENCE_SYNTHESIS, publication_template_for

_DOI_PATTERN = re.compile(r"^10\.\d{4,}/\S+$")
# Prose-citation patterns: identifiers an author cites inside section text.
# Every one must be a member of the submitted source bundle — citing receipts
# the bundle does not carry is the canonical fabrication/slip vector.
_PROSE_DOI_PATTERN = re.compile(r"\b10\.\d{4,9}/[^\s\"\'\])}>,;]+", re.IGNORECASE)
_PROSE_PMID_PATTERN = re.compile(r"\bPMID[:\s#-]*(\d{4,12})\b", re.IGNORECASE)
_TABLE_SEPARATOR_PATTERN = re.compile(r"^:?-{3,}:?$")
_TOPIC_STOPWORDS = {
    "across",
    "brief",
    "evidence",
    "findings",
    "full",
    "generating",
    "hypothesis",
    "map",
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


def _clean_doi(value: str) -> str:
    return value.strip().rstrip(".,;").lower()


def _citation_membership_failures(sections: dict[str, str], source_bundle: list[dict]) -> list[str]:
    prose = "\n".join(str(value) for value in sections.values())
    bundle_dois = {_clean_doi(str(entry.get("doi") or "")) for entry in source_bundle}
    bundle_pmids = {str(entry.get("pmid") or entry.get("id") or "").strip() for entry in source_bundle}
    cited_dois = {_clean_doi(match) for match in _PROSE_DOI_PATTERN.findall(prose)}
    cited_pmids = set(_PROSE_PMID_PATTERN.findall(prose))
    missing = [f"doi:{doi}" for doi in sorted(cited_dois - bundle_dois)]
    missing.extend(f"pmid:{pmid}" for pmid in sorted(cited_pmids - bundle_pmids))
    return missing


def _topic_anchors(title: str) -> set[str]:
    topic = re.split(r":|\s+[—-]\s+", title, maxsplit=1)[0].lower()
    tokens = {token for token in re.findall(r"[a-z0-9]+", topic) if len(token) > 2 or token == "ai"}
    anchors = {token for token in tokens if token not in _TOPIC_STOPWORDS}
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


class SourceBundleEntry(BaseModel):
    title: str
    url: str | None = None
    doi: str | None = None
    year: int | None = None
    evidence_type: Literal["primary", "review"]
    relevance: float | None = None
    # The in-text citation token the manuscript uses for this source (e.g.
    # "Zufry 2025"). Lets reviewers cross-walk author-year prose citations to
    # bundle entries instead of flagging present sources as ungrounded.
    cited_as: str | None = None


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
            raise ValueError(f"submission_template:source_bundle_entry_invalid:{index}:{exc.errors()[0]['type']}") from exc
    return normalized


def _int_value(value: object) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return 0


def _alpha_source_exception(article_type: str, citation_count: int, evidence_bundle: dict | None) -> bool:
    if article_type != ArticleType.ALPHA_MEMO.value or not 2 <= citation_count <= 4:
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


def run_submission_template_checks(
    *,
    title: str = "",
    sections: dict[str, str],
    source_bundle: list[dict],
    article_type: str = ArticleType.RAPID_EVIDENCE_SYNTHESIS.value,
    template: SubmissionTemplateV1 | None = None,
    evidence_bundle: dict | None = None,
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

    citation_count = len(normalized_bundle)
    citation_floor_exception = _alpha_source_exception(article_type, citation_count, evidence_bundle)
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

    recent_count = sum(
        1
        for entry in normalized_bundle
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
