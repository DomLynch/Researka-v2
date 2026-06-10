from __future__ import annotations

from typing import Any

from contracts import ObjectType, ResearchObject
from runtime_core.repos import InMemoryRuntimeRepository
from runtime_core.osf import (
    OSFConfig,
    backfill_missing_publication_dois,
    build_oauth_authorization_url,
    mint_publication_doi,
    oauth_config_from_env,
    sign_oauth_state,
    verify_oauth_state,
)


class FakeOSFClient:
    def __init__(
        self,
        *,
        existing_node: dict[str, Any] | None = None,
        existing_doi: str | None = None,
        identifier_failures: int = 0,
        mint_failures: int = 0,
        doi_after_mint_failure: str | None = None,
    ) -> None:
        self.existing_node = existing_node
        self.existing_doi = existing_doi
        self.identifier_failures = identifier_failures
        self.mint_failures = mint_failures
        self.doi_after_mint_failure = doi_after_mint_failure
        self.created = 0
        self.updated: list[tuple[str, bool]] = []
        self.minted = 0

    def list_child_nodes(self, node_id: str) -> list[dict[str, Any]]:
        assert node_id == "root-node"
        return [self.existing_node] if self.existing_node else []

    def create_child_node(self, node_id: str, *, title: str, description: str, tags: list[str]) -> dict[str, Any]:
        assert node_id == "root-node"
        assert title == "Accepted paper"
        assert "Researka accepted publication: pub-1" in description
        assert "researka-publication:pub-1" in tags
        self.created += 1
        return {
            "id": "node-1",
            "type": "nodes",
            "attributes": {"tags": tags},
            "links": {"html": "https://osf.io/node1/"},
        }

    def update_node(self, node_id: str, *, public: bool) -> None:
        self.updated.append((node_id, public))

    def list_identifiers(self, node_id: str) -> list[dict[str, Any]]:
        if self.identifier_failures:
            self.identifier_failures -= 1
            raise RuntimeError(f"osf_request_failed:GET:/nodes/{node_id}/identifiers/:404:not ready")
        if not self.existing_doi:
            return []
        return [{"attributes": {"category": "doi", "value": self.existing_doi}}]

    def mint_doi(self, node_id: str) -> dict[str, Any]:
        self.minted += 1
        if self.mint_failures:
            self.mint_failures -= 1
            self.existing_doi = self.doi_after_mint_failure
            raise RuntimeError(f"osf_request_failed:POST:/nodes/{node_id}/identifiers/:503:try later")
        return {"attributes": {"category": "doi", "value": "10.17605/OSF.IO/NODE1"}}


def test_mint_publication_doi_creates_public_child_node_and_doi() -> None:
    publication = ResearchObject(
        id="pub-1",
        object_type=ObjectType.PUBLICATION,
        parent_object_id="sub-1",
        title="Accepted paper",
        body_markdown="Body",
        metadata={"abstract": "Short abstract."},
    )
    client = FakeOSFClient()

    metadata = mint_publication_doi(
        publication,
        config=OSFConfig(api_base_url="https://api.osf.io/v2", token="test-token", root_project_id="root-node"),
        client=client,  # type: ignore[arg-type]
    )

    assert client.created == 1
    assert client.updated == [("node-1", True)]
    assert client.minted == 1
    assert metadata["doi"] == "10.17605/OSF.IO/NODE1"
    assert metadata["doi_status"] == "minted"
    assert metadata["osf_guid"] == "node-1"


def test_mint_publication_doi_reuses_existing_publication_node() -> None:
    publication = ResearchObject(
        id="pub-1",
        object_type=ObjectType.PUBLICATION,
        parent_object_id="sub-1",
        title="Accepted paper",
        body_markdown="Body",
    )
    client = FakeOSFClient(
        existing_node={
            "id": "node-1",
            "type": "nodes",
            "attributes": {"tags": ["researka-publication:pub-1"]},
            "links": {"html": "https://osf.io/node1/"},
        },
        existing_doi="10.17605/OSF.IO/EXIST",
    )

    metadata = mint_publication_doi(
        publication,
        config=OSFConfig(api_base_url="https://api.osf.io/v2", token="test-token", root_project_id="root-node"),
        client=client,  # type: ignore[arg-type]
    )

    assert client.created == 0
    assert client.minted == 0
    assert metadata["doi"] == "10.17605/OSF.IO/EXIST"


