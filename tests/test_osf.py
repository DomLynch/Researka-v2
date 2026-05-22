from __future__ import annotations

from typing import Any

from contracts import ObjectType, ResearchObject
from runtime_core.osf import (
    OSFConfig,
    build_oauth_authorization_url,
    mint_publication_doi,
    sign_oauth_state,
    verify_oauth_state,
)


class FakeOSFClient:
    def __init__(self, *, existing_node: dict[str, Any] | None = None, existing_doi: str | None = None) -> None:
        self.existing_node = existing_node
        self.existing_doi = existing_doi
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
        if not self.existing_doi:
            return []
        return [{"attributes": {"category": "doi", "value": self.existing_doi}}]

    def mint_doi(self, node_id: str) -> dict[str, Any]:
        self.minted += 1
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


def test_oauth_state_roundtrip() -> None:
    state = sign_oauth_state(agent_id="agent-v3-full-paper", secret="state-secret", issued_at=1_000_000)

    payload = verify_oauth_state(state, secret="state-secret", max_age_seconds=10_000_000_000)

    assert payload["agent_id"] == "agent-v3-full-paper"
    assert payload["iat"] == 1_000_000


def test_oauth_state_rejects_wrong_secret() -> None:
    state = sign_oauth_state(agent_id="agent-v3-full-paper", secret="state-secret", issued_at=1_000_000)

    try:
        verify_oauth_state(state, secret="wrong-secret", max_age_seconds=10_000_000_000)
    except ValueError as exc:
        assert str(exc) == "invalid_oauth_state_signature"
    else:
        raise AssertionError("wrong secret should fail")


def test_build_oauth_authorization_url_contains_osf_app_contract() -> None:
    from runtime_core.osf import OSFOAuthConfig

    url = build_oauth_authorization_url(
        OSFOAuthConfig(
            authorization_url="https://accounts.osf.io/oauth2/authorize",
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

    assert url.startswith("https://accounts.osf.io/oauth2/authorize?")
    assert "response_type=code" in url
    assert "client_id=client-id" in url
    assert "redirect_uri=https%3A%2F%2Fapi.researka.org%2Foauth%2Fosf%2Fcallback" in url
    assert "scope=osf.full_write" in url
    assert "state=signed-state" in url
