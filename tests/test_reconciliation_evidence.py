from datetime import datetime, timedelta, timezone
import json

import httpx
import pytest

from contracts import ObjectType, ResearchObject, RuntimeJob, Stage, ProviderUsage, FailureClass
from runtime_core.doi_resolver import _pmc_full_texts, _normalized_text, _tokens
from runtime_core.evidence_quality import claim_assessment, claim_candidates, quantitative_claim_candidates, support_for_claim, evidence_profile
from runtime_core.failure_classifier import classify_failure_reason
from runtime_core.ops import reconcile_stalled_submissions, operational_alerts
from runtime_core.providers import ProviderRequest, ProviderResponse, ProviderResult
from runtime_core.reviewer_panel import ReviewerPanel
from runtime_core.review_contract import MODEL_QUORUM_POLICY, model_quorum_metadata
from runtime_core.repos import InMemoryRuntimeRepository
from runtime_core.workflow import WorkflowEngine, _revision_context, _table_evidence_revisions, _review_verification_sources, _authoritative_bundle
from apps.runtime_api.app import _failure_stage
from tests.support import accepted_publish_job
from tests.test_model_quorum import SOL, TERRA
from tests.test_runtime_core import _authenticated_workflow_submission, _review_payload


@pytest.mark.parametrize("passage,expected", [
    ("Drugalpha reduced body weight by 5 kg.", True),
    ("Drugalpha increased body weight by 5 kg.", False),
    ("Drugalpha reduced blood pressure by 5 kg.", False),
    ("Drugbeta reduced body weight by 5 kg.", False),
    ("Drugalpha reduced body weight. The difference was 5 kg.", True),
    ("Drugalpha reduced body weight by 5,000 g.", True),
    ("Drugalpha reduced body weight by 5 mg.", False),
])
def test_claim_reconciliation_discriminates_outcome_direction_units(passage, expected):
    claim = "Drugalpha reduced body weight by 5 kg [1]."
    sources = [{"doi": "10.1234/test", "evidence_span": passage}]
    assert bool(support_for_claim(claim, sources, require_quantitative_agreement=True)) is expected
    result = claim_assessment(claim, sources)
    expected_status = "SUPPORTED" if expected else "NEEDS_SEMANTIC_REVIEW"
    assert result["status"] == expected_status
    assert result["sources"] == ["10.1234/test"]


def test_adjacent_population_sentence_and_short_claim_are_not_dropped():
    sources = [{"evidence_span": "Drugalpha was given to adults. Body weight decreased by 5 kg."}]
    claim = "Drugalpha decreased body weight by 5 kg in adults [1]."
    assert support_for_claim(claim, sources, require_quantitative_agreement=True)
    assert quantitative_claim_candidates("Mortality was 48% [1].") == ["Mortality was 48% [1]."]
    assert claim_candidates("Mortality was 48% [1].") == ["Mortality was 48% [1]."]
    assert not quantitative_claim_candidates("2022\n[1]")


def test_unavailable_text_is_not_called_a_contradiction():
    assert claim_assessment("Mortality was 48% [1].", [{"doi": "10.1234/test"}])["status"] == "INSUFFICIENT_SOURCE_TEXT"
    assert not support_for_claim("Mortality was 48% [1].", [{"evidence_span": "Survival was 48%."}])


@pytest.mark.parametrize("endpoint,value,expected", [("survival", "100%", True), ("mortality", "48%", False), ("mortality", "100%", False)])
def test_table_endpoint_checked_and_feedback_locates_row(endpoint, value, expected):
    table = f"| Study | Endpoint | Value |\n| --- | --- | --- |\n| Smith 2022 | {endpoint} | {value} |"
    sources = [{"doi": "10.1234/test", "cited_as": "Smith 2022", "evidence_span": "Survival was 100% in both groups."}]
    revisions = _table_evidence_revisions({"Evidence": table}, sources)
    assert (not revisions) is expected
    if not expected:
        assert "Evidence, line 3" in revisions[0]
        assert "10.1234/test" in revisions[0]
        assert "Survival was 100%" in revisions[0]


def test_markup_normalization_retains_inequalities_and_tail():
    text = "<p>Risk p &lt; 0.05, age > 65; <b>survival</b> was 100%.</p>"
    assert _normalized_text(text) == "risk p 0 05 age 65 survival was 100"
    assert "65" in _normalized_text(text)
    assert "survival" in _tokens(text)
    assert "100" in _normalized_text(text)


