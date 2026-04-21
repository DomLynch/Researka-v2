from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, Field


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


class ObjectType(StrEnum):
    SUBMISSION = "submission"
    REVIEW = "review"
    DECISION = "decision"
    PUBLICATION = "publication"
    AUDIT_REVIEW = "audit_review"


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


class SubmissionPayload(BaseModel):
    title: str
    abstract: str
    sections: dict[str, str] = Field(default_factory=dict)
    source_bundle: list[dict] = Field(default_factory=list)
    author_agent_id: str
    author_signature: str | None = None
    domain_slug: str = "general"
    core_claims_resolved: bool = True
    submitted_at: datetime = Field(default_factory=utc_now)


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
