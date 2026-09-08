import copy
import json

import pytest
from contracts import ProviderUsage
from contracts import ObjectType, ResearchObject

from runtime_core.evidence_quality import claim_assessment
from runtime_core.providers import ProviderRequest, ProviderResponse, ProviderResult
from runtime_core.review_contract import MODEL_QUORUM_POLICY, review_materiality_failure
from runtime_core.reviewer_panel import ReviewerPanel
from runtime_core.workflow import WorkflowEngine
from tests.test_model_quorum import SOL, TERRA
from tests.test_runtime_core import _review_payload


def material_review():
    payload = _review_payload("revise")
    payload.update(major_issues=[], required_revisions=["Correct the endpoint value."], material_findings=[{
        "issue": "Correct the endpoint value.", "materiality": "blocking", "kind": "incorrect",
        "has_material_impact": True,
        "section": "Results", "quote": "Mortality was 48%.",
        "impact": "The wrong endpoint changes the interpretation.",
        "correction": "Report the actual survival endpoint and value.",
    }])
    return payload


@pytest.mark.parametrize("repairability,reason,error", [
    (None, None, "reject_requires_finding_repairability"),
    ([], None, "reject_requires_finding_repairability"),
    ("scope_reset", "Too broad.", "reject_requires_finding_repairability"),
    ("bounded_revision", None, "reject_requires_irreparable_finding_use_revise"),
    ("new_evidence", "", "reject_requires_why_not_revise"),
    ("invalid_data", "...", "reject_requires_why_not_revise"),
    ("new_evidence", "No source supports any finding on the stated topic.", None),
    ("fabrication", "The underlying participant records are demonstrably invented.", None),
    ("invalid_data", "Corrupt measurements underpin every result and cannot be recovered.", None),
])
def test_reject_requires_an_explained_irreparable_finding(repairability, reason, error):
    payload = material_review()
    payload["recommendation"] = "reject"
    payload["material_findings"][0].update(repairability=repairability, why_not_revise=reason)
    assert review_materiality_failure(payload, CONTEXT) == error


def test_bounded_finding_cannot_hide_an_irreparable_one():
    payload = material_review()
    payload["recommendation"] = "reject"
    payload["material_findings"][0]["repairability"] = "bounded_revision"
    payload["required_revisions"].append("Replace invalid underlying data.")
    payload["material_findings"].append({
        **payload["material_findings"][0], "issue": "Replace invalid underlying data.",
        "repairability": "invalid_data", "why_not_revise": "The only measurements are corrupted.",
    })
    assert review_materiality_failure(payload, CONTEXT) is None


@pytest.mark.parametrize("corrected", [True, False])
def test_bounded_scope_reject_is_reconsidered_without_autoaccept_or_backup(corrected):
    issue = "Narrow the human claim to a preclinical evidence map and correct outcome labels."
    payload = material_review()
    payload.update(recommendation="reject", required_revisions=[issue])
    payload["material_findings"][0].update(
        issue=issue, section="Conclusion", quote="Human benefit is not established.",
        correction=issue, repairability="bounded_revision",
        impact="The title and outcome labels promise more than the acknowledged scope supports.",
    )
    revised = copy.deepcopy(payload)
    if corrected:
        revised.update(recommendation="revise", review_markdown="Revise to an evidence map; do not claim human benefit.")
    primary, secondary = Reviews(SOL, [payload, revised]), Reviews(TERRA, [payload, revised])
    backup = Reviews("forbidden", [])
    submission = ResearchObject(object_type=ObjectType.SUBMISSION, title="Senescence Subgroups", metadata={
        "sections": {"Conclusion": "Human benefit is not established."}, "source_bundle": [],
    })
    engine = WorkflowEngine(provider=ReviewerPanel(
        primary=primary, sparring=secondary, fallback=backup, quorum_policy=MODEL_QUORUM_POLICY))
    if corrected:
        decision, _, metadata = engine._review_submission(submission)
        assert decision == "revise"
        assert metadata["required_revisions"] == [issue]
        assert metadata["material_findings"] == revised["material_findings"]
    else:
        with pytest.raises(ValueError, match="review_disagreement"):
            engine._review_submission(submission)
    assert primary.calls == secondary.calls == 2
    assert backup.calls == 0


