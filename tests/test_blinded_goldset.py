from __future__ import annotations

import hashlib
import json
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from contracts import ArticleType, GoldSetAdjudication, GoldSetCorpus
from runtime_core.goldset import summarize_gold_results
from runtime_core.judge_release import judge_release_id
from scripts.prepare_blinded_gold_set import (
    PROTOCOL_VERSION,
    _sha256,
    candidate_from_row,
    freeze_candidates,
    merge_adjudications,
    select_candidates,
    sign_evaluation,
)


def _candidate(index: int) -> dict:
    decisions = ("accept", "revise", "reject")
    article_types = [item.value for item in ArticleType]
    decision = decisions[index % len(decisions)]
    article_type = article_types[index % len(article_types)]
    domain = f"domain-{index % 10}"
    submission = {
        "title": f"Blinded real submission {index}",
        "abstract": "A bounded research abstract with enough detail for independent review. " * 4,
        "body_markdown": "A complete manuscript body with methods, results, limitations, and conclusion. " * 8,
        "sections": {"Methods": "Reproducible methods.", "Results": "Bounded results."},
        "source_bundle": [
            {"title": f"Source {source}", "doi": f"10.1234/real.{index}.{source}", "excerpt": "Evidence span."}
            for source in range(3)
        ],
        "author_agent_id": "blinded-adjudication",
        "article_type": article_type,
        "domain_slug": domain,
        "submitted_at": "2000-01-01T00:00:00Z",
    }
    return {
        "source_submission_id": f"submission-{index}",
        "source_content_sha256": _sha256(f"source-{index}"),
        "historical_decision": decision,
        "article_type": article_type,
        "domain_slug": domain,
        "created_at": "2026-01-01T00:00:00Z",
        "submission": submission,
        "blinded_content_sha256": _sha256(submission),
    }


def _judge_release() -> dict:
    release = {
        "code_sha": "test-code-sha",
        "policy_version": "judge-policy-v1",
        "reviewer_prompt_version": "reviewer-test-v1",
        "editor_prompt_version": "editor-test-v1",
        "provider": "reviewer-panel",
        "models": ["panel-primary", "panel-sparring"],
        "settings": {"accept_quorum_min": 2},
    }
    return {"id": judge_release_id(release), **release}


def _evaluation_artifact(release_id: str) -> dict:
    article_types = [item.value for item in ArticleType]
    decisions = ("accept", "revise", "reject")
    results = [
        {
            "entry_id": f"case-{index}",
            "article_type": article_types[index % len(article_types)],
            "domain_slug": f"domain-{index % 8}",
            "expected_decision": decisions[index % len(decisions)],
            "actual_decision": decisions[index % len(decisions)],
            "duration_s": 1.0,
            "cost_usd": 0.01,
        }
        for index in range(100)
    ]
    return {
        "run_meta": {
            "corpus_status": "adjudicated",
            "judge_release_consistent": True,
            "judge_release_target_matched": True,
            "target_judge_release_id": release_id,
            "judge_release_id": release_id,
            "judge_release": _judge_release(),
            "inter_adjudicator_decision_agreement": 0.9,
            "inter_adjudicator_kappa": 0.8,
            "labels_frozen_at": "2026-07-25T10:00:00Z",
            "labels_revealed_at": "2026-07-25T11:00:00Z",
            "timestamp": "2026-07-25T12:00:00Z",
        },
        "summary": summarize_gold_results(results),
        "results": results,
        "limitations": ["Performance is bounded to the frozen sample."],
    }


