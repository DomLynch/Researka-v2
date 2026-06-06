import json
import urllib.error

import pytest

from runtime_core.compiler import canonical_bundle_facts, compile_publication
from runtime_core.gates import run_publish_gates
from runtime_core.failure_classifier import classify_failure_reason
from runtime_core.prompts import REVIEWER_PROMPT_VERSION
from runtime_core.providers import FallbackProvider, MimoProvider, OpenRouterProvider, ProviderRequest, ProviderResponse, ProviderResult
from runtime_core.reviewer_panel import ReviewerPanel, reviewer_from_env
from runtime_core.repos import InMemoryRuntimeRepository
from runtime_core.sanitizer import sanitize_source_ledger
from runtime_core.workflow import (
    WorkflowEngine,
)

from contracts import ArticleType, Decision, FailureClass, ObjectType, ProviderUsage, ResearchObject, RuntimeJob, Stage, SubmissionPayload, WorkflowContext, run_submission_template_checks


def _full_sections(
    search_summary: str = "Legit retained summary line describing the databases, publication window, inclusion logic, and narrowing rule in enough detail to satisfy the structure gate."
) -> dict[str, str]:
    return {
        "Research Question": "This synthesis asks a bounded, decision-relevant question about recent evidence, target populations, comparator conditions, intended outcomes, and methodological limits, and it stays narrow enough that another reviewer could reproduce the scope, publication window, inclusion logic, and decision frame without inventing missing assumptions, broadening the target claim, or silently swapping the relevant evidence category.",
        "Search Summary": search_summary,
        "Evidence Landscape": "The bundle includes both review-level and primary evidence so the reader can see the balance of stronger and more applied material, how much of the synthesis rests on reviews, and where individual studies still shape the remaining uncertainty.",
        "Key Findings": "The key findings integrate the current evidence into bounded conclusions instead of stitching raw snippets together, and they distinguish between stronger review-level support, tentative primary-study signals, and areas where conflicting evidence should temper confidence.",
        "Limitations": "The main limits are rapid-review scope, incomplete coverage, heterogeneity across evidence units, and the risk that a synthetic bundle omits conflicting sources that could materially change the certainty of any strong-sounding claim.",
        "Gaps Identified": "No adequately powered human RCT has tested this specific intervention for the primary endpoints reported in non-human models, leaving a translational gap between animal evidence and clinical applicability.",
        "Conclusion": "The current evidence supports a structured MVP publication, but only with explicit uncertainty, honest limits on reproducibility, and no overclaiming beyond what the retained bundle can directly justify.",
    }


def _valid_source_bundle() -> list[dict[str, object]]:
    years = (2024, 2023, 2022, 2021, 2020, 2024, 2023, 2022, 2021, 2019, 2018, 2017)
    evidence_types = ("review",) * 6 + ("primary",) * 6
    return [
        {
            "title": f"{evidence_type.title()} source {index}",
            "year": year,
            "evidence_type": evidence_type,
        }
        for index, (year, evidence_type) in enumerate(zip(years, evidence_types, strict=True), start=1)
    ]


def _review_payload(
    recommendation: str = "accept",
    **overrides: object,
) -> dict[str, object]:
    if recommendation == "accept":
        payload: dict[str, object] = {
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
            "review_markdown": "Acceptable submission.",
        }
    elif recommendation == "revise":
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
            "major_issues": ["Key findings need stronger synthesis."],
            "minor_issues": [],
            "required_revisions": ["Tighten the conclusion and evidence alignment."],
            "claim_support_verdict": "partially_supported",
            "overclaim_verdict": "mild",
            "synthesis_quality_verdict": "weak",
            "review_markdown": "Revise before acceptance.",
        }
    else:
        payload = {
            "recommendation": "reject",
            "rubric_scores": {
                "research_question_quality": 2,
                "synthesis_quality": 1,
                "claim_evidence_alignment": 1,
                "limitations_quality": 2,
                "gaps_quality": 2,
                "source_grounding": 1,
            },
            "major_issues": ["Submission is substantively empty."],
            "minor_issues": [],
            "required_revisions": ["Full rewrite required."],
            "claim_support_verdict": "unsupported",
            "overclaim_verdict": "significant",
            "synthesis_quality_verdict": "empty",
            "review_markdown": "Reject.",
        }
    payload.update(overrides)
    return payload


def test_revise_is_terminal_for_external_author() -> None:
    engine = WorkflowEngine()
    context = WorkflowContext(
        target_object_id="obj-1",
        domain_slug="longevity",
    )
    outcome = engine.plan_from_editorial(context, Decision.REVISE)
    assert outcome.terminal_decision == Decision.REVISE
    assert outcome.next_jobs == []


def test_accept_queues_publish_job() -> None:
    engine = WorkflowEngine()
    context = WorkflowContext(target_object_id="obj-2", domain_slug="longevity")
    outcome = engine.plan_from_editorial(context, Decision.ACCEPT)
    assert outcome.terminal_decision == Decision.ACCEPT
    assert len(outcome.next_jobs) == 1
    assert outcome.next_jobs[0].stage == Stage.PUBLISH


def test_publish_job_is_idempotent_per_target() -> None:
    repo = InMemoryRuntimeRepository()
    first = repo.enqueue_job(RuntimeJob(target_object_id="obj-3", stage=Stage.PUBLISH))
    second = repo.enqueue_job(RuntimeJob(target_object_id="obj-3", stage=Stage.PUBLISH))
    assert first.id == second.id
    assert len(repo.jobs) == 1


def test_publish_uses_submission_body_when_metadata_body_missing() -> None:
    body = "\n\n".join(
        [
            "# Full manuscript",
            "### Abstract\n\n" + " ".join(["abstract"] * 30),
            "### Methods\n\n" + " ".join(["methods"] * 30),
            "### Results\n\n" + " ".join(["results"] * 30),
            "### Limitations\n\n" + " ".join(["limitations"] * 30),
            "### Conclusion\n\n" + " ".join(["conclusion"] * 30),
            "### References\n\n- Example 2024. DOI: 10.1234/example.",
        ]
    )
    repo = InMemoryRuntimeRepository()
    submission = repo.create_object(ResearchObject(
        object_type=ObjectType.SUBMISSION,
        title="Accepted body-column manuscript",
        body_markdown=body,
        metadata={
            "abstract": "A structured abstract for a public research synthesis.",
            "article_type": ArticleType.RAPID_EVIDENCE_SYNTHESIS.value,
            "sections": _full_sections(),
            "source_bundle": _valid_source_bundle(),
            "core_claims_resolved": True,
        },
    ))

    result = WorkflowEngine()._run_publish(RuntimeJob(target_object_id=submission.id, stage=Stage.PUBLISH), repo)

    publication = repo.get_object(result["publication_id"])
    assert publication is not None
    assert publication.body_markdown.startswith("# Full manuscript")


def test_publish_preserves_submission_audit_hashes() -> None:
    repo = InMemoryRuntimeRepository()
    submission = repo.create_object(ResearchObject(
        object_type=ObjectType.SUBMISSION,
        title="Accepted hash-traced manuscript",
        body_markdown="# Full manuscript\n\nBody",
        metadata={
            "abstract": "A structured abstract for a public research synthesis.",
            "article_type": ArticleType.RAPID_EVIDENCE_SYNTHESIS.value,
            "sections": _full_sections(),
            "source_bundle": _valid_source_bundle(),
            "core_claims_resolved": True,
            "author_agent_id": "agent-v3-full-paper",
            "run_id": "synthesis-topic-v06-test",
            "content_hash": "sha256:paper",
            "submission_payload_hash": "sha256:payload",
            "source_citation_hash": "sha256:sources",
            "submission_identity_key": "sha256:identity",
            "author_signature": "sha256:paper",
        },
    ))

    result = WorkflowEngine()._run_publish(RuntimeJob(target_object_id=submission.id, stage=Stage.PUBLISH), repo)

    publication = repo.get_object(result["publication_id"])
    assert publication is not None
    assert publication.metadata["source_submission_id"] == submission.id
    assert publication.metadata["run_id"] == "synthesis-topic-v06-test"
    assert publication.metadata["content_hash"] == "sha256:paper"
    assert publication.metadata["submission_payload_hash"] == "sha256:payload"
    assert publication.metadata["source_citation_hash"] == "sha256:sources"
    assert publication.metadata["submission_identity_key"] == "sha256:identity"


def test_publish_dedupes_by_submission_identity_key() -> None:
    repo = InMemoryRuntimeRepository()
    existing = repo.create_object(ResearchObject(
        object_type=ObjectType.PUBLICATION,
        title="Earlier public title",
        metadata={"submission_identity_key": "sha256:identity"},
    ))
    submission = repo.create_object(ResearchObject(
        object_type=ObjectType.SUBMISSION,
        title="Retitled public manuscript",
        metadata={
            "abstract": "A structured abstract for a public research synthesis.",
            "article_type": ArticleType.RAPID_EVIDENCE_SYNTHESIS.value,
            "sections": _full_sections(),
            "source_bundle": _valid_source_bundle(),
            "core_claims_resolved": True,
            "submission_identity_key": "sha256:identity",
        },
    ))

    result = WorkflowEngine()._run_publish(RuntimeJob(target_object_id=submission.id, stage=Stage.PUBLISH), repo)

    assert result == {"publication_id": existing.id, "deduped": True}


def test_reject_is_terminal() -> None:
    engine = WorkflowEngine()
    context = WorkflowContext(target_object_id="obj-4", domain_slug="longevity")
    outcome = engine.plan_from_editorial(context, Decision.REJECT)
    assert outcome.terminal_decision == Decision.REJECT
    assert outcome.next_jobs == []


def test_compile_publication_strips_leakage_lines() -> None:
    artifact = compile_publication(
        title="Test",
        abstract="A",
        sections=_full_sections(
            search_summary=(
                "The Search Summary is incomplete\n"
                "Legit retained summary line describing the databases searched, the publication window, the inclusion logic, and the narrowing rule that kept only the most objective-aligned receipts."
            )
        ),
        source_bundle=[{"evidence_type": "review", "year": 2024}],
    )
    assert "the search summary is incomplete" not in artifact.body_markdown.lower()
    assert "Legit retained summary line" in artifact.body_markdown


def test_compile_publication_supports_empirical_study_sections() -> None:
    artifact = compile_publication(
        title="Empirical manuscript",
        abstract="A",
        article_type=ArticleType.EMPIRICAL_STUDY.value,
        sections={
            "Research Question": "This empirical study asks whether a bounded intervention changes a measurable outcome in a defined population, and it specifies the comparison frame, endpoint logic, and exclusion boundaries clearly enough that another reviewer could reproduce the intended question without silently broadening the claim.",
            "Methods": "The methods section documents the cohort, intervention assignment, outcome definitions, exclusion rules, and analytic plan in enough detail that a reviewer can audit whether the reported estimates actually answer the stated question and whether obvious confounders remain unresolved.",
            "Results": "The results section reports the primary outcome, notes uncertainty around exploratory subgroup estimates, distinguishes descriptive observations from stronger inferential claims, and avoids pretending that a single dataset proves general benefit across every context that might matter downstream.",
            "Limitations": "The limitations section is honest about sample narrowness, short follow-up, and residual confounding, which materially constrains the force of any extrapolation even if the top-line direction of effect looks directionally favorable.",
            "Conclusion": "The conclusion stays narrow by claiming only that the study contributes one bounded empirical signal and justifies follow-up work, not that it proves universal efficacy or policy readiness.",
        },
        source_bundle=_valid_source_bundle(),
    )
    assert "## Methods" in artifact.body_markdown
    assert "## Results" in artifact.body_markdown