def test_mint_publication_doi_retries_transient_identifier_404(monkeypatch) -> None:
    monkeypatch.setattr("runtime_core.osf.time.sleep", lambda _: None)
    publication = ResearchObject(
        id="pub-1",
        object_type=ObjectType.PUBLICATION,
        parent_object_id="sub-1",
        title="Accepted paper",
        body_markdown="Body",
    )
    client = FakeOSFClient(identifier_failures=1)

    metadata = mint_publication_doi(
        publication,
        config=OSFConfig(api_base_url="https://api.osf.io/v2", token="test-token", root_project_id="root-node"),
        client=client,  # type: ignore[arg-type]
    )

    assert client.minted == 1
    assert metadata["doi"] == "10.17605/OSF.IO/NODE1"


def test_mint_publication_doi_rechecks_identifiers_after_transient_mint_failure(monkeypatch) -> None:
    monkeypatch.setattr("runtime_core.osf.time.sleep", lambda _: None)
    publication = ResearchObject(
        id="pub-1",
        object_type=ObjectType.PUBLICATION,
        parent_object_id="sub-1",
        title="Accepted paper",
        body_markdown="Body",
    )
    client = FakeOSFClient(mint_failures=1, doi_after_mint_failure="10.17605/OSF.IO/EXIST")

    metadata = mint_publication_doi(
        publication,
        config=OSFConfig(api_base_url="https://api.osf.io/v2", token="test-token", root_project_id="root-node"),
        client=client,  # type: ignore[arg-type]
    )

    assert client.minted == 1
    assert metadata["doi"] == "10.17605/OSF.IO/EXIST"


def test_backfill_missing_publication_dois_uses_agent_oauth_token(monkeypatch) -> None:
    repo = InMemoryRuntimeRepository()
    submission = repo.create_object(ResearchObject(object_type=ObjectType.SUBMISSION, title="Submission"))
    publication = repo.create_object(
        ResearchObject(
            object_type=ObjectType.PUBLICATION,
            parent_object_id=submission.id,
            title="Accepted memo",
            metadata={"author_agent_id": "agent-v4-alpha-memo"},
        )
    )
    repo.store_osf_oauth_token("agent-v4-alpha-memo", {"access_token": "oauth-token", "root_project_id": "root-node"})

    def fake_mint(publication_arg: ResearchObject, *, token_metadata: dict[str, object], **_: object):
        assert publication_arg.id == publication.id
        assert token_metadata["access_token"] == "oauth-token"
        return (
            {
                "doi": "10.17605/OSF.IO/OAUTH1",
                "doi_status": "minted",
                "osf_status": "minted",
            },
            token_metadata,
        )

    monkeypatch.setattr("runtime_core.osf.mint_publication_doi_with_oauth", fake_mint)

    summary = backfill_missing_publication_dois(repo, apply=True, publication_id=publication.id)
    updated = repo.get_object(publication.id)

    assert summary["minted"] == 1
    assert updated is not None
    assert updated.metadata["doi"] == "10.17605/OSF.IO/OAUTH1"


def test_backfill_missing_publication_dois_uses_default_oauth_agent(monkeypatch) -> None:
    monkeypatch.setenv("RESEARKA_V2_OSF_DEFAULT_AGENT_ID", "agent-v4-alpha-memo")
    repo = InMemoryRuntimeRepository()
    submission = repo.create_object(ResearchObject(object_type=ObjectType.SUBMISSION, title="Submission"))
    publication = repo.create_object(
        ResearchObject(
            object_type=ObjectType.PUBLICATION,
            parent_object_id=submission.id,
            title="Accepted domain memo",
            metadata={"author_agent_id": "agent-v4-alpha-longevity-research"},
        )
    )
    repo.store_osf_oauth_token("agent-v4-alpha-memo", {"access_token": "oauth-token", "root_project_id": "root-node"})

    def fake_mint(publication_arg: ResearchObject, *, token_metadata: dict[str, object], **_: object):
        assert publication_arg.id == publication.id
        assert token_metadata["access_token"] == "oauth-token"
        return (
            {
                "doi": "10.17605/OSF.IO/DEFAULT",
                "doi_status": "minted",
                "osf_status": "minted",
            },
            token_metadata,
        )

    monkeypatch.setattr("runtime_core.osf.mint_publication_doi_with_oauth", fake_mint)

    summary = backfill_missing_publication_dois(repo, apply=True, publication_id=publication.id)
    updated = repo.get_object(publication.id)

    assert summary["minted"] == 1
    assert updated is not None
    assert updated.metadata["doi"] == "10.17605/OSF.IO/DEFAULT"
    assert updated.metadata["osf_auth_source"] == "oauth_default_agent_token"
    assert updated.metadata["osf_agent_id"] == "agent-v4-alpha-memo"


