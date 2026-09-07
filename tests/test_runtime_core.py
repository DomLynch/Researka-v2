import io
import json
import urllib.error
from datetime import datetime, timedelta, timezone

import pytest

from apps.worker.main import WorkerApp
from runtime_core.compiler import canonical_bundle_facts, compile_publication
from runtime_core.evidence_quality import (
    evidence_profile,
    publication_class,
    support_for_claim,
)
from runtime_core.judge_release import (
    build_judge_release,
    judge_release_manifest_valid,
    resolve_judge_code_sha,
)
from runtime_core.gates import run_publish_gates
from runtime_core.failure_classifier import classify_failure_reason
from runtime_core.ops import reconcile_stalled_submissions
from runtime_core.prompts import REVIEWER_PROMPT_VERSION
from runtime_core.providers import (
    DeterministicProvider,
    FallbackProvider,
    MimoProvider,
    MiniMaxProvider,
    OpenRouterProvider,
    ProviderError,
    ProviderRequest,
    ProviderResponse,
    ProviderResult,
    provider_from_env,
)
from runtime_core.publication_sidecars import publication_sources
from runtime_core.review_contract import accept_quorum_satisfied
from runtime_core.reviewer_panel import ReviewerPanel, reviewer_from_env
from runtime_core.repos import InMemoryRuntimeRepository
from runtime_core.sanitizer import sanitize_source_ledger, validate_template_structure
import runtime_core.workflow as workflow
from runtime_core.workflow import (
    WorkflowEngine,
)
from tests.support import accepted_publish_job, bound_review, sections_from_markdown

from contracts import (
    ArticleType,
    Decision,
    FailureClass,
    EventType,
    ObjectType,
    ProviderErrorClass,
    ProviderUsage,
    ResearchObject,
    RuntimeEvent,
    RuntimeJob,
    Stage,
    SubmissionPayload,
    WorkflowContext,
    publication_template_for,
    run_submission_template_checks,
)


def _full_sections(
    search_summary: str = "Legit retained summary line describing the databases, publication window, inclusion logic, and narrowing rule in enough detail to satisfy the structure gate.",
) -> dict[str, str]:
    return {
        "Research Question": "This synthesis asks a bounded, decision-relevant question about recent evidence, target populations, comparator conditions, intended outcomes, and methodological limits, and it stays narrow enough that another reviewer could reproduce the scope, publication window, inclusion logic, and decision frame without inventing missing assumptions, broadening the target claim, or silently swapping the relevant evidence category.",
        "Search Summary": search_summary,
        "Evidence Landscape": "The bundle includes both review-level and primary evidence so the reader can see the balance of stronger and more applied material, how much of the synthesis rests on reviews, and where individual studies still shape the remaining uncertainty.",
        "Key Findings": "The retained source reports bounded evidence for the scoped outcome and population, while uncertainty and study-design limits prevent a broader causal interpretation [bundle:1].",
        "Limitations": "The main limits are rapid-review scope, incomplete coverage, heterogeneity across evidence units, and the risk that a synthetic bundle omits conflicting sources that could materially change the certainty of any strong-sounding claim.",
        "Gaps Identified": "No adequately powered human RCT has tested this specific intervention for the primary endpoints reported in non-human models, leaving a translational gap between animal evidence and clinical applicability.",
        "Conclusion": "The source reports bounded evidence for the scoped outcome and population, with explicit uncertainty and no claim beyond the population, endpoint, or design that was actually studied [bundle:1].",
    }


def _valid_source_bundle() -> list[dict[str, object]]:
    years = (2024, 2023, 2022, 2021, 2020, 2024, 2023, 2022, 2021, 2019, 2018, 2017)
    evidence_types = ("review",) * 6 + ("primary",) * 6
    return [
        {
            "title": f"{evidence_type.title()} source {index}",
            "year": year,
            "evidence_type": evidence_type,
            "doi": f"10.1234/source.{index}",
            "excerpt": (
                "The retained source reports bounded evidence for the scoped outcome and population, while uncertainty "
                "and study-design limits prevent a broader causal interpretation. The source reports bounded evidence "
                "for the scoped outcome and population, with explicit uncertainty and no claim beyond the population, "
                "endpoint, or design that was actually studied."
            ),
        }
        for index, (year, evidence_type) in enumerate(
            zip(years, evidence_types, strict=True), start=1
        )
    ]


def test_claim_support_requires_explicit_evidence_span() -> None:
    source = {
        "title": "Trial",
        "evidence_span": "mortality was lower in the intervention group",
    }

    assert (
        support_for_claim("Mortality was lower in the intervention group.", [source])[
            0
        ]["support_kind"]
        == "evidence_span_match"
    )
    assert support_for_claim("The intervention may improve outcomes.", [source]) == []


def test_claim_support_resolves_submitted_citation_token() -> None:
    source = {
        "title": "Trial",
        "cited_as": "Lynch et al. 2026",
        "doi": "10.1234/trial",
        "excerpt": "The bounded evidence suggests a context-specific intervention effect in the tested population.",
    }

    support = support_for_claim(
        "The bounded evidence suggests a context-specific effect (Lynch et al. 2026).",
        [source],
    )

    assert support[0]["support_kind"] == "cited_as_match"
    assert support[0]["doi"] == "10.1234/trial"


def test_claim_support_rejects_citation_with_unrelated_receipt() -> None:
    source = {
        "title": "Trial",
        "cited_as": "Lynch et al. 2026",
        "excerpt": "The trial measured blood pressure after a short dietary intervention.",
    }

    assert (
        support_for_claim(
            "The treatment doubled survival in older adults (Lynch et al. 2026).",
            [source],
        )
        == []
    )


def test_claim_support_accepts_exact_statistic_despite_different_prose() -> None:
    source = {
        "cited_as": "Feng 2024",
        "excerpt": "There was no significant difference in the metformin group (p = 0.208).",
    }
    claim = (
        "Feng 2024 [bundle:1] reported a representative non-significant statistic "
        "P = 0.208; direction was unclear."
    )

    assert support_for_claim(claim, [source])
    source["excerpt"] = "The same endpoint was reported with p = 0.310."
    assert support_for_claim(claim, [source]) == []


@pytest.mark.parametrize("field", ["quote", "evidence_span", "excerpt"])
@pytest.mark.parametrize("subject, other", [("Semaglutide", "Tirzepatide"), ("Solar generation", "Wind generation")])
@pytest.mark.parametrize("ending", ["", ". {subject} was not studied", "; {subject} was not studied"])
def test_claim_support_rejects_wrong_subject_with_matching_numbers(field, subject, other, ending) -> None:
    claim = f"{subject} reduced the measured outcome by 12% after 26 weeks [1]."
    source = {field: f"{other} reduced the measured outcome by 12% after 26 weeks{ending.format(subject=subject)}."}

    assert support_for_claim(claim, [source]) == []
    assert support_for_claim(claim, [source], require_quantitative_agreement=True) == []
    assert support_for_claim(claim, [source], require_evidence_alignment=False)


@pytest.mark.parametrize("passage", [
    "Semaglutide did not reduce body weight by 12% after 26 weeks.",
    "Tirzepatide reduced body weight by 12% after 26 weeks. Semaglutide reduced body weight by 5% after 26 weeks.",
    "Tirzepatide reduced body weight by 12% after 26 weeks, compared with semaglutide at 5%.",
])
def test_claim_support_cannot_borrow_results_from_another_statement(passage) -> None:
    claim = "Semaglutide reduced body weight by 12% after 26 weeks [1]."

    assert support_for_claim(claim, [{"evidence_span": passage}], require_quantitative_agreement=True) == []


@pytest.mark.parametrize("passage", [
    "Semaglutide reduced body weight by 12% after 26 weeks.",
    "Semaglutide lowered body weight by 12 percent after 26 weeks.",
    "Tirzepatide was not studied. Semaglutide lowered body weight by 12 percent after 26 weeks.",
    "Semaglutide reduced body weight by 12% after 26 weeks with no serious adverse events.",
    "Body weight was reduced by 12% after 26 weeks with semaglutide.",
    "Semaglutide produced a body weight loss of 12% after 26 weeks.",
])
def test_claim_support_preserves_matching_subject_paraphrases(passage) -> None:
    claim = "Semaglutide reduced body weight by 12% after 26 weeks [1]."

    assert support_for_claim(claim, [{"evidence_span": passage}], require_quantitative_agreement=True)


def test_claim_support_ignores_author_attribution_before_subject() -> None:
    claim = "Feng 2024 [1] reported that semaglutide reduced body weight by 12% after 26 weeks."
    source = {"cited_as": "Feng 2024", "excerpt": "Semaglutide lowered body weight by 12% after 26 weeks."}

    assert support_for_claim(claim, [source], require_quantitative_agreement=True)


def test_claim_support_combines_sources_without_dropping_a_subject() -> None:
    claim = "Semaglutide and tirzepatide reduced body weight by 12% and 15%, respectively [1,2]."
    sources = [
        {"excerpt": "Semaglutide reduced body weight by 12%."},
        {"excerpt": "Tirzepatide reduced body weight by 15%."},
    ]

    assert len(support_for_claim(claim, sources, require_quantitative_agreement=True)) == 2
    sources[1]["excerpt"] = "An unrelated treatment reduced body weight by 15%."
    assert support_for_claim(claim, sources) == []
    assert support_for_claim(claim, sources, require_quantitative_agreement=True) == []


def test_trace_guard_requests_revision_for_wrong_subject_not_missing_citation() -> None:
    submission = ResearchObject(
        object_type=ObjectType.SUBMISSION,
        title="Research Synthesis: Semaglutide outcomes",
        metadata={
            "article_type": ArticleType.RESEARCH_SYNTHESIS.value,
            "abstract": "Semaglutide reduced body weight by 12% after 26 weeks in the measured adult population [1].",
            "source_bundle": [{"evidence_span": "Tirzepatide reduced body weight by 12% after 26 weeks in the measured adult population. Semaglutide was not studied."}],
        },
    )

    revisions = workflow._claim_trace_guard_revisions(submission)
    assert len(revisions) == 2
    assert json.loads(revisions[1])["status"] == "NEEDS_SEMANTIC_REVIEW"
    assert "1/1 claims identify a source; 0/1 also align" in revisions[0]


def test_claim_support_requires_numeric_and_unit_agreement_when_requested() -> None:
    source = {
        "title": "Trial",
        "cited_as": "Alpha 2026",
        "excerpt": "The intervention reduced the primary risk by 12 percent in the tested adult population.",
    }
    claim = "The intervention reduced the primary risk by 47% in the tested adult population (Alpha 2026)."

    assert support_for_claim(claim, [source])
    assert support_for_claim(claim, [source], require_quantitative_agreement=True) == []

    source["excerpt"] = (
        "The intervention reduced the primary risk by 47 percent in the tested adult population."
    )
    assert support_for_claim(claim, [source], require_quantitative_agreement=True)

    source["excerpt"] = (
        "The intervention reduced the biomarker by 47 mg/L in the tested adult population."
    )
    claim = "The intervention reduced the biomarker by 47 mg/dL in the tested adult population (Alpha 2026)."
    assert support_for_claim(claim, [source])
    assert support_for_claim(claim, [source], require_quantitative_agreement=True) == []

    source["excerpt"] = (
        "The trial reported the endpoint after a 12-month follow-up in the tested adult population."
    )
    claim = "The trial reported the endpoint after a 12-week follow-up in the tested adult population (Alpha 2026)."
    assert support_for_claim(claim, [source])
    assert support_for_claim(claim, [source], require_quantitative_agreement=True) == []


def test_judge_release_is_stable_and_prompt_bound(tmp_path, monkeypatch) -> None:
    calibration = tmp_path / "gold.json"
    calibration.write_text('{"cases":[]}')
    monkeypatch.setenv("RESEARKA_V2_CALIBRATION_PATH", str(calibration))
    inputs = {
        "provider": "reviewer-panel",
        "model": "model-b|model-a",
        "response_metadata": {"panel_models": ["model-b", "model-a"]},
    }

    first = build_judge_release(system_prompt="locked prompt", **inputs)
    calibration.write_text('{"cases":[{"id":"new-calibration"}]}')
    second = build_judge_release(system_prompt="locked prompt", **inputs)
    changed = build_judge_release(system_prompt="changed prompt", **inputs)
    changed_observed = build_judge_release(
        system_prompt="locked prompt",
        provider="reviewer-panel",
        model="model-b|model-a",
        response_metadata={"panel_models": ["model-a", "model-c"]},
    )

    assert first["id"] != second["id"]
    assert first["calibration"] != second["calibration"]
    assert first["id"] != changed["id"]
    assert first["id"] != changed_observed["id"]
    assert first["request_prompt_sha256"] != changed["request_prompt_sha256"]
    assert first["models"] == ["model-a", "model-b"]
    assert first["observed_models"] == ["model-a", "model-b"]
    assert judge_release_manifest_valid(first) is True
    assert (
        judge_release_manifest_valid({**first, "models": ["tampered-model"]}) is False
    )
    calibration_identity = first["calibration"]
    assert isinstance(calibration_identity, dict)
    assert calibration_identity["artifact"] == "gold.json"


def test_judge_code_hash_ignores_attestations_but_tracks_code(tmp_path) -> None:
    (tmp_path / "runtime_core").mkdir()
    (tmp_path / "contracts").mkdir()
    (tmp_path / "apps/runtime_api").mkdir(parents=True)
    (tmp_path / "calibration").mkdir()
    source = tmp_path / "runtime_core/judge.py"
    source.write_text("POLICY = 1\n")
    (tmp_path / "contracts/models.py").write_text("class Decision: pass\n")
    (tmp_path / "apps/runtime_api/app.py").write_text("STATUS = 'ok'\n")

    baseline = resolve_judge_code_sha(tmp_path)
    (tmp_path / "calibration/receipt.json").write_text('{"release":"attested"}\n')

    assert resolve_judge_code_sha(tmp_path) == baseline

    source.write_text("POLICY = 2\n")

    assert resolve_judge_code_sha(tmp_path) != baseline


def test_publication_sources_prefer_bundle_and_parse_only_reference_receipts() -> None:
    publication = ResearchObject(
        object_type=ObjectType.PUBLICATION,
        title="Sidecar source test",
        body_markdown=(
            "## Methods\n\n- **Not a citation.** Ordinary method bullet.\n\n"
            "## References\n\n- **Legacy trial.** 2025. DOI: 10.1234/legacy."
        ),
    )
    submission = ResearchObject(
        object_type=ObjectType.SUBMISSION,
        title="Submitted source test",
        metadata={
            "source_bundle": [{"title": "Submitted trial", "doi": "10.1234/submitted"}]
        },
    )

    assert [row["doi"] for row in publication_sources(publication, submission)] == [
        "10.1234/submitted"
    ]
    assert [row["doi"] for row in publication_sources(publication)] == [
        "10.1234/legacy"
    ]


