from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest

from contracts import ArticleType, ObjectType, ResearchObject
from runtime_core.compiler import canonical_package_hash
from runtime_core.judge_release import build_judge_release
from runtime_core.repos import InMemoryRuntimeRepository
from runtime_core.osf import (
    OSFConfig,
    OSFClient,
    backfill_missing_publication_dois,
    build_publication_package,
    build_oauth_authorization_url,
    mint_publication_doi,
    mint_publication_doi_from_repository,
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
        mint_timeouts: int = 0,
        doi_after_mint_failure: str | None = None,
    ) -> None:
        self.existing_node = existing_node
        self.existing_doi = existing_doi
        self.identifier_failures = identifier_failures
        self.mint_failures = mint_failures
        self.mint_timeouts = mint_timeouts
        self.doi_after_mint_failure = doi_after_mint_failure
        self.created = 0
        self.updated: list[tuple[str, bool]] = []
        self.minted = 0
        self.uploaded = 0
        self.events: list[str] = []

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
        self.events.append("public" if public else "private")
        self.updated.append((node_id, public))

    def list_identifiers(self, node_id: str) -> list[dict[str, Any]]:
        if self.identifier_failures:
            self.identifier_failures -= 1
            raise RuntimeError(f"osf_request_failed:GET:/nodes/{node_id}/identifiers/:404:not ready")
        if not self.existing_doi:
            return []
        return [{"attributes": {"category": "doi", "value": self.existing_doi}}]

    def mint_doi(self, node_id: str) -> dict[str, Any]:
        self.events.append("mint")
        self.minted += 1
        if self.mint_timeouts:
            self.mint_timeouts -= 1
            raise TimeoutError("The read operation timed out")
        if self.mint_failures:
            self.mint_failures -= 1
            self.existing_doi = self.doi_after_mint_failure
            raise RuntimeError(f"osf_request_failed:POST:/nodes/{node_id}/identifiers/:503:try later")
        return {"attributes": {"category": "doi", "value": "10.17605/OSF.IO/NODE1"}}

    def upload_package(self, node_id: str, files: dict[str, str], *, release_id: str) -> list[dict[str, Any]]:
        assert node_id == "node-1"
        assert release_id == "a" * 64
        assert "manifest-sha256.json" in files
        self.events.append("upload")
        self.uploaded += 1
        return [{"logical_name": name, "sha256": "verified"} for name in sorted(files)]


def _package() -> dict[str, str]:
    return {"manuscript.md": "Body\n", "manifest-sha256.json": "{}\n"}


def _storage_client(monkeypatch: pytest.MonkeyPatch, *, stored: bytes) -> tuple[OSFClient, list[str]]:
    client = OSFClient(OSFConfig(
        api_base_url="https://api.osf.io/v2",
        token="test-token",
        root_project_id="root-node",
    ))
    monkeypatch.setattr(client, "_request", lambda *_args, **_kwargs: {
        "data": [{
            "id": "osfstorage",
            "links": {
                "upload": "https://files.osf.io/upload/",
                "files": "https://files.osf.io/list/",
            },
        }],
    })
    monkeypatch.setattr(client, "_url_json", lambda _url: {
        "data": [{
            "attributes": {"name": "release-aaaaaaaaaaaaaaaa-manuscript.md"},
            "links": {"download": "https://files.osf.io/download/manuscript.md"},
        }],
    })
    methods: list[str] = []

    def url_bytes(method: str, _url: str, *, body: bytes | None = None) -> bytes:
        methods.append(method)
        assert body is None
        return stored

    monkeypatch.setattr(client, "_url_bytes", url_bytes)
    return client, methods


