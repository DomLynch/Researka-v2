import os
from typing import Any, cast

from fastapi.testclient import TestClient

from contracts import Decision, EventType, ObjectType, ResearchObject, RuntimeEvent, RuntimeJob, Stage
from runtime_core.prompts import EDITOR_PROMPT_VERSION


def _repository(client: TestClient) -> Any:
    return cast(Any, client.app).state.repository


def _valid_source_bundle() -> list[dict[str, object]]:
    years = (2024, 2023, 2022, 2021, 2020, 2024, 2023, 2022, 2021, 2019, 2018, 2017)
    evidence_types = ("review",) * 6 + ("primary",) * 6
    return [
        {
            "title": f"{evidence_type.title()} source {index}",
            "doi": f"10.1234/e2e.{index}",
            "year": year,
            "evidence_type": evidence_type,
        }
        for index, (year, evidence_type) in enumerate(zip(years, evidence_types, strict=True), start=1)
    ]


def _worker_headers() -> dict[str, str]:
    return {"x-api-key": os.environ.get("RESEARKA_V2_ADMIN_KEY", "test-admin-key")}


def _submission_payload(search_summary: str) -> dict:
    return {
        "title": "Rapid Evidence Synthesis: cellular senescence and mitochondrial dysfunction",
        "abstract": "Bounded external submission.",
        "sections": {
            "Research Question": "This submission asks a bounded research question with enough detail on topic, evidence type, comparator, outcome target, and decision frame that a reviewer could reproduce the intended scope, publication window, and inclusion logic without inventing missing assumptions, broadening the claim, silently changing the relevant evidence category, or misreading the intended publication class for downstream review.",
            "Search Summary": search_summary,
            "Evidence Landscape": "The bundle mixes review-level and primary evidence, explains where umbrella or systematic reviews dominate the signal, and avoids overclaiming causal certainty when the retained evidence is heterogeneous or indirect.",
            "Key Findings": "Key findings integrate the retained evidence into a bounded synthesis rather than stitched snippets, and they explicitly separate stronger review-level support from tentative applied or primary-study signals.",
            "Limitations": "The main limits are scope, incomplete coverage, heterogeneous certainty, and the possibility of omitted contradictory sources that could shift the strength of any cautious conclusion.",
            "Gaps Identified": "No replication study has validated these findings outside the primary evidence population, and the translational gap between review-level synthesis and applied outcomes remains untested.",
            "Conclusion": "The current evidence supports a cautious synthesis with explicit uncertainty, constrained claims, and a transparent acknowledgement that this is a rapid evidence product rather than a definitive systematic review.",
        },
        "source_bundle": _valid_source_bundle(),
        "author_agent_id": "agent-demo",
        "domain_slug": "longevity",
        "category": "longevity",
        "topic": "cellular_senescence",
    }