def _review_payload(
    recommendation: str = "accept",
    **overrides: object,
) -> dict[str, object]:
    if recommendation == "accept":
        payload: dict[str, object] = {
            "recommendation": "accept",
            "provider": "reviewer-panel",
            "accept_quorum_count": 2,
            "accept_quorum_models": ["reviewer-a", "reviewer-b"],
            "accept_quorum_identities": [
                "provider-a:reviewer-a",
                "provider-b:reviewer-b",
            ],
            "accept_quorum_providers": ["provider-a", "provider-b"],
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


class _ReviewPayloadProvider:
    def __init__(
        self, model: str, payload: dict[str, object], *, provider: str = "stub"
    ) -> None:
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


def _workflow_submission(repo: InMemoryRuntimeRepository) -> ResearchObject:
    return repo.create_object(
        ResearchObject(
            object_type=ObjectType.SUBMISSION,
            title="Idempotent workflow submission",
            metadata={
                "abstract": "A bounded rapid evidence synthesis.",
                "article_type": ArticleType.RAPID_EVIDENCE_SYNTHESIS.value,
                "sections": _full_sections(),
                "source_bundle": _valid_source_bundle(),
                "core_claims_resolved": True,
            },
        )
    )


def _authenticated_workflow_submission(
    repo: InMemoryRuntimeRepository, agent_id: str = "agent-demo"
) -> ResearchObject:
    submission = _workflow_submission(repo)
    updated = repo.update_object_metadata(
        submission.id,
        {
            **submission.metadata,
            "author_agent_id": agent_id,
            "authenticated_agent_id": agent_id,
            "identity_source": "api_key",
        },
    )
    assert updated is not None
    return updated


def test_review_and_editorial_replay_do_not_duplicate_records() -> None:
    repo = InMemoryRuntimeRepository()
    submission = _workflow_submission(repo)
    provider = _ReviewPayloadProvider("reviewer", _review_payload("revise"))
    engine = WorkflowEngine(provider=provider)
    review_job = RuntimeJob(
        id="review-operation", target_object_id=submission.id, stage=Stage.REVIEW
    )

    first_review = engine._run_review(review_job, repo)
    replayed_review = engine._run_review(review_job, repo)

    assert replayed_review["deduped"] is True
    assert replayed_review["created_object_id"] == first_review["created_object_id"]
    assert provider.calls == 1
    assert len(repo.children_of(submission.id, ObjectType.REVIEW)) == 1
    assert len([job for job in repo.queued_jobs() if job.stage == Stage.EDITORIAL]) == 1

    editorial_job = RuntimeJob(
        id="editorial-operation",
        target_object_id=submission.id,
        stage=Stage.EDITORIAL,
        payload={"review_id": first_review["created_object_id"]},
    )
    first_decision = engine._run_editorial(editorial_job, repo)
    replayed_decision = engine._run_editorial(editorial_job, repo)

    assert replayed_decision["deduped"] is True
    assert replayed_decision["created_object_id"] == first_decision["created_object_id"]
    assert len(repo.children_of(submission.id, ObjectType.DECISION)) == 1


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


def test_publish_uses_canonical_sections_not_alternate_body() -> None:
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
    submission = repo.create_object(
        ResearchObject(
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
        )
    )

    result = WorkflowEngine()._run_publish(accepted_publish_job(repo, submission), repo)

    publication = repo.get_object(result["publication_id"])
    assert publication is not None
    assert publication.body_markdown.startswith("## Research Question")
    assert "# Full manuscript" not in publication.body_markdown


def test_publish_preserves_submission_audit_hashes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RESEARKA_AUTO_LIST_AGENT_IDS", "agent-v3-full-paper")
    repo = InMemoryRuntimeRepository()
    submission = repo.create_object(
        ResearchObject(
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
                "client_claimed_content_hash": "sha256:paper",
                "client_claimed_submission_payload_hash": "sha256:payload",
                "client_claimed_source_citation_hash": "sha256:sources",
                "client_claimed_submission_identity_key": "sha256:identity",
                "author_signature": "sha256:paper",
            },
        )
    )
    monkeypatch.setattr(
        workflow,
        "_mint_publication_doi",
        lambda repository, publication: {
            "doi": "10.17605/OSF.IO/TEST1",
            "doi_status": "minted",
            "osf_status": "minted",
            "osf_package_files": {"manifest-sha256.json": {"sha256": "abc"}},
            "content_hash": "sha256:recomputed-provider-hash",
        },
    )

    result = WorkflowEngine()._run_publish(accepted_publish_job(repo, submission), repo)

    publication = repo.get_object(result["publication_id"])
    assert publication is not None
    osf_job = repo.claim_next_job(target_object_id=publication.id)
    assert osf_job is not None and osf_job.stage is Stage.OSF_DEPOSIT
    WorkflowEngine()._run_osf_deposit(osf_job, repo)
    publication = repo.get_object(publication.id)
    assert publication is not None
    assert publication.metadata["source_submission_id"] == submission.id
    assert publication.metadata["run_id"] == "synthesis-topic-v06-test"
    assert publication.metadata["client_claimed_content_hash"] == "sha256:paper"
    assert (
        publication.metadata["client_claimed_submission_payload_hash"]
        == "sha256:payload"
    )
    assert (
        publication.metadata["client_claimed_source_citation_hash"] == "sha256:sources"
    )
    assert (
        publication.metadata["client_claimed_submission_identity_key"]
        == "sha256:identity"
    )
    assert publication.metadata["doi_status"] == "minted"


def test_publish_does_not_dedupe_by_client_claimed_identity() -> None:
    repo = InMemoryRuntimeRepository()
    existing = repo.create_object(
        ResearchObject(
            object_type=ObjectType.PUBLICATION,
            title="Earlier public title",
            metadata={"client_claimed_submission_identity_key": "sha256:identity"},
        )
    )
    submission = repo.create_object(
        ResearchObject(
            object_type=ObjectType.SUBMISSION,
            title="Retitled public manuscript",
            metadata={
                "abstract": "A structured abstract for a public research synthesis.",
                "article_type": ArticleType.RAPID_EVIDENCE_SYNTHESIS.value,
                "sections": _full_sections(),
                "source_bundle": _valid_source_bundle(),
                "core_claims_resolved": True,
                "client_claimed_submission_identity_key": "sha256:identity",
            },
        )
    )

    result = WorkflowEngine()._run_publish(accepted_publish_job(repo, submission), repo)

    assert result["publication_id"] != existing.id
    assert result["deduped"] is False


def test_publish_idempotency_still_requires_current_accept_proof() -> None:
    repo = InMemoryRuntimeRepository()
    submission = repo.create_object(
        ResearchObject(
            object_type=ObjectType.SUBMISSION,
            title="Existing publication must remain authorised",
            metadata={
                "abstract": "A structured abstract for a public research synthesis.",
                "article_type": ArticleType.RAPID_EVIDENCE_SYNTHESIS.value,
                "sections": _full_sections(),
                "source_bundle": _valid_source_bundle(),
                "core_claims_resolved": True,
            },
        )
    )
    repo.create_object(
        ResearchObject(
            object_type=ObjectType.PUBLICATION,
            parent_object_id=submission.id,
            title=submission.title,
        )
    )

    with pytest.raises(ValueError, match="current_accept_decision_missing"):
        WorkflowEngine()._run_publish(
            RuntimeJob(target_object_id=submission.id, stage=Stage.PUBLISH),
            repo,
        )


@pytest.mark.parametrize(
    ("metadata", "expected_stage"),
    [
        ({}, Stage.OSF_DEPOSIT),
        (
            {
                "doi_status": "minted",
                "osf_package_files": {"manifest-sha256.json": {"sha256": "abc"}},
            },
            Stage.DW_DELIVERY,
        ),
        (
            {
                "doi_status": "minted",
                "osf_package_files": {"manifest-sha256.json": {"sha256": "abc"}},
                "dw_status": "registered",
            },
            Stage.PUBLICATION_FINALIZE,
        ),
    ],
)
def test_blocked_publication_resumes_at_first_incomplete_delivery_stage(
    metadata: dict[str, object],
    expected_stage: Stage,
) -> None:
    repo = InMemoryRuntimeRepository()
    publication = repo.create_object(
        ResearchObject(
            object_type=ObjectType.PUBLICATION,
            title="Resumable publication",
            metadata={
                "requested_public_visibility": "listed",
                "public_visibility": "provisional",
                "publication_state": "PUBLISH_BLOCKED_EXTERNAL",
                **metadata,
            },
        )
    )

    result = workflow._resume_publication_delivery(repo, publication)

    assert result["resumed_stage"] == expected_stage.value
    assert [job.stage for job in repo.queued_jobs()] == [expected_stage]


def test_authenticated_active_agent_does_not_need_auto_list_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RESEARKA_AUTO_LIST_AGENT_IDS", raising=False)
    monkeypatch.delenv("RESEARKA_DISABLED_AGENT_IDS", raising=False)
    repo = InMemoryRuntimeRepository()
    repo.create_api_key("agent-demo")
    submission = _authenticated_workflow_submission(repo)

    result = WorkflowEngine()._run_publish(accepted_publish_job(repo, submission), repo)

    publication = repo.get_object(result["publication_id"])
    assert publication is not None
    assert publication.metadata["requested_public_visibility"] == "listed"
    assert publication.metadata["publication_state"] == "PUBLISHING"
    assert [job.stage for job in repo.queued_jobs()] == [Stage.OSF_DEPOSIT]


def test_disabled_authenticated_agent_remains_quarantined(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RESEARKA_DISABLED_AGENT_IDS", "agent-demo")
    repo = InMemoryRuntimeRepository()
    repo.create_api_key("agent-demo")
    submission = _authenticated_workflow_submission(repo)

    result = WorkflowEngine()._run_publish(accepted_publish_job(repo, submission), repo)

    publication = repo.get_object(result["publication_id"])
    assert publication is not None
    assert publication.metadata["publication_state"] == "ACCEPTED_QUARANTINED"
    assert repo.queued_jobs() == []


def test_reconciler_releases_accepted_quarantine_after_agent_activation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RESEARKA_AUTO_LIST_AGENT_IDS", raising=False)
    repo = InMemoryRuntimeRepository()
    submission = _authenticated_workflow_submission(repo)
    result = WorkflowEngine()._run_publish(accepted_publish_job(repo, submission), repo)
    publication = repo.get_object(result["publication_id"])
    assert publication is not None
    assert publication.metadata["publication_state"] == "ACCEPTED_QUARANTINED"
    repo.create_api_key("agent-demo")
    monkeypatch.setattr(workflow, "_verified_billing_waiver", lambda *_args: True)
    assert workflow.recover_publication_delivery(repo, publication) is None
    monkeypatch.setattr(workflow, "_verified_billing_waiver", lambda *_args: False)

    repaired = reconcile_stalled_submissions(
        repo, now=publication.created_at + timedelta(minutes=3)
    )

    assert [job.stage for job in repaired] == [Stage.OSF_DEPOSIT]
    recovered = repo.get_object(publication.id)
    assert recovered is not None
    assert recovered.metadata["publication_state"] == "PUBLISHING"
    assert recovered.metadata["requested_public_visibility"] == "listed"


@pytest.mark.parametrize("state", ["PUBLISH_BLOCKED_EXTERNAL", "PUBLISHING"])
def test_reconciler_retries_external_delivery_once_without_duplicate_work(
    monkeypatch: pytest.MonkeyPatch,
    state: str,
) -> None:
    monkeypatch.delenv("RESEARKA_DISABLED_AGENT_IDS", raising=False)
    repo = InMemoryRuntimeRepository()
    repo.create_api_key("agent-demo")
    submission = _authenticated_workflow_submission(repo)
    result = WorkflowEngine()._run_publish(accepted_publish_job(repo, submission), repo)
    publication = repo.get_object(result["publication_id"])
    assert publication is not None
    failed_job = repo.claim_next_job(target_object_id=publication.id)
    assert failed_job is not None and failed_job.stage is Stage.OSF_DEPOSIT
    failure_time = datetime.now(timezone.utc)
    repo.fail_job(
        failed_job.id,
        reason="system_unavailable:osf:timeout",
        lease_token=failed_job.lease_token,
        event=RuntimeEvent(
            event_type=EventType.JOB_FAILED,
            target_object_id=publication.id,
            job_id=failed_job.id,
            payload={"stage": Stage.OSF_DEPOSIT.value, "terminal": True},
            ts=failure_time,
        ),
    )
    repo.update_object_metadata(
        publication.id,
        {**publication.metadata, "publication_state": state},
    )

    repaired = reconcile_stalled_submissions(
        repo, now=failure_time + timedelta(minutes=3)
    )
    duplicate = reconcile_stalled_submissions(
        repo, now=failure_time + timedelta(minutes=6)
    )

    assert [job.stage for job in repaired] == [Stage.OSF_DEPOSIT]
    assert duplicate == []
    recovered = repo.get_object(publication.id)
    assert recovered is not None
    assert recovered.metadata["delivery_recovery_count"] == 1


def test_publication_recovery_is_bounded_and_never_retries_integrity_blocks() -> None:
    repo = InMemoryRuntimeRepository()
    exhausted = ResearchObject(
        object_type=ObjectType.PUBLICATION,
        title="Exhausted external delivery",
        metadata={
            "publication_state": "PUBLISH_BLOCKED_EXTERNAL",
            "delivery_recovery_count": 3,
        },
    )
    integrity_blocked = ResearchObject(
        object_type=ObjectType.PUBLICATION,
        title="Integrity blocked",
        metadata={"publication_state": "PUBLISH_BLOCKED_INTEGRITY"},
    )

    assert workflow.recover_publication_delivery(repo, exhausted) is None
    assert workflow.recover_publication_delivery(repo, integrity_blocked) is None
    assert repo.queued_jobs() == []


def test_integrity_blocked_publication_cannot_resume_delivery() -> None:
    repo = InMemoryRuntimeRepository()
    publication = repo.create_object(
        ResearchObject(
            object_type=ObjectType.PUBLICATION,
            title="Integrity-blocked publication",
            metadata={
                "requested_public_visibility": "listed",
                "public_visibility": "provisional",
                "publication_state": "PUBLISH_BLOCKED_INTEGRITY",
            },
        )
    )

    result = workflow._resume_publication_delivery(repo, publication)

    assert result["next_jobs"] == 0
    assert repo.queued_jobs() == []


def test_external_delivery_revalidates_accept_quorum_before_side_effect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RESEARKA_AUTO_LIST_AGENT_IDS", "agent-demo")
    repo = InMemoryRuntimeRepository()
    submission = repo.create_object(
        ResearchObject(
            object_type=ObjectType.SUBMISSION,
            title="Lineage-guarded publication",
            metadata={
                "abstract": "A structured abstract for a public research synthesis.",
                "article_type": ArticleType.RAPID_EVIDENCE_SYNTHESIS.value,
                "sections": _full_sections(),
                "source_bundle": _valid_source_bundle(),
                "core_claims_resolved": True,
                "author_agent_id": "agent-demo",
            },
        )
    )
    result = WorkflowEngine()._run_publish(accepted_publish_job(repo, submission), repo)
    publication = repo.get_object(result["publication_id"])
    assert publication is not None
    review = repo.get_object(str(publication.metadata["review_id"]))
    assert review is not None
    repo.update_object_metadata(
        review.id, {**review.metadata, "accept_quorum_count": 1}
    )
    monkeypatch.setattr(
        workflow,
        "_mint_publication_doi",
        lambda *_args, **_kwargs: pytest.fail(
            "OSF side effect ran without valid quorum"
        ),
    )
    osf_job = repo.claim_next_job(target_object_id=publication.id)
    assert osf_job is not None and osf_job.stage is Stage.OSF_DEPOSIT

    with pytest.raises(ValueError, match="accept_quorum_missing"):
        WorkflowEngine()._run_osf_deposit(osf_job, repo)


def test_external_delivery_rejects_decision_bound_to_different_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RESEARKA_AUTO_LIST_AGENT_IDS", "agent-demo")
    repo = InMemoryRuntimeRepository()
    submission = _workflow_submission(repo)
    updated_submission = repo.update_object_metadata(
        submission.id, {**submission.metadata, "author_agent_id": "agent-demo"}
    )
    assert updated_submission is not None
    submission = updated_submission
    result = WorkflowEngine()._run_publish(accepted_publish_job(repo, submission), repo)
    publication = repo.get_object(result["publication_id"])
    assert publication is not None
    decision = repo.get_object(str(publication.metadata["decision_id"]))
    assert decision is not None
    other_review = bound_review(repo, submission, {"recommendation": "accept"})
    repo.update_object_metadata(
        decision.id, {**decision.metadata, "review_id": other_review.id}
    )
    monkeypatch.setattr(
        workflow,
        "_mint_publication_doi",
        lambda *_args, **_kwargs: pytest.fail("OSF ran with mismatched review lineage"),
    )
    osf_job = repo.claim_next_job(target_object_id=publication.id)
    assert osf_job is not None and osf_job.stage is Stage.OSF_DEPOSIT

    with pytest.raises(ValueError, match="publication_lineage_invalid"):
        WorkflowEngine()._run_osf_deposit(osf_job, repo)


def test_workflow_rejects_stale_lease_before_stage_execution() -> None:
    repo = InMemoryRuntimeRepository(lease_ttl_seconds=-1)
    queued = repo.enqueue_job(
        RuntimeJob(target_object_id="missing", stage=Stage.REVIEW)
    )
    stale = repo.claim_next_job()
    assert stale is not None and stale.id == queued.id
    stale = stale.model_copy(deep=True)
    current = repo.claim_next_job()
    assert current is not None and current.lease_token > stale.lease_token

    with pytest.raises(RuntimeError, match="stale_job_lease"):
        WorkflowEngine().handle_job(stale, repo)


def test_reject_is_terminal() -> None:
    engine = WorkflowEngine()
    context = WorkflowContext(target_object_id="obj-4", domain_slug="longevity")
    outcome = engine.plan_from_editorial(context, Decision.REJECT)
    assert outcome.terminal_decision == Decision.REJECT
    assert outcome.next_jobs == []


def test_compile_publication_flags_leakage_before_sanitizing() -> None:
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
    assert (
        next(gate for gate in artifact.gates if gate.name == "leakage_blocker").passed
        is False
    )


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
                        {
                            "doi": f"10.1000/alpha-{index}",
                            "title": f"Reserve threshold paper {index}",
                            "source_fact": {
                                "canonical_phrase": "The cited source reports a bounded reserve threshold signal with clear limits."
                            },
                        }
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
            "excerpt": "The cited source reports a bounded reserve threshold signal with clear limits.",
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
        sections=payload.sections,
        source_bundle=payload.source_bundle,
        article_type=payload.article_type.value,
    )
    assert artifact.body_markdown.startswith("## Evidence Landscape")
    assert "# Alpha memo" in artifact.body_markdown


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("source_bundle", [{"excerpt": "x" * 2_000_001}], "source_bundle_too_large"),
        ("evidence_bundle", {"receipt": "x" * 2_000_001}, "evidence_bundle_too_large"),
    ],
)
def test_submission_payload_bounds_nested_bundles(
    field: str, value: object, error: str
) -> None:
    with pytest.raises(ValueError, match=error):
        SubmissionPayload(
            title="Bounded payload",
            abstract="Bounded abstract",
            author_agent_id="agent-test",
            **{field: value},
        )


