import json
from typing import Any

import pytest

from contracts import ArticleType, Decision, ObjectType, ProviderUsage, ResearchObject, RuntimeJob, Stage
from runtime_core.integrity_client import check_integrity
from runtime_core.providers import ProviderRequest, ProviderResponse, ProviderResult
from runtime_core.repos import InMemoryRuntimeRepository
from runtime_core.workflow import WorkflowEngine


def _sections() -> dict[str, str]:
    return {
        "Research Question": "This synthesis asks a bounded, decision-relevant question about recent evidence, target populations, comparator conditions, intended outcomes, and methodological limits, and it stays narrow enough that another reviewer could reproduce the scope, publication window, inclusion logic, and decision frame without inventing missing assumptions, broadening the intervention target, or silently changing the evidence standard.",
        "Search Summary": "Searches covered PubMed and review corpora, with a documented date window, explicit inclusion logic, and a clear narrowing rule that explains why the retained receipts best match the scoped research question.",
        "Evidence Landscape": "The bundle includes review-level and primary evidence so the reader can see the balance of stronger and more applied material, and where individual studies still shape the remaining uncertainty.",
        "Key Findings": "The key findings integrate the current evidence into bounded conclusions instead of stitching raw snippets together, and distinguish stronger review-level support from tentative primary-study signals.",
        "Limitations": "The main limits are rapid-review scope, incomplete coverage, heterogeneity across evidence units, and the risk that a synthetic bundle omits conflicting sources that could materially change certainty.",
        "Gaps Identified": "No adequately powered human RCT has tested this specific intervention for the primary endpoints reported in non-human models, leaving a translational gap between animal evidence and clinical applicability.",
        "Conclusion": "The current evidence supports a structured MVP publication, but only with explicit uncertainty, honest limits on reproducibility, and no overclaiming beyond what the retained bundle can directly justify.",
    }


def _source_bundle() -> list[dict[str, object]]:
    return [
        {"title": f"Integrity source {index}", "year": 2024 - (index % 5), "evidence_type": "review" if index <= 6 else "primary"}
        for index in range(1, 13)
    ]


def _submission(repo: InMemoryRuntimeRepository) -> ResearchObject:
    return repo.create_object(
        ResearchObject(
            object_type=ObjectType.SUBMISSION,
            title="Integrity test submission",
            metadata={
                "article_type": ArticleType.RAPID_EVIDENCE_SYNTHESIS.value,
                "domain_slug": "longevity",
                "abstract": "Bounded external submission.",
                "sections": _sections(),
                "source_bundle": _source_bundle(),
                "core_claims_resolved": True,
                "author_agent_id": "agent-integrity",
            },
        )
    )


def _accept_review() -> dict[str, Any]:
    return {
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
        "review_markdown": "Integrity path accepted.",
    }


class AcceptProvider:
    provider = "accept-provider"
    model = "accept-model"

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, request: ProviderRequest) -> ProviderResult:
        self.calls += 1
        return ProviderResult(
            ok=True,
            response=ProviderResponse(
                text=json.dumps(_accept_review()),
                provider=self.provider,
                model=self.model,
                usage=ProviderUsage(input_tokens=5, output_tokens=4, cost_usd=0.01),
            ),
        )


def _claim_next(repo: InMemoryRuntimeRepository) -> RuntimeJob:
    job = repo.claim_next_job()
    assert job is not None
    return job


def test_integrity_service_down_fails_open(monkeypatch: pytest.MonkeyPatch) -> None:
    class BrokenClient:
        def __init__(self, timeout: float) -> None:
            self.timeout = timeout

        def __enter__(self) -> "BrokenClient":
            raise TimeoutError("integrity timeout")

        def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

    monkeypatch.setenv("RESEARKA_INTEGRITY_ENABLED", "1")
    monkeypatch.setattr("runtime_core.integrity_client.httpx.Client", BrokenClient)

    assert check_integrity({"submission_id": "sub-1"}) is None


def test_integrity_malformed_json_fails_open(monkeypatch: pytest.MonkeyPatch) -> None:
    class BadJsonResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            raise ValueError("not json")

    class BadJsonClient:
        def __init__(self, timeout: float) -> None:
            self.timeout = timeout

        def __enter__(self) -> "BadJsonClient":
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

        def post(self, url: str, *, json: dict[str, Any], headers: dict[str, str]) -> BadJsonResponse:
            return BadJsonResponse()

    monkeypatch.setenv("RESEARKA_INTEGRITY_ENABLED", "1")
    monkeypatch.setattr("runtime_core.integrity_client.httpx.Client", BadJsonClient)

    assert check_integrity({"submission_id": "sub-1"}) is None