def test_supported_accept_remains_unchanged_by_repairability_check():
    payload = _review_payload("accept")
    assert review_materiality_failure(payload, CONTEXT) is None


@pytest.mark.parametrize("basis", ["new_evidence", "fabrication", "invalid_data"])
@pytest.mark.parametrize("reassessed", [True, False])
def test_reconsidered_revise_cannot_retain_an_irreparable_finding(basis, reassessed):
    rejected = material_review()
    rejected["recommendation"] = "reject"
    rejected["material_findings"][0]["repairability"] = basis
    revised = copy.deepcopy(rejected)
    revised["recommendation"] = "revise"
    revised["material_findings"][0].update(
        repairability="bounded_revision" if reassessed else basis,
        why_not_revise="" if reassessed else "Even a bounded evidence map cannot use these invalid data.",
    )
    primary, secondary = Reviews(SOL, [rejected, revised]), Reviews(TERRA, [rejected, revised])
    backup = Reviews("forbidden", [])
    submission = ResearchObject(object_type=ObjectType.SUBMISSION, title="Trial", metadata={
        "sections": CONTEXT["sections"], "source_bundle": [],
    })
    engine = WorkflowEngine(provider=ReviewerPanel(
        primary=primary, sparring=secondary, fallback=backup, quorum_policy=MODEL_QUORUM_POLICY))
    if reassessed:
        assert engine._review_submission(submission)[0] == "revise"
    else:
        with pytest.raises(ValueError, match="revise_conflicts_with_irreparable_finding"):
            engine._review_submission(submission)
    assert primary.calls == secondary.calls == 2
    assert backup.calls == 0


@pytest.mark.parametrize("recommendation", ["reject", " REJECT "])
def test_empty_or_case_variant_rejection_cannot_bypass_repairability(recommendation):
    payload = _review_payload("accept")
    payload["recommendation"] = recommendation
    assert review_materiality_failure(payload, CONTEXT) == "reject_requires_irreparable_finding_use_revise"


CONTEXT = {"sections": {"Results": "Mortality was 48%."}, "previous_issues": []}


@pytest.mark.parametrize("update,error", [
    ({"materiality": "optional"}, "blocking_finding_requires_material_impact"),
    ({"quote": "A quote that was invented."}, "material_issue_quote_not_in_section"),
    ({"impact": " "}, "material_issue_missing_impact_or_correction"),
    ({"section": "Discussion"}, "material_issue_quote_not_in_section"),
    ({"has_material_impact": False}, "blocking_finding_requires_material_impact"),
    ({"has_material_impact": "true"}, "blocking_finding_requires_material_impact"),
    ({"has_material_impact": 1}, "blocking_finding_requires_material_impact"),
])
def test_material_findings_require_material_impact_and_real_location(update, error):
    payload = material_review()
    payload["material_findings"][0].update(update)
    assert review_materiality_failure(payload, CONTEXT) == error


def test_missing_issue_and_reopened_resolved_issue_are_not_author_feedback():
    payload = material_review()
    assert review_materiality_failure(payload, CONTEXT) is None
    payload["required_revisions"].append("An unexplained new objection.")
    assert review_materiality_failure(payload, CONTEXT) == "blocking_issues_require_exact_material_findings"
    payload = material_review()
    context = {**CONTEXT, "previous_issues": ["Correct the endpoint value."]}
    assert review_materiality_failure(payload, context) == "material_issue_missing_revision_explanation"
    payload["material_findings"][0].update(change_reason="persisting", prior_issue="Correct the endpoint value.")
    assert review_materiality_failure(payload, context) is None
    payload["resolved_prior_issues"] = ["Correct the endpoint value."]
    assert review_materiality_failure(payload, context) == "resolved_issue_reopened_in_same_verdict"
    payload["material_findings"][0].update(change_reason="newly_discovered", why_new="Missed last review.")
    payload["material_findings"][0].pop("prior_issue")
    assert review_materiality_failure(payload, context) == "resolved_issue_reopened_in_same_verdict"


