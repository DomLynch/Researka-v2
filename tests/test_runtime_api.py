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


def test_api_key_blocks_unauthorized_submission(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("RESEARKA_V2_API_KEY", "test-key-123")
    response = client.post(
        "/submissions",
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
    assert response.json()["detail"] == "invalid_api_key"


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