def test_alpha_memo_agent_artifact_uses_lightweight_intake_contract() -> None:
    payload = SubmissionPayload(
        title="Storage reserves flip after threshold pricing",
        artifact_type="alpha_memo",
        author_agent_id="agent-v4-alpha-memo",
        markdown="# Alpha memo\n\nA bounded evidence-backed signal with clear limits.",
        evidence_bundle={
            "publish_verdict": {
                "axes": {
                    "source_papers": [
                        {"doi": f"10.1000/alpha-{index}", "title": f"Reserve threshold paper {index}"}
                        for index in range(1, 6)
                    ],
                },
            },
        },
    )

    assert payload.article_type == ArticleType.ALPHA_MEMO
    assert payload.body_markdown == payload.markdown
    assert payload.abstract == "Alpha memo"
    assert payload.source_bundle == [
        {
            "title": f"Reserve threshold paper {index}",
            "doi": f"10.1000/alpha-{index}",
            "url": None,
            "year": None,
            "evidence_type": "primary",
        }
        for index in range(1, 6)
    ]

    alpha_failures = [
        gate.name
        for gate in run_submission_template_checks(
            sections=payload.sections,
            source_bundle=payload.source_bundle,
            article_type=payload.article_type.value,
        )
        if not gate.passed
    ]
    rapid_failures = [
        gate.name
        for gate in run_submission_template_checks(
            sections=payload.sections,
            source_bundle=payload.source_bundle,
            article_type=ArticleType.RAPID_EVIDENCE_SYNTHESIS.value,
        )
        if not gate.passed
    ]

    assert alpha_failures == []
    assert "minimum_citations" in rapid_failures

    artifact = compile_publication(
        title=payload.title,
        abstract=payload.abstract,
        body_markdown=payload.body_markdown,
        sections=payload.sections,
        source_bundle=payload.source_bundle,
        article_type=payload.article_type.value,
    )
    assert artifact.body_markdown.startswith("# Alpha memo")


def test_alpha_memo_with_four_sources_fails_public_intake_gate() -> None:
    results = run_submission_template_checks(
        sections={},
        source_bundle=[
            {
                "title": f"Narrow alpha source {index}",
                "doi": f"10.1000/narrow-{index}",
                "evidence_type": "primary",
            }
            for index in range(1, 5)
        ],
        article_type=ArticleType.ALPHA_MEMO.value,
    )
    failures = {gate.name for gate in results if not gate.passed}
    minimum_citations = next(gate for gate in results if gate.name == "minimum_citations")

    assert failures == {"minimum_citations"}
    assert "at least 5 citations" in minimum_citations.reason


def test_alpha_memo_with_four_sources_can_use_structural_exception() -> None:
    bundle = [
        {
            "title": f"Narrow alpha source {index}",
            "doi": f"10.1000/narrow-{index}",
            "evidence_type": "primary",
        }
        for index in range(1, 5)
    ]
    results = run_submission_template_checks(
        sections={},
        source_bundle=bundle,
        article_type=ArticleType.ALPHA_MEMO.value,
        evidence_bundle={
            "publish_verdict": {
                "decision": "ready_to_publish",
                "publish_tier": "TIER_1",
                "maturity_level": "L5",
                "confidence_label": "evidence_backed_signal",
                "axes": {
                    "bound_receipts": 2,
                    "a_core_receipts": 2,
                    "source_papers": bundle,
                },
            },
        },
    )

    failures = {gate.name for gate in results if not gate.passed}

    assert "minimum_citations" not in failures


def test_alpha_memo_single_source_cannot_use_structural_exception() -> None:
    results = run_submission_template_checks(
        sections={},
        source_bundle=[{"title": "Single source", "doi": "10.1000/one", "evidence_type": "primary"}],
        article_type=ArticleType.ALPHA_MEMO.value,
        evidence_bundle={
            "publish_verdict": {
                "decision": "ready_to_publish",
                "publish_tier": "TIER_1",
                "maturity_level": "L5",
                "confidence_label": "evidence_backed_signal",
                "axes": {"bound_receipts": 4, "a_core_receipts": 4},
            },
        },
    )

    assert "minimum_citations" in {gate.name for gate in results if not gate.passed}


def test_compile_publication_preserves_full_manuscript_references() -> None:
    full_body = "\n\n".join(
        [
            "# Full manuscript",
            "## Abstract\n\nThis abstract is long enough to satisfy the public full manuscript structure gate and summarize the accepted research artifact.",
            "## Methods\n\nThe methods describe source retrieval, screening, extraction, appraisal, synthesis, and verification in enough detail to audit the accepted manuscript.",
            "## Results\n\nThe results present outcome-specific evidence, directness, limitations, and traceable findings without reducing the publication to a section-map summary.",
            "## Limitations\n\nThe limitations identify corpus boundaries, uncertainty, scope restrictions, missing endpoints, and interpretation risks that constrain public claims.",
            "## Conclusion\n\nThe conclusion states the bounded finding and preserves the distinction between supported claims, unresolved gaps, and future research needs.",
            "## References\n\n- Example 2024. DOI: 10.1234/example. PMID: 12345678.",
        ]
    )
    artifact = compile_publication(
        title="Full manuscript",
        abstract="A",
        body_markdown=full_body,
        sections=_full_sections(),
        source_bundle=_valid_source_bundle(),
    )
    assert artifact.body_markdown.strip() == full_body
    assert "## References" in artifact.body_markdown
    assert "DOI: 10.1234/example" in artifact.body_markdown


def test_canonical_bundle_facts_reconcile_counts() -> None:
    counts = canonical_bundle_facts(
        [
            {"evidence_type": "review", "year": 2024},
            {"evidence_type": "primary", "year": 2023},
        ]
    )
    results = run_publish_gates(body_markdown="Clean body", counts=counts, core_claims_resolved=True)
    reconciliation = next(result for result in results if result.name == "count_reconciliation")
    assert reconciliation.passed is True


def test_unresolved_core_claims_fail_publish_gate() -> None:
    counts = canonical_bundle_facts([{"evidence_type": "review", "year": 2024}])
    results = run_publish_gates(body_markdown="Clean body", counts=counts, core_claims_resolved=False)
    claim_gate = next(result for result in results if result.name == "core_claims_resolved")
    assert claim_gate.passed is False


def test_workflow_uses_provider_contract_for_trace_metadata() -> None:
    class StubProvider:
        def complete(self, request: ProviderRequest) -> ProviderResult:
            return ProviderResult(
                ok=True,
                response=ProviderResponse(
                    text=json.dumps(
                        {
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
                            "review_markdown": "The submission is structurally complete and sufficiently grounded for an MVP acceptance decision.",
                        }
                    ),
                    provider="stub-provider",
                    model="stub-model",
                    usage=ProviderUsage(input_tokens=11, output_tokens=7, cost_usd=0.42),
                ),
            )

    repo = InMemoryRuntimeRepository()
    submission = repo.create_object(
        ResearchObject(
            object_type="submission",
            title="Rapid Evidence Synthesis: senescence",
            metadata={
                "domain_slug": "longevity",
                "title": "Rapid Evidence Synthesis: senescence",
                "abstract": "Bounded external submission.",
                "sections": _full_sections(
                    "Databases searched include PubMed and review corpora, with a documented date window, explicit inclusion logic, and a clear narrowing rule that explains why the retained receipts best match the scoped research question."
                ),
                "source_bundle": _valid_source_bundle(),
                "core_claims_resolved": True,
                "author_agent_id": "agent-demo",
            },
        )
    )
    engine = WorkflowEngine(provider=StubProvider())
    engine.handle_job(
        RuntimeJob(
            target_object_id=submission.id,
            stage=Stage.INTAKE,
            payload={"domain_slug": "longevity"},
        ),
        repo,
    )
    review_job = repo.queued_jobs()[0]
    engine.handle_job(review_job, repo)
    review = repo.list_objects("review")[0]
    assert review.metadata["prompt_version"] == REVIEWER_PROMPT_VERSION
    assert review.metadata["provider"] == "stub-provider"
    assert review.metadata["model"] == "stub-model"
    assert review.metadata["tokens_in"] == 11
    assert review.metadata["tokens_out"] == 7
    assert review.metadata["cost_usd"] == 0.42


def test_reviewer_prompt_keeps_triage_and_decision_contract_visible() -> None:
    prompt = WorkflowEngine()._review_system_prompt(ArticleType.RAPID_EVIDENCE_SYNTHESIS.value)
    assert "rapid evidence synthesis reviewer" in prompt.lower()
    assert "forced triage call" in prompt
    assert "Do not use revise as a safe default" in prompt
    assert "mixed findings, sparse human data" in prompt
    assert "Judge substance, not house style" in prompt
    assert "House-style revise" in prompt
    assert "Terser-style accept" in prompt
    assert "External-style accept" in prompt
    assert "accept = all scores >= 4" in prompt
    assert "Accept is invalid when the manuscript explicitly says evidence is mixed" in prompt
    assert "required_revisions lists concrete fixes" in prompt
    assert "Do not label accept-quality papers as revise for minor wording polish only" in prompt
    assert "reject = structurally broken" in prompt
    empirical_prompt = WorkflowEngine()._review_system_prompt(ArticleType.EMPIRICAL_STUDY.value)
    assert "empirical study reviewer" in empirical_prompt.lower()
    assert "Empirical-study calibration rules" in empirical_prompt
    assert "one primary study rather than a multi-study synthesis" in empirical_prompt
    assert "stand-ins for methods and results context" in empirical_prompt


