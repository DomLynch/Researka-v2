from __future__ import annotations

import hashlib
import json
import os
import secrets
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol

from contracts import (
    ApiKeyCreateResponse,
    ApiKeyInfo,
    AuditReview,
    AuditVerdict,
    ClaimCard,
    EventType,
    FailureClass,
    JobStatus,
    ObjectType,
    ResearchObject,
    RuntimeEvent,
    RuntimeJob,
)

OSF_TOKEN_METADATA_ENCRYPTION_PREFIX = "fernet:v1:"


def _job_available(job: RuntimeJob, now: datetime) -> bool:
    raw = job.payload.get("retry_not_before")
    if not raw:
        return True
    try:
        not_before = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return True
    if not_before.tzinfo is None:
        not_before = not_before.replace(tzinfo=timezone.utc)
    return not_before <= now


def _claim_card_to_row(card: ClaimCard) -> tuple:
    return (
        card.id,
        card.publication_id,
        card.claim_text,
        card.evidence_grade.value,
        json.dumps(card.citation_support),
        card.contradiction_status.value,
        json.dumps(card.source_ids),
        card.dw_chain_url,
        card.created_at,
    )


def _claim_card_from_row(row: dict | None) -> ClaimCard | None:
    if row is None:
        return None
    return ClaimCard(
        id=row["id"],
        publication_id=row["publication_id"],
        claim_text=row["claim_text"],
        evidence_grade=row["evidence_grade"],
        citation_support=json.loads(row["citation_support"]),
        contradiction_status=row["contradiction_status"],
        source_ids=json.loads(row["source_ids"]),
        dw_chain_url=row["dw_chain_url"],
        created_at=row["created_at"],
    )


def _read_secret_value(*, direct_env: str, path_env: str) -> str | None:
    direct = os.environ.get(direct_env)
    if direct and direct.strip():
        return direct.strip()
    secret_path = os.environ.get(path_env)
    if secret_path and secret_path.strip():
        path = Path(secret_path.strip())
        if not path.exists():
            raise RuntimeError(f"{path_env.lower()}_missing")
        value = path.read_text().strip()
        if not value:
            raise RuntimeError(f"{path_env.lower()}_empty")
        return value
    return None


def _osf_token_cipher():
    key = _read_secret_value(
        direct_env="RESEARKA_V2_OSF_TOKEN_ENCRYPTION_KEY",
        path_env="RESEARKA_V2_OSF_TOKEN_ENCRYPTION_KEY_PATH",
    )
    if not key:
        raise RuntimeError("researka_v2_osf_token_encryption_key_required")
    try:
        from cryptography.fernet import Fernet
    except ImportError as exc:
        raise RuntimeError("cryptography_required_for_osf_token_encryption") from exc
    try:
        return Fernet(key.encode("ascii"))
    except Exception as exc:
        raise RuntimeError("invalid_osf_token_encryption_key") from exc


def _encode_osf_token_metadata(token_metadata: dict) -> str:
    payload = json.dumps(token_metadata, separators=(",", ":"), sort_keys=True)
    cipher = _osf_token_cipher()
    return OSF_TOKEN_METADATA_ENCRYPTION_PREFIX + cipher.encrypt(payload.encode("utf-8")).decode("ascii")


def _decode_osf_token_metadata(raw: str) -> dict | None:
    if raw.startswith(OSF_TOKEN_METADATA_ENCRYPTION_PREFIX):
        cipher = _osf_token_cipher()
        encrypted = raw.removeprefix(OSF_TOKEN_METADATA_ENCRYPTION_PREFIX)
        payload = cipher.decrypt(encrypted.encode("ascii")).decode("utf-8")
    else:
        payload = raw
    token = json.loads(payload)
    return token if isinstance(token, dict) else None


class RuntimeRepository(Protocol):
    def reset(self) -> None: ...
    def create_object(self, obj: ResearchObject) -> ResearchObject: ...
    def create_object_and_enqueue_job(self, obj: ResearchObject, job: RuntimeJob) -> tuple[ResearchObject, RuntimeJob]: ...
    def get_object(self, object_id: str) -> ResearchObject | None: ...
    def update_object_metadata(self, object_id: str, metadata: dict) -> ResearchObject | None: ...
    def list_objects(self, object_type: ObjectType | str | None = None) -> list[ResearchObject]: ...
    def children_of(self, parent_object_id: str, object_type: ObjectType | str | None = None) -> list[ResearchObject]: ...
    def publication_for_target(self, target_object_id: str) -> ResearchObject | None: ...
    def enqueue_job(self, job: RuntimeJob) -> RuntimeJob: ...
    def get_job(self, job_id: str) -> RuntimeJob | None: ...
    def queued_jobs(self, stage: str | None = None) -> list[RuntimeJob]: ...
    def active_jobs(self) -> list[RuntimeJob]: ...
    def claim_next_job(
        self,
        *,
        target_object_id: str | None = None,
        worker_id: str | None = None,
    ) -> RuntimeJob | None: ...
    def complete_job(self, job_id: str, *, event: RuntimeEvent | None = None) -> None: ...
    def fail_job(
        self,
        job_id: str,
        *,
        reason: str,
        failure_class: FailureClass | None = None,
        event: RuntimeEvent | None = None,
    ) -> None: ...
    def record_event(self, event: RuntimeEvent) -> None: ...
    def list_events(self) -> list[RuntimeEvent]: ...
    def events_for_target(self, target_object_id: str) -> list[RuntimeEvent]: ...

    # API key management
    def create_api_key(self, agent_id: str, *, label: str = "", daily_limit: int = 0) -> ApiKeyCreateResponse: ...
    def validate_api_key(self, raw_key: str) -> str | None: ...
    def revoke_api_key(self, key_hash: str) -> bool: ...
    def list_api_keys(self) -> list[ApiKeyInfo]: ...
    def record_api_key_usage(self, key_hash: str) -> None: ...
    def get_api_key_usage_today(self, key_hash: str) -> int: ...
    def store_osf_oauth_token(self, agent_id: str, token_metadata: dict) -> None: ...
    def get_osf_oauth_token(self, agent_id: str) -> dict | None: ...

    # Audit review management
    def create_audit_review(self, review: AuditReview) -> AuditReview: ...
    def list_audit_reviews(self, submission_id: str | None = None) -> list[AuditReview]: ...
    def audit_summary(self, submission_id: str | None = None) -> dict: ...

    # Claim cards (per-publication atomic claims)
    def save_claim_card(self, card: ClaimCard) -> ClaimCard: ...
    def list_claim_cards(self, publication_id: str) -> list[ClaimCard]: ...


