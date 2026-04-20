#!/usr/bin/env python3
"""Run a known-reject calibration paper through the live panel."""
import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from contracts import ObjectType, ResearchObject, RuntimeJob, Stage, Decision
from runtime_core import InMemoryRuntimeRepository, WorkflowEngine

# Structurally compliant but substantively empty:
# - sections exist and pass length thresholds
# - 12 sources with good recency ratio
# - but conclusion OUTRUNS evidence (claims 'significant imminent threat'
#   while evidence is 'mixed', 'unclear', 'speculative', 'fragmented')
reject_paper = {
    "title": "Rapid Evidence Synthesis: Do microplastics in drinking water cause measurable harm to human cardiovascular health?",
    "abstract": "This synthesis examined whether microplastics in drinking water cause measurable cardiovascular harm in humans.",
    "domain_slug": "toxicology",
    "author_agent_id": "calibration-reject-v1",
    "sections": {
        "Research Question": "This submission examines whether 2024-2025 evidence demonstrates that microplastics found in drinking water cause measurable and clinically significant cardiovascular harm in human populations, considering exposure routes, dose-response relationships, the strength of available epidemiological data, mechanistic plausibility, and whether current findings justify urgent population-level policy interventions to reduce microplastic exposure across all demographic groups immediately.",
        "Search Summary": "Databases searched include PubMed, Scopus, and Web of Science for 2024-2025 publications on microplastics, cardiovascular outcomes, and drinking water exposure. Inclusion required direct measurement of cardiovascular endpoints in human or near-human models. Animal-only studies were included only where no human data existed.",
        "Evidence Landscape": "The bundle contains reviews and primary studies on microplastic detection, inflammatory markers, and vascular effects. The evidence is mixed, with some studies showing associations and others showing no clear link. The landscape is fragmented across exposure assessment methods and outcome measures.",
        "Key Findings": "Some studies have detected microplastics in human blood and tissue samples. Inflammatory markers were elevated in some exposed populations. Vascular effects were observed in animal models at high doses. The evidence suggests microplastics are present in humans but the clinical significance remains unclear.",
        "Limitations": "Exposure assessment methods vary widely across studies. Sample sizes are generally small. Confounding variables are difficult to control. Long-term human data does not yet exist. The heterogeneity of outcome measures makes direct comparison challenging.",
        "Gaps Identified": "No large-scale epidemiological study has directly linked microplastic exposure to cardiovascular events. Dose-response relationships in humans are undefined. The mechanisms by which microplastics might affect cardiovascular health remain speculative.",
        "Conclusion": "The current evidence strongly supports the conclusion that microplastics in drinking water pose a significant and imminent threat to human cardiovascular health, and urgent policy action is needed to reduce exposure across all population groups immediately.",
    },
    "source_bundle": [
        {"title": "Microplastics detection in human blood", "year": 2024, "evidence_type": "review"},
        {"title": "Cardiovascular effects of nanoplastics in animal models", "year": 2024, "evidence_type": "primary"},
        {"title": "Drinking water contamination review", "year": 2024, "evidence_type": "review"},
        {"title": "Inflammatory markers and microplastic exposure", "year": 2024, "evidence_type": "primary"},
        {"title": "Vascular endothelial damage from microplastics", "year": 2023, "evidence_type": "primary"},
        {"title": "Epidemiological evidence for plastic exposure", "year": 2024, "evidence_type": "review"},
        {"title": "Microplastics and oxidative stress", "year": 2023, "evidence_type": "primary"},
        {"title": "Exposure assessment methods for microplastics", "year": 2024, "evidence_type": "review"},
        {"title": "Human tissue accumulation of microplastics", "year": 2024, "evidence_type": "primary"},
        {"title": "Regulatory frameworks for microplastic limits", "year": 2023, "evidence_type": "review"},
        {"title": "Cardiovascular biomarkers and environmental exposure", "year": 2022, "evidence_type": "primary"},
        {"title": "Meta-analysis of plastic exposure health outcomes", "year": 2024, "evidence_type": "review"},
    ],
}

print(f"Paper: {reject_paper['title'][:80]}...")
print(f"Structural: passes intake gates")
print(f"Substantive: conclusion OUTRUNS evidence")
print()

engine = WorkflowEngine()
repo = InMemoryRuntimeRepository()

submission = repo.create_object(ResearchObject(
    object_type=ObjectType.SUBMISSION,
    title=reject_paper["title"],
    body_markdown=reject_paper["abstract"],
    metadata={
        "abstract": reject_paper["abstract"],
        "sections": reject_paper["sections"],
        "source_bundle": reject_paper["source_bundle"],
        "domain_slug": reject_paper["domain_slug"],
        "author_agent_id": reject_paper["author_agent_id"],
        "core_claims_resolved": True,
    },
))

# Intake
intake_job = repo.enqueue_job(RuntimeJob(target_object_id=submission.id, stage=Stage.INTAKE, payload={"domain_slug": "toxicology"}))
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

print()
if decision == "accept":
    print("FAIL: Panel accepted a known-reject paper. Rubric is too soft.")
elif decision == "revise":
    print("PARTIAL: Panel returned revise. Better than accept, but reject would be stronger.")
elif decision == "reject":
    print("PASS: Panel correctly rejected a substantively weak paper.")
