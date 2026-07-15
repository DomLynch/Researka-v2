from __future__ import annotations

import json
import os
import subprocess
import hashlib
import hmac
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import BackgroundTasks, Body, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from pydantic import BaseModel, Field

from apps.runtime_api import rate_limits
from apps.worker.main import WorkerApp
from contracts import AuditReview, AuditVerdict, ClaimCard, Decision, EventType, ObjectType, ResearchObject, RuntimeJob, Stage, SubmissionPayload
from runtime_core import InMemoryRuntimeRepository, PostgresRuntimeRepository, WorkflowEngine
from runtime_core.agent_query import fail_agent_query_job, run_agent_query_job
from runtime_core.evidence_quality import classified_title, contradiction_status_for_text, evidence_profile
from runtime_core.osf import (
    backfill_missing_publication_dois,
    build_oauth_authorization_url,
    exchange_oauth_code,
    oauth_config_from_env,
    osf_user_metadata_from_token,
    sign_oauth_state,
    verify_oauth_state,
    warn_if_osf_default_owner_missing,
)
from runtime_core.repos import RuntimeRepository, postgres_dsn_from_env
from runtime_core.publication_sidecars import build_sidecar, sidecar_manifest

_calibration_cache: dict | None = None
_calibration_path: str | None = None

# Captured at module import for the /version endpoint.
_SERVICE_STARTED_AT = datetime.now(timezone.utc).isoformat()


def _resolve_git_sha() -> str:
    """Best-effort SHA resolution for the /version endpoint.

    Order:
    1. Subprocess `git rev-parse HEAD` from package root
    2. RESEARKA_GIT_SHA env var (packaged deployments without Git)
    3. /etc/researka/git_sha file (packaged deployments without Git)
    4. "unknown"
    """
    try:
        repo_root = Path(__file__).resolve().parents[2]
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    env_sha = os.environ.get("RESEARKA_GIT_SHA", "").strip()
    if env_sha:
        return env_sha
    sha_file = Path("/etc/researka/git_sha")
    if sha_file.exists():
        try:
            return sha_file.read_text().strip() or "unknown"
        except OSError:
            pass
    return "unknown"


_SERVICE_GIT_SHA = _resolve_git_sha()
DEFAULT_AGENT_DAILY_LIMIT = 10
DEFAULT_INTAKE_REJECTION_BACKOFF = 3
# Hours after which an intake rejection stops arming the submission backoff.
# Blocked submits are never recorded, so without decay a 3-strike cluster
# locks an agent out indefinitely; the window bounds the blackout instead.
DEFAULT_INTAKE_REJECTION_BACKOFF_WINDOW_HOURS = 6
_AGENT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,62}$")


class AgentQueryPayload(BaseModel):
    query: str = Field(min_length=3, max_length=240)
    depth: str = "standard"
    contactEmail: str | None = Field(default=None, max_length=320)


def reset_calibration_cache() -> None:
    global _calibration_cache, _calibration_path
    _calibration_cache = None
    _calibration_path = None


def _normalize_benchmark(raw: dict) -> dict:
    if "summary" in raw:
        return raw
    aggregates = raw.get("aggregates", {})
    papers = raw.get("papers", [])
    mismatches = []
    gate_failures: dict[str, list] = {}
    confusion_matrix = aggregates.get("confusion_matrix", {})
    correct = aggregates.get("correct")
    accuracy = aggregates.get("accuracy")
    quality_expectations = {
        "high": "accept",
        "medium": "revise",
        "low": "reject",
        "broken": "reject",
    }
    for p in papers:
        if p.get("error"):
            stage = p.get("stage_reached", "unknown")
            gate_failures.setdefault(stage, []).append(p["paper_id"])
        quality = p.get("quality", "unknown")
        expected = p.get("expected_decision") or quality_expectations.get(quality, "revise")
        actual = p.get("decision")
        if not actual:
            actual = "reject" if p.get("outcome") == "intake_rejected" else p.get("outcome")
        if actual and actual != expected:
            mismatches.append(
                {
                    "paper_id": p["paper_id"],
                    "quality": quality,
                    "domain": p.get("domain", "unknown"),
                    "expected": expected,
                    "actual": actual,
                }
            )
    if correct is None:
        correct = len(papers) - len(mismatches)
    if accuracy is None:
        accuracy = round(correct / len(papers), 3) if papers else 0.0
    public_aggregates = {key: value for key, value in aggregates.items() if key != "mismatches"}
    return {
        "summary": {
            "overall": {
                **public_aggregates,
                "total": len(papers),
                "correct": correct,
                "accuracy": accuracy,
            },
            "by_category": aggregates.get("by_quality", {}),
            "gate_failures": gate_failures,
            "confusion_matrix": confusion_matrix,
            "mismatches": mismatches,
        },
        "results": papers,
    }


def _load_calibration_data() -> dict:
    global _calibration_cache, _calibration_path
    default_path = os.environ.get(
        "RESEARKA_V2_CALIBRATION_PATH",
        str(Path(__file__).resolve().parents[2] / "artifacts" / "benchmark_style_v7.json"),
    )
    if _calibration_cache is not None and _calibration_path == default_path:
        _calibration_cache["receipt"] = _refresh_calibration_receipt(_calibration_cache["receipt"])
        return _calibration_cache
    path = Path(default_path)
    if not path.exists():
        return {"summary": {}, "results": [], "receipt": {"status": "missing", "valid": False}}
    raw = json.loads(path.read_text())
    _calibration_cache = _normalize_benchmark(raw)
    run_meta = raw.get("run_meta", {}) if isinstance(raw, dict) else {}
    generated_at = str(run_meta.get("repaired_at") or run_meta.get("timestamp") or "")
    results = _calibration_cache.get("results", [])
    case_count = len(results) if isinstance(results, list) else 0
    _calibration_cache["receipt"] = _refresh_calibration_receipt({
        "artifact": path.name,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "provider": run_meta.get("provider"),
        "generated_at": generated_at or None,
        "case_count": case_count,
    })
    _calibration_path = default_path
    return _calibration_cache


def _refresh_calibration_receipt(receipt: dict) -> dict:
    generated_at = str(receipt.get("generated_at") or "")
    try:
        generated = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
        if generated.tzinfo is None:
            generated = generated.replace(tzinfo=timezone.utc)
        age_days = max(0, (datetime.now(timezone.utc) - generated).days)
    except (TypeError, ValueError):
        age_days = None
    case_count = int(receipt.get("case_count") or 0)
    max_age_days = _bounded_env_int("RESEARKA_V2_CALIBRATION_MAX_AGE_DAYS", 90, floor=1, ceiling=365)
    minimum_cases = _bounded_env_int("RESEARKA_V2_CALIBRATION_MIN_CASES", 100, floor=1, ceiling=10000)
    fresh = age_days is not None and age_days <= max_age_days
    return {
        **receipt,
        "status": "current" if fresh and case_count >= minimum_cases else "stale_or_insufficient",
        "valid": fresh and case_count >= minimum_cases,
        "age_days": age_days,
        "max_age_days": max_age_days,
        "case_count": case_count,
        "minimum_cases": minimum_cases,
    }


def _check_api_key(repo: RuntimeRepository, request: Request) -> str | None:
    """Validate API key. Returns agent_id if valid, raises 403 if not.

    Check order:
    1. Legacy env-var key (RESEARKA_V2_API_KEY) — exact match
    2. Per-agent key via repo — hash match, daily limit check
    """
    provided = request.headers.get("x-api-key", "")
    if not provided:
        raise HTTPException(status_code=403, detail="missing_api_key")

    # Legacy env-var key
    legacy_key = os.environ.get("RESEARKA_V2_API_KEY")
    if legacy_key and provided == legacy_key:
        return None  # legacy key, no agent_id

    # Per-agent key
    key_hash = hashlib.sha256(provided.encode()).hexdigest()
    key_info = next((key for key in repo.list_api_keys() if key.key_hash == key_hash), None)
    if key_info is not None and not key_info.revoked and key_info.daily_limit > 0:
        used = repo.get_api_key_usage_today(key_hash)
        if used >= key_info.daily_limit:
            raise HTTPException(status_code=429, detail="daily_limit_exceeded")

    agent_id = repo.validate_api_key(provided)
    if agent_id is not None:
        repo.record_api_key_usage(key_hash)
        return agent_id

    raise HTTPException(status_code=403, detail="invalid_api_key")


