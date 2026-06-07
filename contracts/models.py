from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Stage(StrEnum):
    INTAKE = "submission_intake"
    REVIEW = "autonomous_review"
    EDITORIAL = "autonomous_editorial_decision"
    PUBLISH = "autonomous_publish"


class JobStatus(StrEnum):
    QUEUED = "queued"
    LEASED = "leased"
    COMPLETED = "completed"
    FAILED = "failed"


class Decision(StrEnum):
    ACCEPT = "accept"
    REVISE = "revise"
    REJECT = "reject"


class ArticleType(StrEnum):
    ALPHA_MEMO = "alpha_memo"
    RAPID_EVIDENCE_SYNTHESIS = "rapid_evidence_synthesis"
    EMPIRICAL_STUDY = "empirical_study"
    # RESEARCH_SYNTHESIS is the v2 long-form path: full multi-section research
    # synthesis paper (~10-30k words) with cross-domain integration, numeric
    # traceability, and explicit mechanistic-vs-clinical separation. Designed
    # to accept the kind of paper the Research Agent Bot produces in its
    # full-synthesis pipeline, where RES would force destructive compression.
    RESEARCH_SYNTHESIS = "research_synthesis"


class ObjectType(StrEnum):
    SUBMISSION = "submission"
    REVIEW = "review"
    DECISION = "decision"
    PUBLICATION = "publication"
    AUDIT_REVIEW = "audit_review"
    AGENT_QUERY = "agent_query"


class EventType(StrEnum):
    JOB_QUEUED = "job_queued"
    JOB_LEASED = "job_leased"
    JOB_COMPLETED = "job_completed"
    JOB_FAILED = "job_failed"


class FailureClass(StrEnum):
    STRUCTURE_GATE = "structure_gate"
    COMPILE_BLOCKER = "compile_blocker"
    VALIDATION_ERROR = "validation_error"
    QUALITY_GATE = "quality_gate"
    PUBLISH_DEFERRED = "publish_deferred"
    DB_TIMEOUT = "db_timeout"
    JOB_TIMEOUT = "job_timeout"
    DB_CONNECTION_BAD = "db_connection_bad"
    PUBLISH_GATES_FAILED = "publish_gates_failed"
    TARGET_NOT_FOUND = "target_not_found"
    STALE_TARGET = "stale_target"
    ORPHAN_REFERENCE = "orphan_reference"
    REVIEW_MISSING = "review_missing"
    EXACT_QUOTE_MISSING = "exact_quote_missing"
    PROVIDER_ERROR = "provider_error"
    OTHER = "other"


class ProviderErrorClass(StrEnum):
    TIMEOUT = "timeout"
    RATE_LIMIT = "rate_limit"
    BAD_REQUEST = "bad_request"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    OTHER = "other"


class PublicationCounts(BaseModel):
    retrieved_count: int = 0
    selected_count: int = 0
    review_like_count: int = 0
    primary_like_count: int = 0
    year_start: int | None = None
    year_end: int | None = None


class GateResult(BaseModel):
    name: str
    passed: bool
    reason: str


class ProviderUsage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0


class RuntimeJob(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    target_object_id: str
    stage: Stage
    status: JobStatus = JobStatus.QUEUED
    payload: dict = Field(default_factory=dict)
    lease_expires_at: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)


class ResearchObject(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    object_type: ObjectType
    parent_object_id: str | None = None
    title: str
    body_markdown: str = ""
    metadata: dict = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)


def _markdown_summary(markdown: str | None, fallback: str) -> str:
    for line in str(markdown or "").splitlines():
        text = line.lstrip("#").strip()
        if text:
            return text[:500]
    return fallback


def _review_like_source(item: dict) -> bool:
    text = f"{item.get('title', '')} {item.get('evidence_type', '')}".lower()
    return any(token in text for token in ("review", "meta-analysis", "systematic"))


def _source_bundle_from_evidence_bundle(evidence_bundle: dict) -> list[dict]:
    verdict = evidence_bundle.get("publish_verdict") if isinstance(evidence_bundle, dict) else {}
    axes = verdict.get("axes") if isinstance(verdict, dict) else {}
    papers = axes.get("source_papers") if isinstance(axes, dict) else []
    if not isinstance(papers, list):
        return []
    bundle: list[dict] = []
    for index, paper in enumerate(papers, start=1):
        if not isinstance(paper, dict):
            continue
        title = str(paper.get("title") or f"Alpha memo source {index}").strip()
        entry = {
            "title": title,
            "doi": paper.get("doi") or None,
            "url": paper.get("url") or None,
            "year": paper.get("year") if isinstance(paper.get("year"), int) else None,
            "evidence_type": "review" if _review_like_source(paper) else "primary",
        }
        bundle.append(entry)
    return bundle


