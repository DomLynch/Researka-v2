from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
from pathlib import Path

MODEL_QUORUM_POLICY = "two_models_v1"
MODEL_QUORUM_PROVIDERS = {
    "gpt-5.6-sol": "codex",
    "gpt-5.6-terra": "codex",
    "z-ai/glm-5.3-flash": "openrouter",
}

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
    "accept_quorum_identities",
    "accept_quorum_providers",
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
_SOURCE_ID_ALLEGATIONS = tuple(
    re.compile(
        rf"(?:{problem.pattern})\s+(?:{context.pattern}|{_DOI.pattern})|"
        rf"(?:{context.pattern}|{_DOI.pattern})(?:\s+(?:{_DOI.pattern}|\d+))?"
        r"(?:\s+(?:citations?|references?))?"
        r"(?:\s+(?:is|are|was|were|appears?|seems?)(?:\s+to be)?)?\s+"
        rf"(?:{problem.pattern})",
        re.IGNORECASE,
    )
    for problem, context in (
        (_SOURCE_ID_PROBLEM, _SOURCE_ID_CONTEXT),
        (_SOURCE_IDENTIFIER_PROBLEM, _SOURCE_IDENTIFIER_CONTEXT),
    )
)
_SOURCE_ID_NEGATION = re.compile(
    r"\b(?:no(?:\s+evidence(?:\s+of|\s+that)?)?|not)\s+(?:the\s+)?$", re.IGNORECASE
)
_MISSING_MANUSCRIPT = re.compile(
    r"\b(?:missing|no) (?:full )?(?:manuscript|submission)? ?(?:text|content)\b|"
    r"\b(?:manuscript|submission)(?: text| content)? (?:is )?(?:missing|absent|not (?:provided|present|included))\b|"
    r"\bcontent between\b.{0,80}\bmarkers?\b|\bprovide the full manuscript text\b",
    re.IGNORECASE,
)


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


def _has_manuscript_content(user_prompt: str) -> bool:
    try:
        payload = json.loads(_fenced_submission_text(user_prompt))
    except (TypeError, ValueError):
        return False
    if not isinstance(payload, dict):
        return False
    sections = payload.get("sections")
    return bool(
        str(payload.get("abstract") or "").strip()
        or isinstance(sections, dict) and any(str(value).strip() for value in sections.values())
    )


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
    # Bind the allegation to a source/identifier noun, not unrelated words
    # elsewhere in an entire review (e.g. an unverified effect estimate).
    allegations = [item for item in feedback if any(
        not _SOURCE_ID_NEGATION.search(item[:match.start()])
        for pattern in _SOURCE_ID_ALLEGATIONS for match in pattern.finditer(item)
    )]
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


def _verbatim_quote_in_section(quote: str, section: str) -> bool:
    quote = " ".join(quote.split())
    prefix = r"(?<![\w.+,\-−±])" if re.match(r"[+\-−]?(?:\d|\.\d)", quote) else r"(?<!\w)"
    return bool(re.search(prefix + re.escape(quote) + r"(?!\w|[.,]\d)", " ".join(section.split())))


def _material_finding_failure(finding: dict, context: dict) -> str | None:
    if finding.get("materiality") != "blocking" or finding.get("has_material_impact") is not True or re.match(
        r"\s*(?:optional\s*[:\-]|non[- ]blocking\s*[:\-])", str(finding.get("issue") or ""), re.I
    ):
        return "blocking_finding_requires_material_impact"
    if any(not isinstance(finding.get(key), str) or not any(char.isalnum() for char in finding[key])
           for key in ("impact", "correction")):
        return "material_issue_missing_impact_or_correction"
    section, quote = finding.get("section"), finding.get("quote")
    if not isinstance(section, str) or not section.strip():
        return "material_issue_missing_location"
    if finding.get("kind") not in {"omission", "incorrect"}:
        return "material_issue_invalid_kind"
    if finding.get("kind") == "incorrect" and (
        not isinstance(quote, str) or not _normalized_words(quote)
        or not _verbatim_quote_in_section(quote, str(context.get("sections", {}).get(section, "")))
    ):
        return "material_issue_quote_not_in_section"
    previous = context.get("previous_issues", [])
    if previous:
        reason = finding.get("change_reason")
        if reason == "persisting":
            if finding.get("prior_issue") not in previous:
                return "material_issue_unknown_prior_issue"
        elif reason not in {"newly_introduced", "newly_discovered"} or not isinstance(finding.get("why_new"), str) or not any(char.isalnum() for char in finding["why_new"]):
            return "material_issue_missing_revision_explanation"
    return None


