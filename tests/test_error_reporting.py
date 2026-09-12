import json
from uuid import uuid4

import pytest
import sentry_sdk
from fastapi import HTTPException, Request
from fastapi.testclient import TestClient
from sentry_sdk.transport import Transport

from apps.runtime_api import app as runtime_api
from apps.runtime_api.app import create_app
from apps.worker import loop
from apps.worker.main import WorkerApp
from contracts import Decision, FailureClass, RuntimeJob, Stage
from runtime_core import error_reporting as reporting
from runtime_core.repos import InMemoryRuntimeRepository

SECRET = "PRIVATE-manuscript-prompt-evidence-bearer-123"


class MemoryTransport(Transport):
    def __init__(self):
        super().__init__()
        self.envelopes = []

    def capture_envelope(self, envelope):
        self.envelopes.append(envelope)

    @property
    def events(self):
        return [item.get_event() for envelope in self.envelopes for item in envelope.items if item.type == "event"]


@pytest.fixture
def captured(monkeypatch, request):
    transport = MemoryTransport()
    client_type = sentry_sdk.Client
    monkeypatch.setattr(reporting, "_client", None)
    monkeypatch.setenv("SENTRY_DSN", "https://public@example.invalid/1")
    monkeypatch.setenv("SENTRY_ENVIRONMENT", "test")
    monkeypatch.setenv("SENTRY_SPOTLIGHT", "https://unexpected.invalid/stream")
    monkeypatch.setenv("SENTRY_DEBUG", "true")
    monkeypatch.setattr(sentry_sdk, "Client", lambda **options: client_type(transport=transport, **options))
    assert reporting.configure_error_reporting(getattr(request, "param", "worker"))
    yield transport
    reporting._client.close()


def test_disabled_reporting_has_no_client_or_capture(monkeypatch):
    monkeypatch.delenv("SENTRY_DSN", raising=False)
    monkeypatch.setattr(reporting, "_client", None)
    assert not reporting.configure_error_reporting("api")
    assert reporting.report_error(ValueError(SECRET), stage="api") is None
    reporting.flush_error_reporting()
    assert reporting._client is None


def test_invalid_dsn_is_nonfatal_and_not_logged(monkeypatch, caplog):
    monkeypatch.setattr(reporting, "_client", None)
    monkeypatch.setenv("SENTRY_DSN", SECRET)
    assert not reporting.configure_error_reporting("api")
    assert SECRET not in caplog.text
    assert "invalid configuration" in caplog.text


def test_actual_envelope_excludes_exception_scope_and_sdk_context(captured):
    job = RuntimeJob(target_object_id=str(uuid4()), stage=Stage.REVIEW, payload={"manuscript": SECRET})
    with sentry_sdk.isolation_scope() as scope:
        scope.set_user({"email": SECRET})
        scope.set_extra("manuscript", SECRET)
        scope.set_tag("auth", SECRET)
        scope.set_context("prompt", {"text": SECRET})
        scope.add_attachment(bytes=SECRET.encode(), filename="private.txt")
        try:
            try:
                raise ValueError(SECRET)
            except ValueError as cause:
                raise RuntimeError(f"https://user:{SECRET}@host/path?token={SECRET}") from cause
        except RuntimeError as exc:
            event_id = reporting.report_error(exc, stage=Stage.REVIEW.value, job=job, failure_class=FailureClass.OTHER)
    assert event_id
    assert len(captured.events) == 1
    event = captured.events[0]
    assert event["event_id"] == event_id
    assert event["tags"] == {"service": "worker", "stage": Stage.REVIEW.value, "job_id": job.id,
                             "target_id": job.target_object_id, "failure_class": "other"}
    assert event["release"].startswith("researka-core@")
    assert event["environment"] == "test"
    assert event["exception"]["values"][0]["value"] == "[redacted]"
    assert not ({"request", "user", "extra", "contexts", "breadcrumbs", "server_name", "sdk"} & event.keys())
    wire = b"\n".join(envelope.serialize() for envelope in captured.envelopes)
    assert SECRET.encode() not in wire
    assert b"attachment" not in wire
    assert reporting._client.integrations == {}
    assert reporting._client.spotlight is None
    assert reporting._client.options["debug"] is False


@pytest.mark.parametrize("captured", ["api"], indirect=True)
def test_api_real_unhandled_error_is_captured_but_handled_response_is_not(captured):
    app = create_app(InMemoryRuntimeRepository())

    @app.post("/test-error")
    async def crash(request: Request):
        body = await request.body()
        raise RuntimeError(body.decode() + str(request.url) + request.headers["authorization"])

    @app.get("/test-handled")
    def handled():
        raise HTTPException(422, SECRET)

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(f"/test-error?secret={SECRET}", content=SECRET, headers={"authorization": SECRET})
        assert response.status_code == 500
        assert client.get("/test-handled").status_code == 422
    assert len(captured.events) == 1
    assert captured.events[0]["tags"]["service"] == "api"
    assert SECRET not in json.dumps(captured.events)
    frames = captured.events[0]["exception"]["values"][0]["stacktrace"]["frames"]
    assert frames and all(frame["filename"].startswith(("apps/", "runtime_core/", "contracts/")) for frame in frames)
    assert all(set(frame) == {"filename", "lineno", "in_app"} for frame in frames)


