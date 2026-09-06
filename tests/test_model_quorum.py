from __future__ import annotations

import hashlib
import hmac
import json
import socket
from copy import deepcopy

import pytest

from contracts import ObjectType, ProviderUsage, ResearchObject, RuntimeJob, Stage
from runtime_core.osf import build_publication_package
from runtime_core.providers import ProviderRequest, ProviderResponse, ProviderResult
from runtime_core.repos import InMemoryRuntimeRepository
from runtime_core.review_contract import (
    MODEL_QUORUM_POLICY,
    MODEL_QUORUM_PROVIDERS,
    accept_quorum_satisfied,
    billing_waiver_attestation,
    billing_waiver_attestation_valid,
    model_quorum_attestation,
    model_quorum_attestation_valid,
    model_quorum_metadata,
)
from runtime_core.workflow import WorkflowEngine, _canonical_submission_hash, _require_accept_quorum
from tests.support import accepted_publish_job
from tests.test_runtime_core import _calibration_submission, _review_payload


SOL, TERRA, GLM = MODEL_QUORUM_PROVIDERS
SECRET = "synthetic-model-quorum-test-key"
PACKAGE_HASH = "sha256:" + "a" * 64


@pytest.fixture(autouse=True)
def offline_attestation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RESEARKA_V2_REVIEW_ATTESTATION_SECRET", SECRET)
    monkeypatch.delenv("RESEARKA_V2_REVIEW_ATTESTATION_SECRET_PATH", raising=False)

    def forbidden(*args, **kwargs):
        raise AssertionError("network forbidden in model quorum tests")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


def _receipt(model: str, recommendation: str = "accept") -> dict:
    response = _review_payload(recommendation)
    return {
        "ok": True,
        "provider": MODEL_QUORUM_PROVIDERS[model],
        "model": model,
        "recommendation": recommendation,
        "response_sha256": hashlib.sha256(json.dumps(response).encode()).hexdigest(),
        "response": response,
        "usage": {"input_tokens": 10, "output_tokens": 10, "cost_usd": 0},
        "reasoning_effort": "high" if model == SOL else "medium",
        "fallback_used": model == GLM,
        "fallback_reason": "timeout:secondary unavailable" if model == GLM else "",
    }


def _signed(models: tuple[str, ...] = (SOL, TERRA)) -> tuple[dict, dict]:
    metadata = {
        **model_quorum_metadata([_receipt(model) for model in models]),
        "provider": "reviewer-panel",
        "recommendation": "accept",
        "reviewed_package_hash": PACKAGE_HASH,
        "judge_release_id": "test-release",
        "judge_release": {"settings": {"quorum_policy": MODEL_QUORUM_POLICY}},
    }
    context = {
        "submission_id": "test-submission",
        "reviewed_package_hash": PACKAGE_HASH,
        "recommendation": "accept",
        "judge_release_id": "test-release",
        "secret": SECRET,
    }
    metadata["model_quorum_attestation"] = model_quorum_attestation(metadata, **context)
    return metadata, context


@pytest.mark.parametrize("models", [(SOL, TERRA), (SOL, GLM), (TERRA, GLM)])
def test_model_diverse_quorum_requires_valid_content_bound_signature(models) -> None:
    metadata, context = _signed(models)
    assert model_quorum_attestation_valid(metadata, **context)
    assert accept_quorum_satisfied(metadata, submission_id=context["submission_id"], reviewed_package_hash=PACKAGE_HASH, secret=SECRET)
    assert not accept_quorum_satisfied(metadata)
    assert metadata["accept_quorum_count"] == 2
    assert metadata["accept_quorum_providers"] == sorted({MODEL_QUORUM_PROVIDERS[model] for model in models})


@pytest.mark.parametrize("field,value", [
    ("accept_quorum_count", 1), ("accept_quorum_count", 3),
    ("accept_quorum_count", True), ("accept_quorum_count", 2.0),
    ("accept_quorum_count", "2"), ("accept_quorum_models", [SOL, SOL]),
    ("accept_quorum_providers", ["codex", "openrouter"]),
    ("accept_quorum_identities", ["codex:fake", "codex:other"]),
    ("model_quorum_attestation", "hmac-sha256:forged"),
    ("quorum_policy", "provider_diversity_v1"), ("quorum_policy", "unknown"),
    ("provider", "codex"), ("reviewed_package_hash", "sha256:" + "b" * 64),
    ("judge_release_id", "different-release"),
    ("recommendation", "reject"), ("accept_quorum_waiver", "sparring_billing_unavailable"),
])
def test_signed_quorum_rejects_tampered_metadata(field, value) -> None:
    metadata, context = _signed()
    metadata[field] = value
    assert not model_quorum_attestation_valid(metadata, **context)


@pytest.mark.parametrize("field,value", [
    ("submission_id", "other-submission"),
    ("reviewed_package_hash", "sha256:" + "b" * 64),
    ("judge_release_id", "other-release"), ("recommendation", "reject"),
    ("secret", "other-key"), ("secret", None),
])
def test_signed_quorum_rejects_replay(field, value) -> None:
    metadata, context = _signed()
    assert not model_quorum_attestation_valid(metadata, **{**context, field: value})


