import threading
from datetime import datetime, timezone

import pytest
from cryptography.fernet import Fernet

from contracts import (
    ClaimCard,
    ContradictionStatus,
    EvidenceGrade,
    EventType,
    FailureClass,
    JobStatus,
    ObjectType,
    ResearchObject,
    RuntimeJob,
    Stage,
)
from runtime_core.repos import (
    InMemoryRuntimeRepository,
    PostgresRuntimeRepository,
    _decode_osf_token_metadata,
    _encode_osf_token_metadata,
    _postgres_connect_timeout_seconds,
    postgres_dsn_from_env,
    postgres_runtime_available,
)


def _sample_claim(
    publication_id: str, claim_text: str = "Metformin extends median lifespan in mice."
) -> ClaimCard:
    return ClaimCard(
        publication_id=publication_id,
        claim_text=claim_text,
        evidence_grade=EvidenceGrade.VERIFIED,
        citation_support=[
            {
                "source_id": "src-1",
                "quote": "5.83% extension",
                "dw_chain_ref": "dw://chain/abc",
            }
        ],
        contradiction_status=ContradictionStatus.NONE,
        source_ids=["src-1"],
        dw_chain_url="https://provenance.researka.org/chain/abc",
    )


def test_postgres_connect_has_bounded_timeout(monkeypatch) -> None:
    # Connections come from one bounded pool per process. The env-configured
    # connect timeout must reach every pooled connection, the pool acquire wait
    # must be bounded too, and the pool must be capped.
    built: list[dict[str, object]] = []

    class _FakePool:
        def __init__(self, dsn: str, **kwargs: object) -> None:
            built.append({"dsn": dsn, **kwargs})

        def connection(self) -> object:
            return object()

    import psycopg_pool

    monkeypatch.setenv("RESEARKA_V2_POSTGRES_CONNECT_TIMEOUT_SEC", "7")
    monkeypatch.setenv("RESEARKA_V2_POSTGRES_POOL_MAX", "3")
    monkeypatch.setattr(psycopg_pool, "ConnectionPool", _FakePool)
    monkeypatch.setattr(PostgresRuntimeRepository, "_ensure_schema", lambda self: None)

    repo = PostgresRuntimeRepository("postgresql://example")
    repo._connect()

    assert len(built) == 1
    pool = built[0]
    assert pool["dsn"] == "postgresql://example"
    assert pool["kwargs"] == {"row_factory": repo._dict_row, "connect_timeout": 7}
    assert pool["timeout"] == 7.0
    assert pool["min_size"] == 1
    assert pool["max_size"] == 3
    assert pool["open"] is True

def test_postgres_operation_lookup_casts_text_payload_to_jsonb(monkeypatch) -> None:
    calls: list[tuple[str, tuple[object, ...]]] = []

    class _Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, query: str, params: tuple[object, ...]) -> None:
            calls.append((query, params))

        def fetchone(self) -> None:
            return None

    class _Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def cursor(self) -> _Cursor:
            return _Cursor()

    repo = PostgresRuntimeRepository.__new__(PostgresRuntimeRepository)
    monkeypatch.setattr(repo, "_connect", lambda: _Connection())
    job = RuntimeJob(
        target_object_id="obj-reprocess",
        stage=Stage.INTAKE,
        payload={"operation_id": "attempt-2"},
    )

    assert repo._existing_job_for_stage(job) is None
    query, params = calls[0]
    assert "(payload::jsonb)->>'operation_id'" in query
    assert params == (job.target_object_id, Stage.INTAKE.value, "attempt-2", "attempt-2")


def test_postgres_object_row_accepts_decoded_json_metadata() -> None:
    repo = PostgresRuntimeRepository.__new__(PostgresRuntimeRepository)

    obj = repo._object_from_row(
        {
            "id": "summary-1",
            "object_type": ObjectType.SUBMISSION.value,
            "parent_object_id": None,
            "title": "Summary",
            "body_markdown": "",
            "metadata": {},
            "created_at": datetime.now(timezone.utc),
        }
    )

    assert obj is not None and obj.metadata == {}


