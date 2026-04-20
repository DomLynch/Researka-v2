#!/usr/bin/env python3
"""Run all 5 calibration submissions through the live judge panel."""
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

    print(f"Provider: {os.getenv('RESEARKA_V2_PROVIDER', 'deterministic')}")
    print(f"Submissions: {len(submissions_data)}")
    print()

    engine = WorkflowEngine()
    repo = InMemoryRuntimeRepository()

    results = []

    for i, sub in enumerate(submissions_data, 1):
        title = sub["title"]
        short = title[:80] + "..." if len(title) > 80 else title
        print(f"[{i}/5] {short}")

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
            RuntimeJob(target_object_id=submission.id, stage=Stage.INTAKE,
                       payload={"domain_slug": sub.get("domain_slug", "general")})
        )
        try:
            intake_result = engine.handle_job(intake_job, repo)
            repo.complete_job(intake_job.id)
        except Exception as e:
            print(f"  INTAKE FAILED: {e}")
            results.append({"title": short, "stage": "intake", "outcome": "failed", "error": str(e)})
            print()
            continue

        if intake_result.get("terminal_decision") == Decision.REJECT.value:
            print("  INTAKE: REJECTED")
            results.append({"title": short, "stage": "intake", "outcome": "rejected"})
            print()
            continue

        print("  INTAKE: passed")

        # Review
        review_job = repo.claim_next_job()
        if review_job is None:
            print("  REVIEW: no job")
            results.append({"title": short, "stage": "review", "outcome": "no_job"})
            print()
            continue

        try:
            engine.handle_job(review_job, repo)
            repo.complete_job(review_job.id)
        except Exception as e:
            error_str = str(e)
            print(f"  REVIEW FAILED: {error_str}")
            results.append({"title": short, "stage": "review", "outcome": "failed", "error": error_str})
            print()
            continue

        review = repo.list_objects(ObjectType.REVIEW)[-1]
        rec = review.metadata.get("recommendation", "?")
        route = review.metadata.get("route", "single")
        winner = review.metadata.get("winner_provider", review.metadata.get("provider", "?"))
        primary_rec = review.metadata.get("primary_recommendation", "?")
        sparring_rec = review.metadata.get("sparring_recommendation", "?")
        consensus = review.metadata.get("consensus", "?")
        rubric = review.metadata.get("rubric_scores", {})
        major = review.metadata.get("major_issues", [])
        overclaim = review.metadata.get("overclaim_verdict", "?")
        claim_support = review.metadata.get("claim_support_verdict", "?")
        synthesis = review.metadata.get("synthesis_quality_verdict", "?")
        tokens_in = review.metadata.get("tokens_in", 0)
        tokens_out = review.metadata.get("tokens_out", 0)
        cost = review.metadata.get("cost_usd", 0)

        print(f"  REVIEW: {rec}")
        print(f"    route: {route} | winner: {winner}")
        if route in ("consensus", "fallback_tiebreak"):
            print(f"    primary: {primary_rec} | sparring: {sparring_rec} | consensus: {consensus}")
        print(f"    rubric: {rubric}")
        print(f"    claim_support: {claim_support} | overclaim: {overclaim} | synthesis: {synthesis}")
        print(f"    major_issues: {len(major)} | tokens: {tokens_in}+{tokens_out} | cost: ${cost:.4f}")

        # Editorial
        editorial_job = repo.claim_next_job()
        if editorial_job:
            try:
                engine.handle_job(editorial_job, repo)
                repo.complete_job(editorial_job.id)
            except Exception as e:
                print(f"  EDITORIAL FAILED: {e}")
                results.append({"title": short, "stage": "editorial", "outcome": "failed",
                               "error": str(e), "recommendation": rec})
                print()
                continue

        decision_obj = repo.list_objects(ObjectType.DECISION)[-1]
        decision = decision_obj.metadata.get("decision", "?")
        print(f"  DECISION: {decision}")

        # Publish if accepted
        publish_job = repo.claim_next_job()
        if publish_job:
            try:
                engine.handle_job(publish_job, repo)
                repo.complete_job(publish_job.id)
                print("  PUBLISH: completed")
            except Exception as e:
                print(f"  PUBLISH FAILED: {e}")

        results.append({
            "title": short,
            "recommendation": rec,
            "decision": decision,
            "route": route,
            "winner": winner,
            "rubric": rubric,
            "major_issues_count": len(major),
            "overclaim": overclaim,
            "cost_usd": cost,
        })
        print()

    # Summary
    print("=" * 70)
    print("SUMMARY")
    print("=" * 70)
    decisions_list = [r.get("decision") for r in results if "decision" in r]
    total_cost = sum(r.get("cost_usd", 0) for r in results)

    print(f"Accept: {decisions_list.count('accept')}/{len(decisions_list)}")
    print(f"Revise: {decisions_list.count('revise')}/{len(decisions_list)}")
    print(f"Reject: {decisions_list.count('reject')}/{len(decisions_list)}")
    intake_rej = sum(1 for r in results if r.get("stage") == "intake" and r.get("outcome") == "rejected")
    review_fail = sum(1 for r in results if r.get("stage") == "review" and r.get("outcome") == "failed")
    print(f"Intake rejected: {intake_rej}")
    print(f"Review failed: {review_fail}")
    print(f"Total cost: ${total_cost:.4f}")
    print()

    for i, r in enumerate(results, 1):
        if "decision" in r:
            print(f"  {i}. {r['decision'].upper():6s} | {r.get('overclaim', '?'):10s} | {r['title']}")
        elif r.get("stage") == "intake":
            print(f"  {i}. INTAKE REJECT | {r['title']}")
        elif r.get("stage") == "review":
            print(f"  {i}. REVIEW FAILED | {r.get('error', '?')[:60]} | {r['title']}")


if __name__ == "__main__":
    main()