def _check_admin(request: Request) -> None:
    """Require admin key for privileged operator endpoints."""
    provided = request.headers.get("x-api-key", "")
    admin_key = os.environ.get("RESEARKA_V2_ADMIN_KEY")
    if admin_key and hmac.compare_digest(provided, admin_key):
        return
    raise HTTPException(status_code=403, detail="admin_key_required")


def _env_int(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(name, str(default))))
    except ValueError:
        return default


def _bounded_env_int(name: str, default: int, *, floor: int, ceiling: int) -> int:
    return min(ceiling, max(floor, _env_int(name, default)))


def _bounded_env_float(name: str, default: float, *, floor: float, ceiling: float) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except ValueError:
        value = default
    return max(floor, min(value, ceiling))


def _feature_enabled(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).lower() not in {"0", "false", "no", "off"}


def _registration_client_key(request: Request) -> str | None:
    host = request.client.host if request.client else "unknown"
    if host in {"127.0.0.1", "::1", "testclient"}:
        forwarded = [part.strip() for part in request.headers.get("x-forwarded-for", "").split(",") if part.strip()]
        if forwarded:
            host = forwarded[-1]
    if host in {"127.0.0.1", "::1", "testclient"}:
        return None
    return hashlib.sha256(host[:128].encode()).hexdigest()


def _registration_window() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _check_public_registration(request: Request) -> None:
    if os.environ.get("RESEARKA_V2_PUBLIC_REGISTRATION_ENABLED", "1").lower() in {"0", "false", "no"}:
        raise HTTPException(status_code=403, detail="public_registration_disabled")
    if not rate_limits.public_registration_enabled():
        raise HTTPException(status_code=503, detail="registration_paused")

    window = _registration_window()
    client_key = _registration_client_key(request)
    if client_key is not None:
        ip_limit = _bounded_env_int("RESEARKA_V2_PUBLIC_REGISTRATIONS_PER_IP_PER_DAY", 3, floor=1, ceiling=10_000)
        if not rate_limits.check_and_incr("ip", client_key, limit=ip_limit, window=window):
            raise HTTPException(status_code=429, detail="registration_rate_limited")
    global_limit = _bounded_env_int("RESEARKA_V2_PUBLIC_REGISTRATIONS_PER_DAY", 200, floor=1, ceiling=200_000)
    if not rate_limits.check_and_incr("global", "registration", limit=global_limit, window=window):
        raise HTTPException(status_code=429, detail="registration_rate_limited")


def _check_agent_registration(agent_id: str) -> None:
    agent_key = hashlib.sha256(agent_id.encode()).hexdigest()
    if not rate_limits.check_and_incr("agent_id", agent_key, limit=1, window=_registration_window()):
        raise HTTPException(status_code=429, detail="registration_rate_limited")


def _clean_query(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()[:240]


def _check_agent_query(request: Request) -> dict[str, str]:
    if not _feature_enabled("RESEARKA_V2_AGENT_QUERY_ENABLED"):
        raise HTTPException(status_code=503, detail="agent_query_disabled")
    day = _registration_window()
    client_key = _registration_client_key(request) or hashlib.sha256(b"global").hexdigest()
    limit = _bounded_env_int("RESEARKA_V2_AGENT_QUERY_PER_IP_PER_DAY", 25, floor=1, ceiling=10_000)
    if not rate_limits.check_and_incr("agent_query_ip", client_key, limit=limit, window=day):
        raise HTTPException(status_code=429, detail="agent_query_rate_limited")
    return {"request_day": day, "client_bucket_hash": client_key}


def _query_caps(depth: str) -> dict:
    standard = depth == "standard"
    return {
        "max_runtime_sec": _bounded_env_int("RESEARKA_V2_AGENT_QUERY_MAX_RUNTIME_SEC", 180 if standard else 60, floor=5, ceiling=3600),
        "max_sources": _bounded_env_int("RESEARKA_V2_AGENT_QUERY_MAX_SOURCES", 24 if standard else 8, floor=1, ceiling=100),
        "max_cost_usd": _bounded_env_float("RESEARKA_V2_AGENT_QUERY_MAX_COST_USD", 0.50 if standard else 0.10, floor=0.0, ceiling=100.0),
    }


def _agent_query_response(obj: ResearchObject, *, repo: RuntimeRepository) -> dict:
    metadata = obj.metadata
    queued = [item for item in repo.list_objects(ObjectType.AGENT_QUERY) if item.metadata.get("status") == "queued"]
    return {
        "jobId": obj.id,
        "status": metadata.get("status", "queued"),
        "query": metadata.get("query", obj.title),
        "depth": metadata.get("depth", "standard"),
        "createdAt": obj.created_at.isoformat(),
        "updatedAt": metadata.get("updated_at", obj.created_at.isoformat()),
        "position": next((index + 1 for index, item in enumerate(queued) if item.id == obj.id), None),
        "errorMessage": metadata.get("error_message"),
        "result": metadata.get("result"),
        "caps": metadata.get("caps", {}),
    }


def _run_agent_query_background(repo: RuntimeRepository, job_id: str) -> None:
    try:
        run_agent_query_job(repo, job_id)
    except Exception as exc:
        fail_agent_query_job(repo, job_id, str(exc))


def _daily_limit_from_body(body: dict) -> int:
    if "daily_limit" not in body:
        return _env_int("RESEARKA_V2_DEFAULT_DAILY_LIMIT", DEFAULT_AGENT_DAILY_LIMIT)
    try:
        return max(0, int(body.get("daily_limit") or 0))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="daily_limit_invalid")


def _submission_content_hash(payload: SubmissionPayload) -> str:
    data = payload.model_dump(
        mode="json",
        exclude={"submitted_at", "parent_submission_id", "author_signature", "author_agent_id", "agent_id"},
    )
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


def _matching_agent(metadata: dict, agent_id: str) -> bool:
    return agent_id in {
        str(metadata.get("authenticated_agent_id") or ""),
        str(metadata.get("author_agent_id") or ""),
        str(metadata.get("agent_id") or ""),
    }


def _duplicate_submission_id(repo: RuntimeRepository, *, content_hash: str) -> str | None:
    for obj in reversed(repo.list_objects(ObjectType.SUBMISSION)):
        if obj.metadata.get("submission_content_hash") != content_hash:
            continue
        decisions = repo.children_of(obj.id, ObjectType.DECISION)
        latest = decisions[-1] if decisions else None
        if latest and latest.metadata.get("decision") == Decision.REJECT.value:
            continue
        return obj.id
    return None


SUBMISSION_AUDIT_METADATA_KEYS = {
    "artifact_type",
    "content_hash",
    "counts",
    "revision_feedback",
    "revision_of",
    "run_id",
    "source_citation_hash",
    "submission_identity_key",
    "submission_payload_hash",
    "topic",
}


def _trusted_submission_metadata(metadata: dict) -> dict:
    return {key: metadata[key] for key in SUBMISSION_AUDIT_METADATA_KEYS if metadata.get(key) not in (None, "")}


def _is_intake_rejection(decision: ResearchObject) -> bool:
    if decision.metadata.get("decision") != Decision.REJECT.value:
        return False
    notes = decision.metadata.get("notes", [])
    if isinstance(notes, list) and "intake gate rejection" in notes:
        return True
    return bool(decision.metadata.get("gate_failures")) and not decision.metadata.get("review_id")


def _failure_stage(decision: ResearchObject, review: ResearchObject | None) -> str:
    if _is_intake_rejection(decision):
        return "intake_gate"
    if decision.metadata.get("failure_category") == "integrity_duplicate":
        return "integrity_check"
    if review is not None:
        return "reviewer_panel"
    return "editorial"


def _consecutive_intake_rejections(repo: RuntimeRepository, *, agent_id: str) -> int:
    window_hours = _env_int(
        "RESEARKA_V2_INTAKE_REJECTION_BACKOFF_WINDOW_HOURS",
        DEFAULT_INTAKE_REJECTION_BACKOFF_WINDOW_HOURS,
    )
    cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours) if window_hours > 0 else None
    count = 0
    submissions = [
        obj
        for obj in repo.list_objects(ObjectType.SUBMISSION)
        if _matching_agent(obj.metadata, agent_id)
    ]
    submissions.sort(key=lambda obj: obj.created_at, reverse=True)
    for submission in submissions:
        decisions = repo.children_of(submission.id, ObjectType.DECISION)
        if not decisions:
            continue
        if cutoff is not None:
            created = submission.created_at
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            if created < cutoff:
                # Time-decay: rejections older than the window no longer arm
                # the backoff. Blocked submits are never recorded, so without
                # this the streak could never break on its own and a reject
                # cluster would black out the agent indefinitely.
                break
        latest = decisions[-1]
        if _is_intake_rejection(latest):
            count += 1
            continue
        break
    return count


