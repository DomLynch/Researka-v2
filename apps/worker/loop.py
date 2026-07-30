from __future__ import annotations

import json
import os
import signal
import time

from apps.worker.main import WorkerApp
from runtime_core import WorkflowEngine
from runtime_core.ops import operational_alerts, reconcile_stalled_submissions
from runtime_core.osf import warn_if_osf_default_owner_missing
from runtime_core.repos import PostgresRuntimeRepository, postgres_dsn_from_env


def _sleep_seconds(env_name: str, default: float) -> float:
    raw = os.environ.get(env_name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return max(value, 0.1)


def main() -> None:
    warn_if_osf_default_owner_missing()
    dsn = postgres_dsn_from_env()
    if not dsn:
        raise RuntimeError("researka_v2_postgres_dsn_required_for_worker")

    repository = PostgresRuntimeRepository(dsn)
    worker = WorkerApp(
        repository,
        worker_id=os.environ.get("RESEARKA_V2_WORKER_ID", "researka-v2-worker"),
        engine=WorkflowEngine(),
    )
    idle_sleep = _sleep_seconds("RESEARKA_V2_WORKER_IDLE_SLEEP_SEC", 5.0)
    error_sleep = _sleep_seconds("RESEARKA_V2_WORKER_ERROR_SLEEP_SEC", 10.0)
    maintenance_interval = _sleep_seconds("RESEARKA_V2_MAINTENANCE_INTERVAL_SEC", 60.0)
    next_maintenance = 0.0
    should_stop = False

    def _stop(_signum: int, _frame: object) -> None:
        nonlocal should_stop
        should_stop = True

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    while not should_stop:
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
                print(json.dumps({"event": "worker_maintenance_error", "error": str(exc)}, sort_keys=True), flush=True)
            next_maintenance = time.monotonic() + maintenance_interval
        try:
            result = worker.run_once()
            print(json.dumps({"event": "worker_run_once", **result}, sort_keys=True), flush=True)
            if not result.get("claimed"):
                time.sleep(idle_sleep)
        except Exception as exc:
            print(json.dumps({"event": "worker_error", "error": str(exc)}, sort_keys=True), flush=True)
            time.sleep(error_sleep)


if __name__ == "__main__":
    main()
