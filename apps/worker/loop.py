from __future__ import annotations

import json
import os
import signal
import time

from apps.worker.main import WorkerApp
from runtime_core import WorkflowEngine
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
    dsn = postgres_dsn_from_env()
    if not dsn:
        raise RuntimeError("researka_v2_postgres_dsn_required_for_worker")

    worker = WorkerApp(
        PostgresRuntimeRepository(dsn),
        worker_id=os.environ.get("RESEARKA_V2_WORKER_ID", "researka-v2-worker"),
        engine=WorkflowEngine(),
    )
    idle_sleep = _sleep_seconds("RESEARKA_V2_WORKER_IDLE_SLEEP_SEC", 5.0)
    error_sleep = _sleep_seconds("RESEARKA_V2_WORKER_ERROR_SLEEP_SEC", 10.0)
    should_stop = False

    def _stop(_signum: int, _frame: object) -> None:
        nonlocal should_stop
        should_stop = True

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    while not should_stop:
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