def test_production_schema_migration_failure_is_fatal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, _query: str) -> None:
            return None

        def fetchone(self) -> dict[str, bool]:
            return {"exists": True}

    class _Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def cursor(self) -> _Cursor:
            return _Cursor()

    repo = PostgresRuntimeRepository.__new__(PostgresRuntimeRepository)
    monkeypatch.setenv("RESEARKA_V2_ENV", "production")
    monkeypatch.setattr(repo, "_connect", lambda: _Connection())
    monkeypatch.setattr(
        repo,
        "_auto_migrate",
        lambda: (_ for _ in ()).throw(RuntimeError("migration failed")),
    )

    with pytest.raises(RuntimeError, match="runtime_schema_migration_failed"):
        repo._alembic_manages_schema()


def test_production_never_falls_back_to_raw_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = PostgresRuntimeRepository.__new__(PostgresRuntimeRepository)
    monkeypatch.setenv("RESEARKA_V2_ENV", "production")
    monkeypatch.setattr(repo, "_alembic_manages_schema", lambda: False)
    monkeypatch.setattr(
        repo,
        "_create_tables_raw",
        lambda: pytest.fail("raw schema fallback called"),
    )

    with pytest.raises(RuntimeError, match="alembic_schema_required_in_production"):
        repo._ensure_schema()


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
    assert any(
        event.payload.get("lease_reclaimed") is True for event in repo.list_events()
    )
    assert repo.list_events()[-1].event_type == EventType.JOB_LEASED


def test_inmemory_fencing_rejects_stale_worker_after_reclaim() -> None:
    repo = InMemoryRuntimeRepository(lease_ttl_seconds=-1)
    job = repo.enqueue_job(
        RuntimeJob(target_object_id="obj-fenced", stage=Stage.REVIEW)
    )
    first = repo.claim_next_job()
    assert first is not None
    first_token = first.lease_token
    second = repo.claim_next_job()

    assert second is not None
    assert second.id == job.id
    assert second.lease_token == first_token + 1
    with pytest.raises(RuntimeError, match="stale_job_lease"):
        repo.complete_job(job.id, lease_token=first_token)
    with pytest.raises(RuntimeError, match="stale_job_lease"):
        repo.fail_job(job.id, reason="late failure", lease_token=first_token)

    repo.complete_job(job.id, lease_token=second.lease_token)
    completed = repo.get_job(job.id)
    assert completed is not None and completed.status == JobStatus.COMPLETED


def test_inmemory_renews_only_current_lease() -> None:
    repo = InMemoryRuntimeRepository(lease_ttl_seconds=30)
    job = repo.enqueue_job(RuntimeJob(target_object_id="obj-renew", stage=Stage.REVIEW))
    claimed = repo.claim_next_job()

    assert claimed is not None
    before = claimed.lease_expires_at
    assert repo.renew_job_lease(job.id, claimed.lease_token) is True
    renewed = repo.get_job(job.id)
    assert (
        before is not None
        and renewed is not None
        and renewed.lease_expires_at is not None
    )
    assert renewed.lease_expires_at >= before
    assert repo.renew_job_lease(job.id, claimed.lease_token + 1) is False


def test_inmemory_creates_submission_job_and_queue_event_together() -> None:
    repo = InMemoryRuntimeRepository()
    submission = ResearchObject(
        object_type=ObjectType.SUBMISSION, title="Atomic submission"
    )
    job = RuntimeJob(target_object_id=submission.id, stage=Stage.INTAKE)

    stored_submission, stored_job = repo.create_object_and_enqueue_job(submission, job)

    assert repo.get_object(stored_submission.id) == stored_submission
    assert repo.get_job(stored_job.id) == stored_job
    assert repo.list_events()[-1].event_type == EventType.JOB_QUEUED