def test_integrity_missing_recommendation_continues_to_review(monkeypatch: pytest.MonkeyPatch) -> None:
    repo = InMemoryRuntimeRepository()
    submission = _submission(repo)
    monkeypatch.setattr("runtime_core.workflow.check_integrity", lambda payload: {"duplication_score": 0.91})

    result = WorkflowEngine(provider=AcceptProvider()).handle_job(RuntimeJob(target_object_id=submission.id, stage=Stage.INTAKE), repo)
    updated = repo.get_object(submission.id)

    assert result["next_stage"] == Stage.REVIEW.value
    assert [job.stage for job in repo.queued_jobs()] == [Stage.REVIEW]
    assert updated is not None
    assert updated.metadata["integrity"]["recommendation"] == "pass"
    assert updated.metadata["integrity"]["duplication_score"] == 0.91


@pytest.mark.parametrize("recommendation", [Decision.REJECT.value, Decision.REVISE.value])
def test_integrity_reject_or_revise_skips_reviewer_panel(monkeypatch: pytest.MonkeyPatch, recommendation: str) -> None:
    repo = InMemoryRuntimeRepository()
    submission = _submission(repo)
    provider = AcceptProvider()
    monkeypatch.setattr(
        "runtime_core.workflow.check_integrity",
        lambda payload: {
            "recommendation": recommendation,
            "matched_publication_id": "pub-existing",
            "duplication_score": 0.94,
            "breakdown": {"abstract": 0.92},
            "feedback_for_agent": "This submission is too close to an existing accepted artifact.",
        },
    )

    result = WorkflowEngine(provider=provider).handle_job(RuntimeJob(target_object_id=submission.id, stage=Stage.INTAKE), repo)
    decision = repo.children_of(submission.id, ObjectType.DECISION)[0]

    assert result["terminal_decision"] == recommendation
    assert repo.queued_jobs() == []
    assert repo.children_of(submission.id, ObjectType.REVIEW) == []
    assert provider.calls == 0
    assert decision.metadata["failure_category"] == "integrity_duplicate"
    assert decision.metadata["integrity"]["matched_publication_id"] == "pub-existing"
    assert decision.metadata["integrity"]["duplication_score"] == 0.94


def test_integrity_indexes_once_after_accept(monkeypatch: pytest.MonkeyPatch) -> None:
    repo = InMemoryRuntimeRepository()
    submission = _submission(repo)
    indexed: list[dict[str, Any]] = []
    monkeypatch.setenv("RESEARKA_V2_OSF_ENABLED", "0")
    monkeypatch.setattr("runtime_core.workflow.check_integrity", lambda payload: {"recommendation": "pass", "similarity_score": 0.08})
    monkeypatch.setattr("runtime_core.workflow.index_integrity", lambda payload: indexed.append(payload))

    engine = WorkflowEngine(provider=AcceptProvider())
    intake_job = repo.enqueue_job(RuntimeJob(target_object_id=submission.id, stage=Stage.INTAKE))
    engine.handle_job(intake_job, repo)
    repo.complete_job(intake_job.id)
    review_job = _claim_next(repo)
    engine.handle_job(review_job, repo)
    repo.complete_job(review_job.id)
    editorial_job = _claim_next(repo)
    engine.handle_job(editorial_job, repo)
    repo.complete_job(editorial_job.id)
    publish_job = _claim_next(repo)
    publish_result = engine.handle_job(publish_job, repo)

    assert len(indexed) == 1
    assert indexed[0]["publication_id"] == publish_result["publication_id"]
    assert indexed[0]["submission_id"] == submission.id
    assert indexed[0]["domain"] == "longevity"
    publication = repo.get_object(publish_result["publication_id"])
    assert publication is not None
    assert publication.metadata["integrity"]["recommendation"] == "pass"
    assert publication.metadata["integrity"]["similarity_score"] == 0.08


def test_integrity_decisions_are_not_indexed(monkeypatch: pytest.MonkeyPatch) -> None:
    repo = InMemoryRuntimeRepository()
    submission = _submission(repo)
    indexed: list[dict[str, Any]] = []
    monkeypatch.setattr("runtime_core.workflow.index_integrity", lambda payload: indexed.append(payload))
    monkeypatch.setattr("runtime_core.workflow.check_integrity", lambda payload: {"recommendation": Decision.REJECT.value})

    WorkflowEngine(provider=AcceptProvider()).handle_job(RuntimeJob(target_object_id=submission.id, stage=Stage.INTAKE), repo)

    assert indexed == []
