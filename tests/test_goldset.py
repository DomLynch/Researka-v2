from __future__ import annotations

import json

from contracts import ArticleType, Decision, GoldSetCorpus, GoldSetEntry, GoldSetExpectation, ProviderUsage, SubmissionPayload
from scripts.build_gold_set_v1 import build_gold_set
from runtime_core.goldset import evaluate_gold_set, load_gold_set, render_gold_set_report
from runtime_core.providers import ProviderRequest, ProviderResponse, ProviderResult
from runtime_core.workflow import WorkflowEngine


def _empirical_sections() -> dict[str, str]:
    return {
        "Research Question": "This empirical study asks whether a bounded intervention changes a measurable outcome in a defined population, and it specifies the comparison frame, endpoint logic, measurement window, and exclusion boundaries clearly enough that another reviewer could reproduce the study question without silently broadening the claim or swapping the relevant evidence unit.",
        "Methods": "The methods section describes the cohort, intervention assignment, outcome definitions, statistical tests, missing-data handling, and exclusion rules in enough detail that an external reviewer can audit whether the design matches the question, whether the reported estimates are comparable across groups, and whether obvious confounders remain uncontrolled.",
        "Results": "The results section reports the direction and magnitude of the primary outcome, notes uncertainty around subgroup estimates, distinguishes exploratory findings from prespecified endpoints, and avoids mixing descriptive observations with causal claims that the design cannot support on its own.",
        "Limitations": "The limitations section admits that the sample is narrow, the intervention window is short, and residual confounding may remain, which materially narrows the force of any downstream policy or deployment claim even if the top-line direction of effect looks encouraging.",
        "Conclusion": "The conclusion stays narrow: the study adds one bounded empirical signal and justifies follow-up work, but it does not claim universal benefit, mechanistic proof, or ready-to-deploy policy conclusions beyond the design actually reported here.",
    }


class RoutingProvider:
    provider = "reviewer-panel"
    model = "routing-model"
    enforces_accept_quorum = True

    def complete(self, request: ProviderRequest) -> ProviderResult:
        if "Empirical Study" in request.user_prompt and "empirical study reviewer" in request.system_prompt.lower():
            payload = {
                "recommendation": "accept",
                "rubric_scores": {
                    "research_question_quality": 5,
                    "synthesis_quality": 4,
                    "claim_evidence_alignment": 5,
                    "limitations_quality": 4,
                    "gaps_quality": 4,
                    "source_grounding": 5,
                },
                "major_issues": [],
                "minor_issues": [],
                "required_revisions": [],
                "claim_support_verdict": "supported",
                "overclaim_verdict": "none",
                "synthesis_quality_verdict": "strong",
                "review_markdown": "Empirical manuscript accepted.",
            }
        else:
            payload = {
                "recommendation": "revise",
                "rubric_scores": {
                    "research_question_quality": 4,
                    "synthesis_quality": 3,
                    "claim_evidence_alignment": 3,
                    "limitations_quality": 4,
                    "gaps_quality": 4,
                    "source_grounding": 3,
                },
                "major_issues": ["Needs tighter evidence alignment."],
                "minor_issues": [],
                "required_revisions": ["Tighten claims."],
                "claim_support_verdict": "partially_supported",
                "overclaim_verdict": "mild",
                "synthesis_quality_verdict": "weak",
                "review_markdown": "Revise.",
            }
        return ProviderResult(
            ok=True,
            response=ProviderResponse(
                text=json.dumps(payload),
                provider="reviewer-panel",
                model="routing-model",
                usage=ProviderUsage(input_tokens=12, output_tokens=8, cost_usd=0.05),
                metadata={"accept_quorum_count": 2, "accept_quorum_models": ["routing-a", "routing-b"]},
            ),
        )


def test_load_gold_set_accepts_list_format(tmp_path) -> None:
    path = tmp_path / "gold.json"
    path.write_text(json.dumps([{
        "entry_id": "g-1",
        "article_type": "rapid_evidence_synthesis",
        "submission": {
            "title": "T",
            "abstract": "A",
            "sections": {},
            "source_bundle": [],
            "author_agent_id": "agent-1",
        },
        "expected": {"decision": "reject"},
    }]))
    corpus = load_gold_set(str(path))
    assert corpus.version == "gold-set-v1"
    assert len(corpus.entries) == 1


