from __future__ import annotations

import json
import os
import signal
import time
from threading import Event

from apps.worker.main import WorkerApp
from runtime_core import WorkflowEngine
from runtime_core.derivation_web import is_configured as derivation_web_configured
from runtime_core.error_reporting import configure_error_reporting, flush_error_reporting, report_error
from runtime_core.ops import operational_alerts, reconcile_stalled_submissions
from runtime_core.osf import (
    config_from_env as osf_service_config,
    oauth_config_from_env,
    warn_if_osf_default_owner_missing,
)
from runtime_core.repos import (
    PostgresRuntimeRepository,
    RuntimeRepository,
    postgres_dsn_from_env,
)


def _sleep_seconds(env_name: str, default: float) -> float:
    raw = os.environ.get(env_name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return max(value, 0.1)


def _assert_production_dependencies(repository: RuntimeRepository) -> None:
    if os.getenv("RESEARKA_V2_ENV", "development").strip().lower() != "production":
        return
    if not os.getenv("RESEARKA_INTEGRITY_API_KEY"):
        raise RuntimeError("integrity_credential_required_in_production")
    if not derivation_web_configured():
        raise RuntimeError("derivation_web_required_in_production")
    if osf_service_config() is not None:
        return
    default_agent = os.getenv("RESEARKA_V2_OSF_DEFAULT_AGENT_ID", "").strip()
    if (
        oauth_config_from_env() is None
        or not default_agent
        or not repository.get_osf_oauth_token(default_agent)
    ):
        raise RuntimeError("osf_delivery_required_in_production")


def main() -> None:
    warn_if_osf_default_owner_missing()
    dsn = postgres_dsn_from_env()
    if not dsn:
        raise RuntimeError("researka_v2_postgres_dsn_required_for_worker")

    repository = PostgresRuntimeRepository(dsn)
    _assert_production_dependencies(repository)
    worker = WorkerApp(
        repository,
        worker_id=os.environ.get("RESEARKA_V2_WORKER_ID", "researka-v2-worker"),
        engine=WorkflowEngine(),
    )
    idle_sleep = _sleep_seconds("RESEARKA_V2_WORKER_IDLE_SLEEP_SEC", 5.0)
    error_sleep = _sleep_seconds("RESEARKA_V2_WORKER_ERROR_SLEEP_SEC", 10.0)
    maintenance_interval = _sleep_seconds("RESEARKA_V2_MAINTENANCE_INTERVAL_SEC", 60.0)
    next_maintenance = 0.0
    stop_event = Event()

    def _stop(_signum: int, _frame: object) -> None:
        stop_event.set()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    while not stop_event.is_set():
        if time.monotonic() >= next_maintenance:
            try:
                repaired = reconcile_stalled_submissions(
                    repository,
                    stale_after_seconds=_sleep_seconds("RESEARKA_V2_RECONCILE_STALE_SEC", 120.0),
                )
                alerts = operational_alerts(
                    repository,
                    queue_age_seconds=_sleep_seconds("RESEARKA_V2_ALERT_QUEUE_AGE_SEC", 900.0),
                    failure_window_seconds=_sleep_seconds("RESEARKA_V2_ALERT_FAILURE_WINDOW_SEC", 3600.0),
                    failure_threshold=int(_sleep_seconds("RESEARKA_V2_ALERT_FAILURE_THRESHOLD", 3.0)),
                    publication_stall_seconds=_sleep_seconds("RESEARKA_V2_ALERT_PUBLICATION_STALL_SEC", 86400.0),
                )
                if repaired or alerts:
                    print(json.dumps({
                        "event": "worker_maintenance",
                        "reconciled_jobs": [job.id for job in repaired],
                        "alerts": alerts,
                    }, sort_keys=True), flush=True)
            except Exception as exc:
                report_error(exc, stage="maintenance")
                print(json.dumps({"event": "worker_maintenance_error", "error": str(exc)}, sort_keys=True), flush=True)
            next_maintenance = time.monotonic() + maintenance_interval
        try:
            result = worker.run_once()
            print(json.dumps({"event": "worker_run_once", **result}, sort_keys=True), flush=True)
            if not result.get("claimed"):
                stop_event.wait(idle_sleep)
        except Exception as exc:
            report_error(exc, stage="worker_loop")
            print(json.dumps({"event": "worker_error", "error": str(exc)}, sort_keys=True), flush=True)
            stop_event.wait(error_sleep)


def run() -> None:
    configure_error_reporting("worker")
    try:
        main()
    except Exception as exc:
        report_error(exc, stage="worker_startup")
        raise
    finally:
        flush_error_reporting()


if __name__ == "__main__":
    run()
