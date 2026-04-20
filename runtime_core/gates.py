from __future__ import annotations

from contracts import GateResult, PublicationCounts
from .sanitizer import contains_pipeline_leakage


def _contains_leakage(text: str) -> bool:
    return contains_pipeline_leakage(text)


def _counts_reconcile(counts: PublicationCounts) -> bool:
    return counts.selected_count == counts.review_like_count + counts.primary_like_count


def run_publish_gates(
    *,
    body_markdown: str,
    counts: PublicationCounts,
    core_claims_resolved: bool = True,
) -> list[GateResult]:
    results = [
        GateResult(
            name="leakage_blocker",
            passed=not _contains_leakage(body_markdown),
            reason="final body must not contain reviewer or pipeline leakage",
        ),
        GateResult(
            name="count_reconciliation",
            passed=_counts_reconcile(counts),
            reason="selected count must equal review-like + primary-like counts",
        ),
        GateResult(
            name="core_claims_resolved",
            passed=core_claims_resolved,
            reason="title/abstract/conclusion claims must not remain unresolved",
        ),
    ]
    return results
