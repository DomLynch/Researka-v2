import threading

from contracts import FailureClass, JobStatus, RuntimeJob, Stage
from runtime_core.repos import InMemoryRuntimeRepository, PostgresRuntimeRepository, postgres_dsn_from_env, postgres_runtime_available


def test_inmemory_claim_sets_lease_and_reclaims_expired_job() -> None:
    repo = InMemoryRuntimeRepository(lease_ttl_seconds=-1)
    job = repo.enqueue_job(RuntimeJob(target_object_id="obj-1", stage=Stage.INTAKE))
    claimed = repo.claim_next_job()
    assert claimed is not None
    assert claimed.id == job.id
    assert claimed.lease_expires_at is not None
    reclaimed = repo.claim_next_job()
    assert reclaimed is not None
    assert reclaimed.id == job.id


def test_inmemory_fail_job_persists_failure_class() -> None:
    repo = InMemoryRuntimeRepository()
    job = repo.enqueue_job(RuntimeJob(target_object_id="obj-2", stage=Stage.REVIEW))
    claimed = repo.claim_next_job()
    assert claimed is not None
    repo.fail_job(job.id, reason="structure_gate: missing conclusion", failure_class=FailureClass.STRUCTURE_GATE)
    failed = repo.get_job(job.id)
    assert failed.payload["failure_reason"] == "structure_gate: missing conclusion"
    assert failed.payload["failure_class"] == FailureClass.STRUCTURE_GATE.value
    assert failed.lease_expires_at is None


def test_postgres_claim_sets_lease_and_reclaims_expired_job() -> None:
    if not postgres_runtime_available():
        return
    repo = PostgresRuntimeRepository(postgres_dsn_from_env(), lease_ttl_seconds=-1)
    repo.reset()
    job = repo.enqueue_job(RuntimeJob(target_object_id="obj-3", stage=Stage.INTAKE))
    claimed = repo.claim_next_job()
    assert claimed is not None
    assert claimed.id == job.id
    assert claimed.lease_expires_at is not None
    reclaimed = repo.claim_next_job()
    assert reclaimed is not None
    assert reclaimed.id == job.id


def test_postgres_fail_job_persists_failure_class() -> None:
    if not postgres_runtime_available():
        return
    repo = PostgresRuntimeRepository(postgres_dsn_from_env())
    repo.reset()
    job = repo.enqueue_job(RuntimeJob(target_object_id="obj-4", stage=Stage.REVIEW))
    claimed = repo.claim_next_job()
    assert claimed is not None
    repo.fail_job(job.id, reason="structure_gate: missing conclusion", failure_class=FailureClass.STRUCTURE_GATE)
    failed = repo.get_job(job.id)
    assert failed.payload["failure_reason"] == "structure_gate: missing conclusion"
    assert failed.payload["failure_class"] == FailureClass.STRUCTURE_GATE.value
    assert failed.lease_expires_at is None


def test_postgres_single_job_cannot_be_double_claimed() -> None:
    if not postgres_runtime_available():
        return
    dsn = postgres_dsn_from_env()
    setup_repo = PostgresRuntimeRepository(dsn)
    setup_repo.reset()
    job = setup_repo.enqueue_job(RuntimeJob(target_object_id="obj-5", stage=Stage.REVIEW))

    repo_a = PostgresRuntimeRepository(dsn)
    repo_b = PostgresRuntimeRepository(dsn)
    barrier = threading.Barrier(3)
    results: list[str | None] = [None, None]
    errors: list[BaseException] = []

    def _claim(index: int, repo: PostgresRuntimeRepository) -> None:
        try:
            barrier.wait(timeout=5)
            claimed = repo.claim_next_job()
            results[index] = claimed.id if claimed is not None else None
        except BaseException as exc:  # pragma: no cover - test helper failure path
            errors.append(exc)

    thread_a = threading.Thread(target=_claim, args=(0, repo_a))
    thread_b = threading.Thread(target=_claim, args=(1, repo_b))
    thread_a.start()
    thread_b.start()
    barrier.wait(timeout=5)
    thread_a.join(timeout=5)
    thread_b.join(timeout=5)

    assert not errors
    claimed_ids = [claimed_id for claimed_id in results if claimed_id is not None]
    assert claimed_ids == [job.id]
    assert results.count(None) == 1

    persisted = setup_repo.get_job(job.id)
    assert persisted is not None
    assert persisted.id == job.id
    assert persisted.status == JobStatus.LEASED
    assert setup_repo.claim_next_job() is None
