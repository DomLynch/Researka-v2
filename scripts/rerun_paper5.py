#!/usr/bin/env python3
"""Rerun paper 5 only."""
import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from contracts import ObjectType, ResearchObject, RuntimeJob, Stage, Decision
from runtime_core import InMemoryRuntimeRepository, WorkflowEngine

input_path = "/Users/domininclynch/Downloads/researka_5_rapid_evidence_syntheses_2026.json"
with open(input_path) as f:
    sub = json.load(f)[4]

engine = WorkflowEngine()
repo = InMemoryRuntimeRepository()

submission = repo.create_object(ResearchObject(
    object_type=ObjectType.SUBMISSION,
    title=sub["title"],
    body_markdown=sub.get("abstract", ""),
    metadata={
        "abstract": sub.get("abstract", ""),
        "sections": sub.get("sections", {}),
        "source_bundle": sub.get("source_bundle", []),
        "domain_slug": sub.get("domain_slug", "general"),
        "author_agent_id": sub.get("author_agent_id", "unknown"),
        "core_claims_resolved": True,
    },
))

intake_job = repo.enqueue_job(RuntimeJob(target_object_id=submission.id, stage=Stage.INTAKE, payload={"domain_slug": "longevity"}))
r = engine.handle_job(intake_job, repo)
repo.complete_job(intake_job.id)
print(f"INTAKE: {'passed' if r.get('next_stage') else 'rejected'}")

review_job = repo.claim_next_job()
try:
    engine.handle_job(review_job, repo)
    repo.complete_job(review_job.id)
    review = repo.list_objects(ObjectType.REVIEW)[-1]
    rec = review.metadata.get("recommendation")
    route = review.metadata.get("route")
    rubric = review.metadata.get("rubric_scores", {})
    print(f"REVIEW: {rec} | route: {route}")
    print(f"  rubric: {rubric}")
    print(f"  primary: {review.metadata.get('primary_recommendation')} | sparring: {review.metadata.get('sparring_recommendation')}")
except Exception as e:
    print(f"REVIEW FAILED: {e}")
