from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

from .models import ArticleType, Decision, SubmissionPayload


class GoldSetExpectation(BaseModel):
    decision: Decision
    rubric_scores: dict[str, int] = Field(default_factory=dict)
    claim_support_verdict: Literal["supported", "partially_supported", "unsupported"] | None = None
    overclaim_verdict: Literal["none", "mild", "significant"] | None = None
    synthesis_quality_verdict: Literal["strong", "adequate", "weak", "empty"] | None = None
    rationale: str = ""


class GoldSetAdjudication(BaseModel):
    source: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    source_url: str = Field(min_length=1)
    status_text: str = Field(min_length=1)
    reviewer_count: int = Field(ge=1)
    retrieved_at: str = Field(min_length=1)
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class GoldSetEntry(BaseModel):
    entry_id: str
    article_type: ArticleType = ArticleType.RAPID_EVIDENCE_SYNTHESIS
    submission: SubmissionPayload
    expected: GoldSetExpectation
    tags: list[str] = Field(default_factory=list)
    notes: str = ""
    adjudication: GoldSetAdjudication | None = None


class GoldSetCorpus(BaseModel):
    version: str = "gold-set-v1"
    corpus_status: Literal["working", "adjudicated"] = "working"
    evaluation_scope: Literal["full_workflow", "reviewer_only"] = "full_workflow"
    ground_truth_source: str = ""
    ground_truth_url: str = ""
    label_policy: str = ""
    generated_at: str = ""
    entries: list[GoldSetEntry] = Field(default_factory=list)

    @model_validator(mode="after")
    def require_adjudication_receipts(self) -> "GoldSetCorpus":
        if self.corpus_status == "adjudicated" and any(entry.adjudication is None for entry in self.entries):
            raise ValueError("adjudicated corpus entries require independent adjudication receipts")
        return self