def test_build_gold_set_emits_expected_working_mix() -> None:
    corpus = build_gold_set()
    entries = corpus["entries"]
    assert corpus["version"] == "gold-set-v1-working"
    assert len(entries) == 30
    article_types = {entry["article_type"] for entry in entries}
    assert article_types == {"rapid_evidence_synthesis", "empirical_study"}
    assert sum(1 for entry in entries if "real-draft" in entry.get("tags", [])) == 10
    assert sum(1 for entry in entries if entry["article_type"] == "empirical_study") == 8


def test_evaluate_gold_set_scores_article_types_and_accept_blockers() -> None:
    corpus = GoldSetCorpus(
        entries=[
            GoldSetEntry(
                entry_id="emp-1",
                article_type=ArticleType.EMPIRICAL_STUDY,
                submission=SubmissionPayload(
                    title="Empirical Study: bounded cohort signal",
                    abstract="Bounded empirical study.",
                    sections=_empirical_sections(),
                    source_bundle=[
                        {
                            "title": f"Source {i}",
                            "doi": f"10.1234/gold.emp.{i}",
                            "evidence_type": "primary",
                            "year": 2025,
                            "excerpt": "The empirical source reports a bounded measured outcome in the defined population.",
                        }
                        for i in range(12)
                    ],
                    author_agent_id="gold-agent",
                    article_type=ArticleType.EMPIRICAL_STUDY,
                ),
                expected=GoldSetExpectation(
                    decision=Decision.ACCEPT,
                    rubric_scores={
                        "research_question_quality": 5,
                        "synthesis_quality": 4,
                        "claim_evidence_alignment": 5,
                        "limitations_quality": 4,
                        "gaps_quality": 4,
                        "source_grounding": 5,
                    },
                    claim_support_verdict="supported",
                    overclaim_verdict="none",
                    synthesis_quality_verdict="strong",
                ),
            ),
            GoldSetEntry(
                entry_id="res-1",
                article_type=ArticleType.RAPID_EVIDENCE_SYNTHESIS,
                submission=SubmissionPayload(
                    title="Rapid Evidence Synthesis: weak synthesis",
                    abstract="Bounded synthesis.",
                    sections={
                        "Research Question": "This synthesis asks a bounded question about an intervention, target population, comparator, outcome frame, inclusion logic, and publication window clearly enough that a downstream reviewer can reproduce scope without inventing missing assumptions, silently changing the relevant evidence class, or broadening the decision frame beyond what the retained sources can plausibly support.",
                        "Search Summary": "The search summary lists the databases, date window, inclusion logic, and narrowing rule, but it still leaves too much ambiguity about what contradictory evidence was excluded, which search branches were abandoned, and why the final bundle should anchor a strong conclusion instead of a more cautious revise-style synthesis.",
                        "Evidence Landscape": "The landscape references a mix of reviews and primary studies but mostly restates what the bundle contains instead of synthesizing why the stronger sources outweigh the weaker signals, how much of the conclusion rests on indirect evidence, and where the remaining uncertainty actually sits.",
                        "Key Findings": "The findings summarize multiple studies but lean too hard on directionally positive signals, compress uncertainty too aggressively, and do not adequately bound the conclusion to the retained evidence, creating a revise-shaped manuscript rather than a clean accept under the current Researka contract.",
                        "Limitations": "The limitations note heterogeneity, sparse direct human evidence, and uncertainty around transferability, but they still leave enough slack that a reviewer could reasonably ask for sharper constraint language before treating the synthesis as publication-ready and fully supported by the cited bundle.",
                        "Gaps Identified": "The gaps are real but generic, and they do not fully isolate the most decision-relevant missing evidence that would most improve confidence in the thesis, especially around counterevidence, external validity, and outcome measurement consistency across the bundle.",
                        "Conclusion": "The conclusion remains directionally useful but still overreaches slightly relative to the cited bundle, which is why this submission should trigger a revise decision rather than an accept even though the overall structure is complete and the synthesis is not substantively broken.",
                    },
                    source_bundle=[
                        {
                            "title": f"Review {i}",
                            "doi": f"10.1234/gold.res.{i}",
                            "evidence_type": "review",
                            "year": 2025,
                            "excerpt": "The review reports a bounded synthesis signal with explicit uncertainty.",
                        }
                        for i in range(12)
                    ],
                    author_agent_id="gold-agent",
                ),
                expected=GoldSetExpectation(
                    decision=Decision.ACCEPT,
                    rubric_scores={
                        "research_question_quality": 4,
                        "synthesis_quality": 4,
                        "claim_evidence_alignment": 4,
                        "limitations_quality": 4,
                        "gaps_quality": 4,
                        "source_grounding": 4,
                    },
                    claim_support_verdict="supported",
                    overclaim_verdict="none",
                    synthesis_quality_verdict="adequate",
                ),
            ),
        ]
    )

    artifact = evaluate_gold_set(corpus, engine=WorkflowEngine(provider=RoutingProvider()))

    assert artifact["summary"]["total"] == 2
    assert artifact["summary"]["correct"] == 1
    assert artifact["summary"]["accuracy"] == 0.5
    assert artifact["summary"]["by_article_type"]["empirical_study"]["accuracy"] == 1.0
    assert artifact["summary"]["by_article_type"]["rapid_evidence_synthesis"]["accuracy"] == 0.0
    assert artifact["summary"]["accept_blockers"]["claim_support_verdict"] == 1
    assert artifact["summary"]["confusion_matrix"]["accept"]["accept"] == 1
    assert artifact["summary"]["mismatch_count"] == 1


