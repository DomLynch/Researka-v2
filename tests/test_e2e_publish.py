from typing import Any, cast

from fastapi.testclient import TestClient

from contracts import ResearchObject, RuntimeJob, Stage
from runtime_core.prompts import EDITOR_PROMPT_VERSION

VALID_ORCID = "0000-0002-1825-0097"


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

    for _ in range(12):
        queue = client.get("/jobs/queue").json()["queued"]
        if not queue:
            break
        response = client.post("/jobs/run-once")
        assert response.status_code == 200

    publications = repository.list_objects("publication")
    assert len(publications) == 1
    publication = publications[0]
    assert "the search summary is incomplete" not in publication.body_markdown.lower()
    assert repository.publication_for_target(publication.parent_object_id).id == publication.id
    assert publication.parent_object_id == submission_id
    assert publication.metadata["prompt_version"] == EDITOR_PROMPT_VERSION

    repository.enqueue_job(RuntimeJob(target_object_id=publication.parent_object_id, stage=Stage.PUBLISH))
    duplicate_publish = client.post("/jobs/run-once")
    assert duplicate_publish.status_code == 200
    assert len(repository.list_objects("publication")) == 1
    stages_seen = {event.payload["stage"] for event in repository.list_events() if "stage" in event.payload}
    assert stages_seen == {"submission_intake", "autonomous_review", "autonomous_editorial_decision", "autonomous_publish"}


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
    client.post("/jobs/run-once")
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
            "Databases searched include PubMed and review corpora, with a documented date window, explicit inclusion logic, and a stated narrowing rule that explains why these retained receipts best match the scoped research question."
        ),
    )
    assert seed2.status_code == 200

    for _ in range(12):
        queue = client.get("/jobs/queue").json()["queued"]
        if not queue:
            break
        client.post("/jobs/run-once")

    publications = _repository(client).list_objects("publication")
    assert len(publications) == 1
    assert publications[0].id == first_pub_id


def test_publication_carries_orcid_and_osf_pending_metadata(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("RESEARKA_V2_ADMIN_KEY", "admin-secret-123")
    create_resp = client.post(
        "/ops/keys",
        headers={"x-api-key": "admin-secret-123"},
        json={"agent_id": "agent-v3-full-paper", "owner_name": "Dominic Lynch", "owner_orcid": VALID_ORCID},
    )
    raw_key = create_resp.json()["raw_key"]
    seed = client.post(
        "/submissions",
        headers={"x-api-key": raw_key},
        json=_submission_payload(
            "Databases searched include PubMed and review corpora, with a documented date window, explicit inclusion logic, and a stated narrowing rule that explains why these retained receipts best match the scoped research question."
        ),
    )
    assert seed.status_code == 200

    for _ in range(12):
        queue = client.get("/jobs/queue").json()["queued"]
        if not queue:
            break
        assert client.post("/jobs/run-once").status_code == 200

    publication = _repository(client).list_objects("publication")[0]
    assert publication.metadata["author_agent_id"] == "agent-v3-full-paper"
    assert publication.metadata["human_owner_name"] == "Dominic Lynch"
    assert publication.metadata["orcid"] == VALID_ORCID
    assert publication.metadata["doi"] is None
    assert publication.metadata["doi_status"] == "pending_osf_credentials"
    assert publication.metadata["osf"]["status"] == "pending_osf_credentials"
    assert "dw_chain_url" not in publication.metadata
    assert "content_hash" not in publication.metadata


def test_publish_attaches_real_derivation_web_metadata(client: TestClient, monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_emit_publication_chain(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        publication = cast(ResearchObject, kwargs["publication"])
        return {
            "dw_artifact_id": "art_dw_publication",
            "dw_chain_url": "https://provenance.researka.org/artifacts/art_dw_publication/chain",
            "dw_api_chain_url": "https://provenance.researka.org/api/artifacts/art_dw_publication/chain",
            "dw_source_artifact_id": "art_dw_submission",
            "dw_step_id": "step_dw_accept",
            "dw_step_hash": "abc123",
            "dw_status": "registered",
            "content_hash": "sha256:real-dw-hash",
            "sha256": "sha256:real-dw-hash",
            "publication_id_seen": publication.id,
        }

    monkeypatch.setattr("runtime_core.workflow.emit_publication_chain", fake_emit_publication_chain)

    seed = client.post(
        "/submissions",
        json=_submission_payload(
            "Databases searched include PubMed and review corpora, with a documented date window, explicit inclusion logic, and a stated narrowing rule that explains why these retained receipts best match the scoped research question."
        ),
    )
    assert seed.status_code == 200

    for _ in range(12):
        queue = client.get("/jobs/queue").json()["queued"]
        if not queue:
            break
        assert client.post("/jobs/run-once").status_code == 200

    publication = _repository(client).list_objects("publication")[0]
    assert publication.metadata["dw_status"] == "registered"
    assert publication.metadata["dw_artifact_id"] == "art_dw_publication"
    assert publication.metadata["dw_chain_url"] == "https://provenance.researka.org/artifacts/art_dw_publication/chain"
    assert publication.metadata["content_hash"] == "sha256:real-dw-hash"
    captured_submission = cast(ResearchObject, captured["submission"])
    captured_publication = cast(ResearchObject, captured["publication"])
    assert captured_submission.id == publication.parent_object_id
    assert captured_publication.id == publication.id
    assert captured["review"] is not None
    assert captured["decision"] is not None
