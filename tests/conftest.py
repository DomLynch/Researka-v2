import pytest
from fastapi.testclient import TestClient

from apps.runtime_api.app import create_app
from runtime_core.providers import DeterministicProvider
from runtime_core.reviewer_panel import ReviewerPanel
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


@pytest.fixture(autouse=True)
def disable_integrity_by_default(monkeypatch):
    monkeypatch.setenv("RESEARKA_INTEGRITY_ENABLED", "0")


@pytest.fixture(autouse=True)
def disable_doi_check_by_default(monkeypatch):
    # Tests must never hit doi.org; gate tests re-enable with a mocked client.
    monkeypatch.setenv("RESEARKA_DOI_CHECK_ENABLED", "0")


@pytest.fixture(autouse=True)
def isolate_rate_limit_db(monkeypatch, tmp_path):
    monkeypatch.setenv("RESEARKA_V2_RATE_LIMIT_DB_PATH", str(tmp_path / "rate_limits.db"))


@pytest.fixture(autouse=True)
def deterministic_review_panel(monkeypatch):
    def factory():
        return ReviewerPanel(
            primary=DeterministicProvider(model="deterministic-primary"),
            sparring=DeterministicProvider(model="deterministic-sparring"),
            fallback=DeterministicProvider(model="deterministic-fallback"),
        )

    monkeypatch.setattr("runtime_core.workflow.reviewer_from_env", factory)


@pytest.fixture
def client(inmemory_repo, monkeypatch):
    monkeypatch.setenv("RESEARKA_V2_API_KEY", "test-legacy-key")
    monkeypatch.setenv("RESEARKA_V2_ADMIN_KEY", "test-admin-key")
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
def postgres_client(postgres_repo, monkeypatch):
    monkeypatch.setenv("RESEARKA_V2_API_KEY", "test-postgres-key")
    monkeypatch.setenv("RESEARKA_V2_ADMIN_KEY", "test-postgres-admin-key")
    return TestClient(create_app(postgres_repo), headers={"x-api-key": "test-postgres-key"})