def test_concise_valid_correction_is_not_blocked_for_character_count():
    payload = material_review()
    payload["material_findings"][0].update(quote="48%", impact="Wrong unit.", correction="Add mg.",
                                         change_reason="newly_discovered", why_new="Missed last review.")
    payload["resolved_prior_issues"] = ["Old issue."]
    assert review_materiality_failure(payload, {**CONTEXT, "previous_issues": ["Old issue."]}) is None


def test_nonmaterial_omission_is_not_a_mandatory_revision():
    payload = material_review()
    issue = "Add a graphical abstract."
    payload["required_revisions"] = [issue]
    payload["material_findings"][0].update(issue=issue, kind="omission", has_material_impact=False,
        impact="Only presentation changes; validity and interpretation are unaffected.",
        correction="Add a graphical abstract for visual polish.")
    assert review_materiality_failure(payload, CONTEXT) == "blocking_finding_requires_material_impact"


def test_resolved_history_and_new_material_omission_require_accounting():
    payload = material_review()
    previous = "Explain the sampling method."
    context = {**CONTEXT, "previous_issues": [previous]}
    payload["material_findings"][0].update(
        kind="omission", section="Missing Methods", quote="", change_reason="newly_discovered",
        why_new="The newly available source reveals an omitted denominator.",
    )
    assert review_materiality_failure(payload, context) == "previous_material_issues_not_accounted_for"
    payload["resolved_prior_issues"] = [previous]
    assert review_materiality_failure(payload, context) is None
    payload["resolved_prior_issues"] = ["Invented prior issue"]
    assert review_materiality_failure(payload, context) == "unknown_resolved_prior_issue"


def test_workflow_preserves_structured_author_feedback():
    payload = material_review()
    previous = "Explain the sampling method."
    payload["material_findings"][0].update(
        change_reason="newly_introduced", why_new="The revised Results now report a different endpoint.")
    payload["resolved_prior_issues"] = [previous]
    panel = ReviewerPanel(primary=Reviews(SOL, [payload]), sparring=Reviews(TERRA, [payload]),
                          fallback=Reviews("forbidden", []), quorum_policy=MODEL_QUORUM_POLICY)
    submission = ResearchObject(object_type=ObjectType.SUBMISSION, title="Trial", metadata={
        "sections": CONTEXT["sections"], "source_bundle": [],
    })
    recommendation, _, metadata = WorkflowEngine(provider=panel)._review_submission(
        submission, revision_context={"required_revisions": [previous]})
    assert recommendation == "revise"
    assert metadata["material_findings"] == payload["material_findings"]
    assert metadata["resolved_prior_issues"] == [previous]
    assert panel.primary.requests == panel.sparring.requests
    prompt = panel.primary.requests[0]["system_prompt"]
    assert "Apply one repairability rule to every section and table" in prompt
    assert "specific fix or indispensable missing evidence under the repairability rule" in prompt
    assert "bounded required fix" not in prompt


class Reviews:
    provider = "codex"

    def __init__(self, model, responses):
        self.model, self.responses, self.calls = model, responses, 0
        self.requests = []

    def complete(self, request):
        self.requests.append(request.model_dump())
        response = self.responses[self.calls]
        self.calls += 1
        return ProviderResult(ok=True, response=ProviderResponse(
            provider="codex", model=self.model, text=json.dumps(response), usage=ProviderUsage()))


@pytest.mark.parametrize("repair", [True, False])
def test_optional_only_revise_gets_one_gpt_reconsideration_not_paid_backup_or_autoaccept(repair):
    optional = material_review()
    optional["material_findings"][0]["materiality"] = "optional"
    final = _review_payload("accept") if repair else optional
    primary, secondary = Reviews(SOL, [optional, final]), Reviews(TERRA, [optional, final])
    backup = Reviews("forbidden", [])
    result = ReviewerPanel(primary=primary, sparring=secondary, fallback=backup, quorum_policy=MODEL_QUORUM_POLICY).complete(
        ProviderRequest(system_prompt="Review", user_prompt="paper", prompt_version="test", context={"materiality": CONTEXT}))
    assert primary.calls == secondary.calls == 2
    assert backup.calls == 0
    assert result.ok is repair
    if repair:
        assert result.response.metadata["decision_quorum_count"] == 2
    else:
        assert result.error.message.startswith("review_disagreement:")
        assert "blocking_finding_requires_material_impact" in result.error.message


