import json
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

        @staticmethod
        def check_connection(_conn: object) -> None:
            return None

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
    # Health check on checkout: a DB restart must not hand out dead connections.
    assert pool["check"] is _FakePool.check_connection


def test_postgres_close_closes_pool_exactly_once(monkeypatch) -> None:
    class _FakePool:
        def __init__(self) -> None:
            self.close_calls = 0

        def close(self) -> None:
            self.close_calls += 1

    repo = PostgresRuntimeRepository.__new__(PostgresRuntimeRepository)
    pool = _FakePool()
    monkeypatch.setattr(repo, "_pool", pool, raising=False)
    repo._closed = False

    repo.close()
    repo.close()

    assert pool.close_calls == 1


def test_inmemory_close_is_a_no_op() -> None:
    InMemoryRuntimeRepository().close()


def test_api_shutdown_closes_internally_created_repository(monkeypatch) -> None:
    from fastapi.testclient import TestClient

    from apps.runtime_api.app import create_app

    closed: list[bool] = []

    class _Repo(InMemoryRuntimeRepository):
        def close(self) -> None:
            closed.append(True)

    monkeypatch.setenv("RESEARKA_V2_POSTGRES_DSN", "postgresql://example")
    monkeypatch.setattr("apps.runtime_api.app.PostgresRuntimeRepository", lambda _dsn: _Repo())

    app = create_app()
    with TestClient(app):
        pass

    assert closed == [True]


def test_api_shutdown_does_not_close_injected_repository() -> None:
    from fastapi.testclient import TestClient

    from apps.runtime_api.app import create_app

    closed: list[bool] = []

    class _Repo(InMemoryRuntimeRepository):
        def close(self) -> None:
            closed.append(True)

    with TestClient(create_app(_Repo())):
        pass

    assert closed == []

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
    assert [event.event_type for event in repo.events_for_target(job.target_object_id)][-2:] == [
        EventType.JOB_QUEUED, EventType.JOB_LEASED,
    ]


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
        pytest.skip("Postgres integration requires psycopg and TEST_POSTGRES_DSN")
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
    assert [event.event_type for event in repo.events_for_target(job.target_object_id)][-2:] == [
        EventType.JOB_QUEUED, EventType.JOB_LEASED,
    ]


def test_postgres_atomic_create_rolls_back_if_job_insert_fails() -> None:
    if not postgres_runtime_available():
        pytest.skip("Postgres integration requires psycopg and TEST_POSTGRES_DSN")
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
        pytest.skip("Postgres integration requires psycopg and TEST_POSTGRES_DSN")
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
        pytest.skip("Postgres integration requires psycopg and TEST_POSTGRES_DSN")
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
        pytest.skip("Postgres integration requires psycopg and TEST_POSTGRES_DSN")
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
        pytest.skip("Postgres integration requires psycopg and TEST_POSTGRES_DSN")
    dsn = postgres_dsn_from_env()
    assert dsn is not None
    repo = PostgresRuntimeRepository(dsn)
    repo.reset()

    assert repo.list_claim_cards("pub-pg-with-no-claims") == []


def test_inmemory_merge_object_metadata_patches_without_disturbing_existing() -> None:
    repo = InMemoryRuntimeRepository()
    obj = repo.create_object(
        ResearchObject(
            object_type=ObjectType.PUBLICATION,
            title="Merge target",
            metadata={"existing": 1, "shared": "old"},
        )
    )

    updated = repo.merge_object_metadata(obj.id, {"shared": "new", "added": True})

    assert updated is not None
    assert updated.metadata == {"existing": 1, "shared": "new", "added": True}
    assert repo.get_object(obj.id).metadata == updated.metadata  # type: ignore[union-attr]


def test_inmemory_merge_object_metadata_returns_none_for_unknown_id() -> None:
    repo = InMemoryRuntimeRepository()

    assert repo.merge_object_metadata("missing-object", {"a": 1}) is None


def test_inmemory_merge_object_metadata_and_enqueue_job_applies_both() -> None:
    repo = InMemoryRuntimeRepository()
    obj = repo.create_object(
        ResearchObject(
            object_type=ObjectType.PUBLICATION,
            title="Merge + enqueue",
            metadata={"existing": 1},
        )
    )
    job = RuntimeJob(target_object_id=obj.id, stage=Stage.OSF_DEPOSIT)

    updated, queued = repo.merge_object_metadata_and_enqueue_job(
        obj.id, {"publication_state": "PUBLISHING"}, job
    )

    assert updated.metadata == {"existing": 1, "publication_state": "PUBLISHING"}
    assert repo.get_object(obj.id).metadata == updated.metadata  # type: ignore[union-attr]
    assert queued.id == job.id
    assert repo.get_job(job.id) is not None
    assert repo.events[-1].event_type == EventType.JOB_QUEUED


def test_postgres_merge_object_metadata_uses_jsonb_merge_sql(monkeypatch) -> None:
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

        def commit(self) -> None:
            return None

    repo = PostgresRuntimeRepository.__new__(PostgresRuntimeRepository)
    monkeypatch.setattr(repo, "_connect", lambda: _Connection())
    patch = {"integrity": {"recommendation": "pass"}, "publication_state": "PUBLISHING"}

    assert repo.merge_object_metadata("pub-1", patch) is None
    query, params = calls[0]
    assert "metadata::jsonb || %s::jsonb" in query
    assert "SET metadata = %s" not in query
    assert params == (json.dumps(patch), "pub-1")


