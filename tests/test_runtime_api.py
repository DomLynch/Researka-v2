from fastapi.testclient import TestClient


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


def test_architecture(client: TestClient) -> None:
    response = client.get("/architecture")
    assert response.status_code == 200
    data = response.json()
    assert "runtime_core" in data["top_level_modules"]
    assert data["critical_flow"] == ["intake", "review", "editorial", "publish"]


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
    repo = client.app.state.repository
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
