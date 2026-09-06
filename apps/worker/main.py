from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from threading import Event, Thread
from typing import Protocol

from contracts import EventType, FailureClass, RuntimeEvent, RuntimeJob, Stage
from runtime_core import InMemoryRuntimeRepository, WorkflowEngine
from runtime_core.ops import classify_failure
from runtime_core.repos import RuntimeRepository


class JobEngine(Protocol):
    def handle_job(self, job: RuntimeJob, repository: RuntimeRepository) -> dict: ...


def _retry_not_before(retry_count: int) -> str:
    try:
        base = max(0.0, float(os.getenv("RESEARKA_V2_REVIEW_JOB_RETRY_BACKOFF_SEC", "15")))
        cap = max(base, float(os.getenv("RESEARKA_V2_REVIEW_JOB_RETRY_BACKOFF_CAP_SEC", "300")))
    except ValueError:
        base, cap = 15.0, 300.0
    delay = min(cap, base * (2**retry_count))
    return (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()


class WorkerApp:
    def __init__(
        self,
        repository: RuntimeRepository,
        *,
        worker_id: str = "worker-1",
        engine: JobEngine | None = None,
    ) -> None:
        self.repository = repository
        self.worker_id = worker_id
        self.engine = engine or WorkflowEngine()

    def _lease_heartbeat(self, job: RuntimeJob, stop: Event, lost: Event) -> None:
        ttl = max(0.3, float(getattr(self.repository, "lease_ttl_seconds", 300)))
        try:
            configured = float(os.getenv("RESEARKA_V2_WORKER_HEARTBEAT_SEC", str(ttl / 3)))
        except ValueError:
            configured = ttl / 3
        interval = max(0.05, min(configured, ttl / 3))
        while not stop.wait(interval):
            try:
                if not self.repository.renew_job_lease(job.id, job.lease_token):
                    lost.set()
                    return
            except Exception:
                lost.set()
                return

    def run_once(self, *, target_object_id: str | None = None) -> dict:
        job = self.repository.claim_next_job(target_object_id=target_object_id, worker_id=self.worker_id)
        if not job:
            return {"claimed": 0, "completed": 0, "failed": 0}
        heartbeat_stop = Event()
        lease_lost = Event()
        heartbeat = Thread(
            target=self._lease_heartbeat,
            args=(job, heartbeat_stop, lease_lost),
            name=f"lease-{job.id}",
            daemon=True,
        )
        heartbeat.start()
        try:
            result = self.engine.handle_job(job, self.repository)
            if lease_lost.is_set():
                raise RuntimeError("stale_job_lease")
            self.repository.complete_job(
                job.id,
                lease_token=job.lease_token,
                event=RuntimeEvent(
                    event_type=EventType.JOB_COMPLETED,
                    target_object_id=job.target_object_id,
                    job_id=job.id,
                    worker_id=self.worker_id,
                    payload={"stage": job.stage.value, **result},
                )
            )
            return {
                "claimed": 1,
                "completed": 1,
                "failed": 0,
                "target_object_id": job.target_object_id,
                "stage": job.stage.value,
                "job_id": job.id,
            }
        except Exception as exc:
            if lease_lost.is_set() or str(exc) == "stale_job_lease":
                return {
                    "claimed": 1,
                    "completed": 0,
                    "failed": 0,
                    "lease_lost": 1,
                    "target_object_id": job.target_object_id,
                    "stage": job.stage.value,
                    "job_id": job.id,
                }
            failure_ts = datetime.now(timezone.utc)
            failure_class = classify_failure(str(exc))
            failure_reason = str(exc)
            try:
                retry_count = max(0, int(job.payload.get("provider_retry_count", 0) or 0))
            except (TypeError, ValueError):
                retry_count = 0
            try:
                retry_limit = min(10, max(0, int(os.getenv("RESEARKA_V2_REVIEW_JOB_MAX_RETRIES", "2"))))
            except ValueError:
                retry_limit = 2
            retry_eligible = (
                failure_class in {FailureClass.PROVIDER_ERROR, FailureClass.SYSTEM_UNAVAILABLE}
                and job.stage in {
                    Stage.INTAKE,
                    Stage.REVIEW,
                    Stage.PUBLISH,
                    Stage.OSF_DEPOSIT,
                    Stage.DW_DELIVERY,
                    Stage.PUBLICATION_FINALIZE,
                }
                and retry_count < retry_limit
                and not failure_reason.startswith("provider_error:billing:")
            )
            self.repository.fail_job(
                job.id,
                reason=failure_reason,
                failure_class=failure_class,
                lease_token=job.lease_token,
                event=RuntimeEvent(
                    event_type=EventType.JOB_FAILED,
                    target_object_id=job.target_object_id,
                    job_id=job.id,
                    worker_id=self.worker_id,
                    payload={
                        "stage": job.stage.value,
                        "reason": failure_reason,
                        "failure_class": failure_class.value,
                        "terminal": not retry_eligible,
                    },
                    ts=failure_ts,
                ),
            )
            retry_job = None
            retry_error = None
            if retry_eligible:
                try:
                    retry_job = self.repository.enqueue_job(RuntimeJob(
                        target_object_id=job.target_object_id,
                        stage=job.stage,
                        payload={
                            **job.payload,
                            "operation_id": job.payload.get("operation_id") or job.id,
                            "provider_retry_count": retry_count + 1,
                            "retry_of_job_id": job.id,
                            "retry_not_before": _retry_not_before(retry_count),
                        },
                    ))
                except Exception as retry_exc:
                    retry_error = retry_exc
            if retry_error is not None:
                failure_reason = f"{failure_reason}; retry_enqueue_failed: {retry_error}"
                self.repository.fail_job(
                    job.id,
                    reason=failure_reason,
                    failure_class=failure_class,
                    lease_token=job.lease_token,
                    event=RuntimeEvent(
                        event_type=EventType.JOB_FAILED,
                        target_object_id=job.target_object_id,
                        job_id=job.id,
                        worker_id=self.worker_id,
                        payload={
                            "stage": job.stage.value,
                            "reason": failure_reason,
                            "failure_class": failure_class.value,
                            "terminal": True,
                        },
                    ),
                )
            return {
                "claimed": 1,
                "completed": 0,
                "failed": 1,
                "target_object_id": job.target_object_id,
                "stage": job.stage.value,
                "job_id": job.id,
                "retried": int(retry_job is not None),
                "retry_job_id": retry_job.id if retry_job else None,
            }
        finally:
            heartbeat_stop.set()
            heartbeat.join(timeout=1)


if __name__ == "__main__":
    print(WorkerApp(InMemoryRuntimeRepository()).run_once())
