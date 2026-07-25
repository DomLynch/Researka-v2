from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from .models import ArticleType, Decision, SubmissionPayload

GOLD_SET_RUBRIC_KEYS = {
    "research_question_quality",
    "synthesis_quality",
    "claim_evidence_alignment",
    "limitations_quality",
    "gaps_quality",
    "source_grounding",
}


def _is_sha256(value: str | None) -> bool:
    if not value or not value.startswith("sha256:") or len(value) != 71:
        return False
    return all(character in "0123456789abcdef" for character in value[7:].lower())


class GoldSetAdjudication(BaseModel):
    corpus_status: Literal["working", "adjudicated"] = "working"
    protocol_version: str | None = None
    sampling_manifest_sha256: str | None = None
    target_judge_release_id: str | None = None
    blinded_packet_sha256: list[str] = Field(default_factory=list)
    adjudicator_ids: list[str] = Field(default_factory=list)
    label_file_sha256: list[str] = Field(default_factory=list)
    conflict_count: int = Field(default=0, ge=0)
    resolution_file_sha256: str | None = None
    inter_adjudicator_decision_agreement: float | None = Field(default=None, ge=0, le=1)
    inter_adjudicator_kappa: float | None = Field(default=None, ge=-1, le=1)
    labels_frozen_at: datetime | None = None
    labels_revealed_at: datetime | None = None

    @model_validator(mode="after")
    def require_adjudication_receipts(self) -> GoldSetAdjudication:
        if self.corpus_status != "adjudicated":
            return self
        if not self.protocol_version or not self.sampling_manifest_sha256:
            raise ValueError("adjudicated_corpus_missing_protocol_or_manifest")
        hashes = [
            self.sampling_manifest_sha256,
            self.target_judge_release_id,
            *self.blinded_packet_sha256,
            *self.label_file_sha256,
        ]
        if self.resolution_file_sha256:
            hashes.append(self.resolution_file_sha256)
        if not all(_is_sha256(value) for value in hashes):
            raise ValueError("adjudicated_corpus_invalid_receipt_hash")
        if (
            len(set(self.blinded_packet_sha256)) < 2
            or len(set(self.label_file_sha256)) < 2
        ):
            raise ValueError("adjudicated_corpus_missing_independent_label_receipts")
        if len({adjudicator_id.strip().casefold() for adjudicator_id in self.adjudicator_ids}) < 2:
            raise ValueError("adjudicated_corpus_requires_two_distinct_adjudicators")
        if self.conflict_count and not self.resolution_file_sha256:
            raise ValueError("adjudicated_corpus_missing_conflict_resolution")
        if self.inter_adjudicator_decision_agreement is None or self.inter_adjudicator_kappa is None:
            raise ValueError("adjudicated_corpus_missing_agreement_statistics")
        if (
            self.labels_frozen_at is None
            or self.labels_revealed_at is None
            or self.labels_revealed_at < self.labels_frozen_at
        ):
            raise ValueError("adjudicated_corpus_missing_label_freeze")
        return self


class GoldSetExpectation(BaseModel):
    decision: Decision
    rubric_scores: dict[str, int] = Field(default_factory=dict)
    claim_support_verdict: Literal["supported", "partially_supported", "unsupported"] | None = None
    overclaim_verdict: Literal["none", "mild", "significant"] | None = None
    synthesis_quality_verdict: Literal["strong", "adequate", "weak", "empty"] | None = None
    rationale: str = ""


class GoldSetEntry(BaseModel):
    entry_id: str
    article_type: ArticleType = ArticleType.RAPID_EVIDENCE_SYNTHESIS
    submission: SubmissionPayload
    expected: GoldSetExpectation
    tags: list[str] = Field(default_factory=list)
    notes: str = ""


class GoldSetCorpus(BaseModel):
    version: str = "gold-set-v1"
    entries: list[GoldSetEntry] = Field(default_factory=list)
    adjudication: GoldSetAdjudication = Field(default_factory=GoldSetAdjudication)

    @model_validator(mode="after")
    def require_certification_sample_floor(self) -> GoldSetCorpus:
        if self.adjudication.corpus_status != "adjudicated":
            return self
        if len(self.entries) < 100:
            raise ValueError("adjudicated_corpus_requires_at_least_100_cases")
        if len({entry.entry_id for entry in self.entries}) != len(self.entries):
            raise ValueError("adjudicated_corpus_requires_unique_case_ids")
        content_hashes = {
            hashlib.sha256(
                json.dumps(
                    entry.submission.model_dump(mode="json"),
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            for entry in self.entries
        }
        if len(content_hashes) != len(self.entries):
            raise ValueError("adjudicated_corpus_requires_unique_submissions")
        if {entry.article_type for entry in self.entries} != set(ArticleType):
            raise ValueError("adjudicated_corpus_must_span_supported_article_types")
        if len({entry.submission.domain_slug for entry in self.entries}) < 8:
            raise ValueError("adjudicated_corpus_requires_eight_domains")
        if {entry.expected.decision for entry in self.entries} != set(Decision):
            raise ValueError("adjudicated_corpus_must_span_expected_decisions")
        for entry in self.entries:
            expected = entry.expected
            if (
                set(expected.rubric_scores) != GOLD_SET_RUBRIC_KEYS
                or any(not 1 <= score <= 5 for score in expected.rubric_scores.values())
                or expected.claim_support_verdict is None
                or expected.overclaim_verdict is None
                or expected.synthesis_quality_verdict is None
                or len(expected.rationale.strip()) < 20
            ):
                raise ValueError("adjudicated_corpus_requires_complete_labels")
        return self