@pytest.mark.parametrize("field,value", [
    ("model", "unknown"), ("provider", "other"), ("ok", False),
    ("recommendation", "reject"), ("response_sha256", "0" * 64),
    ("reasoning_effort", "low"), ("fallback_reason", "different cause"),
    ("usage", {"cost_usd": 999}), ("response", _review_payload("reject")),
])
def test_actual_receipt_tampering_invalidates_signature(field, value) -> None:
    metadata, context = _signed()
    metadata["reviewer_receipts"][0][field] = value
    assert not model_quorum_attestation_valid(metadata, **context)


def test_missing_new_policy_cannot_downgrade_cross_provider_pair() -> None:
    metadata, context = _signed((SOL, GLM))
    metadata.pop("quorum_policy")
    metadata.pop("model_quorum_attestation")
    assert not accept_quorum_satisfied(metadata, submission_id=context["submission_id"], reviewed_package_hash=PACKAGE_HASH, secret=SECRET, allow_billing_waiver=True)


def test_one_model_and_different_effort_are_not_two_votes() -> None:
    with pytest.raises(ValueError, match="accept_quorum_missing"):
        _signed((SOL,))
    duplicates = [_receipt(SOL), {**_receipt(SOL), "reasoning_effort": "medium"}]
    with pytest.raises(ValueError, match="duplicate_model_quorum_identity"):
        model_quorum_metadata(duplicates)


@pytest.mark.parametrize("change", [
    {"model": "unapproved", "provider": "codex"},
    {"model": "unapproved", "provider": None},
    {"model": TERRA, "provider": "openrouter"},
    {"response": _review_payload("accept", rubric_scores={"source_grounding": 1})},
])
def test_server_cannot_sign_unknown_or_invalid_votes(change) -> None:
    with pytest.raises(ValueError):
        model_quorum_metadata([_receipt(SOL), {**_receipt(TERRA), **change}])


def test_glm_requires_failure_cause_and_agreement() -> None:
    with pytest.raises(ValueError, match="fallback_cause_missing"):
        model_quorum_metadata([_receipt(SOL), {**_receipt(GLM), "fallback_reason": ""}])
    metadata = model_quorum_metadata([_receipt(SOL), _receipt(TERRA, "reject")])
    assert metadata["accept_quorum_count"] == 1


class RecordedPanel:
    provider = "reviewer-panel"
    model = f"{SOL}|{TERRA}"
    quorum_policy = MODEL_QUORUM_POLICY
    enforces_accept_quorum = True

    def __init__(self, metadata: dict | None = None) -> None:
        self.metadata: dict = metadata if metadata is not None else {
            "quorum_policy": MODEL_QUORUM_POLICY,
            "reviewer_receipts": [_receipt(SOL), _receipt(TERRA)],
            "accept_quorum_count": 99,
            "accept_quorum_models": ["forged"],
            "reviewed_package_hash": "caller-supplied",
            "model_quorum_attestation": "caller-supplied",
        }

    def complete(self, request: ProviderRequest) -> ProviderResult:
        return ProviderResult(ok=True, response=ProviderResponse(
            provider=self.provider, model=self.model, text=json.dumps(_review_payload()),
            usage=ProviderUsage(), metadata=deepcopy(self.metadata),
        ))


def _reviewed() -> tuple[InMemoryRuntimeRepository, ResearchObject, dict]:
    repo = InMemoryRuntimeRepository()
    submission = _calibration_submission(repo, recommendation="accept")
    recommendation, _, metadata = WorkflowEngine(provider=RecordedPanel())._review_submission(submission)
    assert recommendation == "accept"
    return repo, submission, metadata


def test_workflow_recomputes_claimed_counts_hash_and_signature() -> None:
    _, submission, metadata = _reviewed()
    assert metadata["accept_quorum_count"] == 2
    assert metadata["accept_quorum_models"] == [SOL, TERRA]
    assert metadata["reviewed_package_hash"] == _canonical_submission_hash(submission)
    assert accept_quorum_satisfied(metadata, submission_id=submission.id, reviewed_package_hash=_canonical_submission_hash(submission), secret=SECRET)


@pytest.mark.parametrize("policy", [None, "provider_diversity_v1", "unknown"])
def test_active_model_reviewer_rejects_missing_or_downgraded_policy(policy) -> None:
    repo = InMemoryRuntimeRepository()
    submission = _calibration_submission(repo, recommendation="accept")
    metadata = RecordedPanel().metadata
    metadata["quorum_policy"] = policy
    with pytest.raises(ValueError, match="model_quorum_policy_missing"):
        WorkflowEngine(provider=RecordedPanel(metadata))._review_submission(submission)


def test_workflow_revalidates_each_receipt_before_signing() -> None:
    repo = InMemoryRuntimeRepository()
    submission = _calibration_submission(repo, recommendation="accept")
    metadata = RecordedPanel().metadata
    metadata["reviewer_receipts"][1]["response"] = _review_payload(
        "accept", review_markdown="The submission is missing manuscript content."
    )
    with pytest.raises(ValueError, match="false_missing_manuscript"):
        WorkflowEngine(provider=RecordedPanel(metadata))._review_submission(submission)


