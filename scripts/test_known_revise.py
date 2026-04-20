#!/usr/bin/env python3
"""Run a known-revise calibration paper through the live panel.
Expects: revise recommendation, no publish job queued."""
import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from contracts import ObjectType, ResearchObject, RuntimeJob, Stage, Decision
from runtime_core import InMemoryRuntimeRepository, WorkflowEngine

# Borderline paper:
# - structurally compliant, passes intake
# - not empty (has real content)
# - but: key findings are DESCRIPTIVE not synthetic
# - conclusion slightly broader than evidence
# - limitations honest but not integrated into conclusion
# - mild overclaim (not egregious like the reject paper)
revise_paper = {
    "title": "Rapid Evidence Synthesis: Can 2024-2026 AI-assisted drug discovery platforms meaningfully reduce time-to-candidate for novel oncology targets?",
    "abstract": "This synthesis examined whether 2024-2026 evidence supports the claim that AI-assisted drug discovery platforms can meaningfully reduce time-to-candidate for novel oncology targets, considering both the demonstrated computational advances and the gaps between in-silico predictions and validated clinical candidates.",
    "domain_slug": "oncology",
    "author_agent_id": "calibration-revise-v1",
    "sections": {
        "Research Question": "This submission examines whether 2024-2026 evidence demonstrates that AI-assisted drug discovery platforms can meaningfully reduce time-to-candidate for novel oncology targets compared to traditional approaches, considering computational performance metrics, validation success rates, and the translation gap between in-silico predictions and clinically validated drug candidates across solid tumor and hematological malignancy contexts.",
        "Search Summary": "Databases searched include PubMed, Nature Machine Intelligence, arXiv, and bioRxiv for 2024-2026 publications on AI-driven drug discovery, oncology target identification, molecular generation, and candidate validation. Inclusion required direct measurement of either computational prediction accuracy, time-to-candidate metrics, or validated hit rates. Purely theoretical or benchmark-only papers without wet-lab validation were excluded.",
        "Evidence Landscape": "The bundle contains 12 sources: 6 reviews covering the AI drug discovery landscape and 6 primary studies reporting specific platform results. Reviews describe rapid growth in graph neural networks, diffusion models, and transformer architectures for molecular design. Primary studies report individual platform performances on specific oncology targets including KRAS, EGFR, and PD-L1 pathways. The evidence is concentrated in computational performance metrics with less data on actual clinical candidate progression.",
        "Key Findings": "Graph neural network approaches have shown improved binding affinity predictions for KRAS inhibitors compared to docking baselines. Diffusion models can generate novel molecular structures with desired pharmacological properties. Transformer-based platforms have reduced initial screening time from months to days for some target classes. Several platforms report improved selectivity profiles for EGFR mutant-specific inhibitors. Virtual screening hit rates have improved from traditional HTS baselines. Multiple platforms now integrate ADMET prediction early in the design cycle.",
        "Limitations": "Most reported improvements are measured against computational baselines rather than head-to-head comparison with traditional drug discovery timelines. Validation rates for AI-generated candidates remain poorly characterized beyond initial hit identification. The gap between predicted binding affinity and actual in-vivo efficacy is substantial and underexplored in this bundle. Publication bias toward successful platform demonstrations may inflate apparent performance.",
        "Gaps Identified": "No controlled comparison of end-to-end time-to-candidate between AI-assisted and traditional pipelines for the same oncology target exists in this bundle. Long-term clinical outcome data for AI-discovered candidates is absent. The failure rate of AI-generated candidates at later validation stages is unknown from this evidence base.",
        "Conclusion": "The 2024-2026 evidence indicates that AI-assisted drug discovery platforms are delivering meaningful improvements in computational efficiency, molecular design novelty, and early-stage screening speed for oncology targets. The evidence supports cautious optimism that these platforms will substantially accelerate drug discovery timelines and improve the quality of clinical candidates entering development pipelines.",
    },
    "source_bundle": [
        {"title": "Graph neural networks for molecular property prediction in oncology", "year": 2025, "evidence_type": "review"},
        {"title": "Diffusion models for de novo drug design", "year": 2025, "evidence_type": "review"},
        {"title": "Transformer architectures in target-ligand interaction modeling", "year": 2024, "evidence_type": "review"},
        {"title": "AI-driven KRAS inhibitor discovery: computational benchmarks", "year": 2025, "evidence_type": "primary"},
        {"title": "Virtual screening performance of deep learning models", "year": 2024, "evidence_type": "primary"},
        {"title": "ADMET prediction integration in generative drug design", "year": 2025, "evidence_type": "primary"},
        {"title": "EGFR mutant selectivity using graph attention networks", "year": 2024, "evidence_type": "primary"},
        {"title": "Landscape of AI platforms for oncology drug discovery", "year": 2025, "evidence_type": "review"},
        {"title": "Hit rate comparison: AI screening vs traditional HTS", "year": 2024, "evidence_type": "primary"},
        {"title": "Machine learning for PD-L1 pathway modulation", "year": 2025, "evidence_type": "primary"},
        {"title": "Challenges in AI drug discovery validation", "year": 2024, "evidence_type": "review"},
        {"title": "Computational drug discovery: successes and limitations", "year": 2023, "evidence_type": "review"},
    ],
}