@pytest.mark.parametrize("passage,expected", [
    ("Drugalpha increased body weight by 5 kg.", "NEEDS_SEMANTIC_REVIEW"),
    ("Drugalpha reduced body weight by 12 kg.", "NEEDS_SEMANTIC_REVIEW"),
    ("Drugalpha reduced body weight by 5,000 g.", "SUPPORTED"),
    ("Drugbeta reduced body weight by 12 kg.", "NEEDS_SEMANTIC_REVIEW"),
    ("Drugalpha reduced body weight by 12 kg at 24 months.", "NEEDS_SEMANTIC_REVIEW"),
])
def test_precise_diagnostics_do_not_conflate_sources_or_timepoints(passage, expected):
    sources = [{"doi": "10.1234/trial", "evidence_span": passage}]
    before = copy.deepcopy(sources)
    result = claim_assessment("Drugalpha reduced body weight by 5 kg [1].", sources)
    assert result["status"] == expected
    assert result["claim_id"].startswith("claim_")
    assert result["comparisons"][0]["source_id"] == "source_1"
    assert sources == before
    if expected == "NEEDS_SEMANTIC_REVIEW":
        assert result["comparisons"][0]["mismatch_axes"]


def test_mixed_source_passages_require_review_not_a_definitive_contradiction():
    result = claim_assessment("Drugalpha reduced body weight by 5 kg [1].", [{
        "quote": "Drugalpha reduced body weight by 5 kg.",
        "excerpt": "Drugalpha increased body weight by 5 kg.",
    }])
    assert result["status"] == "NEEDS_SEMANTIC_REVIEW"
    assert any(row["mismatch_axes"] for row in result["comparisons"])


@pytest.mark.parametrize("quote,section,valid", [
    ("p > 0.05", "p < 0.05", False),
    ("48", "148", False),
    ("48%", "0.48%", False),
    ("48%", "-48%", False),
    ("48%", "+48%", False),
    ("48", "48.5%", False),
    ("0.48%", "Rate: 0.48%.", True),
    ("-48%", "Change: -48%.", True),
    (".48%", "Change: -.48%.", False),
    ("-.48%", "Change: -.48%.", True),
    ("48%", "Mortality was 48%.", True),
    ("p < 0.05", "Result: p < 0.05.", True),
])
def test_quoted_findings_preserve_operators_and_numeric_boundaries(quote, section, valid):
    payload = material_review()
    payload["material_findings"][0]["quote"] = quote
    error = review_materiality_failure(payload, {**CONTEXT, "sections": {"Results": section}})
    assert error == (None if valid else "material_issue_quote_not_in_section")


@pytest.mark.parametrize("field,value", [("impact", "!!!"), ("correction", "..."), ("why_new", "???")])
def test_punctuation_is_not_a_material_explanation(field, value):
    payload = material_review()
    payload["material_findings"][0].update(change_reason="newly_discovered", why_new="Missed before.")
    payload["material_findings"][0][field] = value
    payload["resolved_prior_issues"] = ["Old issue"]
    assert review_materiality_failure(payload, {**CONTEXT, "previous_issues": ["Old issue"]}) is not None


@pytest.mark.parametrize("claim,passage", [
    ("Drugalpha reduced body weight by <5 kg [1].", "Drugalpha reduced body weight by <12 kg."),
    ("Drugalpha improved lung function by 5% [1].", "Drugalpha increased lung function by 5%."),
    ("Drugalpha reduced body weight by 5 kg [1].", "In children at 24 months. Drugalpha reduced body weight by 12 kg."),
])
def test_ambiguous_comparisons_never_claim_a_proven_contradiction(claim, passage):
    result = claim_assessment(claim, [{"doi": "10.1234/test", "evidence_span": passage}])
    assert result["status"] == "NEEDS_SEMANTIC_REVIEW"
