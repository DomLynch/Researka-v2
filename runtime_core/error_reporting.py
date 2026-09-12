"""Opt-in operational errors. Never give the SDK exceptions or application data."""
from __future__ import annotations

import builtins
import logging
import os
import re
from pathlib import Path
from traceback import walk_tb
from typing import TYPE_CHECKING, cast
from uuid import UUID

import sentry_sdk

from contracts import FailureClass, RuntimeJob, Stage
from runtime_core.judge_release import SERVICE_GIT_SHA

if TYPE_CHECKING:
    from sentry_sdk._types import Event

_ROOT = Path(__file__).resolve().parents[1]
_client: sentry_sdk.Client | None = None
_service = "api"
_TECHNICAL_FAILURES = {
    FailureClass.AUTHENTICATION_REQUIRED, FailureClass.DB_TIMEOUT,
    FailureClass.JOB_TIMEOUT, FailureClass.DB_CONNECTION_BAD,
    FailureClass.PROVIDER_ERROR, FailureClass.SYSTEM_UNAVAILABLE, FailureClass.OTHER,
}
_STAGES = {stage.value for stage in Stage} | {
    "api", "api_startup", "worker_startup", "worker_loop", "maintenance", "lease_heartbeat", "smoke",
}


def _before_send(event: Event, _hint: dict) -> Event:
    # Reconstruct, rather than redact: SDK-added context is not part of our contract.
    allowed = (
        "event_id", "timestamp", "platform", "level", "release", "environment",
        "exception", "tags",
    )
    return cast("Event", {key: value for key, value in event.items() if key in allowed})


def configure_error_reporting(service: str) -> bool:
    global _client, _service
    if _client is not None:
        return True
    dsn = os.getenv("SENTRY_DSN", "").strip()
    if not dsn:
        return False
    environment = os.getenv("SENTRY_ENVIRONMENT", os.getenv("RESEARKA_V2_ENV", "development"))
    if environment not in {"production", "staging", "development", "test"}:
        environment = "unknown"
    release = SERVICE_GIT_SHA if re.fullmatch(r"[a-f0-9]{40}", SERVICE_GIT_SHA) else "unknown"
    try:
        _client = sentry_sdk.Client(
            dsn=dsn, environment=environment, release=f"researka-core@{release}",
            default_integrations=False, auto_enabling_integrations=False,
            send_default_pii=False, include_local_variables=False,
            include_source_context=False, max_request_body_size="never",
            max_breadcrumbs=0, before_send=_before_send,
            traces_sample_rate=0.0, profiles_sample_rate=0.0,
            enable_logs=False, before_send_metric=lambda _metric, _hint: None, auto_session_tracking=False,
            send_client_reports=False, enable_backpressure_handling=False,
            trace_propagation_targets=[], server_name="", shutdown_timeout=2, debug=False,
            spotlight=False,
        )
        _service = service if service in {"api", "worker"} else "unknown"
        return True
    except Exception:
        logging.getLogger(__name__).warning("Core error reporting unavailable: invalid configuration")
        return False


def _safe_frames(exc: Exception) -> list[dict]:
    frames = []
    for frame, line in walk_tb(exc.__traceback__):
        path = Path(frame.f_code.co_filename).resolve()
        if not path.is_relative_to(_ROOT):
            continue
        relative = path.relative_to(_ROOT)
        if relative.parts[0] not in {"apps", "runtime_core", "contracts"} or path.suffix != ".py":
            continue
        frames.append({"filename": relative.as_posix(), "lineno": line, "in_app": True})
    return frames[-30:]


def _job_tags(job: RuntimeJob | None) -> dict[str, str]:
    if job is None:
        return {}
    tags = {}
    for key, value in (("job_id", job.id), ("target_id", job.target_object_id)):
        try:
            tags[key] = str(UUID(value))
        except (ValueError, TypeError, AttributeError):
            pass
    return tags


def report_error(
    exc: Exception, *, stage: str, job: RuntimeJob | None = None,
    failure_class: FailureClass | None = None,
) -> str | None:
    if _client is None:
        return None
    try:
        kind = type(exc).__name__
        if getattr(builtins, kind, None) is not type(exc):
            kind = "ApplicationError"
        tags = {"service": _service, "stage": stage if stage in _STAGES else "unknown", **_job_tags(job)}
        if failure_class in _TECHNICAL_FAILURES:
            tags["failure_class"] = failure_class.value
        frames = _safe_frames(exc)
        return _client.capture_event({
            "level": "error", "tags": tags,
            "exception": {"values": [{
                "type": kind, "value": "[redacted]",
                **({"stacktrace": {"frames": frames}} if frames else {}),
            }]},
        })
    except Exception:
        # Telemetry must never affect a decision, retry, response, or original error.
        return None


def report_job_failure(exc: Exception, job: RuntimeJob, failure_class: FailureClass, *, retrying: bool) -> None:
    if not retrying and failure_class in _TECHNICAL_FAILURES:
        report_error(exc, stage=job.stage.value, job=job, failure_class=failure_class)


def flush_error_reporting() -> None:
    try:
        if _client is not None:
            _client.flush(timeout=2)
    except Exception:
        pass