@pytest.mark.parametrize("reason", ["structure_gate:missing", "quality_gate_failed", "review_disagreement:revise_reject"])
def test_domain_failures_are_not_reported(captured, reason):
    class Engine:
        def handle_job(self, job, repository):
            raise ValueError(reason)

    repo = InMemoryRuntimeRepository()
    repo.enqueue_job(RuntimeJob(target_object_id=str(uuid4()), stage=Stage.REVIEW))
    assert WorkerApp(repo, engine=Engine()).run_once()["failed"] == 1
    assert not captured.events


@pytest.mark.parametrize("decision", [Decision.REVISE, Decision.REJECT])
def test_normal_review_outcomes_are_not_reported(captured, decision):
    class Engine:
        def handle_job(self, job, repository):
            return {"decision": decision.value}

    repo = InMemoryRuntimeRepository()
    repo.enqueue_job(RuntimeJob(target_object_id=str(uuid4()), stage=Stage.REVIEW))
    assert WorkerApp(repo, engine=Engine()).run_once()["completed"] == 1
    assert not captured.events


def test_retry_emits_only_terminal_failure_with_safe_job_metadata(captured, monkeypatch):
    monkeypatch.setenv("RESEARKA_V2_REVIEW_JOB_MAX_RETRIES", "1")
    monkeypatch.setenv("RESEARKA_V2_REVIEW_JOB_RETRY_BACKOFF_SEC", "0")

    class Engine:
        def handle_job(self, job, repository):
            raise RuntimeError("provider_error:" + SECRET)

    repo = InMemoryRuntimeRepository()
    repo.enqueue_job(RuntimeJob(target_object_id=str(uuid4()), stage=Stage.REVIEW))
    worker = WorkerApp(repo, engine=Engine())
    assert worker.run_once()["retried"] == 1
    assert not captured.events
    assert worker.run_once()["retried"] == 0
    assert len(captured.events) == 1
    assert captured.events[0]["tags"]["failure_class"] == "provider_error"
    assert SECRET not in json.dumps(captured.events)


def test_capture_failure_cannot_change_worker_result(captured, monkeypatch):
    def broken_transport(_envelope):
        raise RuntimeError("offline")

    monkeypatch.setattr(captured, "capture_envelope", broken_transport)
    assert reporting.report_error(RuntimeError(SECRET), stage="worker_loop") is None
    class Engine:
        def handle_job(self, job, repository):
            raise RuntimeError(SECRET)

    repo = InMemoryRuntimeRepository()
    repo.enqueue_job(RuntimeJob(target_object_id=str(uuid4()), stage=Stage.REVIEW))
    result = WorkerApp(repo, engine=Engine()).run_once()
    assert result["failed"] == 1
    assert result["retried"] == 0


def test_worker_startup_failure_is_captured_and_reraised(captured, monkeypatch):
    monkeypatch.setattr(loop, "postgres_dsn_from_env", lambda: None)
    with pytest.raises(RuntimeError, match="postgres_dsn_required"):
        loop.run()
    assert len(captured.events) == 1
    assert captured.events[0]["tags"]["stage"] == "worker_startup"


@pytest.mark.parametrize("captured", ["api"], indirect=True)
def test_api_startup_failure_is_captured_before_middleware_exists(captured, monkeypatch):
    monkeypatch.setenv("RESEARKA_V2_ENV", "production")
    monkeypatch.setattr(runtime_api, "postgres_dsn_from_env", lambda: None)
    reporting._client.close()
    monkeypatch.setattr(reporting, "_client", None)
    with pytest.raises(RuntimeError, match="postgres_dsn_required"):
        create_app()
    assert len(captured.events) == 1
    assert captured.events[0]["tags"] == {"service": "api", "stage": "api_startup"}


def test_untrusted_identifiers_and_exception_type_are_not_sent(captured):
    error_type = type(SECRET, (Exception,), {})
    job = RuntimeJob(target_object_id=SECRET, stage=Stage.REVIEW)
    reporting.report_error(error_type(SECRET), stage=SECRET, job=job)
    event = captured.events[0]
    assert event["exception"]["values"][0]["type"] == "ApplicationError"
    assert event["tags"]["stage"] == "unknown"
    assert "target_id" not in event["tags"]
    assert SECRET not in json.dumps(event)