class SubmissionPayload(BaseModel):
    title: str
    abstract: str
    body_markdown: str | None = None
    sections: dict[str, str] = Field(default_factory=dict)
    source_bundle: list[dict] = Field(default_factory=list)
    author_agent_id: str
    article_type: ArticleType = ArticleType.RAPID_EVIDENCE_SYNTHESIS
    artifact_type: str | None = None
    agent_id: str | None = None
    topic: str | None = None
    markdown: str | None = None
    novelty_score: float | str | None = None
    confidence_score: float | str | None = None
    evidence_bundle: dict = Field(default_factory=dict)
    metadata: dict = Field(default_factory=dict)
    author_signature: str | None = None
    parent_submission_id: str | None = None
    domain_slug: str = "general"
    institution_name: str | None = None
    institution_ror: str | None = None
    ror_id: str | None = None
    raid_id: str | None = None
    core_claims_resolved: bool = True
    submitted_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="before")
    @classmethod
    def normalize_agent_artifact(cls, data: object) -> object:
        if not isinstance(data, dict):
            return data
        values = dict(data)
        if values.get("artifact_type") != ArticleType.ALPHA_MEMO.value and values.get("article_type") != ArticleType.ALPHA_MEMO.value:
            return values
        values["article_type"] = ArticleType.ALPHA_MEMO.value
        markdown = str(values.get("markdown") or values.get("body_markdown") or "")
        if markdown and not values.get("body_markdown"):
            values["body_markdown"] = markdown
        if not values.get("abstract"):
            values["abstract"] = _markdown_summary(markdown, str(values.get("title") or "Alpha memo"))
        if markdown and not values.get("sections"):
            values["sections"] = {"Evidence Landscape": markdown}
        if not values.get("source_bundle"):
            raw_evidence_bundle = values.get("evidence_bundle")
            evidence_bundle: dict = raw_evidence_bundle if isinstance(raw_evidence_bundle, dict) else {}
            values["source_bundle"] = _source_bundle_from_evidence_bundle(evidence_bundle)
        return values


class WorkflowContext(BaseModel):
    target_object_id: str
    domain_slug: str
    review_ids: list[str] = Field(default_factory=list)
    decision_id: str | None = None


class WorkflowOutcome(BaseModel):
    terminal_decision: Decision | None = None
    next_jobs: list[RuntimeJob] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class PublicationArtifact(BaseModel):
    title: str
    abstract: str
    body_markdown: str
    counts: PublicationCounts
    gates: list[GateResult] = Field(default_factory=list)


class RuntimeEvent(BaseModel):
    event_type: EventType
    target_object_id: str
    job_id: str | None = None
    worker_id: str | None = None
    payload: dict = Field(default_factory=dict)
    ts: datetime = Field(default_factory=utc_now)


class ApiKeyInfo(BaseModel):
    key_hash: str
    agent_id: str
    label: str = ""
    daily_limit: int = 0
    revoked: bool = False
    created_at: datetime = Field(default_factory=utc_now)


class ApiKeyCreateResponse(BaseModel):
    key_hash: str
    agent_id: str
    label: str = ""
    daily_limit: int = 0
    raw_key: str
    created_at: datetime = Field(default_factory=utc_now)


class AuditVerdict(StrEnum):
    AGREE = "agree"
    DISAGREE = "disagree"
    PARTIAL = "partial"


class AuditReview(BaseModel):
    """External auditor review of a system decision."""
    submission_id: str
    auditor_id: str
    auditor_verdict: Decision
    auditor_notes: str = ""
    system_verdict: Decision | None = None
    verdict_match: AuditVerdict | None = None
    confidence: float = 0.0  # 0.0-1.0
    created_at: datetime = Field(default_factory=utc_now)


class EvidenceGrade(StrEnum):
    """Badge taxonomy for a single claim. Mirrors the public Researka grade ladder."""
    EXPLORATORY = "exploratory"
    VERIFIED = "verified"
    CERTIFIED = "certified"
    CONTESTED = "contested"
    REJECTED = "rejected"


class ContradictionStatus(StrEnum):
    NONE = "none"
    CONTRADICTED = "contradicted"
    CORROBORATED = "corroborated"


class ClaimCard(BaseModel):
    """Atomic claim extracted from an accepted publication.

    First-class entity so badges, evidence index, verification page, RO-Crate,
    and contradiction signals can join/filter without re-parsing publication
    metadata blobs.
    """
    id: str = Field(default_factory=lambda: str(uuid4()))
    publication_id: str
    claim_text: str
    evidence_grade: EvidenceGrade = EvidenceGrade.EXPLORATORY
    citation_support: list[dict] = Field(default_factory=list)
    contradiction_status: ContradictionStatus = ContradictionStatus.NONE
    source_ids: list[str] = Field(default_factory=list)
    dw_chain_url: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
