from __future__ import annotations

import os
from typing import Protocol

from contracts import EventType, FailureClass, RuntimeEvent, RuntimeJob, Stage
from runtime_core import InMemoryRuntimeRepository, WorkflowEngine
from runtime_core.ops import classify_failure
from runtime_core.repos import RuntimeRepository


class JobEngine(Protocol):
    def handle_job(self, job: RuntimeJob, repository: RuntimeRepository) -> dict: ...


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

    def run_once(self, *, target_object_id: str | None = None) -> dict:
        job = self.repository.claim_next_job(target_object_id=target_object_id)
        if not job:
            return {"claimed": 0, "completed": 0, "failed": 0}

        self.repository.record_event(
            RuntimeEvent(
                event_type=EventType.JOB_LEASED,
                target_object_id=job.target_object_id,
                job_id=job.id,
                worker_id=self.worker_id,
                payload={"stage": job.stage.value},
            )
        )
        try:
            result = self.engine.handle_job(job, self.repository)
            self.repository.complete_job(job.id)
            self.repository.record_event(
                RuntimeEvent(
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
            failure_class = classify_failure(str(exc))
            self.repository.fail_job(job.id, reason=str(exc), failure_class=failure_class)
            retry_job = None
            try:
                retry_count = max(0, int(job.payload.get("provider_retry_count", 0) or 0))
            except (TypeError, ValueError):
                retry_count = 0
            try:
                retry_limit = min(10, max(0, int(os.getenv("RESEARKA_V2_REVIEW_JOB_MAX_RETRIES", "2"))))
            except ValueError:
                retry_limit = 2
            retry_error = None
            if job.stage == Stage.REVIEW and failure_class == FailureClass.PROVIDER_ERROR and retry_count < retry_limit:
                try:
                    retry_job = self.repository.enqueue_job(RuntimeJob(
                        target_object_id=job.target_object_id,
                        stage=job.stage,
                        payload={
                            **job.payload,
                            "provider_retry_count": retry_count + 1,
                            "retry_of_job_id": job.id,
                        },
                    ))
                except Exception as retry_exc:
                    retry_error = retry_exc
            failure_reason = str(exc)
            if retry_error is not None:
                failure_reason = f"{failure_reason}; retry_enqueue_failed: {retry_error}"
                self.repository.fail_job(job.id, reason=failure_reason, failure_class=failure_class)
            self.repository.record_event(RuntimeEvent(
                event_type=EventType.JOB_FAILED,
                target_object_id=job.target_object_id,
                job_id=job.id,
                worker_id=self.worker_id,
                payload={
                    "stage": job.stage.value,
                    "reason": failure_reason,
                    "failure_class": failure_class.value,
                    "terminal": retry_job is None,
                },
            ))
            if retry_job is not None:
                self.repository.record_event(RuntimeEvent(
                    event_type=EventType.JOB_QUEUED,
                    target_object_id=job.target_object_id,
                    job_id=retry_job.id,
                    worker_id=self.worker_id,
                    payload={
                        "stage": job.stage.value,
                        "provider_retry_count": retry_count + 1,
                        "retry_of_job_id": job.id,
                    },
                ))
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


if __name__ == "__main__":
    print(WorkerApp(InMemoryRuntimeRepository()).run_once())