def test_list_child_nodes_follows_osf_pagination(monkeypatch: pytest.MonkeyPatch) -> None:
    client = OSFClient(OSFConfig("https://api.osf.io/v2", "test-token", "root-node"))
    next_url = "https://api.osf.io/v2/nodes/root-node/children/?page=2"
    monkeypatch.setattr(client, "_request", lambda *_args, **_kwargs: {
        "data": [{"id": "node-1"}], "links": {"next": next_url},
    })
    followed: list[str] = []

    def next_page(url: str) -> dict[str, Any]:
        followed.append(url)
        return {"data": [{"id": "node-2"}], "links": {"next": None}}

    monkeypatch.setattr(client, "_url_json", next_page)

    assert [node["id"] for node in client.list_child_nodes("root-node")] == ["node-1", "node-2"]
    assert followed == [next_url]


def test_upload_package_waits_for_osf_storage(monkeypatch: pytest.MonkeyPatch) -> None:
    client = OSFClient(OSFConfig("https://api.osf.io/v2", "test-token", "root-node"))
    provider_calls = 0

    def providers(*_args: object, **_kwargs: object) -> dict[str, Any]:
        nonlocal provider_calls
        provider_calls += 1
        if provider_calls == 1:
            return {"data": []}
        return {"data": [{"id": "osfstorage", "links": {
            "upload": "https://files.osf.io/upload/", "files": "https://files.osf.io/list/",
        }}]}

    monkeypatch.setattr(client, "_request", providers)
    monkeypatch.setattr(client, "_url_json", lambda _url: {"data": []})
    monkeypatch.setattr("runtime_core.osf._sleep_before_retry", lambda _attempt: None)

    def url_bytes(method: str, _url: str, *, body: bytes | None = None) -> bytes:
        if method == "PUT":
            assert body == b"Body\n"
            return json.dumps({"data": {"links": {"download": "https://files.osf.io/download/body"}}}).encode()
        assert method == "GET" and body is None
        return b"Body\n"

    monkeypatch.setattr(client, "_url_bytes", url_bytes)

    receipts = client.upload_package("node-1", {"manuscript.md": "Body\n"}, release_id="a" * 64)

    assert provider_calls == 2
    assert receipts[0]["logical_name"] == "manuscript.md"


def test_osf_package_release_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    client, methods = _storage_client(monkeypatch, stored=b"Body\n")

    receipts = client.upload_package("node-1", {"manuscript.md": "Body\n"}, release_id="a" * 64)

    assert methods == ["GET"]
    assert receipts[0]["sha256"] == hashlib.sha256(b"Body\n").hexdigest()


def test_osf_package_release_rejects_content_change(monkeypatch: pytest.MonkeyPatch) -> None:
    client, methods = _storage_client(monkeypatch, stored=b"Original\n")

    with pytest.raises(RuntimeError, match="osf_package_release_conflict:manuscript.md"):
        client.upload_package("node-1", {"manuscript.md": "Changed\n"}, release_id="a" * 64)

    assert methods == ["GET"]


def _publication(**metadata: object) -> ResearchObject:
    return ResearchObject(
        id="pub-1",
        object_type=ObjectType.PUBLICATION,
        parent_object_id="sub-1",
        title="Accepted paper",
        body_markdown="Body",
        metadata={"abstract": "Short abstract.", "canonical_package_hash": f"sha256:{'a' * 64}", **metadata},
    )


