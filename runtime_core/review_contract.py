from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
from pathlib import Path

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
_BILLING_WAIVER_FIELDS = (
    "route",
    "ops_flag",
    "secondary_review_skipped",
    "accept_quorum_count",
    "accept_quorum_models",
    "accept_quorum_waiver",
    "sparring_provider",
    "sparring_http_status",
    "primary_fallback_used",
    "winner_provider",
    "winner_model",
)

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
    r"\b(?:author|manuscript|submission|paper|section|text)\b.{0,80}"
    r"\b(?:tells?|instructs?|directs?|asks?|orders?|commands?|manipulates?|pressures?|steers?)\b.{0,80}"
    r"\b(?:reviewer|evaluator|judge|panel)\b\s+(?:(?:how\s+)?to|toward|into)\s+"
    r"(?:approve|accept|reject|revise|grade|score|rate|decide|choose|recommend|return|issue|give|award)\b",
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
_SOURCE_ID_PROBLEM = re.compile(
    r"\b(?:fabricated|fake|implausible|non[- ]?existent|unresolvable|"
    r"(?:does|do|did|can|could) not (?:be )?resolve[dm]?|cannot be resolved|not found|"
    r"unverified)\b",
    re.IGNORECASE,
)
_SOURCE_IDENTIFIER_PROBLEM = re.compile(r"\b(?:invalid|mismatch(?:ed)?)\b", re.IGNORECASE)
_SOURCE_ID_CONTEXT = re.compile(
    r"\b(?:pmids?|dois?|identifiers?|citations?|references?|sources?)\b",
    re.IGNORECASE,
)
_SOURCE_IDENTIFIER_CONTEXT = re.compile(r"\b(?:pmids?|dois?|identifiers?)\b", re.IGNORECASE)
_DOI = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.IGNORECASE)


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


def _source_integrity_failure(
    feedback: list[str],
    source_verification: dict[str, object] | None,
) -> str | None:
    allegations = [
        item
        for item in feedback
        if (
            _SOURCE_ID_PROBLEM.search(item) and (_SOURCE_ID_CONTEXT.search(item) or _DOI.search(item))
        ) or (
            _SOURCE_IDENTIFIER_PROBLEM.search(item)
            and (_SOURCE_IDENTIFIER_CONTEXT.search(item) or _DOI.search(item))
        )
    ]
    if not allegations:
        return None
    allowed: set[str] = set()
    for field in ("unverified", "title_mismatches", "identifier_unverified", "identifier_mismatches"):
        field_values = (source_verification or {}).get(field, [])
        if isinstance(field_values, list):
            allowed.update(str(identity).strip().lower() for identity in field_values if str(identity).strip())
    identities: set[str] = set()
    for allegation in allegations:
        identities.update({
            "doi:" + match.group(0).rstrip(".,;:)}").lower()
            for match in _DOI.finditer(allegation)
        })
        if re.search(r"\bpmids?\b", allegation, re.IGNORECASE):
            identities.update(f"pmid:{value}" for value in re.findall(r"\b\d{5,9}\b", allegation))
    if not identities:
        return "source_integrity_finding_missing_id"
    unsupported = sorted(identities - allowed)
    if unsupported:
        return f"unsupported_source_integrity_finding:{unsupported[0]}"
    return None


def review_grounding_failure(
    payload: dict[str, object],
    *,
    user_prompt: str,
    source_verification: dict[str, object] | None = None,
) -> str | None:
    feedback = [str(payload.get("review_markdown") or "")]
    for field in ("major_issues", "minor_issues", "required_revisions"):
        value = payload.get(field)
        if isinstance(value, list):
            feedback.extend(str(item) for item in value)
    if source_failure := _source_integrity_failure(feedback, source_verification):
        return source_failure
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


def accept_quorum_satisfied(
    metadata: dict,
    *,
    provider: str | None = None,
    allow_billing_waiver: bool = False,
) -> bool:
    models = metadata.get("accept_quorum_models")
    distinct_models = {model.strip() for model in models if isinstance(model, str) and model.strip()} if isinstance(models, list) else set()
    try:
        count = int(metadata.get("accept_quorum_count") or 0)
    except (TypeError, ValueError):
        return False
    if (provider or metadata.get("provider")) != "reviewer-panel":
        return False
    if count >= 2 and len(distinct_models) >= 2:
        return True
    return allow_billing_waiver and billing_waiver_receipt_valid(metadata, provider=provider)


def billing_waiver_receipt_valid(metadata: dict, *, provider: str | None = None) -> bool:
    models = metadata.get("accept_quorum_models")
    distinct_models = {model.strip() for model in models if isinstance(model, str) and model.strip()} if isinstance(models, list) else set()
    try:
        count = int(metadata.get("accept_quorum_count") or 0)
    except (TypeError, ValueError):
        return False
    return (
        (provider or metadata.get("provider")) == "reviewer-panel"
        and count == len(distinct_models) == 1
        and metadata.get("route") == "sparring_billing_skipped_primary_used"
        and metadata.get("ops_flag") == "sparring_billing_skipped"
        and metadata.get("secondary_review_skipped") is True
        and metadata.get("accept_quorum_waiver") == "sparring_billing_unavailable"
        and metadata.get("sparring_provider") == "openrouter"
        and metadata.get("sparring_http_status") == 402
        and metadata.get("primary_fallback_used") is False
        and metadata.get("winner_provider") in {"minimax", "mimo"}
    )


def review_attestation_secret(*, required: bool = False) -> str | None:
    secret = os.environ.get("RESEARKA_V2_REVIEW_ATTESTATION_SECRET", "").strip()
    path = os.environ.get("RESEARKA_V2_REVIEW_ATTESTATION_SECRET_PATH", "").strip()
    if not secret and path:
        try:
            secret = Path(path).read_text().strip()
        except OSError as exc:
            if required:
                raise RuntimeError("review_attestation_secret_unreadable") from exc
    if required and not secret:
        raise RuntimeError("review_attestation_secret_required")
    return secret or None


def billing_waiver_attestation(
    metadata: dict,
    *,
    submission_id: str,
    recommendation: str,
    judge_release_id: str,
    secret: str,
) -> str:
    payload = {
        "submission_id": submission_id,
        "recommendation": recommendation,
        "judge_release_id": judge_release_id,
        "receipt": {key: metadata.get(key) for key in _BILLING_WAIVER_FIELDS},
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return f"hmac-sha256:{hmac.new(secret.encode(), encoded, hashlib.sha256).hexdigest()}"


def billing_waiver_attestation_valid(
    metadata: dict,
    *,
    submission_id: str,
    recommendation: str,
    judge_release_id: str,
    secret: str | None,
) -> bool:
    provided = str(metadata.get("accept_quorum_waiver_attestation") or "")
    if not secret or not provided or not billing_waiver_receipt_valid(metadata):
        return False
    expected = billing_waiver_attestation(
        metadata,
        submission_id=submission_id,
        recommendation=recommendation,
        judge_release_id=judge_release_id,
        secret=secret,
    )
    return hmac.compare_digest(provided, expected)


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
