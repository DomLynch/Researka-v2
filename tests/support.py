from __future__ import annotations

from contracts import Decision, ObjectType, ResearchObject, RuntimeJob, Stage
from contracts import publication_template_for
from runtime_core.repos import RuntimeRepository
from runtime_core.judge_release import build_judge_release
from runtime_core.sanitizer import extract_markdown_section
from runtime_core.workflow import _ensure_canonical_package


def bound_review(
    repository: RuntimeRepository,
    submission: ResearchObject,
    metadata: dict[str, object],
) -> ResearchObject:
    submission, package_hash = _ensure_canonical_package(repository, submission)
    return repository.create_object(
        ResearchObject(
            object_type=ObjectType.REVIEW,
            parent_object_id=submission.id,
            title=f"Review for {submission.title}",
            metadata={**metadata, "reviewed_package_hash": package_hash},
        )
    )


def accepted_publish_job(
    repository: RuntimeRepository,
    submission: ResearchObject,
    review_metadata: dict[str, object] | None = None,
) -> RuntimeJob:
    submission, package_hash = _ensure_canonical_package(repository, submission)
    judge_release = build_judge_release(
        system_prompt="test-review-prompt",
        provider="reviewer-panel",
        model="test-reviewer-a|test-reviewer-b",
        response_metadata={"panel_models": ["test-reviewer-a", "test-reviewer-b"]},
    )
    review = bound_review(
        repository,
        submission,
        {
            "recommendation": Decision.ACCEPT.value,
            "provider": "reviewer-panel",
            "accept_quorum_count": 2,
            "accept_quorum_models": ["test-reviewer-a", "test-reviewer-b"],
            "accept_quorum_identities": ["test-provider-a:test-reviewer-a", "test-provider-b:test-reviewer-b"],
            "accept_quorum_providers": ["test-provider-a", "test-provider-b"],
            "judge_release_id": judge_release["id"],
            "judge_release": judge_release,
            **(review_metadata or {}),
        },
    )
    decision = repository.create_object(
        ResearchObject(
            object_type=ObjectType.DECISION,
            parent_object_id=submission.id,
            title=f"Decision for {submission.title}",
            metadata={
                "decision": Decision.ACCEPT.value,
                "review_id": review.id,
                "canonical_package_hash": package_hash,
                "reviewed_package_hash": package_hash,
            },
        )
    )
    return RuntimeJob(
        target_object_id=submission.id,
        stage=Stage.PUBLISH,
        payload={"decision_id": decision.id, "canonical_package_hash": package_hash},
    )


def sections_from_markdown(body: str, article_type: str) -> dict[str, str]:
    template = publication_template_for(article_type)
    headings = (*template.required_sections, *template.recommended_sections)
    return {heading: text for heading in headings if (text := extract_markdown_section(body, heading))}