class InMemoryRuntimeRepository:
    def __init__(self, *, lease_ttl_seconds: int = 300) -> None:
        self.objects: dict[str, ResearchObject] = {}
        self.jobs: dict[str, RuntimeJob] = {}
        self.events: list[RuntimeEvent] = []
        self.jobs_by_target: dict[str, list[str]] = defaultdict(list)
        self.objects_by_parent: dict[str, list[str]] = defaultdict(list)
        self.publication_by_target: dict[str, str] = {}
        self.api_keys: dict[str, ApiKeyInfo] = {}
        self.api_key_usage: dict[tuple[str, str], int] = {}
        self.osf_oauth_tokens: dict[str, dict] = {}
        self.audit_reviews: list[AuditReview] = []
        self.claim_cards: list[ClaimCard] = []
        self.lease_ttl_seconds = lease_ttl_seconds

    def reset(self) -> None:
        self.objects.clear()
        self.jobs.clear()
        self.events.clear()
        self.jobs_by_target.clear()
        self.objects_by_parent.clear()
        self.publication_by_target.clear()
        self.api_keys.clear()
        self.api_key_usage.clear()
        self.osf_oauth_tokens.clear()
        self.audit_reviews.clear()
        self.claim_cards.clear()

    def create_object(self, obj: ResearchObject) -> ResearchObject:
        self.objects[obj.id] = obj
        if obj.parent_object_id:
            self.objects_by_parent[obj.parent_object_id].append(obj.id)
        if obj.object_type == ObjectType.PUBLICATION and obj.parent_object_id:
            self.publication_by_target[obj.parent_object_id] = obj.id
        return obj

    def create_object_and_enqueue_job(self, obj: ResearchObject, job: RuntimeJob) -> tuple[ResearchObject, RuntimeJob]:
        if job.target_object_id != obj.id:
            raise ValueError("job_target_must_match_object")
        self.create_object(obj)
        try:
            return obj, self.enqueue_job(job)
        except Exception:
            self.objects.pop(obj.id, None)
            if obj.parent_object_id:
                self.objects_by_parent[obj.parent_object_id].remove(obj.id)
            raise

    def get_object(self, object_id: str) -> ResearchObject | None:
        return self.objects.get(object_id)

    def update_object_metadata(self, object_id: str, metadata: dict) -> ResearchObject | None:
        obj = self.objects.get(object_id)
        if obj is None:
            return None
        updated = obj.model_copy(update={"metadata": dict(metadata)})
        self.objects[object_id] = updated
        return updated

    def list_objects(self, object_type: ObjectType | str | None = None) -> list[ResearchObject]:
        objects = list(self.objects.values())
        if object_type is None:
            return objects
        return [obj for obj in objects if obj.object_type == object_type]

    def children_of(self, parent_object_id: str, object_type: ObjectType | str | None = None) -> list[ResearchObject]:
        children = [self.objects[obj_id] for obj_id in self.objects_by_parent.get(parent_object_id, [])]
        if object_type is None:
            return children
        return [obj for obj in children if obj.object_type == object_type]

    def publication_for_target(self, target_object_id: str) -> ResearchObject | None:
        publication_id = self.publication_by_target.get(target_object_id)
        if publication_id is None:
            return None
        return self.objects.get(publication_id)

    def enqueue_job(self, job: RuntimeJob) -> RuntimeJob:
        if job.stage.value == "autonomous_publish":
            existing_publication = self.publication_for_target(job.target_object_id)
            if existing_publication is not None:
                completed = RuntimeJob(
                    target_object_id=job.target_object_id,
                    stage=job.stage,
                    status=JobStatus.COMPLETED,
                    payload={"deduped_to_existing_publication": True},
                )
                self.jobs[completed.id] = completed
                self.jobs_by_target[completed.target_object_id].append(completed.id)
                return completed
        for existing_id in self.jobs_by_target[job.target_object_id]:
            existing = self.jobs[existing_id]
            if existing.stage == job.stage and existing.status in {
                JobStatus.QUEUED,
                JobStatus.LEASED,
                JobStatus.COMPLETED,
            }:
                return existing
        self.jobs[job.id] = job
        self.jobs_by_target[job.target_object_id].append(job.id)
        self.record_event(
            RuntimeEvent(
                event_type=EventType.JOB_QUEUED,
                target_object_id=job.target_object_id,
                job_id=job.id,
                payload={"stage": job.stage.value, **job.payload},
            )
        )
        return job

    def get_job(self, job_id: str) -> RuntimeJob | None:
        return self.jobs.get(job_id)

    def queued_jobs(self, stage: str | None = None) -> list[RuntimeJob]:
        jobs = [job for job in self.jobs.values() if job.status == JobStatus.QUEUED]
        if stage is None:
            return jobs
        return [job for job in jobs if job.stage.value == stage]

    def active_jobs(self) -> list[RuntimeJob]:
        return [
            job
            for job in self.jobs.values()
            if job.status in {JobStatus.QUEUED, JobStatus.LEASED}
        ]

    def claim_next_job(
        self,
        *,
        target_object_id: str | None = None,
        worker_id: str | None = None,
    ) -> RuntimeJob | None:
        now = datetime.now(timezone.utc)
        for job in self.jobs.values():
            if job.status == JobStatus.LEASED and job.lease_expires_at and job.lease_expires_at <= now:
                job.status = JobStatus.QUEUED
                job.lease_expires_at = None
                self.record_event(
                    RuntimeEvent(
                        event_type=EventType.JOB_QUEUED,
                        target_object_id=job.target_object_id,
                        job_id=job.id,
                        payload={"stage": job.stage.value, "lease_reclaimed": True},
                    )
                )
        jobs = sorted(
            (job for job in self.queued_jobs() if _job_available(job, now)),
            key=lambda job: job.created_at,
        )
        if target_object_id is not None:
            jobs = [j for j in jobs if j.target_object_id == target_object_id]
        if not jobs:
            return None
        job = jobs[0]
        job.status = JobStatus.LEASED
        job.lease_expires_at = now + timedelta(seconds=self.lease_ttl_seconds)
        self.record_event(
            RuntimeEvent(
                event_type=EventType.JOB_LEASED,
                target_object_id=job.target_object_id,
                job_id=job.id,
                worker_id=worker_id,
                payload={"stage": job.stage.value},
                ts=now,
            )
        )
        return job

    def complete_job(self, job_id: str, *, event: RuntimeEvent | None = None) -> None:
        self.jobs[job_id].status = JobStatus.COMPLETED
        self.jobs[job_id].lease_expires_at = None
        if event is not None:
            self.record_event(event)

    def fail_job(
        self,
        job_id: str,
        *,
        reason: str,
        failure_class: FailureClass | None = None,
        event: RuntimeEvent | None = None,
    ) -> None:
        self.jobs[job_id].status = JobStatus.FAILED
        self.jobs[job_id].lease_expires_at = None
        self.jobs[job_id].payload["failure_reason"] = reason
        if failure_class is not None:
            self.jobs[job_id].payload["failure_class"] = failure_class.value
        if event is not None:
            self.record_event(event)

    def record_event(self, event: RuntimeEvent) -> None:
        self.events.append(event)

    def list_events(self) -> list[RuntimeEvent]:
        return list(self.events)

    def events_for_target(self, target_object_id: str) -> list[RuntimeEvent]:
        return sorted(
            (event for event in self.events if event.target_object_id == target_object_id),
            key=lambda event: event.ts,
        )

    # --- API key management (in-memory) ---

    def _hash_key(self, raw_key: str) -> str:
        return hashlib.sha256(raw_key.encode()).hexdigest()

    def create_api_key(self, agent_id: str, *, label: str = "", daily_limit: int = 0) -> ApiKeyCreateResponse:
        raw_key = f"rk_{secrets.token_urlsafe(32)}"
        key_hash = self._hash_key(raw_key)
        info = ApiKeyInfo(
            key_hash=key_hash,
            agent_id=agent_id,
            label=label,
            daily_limit=daily_limit,
            created_at=datetime.now(timezone.utc),
        )
        self.api_keys[key_hash] = info
        return ApiKeyCreateResponse(
            key_hash=key_hash,
            agent_id=agent_id,
            label=label,
            daily_limit=daily_limit,
            raw_key=raw_key,
            created_at=info.created_at,
        )

    def validate_api_key(self, raw_key: str) -> str | None:
        key_hash = self._hash_key(raw_key)
        info = self.api_keys.get(key_hash)
        if info is None or info.revoked:
            return None
        if info.daily_limit > 0:
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            used = self.api_key_usage.get((key_hash, today), 0)
            if used >= info.daily_limit:
                return None
        return info.agent_id

    def revoke_api_key(self, key_hash: str) -> bool:
        info = self.api_keys.get(key_hash)
        if info is None or info.revoked:
            return False
        info.revoked = True
        return True

    def list_api_keys(self) -> list[ApiKeyInfo]:
        return list(self.api_keys.values())

    def record_api_key_usage(self, key_hash: str) -> None:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self.api_key_usage[(key_hash, today)] = self.api_key_usage.get((key_hash, today), 0) + 1

    def get_api_key_usage_today(self, key_hash: str) -> int:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return self.api_key_usage.get((key_hash, today), 0)

    def store_osf_oauth_token(self, agent_id: str, token_metadata: dict) -> None:
        self.osf_oauth_tokens[agent_id] = dict(token_metadata)

    def get_osf_oauth_token(self, agent_id: str) -> dict | None:
        token = self.osf_oauth_tokens.get(agent_id)
        return dict(token) if token is not None else None

    def create_audit_review(self, review: AuditReview) -> AuditReview:
        self.audit_reviews.append(review)
        return review

    def list_audit_reviews(self, submission_id: str | None = None) -> list[AuditReview]:
        if submission_id is None:
            return list(self.audit_reviews)
        return [r for r in self.audit_reviews if r.submission_id == submission_id]

    def audit_summary(self, submission_id: str | None = None) -> dict:
        reviews = self.list_audit_reviews(submission_id)
        total = len(reviews)
        if total == 0:
            return {"total_audits": 0, "agreement_rate": 0.0, "by_auditor": {}, "by_verdict": {}}
        agree = sum(1 for r in reviews if r.verdict_match == AuditVerdict.AGREE)
        by_auditor: dict[str, dict] = {}
        by_verdict: dict[str, int] = {}
        for r in reviews:
            by_auditor.setdefault(r.auditor_id, {"total": 0, "agree": 0})
            by_auditor[r.auditor_id]["total"] += 1
            if r.verdict_match == AuditVerdict.AGREE:
                by_auditor[r.auditor_id]["agree"] += 1
            v = r.auditor_verdict.value if r.auditor_verdict else "unknown"
            by_verdict[v] = by_verdict.get(v, 0) + 1
        return {
            "total_audits": total,
            "agreement_rate": round(agree / total, 4),
            "by_auditor": by_auditor,
            "by_verdict": by_verdict,
        }

    # --- Claim cards (in-memory) ---

    def save_claim_card(self, card: ClaimCard) -> ClaimCard:
        self.claim_cards.append(card)
        return card

    def list_claim_cards(self, publication_id: str) -> list[ClaimCard]:
        return sorted(
            (c for c in self.claim_cards if c.publication_id == publication_id),
            key=lambda c: c.created_at,
        )