def test_alpha_memo_with_four_sources_fails_public_intake_gate() -> None:
    results = run_submission_template_checks(
        sections={},
        source_bundle=[
            {
                "title": f"Narrow alpha source {index}",
                "doi": f"10.1000/narrow-{index}",
                "evidence_type": "primary",
                "excerpt": "This source provides a bounded narrow alpha signal for the tested endpoint.",
            }
            for index in range(1, 5)
        ],
        article_type=ArticleType.ALPHA_MEMO.value,
    )
    failures = {gate.name for gate in results if not gate.passed}
    minimum_citations = next(
        gate for gate in results if gate.name == "minimum_citations"
    )

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
        alpha_exception_trusted=True,
    )

    failures = {gate.name for gate in results if not gate.passed}

    assert "minimum_citations" not in failures


def test_alpha_source_exception_requires_server_authenticated_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RESEARKA_ALPHA_EXCEPTION_AGENT_IDS", "agent-v4,agent-v6")
    submission = ResearchObject(
        object_type=ObjectType.SUBMISSION,
        title="Authenticated alpha submission",
        metadata={
            "author_agent_id": "agent-v4",
            "authenticated_agent_id": "agent-v4",
            "identity_source": "api_key",
        },
    )

    assert workflow._alpha_exception_trusted(submission)
    submission.metadata["authenticated_agent_id"] = "agent-attacker"
    assert not workflow._alpha_exception_trusted(submission)
    submission.metadata["authenticated_agent_id"] = "agent-v4"
    submission.metadata["identity_source"] = "payload"
    assert not workflow._alpha_exception_trusted(submission)