def test_inmemory_atomic_create_rolls_back_if_enqueue_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = InMemoryRuntimeRepository()
    submission = ResearchObject(
        object_type=ObjectType.SUBMISSION, title="Atomic rollback"
    )
    job = RuntimeJob(target_object_id=submission.id, stage=Stage.INTAKE)

    def fail_enqueue(_job: RuntimeJob) -> RuntimeJob:
        raise RuntimeError("queue unavailable")

    monkeypatch.setattr(repo, "enqueue_job", fail_enqueue)

    with pytest.raises(RuntimeError, match="queue unavailable"):
        repo.create_object_and_enqueue_job(submission, job)

    assert repo.get_object(submission.id) is None


def test_inmemory_enqueue_is_idempotent_until_stage_fails() -> None:
    repo = InMemoryRuntimeRepository()
    first = repo.enqueue_job(
        RuntimeJob(target_object_id="obj-idempotent", stage=Stage.REVIEW)
    )

    assert (
        repo.enqueue_job(
            RuntimeJob(target_object_id="obj-idempotent", stage=Stage.REVIEW)
        )
        == first
    )
    repo.fail_job(
        first.id, reason="provider_error", failure_class=FailureClass.PROVIDER_ERROR
    )
    retry = repo.enqueue_job(
        RuntimeJob(target_object_id="obj-idempotent", stage=Stage.REVIEW)
    )

    assert retry.id != first.id
    assert len(repo.jobs) == 2


def test_inmemory_new_operation_can_reprocess_completed_stage() -> None:
    repo = InMemoryRuntimeRepository()
    first = repo.enqueue_job(
        RuntimeJob(
            target_object_id="obj-reprocess",
            stage=Stage.INTAKE,
            payload={"operation_id": "attempt-1"},
        )
    )
    repo.complete_job(first.id)

    same = repo.enqueue_job(
        RuntimeJob(
            target_object_id="obj-reprocess",
            stage=Stage.INTAKE,
            payload={"operation_id": "attempt-1"},
        )
    )
    second = repo.enqueue_job(
        RuntimeJob(
            target_object_id="obj-reprocess",
            stage=Stage.INTAKE,
            payload={"operation_id": "attempt-2"},
        )
    )

    assert same.id == first.id
    assert second.id != first.id


def test_existing_publication_does_not_bypass_publish_authorization() -> None:
    repo = InMemoryRuntimeRepository()
    submission = repo.create_object(
        ResearchObject(object_type=ObjectType.SUBMISSION, title="Submission")
    )
    repo.create_object(
        ResearchObject(
            object_type=ObjectType.PUBLICATION,
            parent_object_id=submission.id,
            title="Existing publication",
        )
    )

    job = repo.enqueue_job(
        RuntimeJob(target_object_id=submission.id, stage=Stage.PUBLISH)
    )

    assert job.status == JobStatus.QUEUED


def test_inmemory_fail_job_persists_failure_class() -> None:
    repo = InMemoryRuntimeRepository()
    job = repo.enqueue_job(RuntimeJob(target_object_id="obj-2", stage=Stage.REVIEW))
    claimed = repo.claim_next_job()
    assert claimed is not None
    repo.fail_job(
        job.id,
        reason="structure_gate: missing conclusion",
        failure_class=FailureClass.STRUCTURE_GATE,
    )
    failed = repo.get_job(job.id)
    assert failed is not None
    assert failed.payload["failure_reason"] == "structure_gate: missing conclusion"
    assert failed.payload["failure_class"] == FailureClass.STRUCTURE_GATE.value
    assert failed.lease_expires_at is None


def test_osf_token_metadata_encryption_roundtrip(monkeypatch) -> None:
    monkeypatch.setenv(
        "RESEARKA_V2_OSF_TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii")
    )

    encoded = _encode_osf_token_metadata(
        {"access_token": "access-secret", "refresh_token": "refresh-secret"}
    )

    assert encoded.startswith("fernet:v1:")
    assert "access-secret" not in encoded
    assert "refresh-secret" not in encoded
    assert _decode_osf_token_metadata(encoded) == {
        "access_token": "access-secret",
        "refresh_token": "refresh-secret",
    }


