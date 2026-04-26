#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from copy import deepcopy
from pathlib import Path


def _source_bundle(prefix: str, *, primary_bias: bool = False) -> list[dict]:
    bundle = []
    for index in range(1, 13):
        evidence_type = "primary" if (primary_bias or index % 3 != 0) else "review"
        bundle.append(
            {
                "title": f"{prefix} source {index}",
                "doi": f"10.1000/{prefix.lower().replace(' ', '-')}.{index}",
                "url": f"https://doi.org/10.1000/{prefix.lower().replace(' ', '-')}.{index}",
                "year": 2025 if index < 9 else 2024,
                "evidence_type": evidence_type,
            }
        )
    return bundle


def _res_sections(topic: str, *, mode: str) -> dict[str, str]:
    if mode == "accept":
        return {
            "Research Question": f"This rapid evidence synthesis asks whether recent evidence supports a bounded claim about {topic}, and it specifies the intervention frame, relevant population, comparison logic, outcome target, inclusion window, and uncertainty boundary clearly enough that another reviewer could reproduce the intended scope without inventing missing assumptions or silently broadening the thesis beyond what the retained bundle can directly support.",
            "Search Summary": f"The search summary documents the databases, date window, narrowing rule, and inclusion logic used to retain twelve directly relevant sources on {topic}, and it explicitly separates stronger review-level evidence from narrower primary studies so the resulting synthesis remains auditable and bounded to the stated question.",
            "Evidence Landscape": f"The retained bundle on {topic} contains a mix of review and primary evidence, with the review layer providing the stronger framing and the primary layer mainly refining magnitude, boundary conditions, and operational limits rather than changing the overall direction of the claim.",
            "Key Findings": f"The key findings on {topic} stay narrow: the overall evidence direction is supportive, stronger reviews and better-controlled studies align on the same bounded conclusion, and the remaining uncertainty is concentrated in magnitude, external validity, and implementation details rather than in the existence of the underlying signal itself.",
            "Limitations": f"The limitations are material rather than ceremonial: direct human confirmation remains incomplete in parts of the {topic} bundle, some studies are indirect or context-specific, and the synthesis should not be read as universal proof or policy readiness beyond the exact populations and outcomes reflected in the retained sources.",
            "Gaps Identified": f"The most decision-relevant gaps for {topic} are stronger head-to-head comparisons, clearer outcome harmonization, and longer follow-up in the most promising settings, which would improve confidence in magnitude and transferability without changing the current bounded direction of the evidence.",
            "Conclusion": f"The conclusion is that {topic} currently supports a narrow and source-grounded positive synthesis, but only as a bounded claim with explicit limits; the manuscript is acceptable because it stays proportional to the bundle and does not escalate beyond what the retained evidence can directly justify.",
        }
    if mode == "revise":
        return {
            "Research Question": f"This rapid evidence synthesis asks whether recent evidence supports a bounded claim about {topic}, and the question is structurally valid, but it still leaves too much room for the manuscript to slide between mechanistic support, applied evidence, and broader translational framing in a way that makes a revise decision more defensible than a clean accept.",
            "Search Summary": f"The search summary names the databases, date window, and inclusion logic for {topic}, but it still underspecifies how contradictory or weaker sources were handled, which makes the synthesis direction understandable yet not fully audit-ready for an accept decision on first pass.",
            "Evidence Landscape": f"The evidence landscape on {topic} contains enough source material to support a useful synthesis, but the body still leans too heavily on the supportive layer without clearly isolating where the weaker or more indirect studies materially constrain confidence in the headline thesis.",
            "Key Findings": f"The key findings on {topic} are plausible and mostly source-grounded, but they compress uncertainty too aggressively, smooth over a few conflicting signals, and therefore read like a revise-grade manuscript that needs tighter evidence alignment before it should count as publication-ready.",
            "Limitations": "The limitations are present but not fully doing their job, because they acknowledge heterogeneity and missing direct confirmation without yet constraining the conclusion enough to stop a reviewer from flagging mild overclaim and partially supported claim language.",
            "Gaps Identified": f"The gaps identified for {topic} are real but still generic, especially around counterevidence, transferability, and stronger direct outcome measures, so the manuscript remains valid but insufficiently sharpened for acceptance under the current reviewer contract.",
            "Conclusion": f"The conclusion on {topic} is useful and directionally right, but it still overreaches slightly relative to the retained bundle, which is why this manuscript belongs in revise rather than accept even though it is structurally complete and not substantively broken.",
        }
    return {
        "Research Question": f"This rapid evidence synthesis claims to determine whether {topic} is broadly beneficial across most relevant settings and populations, but the question itself is overextended because it collapses mechanistic support, indirect evidence, and narrow positive signals into one sweeping thesis that the retained bundle cannot legitimately carry.",
        "Search Summary": f"The search summary gestures at a recent evidence window for {topic}, yet it does not actually justify why the retained sources should support a strong conclusion, and the narrative still treats weak or indirect material as if it had the same force as the best evidence in the bundle.",
        "Evidence Landscape": f"The evidence landscape on {topic} is presented as if it were coherent and strongly positive, but in reality the manuscript flattens important distinctions between stronger and weaker evidence, minimizes uncertainty, and never meaningfully shows why the conflicting or indirect material should not change the editorial outcome.",
        "Key Findings": f"The key findings for {topic} repeatedly turn weak directional signals into broad claims of benefit, convert mechanistic plausibility into applied confidence, and therefore outrun the retained evidence to a degree that should land in reject rather than revise under a serious gatekeeper rubric.",
        "Limitations": "The limitations section is too weak to rescue the manuscript because it admits uncertainty without materially constraining the conclusion, leaving the main overclaim and partial-support problems intact rather than honestly narrowing what the current evidence can justify.",
        "Gaps Identified": f"The gaps section acknowledges that stronger direct evidence is still missing for {topic}, but it treats those absences as future nice-to-haves instead of recognizing that they are central reasons the current synthesis should not pass editorial review in its present form.",
        "Conclusion": f"The conclusion claims too much for the current {topic} bundle and remains substantially unsupported even after its caveats, so rejection is more defensible than revision because the manuscript would require a real reset in claim scope rather than simple tightening.",
    }


