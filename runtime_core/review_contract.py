from __future__ import annotations

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
