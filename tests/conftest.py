import pytest
from fastapi.testclient import TestClient

from apps.runtime_api.app import create_app
from runtime_core.repos import (
    InMemoryRuntimeRepository,
    PostgresRuntimeRepository,
    postgres_dsn_from_env,
    postgres_runtime_available,
)


@pytest.fixture
def inmemory_repo():
    repo = InMemoryRuntimeRepository()
    repo.reset()
    return repo


@pytest.fixture
def client(inmemory_repo, monkeypatch):
    monkeypatch.setenv("RESEARKA_V2_API_KEY", "test-legacy-key")
    return TestClient(
        create_app(inmemory_repo),
        headers={"x-api-key": "test-legacy-key"},
    )


@pytest.fixture
def postgres_repo():
    if not postgres_runtime_available():
        pytest.skip("Postgres runtime parity requires psycopg and TEST_POSTGRES_DSN")
    repo = PostgresRuntimeRepository(postgres_dsn_from_env())
    repo.reset()
    return repo


@pytest.fixture
def postgres_client(postgres_repo):
    return TestClient(create_app(postgres_repo))
