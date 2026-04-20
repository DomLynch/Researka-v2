from __future__ import annotations

from fastapi import FastAPI, HTTPException

from apps.worker.main import WorkerApp
from contracts import ObjectType, ResearchObject, RuntimeJob, Stage, SubmissionPayload
from runtime_core import InMemoryRuntimeRepository, WorkflowEngine
from runtime_core.repos import RuntimeRepository


def create_app(repository: RuntimeRepository | None = None) -> FastAPI:
    repo = repository or InMemoryRuntimeRepository()
    app = FastAPI(title="Researka v2 Runtime API")
    app.state.repository = repo
    app.state.engine = WorkflowEngine()
    app.state.worker = WorkerApp(repo, worker_id="api-worker", engine=app.state.engine)

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
    def submit(payload: SubmissionPayload) -> dict:
        submission = app.state.repository.create_object(
            ResearchObject(
                object_type=ObjectType.SUBMISSION,
                title=payload.title,
                body_markdown=payload.abstract,
                metadata=payload.model_dump(mode="json"),
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

    @app.get("/submissions/{submission_id}")
    def get_submission(submission_id: str) -> dict:
        submission = app.state.repository.get_object(submission_id)
        if submission is None or submission.object_type != ObjectType.SUBMISSION:
            raise HTTPException(status_code=404, detail="submission_not_found")
        return submission.model_dump(mode="json")

    @app.post("/jobs/run-once")
    def run_one_job() -> dict:
        return app.state.worker.run_once()

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

    return app


app = create_app()