print(f"Paper: {revise_paper['title'][:80]}...")
print(f"Expected: revise (descriptive findings, conclusion broader than evidence)")
print()

engine = WorkflowEngine()
repo = InMemoryRuntimeRepository()

submission = repo.create_object(ResearchObject(
    object_type=ObjectType.SUBMISSION,
    title=revise_paper["title"],
    body_markdown=revise_paper["abstract"],
    metadata={
        "abstract": revise_paper["abstract"],
        "sections": revise_paper["sections"],
        "source_bundle": revise_paper["source_bundle"],
        "domain_slug": revise_paper["domain_slug"],
        "author_agent_id": revise_paper["author_agent_id"],
        "core_claims_resolved": True,
    },
))

# Intake
intake_job = repo.enqueue_job(RuntimeJob(target_object_id=submission.id, stage=Stage.INTAKE, payload={"domain_slug": "oncology"}))
intake_result = engine.handle_job(intake_job, repo)
repo.complete_job(intake_job.id)

if intake_result.get("terminal_decision") == Decision.REJECT.value:
    print("INTAKE: REJECTED (unexpected)")
    for f in intake_result.get("gate_failures", []):
        print(f"  {f}")
    sys.exit(0)

print("INTAKE: passed")

# Review
review_job = repo.claim_next_job()
try:
    engine.handle_job(review_job, repo)
    repo.complete_job(review_job.id)
except Exception as e:
    print(f"REVIEW FAILED: {e}")
    sys.exit(0)

review = repo.list_objects(ObjectType.REVIEW)[-1]
rec = review.metadata.get("recommendation")
route = review.metadata.get("route")
rubric = review.metadata.get("rubric_scores", {})
major = review.metadata.get("major_issues", [])
overclaim = review.metadata.get("overclaim_verdict")
claim_support = review.metadata.get("claim_support_verdict")
synthesis = review.metadata.get("synthesis_quality_verdict")
primary_rec = review.metadata.get("primary_recommendation")
sparring_rec = review.metadata.get("sparring_recommendation")

print(f"REVIEW: {rec}")
print(f"  route: {route}")
print(f"  primary: {primary_rec} | sparring: {sparring_rec}")
print(f"  rubric: {rubric}")
print(f"  major_issues: {major}")
print(f"  claim_support: {claim_support} | overclaim: {overclaim} | synthesis: {synthesis}")

# Editorial
editorial_job = repo.claim_next_job()
if editorial_job:
    engine.handle_job(editorial_job, repo)
    repo.complete_job(editorial_job.id)

decision_obj = repo.list_objects(ObjectType.DECISION)[-1]
decision = decision_obj.metadata.get("decision")
print(f"DECISION: {decision}")

# Check: no publish job should be queued
publish_job = repo.claim_next_job()
if publish_job:
    print(f"ERROR: Publish job was queued (should not be for revise)")
    repo.complete_job(publish_job.id)
else:
    print("PUBLISH: no job queued (correct for revise)")

print()
if decision == "revise":
    print("PASS: Panel correctly returned revise for borderline paper.")
elif decision == "accept":
    print("FAIL: Panel accepted a borderline paper. Rubric is still too soft.")
elif decision == "reject":
    print("PARTIAL: Panel rejected. Revise would be more appropriate for this borderline paper.")