def _empirical_sections(topic: str, *, mode: str) -> dict[str, str]:
    if mode == "accept":
        return {
            "Research Question": f"This empirical study asks whether a bounded intervention changes a prespecified outcome in the context of {topic}, and it states the intervention, comparator, endpoint logic, follow-up window, cohort boundaries, and exclusion rules clearly enough that another reviewer could reproduce the study question without broadening the claim beyond the reported data.",
            "Methods": f"The methods section for {topic} documents the cohort source, inclusion and exclusion criteria, intervention definition, outcome measurement, missing-data handling, and statistical plan in enough detail that a reviewer can audit whether the design answers the stated question and whether the main threats to inference have been bounded rather than ignored.",
            "Results": f"The results section for {topic} reports the prespecified endpoint, gives the direction and approximate magnitude of effect, separates exploratory subgroup observations from the primary analysis, and avoids turning one bounded dataset into a sweeping claim that the design cannot support on its own.",
            "Limitations": f"The limitations section is honest that the {topic} study is bounded by one dataset, finite follow-up, and residual confounding risk, which materially constrains generalizability while still allowing a narrow empirical accept decision because the manuscript does not pretend to prove more than it actually tests.",
            "Conclusion": f"The conclusion on {topic} stays narrow and empirical: the study adds one credible signal for one defined endpoint in one defined setting, and it does not overstate causality, universal benefit, or implementation readiness beyond what the reported data can directly support.",
        }
    if mode == "revise":
        return {
            "Research Question": f"This empirical study asks a valid question about whether a bounded intervention changes a measured outcome in the context of {topic}, and it specifies the intervention frame, comparator logic, endpoint family, and study boundaries clearly enough for review, but it still leaves enough ambiguity around endpoint hierarchy and interpretive scope that revision is more defensible than outright acceptance.",
            "Methods": f"The methods section for {topic} is mostly complete, yet it still underspecifies a few design boundaries, especially around exploratory analyses, replicate handling, subgroup slicing, and how the final narrative distinguishes prespecified endpoints from looser supporting observations that should not carry the same inferential weight.",
            "Results": f"The results section for {topic} reports real data and a coherent directional pattern, but it still leans too hard on translational framing, treats some exploratory findings too confidently, and therefore invites a revise decision even though the manuscript is structurally valid, bounded, and still salvageable with tighter evidence language.",
            "Limitations": "The limitations section acknowledges narrow sampling, short follow-up, and residual uncertainty, but it does not yet constrain the conclusions enough to stop a reviewer from flagging mild overclaim or partial support in the current version of the manuscript, especially where exploratory and confirmatory findings blur together.",
            "Conclusion": f"The conclusion on {topic} is useful and directionally plausible, but it is still too expansive relative to the reported data, which is why revise is the correct editorial outcome until the claims are narrowed, the hierarchy of evidence is made clearer, and the evidence alignment is tightened.",
        }
    return {
        "Research Question": f"This empirical study claims to determine whether an intervention definitively changes broad outcomes related to {topic}, but the question itself is already too large for the dataset because it implies general causal proof from one weak or loosely controlled design.",
        "Methods": f"The methods section for {topic} leaves major inferential risks unresolved, including weak comparator logic, residual confounding, endpoint ambiguity, and insufficient justification for treating observational or indirect evidence as if it had experimental force.",
        "Results": f"The results section for {topic} repeatedly overstates small or noisy signals as if they proved a broad intervention effect, blurs the boundary between descriptive observations and causal claims, and therefore outruns the design in a way that should trigger rejection rather than mere revision.",
        "Limitations": "The limitations section is too weak to rescue the manuscript because it fails to materially constrain the central overclaim, leaving the core inference problem untouched even after the caveats are stated on paper.",
        "Conclusion": f"The conclusion on {topic} still claims broad empirical proof from a design that cannot support it, which makes rejection more defensible than revision under a serious evidence gatekeeper standard.",
    }