def postgres_dsn_from_env() -> str | None:
    return os.environ.get("TEST_POSTGRES_DSN") or os.environ.get("RESEARKA_V2_POSTGRES_DSN")


def postgres_runtime_available() -> bool:
    if not postgres_dsn_from_env():
        return False
    try:
        import psycopg  # noqa: F401
    except Exception:
        return False
    return True


def _postgres_connect_timeout_seconds() -> int:
    raw = os.environ.get("RESEARKA_V2_POSTGRES_CONNECT_TIMEOUT_SEC", "5").strip()
    return max(1, int(raw)) if raw.isdigit() else 5


class PostgresRuntimeRepository:
    def __init__(self, dsn: str, *, lease_ttl_seconds: int = 300) -> None:
        try:
            import psycopg
            from psycopg.rows import dict_row
        except Exception as exc:
            raise RuntimeError("psycopg is required for PostgresRuntimeRepository") from exc
        self._psycopg = psycopg
        self._dict_row = dict_row
        self.dsn = dsn
        self.lease_ttl_seconds = lease_ttl_seconds
        self.connect_timeout_seconds = _postgres_connect_timeout_seconds()
        self._ensure_schema()

    def _connect(self):
        return self._psycopg.connect(
            self.dsn,
            row_factory=self._dict_row,
            connect_timeout=self.connect_timeout_seconds,
        )

    def _ensure_schema(self) -> None:
        if self._alembic_manages_schema():
            return
        self._create_tables_raw()

    def _alembic_manages_schema(self) -> bool:
        """Return True if alembic_version table exists and is current.

        When True, Alembic owns schema lifecycle — skip raw table creation.
        """
        try:
            with self._connect() as conn, conn.cursor() as cur:
                cur.execute(
                    "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
                    "WHERE table_name = 'alembic_version')"
                )
                row = cur.fetchone()
                has_version_table = bool(row and row.get("exists"))
            if not has_version_table:
                return False
            # Version table exists — assume managed.  Auto-migrate handles upgrades.
            self._auto_migrate()
            return True
        except Exception:
            return False

    def _auto_migrate(self) -> None:
        """Run alembic upgrade head if available. Best-effort."""
        try:
            from alembic import command as alembic_command  # type: ignore[attr-defined]
            from alembic.config import Config
        except ImportError:
            pass
        else:
            config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
            config.set_main_option("sqlalchemy.url", self.dsn)
            alembic_command.upgrade(config, "head")

    def _create_tables_raw(self) -> None:
        """Fallback: create all tables via raw SQL (no Alembic dependency).

        Used by tests and local dev when alembic is not installed.
        """
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(62004201)")
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS research_objects (
                    id TEXT PRIMARY KEY,
                    object_type TEXT NOT NULL,
                    parent_object_id TEXT NULL,
                    title TEXT NOT NULL,
                    body_markdown TEXT NOT NULL,
                    metadata TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS runtime_jobs (
                    id TEXT PRIMARY KEY,
                    target_object_id TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    lease_expires_at TIMESTAMPTZ NULL,
                    created_at TIMESTAMPTZ NOT NULL
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS runtime_events (
                    ts TIMESTAMPTZ NOT NULL,
                    event_type TEXT NOT NULL,
                    target_object_id TEXT NOT NULL,
                    job_id TEXT NULL,
                    worker_id TEXT NULL,
                    payload TEXT NOT NULL
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS api_keys (
                    key_hash TEXT PRIMARY KEY,
                    agent_id TEXT NOT NULL,
                    label TEXT NOT NULL DEFAULT '',
                    daily_limit INTEGER NOT NULL DEFAULT 0,
                    revoked BOOLEAN NOT NULL DEFAULT FALSE,
                    created_at TIMESTAMPTZ NOT NULL
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS api_key_usage (
                    key_hash TEXT NOT NULL,
                    day TEXT NOT NULL,
                    count INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (key_hash, day)
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS osf_oauth_tokens (
                    agent_id TEXT PRIMARY KEY,
                    token_metadata TEXT NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS audit_reviews (
                    submission_id TEXT NOT NULL,
                    auditor_id TEXT NOT NULL,
                    auditor_verdict TEXT NOT NULL,
                    auditor_notes TEXT NOT NULL DEFAULT '',
                    system_verdict TEXT NULL,
                    verdict_match TEXT NULL,
                    confidence REAL NOT NULL DEFAULT 0.0,
                    created_at TIMESTAMPTZ NOT NULL
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS claim_cards (
                    id TEXT PRIMARY KEY,
                    publication_id TEXT NOT NULL,
                    claim_text TEXT NOT NULL,
                    evidence_grade TEXT NOT NULL,
                    citation_support TEXT NOT NULL DEFAULT '[]',
                    contradiction_status TEXT NOT NULL DEFAULT 'none',
                    source_ids TEXT NOT NULL DEFAULT '[]',
                    dw_chain_url TEXT NULL,
                    created_at TIMESTAMPTZ NOT NULL
                )
                """
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_claim_cards_publication_id "
                "ON claim_cards(publication_id)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_runtime_events_target_ts "
                "ON runtime_events(target_object_id, ts)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_runtime_jobs_status_created "
                "ON runtime_jobs(status, created_at)"
            )
            cur.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_runtime_jobs_active_target_stage "
                "ON runtime_jobs(target_object_id, stage) WHERE status IN ('queued', 'leased')"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_research_objects_parent_type "
                "ON research_objects(parent_object_id, object_type, created_at)"
            )
            cur.execute("SELECT pg_advisory_unlock(62004201)")
            conn.commit()

    def reset(self) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "TRUNCATE claim_cards, audit_reviews, osf_oauth_tokens, api_key_usage, "
                "api_keys, runtime_events, runtime_jobs, research_objects;"
            )
            conn.commit()

    def _object_from_row(self, row: dict | None) -> ResearchObject | None:
        if row is None:
            return None
        return ResearchObject(
            id=row["id"],
            object_type=row["object_type"],
            parent_object_id=row["parent_object_id"],
            title=row["title"],
            body_markdown=row["body_markdown"],
            metadata=json.loads(row["metadata"]),
            created_at=row["created_at"],
        )

    def _job_from_row(self, row: dict | None) -> RuntimeJob | None:
        if row is None:
            return None
        return RuntimeJob(
            id=row["id"],
            target_object_id=row["target_object_id"],
            stage=row["stage"],
            status=row["status"],
            payload=json.loads(row["payload"]),
            lease_expires_at=row["lease_expires_at"],
            created_at=row["created_at"],
        )

    def _event_from_row(self, row: dict) -> RuntimeEvent:
        return RuntimeEvent(
            ts=row["ts"],
            event_type=row["event_type"],
            target_object_id=row["target_object_id"],
            job_id=row["job_id"],
            worker_id=row["worker_id"],
            payload=json.loads(row["payload"]),
        )

    def create_object(self, obj: ResearchObject) -> ResearchObject:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO research_objects (id, object_type, parent_object_id, title, body_markdown, metadata, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    obj.id,
                    obj.object_type,
                    obj.parent_object_id,
                    obj.title,
                    obj.body_markdown,
                    json.dumps(obj.metadata),
                    obj.created_at,
                ),
            )
            conn.commit()
        return obj

    def create_object_and_enqueue_job(self, obj: ResearchObject, job: RuntimeJob) -> tuple[ResearchObject, RuntimeJob]:
        if job.target_object_id != obj.id:
            raise ValueError("job_target_must_match_object")
        event = RuntimeEvent(
            event_type=EventType.JOB_QUEUED,
            target_object_id=job.target_object_id,
            job_id=job.id,
            payload={"stage": job.stage.value, **job.payload},
        )
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO research_objects (id, object_type, parent_object_id, title, body_markdown, metadata, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    obj.id,
                    obj.object_type,
                    obj.parent_object_id,
                    obj.title,
                    obj.body_markdown,
                    json.dumps(obj.metadata),
                    obj.created_at,
                ),
            )
            cur.execute(
                """
                INSERT INTO runtime_jobs (id, target_object_id, stage, status, payload, lease_expires_at, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    job.id,
                    job.target_object_id,
                    job.stage.value,
                    job.status.value,
                    json.dumps(job.payload),
                    job.lease_expires_at,
                    job.created_at,
                ),
            )
            self._insert_event(cur, event)
            conn.commit()
        return obj, job

    def get_object(self, object_id: str) -> ResearchObject | None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM research_objects WHERE id = %s", (object_id,))
            return self._object_from_row(cur.fetchone())

    def update_object_metadata(self, object_id: str, metadata: dict) -> ResearchObject | None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE research_objects
                SET metadata = %s
                WHERE id = %s
                RETURNING *
                """,
                (json.dumps(metadata), object_id),
            )
            row = cur.fetchone()
            conn.commit()
            return self._object_from_row(row)

    def list_objects(self, object_type: ObjectType | str | None = None) -> list[ResearchObject]:
        with self._connect() as conn, conn.cursor() as cur:
            if object_type is None:
                cur.execute("SELECT * FROM research_objects ORDER BY created_at ASC")
            else:
                cur.execute("SELECT * FROM research_objects WHERE object_type = %s ORDER BY created_at ASC", (str(object_type),))
            objects = [self._object_from_row(row) for row in cur.fetchall()]
            return [obj for obj in objects if obj is not None]

    def children_of(self, parent_object_id: str, object_type: ObjectType | str | None = None) -> list[ResearchObject]:
        with self._connect() as conn, conn.cursor() as cur:
            if object_type is None:
                cur.execute(
                    "SELECT * FROM research_objects WHERE parent_object_id = %s ORDER BY created_at ASC",
                    (parent_object_id,),
                )
            else:
                cur.execute(
                    "SELECT * FROM research_objects WHERE parent_object_id = %s AND object_type = %s ORDER BY created_at ASC",
                    (parent_object_id, str(object_type)),
                )
            objects = [self._object_from_row(row) for row in cur.fetchall()]
            return [obj for obj in objects if obj is not None]

    def publication_for_target(self, target_object_id: str) -> ResearchObject | None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM research_objects
                WHERE object_type = %s AND parent_object_id = %s
                ORDER BY created_at ASC
                LIMIT 1
                """,
                (ObjectType.PUBLICATION.value, target_object_id),
            )
            return self._object_from_row(cur.fetchone())

    def enqueue_job(self, job: RuntimeJob) -> RuntimeJob:
        if job.stage.value == "autonomous_publish":
            existing_publication = self.publication_for_target(job.target_object_id)
            if existing_publication is not None:
                completed = RuntimeJob(
                    target_object_id=job.target_object_id,
                    stage=job.stage,
                    status=JobStatus.COMPLETED,
                    payload={"deduped_to_existing_publication": True},
                )
                self._insert_job(completed)
                return completed
        existing_job = self._existing_job_for_stage(job)
        if existing_job is not None:
            return existing_job
        try:
            self._insert_job(job)
        except self._psycopg.errors.UniqueViolation:
            existing_job = self._existing_job_for_stage(job)
            if existing_job is None:
                raise
            return existing_job
        return job

    def _existing_job_for_stage(self, job: RuntimeJob) -> RuntimeJob | None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM runtime_jobs
                WHERE target_object_id = %s AND stage = %s
                  AND status IN ('queued', 'leased', 'completed')
                ORDER BY created_at ASC
                LIMIT 1
                """,
                (job.target_object_id, job.stage.value),
            )
            return self._job_from_row(cur.fetchone())

    def _insert_job(self, job: RuntimeJob) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO runtime_jobs (id, target_object_id, stage, status, payload, lease_expires_at, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    job.id,
                    job.target_object_id,
                    job.stage.value,
                    job.status.value,
                    json.dumps(job.payload),
                    job.lease_expires_at,
                    job.created_at,
                ),
            )
            if job.status == JobStatus.QUEUED:
                self._insert_event(
                    cur,
                    RuntimeEvent(
                        event_type=EventType.JOB_QUEUED,
                        target_object_id=job.target_object_id,
                        job_id=job.id,
                        payload={"stage": job.stage.value, **job.payload},
                    ),
                )
            conn.commit()

    def get_job(self, job_id: str) -> RuntimeJob | None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM runtime_jobs WHERE id = %s", (job_id,))
            return self._job_from_row(cur.fetchone())

    def queued_jobs(self, stage: str | None = None) -> list[RuntimeJob]:
        with self._connect() as conn, conn.cursor() as cur:
            if stage is None:
                cur.execute("SELECT * FROM runtime_jobs WHERE status = 'queued' ORDER BY created_at ASC")
            else:
                cur.execute(
                    "SELECT * FROM runtime_jobs WHERE status = 'queued' AND stage = %s ORDER BY created_at ASC",
                    (stage,),
                )
            jobs = [self._job_from_row(row) for row in cur.fetchall()]
            return [job for job in jobs if job is not None]

    def active_jobs(self) -> list[RuntimeJob]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM runtime_jobs WHERE status IN ('queued', 'leased') ORDER BY created_at ASC"
            )
            jobs = [self._job_from_row(row) for row in cur.fetchall()]
            return [job for job in jobs if job is not None]

    def claim_next_job(
        self,
        *,
        target_object_id: str | None = None,
        worker_id: str | None = None,
    ) -> RuntimeJob | None:
        now = datetime.now(timezone.utc)
        lease_expires_at = now + timedelta(seconds=self.lease_ttl_seconds)
        with self._connect() as conn, conn.cursor() as cur:
            # Always reset expired leases globally — scoped filter is on the claim query only
            cur.execute(
                """
                UPDATE runtime_jobs
                SET status = 'queued', lease_expires_at = NULL
                WHERE status = 'leased' AND lease_expires_at IS NOT NULL AND lease_expires_at <= %s
                RETURNING id, target_object_id, stage
                """,
                (now,),
            )
            for reclaimed in cur.fetchall():
                self._insert_event(
                    cur,
                    RuntimeEvent(
                        event_type=EventType.JOB_QUEUED,
                        target_object_id=reclaimed["target_object_id"],
                        job_id=reclaimed["id"],
                        payload={"stage": reclaimed["stage"], "lease_reclaimed": True},
                    ),
                )
            if target_object_id is not None:
                cur.execute(
                    """
                    WITH next_job AS (
                        SELECT id
                        FROM runtime_jobs
                        WHERE status = 'queued'
                          AND target_object_id = %s
                          AND COALESCE(NULLIF(payload::jsonb ->> 'retry_not_before', '')::timestamptz, '-infinity') <= %s
                        ORDER BY created_at ASC
                        FOR UPDATE SKIP LOCKED
                        LIMIT 1
                    )
                    UPDATE runtime_jobs
                    SET status = 'leased', lease_expires_at = %s
                    WHERE id = (SELECT id FROM next_job)
                    RETURNING *
                    """,
                    (target_object_id, now, lease_expires_at),
                )
            else:
                cur.execute(
                    """
                    WITH next_job AS (
                        SELECT id
                        FROM runtime_jobs
                        WHERE status = 'queued'
                          AND COALESCE(NULLIF(payload::jsonb ->> 'retry_not_before', '')::timestamptz, '-infinity') <= %s
                        ORDER BY created_at ASC
                        FOR UPDATE SKIP LOCKED
                        LIMIT 1
                    )
                    UPDATE runtime_jobs
                    SET status = 'leased', lease_expires_at = %s
                    WHERE id = (SELECT id FROM next_job)
                    RETURNING *
                    """,
                    (now, lease_expires_at),
                )
            row = cur.fetchone()
            if row is None:
                conn.commit()
                return None
            job = self._job_from_row(row)
            if job is None:
                raise RuntimeError("claimed_job_decode_failed")
            self._insert_event(
                cur,
                RuntimeEvent(
                    event_type=EventType.JOB_LEASED,
                    target_object_id=job.target_object_id,
                    job_id=job.id,
                    worker_id=worker_id,
                    payload={"stage": job.stage.value},
                    ts=now,
                ),
            )
            conn.commit()
            return job

    def complete_job(self, job_id: str, *, event: RuntimeEvent | None = None) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("UPDATE runtime_jobs SET status = 'completed', lease_expires_at = NULL WHERE id = %s", (job_id,))
            if event is not None:
                self._insert_event(cur, event)
            conn.commit()

    def fail_job(
        self,
        job_id: str,
        *,
        reason: str,
        failure_class: FailureClass | None = None,
        event: RuntimeEvent | None = None,
    ) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT payload FROM runtime_jobs WHERE id = %s FOR UPDATE", (job_id,))
            row = cur.fetchone()
            if row is None:
                conn.commit()
                return
            payload = json.loads(row["payload"])
            payload["failure_reason"] = reason
            if failure_class is not None:
                payload["failure_class"] = failure_class.value
            cur.execute(
                "UPDATE runtime_jobs SET status = 'failed', payload = %s, lease_expires_at = NULL WHERE id = %s",
                (json.dumps(payload), job_id),
            )
            if event is not None:
                self._insert_event(cur, event)
            conn.commit()

    def record_event(self, event: RuntimeEvent) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            self._insert_event(cur, event)
            conn.commit()

    @staticmethod
    def _insert_event(cur, event: RuntimeEvent) -> None:
        cur.execute(
            """
            INSERT INTO runtime_events (ts, event_type, target_object_id, job_id, worker_id, payload)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                event.ts,
                event.event_type.value,
                event.target_object_id,
                event.job_id,
                event.worker_id,
                json.dumps(event.payload),
            ),
        )

    def list_events(self) -> list[RuntimeEvent]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM runtime_events ORDER BY ts ASC")
            return [self._event_from_row(row) for row in cur.fetchall()]

    def events_for_target(self, target_object_id: str) -> list[RuntimeEvent]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM runtime_events WHERE target_object_id = %s ORDER BY ts ASC",
                (target_object_id,),
            )
            return [self._event_from_row(row) for row in cur.fetchall()]

    # --- API key management (postgres) ---

    def _hash_key(self, raw_key: str) -> str:
        return hashlib.sha256(raw_key.encode()).hexdigest()

    def create_api_key(self, agent_id: str, *, label: str = "", daily_limit: int = 0) -> ApiKeyCreateResponse:
        raw_key = f"rk_{secrets.token_urlsafe(32)}"
        key_hash = self._hash_key(raw_key)
        created_at = datetime.now(timezone.utc)
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO api_keys (key_hash, agent_id, label, daily_limit, revoked, created_at)
                VALUES (%s, %s, %s, %s, FALSE, %s)
                """,
                (key_hash, agent_id, label, daily_limit, created_at),
            )
            conn.commit()
        return ApiKeyCreateResponse(
            key_hash=key_hash,
            agent_id=agent_id,
            label=label,
            daily_limit=daily_limit,
            raw_key=raw_key,
            created_at=created_at,
        )

    def validate_api_key(self, raw_key: str) -> str | None:
        key_hash = self._hash_key(raw_key)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM api_keys WHERE key_hash = %s", (key_hash,))
            row = cur.fetchone()
            if row is None or row["revoked"]:
                return None
            daily_limit = row["daily_limit"]
            agent_id = row["agent_id"]
            if daily_limit > 0:
                cur.execute(
                    "SELECT count FROM api_key_usage WHERE key_hash = %s AND day = %s",
                    (key_hash, today),
                )
                usage_row = cur.fetchone()
                used = usage_row["count"] if usage_row else 0
                if used >= daily_limit:
                    return None
            return agent_id

    def revoke_api_key(self, key_hash: str) -> bool:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE api_keys SET revoked = TRUE WHERE key_hash = %s AND revoked = FALSE",
                (key_hash,),
            )
            conn.commit()
            return cur.rowcount > 0

    def list_api_keys(self) -> list[ApiKeyInfo]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM api_keys ORDER BY created_at ASC")
            rows = cur.fetchall()
        return [
            ApiKeyInfo(
                key_hash=r["key_hash"],
                agent_id=r["agent_id"],
                label=r["label"],
                daily_limit=r["daily_limit"],
                revoked=r["revoked"],
                created_at=r["created_at"],
            )
            for r in rows
        ]

    def record_api_key_usage(self, key_hash: str) -> None:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO api_key_usage (key_hash, day, count)
                VALUES (%s, %s, 1)
                ON CONFLICT (key_hash, day)
                DO UPDATE SET count = api_key_usage.count + 1
                """,
                (key_hash, today),
            )
            conn.commit()

    def get_api_key_usage_today(self, key_hash: str) -> int:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT count FROM api_key_usage WHERE key_hash = %s AND day = %s",
                (key_hash, today),
            )
            row = cur.fetchone()
            return row["count"] if row else 0

    def store_osf_oauth_token(self, agent_id: str, token_metadata: dict) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO osf_oauth_tokens (agent_id, token_metadata, updated_at)
                VALUES (%s, %s, %s)
                ON CONFLICT (agent_id)
                DO UPDATE SET token_metadata = EXCLUDED.token_metadata, updated_at = EXCLUDED.updated_at
                """,
                (agent_id, _encode_osf_token_metadata(token_metadata), datetime.now(timezone.utc)),
            )
            conn.commit()

    def get_osf_oauth_token(self, agent_id: str) -> dict | None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT token_metadata FROM osf_oauth_tokens WHERE agent_id = %s", (agent_id,))
            row = cur.fetchone()
        if row is None:
            return None
        return _decode_osf_token_metadata(row["token_metadata"])

    def _audit_from_row(self, row: dict) -> AuditReview:
        return AuditReview(
            submission_id=row["submission_id"],
            auditor_id=row["auditor_id"],
            auditor_verdict=row["auditor_verdict"],
            auditor_notes=row["auditor_notes"],
            system_verdict=row["system_verdict"],
            verdict_match=row["verdict_match"],
            confidence=row["confidence"],
            created_at=row["created_at"],
        )

    def create_audit_review(self, review: AuditReview) -> AuditReview:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO audit_reviews (submission_id, auditor_id, auditor_verdict, auditor_notes, system_verdict, verdict_match, confidence, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    review.submission_id,
                    review.auditor_id,
                    review.auditor_verdict,
                    review.auditor_notes,
                    review.system_verdict,
                    review.verdict_match,
                    review.confidence,
                    review.created_at,
                ),
            )
            conn.commit()
        return review

    def list_audit_reviews(self, submission_id: str | None = None) -> list[AuditReview]:
        with self._connect() as conn, conn.cursor() as cur:
            if submission_id:
                cur.execute("SELECT * FROM audit_reviews WHERE submission_id = %s ORDER BY created_at ASC", (submission_id,))
            else:
                cur.execute("SELECT * FROM audit_reviews ORDER BY created_at ASC")
            rows = cur.fetchall()
        return [self._audit_from_row(r) for r in rows]

    def audit_summary(self, submission_id: str | None = None) -> dict:
        with self._connect() as conn, conn.cursor() as cur:
            if submission_id:
                cur.execute("SELECT * FROM audit_reviews WHERE submission_id = %s", (submission_id,))
            else:
                cur.execute("SELECT * FROM audit_reviews")
            rows = cur.fetchall()
        reviews = [self._audit_from_row(r) for r in rows]
        total = len(reviews)
        if total == 0:
            return {"total_audits": 0, "agreement_rate": 0.0, "by_auditor": {}, "by_verdict": {}}
        agree = sum(1 for r in reviews if r.verdict_match == AuditVerdict.AGREE)
        by_auditor: dict[str, dict] = {}
        by_verdict: dict[str, int] = {}
        for r in reviews:
            by_auditor.setdefault(r.auditor_id, {"total": 0, "agree": 0})
            by_auditor[r.auditor_id]["total"] += 1
            if r.verdict_match == AuditVerdict.AGREE:
                by_auditor[r.auditor_id]["agree"] += 1
            v = r.auditor_verdict.value if r.auditor_verdict else "unknown"
            by_verdict[v] = by_verdict.get(v, 0) + 1
        return {
            "total_audits": total,
            "agreement_rate": round(agree / total, 4),
            "by_auditor": by_auditor,
            "by_verdict": by_verdict,
        }

    # --- Claim cards (postgres) ---

    def save_claim_card(self, card: ClaimCard) -> ClaimCard:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO claim_cards (
                    id, publication_id, claim_text, evidence_grade,
                    citation_support, contradiction_status, source_ids,
                    dw_chain_url, created_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                _claim_card_to_row(card),
            )
            conn.commit()
        return card

    def list_claim_cards(self, publication_id: str) -> list[ClaimCard]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM claim_cards WHERE publication_id = %s ORDER BY created_at ASC",
                (publication_id,),
            )
            rows = cur.fetchall()
        cards = [_claim_card_from_row(row) for row in rows]
        return [card for card in cards if card is not None]