def test_osf_token_metadata_missing_encryption_key_file_fails(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv(
        "RESEARKA_V2_OSF_TOKEN_ENCRYPTION_KEY_PATH", str(tmp_path / "missing.key")
    )

    try:
        _encode_osf_token_metadata({"access_token": "access-secret"})
    except RuntimeError as exc:
        assert str(exc) == "researka_v2_osf_token_encryption_key_path_missing"
    else:
        raise AssertionError(
            "missing configured encryption key file should fail closed"
        )


def test_osf_token_metadata_empty_encryption_key_file_fails(
    monkeypatch, tmp_path
) -> None:
    key_path = tmp_path / "empty.key"
    key_path.write_text("\n")
    monkeypatch.setenv("RESEARKA_V2_OSF_TOKEN_ENCRYPTION_KEY_PATH", str(key_path))

    try:
        _encode_osf_token_metadata({"access_token": "access-secret"})
    except RuntimeError as exc:
        assert str(exc) == "researka_v2_osf_token_encryption_key_path_empty"
    else:
        raise AssertionError("empty configured encryption key file should fail closed")


def test_osf_token_metadata_missing_encryption_key_fails(monkeypatch) -> None:
    monkeypatch.delenv("RESEARKA_V2_OSF_TOKEN_ENCRYPTION_KEY", raising=False)
    monkeypatch.delenv("RESEARKA_V2_OSF_TOKEN_ENCRYPTION_KEY_PATH", raising=False)

    try:
        _encode_osf_token_metadata({"access_token": "access-secret"})
    except RuntimeError as exc:
        assert str(exc) == "researka_v2_osf_token_encryption_key_required"
    else:
        raise AssertionError("OSF OAuth tokens must not fall back to plaintext storage")


def test_postgres_claim_sets_lease_and_reclaims_expired_job() -> None:
    if not postgres_runtime_available():
        return
    dsn = postgres_dsn_from_env()
    assert dsn is not None
    repo = PostgresRuntimeRepository(dsn, lease_ttl_seconds=-1)
    repo.reset()
    job = repo.enqueue_job(RuntimeJob(target_object_id="obj-3", stage=Stage.INTAKE))
    claimed = repo.claim_next_job()
    assert claimed is not None
    assert claimed.id == job.id
    assert claimed.lease_expires_at is not None
    reclaimed = repo.claim_next_job()
    assert reclaimed is not None
    assert reclaimed.id == job.id
    assert any(
        event.payload.get("lease_reclaimed") is True for event in repo.list_events()
    )
    assert repo.list_events()[-1].event_type == EventType.JOB_LEASED


def test_postgres_atomic_create_rolls_back_if_job_insert_fails() -> None:
    if not postgres_runtime_available():
        return
    dsn = postgres_dsn_from_env()
    assert dsn is not None
    repo = PostgresRuntimeRepository(dsn)
    repo.reset()
    submission = ResearchObject(
        object_type=ObjectType.SUBMISSION, title="Atomic rollback"
    )
    job = RuntimeJob(target_object_id=submission.id, stage=Stage.INTAKE)
    repo.enqueue_job(job)

    with pytest.raises(repo._psycopg.errors.UniqueViolation):
        repo.create_object_and_enqueue_job(submission, job)

    assert repo.get_object(submission.id) is None
    assert repo.get_job(job.id) == job


def test_postgres_fail_job_persists_failure_class() -> None:
    if not postgres_runtime_available():
        return
    dsn = postgres_dsn_from_env()
    assert dsn is not None
    repo = PostgresRuntimeRepository(dsn)
    repo.reset()
    job = repo.enqueue_job(RuntimeJob(target_object_id="obj-4", stage=Stage.REVIEW))
    claimed = repo.claim_next_job()
    assert claimed is not None
    repo.fail_job(
        job.id,
        reason="structure_gate: missing conclusion",
        failure_class=FailureClass.STRUCTURE_GATE,
    )
    failed = repo.get_job(job.id)
    assert failed is not None
    assert failed.payload["failure_reason"] == "structure_gate: missing conclusion"
    assert failed.payload["failure_class"] == FailureClass.STRUCTURE_GATE.value
    assert failed.lease_expires_at is None


def test_postgres_single_job_cannot_be_double_claimed() -> None:
    if not postgres_runtime_available():
        return
    dsn = postgres_dsn_from_env()
    assert dsn is not None
    setup_repo = PostgresRuntimeRepository(dsn)
    setup_repo.reset()
    job = setup_repo.enqueue_job(
        RuntimeJob(target_object_id="obj-5", stage=Stage.REVIEW)
    )

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


def test_inmemory_claim_card_roundtrip_orders_by_created_at() -> None:
    repo = InMemoryRuntimeRepository()
    first = repo.save_claim_card(_sample_claim("pub-1", claim_text="First claim"))
    second = repo.save_claim_card(_sample_claim("pub-1", claim_text="Second claim"))
    repo.save_claim_card(_sample_claim("pub-other", claim_text="Different pub"))

    listed = repo.list_claim_cards("pub-1")

    assert [c.id for c in listed] == [first.id, second.id]
    assert listed[0].claim_text == "First claim"
    assert listed[0].evidence_grade == EvidenceGrade.VERIFIED
    assert listed[0].citation_support == [
        {
            "source_id": "src-1",
            "quote": "5.83% extension",
            "dw_chain_ref": "dw://chain/abc",
        }
    ]
    assert listed[0].contradiction_status == ContradictionStatus.NONE
    assert listed[0].source_ids == ["src-1"]
    assert listed[0].dw_chain_url == "https://provenance.researka.org/chain/abc"


def test_inmemory_list_claim_cards_returns_empty_for_unknown_publication() -> None:
    """Discriminating test (V4): an existing publication with no claims must
    return an empty list, NOT raise / 404. Storage layer cannot couple claim
    presence to publication existence — that's the API layer's job."""
    repo = InMemoryRuntimeRepository()
    repo.save_claim_card(_sample_claim("pub-1"))

    assert repo.list_claim_cards("pub-with-no-claims") == []


def test_postgres_claim_card_roundtrip_orders_by_created_at() -> None:
    if not postgres_runtime_available():
        return
    dsn = postgres_dsn_from_env()
    assert dsn is not None
    repo = PostgresRuntimeRepository(dsn)
    repo.reset()

    first = repo.save_claim_card(_sample_claim("pub-pg-1", claim_text="First pg claim"))
    second = repo.save_claim_card(
        _sample_claim("pub-pg-1", claim_text="Second pg claim")
    )
    repo.save_claim_card(_sample_claim("pub-pg-other", claim_text="Different pg pub"))

    listed = repo.list_claim_cards("pub-pg-1")

    assert [c.id for c in listed] == [first.id, second.id]
    assert listed[0].evidence_grade == EvidenceGrade.VERIFIED
    assert listed[0].citation_support == [
        {
            "source_id": "src-1",
            "quote": "5.83% extension",
            "dw_chain_ref": "dw://chain/abc",
        }
    ]
    assert listed[0].source_ids == ["src-1"]
    assert listed[0].contradiction_status == ContradictionStatus.NONE
    assert listed[0].dw_chain_url == "https://provenance.researka.org/chain/abc"


def test_postgres_list_claim_cards_returns_empty_for_unknown_publication() -> None:
    if not postgres_runtime_available():
        return
    dsn = postgres_dsn_from_env()
    assert dsn is not None
    repo = PostgresRuntimeRepository(dsn)
    repo.reset()

    assert repo.list_claim_cards("pub-pg-with-no-claims") == []