def _assert_publish_happy_path(client: TestClient) -> None:
    repository = _repository(client)
    seed = client.post(
        "/submissions",
        json=_submission_payload(
            "Databases searched include PubMed and review corpora, with a documented date window, explicit inclusion logic, and a stated narrowing rule that explains why these retained receipts best match the scoped research question."
        ),
    )
    assert seed.status_code == 200
    submission_id = seed.json()["submission"]["id"]
    submission_metadata = seed.json()["submission"]["metadata"]
    assert submission_metadata["domain_slug"] == "longevity"
    assert submission_metadata["category"] == "longevity"
    assert submission_metadata["topic"] == "cellular_senescence"

    for _ in range(12):
        queue = client.get("/jobs/queue", headers=_worker_headers()).json()["queued"]
        if not queue:
            break
        response = client.post("/jobs/run-once", headers=_worker_headers())
        assert response.status_code == 200

    publications = repository.list_objects("publication")
    assert len(publications) == 1
    publication = publications[0]
    assert "the search summary is incomplete" not in publication.body_markdown.lower()
    assert repository.publication_for_target(publication.parent_object_id).id == publication.id
    assert publication.parent_object_id == submission_id
    assert publication.metadata["prompt_version"] == EDITOR_PROMPT_VERSION
    assert publication.metadata["domain_slug"] == "longevity"
    assert publication.metadata["category"] == "longevity"
    assert publication.metadata["topic"] == "cellular_senescence"
    decision = client.get(f"/submissions/{submission_id}/decision")
    assert decision.status_code == 200
    decision_payload = decision.json()
    assert decision_payload["decision"] == "accept"
    assert decision_payload["resubmission"] == {"allowed": False, "parent_submission_id": None}
    assert decision_payload["publication"]["publication_id"] == publication.id
    assert decision_payload["publication"]["url"] == f"https://researka.org/papers/{publication.id}"
    assert decision_payload["publication"]["deduped"] is False

    repository.enqueue_job(RuntimeJob(target_object_id=publication.parent_object_id, stage=Stage.PUBLISH))
    duplicate_publish = client.post("/jobs/run-once", headers=_worker_headers())
    assert duplicate_publish.status_code == 200
    assert len(repository.list_objects("publication")) == 1
    stages_seen = {event.payload["stage"] for event in repository.list_events() if "stage" in event.payload}
    assert stages_seen == {"submission_intake", "autonomous_review", "autonomous_editorial_decision", "autonomous_publish"}


def test_public_review_record_preserves_submission_domain_metadata(client: TestClient) -> None:
    repository = _repository(client)
    submission = repository.create_object(
        ResearchObject(
            object_type=ObjectType.SUBMISSION,
            title="MedQA benchmark memo",
            metadata={
                "article_type": "alpha_memo",
                "author_agent_id": "agent-v4-alpha-ai-research",
                "category": "ai",
                "domain_slug": "ai_research",
                "topic": "medqa_benchmark",
            },
        )
    )
    review = repository.create_object(
        ResearchObject(
            object_type=ObjectType.REVIEW,
            parent_object_id=submission.id,
            title="Review for MedQA benchmark memo",
            metadata={"major_issues": ["Needs tighter metric role separation."]},
        )
    )
    decision = repository.create_object(
        ResearchObject(
            object_type=ObjectType.DECISION,
            parent_object_id=submission.id,
            title="Decision for MedQA benchmark memo",
            metadata={
                "decision": Decision.REJECT.value,
                "review_id": review.id,
            },
        )
    )

    response = client.get(f"/reviews/{decision.id}")

    assert response.status_code == 200
    body = response.json()
    assert body["topic"] == "medqa_benchmark"
    assert body["domain_slug"] == "ai_research"
    assert body["category"] == "ai"


def test_submission_api_preserves_v4_alpha_category_metadata(client: TestClient) -> None:
    payload = {
        "artifact_type": "alpha_memo",
        "article_type": "alpha_memo",
        "author_agent_id": "agent-v4-alpha-ai-research",
        "agent_id": "agent-v4-alpha-ai-research",
        "domain_slug": "ai_research",
        "category": "ai",
        "topic": "medqa_benchmark",
        "metadata": {
            "article_type": "alpha_memo",
            "category": "ai",
            "domain_slug": "ai_research",
            "topic": "medqa_benchmark",
        },
        "title": "MedQA benchmark memo",
        "abstract": "A bounded alpha memo on a benchmark-specific finding.",
        "markdown": "# Alpha memo\n\nBounded benchmark memo.\n",
        "source_bundle": [
            {
                "title": f"MedQA source {index}",
                "doi": f"10.1234/medqa.{index}",
                "evidence_type": "primary",
                "year": 2025,
            }
            for index in range(1, 6)
        ],
    }

    response = client.post("/submissions", json=payload)

    assert response.status_code == 200
    metadata = response.json()["submission"]["metadata"]
    assert metadata["article_type"] == "alpha_memo"
    assert metadata["domain_slug"] == "ai_research"
    assert metadata["category"] == "ai"
    assert metadata["topic"] == "medqa_benchmark"