def test_alpha_memo_single_source_cannot_use_structural_exception() -> None:
    results = run_submission_template_checks(
        sections={},
        source_bundle=[
            {"title": "Single source", "doi": "10.1000/one", "evidence_type": "primary"}
        ],
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


def test_alpha_memo_raw_title_fails_public_novelty_gate() -> None:
    results = run_submission_template_checks(
        title="desk",
        sections={
            "Evidence Landscape": "This memo states one bounded receipt-backed signal with clear limits."
        },
        source_bundle=[
            {
                "title": f"Desk source {index}",
                "doi": f"10.1000/desk-{index}",
                "evidence_type": "primary",
            }
            for index in range(1, 6)
        ],
        article_type=ArticleType.ALPHA_MEMO.value,
    )

    novelty_gate = next(gate for gate in results if gate.name == "alpha_title_novelty")

    assert novelty_gate.passed is False
    assert "human-readable" in novelty_gate.reason


def test_alpha_memo_specific_title_passes_public_novelty_gate() -> None:
    results = run_submission_template_checks(
        title="Desk interventions split posture and productivity signals",
        sections={
            "Evidence Landscape": "This memo states one bounded receipt-backed tension with a falsifiable gap."
        },
        source_bundle=[
            {
                "title": f"Desk source {index}",
                "doi": f"10.1000/desk-{index}",
                "evidence_type": "primary",
            }
            for index in range(1, 6)
        ],
        article_type=ArticleType.ALPHA_MEMO.value,
    )

    novelty_gate = next(gate for gate in results if gate.name == "alpha_title_novelty")

    assert novelty_gate.passed is True


def test_memo_prefix_does_not_mask_title_topic_anchors() -> None:
    results = run_submission_template_checks(
        title="Memo: Desk interventions split posture and productivity signals",
        sections={
            "Evidence Landscape": (
                "| Source | Finding | Endpoint |\n"
                "| --- | --- | --- |\n"
                "| Desk intervention study | One bounded signal | posture |\n"
            )
        },
        source_bundle=[
            {
                "title": f"Desk source {index}",
                "doi": f"10.1000/desk-{index}",
                "evidence_type": "primary",
            }
            for index in range(1, 6)
        ],
        article_type=ArticleType.EVIDENCE_MAP.value,
    )

    coherence_gate = next(gate for gate in results if gate.name == "topic_coherence")

    assert coherence_gate.passed is True


def test_alpha_reviewer_prompt_requires_title_source_alignment() -> None:
    prompt = WorkflowEngine()._review_system_prompt(ArticleType.ALPHA_MEMO.value)

    assert "title/source alignment" in prompt
    assert "metformin memo relying on a dapagliflozin receipt" in prompt
    assert (
        "resistance-training memo backed only by sprint/heat cycling receipts" in prompt
    )
    assert "require merge or narrower differentiation" in prompt


def test_alpha_claim_trace_guard_requires_exact_source_token() -> None:
    submission = ResearchObject(
        object_type=ObjectType.SUBMISSION,
        title="Metformin longevity signal",
        metadata={
            "article_type": ArticleType.ALPHA_MEMO.value,
            "abstract": (
                "The bounded evidence suggests a context-specific metformin longevity signal, but the result remains "
                "hypothesis-generating and does not establish clinical benefit across populations or endpoints."
            ),
            "source_bundle": [
                {
                    "title": "Metformin trial",
                    "doi": "10.1234/metformin",
                    "cited_as": "Lynch 2026",
                    "excerpt": "Metformin produced a context-specific longevity signal in the tested population.",
                }
            ],
        },
    )

    assert workflow._claim_trace_guard_revisions(submission)
    submission.metadata["abstract"] += " (Lynch 2026)."
    assert workflow._claim_trace_guard_revisions(submission) == []


def test_claim_trace_guard_rejects_claimless_memo() -> None:
    submission = ResearchObject(
        object_type=ObjectType.SUBMISSION,
        title="Short memo",
        metadata={
            "article_type": ArticleType.ALPHA_MEMO.value,
            "abstract": "A short note.",
            "source_bundle": [{"title": "Source", "doi": "10.1234/source"}],
        },
    )

    assert workflow._claim_trace_guard_revisions(submission) == [
        "Add at least one substantive, source-traceable claim before acceptance."
    ]


def test_research_synthesis_trace_guard_requires_eighty_percent_exact() -> None:
    submission = ResearchObject(
        object_type=ObjectType.SUBMISSION,
        title="Research Synthesis: bounded outcomes",
        metadata={
            "article_type": ArticleType.RESEARCH_SYNTHESIS.value,
            "abstract": "\n".join(
                [
                    "The evidence supports a bounded endpoint-specific improvement while preserving uncertainty and population limits (Alpha 2026).",
                    "The evidence also suggests a second outcome remains context-dependent and does not justify a broad clinical recommendation.",
                ]
            ),
            "source_bundle": [
                {
                    "title": "Alpha trial",
                    "doi": "10.1234/alpha",
                    "cited_as": "Alpha 2026",
                    "excerpt": "The tested intervention produced a bounded endpoint-specific improvement with population limits.",
                },
                {
                    "title": "Beta trial",
                    "doi": "10.1234/beta",
                    "cited_as": "Beta 2026",
                    "excerpt": "The second outcome remained context-dependent and did not justify a broad clinical recommendation.",
                },
            ],
        },
    )

    assert workflow._claim_trace_guard_revisions(submission)
    submission.metadata["abstract"] += " (Beta 2026)."
    assert workflow._claim_trace_guard_revisions(submission) == []


def test_claim_trace_guard_accepts_numeric_and_pmid_citations() -> None:
    submission = ResearchObject(
        object_type=ObjectType.SUBMISSION,
        title="Research Synthesis: bounded outcomes",
        metadata={
            "article_type": ArticleType.RESEARCH_SYNTHESIS.value,
            "abstract": "\n".join(
                [
                    "The evidence supports a bounded endpoint-specific improvement in the tested population [1].",
                    "The evidence suggests the second outcome remains uncertain outside that population (PMID: 22222222).",
                ]
            ),
            "source_bundle": [
                {
                    "title": "Alpha trial",
                    "excerpt": "The tested population had a bounded endpoint-specific improvement.",
                },
                {
                    "title": "Beta trial",
                    "pmid": "22222222",
                    "excerpt": "The second outcome remained uncertain outside the tested population.",
                },
            ],
        },
    )

    assert workflow._claim_trace_guard_revisions(submission) == []
    submission.metadata["source_bundle"][0]["excerpt"] = (
        "An unrelated source that does not support the claim."
    )
    revision = workflow._claim_trace_guard_revisions(submission)[0]
    assert "2/2 claims identify a source" in revision
    assert "1/2 also align with its evidence text" in revision


def test_claim_trace_guard_ignores_markdown_table_structure() -> None:
    submission = ResearchObject(
        object_type=ObjectType.SUBMISSION,
        title="Research Synthesis: bounded outcome",
        metadata={
            "article_type": ArticleType.RESEARCH_SYNTHESIS.value,
            "sections": {
                "Results": "\n".join(
                    [
                        "| Evidence domain | Evidence support summary | Main limitation |",
                        "| --- | --- | --- |",
                        "| Biomarker | The evidence supports a bounded signal across the retained corpus | Sparse data |",
                        "The evidence supports a bounded endpoint-specific improvement in the tested population (Alpha 2026).",
                    ]
                )
            },
            "source_bundle": [
                {
                    "title": "Alpha trial",
                    "cited_as": "Alpha 2026",
                    "excerpt": "The tested population had a bounded endpoint-specific improvement.",
                }
            ],
        },
    )

    assert workflow._claim_trace_guard_revisions(submission) == []


def test_claim_trace_guard_checks_abstract_numbers_against_evidence() -> None:
    source = {
        "title": "Alpha trial",
        "doi": "10.1234/alpha",
        "cited_as": "Alpha 2026",
        "excerpt": "The intervention reduced the primary risk by 12 percent in the tested adult population.",
    }
    submission = ResearchObject(
        object_type=ObjectType.SUBMISSION,
        title="Research Synthesis: bounded outcome",
        metadata={
            "article_type": ArticleType.RESEARCH_SYNTHESIS.value,
            "abstract": (
                "The intervention reduced the primary risk by 47% in the tested adult population, "
                "while uncertainty remains for other populations (Alpha 2026)."
            ),
            "source_bundle": [source],
        },
    )

    revisions = workflow._claim_trace_guard_revisions(submission)
    assert revisions[:1] == [
        "Align every number and unit in the abstract and conclusion with its cited evidence span; "
        "0/1 quantitative claims agree."
    ]
    assert json.loads(revisions[1])["sources"] == ["10.1234/alpha"]

    source["excerpt"] = (
        "The intervention reduced the primary risk by 47 percent in the tested adult population."
    )
    assert workflow._claim_trace_guard_revisions(submission) == []


def test_research_synthesis_class_requires_directness_and_risk_appraisal() -> None:
    sources = [
        {
            "title": f"Direct trial {index}",
            "evidence_type": "primary",
            "directness": "direct clinical",
            "risk_of_bias": "low" if index < 4 else "not appraised",
        }
        for index in range(5)
    ]
    profile = evidence_profile(
        text="The evidence supports a bounded effect [bundle:1].",
        source_bundle=sources,
    )

    assert profile["directness_coverage"] == 1.0
    assert profile["risk_of_bias_coverage"] == 0.8
    assert (
        publication_class(
            article_type=ArticleType.RESEARCH_SYNTHESIS.value,
            title="Research Synthesis: bounded effect",
            profile=profile,
        )
        == "research_synthesis"
    )

    sources[-2]["risk_of_bias"] = "not appraised"
    profile = evidence_profile(
        text="The evidence supports a bounded effect [bundle:1].", source_bundle=sources
    )
    assert (
        publication_class(
            article_type=ArticleType.RESEARCH_SYNTHESIS.value,
            title="Research Synthesis: bounded effect",
            profile=profile,
        )
        == "adjacent_evidence_brief"
    )

    sources[-2]["risk_of_bias"] = "low"
    sources[-1]["directness"] = "unknown"
    profile = evidence_profile(
        text="The evidence supports a bounded effect [bundle:1].", source_bundle=sources
    )
    assert profile["directness_coverage"] == 0.8
    assert (
        publication_class(
            article_type=ArticleType.RESEARCH_SYNTHESIS.value,
            title="Research Synthesis: bounded effect",
            profile=profile,
        )
        == "research_synthesis"
    )

    sources[-2]["directness"] = "not extracted"
    profile = evidence_profile(
        text="The evidence supports a bounded effect [bundle:1].", source_bundle=sources
    )
    assert (
        publication_class(
            article_type=ArticleType.RESEARCH_SYNTHESIS.value,
            title="Research Synthesis: bounded effect",
            profile=profile,
        )
        == "adjacent_evidence_brief"
    )


def test_alpha_accept_with_unsupported_title_anchor_becomes_revise() -> None:
    repo = InMemoryRuntimeRepository()
    engine = WorkflowEngine()
    submission = repo.create_object(
        ResearchObject(
            object_type=ObjectType.SUBMISSION,
            title="cold water immersion resistance training adaptation",
            metadata={
                "article_type": ArticleType.ALPHA_MEMO.value,
                "source_bundle": [
                    {
                        "title": "Cold-water immersion after sprint-interval training affects K+ transport proteins",
                        "doi": "10.1152/japplphysiol.00259.2018",
                        "evidence_type": "primary",
                        "excerpt": "Cold-water immersion after sprint-interval training affected potassium transport proteins.",
                    },
                    {
                        "title": "Cold-water recovery during heat-based cycling training changes session load",
                        "doi": "10.1123/ijspp.2019-0313",
                        "evidence_type": "primary",
                    },
                ],
                "abstract": "Cold-water immersion after sprint-interval training affected potassium transport proteins under the tested recovery protocol, but the finding remains bounded to that training context [bundle:1].",
            },
        )
    )
    review = bound_review(
        repo,
        submission,
        {"article_type": ArticleType.ALPHA_MEMO.value, **_review_payload("accept")},
    )

    outcome = engine.handle_job(
        RuntimeJob(
            target_object_id=submission.id,
            stage=Stage.EDITORIAL,
            payload={"review_id": review.id},
        ),
        repo,
    )
    decision = repo.get_object(str(outcome["created_object_id"]))

    assert outcome["terminal_decision"] == Decision.REVISE.value
    assert outcome["next_jobs"] == 0
    assert decision is not None
    assert decision.metadata["decision"] == Decision.REVISE.value
    assert (
        "unsupported title anchors: resistance"
        in decision.metadata["alpha_accept_guard"][0]
    )
    assert (
        decision.metadata["required_revisions"]
        == decision.metadata["alpha_accept_guard"]
    )


def test_alpha_accept_ignores_null_topic_anchor() -> None:
    repo = InMemoryRuntimeRepository()
    engine = WorkflowEngine()
    source_bundle = [
        {
            "title": "Does Cold-Water Immersion After Strength Training Attenuate Training Adaptation?",
            "doi": "10.1123/ijspp.2019-0965",
            "evidence_type": "primary",
            "excerpt": "Cold-water immersion after strength training may attenuate training adaptation in the tested population.",
        },
        {
            "title": "Strength Training Adaptations After Cold-Water Immersion",
            "doi": "10.1519/JSC.0000000000000434",
            "evidence_type": "primary",
        },
    ]
    submission = repo.create_object(
        ResearchObject(
            object_type=ObjectType.SUBMISSION,
            title="Does Cold-Water Immersion After Strength Training Attenuate Training Adaptation?",
            metadata={
                "article_type": ArticleType.ALPHA_MEMO.value,
                "source_bundle": source_bundle,
                "topic": None,
                "abstract": "Cold-water immersion after strength training may attenuate training adaptation in the tested population, but the source does not establish a universal recovery effect [bundle:1].",
            },
        )
    )
    review = bound_review(
        repo,
        submission,
        {"article_type": ArticleType.ALPHA_MEMO.value, **_review_payload("accept")},
    )

    outcome = engine.handle_job(
        RuntimeJob(
            target_object_id=submission.id,
            stage=Stage.EDITORIAL,
            payload={"review_id": review.id},
        ),
        repo,
    )
    decision = repo.get_object(str(outcome["created_object_id"]))

    assert outcome["terminal_decision"] == Decision.ACCEPT.value
    assert outcome["next_jobs"] == 1
    assert decision is not None
    assert decision.metadata["decision"] == Decision.ACCEPT.value
    assert "alpha_accept_guard" not in decision.metadata


def test_alpha_accept_guard_ignores_named_program_scaffold_and_acronym_fragments() -> (
    None
):
    repo = InMemoryRuntimeRepository()
    engine = WorkflowEngine()
    source_bundle = [
        {
            "title": "Fisetin senolytic pilot reports epigenetic age acceleration",
            "doi": "10.1000/fisetin-pilot",
            "evidence_type": "primary",
            "excerpt": "The fisetin senolytic pilot reported endpoint-specific epigenetic age acceleration findings.",
        },
        {
            "title": "Fisetin pregnancy cohort measures epigenetic age acceleration",
            "doi": "10.1000/fisetin-cohort",
            "evidence_type": "primary",
        },
    ]
    submission = repo.create_object(
        ResearchObject(
            object_type=ObjectType.SUBMISSION,
            title="EX-MET Program: Endpoint-Specific Fisetin Findings",
            metadata={
                "article_type": ArticleType.ALPHA_MEMO.value,
                "source_bundle": source_bundle,
                "topic": "fisetin",
                "abstract": "The fisetin senolytic pilot reported endpoint-specific epigenetic age acceleration findings, but the result remains bounded and requires independent replication [bundle:1].",
            },
        )
    )
    review = bound_review(
        repo,
        submission,
        {"article_type": ArticleType.ALPHA_MEMO.value, **_review_payload("accept")},
    )

    outcome = engine.handle_job(
        RuntimeJob(
            target_object_id=submission.id,
            stage=Stage.EDITORIAL,
            payload={"review_id": review.id},
        ),
        repo,
    )
    decision = repo.get_object(str(outcome["created_object_id"]))

    assert outcome["terminal_decision"] == Decision.ACCEPT.value
    assert outcome["next_jobs"] == 1
    assert decision is not None
    assert decision.metadata["decision"] == Decision.ACCEPT.value
    assert "alpha_accept_guard" not in decision.metadata


def test_alpha_anchor_terms_keep_scientific_hyphenated_tokens() -> None:
    assert "met" not in workflow._alpha_anchor_terms(
        "EX-MET Program: Endpoint-Specific Findings"
    )
    assert "covid" in workflow._alpha_anchor_terms("COVID-19 intervention findings")


def test_alpha_accept_guard_reads_structured_source_fact_terms() -> None:
    repo = InMemoryRuntimeRepository()
    engine = WorkflowEngine()
    source_bundle = [
        {
            "title": "Digital transformation and firm performance source",
            "doi": "10.1000/digital-transformation",
            "evidence_type": "primary",
            "source_fact": {
                "population": "Banking firms",
                "intervention": "Use of big data",
                "endpoint": "Firm performance",
            },
            "excerpt": "Use of big data was associated with firm performance in the sampled banking firms.",
        }
    ]
    submission = repo.create_object(
        ResearchObject(
            object_type=ObjectType.SUBMISSION,
            title="digital transformation: big data in banking firms context",
            metadata={
                "article_type": ArticleType.ALPHA_MEMO.value,
                "source_bundle": source_bundle,
                "topic": "digital_transformation",
                "abstract": "Use of big data was associated with firm performance in the sampled banking firms, but the result remains context-specific and does not establish universal causality [bundle:1].",
            },
        )
    )
    review = bound_review(
        repo,
        submission,
        {"article_type": ArticleType.ALPHA_MEMO.value, **_review_payload("accept")},
    )

    outcome = engine.handle_job(
        RuntimeJob(
            target_object_id=submission.id,
            stage=Stage.EDITORIAL,
            payload={"review_id": review.id},
        ),
        repo,
    )
    decision = repo.get_object(str(outcome["created_object_id"]))

    assert outcome["terminal_decision"] == Decision.ACCEPT.value
    assert outcome["next_jobs"] == 1
    assert decision is not None
    assert decision.metadata["decision"] == Decision.ACCEPT.value
    assert "alpha_accept_guard" not in decision.metadata


def test_alpha_accept_duplicate_source_pair_becomes_revise() -> None:
    repo = InMemoryRuntimeRepository()
    engine = WorkflowEngine()
    source_bundle = [
        {
            "title": "Metformin exercise adaptation source",
            "pmid": "12345678",
            "evidence_type": "primary",
        },
        {
            "title": "Exercise metformin adaptation replication",
            "pmid": "23456789",
            "evidence_type": "primary",
        },
    ]
    existing_submission = repo.create_object(
        ResearchObject(
            object_type=ObjectType.SUBMISSION,
            title="metformin exercise training adaptation",
            metadata={
                "article_type": ArticleType.ALPHA_MEMO.value,
                "source_bundle": source_bundle,
            },
        )
    )
    existing_publication = repo.create_object(
        ResearchObject(
            object_type=ObjectType.PUBLICATION,
            parent_object_id=existing_submission.id,
            title="metformin exercise training adaptation",
            metadata={"article_type": ArticleType.ALPHA_MEMO.value},
        )
    )
    submission = repo.create_object(
        ResearchObject(
            object_type=ObjectType.SUBMISSION,
            title="metformin exercise adaptation signal",
            metadata={
                "article_type": ArticleType.ALPHA_MEMO.value,
                "source_bundle": source_bundle,
            },
        )
    )
    review = bound_review(
        repo,
        submission,
        {"article_type": ArticleType.ALPHA_MEMO.value, **_review_payload("accept")},
    )

    outcome = engine.handle_job(
        RuntimeJob(
            target_object_id=submission.id,
            stage=Stage.EDITORIAL,
            payload={"review_id": review.id},
        ),
        repo,
    )
    decision = repo.get_object(str(outcome["created_object_id"]))

    assert outcome["terminal_decision"] == Decision.REVISE.value
    assert outcome["next_jobs"] == 0
    assert decision is not None
    assert decision.metadata["decision"] == Decision.REVISE.value
    assert existing_publication.id in decision.metadata["alpha_accept_guard"][0]
    assert "same stable source set" in decision.metadata["required_revisions"][0]


def test_publish_relabels_non_supportive_research_synthesis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = InMemoryRuntimeRepository()
    full_body = "\n\n".join(
        [
            "# Full manuscript",
            "## Abstract\n\nEvidence-honesty note: 24/26 retained sources are coded as null or no extracted directional signal; this corpus is non-supportive for clinical efficacy claims and hypothesis-generating only.",
            "## Introduction\n\nThe introduction defines the bounded intervention question, explains why the retained corpus is relevant, and avoids presenting indirect or null evidence as proof of clinical efficacy.",
            "## Methods\n\nThe methods describe source retrieval, screening, extraction, appraisal, synthesis, and verification in enough detail to audit the accepted manuscript.",
            "## Results\n\nThe results preserve null and heterogeneous source-level findings without claiming broad clinical efficacy, separate direct findings from adjacent evidence, and report the corpus as hypothesis-generating rather than intervention-ready [bundle:1].",
            "## Discussion\n\nThe discussion treats disagreement and indirect evidence as boundary conditions instead of a settled intervention claim.",
            "## Limitations\n\nThe limitations identify corpus boundaries, uncertainty, scope restrictions, missing endpoints, and interpretation risks that constrain public claims.",
            "## Conclusion\n\nThe conclusion states a bounded hypothesis-generating finding, avoids clinical guidance, and makes clear that the weak retained corpus supports classification discipline rather than a definitive research synthesis label [bundle:1].",
            "## References\n\n- Example source. DOI: 10.1000/example.",
        ]
    )
    submission = repo.create_object(
        ResearchObject(
            object_type=ObjectType.SUBMISSION,
            title="Research Synthesis: Example intervention",
            body_markdown=full_body,
            metadata={
                "abstract": (
                    "Evidence-honesty note: 24/26 retained sources are coded as null or no extracted directional signal; "
                    "this corpus is non-supportive for clinical efficacy claims and hypothesis-generating only [bundle:1]."
                ),
                "article_type": ArticleType.RESEARCH_SYNTHESIS.value,
                "sections": sections_from_markdown(
                    full_body, ArticleType.RESEARCH_SYNTHESIS.value
                ),
                "source_bundle": [
                    {
                        "title": f"Source {index}",
                        "year": 2024,
                        "evidence_type": "review",
                        "excerpt": (
                            "Evidence-honesty note: 24/26 retained sources are coded as null or no extracted "
                            "directional signal; this corpus is non-supportive for clinical efficacy claims and "
                            "hypothesis-generating only. The results preserve null and heterogeneous source-level findings "
                            "without claiming broad clinical efficacy, separate direct findings from adjacent evidence, and "
                            "report the corpus as hypothesis-generating rather than intervention-ready. The conclusion states "
                            "a bounded hypothesis-generating finding, avoids clinical guidance, and makes clear that the weak "
                            "retained corpus supports classification discipline rather than a definitive research synthesis label."
                        )
                        if index == 1
                        else "The source reports a null or non-directional finding.",
                    }
                    for index in range(1, 27)
                ],
                "core_claims_resolved": True,
            },
        )
    )
    monkeypatch.setattr(
        workflow, "_mint_publication_doi", lambda repository, publication: {}
    )

    result = WorkflowEngine()._run_publish(accepted_publish_job(repo, submission), repo)

    publication = repo.get_object(result["publication_id"])
    assert publication is not None
    assert publication.title == "Hypothesis-Generating Brief: Example intervention"
    assert publication.metadata["publication_class"] == "hypothesis_generating_brief"
    assert publication.metadata["evidence_profile"]["weak_evidence_ratio"] == 0.9231


def test_publish_preserves_research_synthesis_with_direct_clinical_core(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = InMemoryRuntimeRepository()
    full_body = "\n\n".join(
        [
            "# Full manuscript",
            "## Abstract\n\nEvidence-honesty note: 54/85 retained sources are indirect, review-level, adjacent, or mechanistic and are used only to bound interpretation. The conclusion therefore does not support broad causal, clinical, or policy claims [bundle:1].",
            "## Introduction\n\nThe corpus contains 31 direct clinical sources, 53 adjacent, review, or context sources, and 1 mechanistic or model-system source.",
            "## Methods\n\nThe methods describe source retrieval, screening, extraction, appraisal, synthesis, and verification in enough detail to audit the accepted manuscript.",
            "## Results\n\nThe results preserve heterogeneous source-level findings, separate direct findings from adjacent evidence, and report contextual evidence without overstating clinical certainty [bundle:1].",
            "## Discussion\n\nThe discussion treats disagreement, indirect evidence, and mechanistic evidence as boundary conditions instead of a settled universal intervention claim [bundle:1].",
            "## Limitations\n\nThe limitations identify corpus boundaries, uncertainty, scope restrictions, missing endpoints, and interpretation risks that constrain public claims [bundle:1].",
            "## Conclusion\n\nThe conclusion states a bounded research synthesis, avoids clinical guidance, and makes clear that contextual evidence does not replace the direct clinical core [bundle:1].",
            "## References\n\n- Example source. DOI: 10.1000/example.",
        ]
    )
    submission = repo.create_object(
        ResearchObject(
            object_type=ObjectType.SUBMISSION,
            title="Research Synthesis: Resistance Training Effects — full paper",
            body_markdown=full_body,
            metadata={
                "abstract": "Evidence-honesty note: 54/85 retained sources are indirect, review-level, adjacent, or mechanistic and are used only to bound interpretation [bundle:1].",
                "article_type": ArticleType.RESEARCH_SYNTHESIS.value,
                "sections": sections_from_markdown(
                    full_body, ArticleType.RESEARCH_SYNTHESIS.value
                ),
                "source_bundle": [
                    {
                        "title": f"Primary source {index}",
                        "year": 2024,
                        "evidence_type": "primary",
                        "directness": "direct clinical",
                        "risk_of_bias": "low",
                        "excerpt": (
                            "Evidence-honesty note: 54/85 retained sources are indirect, review-level, adjacent, or "
                            "mechanistic and are used only to bound interpretation. The evidence bounds interpretation, "
                            "preserves heterogeneous findings, separates direct "
                            "clinical results from adjacent evidence, treats disagreement as a boundary condition, "
                            "identifies corpus uncertainty and scope restrictions, and avoids broad clinical guidance."
                        )
                        if index == 1
                        else "This direct clinical source reports an appraised endpoint-specific result.",
                    }
                    for index in range(1, 59)
                ]
                + [
                    {
                        "title": f"Review source {index}",
                        "year": 2024,
                        "evidence_type": "review",
                        "directness": "review level",
                    }
                    for index in range(59, 86)
                ],
                "core_claims_resolved": True,
            },
        )
    )
    monkeypatch.setattr(
        workflow, "_mint_publication_doi", lambda repository, publication: {}
    )

    result = WorkflowEngine()._run_publish(accepted_publish_job(repo, submission), repo)

    publication = repo.get_object(result["publication_id"])
    assert publication is not None
    assert (
        publication.title
        == "Research Synthesis: Resistance Training Effects — full paper"
    )
    assert publication.metadata["publication_class"] == "research_synthesis"
    assert publication.metadata["evidence_profile"]["weak_evidence_ratio"] == 0.6353
    assert publication.metadata["evidence_profile"]["direct_clinical_sources"] == 31


def test_publish_labels_declared_evidence_map_as_evidence_map(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = InMemoryRuntimeRepository()
    full_body = "\n\n".join(
        [
            "# Full manuscript",
            "## Abstract\n\nThis evidence map catalogs heterogeneous source-level findings without collapsing them into a single causal thesis. It defines the topic boundary, preserves source-level disagreement, separates direct evidence from adjacent context, and frames every conclusion as exploratory rather than clinical guidance or policy advice.",
            "## Methods\n\nThe methods describe source retrieval, screening, extraction, appraisal, synthesis, and verification in enough detail to audit the accepted manuscript.",
            "## Results\n\nThe results preserve heterogeneous source-level findings and report contextual evidence without overstating clinical certainty.",
            "## Limitations\n\nThe limitations identify corpus boundaries, uncertainty, scope restrictions, missing endpoints, and interpretation risks that constrain public claims.",
            "## Conclusion\n\nThe conclusion states that the map is exploratory, bounded, and source-dependent. It does not support broad causal, clinical, or policy claims, and it directs future work toward better endpoint-specific replication rather than pretending that the current corpus has converged.",
            "## References\n\n- Example source. DOI: 10.1000/example.",
        ]
    )
    submission = repo.create_object(
        ResearchObject(
            object_type=ObjectType.SUBMISSION,
            title="Research Synthesis: Tai Chi Exercise Effects — full paper",
            body_markdown=full_body,
            metadata={
                "abstract": "This evidence map catalogs heterogeneous source-level findings [bundle:1].",
                "article_type": ArticleType.EVIDENCE_MAP.value,
                "sections": {
                    "Evidence Landscape": (
                        "This evidence map catalogs heterogeneous source-level findings without collapsing them into a single "
                        "causal thesis. It preserves source-level disagreement and frames conclusions as exploratory [bundle:1]."
                    ),
                    "Limitations": "The map is bounded by corpus coverage, heterogeneous endpoints, and uncertain external validity.",
                },
                "source_bundle": [
                    {
                        "title": f"Source {index}",
                        "year": 2024,
                        "evidence_type": "primary",
                        "excerpt": "This evidence map catalogs heterogeneous source-level findings and preserves source-level disagreement.",
                    }
                    for index in range(1, 13)
                ],
                "core_claims_resolved": True,
            },
        )
    )
    monkeypatch.setattr(
        workflow, "_mint_publication_doi", lambda repository, publication: {}
    )

    result = WorkflowEngine()._run_publish(accepted_publish_job(repo, submission), repo)

    publication = repo.get_object(result["publication_id"])
    assert publication is not None
    assert publication.title == "Evidence Map: Tai Chi Exercise Effects — full paper"
    assert publication.metadata["publication_class"] == "evidence_map"


def test_compile_publication_preserves_full_manuscript_references() -> None:
    sections = {
        **_full_sections(),
        "References": "- Example 2024. DOI: 10.1234/example. PMID: 12345678.",
    }
    artifact = compile_publication(
        title="Full manuscript",
        abstract="A",
        sections=sections,
        source_bundle=_valid_source_bundle(),
    )
    assert "## References" in artifact.body_markdown
    assert "DOI: 10.1234/example" in artifact.body_markdown


def test_canonical_bundle_facts_reconcile_counts() -> None:
    counts = canonical_bundle_facts(
        [
            {"evidence_type": "review", "year": 2024},
            {"evidence_type": "primary", "year": 2023},
        ]
    )
    results = run_publish_gates(
        body_markdown="Clean body", counts=counts, core_claims_resolved=True
    )
    reconciliation = next(
        result for result in results if result.name == "count_reconciliation"
    )
    assert reconciliation.passed is True


def test_unresolved_core_claims_fail_publish_gate() -> None:
    counts = canonical_bundle_facts([{"evidence_type": "review", "year": 2024}])
    results = run_publish_gates(
        body_markdown="Clean body", counts=counts, core_claims_resolved=False
    )
    claim_gate = next(
        result for result in results if result.name == "core_claims_resolved"
    )
    assert claim_gate.passed is False


def test_workflow_uses_provider_contract_for_trace_metadata() -> None:
    class StubProvider:
        enforces_accept_quorum = True

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
                    provider="reviewer-panel",
                    model="stub-model",
                    usage=ProviderUsage(
                        input_tokens=11, output_tokens=7, cost_usd=0.42
                    ),
                    metadata={
                        "accept_quorum_count": 2,
                        "accept_quorum_models": ["stub-primary", "stub-sparring"],
                        "accept_quorum_identities": [
                            "stub-a:stub-primary",
                            "stub-b:stub-sparring",
                        ],
                        "accept_quorum_providers": ["stub-a", "stub-b"],
                    },
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
    assert review.metadata["provider"] == "reviewer-panel"
    assert review.metadata["model"] == "stub-model"
    assert review.metadata["tokens_in"] == 11
    assert review.metadata["tokens_out"] == 7
    assert review.metadata["cost_usd"] == 0.42
    assert review.metadata["rubric_scores"]["claim_evidence_alignment"] == 4
    assert review.metadata["rubric_scores"]["source_grounding"] == 4
    assert set(review.metadata["rubric_calibration"]["changes"]) == {
        "claim_evidence_alignment",
        "source_grounding",
    }
    assert review.metadata["judge_release_id"].startswith("sha256:")
    assert review.metadata["judge_release"]["models"] == ["stub-model"]
    assert review.metadata["judge_release"]["observed_models"] == [
        "stub-primary",
        "stub-sparring",
    ]
    assert review.metadata["judge_release"]["settings"]["accept_quorum_min"] == 2


def test_reviewer_prompt_keeps_triage_and_decision_contract_visible() -> None:
    prompt = WorkflowEngine()._review_system_prompt(
        ArticleType.RAPID_EVIDENCE_SYNTHESIS.value
    )
    assert "rapid evidence synthesis reviewer" in prompt.lower()
    assert "forced triage call" in prompt
    assert "Do not use revise as a safe default" in prompt
    assert "mixed or heterogeneous findings are acceptable" in prompt
    assert "Judge substance, not house style" in prompt
    assert "House-style revise" in prompt
    assert "Terser-style accept" in prompt
    assert "External-style accept" in prompt
    assert "accept = all scores >= 4" in prompt
    assert "Never assume unverifiable percentages" in prompt
    assert "required_revisions lists concrete fixes" in prompt
    assert "review_markdown must be a non-empty rationale" in prompt
    assert (
        "Do not label accept-quality papers as revise for minor wording polish only"
        in prompt
    )
    assert "reject = structurally broken" in prompt
    empirical_prompt = WorkflowEngine()._review_system_prompt(
        ArticleType.EMPIRICAL_STUDY.value
    )
    assert "empirical study reviewer" in empirical_prompt.lower()
    assert "Empirical-study calibration rules" in empirical_prompt
    assert "one primary study rather than a multi-study synthesis" in empirical_prompt
    assert "stand-ins for methods and results context" in empirical_prompt


def test_workflow_marks_empirical_study_in_review_metadata() -> None:
    class EmpiricalProvider:
        enforces_accept_quorum = True

        def complete(self, request: ProviderRequest) -> ProviderResult:
            assert "empirical study reviewer" in request.system_prompt.lower()
            return ProviderResult(
                ok=True,
                response=ProviderResponse(
                    text=json.dumps(
                        _review_payload(
                            "accept", review_markdown="Empirical manuscript accepted."
                        )
                    ),
                    provider="reviewer-panel",
                    model="stub-model",
                    usage=ProviderUsage(input_tokens=9, output_tokens=6, cost_usd=0.21),
                    metadata={
                        "accept_quorum_count": 2,
                        "accept_quorum_models": [
                            "empirical-primary",
                            "empirical-sparring",
                        ],
                        "accept_quorum_identities": [
                            "empirical-a:empirical-primary",
                            "empirical-b:empirical-sparring",
                        ],
                        "accept_quorum_providers": ["empirical-a", "empirical-b"],
                    },
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
    engine.handle_job(
        RuntimeJob(target_object_id=submission.id, stage=Stage.INTAKE, payload={}), repo
    )
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
    assert ledger["revision_hints"] == [
        "Keep the retained hint about explicit date windows and databases searched."
    ]
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
    assert (
        next(gate for gate in artifact.gates if gate.name == "leakage_blocker").passed
        is False
    )


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
            "### Introduction\n\n" + " ".join(["introduction"] * 30),
            "### Methods\n\n" + " ".join(["methods"] * 30),
            "### Results\n\n" + " ".join(["results"] * 30),
            "### Discussion\n\n" + " ".join(["discussion"] * 30),
            "### Limitations\n\n" + " ".join(["limitations"] * 30),
            "### Conclusion\n\n" + " ".join(["conclusion"] * 30),
            "### References\n\n- Example 2024. DOI: 10.1234/example.",
        ]
    )

    validate_template_structure(
        body,
        publication_template_for(
            ArticleType.RESEARCH_SYNTHESIS.value
        ).required_sections,
    )


def test_full_manuscript_structure_counts_nested_subsections() -> None:
    body = "\n\n".join(
        [
            "# Full manuscript",
            "### Abstract\n\n" + " ".join(["abstract"] * 30),
            "### Introduction\n\n" + " ".join(["introduction"] * 30),
            "### Methods",
            "#### Review type\n\n" + " ".join(["methods"] * 30),
            "#### Search strategy\n\n" + " ".join(["search"] * 30),
            "### Results\n\n" + " ".join(["results"] * 30),
            "### Discussion\n\n" + " ".join(["discussion"] * 30),
            "### Limitations\n\n" + " ".join(["limitations"] * 30),
            "### Conclusion\n\n" + " ".join(["conclusion"] * 30),
            "### References\n\n- Example 2024. DOI: 10.1234/example.",
        ]
    )

    validate_template_structure(
        body,
        publication_template_for(
            ArticleType.RESEARCH_SYNTHESIS.value
        ).required_sections,
    )


def test_failure_classifier_maps_structure_gate() -> None:
    assert (
        classify_failure_reason(
            "structure_gate: 'Conclusion' empty or placeholder-thin"
        )
        == FailureClass.STRUCTURE_GATE
    )


def test_failure_classifier_maps_integrity_publish_block() -> None:
    assert (
        classify_failure_reason("publish_blocked_by_integrity:reject")
        == FailureClass.PUBLISH_GATES_FAILED
    )


def test_openrouter_provider_retries_transient_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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

    monkeypatch.setattr(
        "runtime_core.providers.time.sleep", lambda seconds: sleeps.append(seconds)
    )
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


def test_openrouter_provider_rejects_unapproved_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RESEARKA_V2_OPENROUTER_ALLOWED_MODELS", raising=False)

    with pytest.raises(
        ValueError, match="openrouter_model_not_allowed:anthropic/claude-sonnet-5"
    ):
        OpenRouterProvider(api_key="test-key", model="anthropic/claude-sonnet-5")


def test_openrouter_provider_retries_rate_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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

    monkeypatch.setattr(
        "runtime_core.providers.time.sleep", lambda seconds: sleeps.append(seconds)
    )
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


def test_reviewer_panel_from_env_uses_mimo_gemma_mistral(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RESEARKA_V2_PROVIDER", "judge_panel")
    monkeypatch.setenv("RESEARKA_V2_REVIEWER_PRIMARY_PROVIDER", "mimo")
    monkeypatch.delenv("RESEARKA_V2_MIMO_MODEL", raising=False)
    monkeypatch.delenv("RESEARKA_V2_REVIEWER_MODEL", raising=False)
    monkeypatch.delenv("RESEARKA_V2_JUDGE_MODEL", raising=False)
    monkeypatch.delenv("RESEARKA_V2_SKIP_SPARRING_ON_BILLING_ERROR", raising=False)
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
    assert provider.allow_sparring_billing_skip is False


def test_production_forbids_deterministic_provider_and_reviewer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RESEARKA_V2_ENV", "production")
    monkeypatch.setenv("RESEARKA_V2_PROVIDER", "deterministic")

    with pytest.raises(RuntimeError, match="deterministic_provider_forbidden_in_production"):
        provider_from_env()
    with pytest.raises(RuntimeError, match="deterministic_reviewer_forbidden_in_production"):
        reviewer_from_env()


def test_reviewer_panel_from_env_can_disable_per_slot_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Setting RESEARKA_V2_REVIEWER_FALLBACK_ENABLED=0 returns to the previous
    behaviour: bare primary and Gemma in the primary/sparring slots, no wrapping.
    Useful for measuring raw provider failure rates without the safety net."""
    monkeypatch.setenv("RESEARKA_V2_PROVIDER", "judge_panel")
    monkeypatch.setenv("RESEARKA_V2_REVIEWER_PRIMARY_PROVIDER", "mimo")
    monkeypatch.setenv("RESEARKA_V2_REVIEWER_FALLBACK_ENABLED", "0")

    provider = reviewer_from_env()

    assert isinstance(provider, ReviewerPanel)
    assert isinstance(provider.primary, MimoProvider)
    assert isinstance(provider.sparring, OpenRouterProvider)


@pytest.mark.parametrize(
    ("value", "expected"), [("1", True), ("true", True), ("typo", False), ("", False)]
)
def test_reviewer_panel_from_env_parses_billing_skip_strictly(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
    expected: bool,
) -> None:
    monkeypatch.setenv("RESEARKA_V2_PROVIDER", "judge_panel")
    monkeypatch.setenv("RESEARKA_V2_REVIEWER_PRIMARY_PROVIDER", "mimo")
    monkeypatch.setenv("RESEARKA_V2_SKIP_SPARRING_ON_BILLING_ERROR", value)
    if expected:
        monkeypatch.setenv("RESEARKA_V2_REVIEW_ATTESTATION_SECRET", "test-secret")

    provider = reviewer_from_env()

    assert isinstance(provider, ReviewerPanel)
    assert provider.allow_sparring_billing_skip is expected


def test_reviewer_panel_billing_skip_requires_attestation_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RESEARKA_V2_PROVIDER", "judge_panel")
    monkeypatch.setenv("RESEARKA_V2_SKIP_SPARRING_ON_BILLING_ERROR", "1")
    monkeypatch.delenv("RESEARKA_V2_REVIEW_ATTESTATION_SECRET", raising=False)
    monkeypatch.delenv("RESEARKA_V2_REVIEW_ATTESTATION_SECRET_PATH", raising=False)

    with pytest.raises(RuntimeError, match="review_attestation_secret_required"):
        reviewer_from_env()


def test_reviewer_panel_from_env_can_select_minimax_primary_for_rollback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RESEARKA_V2_PROVIDER", "judge_panel")
    monkeypatch.setenv("RESEARKA_V2_REVIEWER_PRIMARY_PROVIDER", "minimax")
    monkeypatch.setenv("RESEARKA_V2_REVIEWER_FALLBACK_ENABLED", "0")

    provider = reviewer_from_env()

    assert isinstance(provider, ReviewerPanel)
    assert isinstance(provider.primary, MiniMaxProvider)
    assert provider.primary.model == "MiniMax-M3"


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
    review = bound_review(
        repo,
        submission,
        {},
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


def test_reviewer_panel_returns_actionable_revise_on_three_way_disagreement() -> None:
    class FakeProvider:
        def __init__(self, provider: str, model: str, recommendation: str) -> None:
            self.provider = provider
            self.model = model
            self.recommendation = recommendation

        def complete(self, request: ProviderRequest) -> ProviderResult:
            return ProviderResult(
                ok=True,
                response=ProviderResponse(
                    text=json.dumps(
                        _review_payload(
                            self.recommendation,
                            review_markdown=f"{self.provider} says {self.recommendation}",
                        )
                    ),
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
    assert result.response.metadata["route"] == "disagreement_conservative_revise"


def test_same_provider_accepts_cannot_override_independent_revise() -> None:
    request = ProviderRequest(
        system_prompt="system", user_prompt="user", prompt_version="reviewer-v1"
    )
    result = ReviewerPanel(
        primary=_ReviewPayloadProvider(
            "mimo-v2.5-pro", _review_payload("revise"), provider="mimo"
        ),
        sparring=_ReviewPayloadProvider(
            "google/gemma-4-31b-it", _review_payload("accept"), provider="openrouter"
        ),
        fallback=_ReviewPayloadProvider(
            "mistralai/mistral-small-2603",
            _review_payload("accept"),
            provider="openrouter",
        ),
    ).complete(request)

    assert result.ok is True
    assert result.response is not None
    assert '"recommendation": "revise"' in result.response.text
    assert result.response.metadata["accept_quorum_count"] == 1
    assert result.response.metadata["ops_flag"] == "reviewer_disagreement_conservative_revise"


def test_reviewer_panel_falls_back_from_false_missing_manuscript_rejection() -> None:
    class FailedProvider:
        provider = "minimax"
        model = "primary"

        def complete(self, request: ProviderRequest) -> ProviderResult:  # noqa: ARG002
            return ProviderResult(
                ok=False,
                error=ProviderError(
                    error_class=ProviderErrorClass.PROVIDER_UNAVAILABLE,
                    message="unavailable",
                ),
            )

    sparring = _ReviewPayloadProvider(
        "sparring",
        _review_payload(
            "reject",
            major_issues=[
                "The submission is empty/missing content between the submission data markers."
            ],
            required_revisions=["Provide the full manuscript text for review."],
        ),
    )
    fallback = _ReviewPayloadProvider("fallback", _review_payload("revise"))
    nonce = "a" * 24
    request = ProviderRequest(
        system_prompt="system",
        user_prompt=(
            f"SUBMISSION_DATA_START_{nonce}\n"
            + json.dumps(
                {"abstract": "A complete abstract.", "sections": _full_sections()}
            )
            + f"\nSUBMISSION_DATA_END_{nonce}"
        ),
        prompt_version="reviewer-v1",
    )

    result = ReviewerPanel(
        primary=FailedProvider(), sparring=sparring, fallback=fallback
    ).complete(request)

    assert result.ok is False
    assert result.error is not None
    assert "panel_single_fallback_cannot_decide" in result.error.message
    assert "false_missing_manuscript" in result.error.message


def test_reviewer_panel_allows_substantively_empty_rejection() -> None:
    primary = _ReviewPayloadProvider(
        "reviewer-a", _review_payload("reject"), provider="provider-a"
    )
    sparring = _ReviewPayloadProvider(
        "reviewer-b", _review_payload("reject"), provider="provider-b"
    )
    nonce = "b" * 24
    request = ProviderRequest(
        system_prompt="system",
        user_prompt=(
            f"SUBMISSION_DATA_START_{nonce}\n"
            + json.dumps({"abstract": "Padded abstract.", "sections": _full_sections()})
            + f"\nSUBMISSION_DATA_END_{nonce}"
        ),
        prompt_version="reviewer-v1",
    )

    result = ReviewerPanel(
        primary=primary, sparring=sparring, fallback=primary
    ).complete(request)

    assert result.ok is True
    assert result.response is not None
    assert result.response.metadata["decision_quorum_count"] == 2
    assert '"recommendation": "reject"' in result.response.text


@pytest.mark.parametrize("recommendation", ["revise", "reject"])
def test_one_provider_cannot_form_non_accept_decision_quorum(
    recommendation: str,
) -> None:
    payload = _review_payload(recommendation)
    panel = ReviewerPanel(
        primary=_ReviewPayloadProvider("reviewer-a", payload),
        sparring=_ReviewPayloadProvider("reviewer-b", payload),
        fallback=_ReviewPayloadProvider("reviewer-c", payload),
    )

    result = panel.complete(
        ProviderRequest(
            system_prompt="system", user_prompt="user", prompt_version="reviewer-v1"
        )
    )

    assert result.ok is False
    assert result.error is not None
    assert "panel_decision_quorum_unavailable" in result.error.message


def test_reviewer_panel_cannot_create_accept_without_two_accept_votes() -> None:
    class Provider:
        def __init__(self, model: str, recommendation: str) -> None:
            self.provider = "test"
            self.model = model
            self.recommendation = recommendation

        def complete(self, request: ProviderRequest) -> ProviderResult:  # noqa: ARG002
            return ProviderResult(
                ok=True,
                response=ProviderResponse(
                    text=json.dumps(_review_payload(self.recommendation)),
                    provider=self.provider,
                    model=self.model,
                    usage=ProviderUsage(),
                ),
            )

    request = ProviderRequest(
        system_prompt="system", user_prompt="user", prompt_version="reviewer-v1"
    )
    result = ReviewerPanel(
        primary=Provider("primary", "revise"),
        sparring=Provider("sparring", "reject"),
        fallback=Provider("fallback", "accept"),
    ).complete(request)

    assert result.ok is True
    assert result.response is not None
    assert '"recommendation": "revise"' in result.response.text

    duplicate_model = ReviewerPanel(
        primary=Provider("same-model", "accept"),
        sparring=Provider("same-model", "accept"),
        fallback=Provider("fallback", "accept"),
    ).complete(request)
    assert duplicate_model.ok is False
    assert duplicate_model.error is not None
    assert "panel_accept_quorum_unavailable" in duplicate_model.error.message

    blank_model = ReviewerPanel(
        primary=Provider("", "accept"),
        sparring=Provider("sparring", "accept"),
        fallback=Provider("fallback", "accept"),
    ).complete(request)
    assert blank_model.ok is False
    assert blank_model.error is not None
    assert "panel_accept_quorum_unavailable" in blank_model.error.message


def test_reviewer_panel_retries_malformed_tiebreaker_once() -> None:
    class Provider:
        def __init__(
            self, provider: str, model: str, payloads: list[dict[str, object]]
        ) -> None:
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
        "independent-fallback",
        "mistralai/mistral-small-2603",
        [
            _review_payload(
                "reject",
                review_markdown="",
                major_issues=[],
                minor_issues=[],
                required_revisions=[],
            ),
            _review_payload("reject", review_markdown="Fallback rejects on retry."),
        ],
    )
    panel = ReviewerPanel(
        primary=Provider("mimo", "mimo-v2.5-pro", [_review_payload("accept")]),
        sparring=Provider(
            "openrouter", "google/gemma-4-31b-it", [_review_payload("reject")]
        ),
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
    assert result.response.metadata["route"] == "fallback_tiebreak"
    assert result.response.metadata["fallback_tiebreak_attempts"] == 2


def test_reviewer_panel_uses_conservative_valid_review_when_tiebreaker_stays_malformed() -> (
    None
):
    class Provider:
        def __init__(
            self, provider: str, model: str, payload: dict[str, object]
        ) -> None:
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

    fallback = Provider(
        "openrouter",
        "mistralai/mistral-small-2603",
        _review_payload("accept", review_markdown=""),
    )
    panel = ReviewerPanel(
        primary=Provider("mimo", "mimo-v2.5-pro", _review_payload("accept")),
        sparring=Provider(
            "openrouter", "google/gemma-4-31b-it", _review_payload("reject")
        ),
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
    assert result.ok is False
    assert result.error is not None
    assert fallback.calls == 2
    assert "panel_disagreement_unresolved" in result.error.message
    assert "missing_review_markdown" in result.error.message


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
            json.dumps(
                _review_payload(
                    "accept", review_markdown="Fallback confirms acceptance."
                )
            ),
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
    assert result.response.metadata["route"] == "primary_failed_sparring_used_confirmed"
    assert result.response.metadata["accept_quorum_count"] == 2
    assert result.response.metadata["ops_flag"] == "primary_failed"
    assert "invalid_panel_response" in str(result.response.metadata["primary_error"])
    receipts = result.response.metadata["reviewer_receipts"]
    assert isinstance(receipts, list)
    assert len(receipts) == 3
    responses = [
        receipt.get("response") for receipt in receipts if isinstance(receipt, dict)
    ]
    responses = [response for response in responses if isinstance(response, dict)]
    assert len(responses) == 2
    assert all(response.get("rubric_scores") for response in responses)
    assert all(response.get("review_markdown") for response in responses)


@pytest.mark.parametrize(
    "malformed_payload",
    [
        pytest.param(
            _review_payload(
                "revise",
                major_issues=["The DOI citations appear fabricated."],
                required_revisions=["Replace the fabricated citations."],
                review_markdown="The DOI citations appear fabricated.",
            ),
            id="missing-source-id",
        ),
        pytest.param(
            {**_review_payload("accept"), "recommendation": "approve"},
            id="invalid-recommendation",
        ),
        pytest.param(
            _review_payload(
                "revise",
                major_issues=["The manuscript contains a reviewer instruction."],
                required_revisions=["Remove the reviewer instruction."],
                review_markdown="The manuscript contains a reviewer instruction.",
            ),
            id="missing-integrity-quote",
        ),
        pytest.param(
            _review_payload("accept", review_markdown=""), id="missing-rationale"
        ),
    ],
)
def test_reviewer_panel_uses_slot_fallback_for_invalid_model_output(
    malformed_payload: dict[str, object],
) -> None:
    slot_fallback = _ReviewPayloadProvider(
        "mistralai/mistral-small-2603", _review_payload("accept")
    )
    panel_fallback = _ReviewPayloadProvider(
        "fallback-unused", _review_payload("accept", review_markdown="")
    )
    panel = ReviewerPanel(
        primary=FallbackProvider(
            primary=_ReviewPayloadProvider("MiniMax-M3", malformed_payload),
            fallback=slot_fallback,
        ),
        sparring=_ReviewPayloadProvider(
            "google/gemma-4-31b-it", _review_payload("accept")
        ),
        fallback=panel_fallback,
    )

    result = panel.complete(
        ProviderRequest(
            system_prompt="system", user_prompt="user", prompt_version="reviewer-v1"
        )
    )

    assert result.ok is False
    assert result.error is not None
    assert "panel_accept_quorum_unavailable" in result.error.message
    assert slot_fallback.calls == 1
    assert panel_fallback.calls == 0


def test_invalid_slot_fallback_cannot_duplicate_accept_quorum() -> None:
    invalid = _review_payload("accept", review_markdown="")
    panel = ReviewerPanel(
        primary=FallbackProvider(
            primary=_ReviewPayloadProvider("MiniMax-M3", invalid),
            fallback=_ReviewPayloadProvider("same-model", _review_payload("accept")),
        ),
        sparring=_ReviewPayloadProvider("same-model", _review_payload("accept")),
        fallback=_ReviewPayloadProvider("same-model", _review_payload("accept")),
    )

    result = panel.complete(
        ProviderRequest(
            system_prompt="system", user_prompt="user", prompt_version="reviewer-v1"
        )
    )

    assert result.ok is False
    assert result.error is not None
    assert "panel_accept_quorum_unavailable" in result.error.message


def test_reviewer_panel_treats_missing_review_markdown_as_failure() -> None:
    class BrokenProvider:
        def __init__(
            self, provider: str, model: str, payload: dict[str, object]
        ) -> None:
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
    assert result.ok is False
    assert result.error is not None
    assert "panel_single_reviewer_disagreement" in result.error.message
    assert "missing_review_markdown" in result.error.message


def test_reviewer_panel_recovers_non_accept_review_markdown_from_structured_feedback() -> (
    None
):
    class Provider:
        def __init__(
            self, payload: dict[str, object], *, provider: str, model: str
        ) -> None:
            self.payload = payload
            self.provider = provider
            self.model = model

        def complete(self, request: ProviderRequest) -> ProviderResult:  # noqa: ARG002
            return ProviderResult(
                ok=True,
                response=ProviderResponse(
                    text=json.dumps(self.payload),
                    provider=self.provider,
                    model=self.model,
                    usage=ProviderUsage(input_tokens=10, output_tokens=5, cost_usd=0.1),
                ),
            )

    missing_markdown = _review_payload("revise", review_markdown="")
    panel = ReviewerPanel(
        primary=Provider(missing_markdown, provider="provider-a", model="reviewer-a"),
        sparring=Provider(
            _review_payload("revise"), provider="provider-b", model="reviewer-b"
        ),
        fallback=Provider(
            _review_payload("reject"), provider="provider-c", model="reviewer-c"
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
    payload = json.loads(result.response.text)
    assert payload["recommendation"] == "revise"
    assert "Required revisions:" in payload["review_markdown"]
    assert result.response.metadata["review_markdown_recovered"] is True


def test_reviewer_panel_treats_weak_accept_contract_as_failure() -> None:
    class BrokenProvider:
        def __init__(
            self, provider: str, model: str, payload: dict[str, object]
        ) -> None:
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
    assert result.ok is False
    assert result.error is not None
    assert "panel_single_reviewer_disagreement" in result.error.message
    assert "accept_rubric_too_weak" in result.error.message


@pytest.mark.parametrize(
    ("required_revisions", "expected_error"),
    [
        ([], "revise_missing_required_revisions"),
        (None, "missing_required_revisions"),
        ("__missing__", "missing_required_revisions"),
    ],
)
def test_reviewer_panel_rejects_non_actionable_revise_contract(
    required_revisions: object, expected_error: str
) -> None:
    class Provider:
        def __init__(
            self, provider: str, model: str, payload: dict[str, object]
        ) -> None:
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

    assert result.ok is False
    assert result.error is not None
    assert "panel_single_reviewer_disagreement" in result.error.message
    assert expected_error in result.error.message


def test_workflow_stores_panel_route_metadata() -> None:
    class PanelProvider:
        provider = "reviewer-panel"
        model = "mimo-v2.5-pro|google/gemma-4-31b-it|mistralai/mistral-small-2603"
        enforces_accept_quorum = True

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
                    usage=ProviderUsage(
                        input_tokens=33, output_tokens=12, cost_usd=0.9
                    ),
                    metadata={
                        "route": "consensus",
                        "winner_provider": "mimo",
                        "winner_model": "mimo-v2.5-pro",
                        "primary_recommendation": "accept",
                        "accept_quorum_count": 2,
                        "accept_quorum_models": [
                            "mimo-v2.5-pro",
                            "google/gemma-4-31b-it",
                        ],
                        "accept_quorum_identities": [
                            "mimo:mimo-v2.5-pro",
                            "openrouter:google/gemma-4-31b-it",
                        ],
                        "accept_quorum_providers": ["mimo", "openrouter"],
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
    intake_job = repo.enqueue_job(
        RuntimeJob(
            target_object_id=submission.id,
            stage=Stage.INTAKE,
            payload={"domain_slug": "longevity"},
        )
    )
    engine.handle_job(intake_job, repo)
    repo.complete_job(intake_job.id)

    review_job = repo.claim_next_job()
    engine.handle_job(review_job, repo)
    repo.complete_job(review_job.id)

    review = repo.list_objects(ObjectType.REVIEW)[0]
    editorial_job = repo.enqueue_job(
        RuntimeJob(
            target_object_id=submission.id,
            stage=Stage.EDITORIAL,
            payload={"review_id": review.id, "domain_slug": "longevity"},
        )
    )
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
    result = engine.handle_job(
        RuntimeJob(
            target_object_id=submission.id,
            stage=Stage.INTAKE,
            payload={"domain_slug": "longevity"},
        ),
        repo,
    )
    assert result.get("terminal_decision") == Decision.REVISE.value


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
    result = engine.handle_job(
        RuntimeJob(
            target_object_id=submission.id,
            stage=Stage.INTAKE,
            payload={"domain_slug": "longevity"},
        ),
        repo,
    )
    assert result.get("terminal_decision") == Decision.REVISE.value


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
    result = engine.handle_job(
        RuntimeJob(
            target_object_id=submission.id,
            stage=Stage.INTAKE,
            payload={"domain_slug": "longevity"},
        ),
        repo,
    )
    decision = repo.list_objects(ObjectType.DECISION)[0]
    assert (
        result.get("terminal_decision") == Decision.REVISE.value
    )  # author-correctable intake defect
    assert {failure["name"] for failure in decision.metadata["gate_failures"]} == {
        "research_question_word_budget"
    }


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
    result = engine.handle_job(
        RuntimeJob(
            target_object_id=submission.id,
            stage=Stage.INTAKE,
            payload={"domain_slug": "longevity"},
        ),
        repo,
    )
    decision = repo.list_objects(ObjectType.DECISION)[0]
    assert result.get("terminal_decision") == Decision.REVISE.value
    assert {failure["name"] for failure in decision.metadata["gate_failures"]} == {
        "minimum_citations"
    }


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
    result = engine.handle_job(
        RuntimeJob(
            target_object_id=submission.id,
            stage=Stage.INTAKE,
            payload={"domain_slug": "longevity"},
        ),
        repo,
    )
    decision = repo.list_objects(ObjectType.DECISION)[0]
    assert result.get("terminal_decision") == Decision.REVISE.value
    assert {failure["name"] for failure in decision.metadata["gate_failures"]} == {
        "recency_ratio"
    }


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
    result = engine.handle_job(
        RuntimeJob(
            target_object_id=submission.id,
            stage=Stage.INTAKE,
            payload={"domain_slug": "longevity"},
        ),
        repo,
    )
    decision = repo.list_objects(ObjectType.DECISION)[0]
    assert (
        result.get("terminal_decision") == Decision.REVISE.value
    )  # author-correctable intake defect
    assert {failure["name"] for failure in decision.metadata["gate_failures"]} == {
        "source_bundle_schema"
    }


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
    result = engine.handle_job(
        RuntimeJob(
            target_object_id=submission.id,
            stage=Stage.INTAKE,
            payload={"domain_slug": "longevity"},
        ),
        repo,
    )
    assert (
        result.get("terminal_decision") == Decision.REVISE.value
    )  # author-correctable intake defect


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
                            "minor_issues": [
                                "Limitations section is honest but not integrated into the conclusion."
                            ],
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
                    usage=ProviderUsage(
                        input_tokens=20, output_tokens=10, cost_usd=0.0
                    ),
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

    intake_job = repo.enqueue_job(
        RuntimeJob(
            target_object_id=submission.id,
            stage=Stage.INTAKE,
            payload={"domain_slug": "longevity"},
        )
    )
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
        provider = "reviewer-panel"
        model = "calibration-accept-model"
        enforces_accept_quorum = True

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
                    usage=ProviderUsage(
                        input_tokens=20, output_tokens=10, cost_usd=0.0
                    ),
                    metadata={
                        "accept_quorum_count": 2,
                        "accept_quorum_models": [
                            "calibration-primary",
                            "calibration-sparring",
                        ],
                        "accept_quorum_identities": [
                            "calibration-a:calibration-primary",
                            "calibration-b:calibration-sparring",
                        ],
                        "accept_quorum_providers": ["calibration-a", "calibration-b"],
                    },
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
                    usage=ProviderUsage(
                        input_tokens=20, output_tokens=10, cost_usd=0.0
                    ),
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
                            "minor_issues": [
                                "Limitations section is honest but not integrated into the conclusion."
                            ],
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
                    usage=ProviderUsage(
                        input_tokens=20, output_tokens=10, cost_usd=0.0
                    ),
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
                    usage=ProviderUsage(
                        input_tokens=20, output_tokens=10, cost_usd=0.0
                    ),
                ),
            )

    return Provider()


def _calibration_submission(
    repo: InMemoryRuntimeRepository, *, recommendation: str
) -> ResearchObject:
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

    intake_job = repo.enqueue_job(
        RuntimeJob(
            target_object_id=submission.id,
            stage=Stage.INTAKE,
            payload={"domain_slug": "longevity"},
        )
    )
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


def test_reprocess_operation_propagates_through_review_and_editorial() -> None:
    repo = InMemoryRuntimeRepository()
    submission = _calibration_submission(repo, recommendation="accept")
    engine = WorkflowEngine(provider=_rubric_accept_provider())
    operation_id = "reprocess-verifier-upgrade"

    intake_job = repo.enqueue_job(
        RuntimeJob(
            target_object_id=submission.id,
            stage=Stage.INTAKE,
            payload={"operation_id": operation_id},
        )
    )
    engine.handle_job(intake_job, repo)
    repo.complete_job(intake_job.id)
    review_job = repo.claim_next_job()
    assert review_job is not None
    assert review_job.payload["operation_id"] == operation_id

    engine.handle_job(review_job, repo)
    repo.complete_job(review_job.id)
    editorial_job = repo.claim_next_job()
    assert editorial_job is not None
    assert editorial_job.payload["operation_id"] == operation_id

    engine.handle_job(editorial_job, repo)
    publish_job = repo.queued_jobs()[0]
    assert publish_job.stage is Stage.PUBLISH
    assert publish_job.payload["operation_id"] == operation_id


def test_single_provider_accept_cannot_bypass_panel_quorum() -> None:
    class SingleProvider:
        provider = "single-reviewer"
        model = "single-model"

        def complete(self, request: ProviderRequest) -> ProviderResult:
            return ProviderResult(
                ok=True,
                response=ProviderResponse(
                    text=json.dumps(_review_payload("accept")),
                    provider=self.provider,
                    model=self.model,
                    usage=ProviderUsage(input_tokens=1, output_tokens=1, cost_usd=0.0),
                ),
            )

    submission = _calibration_submission(
        InMemoryRuntimeRepository(), recommendation="accept"
    )
    with pytest.raises(ValueError, match="accept_quorum_missing"):
        WorkflowEngine(provider=SingleProvider())._review_submission(submission)


@pytest.mark.parametrize("quota_message", ["quota exhausted", "insufficient_quota"])
def test_primary_quota_exhaustion_stops_review_without_paid_fallback_or_retries(
    monkeypatch: pytest.MonkeyPatch, quota_message: str,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr("runtime_core.providers.time.sleep", lambda _: None)

    def exhausted(req, **kwargs):
        calls.append(req.full_url)
        raise urllib.error.HTTPError(
            req.full_url, 429, "Too Many Requests", {},
            io.BytesIO(json.dumps({"error": {"message": quota_message}}).encode()),
        )

    monkeypatch.setattr("runtime_core.providers.urllib.request.urlopen", exhausted)
    backup = OpenRouterProvider(api_key="test", model="mistralai/mistral-small-2603")
    panel = ReviewerPanel(
        primary=FallbackProvider(primary=MimoProvider(api_key="test"), fallback=backup),
        sparring=OpenRouterProvider(api_key="test", model="google/gemma-4-31b-it"),
        fallback=backup,
        allow_sparring_billing_skip=True,
    )
    repo = InMemoryRuntimeRepository()
    submission = _calibration_submission(repo, recommendation="accept")
    job = repo.enqueue_job(RuntimeJob(target_object_id=submission.id, stage=Stage.REVIEW))
    worker = WorkerApp(repo, engine=WorkflowEngine(provider=panel))

    result = worker.run_once()

    assert result["failed"] == 1
    assert result["retried"] == 0
    assert len(calls) == 1
    assert "xiaomimimo.com" in calls[0]
    failed = repo.get_job(job.id)
    assert failed is not None
    assert failed.payload["failure_reason"].startswith("provider_error:billing:mimo:")
    assert quota_message in failed.payload["failure_reason"]
    assert not repo.list_objects(ObjectType.REVIEW)
    assert not repo.list_objects(ObjectType.PUBLICATION)
    assert worker.run_once()["claimed"] == 0
    failure = next(e for e in reversed(repo.list_events()) if e.event_type == EventType.JOB_FAILED)
    assert failure.payload["terminal"] is True


def test_secondary_billing_failure_skips_without_backlogging_accept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    monkeypatch.setenv("RESEARKA_V2_REVIEW_ATTESTATION_SECRET", "test-secret")

    def payment_required(*args: object, **kwargs: object) -> object:  # noqa: ARG001
        nonlocal calls
        calls += 1
        raise urllib.error.HTTPError(
            "https://openrouter.ai/api/v1/chat/completions",
            402,
            "Payment Required",
            {},
            None,
        )

    monkeypatch.setattr(
        "runtime_core.providers.urllib.request.urlopen", payment_required
    )
    primary = FallbackProvider(
        primary=DeterministicProvider(provider="minimax", model="MiniMax-M3"),
        fallback=OpenRouterProvider(
            api_key="test", model="mistralai/mistral-small-2603"
        ),
    )
    sparring = FallbackProvider(
        primary=OpenRouterProvider(api_key="test", model="google/gemma-4-31b-it"),
        fallback=OpenRouterProvider(
            api_key="test", model="mistralai/mistral-small-2603"
        ),
    )
    panel = ReviewerPanel(
        primary=primary,
        sparring=sparring,
        fallback=OpenRouterProvider(
            api_key="test", model="mistralai/mistral-small-2603"
        ),
        allow_sparring_billing_skip=True,
    )
    repo = InMemoryRuntimeRepository()
    submission = _calibration_submission(repo, recommendation="accept")
    repo.enqueue_job(RuntimeJob(target_object_id=submission.id, stage=Stage.REVIEW))

    result = WorkerApp(repo, engine=WorkflowEngine(provider=panel)).run_once()
    review = repo.list_objects(ObjectType.REVIEW)[0]
    metadata = review.metadata

    assert result["completed"] == 1
    assert result["failed"] == 0
    assert metadata["recommendation"] == "accept"
    assert metadata["route"] == "sparring_billing_skipped_primary_used"
    assert metadata["accept_quorum_count"] == 1
    assert metadata["accept_quorum_waiver"] == "sparring_billing_unavailable"
    assert metadata["secondary_review_skipped"] is True
    assert metadata["accept_quorum_waiver_verified"] is True
    assert str(metadata["accept_quorum_waiver_attestation"]).startswith("hmac-sha256:")
    assert metadata["sparring_provider"] == "openrouter"
    assert metadata["sparring_http_status"] == 402
    assert metadata["primary_fallback_used"] is False
    assert metadata["judge_release"]["settings"]["accept_quorum_min"] == 1
    assert (
        metadata["judge_release"]["settings"]["accept_quorum_waiver"]
        == "sparring_billing_unavailable"
    )
    assert calls == 1
    assert not accept_quorum_satisfied(metadata)
    assert accept_quorum_satisfied(metadata, allow_billing_waiver=True)

    editorial = WorkerApp(repo, engine=WorkflowEngine(provider=panel)).run_once()
    assert editorial["completed"] == 1
    assert repo.list_objects(ObjectType.DECISION)[0].metadata["decision"] == "accept"
    assert all(job.stage is not Stage.REVIEW for job in repo.queued_jobs())

    metadata["accept_quorum_waiver_attestation"] = "hmac-sha256:forged"
    with pytest.raises(ValueError, match="accept_quorum_missing"):
        WorkflowEngine(provider=panel)._run_editorial(
            RuntimeJob(
                target_object_id=submission.id,
                stage=Stage.EDITORIAL,
                payload={"review_id": review.id},
            ),
            repo,
        )

    class TimedOutPrimary(DeterministicProvider):
        def complete(self, request: ProviderRequest) -> ProviderResult:  # noqa: ARG002
            return ProviderResult(
                ok=False,
                error=ProviderError(
                    error_class=ProviderErrorClass.TIMEOUT, message="timed out"
                ),
            )

    openrouter_primary = FallbackProvider(
        primary=TimedOutPrimary(provider="minimax", model="MiniMax-M3"),
        fallback=DeterministicProvider(
            provider="openrouter", model="mistralai/mistral-small-2603"
        ),
    )
    unsafe = ReviewerPanel(
        primary=openrouter_primary,
        sparring=OpenRouterProvider(api_key="test", model="google/gemma-4-31b-it"),
        fallback=OpenRouterProvider(
            api_key="test", model="mistralai/mistral-small-2603"
        ),
        allow_sparring_billing_skip=True,
    ).complete(ProviderRequest(system_prompt="s", user_prompt="u", prompt_version="v"))
    assert unsafe.ok is False


def test_single_vote_waiver_requires_exact_billing_skip_receipt() -> None:
    receipt = {
        "provider": "reviewer-panel",
        "route": "sparring_billing_skipped_primary_used",
        "accept_quorum_count": 1,
        "accept_quorum_models": ["MiniMax-M3"],
        "accept_quorum_waiver": "sparring_billing_unavailable",
        "ops_flag": "sparring_billing_skipped",
        "secondary_review_skipped": True,
        "sparring_provider": "openrouter",
        "sparring_http_status": 402,
        "primary_fallback_used": False,
        "winner_provider": "minimax",
    }

    assert not accept_quorum_satisfied(receipt)
    assert accept_quorum_satisfied(receipt, allow_billing_waiver=True)
    assert not ReviewerPanel(
        primary=DeterministicProvider(provider="minimax", model="MiniMax-M3"),
        sparring=OpenRouterProvider(api_key="test", model="google/gemma-4-31b-it"),
        fallback=OpenRouterProvider(
            api_key="test", model="mistralai/mistral-small-2603"
        ),
        allow_sparring_billing_skip=True,
    ).billing_skip_receipt_valid(
        {
            "provider": "reviewer-panel",
            "route": "consensus",
            "accept_quorum_count": 2,
            "accept_quorum_models": ["MiniMax-M3", "google/gemma-4-31b-it"],
        }
    )
    assert not accept_quorum_satisfied(
        {**receipt, "route": "sparring_failed_primary_used"}
    )
    assert not accept_quorum_satisfied(
        {**receipt, "accept_quorum_waiver": "timeout"},
        allow_billing_waiver=True,
    )
    assert not accept_quorum_satisfied(
        {**receipt, "secondary_review_skipped": False},
        allow_billing_waiver=True,
    )
    assert not accept_quorum_satisfied(
        {**receipt, "provider": "single-reviewer"},
        allow_billing_waiver=True,
    )


def test_non_openrouter_sparring_fallback_cannot_claim_billing_skip() -> None:
    class UnavailableOpenRouter(OpenRouterProvider):
        def complete(self, request: ProviderRequest) -> ProviderResult:  # noqa: ARG002
            return ProviderResult(
                ok=False,
                error=ProviderError(
                    error_class=ProviderErrorClass.PROVIDER_UNAVAILABLE,
                    message="unavailable",
                    status_code=503,
                ),
            )

    class ForeignBillingProvider:
        provider = "foreign"
        model = "foreign"

        def complete(self, request: ProviderRequest) -> ProviderResult:  # noqa: ARG002
            return ProviderResult(
                ok=False,
                error=ProviderError(
                    error_class=ProviderErrorClass.BILLING,
                    message="payment required",
                    status_code=402,
                ),
            )

    billing = ForeignBillingProvider()
    result = ReviewerPanel(
        primary=DeterministicProvider(provider="minimax", model="MiniMax-M3"),
        sparring=FallbackProvider(
            primary=UnavailableOpenRouter(
                api_key="test", model="google/gemma-4-31b-it"
            ),
            fallback=billing,
        ),
        fallback=billing,
        allow_sparring_billing_skip=True,
    ).complete(ProviderRequest(system_prompt="s", user_prompt="u", prompt_version="v"))

    assert result.ok is False
    assert result.error is not None
    assert "panel_decision_quorum_unavailable" in result.error.message


def test_duplicate_reviewer_models_cannot_forge_accept_quorum() -> None:
    class ForgedPanel:
        provider = "reviewer-panel"
        model = "same-model"
        enforces_accept_quorum = True

        def __init__(self, metadata: dict[str, object] | None = None) -> None:
            self.metadata = metadata or {
                "accept_quorum_count": 2,
                "accept_quorum_models": ["same-model", "same-model"],
            }

        def complete(self, request: ProviderRequest) -> ProviderResult:
            return ProviderResult(
                ok=True,
                response=ProviderResponse(
                    text=json.dumps(_review_payload("accept")),
                    provider=self.provider,
                    model=self.model,
                    usage=ProviderUsage(),
                    metadata=self.metadata,
                ),
            )

    submission = _calibration_submission(
        InMemoryRuntimeRepository(), recommendation="accept"
    )
    with pytest.raises(ValueError, match="accept_quorum_missing"):
        WorkflowEngine(provider=ForgedPanel())._review_submission(submission)
    forged_waiver = {
        "route": "sparring_billing_skipped_primary_used",
        "ops_flag": "sparring_billing_skipped",
        "secondary_review_skipped": True,
        "accept_quorum_count": 1,
        "accept_quorum_models": ["MiniMax-M3"],
        "accept_quorum_waiver": "sparring_billing_unavailable",
        "sparring_provider": "openrouter",
        "sparring_http_status": 402,
        "primary_fallback_used": False,
        "winner_provider": "minimax",
    }
    with pytest.raises(ValueError, match="accept_quorum_missing"):
        WorkflowEngine(provider=ForgedPanel(forged_waiver))._review_submission(
            submission
        )


def test_two_models_from_one_provider_do_not_form_accept_quorum() -> None:
    assert not accept_quorum_satisfied(
        {
            "provider": "reviewer-panel",
            "accept_quorum_count": 2,
            "accept_quorum_models": ["legacy-a", "legacy-b"],
        }
    )
    assert not accept_quorum_satisfied(
        {
            "provider": "reviewer-panel",
            "accept_quorum_count": 1,
            "accept_quorum_models": ["model-a", "model-b"],
            "accept_quorum_identities": ["provider-a:model-a", "provider-a:model-b"],
            "accept_quorum_providers": ["provider-a"],
        }
    )
    assert accept_quorum_satisfied(
        {
            "provider": "reviewer-panel",
            "accept_quorum_count": 2,
            "accept_quorum_models": ["model-a", "model-b"],
            "accept_quorum_identities": ["provider-a:model-a", "provider-b:model-b"],
            "accept_quorum_providers": ["provider-a", "provider-b"],
        }
    )


@pytest.mark.parametrize("count", ["invalid", [], {}])
def test_malformed_or_blank_accept_quorum_fails_closed(count: object) -> None:
    assert not accept_quorum_satisfied(
        {
            "provider": "reviewer-panel",
            "accept_quorum_count": count,
            "accept_quorum_models": ["reviewer-a", "", None],
        }
    )


def test_panel_and_workflow_reject_score_below_accept_floor() -> None:
    class StaticReviewProvider:
        provider = "static-reviewer"

        def __init__(self, model: str) -> None:
            self.model = model

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
                    usage=ProviderUsage(
                        input_tokens=10, output_tokens=10, cost_usd=0.0
                    ),
                ),
            )

    repo = InMemoryRuntimeRepository()
    submission = _calibration_submission(repo, recommendation="accept")
    panel = ReviewerPanel(
        primary=StaticReviewProvider("static-primary"),
        sparring=StaticReviewProvider("static-sparring"),
        fallback=StaticReviewProvider("static-fallback"),
    )
    engine = WorkflowEngine(provider=panel)

    intake_job = repo.enqueue_job(
        RuntimeJob(
            target_object_id=submission.id,
            stage=Stage.INTAKE,
            payload={"domain_slug": "longevity"},
        )
    )
    engine.handle_job(intake_job, repo)
    repo.complete_job(intake_job.id)

    review_job = repo.claim_next_job()
    with pytest.raises(ValueError, match="accept_rubric_too_weak"):
        engine.handle_job(review_job, repo)
    assert repo.list_objects(ObjectType.REVIEW) == []


def test_minor_issues_only_revise_without_action_is_invalid() -> None:
    repo = InMemoryRuntimeRepository()
    submission = _calibration_submission(repo, recommendation="revise")
    engine = WorkflowEngine(provider=_minor_only_revise_provider())

    intake_job = repo.enqueue_job(
        RuntimeJob(
            target_object_id=submission.id,
            stage=Stage.INTAKE,
            payload={"domain_slug": "longevity"},
        )
    )
    engine.handle_job(intake_job, repo)
    repo.complete_job(intake_job.id)

    review_job = repo.claim_next_job()
    with pytest.raises(ValueError, match="revise_missing_required_revisions"):
        engine.handle_job(review_job, repo)
    assert repo.list_objects(ObjectType.REVIEW) == []


def test_existing_minor_issues_only_review_remains_revise() -> None:
    repo = InMemoryRuntimeRepository()
    submission = _calibration_submission(repo, recommendation="revise")
    review = bound_review(
        repo,
        submission,
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
        },
    )

    engine = WorkflowEngine(provider=_rubric_revise_provider())
    editorial_job = RuntimeJob(
        target_object_id=submission.id,
        stage=Stage.EDITORIAL,
        payload={"review_id": review.id, "domain_slug": "longevity"},
    )
    result = engine.handle_job(editorial_job, repo)

    decision = repo.list_objects(ObjectType.DECISION)[0]
    assert result["terminal_decision"] == Decision.REVISE.value
    assert decision.metadata["decision"] == Decision.REVISE.value
    assert "recommendation_calibration" not in decision.metadata
    assert repo.queued_jobs() == []


def test_calibration_revise_rubric_fields_stored() -> None:
    repo = InMemoryRuntimeRepository()
    submission = _calibration_submission(repo, recommendation="revise")
    engine = WorkflowEngine(provider=_rubric_revise_provider())

    intake_job = repo.enqueue_job(
        RuntimeJob(
            target_object_id=submission.id,
            stage=Stage.INTAKE,
            payload={"domain_slug": "longevity"},
        )
    )
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

    intake_job = repo.enqueue_job(
        RuntimeJob(
            target_object_id=submission.id,
            stage=Stage.INTAKE,
            payload={"domain_slug": "longevity"},
        )
    )
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
                            "major_issues": [
                                "Key findings are descriptive rather than synthetic."
                            ],
                            "minor_issues": [],
                            "required_revisions": [
                                "Rewrite key findings to integrate evidence."
                            ],
                            "claim_support_verdict": "partially_supported",
                            "overclaim_verdict": "mild",
                            "synthesis_quality_verdict": "adequate",
                            "review_markdown": "Panel consensus: revise for synthesis quality.",
                        }
                    ),
                    provider=self.provider,
                    model=self.model,
                    usage=ProviderUsage(
                        input_tokens=33, output_tokens=12, cost_usd=0.9
                    ),
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

    intake_job = repo.enqueue_job(
        RuntimeJob(
            target_object_id=submission.id,
            stage=Stage.INTAKE,
            payload={"domain_slug": "longevity"},
        )
    )
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
    assert review.metadata["major_issues"] == [
        "Key findings are descriptive rather than synthetic."
    ]


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
                            "major_issues": [
                                "Key findings are descriptive rather than synthetic."
                            ],
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
                    usage=ProviderUsage(
                        input_tokens=10, output_tokens=10, cost_usd=0.0
                    ),
                ),
            )

    repo = InMemoryRuntimeRepository()
    submission = _calibration_submission(repo, recommendation="accept")
    engine = WorkflowEngine(provider=WeakAcceptProvider())
    intake_job = repo.enqueue_job(
        RuntimeJob(
            target_object_id=submission.id,
            stage=Stage.INTAKE,
            payload={"domain_slug": "longevity"},
        )
    )
    engine.handle_job(intake_job, repo)
    repo.complete_job(intake_job.id)

    review_job = repo.claim_next_job()
    assert review_job is not None
    with pytest.raises(
        ValueError,
        match="accept_rubric_too_weak|accept_has_major_issues|accept_claim_support_not_supported|accept_has_overclaim|accept_has_required_revisions",
    ):
        engine.handle_job(review_job, repo)


@pytest.mark.parametrize(
    ("required_revisions", "expected_error"),
    [
        ([], "revise_missing_required_revisions"),
        (None, "missing_required_revisions"),
        ("__missing__", "missing_required_revisions"),
    ],
)
def test_non_actionable_revise_is_rejected_before_review_storage(
    required_revisions: object, expected_error: str
) -> None:
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
                    usage=ProviderUsage(
                        input_tokens=10, output_tokens=10, cost_usd=0.0
                    ),
                ),
            )

    repo = InMemoryRuntimeRepository()
    submission = _calibration_submission(repo, recommendation="revise")
    engine = WorkflowEngine(provider=BadReviseProvider())
    intake_job = repo.enqueue_job(
        RuntimeJob(
            target_object_id=submission.id,
            stage=Stage.INTAKE,
            payload={"domain_slug": "longevity"},
        )
    )
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
                    usage=ProviderUsage(
                        input_tokens=10, output_tokens=10, cost_usd=0.0
                    ),
                ),
            )

    repo = InMemoryRuntimeRepository()
    submission = _calibration_submission(repo, recommendation="accept")
    engine = WorkflowEngine(provider=LegacyProvider())
    intake_job = repo.enqueue_job(
        RuntimeJob(
            target_object_id=submission.id,
            stage=Stage.INTAKE,
            payload={"domain_slug": "longevity"},
        )
    )
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
    result = engine.handle_job(
        RuntimeJob(
            target_object_id=submission.id,
            stage=Stage.INTAKE,
            payload={"domain_slug": "longevity"},
        ),
        repo,
    )
    assert (
        result.get("terminal_decision") == Decision.REVISE.value
    )  # author-correctable intake defect
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
    result = engine.handle_job(
        RuntimeJob(
            target_object_id=submission.id,
            stage=Stage.INTAKE,
            payload={"domain_slug": "longevity"},
        ),
        repo,
    )
    assert result.get("next_stage") == Stage.REVIEW.value