def _lineaged_publication(
    repo: InMemoryRuntimeRepository,
    *,
    title: str,
    author_agent_id: str,
    doi_status: str | None = None,
) -> ResearchObject:
    source_bundle = [
        {
            "doi": "10.1234/source",
            "excerpt": "Bounded verified source excerpt.",
            "evidence_text_verified": True,
        }
    ]
    sections = {
        "Research Question": "Bounded question.",
        "Search Summary": "Bounded search.",
        "Evidence Landscape": "Bounded landscape.",
        "Key Findings": "Bounded finding.",
        "Limitations": "Bounded limitation.",
        "Gaps Identified": "Bounded gap.",
        "Conclusion": "Bounded conclusion.",
    }
    article_type = ArticleType.RAPID_EVIDENCE_SYNTHESIS.value
    package_hash = canonical_package_hash(
        title=title,
        abstract="Bounded abstract.",
        sections=sections,
        source_bundle=source_bundle,
        article_type=article_type,
    )
    submission = repo.create_object(ResearchObject(
        object_type=ObjectType.SUBMISSION,
        title=title,
        metadata={
            "abstract": "Bounded abstract.",
            "article_type": article_type,
            "sections": sections,
            "canonical_package_hash": package_hash,
            "source_bundle": source_bundle,
        },
    ))
    judge_release = build_judge_release(
        system_prompt="test-review-prompt",
        provider="reviewer-panel",
        model="test-reviewer-a|test-reviewer-b",
        response_metadata={"panel_models": ["test-reviewer-a", "test-reviewer-b"]},
    )
    review = repo.create_object(ResearchObject(
        object_type=ObjectType.REVIEW,
        parent_object_id=submission.id,
        title=f"Review: {title}",
        body_markdown="Accepted review receipt.",
        metadata={
            "recommendation": "accept",
            "reviewed_package_hash": package_hash,
            "provider": "reviewer-panel",
            "accept_quorum_count": 2,
            "accept_quorum_models": ["test-reviewer-a", "test-reviewer-b"],
            "accept_quorum_identities": [
                "test-provider-a:test-reviewer-a",
                "test-provider-b:test-reviewer-b",
            ],
            "accept_quorum_providers": ["test-provider-a", "test-provider-b"],
            "judge_release_id": judge_release["id"],
            "judge_release": judge_release,
        },
    ))
    decision = repo.create_object(ResearchObject(
        object_type=ObjectType.DECISION,
        parent_object_id=submission.id,
        title=f"Decision: {title}",
        metadata={"decision": "accept", "review_id": review.id, "canonical_package_hash": package_hash},
    ))
    status = {"doi_status": doi_status, "osf_status": doi_status} if doi_status else {}
    return repo.create_object(ResearchObject(
        object_type=ObjectType.PUBLICATION,
        parent_object_id=submission.id,
        title=title,
        body_markdown="Accepted publication body.\n",
        metadata={
            "author_agent_id": author_agent_id,
            "canonical_package_hash": package_hash,
            "review_id": review.id,
            "decision_id": decision.id,
            "judge_release_id": judge_release["id"],
            **status,
        },
    ))


def test_publication_package_uses_server_evidence_verification_only() -> None:
    repo = InMemoryRuntimeRepository()
    publication = _lineaged_publication(
        repo,
        title="Server-verified package",
        author_agent_id="agent-a",
    )
    submission = repo.get_object(str(publication.parent_object_id))
    assert submission is not None
    repo.update_object_metadata(
        submission.id,
        {**submission.metadata, "source_verification": {}},
    )

    unverified = json.loads(build_publication_package(repo, publication)["verified_evidence_spans.json"])
    assert unverified[0]["evidence_text_verified"] is False

    repo.update_object_metadata(
        submission.id,
        {
            **submission.metadata,
            "source_verification": {"evidence_text_verified": ["doi:10.1234/source"]},
        },
    )
    verified = json.loads(build_publication_package(repo, publication)["verified_evidence_spans.json"])
    assert verified[0]["evidence_text_verified"] is True


def test_publication_package_rejects_decision_bound_to_different_review() -> None:
    repo = InMemoryRuntimeRepository()
    publication = _lineaged_publication(
        repo,
        title="Mismatched review lineage",
        author_agent_id="agent-a",
    )
    submission_id = str(publication.parent_object_id)
    decision = repo.get_object(str(publication.metadata["decision_id"]))
    assert decision is not None
    other_review = repo.create_object(
        ResearchObject(
            object_type=ObjectType.REVIEW,
            parent_object_id=submission_id,
            title="Different review",
            metadata={"reviewed_package_hash": publication.metadata["canonical_package_hash"]},
        )
    )
    repo.update_object_metadata(
        decision.id, {**decision.metadata, "review_id": other_review.id}
    )

    with pytest.raises(RuntimeError, match="osf_scientific_package_lineage_invalid"):
        build_publication_package(repo, publication)