def test_decision_response_reports_deduped_publication(client: TestClient) -> None:
    repository = _repository(client)
    original_submission = repository.create_object(
        ResearchObject(object_type=ObjectType.SUBMISSION, title="Original semaglutide memo")
    )
    publication = repository.create_object(
        ResearchObject(
            object_type=ObjectType.PUBLICATION,
            parent_object_id=original_submission.id,
            title="Semaglutide memo",
            metadata={"article_type": "alpha_memo", "doi_status": "minted"},
        )
    )
    duplicate_submission = repository.create_object(
        ResearchObject(object_type=ObjectType.SUBMISSION, title="Duplicate semaglutide memo")
    )
    decision = repository.create_object(
        ResearchObject(
            object_type=ObjectType.DECISION,
            parent_object_id=duplicate_submission.id,
            title="Decision for duplicate semaglutide memo",
            metadata={"decision": Decision.ACCEPT.value, "notes": ["accepted and queued for publish"]},
        )
    )
    repository.record_event(
        RuntimeEvent(
            event_type=EventType.JOB_COMPLETED,
            target_object_id=duplicate_submission.id,
            payload={"stage": Stage.PUBLISH.value, "publication_id": publication.id, "deduped": True},
        )
    )

    response = client.get(f"/submissions/{duplicate_submission.id}/decision")

    assert response.status_code == 200
    payload = response.json()
    assert payload["decision_object_id"] == decision.id
    assert payload["publication"]["publication_id"] == publication.id
    assert payload["publication"]["url"] == f"https://researka.org/alpha/{publication.id}"
    assert payload["publication"]["deduped"] is True


def test_end_to_end_publish_happy_path(client: TestClient) -> None:
    _assert_publish_happy_path(client)


def test_end_to_end_publish_happy_path_postgres(postgres_client: TestClient) -> None:
    _assert_publish_happy_path(postgres_client)


def test_leakage_submission_is_rejected_at_intake(client: TestClient) -> None:
    response = client.post(
        "/submissions",
        json=_submission_payload("The Search Summary is incomplete and lacks reproducibility."),
    )
    submission_id = response.json()["submission"]["id"]
    client.post("/jobs/run-once", headers=_worker_headers())
    decision = client.get(f"/submissions/{submission_id}/decision").json()
    assert decision["status"] == "complete"
    assert decision["decision"] == "reject"
    assert decision["gate_failures"]
    assert _repository(client).list_objects("review") == []


def test_duplicate_title_blocked_at_publish(client: TestClient) -> None:
    _assert_publish_happy_path(client)
    publications = _repository(client).list_objects("publication")
    assert len(publications) == 1
    first_pub_id = publications[0].id

    seed2 = client.post(
        "/submissions",
        json=_submission_payload(
            "Databases searched include PubMed, review corpora, and citation chaining, with the same title retained to verify the publish-stage title duplicate guard without submitting an exact content duplicate."
        ),
    )
    assert seed2.status_code == 200

    for _ in range(12):
        queue = client.get("/jobs/queue", headers=_worker_headers()).json()["queued"]
        if not queue:
            break
        client.post("/jobs/run-once", headers=_worker_headers())

    publications = _repository(client).list_objects("publication")
    assert len(publications) == 1
    assert publications[0].id == first_pub_id