def _rejection_basis_failure(findings: list[dict]) -> str | None:
    irreparable = False
    for finding in findings:
        repairability = finding.get("repairability")
        if repairability == "bounded_revision":
            continue
        if not isinstance(repairability, str) or repairability not in {"new_evidence", "fabrication", "invalid_data"}:
            return "reject_requires_finding_repairability"
        reason = finding.get("why_not_revise")
        if not isinstance(reason, str) or not any(char.isalnum() for char in reason):
            return "reject_requires_why_not_revise"
        irreparable = True
    return None if irreparable else "reject_requires_irreparable_finding_use_revise"


def review_materiality_failure(payload: dict, context: dict) -> str | None:
    issues = set(payload.get("major_issues", [])) | set(payload.get("required_revisions", []))
    findings = payload.get("material_findings", [])
    if not isinstance(findings, list) or any(not isinstance(finding, dict) for finding in findings):
        return "invalid_material_findings"
    if {finding.get("issue") for finding in findings} != issues or len(findings) != len(issues):
        return "blocking_issues_require_exact_material_findings"
    resolved = payload.get("resolved_prior_issues", [])
    if not isinstance(resolved, list) or any(issue not in context.get("previous_issues", []) for issue in resolved):
        return "unknown_resolved_prior_issue"
    for finding in findings:
        if finding.get("prior_issue") in resolved or finding.get("issue") in resolved:
            return "resolved_issue_reopened_in_same_verdict"
        if failure := _material_finding_failure(finding, context):
            return failure
    persisting = {finding.get("prior_issue") for finding in findings if finding.get("change_reason") == "persisting"}
    if set(context.get("previous_issues", [])) != set(resolved) | persisting:
        return "previous_material_issues_not_accounted_for"
    recommendation = str(payload.get("recommendation") or "").strip().lower()
    if recommendation == "revise" and any(
        finding.get("repairability") != "bounded_revision" for finding in findings
    ):
        return "revise_requires_explicit_bounded_repairability"
    if recommendation == "reject":
        return _rejection_basis_failure(findings)
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
    if _has_manuscript_content(user_prompt) and any(_MISSING_MANUSCRIPT.search(item) for item in feedback):
        return "false_missing_manuscript"
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
    submission_id: str = "",
    reviewed_package_hash: str = "",
    secret: str | None = None,
) -> bool:
    if _uses_model_quorum(metadata):
        return model_quorum_attestation_valid(
            metadata,
            submission_id=submission_id,
            reviewed_package_hash=reviewed_package_hash,
            recommendation=str(metadata.get("recommendation") or ""),
            judge_release_id=str(metadata.get("judge_release_id") or ""),
            secret=secret,
        )
    models = metadata.get("accept_quorum_models")
    distinct_models = {model.strip() for model in models if isinstance(model, str) and model.strip()} if isinstance(models, list) else set()
    identities = metadata.get("accept_quorum_identities")
    distinct_identities = {
        identity.strip() for identity in identities if isinstance(identity, str) and identity.strip()
    } if isinstance(identities, list) else set()
    providers = metadata.get("accept_quorum_providers")
    distinct_providers = {
        item.strip() for item in providers if isinstance(item, str) and item.strip()
    } if isinstance(providers, list) else set()
    try:
        count = int(metadata.get("accept_quorum_count") or 0)
    except (TypeError, ValueError):
        return False
    if (provider or metadata.get("provider")) != "reviewer-panel":
        return False
    if (
        count >= 2
        and len(distinct_models) >= 2
        and len(distinct_identities) >= 2
        and len(distinct_providers) >= 2
    ):
        return True
    return allow_billing_waiver and billing_waiver_receipt_valid(metadata, provider=provider)


def _uses_model_quorum(metadata: dict) -> bool:
    release = metadata.get("judge_release")
    settings = release.get("settings", {}) if isinstance(release, dict) else {}
    return (
        metadata.get("quorum_policy") not in (None, "provider_diversity_v1")
        or "model_quorum_attestation" in metadata
        or isinstance(settings, dict) and settings.get("quorum_policy") not in (None, "provider_diversity_v1")
    )