def _entry(entry_id: str, *, article_type: str, submission: dict, expected: str, tags: list[str], rationale: str, notes: str) -> dict:
    payload = deepcopy(submission)
    payload["article_type"] = article_type
    payload.setdefault("core_claims_resolved", True)
    return {
        "entry_id": entry_id,
        "article_type": article_type,
        "submission": payload,
        "expected": {
            "decision": expected,
            "rationale": rationale,
        },
        "tags": tags,
        "notes": notes,
    }


def build_gold_set() -> dict:
    calibration_dir = Path(__file__).resolve().parents[1] / "calibration"
    top50_drafts = json.loads((calibration_dir / "top50_antiaging_drafts_2026-04-21.json").read_text())
    top50_runs = json.loads((calibration_dir / "top50_antiaging_run_2026-04-21.json").read_text())
    draft_by_name = {item["paper_name"]: item for item in top50_drafts}

    entries: list[dict] = []

    # 10 real-draft RES entries from top50 anti-aging persisted runs
    for run in top50_runs:
        source = draft_by_name[run["paper"]]
        draft = deepcopy(source["draft"])
        actual = run["actual"]
        notes = f"Derived from {source['journal']} anchor DOI {source['anchor_doi']}."
        if actual == "reject":
            rationale = "This real drafted RES entry failed intake or clearly broke a deterministic rule and is therefore a reject control."
            tags = ["working", "real-draft", "rapid-evidence-synthesis", "reject-control", source["journal"].lower().replace(" ", "-")]
        else:
            rationale = "This real drafted RES entry reflects strong source material but currently behaves as a revise-grade synthesis artifact under live Researka review."
            tags = ["working", "real-draft", "rapid-evidence-synthesis", "revise-control", source["journal"].lower().replace(" ", "-")]
        entries.append(
            _entry(
                f"top50-{run['paper'].replace('.pdf', '').lower()}",
                article_type="rapid_evidence_synthesis",
                submission=draft,
                expected=actual,
                tags=tags,
                rationale=rationale,
                notes=notes,
            )
        )

    # 12 manual RES controls: 4 accept, 4 revise, 4 reject
    res_topics = [
        ("res-accept-1", "cellular senescence modulation", "accept", "longevity"),
        ("res-accept-2", "heat adaptation in dense cities", "accept", "climate-adaptation"),
        ("res-accept-3", "vaccine confidence interventions", "accept", "public-health"),
        ("res-accept-4", "battery storage deployment safety", "accept", "energy-transition"),
        ("res-revise-1", "extracellular vesicle anti-aging therapies", "revise", "biomedical"),
        ("res-revise-2", "kinase pathway modulation in inflammaging", "revise", "biomedical"),
        ("res-revise-3", "AI fairness evaluations in hiring", "revise", "ai-ethics"),
        ("res-revise-4", "coastal adaptation finance programs", "revise", "climate-adaptation"),
        ("res-reject-1", "universal anti-aging intervention claims", "reject", "longevity"),
        ("res-reject-2", "definitive algorithmic neutrality claims", "reject", "ai-ethics"),
        ("res-reject-3", "one-shot coral restoration success claims", "reject", "ocean-biodiversity"),
        ("res-reject-4", "broad causal proof for carbon capture deployment", "reject", "oil-gas-decarbonisation"),
    ]
    for entry_id, topic, expected, domain_slug in res_topics:
        submission = {
            "title": f"Rapid Evidence Synthesis: {topic}",
            "abstract": f"This rapid evidence synthesis evaluates the current evidence on {topic} and keeps the decision frame explicitly bounded to what the retained sources can justify.",
            "sections": _res_sections(topic, mode=expected),
            "source_bundle": _source_bundle(topic),
            "author_agent_id": "gold-set-builder",
            "domain_slug": domain_slug,
        }
        entries.append(
            _entry(
                entry_id,
                article_type="rapid_evidence_synthesis",
                submission=submission,
                expected=expected,
                tags=["working", "manual-control", "rapid-evidence-synthesis", f"{expected}-control", domain_slug],
                rationale=f"Manually authored rapid-evidence-synthesis {expected} control for {topic}.",
                notes="Manual control designed to stabilize verdict boundaries for reviewer tuning.",
            )
        )

    # 8 empirical-study controls: 3 accept, 3 revise, 2 reject
    empirical_topics = [
        ("empirical-accept-1", "mobility endpoint in older adults", "accept"),
        ("empirical-accept-2", "bounded cardiometabolic biomarker improvement", "accept"),
        ("empirical-accept-3", "well-specified digital adherence intervention", "accept"),
        ("empirical-revise-1", "short-term biomarker shift after intervention", "revise"),
        ("empirical-revise-2", "pilot cohort with exploratory subgroup effects", "revise"),
        ("empirical-revise-3", "mixed outcome dataset with partial support", "revise"),
        ("empirical-reject-1", "single-site observational anti-aging proof claim", "reject"),
        ("empirical-reject-2", "weak uncontrolled exposure framed as causal effect", "reject"),
    ]
    for entry_id, topic, expected in empirical_topics:
        submission = {
            "title": f"Empirical Study: {topic}",
            "abstract": f"This empirical study reports one bounded dataset related to {topic} and tests whether the manuscript framing stays proportional to the reported design and outcomes.",
            "sections": _empirical_sections(topic, mode=expected),
            "source_bundle": _source_bundle(topic, primary_bias=True),
            "author_agent_id": "gold-set-builder",
            "domain_slug": "biomedical",
        }
        entries.append(
            _entry(
                entry_id,
                article_type="empirical_study",
                submission=submission,
                expected=expected,
                tags=["working", "manual-control", "empirical-study", f"{expected}-control", "biomedical"],
                rationale=f"Manually authored empirical-study {expected} control for {topic}.",
                notes="Manual empirical control designed to keep article-type routing visible in the evaluator.",
            )
        )

    return {
        "version": "gold-set-v1-working",
        "entries": entries,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the working gold_set_v1 corpus.")
    parser.add_argument("--output", default="calibration/gold_set_v1.json", help="Output path")
    args = parser.parse_args()

    corpus = build_gold_set()
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(corpus, indent=2))
    print(output)
    print(f"entries {len(corpus['entries'])}")


if __name__ == "__main__":
    main()
