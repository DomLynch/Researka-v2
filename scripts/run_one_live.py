#!/usr/bin/env python3
"""Run 1 calibration submission through the live judge panel."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from contracts import ObjectType, ResearchObject, RuntimeJob, Stage, Decision
from runtime_core import InMemoryRuntimeRepository, WorkflowEngine


def main() -> None:
    input_path = "/Users/domininclynch/Downloads/researka_5_rapid_evidence_syntheses_2026.json"
    with open(input_path) as f:
        submissions_data = json.load(f)

    sub = submissions_data[0]
    title = sub["title"]
    print(f"Running: {title[:80]}...")
    print(f"Provider: {os.getenv('RESEARKA_V2_PROVIDER', 'deterministic')}")
    print()

    engine = WorkflowEngine()
    repo = InMemoryRuntimeRepository()

    submission = repo.create_object(
        ResearchObject(
            object_type=ObjectType.SUBMISSION,
            title=title,
            body_markdown=sub.get("abstract", ""),
            metadata={
                "abstract": sub.get("abstract", ""),
                "sections": sub.get("sections", {}),
                "source_bundle": sub.get("source_bundle", []),
                "domain_slug": sub.get("domain_slug", "general"),
                "author_agent_id": sub.get("author_agent_id", "unknown"),
                "core_claims_resolved": True,
            },
        )
    )

    # Intake
    intake_job = repo.enqueue_job(
        RuntimeJob(target_object_id=submission.id, stage=Stage.INTAKE, payload={"domain_slug": sub.get("domain_slug", "general")})
    )
    intake_result = engine.handle_job(intake_job, repo)
    repo.complete_job(intake_job.id)

    if intake_result.get("terminal_decision") == Decision.REJECT.value:
        print("INTAKE: REJECTED")
        return

    print("INTAKE: passed")

    # Review
    review_job = repo.claim_next_job()
    engine.handle_job(review_job, repo)
    repo.complete_job(review_job.id)

    review = repo.list_objects(ObjectType.REVIEW)[-1]
    rec = review.metadata.get("recommendation")
    route = review.metadata.get("route", "single")
    rubric = review.metadata.get("rubric_scores", {})
    major = review.metadata.get("major_issues", [])
    overclaim = review.metadata.get("overclaim_verdict", "?")
    cost = review.metadata.get("cost_usd", 0)

    print(f"REVIEW: {rec}")
    print(f"  route: {route}")
    print(f"  rubric: {rubric}")
    print(f"  major_issues: {len(major)}")
    print(f"  overclaim: {overclaim}")
    print(f"  cost: ${cost:.4f}")

    # Editorial
    editorial_job = repo.claim_next_job()
    if editorial_job:
        engine.handle_job(editorial_job, repo)
        repo.complete_job(editorial_job.id)
        decision_obj = repo.list_objects(ObjectType.DECISION)[-1]
        print(f"DECISION: {decision_obj.metadata.get('decision')}")


if __name__ == "__main__":
    main()