def test_postgres_merge_object_metadata_and_enqueue_job_uses_jsonb_merge_sql(
    monkeypatch,
) -> None:
    calls: list[tuple[str, tuple[object, ...]]] = []
    object_row = {
        "id": "pub-2",
        "object_type": ObjectType.PUBLICATION.value,
        "parent_object_id": None,
        "title": "Merged",
        "body_markdown": "",
        "metadata": {"existing": 1, "added": True},
        "created_at": datetime.now(timezone.utc),
    }

    class _Cursor:
        rowcount = 1

        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, query: str, params: tuple[object, ...]) -> None:
            calls.append((query, params))

        def fetchone(self):
            if "UPDATE research_objects" in calls[-1][0]:
                return object_row
            return None

    class _Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def cursor(self) -> _Cursor:
            return _Cursor()

        def commit(self) -> None:
            return None

    repo = PostgresRuntimeRepository.__new__(PostgresRuntimeRepository)
    monkeypatch.setattr(repo, "_connect", lambda: _Connection())
    patch = {"added": True}
    job = RuntimeJob(target_object_id="pub-2", stage=Stage.DW_DELIVERY)

    updated, queued = repo.merge_object_metadata_and_enqueue_job("pub-2", patch, job)

    merge_query, merge_params = calls[0]
    assert "metadata::jsonb || %s::jsonb" in merge_query
    assert merge_params == (json.dumps(patch), "pub-2")
    assert updated.metadata == {"existing": 1, "added": True}
    assert queued.id == job.id
    assert any("INSERT INTO runtime_jobs" in query for query, _ in calls)
    assert any("INSERT INTO runtime_events" in query for query, _ in calls)


def test_postgres_merge_object_metadata_against_real_database() -> None:
    """Integration: the merge SQL must run against a real TEXT metadata column.
    Fake-cursor tests locked in a `metadata ||` query that Postgres resolves as
    text concatenation on this schema — only a live database catches that."""
    if not postgres_runtime_available():
        pytest.skip("Postgres integration requires psycopg and TEST_POSTGRES_DSN")
    dsn = postgres_dsn_from_env()
    assert dsn is not None
    repo = PostgresRuntimeRepository(dsn)
    repo.reset()
    obj = repo.create_object(
        ResearchObject(object_type=ObjectType.SUBMISSION, title="Merge target")
    )

    merged = repo.merge_object_metadata(
        obj.id, {"integrity": {"recommendation": "pass"}, "publication_state": "PUBLISHING"}
    )
    assert merged is not None
    assert merged.metadata["integrity"] == {"recommendation": "pass"}
    assert merged.metadata["publication_state"] == "PUBLISHING"

    # A second disjoint merge survives alongside the first (no lost updates).
    again = repo.merge_object_metadata(obj.id, {"delivery_recovery_count": 2})
    assert again is not None
    assert again.metadata["integrity"] == {"recommendation": "pass"}
    assert again.metadata["publication_state"] == "PUBLISHING"
    assert again.metadata["delivery_recovery_count"] == 2

    # Re-read from the database: persisted document is valid and complete.
    stored = repo.get_object(obj.id)
    assert stored is not None and stored.metadata == again.metadata

    # Merge + enqueue variant: patch applied and job queued atomically.
    job = RuntimeJob(target_object_id=obj.id, stage=Stage.REVIEW)
    updated, queued = repo.merge_object_metadata_and_enqueue_job(
        obj.id, {"review_requested": True}, job
    )
    assert updated.metadata["review_requested"] is True
    assert updated.metadata["integrity"] == {"recommendation": "pass"}
    assert queued.id == job.id
    stored = repo.get_object(obj.id)
    assert stored is not None and stored.metadata["review_requested"] is True
    assert repo.merge_object_metadata("missing-object", {"x": 1}) is None


@pytest.mark.parametrize("merge", [False, True])
@pytest.mark.parametrize("repo_fixture", ["inmemory_repo", "postgres_repo"])
def test_bulk_metadata_write_is_atomic_on_missing_object(
    request: pytest.FixtureRequest, repo_fixture: str, merge: bool,
) -> None:
    repo = request.getfixturevalue(repo_fixture)
    obj = repo.create_object(ResearchObject(
        id="a-present", object_type=ObjectType.PUBLICATION, title="Atomic visibility",
        metadata={"public_visibility": "hidden", "receipt": "keep"},
    ))
    with pytest.raises(ValueError, match="object_not_found"):
        repo.update_objects_metadata({
            obj.id: {"public_visibility": "listed"},
            "z-missing": {"public_visibility": "listed"},
        }, merge=merge)

    assert repo.get_object(obj.id).metadata == obj.metadata
    repo.update_objects_metadata({obj.id: {"public_visibility": "listed"}}, merge=merge)
    assert repo.get_object(obj.id).metadata == (
        {"public_visibility": "listed", "receipt": "keep"} if merge
        else {"public_visibility": "listed"}
    )


@pytest.mark.parametrize("merge", [False, True])
def test_postgres_metadata_enqueue_rolls_back_on_job_id_collision(
    postgres_repo: PostgresRuntimeRepository, merge: bool,
) -> None:
    repo = postgres_repo
    obj = repo.create_object(
        ResearchObject(object_type=ObjectType.SUBMISSION, title="Atomic metadata", metadata={"keep": 1})
    )
    existing = repo.enqueue_job(RuntimeJob(target_object_id="other-object", stage=Stage.REVIEW))
    conflicting = RuntimeJob(id=existing.id, target_object_id=obj.id, stage=Stage.REVIEW)
    write = repo.merge_object_metadata_and_enqueue_job if merge else repo.update_object_metadata_and_enqueue_job

    with pytest.raises(repo._psycopg.errors.UniqueViolation):
        write(obj.id, {"new": 2}, conflicting)

    stored = repo.get_object(obj.id)
    assert stored is not None and stored.metadata == {"keep": 1}
    assert repo.get_job(existing.id) == existing
    assert repo.events_for_target(obj.id) == []