def test_evaluate_gold_set_progress_callback_receives_partial_artifact() -> None:
    corpus = GoldSetCorpus(
        entries=[
            GoldSetEntry(
                entry_id="emp-1",
                article_type=ArticleType.EMPIRICAL_STUDY,
                submission=SubmissionPayload(
                    title="Empirical Study: bounded cohort signal",
                    abstract="Bounded empirical study.",
                    sections=_empirical_sections(),
                    source_bundle=[
                        {"title": f"Source {i}", "doi": f"10.1234/gold.progress1.{i}", "evidence_type": "primary", "year": 2025}
                        for i in range(12)
                    ],
                    author_agent_id="gold-agent",
                    article_type=ArticleType.EMPIRICAL_STUDY,
                ),
                expected=GoldSetExpectation(decision=Decision.ACCEPT),
            ),
            GoldSetEntry(
                entry_id="emp-2",
                article_type=ArticleType.EMPIRICAL_STUDY,
                submission=SubmissionPayload(
                    title="Empirical Study: bounded cohort signal 2",
                    abstract="Bounded empirical study.",
                    sections=_empirical_sections(),
                    source_bundle=[
                        {"title": f"Source {i}", "doi": f"10.1234/gold.progress2.{i}", "evidence_type": "primary", "year": 2025}
                        for i in range(12)
                    ],
                    author_agent_id="gold-agent",
                    article_type=ArticleType.EMPIRICAL_STUDY,
                ),
                expected=GoldSetExpectation(decision=Decision.ACCEPT),
            ),
        ]
    )
    snapshots: list[tuple[int, int, str, int]] = []

    def _progress(index: int, total: int, record: dict, artifact: dict) -> None:
        snapshots.append((index, total, record["entry_id"], artifact["summary"]["total"]))

    artifact = evaluate_gold_set(corpus, engine=WorkflowEngine(provider=RoutingProvider()), progress_callback=_progress)

    assert artifact["summary"]["total"] == 2
    assert snapshots == [(1, 2, "emp-1", 1), (2, 2, "emp-2", 2)]


def test_render_gold_set_report_contains_core_sections() -> None:
    artifact = {
        "run_meta": {
            "timestamp": "2026-04-21T00:00:00Z",
            "corpus_version": "gold-set-v1-working",
            "provider": "reviewer-panel",
            "model": "mimo|openrouter",
        },
        "summary": {
            "total": 2,
            "correct": 1,
            "accuracy": 0.5,
            "mismatch_count": 1,
            "by_article_type": {
                "empirical_study": {"count": 1, "correct": 1, "accuracy": 1.0},
                "rapid_evidence_synthesis": {"count": 1, "correct": 0, "accuracy": 0.0},
            },
            "accept_blockers": {"claim_support_verdict": 1},
            "mismatches": [
                {
                    "entry_id": "res-1",
                    "article_type": "rapid_evidence_synthesis",
                    "expected": "accept",
                    "actual": "revise",
                    "error": None,
                }
            ],
        },
    }

    report = render_gold_set_report(artifact)

    assert "# Gold Set Evaluation Report" in report
    assert "gold-set-v1-working" in report
    assert "| empirical_study | 1 | 1 | 100.0% |" in report
    assert "| claim_support_verdict | 1 |" in report
    assert "| res-1 | rapid_evidence_synthesis | accept | revise |  |" in report