def _freeze(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    private_dir = tmp_path / "private"
    receipt = tmp_path / "receipt.json"
    freeze_candidates(
        [_candidate(index) for index in range(120)],
        out_dir=private_dir,
        receipt_path=receipt,
        size=120,
        seed="test-freeze-v1",
        judge_release=_judge_release(),
    )
    return private_dir / "private_manifest.json", receipt, private_dir / "adjudicator-a.json", private_dir / "adjudicator-b.json"


def _label(packet_path: Path, adjudicator_id: str, *, conflict_case: str | None = None) -> None:
    packet = json.loads(packet_path.read_text())
    packet["adjudicator"] = {
        "id": adjudicator_id,
        "type": "human",
        "qualified": True,
        "qualification_statement": "Experienced independent research-methods reviewer.",
        "conflict_disclosure": "No material conflict with the submissions or producing agents.",
        "reviewer_outputs_hidden_until_freeze": True,
        "judge_release_id": packet["judge_release"]["id"],
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }
    decisions = ("accept", "revise", "reject")
    for case in packet["cases"]:
        decision = decisions[int(case["case_id"][-1], 16) % len(decisions)]
        if case["case_id"] == conflict_case:
            decision = decisions[(decisions.index(decision) + 1) % len(decisions)]
        case["adjudication"] = {
            "decision": decision,
            "rubric_scores": {
                "research_question_quality": 4,
                "synthesis_quality": 4,
                "claim_evidence_alignment": 4,
                "limitations_quality": 4,
                "gaps_quality": 4,
                "source_grounding": 4,
            },
            "claim_support_verdict": "partially_supported",
            "overclaim_verdict": "mild",
            "synthesis_quality_verdict": "adequate",
            "rationale": "The manuscript is credible but requires bounded evidence-alignment revisions.",
        }
    packet_path.write_text(json.dumps(packet, indent=2))


def test_candidate_blinds_submitter_identity_and_prior_verdict() -> None:
    metadata = {
        "abstract": "Dominic Lynch reports a bounded result under ORCID 0009-0005-4286-8363. " * 4,
        "body_markdown": "Institution: Example University\nSubmitted by: Dominic Lynch\n" + ("Methods and results. " * 20),
        "sections": {"Methods": "Dominic Lynch prepared the bounded protocol."},
        "source_bundle": [
            {"title": f"Real source {index} by DOMINIC LYNCH", "doi": f"10.1234/source.{index}"}
            for index in range(3)
        ],
        "author_agent_id": "agent-v3",
        "author_name": "Dominic Lynch",
        "institution_name": "Example University",
        "article_type": "research_synthesis",
        "domain_slug": "longevity",
    }
    candidate = candidate_from_row(
        {
            "id": "submission-real",
            "title": "Real manuscript by dominic lynch",
            "body_markdown": metadata["body_markdown"],
            "metadata": json.dumps(metadata),
            "created_at": datetime.now(timezone.utc),
            "decision": "accept",
        }
    )

    assert candidate is not None
    packet_text = json.dumps(candidate["submission"])
    assert "Dominic Lynch" not in packet_text
    assert "Example University" not in packet_text
    assert "0009-0005-4286-8363" not in packet_text
    assert "accept" not in packet_text
    assert candidate["historical_decision"] == "accept"


@pytest.mark.parametrize(
    "metadata_update",
    [
        {"authenticated_agent_id": "external-agent", "author_agent_id": "benchmark-runner"},
        {"authenticated_agent_id": "external-agent", "is_synthetic": True},
        {"authenticated_agent_id": "external-agent", "generation": {"_benchmark_quality": "high"}},
    ],
)
def test_candidate_excludes_synthetic_rows_across_all_metadata(metadata_update: dict) -> None:
    candidate = _candidate(1)
    metadata = {**candidate["submission"], **metadata_update}

    assert candidate_from_row(
        {
            "id": "synthetic-row",
            "title": metadata["title"],
            "body_markdown": metadata["body_markdown"],
            "metadata": metadata,
            "decision": "accept",
        }
    ) is None


def test_freeze_creates_private_diverse_packets_without_outcome_leakage(tmp_path: Path) -> None:
    manifest_path, receipt_path, packet_a, packet_b = _freeze(tmp_path)
    receipt = json.loads(receipt_path.read_text())
    first = json.loads(packet_a.read_text())
    second = json.loads(packet_b.read_text())

    assert receipt["case_count"] == 120
    assert receipt["historical_decision_strata"] == {"accept": 40, "reject": 40, "revise": 40}
    assert receipt["supported_article_types"] == sorted(item.value for item in ArticleType)
    assert receipt["covered_article_types"] == sorted(item.value for item in ArticleType)
    assert set(receipt["article_type_counts"]) == {item.value for item in ArticleType}
    assert receipt["missing_article_types"] == []
    assert receipt["article_type_coverage_complete"] is True
    assert len(receipt["domain_counts"]) == 10
    assert receipt["private_material_committed"] is False
    assert [case["case_id"] for case in first["cases"]] != [case["case_id"] for case in second["cases"]]
    assert "historical_decision" not in packet_a.read_text()
    assert "source_submission_id" not in receipt_path.read_text()
    assert stat.S_IMODE(manifest_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(packet_a.stat().st_mode) == 0o600


def test_sampler_preserves_rare_article_types_across_many_domain_buckets() -> None:
    candidates = [_candidate(index) for index in range(600)]
    for candidate in candidates:
        candidate["article_type"] = "rapid_evidence_synthesis"
        candidate["submission"]["article_type"] = "rapid_evidence_synthesis"
        candidate["domain_slug"] = f"domain-{candidate['source_submission_id']}"
    for offset, article_type in enumerate(ArticleType):
        for decision_index, decision in enumerate(("accept", "revise", "reject")):
            candidate = candidates[offset * 3 + decision_index]
            candidate["historical_decision"] = decision
            candidate["article_type"] = article_type.value
            candidate["submission"]["article_type"] = article_type.value

    selected = select_candidates(candidates, size=120, seed="rare-type-regression")

    assert {item["article_type"] for item in selected} == {item.value for item in ArticleType}


def test_freeze_records_missing_real_article_type_without_using_synthetic_cases(tmp_path: Path) -> None:
    candidates = [
        candidate
        for index in range(150)
        if (candidate := _candidate(index))["article_type"] != "empirical_study"
    ]

    receipt = freeze_candidates(
        candidates,
        out_dir=tmp_path / "private",
        receipt_path=tmp_path / "receipt.json",
        size=120,
        seed="missing-real-type",
        judge_release=_judge_release(),
    )

    assert receipt["supported_article_types"] == sorted(item.value for item in ArticleType)
    assert receipt["covered_article_types"] == sorted(
        item.value for item in ArticleType if item is not ArticleType.EMPIRICAL_STUDY
    )
    assert receipt["missing_article_types"] == ["empirical_study"]
    assert receipt["article_type_coverage_complete"] is False
    assert receipt["corpus_status"] == "blinded_incomplete_coverage"


def test_merge_cannot_certify_missing_supported_article_type(tmp_path: Path) -> None:
    candidates = [
        candidate
        for index in range(150)
        if (candidate := _candidate(index))["article_type"] != "empirical_study"
    ]
    private = tmp_path / "private"
    receipt = tmp_path / "receipt.json"
    freeze_candidates(
        candidates,
        out_dir=private,
        receipt_path=receipt,
        size=120,
        seed="missing-real-type",
        judge_release=_judge_release(),
    )
    packet_a = private / "adjudicator-a.json"
    packet_b = private / "adjudicator-b.json"
    _label(packet_a, "reviewer-one")
    _label(packet_b, "reviewer-two")

    with pytest.raises(ValueError, match="must_span_supported_article_types"):
        merge_adjudications(
            manifest_path=private / "private_manifest.json",
            receipt_path=receipt,
            label_paths=(packet_a, packet_b),
            resolution_path=None,
            output_path=tmp_path / "gold.json",
        )


def test_merge_rejects_same_adjudicator_and_unresolved_conflict(tmp_path: Path) -> None:
    manifest, receipt, packet_a, packet_b = _freeze(tmp_path)
    _label(packet_a, "reviewer-one")
    _label(packet_b, " REVIEWER-ONE ")

    with pytest.raises(ValueError, match="two_distinct_adjudicators_required"):
        merge_adjudications(
            manifest_path=manifest,
            receipt_path=receipt,
            label_paths=(packet_a, packet_b),
            resolution_path=None,
            output_path=tmp_path / "gold.json",
        )

    conflict_case = json.loads(packet_a.read_text())["cases"][0]["case_id"]
    _label(packet_b, "reviewer-two", conflict_case=conflict_case)
    with pytest.raises(ValueError, match="conflicts_require_resolution_record"):
        merge_adjudications(
            manifest_path=manifest,
            receipt_path=receipt,
            label_paths=(packet_a, packet_b),
            resolution_path=None,
            output_path=tmp_path / "gold.json",
        )


def test_merge_requires_adjudicator_independence_from_judge_release(tmp_path: Path) -> None:
    manifest, receipt, packet_a, packet_b = _freeze(tmp_path)
    _label(packet_a, "reviewer-one")
    _label(packet_b, "reviewer-two")
    first = json.loads(packet_a.read_text())
    first["adjudicator"].pop("reviewer_outputs_hidden_until_freeze")
    packet_a.write_text(json.dumps(first, indent=2))
    with pytest.raises(ValueError, match="independent_adjudicator_provenance_required"):
        merge_adjudications(
            manifest_path=manifest,
            receipt_path=receipt,
            label_paths=(packet_a, packet_b),
            resolution_path=None,
            output_path=tmp_path / "gold.json",
        )

    first["adjudicator"]["reviewer_outputs_hidden_until_freeze"] = True
    first["adjudicator"].update({"type": "independent_model", "model": "provider/panel-primary"})
    packet_a.write_text(json.dumps(first, indent=2))

    with pytest.raises(ValueError, match="independent_adjudicator_provenance_required"):
        merge_adjudications(
            manifest_path=manifest,
            receipt_path=receipt,
            label_paths=(packet_a, packet_b),
            resolution_path=None,
            output_path=tmp_path / "gold.json",
        )


def test_merge_rejects_tampered_release_and_predated_adjudication(tmp_path: Path) -> None:
    manifest, receipt, packet_a, packet_b = _freeze(tmp_path)
    _label(packet_a, "reviewer-one")
    _label(packet_b, "reviewer-two")
    first = json.loads(packet_a.read_text())
    first["judge_release"]["id"] = f"sha256:{'0' * 64}"
    packet_a.write_text(json.dumps(first, indent=2))

    with pytest.raises(ValueError, match="judge_release_mismatch"):
        merge_adjudications(
            manifest_path=manifest,
            receipt_path=receipt,
            label_paths=(packet_a, packet_b),
            resolution_path=None,
            output_path=tmp_path / "gold.json",
        )

    first["judge_release"] = _judge_release()
    first["adjudicator"]["completed_at"] = "2000-01-01T00:00:00Z"
    packet_a.write_text(json.dumps(first, indent=2))
    with pytest.raises(ValueError, match="adjudication_predates_freeze"):
        merge_adjudications(
            manifest_path=manifest,
            receipt_path=receipt,
            label_paths=(packet_a, packet_b),
            resolution_path=None,
            output_path=tmp_path / "gold.json",
        )


def test_merge_emits_adjudicated_corpus_only_after_complete_independent_labels(tmp_path: Path) -> None:
    manifest, receipt, packet_a, packet_b = _freeze(tmp_path)
    output = tmp_path / "gold.json"
    _label(packet_a, "reviewer-one")
    _label(packet_b, "reviewer-two")
    second = json.loads(packet_b.read_text())
    second["cases"][0]["adjudication"]["rationale"] = (
        "Independent wording reaches the same substantive labels without manufacturing a conflict."
    )
    packet_b.write_text(json.dumps(second, indent=2))

    corpus = merge_adjudications(
        manifest_path=manifest,
        receipt_path=receipt,
        label_paths=(packet_a, packet_b),
        resolution_path=None,
        output_path=output,
    )

    assert len(corpus.entries) == 120
    assert corpus.adjudication.corpus_status == "adjudicated"
    assert corpus.adjudication.adjudicator_ids == ["reviewer-one", "reviewer-two"]
    assert corpus.adjudication.conflict_count == 0
    assert corpus.adjudication.inter_adjudicator_decision_agreement == 1.0
    assert corpus.adjudication.inter_adjudicator_kappa == 1.0
    assert corpus.adjudication.target_judge_release_id == _judge_release()["id"]
    assert corpus.adjudication.labels_frozen_at is not None
    assert corpus.adjudication.labels_revealed_at is not None
    assert corpus.adjudication.labels_revealed_at > corpus.adjudication.labels_frozen_at
    assert output.exists()


def test_merge_accepts_qualified_third_adjudicator_resolution(tmp_path: Path) -> None:
    manifest, receipt, packet_a, packet_b = _freeze(tmp_path)
    resolution = tmp_path / "resolutions.json"
    _label(packet_a, "reviewer-one")
    conflict_case = json.loads(packet_a.read_text())["cases"][0]["case_id"]
    _label(packet_b, "reviewer-two", conflict_case=conflict_case)
    resolution_record: dict[str, Any] = {
        "sampling_manifest_sha256": _sha256(json.loads(manifest.read_text())),
        "method": "third_adjudicator",
        "labels_revealed_at": datetime.now(timezone.utc).isoformat(),
        "resolver": {
            "id": " reviewer-one ",
            "type": "human",
            "qualified": True,
            "qualification_statement": "Independent domain and methods reviewer.",
            "conflict_disclosure": "No material conflict.",
            "reviewer_outputs_hidden_until_freeze": True,
            "judge_release_id": _judge_release()["id"],
            "completed_at": datetime.now(timezone.utc).isoformat(),
        },
        "resolutions": [
            {
                "case_id": conflict_case,
                "adjudication": json.loads(packet_a.read_text())["cases"][0]["adjudication"],
            }
        ],
    }
    resolution.write_text(json.dumps(resolution_record))
    with pytest.raises(ValueError, match="qualified_independent_third_adjudicator_required"):
        merge_adjudications(
            manifest_path=manifest,
            receipt_path=receipt,
            label_paths=(packet_a, packet_b),
            resolution_path=resolution,
            output_path=tmp_path / "gold.json",
        )

    resolution_record["resolver"]["id"] = "reviewer-three"
    resolution_record["resolver"]["completed_at"] = datetime.now(timezone.utc).isoformat()
    resolution.write_text(json.dumps(resolution_record))

    corpus = merge_adjudications(
        manifest_path=manifest,
        receipt_path=receipt,
        label_paths=(packet_a, packet_b),
        resolution_path=resolution,
        output_path=tmp_path / "gold.json",
    )

    assert corpus.adjudication.conflict_count == 1
    assert corpus.adjudication.resolution_file_sha256 == _file_hash(resolution)


def _file_hash(path: Path) -> str:
    import hashlib

    return f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"


def test_sign_evaluation_binds_human_review_to_release_and_artifact(tmp_path: Path) -> None:
    artifact_path = tmp_path / "evaluation.json"
    release_path = tmp_path / "active-release.json"
    release_id = _judge_release()["id"]
    artifact = _evaluation_artifact(release_id)
    artifact["run_meta"]["judge_release_target_matched"] = False
    artifact_path.write_text(json.dumps(artifact))
    with pytest.raises(ValueError, match="evaluation_not_ready_for_human_signoff"):
        sign_evaluation(
            artifact_path,
            active_release_path=release_path,
            signed_by="calibration-chair",
            statement="A different judge release must not be signed.",
        )
    artifact["run_meta"]["judge_release_target_matched"] = True
    artifact_path.write_text(json.dumps(artifact))

    signoff = sign_evaluation(
        artifact_path,
        active_release_path=release_path,
        signed_by="calibration-chair",
        statement="I reviewed the results and limitations.",
    )
    signed_artifact = json.loads(artifact_path.read_text())
    active_release = json.loads(release_path.read_text())

    assert signoff["results_reviewed"] is True
    assert signoff["limitations_reviewed"] is True
    assert signed_artifact["run_meta"]["human_signoff"] == signoff
    assert active_release["id"] == release_id
    assert active_release["calibration"]["sha256"] == hashlib.sha256(artifact_path.read_bytes()).hexdigest()
    assert stat.S_IMODE(artifact_path.stat().st_mode) == 0o600
    with pytest.raises(ValueError, match="evaluation_not_ready_for_human_signoff"):
        sign_evaluation(
            artifact_path,
            active_release_path=release_path,
            signed_by="calibration-chair",
            statement="Duplicate signoff must not replace the first.",
        )


def test_contract_cannot_self_certify_small_or_unproven_corpus() -> None:
    with pytest.raises(ValueError, match="missing_independent_label_receipts"):
        GoldSetAdjudication(
            corpus_status="adjudicated",
            protocol_version=PROTOCOL_VERSION,
            sampling_manifest_sha256=f"sha256:{'a' * 64}",
            target_judge_release_id=f"sha256:{'f' * 64}",
        )
    with pytest.raises(ValueError, match="at_least_100"):
        GoldSetCorpus(
            adjudication=GoldSetAdjudication(
                corpus_status="adjudicated",
                protocol_version=PROTOCOL_VERSION,
                sampling_manifest_sha256=f"sha256:{'a' * 64}",
                target_judge_release_id=f"sha256:{'f' * 64}",
                blinded_packet_sha256=[f"sha256:{'b' * 64}", f"sha256:{'c' * 64}"],
                adjudicator_ids=["one", "two"],
                label_file_sha256=[f"sha256:{'d' * 64}", f"sha256:{'e' * 64}"],
                inter_adjudicator_decision_agreement=1.0,
                inter_adjudicator_kappa=1.0,
                labels_frozen_at=datetime.now(timezone.utc),
                labels_revealed_at=datetime.now(timezone.utc),
            )
        )