def test_model_receipts_preserve_case_normalization_without_aliases() -> None:
    receipts = [_receipt(SOL), _receipt(TERRA)]
    for receipt in receipts:
        receipt["recommendation"] = " ACCEPT "
        receipt["response"]["recommendation"] = "Accept"
        receipt["response"]["claim_support_verdict"] = " Supported "
        receipt["response"]["overclaim_verdict"] = "None"
        receipt["response"]["synthesis_quality_verdict"] = "Strong"
    assert model_quorum_metadata(receipts)["accept_quorum_count"] == 2
    receipts[0]["recommendation"] = "approved"
    receipts[0]["response"]["recommendation"] = "approved"
    with pytest.raises(ValueError, match="invalid_model_quorum_response"):
        model_quorum_metadata(receipts)


def _publication_fixture():
    repo, submission, metadata = _reviewed()
    job = accepted_publish_job(repo, submission, metadata)
    decision = repo.get_object(job.payload["decision_id"])
    review = repo.get_object(decision.metadata["review_id"])
    publication = repo.create_object(ResearchObject(
        object_type=ObjectType.PUBLICATION, parent_object_id=submission.id,
        title=submission.title, body_markdown="Accepted manuscript.",
        metadata={
            "decision_id": decision.id, "review_id": review.id,
            "canonical_package_hash": metadata["reviewed_package_hash"],
            "judge_release_id": metadata["judge_release_id"],
        },
    ))
    return repo, submission, review, publication, job


def test_valid_attested_review_reaches_osf_package() -> None:
    repo, _, review, publication, _ = _publication_fixture()
    files = build_publication_package(repo, publication)
    assert json.loads(files["review_receipts.json"])["metadata"]["model_quorum_attestation"] == review.metadata["model_quorum_attestation"]


@pytest.mark.parametrize("boundary", ["editorial", "publish", "osf"])
def test_all_acceptance_boundaries_reject_forged_signature(boundary) -> None:
    repo, submission, review, publication, job = _publication_fixture()
    repo.update_object_metadata(review.id, {**review.metadata, "model_quorum_attestation": "forged"})
    with pytest.raises((ValueError, RuntimeError), match="accept_quorum_missing|osf_scientific_package_lineage_invalid"):
        if boundary == "osf":
            build_publication_package(repo, publication)
        elif boundary == "publish":
            WorkflowEngine(provider=RecordedPanel())._run_publish(job, repo)
        else:
            WorkflowEngine(provider=RecordedPanel())._run_editorial(RuntimeJob(
                target_object_id=submission.id, stage=Stage.EDITORIAL,
                payload={"review_id": review.id},
            ), repo)


@pytest.mark.parametrize("field", ["abstract", "source_bundle"])
def test_submission_or_evidence_change_invalidates_bound_review(field) -> None:
    _, submission, metadata = _reviewed()
    submission.metadata[field] = "changed" if field == "abstract" else [{"excerpt": "changed evidence"}]
    review = ResearchObject(object_type=ObjectType.REVIEW, parent_object_id=submission.id, title="Review", metadata=metadata)
    with pytest.raises(ValueError, match="accept_quorum_missing"):
        _require_accept_quorum(review, submission.id, _canonical_submission_hash(submission))


@pytest.mark.parametrize("policy", [None, "provider_diversity_v1"])
def test_legacy_quorum_and_billing_signature_parity(policy) -> None:
    metadata = _review_payload()
    if policy:
        metadata["quorum_policy"] = policy
    assert accept_quorum_satisfied(metadata)
    waiver = {
        "provider": "reviewer-panel", "route": "sparring_billing_skipped_primary_used",
        "ops_flag": "sparring_billing_skipped", "secondary_review_skipped": True,
        "accept_quorum_count": 1, "accept_quorum_models": ["MiniMax-M3"],
        "accept_quorum_identities": ["minimax:MiniMax-M3"], "accept_quorum_providers": ["minimax"],
        "accept_quorum_waiver": "sparring_billing_unavailable", "sparring_provider": "openrouter",
        "sparring_http_status": 402, "primary_fallback_used": False,
        "winner_provider": "minimax", "winner_model": "MiniMax-M3",
    }
    context = {"submission_id": "legacy", "recommendation": "accept", "judge_release_id": "legacy-release", "secret": SECRET}
    signed_fields = {key: value for key, value in waiver.items() if key != "provider"}
    original_payload = {**{key: value for key, value in context.items() if key != "secret"}, "receipt": signed_fields}
    expected = "hmac-sha256:" + hmac.new(SECRET.encode(), json.dumps(original_payload, sort_keys=True, separators=(",", ":")).encode(), hashlib.sha256).hexdigest()
    if policy:
        waiver["quorum_policy"] = policy
    waiver["accept_quorum_waiver_attestation"] = billing_waiver_attestation(waiver, **context)
    assert waiver["accept_quorum_waiver_attestation"] == expected
    assert billing_waiver_attestation_valid(waiver, **context)
    assert accept_quorum_satisfied(waiver, allow_billing_waiver=True)
