from typing import Any, cast
from fastapi.testclient import TestClient
from urllib.parse import parse_qs, quote, urlparse

from runtime_core.osf import sign_oauth_state


def _repository(client: TestClient) -> Any:
    return cast(Any, client.app).state.repository


def _valid_source_bundle() -> list[dict[str, object]]:
    years = (2024, 2023, 2022, 2021, 2020, 2024, 2023, 2022, 2021, 2019, 2018, 2017)
    evidence_types = ("review",) * 6 + ("primary",) * 6
    return [
        {
            "title": f"{evidence_type.title()} source {index}",
            "year": year,
            "evidence_type": evidence_type,
        }
        for index, (year, evidence_type) in enumerate(zip(years, evidence_types, strict=True), start=1)
    ]


def _run_until_idle(client: TestClient, limit: int = 12) -> None:
    for _ in range(limit):
        queue = client.get("/jobs/queue").json()["queued"]
        if not queue:
            return
        client.post("/jobs/run-once")


def test_health(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_version_returns_sha_and_start_time(client: TestClient) -> None:
    """The /version endpoint lets remote auditors verify the deployed SHA without SSH."""
    response = client.get("/version")
    assert response.status_code == 200
    data = response.json()
    assert data["service"] == "researka-v2-runtime-api"
    assert "git_sha" in data
    assert "started_at" in data
    # SHA is either a 40-char hex (real git output) or "unknown" (no git available).
    sha = data["git_sha"]
    assert sha == "unknown" or (len(sha) == 40 and all(c in "0123456789abcdef" for c in sha))
    # started_at is an ISO-8601 UTC timestamp.
    assert "T" in data["started_at"] and data["started_at"].endswith("+00:00")


def test_architecture(client: TestClient) -> None:
    response = client.get("/architecture")
    assert response.status_code == 200
    data = response.json()
    assert "runtime_core" in data["top_level_modules"]
    assert data["critical_flow"] == ["intake", "review", "editorial", "publish"]


def test_osf_oauth_start_uses_authenticated_agent_key(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("RESEARKA_V2_OSF_OAUTH_CLIENT_ID", "client-id")
    monkeypatch.setenv("RESEARKA_V2_OSF_OAUTH_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("RESEARKA_V2_OSF_OAUTH_STATE_SECRET", "state-secret")
    monkeypatch.setenv("RESEARKA_V2_OSF_OAUTH_REDIRECT_URI", "https://api.researka.org/oauth/osf/callback")
    raw_key = _repository(client).create_api_key("agent-v4-alpha-memo").raw_key

    response = client.get("/oauth/osf/start", headers={"x-api-key": raw_key}, follow_redirects=False)

    assert response.status_code == 302
    location = response.headers["location"]
    parsed = urlparse(location)
    query = parse_qs(parsed.query)
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == "https://accounts.osf.io/oauth2/authorize"
    assert query["client_id"] == ["client-id"]
    assert query["redirect_uri"] == ["https://api.researka.org/oauth/osf/callback"]
    assert query["scope"] == ["osf.full_write"]


def test_osf_oauth_start_requires_per_agent_key(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("RESEARKA_V2_OSF_OAUTH_CLIENT_ID", "client-id")
    monkeypatch.setenv("RESEARKA_V2_OSF_OAUTH_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("RESEARKA_V2_OSF_OAUTH_REDIRECT_URI", "https://api.researka.org/oauth/osf/callback")

    response = client.get("/oauth/osf/start")

    assert response.status_code == 400
    assert response.json()["detail"] == "per_agent_api_key_required"


def test_osf_oauth_callback_stores_token_without_exposing_it(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("RESEARKA_V2_OSF_OAUTH_CLIENT_ID", "client-id")
    monkeypatch.setenv("RESEARKA_V2_OSF_OAUTH_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("RESEARKA_V2_OSF_OAUTH_STATE_SECRET", "state-secret")
    monkeypatch.setenv("RESEARKA_V2_OSF_OAUTH_REDIRECT_URI", "https://api.researka.org/oauth/osf/callback")
    state = sign_oauth_state(agent_id="agent-v4-alpha-memo", secret="state-secret")

    def fake_exchange(config, *, code: str) -> dict[str, str]:
        assert code == "oauth-code"
        assert config.client_id == "client-id"
        return {"access_token": "oauth-access-token", "refresh_token": "oauth-refresh-token", "scope": "osf.full_write"}

    def fake_user_lookup(access_token: str, **kwargs) -> dict[str, str]:
        assert access_token == "oauth-access-token"
        return {"osf_user_id": "osf-user-1", "osf_user_name": "Dominic Lynch"}

    monkeypatch.setattr("apps.runtime_api.app.exchange_oauth_code", fake_exchange)
    monkeypatch.setattr("apps.runtime_api.app.osf_user_metadata_from_token", fake_user_lookup)

    response = client.get(f"/oauth/osf/callback?code=oauth-code&state={quote(state)}")

    assert response.status_code == 200
    body = response.json()
    assert body == {
        "status": "connected",
        "agent_id": "agent-v4-alpha-memo",
        "osf_user_id": "osf-user-1",
        "scope": "osf.full_write",
    }
    stored = _repository(client).get_osf_oauth_token("agent-v4-alpha-memo")
    assert stored["access_token"] == "oauth-access-token"
    assert stored["refresh_token"] == "oauth-refresh-token"


def test_submission_creates_intake_job(client: TestClient) -> None:
    response = client.post(
        "/submissions",
        json={
            "title": "Rapid Evidence Synthesis: cellular senescence",
            "abstract": "Bounded external submission.",
            "sections": {
                "Research Question": "This submission asks a bounded research question with enough detail on topic, evidence type, comparator, outcome target, and decision frame that a reviewer could reproduce the intended scope, publication window, and inclusion logic without inventing missing assumptions, broadening the claim, silently changing the relevant evidence category, or misreading the intended publication class for downstream review.",
                "Search Summary": "Databases searched include PubMed and review corpora, with a documented date window, explicit inclusion logic, and a clear narrowing rule that explains why these retained receipts best match the scoped research question.",
                "Evidence Landscape": "The bundle mixes review-level and primary evidence, explains where review-level support dominates the synthesis, and does not overclaim causal certainty when the retained evidence is heterogeneous.",
                "Key Findings": "Key findings integrate the retained evidence into a bounded synthesis rather than stitched snippets, and they distinguish stronger review-level support from more tentative primary-study signals.",
                "Limitations": "The main limits are scope, incomplete coverage, heterogeneous certainty, and the possibility of omitted contradictory sources that could materially shift the confidence of the synthesis.",
                "Gaps Identified": "No independent replication has confirmed these synthesis-level findings, and the gap between review-level evidence and applied outcomes remains untested.",
                "Conclusion": "The current evidence supports a cautious synthesis with explicit uncertainty, transparent methodological limits, and no claim that exceeds the retained bundle.",
            },
            "source_bundle": _valid_source_bundle(),
            "author_agent_id": "agent-demo",
            "domain_slug": "longevity",
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["job"]["stage"] == "submission_intake"
    queue = client.get("/jobs/queue").json()["queued"]
    assert len(queue) == 1


def test_can_get_created_submission(client: TestClient) -> None:
    submission = client.post(
        "/submissions",
        json={
            "title": "Rapid Evidence Synthesis: cellular senescence",
            "abstract": "Bounded external submission.",
            "sections": {
                "Research Question": "This submission asks a bounded research question with enough detail on topic, evidence type, comparator, outcome target, and decision frame that a reviewer could reproduce the intended scope, publication window, and inclusion logic without inventing missing assumptions, broadening the claim, silently changing the relevant evidence category, or misreading the intended publication class for downstream review.",
                "Search Summary": "Databases searched include PubMed and review corpora, with a documented date window, explicit inclusion logic, and a clear narrowing rule that explains why these retained receipts best match the scoped research question.",
                "Evidence Landscape": "The bundle mixes review-level and primary evidence, explains where review-level support dominates the synthesis, and does not overclaim causal certainty when the retained evidence is heterogeneous.",
                "Key Findings": "Key findings integrate the retained evidence into a bounded synthesis rather than stitched snippets, and they distinguish stronger review-level support from more tentative primary-study signals.",
                "Limitations": "The main limits are scope, incomplete coverage, heterogeneous certainty, and the possibility of omitted contradictory sources that could materially shift the confidence of the synthesis.",
                "Gaps Identified": "No independent replication has confirmed these synthesis-level findings, and the gap between review-level evidence and applied outcomes remains untested.",
                "Conclusion": "The current evidence supports a cautious synthesis with explicit uncertainty, transparent methodological limits, and no claim that exceeds the retained bundle.",
            },
            "source_bundle": _valid_source_bundle(),
            "author_agent_id": "agent-demo",
            "domain_slug": "longevity",
        },
    ).json()["submission"]
    response = client.get(f"/submissions/{submission['id']}")
    assert response.status_code == 200
    assert response.json()["object_type"] == "submission"


def test_submission_decision_pending_before_review(client: TestClient) -> None:
    submission = client.post(
        "/submissions",
        json={
            "title": "Rapid Evidence Synthesis: cellular senescence",
            "abstract": "Bounded external submission.",
            "sections": {
                "Research Question": "This submission asks a bounded research question with enough detail on topic, evidence type, comparator, outcome target, and decision frame that a reviewer could reproduce the intended scope, publication window, and inclusion logic without inventing missing assumptions, broadening the claim, silently changing the relevant evidence category, or misreading the intended publication class for downstream review.",
                "Search Summary": "Databases searched include PubMed and review corpora, with a documented date window, explicit inclusion logic, and a clear narrowing rule that explains why these retained receipts best match the scoped research question.",
                "Evidence Landscape": "The bundle mixes review-level and primary evidence, explains where review-level support dominates the synthesis, and does not overclaim causal certainty when the retained evidence is heterogeneous.",
                "Key Findings": "Key findings integrate the retained evidence into a bounded synthesis rather than stitched snippets, and they distinguish stronger review-level support from more tentative primary-study signals.",
                "Limitations": "The main limits are scope, incomplete coverage, heterogeneous certainty, and the possibility of omitted contradictory sources that could materially shift the confidence of the synthesis.",
                "Gaps Identified": "No independent replication has confirmed these synthesis-level findings, and the gap between review-level evidence and applied outcomes remains untested.",
                "Conclusion": "The current evidence supports a cautious synthesis with explicit uncertainty, transparent methodological limits, and no claim that exceeds the retained bundle.",
            },
            "source_bundle": _valid_source_bundle(),
            "author_agent_id": "agent-demo",
            "domain_slug": "longevity",
        },
    ).json()["submission"]
    response = client.get(f"/submissions/{submission['id']}/decision")
    assert response.status_code == 200
    assert response.json()["status"] == "pending"


def test_can_list_publications_after_processing(client: TestClient) -> None:
    submission = client.post(
        "/submissions",
        json={
            "title": "Rapid Evidence Synthesis: cellular senescence",
            "abstract": "Bounded external submission.",
            "sections": {
                "Research Question": "This submission asks a bounded research question with enough detail on topic, evidence type, comparator, outcome target, and decision frame that a reviewer could reproduce the intended scope, publication window, and inclusion logic without inventing missing assumptions, broadening the claim, silently changing the relevant evidence category, or misreading the intended publication class for downstream review.",
                "Search Summary": "Databases searched include PubMed and review corpora, with a documented date window, explicit inclusion logic, and a clear narrowing rule that explains why these retained receipts best match the scoped research question.",
                "Evidence Landscape": "The bundle mixes review-level and primary evidence, explains where review-level support dominates the synthesis, and does not overclaim causal certainty when the retained evidence is heterogeneous.",
                "Key Findings": "Key findings integrate the retained evidence into a bounded synthesis rather than stitched snippets, and they distinguish stronger review-level support from more tentative primary-study signals.",
                "Limitations": "The main limits are scope, incomplete coverage, heterogeneous certainty, and the possibility of omitted contradictory sources that could materially shift the confidence of the synthesis.",
                "Gaps Identified": "No independent replication has confirmed these synthesis-level findings, and the gap between review-level evidence and applied outcomes remains untested.",
                "Conclusion": "The current evidence supports a cautious synthesis with explicit uncertainty, transparent methodological limits, and no claim that exceeds the retained bundle.",
            },
            "source_bundle": _valid_source_bundle(),
            "author_agent_id": "agent-demo",
            "domain_slug": "longevity",
        },
    ).json()["submission"]
    _run_until_idle(client)
    publications = client.get("/publications")
    assert publications.status_code == 200
    publication = publications.json()["publications"][0]
    detail = client.get(f"/publications/{publication['id']}")
    assert detail.status_code == 200
    assert detail.json()["parent_object_id"] == submission["id"]


def test_submission_timeline(client: TestClient) -> None:
    submission = client.post(
        "/submissions",
        json={
            "title": "Rapid Evidence Synthesis: cellular senescence",
            "abstract": "Bounded external submission.",
            "sections": {
                "Research Question": "This submission asks a bounded research question with enough detail on topic, evidence type, comparator, outcome target, and decision frame that a reviewer could reproduce the intended scope, publication window, and inclusion logic without inventing missing assumptions, broadening the claim, silently changing the relevant evidence category, or misreading the intended publication class for downstream review.",
                "Search Summary": "Databases searched include PubMed and review corpora, with a documented date window, explicit inclusion logic, and a clear narrowing rule that explains why these retained receipts best match the scoped research question.",
                "Evidence Landscape": "The bundle mixes review-level and primary evidence, explains where review-level support dominates the synthesis, and does not overclaim causal certainty when the retained evidence is heterogeneous.",
                "Key Findings": "Key findings integrate the retained evidence into a bounded synthesis rather than stitched snippets, and they distinguish stronger review-level support from more tentative primary-study signals.",
                "Limitations": "The main limits are scope, incomplete coverage, heterogeneous certainty, and the possibility of omitted contradictory sources that could materially shift the confidence of the synthesis.",
                "Gaps Identified": "No independent replication has confirmed these synthesis-level findings, and the gap between review-level evidence and applied outcomes remains untested.",
                "Conclusion": "The current evidence supports a cautious synthesis with explicit uncertainty, transparent methodological limits, and no claim that exceeds the retained bundle.",
            },
            "source_bundle": _valid_source_bundle(),
            "author_agent_id": "agent-demo",
            "domain_slug": "longevity",
        },
    ).json()["submission"]
    _run_until_idle(client)
    timeline = client.get(f"/submissions/{submission['id']}/timeline")
    assert timeline.status_code == 200
    data = timeline.json()
    assert data["submission"]["id"] == submission["id"]
    assert len(data["reviews"]) == 1
    assert len(data["decisions"]) == 1
    assert len(data["publications"]) == 1
    assert data["reviews"][0]["metadata"]["recommendation"] == "accept"


def _minimal_submission_payload() -> dict:
    return {
        "title": "Rapid Evidence Synthesis: test topic",
        "abstract": "Bounded external submission.",
        "sections": {
            "Research Question": "This submission asks a bounded research question with enough detail on topic, evidence type, comparator, outcome target, and decision frame that a reviewer could reproduce the intended scope, publication window, and inclusion logic without inventing missing assumptions, broadening the claim, silently changing the relevant evidence category, or misreading the intended publication class for downstream review.",
            "Search Summary": "Databases searched include PubMed and review corpora, with a documented date window, explicit inclusion logic, and a clear narrowing rule that explains why these retained receipts best match the scoped research question.",
            "Evidence Landscape": "The bundle mixes review-level and primary evidence, explains where review-level support dominates the synthesis, and does not overclaim causal certainty when the retained evidence is heterogeneous.",
            "Key Findings": "Key findings integrate the retained evidence into a bounded synthesis rather than stitched snippets, and they distinguish stronger review-level support from more tentative primary-study signals.",
            "Limitations": "The main limits are scope, incomplete coverage, heterogeneous certainty, and the possibility of omitted contradictory sources that could materially shift the confidence of the synthesis.",
            "Gaps Identified": "No independent replication has confirmed these synthesis-level findings, and the gap between review-level evidence and applied outcomes remains untested.",
            "Conclusion": "The current evidence supports a cautious synthesis with explicit uncertainty, transparent methodological limits, and no claim that exceeds the retained bundle.",
        },
        "source_bundle": [{"title": f"S{i}", "year": 2024, "evidence_type": "review"} for i in range(12)],
        "author_agent_id": "test",
        "domain_slug": "longevity",
    }


def _ops_headers(admin_key: str = "admin-secret-123") -> dict:
    return {"x-api-key": admin_key}


def test_api_key_blocks_unauthorized_submission(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("RESEARKA_V2_API_KEY", "test-key-123")
    response = client.post(
        "/submissions",
        headers={"x-api-key": ""},
        json={
            "title": "Unauthorized",
            "abstract": "Test.",
            "sections": {
                "Research Question": "word " * 50,
                "Search Summary": "x" * 130,
                "Evidence Landscape": "x" * 130,
                "Key Findings": "x" * 130,
                "Limitations": "x" * 130,
                "Gaps Identified": "x" * 130,
                "Conclusion": "x" * 130,
            },
            "source_bundle": [{"title": f"S{i}", "year": 2024, "evidence_type": "review"} for i in range(12)],
            "author_agent_id": "test",
            "domain_slug": "test",
        },
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "missing_api_key"


def test_api_key_allows_authorized_submission(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("RESEARKA_V2_API_KEY", "test-key-123")
    response = client.post(
        "/submissions",
        headers={"x-api-key": "test-key-123"},
        json={
            "title": "Authorized",
            "abstract": "Test.",
            "sections": {
                "Research Question": "word " * 50,
                "Search Summary": "x" * 130,
                "Evidence Landscape": "x" * 130,
                "Key Findings": "x" * 130,
                "Limitations": "x" * 130,
                "Gaps Identified": "x" * 130,
                "Conclusion": "x" * 130,
            },
            "source_bundle": [{"title": f"S{i}", "year": 2024, "evidence_type": "review"} for i in range(12)],
            "author_agent_id": "test",
            "domain_slug": "test",
        },
    )
    assert response.status_code == 200


def test_api_key_rejects_invalid_key(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("RESEARKA_V2_API_KEY", "correct-key")
    response = client.post(
        "/submissions",
        headers={"x-api-key": "wrong-key"},
        json={
            "title": "Bad Key",
            "abstract": "Test.",
            "sections": {
                "Research Question": "word " * 50,
                "Search Summary": "x" * 130,
                "Evidence Landscape": "x" * 130,
                "Key Findings": "x" * 130,
                "Limitations": "x" * 130,
                "Gaps Identified": "x" * 130,
                "Conclusion": "x" * 130,
            },
            "source_bundle": [{"title": f"S{i}", "year": 2024, "evidence_type": "review"} for i in range(12)],
            "author_agent_id": "test",
            "domain_slug": "test",
        },
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "invalid_api_key"


def test_ops_create_key(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("RESEARKA_V2_ADMIN_KEY", "admin-secret-123")
    response = client.post(
        "/ops/keys",
        headers=_ops_headers(),
        json={"agent_id": "agent-1", "label": "pilot-key", "daily_limit": 10},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["agent_id"] == "agent-1"
    assert data["label"] == "pilot-key"
    assert data["daily_limit"] == 10
    assert data["raw_key"].startswith("rk_")
    assert len(data["key_hash"]) == 64  # SHA-256 hex


def test_ops_create_key_requires_admin(client: TestClient) -> None:
    response = client.post(
        "/ops/keys",
        json={"agent_id": "agent-1"},
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "admin_key_required"


def test_ops_list_keys(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("RESEARKA_V2_ADMIN_KEY", "admin-secret-123")
    client.post(
        "/ops/keys",
        headers=_ops_headers(),
        json={"agent_id": "agent-1", "label": "key-a"},
    )
    client.post(
        "/ops/keys",
        headers=_ops_headers(),
        json={"agent_id": "agent-2", "label": "key-b", "daily_limit": 5},
    )
    response = client.get("/ops/keys", headers=_ops_headers())
    assert response.status_code == 200
    keys = response.json()["keys"]
    assert len(keys) == 2
    assert keys[0]["label"] == "key-a"
    assert keys[1]["label"] == "key-b"
    assert all(k["usage_today"] == 0 for k in keys)


def test_ops_revoke_key(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("RESEARKA_V2_ADMIN_KEY", "admin-secret-123")
    create_resp = client.post(
        "/ops/keys",
        headers=_ops_headers(),
        json={"agent_id": "agent-1"},
    )
    key_hash = create_resp.json()["key_hash"]
    response = client.delete(f"/ops/keys/{key_hash}", headers=_ops_headers())
    assert response.status_code == 200
    assert response.json()["revoked"] is True
    # Revoke again should 404
    response2 = client.delete(f"/ops/keys/{key_hash}", headers=_ops_headers())
    assert response2.status_code == 404


def test_per_agent_key_submits_successfully(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("RESEARKA_V2_ADMIN_KEY", "admin-secret-123")
    # Need legacy key for this — or set RESEARKA_V2_API_KEY
    monkeypatch.setenv("RESEARKA_V2_API_KEY", "legacy-key")
    # First, create a per-agent key via admin
    create_resp = client.post(
        "/ops/keys",
        headers=_ops_headers(),
        json={"agent_id": "agent-pilot-1", "label": "pilot"},
    )
    raw_key = create_resp.json()["raw_key"]
    # Now submit using the per-agent key
    response = client.post(
        "/submissions",
        headers={"x-api-key": raw_key},
        json=_minimal_submission_payload(),
    )
    assert response.status_code == 200


def test_per_agent_key_rejected_if_invalid(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("RESEARKA_V2_API_KEY", "legacy-key")
    response = client.post(
        "/submissions",
        headers={"x-api-key": "rk_invalid-key"},
        json=_minimal_submission_payload(),
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "invalid_api_key"


def test_per_agent_key_rejected_if_revoked(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("RESEARKA_V2_ADMIN_KEY", "admin-secret-123")
    create_resp = client.post(
        "/ops/keys",
        headers=_ops_headers(),
        json={"agent_id": "agent-1"},
    )
    raw_key = create_resp.json()["raw_key"]
    key_hash = create_resp.json()["key_hash"]
    # Revoke it
    client.delete(f"/ops/keys/{key_hash}", headers=_ops_headers())
    # Try to submit — need env var to enter fence
    monkeypatch.setenv("RESEARKA_V2_API_KEY", "legacy-key")
    response = client.post(
        "/submissions",
        headers={"x-api-key": raw_key},
        json=_minimal_submission_payload(),
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "invalid_api_key"


def test_per_agent_key_daily_limit_enforced(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("RESEARKA_V2_ADMIN_KEY", "admin-secret-123")
    monkeypatch.setenv("RESEARKA_V2_API_KEY", "legacy-key")
    # Create key with daily_limit=2
    create_resp = client.post(
        "/ops/keys",
        headers=_ops_headers(),
        json={"agent_id": "agent-1", "daily_limit": 2},
    )
    raw_key = create_resp.json()["raw_key"]
    # First submission should work
    resp1 = client.post(
        "/submissions",
        headers={"x-api-key": raw_key},
        json=_minimal_submission_payload(),
    )
    assert resp1.status_code == 200
    # Second submission should work
    payload2 = _minimal_submission_payload()
    payload2["title"] = "Rapid Evidence Synthesis: second test topic"
    resp2 = client.post(
        "/submissions",
        headers={"x-api-key": raw_key},
        json=payload2,
    )
    assert resp2.status_code == 200
    # Third submission should fail (daily limit reached)
    payload3 = _minimal_submission_payload()
    payload3["title"] = "Rapid Evidence Synthesis: third test topic"
    resp3 = client.post(
        "/submissions",
        headers={"x-api-key": raw_key},
        json=payload3,
    )
    assert resp3.status_code == 403
    assert resp3.json()["detail"] == "invalid_api_key"


def test_per_agent_key_usage_tracked_in_list(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("RESEARKA_V2_ADMIN_KEY", "admin-secret-123")
    monkeypatch.setenv("RESEARKA_V2_API_KEY", "legacy-key")
    create_resp = client.post(
        "/ops/keys",
        headers=_ops_headers(),
        json={"agent_id": "agent-1"},
    )
    raw_key = create_resp.json()["raw_key"]
    # Submit once
    client.post(
        "/submissions",
        headers={"x-api-key": raw_key},
        json=_minimal_submission_payload(),
    )
    # Check usage in list
    list_resp = client.get("/ops/keys", headers=_ops_headers())
    assert list_resp.status_code == 200
    keys = list_resp.json()["keys"]
    assert len(keys) == 1
    assert keys[0]["usage_today"] == 1


def test_legacy_key_with_no_per_agent_keys_still_works(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("RESEARKA_V2_API_KEY", "legacy-only")
    response = client.post(
        "/submissions",
        headers={"x-api-key": "legacy-only"},
        json=_minimal_submission_payload(),
    )
    assert response.status_code == 200


def test_ops_summary_empty(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("RESEARKA_V2_ADMIN_KEY", "admin-secret-123")
    resp = client.get("/ops/summary", headers=_ops_headers())
    assert resp.status_code == 200
    data = resp.json()
    assert data["submissions"] == 0
    assert data["decisions"] == {"accept": 0, "revise": 0, "reject": 0}
    assert data["publications"] == 0
    assert data["intake_rejections"] == 0
    assert data["disagreement_rate"] == 0.0
    assert data["avg_cost_usd"] == 0.0


def test_ops_summary_with_data(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("RESEARKA_V2_ADMIN_KEY", "admin-secret-123")
    # Submit and run through to a decision
    payload = _minimal_submission_payload()
    client.post("/submissions", json=payload)
    _run_until_idle(client)
    resp = client.get("/ops/summary", headers=_ops_headers())
    assert resp.status_code == 200
    data = resp.json()
    assert data["submissions"] == 1
    total_decisions = sum(data["decisions"].values())
    assert total_decisions == 1


def test_ops_summary_requires_admin(client: TestClient) -> None:
    resp = client.get("/ops/summary")
    assert resp.status_code == 403
    assert resp.json()["detail"] == "admin_key_required"


def test_per_agent_key_works_without_legacy_env_var(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("RESEARKA_V2_ADMIN_KEY", "admin-secret-123")
    monkeypatch.delenv("RESEARKA_V2_API_KEY", raising=False)
    create_resp = client.post(
        "/ops/keys",
        headers=_ops_headers(),
        json={"agent_id": "agent-pilot-1", "label": "pilot"},
    )
    raw_key = create_resp.json()["raw_key"]
    response = client.post(
        "/submissions",
        headers={"x-api-key": raw_key},
        json=_minimal_submission_payload(),
    )
    assert response.status_code == 200


def test_submission_always_requires_auth(client: TestClient, monkeypatch) -> None:
    monkeypatch.delenv("RESEARKA_V2_API_KEY", raising=False)
    monkeypatch.delenv("RESEARKA_V2_ADMIN_KEY", raising=False)
    response = client.post(
        "/submissions",
        headers={"x-api-key": ""},
        json=_minimal_submission_payload(),
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "missing_api_key"


def test_admin_rejects_submission_key(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("RESEARKA_V2_API_KEY", "submission-key-123")
    monkeypatch.setenv("RESEARKA_V2_ADMIN_KEY", "admin-secret-123")
    resp = client.get("/ops/summary", headers={"x-api-key": "submission-key-123"})
    assert resp.status_code == 403
    assert resp.json()["detail"] == "admin_key_required"
    # Also try creating a key with the submission key
    resp2 = client.post(
        "/ops/keys",
        headers={"x-api-key": "submission-key-123"},
        json={"agent_id": "agent-1"},
    )
    assert resp2.status_code == 403
    assert resp2.json()["detail"] == "admin_key_required"


def test_ops_summary_cost_from_reviews(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("RESEARKA_V2_ADMIN_KEY", "admin-secret-123")
    monkeypatch.delenv("RESEARKA_V2_API_KEY", raising=False)
    from contracts import ObjectType, ResearchObject
    # Inject a review with non-zero cost to simulate real provider usage
    repo = _repository(client)
    review = ResearchObject(
        object_type=ObjectType.REVIEW,
        title="cost-test-review",
        body_markdown="",
        metadata={"cost_usd": 0.05, "recommendation": "accept"},
    )
    repo.create_object(review)
    resp = client.get("/ops/summary", headers=_ops_headers())
    assert resp.status_code == 200
    data = resp.json()
    assert data["avg_cost_usd"] > 0
    assert data["total_cost_usd"] > 0


def test_provenance_returns_structured_scoring(client: TestClient) -> None:
    submission = client.post(
        "/submissions",
        json=_minimal_submission_payload(),
    ).json()["submission"]
    _run_until_idle(client)
    resp = client.get(f"/submissions/{submission['id']}/provenance")
    assert resp.status_code == 200
    data = resp.json()
    assert data["submission_id"] == submission["id"]
    assert len(data["reviews"]) >= 1
    review = data["reviews"][0]
    assert "rubric_scores" in review
    assert isinstance(review["rubric_scores"], dict)
    assert len(review["rubric_scores"]) == 6
    assert review["recommendation"] in {"accept", "revise", "reject"}
    assert review["claim_support_verdict"] is not None
    assert review["overclaim_verdict"] is not None
    assert review["synthesis_quality_verdict"] is not None
    assert isinstance(review["major_issues"], list)
    assert isinstance(review["minor_issues"], list)
    assert review["provider"] is not None
    assert review["created_at"] is not None


def test_provenance_returns_decisions(client: TestClient) -> None:
    submission = client.post(
        "/submissions",
        json=_minimal_submission_payload(),
    ).json()["submission"]
    _run_until_idle(client)
    resp = client.get(f"/submissions/{submission['id']}/provenance")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["decisions"]) >= 1
    decision = data["decisions"][0]
    assert decision["decision"] in {"accept", "revise", "reject"}
    assert isinstance(decision["notes"], list)
    assert decision["review_id"] is not None
    assert decision["created_at"] is not None


def test_provenance_totals_cost(client: TestClient) -> None:
    submission = client.post(
        "/submissions",
        json=_minimal_submission_payload(),
    ).json()["submission"]
    _run_until_idle(client)
    resp = client.get(f"/submissions/{submission['id']}/provenance")
    data = resp.json()
    expected_cost = sum(r.get("cost_usd", 0.0) for r in data["reviews"])
    assert round(data["total_cost_usd"], 4) == round(expected_cost, 4)


def test_provenance_404_for_missing_submission(client: TestClient) -> None:
    resp = client.get("/submissions/nonexistent-id/provenance")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "submission_not_found"


def test_provenance_before_pipeline(client: TestClient) -> None:
    submission = client.post(
        "/submissions",
        json=_minimal_submission_payload(),
    ).json()["submission"]
    # Don't run pipeline — provenance should still return with empty reviews
    resp = client.get(f"/submissions/{submission['id']}/provenance")
    assert resp.status_code == 200
    data = resp.json()
    assert data["reviews"] == []
    assert data["decisions"] == []
    assert data["total_cost_usd"] == 0.0


def test_calibration_no_file(client: TestClient, tmp_path, monkeypatch) -> None:
    from apps.runtime_api.app import reset_calibration_cache

    reset_calibration_cache()
    monkeypatch.setenv("RESEARKA_V2_CALIBRATION_PATH", str(tmp_path / "missing.json"))
    resp = client.get("/calibration")
    assert resp.status_code == 200
    data = resp.json()
    assert data["overall"] == {}
    assert data["by_category"] == {}
    assert data["gate_failures"] == {}
    assert data["mismatch_count"] == 0


def test_calibration_summary(client: TestClient, tmp_path, monkeypatch) -> None:
    from apps.runtime_api.app import reset_calibration_cache

    reset_calibration_cache()
    test_data = {
        "summary": {
            "overall": {
                "total": 50,
                "correct": 30,
                "expected_reject": 30,
                "actual_reject": 50,
                "expected_revise": 10,
                "expected_accept": 10,
                "accuracy": 0.6,
                "cost_usd_total": 1.23,
                "elapsed_sec": 10.0,
            },
            "by_category": {
                "overclaim": {
                    "total": 10,
                    "correct": 10,
                    "expected_reject": 10,
                    "actual_reject": 10,
                    "accuracy": 1.0,
                },
                "good_baseline": {
                    "total": 10,
                    "correct": 0,
                    "expected_accept": 10,
                    "actual_reject": 10,
                    "accuracy": 0.0,
                },
            },
            "gate_failures": {"research_question_word_budget": 50},
            "mismatches": [
                {
                    "index": 1,
                    "title": "Paper A",
                    "category": "good_baseline",
                    "expected": "accept",
                    "actual": "reject",
                    "route": "intake_gate",
                    "stage": "intake",
                    "gate_failures": ["research_question_word_budget"],
                },
            ],
        },
        "results": [],
    }
    cal_path = tmp_path / "calibration_run_v1_results.json"
    cal_path.write_text(__import__("json").dumps(test_data))
    monkeypatch.setenv("RESEARKA_V2_CALIBRATION_PATH", str(cal_path))

    resp = client.get("/calibration")
    assert resp.status_code == 200
    data = resp.json()
    assert data["overall"]["total"] == 50
    assert data["overall"]["correct"] == 30
    assert data["overall"]["accuracy"] == 0.6
    assert "overclaim" in data["by_category"]
    assert data["by_category"]["overclaim"]["accuracy"] == 1.0
    assert data["gate_failures"]["research_question_word_budget"] == 50
    assert data["mismatch_count"] == 1


def test_calibration_no_auth_required(client: TestClient) -> None:
    """Calibration is public — test with a fresh client that has NO default api key."""
    from apps.runtime_api.app import create_app, reset_calibration_cache
    from runtime_core import InMemoryRuntimeRepository

    reset_calibration_cache()
    import importlib
    import apps.runtime_api.app as app_mod

    importlib.reload(app_mod)
    no_auth_client = TestClient(create_app(InMemoryRuntimeRepository()))
    resp = no_auth_client.get("/calibration")
    assert resp.status_code == 200


def test_calibration_mismatches(client: TestClient, tmp_path, monkeypatch) -> None:
    from apps.runtime_api.app import reset_calibration_cache

    reset_calibration_cache()
    test_data = {
        "summary": {
            "overall": {},
            "by_category": {},
            "gate_failures": {},
            "mismatches": [
                {
                    "index": 1,
                    "title": "Paper A",
                    "category": "good_baseline",
                    "expected": "accept",
                    "actual": "reject",
                    "route": "intake_gate",
                    "stage": "intake",
                    "gate_failures": ["research_question_word_budget"],
                },
                {
                    "index": 2,
                    "title": "Paper B",
                    "category": "overclaim",
                    "expected": "reject",
                    "actual": "reject",
                    "route": "review",
                    "stage": "review",
                    "gate_failures": [],
                },
            ],
        },
        "results": [],
    }
    cal_path = tmp_path / "calibration_run_v1_results.json"
    cal_path.write_text(__import__("json").dumps(test_data))
    monkeypatch.setenv("RESEARKA_V2_CALIBRATION_PATH", str(cal_path))

    resp = client.get("/calibration/mismatches")
    assert resp.status_code == 200
    data = resp.json()
    mismatches = data["mismatches"]
    assert len(mismatches) == 2
    assert mismatches[0]["title"] == "Paper A"
    assert mismatches[1]["expected"] == "reject"


def test_calibration_benchmark_format(client: TestClient, tmp_path, monkeypatch) -> None:
    from apps.runtime_api.app import reset_calibration_cache

    reset_calibration_cache()
    benchmark_data = {
        "run_meta": {"run_id": "test-1", "papers": 4},
        "aggregates": {
            "total": 4,
            "completed": 4,
            "correct": 3,
            "accuracy": 0.75,
            "accepts": 3,
            "rejects": 1,
            "accept_rate": 0.75,
            "reject_rate": 0.25,
            "confusion_matrix": {
                "accept": {"accept": 2, "revise": 0, "reject": 0},
                "revise": {"accept": 1, "revise": 0, "reject": 0},
                "reject": {"accept": 0, "revise": 0, "reject": 1},
            },
            "by_quality": {
                "high": {"count": 2, "expected_decision": "accept", "accuracy": 1.0, "accept_rate": 1.0, "reject_rate": 0.0},
                "medium": {"count": 1, "expected_decision": "revise", "accuracy": 0.0, "accept_rate": 1.0, "reject_rate": 0.0},
                "low": {"count": 1, "expected_decision": "reject", "accuracy": 1.0, "accept_rate": 0.0, "reject_rate": 1.0},
            },
            "by_domain": {
                "longevity": {"count": 1, "accept": 1, "reject": 0},
            },
        },
        "papers": [
            {"paper_id": 1, "quality": "high", "expected_decision": "accept", "domain": "longevity", "decision": "accept", "outcome": "accept"},
            {"paper_id": 2, "quality": "high", "expected_decision": "accept", "domain": "ai-ethics", "decision": "accept", "outcome": "accept"},
            {"paper_id": 3, "quality": "medium", "expected_decision": "revise", "domain": "climate", "decision": "accept", "outcome": "accept"},
            {"paper_id": 4, "quality": "low", "expected_decision": "reject", "domain": "energy", "decision": "reject", "outcome": "reject", "stage_reached": "review", "error": "gate_timeout"},
        ],
    }
    bench_path = tmp_path / "benchmark_baseline.json"
    bench_path.write_text(__import__("json").dumps(benchmark_data))
    monkeypatch.setenv("RESEARKA_V2_CALIBRATION_PATH", str(bench_path))

    resp = client.get("/calibration")
    assert resp.status_code == 200
    data = resp.json()
    assert data["overall"]["total"] == 4
    assert data["overall"]["correct"] == 3
    assert data["overall"]["accept_rate"] == 0.75
    assert data["confusion_matrix"]["revise"]["accept"] == 1
    assert "high" in data["by_category"]
    # Paper 4 has error, counted as gate failure
    assert data["gate_failures"] == {"review": [4]}
    # Paper 3: medium quality, expected revise, actual accept → mismatch
    assert data["mismatch_count"] == 1

    resp2 = client.get("/calibration/mismatches")
    assert resp2.status_code == 200
    mismatches = resp2.json()["mismatches"]
    assert len(mismatches) == 1
    assert mismatches[0]["paper_id"] == 3
    assert mismatches[0]["expected"] == "revise"
    assert mismatches[0]["actual"] == "accept"


def _submit_and_process(client: TestClient) -> str:
    submission = client.post(
        "/submissions",
        json={
            "title": "Rapid Evidence Synthesis: cellular senescence",
            "abstract": "Bounded external submission.",
            "sections": {
                "Research Question": "This submission asks a bounded research question with enough detail on topic, evidence type, comparator, outcome target, and decision frame that a reviewer could reproduce the intended scope, publication window, and inclusion logic without inventing missing assumptions, broadening the claim, silently changing the relevant evidence category, or misreading the intended publication class for downstream review.",
                "Search Summary": "Databases searched include PubMed and review corpora, with a documented date window, explicit inclusion logic, and a clear narrowing rule that explains why these retained receipts best match the scoped research question.",
                "Evidence Landscape": "The bundle mixes review-level and primary evidence, explains where review-level support dominates the synthesis, and does not overclaim causal certainty when the retained evidence is heterogeneous.",
                "Key Findings": "Key findings integrate the retained evidence into a bounded synthesis rather than stitched snippets, and they distinguish stronger review-level support from more tentative primary-study signals.",
                "Limitations": "The main limits are scope, incomplete coverage, heterogeneous certainty, and the possibility of omitted contradictory sources that could materially shift the confidence of the synthesis.",
                "Gaps Identified": "No independent replication has confirmed these synthesis-level findings, and the gap between review-level evidence and applied outcomes remains untested.",
                "Conclusion": "The current evidence supports a cautious synthesis with explicit uncertainty, transparent methodological limits, and no claim that exceeds the retained bundle.",
            },
            "source_bundle": _valid_source_bundle(),
            "author_agent_id": "agent-demo",
            "domain_slug": "longevity",
        },
    ).json()["submission"]
    _run_until_idle(client)
    return submission["id"]


def test_submit_audit_review(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("RESEARKA_V2_ADMIN_KEY", "admin-secret-123")
    sub_id = _submit_and_process(client)
    resp = client.post(
        f"/audit/{sub_id}",
        headers=_ops_headers(),
        json={
            "auditor_id": "auditor-alpha",
            "auditor_verdict": "reject",
            "auditor_notes": "Missing citation support",
            "confidence": 0.85,
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["submission_id"] == sub_id
    assert data["auditor_id"] == "auditor-alpha"
    assert data["auditor_verdict"] == "reject"
    assert data["auditor_notes"] == "Missing citation support"
    assert data["confidence"] == 0.85
    assert data["verdict_match"] in ("agree", "disagree")


def test_audit_summary_empty(client: TestClient) -> None:
    resp = client.get("/audit-summary")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_audits"] == 0
    assert data["agreement_rate"] == 0.0


def test_audit_summary_after_review(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("RESEARKA_V2_ADMIN_KEY", "admin-secret-123")
    sub_id = _submit_and_process(client)
    client.post(
        f"/audit/{sub_id}",
        headers=_ops_headers(),
        json={"auditor_id": "auditor-a", "auditor_verdict": "reject", "auditor_notes": "notes", "confidence": 0.9},
    )
    client.post(
        f"/audit/{sub_id}",
        headers=_ops_headers(),
        json={"auditor_id": "auditor-b", "auditor_verdict": "reject", "auditor_notes": "more notes", "confidence": 0.7},
    )
    resp = client.get("/audit-summary")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_audits"] == 2
    assert 0.0 <= data["agreement_rate"] <= 1.0
    assert "auditor-a" in data["by_auditor"]
    assert "auditor-b" in data["by_auditor"]


def test_list_audit_reviews_by_submission(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("RESEARKA_V2_ADMIN_KEY", "admin-secret-123")
    sub_id = _submit_and_process(client)
    client.post(
        f"/audit/{sub_id}",
        headers=_ops_headers(),
        json={"auditor_id": "auditor-x", "auditor_verdict": "reject", "auditor_notes": "x", "confidence": 1.0},
    )
    resp = client.get(f"/audit/{sub_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["reviews"]) == 1
    assert data["reviews"][0]["auditor_id"] == "auditor-x"


def test_audit_rejects_without_admin(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("RESEARKA_V2_ADMIN_KEY", "admin-secret-123")
    sub_id = _submit_and_process(client)
    resp = client.post(
        f"/audit/{sub_id}",
        json={"auditor_id": "auditor-x", "auditor_verdict": "reject", "auditor_notes": "x", "confidence": 1.0},
    )
    assert resp.status_code == 403


def test_audit_rejects_invalid_verdict(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("RESEARKA_V2_ADMIN_KEY", "admin-secret-123")
    sub_id = _submit_and_process(client)
    resp = client.post(
        f"/audit/{sub_id}",
        headers=_ops_headers(),
        json={"auditor_id": "auditor-x", "auditor_verdict": "invalid_verdict", "auditor_notes": "x", "confidence": 1.0},
    )
    assert resp.status_code == 400
