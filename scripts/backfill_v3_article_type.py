#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from contracts.models import ArticleType, Decision

SPECIAL_SYNTHESIS_TITLES = {
    "NAD World 3.0: Systemic NAD+ Metabolism and the Role of NMN Transporters and Extracellular eNAMPT in Organismal Aging",
    "Forty Percent and Rising: The Global Obesity Epidemic and Emerging Pharmacological Interventions",
}

SYNTHESIS_MARKERS = (
    "meta-analysis",
    "critical evaluation",
    "critical review",
    "critical examination",
    "critical analysis",
    "global assessment",
    "sectoral analysis",
    "vulnerability assessment",
    "understanding ",
    "fundamental advances",
)

MICRO_TEST_TITLES = (
    "Ginkgolide B Extends Healthspan and Lifespan Through Neuronal NAD+ Restoration and Mitochondrial Proteostasis",
    "Liberal versus Restrictive Transfusion Thresholds in High-Cardiac-Risk Patients Undergoing Non-Cardiac Surgery: The TOP Trial",
    "Targeted Capillary Refill Time-Guided Resuscitation versus Usual Care in Early Septic Shock: The ANDROMEDA-SHOCK-2 Trial",
    "Sodium Bicarbonate Therapy for Severe Metabolic Acidemia Complicating Acute Kidney Injury: The BICARICU-2 Randomized Trial",
    "Rituximab versus Tacrolimus for Maintenance Immunosuppression in Adult Relapsing Nephrotic Syndrome: A Randomized Controlled Trial",
    "Dual Antiplatelet Therapy after Percutaneous Coronary Intervention According to Bleeding Risk: The HOST-BR Randomized Clinical Trial",
    "MathNet: A Global Multimodal Benchmark for Mathematical Reasoning and Retrieval in Large Language Models",
    "TRIALSCOPE: Clinical Trial Simulation from Real-World Data Using Causal Machine Learning for Counterfactual Prediction",
    "Apollo: A Multimodal Temporal Foundation Model for Virtual Patient Representations at Healthcare System Scale",
    "Organism-Wide Cellular Dynamics and Epigenomic Remodeling During Natural Aging in Non-Human Primates",
    "Global Greenhouse Gas Emissions Mitigation Potential and Life-Cycle Assessment of Green Hydrogen Projects: A Meta-Analysis",
    "Improving Energy Return on Investment Calculations for Green Hydrogen Pathways: A Critical Review and Corrected Methodology",
    "Is Taurine an Aging Biomarker? A Critical Evaluation of Taurine Supplementation Studies and Their Implications for Lifespan Extension Claims",
    "Discordance Between Creatinine- and Cystatin C-Based eGFR: Clinical Implications and Prognostic Significance - A Critical Meta-Analysis",
    "Critical examination of cold fusion: reconciling conflicting experimental results - reconciling conflicting evidence",
)


def infer_v3_article_type(paper: dict) -> str:
    title = str(paper.get("title", "")).strip()
    lowered = title.lower()
    if title in SPECIAL_SYNTHESIS_TITLES:
        return ArticleType.RAPID_EVIDENCE_SYNTHESIS.value
    if any(marker in lowered for marker in SYNTHESIS_MARKERS):
        return ArticleType.RAPID_EVIDENCE_SYNTHESIS.value
    return ArticleType.EMPIRICAL_STUDY.value


def backfill_v3_entries(papers: list[dict]) -> list[dict]:
    routed: list[dict] = []
    for paper in papers:
        verdict = str(paper.get("_benchmark_editorial_verdict", Decision.REVISE.value))
        routed.append(
            {
                **paper,
                "article_type": infer_v3_article_type(paper),
                "_style_tag": "terser_v3",
                "_benchmark_quality": "high" if verdict == Decision.ACCEPT.value else "medium",
                "_benchmark_expected_decision": verdict,
            }
        )
    return routed


def build_micro_test(papers: list[dict]) -> list[dict]:
    picked = [paper for paper in papers if paper["title"] in MICRO_TEST_TITLES]
    if len(picked) != len(MICRO_TEST_TITLES):
        missing = sorted(set(MICRO_TEST_TITLES) - {paper["title"] for paper in picked})
        raise ValueError(f"missing_micro_titles:{missing}")
    return picked


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill article_type for elite_benchmark_v3")
    parser.add_argument("--input", required=True, help="Input v3 JSON path")
    parser.add_argument("--output", required=True, help="Output routed corpus path")
    parser.add_argument("--micro-output", required=True, help="Output routed micro-test path")
    args = parser.parse_args()

    papers = json.loads(Path(args.input).read_text())
    if not isinstance(papers, list):
        raise ValueError("input_must_be_list")

    routed = backfill_v3_entries(papers)
    Path(args.output).write_text(json.dumps(routed, indent=2) + "\n")
    Path(args.micro_output).write_text(json.dumps(build_micro_test(routed), indent=2) + "\n")


if __name__ == "__main__":
    main()