def test_mint_publication_doi_creates_public_child_node_and_doi() -> None:
    publication = _publication()
    client = FakeOSFClient()

    metadata = mint_publication_doi(
        publication,
        package_files=_package(),
        config=OSFConfig(api_base_url="https://api.osf.io/v2", token="test-token", root_project_id="root-node"),
        client=client,  # type: ignore[arg-type]
    )

    assert client.created == 1
    assert client.uploaded == 1
    assert client.updated == [("node-1", True)]
    assert client.minted == 1
    assert client.events == ["upload", "public", "mint"]
    assert metadata["doi"] == "10.17605/OSF.IO/NODE1"
    assert metadata["doi_status"] == "minted"
    assert metadata["osf_guid"] == "node-1"


def test_mint_publication_doi_reuses_existing_publication_node() -> None:
    publication = _publication()
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
        package_files=_package(),
        config=OSFConfig(api_base_url="https://api.osf.io/v2", token="test-token", root_project_id="root-node"),
        client=client,  # type: ignore[arg-type]
    )

    assert client.created == 0
    assert client.minted == 0
    assert metadata["doi"] == "10.17605/OSF.IO/EXIST"


def test_mint_publication_doi_retries_transient_identifier_404(monkeypatch) -> None:
    monkeypatch.setattr("runtime_core.osf.time.sleep", lambda _: None)
    publication = _publication()
    client = FakeOSFClient(identifier_failures=1)

    metadata = mint_publication_doi(
        publication,
        package_files=_package(),
        config=OSFConfig(api_base_url="https://api.osf.io/v2", token="test-token", root_project_id="root-node"),
        client=client,  # type: ignore[arg-type]
    )

    assert client.minted == 1
    assert metadata["doi"] == "10.17605/OSF.IO/NODE1"


def test_mint_publication_doi_rechecks_identifiers_after_transient_mint_failure(monkeypatch) -> None:
    monkeypatch.setattr("runtime_core.osf.time.sleep", lambda _: None)
    publication = _publication()
    client = FakeOSFClient(mint_failures=1, doi_after_mint_failure="10.17605/OSF.IO/EXIST")

    metadata = mint_publication_doi(
        publication,
        package_files=_package(),
        config=OSFConfig(api_base_url="https://api.osf.io/v2", token="test-token", root_project_id="root-node"),
        client=client,  # type: ignore[arg-type]
    )

    assert client.minted == 1
    assert metadata["doi"] == "10.17605/OSF.IO/EXIST"


def test_mint_publication_doi_retries_read_timeout(monkeypatch) -> None:
    monkeypatch.setattr("runtime_core.osf.time.sleep", lambda _: None)
    publication = _publication()
    client = FakeOSFClient(mint_timeouts=1)

    metadata = mint_publication_doi(
        publication,
        package_files=_package(),
        config=OSFConfig(api_base_url="https://api.osf.io/v2", token="test-token", root_project_id="root-node"),
        client=client,  # type: ignore[arg-type]
    )

    assert client.minted == 2
    assert metadata["doi"] == "10.17605/OSF.IO/NODE1"