def _validated_model_receipt(receipt: object) -> dict:
    if not isinstance(receipt, dict) or type(receipt.get("ok")) is not bool:
        raise ValueError("invalid_model_quorum_receipt")
    if not receipt["ok"]:
        return receipt
    model = receipt.get("model")
    if not isinstance(model, str) or model not in MODEL_QUORUM_PROVIDERS or MODEL_QUORUM_PROVIDERS[model] != receipt.get("provider"):
        raise ValueError("unapproved_model_quorum_identity")
    payload = receipt.get("response")
    raw_recommendation = receipt.get("recommendation")
    recommendation = raw_recommendation.strip().lower() if isinstance(raw_recommendation, str) else ""
    if (
        not isinstance(payload, dict)
        or recommendation not in {"accept", "revise", "reject"}
        or str(payload.get("recommendation") or "").strip().lower() != recommendation
        or not isinstance(payload.get("review_markdown"), str)
        or not payload["review_markdown"].strip()
        or not isinstance(receipt.get("response_sha256"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", receipt["response_sha256"])
    ):
        raise ValueError("invalid_model_quorum_response")
    if model == "z-ai/glm-5.3-flash" and (
        receipt.get("fallback_used") is not True
        or not isinstance(receipt.get("fallback_reason"), str)
        or not receipt["fallback_reason"].strip()
    ):
        raise ValueError("model_quorum_fallback_cause_missing")
    if recommendation == "accept":
        _validate_model_accept(payload)
    return {**receipt, "recommendation": recommendation}


def _validate_model_accept(payload: dict) -> None:
    scores = payload.get("rubric_scores")
    if not isinstance(scores, dict) or any(type(score) is not int or not 1 <= score <= 5 for score in scores.values()):
        raise ValueError("invalid_model_quorum_scores")
    for field in ("major_issues", "minor_issues", "required_revisions"):
        items = payload.get(field)
        if not isinstance(items, list) or any(not isinstance(item, str) for item in items):
            raise ValueError("invalid_model_quorum_issues")
    failure = accept_contract_failure(
        scores,
        major_issues=payload["major_issues"],
        required_revisions=payload["required_revisions"],
        claim_support=str(payload.get("claim_support_verdict") or "").strip().lower(),
        overclaim=str(payload.get("overclaim_verdict") or "").strip().lower(),
        synthesis_quality=str(payload.get("synthesis_quality_verdict") or "").strip().lower(),
    )
    if failure:
        raise ValueError(failure)


def model_quorum_metadata(reviewer_receipts: object) -> dict:
    """Recompute voting identities from validated receipts, never claimed counts."""
    if not isinstance(reviewer_receipts, list) or not reviewer_receipts:
        raise ValueError("model_quorum_receipts_missing")
    receipts = [_validated_model_receipt(receipt) for receipt in reviewer_receipts]
    successful = [receipt for receipt in receipts if receipt["ok"]]
    if len({receipt["model"] for receipt in successful}) != len(successful):
        raise ValueError("duplicate_model_quorum_identity")
    accepting = [receipt for receipt in successful if receipt["recommendation"] == "accept"]
    return {
        "quorum_policy": MODEL_QUORUM_POLICY,
        "reviewer_receipts": json.loads(json.dumps(receipts, allow_nan=False)),
        "accept_quorum_count": len(accepting),
        "accept_quorum_models": sorted(receipt["model"] for receipt in accepting),
        "accept_quorum_identities": sorted(f"{receipt['provider']}:{receipt['model']}" for receipt in accepting),
        "accept_quorum_providers": sorted({receipt["provider"] for receipt in accepting}),
    }


def model_quorum_attestation(
    metadata: dict,
    *,
    submission_id: str,
    reviewed_package_hash: str,
    recommendation: str,
    judge_release_id: str,
    secret: str,
) -> str:
    computed = model_quorum_metadata(metadata.get("reviewer_receipts"))
    if any(metadata.get(key) != value for key, value in computed.items()):
        raise ValueError("model_quorum_metadata_mismatch")
    if recommendation == "accept" and computed["accept_quorum_count"] < 2:
        raise ValueError("accept_quorum_missing")
    return _review_attestation(
        {
            "quorum_policy": MODEL_QUORUM_POLICY,
            "submission_id": submission_id,
            "reviewed_package_hash": reviewed_package_hash,
            "recommendation": recommendation,
            "judge_release_id": judge_release_id,
            "receipt": computed,
        },
        secret,
    )


def model_quorum_attestation_valid(
    metadata: dict,
    *,
    submission_id: str,
    reviewed_package_hash: str,
    recommendation: str,
    judge_release_id: str,
    secret: str | None,
) -> bool:
    if (
        not secret or not submission_id or not judge_release_id
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", reviewed_package_hash)
        or metadata.get("provider") != "reviewer-panel"
        or metadata.get("quorum_policy") != MODEL_QUORUM_POLICY
        or metadata.get("reviewed_package_hash") != reviewed_package_hash
        or metadata.get("judge_release_id") != judge_release_id
        or metadata.get("recommendation") != recommendation or recommendation != "accept"
        or type(metadata.get("accept_quorum_count")) is not int
        or metadata["accept_quorum_count"] < 2
        or metadata.get("accept_quorum_waiver")
    ):
        return False
    try:
        expected = model_quorum_attestation(
            metadata,
            submission_id=submission_id,
            reviewed_package_hash=reviewed_package_hash,
            recommendation=recommendation,
            judge_release_id=judge_release_id,
            secret=secret,
        )
        return hmac.compare_digest(str(metadata.get("model_quorum_attestation") or ""), expected)
    except (TypeError, ValueError, KeyError):
        return False


def billing_waiver_receipt_valid(metadata: dict, *, provider: str | None = None) -> bool:
    if _uses_model_quorum(metadata):
        return False
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
    return _review_attestation(payload, secret)


def _review_attestation(payload: dict, secret: str) -> str:
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
