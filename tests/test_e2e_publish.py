from fastapi.testclient import TestClient

from contracts import RuntimeJob, Stage
from runtime_core.prompts import EDITOR_PROMPT_VERSION


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
    repository = client.app.state.repository
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
    assert client.app.state.repository.list_objects("review") == []


def test_duplicate_title_blocked_at_publish(client: TestClient) -> None:
    _assert_publish_happy_path(client)
    publications = client.app.state.repository.list_objects("publication")
    assert len(publications) == 1
    first_pub_id = publications[0].id

    seed2 = client.post(
        "/submissions",
        json=_submission_payload(
            "Databases searched include PubMed and review corpora, with a documented date window, explicit inclusion logic, and a stated narrowing rule that explains why these retained receipts best match the scoped research question."
        ),
    )
    assert seed2.status_code == 200
    submission2_id = seed2.json()["submission"]["id"]

    for _ in range(12):
        queue = client.get("/jobs/queue").json()["queued"]
        if not queue:
            break
        client.post("/jobs/run-once")

    publications = client.app.state.repository.list_objects("publication")
    assert len(publications) == 1
    assert publications[0].id == first_pub_id
