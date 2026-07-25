from __future__ import annotations

import json
import stat
from datetime import datetime, timezone
from pathlib import Path

import pytest

from contracts import ArticleType, GoldSetAdjudication, GoldSetCorpus
from scripts.prepare_blinded_gold_set import (
    PROTOCOL_VERSION,
    _sha256,
    candidate_from_row,
    freeze_candidates,
    merge_adjudications,
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


def _freeze(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    private_dir = tmp_path / "private"
    receipt = tmp_path / "receipt.json"
    freeze_candidates(
        [_candidate(index) for index in range(120)],
        out_dir=private_dir,
        receipt_path=receipt,
        size=120,
        seed="test-freeze-v1",
    )
    return private_dir / "private_manifest.json", receipt, private_dir / "adjudicator-a.json", private_dir / "adjudicator-b.json"


def _label(packet_path: Path, adjudicator_id: str, *, conflict_case: str | None = None) -> None:
    packet = json.loads(packet_path.read_text())
    packet["adjudicator"] = {
        "id": adjudicator_id,
        "qualified": True,
        "qualification_statement": "Experienced independent research-methods reviewer.",
        "conflict_disclosure": "No material conflict with the submissions or producing agents.",
        "completed_at": "2026-07-25T12:00:00Z",
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


def _signoff(manifest_path: Path, path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "sampling_manifest_sha256": _sha256(json.loads(manifest_path.read_text())),
                "approved": True,
                "signed_by": "independent-calibration-chair",
                "signed_at": "2026-07-25T13:00:00Z",
                "statement": "The blinded labels and conflict records were reviewed before evaluation.",
                "reviewer_outputs_hidden_until_freeze": True,
            }
        )
    )


def test_candidate_blinds_submitter_identity_and_prior_verdict() -> None:
    metadata = {
        "abstract": "Dominic Lynch reports a bounded result under ORCID 0009-0005-4286-8363. " * 4,
        "body_markdown": "Institution: Example University\nSubmitted by: Dominic Lynch\n" + ("Methods and results. " * 20),
        "sections": {"Methods": "Dominic Lynch prepared the bounded protocol."},
        "source_bundle": [{"title": f"Real source {index}", "doi": f"10.1234/source.{index}"} for index in range(3)],
        "author_agent_id": "agent-v3",
        "institution_name": "Example University",
        "article_type": "research_synthesis",
        "domain_slug": "longevity",
    }
    candidate = candidate_from_row(
        {
            "id": "submission-real",
            "title": "Real manuscript",
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


def test_freeze_creates_private_diverse_packets_without_outcome_leakage(tmp_path: Path) -> None:
    manifest_path, receipt_path, packet_a, packet_b = _freeze(tmp_path)
    receipt = json.loads(receipt_path.read_text())
    first = json.loads(packet_a.read_text())
    second = json.loads(packet_b.read_text())

    assert receipt["case_count"] == 120
    assert receipt["historical_decision_strata"] == {"accept": 40, "reject": 40, "revise": 40}
    assert set(receipt["article_type_counts"]) == {item.value for item in ArticleType}
    assert len(receipt["domain_counts"]) == 10
    assert receipt["private_material_committed"] is False
    assert [case["case_id"] for case in first["cases"]] != [case["case_id"] for case in second["cases"]]
    assert "historical_decision" not in packet_a.read_text()
    assert "source_submission_id" not in receipt_path.read_text()
    assert stat.S_IMODE(manifest_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(packet_a.stat().st_mode) == 0o600


def test_merge_rejects_same_adjudicator_and_unresolved_conflict(tmp_path: Path) -> None:
    manifest, receipt, packet_a, packet_b = _freeze(tmp_path)
    signoff = tmp_path / "signoff.json"
    _signoff(manifest, signoff)
    _label(packet_a, "reviewer-one")
    _label(packet_b, " REVIEWER-ONE ")

    with pytest.raises(ValueError, match="two_distinct_adjudicators_required"):
        merge_adjudications(
            manifest_path=manifest,
            receipt_path=receipt,
            label_paths=(packet_a, packet_b),
            resolution_path=None,
            signoff_path=signoff,
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
            signoff_path=signoff,
            output_path=tmp_path / "gold.json",
        )


def test_merge_emits_adjudicated_corpus_only_after_complete_independent_labels(tmp_path: Path) -> None:
    manifest, receipt, packet_a, packet_b = _freeze(tmp_path)
    signoff = tmp_path / "signoff.json"
    output = tmp_path / "gold.json"
    _signoff(manifest, signoff)
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
        signoff_path=signoff,
        output_path=output,
    )

    assert len(corpus.entries) == 120
    assert corpus.adjudication.corpus_status == "adjudicated"
    assert corpus.adjudication.adjudicator_ids == ["reviewer-one", "reviewer-two"]
    assert corpus.adjudication.conflict_count == 0
    assert corpus.adjudication.inter_adjudicator_decision_agreement == 1.0
    assert corpus.adjudication.inter_adjudicator_kappa == 1.0
    assert corpus.adjudication.labels_revealed_at == corpus.adjudication.labels_frozen_at
    assert output.exists()


def test_merge_accepts_qualified_third_adjudicator_resolution(tmp_path: Path) -> None:
    manifest, receipt, packet_a, packet_b = _freeze(tmp_path)
    signoff = tmp_path / "signoff.json"
    resolution = tmp_path / "resolutions.json"
    _signoff(manifest, signoff)
    _label(packet_a, "reviewer-one")
    conflict_case = json.loads(packet_a.read_text())["cases"][0]["case_id"]
    _label(packet_b, "reviewer-two", conflict_case=conflict_case)
    resolution.write_text(
        json.dumps(
            {
                "sampling_manifest_sha256": _sha256(json.loads(manifest.read_text())),
                "method": "third_adjudicator",
                "resolver": {
                    "id": "reviewer-three",
                    "qualified": True,
                    "qualification_statement": "Independent domain and methods reviewer.",
                    "conflict_disclosure": "No material conflict.",
                    "completed_at": "2026-07-25T12:30:00Z",
                },
                "resolutions": [
                    {
                        "case_id": conflict_case,
                        "adjudication": json.loads(packet_a.read_text())["cases"][0]["adjudication"],
                    }
                ],
            }
        )
    )

    corpus = merge_adjudications(
        manifest_path=manifest,
        receipt_path=receipt,
        label_paths=(packet_a, packet_b),
        resolution_path=resolution,
        signoff_path=signoff,
        output_path=tmp_path / "gold.json",
    )

    assert corpus.adjudication.conflict_count == 1
    assert corpus.adjudication.resolution_file_sha256 == _file_hash(resolution)


def _file_hash(path: Path) -> str:
    import hashlib

    return f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"


def test_contract_cannot_self_certify_small_or_unproven_corpus() -> None:
    with pytest.raises(ValueError, match="missing_independent_label_receipts"):
        GoldSetAdjudication(
            corpus_status="adjudicated",
            protocol_version=PROTOCOL_VERSION,
            sampling_manifest_sha256=f"sha256:{'a' * 64}",
        )
    with pytest.raises(ValueError, match="at_least_100"):
        GoldSetCorpus(
            adjudication=GoldSetAdjudication(
                corpus_status="adjudicated",
                protocol_version=PROTOCOL_VERSION,
                sampling_manifest_sha256=f"sha256:{'a' * 64}",
                blinded_packet_sha256=[f"sha256:{'b' * 64}", f"sha256:{'c' * 64}"],
                adjudicator_ids=["one", "two"],
                label_file_sha256=[f"sha256:{'d' * 64}", f"sha256:{'e' * 64}"],
                inter_adjudicator_decision_agreement=1.0,
                inter_adjudicator_kappa=1.0,
                labels_frozen_at=datetime.now(timezone.utc),
                labels_revealed_at=datetime.now(timezone.utc),
                human_signoff={
                    "approved": True,
                    "signed_by": "chair",
                    "signed_at": "2026-07-25T13:00:00Z",
                    "statement": "Independent labels were frozen before evaluation.",
                    "reviewer_outputs_hidden_until_freeze": True,
                },
            )
        )