@pytest.mark.parametrize("doi,expected", [("10.1234/test", True), ("10.1234/wrong", False)])
def test_pmc_fallback_retries_but_never_accepts_wrong_identity(monkeypatch, doi, expected):
    calls = []
    monkeypatch.setattr("runtime_core.doi_resolver.time.sleep", lambda _: None)
    def serve(request):
        calls.append(str(request.url))
        if "idconv" in str(request.url):
            return httpx.Response(200, json={"records": []})
        if "eutils" in str(request.url):
            return httpx.Response(503)
        return httpx.Response(200, text=f"<article><front><article-meta><article-id pub-id-type='pmcid'>PMC123456</article-id><article-id pub-id-type='doi'>{doi}</article-id></article-meta></front><body><p>Survival was 100%.</p><ref><article-id pub-id-type='doi'>10.1234/cited</article-id></ref></body></article>")
    with httpx.Client(transport=httpx.MockTransport(serve)) as client:
        result = _pmc_full_texts(client, [{"doi": "10.1234/test", "source_record_locator": "PMC123456", "evidence_origin": "full_text"}])
    assert bool(result) is expected
    assert sum("eutils" in url for url in calls) >= 2
    assert any("fullTextXML" in url for url in calls)


def test_accepted_missing_job_recovers_with_real_lineage(monkeypatch):
    monkeypatch.delenv("RESEARKA_DISABLED_AGENT_IDS", raising=False)
    repo = InMemoryRuntimeRepository()
    repo.create_api_key("agent-demo")
    submission = _authenticated_workflow_submission(repo)
    original = accepted_publish_job(repo, submission)
    jobs = reconcile_stalled_submissions(repo, now=datetime.now(timezone.utc) + timedelta(minutes=5))
    assert len(jobs) == 1 and jobs[0].stage == Stage.PUBLISH
    assert jobs[0].payload["decision_id"] == original.payload["decision_id"]
    assert jobs[0].payload["canonical_package_hash"] == original.payload["canonical_package_hash"]
    result = WorkflowEngine()._run_publish(jobs[0], repo)
    assert result["publication_id"]


def test_editorial_override_not_mislabelled_reviewer_failure():
    decision = ResearchObject(object_type=ObjectType.DECISION, title="Revision", metadata={"claim_trace_guard": True})
    review = ResearchObject(object_type=ObjectType.REVIEW, title="Accept")
    assert _failure_stage(decision, review) == "claim_trace_guard"


@pytest.mark.parametrize("newer_decision", [False, True])
def test_editorial_retry_finishes_supersession_without_reopening_old_decision(monkeypatch, newer_decision):
    repo = InMemoryRuntimeRepository()
    submission = _authenticated_workflow_submission(repo)
    original = accepted_publish_job(repo, submission)
    original_id = original.payload["decision_id"]
    review = repo.create_object(ResearchObject(
        object_type=ObjectType.REVIEW, parent_object_id=submission.id, title="Reassessment",
        metadata={"recommendation": "revise", "reviewed_package_hash": original.payload["canonical_package_hash"]},
    ))
    job = RuntimeJob(target_object_id=submission.id, stage=Stage.EDITORIAL, payload={"review_id": review.id})
    engine = WorkflowEngine()
    update = repo.update_object_metadata

    def interrupt_supersession(object_id, metadata):
        if object_id == original_id and metadata.get("superseded_by"):
            raise RuntimeError("interrupted_supersession")
        return update(object_id, metadata)

    monkeypatch.setattr(repo, "update_object_metadata", interrupt_supersession)
    with pytest.raises(RuntimeError, match="interrupted_supersession"):
        engine._run_editorial(job, repo)
    decisions = repo.children_of(submission.id, ObjectType.DECISION)
    assert len(decisions) == 2
    interrupted_id = decisions[-1].id
    monkeypatch.setattr(repo, "update_object_metadata", update)
    if newer_decision:
        newest = engine._run_editorial(RuntimeJob(
            target_object_id=submission.id, stage=Stage.EDITORIAL,
            payload={"review_id": review.id},
        ), repo)
        latest_id = newest["created_object_id"]
    else:
        latest_id = interrupted_id

    result = engine._run_editorial(job, repo)
    assert result["deduped"] is True
    assert result["created_object_id"] == interrupted_id
    assert repo.get_object(original_id).metadata["superseded_by"] == latest_id
    assert repo.get_object(latest_id).metadata.get("superseded_by") is None
    assert len(repo.children_of(submission.id, ObjectType.DECISION)) == (3 if newer_decision else 2)


def test_revision_context_is_server_owned_and_contains_original_issues():
    repo = InMemoryRuntimeRepository()
    parent = repo.create_object(ResearchObject(object_type=ObjectType.SUBMISSION, title="Original", metadata={"authenticated_agent_id": "owner", "sections": {"Methods": "old", "Results": "same"}}))
    decision = repo.create_object(ResearchObject(object_type=ObjectType.DECISION, parent_object_id=parent.id, title="Revise", metadata={"required_revisions": ["Explain endpoint definition"]}))
    revised = ResearchObject(object_type=ObjectType.SUBMISSION, title="Revised", metadata={"authenticated_agent_id": "owner", "parent_submission_id": parent.id, "sections": {"Methods": "new", "Results": "same"}})
    context = _revision_context(repo, revised)
    assert context["previous_decision_id"] == decision.id
    assert context["required_revisions"] == ["Explain endpoint definition"]
    assert context["changed_sections"] == ["Methods"]
    revised.metadata["authenticated_agent_id"] = "other"
    assert _revision_context(repo, revised) == {}


