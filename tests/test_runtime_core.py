import json

import pytest

from runtime_core.compiler import canonical_bundle_facts, compile_publication
from runtime_core.gates import run_publish_gates
from runtime_core.failure_classifier import classify_failure_reason
from runtime_core.prompts import REVIEWER_PROMPT_VERSION
from runtime_core.providers import MimoProvider, OpenRouterProvider, ProviderRequest, ProviderResponse, ProviderResult
from runtime_core.reviewer_panel import ReviewerPanel, reviewer_from_env
from runtime_core.repos import InMemoryRuntimeRepository
from runtime_core.sanitizer import sanitize_source_ledger
from runtime_core.workflow import (
    WorkflowEngine,
)

from contracts import ArticleType, Decision, FailureClass, ObjectType, ProviderUsage, ResearchObject, RuntimeJob, Stage, WorkflowContext


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
            return b'{"choices":[{"message":{"content":"{\\"recommendation\\":\\"accept\\",\\"review_markdown\\":\\"ok\\"}"}}],"usage":{"prompt_tokens":12,"completion_tokens":8},"model":"nvidia/nemotron-3-super-120b-a12b"}'

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
    assert sleeps == [0.25, 0.5]


def test_reviewer_panel_from_env_uses_mimo_nemotron_deepseek(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RESEARKA_V2_PROVIDER", "judge_panel")
    monkeypatch.delenv("RESEARKA_V2_REVIEWER_MODEL", raising=False)
    monkeypatch.delenv("RESEARKA_V2_JUDGE_MODEL", raising=False)

    provider = reviewer_from_env()

    assert isinstance(provider, ReviewerPanel)
    assert isinstance(provider.primary, MimoProvider)
    assert isinstance(provider.sparring, OpenRouterProvider)
    assert isinstance(provider.fallback, OpenRouterProvider)
    assert provider.primary.model == "mimo-v2.5-pro"
    assert provider.sparring.model == "nvidia/nemotron-3-super-120b-a12b"
    assert provider.fallback.model == "deepseek/deepseek-v4-flash"


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
        sparring=FakeProvider("openrouter", "nvidia/nemotron-3-super-120b-a12b", "reject"),
        fallback=FakeProvider("openrouter", "deepseek/deepseek-v4-flash", "revise"),
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
    assert result.response.usage.cost_usd == 0.3


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
            "deepseek/deepseek-v4-flash",
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
            "deepseek/deepseek-v4-flash",
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
            "deepseek/deepseek-v4-flash",
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


def test_workflow_stores_panel_route_metadata() -> None:
    class PanelProvider:
        provider = "reviewer-panel"
        model = "mimo-v2.5-pro|nvidia/nemotron-3-super-120b-a12b|deepseek/deepseek-v4-flash"

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
        model = "mimo-v2.5-pro|nvidia/nemotron-3-super-120b-a12b|deepseek/deepseek-v4-flash"

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