def test_end_to_end_publish_uses_full_body_when_present(client: TestClient) -> None:
    payload = _submission_payload(
        "Databases searched include PubMed and review corpora, with a documented date window, explicit inclusion logic, and a stated narrowing rule that explains why these retained receipts best match the scoped research question."
    )
    payload["body_markdown"] = "\n\n".join(
        [
            "# Full manuscript",
            "## Abstract\n\nThis abstract is long enough to describe the accepted evidence synthesis and its bounded interpretation for public release.",
            "## Methods\n\nThe methods describe retrieval, screening, extraction, appraisal, synthesis, and verification in enough detail for audit by a public reader.",
            "## Results\n\nThe results preserve the full manuscript evidence narrative instead of collapsing the paper into a shortened publication section map.",
            "## Limitations\n\nThe limitations identify scope restrictions, missing endpoints, uncertainty, and interpretation risks that constrain public claims.",
            "## Conclusion\n\nThe conclusion states the bounded finding and separates supported claims from unresolved evidence gaps and future research needs.",
            "## References\n\n- Example 2024. DOI: 10.1234/example. PMID: 12345678.",
        ]
    )
    seed = client.post("/submissions", json=payload)
    assert seed.status_code == 200
    for _ in range(12):
        if not client.get("/jobs/queue", headers=_worker_headers()).json()["queued"]:
            break
        assert client.post("/jobs/run-once", headers=_worker_headers()).status_code == 200
    publication = _repository(client).list_objects("publication")[0]
    assert "## References" in publication.body_markdown
    assert "DOI: 10.1234/example" in publication.body_markdown


