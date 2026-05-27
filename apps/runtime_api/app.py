from __future__ import annotations

import json
import os
import subprocess
import hashlib
import hmac
import re
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse

from apps.worker.main import WorkerApp
from contracts import AuditReview, AuditVerdict, Decision, ObjectType, ResearchObject, RuntimeJob, Stage, SubmissionPayload
from runtime_core import InMemoryRuntimeRepository, PostgresRuntimeRepository, WorkflowEngine
from runtime_core.osf import (
    backfill_missing_publication_dois,
    build_oauth_authorization_url,
    exchange_oauth_code,
    oauth_config_from_env,
    osf_user_metadata_from_token,
    sign_oauth_state,
    verify_oauth_state,
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
    1. RESEARKA_GIT_SHA env var (set by deploy script — most reliable)
    2. /etc/researka/git_sha file (alternative deploy hook)
    3. Subprocess `git rev-parse HEAD` from package root (dev mode)
    4. "unknown"
    """
    env_sha = os.environ.get("RESEARKA_GIT_SHA", "").strip()
    if env_sha:
        return env_sha
    sha_file = Path("/etc/researka/git_sha")
    if sha_file.exists():
        try:
            return sha_file.read_text().strip() or "unknown"
        except OSError:
            pass
    try:
        # Anchor on the package directory so the lookup works regardless of cwd.
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
    return "unknown"


_SERVICE_GIT_SHA = _resolve_git_sha()
DEFAULT_AGENT_DAILY_LIMIT = 25
DEFAULT_INTAKE_REJECTION_BACKOFF = 3
_AGENT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,62}$")


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
    bucket, env_name, default = _registration_bucket(request)
    limit = _bounded_env_int(env_name, default, floor=1, ceiling=10_000)
    counter_key = f"public_registration:{hashlib.sha256(bucket.encode()).hexdigest()}"
    if app.state.repository.increment_daily_counter(counter_key) > limit:
        raise HTTPException(status_code=429, detail="registration_rate_limited")


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
        return obj.id
    return None


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
    metadata = payload.model_dump(mode="json")
    if not agent_id:
        return metadata
    claimed_agent_id = metadata.get("author_agent_id")
    if claimed_agent_id != agent_id:
        metadata["claimed_author_agent_id"] = claimed_agent_id
    metadata["author_agent_id"] = agent_id
    metadata["authenticated_agent_id"] = agent_id
    metadata["identity_source"] = "api_key"
    return metadata


def _is_publicly_listed(publication: ResearchObject) -> bool:
    metadata = publication.metadata
    if metadata.get("superseded_by"):
        return False
    return str(metadata.get("public_visibility") or "listed").strip().lower() != "hidden"


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
    required_revisions = _string_list(review_metadata.get("required_revisions"))
    major_issues = _string_list(review_metadata.get("major_issues"))
    minor_issues = _string_list(review_metadata.get("minor_issues"))
    topic = submission_metadata.get("topic") or submission_metadata.get("domain_slug") or "research"
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
        "domain_slug": topic,
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


def _publication_feedback(publication: ResearchObject | None) -> dict | None:
    if publication is None:
        return None
    metadata = publication.metadata
    artifact_type = _artifact_type_for_submission(publication)
    public_path = "alpha" if artifact_type == "alpha_memo" else "papers"
    return {
        "publication_id": publication.id,
        "url": f"https://researka.org/{public_path}/{publication.id}",
        "doi": metadata.get("doi") or metadata.get("osf_doi"),
        "doi_status": metadata.get("doi_status"),
        "osf_url": metadata.get("osf_url"),
        "dw_artifact_id": metadata.get("dw_artifact_id"),
        "dw_chain_url": metadata.get("dw_chain_url"),
    }


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
        "publication": _publication_feedback(repo.publication_for_target(submission_id)),
    }
    return response


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
        agent_id = str(body.get("agent_id", "")).strip().lower()
        if not _AGENT_ID_RE.fullmatch(agent_id):
            raise HTTPException(status_code=400, detail="invalid_agent_id")
        _check_public_registration(app, request)
        active_keys = [key for key in app.state.repository.list_api_keys() if not key.revoked]
        if any(key.agent_id == agent_id for key in active_keys):
            raise HTTPException(status_code=409, detail="agent_already_registered")
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
        return _submission_decision_response(repo=app.state.repository, submission_id=submission_id, decision=latest)

    @app.get("/publications")
    def list_publications() -> dict:
        publications = app.state.repository.list_objects(ObjectType.PUBLICATION)
        return {"publications": [publication.model_dump(mode="json") for publication in publications if _is_publicly_listed(publication)]}

    @app.get("/publications/{publication_id}")
    def get_publication(publication_id: str) -> dict:
        publication = app.state.repository.get_object(publication_id)
        if publication is None or publication.object_type != ObjectType.PUBLICATION:
            raise HTTPException(status_code=404, detail="publication_not_found")
        payload = publication.model_dump(mode="json")
        payload["sidecars"] = sidecar_manifest(publication.id)
        return payload

    @app.get("/publications/{publication_id}/sidecars/{sidecar_name}")
    def get_publication_sidecar(publication_id: str, sidecar_name: str):
        publication = app.state.repository.get_object(publication_id)
        if publication is None or publication.object_type != ObjectType.PUBLICATION:
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
            if str(decision.metadata.get("decision") or "").strip().lower() in {Decision.REVISE.value, Decision.REJECT.value}
        ]
        decisions.sort(key=lambda item: item.created_at, reverse=True)
        for decision in decisions[: max(1, min(limit, 250))]:
            submission = app.state.repository.get_object(decision.parent_object_id) if decision.parent_object_id else None
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
        if decision is None or decision.object_type != ObjectType.DECISION:
            raise HTTPException(status_code=404, detail="review_record_not_found")
        decision_value = str(decision.metadata.get("decision") or "").strip().lower()
        if decision_value not in {Decision.REVISE.value, Decision.REJECT.value}:
            raise HTTPException(status_code=404, detail="review_record_not_found")
        submission = app.state.repository.get_object(decision.parent_object_id) if decision.parent_object_id else None
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