def test_workflow_marks_empirical_study_in_review_metadata() -> None:
    class EmpiricalProvider:
        def complete(self, request: ProviderRequest) -> ProviderResult:
            assert "empirical study reviewer" in request.system_prompt.lower()
            return ProviderResult(
                ok=True,
                response=ProviderResponse(
                    text=json.dumps(_review_payload("accept", review_markdown="Empirical manuscript accepted.")),
                    provider="stub-provider",
                    model="stub-model",
                    usage=ProviderUsage(input_tokens=9, output_tokens=6, cost_usd=0.21),
                ),
            )

    repo = InMemoryRuntimeRepository()
    submission = repo.create_object(
        ResearchObject(
            object_type="submission",
            title="Empirical Study: bounded cohort",
            metadata={
                "article_type": ArticleType.EMPIRICAL_STUDY.value,
                "domain_slug": "longevity",
                "title": "Empirical Study: bounded cohort",
                "abstract": "Bounded empirical submission.",
                "sections": {
                    "Research Question": "This empirical study asks whether a bounded intervention changes a measurable outcome in a defined population, and it specifies the comparison frame, endpoint logic, measurement window, exclusion boundaries, and uncertainty tolerance clearly enough that another reviewer could reproduce the intended question without silently broadening the claim or swapping the relevant evidence unit.",
                    "Methods": "The methods section documents the cohort, intervention assignment, outcome definitions, exclusion rules, and analytic plan in enough detail that a reviewer can audit whether the reported estimates actually answer the stated question and whether obvious confounders remain unresolved.",
                    "Results": "The results section reports the primary outcome, notes uncertainty around exploratory subgroup estimates, distinguishes descriptive observations from stronger inferential claims, and avoids pretending that a single dataset proves general benefit across every context that might matter downstream.",
                    "Limitations": "The limitations section is honest about sample narrowness, short follow-up, and residual confounding, which materially constrains the force of any extrapolation even if the top-line direction of effect looks directionally favorable.",
                    "Conclusion": "The conclusion stays narrow by claiming only that the study contributes one bounded empirical signal and justifies follow-up work, not that it proves universal efficacy or policy readiness.",
                },
                "source_bundle": _valid_source_bundle(),
                "core_claims_resolved": True,
                "author_agent_id": "agent-demo",
            },
        )
    )
    engine = WorkflowEngine(provider=EmpiricalProvider())
    engine.handle_job(RuntimeJob(target_object_id=submission.id, stage=Stage.INTAKE, payload={}), repo)
    review_job = repo.queued_jobs()[0]
    engine.handle_job(review_job, repo)
    review = repo.children_of(submission.id, "review")[0]
    assert review.metadata["article_type"] == ArticleType.EMPIRICAL_STUDY.value


def test_sanitize_source_ledger_drops_polluted_revision_hints() -> None:
    ledger, actions = sanitize_source_ledger(
        {
            "primary_query": "cellular senescence mitochondrial dysfunction reviews",
            "revision_hints": [
                "The Search Summary is incomplete and lacks reproducibility.",
                "Keep the retained hint about explicit date windows and databases searched.",
                "Deterministic review records show the proposal does not explain selection.",
            ],
        }
    )
    assert ledger["revision_hints"] == ["Keep the retained hint about explicit date windows and databases searched."]
    assert actions == ["revision_hints:dropped_2_polluted_entries"]


def test_compile_publication_blocks_real_v1_leakage_samples() -> None:
    artifact = compile_publication(
        title="Leakage test",
        abstract="A",
        sections=_full_sections(
            search_summary="\n".join(
                    [
                        "The Search Summary is incomplete and lacks reproducibility.",
                        "Deterministic review records show the proposal does not explain the selection method.",
                        "Leaves semantic support for LLM review.",
                        "Legit retained line describing the databases, publication window, and inclusion logic in enough detail to survive structure gating.",
                    ]
                )
            ),
        source_bundle=[{"evidence_type": "review", "year": 2024}],
    )
    body = artifact.body_markdown.lower()
    assert "search summary is incomplete" not in body
    assert "deterministic review records" not in body
    assert "leaves semantic support for llm review" not in body
    assert "legit retained line describing the databases" in body


def test_structure_gate_rejects_missing_required_sections() -> None:
    with pytest.raises(ValueError, match="structure_gate"):
        compile_publication(
            title="Thin",
            abstract="A",
            sections={"Research Question": "Too short"},
            source_bundle=[{"evidence_type": "review", "year": 2024}],
        )


def test_full_manuscript_structure_accepts_demoted_heading_levels() -> None:
    body = "\n\n".join(
        [
            "# Full manuscript",
            "### Abstract\n\n" + " ".join(["abstract"] * 30),
            "### Methods\n\n" + " ".join(["methods"] * 30),
            "### Results\n\n" + " ".join(["results"] * 30),
            "### Limitations\n\n" + " ".join(["limitations"] * 30),
            "### Conclusion\n\n" + " ".join(["conclusion"] * 30),
            "### References\n\n- Example 2024. DOI: 10.1234/example.",
        ]
    )

    artifact = compile_publication(
        title="Demoted manuscript",
        abstract="A structured abstract for a public research synthesis.",
        sections={},
        source_bundle=[{"evidence_type": "primary", "year": 2024}],
        body_markdown=body,
        article_type="rapid_evidence_synthesis",
    )

    assert artifact.body_markdown.startswith("# Full manuscript")


def test_full_manuscript_structure_counts_nested_subsections() -> None:
    body = "\n\n".join(
        [
            "# Full manuscript",
            "### Abstract\n\n" + " ".join(["abstract"] * 30),
            "### Methods",
            "#### Review type\n\n" + " ".join(["methods"] * 30),
            "#### Search strategy\n\n" + " ".join(["search"] * 30),
            "### Results\n\n" + " ".join(["results"] * 30),
            "### Limitations\n\n" + " ".join(["limitations"] * 30),
            "### Conclusion\n\n" + " ".join(["conclusion"] * 30),
            "### References\n\n- Example 2024. DOI: 10.1234/example.",
        ]
    )

    artifact = compile_publication(
        title="Nested manuscript",
        abstract="A structured abstract for a public research synthesis.",
        sections={},
        source_bundle=[{"evidence_type": "primary", "year": 2024}],
        body_markdown=body,
        article_type="rapid_evidence_synthesis",
    )

    assert "#### Search strategy" in artifact.body_markdown


def test_failure_classifier_maps_structure_gate() -> None:
    assert classify_failure_reason("structure_gate: 'Conclusion' empty or placeholder-thin") == FailureClass.STRUCTURE_GATE


def test_openrouter_provider_retries_transient_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = {"count": 0}
    sleeps: list[float] = []

    class StubResponse:
        def __enter__(self) -> "StubResponse":
            return self

        def __exit__(self, exc_type, exc, tb) -> bool:
            return False

        def read(self) -> bytes:
            return b'{"choices":[{"message":{"content":"{\\"recommendation\\":\\"accept\\",\\"review_markdown\\":\\"ok\\"}"}}],"usage":{"prompt_tokens":12,"completion_tokens":8},"model":"google/gemma-4-31b-it"}'

    def fake_urlopen(*args, **kwargs):
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise TimeoutError("timed out")
        return StubResponse()

    monkeypatch.setattr("runtime_core.providers.time.sleep", lambda seconds: sleeps.append(seconds))
    monkeypatch.setattr("runtime_core.providers.urllib.request.urlopen", fake_urlopen)
    provider = OpenRouterProvider(api_key="test-key")
    result = provider.complete(
        ProviderRequest(
            system_prompt="system",
            user_prompt="user",
            prompt_version="reviewer-v1",
            response_format="json_object",
        )
    )
    assert result.ok is True
    assert attempts["count"] == 3
    # Two retries with exponential base 0.25s and 0.5s, ±25% jitter to avoid
    # thundering-herd retries when many jobs hit a transient outage at once.
    assert len(sleeps) == 2
    assert sleeps[0] == pytest.approx(0.25, abs=0.0625)
    assert sleeps[1] == pytest.approx(0.5, abs=0.125)


def test_openrouter_provider_retries_rate_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = {"count": 0}
    sleeps: list[float] = []

    class StubResponse:
        def __enter__(self) -> "StubResponse":
            return self

        def __exit__(self, exc_type, exc, tb) -> bool:
            return False

        def read(self) -> bytes:
            return b'{"choices":[{"message":{"content":"{\\"recommendation\\":\\"accept\\",\\"review_markdown\\":\\"ok\\"}"}}],"usage":{"prompt_tokens":12,"completion_tokens":8},"model":"google/gemma-4-31b-it"}'

    def fake_urlopen(*args, **kwargs):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise urllib.error.HTTPError("url", 429, "rate limit", {}, None)
        return StubResponse()

    monkeypatch.setattr("runtime_core.providers.time.sleep", lambda seconds: sleeps.append(seconds))
    monkeypatch.setattr("runtime_core.providers.urllib.request.urlopen", fake_urlopen)
    provider = OpenRouterProvider(api_key="test-key", model="google/gemma-4-31b-it")
    result = provider.complete(
        ProviderRequest(
            system_prompt="system",
            user_prompt="user",
            prompt_version="reviewer-v1",
            response_format="json_object",
        )
    )
    assert result.ok is True
    assert attempts["count"] == 2
    # Rate-limit backoff now uses a separate, larger base (default 2.0s) so a
    # 429 burst doesn't burn the same tiny budget as a transient timeout.
    # ±25% jitter applied.
    assert len(sleeps) == 1
    assert sleeps[0] == pytest.approx(2.0, abs=0.5)