def test_publication_mints_osf_doi_before_derivation_web_metadata(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("RESEARKA_V2_OSF_PROJECT_ID", "root-osf-node")
    monkeypatch.setenv("RESEARKA_V2_OSF_TOKEN", "test-token")
    captured: dict[str, object] = {}

    def fake_mint_publication_doi(publication: ResearchObject) -> dict[str, object]:
        assert publication.metadata["doi_status"] == "pending_osf_export"
        return {
            "doi": "10.17605/OSF.IO/ABC12",
            "doi_status": "minted",
            "osf_status": "minted",
            "osf_project_id": "root-osf-node",
            "osf_guid": "abc12",
            "osf_url": "https://osf.io/abc12/",
            "osf": {
                "enabled": True,
                "status": "minted",
                "project_id": "root-osf-node",
                "guid": "abc12",
                "url": "https://osf.io/abc12/",
                "doi": "10.17605/OSF.IO/ABC12",
            },
        }

    def fake_emit_publication_to_derivation_web(**kwargs: object) -> dict[str, object]:
        publication = cast(ResearchObject, kwargs["publication"])
        captured["doi_seen_by_dw"] = publication.metadata.get("doi")
        return {
            "dw_artifact_id": "art_dw_publication",
            "dw_chain_url": "https://provenance.researka.org/artifacts/art_dw_publication/chain",
            "content_hash": "sha256:real-dw-hash",
            "sha256": "sha256:real-dw-hash",
        }

    monkeypatch.setattr("runtime_core.osf.mint_publication_doi", fake_mint_publication_doi)
    monkeypatch.setattr("runtime_core.workflow.emit_publication_to_derivation_web", fake_emit_publication_to_derivation_web)

    seed = client.post(
        "/submissions",
        json=_submission_payload(
            "Databases searched include PubMed and review corpora, with a documented date window, explicit inclusion logic, and a stated narrowing rule that explains why these retained receipts best match the scoped research question."
        ),
    )
    assert seed.status_code == 200

    for _ in range(12):
        queue = client.get("/jobs/queue", headers=_worker_headers()).json()["queued"]
        if not queue:
            break
        assert client.post("/jobs/run-once", headers=_worker_headers()).status_code == 200

    publication = cast(Any, client.app).state.repository.list_objects("publication")[0]
    assert publication.metadata["doi"] == "10.17605/OSF.IO/ABC12"
    assert publication.metadata["doi_status"] == "minted"
    assert publication.metadata["osf_guid"] == "abc12"
    assert publication.metadata["dw_artifact_id"] == "art_dw_publication"
    assert captured["doi_seen_by_dw"] == "10.17605/OSF.IO/ABC12"


def test_publication_uses_connected_osf_oauth_token_before_service_token(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("RESEARKA_V2_ADMIN_KEY", "admin-secret-123")
    create_resp = client.post(
        "/ops/keys",
        headers={"x-api-key": "admin-secret-123"},
        json={"agent_id": "agent-v3-full-paper"},
    )
    raw_key = create_resp.json()["raw_key"]
    _repository(client).store_osf_oauth_token("agent-v3-full-paper", {"access_token": "oauth-token"})

    def fake_mint_publication_doi(publication: ResearchObject) -> dict[str, object]:
        raise AssertionError("service token fallback should not run when agent OAuth is connected")

    def fake_mint_publication_doi_with_oauth(publication: ResearchObject, *, token_metadata: dict) -> tuple[dict[str, object], dict[str, object]]:
        assert token_metadata["access_token"] == "oauth-token"
        return (
            {
                "doi": "10.17605/OSF.IO/OAUTH1",
                "doi_status": "minted",
                "osf_status": "minted",
                "osf_project_id": "oauth-root",
                "osf_guid": "oauth1",
                "osf_auth_source": "oauth_agent_token",
                "osf": {"enabled": True, "status": "minted", "project_id": "oauth-root", "guid": "oauth1"},
            },
            {**token_metadata, "root_project_id": "oauth-root"},
        )

    monkeypatch.setattr("runtime_core.osf.mint_publication_doi", fake_mint_publication_doi)
    monkeypatch.setattr("runtime_core.osf.mint_publication_doi_with_oauth", fake_mint_publication_doi_with_oauth)

    seed = client.post(
        "/submissions",
        headers={"x-api-key": raw_key},
        json=_submission_payload(
            "Databases searched include PubMed and review corpora, with a documented date window, explicit inclusion logic, and a stated narrowing rule that explains why these retained receipts best match the scoped research question."
        ),
    )
    assert seed.status_code == 200

    for _ in range(12):
        queue = client.get("/jobs/queue", headers=_worker_headers()).json()["queued"]
        if not queue:
            break
        assert client.post("/jobs/run-once", headers=_worker_headers()).status_code == 200

    publication = _repository(client).list_objects("publication")[0]
    assert publication.metadata["doi"] == "10.17605/OSF.IO/OAUTH1"
    assert publication.metadata["osf_auth_source"] == "oauth_agent_token"
    assert _repository(client).get_osf_oauth_token("agent-v3-full-paper")["root_project_id"] == "oauth-root"


def test_publication_uses_default_osf_oauth_agent_when_submitter_is_not_connected(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("RESEARKA_V2_ADMIN_KEY", "admin-secret-123")
    monkeypatch.setenv("RESEARKA_V2_OSF_DEFAULT_AGENT_ID", "agent-v4-alpha-memo")
    create_resp = client.post(
        "/ops/keys",
        headers={"x-api-key": "admin-secret-123"},
        json={"agent_id": "agent-v4-alpha-longevity-research"},
    )
    raw_key = create_resp.json()["raw_key"]
    _repository(client).store_osf_oauth_token("agent-v4-alpha-memo", {"access_token": "default-oauth-token", "root_project_id": "oauth-root"})

    def fake_mint_publication_doi_with_oauth(publication: ResearchObject, *, token_metadata: dict) -> tuple[dict[str, object], dict[str, object]]:
        assert token_metadata["access_token"] == "default-oauth-token"
        return (
            {
                "doi": "10.17605/OSF.IO/DEF01",
                "doi_status": "minted",
                "osf_status": "minted",
                "osf_project_id": "oauth-root",
                "osf_guid": "def01",
                "osf_auth_source": "oauth_agent_token",
                "osf": {"enabled": True, "status": "minted", "project_id": "oauth-root", "guid": "def01"},
            },
            token_metadata,
        )

    monkeypatch.setattr("runtime_core.osf.mint_publication_doi_with_oauth", fake_mint_publication_doi_with_oauth)

    seed = client.post(
        "/submissions",
        headers={"x-api-key": raw_key},
        json=_submission_payload(
            "Databases searched include PubMed and review corpora, with a documented date window, explicit inclusion logic, and a stated narrowing rule that explains why these retained receipts best match the scoped research question."
        ),
    )
    assert seed.status_code == 200

    for _ in range(12):
        queue = client.get("/jobs/queue", headers=_worker_headers()).json()["queued"]
        if not queue:
            break
        assert client.post("/jobs/run-once", headers=_worker_headers()).status_code == 200

    publication = _repository(client).list_objects("publication")[0]
    assert publication.metadata["doi"] == "10.17605/OSF.IO/DEF01"
    assert publication.metadata["osf_auth_source"] == "oauth_default_agent_token"
    assert publication.metadata["osf_agent_id"] == "agent-v4-alpha-memo"


def test_osf_failure_marks_publication_without_blocking_derivation_web(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("RESEARKA_V2_OSF_PROJECT_ID", "root-osf-node")
    monkeypatch.setenv("RESEARKA_V2_OSF_TOKEN", "revoked-token")
    captured: dict[str, object] = {}

    def fake_mint_publication_doi(publication: ResearchObject) -> dict[str, object]:
        raise RuntimeError("osf_request_failed:POST:/nodes/root-osf-node/identifiers/:403:Forbidden")

    def fake_emit_publication_to_derivation_web(**kwargs: object) -> dict[str, object]:
        publication = cast(ResearchObject, kwargs["publication"])
        captured["doi_status_seen_by_dw"] = publication.metadata.get("doi_status")
        return {
            "dw_artifact_id": "art_dw_publication",
            "dw_chain_url": "https://provenance.researka.org/artifacts/art_dw_publication/chain",
            "content_hash": "sha256:real-dw-hash",
            "sha256": "sha256:real-dw-hash",
        }

    monkeypatch.setattr("runtime_core.osf.mint_publication_doi", fake_mint_publication_doi)
    monkeypatch.setattr("runtime_core.workflow.emit_publication_to_derivation_web", fake_emit_publication_to_derivation_web)

    seed = client.post(
        "/submissions",
        json=_submission_payload(
            "Databases searched include PubMed and review corpora, with a documented date window, explicit inclusion logic, and a stated narrowing rule that explains why these retained receipts best match the scoped research question."
        ),
    )
    assert seed.status_code == 200

    for _ in range(12):
        queue = client.get("/jobs/queue", headers=_worker_headers()).json()["queued"]
        if not queue:
            break
        assert client.post("/jobs/run-once", headers=_worker_headers()).status_code == 200

    publication = _repository(client).list_objects("publication")[0]
    assert publication.metadata["doi"] is None
    assert publication.metadata["doi_status"] == "failed"
    assert publication.metadata["osf_status"] == "failed"
    assert publication.metadata["osf_error"].startswith("osf_request_failed")
    assert publication.metadata["dw_artifact_id"] == "art_dw_publication"
    assert captured["doi_status_seen_by_dw"] == "failed"


def test_publish_attaches_derivation_web_publication_metadata(client: TestClient, monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_emit_publication_to_derivation_web(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return {
            "dw_artifact_id": "art_publication",
            "dw_chain_url": "https://provenance.researka.org/artifacts/art_publication/chain",
            "dw_api_chain_url": "https://provenance.researka.org/api/artifacts/art_publication/chain",
            "dw_status": "registered",
            "content_hash": "sha256:abc123",
            "sha256": "sha256:abc123",
        }

    monkeypatch.setattr("runtime_core.workflow.emit_publication_to_derivation_web", fake_emit_publication_to_derivation_web)
    _assert_publish_happy_path(client)

    publication = cast(Any, client.app).state.repository.list_objects("publication")[0]
    assert publication.metadata["dw_status"] == "registered"
    assert publication.metadata["dw_artifact_id"] == "art_publication"
    assert publication.metadata["dw_chain_url"] == "https://provenance.researka.org/artifacts/art_publication/chain"
    assert publication.metadata["sha256"] == "sha256:abc123"
