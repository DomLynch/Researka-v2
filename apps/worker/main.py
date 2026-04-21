from __future__ import annotations

from contracts import EventType, RuntimeEvent
from runtime_core import InMemoryRuntimeRepository, WorkflowEngine
from runtime_core.ops import classify_failure
from runtime_core.repos import RuntimeRepository


class WorkerApp:
    def __init__(
        self,
        repository: RuntimeRepository,
        *,
        worker_id: str = "worker-1",
        engine: WorkflowEngine | None = None,
    ) -> None:
        self.repository = repository
        self.worker_id = worker_id
        self.engine = engine or WorkflowEngine()

    def run_once(self) -> dict:
        job = self.repository.claim_next_job()
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
            self.repository.record_event(
                RuntimeEvent(
                    event_type=EventType.JOB_FAILED,
                    target_object_id=job.target_object_id,
                    job_id=job.id,
                    worker_id=self.worker_id,
                    payload={
                        "stage": job.stage.value,
                        "reason": str(exc),
                        "failure_class": failure_class.value,
                    },
                )
            )
            return {
                "claimed": 1,
                "completed": 0,
                "failed": 1,
                "target_object_id": job.target_object_id,
                "stage": job.stage.value,
                "job_id": job.id,
            }


if __name__ == "__main__":
    print(WorkerApp(InMemoryRuntimeRepository()).run_once())