def test_reviewer_panel_from_env_uses_mimo_gemma_mistral(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RESEARKA_V2_PROVIDER", "judge_panel")
    monkeypatch.delenv("RESEARKA_V2_REVIEWER_MODEL", raising=False)
    monkeypatch.delenv("RESEARKA_V2_JUDGE_MODEL", raising=False)
    # Default behaviour: per-slot Mistral fallback is enabled, so primary and
    # sparring are wrapped in FallbackProvider.
    monkeypatch.delenv("RESEARKA_V2_REVIEWER_FALLBACK_ENABLED", raising=False)

    provider = reviewer_from_env()

    assert isinstance(provider, ReviewerPanel)
    assert isinstance(provider.primary, FallbackProvider)
    assert isinstance(provider.sparring, FallbackProvider)
    assert isinstance(provider.fallback, OpenRouterProvider)
    # The wrapper exposes the primary inner model in its .model attribute.
    assert provider.primary.model == "mimo-v2.5-pro"
    assert provider.sparring.model == "google/gemma-4-31b-it"
    assert provider.fallback.model == "mistralai/mistral-small-2603"
    # Inner primaries must be the right concrete provider type.
    assert isinstance(provider.primary.primary, MimoProvider)
    assert isinstance(provider.sparring.primary, OpenRouterProvider)
    # The fallback inside each wrapper is Mistral via OpenRouter.
    assert provider.primary.fallback.model == "mistralai/mistral-small-2603"
    assert provider.sparring.fallback.model == "mistralai/mistral-small-2603"


def test_reviewer_panel_from_env_can_disable_per_slot_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Setting RESEARKA_V2_REVIEWER_FALLBACK_ENABLED=0 returns to the previous
    behaviour: bare MiMo and Gemma in the primary/sparring slots, no wrapping.
    Useful for measuring raw provider failure rates without the safety net."""
    monkeypatch.setenv("RESEARKA_V2_PROVIDER", "judge_panel")
    monkeypatch.setenv("RESEARKA_V2_REVIEWER_FALLBACK_ENABLED", "0")

    provider = reviewer_from_env()

    assert isinstance(provider, ReviewerPanel)
    assert isinstance(provider.primary, MimoProvider)
    assert isinstance(provider.sparring, OpenRouterProvider)


def test_editorial_requires_recommendation_metadata() -> None:
    repo = InMemoryRuntimeRepository()
    submission = repo.create_object(
        ResearchObject(
            object_type="submission",
            title="Missing recommendation",
            metadata={
                "domain_slug": "longevity",
                "abstract": "Bounded external submission.",
                "sections": _full_sections(),
                "source_bundle": _valid_source_bundle(),
                "core_claims_resolved": True,
            },
        )
    )
    review = repo.create_object(
        ResearchObject(
            object_type="review",
            parent_object_id=submission.id,
            title=f"Review for {submission.title}",
            body_markdown="Review exists but has no recommendation.",
            metadata={},
        )
    )
    engine = WorkflowEngine()
    with pytest.raises(ValueError, match="invalid_review_recommendation:missing"):
        engine.handle_job(
            RuntimeJob(
                target_object_id=submission.id,
                stage=Stage.EDITORIAL,
                payload={"review_id": review.id, "domain_slug": "longevity"},
            ),
            repo,
        )


def test_reviewer_panel_escalates_on_disagreement() -> None:
    class FakeProvider:
        def __init__(self, provider: str, model: str, recommendation: str) -> None:
            self.provider = provider
            self.model = model
            self.recommendation = recommendation

        def complete(self, request: ProviderRequest) -> ProviderResult:
            return ProviderResult(
                ok=True,
                response=ProviderResponse(
                    text=json.dumps(_review_payload(self.recommendation, review_markdown=f"{self.provider} says {self.recommendation}")),
                    provider=self.provider,
                    model=self.model,
                    usage=ProviderUsage(input_tokens=10, output_tokens=5, cost_usd=0.1),
                ),
            )

    panel = ReviewerPanel(
        primary=FakeProvider("mimo", "mimo-v2.5-pro", "accept"),
        sparring=FakeProvider("openrouter", "google/gemma-4-31b-it", "reject"),
        fallback=FakeProvider("openrouter", "mistralai/mistral-small-2603", "revise"),
    )
    result = panel.complete(
        ProviderRequest(
            system_prompt="system",
            user_prompt="user",
            prompt_version="reviewer-v1",
            response_format="json_object",
        )
    )
    assert result.ok is True
    assert result.response is not None
    assert '"recommendation": "revise"' in result.response.text
    assert result.response.provider == "reviewer-panel"
    assert result.response.metadata["route"] == "fallback_tiebreak"
    assert result.response.metadata["escalated_to_fallback"] is True
    assert result.response.metadata["primary_recommendation"] == "accept"
    assert result.response.metadata["sparring_recommendation"] == "reject"
    assert result.response.metadata["fallback_tiebreak_attempts"] == 1
    assert result.response.usage.cost_usd == 0.3


def test_reviewer_panel_retries_malformed_tiebreaker_once() -> None:
    class Provider:
        def __init__(self, provider: str, model: str, payloads: list[dict[str, object]]) -> None:
            self.provider = provider
            self.model = model
            self.payloads = payloads
            self.calls = 0

        def complete(self, request: ProviderRequest) -> ProviderResult:  # noqa: ARG002
            payload = self.payloads[min(self.calls, len(self.payloads) - 1)]
            self.calls += 1
            return ProviderResult(
                ok=True,
                response=ProviderResponse(
                    text=json.dumps(payload),
                    provider=self.provider,
                    model=self.model,
                    usage=ProviderUsage(input_tokens=10, output_tokens=5, cost_usd=0.1),
                ),
            )

    fallback = Provider(
        "openrouter",
        "mistralai/mistral-small-2603",
        [
            _review_payload("revise", review_markdown=""),
            _review_payload("revise", review_markdown="Fallback revises on retry."),
        ],
    )
    panel = ReviewerPanel(
        primary=Provider("mimo", "mimo-v2.5-pro", [_review_payload("accept")]),
        sparring=Provider("openrouter", "google/gemma-4-31b-it", [_review_payload("reject")]),
        fallback=fallback,
    )
    result = panel.complete(
        ProviderRequest(
            system_prompt="system",
            user_prompt="user",
            prompt_version="reviewer-v1",
            response_format="json_object",
        )
    )
    assert result.ok is True
    assert result.response is not None
    assert fallback.calls == 2
    assert '"recommendation": "revise"' in result.response.text
    assert result.response.metadata["route"] == "fallback_tiebreak"
    assert result.response.metadata["fallback_tiebreak_attempts"] == 2


def test_reviewer_panel_uses_conservative_valid_review_when_tiebreaker_stays_malformed() -> None:
    class Provider:
        def __init__(self, provider: str, model: str, payload: dict[str, object]) -> None:
            self.provider = provider
            self.model = model
            self.payload = payload
            self.calls = 0

        def complete(self, request: ProviderRequest) -> ProviderResult:  # noqa: ARG002
            self.calls += 1
            return ProviderResult(
                ok=True,
                response=ProviderResponse(
                    text=json.dumps(self.payload),
                    provider=self.provider,
                    model=self.model,
                    usage=ProviderUsage(input_tokens=10, output_tokens=5, cost_usd=0.1),
                ),
            )

    fallback = Provider("openrouter", "mistralai/mistral-small-2603", _review_payload("accept", review_markdown=""))
    panel = ReviewerPanel(
        primary=Provider("mimo", "mimo-v2.5-pro", _review_payload("accept")),
        sparring=Provider("openrouter", "google/gemma-4-31b-it", _review_payload("reject")),
        fallback=fallback,
    )
    result = panel.complete(
        ProviderRequest(
            system_prompt="system",
            user_prompt="user",
            prompt_version="reviewer-v1",
            response_format="json_object",
        )
    )
    assert result.ok is True
    assert result.response is not None
    assert fallback.calls == 2
    assert '"recommendation": "reject"' in result.response.text
    assert result.response.metadata["route"] == "fallback_tiebreak_failed_conservative"
    assert result.response.metadata["ops_flag"] == "fallback_tiebreak_failed_conservative"
    assert result.response.metadata["fallback_tiebreak_attempts"] == 2
    assert "missing_review_markdown" in str(result.response.metadata["fallback_error"])
    assert result.response.usage.cost_usd == 0.2


def test_reviewer_panel_treats_malformed_primary_as_failure() -> None:
    class BrokenProvider:
        def __init__(self, provider: str, model: str, text: str) -> None:
            self.provider = provider
            self.model = model
            self.text = text

        def complete(self, request: ProviderRequest) -> ProviderResult:
            return ProviderResult(
                ok=True,
                response=ProviderResponse(
                    text=self.text,
                    provider=self.provider,
                    model=self.model,
                    usage=ProviderUsage(input_tokens=10, output_tokens=5, cost_usd=0.1),
                ),
            )

    panel = ReviewerPanel(
        primary=BrokenProvider("mimo", "mimo-v2.5-pro", "<think>not json Think"),
        sparring=BrokenProvider(
            "mimo",
            "mimo-v2.5-pro",
            json.dumps(_review_payload("accept", review_markdown="Mimo accepts.")),
        ),
        fallback=BrokenProvider(
            "openrouter",
            "google/gemma-4-31b-it",
            json.dumps(_review_payload("revise", review_markdown="Fallback revises.")),
        ),
    )
    result = panel.complete(
        ProviderRequest(
            system_prompt="system",
            user_prompt="user",
            prompt_version="reviewer-v1",
            response_format="json_object",
        )
    )
    assert result.ok is True
    assert result.response is not None
    assert '"recommendation": "accept"' in result.response.text
    assert result.response.metadata["route"] == "primary_failed_sparring_used"
    assert result.response.metadata["ops_flag"] == "primary_failed"
    assert "invalid_panel_response" in str(result.response.metadata["primary_error"])


def test_reviewer_panel_treats_missing_review_markdown_as_failure() -> None:
    class BrokenProvider:
        def __init__(self, provider: str, model: str, payload: dict[str, object]) -> None:
            self.provider = provider
            self.model = model
            self.payload = payload

        def complete(self, request: ProviderRequest) -> ProviderResult:
            return ProviderResult(
                ok=True,
                response=ProviderResponse(
                    text=json.dumps(self.payload),
                    provider=self.provider,
                    model=self.model,
                    usage=ProviderUsage(input_tokens=10, output_tokens=5, cost_usd=0.1),
                ),
            )

    panel = ReviewerPanel(
        primary=BrokenProvider(
            "mimo",
            "mimo-v2.5-pro",
            _review_payload("accept", review_markdown=""),
        ),
        sparring=BrokenProvider(
            "mimo",
            "mimo-v2.5-pro",
            _review_payload("accept", review_markdown="Mimo accepts."),
        ),
        fallback=BrokenProvider(
            "openrouter",
            "google/gemma-4-31b-it",
            _review_payload("revise", review_markdown="Fallback revises."),
        ),
    )
    result = panel.complete(
        ProviderRequest(
            system_prompt="system",
            user_prompt="user",
            prompt_version="reviewer-v1",
            response_format="json_object",
        )
    )
    assert result.ok is True
    assert result.response is not None
    assert '"recommendation": "accept"' in result.response.text
    assert result.response.metadata["route"] == "primary_failed_sparring_used"
    assert "missing_review_markdown" in str(result.response.metadata["primary_error"])


def test_reviewer_panel_treats_weak_accept_contract_as_failure() -> None:
    class BrokenProvider:
        def __init__(self, provider: str, model: str, payload: dict[str, object]) -> None:
            self.provider = provider
            self.model = model
            self.payload = payload

        def complete(self, request: ProviderRequest) -> ProviderResult:
            return ProviderResult(
                ok=True,
                response=ProviderResponse(
                    text=json.dumps(self.payload),
                    provider=self.provider,
                    model=self.model,
                    usage=ProviderUsage(input_tokens=10, output_tokens=5, cost_usd=0.1),
                ),
            )

    panel = ReviewerPanel(
        primary=BrokenProvider(
            "mimo",
            "mimo-v2.5-pro",
            _review_payload(
                "accept",
                rubric_scores={
                    "research_question_quality": 4,
                    "synthesis_quality": 2,
                    "claim_evidence_alignment": 3,
                    "limitations_quality": 4,
                    "gaps_quality": 4,
                    "source_grounding": 3,
                },
                major_issues=["Weak synthesis."],
                required_revisions=["Rewrite key findings."],
                claim_support_verdict="partially_supported",
                overclaim_verdict="mild",
                synthesis_quality_verdict="weak",
            ),
        ),
        sparring=BrokenProvider(
            "mimo",
            "mimo-v2.5-pro",
            _review_payload("revise", review_markdown="Mimo requests revision."),
        ),
        fallback=BrokenProvider(
            "openrouter",
            "google/gemma-4-31b-it",
            _review_payload("reject", review_markdown="Fallback rejects."),
        ),
    )
    result = panel.complete(
        ProviderRequest(
            system_prompt="system",
            user_prompt="user",
            prompt_version="reviewer-v1",
            response_format="json_object",
        )
    )
    assert result.ok is True
    assert result.response is not None
    assert '"recommendation": "revise"' in result.response.text
    assert result.response.metadata["route"] == "primary_failed_sparring_used"
    assert "accept_rubric_too_weak" in str(result.response.metadata["primary_error"])


@pytest.mark.parametrize(
    ("required_revisions", "expected_error"),
    [
        ([], "revise_missing_required_revisions"),
        (None, "missing_required_revisions"),
        ("__missing__", "missing_required_revisions"),
    ],
)
def test_reviewer_panel_rejects_non_actionable_revise_contract(required_revisions: object, expected_error: str) -> None:
    class Provider:
        def __init__(self, provider: str, model: str, payload: dict[str, object]) -> None:
            self.provider = provider
            self.model = model
            self.payload = payload

        def complete(self, request: ProviderRequest) -> ProviderResult:
            return ProviderResult(
                ok=True,
                response=ProviderResponse(
                    text=json.dumps(self.payload),
                    provider=self.provider,
                    model=self.model,
                    usage=ProviderUsage(input_tokens=10, output_tokens=5, cost_usd=0.1),
                ),
            )

    primary_payload = _review_payload("revise")
    if required_revisions == "__missing__":
        primary_payload.pop("required_revisions")
    else:
        primary_payload["required_revisions"] = required_revisions

    panel = ReviewerPanel(
        primary=Provider(
            "mimo",
            "mimo-v2.5-pro",
            primary_payload,
        ),
        sparring=Provider(
            "gemma",
            "google/gemma-4-31b-it",
            _review_payload("accept", review_markdown="Sparring accepts."),
        ),
        fallback=Provider(
            "mistral",
            "mistralai/mistral-small-2603",
            _review_payload("reject", review_markdown="Fallback rejects."),
        ),
    )

    result = panel.complete(
        ProviderRequest(
            system_prompt="system",
            user_prompt="user",
            prompt_version="reviewer-v1",
            response_format="json_object",
        )
    )

    assert result.ok is True
    assert result.response is not None
    assert result.response.metadata["route"] == "primary_failed_sparring_used"
    assert expected_error in str(result.response.metadata["primary_error"])
    assert '"recommendation": "accept"' in result.response.text


def test_workflow_stores_panel_route_metadata() -> None:
    class PanelProvider:
        provider = "reviewer-panel"
        model = "mimo-v2.5-pro|google/gemma-4-31b-it|mistralai/mistral-small-2603"

        def complete(self, request: ProviderRequest) -> ProviderResult:
            return ProviderResult(
                ok=True,
                response=ProviderResponse(
                    text=json.dumps(
                        {
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
                            "review_markdown": "Consensus says accept.",
                        }
                    ),
                    provider=self.provider,
                    model=self.model,
                    usage=ProviderUsage(input_tokens=33, output_tokens=12, cost_usd=0.9),
                    metadata={
                        "route": "consensus",
                        "winner_provider": "mimo",
                        "winner_model": "mimo-v2.5-pro",
                        "primary_recommendation": "accept",
                        "sparring_recommendation": "accept",
                    },
                ),
            )

    repo = InMemoryRuntimeRepository()
    submission = repo.create_object(
        ResearchObject(
            object_type="submission",
            title="Panel metadata",
            metadata={
                "domain_slug": "longevity",
                "abstract": "Bounded external submission.",
                "sections": _full_sections(),
                "source_bundle": _valid_source_bundle(),
                "core_claims_resolved": True,
            },
        )
    )
    engine = WorkflowEngine(provider=PanelProvider())
    engine.handle_job(
        RuntimeJob(target_object_id=submission.id, stage=Stage.INTAKE, payload={"domain_slug": "longevity"}),
        repo,
    )
    review_job = repo.queued_jobs()[0]
    engine.handle_job(review_job, repo)
    review = repo.list_objects("review")[0]
    assert review.metadata["provider"] == "reviewer-panel"
    assert review.metadata["route"] == "consensus"
    assert review.metadata["winner_provider"] == "mimo"


_LONGEVITY_EXEMPLAR_ACCEPT = {
    "title": "Taurine deficiency as a driver of aging",
    "abstract": "This rapid evidence synthesis asks whether circulating taurine concentrations decline with aging and whether reversing that decline through supplementation increases healthspan and lifespan across species.",
    "sections": {
        "Research Question": "Do circulating taurine concentrations decline with aging, and does reversing that decline through supplementation increase healthspan and lifespan across species (worms, mice, monkeys, humans)? This synthesis evaluates the interventional evidence from controlled mouse and monkey studies alongside the available human observational data, explicitly noting the limits of cross-species causal inference.",
        "Search Summary": "Searches were conducted across PubMed and Google Scholar for studies published 2015–2023 reporting on taurine supplementation, aging biomarkers, and lifespan outcomes in vertebrate models. Inclusion criteria required interventional arms with placebo controls and reported mortality or functional healthspan endpoints. Human evidence was restricted to prospective cohort or cross-sectional studies with measured circulating taurine levels and aging outcomes.",
        "Evidence Landscape": "The retained bundle contains 1 multi-species interventional study (Singh et al., Science, 2023) with mice, monkeys, and human observational arms, plus 3 supporting primary studies on taurine biology in aging. The evidence base is narrow but internally consistent: all species show age-related decline in circulating taurine, and supplementation shows lifespan or functional benefit in non-human models.",
        "Key Findings": "In mice, taurine supplementation increased average lifespan by approximately 10% in males and 12% in females compared to placebo controls. In rhesus monkeys, 6 months of taurine supplementation improved bone density, glucose tolerance, and grip strength relative to baseline, though no lifespan data are available for the monkey cohort. Human evidence shows an association between lower circulating taurine and higher mortality risk in a cross-sectional cohort (n=927), but no interventional human data exist yet.",
        "Limitations": "The causal chain from taurine supplementation to extended lifespan rests entirely on non-human evidence. Human data are observational and cannot establish causality. No adequately powered RCT has tested taurine supplementation in humans for healthspan or lifespan outcomes.",
        "Gaps Identified": "No interventional human trial has tested taurine supplementation against aging endpoints; the translational gap between mouse lifespan data and human healthspan outcomes remains the critical unknown.",
        "Conclusion": "The available evidence supports taurine as a promising longevity intervention in non-human models. No adequately powered human RCT has confirmed healthspan or lifespan benefits. This synthesis should be treated as hypothesis-generating rather than actionable clinical guidance.",
    },
    "source_bundle": [
        *_valid_source_bundle(),
    ],
}


def test_longevity_exemplar_submission_is_accepted() -> None:
    repo = InMemoryRuntimeRepository()
    submission = repo.create_object(
        ResearchObject(
            object_type=ObjectType.SUBMISSION,
            title=_LONGEVITY_EXEMPLAR_ACCEPT["title"],
            metadata={
                "abstract": _LONGEVITY_EXEMPLAR_ACCEPT["abstract"],
                "sections": _LONGEVITY_EXEMPLAR_ACCEPT["sections"],
                "source_bundle": _LONGEVITY_EXEMPLAR_ACCEPT["source_bundle"],
                "core_claims_resolved": True,
                "author_agent_id": "agent-calibration",
                "domain_slug": "longevity",
            },
        )
    )
    engine = WorkflowEngine()
    intake_job = repo.enqueue_job(RuntimeJob(target_object_id=submission.id, stage=Stage.INTAKE, payload={"domain_slug": "longevity"}))
    engine.handle_job(intake_job, repo)
    repo.complete_job(intake_job.id)

    review_job = repo.claim_next_job()
    engine.handle_job(review_job, repo)
    repo.complete_job(review_job.id)

    review = repo.list_objects(ObjectType.REVIEW)[0]
    editorial_job = repo.enqueue_job(RuntimeJob(target_object_id=submission.id, stage=Stage.EDITORIAL, payload={"review_id": review.id, "domain_slug": "longevity"}))
    engine.handle_job(editorial_job, repo)
    repo.complete_job(editorial_job.id)

    decision = repo.list_objects(ObjectType.DECISION)[0]
    assert decision.metadata["decision"] == Decision.ACCEPT.value


def test_missing_limitations_fails_intake() -> None:
    repo = InMemoryRuntimeRepository()
    submission = repo.create_object(
        ResearchObject(
            object_type=ObjectType.SUBMISSION,
            title="Calorie restriction reduces biomarkers of cellular senescence",
            metadata={
                "abstract": "CALERIE-2 RCT analysis.",
                "sections": {
        "Research Question": "Does two years of moderate caloric restriction reduce circulating biomarkers of cellular senescence in healthy adults enrolled in CALERIE-2, and can that signal be interpreted as evidence of a durable anti-aging effect rather than a study-specific biomarker shift confined to one tightly monitored trial population with unusually high adherence, intensive follow-up, and limited demographic diversity?",
                    "Search Summary": "CALERIE-2 biomarker sub-study retained as sole source. No other RCTs of CR with senescence endpoints identified in this population.",
                    "Evidence Landscape": "Single well-powered RCT supplemented by mechanistic reviews. Evidence base is narrow by design.",
                    "Key Findings": "CR significantly reduced 9 SASP biomarkers at 12 months and a subset at 24 months vs ad libitum controls.",
                    "Gaps Identified": "No replication cohort has confirmed these biomarker changes in a different population or dietary context.",
                    "Conclusion": "CALERIE-2 demonstrates biomarker-level effects of CR consistent with reduced cellular senescence.",
                },
                "source_bundle": _valid_source_bundle(),
                "core_claims_resolved": True,
                "author_agent_id": "agent-calibration",
                "domain_slug": "longevity",
            },
        )
    )
    engine = WorkflowEngine()
    result = engine.handle_job(RuntimeJob(target_object_id=submission.id, stage=Stage.INTAKE, payload={"domain_slug": "longevity"}), repo)
    assert result.get("terminal_decision") == Decision.REJECT.value


def test_missing_gaps_identified_fails_intake() -> None:
    repo = InMemoryRuntimeRepository()
    submission = repo.create_object(
        ResearchObject(
            object_type=ObjectType.SUBMISSION,
            title="Senolytic D+Q trial in postmenopausal bone loss",
            metadata={
                "abstract": "Phase 2 RCT of dasatinib + quercetin for bone metabolism.",
                "sections": {
        "Research Question": "In postmenopausal women with osteopenia, does twenty weeks of intermittent senolytic therapy with dasatinib plus quercetin reduce the bone resorption marker CTx versus placebo, and is any observed effect strong enough to justify broader claims about bone-health deployment beyond this single short-duration randomized trial, limited endpoint set, and one narrowly defined patient population?",
                    "Search Summary": "Single phase 2 RCT retained. PubMed and ClinicalTrials.gov searched for senolytic trials with bone endpoints in postmenopausal populations.",
                    "Evidence Landscape": "One well-powered RCT with preregistered primary endpoint. No systematic reviews of senolytics for bone metabolism exist.",
                    "Key Findings": "The primary endpoint did not differ between groups at 20 weeks. Secondary bone formation markers showed transient early elevation.",
                    "Limitations": "Single-site trial, limited to postmenopausal women, short follow-up, and exploratory subgroup analyses are hypothesis-generating only.",
                    "Conclusion": "Senolytic D+Q did not reduce bone resorption at 20 weeks in this population. Further dose-finding and longer follow-up are needed.",
                },
                "source_bundle": _valid_source_bundle(),
                "core_claims_resolved": True,
                "author_agent_id": "agent-calibration",
                "domain_slug": "longevity",
            },
        )
    )
    engine = WorkflowEngine()
    result = engine.handle_job(RuntimeJob(target_object_id=submission.id, stage=Stage.INTAKE, payload={"domain_slug": "longevity"}), repo)
    assert result.get("terminal_decision") == Decision.REJECT.value


def test_short_research_question_fails_template_gate() -> None:
    repo = InMemoryRuntimeRepository()
    sections = _full_sections()
    sections["Research Question"] = "Does this work for aging outcomes in humans?"
    submission = repo.create_object(
        ResearchObject(
            object_type=ObjectType.SUBMISSION,
            title="Short question",
            metadata={
                "abstract": "Bounded external submission.",
                "sections": sections,
                "source_bundle": _valid_source_bundle(),
                "core_claims_resolved": True,
                "author_agent_id": "agent-calibration",
                "domain_slug": "longevity",
            },
        )
    )
    engine = WorkflowEngine()
    result = engine.handle_job(RuntimeJob(target_object_id=submission.id, stage=Stage.INTAKE, payload={"domain_slug": "longevity"}), repo)
    decision = repo.list_objects(ObjectType.DECISION)[0]
    assert result.get("terminal_decision") == Decision.REJECT.value
    assert {failure["name"] for failure in decision.metadata["gate_failures"]} == {"research_question_word_budget"}


def test_too_few_citations_fail_template_gate() -> None:
    repo = InMemoryRuntimeRepository()
    submission = repo.create_object(
        ResearchObject(
            object_type=ObjectType.SUBMISSION,
            title="Too few citations",
            metadata={
                "abstract": "Bounded external submission.",
                "sections": _full_sections(),
                "source_bundle": _valid_source_bundle()[:11],
                "core_claims_resolved": True,
                "author_agent_id": "agent-calibration",
                "domain_slug": "longevity",
            },
        )
    )
    engine = WorkflowEngine()
    result = engine.handle_job(RuntimeJob(target_object_id=submission.id, stage=Stage.INTAKE, payload={"domain_slug": "longevity"}), repo)
    decision = repo.list_objects(ObjectType.DECISION)[0]
    assert result.get("terminal_decision") == Decision.REJECT.value
    assert {failure["name"] for failure in decision.metadata["gate_failures"]} == {"minimum_citations"}


def test_low_recency_ratio_fails_template_gate() -> None:
    repo = InMemoryRuntimeRepository()
    old_bundle = [
        {**entry, "year": 2018 if index >= 5 else 2021}
        for index, entry in enumerate(_valid_source_bundle())
    ]
    submission = repo.create_object(
        ResearchObject(
            object_type=ObjectType.SUBMISSION,
            title="Low recency ratio",
            metadata={
                "abstract": "Bounded external submission.",
                "sections": _full_sections(),
                "source_bundle": old_bundle,
                "core_claims_resolved": True,
                "author_agent_id": "agent-calibration",
                "domain_slug": "longevity",
            },
        )
    )
    engine = WorkflowEngine()
    result = engine.handle_job(RuntimeJob(target_object_id=submission.id, stage=Stage.INTAKE, payload={"domain_slug": "longevity"}), repo)
    decision = repo.list_objects(ObjectType.DECISION)[0]
    assert result.get("terminal_decision") == Decision.REJECT.value
    assert {failure["name"] for failure in decision.metadata["gate_failures"]} == {"recency_ratio"}


def test_invalid_source_bundle_schema_fails_intake() -> None:
    repo = InMemoryRuntimeRepository()
    invalid_bundle = _valid_source_bundle()
    invalid_bundle[0].pop("title")
    submission = repo.create_object(
        ResearchObject(
            object_type=ObjectType.SUBMISSION,
            title="Invalid source schema",
            metadata={
                "abstract": "Bounded external submission.",
                "sections": _full_sections(),
                "source_bundle": invalid_bundle,
                "core_claims_resolved": True,
                "author_agent_id": "agent-calibration",
                "domain_slug": "longevity",
            },
        )
    )
    engine = WorkflowEngine()
    result = engine.handle_job(RuntimeJob(target_object_id=submission.id, stage=Stage.INTAKE, payload={"domain_slug": "longevity"}), repo)
    decision = repo.list_objects(ObjectType.DECISION)[0]
    assert result.get("terminal_decision") == Decision.REJECT.value
    assert {failure["name"] for failure in decision.metadata["gate_failures"]} == {"source_bundle_schema"}


def test_missing_evidence_type_fails_intake() -> None:
    repo = InMemoryRuntimeRepository()
    bundle = _valid_source_bundle()
    del bundle[0]["evidence_type"]
    submission = repo.create_object(
        ResearchObject(
            object_type=ObjectType.SUBMISSION,
            title="Missing evidence type",
            metadata={
                "abstract": "Bounded external submission.",
                "sections": _full_sections(),
                "source_bundle": bundle,
                "core_claims_resolved": True,
                "author_agent_id": "agent-calibration",
                "domain_slug": "longevity",
            },
        )
    )
    engine = WorkflowEngine()
    result = engine.handle_job(RuntimeJob(target_object_id=submission.id, stage=Stage.INTAKE, payload={"domain_slug": "longevity"}), repo)
    assert result.get("terminal_decision") == Decision.REJECT.value


def test_calibration_revise_decision() -> None:
    class ReviseProvider:
        provider = "calibration-revise"
        model = "calibration-revise-model"

        def complete(self, request: ProviderRequest) -> ProviderResult:
            return ProviderResult(
                ok=True,
                response=ProviderResponse(
                    text=json.dumps(
                        {
                            "recommendation": "revise",
                            "rubric_scores": {
                                "research_question_quality": 4,
                                "synthesis_quality": 2,
                                "claim_evidence_alignment": 3,
                                "limitations_quality": 3,
                                "gaps_quality": 4,
                                "source_grounding": 3,
                            },
                            "major_issues": [
                                "Key findings are descriptive rather than synthetic.",
                                "Conclusion is broader than the evidence landscape supports.",
                            ],
                            "minor_issues": ["Limitations section is honest but not integrated into the conclusion."],
                            "required_revisions": [
                                "Rewrite key findings to synthesize rather than describe.",
                                "Narrow conclusion to match evidence scope.",
                            ],
                            "claim_support_verdict": "partially_supported",
                            "overclaim_verdict": "mild",
                            "synthesis_quality_verdict": "weak",
                            "review_markdown": "The submission has a sound research question and adequate search summary, but the key findings section needs expansion with more specific quantitative detail from the source bundle.",
                        }
                    ),
                    provider=self.provider,
                    model=self.model,
                    usage=ProviderUsage(input_tokens=20, output_tokens=10, cost_usd=0.0),
                ),
            )

    repo = InMemoryRuntimeRepository()
    submission = repo.create_object(
        ResearchObject(
            object_type=ObjectType.SUBMISSION,
            title="Partial OSKM reprogramming in aged wild-type mice",
            metadata={
                "abstract": "Does cyclic Yamanaka factor expression reverse aging markers in physiologically aged mice without tumor formation?",
                "sections": {
                    "Research Question": "Does long-term cyclic expression of Yamanaka reprogramming factors in physiologically aged wild-type mice reverse age-associated epigenetic changes without inducing tumor formation, and can a single mouse intervention study support any bounded inference about translational relevance beyond the tested induction schedule, tissue compartments, follow-up horizon, and one narrowly controlled experimental background?",
                    "Search Summary": "Retained 1 primary study from Nature Aging 2022 on cyclic OSKM in aged wild-type mice. No human reprogramming data met inclusion criteria.",
                    "Evidence Landscape": "Single primary study with interventional arms in aged wild-type mice. No systematic review exists for this specific paradigm.",
                    "Key Findings": "Cyclic OSKM reversed epigenetic clock markers in kidney and skin tissue, with reduced expression of inflammation, senescence, and stress-response gene programs observed across multiple tissue compartments in the aged wild-type cohort.",
                    "Limitations": "Single mouse study, no human data, cancer risk excluded only within the tested induction schedule, and long-term effects remain unknown.",
                    "Gaps Identified": "No primate or human safety data exist for cyclic reprogramming, and the parameter space of induction schedules is unexplored beyond the single tested protocol.",
                    "Conclusion": "The evidence supports cyclic OSKM as a promising aging intervention in mice but cannot extrapolate to human application without further safety and efficacy studies.",
                },
                "source_bundle": _valid_source_bundle(),
                "core_claims_resolved": True,
                "author_agent_id": "agent-calibration",
                "domain_slug": "longevity",
            },
        )
    )
    engine = WorkflowEngine(provider=ReviseProvider())

    intake_job = repo.enqueue_job(RuntimeJob(target_object_id=submission.id, stage=Stage.INTAKE, payload={"domain_slug": "longevity"}))
    intake_result = engine.handle_job(intake_job, repo)
    assert intake_result.get("next_stage") == Stage.REVIEW.value
    repo.complete_job(intake_job.id)

    review_job = repo.claim_next_job()
    assert review_job is not None
    engine.handle_job(review_job, repo)
    repo.complete_job(review_job.id)

    review = repo.list_objects(ObjectType.REVIEW)[0]
    assert review.metadata["recommendation"] == "revise"

    editorial_job = repo.claim_next_job()
    assert editorial_job is not None
    engine.handle_job(editorial_job, repo)
    repo.complete_job(editorial_job.id)

    decision = repo.list_objects(ObjectType.DECISION)[0]
    assert decision.metadata["decision"] == Decision.REVISE.value
    assert repo.queued_jobs() == []


def _rubric_accept_provider() -> object:
    class Provider:
        provider = "calibration-accept"
        model = "calibration-accept-model"

        def complete(self, request: ProviderRequest) -> ProviderResult:
            return ProviderResult(
                ok=True,
                response=ProviderResponse(
                    text=json.dumps(
                        {
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
                            "minor_issues": ["Could expand gaps section slightly."],
                            "required_revisions": [],
                            "claim_support_verdict": "supported",
                            "overclaim_verdict": "none",
                            "synthesis_quality_verdict": "strong",
                            "review_markdown": "The submission meets all rubric thresholds for acceptance.",
                        }
                    ),
                    provider=self.provider,
                    model=self.model,
                    usage=ProviderUsage(input_tokens=20, output_tokens=10, cost_usd=0.0),
                ),
            )

    return Provider()


def _minor_only_revise_provider() -> object:
    class Provider:
        provider = "minor-only-revise"
        model = "minor-only-revise-model"

        def complete(self, request: ProviderRequest) -> ProviderResult:
            return ProviderResult(
                ok=True,
                response=ProviderResponse(
                    text=json.dumps(
                        {
                            "recommendation": "revise",
                            "rubric_scores": {
                                "research_question_quality": 5,
                                "synthesis_quality": 5,
                                "claim_evidence_alignment": 5,
                                "limitations_quality": 5,
                                "gaps_quality": 4,
                                "source_grounding": 4,
                            },
                            "major_issues": [],
                            "minor_issues": ["Clarify one wording detail."],
                            "required_revisions": [],
                            "claim_support_verdict": "supported",
                            "overclaim_verdict": "none",
                            "synthesis_quality_verdict": "strong",
                            "review_markdown": "Excellent synthesis. Minor issues do not detract from core quality.",
                        }
                    ),
                    provider=self.provider,
                    model=self.model,
                    usage=ProviderUsage(input_tokens=20, output_tokens=10, cost_usd=0.0),
                ),
            )

    return Provider()


def _rubric_revise_provider() -> object:
    class Provider:
        provider = "calibration-revise"
        model = "calibration-revise-model"

        def complete(self, request: ProviderRequest) -> ProviderResult:
            return ProviderResult(
                ok=True,
                response=ProviderResponse(
                    text=json.dumps(
                        {
                            "recommendation": "revise",
                            "rubric_scores": {
                                "research_question_quality": 4,
                                "synthesis_quality": 2,
                                "claim_evidence_alignment": 3,
                                "limitations_quality": 3,
                                "gaps_quality": 4,
                                "source_grounding": 3,
                            },
                            "major_issues": [
                                "Key findings describe rather than synthesize.",
                                "Conclusion is broader than the evidence landscape supports.",
                            ],
                            "minor_issues": ["Limitations section is honest but not integrated into the conclusion."],
                            "required_revisions": [
                                "Rewrite key findings to synthesize rather than describe.",
                                "Narrow conclusion to match evidence scope.",
                            ],
                            "claim_support_verdict": "partially_supported",
                            "overclaim_verdict": "mild",
                            "synthesis_quality_verdict": "weak",
                            "review_markdown": "The submission has a sound research question but key findings are descriptive and the conclusion overclaims.",
                        }
                    ),
                    provider=self.provider,
                    model=self.model,
                    usage=ProviderUsage(input_tokens=20, output_tokens=10, cost_usd=0.0),
                ),
            )

    return Provider()


def _rubric_reject_provider() -> object:
    class Provider:
        provider = "calibration-reject"
        model = "calibration-reject-model"

        def complete(self, request: ProviderRequest) -> ProviderResult:
            return ProviderResult(
                ok=True,
                response=ProviderResponse(
                    text=json.dumps(
                        {
                            "recommendation": "reject",
                            "rubric_scores": {
                                "research_question_quality": 3,
                                "synthesis_quality": 1,
                                "claim_evidence_alignment": 1,
                                "limitations_quality": 2,
                                "gaps_quality": 2,
                                "source_grounding": 1,
                            },
                            "major_issues": [
                                "Key findings section is substantively empty with padded filler.",
                                "Major claims outrun the source bundle entirely.",
                                "Conclusion depends on speculative extrapolation.",
                            ],
                            "minor_issues": [],
                            "required_revisions": [
                                "Complete rewrite needed. This is a review-shaped paper without real synthesis.",
                            ],
                            "claim_support_verdict": "unsupported",
                            "overclaim_verdict": "significant",
                            "synthesis_quality_verdict": "empty",
                            "review_markdown": "The submission passes structural gates but is substantively empty. Sections are padded, claims are unsupported, and there is no real synthesis.",
                        }
                    ),
                    provider=self.provider,
                    model=self.model,
                    usage=ProviderUsage(input_tokens=20, output_tokens=10, cost_usd=0.0),
                ),
            )

    return Provider()


def _calibration_submission(repo: InMemoryRuntimeRepository, *, recommendation: str) -> ResearchObject:
    return repo.create_object(
        ResearchObject(
            object_type=ObjectType.SUBMISSION,
            title=f"Calibration {recommendation} paper",
            metadata={
                "abstract": "Bounded calibration submission.",
                "sections": _full_sections(),
                "source_bundle": _valid_source_bundle(),
                "core_claims_resolved": True,
                "author_agent_id": "agent-calibration",
                "domain_slug": "longevity",
            },
        )
    )


def test_calibration_accept_rubric_fields_stored() -> None:
    repo = InMemoryRuntimeRepository()
    submission = _calibration_submission(repo, recommendation="accept")
    engine = WorkflowEngine(provider=_rubric_accept_provider())

    intake_job = repo.enqueue_job(RuntimeJob(target_object_id=submission.id, stage=Stage.INTAKE, payload={"domain_slug": "longevity"}))
    engine.handle_job(intake_job, repo)
    repo.complete_job(intake_job.id)

    review_job = repo.claim_next_job()
    engine.handle_job(review_job, repo)
    repo.complete_job(review_job.id)

    review = repo.list_objects(ObjectType.REVIEW)[0]
    assert review.metadata["recommendation"] == "accept"
    assert review.metadata["rubric_scores"]["synthesis_quality"] == 4
    assert review.metadata["claim_support_verdict"] == "supported"
    assert review.metadata["overclaim_verdict"] == "none"
    assert review.metadata["synthesis_quality_verdict"] == "strong"
    assert review.metadata["major_issues"] == []

    editorial_job = repo.claim_next_job()
    engine.handle_job(editorial_job, repo)
    repo.complete_job(editorial_job.id)

    decision = repo.list_objects(ObjectType.DECISION)[0]
    assert decision.metadata["decision"] == Decision.ACCEPT.value


def test_panel_accept_contract_matches_workflow_contract() -> None:
    class StaticReviewProvider:
        provider = "static-reviewer"
        model = "static-reviewer-model"

        def complete(self, request: ProviderRequest) -> ProviderResult:
            return ProviderResult(
                ok=True,
                response=ProviderResponse(
                    text=json.dumps(
                        _review_payload(
                            "accept",
                            rubric_scores={
                                "research_question_quality": 5,
                                "synthesis_quality": 3,
                                "claim_evidence_alignment": 5,
                                "limitations_quality": 4,
                                "gaps_quality": 4,
                                "source_grounding": 5,
                            },
                        )
                    ),
                    provider=self.provider,
                    model=self.model,
                    usage=ProviderUsage(input_tokens=10, output_tokens=10, cost_usd=0.0),
                ),
            )

    repo = InMemoryRuntimeRepository()
    submission = _calibration_submission(repo, recommendation="accept")
    panel = ReviewerPanel(
        primary=StaticReviewProvider(),
        sparring=StaticReviewProvider(),
        fallback=StaticReviewProvider(),
    )
    engine = WorkflowEngine(provider=panel)

    intake_job = repo.enqueue_job(RuntimeJob(target_object_id=submission.id, stage=Stage.INTAKE, payload={"domain_slug": "longevity"}))
    engine.handle_job(intake_job, repo)
    repo.complete_job(intake_job.id)

    review_job = repo.claim_next_job()
    engine.handle_job(review_job, repo)
    repo.complete_job(review_job.id)

    review = repo.list_objects(ObjectType.REVIEW)[0]
    assert review.metadata["recommendation"] == Decision.ACCEPT.value
    assert review.metadata["rubric_scores"]["synthesis_quality"] == 3


def test_minor_issues_only_revise_is_calibrated_to_accept() -> None:
    repo = InMemoryRuntimeRepository()
    submission = _calibration_submission(repo, recommendation="revise")
    engine = WorkflowEngine(provider=_minor_only_revise_provider())

    intake_job = repo.enqueue_job(RuntimeJob(target_object_id=submission.id, stage=Stage.INTAKE, payload={"domain_slug": "longevity"}))
    engine.handle_job(intake_job, repo)
    repo.complete_job(intake_job.id)

    review_job = repo.claim_next_job()
    engine.handle_job(review_job, repo)
    repo.complete_job(review_job.id)

    review = repo.list_objects(ObjectType.REVIEW)[0]
    assert review.metadata["recommendation"] == Decision.ACCEPT.value
    assert review.metadata["original_recommendation"] == Decision.REVISE.value
    assert review.metadata["recommendation_calibration"] == "minor_issues_only_accept_contract"
    assert review.metadata["minor_issues"] == ["Clarify one wording detail."]

    editorial_job = repo.claim_next_job()
    engine.handle_job(editorial_job, repo)
    repo.complete_job(editorial_job.id)

    decision = repo.list_objects(ObjectType.DECISION)[0]
    assert decision.metadata["decision"] == Decision.ACCEPT.value
    assert repo.queued_jobs()[0].stage == Stage.PUBLISH


def test_existing_minor_issues_only_review_is_calibrated_by_editorial() -> None:
    repo = InMemoryRuntimeRepository()
    submission = _calibration_submission(repo, recommendation="revise")
    review = repo.create_object(
        ResearchObject(
            object_type=ObjectType.REVIEW,
            parent_object_id=submission.id,
            title=f"Review for {submission.title}",
            body_markdown="Excellent synthesis. Minor issues do not detract from core quality.",
            metadata={
                "recommendation": "revise",
                "rubric_scores": {
                    "research_question_quality": 5,
                    "synthesis_quality": 5,
                    "claim_evidence_alignment": 5,
                    "limitations_quality": 5,
                    "gaps_quality": 4,
                    "source_grounding": 4,
                },
                "major_issues": [],
                "minor_issues": ["Clarify one wording detail."],
                "required_revisions": [],
                "claim_support_verdict": "supported",
                "overclaim_verdict": "none",
                "synthesis_quality_verdict": "strong",
            },
        )
    )

    engine = WorkflowEngine(provider=_rubric_revise_provider())
    editorial_job = RuntimeJob(
        target_object_id=submission.id,
        stage=Stage.EDITORIAL,
        payload={"review_id": review.id, "domain_slug": "longevity"},
    )
    result = engine.handle_job(editorial_job, repo)

    decision = repo.list_objects(ObjectType.DECISION)[0]
    assert result["terminal_decision"] == Decision.ACCEPT.value
    assert decision.metadata["decision"] == Decision.ACCEPT.value
    assert decision.metadata["original_recommendation"] == Decision.REVISE.value
    assert decision.metadata["recommendation_calibration"] == "minor_issues_only_accept_contract"
    assert repo.queued_jobs()[0].stage == Stage.PUBLISH


def test_calibration_revise_rubric_fields_stored() -> None:
    repo = InMemoryRuntimeRepository()
    submission = _calibration_submission(repo, recommendation="revise")
    engine = WorkflowEngine(provider=_rubric_revise_provider())

    intake_job = repo.enqueue_job(RuntimeJob(target_object_id=submission.id, stage=Stage.INTAKE, payload={"domain_slug": "longevity"}))
    engine.handle_job(intake_job, repo)
    repo.complete_job(intake_job.id)

    review_job = repo.claim_next_job()
    engine.handle_job(review_job, repo)
    repo.complete_job(review_job.id)

    review = repo.list_objects(ObjectType.REVIEW)[0]
    assert review.metadata["recommendation"] == "revise"
    assert review.metadata["rubric_scores"]["synthesis_quality"] == 2
    assert review.metadata["claim_support_verdict"] == "partially_supported"
    assert review.metadata["overclaim_verdict"] == "mild"
    assert review.metadata["synthesis_quality_verdict"] == "weak"
    assert len(review.metadata["major_issues"]) == 2

    editorial_job = repo.claim_next_job()
    engine.handle_job(editorial_job, repo)
    repo.complete_job(editorial_job.id)

    decision = repo.list_objects(ObjectType.DECISION)[0]
    assert decision.metadata["decision"] == Decision.REVISE.value
    assert repo.queued_jobs() == []


def test_calibration_reject_rubric_fields_stored() -> None:
    repo = InMemoryRuntimeRepository()
    submission = _calibration_submission(repo, recommendation="reject")
    engine = WorkflowEngine(provider=_rubric_reject_provider())

    intake_job = repo.enqueue_job(RuntimeJob(target_object_id=submission.id, stage=Stage.INTAKE, payload={"domain_slug": "longevity"}))
    engine.handle_job(intake_job, repo)
    repo.complete_job(intake_job.id)

    review_job = repo.claim_next_job()
    engine.handle_job(review_job, repo)
    repo.complete_job(review_job.id)

    review = repo.list_objects(ObjectType.REVIEW)[0]
    assert review.metadata["recommendation"] == "reject"
    assert review.metadata["rubric_scores"]["synthesis_quality"] == 1
    assert review.metadata["claim_support_verdict"] == "unsupported"
    assert review.metadata["overclaim_verdict"] == "significant"
    assert review.metadata["synthesis_quality_verdict"] == "empty"
    assert len(review.metadata["major_issues"]) == 3

    editorial_job = repo.claim_next_job()
    engine.handle_job(editorial_job, repo)
    repo.complete_job(editorial_job.id)

    decision = repo.list_objects(ObjectType.DECISION)[0]
    assert decision.metadata["decision"] == Decision.REJECT.value
    assert repo.queued_jobs() == []


def test_panel_stores_rubric_metadata_in_review() -> None:
    class PanelProviderWithRubric:
        provider = "reviewer-panel"
        model = "mimo-v2.5-pro|google/gemma-4-31b-it|mistralai/mistral-small-2603"

        def complete(self, request: ProviderRequest) -> ProviderResult:
            return ProviderResult(
                ok=True,
                response=ProviderResponse(
                    text=json.dumps(
                        {
                            "recommendation": "revise",
                            "rubric_scores": {
                                "research_question_quality": 4,
                                "synthesis_quality": 3,
                                "claim_evidence_alignment": 3,
                                "limitations_quality": 3,
                                "gaps_quality": 4,
                                "source_grounding": 3,
                            },
                            "major_issues": ["Key findings are descriptive rather than synthetic."],
                            "minor_issues": [],
                            "required_revisions": ["Rewrite key findings to integrate evidence."],
                            "claim_support_verdict": "partially_supported",
                            "overclaim_verdict": "mild",
                            "synthesis_quality_verdict": "adequate",
                            "review_markdown": "Panel consensus: revise for synthesis quality.",
                        }
                    ),
                    provider=self.provider,
                    model=self.model,
                    usage=ProviderUsage(input_tokens=33, output_tokens=12, cost_usd=0.9),
                    metadata={
                        "route": "consensus",
                        "winner_provider": "mimo",
                        "winner_model": "mimo-v2.5-pro",
                        "primary_recommendation": "revise",
                        "sparring_recommendation": "revise",
                    },
                ),
            )

    repo = InMemoryRuntimeRepository()
    submission = _calibration_submission(repo, recommendation="revise")
    engine = WorkflowEngine(provider=PanelProviderWithRubric())

    intake_job = repo.enqueue_job(RuntimeJob(target_object_id=submission.id, stage=Stage.INTAKE, payload={"domain_slug": "longevity"}))
    engine.handle_job(intake_job, repo)
    repo.complete_job(intake_job.id)

    review_job = repo.claim_next_job()
    engine.handle_job(review_job, repo)
    repo.complete_job(review_job.id)

    review = repo.list_objects(ObjectType.REVIEW)[0]
    assert review.metadata["provider"] == "reviewer-panel"
    assert review.metadata["route"] == "consensus"
    assert review.metadata["rubric_scores"]["synthesis_quality"] == 3
    assert review.metadata["claim_support_verdict"] == "partially_supported"
    assert review.metadata["overclaim_verdict"] == "mild"
    assert review.metadata["major_issues"] == ["Key findings are descriptive rather than synthetic."]


def test_accept_requires_strong_rubric_contract() -> None:
    class WeakAcceptProvider:
        provider = "weak-accept"
        model = "weak-accept-model"

        def complete(self, request: ProviderRequest) -> ProviderResult:
            return ProviderResult(
                ok=True,
                response=ProviderResponse(
                    text=json.dumps(
                        {
                            "recommendation": "accept",
                            "rubric_scores": {
                                "research_question_quality": 4,
                                "synthesis_quality": 2,
                                "claim_evidence_alignment": 3,
                                "limitations_quality": 4,
                                "gaps_quality": 4,
                                "source_grounding": 3,
                            },
                            "major_issues": ["Key findings are descriptive rather than synthetic."],
                            "minor_issues": [],
                            "required_revisions": ["Rewrite key findings."],
                            "claim_support_verdict": "partially_supported",
                            "overclaim_verdict": "mild",
                            "synthesis_quality_verdict": "weak",
                            "review_markdown": "Accept.",
                        }
                    ),
                    provider=self.provider,
                    model=self.model,
                    usage=ProviderUsage(input_tokens=10, output_tokens=10, cost_usd=0.0),
                ),
            )

    repo = InMemoryRuntimeRepository()
    submission = _calibration_submission(repo, recommendation="accept")
    engine = WorkflowEngine(provider=WeakAcceptProvider())
    intake_job = repo.enqueue_job(RuntimeJob(target_object_id=submission.id, stage=Stage.INTAKE, payload={"domain_slug": "longevity"}))
    engine.handle_job(intake_job, repo)
    repo.complete_job(intake_job.id)

    review_job = repo.claim_next_job()
    assert review_job is not None
    with pytest.raises(ValueError, match="accept_rubric_too_weak|accept_has_major_issues|accept_claim_support_not_supported|accept_has_overclaim|accept_has_required_revisions"):
        engine.handle_job(review_job, repo)


@pytest.mark.parametrize(
    ("required_revisions", "expected_error"),
    [
        ([], "revise_missing_required_revisions"),
        (None, "missing_required_revisions"),
        ("__missing__", "missing_required_revisions"),
    ],
)
def test_non_actionable_revise_is_rejected_before_review_storage(required_revisions: object, expected_error: str) -> None:
    class BadReviseProvider:
        provider = "bad-revise"
        model = "bad-revise-model"

        def complete(self, request: ProviderRequest) -> ProviderResult:
            payload = _review_payload("revise")
            if required_revisions == "__missing__":
                payload.pop("required_revisions")
            else:
                payload["required_revisions"] = required_revisions
            return ProviderResult(
                ok=True,
                response=ProviderResponse(
                    text=json.dumps(payload),
                    provider=self.provider,
                    model=self.model,
                    usage=ProviderUsage(input_tokens=10, output_tokens=10, cost_usd=0.0),
                ),
            )

    repo = InMemoryRuntimeRepository()
    submission = _calibration_submission(repo, recommendation="revise")
    engine = WorkflowEngine(provider=BadReviseProvider())
    intake_job = repo.enqueue_job(RuntimeJob(target_object_id=submission.id, stage=Stage.INTAKE, payload={"domain_slug": "longevity"}))
    engine.handle_job(intake_job, repo)
    repo.complete_job(intake_job.id)

    review_job = repo.claim_next_job()
    assert review_job is not None
    with pytest.raises(ValueError, match=expected_error):
        engine.handle_job(review_job, repo)
    assert repo.list_objects(ObjectType.REVIEW) == []


def test_missing_rubric_fields_are_rejected() -> None:
    class LegacyProvider:
        provider = "legacy"
        model = "legacy-model"

        def complete(self, request: ProviderRequest) -> ProviderResult:
            return ProviderResult(
                ok=True,
                response=ProviderResponse(
                    text=json.dumps(
                        {
                            "recommendation": "accept",
                            "review_markdown": "Legacy schema without rubric fields.",
                        }
                    ),
                    provider=self.provider,
                    model=self.model,
                    usage=ProviderUsage(input_tokens=10, output_tokens=10, cost_usd=0.0),
                ),
            )

    repo = InMemoryRuntimeRepository()
    submission = _calibration_submission(repo, recommendation="accept")
    engine = WorkflowEngine(provider=LegacyProvider())
    intake_job = repo.enqueue_job(RuntimeJob(target_object_id=submission.id, stage=Stage.INTAKE, payload={"domain_slug": "longevity"}))
    engine.handle_job(intake_job, repo)
    repo.complete_job(intake_job.id)

    review_job = repo.claim_next_job()
    assert review_job is not None
    with pytest.raises(ValueError, match="missing_rubric_scores"):
        engine.handle_job(review_job, repo)


def test_malformed_doi_fails_intake() -> None:
    repo = InMemoryRuntimeRepository()
    bundle = _valid_source_bundle()
    bundle[0]["doi"] = "not-a-doi"
    submission = repo.create_object(
        ResearchObject(
            object_type=ObjectType.SUBMISSION,
            title="Malformed DOI",
            metadata={
                "abstract": "Bounded.",
                "sections": _full_sections(),
                "source_bundle": bundle,
                "core_claims_resolved": True,
                "author_agent_id": "agent-test",
                "domain_slug": "longevity",
            },
        )
    )
    engine = WorkflowEngine()
    result = engine.handle_job(RuntimeJob(target_object_id=submission.id, stage=Stage.INTAKE, payload={"domain_slug": "longevity"}), repo)
    assert result.get("terminal_decision") == Decision.REJECT.value
    decision = repo.list_objects(ObjectType.DECISION)[0]
    assert "doi_sanity" in {f["name"] for f in decision.metadata["gate_failures"]}


def test_valid_doi_passes_intake() -> None:
    repo = InMemoryRuntimeRepository()
    bundle = _valid_source_bundle()
    bundle[0]["doi"] = "10.1234/example.2024"
    submission = repo.create_object(
        ResearchObject(
            object_type=ObjectType.SUBMISSION,
            title="Valid DOI",
            metadata={
                "abstract": "Bounded.",
                "sections": _full_sections(),
                "source_bundle": bundle,
                "core_claims_resolved": True,
                "author_agent_id": "agent-test",
                "domain_slug": "longevity",
            },
        )
    )
    engine = WorkflowEngine()
    result = engine.handle_job(RuntimeJob(target_object_id=submission.id, stage=Stage.INTAKE, payload={"domain_slug": "longevity"}), repo)
    assert result.get("next_stage") == Stage.REVIEW.value
