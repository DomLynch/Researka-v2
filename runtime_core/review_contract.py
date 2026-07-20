from __future__ import annotations

import re

REVIEW_RUBRIC_KEYS = (
    "research_question_quality",
    "synthesis_quality",
    "claim_evidence_alignment",
    "limitations_quality",
    "gaps_quality",
    "source_grounding",
)
CLAIM_SUPPORT_VERDICTS = {"supported", "partially_supported", "unsupported"}
OVERCLAIM_VERDICTS = {"none", "mild", "significant"}
SYNTHESIS_QUALITY_VERDICTS = {"strong", "adequate", "weak", "empty"}
SUBMISSION_DATA_START = "SUBMISSION_DATA_START"
SUBMISSION_DATA_END = "SUBMISSION_DATA_END"

_REVIEW_DIRECTIVE = re.compile(
    r"\b(?:ignore (?:all |any )?(?:previous|prior|system) instructions?|"
    r"(?:score|rate) (?:this|the) (?:paper|manuscript|submission)|"
    r"reviewer\s*,?\s*(?:please\s+)?(?:approve|accept|reject|revise|score|rate)|"
    r"(?:approve|accept|reject|revise) this (?:paper|manuscript|submission)|"
    r"(?:give|award) (?:this (?:paper|manuscript|submission) )?(?:a )?(?:positive|favorable|high|5/5) (?:score|grade|assessment|review)|"
    r"do not (?:use|choose|recommend) (?:accept|revise|reject)|"
    r"(?:recommendation|verdict) (?:must|should) be (?:accept|revise|reject))\b",
    re.IGNORECASE,
)
_INTEGRITY_MANIPULATION = re.compile(
    r"\b(?:author|manuscript|submission|paper|section|text)\b.{0,100}"
    r"\b(?:tells?|instructs?|directs?|asks?|orders?|commands?|manipulates?|pressures?|steers?)\b.{0,100}"
    r"\b(?:reviewer|evaluator|judge|panel|grade|score|verdict|decision|instruction|directive|prompt)\b",
    re.IGNORECASE,
)
_INTEGRITY_CONTAINER = re.compile(
    r"\b(?:author|manuscript|submission|paper|section|text)\b.{0,100}"
    r"\b(?:contains?|embeds?|includes?|inserts?|adds?|uses?)\b.{0,60}"
    r"\b(?:reviewer[- ]directed (?:instruction|directive)s?|(?:embedded |hidden )?reviewer (?:instruction|directive)s?|"
    r"scoring directive|prompt (?:override|residue|injection attempt)|instruction (?:to|for) (?:the )?reviewer)\b",
    re.IGNORECASE,
)
_INTEGRITY_LABEL = re.compile(
    r"\b(?:author|manuscript|submission|paper|section|text)\b.{0,80}"
    r"\b(?:is|reads as|resembles)\b.{0,30}"
    r"\b(?:prompt (?:override|residue|injection attempt)|reviewer[- ]directed (?:instruction|directive))\b",
    re.IGNORECASE,
)
_QUOTED_TEXT = re.compile(r'"([^"]+)"|“([^”]+)”|\'([^\']+)\'|‘([^’]+)’')


def _normalized_words(value: str) -> str:
    return re.sub(r"\W+", " ", value.casefold()).strip()


def _fenced_submission_text(user_prompt: str) -> str:
    start_match = re.search(rf"{SUBMISSION_DATA_START}(?:_[0-9a-f]{{24}})?", user_prompt)
    if start_match is None:
        return ""
    suffix = start_match.group(0)[len(SUBMISSION_DATA_START) :]
    end = user_prompt.rfind(f"{SUBMISSION_DATA_END}{suffix}")
    if end < 0:
        return ""
    return user_prompt[start_match.end() : end]


def _is_integrity_allegation(value: str) -> bool:
    return bool(
        _REVIEW_DIRECTIVE.search(value)
        or _INTEGRITY_MANIPULATION.search(value)
        or _INTEGRITY_CONTAINER.search(value)
        or _INTEGRITY_LABEL.search(value)
    )


def _quoted_directives(value: str) -> list[str]:
    quotes = [next(group for group in match.groups() if group) for match in _QUOTED_TEXT.finditer(value)]
    return [_normalized_words(quote) for quote in quotes if _REVIEW_DIRECTIVE.search(quote)]


def review_grounding_failure(payload: dict[str, object], *, user_prompt: str) -> str | None:
    feedback = [str(payload.get("review_markdown") or "")]
    for field in ("major_issues", "minor_issues", "required_revisions"):
        value = payload.get(field)
        if isinstance(value, list):
            feedback.extend(str(item) for item in value)
    allegations = [item for item in feedback if _is_integrity_allegation(item)]

    findings = payload.get("integrity_findings", [])
    if not isinstance(findings, list):
        return "invalid_integrity_findings"
    if allegations and not findings:
        return "integrity_finding_missing_quote"

    manuscript = _normalized_words(_fenced_submission_text(user_prompt))
    grounded_quotes: list[str] = []
    for finding in findings:
        if not isinstance(finding, dict) or str(finding.get("category") or "").strip().lower() != "reviewer_directive":
            return "invalid_integrity_finding"
        quote = str(finding.get("quote") or "").strip()
        normalized_quote = _normalized_words(quote)
        if not quote or not _REVIEW_DIRECTIVE.search(quote) or normalized_quote not in manuscript:
            return "ungrounded_integrity_finding"
        grounded_quotes.append(normalized_quote)
    for allegation in allegations:
        quoted_directives = _quoted_directives(allegation)
        if not quoted_directives or any(quote not in grounded_quotes for quote in quoted_directives):
            return "integrity_finding_missing_quote"
    return None


def accept_quorum_satisfied(metadata: dict, *, provider: str | None = None) -> bool:
    models = metadata.get("accept_quorum_models")
    distinct_models = {model.strip() for model in models if isinstance(model, str) and model.strip()} if isinstance(models, list) else set()
    try:
        count = int(metadata.get("accept_quorum_count") or 0)
    except (TypeError, ValueError):
        return False
    return (
        (provider or metadata.get("provider")) == "reviewer-panel"
        and count >= 2
        and len(distinct_models) >= 2
    )


def accept_contract_failure(
    rubric_scores: dict[str, int],
    *,
    major_issues: list[str],
    required_revisions: list[str],
    claim_support: str,
    overclaim: str,
    synthesis_quality: str,
) -> str | None:
    if set(rubric_scores) != set(REVIEW_RUBRIC_KEYS):
        return "accept_rubric_too_weak"
    if min(rubric_scores.values()) < 4:
        return "accept_rubric_too_weak"
    if major_issues:
        return "accept_has_major_issues"
    if required_revisions:
        return "accept_has_required_revisions"
    if claim_support != "supported":
        return "accept_claim_support_not_supported"
    if overclaim != "none":
        return "accept_has_overclaim"
    if synthesis_quality not in {"strong", "adequate"}:
        return "accept_synthesis_quality_invalid"
    return None


def accept_contract_satisfied(
    rubric_scores: dict[str, int],
    *,
    major_issues: list[str],
    required_revisions: list[str],
    claim_support: str,
    overclaim: str,
    synthesis_quality: str,
) -> bool:
    return (
        accept_contract_failure(
            rubric_scores,
            major_issues=major_issues,
            required_revisions=required_revisions,
            claim_support=claim_support,
            overclaim=overclaim,
            synthesis_quality=synthesis_quality,
        )
        is None
    )
