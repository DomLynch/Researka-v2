from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from .models import ArticleType, Decision, SubmissionPayload


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

