#!/usr/bin/env python3
"""
Run an N-paper benchmark through the Researka v2 pipeline.

Usage:
    python scripts/run_benchmark.py [--count 200] [--output artifacts/benchmark_baseline.json]
    python scripts/run_benchmark.py --dry-run          # uses deterministic provider, 10 papers
    python scripts/run_benchmark.py --count 10          # 10 papers with live provider

Requires RESEARKA_V2_PROVIDER env var (default: deterministic).
For judge_panel: MINIMAX_API_KEY, MIMO_API_KEY, DEEPSEEK_API_KEY must be set.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from contracts import Decision, ObjectType, ResearchObject, RuntimeJob, Stage
from runtime_core import InMemoryRuntimeRepository, WorkflowEngine

# ---------------------------------------------------------------------------
# Synthetic paper generation
# ---------------------------------------------------------------------------

DOMAINS = [
    "longevity",
    "ai-ethics",
    "public-health",
    "climate-adaptation",
    "ocean-biodiversity",
    "energy-transition",
    "oil-gas-decarbonisation",
    "biomedical-engineering",
    "neuroscience",
    "genomics",
]

SOURCE_TEMPLATES = [
    {"title": "Systematic review of {topic} interventions", "evidence_type": "review", "year": 2025,
     "doi": "10.1000/{id}", "url": "https://doi.org/10.1000/{id}"},
    {"title": "Primary study: {topic} efficacy in {model}", "evidence_type": "primary", "year": 2026,
     "doi": "10.1000/{id}", "url": "https://doi.org/10.1000/{id}"},
    {"title": "Meta-analysis of {topic} outcomes", "evidence_type": "review", "year": 2025,
     "doi": "10.1000/{id}", "url": "https://doi.org/10.1000/{id}"},
    {"title": "Randomized trial of {topic} in {model}", "evidence_type": "primary", "year": 2026,
     "doi": "10.1000/{id}", "url": "https://doi.org/10.1000/{id}"},
    {"title": "Cochrane review of {topic} safety profiles", "evidence_type": "review", "year": 2024,
     "doi": "10.1000/{id}", "url": "https://doi.org/10.1000/{id}"},
    {"title": "Longitudinal cohort: {topic} long-term effects", "evidence_type": "primary", "year": 2025,
     "doi": "10.1000/{id}", "url": "https://doi.org/10.1000/{id}"},
    {"title": "Mechanistic analysis of {topic} pathways", "evidence_type": "primary", "year": 2026,
     "doi": "10.1000/{id}", "url": "https://doi.org/10.1000/{id}"},
    {"title": "Review: emerging approaches to {topic}", "evidence_type": "review", "year": 2025,
     "doi": "10.1000/{id}", "url": "https://doi.org/10.1000/{id}"},
    {"title": "Clinical evidence for {topic} in diverse populations", "evidence_type": "primary", "year": 2025,
     "doi": "10.1000/{id}", "url": "https://doi.org/10.1000/{id}"},
    {"title": "Umbrella review of {topic} interventions", "evidence_type": "review", "year": 2026,
     "doi": "10.1000/{id}", "url": "https://doi.org/10.1000/{id}"},
    {"title": "Dose-response relationship in {topic}", "evidence_type": "primary", "year": 2025,
     "doi": "10.1000/{id}", "url": "https://doi.org/10.1000/{id}"},
    {"title": "Comparative effectiveness of {topic} strategies", "evidence_type": "review", "year": 2026,
     "doi": "10.1000/{id}", "url": "https://doi.org/10.1000/{id}"},
    {"title": "Biomarker validation for {topic} outcomes", "evidence_type": "primary", "year": 2025,
     "doi": "10.1000/{id}", "url": "https://doi.org/10.1000/{id}"},
    {"title": "Translational gaps in {topic} research", "evidence_type": "review", "year": 2026,
     "doi": "10.1000/{id}", "url": "https://doi.org/10.1000/{id}"},
    {"title": "Health economics of {topic} implementation", "evidence_type": "review", "year": 2025,
     "doi": "10.1000/{id}", "url": "https://doi.org/10.1000/{id}"},
    {"title": "Patient-reported outcomes in {topic} trials", "evidence_type": "primary", "year": 2026,
     "doi": "10.1000/{id}", "url": "https://doi.org/10.1000/{id}"},
]

DOMAIN_TOPICS = {
    "longevity": "anti-aging",
    "ai-ethics": "algorithmic bias",
    "public-health": "vaccine hesitancy",
    "climate-adaptation": "heat resilience",
    "ocean-biodiversity": "coral restoration",
    "energy-transition": "battery storage",
    "oil-gas-decarbonisation": "carbon capture",
    "biomedical-engineering": "tissue scaffolds",
    "neuroscience": "neuroplasticity",
    "genomics": "CRISPR safety",
}

MODELS = ["human", "mouse", "in-vitro", "computational", "primate", "cohort"]
EXPECTED_BY_QUALITY = {
    "high": Decision.ACCEPT.value,
    "medium": Decision.REVISE.value,
    "low": Decision.REJECT.value,
    "broken": Decision.REJECT.value,
}
DECISION_LABELS = [Decision.ACCEPT.value, Decision.REVISE.value, Decision.REJECT.value]


def _build_source_bundle(domain: str, count: int, paper_id: int) -> list[dict]:
    topic = DOMAIN_TOPICS.get(domain, "intervention")
    bundle = []
    for i in range(count):
        template = SOURCE_TEMPLATES[i % len(SOURCE_TEMPLATES)]
        entry = {
            "title": template["title"].format(
                topic=topic,
                model=MODELS[i % len(MODELS)],
            ),
            "evidence_type": template["evidence_type"],
            "year": template["year"],
            "doi": template["doi"].format(id=f"bench.{paper_id}.{i}"),
            "url": template["url"].format(id=f"bench.{paper_id}.{i}"),
        }
        bundle.append(entry)
    return bundle


def _make_sections(domain: str, quality: str, paper_id: int) -> dict:
    topic = DOMAIN_TOPICS.get(domain, "intervention")

    rq = (
        f"Among recent studies on {topic}, does the evidence support implementing "
        f"targeted {topic} strategies to improve measurable outcomes in relevant populations, "
        f"and can that claim be defended without overstating mechanistic signals "
        f"as proof of broad population benefit? Furthermore, what specific methodological "
        f"limitations constrain the current evidence base, and how do these gaps "
        f"affect the reliability of any policy recommendations drawn from this synthesis?"
    )
    if quality == "broken":
        rq = "Is it good?"  # too short — will fail intake

    search = (
        f"The search prioritized 2024-2026 peer-reviewed studies on {topic}. "
        f"Evidence was included when it directly addressed {topic} efficacy, safety, "
        f"or implementation."
    )
    landscape = (
        f"The bundle contains 12 sources spanning reviews and primary studies. "
        f"Reviews synthesize the field's translational logic and known bottlenecks. "
        f"Primary studies concentrate on mechanistic refinement and efficacy. "
    )
    findings = (
        f"First, {topic} remains a credible intervention target with consistent signals "
        f"across studies. Second, the center of gravity has shifted toward precision approaches. "
        f"Third, new evidence cuts both ways with both supportive and cautionary findings. "
        f"Fourth, population-level proof remains limited in the current evidence window."
    )
    gaps = (
        f"No generalized human randomized evidence yet shows broad benefit from {topic}, "
        f"and the field still lacks stable biomarkers and safety-calibrated trials."
    )
    limitations = (
        f"The evidence base is heterogeneous. Functional outcomes differ by context, "
        f"which limits clean aggregation. Human data directly testing generalized benefit "
        f"are still sparse."
    )
    conclusion = (
        f"The evidence supports a calibrated position: {topic} remains credible "
        f"mechanistically, but falls short of proving broad population-level benefit. "
        f"The most defensible position is that targeted strategies may become useful "
        f"for specific indications, provided safety and efficacy gaps are addressed."
    )

    if quality == "high":
        search += (
            " The search explicitly documented database coverage, inclusion criteria, and direct-outcome filters, "
            "so the retained bundle is tightly aligned to the stated question rather than adjacent mechanistic literature."
        )
        landscape += (
            " Multiple recent reviews and directly relevant primary studies converge on the same bounded answer, "
            "with replication across settings and no major contradiction on the core claim."
        )
        findings = (
            f"Directly cited reviews and primary studies support a narrow answer: targeted {topic} strategies show "
            f"repeatable benefits in the populations actually studied, the positive effects are not driven by a single outlier source, "
            f"and the manuscript does not rely on mechanistic speculation to claim impact beyond the retained evidence bundle."
        )
        gaps = (
            f"Remaining gaps are mostly about optimization, longer follow-up, and transferability across adjacent settings, "
            f"not about whether the core bounded claim is supported by the cited bundle."
        )
        limitations = (
            f"The limitations section is honest that follow-up is finite and external validity is bounded, "
            f"but those limits constrain magnitude and transferability rather than overturning the directly supported core conclusion."
        )
        conclusion = (
            f"The evidence supports a publication-ready bounded conclusion: {topic} strategies are justified for the specific contexts represented in the bundle, "
            f"the claims remain proportional to the cited evidence, and no scope reset is needed beyond minor polish."
        )
    elif quality == "low":
        search += (
            " The search is broad but noisy, mixing adjacent mechanistic papers, indirect proxy outcomes, and context-mismatched sources "
            "that only weakly speak to the stated question."
        )
        landscape += (
            " Much of the bundle is indirect, uses surrogate endpoints, or studies adjacent populations, so the manuscript keeps leaning on thin support for its practical claims."
        )
        findings = (
            f"The manuscript repeatedly stretches from mechanistic or context-mismatched evidence to practical claims about {topic}, "
            f"treats indirect signal as if it were direct outcome evidence, and never establishes that the cited bundle actually supports the headline conclusion."
        )
        gaps = (
            f"The critical gaps are foundational: direct outcome evidence is thin, external validity is unresolved, and the bundle does not justify policy, deployment, or broad causal language."
        )
        limitations = (
            f"The limitations are severe enough to change the decision, because the evidence is indirect, heterogeneous, and too weakly matched to the question to support the manuscript's current framing."
        )
        conclusion = (
            f"The current manuscript should not be treated as publication-ready or merely polishable: the evidence for {topic} is too indirect for the claims being made, and the paper needs a scope reset rather than bounded revision."
        )

    sections = {
        "Research Question": rq,
        "Search Summary": search,
        "Evidence Landscape": landscape,
        "Gaps Identified": gaps,
        "Key Findings": findings,
        "Limitations": limitations,
        "Conclusion": conclusion,
    }
    if quality == "broken":
        del sections["Research Question"]  # missing section
    return sections


def expected_decision_for_paper(paper: dict) -> str:
    expected = paper.get("_benchmark_expected_decision")
    if isinstance(expected, str) and expected:
        return expected
    editorial = paper.get("_benchmark_editorial_verdict")
    if isinstance(editorial, str) and editorial in DECISION_LABELS:
        return editorial
    quality = paper.get("_benchmark_quality", "medium")
    return EXPECTED_BY_QUALITY.get(quality, Decision.REVISE.value)


def submission_payload_for_paper(paper: dict) -> dict:
    return {
        "title": paper["title"],
        "abstract": paper.get("abstract", ""),
        "sections": paper.get("sections", {}),
        "source_bundle": paper.get("source_bundle", []),
        "author_agent_id": paper.get("author_agent_id", "benchmark"),
        "article_type": paper.get("article_type", "rapid_evidence_synthesis"),
        "domain_slug": paper.get("domain_slug", "general"),
        "core_claims_resolved": True,
    }


def actual_label_for_record(record: dict) -> str:
    decision = record.get("decision")
    if isinstance(decision, str) and decision in DECISION_LABELS:
        return decision
    outcome = record.get("outcome")
    if outcome == "intake_rejected":
        return Decision.REJECT.value
    if isinstance(outcome, str) and outcome:
        return outcome
    return "unknown"


def generate_papers(count: int) -> list[dict]:
    papers = []
    qualities = ["high", "high", "medium", "medium", "medium", "medium", "low", "low", "broken"]
    bundle_sizes = [12, 14, 16, 20]
    for i in range(count):
        domain = DOMAINS[i % len(DOMAINS)]
        quality = qualities[i % len(qualities)]
        bundle_size = bundle_sizes[i % len(bundle_sizes)]
        paper_id = i + 1
        papers.append({
            "title": f"Benchmark Paper #{paper_id}: {DOMAIN_TOPICS[domain]} evidence synthesis",
            "abstract": (
                f"This Rapid Evidence Synthesis examined whether current evidence supports "
                f"targeted {DOMAIN_TOPICS.get(domain, 'intervention')} strategies. "
                f"The evidence base spans reviews and primary studies across multiple contexts."
            ),
            "domain_slug": domain,
            "author_agent_id": f"benchmark-agent-{paper_id}",
            "sections": _make_sections(domain, quality, paper_id),
            "source_bundle": _build_source_bundle(domain, bundle_size, paper_id),
            "_benchmark_quality": quality,
            "_benchmark_bundle_size": bundle_size,
            "_benchmark_paper_id": paper_id,
            "_benchmark_expected_decision": EXPECTED_BY_QUALITY.get(quality, Decision.REVISE.value),
        })
    return papers


# ---------------------------------------------------------------------------
# Pipeline runner
# ---------------------------------------------------------------------------

def run_paper(submission_data: dict, engine: WorkflowEngine, repo: InMemoryRuntimeRepository) -> dict:
    t0 = time.time()
    paper_id = submission_data.get("_benchmark_paper_id", "?")
    title = submission_data["title"]

    submission = repo.create_object(
        ResearchObject(
            object_type=ObjectType.SUBMISSION,
            title=title,
            body_markdown=submission_data.get("abstract", ""),
            metadata=submission_payload_for_paper(submission_data),
        )
    )

    record = {
        "paper_id": paper_id,
        "title": title,
        "domain": submission_data.get("domain_slug", "general"),
        "quality": submission_data.get("_benchmark_quality", "medium"),
        "style": submission_data.get("_style_tag", "house"),
        "bundle_size": submission_data.get("_benchmark_bundle_size", 12),
        "expected_decision": expected_decision_for_paper(submission_data),
        "stage_reached": None,
        "outcome": None,
        "recommendation": None,
        "decision": None,
        "route": None,
        "rubric_scores": None,
        "major_issues_count": None,
        "overclaim": None,
        "claim_support": None,
        "synthesis_quality": None,
        "cost_usd": 0.0,
        "tokens_in": 0,
        "tokens_out": 0,
        "error": None,
        "duration_s": 0.0,
    }

    # Intake
    try:
        intake_job = repo.enqueue_job(
            RuntimeJob(
                target_object_id=submission.id,
                stage=Stage.INTAKE,
                payload={"domain_slug": submission_data.get("domain_slug", "general")},
            )
        )
        intake_result = engine.handle_job(intake_job, repo)
        repo.complete_job(intake_job.id)
    except Exception as e:
        record["stage_reached"] = "intake"
        record["outcome"] = "intake_error"
        record["error"] = str(e)
        record["duration_s"] = round(time.time() - t0, 3)
        return record

    if intake_result.get("terminal_decision") == Decision.REJECT.value:
        record["stage_reached"] = "intake"
        record["outcome"] = "intake_rejected"
        record["decision"] = Decision.REJECT.value
        record["error"] = intake_result.get("notes", ["intake gate failure"])
        record["duration_s"] = round(time.time() - t0, 3)
        return record

    # Review
    review_job = repo.claim_next_job()
    if review_job is None:
        record["stage_reached"] = "review"
        record["outcome"] = "no_review_job"
        record["duration_s"] = round(time.time() - t0, 3)
        return record

    try:
        engine.handle_job(review_job, repo)
        repo.complete_job(review_job.id)
    except Exception as e:
        record["stage_reached"] = "review"
        record["outcome"] = "review_error"
        record["error"] = str(e)
        record["duration_s"] = round(time.time() - t0, 3)
        return record

    review = repo.list_objects(ObjectType.REVIEW)[-1]
    rec = review.metadata.get("recommendation", "?")
    route = review.metadata.get("route", "single")
    rubric = review.metadata.get("rubric_scores", {})
    major = review.metadata.get("major_issues", [])
    overclaim = review.metadata.get("overclaim_verdict", "?")
    claim_support = review.metadata.get("claim_support_verdict", "?")
    synthesis = review.metadata.get("synthesis_quality_verdict", "?")
    tokens_in = review.metadata.get("tokens_in", 0)
    tokens_out = review.metadata.get("tokens_out", 0)
    cost = review.metadata.get("cost_usd", 0)

    record["recommendation"] = rec
    record["route"] = route
    record["rubric_scores"] = rubric
    record["major_issues_count"] = len(major)
    record["overclaim"] = overclaim
    record["claim_support"] = claim_support
    record["synthesis_quality"] = synthesis
    record["tokens_in"] = tokens_in
    record["tokens_out"] = tokens_out
    record["cost_usd"] = cost

    # Editorial
    editorial_job = repo.claim_next_job()
    if editorial_job:
        try:
            engine.handle_job(editorial_job, repo)
            repo.complete_job(editorial_job.id)
        except Exception as e:
            record["stage_reached"] = "editorial"
            record["outcome"] = "editorial_error"
            record["error"] = str(e)
            record["duration_s"] = round(time.time() - t0, 3)
            return record

    decision_obj = repo.list_objects(ObjectType.DECISION)[-1]
    decision = decision_obj.metadata.get("decision", "?")
    record["decision"] = decision

    # Publish if accepted
    publish_job = repo.claim_next_job()
    if publish_job:
        try:
            engine.handle_job(publish_job, repo)
            repo.complete_job(publish_job.id)
        except Exception as e:
            record["stage_reached"] = "publish"
            record["outcome"] = "publish_error"
            record["error"] = str(e)
            record["duration_s"] = round(time.time() - t0, 3)
            return record

    record["stage_reached"] = "done"
    record["outcome"] = decision
    record["duration_s"] = round(time.time() - t0, 3)
    return record


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def aggregate(records: list[dict]) -> dict:
    total = len(records)
    completed = [r for r in records if r["stage_reached"] == "done"]
    intake_rejected = [r for r in records if r["outcome"] == "intake_rejected"]
    errors = [r for r in records if r["error"] and r["stage_reached"] != "intake"]

    decisions = [r for r in records if r["decision"] is not None]
    accepts = sum(1 for r in decisions if r["decision"] == Decision.ACCEPT.value)
    revises = sum(1 for r in decisions if r["decision"] == Decision.REVISE.value)
    rejects = sum(1 for r in decisions if r["decision"] == Decision.REJECT.value)

    tiebreaks = sum(1 for r in completed if r.get("route") == "fallback_tiebreak")
    consensus = sum(1 for r in completed if r.get("route") == "consensus")

    costs = [r["cost_usd"] for r in records if r["cost_usd"]]
    durations = [r["duration_s"] for r in records if r["duration_s"]]
    confusion_matrix = {
        expected: {actual: 0 for actual in DECISION_LABELS}
        for expected in DECISION_LABELS
    }
    correct = 0
    mismatches = []

    for r in records:
        expected = r.get("expected_decision")
        actual = actual_label_for_record(r)
        if expected in confusion_matrix:
            if actual not in confusion_matrix[expected]:
                confusion_matrix[expected][actual] = 0
            confusion_matrix[expected][actual] += 1
            if actual == expected:
                correct += 1
            else:
                mismatches.append(
                    {
                        "paper_id": r.get("paper_id"),
                        "title": r.get("title"),
                        "quality": r.get("quality"),
                        "domain": r.get("domain"),
                        "expected": expected,
                        "actual": actual,
                        "stage_reached": r.get("stage_reached"),
                        "route": r.get("route"),
                        "error": r.get("error"),
                    }
                )

    by_quality = {}
    for q in ("high", "medium", "low", "broken"):
        group = [r for r in records if r.get("quality") == q]
        if group:
            g_decisions = [r for r in group if r["decision"] is not None]
            g_correct = sum(1 for r in group if actual_label_for_record(r) == r.get("expected_decision"))
            by_quality[q] = {
                "count": len(group),
                "expected_decision": EXPECTED_BY_QUALITY.get(q, Decision.REVISE.value),
                "correct": g_correct,
                "accuracy": round(g_correct / len(group), 3) if group else 0,
                "accept_rate": round(sum(1 for r in g_decisions if r["decision"] == Decision.ACCEPT.value) / len(group), 3) if group else 0,
                "revise_rate": round(sum(1 for r in g_decisions if r["decision"] == Decision.REVISE.value) / len(group), 3) if group else 0,
                "reject_rate": round(sum(1 for r in g_decisions if r["decision"] == Decision.REJECT.value) / len(group), 3) if group else 0,
                "intake_reject_rate": round(sum(1 for r in group if r["outcome"] == "intake_rejected") / len(group), 3) if group else 0,
            }

    by_domain = {}
    for r in records:
        d = r.get("domain", "unknown")
        if d not in by_domain:
            by_domain[d] = {"count": 0, "accept": 0, "revise": 0, "reject": 0, "intake_rejected": 0}
        by_domain[d]["count"] += 1
        if r["decision"] == Decision.ACCEPT.value:
            by_domain[d]["accept"] += 1
        elif r["decision"] == Decision.REVISE.value:
            by_domain[d]["revise"] += 1
        elif r["decision"] == Decision.REJECT.value:
            by_domain[d]["reject"] += 1
        if r["outcome"] == "intake_rejected":
            by_domain[d]["intake_rejected"] += 1

    by_style = {}
    for r in records:
        style = r.get("style", "house")
        if style not in by_style:
            by_style[style] = {"count": 0, "correct": 0, "accept": 0, "revise": 0, "reject": 0, "intake_rejected": 0}
        by_style[style]["count"] += 1
        if actual_label_for_record(r) == r.get("expected_decision"):
            by_style[style]["correct"] += 1
        if r["decision"] == Decision.ACCEPT.value:
            by_style[style]["accept"] += 1
        elif r["decision"] == Decision.REVISE.value:
            by_style[style]["revise"] += 1
        elif r["decision"] == Decision.REJECT.value:
            by_style[style]["reject"] += 1
        if r["outcome"] == "intake_rejected":
            by_style[style]["intake_rejected"] += 1

    for style, stats in by_style.items():
        count = stats["count"]
        stats["accuracy"] = round(stats["correct"] / count, 3) if count else 0
        stats["accept_rate"] = round(stats["accept"] / count, 3) if count else 0
        stats["revise_rate"] = round(stats["revise"] / count, 3) if count else 0
        stats["reject_rate"] = round(stats["reject"] / count, 3) if count else 0
        stats["intake_reject_rate"] = round(stats["intake_rejected"] / count, 3) if count else 0

    return {
        "total": total,
        "completed": len(completed),
        "intake_rejected": len(intake_rejected),
        "errors": len(errors),
        "correct": correct,
        "accuracy": round(correct / total, 3) if total else 0,
        "accepts": accepts,
        "revises": revises,
        "rejects": rejects,
        "accept_rate": round(accepts / total, 3) if total else 0,
        "revise_rate": round(revises / total, 3) if total else 0,
        "reject_rate": round(rejects / total, 3) if total else 0,
        "disagreement_rate": round(tiebreaks / len(completed), 3) if completed else 0,
        "consensus_count": consensus,
        "tiebreak_count": tiebreaks,
        "total_cost_usd": round(sum(costs), 4),
        "median_cost_usd": round(sorted(costs)[len(costs) // 2], 4) if costs else 0,
        "mean_duration_s": round(sum(durations) / len(durations), 3) if durations else 0,
        "median_duration_s": round(sorted(durations)[len(durations) // 2], 3) if durations else 0,
        "error_rate": round(len(errors) / total, 3) if total else 0,
        "confusion_matrix": confusion_matrix,
        "mismatches": mismatches,
        "by_quality": by_quality,
        "by_domain": by_domain,
        "by_style": by_style,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Researka v2 benchmark runner")
    parser.add_argument("--count", type=int, default=200, help="Number of synthetic papers")
    parser.add_argument("--output", default="artifacts/benchmark_baseline.json", help="Output artifact path")
    parser.add_argument("--dry-run", action="store_true", help="Use deterministic provider, 10 papers")
    args = parser.parse_args()

    if args.dry_run:
        os.environ["RESEARKA_V2_PROVIDER"] = "deterministic"
        count = min(args.count, 10)
    else:
        count = args.count

    provider_name = os.getenv("RESEARKA_V2_PROVIDER", "deterministic")
    print(f"Provider: {provider_name}")
    print(f"Papers: {count}")
    print()

    papers = generate_papers(count)
    engine = WorkflowEngine()
    repo = InMemoryRuntimeRepository()

    records = []
    t_start = time.time()

    for i, paper in enumerate(papers, 1):
        quality = paper.get("_benchmark_quality", "medium")
        print(f"[{i}/{count}] Q={quality} | {paper['domain_slug']}", end="", flush=True)
        record = run_paper(paper, engine, repo)
        status = record["decision"] or record["outcome"]
        cost_str = f" ${record['cost_usd']:.4f}" if record["cost_usd"] > 0 else ""
        print(f" -> {status}{cost_str} ({record['duration_s']:.2f}s)")
        records.append(record)

    elapsed = round(time.time() - t_start, 2)
    agg = aggregate(records)

    artifact = {
        "run_meta": {
            "provider": provider_name,
            "paper_count": count,
            "elapsed_s": elapsed,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
        "aggregates": agg,
        "papers": records,
    }

    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    with open(args.output, "w") as f:
        json.dump(artifact, f, indent=2)

    print()
    print("=" * 70)
    print("BENCHMARK RESULTS")
    print("=" * 70)
    print(f"Total papers:    {agg['total']}")
    print(f"Accuracy:        {agg['correct']} / {agg['total']} ({agg['accuracy']:.1%})")
    print(f"Accept:          {agg['accepts']} ({agg['accept_rate']:.1%})")
    print(f"Revise:          {agg['revises']} ({agg['revise_rate']:.1%})")
    print(f"Reject:          {agg['rejects']} ({agg['reject_rate']:.1%})")
    print(f"Intake rejected: {agg['intake_rejected']}")
    print(f"Errors:          {agg['errors']} ({agg['error_rate']:.1%})")
    print(f"Disagreement:    {agg['tiebreak_count']} / {agg['completed']} ({agg['disagreement_rate']:.1%})")
    print(f"Total cost:      ${agg['total_cost_usd']:.4f}")
    print(f"Median duration: {agg['median_duration_s']:.3f}s")
    print(f"Elapsed:         {elapsed:.1f}s")
    print()
    print("By quality:")
    for q, stats in agg.get("by_quality", {}).items():
        print(f"  {q:8s}  n={stats['count']:4d}  expected={stats['expected_decision']:6s}  "
              f"accuracy={stats['accuracy']:.0%}  accept={stats['accept_rate']:.0%}  "
              f"revise={stats['revise_rate']:.0%}  reject={stats['reject_rate']:.0%}  "
              f"intake_rej={stats['intake_reject_rate']:.0%}")
    print()
    print("Confusion matrix:")
    for expected, actuals in agg.get("confusion_matrix", {}).items():
        cells = "  ".join(f"{actual}={count}" for actual, count in actuals.items())
        print(f"  expected {expected:6s} -> {cells}")
    print()
    print(f"Artifact saved to: {args.output}")


if __name__ == "__main__":
    main()