def test_oauth_state_roundtrip() -> None:
    state = sign_oauth_state(
        agent_id="agent-v3-full-paper",
        secret="state-secret",
        issued_at=1_000_000,
        publication_id="pub-1",
    )

    payload = verify_oauth_state(state, secret="state-secret", max_age_seconds=10_000_000_000)

    assert payload["agent_id"] == "agent-v3-full-paper"
    assert payload["iat"] == 1_000_000
    assert payload["publication_id"] == "pub-1"


def test_oauth_state_rejects_wrong_secret() -> None:
    state = sign_oauth_state(agent_id="agent-v3-full-paper", secret="state-secret", issued_at=1_000_000)

    try:
        verify_oauth_state(state, secret="wrong-secret", max_age_seconds=10_000_000_000)
    except ValueError as exc:
        assert str(exc) == "invalid_oauth_state_signature"
    else:
        raise AssertionError("wrong secret should fail")


def test_oauth_config_prefers_dedicated_state_secret(monkeypatch, tmp_path) -> None:
    state_secret_path = tmp_path / "state-secret"
    state_secret_path.write_text("dedicated-state-secret\n")
    monkeypatch.setenv("RESEARKA_V2_OSF_OAUTH_CLIENT_ID", "client-id")
    monkeypatch.setenv("RESEARKA_V2_OSF_OAUTH_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("RESEARKA_V2_OSF_OAUTH_REDIRECT_URI", "https://api.researka.org/oauth/osf/callback")
    monkeypatch.setenv("RESEARKA_V2_OSF_OAUTH_STATE_SECRET_PATH", str(state_secret_path))

    config = oauth_config_from_env()

    assert config is not None
    assert config.state_secret == "dedicated-state-secret"


def test_oauth_config_requires_dedicated_state_secret(monkeypatch) -> None:
    monkeypatch.setenv("RESEARKA_V2_OSF_OAUTH_CLIENT_ID", "client-id")
    monkeypatch.setenv("RESEARKA_V2_OSF_OAUTH_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("RESEARKA_V2_OSF_OAUTH_REDIRECT_URI", "https://api.researka.org/oauth/osf/callback")

    try:
        oauth_config_from_env()
    except RuntimeError as exc:
        assert str(exc) == "researka_v2_osf_oauth_state_secret_required"
    else:
        raise AssertionError("OAuth state signing must not reuse the OSF client secret")


def test_build_oauth_authorization_url_contains_osf_app_contract() -> None:
    from runtime_core.osf import OSFOAuthConfig

    url = build_oauth_authorization_url(
        OSFOAuthConfig(
            authorization_url="https://osf.io/oauth2/authorize",
            token_url="https://accounts.osf.io/oauth2/token",
            api_base_url="https://api.osf.io/v2",
            client_id="client-id",
            client_secret="client-secret",
            redirect_uri="https://api.researka.org/oauth/osf/callback",
            scope="osf.full_write",
            state_secret="state-secret",
        ),
        state="signed-state",
    )

    assert url.startswith("https://osf.io/oauth2/authorize?")
    assert "response_type=code" in url
    assert "client_id=client-id" in url
    assert "redirect_uri=https%3A%2F%2Fapi.researka.org%2Foauth%2Fosf%2Fcallback" in url
    assert "scope=osf.full_write" in url
    assert "state=signed-state" in url