def _osf_oauth_config_or_error():
    config = oauth_config_from_env()
    if config is None:
        raise HTTPException(status_code=500, detail="osf_oauth_not_configured")
    return config


def _submission_metadata_for_agent(payload: SubmissionPayload, agent_id: str | None) -> dict:
    metadata = payload.model_dump(mode="json", exclude={"metadata"})
    metadata.update(_trusted_submission_metadata(payload.metadata))
    metadata["domain_slug"] = payload.domain_slug
    metadata["category"] = str(metadata.get("category") or payload.domain_slug).removesuffix("_research")
    metadata["topic"] = payload.topic or metadata.get("topic")
    if not agent_id:
        return metadata
    claimed_agent_id = metadata.get("author_agent_id")
    if claimed_agent_id != agent_id:
        metadata["claimed_author_agent_id"] = claimed_agent_id
    metadata["author_agent_id"] = agent_id
    metadata["authenticated_agent_id"] = agent_id
    metadata["identity_source"] = "api_key"
    return metadata


def _is_hidden_public_record(obj: ResearchObject | None) -> bool:
    if obj is None:
        return False
    # "provisional" = published by a not-yet-trusted agent: quarantined from
    # every public surface (listings AND direct fetch) until promoted via /ops.
    return str(obj.metadata.get("public_visibility") or "listed").strip().lower() in {"hidden", "provisional"}


def _is_publicly_listed(publication: ResearchObject) -> bool:
    if _is_hidden_public_record(publication) or publication.metadata.get("superseded_by"):
        return False
    return True


def _public_integrity_signal(value: object) -> dict | None:
    if not isinstance(value, dict):
        return None
    payload = dict(value)
    reason = str(payload.get("reason") or "").lower()
    unavailable = payload.get("available") is False or "integrity_unavailable" in reason or "timed out" in reason
    if unavailable:
        payload["available"] = False
        payload["recommendation"] = "unavailable"
        payload["status"] = "unavailable"
    else:
        payload["status"] = "checked" if payload.get("recommendation") else "unknown"
    return payload


def _support_row_is_exact(row: dict) -> bool:
    if row.get("support_kind") in {"direct_doi_match", "bundle_reference"}:
        return True
    if row.get("quote") or row.get("evidence_span") or row.get("dw_chain_ref"):
        return True
    return False