def test_backfill_missing_publication_dois_uses_agent_oauth_token(monkeypatch) -> None:
    repo = InMemoryRuntimeRepository()
    publication = _lineaged_publication(
        repo,
        title="Accepted memo",
        author_agent_id="agent-v4-alpha-memo",
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
    publication = _lineaged_publication(
        repo,
        title="Accepted domain memo",
        author_agent_id="agent-v4-alpha-longevity-research",
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


def test_default_oauth_agent_falls_through_to_service_token_when_disconnected(monkeypatch) -> None:
    monkeypatch.setenv("RESEARKA_V2_OSF_DEFAULT_AGENT_ID", "agent-v4-alpha-memo")
    repo = InMemoryRuntimeRepository()
    publication = _lineaged_publication(
        repo,
        title="Accepted domain memo",
        author_agent_id="agent-v4-alpha-longevity-research",
    )

    def fake_service_mint(publication_arg: ResearchObject, **_: object) -> dict[str, object]:
        assert publication_arg.title == "Accepted domain memo"
        return {"doi": "10.17605/OSF.IO/SVC01", "doi_status": "minted", "osf_status": "minted"}

    monkeypatch.setattr("runtime_core.osf.mint_publication_doi", fake_service_mint)

    metadata = mint_publication_doi_from_repository(repo, publication)

    assert metadata["doi"] == "10.17605/OSF.IO/SVC01"
    assert metadata["doi_status"] == "minted"


def test_backfill_missing_publication_dois_fails_visibly_without_owner_or_service_token(monkeypatch) -> None:
    for env_name in (
        "RESEARKA_V2_OSF_DEFAULT_AGENT_ID",
        "RESEARKA_V2_OSF_FALLBACK_AGENT_ID",
        "RESEARKA_V2_OSF_PROJECT_ID",
        "RESEARKA_V2_OSF_TOKEN",
        "RESEARKA_V2_OSF_TOKEN_PATH",
        "OSF_ACCESS_TOKEN",
    ):
        monkeypatch.delenv(env_name, raising=False)
    repo = InMemoryRuntimeRepository()
    publication = _lineaged_publication(
        repo,
        title="Pending alpha memo",
        author_agent_id="agent-v4-alpha-longevity-research",
        doi_status="pending_osf_credentials",
    )

    summary = backfill_missing_publication_dois(repo, apply=True)
    updated = repo.get_object(publication.id)

    assert summary["eligible"] == 1
    assert summary["minted"] == 0
    assert summary["failed"] == 1
    assert updated is not None
    assert updated.metadata["doi_status"] == "failed"
    assert updated.metadata["osf_error"] == "osf_not_configured_for_agent"


def test_backfill_missing_publication_dois_retries_pending_and_failed_records(monkeypatch) -> None:
    monkeypatch.setenv("RESEARKA_V2_OSF_DEFAULT_AGENT_ID", "agent-v4-alpha-memo")
    repo = InMemoryRuntimeRepository()
    repo.store_osf_oauth_token("agent-v4-alpha-memo", {"access_token": "oauth-token", "root_project_id": "root-node"})
    pending = _lineaged_publication(
        repo,
        title="Pending alpha memo",
        author_agent_id="agent-v4-alpha-longevity-research",
        doi_status="pending_osf_credentials",
    )
    failed = _lineaged_publication(
        repo,
        title="Failed alpha memo",
        author_agent_id="agent-v4-alpha-ai-research",
        doi_status="failed",
    )

    def fake_mint(publication_arg: ResearchObject, *, token_metadata: dict[str, object], **_: object):
        assert token_metadata["access_token"] == "oauth-token"
        return (
            {
                "doi": f"10.17605/OSF.IO/{publication_arg.id[:5].upper()}",
                "doi_status": "minted",
                "osf_status": "minted",
            },
            token_metadata,
        )

    monkeypatch.setattr("runtime_core.osf.mint_publication_doi_with_oauth", fake_mint)

    summary = backfill_missing_publication_dois(repo, apply=True)

    assert summary["eligible"] == 2
    assert summary["minted"] == 2
    assert summary["failed"] == 0
    assert repo.get_object(pending.id).metadata["doi_status"] == "minted"  # type: ignore[union-attr]
    assert repo.get_object(failed.id).metadata["doi_status"] == "minted"  # type: ignore[union-attr]


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