class _SequenceReviewer:
    provider = "codex"

    def __init__(self, model, votes):
        self.model, self.votes, self.calls = model, votes, 0

    def complete(self, request):
        vote = self.votes[self.calls]
        self.calls += 1
        return ProviderResult(ok=True, response=ProviderResponse(provider=self.provider, model=self.model, text=json.dumps(_review_payload(vote)), usage=ProviderUsage()))


@pytest.mark.parametrize("final", ["accept", "reject"])
def test_bounded_adjudication_uses_two_final_valid_votes_no_paid_backup(final):
    primary = _SequenceReviewer(SOL, ["accept", "accept"])
    secondary = _SequenceReviewer(TERRA, ["reject", final])
    backup = _SequenceReviewer("forbidden-backup", [])
    panel = ReviewerPanel(primary=primary, sparring=secondary, fallback=backup, quorum_policy=MODEL_QUORUM_POLICY)
    result = panel.complete(ProviderRequest(system_prompt="Review", user_prompt="manuscript", prompt_version="test"))
    assert primary.calls == secondary.calls == 2
    assert backup.calls == 0
    if final == "accept":
        assert result.ok
        receipts = result.response.metadata["reviewer_receipts"]
        assert len(receipts) == 2
        assert model_quorum_metadata(receipts)["accept_quorum_count"] == 2
        assert result.response.metadata["adjudication_rounds"] == 1
    else:
        assert not result.ok
        assert result.error.message.startswith("review_disagreement:")
        assert classify_failure_reason(result.error.message) is FailureClass.REVIEW_DISAGREEMENT


def test_publishing_state_is_alerted():
    repo = InMemoryRuntimeRepository()
    repo.create_object(ResearchObject(object_type=ObjectType.PUBLICATION, title="Stranded", metadata={"publication_state": "PUBLISHING"}, created_at=datetime.now(timezone.utc) - timedelta(days=2)))
    assert any(alert["code"] == "publication_delivery_stall" for alert in operational_alerts(repo))


def test_review_path_requests_independent_claim_and_table_passages_without_mutating_bundle(monkeypatch):
    sources = [{"doi": "10.1234/test", "cited_as": "Smith 2022", "evidence_span": "Recruitment."}]
    submission = ResearchObject(object_type=ObjectType.SUBMISSION, title="Trial", metadata={
        "abstract": "Mortality was 48% [1].", "source_bundle": sources,
        "sections": {"Evidence": "| Study | Endpoint | Value |\n| --- | --- | --- |\n| Smith 2022 | survival | 100% |"},
    })
    enriched = _review_verification_sources(submission, sources)
    assert enriched[0]["verification_claims"] == ["Mortality was 48% [1].", "survival was 100%."]
    assert "verification_claims" not in sources[0]
    submission.metadata["source_verification"] = {"claim_checks": [
        {"identity": "doi:10.1234/test", "passage": "Survival was 100% in both groups."},
        {"identity": "doi:10.1234/other", "passage": "Mortality was 48%."},
    ]}
    reconciled = _authoritative_bundle(submission, sources)
    assert "Mortality" not in reconciled[0]["excerpt"]
    assert not _table_evidence_revisions(submission.metadata["sections"], reconciled)
    assert sources[0]["evidence_span"] == "Recruitment."
    reviewer = _SequenceReviewer(SOL, [])

    def inspect_request(request):
        assert '"table_evidence_checks": []' in request.user_prompt
        assert '"claim_evidence_checks": [' in request.user_prompt
        assert "Survival was 100% in both groups." in request.user_prompt
        assert request.context["materiality"]["sections"]["Abstract"] == "Mortality was 48% [1]."
        raise RuntimeError("review_input_checked")

    monkeypatch.setattr(reviewer, "complete", inspect_request)
    with pytest.raises(RuntimeError, match="review_input_checked"):
        WorkflowEngine(provider=reviewer)._review_submission(submission)


def test_protocol_context_does_not_count_as_primary_results():
    from contracts.submissions import run_submission_template_checks
    from contracts import ArticleType
    sources = [{"title": "Study protocol for a trial", "doi": "10.1234/protocol", "evidence_type": "primary",
                "publication_type": "study protocol", "directness": "protocol", "evidence_context": "context"}]
    gates = run_submission_template_checks(article_type=ArticleType.RESEARCH_SYNTHESIS.value, sections={}, source_bundle=sources)
    assert next(gate for gate in gates if gate.name == "source_role").passed
    assert not next(gate for gate in gates if gate.name == "minimum_citations").passed
    assert evidence_profile(text="", source_bundle=sources)["primary_source_ratio"] == 0
