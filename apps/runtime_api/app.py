from __future__ import annotations

import json
import os
import hashlib
import re
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field
from fastapi import BackgroundTasks, Body, FastAPI, HTTPException, Request

from apps.worker.main import WorkerApp
from contracts import AuditReview, AuditVerdict, Decision, ObjectType, ResearchObject, RuntimeJob, Stage, SubmissionPayload, normalize_orcid
from runtime_core import InMemoryRuntimeRepository, PostgresRuntimeRepository, WorkflowEngine
from runtime_core.agent_query import fail_agent_query_job, run_agent_query_job
from runtime_core.repos import RuntimeRepository, postgres_dsn_from_env

_calibration_cache: dict | None = None
_calibration_path: str | None = None
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
    return {
        "summary": {
            "overall": {
                **aggregates,
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
        str(Path(__file__).resolve().parents[2] / "artifacts" / "benchmark_baseline.json"),
    )
    if _calibration_cache is not None and _calibration_path == default_path:
        return _calibration_cache
    path = Path(default_path)
    if not path.exists():
        return {"summary": {}, "results": []}
    raw = json.loads(path.read_text())
    _calibration_cache = _normalize_benchmark(raw)
    _calibration_path = default_path
    return _calibration_cache


def _legacy_identity() -> dict[str, str | None]:
    try:
        owner_orcid = normalize_orcid(os.environ.get("RESEARKA_V2_DEFAULT_ORCID"))
    except ValueError as exc:
        raise HTTPException(status_code=500, detail=f"invalid_default_orcid:{exc}") from exc
    owner_name = os.environ.get("RESEARKA_V2_DEFAULT_OWNER_NAME")
    return {
        "auth_source": "legacy_api_key",
        "agent_id": None,
        "owner_name": owner_name.strip() if owner_name else None,
        "owner_orcid": owner_orcid,
    }


def _check_api_key(repo: RuntimeRepository, request: Request) -> dict[str, str | None]:
    """Validate API key. Returns trusted identity metadata if valid, raises 403 if not.

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
        return _legacy_identity()

    # Per-agent key
    key_info = repo.validate_api_key_info(provided)
    if key_info is not None:
        key_hash = hashlib.sha256(provided.encode()).hexdigest()
        repo.record_api_key_usage(key_hash)
        return {
            "auth_source": "api_key",
            "agent_id": key_info.agent_id,
            "owner_name": key_info.owner_name,
            "owner_orcid": key_info.owner_orcid,
        }

    raise HTTPException(status_code=403, detail="invalid_api_key")


def _check_admin(request: Request) -> None:
    """Require admin key for /ops/* endpoints."""
    provided = request.headers.get("x-api-key", "")
    admin_key = os.environ.get("RESEARKA_V2_ADMIN_KEY")
    if admin_key and provided == admin_key:
        return
    raise HTTPException(status_code=403, detail="admin_key_required")


def _env_int(name: str, default: int, *, floor: int, ceiling: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        value = default
    return max(floor, min(value, ceiling))


def _env_float(name: str, default: float, *, floor: float, ceiling: float) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except ValueError:
        value = default
    return max(floor, min(value, ceiling))


def _feature_enabled(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).lower() not in {"0", "false", "no", "off"}


def _clean_query(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()[:240]


def _registration_bucket(request: Request) -> tuple[str, str, int]:
    host = request.client.host if request.client else "unknown"
    if host in {"127.0.0.1", "::1", "testclient"}:
        forwarded = request.headers.get("x-forwarded-for", "").split(",", 1)[0].strip()
        if forwarded:
            host = forwarded
    if host in {"127.0.0.1", "::1", "testclient"}:
        return ("global", "RESEARKA_V2_PUBLIC_REGISTRATIONS_PER_DAY", 200)
    return (f"ip:{host[:64]}", "RESEARKA_V2_PUBLIC_REGISTRATIONS_PER_IP_PER_DAY", 3)


def _check_public_registration(app: FastAPI, request: Request) -> None:
    if os.environ.get("RESEARKA_V2_PUBLIC_REGISTRATION_ENABLED", "1").lower() in {"0", "false", "no"}:
        raise HTTPException(status_code=403, detail="public_registration_disabled")
    day = datetime.now(UTC).strftime("%Y-%m-%d")
    bucket, env_name, default = _registration_bucket(request)
    key = (bucket, day)
    limit = _env_int(env_name, default, floor=1, ceiling=10_000)
    if app.state.public_registration_counts.get(key, 0) >= limit:
        raise HTTPException(status_code=429, detail="registration_rate_limited")
    app.state.public_registration_counts[key] = app.state.public_registration_counts.get(key, 0) + 1


def _check_agent_query(app: FastAPI, request: Request) -> dict[str, str]:
    if not _feature_enabled("RESEARKA_V2_AGENT_QUERY_ENABLED"):
        raise HTTPException(status_code=503, detail="agent_query_disabled")
    day = datetime.now(UTC).strftime("%Y-%m-%d")
    bucket, _, _ = _registration_bucket(request)
    bucket_hash = hashlib.sha256(bucket.encode()).hexdigest()
    limit = _env_int("RESEARKA_V2_AGENT_QUERY_PER_IP_PER_DAY", 25, floor=1, ceiling=10_000)
    used = sum(
        1 for item in app.state.repository.list_objects(ObjectType.AGENT_QUERY)
        if item.metadata.get("request_day") == day and item.metadata.get("client_bucket_hash") == bucket_hash
    )
    if used >= limit:
        raise HTTPException(status_code=429, detail="agent_query_rate_limited")
    return {"request_day": day, "client_bucket_hash": bucket_hash}


def _query_caps(depth: str) -> dict:
    standard = depth == "standard"
    return {
        "max_runtime_sec": _env_int("RESEARKA_V2_AGENT_QUERY_MAX_RUNTIME_SEC", 180 if standard else 60, floor=5, ceiling=3600),
        "max_sources": _env_int("RESEARKA_V2_AGENT_QUERY_MAX_SOURCES", 24 if standard else 8, floor=1, ceiling=100),
        "max_cost_usd": _env_float("RESEARKA_V2_AGENT_QUERY_MAX_COST_USD", 0.50 if standard else 0.10, floor=0.0, ceiling=100.0),
    }


def _agent_query_response(obj: ResearchObject, *, repo: RuntimeRepository) -> dict:
    metadata = obj.metadata
    queued = [
        item for item in repo.list_objects(ObjectType.AGENT_QUERY)
        if item.metadata.get("status") == "queued"
    ]
    position = next((i + 1 for i, item in enumerate(queued) if item.id == obj.id), None)
    return {
        "jobId": obj.id,
        "status": metadata.get("status", "queued"),
        "query": metadata.get("query", obj.title),
        "depth": metadata.get("depth", "standard"),
        "createdAt": obj.created_at.isoformat(),
        "updatedAt": metadata.get("updated_at", obj.created_at.isoformat()),
        "position": position,
        "errorMessage": metadata.get("error_message"),
        "result": metadata.get("result"),
        "caps": metadata.get("caps", {}),
    }


def _run_agent_query_background(repo: RuntimeRepository, job_id: str) -> None:
    try:
        run_agent_query_job(repo, job_id)
    except Exception as exc:
        fail_agent_query_job(repo, job_id, str(exc))


def _trusted_submission_metadata(payload: SubmissionPayload, identity: dict[str, str | None]) -> dict:
    metadata = payload.model_dump(mode="json")
    trusted_agent_id = identity.get("agent_id")
    claimed_agent_id = metadata.get("author_agent_id")
    if trusted_agent_id:
        if claimed_agent_id != trusted_agent_id:
            metadata["claimed_author_agent_id"] = claimed_agent_id
        metadata["author_agent_id"] = trusted_agent_id
        metadata["authenticated_agent_id"] = trusted_agent_id
    metadata["identity_source"] = identity.get("auth_source")

    try:
        submitted_orcid = normalize_orcid(metadata.get("submitter_orcid"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"invalid_submitter_orcid:{exc}") from exc
    trusted_orcid = identity.get("owner_orcid")
    if trusted_orcid and submitted_orcid and trusted_orcid != submitted_orcid:
        raise HTTPException(status_code=403, detail="submitter_orcid_mismatch")
    final_orcid = trusted_orcid or submitted_orcid
    if final_orcid:
        metadata["orcid"] = final_orcid
        metadata["author_orcid"] = final_orcid
        metadata["human_owner_orcid"] = final_orcid

    owner_name = identity.get("owner_name") or metadata.get("submitter_name")
    if owner_name:
        metadata["human_owner_name"] = owner_name
    return metadata


def create_app(repository: RuntimeRepository | None = None) -> FastAPI:
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
    app.state.public_registration_counts = {}

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "service": "researka-v2-runtime-api"}

    @app.get("/architecture")
    def architecture() -> dict[str, list[str]]:
        return {
            "top_level_modules": ["apps", "runtime_core", "contracts"],
            "critical_flow": ["intake", "review", "editorial", "publish"],
        }

    @app.post("/submissions")
    def submit(payload: SubmissionPayload, request: Request) -> dict:
        identity = _check_api_key(app.state.repository, request)
        metadata = _trusted_submission_metadata(payload, identity)
        submission = app.state.repository.create_object(
            ResearchObject(
                object_type=ObjectType.SUBMISSION,
                title=payload.title,
                body_markdown=payload.abstract,
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
        agent_id = str(body.get("agent_id", "")).strip().lower()
        if not _AGENT_ID_RE.fullmatch(agent_id):
            raise HTTPException(status_code=400, detail="invalid_agent_id")
        _check_public_registration(app, request)
        active_keys = [key for key in app.state.repository.list_api_keys() if not key.revoked]
        if any(key.agent_id == agent_id for key in active_keys):
            raise HTTPException(status_code=409, detail="agent_already_registered")
        active_limit = _env_int("RESEARKA_V2_PUBLIC_ACTIVE_KEY_LIMIT", 1000, floor=1, ceiling=1_000_000)
        if len(active_keys) >= active_limit:
            raise HTTPException(status_code=429, detail="public_key_capacity_reached")
        owner_name = body.get("owner_name")
        if isinstance(owner_name, str):
            owner_name = owner_name.strip()[:120] or None
        try:
            owner_orcid = normalize_orcid(body.get("owner_orcid"))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"invalid_owner_orcid:{exc}") from exc
        key = app.state.repository.create_api_key(
            agent_id,
            label="public:self-registered",
            daily_limit=_env_int("RESEARKA_V2_PUBLIC_KEY_DAILY_LIMIT", 10, floor=1, ceiling=1000),
            owner_name=owner_name,
            owner_orcid=owner_orcid,
        )
        return {
            "agent_id": key.agent_id,
            "api_key": key.raw_key,
            "daily_limit": key.daily_limit,
            "owner_name": key.owner_name,
            "owner_orcid": key.owner_orcid,
            "created_at": key.created_at.isoformat(),
        }

    @app.post("/agent-query/jobs", status_code=202)
    def create_agent_query_job(payload: AgentQueryPayload, request: Request, background_tasks: BackgroundTasks) -> dict:
        request_bucket = _check_agent_query(app, request)
        query = _clean_query(payload.query)
        depth = payload.depth if payload.depth in {"brief", "standard"} else "standard"
        obj = app.state.repository.create_object(
            ResearchObject(
                object_type=ObjectType.AGENT_QUERY,
                title=query,
                metadata={
                    "query": query,
                    "depth": depth,
                    "status": "queued",
                    "updated_at": datetime.now(UTC).isoformat(),
                    "caps": _query_caps(depth),
                    "contact_email_provided": bool(payload.contactEmail),
                    "lane": "public_on_demand_agent_query",
                    **request_bucket,
                },
            )
        )
        response = _agent_query_response(obj, repo=app.state.repository)
        background_tasks.add_task(_run_agent_query_background, app.state.repository, obj.id)
        return response

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
    def run_one_job(target_object_id: str | None = None) -> dict:
        return app.state.worker.run_once(target_object_id=target_object_id)

    @app.get("/jobs/queue")
    def queue() -> dict:
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
        return {
            "status": "complete",
            "decision": latest.metadata.get("decision"),
            "notes": latest.metadata.get("notes", []),
            "gate_failures": latest.metadata.get("gate_failures", []),
            "decision_object_id": latest.id,
        }

    @app.get("/publications")
    def list_publications() -> dict:
        publications = app.state.repository.list_objects(ObjectType.PUBLICATION)
        return {"publications": [publication.model_dump(mode="json") for publication in publications]}

    @app.get("/publications/{publication_id}")
    def get_publication(publication_id: str) -> dict:
        publication = app.state.repository.get_object(publication_id)
        if publication is None or publication.object_type != ObjectType.PUBLICATION:
            raise HTTPException(status_code=404, detail="publication_not_found")
        return publication.model_dump(mode="json")

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
        daily_limit = body.get("daily_limit", 0)
        owner_name = body.get("owner_name")
        if isinstance(owner_name, str):
            owner_name = owner_name.strip() or None
        try:
            owner_orcid = normalize_orcid(body.get("owner_orcid"))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"invalid_owner_orcid:{exc}") from exc
        response = app.state.repository.create_api_key(
            agent_id,
            label=label,
            daily_limit=daily_limit,
            owner_name=owner_name,
            owner_orcid=owner_orcid,
        )
        return response.model_dump(mode="json")

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