def _claims_are_scoping_only(cards: list[ClaimCard]) -> bool:
    if not cards:
        return False
    weak_statuses = {"mixed", "contested", "insufficient", "non_supportive", "contradicted"}
    exploratory = all(str(card.evidence_grade) == "exploratory" for card in cards)
    weak = sum(1 for card in cards if str(card.contradiction_status) in weak_statuses)
    exact = sum(1 for card in cards for row in card.citation_support if isinstance(row, dict) and _support_row_is_exact(row))
    return exploratory and weak >= max(1, len(cards) // 2) and exact == 0


def _public_publication_class(repo: RuntimeRepository, publication: ResearchObject) -> str | None:
    stored = publication.metadata.get("publication_class")
    pub_class = str(stored or "").strip() or None
    article_type = str(publication.metadata.get("article_type") or "").strip()
    if article_type == "evidence_map":
        return "evidence_map"
    if pub_class == "research_synthesis":
        cards = _publication_claim_cards(repo, publication)
        if not _claims_are_scoping_only(cards):
            return pub_class
        raw_profile = publication.metadata.get("evidence_profile")
        profile = raw_profile if isinstance(raw_profile, dict) else {}
        if profile.get("indirect_signal") or float(profile.get("weak_evidence_ratio") or 0) >= 0.6:
            return "adjacent_evidence_brief"
        return "evidence_map"
    return pub_class


def _publication_response(repo: RuntimeRepository, publication: ResearchObject) -> dict:
    payload = publication.model_dump(mode="json")
    pub_class = _public_publication_class(repo, publication)
    if pub_class:
        payload["title"] = classified_title(publication.title, pub_class)
    payload["artifact_type"] = _artifact_type_for_submission(publication)
    payload["surface"] = _publication_surface(publication)
    payload["publication_class"] = pub_class
    payload["evidence_profile"] = publication.metadata.get("evidence_profile")
    payload["integrity"] = _public_integrity_signal(publication.metadata.get("integrity"))
    payload["doi"] = publication.metadata.get("doi")
    payload["doi_status"] = publication.metadata.get("doi_status")
    payload["osf_url"] = publication.metadata.get("osf_url")
    return payload


def _publication_submission(repo: RuntimeRepository, publication: ResearchObject) -> ResearchObject | None:
    submission = repo.get_object(publication.parent_object_id) if publication.parent_object_id else None
    if submission is None or submission.object_type != ObjectType.SUBMISSION:
        return None
    return submission


def _stable_claim_id(publication_id: str, index: int, text: str) -> str:
    digest = hashlib.sha256(f"{publication_id}:{index}:{text}".encode("utf-8")).hexdigest()[:16]
    return f"claim_{digest}"


def _derived_claim_cards(publication: ResearchObject, submission: ResearchObject | None) -> list[ClaimCard]:
    graph, _, _ = build_sidecar(publication, submission, "claim_graph.json")
    traces, _, _ = build_sidecar(publication, submission, "citation_traces.json")
    trace_by_claim = {
        str(trace.get("claim_id")): trace.get("citation_support") or []
        for trace in traces.get("traces", [])
        if isinstance(trace, dict)
    } if isinstance(traces, dict) else {}
    nodes = graph.get("nodes", []) if isinstance(graph, dict) else []
    raw_bundle = (submission.metadata if submission else {}).get("source_bundle", [])
    source_bundle = raw_bundle if isinstance(raw_bundle, list) else []
    profile = evidence_profile(
        text=f"{publication.title}\n{publication.metadata.get('abstract') or ''}\n{publication.body_markdown or ''}",
        source_bundle=[item for item in source_bundle if isinstance(item, dict)],
    )
    cards: list[ClaimCard] = []
    for index, node in enumerate((n for n in nodes if isinstance(n, dict) and n.get("type") == "claim"), start=1):
        text = str(node.get("text") or "").strip()
        if not text:
            continue
        support = [dict(source) for source in trace_by_claim.get(str(node.get("id")), [])[:5] if isinstance(source, dict)]
        cards.append(
            ClaimCard(
                id=_stable_claim_id(publication.id, index, text),
                publication_id=publication.id,
                claim_text=text,
                citation_support=support,
                contradiction_status=contradiction_status_for_text(text, profile),
                source_ids=[str(item["source_id"]) for item in support],
                dw_chain_url=publication.metadata.get("dw_chain_url") or publication.metadata.get("dw_chain"),
            )
        )
    return cards


def _publication_claim_cards(repo: RuntimeRepository, publication: ResearchObject) -> list[ClaimCard]:
    saved = repo.list_claim_cards(publication.id)
    if saved:
        return saved
    return _derived_claim_cards(publication, _publication_submission(repo, publication))


def _publication_passport(repo: RuntimeRepository, publication: ResearchObject) -> dict:
    metadata = publication.metadata
    submission = _publication_submission(repo, publication)
    submission_metadata = submission.metadata if submission else {}
    decisions = repo.children_of(submission.id, ObjectType.DECISION) if submission else []
    latest_decision = decisions[-1] if decisions else None
    content_hash = metadata.get("content_hash") or metadata.get("sha256") or f"sha256:{hashlib.sha256((publication.body_markdown or '').encode('utf-8')).hexdigest()}"
    ror_id = metadata.get("institution_ror") or metadata.get("ror_id") or submission_metadata.get("institution_ror") or submission_metadata.get("ror_id")
    institution_name = metadata.get("institution_name") or submission_metadata.get("institution_name")
    identifiers = {
        "doi": metadata.get("doi") or metadata.get("osf_doi"),
        "osf_url": metadata.get("osf_url"),
        "orcid": metadata.get("orcid") or metadata.get("submitter_orcid") or metadata.get("author_orcid"),
        "ror_id": ror_id,
        "raid_id": metadata.get("raid_id") or submission_metadata.get("raid_id"),
    }
    return {
        "publication_id": publication.id,
        "submission_id": publication.parent_object_id,
        "artifact_type": _artifact_type_for_submission(submission),
        "decision": (latest_decision.metadata.get("decision") if latest_decision else Decision.ACCEPT.value),
        "content_hash": content_hash,
        "persistent_identifiers": identifiers,
        "persistent_identifier_status": {key: "supplied" if value else "not_supplied" for key, value in identifiers.items()},
        "institution": {
            "name": institution_name,
            "ror_id": ror_id,
            "status": "supplied" if institution_name or ror_id else "not_supplied",
        },
        "integrity": _public_integrity_signal(metadata.get("integrity")),
        "provenance": {
            "dw_artifact_id": metadata.get("dw_artifact_id"),
            "dw_chain_url": metadata.get("dw_chain_url"),
        },
        "timeline": [
            Stage.INTAKE.value,
            Stage.REVIEW.value,
            Stage.EDITORIAL.value,
            Stage.PUBLISH.value,
        ],
    }


def _badge_definitions() -> list[dict[str, str]]:
    return [
        {"id": "exploratory", "label": "Exploratory", "meaning": "Claim is public but still early or indirectly supported."},
        {"id": "verified", "label": "Verified", "meaning": "Claim has direct citation support and no known contradiction."},
        {"id": "certified", "label": "Certified", "meaning": "Claim has strong support plus provenance and reviewer confidence."},
        {"id": "contested", "label": "Contested", "meaning": "Claim has material conflicting evidence or reviewer concern."},
        {"id": "rejected", "label": "Rejected", "meaning": "Claim or artifact failed Researka gatekeeping."},
    ]


def _normalise_sha(value: str) -> str:
    text = value.strip().lower()
    if text.startswith("sha256:"):
        return text
    if re.fullmatch(r"[0-9a-f]{64}", text):
        return f"sha256:{text}"
    return text


def _agent_rows(repo: RuntimeRepository) -> list[dict[str, int | str | float]]:
    stats: dict[str, dict[str, int | str | float]] = {}
    decisions_by_parent: dict[str, list[ResearchObject]] = {}
    for decision in repo.list_objects(ObjectType.DECISION):
        if decision.parent_object_id and not _is_hidden_public_record(decision):
            decisions_by_parent.setdefault(decision.parent_object_id, []).append(decision)
    published_targets = {
        publication.parent_object_id
        for publication in repo.list_objects(ObjectType.PUBLICATION)
        if publication.parent_object_id and _is_publicly_listed(publication)
    }
    for submission in repo.list_objects(ObjectType.SUBMISSION):
        if _is_hidden_public_record(submission):
            continue
        agent_id = str(submission.metadata.get("author_agent_id") or submission.metadata.get("agent_id") or "unknown")
        row = stats.setdefault(agent_id, {"agent_id": agent_id, "submissions": 0, "accept": 0, "revise": 0, "reject": 0})
        row["submissions"] = int(row["submissions"]) + 1
        decisions = decisions_by_parent.get(submission.id, [])
        if decisions:
            decision_value = str(decisions[-1].metadata.get("decision") or "")
        elif submission.id in published_targets:
            decision_value = Decision.ACCEPT.value
        else:
            decision_value = ""
        if decision_value in {Decision.ACCEPT.value, Decision.REVISE.value, Decision.REJECT.value}:
            row[decision_value] = int(row[decision_value]) + 1
    rows = []
    for row in stats.values():
        decided = int(row["accept"]) + int(row["revise"]) + int(row["reject"])
        row["accept_rate"] = round(int(row["accept"]) / decided, 4) if decided else 0.0
        rows.append(row)
    return sorted(rows, key=lambda item: (int(item["accept"]), float(item["accept_rate"]), str(item["agent_id"])), reverse=True)


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip():
            out.append(item.strip())
        elif isinstance(item, dict):
            text = item.get("reason") or item.get("message") or item.get("name") or item.get("check")
            if isinstance(text, str) and text.strip():
                out.append(text.strip())
    return out


def _score_dict(value: object) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    scores: dict[str, int] = {}
    for key, raw in value.items():
        try:
            score = int(raw)
        except (TypeError, ValueError):
            continue
        if 1 <= score <= 5:
            scores[str(key)] = score
    return scores


def _model_list(value: object) -> list[str]:
    if not isinstance(value, str):
        return []
    return [item.strip() for item in value.replace(",", "|").split("|") if item.strip()]


def _decision_derivation_map(repo: RuntimeRepository) -> dict[str, dict]:
    derivations: dict[str, dict] = {}
    for event in repo.list_events():
        decision_id = event.payload.get("created_object_id")
        derivation = event.payload.get("derivation_web")
        if isinstance(decision_id, str) and isinstance(derivation, dict):
            derivations[decision_id] = derivation
    return derivations


def _artifact_type_for_submission(submission: ResearchObject | None) -> str:
    article_type = str((submission.metadata if submission else {}).get("article_type") or "")
    artifact_type = str((submission.metadata if submission else {}).get("artifact_type") or "")
    marker = f"{article_type} {artifact_type}".lower()
    return "alpha_memo" if "alpha_memo" in marker else "research_paper"


def _publication_surface(publication: ResearchObject) -> str:
    return "alpha" if _artifact_type_for_submission(publication) == "alpha_memo" else "papers"


def _public_decision_record(
    *,
    decision: ResearchObject,
    submission: ResearchObject | None,
    review: ResearchObject | None,
    derivation: dict | None = None,
) -> dict:
    decision_metadata = decision.metadata
    submission_metadata = submission.metadata if submission else {}
    review_metadata = review.metadata if review else {}
    gate_failures = decision_metadata.get("gate_failures", [])
    failed_checks = _string_list(gate_failures) or _string_list(decision_metadata.get("failed_checks"))
    decision_value = str(decision_metadata.get("decision") or "").strip().lower()
    required_revisions = list(dict.fromkeys([
        *_string_list(decision_metadata.get("required_revisions")),
        *_string_list(review_metadata.get("required_revisions")),
    ]))
    major_issues = _string_list(review_metadata.get("major_issues"))
    minor_issues = _string_list(review_metadata.get("minor_issues"))
    domain_slug = submission_metadata.get("domain_slug") or "general"
    topic = submission_metadata.get("topic") or domain_slug or "research"
    category = submission_metadata.get("category") or str(domain_slug).removesuffix("_research")
    agent_id = (
        submission_metadata.get("authenticated_agent_id")
        or submission_metadata.get("author_agent_id")
        or submission_metadata.get("agent_id")
        or "unknown-agent"
    )
    dw_artifact_id = (derivation or {}).get("decision_artifact_id") or decision_metadata.get("dw_artifact_id")
    review_markdown = (review.body_markdown if review else "").strip()
    summary_parts = required_revisions or major_issues or failed_checks or ([review_markdown] if review_markdown else [])
    review_summary = "; ".join(summary_parts) or f"Researka gate decision: {decision_value}."
    return {
        "id": decision.id,
        "artifact_id": decision.id,
        "submission_id": decision.parent_object_id,
        "parent_object_id": decision.parent_object_id,
        "artifact_type": _artifact_type_for_submission(submission),
        "title": submission.title if submission else decision.title,
        "topic": topic,
        "domain_slug": domain_slug,
        "category": category,
        "author_name": submission_metadata.get("author_name") or submission_metadata.get("human_owner_name"),
        "orcid": submission_metadata.get("orcid") or submission_metadata.get("submitter_orcid") or submission_metadata.get("author_orcid"),
        "agent_id": agent_id,
        "author_agent_id": agent_id,
        "decision": decision_value,
        "failure_stage": _failure_stage(decision, review),
        "failure_category": next((item.get("name") for item in gate_failures if isinstance(item, dict) and item.get("name")), None)
        or decision_metadata.get("failure_category"),
        "failed_checks": failed_checks,
        "gate_failures": gate_failures if isinstance(gate_failures, list) else [],
        "integrity": decision_metadata.get("integrity") if isinstance(decision_metadata.get("integrity"), dict) else None,
        "rubric_scores": _score_dict(review_metadata.get("rubric_scores")),
        "required_revisions": required_revisions,
        "major_issues": major_issues,
        "minor_issues": minor_issues,
        "claim_support_verdict": review_metadata.get("claim_support_verdict"),
        "overclaim_verdict": review_metadata.get("overclaim_verdict"),
        "synthesis_quality_verdict": review_metadata.get("synthesis_quality_verdict"),
        "review_markdown": review_markdown[:4000],
        "panel_route": review_metadata.get("route"),
        "models": _model_list(review_metadata.get("model")),
        "fallback_used": {
            "primary": bool(review_metadata.get("primary_fallback_used")),
            "sparring": bool(review_metadata.get("sparring_fallback_used")),
        },
        "prompt_version": review_metadata.get("prompt_version"),
        "review_id": review.id if review else decision_metadata.get("review_id"),
        "review_summary": review_summary[:1000],
        "public_visible": True,
        "public_full_text": False,
        "full_text": "",
        "body_markdown": "",
        "dw_artifact_id": dw_artifact_id,
        "dw_chain_url": f"https://provenance.researka.org/artifacts/{dw_artifact_id}/chain" if dw_artifact_id else None,
        "actor_id": decision_metadata.get("actor_id") or review_metadata.get("actor_id") or "reviewer-panel",
        "review_provider": decision_metadata.get("provider") or review_metadata.get("provider") or "reviewer-panel",
        "reviewed_at": decision.created_at.isoformat(),
        "created_at": decision.created_at.isoformat(),
        "timeline": [
            Stage.INTAKE.value,
            *([Stage.REVIEW.value] if review else []),
            Stage.EDITORIAL.value,
        ],
    }


def _publication_feedback(publication: ResearchObject | None, *, deduped: bool = False) -> dict | None:
    if publication is None:
        return None
    metadata = publication.metadata
    artifact_type = _artifact_type_for_submission(publication)
    public_path = "alpha" if artifact_type == "alpha_memo" else "papers"
    return {
        "publication_id": publication.id,
        "url": f"https://researka.org/{public_path}/{publication.id}",
        "deduped": deduped,
        "doi": metadata.get("doi") or metadata.get("osf_doi"),
        "doi_status": metadata.get("doi_status"),
        "osf_url": metadata.get("osf_url"),
        "dw_artifact_id": metadata.get("dw_artifact_id"),
        "dw_chain_url": metadata.get("dw_chain_url"),
    }


def _decision_publication_feedback(repo: RuntimeRepository, submission_id: str) -> dict | None:
    direct = repo.publication_for_target(submission_id)
    if direct is not None:
        return _publication_feedback(direct)
    for event in reversed(repo.list_events()):
        if event.target_object_id != submission_id or event.event_type != EventType.JOB_COMPLETED:
            continue
        if event.payload.get("stage") != Stage.PUBLISH.value or not event.payload.get("deduped"):
            continue
        publication_id = event.payload.get("publication_id")
        publication = repo.get_object(str(publication_id)) if publication_id else None
        if publication is not None and publication.object_type == ObjectType.PUBLICATION:
            return _publication_feedback(publication, deduped=True)
    return None


def _submission_decision_response(
    *,
    repo: RuntimeRepository,
    submission_id: str,
    decision: ResearchObject,
) -> dict:
    submission = repo.get_object(submission_id)
    review_id = decision.metadata.get("review_id")
    review = repo.get_object(str(review_id)) if review_id else None
    public_record = _public_decision_record(
        decision=decision,
        submission=submission if submission and submission.object_type == ObjectType.SUBMISSION else None,
        review=review if review and review.object_type == ObjectType.REVIEW else None,
        derivation=_decision_derivation_map(repo).get(decision.id),
    )
    decision_value = public_record["decision"]
    response = {
        "status": "complete",
        "decision": decision_value,
        "notes": decision.metadata.get("notes", []),
        "gate_failures": public_record["gate_failures"],
        "decision_object_id": decision.id,
        "review_id": public_record["review_id"],
        "failure_stage": public_record["failure_stage"],
        "failure_category": public_record["failure_category"],
        "failed_checks": public_record["failed_checks"],
        "integrity": public_record["integrity"],
        "review_summary": public_record["review_summary"],
        "rubric_scores": public_record["rubric_scores"],
        "required_revisions": public_record["required_revisions"],
        "major_issues": public_record["major_issues"],
        "minor_issues": public_record["minor_issues"],
        "claim_support_verdict": public_record["claim_support_verdict"],
        "overclaim_verdict": public_record["overclaim_verdict"],
        "synthesis_quality_verdict": public_record["synthesis_quality_verdict"],
        "panel_route": public_record["panel_route"],
        "models": public_record["models"],
        "fallback_used": public_record["fallback_used"],
        "prompt_version": public_record["prompt_version"],
        "dw_artifact_id": public_record["dw_artifact_id"],
        "dw_chain_url": public_record["dw_chain_url"],
        "resubmission": {
            "allowed": decision_value in {Decision.REVISE.value, Decision.REJECT.value},
            "parent_submission_id": submission_id if decision_value in {Decision.REVISE.value, Decision.REJECT.value} else None,
        },
        "publication": _decision_publication_feedback(repo, submission_id),
    }
    return response


def create_app(repository: RuntimeRepository | None = None) -> FastAPI:
    warn_if_osf_default_owner_missing()
    if repository is not None:
        repo = repository
    elif dsn := postgres_dsn_from_env():
        repo = PostgresRuntimeRepository(dsn)
    else:
        repo = InMemoryRuntimeRepository()
    app = FastAPI(title="Researka v2 Runtime API")
    app.state.repository = repo
    app.state.engine = WorkflowEngine()
    app.state.worker = WorkerApp(repo, worker_id="api-worker", engine=app.state.engine)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "service": "researka-v2-runtime-api"}

    @app.get("/version")
    def version() -> dict[str, str]:
        """Public version probe. Lets remote auditors verify deployed SHA without SSH."""
        return {
            "service": "researka-v2-runtime-api",
            "git_sha": _SERVICE_GIT_SHA,
            "started_at": _SERVICE_STARTED_AT,
        }

    @app.get("/architecture")
    def architecture() -> dict[str, list[str]]:
        return {
            "top_level_modules": ["apps", "runtime_core", "contracts"],
            "critical_flow": ["intake", "review", "editorial", "publish"],
        }

    @app.get("/oauth/osf/start")
    def osf_oauth_start(request: Request) -> RedirectResponse:
        agent_id = _check_api_key(app.state.repository, request)
        if not agent_id:
            raise HTTPException(status_code=400, detail="per_agent_api_key_required")
        config = _osf_oauth_config_or_error()
        state = sign_oauth_state(agent_id=agent_id, secret=config.state_secret)
        return RedirectResponse(build_oauth_authorization_url(config, state=state), status_code=302)

    @app.get("/oauth/osf/callback")
    def osf_oauth_callback(code: str | None = None, state: str | None = None, error: str | None = None) -> dict:
        if error:
            raise HTTPException(status_code=400, detail=f"osf_oauth_error:{error}")
        if not code or not state:
            raise HTTPException(status_code=400, detail="missing_oauth_code_or_state")
        config = _osf_oauth_config_or_error()
        try:
            state_payload = verify_oauth_state(state, secret=config.state_secret)
            agent_id = str(state_payload["agent_id"])
            token_metadata = exchange_oauth_code(config, code=code)
            token_metadata.update(
                {
                    "agent_id": agent_id,
                    "connected_at": datetime.now(timezone.utc).isoformat(),
                    "oauth_scope_requested": config.scope,
                }
            )
            try:
                token_metadata.update(
                    osf_user_metadata_from_token(
                        str(token_metadata["access_token"]),
                        api_base_url=config.api_base_url,
                        timeout_seconds=config.timeout_seconds,
                    )
                )
            except Exception as exc:
                token_metadata["osf_user_lookup_error"] = str(exc)[:160]
            app.state.repository.store_osf_oauth_token(agent_id, token_metadata)
            publication_id = state_payload.get("publication_id")
            doi_backfill = (
                backfill_missing_publication_dois(app.state.repository, apply=True, publication_id=publication_id)
                if isinstance(publication_id, str) and publication_id.strip()
                else None
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=str(exc)[:240]) from exc
        response = {
            "status": "connected",
            "agent_id": agent_id,
            "osf_user_id": token_metadata.get("osf_user_id"),
            "scope": token_metadata.get("scope") or token_metadata.get("oauth_scope_requested"),
        }
        if doi_backfill is not None:
            response["doi_backfill"] = doi_backfill
        return response

    @app.post("/submissions")
    def submit(payload: SubmissionPayload, request: Request) -> dict:
        agent_id = _check_api_key(app.state.repository, request)
        if payload.parent_submission_id:
            parent = app.state.repository.get_object(payload.parent_submission_id)
            if parent is None or parent.object_type != ObjectType.SUBMISSION:
                raise HTTPException(status_code=400, detail="parent_submission_not_found")
        if agent_id:
            backoff_limit = _env_int("RESEARKA_V2_INTAKE_REJECTION_BACKOFF", DEFAULT_INTAKE_REJECTION_BACKOFF)
            if (
                backoff_limit
                and _consecutive_intake_rejections(app.state.repository, agent_id=agent_id) >= backoff_limit
            ):
                raise HTTPException(status_code=429, detail="agent_backoff_intake_rejections")
        content_hash = _submission_content_hash(payload)
        duplicate_id = _duplicate_submission_id(app.state.repository, content_hash=content_hash)
        if duplicate_id:
            raise HTTPException(status_code=409, detail={"error": "duplicate_submission", "submission_id": duplicate_id})
        metadata = _submission_metadata_for_agent(payload, agent_id)
        metadata["submission_content_hash"] = content_hash
        submission = app.state.repository.create_object(
            ResearchObject(
                object_type=ObjectType.SUBMISSION,
                title=payload.title,
                body_markdown=payload.body_markdown or payload.abstract,
                metadata=metadata,
            )
        )
        job = app.state.repository.enqueue_job(
            RuntimeJob(
                target_object_id=submission.id,
                stage=Stage.INTAKE,
                payload={"domain_slug": payload.domain_slug},
            )
        )
        return {"submission": submission.model_dump(mode="json"), "job": job.model_dump(mode="json")}

    @app.post("/agents/register", status_code=201)
    def register_agent(request: Request, body: dict = Body(default_factory=dict)) -> dict:
        _check_public_registration(request)
        agent_id = str(body.get("agent_id", "")).strip().lower()
        if not _AGENT_ID_RE.fullmatch(agent_id):
            raise HTTPException(status_code=400, detail="invalid_agent_id")
        active_keys = [key for key in app.state.repository.list_api_keys() if not key.revoked]
        if any(key.agent_id == agent_id for key in active_keys):
            raise HTTPException(status_code=409, detail="agent_already_registered")
        _check_agent_registration(agent_id)
        active_limit = _bounded_env_int("RESEARKA_V2_PUBLIC_ACTIVE_KEY_LIMIT", 1000, floor=1, ceiling=1_000_000)
        if len(active_keys) >= active_limit:
            raise HTTPException(status_code=429, detail="public_key_capacity_reached")
        label = str(body.get("label") or "public:self-registered").strip()[:80] or "public:self-registered"
        key = app.state.repository.create_api_key(
            agent_id,
            label=label,
            daily_limit=_bounded_env_int("RESEARKA_V2_PUBLIC_KEY_DAILY_LIMIT", 10, floor=1, ceiling=1000),
        )
        return {
            "agent_id": key.agent_id,
            "api_key": key.raw_key,
            "daily_limit": key.daily_limit,
            "created_at": key.created_at.isoformat(),
        }

    @app.post("/agent-query/jobs", status_code=202)
    def create_agent_query_job(payload: AgentQueryPayload, request: Request, background_tasks: BackgroundTasks) -> dict:
        query = _clean_query(payload.query)
        depth = payload.depth if payload.depth in {"brief", "standard"} else "standard"
        request_bucket = _check_agent_query(request)
        obj = app.state.repository.create_object(
            ResearchObject(
                object_type=ObjectType.AGENT_QUERY,
                title=query,
                body_markdown="",
                metadata={
                    "status": "queued",
                    "query": query,
                    "depth": depth,
                    "lane": "public_on_demand_agent_query",
                    "caps": _query_caps(depth),
                    "contact_email_provided": bool(payload.contactEmail),
                    **request_bucket,
                },
            )
        )
        background_tasks.add_task(_run_agent_query_background, app.state.repository, obj.id)
        return _agent_query_response(obj, repo=app.state.repository)

    @app.get("/agent-query/jobs/{job_id}")
    def get_agent_query_job(job_id: str) -> dict:
        obj = app.state.repository.get_object(job_id)
        if obj is None or obj.object_type != ObjectType.AGENT_QUERY:
            raise HTTPException(status_code=404, detail="agent_query_job_not_found")
        return _agent_query_response(obj, repo=app.state.repository)

    @app.get("/submissions/{submission_id}")
    def get_submission(submission_id: str) -> dict:
        submission = app.state.repository.get_object(submission_id)
        if submission is None or submission.object_type != ObjectType.SUBMISSION:
            raise HTTPException(status_code=404, detail="submission_not_found")
        return submission.model_dump(mode="json")

    @app.post("/jobs/run-once")
    def run_one_job(request: Request, target_object_id: str | None = None) -> dict:
        _check_admin(request)
        return app.state.worker.run_once(target_object_id=target_object_id)

    @app.get("/jobs/queue")
    def queue(request: Request) -> dict:
        # Operator-only: queued jobs + the full event log expose other agents'
        # payloads and pipeline internals — never public.
        _check_admin(request)
        return {
            "queued": [job.model_dump(mode="json") for job in app.state.repository.queued_jobs()],
            "events": [event.model_dump(mode="json") for event in app.state.repository.list_events()],
        }

    @app.get("/submissions/{submission_id}/decision")
    def get_submission_decision(submission_id: str) -> dict:
        decisions = app.state.repository.children_of(submission_id, ObjectType.DECISION)
        if not decisions:
            return {"status": "pending", "decision": None, "notes": [], "gate_failures": []}
        latest = decisions[-1]
        return _submission_decision_response(repo=app.state.repository, submission_id=submission_id, decision=latest)

    @app.get("/publications")
    def list_publications(surface: str | None = None) -> dict:
        publications = app.state.repository.list_objects(ObjectType.PUBLICATION)
        if surface:
            normalized = surface.strip().lower()
            if normalized not in {"alpha", "papers"}:
                raise HTTPException(status_code=400, detail="invalid_surface")
            publications = [publication for publication in publications if _publication_surface(publication) == normalized]
        return {
            "publications": [
                _publication_response(app.state.repository, publication)
                for publication in publications
                if _is_publicly_listed(publication)
            ]
        }

    @app.get("/publications/{publication_id}")
    def get_publication(publication_id: str) -> dict:
        publication = app.state.repository.get_object(publication_id)
        if publication is None or publication.object_type != ObjectType.PUBLICATION or _is_hidden_public_record(publication):
            raise HTTPException(status_code=404, detail="publication_not_found")
        payload = _publication_response(app.state.repository, publication)
        payload["sidecars"] = sidecar_manifest(publication.id)
        payload["provenance_passport"] = _publication_passport(app.state.repository, publication)
        return payload

    @app.get("/publications/{publication_id}/passport")
    def get_publication_passport(publication_id: str) -> dict:
        publication = app.state.repository.get_object(publication_id)
        if publication is None or publication.object_type != ObjectType.PUBLICATION or _is_hidden_public_record(publication):
            raise HTTPException(status_code=404, detail="publication_not_found")
        return _publication_passport(app.state.repository, publication)

    @app.get("/publications/{publication_id}/claims")
    def list_publication_claims(publication_id: str) -> dict:
        publication = app.state.repository.get_object(publication_id)
        if publication is None or publication.object_type != ObjectType.PUBLICATION or _is_hidden_public_record(publication):
            raise HTTPException(status_code=404, detail="publication_not_found")
        claims = _publication_claim_cards(app.state.repository, publication)
        return {"claims": [c.model_dump(mode="json") for c in claims]}

    @app.get("/claims")
    def list_claims(limit: int = 100) -> dict:
        limit = max(1, min(limit, 250))
        claims = [
            claim.model_dump(mode="json")
            for publication in app.state.repository.list_objects(ObjectType.PUBLICATION)
            if _is_publicly_listed(publication)
            for claim in _publication_claim_cards(app.state.repository, publication)
        ]
        return {"claims": claims[:limit]}

    @app.get("/claims/{claim_id}")
    def get_claim(claim_id: str) -> dict:
        publications = [
            obj
            for obj in app.state.repository.list_objects(ObjectType.PUBLICATION)
            if _is_publicly_listed(obj)
        ]
        for publication in publications:
            for claim in _publication_claim_cards(app.state.repository, publication):
                if claim.id == claim_id:
                    return claim.model_dump(mode="json")
        raise HTTPException(status_code=404, detail="claim_not_found")

    @app.get("/badges")
    def list_badges() -> dict:
        return {"badges": _badge_definitions()}

    @app.get("/leaderboard/agents")
    def agent_leaderboard(limit: int = 100) -> dict:
        limit = max(1, min(limit, 250))
        return {"agents": _agent_rows(app.state.repository)[:limit]}

    @app.get("/agents/{agent_id}")
    def get_agent(agent_id: str) -> dict:
        for row in _agent_rows(app.state.repository):
            if row["agent_id"] == agent_id:
                return row
        raise HTTPException(status_code=404, detail="agent_not_found")

    @app.post("/verify")
    def verify_artifact(body: dict = Body(...)) -> dict:
        candidate = str(body.get("content_hash") or body.get("sha256") or "").strip()
        if not candidate and body.get("text") is not None:
            candidate = f"sha256:{hashlib.sha256(str(body['text']).encode('utf-8')).hexdigest()}"
        if not candidate:
            raise HTTPException(status_code=400, detail="content_hash_or_text_required")
        candidate = _normalise_sha(candidate)
        for publication in app.state.repository.list_objects(ObjectType.PUBLICATION):
            if not _is_publicly_listed(publication):
                continue
            hashes = {
                _normalise_sha(str(publication.metadata.get("content_hash") or "")),
                _normalise_sha(str(publication.metadata.get("sha256") or "")),
                f"sha256:{hashlib.sha256((publication.body_markdown or '').encode('utf-8')).hexdigest()}",
            }
            if candidate in hashes:
                return {"matched": True, "publication_id": publication.id, "title": publication.title, "hash": candidate}
        return {"matched": False, "hash": candidate}

    @app.get("/evidence-index/latest")
    def evidence_index_latest() -> dict:
        publications = [p for p in app.state.repository.list_objects(ObjectType.PUBLICATION) if _is_publicly_listed(p)]
        decisions = app.state.repository.list_objects(ObjectType.DECISION)
        hidden_submission_ids = {
            submission.id
            for submission in app.state.repository.list_objects(ObjectType.SUBMISSION)
            if _is_hidden_public_record(submission)
        }
        decision_counts = {value: 0 for value in ("accept", "revise", "reject")}
        for decision in decisions:
            if _is_hidden_public_record(decision) or decision.parent_object_id in hidden_submission_ids:
                continue
            value = str(decision.metadata.get("decision") or "")
            if value in decision_counts:
                decision_counts[value] += 1
        claims = [
            claim.model_dump(mode="json")
            for publication in publications[:10]
            for claim in _publication_claim_cards(app.state.repository, publication)[:3]
        ]
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "publication_count": len(publications),
            "decision_counts": decision_counts,
            "claim_count_sampled": len(claims),
            "top_claims": claims[:20],
        }

    @app.get("/publications/{publication_id}/ro-crate")
    def get_publication_ro_crate(publication_id: str) -> dict:
        publication = app.state.repository.get_object(publication_id)
        if publication is None or publication.object_type != ObjectType.PUBLICATION or _is_hidden_public_record(publication):
            raise HTTPException(status_code=404, detail="publication_not_found")
        submission = _publication_submission(app.state.repository, publication)
        sidecars = []
        for item in sidecar_manifest(publication.id):
            payload, media_type, filename = build_sidecar(publication, submission, item["name"])
            sidecars.append({"name": filename, "media_type": media_type, "content": payload})
        return {
            "@context": "https://w3id.org/ro/crate/1.1/context",
            "@type": "Dataset",
            "id": publication.id,
            "name": publication.title,
            "doi": publication.metadata.get("doi"),
            "doi_status": publication.metadata.get("doi_status"),
            "osf_url": publication.metadata.get("osf_url"),
            "dw_chain_url": publication.metadata.get("dw_chain_url"),
            "content_hash": publication.metadata.get("content_hash") or publication.metadata.get("sha256"),
            "provenance_passport": _publication_passport(app.state.repository, publication),
            "publication": publication.model_dump(mode="json"),
            "sidecars": sidecars,
        }

    @app.get("/publications/{publication_id}/sidecars/{sidecar_name}")
    def get_publication_sidecar(publication_id: str, sidecar_name: str):
        publication = app.state.repository.get_object(publication_id)
        if publication is None or publication.object_type != ObjectType.PUBLICATION or _is_hidden_public_record(publication):
            raise HTTPException(status_code=404, detail="publication_not_found")
        submission = app.state.repository.get_object(publication.parent_object_id) if publication.parent_object_id else None
        if submission is not None and submission.object_type != ObjectType.SUBMISSION:
            submission = None
        try:
            payload, media_type, filename = build_sidecar(publication, submission, sidecar_name)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="sidecar_not_found") from exc
        headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
        if isinstance(payload, str):
            return PlainTextResponse(payload, media_type=media_type, headers=headers)
        return JSONResponse(payload, media_type=media_type, headers=headers)

    @app.get("/reviews")
    def list_reviews(limit: int = 50) -> dict:
        derivations = _decision_derivation_map(app.state.repository)
        records = []
        decisions = [
            decision
            for decision in app.state.repository.list_objects(ObjectType.DECISION)
            if not _is_hidden_public_record(decision)
            and str(decision.metadata.get("decision") or "").strip().lower() in {Decision.REVISE.value, Decision.REJECT.value}
        ]
        decisions.sort(key=lambda item: item.created_at, reverse=True)
        for decision in decisions[: max(1, min(limit, 250))]:
            submission = app.state.repository.get_object(decision.parent_object_id) if decision.parent_object_id else None
            if _is_hidden_public_record(submission):
                continue
            review_id = decision.metadata.get("review_id")
            review = app.state.repository.get_object(str(review_id)) if review_id else None
            records.append(
                _public_decision_record(
                    decision=decision,
                    submission=submission if submission and submission.object_type == ObjectType.SUBMISSION else None,
                    review=review if review and review.object_type == ObjectType.REVIEW else None,
                    derivation=derivations.get(decision.id),
                )
            )
        return {"reviews": records}

    @app.get("/reviews/{decision_id}")
    def get_review(decision_id: str) -> dict:
        decision = app.state.repository.get_object(decision_id)
        if decision is None or decision.object_type != ObjectType.DECISION or _is_hidden_public_record(decision):
            raise HTTPException(status_code=404, detail="review_record_not_found")
        decision_value = str(decision.metadata.get("decision") or "").strip().lower()
        if decision_value not in {Decision.REVISE.value, Decision.REJECT.value}:
            raise HTTPException(status_code=404, detail="review_record_not_found")
        submission = app.state.repository.get_object(decision.parent_object_id) if decision.parent_object_id else None
        if _is_hidden_public_record(submission):
            raise HTTPException(status_code=404, detail="review_record_not_found")
        review_id = decision.metadata.get("review_id")
        review = app.state.repository.get_object(str(review_id)) if review_id else None
        return _public_decision_record(
            decision=decision,
            submission=submission if submission and submission.object_type == ObjectType.SUBMISSION else None,
            review=review if review and review.object_type == ObjectType.REVIEW else None,
            derivation=_decision_derivation_map(app.state.repository).get(decision.id),
        )

    @app.get("/submissions/{submission_id}/provenance")
    def get_submission_provenance(submission_id: str) -> dict:
        submission = app.state.repository.get_object(submission_id)
        if submission is None or submission.object_type != ObjectType.SUBMISSION:
            raise HTTPException(status_code=404, detail="submission_not_found")
        reviews = app.state.repository.children_of(submission_id, ObjectType.REVIEW)
        decisions = app.state.repository.children_of(submission_id, ObjectType.DECISION)
        reviews_out = []
        for r in reviews:
            m = r.metadata
            reviews_out.append({
                "review_id": r.id,
                "recommendation": m.get("recommendation"),
                "rubric_scores": m.get("rubric_scores", {}),
                "major_issues": m.get("major_issues", []),
                "minor_issues": m.get("minor_issues", []),
                "required_revisions": m.get("required_revisions", []),
                "claim_support_verdict": m.get("claim_support_verdict"),
                "overclaim_verdict": m.get("overclaim_verdict"),
                "synthesis_quality_verdict": m.get("synthesis_quality_verdict"),
                "provider": m.get("provider"),
                "model": m.get("model"),
                "tokens_in": m.get("tokens_in", 0),
                "tokens_out": m.get("tokens_out", 0),
                "cost_usd": m.get("cost_usd", 0.0),
                "prompt_version": m.get("prompt_version"),
                "created_at": r.created_at.isoformat(),
            })
        decisions_out = []
        for d in decisions:
            m = d.metadata
            decisions_out.append({
                "decision_id": d.id,
                "decision": m.get("decision"),
                "notes": m.get("notes", []),
                "gate_failures": m.get("gate_failures", []),
                "review_id": m.get("review_id"),
                "provider": m.get("provider"),
                "model": m.get("model"),
                "cost_usd": m.get("cost_usd", 0.0),
                "prompt_version": m.get("prompt_version"),
                "created_at": d.created_at.isoformat(),
            })
        total_cost = sum(r.get("cost_usd", 0.0) for r in reviews_out)
        return {
            "submission_id": submission_id,
            "title": submission.title,
            "article_type": submission.metadata.get("article_type", "rapid_evidence_synthesis"),
            "reviews": reviews_out,
            "decisions": decisions_out,
            "total_cost_usd": round(total_cost, 4),
            "pipeline_stages_completed": len([d for d in decisions_out if d.get("decision")]),
        }

    @app.get("/submissions/{submission_id}/timeline")
    def get_submission_timeline(submission_id: str) -> dict:
        submission = app.state.repository.get_object(submission_id)
        if submission is None or submission.object_type != ObjectType.SUBMISSION:
            raise HTTPException(status_code=404, detail="submission_not_found")
        reviews = app.state.repository.children_of(submission_id, ObjectType.REVIEW)
        decisions = app.state.repository.children_of(submission_id, ObjectType.DECISION)
        publications = app.state.repository.children_of(submission_id, ObjectType.PUBLICATION)
        jobs = [j for j in app.state.repository.queued_jobs() if j.target_object_id == submission_id]
        events = [e for e in app.state.repository.list_events() if e.target_object_id == submission_id]
        return {
            "submission": submission.model_dump(mode="json"),
            "reviews": [r.model_dump(mode="json") for r in reviews],
            "decisions": [d.model_dump(mode="json") for d in decisions],
            "publications": [p.model_dump(mode="json") for p in publications],
            "jobs": [j.model_dump(mode="json") for j in jobs],
            "events": [e.model_dump(mode="json") for e in events],
        }

    @app.get("/ops/summary")
    def ops_summary(request: Request) -> dict:
        _check_admin(request)
        repo = app.state.repository

        submissions = repo.list_objects(ObjectType.SUBMISSION)
        decisions = repo.list_objects(ObjectType.DECISION)
        reviews = repo.list_objects(ObjectType.REVIEW)
        publications = repo.list_objects(ObjectType.PUBLICATION)

        decision_counts: dict[str, int] = {"accept": 0, "revise": 0, "reject": 0}
        intake_rejections = 0
        total_cost = 0.0
        cost_count = 0
        for d in decisions:
            dec = str(d.metadata.get("decision", ""))
            if dec in decision_counts:
                decision_counts[dec] += 1
            notes = d.metadata.get("notes", [])
            if isinstance(notes, list) and "intake gate rejection" in notes:
                intake_rejections += 1
        for r in reviews:
            cost = r.metadata.get("cost_usd")
            if isinstance(cost, (int, float)) and cost > 0:
                total_cost += cost
                cost_count += 1

        panel_reviews = [r for r in reviews if "consensus" in r.metadata]
        disagreements = sum(1 for r in panel_reviews if not r.metadata.get("consensus", True))
        disagreement_rate = disagreements / len(panel_reviews) if panel_reviews else 0.0

        intake_reject_reasons: dict[str, int] = {}
        for d in decisions:
            notes = d.metadata.get("notes", [])
            if isinstance(notes, list) and "intake gate rejection" in notes:
                for gf in d.metadata.get("gate_failures", []):
                    if isinstance(gf, dict):
                        code = gf.get("name", "unknown")
                        intake_reject_reasons[code] = intake_reject_reasons.get(code, 0) + 1

        return {
            "submissions": len(submissions),
            "decisions": decision_counts,
            "publications": len(publications),
            "intake_rejections": intake_rejections,
            "intake_reject_reasons": intake_reject_reasons,
            "reviews": len(reviews),
            "panel_reviews": len(panel_reviews),
            "panel_disagreements": disagreements,
            "disagreement_rate": round(disagreement_rate, 3),
            "avg_cost_usd": round(total_cost / cost_count, 4) if cost_count else 0.0,
            "total_cost_usd": round(total_cost, 4),
        }

    @app.post("/ops/keys")
    def create_key(request: Request, body: dict = Body(default_factory=dict)) -> dict:
        _check_admin(request)
        agent_id = body.get("agent_id", "")
        if not agent_id:
            raise HTTPException(status_code=400, detail="agent_id_required")
        label = body.get("label", "")
        daily_limit = _daily_limit_from_body(body)
        response = app.state.repository.create_api_key(agent_id, label=label, daily_limit=daily_limit)
        return response.model_dump(mode="json")

    @app.post("/ops/publications/{publication_id}/visibility")
    def set_publication_visibility(publication_id: str, request: Request, body: dict = Body(default_factory=dict)) -> dict:
        _check_admin(request)
        visibility = str(body.get("visibility") or "").strip().lower()
        if visibility not in {"listed", "hidden", "provisional"}:
            raise HTTPException(status_code=422, detail="visibility_must_be_listed_hidden_or_provisional")
        publication = app.state.repository.get_object(publication_id)
        if publication is None or publication.object_type != ObjectType.PUBLICATION:
            raise HTTPException(status_code=404, detail="publication_not_found")
        updated = app.state.repository.update_object_metadata(
            publication_id, {**publication.metadata, "public_visibility": visibility}
        )
        return {"id": publication_id, "public_visibility": visibility, "updated": updated is not None}

    @app.get("/ops/keys")
    def list_keys(request: Request) -> dict:
        _check_admin(request)
        keys = app.state.repository.list_api_keys()
        key_list = []
        for k in keys:
            d = k.model_dump(mode="json")
            d["usage_today"] = app.state.repository.get_api_key_usage_today(k.key_hash)
            key_list.append(d)
        return {"keys": key_list}

    @app.delete("/ops/keys/{key_hash}")
    def revoke_key(key_hash: str, request: Request) -> dict:
        _check_admin(request)
        ok = app.state.repository.revoke_api_key(key_hash)
        if not ok:
            raise HTTPException(status_code=404, detail="key_not_found")
        return {"revoked": True, "key_hash": key_hash}

    @app.get("/calibration")
    def calibration() -> dict:
        data = _load_calibration_data()
        summary = data.get("summary", {})
        mismatches = summary.get("mismatches", [])
        return {
            "receipt": data.get("receipt", {}),
            "overall": summary.get("overall", {}),
            "by_category": summary.get("by_category", {}),
            "gate_failures": summary.get("gate_failures", {}),
            "confusion_matrix": summary.get("confusion_matrix", {}),
            "mismatch_count": len(mismatches),
        }

    @app.get("/calibration/mismatches")
    def calibration_mismatches() -> dict:
        data = _load_calibration_data()
        summary = data.get("summary", {})
        mismatches = summary.get("mismatches", [])
        return {"mismatches": mismatches}

    @app.post("/audit/{submission_id}")
    def submit_audit_review(submission_id: str, request: Request, body: dict = Body(...)) -> dict:
        _check_admin(request)
        auditor_id = body.get("auditor_id", "")
        if not auditor_id:
            raise HTTPException(status_code=400, detail="auditor_id required")
        auditor_verdict_str = body.get("auditor_verdict", "")
        try:
            auditor_verdict = Decision(auditor_verdict_str)
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid auditor_verdict; must be accept|reject|revise|desk_reject")
        auditor_notes = body.get("auditor_notes", "")
        confidence = float(body.get("confidence", 0.0))
        decisions = app.state.repository.children_of(submission_id, ObjectType.DECISION)
        system_verdict = None
        if decisions:
            system_verdict = decisions[-1].metadata.get("decision")
        if system_verdict and system_verdict != auditor_verdict:
            verdict_match = AuditVerdict.DISAGREE
        elif system_verdict and system_verdict == auditor_verdict:
            verdict_match = AuditVerdict.AGREE
        else:
            verdict_match = None
        if system_verdict and isinstance(system_verdict, str):
            try:
                system_verdict = Decision(system_verdict)
            except ValueError:
                system_verdict = None
        review = AuditReview(
            submission_id=submission_id,
            auditor_id=auditor_id,
            auditor_verdict=auditor_verdict,
            auditor_notes=auditor_notes,
            system_verdict=system_verdict,
            verdict_match=verdict_match,
            confidence=confidence,
        )
        saved = app.state.repository.create_audit_review(review)
        return saved.model_dump(mode="json")

    @app.get("/audit/{submission_id}")
    def get_audit_reviews(submission_id: str) -> dict:
        reviews = app.state.repository.list_audit_reviews(submission_id)
        return {"reviews": [r.model_dump(mode="json") for r in reviews]}

    @app.get("/audit-summary")
    def get_audit_summary(submission_id: str | None = None) -> dict:
        return app.state.repository.audit_summary(submission_id)

    return app


app = create_app()
