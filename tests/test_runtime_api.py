import hashlib
import inspect
import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, cast
from urllib.parse import parse_qs, quote, urlparse

import pytest
import starlette.testclient as starlette_testclient
from fastapi.testclient import TestClient

from contracts import ArticleType, ClaimCard, ContradictionStatus, Decision, EventType, EvidenceGrade, ObjectType, ResearchObject, RuntimeEvent, Stage
from runtime_core.goldset import summarize_gold_results
from runtime_core.judge_release import calibration_metrics_complete, judge_release_id
from runtime_core.osf import sign_oauth_state
from runtime_core.repos import InMemoryRuntimeRepository


def _repository(client: TestClient) -> Any:
    return cast(Any, client.app).state.repository


def _seed_publication(client: TestClient, title: str, body: str = "") -> ResearchObject:
    return _repository(client).create_object(
        ResearchObject(
            object_type=ObjectType.PUBLICATION,
            title=title,
            body_markdown=body,
            metadata={"url": f"https://researka.org/papers/{title.lower().replace(' ', '-')}"},
        )
    )


def _valid_source_bundle() -> list[dict[str, object]]:
    years = (2024, 2023, 2022, 2021, 2020, 2024, 2023, 2022, 2021, 2019, 2018, 2017)
    evidence_types = ("review",) * 6 + ("primary",) * 6
    return [
        {
            "title": f"{evidence_type.title()} source {index}",
            "year": year,
            "evidence_type": evidence_type,
            "doi": f"10.1234/source.{index}",
            "excerpt": (
                "The bounded evidence supports an endpoint-specific finding and a cautious synthesis with "
                "explicit uncertainty, while distinguishing review-level support from primary-study signals."
            ),
        }
        for index, (year, evidence_type) in enumerate(zip(years, evidence_types, strict=True), start=1)
    ]


def _worker_headers() -> dict[str, str]:
    return {"x-api-key": os.environ.get("RESEARKA_V2_ADMIN_KEY", "test-admin-key")}


def test_create_app_warns_when_osf_default_owner_missing(monkeypatch, caplog) -> None:
    from apps.runtime_api.app import create_app

    monkeypatch.setenv("RESEARKA_V2_OSF_OAUTH_CLIENT_ID", "client-id")
    monkeypatch.setenv("RESEARKA_V2_OSF_TOKEN_ENCRYPTION_KEY_PATH", "/run/secrets/key")
    monkeypatch.delenv("RESEARKA_V2_OSF_DEFAULT_AGENT_ID", raising=False)
    monkeypatch.delenv("RESEARKA_V2_OSF_FALLBACK_AGENT_ID", raising=False)

    with caplog.at_level(logging.WARNING, logger="runtime_core.osf"):
        create_app(InMemoryRuntimeRepository())

    assert "osf_default_owner_agent_missing" in caplog.text


def _run_until_idle(client: TestClient, limit: int = 12) -> None:
    for _ in range(limit):
        queue = client.get("/jobs/queue", headers=_worker_headers()).json()["queued"]
        if not queue:
            return
        client.post("/jobs/run-once", headers=_worker_headers())


def test_health(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_deprecated_testclient_fallback_is_a_hard_failure() -> None:
    if "import httpx2 as httpx" not in inspect.getsource(starlette_testclient):
        pytest.skip("Starlette predates the httpx2 migration")
    script = """
import builtins
import warnings

real_import = builtins.__import__
def blocked_import(name, *args, **kwargs):
    if name == "httpx2" or name.startswith("httpx2."):
        raise ModuleNotFoundError(name)
    return real_import(name, *args, **kwargs)

builtins.__import__ = blocked_import
warnings.filterwarnings("error", message=r"Using `httpx` with `starlette\\.testclient` is deprecated.*")
from starlette.testclient import TestClient
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=False)
    assert result.returncode != 0
    assert "StarletteDeprecationWarning" in result.stderr


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


def test_resolve_git_sha_prefers_live_checkout_to_stale_file(monkeypatch) -> None:
    import runtime_core.judge_release as release_module

    class GitResult:
        returncode = 0
        stdout = "b" * 40

    monkeypatch.setenv("RESEARKA_GIT_SHA", "a" * 40)
    monkeypatch.setattr(release_module.subprocess, "run", lambda *args, **kwargs: GitResult())
    monkeypatch.setattr(
        release_module.Path,
        "read_text",
        lambda self: (_ for _ in ()).throw(AssertionError("stale SHA read")),
    )

    assert release_module.resolve_git_sha() == "b" * 40

    GitResult.returncode = 1
    assert release_module.resolve_git_sha() == "a" * 40


def test_architecture(client: TestClient) -> None:
    response = client.get("/architecture")
    assert response.status_code == 200
    data = response.json()
    assert "runtime_core" in data["top_level_modules"]
    assert data["critical_flow"] == ["intake", "review", "editorial", "publish"]


def test_run_once_requires_admin_not_submission_key(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("RESEARKA_V2_ADMIN_KEY", "admin-secret-123")
    response = client.post("/jobs/run-once", headers={"x-api-key": "test-legacy-key"})

    assert response.status_code == 403
    assert response.json()["detail"] == "admin_key_required"


def test_run_once_rejects_fake_key(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("RESEARKA_V2_ADMIN_KEY", "admin-secret-123")
    response = client.post("/jobs/run-once", headers={"x-api-key": "ANYTHING"})

    assert response.status_code == 403
    assert response.json()["detail"] == "admin_key_required"


def test_alpha_memo_submission_reaches_review_queue(client: TestClient) -> None:
    payload = {
        "artifact_type": "alpha_memo",
        "article_type": "alpha_memo",
        "author_agent_id": "agent-v4-alpha-memo",
        "title": "Storage reserves flip after threshold pricing",
        "markdown": "# Alpha memo\n\nA bounded evidence-backed signal with clear limits.",
        "evidence_bundle": {
            "publish_verdict": {
                "axes": {
                        "source_papers": [
                            {
                                "doi": f"10.1000/alpha-{index}",
                                "title": f"Reserve threshold paper {index}",
                                "source_fact": {
                                    "canonical_phrase": "A bounded reserve threshold signal was reported with clear limits."
                                },
                            }
                            for index in range(1, 6)
                    ],
                },
            },
        },
    }

    response = client.post("/submissions", json=payload)
    assert response.status_code == 200
    submission = response.json()["submission"]
    assert submission["metadata"]["article_type"] == "alpha_memo"
    assert len(submission["metadata"]["source_bundle"]) == 5
    assert submission["metadata"]["source_bundle"][0]["doi"] == "10.1000/alpha-1"

    intake = client.post("/jobs/run-once", headers=_worker_headers())
    assert intake.status_code == 200
    assert client.get(f"/submissions/{submission['id']}/decision").json()["status"] == "pending"
    assert client.get("/jobs/queue", headers=_worker_headers()).json()["queued"][0]["stage"] == "autonomous_review"


def test_submission_flattens_trusted_audit_metadata(client: TestClient) -> None:
    payload = {
        "title": "Research Synthesis: Hash-traced v3 paper",
        "abstract": " ".join(["abstract"] * 80),
        "author_agent_id": "agent-v3-full-paper",
        "article_type": "rapid_evidence_synthesis",
        "sections": {
            "Research Question": " ".join(["question"] * 60),
            "Search Summary": "Methods and search scope.",
            "Evidence Landscape": "Evidence landscape.",
            "Key Findings": "Key findings.",
            "Limitations": "Limitations.",
            "Gaps Identified": "Gaps.",
            "Conclusion": "Conclusion.",
        },
        "source_bundle": _valid_source_bundle(),
        "metadata": {
            "run_id": "synthesis-topic-v06-test",
            "content_hash": "sha256:paper",
            "source_citation_hash": "sha256:sources",
            "submission_identity_key": "sha256:identity",
            "submission_payload_hash": "sha256:payload",
            "topic": "topic_slug",
            "unsafe_extra": "ignore-me",
        },
    }

    response = client.post("/submissions", json=payload)

    assert response.status_code == 200
    metadata = response.json()["submission"]["metadata"]
    assert metadata["run_id"] == "synthesis-topic-v06-test"
    assert metadata["content_hash"] == "sha256:paper"
    assert metadata["source_citation_hash"] == "sha256:sources"
    assert metadata["submission_identity_key"] == "sha256:identity"
    assert metadata["submission_payload_hash"] == "sha256:payload"
    assert metadata["topic"] == "topic_slug"
    assert "metadata" not in metadata
    assert "unsafe_extra" not in metadata


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
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == "https://osf.io/oauth2/authorize"
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


def test_osf_oauth_callback_can_backfill_target_publication(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("RESEARKA_V2_OSF_OAUTH_CLIENT_ID", "client-id")
    monkeypatch.setenv("RESEARKA_V2_OSF_OAUTH_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("RESEARKA_V2_OSF_OAUTH_STATE_SECRET", "state-secret")
    monkeypatch.setenv("RESEARKA_V2_OSF_OAUTH_REDIRECT_URI", "https://api.researka.org/oauth/osf/callback")
    state = sign_oauth_state(agent_id="agent-v3-full-paper", secret="state-secret", publication_id="pub-1")

    def fake_exchange(config, *, code: str) -> dict[str, str]:
        return {"access_token": "oauth-access-token", "refresh_token": "oauth-refresh-token", "scope": "osf.full_write"}

    def fake_user_lookup(access_token: str, **kwargs) -> dict[str, str]:
        return {"osf_user_id": "osf-user-1"}

    def fake_backfill(repository, *, apply: bool, publication_id: str | None):
        assert repository is _repository(client)
        assert apply is True
        assert publication_id == "pub-1"
        return {"apply": True, "eligible": 1, "minted": 1, "failed": 0, "records": [{"publication_id": "pub-1"}]}

    monkeypatch.setattr("apps.runtime_api.app.exchange_oauth_code", fake_exchange)
    monkeypatch.setattr("apps.runtime_api.app.osf_user_metadata_from_token", fake_user_lookup)
    monkeypatch.setattr("apps.runtime_api.app.backfill_missing_publication_dois", fake_backfill)

    response = client.get(f"/oauth/osf/callback?code=oauth-code&state={quote(state)}")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "connected"
    assert body["agent_id"] == "agent-v3-full-paper"
    assert body["doi_backfill"]["minted"] == 1


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
                    "Key Findings": "Key findings integrate the retained evidence into a bounded synthesis rather than stitched snippets, and they distinguish stronger review-level support from more tentative primary-study signals [bundle:1].",
                "Limitations": "The main limits are scope, incomplete coverage, heterogeneous certainty, and the possibility of omitted contradictory sources that could materially shift the confidence of the synthesis.",
                "Gaps Identified": "No independent replication has confirmed these synthesis-level findings, and the gap between review-level evidence and applied outcomes remains untested.",
                    "Conclusion": "The current evidence supports a cautious synthesis with explicit uncertainty, transparent methodological limits, and no claim that exceeds the retained bundle [bundle:1].",
            },
            "source_bundle": _valid_source_bundle(),
            "author_agent_id": "agent-demo",
            "domain_slug": "longevity",
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["job"]["stage"] == "submission_intake"
    queue = client.get("/jobs/queue", headers=_worker_headers()).json()["queued"]
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
                "Key Findings": "Key findings integrate the retained evidence into a bounded synthesis rather than stitched snippets, and they distinguish stronger review-level support from more tentative primary-study signals [bundle:1].",
                "Limitations": "The main limits are scope, incomplete coverage, heterogeneous certainty, and the possibility of omitted contradictory sources that could materially shift the confidence of the synthesis.",
                "Gaps Identified": "No independent replication has confirmed these synthesis-level findings, and the gap between review-level evidence and applied outcomes remains untested.",
                "Conclusion": "The current evidence supports a cautious synthesis with explicit uncertainty, transparent methodological limits, and no claim that exceeds the retained bundle [bundle:1].",
            },
            "source_bundle": _valid_source_bundle(),
            "author_agent_id": "agent-demo",
            "domain_slug": "longevity",
        },
    ).json()["submission"]
    response = client.get(f"/submissions/{submission['id']}")
    assert response.status_code == 200
    assert response.json()["object_type"] == "submission"


def test_agent_query_disabled_by_default(client: TestClient, monkeypatch) -> None:
    monkeypatch.delenv("RESEARKA_V2_AGENT_QUERY_ENABLED", raising=False)

    response = client.post("/agent-query/jobs", json={"query": "rapamycin and immune aging", "depth": "standard"})

    assert response.status_code == 503
    assert response.json()["detail"] == "agent_query_disabled"


def test_agent_query_creates_separate_public_job(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("RESEARKA_V2_AGENT_QUERY_ENABLED", "1")
    _seed_publication(client, "Research Synthesis: Rapamycin and Immune Aging")

    response = client.post(
        "/agent-query/jobs",
        headers={"x-forwarded-for": "203.0.113.20"},
        json={
            "query": "  rapamycin   and   immune aging  ",
            "depth": "standard",
            "contactEmail": "reader@example.com",
        },
    )

    assert response.status_code == 202
    job = response.json()
    assert job["status"] == "queued"
    assert job["query"] == "rapamycin and immune aging"
    assert job["depth"] == "standard"
    assert job["position"] == 1
    assert job["caps"]["max_runtime_sec"] > 0
    assert client.get("/jobs/queue", headers=_worker_headers()).json()["queued"] == []

    stored = _repository(client).get_object(job["jobId"])
    assert stored.object_type == ObjectType.AGENT_QUERY
    assert stored.metadata["lane"] == "public_on_demand_agent_query"
    assert stored.metadata["contact_email_provided"] is True
    assert "reader@example.com" not in str(stored.model_dump(mode="json"))
    assert "203.0.113.20" not in str(stored.model_dump(mode="json"))
    assert len(stored.metadata["client_bucket_hash"]) == 64

    poll = client.get(f"/agent-query/jobs/{job['jobId']}")
    assert poll.status_code == 200
    assert poll.json()["status"] == "completed"
    assert poll.json()["result"]["citations"][0]["title"] == "Research Synthesis: Rapamycin and Immune Aging"


def test_agent_query_rate_limited_by_client(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("RESEARKA_V2_AGENT_QUERY_ENABLED", "1")
    monkeypatch.setenv("RESEARKA_V2_AGENT_QUERY_PER_IP_PER_DAY", "1")
    headers = {"x-forwarded-for": "203.0.113.21"}

    first = client.post("/agent-query/jobs", headers=headers, json={"query": "rapamycin", "depth": "standard"})
    second = client.post("/agent-query/jobs", headers=headers, json={"query": "metformin", "depth": "standard"})

    assert first.status_code == 202
    assert second.status_code == 429
    assert second.json()["detail"] == "agent_query_rate_limited"


def test_agent_query_handles_sample_topics_end_to_end(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("RESEARKA_V2_AGENT_QUERY_ENABLED", "1")
    sample_topics = [
        "rapamycin immune aging",
        "metformin longevity",
        "senolytics frailty",
        "taurine aging biomarkers",
        "caloric restriction inflammation",
        "GLP-1 cardiometabolic risk",
        "NAD precursors mitochondrial health",
        "exercise VO2max aging",
        "sleep glymphatic clearance",
        "microbiome Akkermansia",
        "protein intake sarcopenia",
        "plasma proteomic age clocks",
        "heat resilience older adults",
        "omega-3 inflammation",
        "melatonin circadian aging",
    ]
    for topic in sample_topics:
        _seed_publication(client, f"Research Synthesis: {topic.title()}", f"Public record covering {topic}.")

    completed = []
    for index, topic in enumerate(sample_topics, start=1):
        response = client.post(
            "/agent-query/jobs",
            headers={"x-forwarded-for": f"203.0.113.{index}"},
            json={"query": topic, "depth": "brief"},
        )
        assert response.status_code == 202
        data = client.get(f"/agent-query/jobs/{response.json()['jobId']}").json()
        assert data["status"] == "completed"
        assert data["result"]["citations"]
        assert data["result"]["answerMarkdown"]
        completed.append(data["jobId"])

    assert len(completed) == 15
    assert client.get("/jobs/queue", headers=_worker_headers()).json()["queued"] == []


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
                "Key Findings": "Key findings integrate the retained evidence into a bounded synthesis rather than stitched snippets, and they distinguish stronger review-level support from more tentative primary-study signals [bundle:1].",
                "Limitations": "The main limits are scope, incomplete coverage, heterogeneous certainty, and the possibility of omitted contradictory sources that could materially shift the confidence of the synthesis.",
                "Gaps Identified": "No independent replication has confirmed these synthesis-level findings, and the gap between review-level evidence and applied outcomes remains untested.",
                "Conclusion": "The current evidence supports a cautious synthesis with explicit uncertainty, transparent methodological limits, and no claim that exceeds the retained bundle [bundle:1].",
            },
            "source_bundle": _valid_source_bundle(),
            "author_agent_id": "agent-demo",
            "domain_slug": "longevity",
        },
    ).json()["submission"]
    response = client.get(f"/submissions/{submission['id']}/decision")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "pending"
    assert payload["pipeline"]["current_stage"] == Stage.INTAKE.value
    assert payload["pipeline"]["attempt_count"] == 1


def test_submission_decision_unknown_id_is_not_pending(client: TestClient) -> None:
    response = client.get("/submissions/missing-submission/decision")

    assert response.status_code == 404
    assert response.json()["detail"] == "submission_not_found"


def test_submission_decision_reports_only_terminal_review_failure(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("RESEARKA_V2_REVIEW_JOB_RETRY_BACKOFF_SEC", "0")
    created = client.post("/submissions", json=_minimal_submission_payload()).json()
    submission_id = created["submission"]["id"]
    worker = cast(Any, client.app).state.worker
    handle_job = worker.engine.handle_job

    def fail_review(job, repository):
        if job.stage != Stage.REVIEW:
            return handle_job(job, repository)
        if not job.payload.get("provider_retry_count"):
            raise RuntimeError("provider_error: temporary reviewer outage")
        raise RuntimeError("quality_gate_failed: terminal review failure")

    monkeypatch.setattr(worker.engine, "handle_job", fail_review)
    client.post("/jobs/run-once", headers=_worker_headers())
    first_failure = client.post("/jobs/run-once", headers=_worker_headers()).json()

    assert first_failure["retried"] == 1
    repository = _repository(client)
    failed_events = [event for event in repository.list_events() if event.event_type == EventType.JOB_FAILED]
    assert failed_events[-1].payload["terminal"] is False
    assert client.get(f"/submissions/{submission_id}/decision").json()["status"] == "pending"

    repository.lease_ttl_seconds = -1
    retry_job = repository.claim_next_job(target_object_id=submission_id)
    assert retry_job is not None
    assert client.get(f"/submissions/{submission_id}/decision").json()["status"] == "pending"

    terminal_failure = client.post("/jobs/run-once", headers=_worker_headers()).json()
    response = client.get(f"/submissions/{submission_id}/decision")

    assert terminal_failure["retried"] == 0
    failed_events = [event for event in repository.list_events() if event.event_type == EventType.JOB_FAILED]
    assert failed_events[-1].payload["terminal"] is True
    assert response.status_code == 200
    payload = response.json()
    assert {key: payload[key] for key in (
        "status",
        "decision",
        "notes",
        "gate_failures",
        "failure_stage",
        "failure_category",
        "failed_checks",
    )} == {
        "status": "failed",
        "decision": None,
        "notes": [],
        "gate_failures": [],
        "failure_stage": Stage.REVIEW.value,
        "failure_category": "quality_gate",
        "failed_checks": ["quality_gate_failed: terminal review failure"],
    }
    assert payload["pipeline"]["attempt_count"] == 3
    assert payload["pipeline"]["attempts"][-1]["terminal"] is True


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
                "Key Findings": "Key findings integrate the retained evidence into a bounded synthesis rather than stitched snippets, and they distinguish stronger review-level support from more tentative primary-study signals [bundle:1].",
                "Limitations": "The main limits are scope, incomplete coverage, heterogeneous certainty, and the possibility of omitted contradictory sources that could materially shift the confidence of the synthesis.",
                "Gaps Identified": "No independent replication has confirmed these synthesis-level findings, and the gap between review-level evidence and applied outcomes remains untested.",
                "Conclusion": "The current evidence supports a cautious synthesis with explicit uncertainty, transparent methodological limits, and no claim that exceeds the retained bundle [bundle:1].",
            },
            "source_bundle": _valid_source_bundle(),
            "author_agent_id": "agent-demo",
            "domain_slug": "longevity",
        },
    ).json()["submission"]
    _run_until_idle(client)
    # Zero-trust publish tier: a first-time agent's publication lands
    # provisional — verifiable but excluded from public lists until promoted.
    created = [
        pub
        for pub in _repository(client).list_objects(ObjectType.PUBLICATION)
        if pub.parent_object_id == submission["id"]
    ]
    assert created and created[0].metadata["public_visibility"] == "provisional"
    assert client.get("/publications").json()["publications"] == []
    promoted = client.post(
        f"/ops/publications/{created[0].id}/visibility",
        json={"visibility": "listed"},
        headers={"x-api-key": "test-admin-key"},
    )
    assert promoted.status_code == 200
    publications = client.get("/publications")
    assert publications.status_code == 200
    publication = publications.json()["publications"][0]
    detail = client.get(f"/publications/{publication['id']}")
    assert detail.status_code == 200
    assert detail.json()["parent_object_id"] == submission["id"]
    assert detail.json()["sidecars"][0]["name"].endswith(".json") or detail.json()["sidecars"][0]["name"].endswith(".csv")

    sidecar = client.get(f"/publications/{publication['id']}/sidecars/evidence_table.csv")
    assert sidecar.status_code == 200
    assert sidecar.headers["content-type"].startswith("text/csv")
    assert sidecar.text.splitlines()[0] == "study,population,intervention_or_exposure,comparator,endpoint,effect,risk_of_bias,directness"

    graph = client.get(f"/publications/{publication['id']}/sidecars/claim_graph.json")
    assert graph.status_code == 200
    assert graph.json()["publication_id"] == publication["id"]
    assert graph.json()["screening"]["flow"] == ["identified", "screened", "excluded_with_reasons", "included"]


def test_publications_list_hides_superseded_records(client: TestClient) -> None:
    repo = _repository(client)
    kept = repo.create_object(
        ResearchObject(
            object_type=ObjectType.PUBLICATION,
            title="Current memo",
            metadata={"article_type": "alpha_memo"},
        )
    )
    repo.create_object(
        ResearchObject(
            object_type=ObjectType.PUBLICATION,
            title="Superseded memo",
            metadata={"article_type": "alpha_memo", "superseded_by": kept.id},
        )
    )
    repo.create_object(
        ResearchObject(
            object_type=ObjectType.PUBLICATION,
            title="Hidden memo",
            metadata={"article_type": "alpha_memo", "public_visibility": "hidden"},
        )
    )

    listed = client.get("/publications").json()["publications"]

    assert [publication["title"] for publication in listed] == ["Current memo"]


def test_get_publication_claims_404_for_unknown_publication(client: TestClient) -> None:
    response = client.get("/publications/does-not-exist/claims")
    assert response.status_code == 404
    assert response.json()["detail"] == "publication_not_found"


def test_get_publication_claims_returns_empty_list_when_no_claims_extracted(client: TestClient) -> None:
    """Discriminating test (V4): publication exists, no claims yet → empty list,
    not 404. Decouples claim storage from publication existence."""
    repo = _repository(client)
    publication = repo.create_object(
        ResearchObject(
            object_type=ObjectType.PUBLICATION,
            title="Accepted memo without extracted claims",
            metadata={"article_type": "alpha_memo"},
        )
    )

    response = client.get(f"/publications/{publication.id}/claims")

    assert response.status_code == 200
    assert response.json() == {"claims": []}


def test_get_publication_claims_derives_cards_from_sidecars(client: TestClient) -> None:
    repo = _repository(client)
    submission = repo.create_object(
        ResearchObject(
            object_type=ObjectType.SUBMISSION,
            title="Metformin submission",
            metadata={"source_bundle": _valid_source_bundle()},
        )
    )
    publication = repo.create_object(
        ResearchObject(
            object_type=ObjectType.PUBLICATION,
            parent_object_id=submission.id,
            title="Metformin publication",
            body_markdown="- Metformin evidence suggests a bounded effect on lifespan risk and supports cautious interpretation.",
            metadata={"article_type": "research_synthesis", "content_hash": "sha256:" + "a" * 64},
        )
    )

    response = client.get(f"/publications/{publication.id}/claims")

    assert response.status_code == 200
    claim = response.json()["claims"][0]
    assert claim["id"].startswith("claim_")
    assert claim["evidence_grade"] == "exploratory"
    assert claim["citation_support"] == []
    trace = client.get(f"/publications/{publication.id}/sidecars/citation_traces.json").json()["traces"][0]
    assert trace["citation_support"] == []
    assert trace["candidate_sources"][0]["support_kind"] == "candidate_source_row"


def test_get_publication_claims_labels_mixed_direct_source_support(client: TestClient) -> None:
    repo = _repository(client)
    submission = repo.create_object(
        ResearchObject(
            object_type=ObjectType.SUBMISSION,
            title="Caffeine submission",
            metadata={
                "source_bundle": [
                    {
                        "title": "Caffeine time-to-exhaustion trial",
                        "doi": "10.1000/caffeine",
                        "evidence_type": "primary",
                        "endpoint": "time to exhaustion",
                            "effect": "increased run distance",
                            "excerpt": "Caffeine increased run distance in the time-to-exhaustion trial under the tested design.",
                    }
                ]
            },
        )
    )
    publication = repo.create_object(
        ResearchObject(
            object_type=ObjectType.PUBLICATION,
            parent_object_id=submission.id,
            title="Caffeine endpoint memo",
            body_markdown="- Mixed endpoint evidence suggests caffeine effects depend on trial design and DOI 10.1000/caffeine supports the time-to-exhaustion signal in the source row.",
            metadata={"article_type": "alpha_memo"},
        )
    )

    response = client.get(f"/publications/{publication.id}/claims")

    assert response.status_code == 200
    claim = response.json()["claims"][0]
    assert claim["contradiction_status"] == "mixed"
    assert claim["citation_support"][0]["support_kind"] == "direct_doi_match"
    assert claim["citation_support"][0]["endpoint"] == "time to exhaustion"


def test_get_publication_claims_resolves_explicit_bundle_reference(client: TestClient) -> None:
    repo = _repository(client)
    submission = repo.create_object(
        ResearchObject(
            object_type=ObjectType.SUBMISSION,
            title="Bundle-linked submission",
            metadata={"source_bundle": _valid_source_bundle()},
        )
    )
    publication = repo.create_object(
        ResearchObject(
            object_type=ObjectType.PUBLICATION,
            parent_object_id=submission.id,
            title="Bundle-linked publication",
            body_markdown="## Methods\n\nSources were retained using the declared protocol.",
            metadata={
                "article_type": "research_synthesis",
                "abstract": (
                    "The bounded evidence supports an endpoint-specific finding without a broad causal claim "
                    "and maps directly to the first submitted source [bundle:1]."
                ),
            },
        )
    )

    claim = client.get(f"/publications/{publication.id}/claims").json()["claims"][0]

    assert claim["citation_support"][0]["support_kind"] == "bundle_reference"
    assert claim["citation_support"][0]["study"] == "Review source 1"


def test_get_publication_claims_returns_saved_cards_in_created_order(client: TestClient) -> None:
    repo = _repository(client)
    publication = repo.create_object(
        ResearchObject(
            object_type=ObjectType.PUBLICATION,
            title="Metformin lifespan publication",
            metadata={"article_type": "research_synthesis"},
        )
    )
    first = repo.save_claim_card(
        ClaimCard(
            publication_id=publication.id,
            claim_text="Metformin extends median lifespan in mice by ~5%.",
            evidence_grade=EvidenceGrade.VERIFIED,
            citation_support=[{"source_id": "src-1", "quote": "5.83%", "dw_chain_ref": "dw://chain/a"}],
            contradiction_status=ContradictionStatus.NONE,
            source_ids=["src-1"],
            dw_chain_url="https://provenance.researka.org/chain/a",
        )
    )
    second = repo.save_claim_card(
        ClaimCard(
            publication_id=publication.id,
            claim_text="Effect size narrows above 1g/kg.",
            evidence_grade=EvidenceGrade.EXPLORATORY,
        )
    )

    response = client.get(f"/publications/{publication.id}/claims")

    assert response.status_code == 200
    claims = response.json()["claims"]
    assert [c["id"] for c in claims] == [first.id, second.id]
    assert claims[0]["evidence_grade"] == "verified"
    assert claims[0]["citation_support"] == [
        {"source_id": "src-1", "quote": "5.83%", "dw_chain_ref": "dw://chain/a"}
    ]
    assert claims[0]["contradiction_status"] == "none"
    assert claims[0]["dw_chain_url"] == "https://provenance.researka.org/chain/a"
    assert claims[1]["evidence_grade"] == "exploratory"
    assert claims[1]["contradiction_status"] == "none"
    assert claims[1]["citation_support"] == []
    assert claims[1]["dw_chain_url"] is None


def test_get_publication_claims_rejects_non_publication_object(client: TestClient) -> None:
    """Object exists but is a submission, not a publication → 404."""
    repo = _repository(client)
    submission = repo.create_object(
        ResearchObject(
            object_type=ObjectType.SUBMISSION,
            title="Pending submission",
            metadata={},
        )
    )

    response = client.get(f"/publications/{submission.id}/claims")

    assert response.status_code == 404
    assert response.json()["detail"] == "publication_not_found"


def test_get_claim_finds_public_claim(client: TestClient) -> None:
    repo = _repository(client)
    publication = repo.create_object(
        ResearchObject(
            object_type=ObjectType.PUBLICATION,
            title="Claim lookup publication",
            body_markdown="- Aspirin evidence suggests no clinical geroprotection support in current human evidence.",
            metadata={"article_type": "research_synthesis"},
        )
    )
    claim_id = client.get(f"/publications/{publication.id}/claims").json()["claims"][0]["id"]

    response = client.get(f"/claims/{claim_id}")

    assert response.status_code == 200
    assert response.json()["publication_id"] == publication.id


def test_publications_surface_filter_splits_alpha_and_papers(client: TestClient) -> None:
    repo = _repository(client)
    paper = repo.create_object(
        ResearchObject(
            object_type=ObjectType.PUBLICATION,
            title="Research paper",
            metadata={"article_type": "research_synthesis", "publication_class": "research_synthesis"},
        )
    )
    alpha = repo.create_object(
        ResearchObject(
            object_type=ObjectType.PUBLICATION,
            title="Alpha memo",
            metadata={"article_type": "alpha_memo", "publication_class": "alpha_memo"},
        )
    )

    alpha_response = client.get("/publications?surface=alpha")
    paper_response = client.get("/publications?surface=papers")
    invalid_response = client.get("/publications?surface=reviews")

    assert [item["id"] for item in alpha_response.json()["publications"]] == [alpha.id]
    assert alpha_response.json()["publications"][0]["surface"] == "alpha"
    assert alpha_response.json()["publications"][0]["publication_class"] == "alpha_memo"
    assert [item["id"] for item in paper_response.json()["publications"]] == [paper.id]
    assert invalid_response.status_code == 400


def test_publications_listing_is_bounded_and_paginated(client: TestClient) -> None:
    repo = _repository(client)
    for index in range(3):
        repo.create_object(
            ResearchObject(
                object_type=ObjectType.PUBLICATION,
                title=f"Publication {index}",
                metadata={
                    "article_type": "research_synthesis",
                    "judge_release_id": f"sha256:{index}",
                    "public_visibility": "listed",
                },
            )
        )

    page = client.get("/publications?limit=1&offset=1").json()

    assert [item["title"] for item in page["publications"]] == ["Publication 1"]
    assert page["total"] == 3
    assert page["limit"] == 1
    assert page["offset"] == 1
    assert page["has_more"] is True
    assert page["next_offset"] == 2
    assert page["publications"][0]["judge_release_id"] == "sha256:1"

    final_page = client.get("/publications?limit=1&offset=2").json()
    assert final_page["has_more"] is False
    assert final_page["next_offset"] is None


def test_publication_response_relabels_scoping_only_research_synthesis(client: TestClient) -> None:
    repo = _repository(client)
    submission = repo.create_object(
        ResearchObject(
            object_type=ObjectType.SUBMISSION,
            title="Tai Chi submission",
            metadata={"source_bundle": _valid_source_bundle()},
        )
    )
    publication = repo.create_object(
        ResearchObject(
            object_type=ObjectType.PUBLICATION,
            parent_object_id=submission.id,
            title="Research Synthesis: Tai Chi Exercise Effects — full paper",
            metadata={
                "article_type": "research_synthesis",
                "publication_class": "research_synthesis",
                "evidence_profile": {"weak_evidence_ratio": 0.76, "indirect_signal": True},
            },
        )
    )
    for claim_text, status in [
        ("The corpus is non-supportive for broad clinical claims.", ContradictionStatus.NON_SUPPORTIVE),
        ("Evidence is mixed and endpoint-dependent.", ContradictionStatus.MIXED),
    ]:
        repo.save_claim_card(
            ClaimCard(
                publication_id=publication.id,
                claim_text=claim_text,
                evidence_grade=EvidenceGrade.EXPLORATORY,
                contradiction_status=status,
                citation_support=[
                    {
                        "source_id": "source_1",
                        "support_kind": "candidate_source_row",
                        "population": "not extracted",
                        "endpoint": "not extracted",
                        "effect": "not extracted",
                    }
                ],
            )
        )

    listed = client.get("/publications").json()["publications"][0]
    detail = client.get(f"/publications/{publication.id}").json()

    assert listed["publication_class"] == "adjacent_evidence_brief"
    assert listed["title"] == "Adjacent Evidence Brief: Tai Chi Exercise Effects — full paper"
    assert detail["publication_class"] == "adjacent_evidence_brief"


def test_publication_response_reclassifies_untraced_derived_claim_cards(client: TestClient) -> None:
    repo = _repository(client)
    submission = repo.create_object(
        ResearchObject(
            object_type=ObjectType.SUBMISSION,
            title="Protein supplementation submission",
            metadata={"source_bundle": _valid_source_bundle()},
        )
    )
    publication = repo.create_object(
        ResearchObject(
            object_type=ObjectType.PUBLICATION,
            parent_object_id=submission.id,
            title="Research Synthesis: Protein supplementation — full paper",
            body_markdown=(
                "Indirect evidence suggests endpoint-specific effects while preserving the limits of "
                "adjacent and mechanistic sources across the retained clinical corpus.\n\n"
                "Mixed and null evidence constrains broad claims, but direct sources still support a "
                "bounded research synthesis with explicit uncertainty."
            ),
            metadata={
                "article_type": "research_synthesis",
                "publication_class": "research_synthesis",
                "evidence_profile": {
                    "direct_clinical_sources": 11,
                    "indirect_signal": True,
                    "weak_evidence_ratio": 0.0,
                },
            },
        )
    )

    assert repo.list_claim_cards(publication.id) == []
    detail = client.get(f"/publications/{publication.id}").json()

    assert detail["publication_class"] == "adjacent_evidence_brief"
    assert detail["title"] == "Adjacent Evidence Brief: Protein supplementation — full paper"


def test_publication_response_preserves_verified_research_synthesis(client: TestClient) -> None:
    repo = _repository(client)
    publication = repo.create_object(
        ResearchObject(
            object_type=ObjectType.PUBLICATION,
            title="Research Synthesis: Resistance Training Effects — full paper",
            metadata={"article_type": "research_synthesis", "publication_class": "research_synthesis"},
        )
    )
    repo.save_claim_card(
        ClaimCard(
            publication_id=publication.id,
            claim_text="Direct trials support improved endpoint-specific function.",
            evidence_grade=EvidenceGrade.VERIFIED,
            contradiction_status=ContradictionStatus.NONE,
            citation_support=[{"source_id": "src-1", "support_kind": "direct_doi_match"}],
        )
    )

    detail = client.get(f"/publications/{publication.id}").json()

    assert detail["publication_class"] == "research_synthesis"
    assert detail["title"] == "Research Synthesis: Resistance Training Effects — full paper"


def test_claims_list_and_agent_profile(client: TestClient) -> None:
    repo = _repository(client)
    submission = repo.create_object(
        ResearchObject(
            object_type=ObjectType.SUBMISSION,
            title="Agent profile submission",
            metadata={"author_agent_id": "agent-profile", "source_bundle": _valid_source_bundle()},
        )
    )
    repo.create_object(
        ResearchObject(
            object_type=ObjectType.PUBLICATION,
            parent_object_id=submission.id,
            title="Agent profile publication",
            body_markdown="- Rapamycin evidence suggests endpoint-specific effects and supports narrow public claims.",
            metadata={"article_type": "research_synthesis"},
        )
    )
    repo.create_object(
        ResearchObject(
            object_type=ObjectType.DECISION,
            parent_object_id=submission.id,
            title="Accept decision",
            metadata={"decision": Decision.ACCEPT.value},
        )
    )

    claims = client.get("/claims").json()["claims"]
    agent = client.get("/agents/agent-profile").json()

    assert claims[0]["publication_id"]
    assert agent["agent_id"] == "agent-profile"
    assert agent["accept"] == 1


def test_badges_leaderboard_verify_index_and_ro_crate(client: TestClient) -> None:
    repo = _repository(client)
    submission = repo.create_object(
        ResearchObject(
            object_type=ObjectType.SUBMISSION,
            title="Leaderboard submission",
            metadata={
                "author_agent_id": "agent-one",
                "source_bundle": _valid_source_bundle(),
                "institution_name": "Researka Lab",
                "institution_ror": "https://ror.org/123456789",
                "raid_id": "https://raid.org/example",
            },
        )
    )
    publication = repo.create_object(
        ResearchObject(
            object_type=ObjectType.PUBLICATION,
            parent_object_id=submission.id,
            title="Leaderboard publication",
            body_markdown="- Exercise evidence suggests endpoint-specific effects and supports narrow public claims.",
            metadata={
                "content_hash": "sha256:" + "b" * 64,
                "doi": "10.17605/OSF.IO/ABC12",
                "doi_status": "minted",
                "osf_url": "https://osf.io/example",
                "institution_name": "Researka Lab",
                "institution_ror": "https://ror.org/123456789",
                "raid_id": "https://raid.org/example",
                "integrity": {"recommendation": "pass", "similarity_score": 0.04},
            },
        )
    )
    repo.create_object(
        ResearchObject(
            object_type=ObjectType.DECISION,
            parent_object_id=submission.id,
            title="Accept decision",
            metadata={"decision": Decision.ACCEPT.value},
        )
    )

    assert client.get("/badges").json()["badges"][0]["id"] == "exploratory"
    assert client.get("/leaderboard/agents").json()["agents"][0]["agent_id"] == "agent-one"
    assert client.post("/verify", json={"content_hash": "sha256:" + "b" * 64}).json()["publication_id"] == publication.id
    publication_list = client.get("/publications").json()["publications"]
    assert publication_list[0]["doi"] == "10.17605/OSF.IO/ABC12"
    publication_detail = client.get(f"/publications/{publication.id}").json()
    assert publication_detail["doi"] == "10.17605/OSF.IO/ABC12"
    assert publication_detail["doi_status"] == "minted"
    assert publication_detail["osf_url"] == "https://osf.io/example"
    evidence_index = client.get("/evidence-index/latest").json()
    assert evidence_index["publication_count"] == 1
    assert evidence_index["decision_counts"]["revise"] == 0
    passport = client.get(f"/publications/{publication.id}/passport").json()
    assert passport["persistent_identifiers"]["ror_id"] == "https://ror.org/123456789"
    assert passport["persistent_identifiers"]["raid_id"] == "https://raid.org/example"
    assert passport["persistent_identifier_status"]["ror_id"] == "supplied"
    assert passport["persistent_identifier_status"]["raid_id"] == "supplied"
    assert passport["institution"]["status"] == "supplied"
    assert passport["integrity"]["recommendation"] == "pass"
    assert passport["integrity"]["status"] == "checked"
    crate = client.get(f"/publications/{publication.id}/ro-crate").json()
    assert crate["@type"] == "Dataset"
    assert crate["provenance_passport"]["content_hash"] == "sha256:" + "b" * 64
    assert {sidecar["name"] for sidecar in crate["sidecars"]} >= {"claim_graph.json", "evidence_table.csv"}

    bare_submission = repo.create_object(ResearchObject(object_type=ObjectType.SUBMISSION, title="Bare submission"))
    bare_publication = repo.create_object(
        ResearchObject(object_type=ObjectType.PUBLICATION, parent_object_id=bare_submission.id, title="Bare publication")
    )
    bare_passport = client.get(f"/publications/{bare_publication.id}/passport").json()
    assert bare_passport["persistent_identifiers"]["ror_id"] is None
    assert bare_passport["persistent_identifiers"]["raid_id"] is None
    assert bare_passport["persistent_identifier_status"]["ror_id"] == "not_supplied"
    assert bare_passport["persistent_identifier_status"]["raid_id"] == "not_supplied"
    assert bare_passport["institution"]["status"] == "not_supplied"


def test_integrity_unavailable_is_not_public_pass(client: TestClient) -> None:
    repo = _repository(client)
    publication = repo.create_object(
        ResearchObject(
            object_type=ObjectType.PUBLICATION,
            title="Timeout publication",
            metadata={
                "integrity": {
                    "available": False,
                    "recommendation": "pass",
                    "reason": "integrity_unavailable: The read operation timed out",
                }
            },
        )
    )

    detail = client.get(f"/publications/{publication.id}").json()
    passport = client.get(f"/publications/{publication.id}/passport").json()

    assert detail["integrity"]["recommendation"] == "unavailable"
    assert detail["integrity"]["status"] == "unavailable"
    assert passport["integrity"]["recommendation"] == "unavailable"


def test_hidden_records_stay_off_public_trust_surfaces(client: TestClient) -> None:
    repo = _repository(client)
    visible_submission = repo.create_object(
        ResearchObject(
            object_type=ObjectType.SUBMISSION,
            title="Visible submission",
            metadata={"author_agent_id": "launch-agent", "source_bundle": _valid_source_bundle()},
        )
    )
    repo.create_object(
        ResearchObject(
            object_type=ObjectType.PUBLICATION,
            parent_object_id=visible_submission.id,
            title="Visible evidence brief",
            body_markdown="- Exercise evidence suggests endpoint-specific effects and supports narrow public claims.",
            metadata={"content_hash": "sha256:" + "c" * 64},
        )
    )
    hidden_submission = repo.create_object(
        ResearchObject(
            object_type=ObjectType.SUBMISSION,
            title="Benchmark submission",
            metadata={"author_agent_id": "benchmark-agent", "public_visibility": "hidden"},
        )
    )
    hidden_publication = repo.create_object(
        ResearchObject(
            object_type=ObjectType.PUBLICATION,
            parent_object_id=hidden_submission.id,
            title="Benchmark paper",
            body_markdown="- Benchmark content should not seed public trust surfaces.",
            metadata={"content_hash": "sha256:" + "d" * 64, "public_visibility": "hidden"},
        )
    )
    hidden_decision = repo.create_object(
        ResearchObject(
            object_type=ObjectType.DECISION,
            parent_object_id=hidden_submission.id,
            title="Hidden benchmark decision",
            metadata={"decision": Decision.REVISE.value, "public_visibility": "hidden"},
        )
    )

    visible_publications = client.get("/publications").json()["publications"]
    assert hidden_publication.id not in [item["id"] for item in visible_publications]
    assert {claim["publication_id"] for claim in client.get("/claims").json()["claims"]} == {visible_publications[0]["id"]}
    assert [agent["agent_id"] for agent in client.get("/leaderboard/agents").json()["agents"]] == ["launch-agent"]
    assert client.get("/agents/benchmark-agent").status_code == 404
    hidden_index = client.get("/evidence-index/latest").json()
    assert hidden_index["publication_count"] == 1
    assert hidden_index["decision_counts"]["revise"] == 0
    assert client.post("/verify", json={"content_hash": "sha256:" + "d" * 64}).json()["matched"] is False
    assert client.get(f"/publications/{hidden_publication.id}").status_code == 404
    assert client.get(f"/publications/{hidden_publication.id}/claims").status_code == 404
    assert client.get(f"/publications/{hidden_publication.id}/passport").status_code == 404
    assert client.get("/reviews").json()["reviews"] == []
    assert client.get(f"/reviews/{hidden_decision.id}").status_code == 404


def test_reviews_list_exposes_failed_decisions_without_failed_draft(client: TestClient) -> None:
    repo = _repository(client)
    submission = repo.create_object(
        ResearchObject(
            object_type=ObjectType.SUBMISSION,
            title="Exercise: thin alpha memo",
            body_markdown="failed draft body must not leak",
            metadata={
                "article_type": "alpha_memo",
                "author_agent_id": "agent-v4-alpha-memo",
                "domain_slug": "exercise",
                "orcid": "0009-0005-4286-8363",
            },
        )
    )
    review = repo.create_object(
        ResearchObject(
            object_type=ObjectType.REVIEW,
            parent_object_id=submission.id,
            title="Review for Exercise: thin alpha memo",
            body_markdown="Panel review: strong narrow memo, but single-trial caveat needs to be explicit.",
            metadata={
                "recommendation": "reject",
                "provider": "reviewer-panel",
                "model": "mimo-v2.5-pro|google/gemma-4-31b-it|mistralai/mistral-small-2603",
                "route": "fallback_tiebreak",
                "prompt_version": "editor-v1-clean-runtime",
                "rubric_scores": {
                    "research_question_quality": 5,
                    "synthesis_quality": 5,
                    "claim_evidence_alignment": 4,
                    "limitations_quality": 5,
                    "gaps_quality": 5,
                    "source_grounding": 5,
                },
                "major_issues": [],
                "minor_issues": ["Tighten the limitations wording."],
                "required_revisions": ["Clarify that all evidence comes from a single trial."],
                "claim_support_verdict": "supported",
                "overclaim_verdict": "none",
                "synthesis_quality_verdict": "strong",
            },
        )
    )
    rejected = repo.create_object(
        ResearchObject(
            object_type=ObjectType.DECISION,
            parent_object_id=submission.id,
            title="Decision for Exercise: thin alpha memo",
            body_markdown="Editorial decision: reject",
            metadata={
                "decision": "reject",
                "notes": ["editorial decision is terminal; external author must resubmit"],
                "review_id": review.id,
                "required_revisions": ["Align title/topic with receipt evidence."],
                "gate_failures": [{"name": "minimum_citations", "passed": False, "reason": "expected at least 12 sources"}],
            },
        )
    )
    repo.record_event(
        RuntimeEvent(
            event_type=EventType.JOB_COMPLETED,
            target_object_id=submission.id,
            payload={
                "created_object_id": rejected.id,
                "derivation_web": {"decision_artifact_id": "claim_failed_alpha"},
            },
        )
    )

    decision_response = client.get(f"/submissions/{submission.id}/decision")
    assert decision_response.status_code == 200
    decision_payload = decision_response.json()
    assert decision_payload["decision"] == "reject"
    assert decision_payload["review_id"] == review.id
    assert decision_payload["failure_stage"] == "reviewer_panel"
    assert decision_payload["failure_category"] == "minimum_citations"
    assert decision_payload["failed_checks"] == ["expected at least 12 sources"]
    assert decision_payload["required_revisions"] == [
        "Align title/topic with receipt evidence.",
        "Clarify that all evidence comes from a single trial.",
    ]
    assert decision_payload["rubric_scores"]["source_grounding"] == 5
    assert decision_payload["claim_support_verdict"] == "supported"
    assert decision_payload["panel_route"] == "fallback_tiebreak"
    assert decision_payload["models"] == ["mimo-v2.5-pro", "google/gemma-4-31b-it", "mistralai/mistral-small-2603"]
    assert decision_payload["resubmission"] == {"allowed": True, "parent_submission_id": submission.id}
    assert decision_payload["publication"] is None

    repo.create_object(
        ResearchObject(
            object_type=ObjectType.DECISION,
            parent_object_id=submission.id,
            title="Accepted decision should stay off /reviews",
            metadata={"decision": "accept"},
        )
    )

    response = client.get("/reviews")
    assert response.status_code == 200
    records = response.json()["reviews"]
    assert [record["id"] for record in records] == [rejected.id]
    record = records[0]
    assert record["decision"] == "reject"
    assert record["artifact_type"] == "alpha_memo"
    assert record["agent_id"] == "agent-v4-alpha-memo"
    assert record["public_full_text"] is False
    assert record["full_text"] == ""
    assert record["failure_category"] == "minimum_citations"
    assert record["failure_stage"] == "reviewer_panel"
    assert record["failed_checks"] == ["expected at least 12 sources"]
    assert record["rubric_scores"]["claim_evidence_alignment"] == 4
    assert record["required_revisions"] == [
        "Align title/topic with receipt evidence.",
        "Clarify that all evidence comes from a single trial.",
    ]
    assert record["major_issues"] == []
    assert record["minor_issues"] == ["Tighten the limitations wording."]
    assert record["claim_support_verdict"] == "supported"
    assert record["overclaim_verdict"] == "none"
    assert record["synthesis_quality_verdict"] == "strong"
    assert record["review_markdown"].startswith("Panel review:")
    assert record["panel_route"] == "fallback_tiebreak"
    assert record["models"] == ["mimo-v2.5-pro", "google/gemma-4-31b-it", "mistralai/mistral-small-2603"]
    assert record["prompt_version"] == "editor-v1-clean-runtime"
    assert record["dw_chain_url"] == "https://provenance.researka.org/artifacts/claim_failed_alpha/chain"

    detail = client.get(f"/reviews/{rejected.id}")
    assert detail.status_code == 200
    assert detail.json()["id"] == rejected.id


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
                "Key Findings": "Key findings integrate the retained evidence into a bounded synthesis rather than stitched snippets, and they distinguish stronger review-level support from more tentative primary-study signals [bundle:1].",
                "Limitations": "The main limits are scope, incomplete coverage, heterogeneous certainty, and the possibility of omitted contradictory sources that could materially shift the confidence of the synthesis.",
                "Gaps Identified": "No independent replication has confirmed these synthesis-level findings, and the gap between review-level evidence and applied outcomes remains untested.",
                "Conclusion": "The current evidence supports a cautious synthesis with explicit uncertainty, transparent methodological limits, and no claim that exceeds the retained bundle [bundle:1].",
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
            "Key Findings": "Key findings integrate the retained evidence into a bounded synthesis rather than stitched snippets, and they distinguish stronger review-level support from more tentative primary-study signals [bundle:1].",
            "Limitations": "The main limits are scope, incomplete coverage, heterogeneous certainty, and the possibility of omitted contradictory sources that could materially shift the confidence of the synthesis.",
            "Gaps Identified": "No independent replication has confirmed these synthesis-level findings, and the gap between review-level evidence and applied outcomes remains untested.",
            "Conclusion": "The current evidence supports a cautious synthesis with explicit uncertainty, transparent methodological limits, and no claim that exceeds the retained bundle [bundle:1].",
        },
        "source_bundle": [
            {
                "title": f"S{i}",
                "year": 2024,
                "evidence_type": "review",
                "doi": f"10.1234/s{i}",
                "excerpt": (
                    "The bounded evidence supports a cautious synthesis with explicit uncertainty and "
                    "distinguishes review-level support from primary-study signals."
                ),
            }
            for i in range(12)
        ],
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


def test_public_register_agent_issues_limited_key_that_can_submit(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("RESEARKA_V2_PUBLIC_KEY_DAILY_LIMIT", "7")
    response = client.post(
        "/agents/register",
        headers={"x-api-key": ""},
        json={"agent_id": "public-agent-1", "label": "pilot"},
    )

    assert response.status_code == 201
    data = response.json()
    assert data["agent_id"] == "public-agent-1"
    assert data["api_key"].startswith("rk_")
    assert data["daily_limit"] == 7
    assert "key_hash" not in data

    submit = client.post(
        "/submissions",
        headers={"x-api-key": data["api_key"]},
        json={**_minimal_submission_payload(), "author_agent_id": "claimed-agent"},
    )
    assert submit.status_code == 200
    assert submit.json()["submission"]["metadata"]["author_agent_id"] == "public-agent-1"
    assert submit.json()["submission"]["metadata"]["claimed_author_agent_id"] == "claimed-agent"


def test_public_register_agent_rejects_duplicate_and_bad_agent_id(client: TestClient) -> None:
    assert client.post("/agents/register", json={"agent_id": "Bad Agent!"}).status_code == 400
    first = client.post("/agents/register", json={"agent_id": "public-agent-2"})
    second = client.post("/agents/register", json={"agent_id": "public-agent-2"})
    assert first.status_code == 201
    assert second.status_code == 409
    assert second.json()["detail"] == "agent_already_registered"


def test_public_register_agent_can_be_disabled(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("RESEARKA_V2_PUBLIC_REGISTRATION_ENABLED", "0")
    response = client.post("/agents/register", json={"agent_id": "public-agent-3"})
    assert response.status_code == 403
    assert response.json()["detail"] == "public_registration_disabled"


def test_public_register_agent_db_flag_blocks_without_restart(client: TestClient) -> None:
    import sqlite3

    from apps.runtime_api.rate_limits import ensure_schema

    db_path = os.environ["RESEARKA_V2_RATE_LIMIT_DB_PATH"]
    ensure_schema(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute("INSERT OR REPLACE INTO flags VALUES ('public_registration', 0)")
    blocked = client.post("/agents/register", json={"agent_id": "flag-agent"})
    assert blocked.status_code == 503
    assert blocked.json()["detail"] == "registration_paused"

    with sqlite3.connect(db_path) as conn:
        conn.execute("INSERT OR REPLACE INTO flags VALUES ('public_registration', 1)")
    allowed = client.post("/agents/register", json={"agent_id": "flag-agent"})
    assert allowed.status_code == 201


def test_public_register_agent_rate_limits_by_client(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("RESEARKA_V2_PUBLIC_REGISTRATIONS_PER_IP_PER_DAY", "1")

    first = client.post("/agents/register", headers={"x-forwarded-for": "203.0.113.10"}, json={"agent_id": "rl-a"})
    second = client.post("/agents/register", headers={"x-forwarded-for": "203.0.113.10"}, json={"agent_id": "rl-b"})
    other_ip = client.post("/agents/register", headers={"x-forwarded-for": "203.0.113.11"}, json={"agent_id": "rl-c"})

    assert first.status_code == 201
    assert second.status_code == 429
    assert second.json()["detail"] == "registration_rate_limited"
    assert other_ip.status_code == 201


def test_public_register_agent_uses_proxy_appended_forwarded_ip(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("RESEARKA_V2_PUBLIC_REGISTRATIONS_PER_IP_PER_DAY", "1")

    first = client.post(
        "/agents/register",
        headers={"x-forwarded-for": "198.51.100.99, 203.0.113.20"},
        json={"agent_id": "proxy-a"},
    )
    spoofed = client.post(
        "/agents/register",
        headers={"x-forwarded-for": "198.51.100.100, 203.0.113.20"},
        json={"agent_id": "proxy-b"},
    )
    other_ip = client.post(
        "/agents/register",
        headers={"x-forwarded-for": "198.51.100.100, 203.0.113.21"},
        json={"agent_id": "proxy-c"},
    )

    assert first.status_code == 201
    assert spoofed.status_code == 429
    assert other_ip.status_code == 201


def test_public_register_agent_uses_global_cap_for_local_mcp_calls(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("RESEARKA_V2_PUBLIC_REGISTRATIONS_PER_DAY", "2")
    monkeypatch.setenv("RESEARKA_V2_PUBLIC_REGISTRATIONS_PER_IP_PER_DAY", "1")

    first = client.post("/agents/register", json={"agent_id": "mcp-a"})
    second = client.post("/agents/register", json={"agent_id": "mcp-b"})
    third = client.post("/agents/register", json={"agent_id": "mcp-c"})

    assert first.status_code == 201
    assert second.status_code == 201
    assert third.status_code == 429


def test_public_register_agent_allows_launch_scale_global_cap(client: TestClient, monkeypatch) -> None:
    from apps.runtime_api import app as app_mod

    observed_limits: list[int] = []

    def fake_check_and_incr(kind: str, _key: str, *, limit: int, window: str) -> bool:
        _ = window
        if kind == "global":
            observed_limits.append(limit)
        return True

    monkeypatch.setenv("RESEARKA_V2_PUBLIC_REGISTRATIONS_PER_DAY", "150000")
    monkeypatch.setattr(app_mod.rate_limits, "check_and_incr", fake_check_and_incr)

    response = client.post("/agents/register", json={"agent_id": "scale-agent"})

    assert response.status_code == 201
    assert observed_limits == [150000]


def test_public_register_agent_rate_limit_survives_app_recreate(inmemory_repo, monkeypatch) -> None:
    from apps.runtime_api.app import create_app

    monkeypatch.setenv("RESEARKA_V2_PUBLIC_REGISTRATIONS_PER_DAY", "1")

    first_client = TestClient(create_app(inmemory_repo))
    second_client = TestClient(create_app(inmemory_repo))

    assert first_client.post("/agents/register", json={"agent_id": "restart-a"}).status_code == 201
    blocked = second_client.post("/agents/register", json={"agent_id": "restart-b"})

    assert blocked.status_code == 429
    assert blocked.json()["detail"] == "registration_rate_limited"


def test_public_register_agent_counts_invalid_attempts(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("RESEARKA_V2_PUBLIC_REGISTRATIONS_PER_DAY", "1")

    invalid = client.post("/agents/register", json={"agent_id": "Bad Agent!"})
    valid = client.post("/agents/register", json={"agent_id": "after-bad-agent"})

    assert invalid.status_code == 400
    assert valid.status_code == 429


def test_public_register_agent_limits_agent_id_once_per_day(client: TestClient) -> None:
    first = client.post("/agents/register", json={"agent_id": "daily-agent"})
    repo = _repository(client)
    key_hash = next(key.key_hash for key in repo.list_api_keys() if key.agent_id == "daily-agent")
    repo.revoke_api_key(key_hash)
    second = client.post("/agents/register", json={"agent_id": "daily-agent"})

    assert first.status_code == 201
    assert second.status_code == 429
    assert second.json()["detail"] == "registration_rate_limited"


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


def test_ops_create_key_defaults_to_public_agent_daily_limit(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("RESEARKA_V2_ADMIN_KEY", "admin-secret-123")
    monkeypatch.setenv("RESEARKA_V2_DEFAULT_DAILY_LIMIT", "25")
    response = client.post(
        "/ops/keys",
        headers=_ops_headers(),
        json={"agent_id": "agent-1", "label": "pilot-key"},
    )
    assert response.status_code == 200
    assert response.json()["daily_limit"] == 25


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
    assert resp3.status_code == 429
    assert resp3.json()["detail"] == "daily_limit_exceeded"


def test_duplicate_submission_rejected_for_same_agent(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("RESEARKA_V2_ADMIN_KEY", "admin-secret-123")
    create_resp = client.post(
        "/ops/keys",
        headers=_ops_headers(),
        json={"agent_id": "agent-1"},
    )
    raw_key = create_resp.json()["raw_key"]
    first = client.post(
        "/submissions",
        headers={"x-api-key": raw_key},
        json=_minimal_submission_payload(),
    )
    assert first.status_code == 200
    duplicate = client.post(
        "/submissions",
        headers={"x-api-key": raw_key},
        json=_minimal_submission_payload(),
    )
    assert duplicate.status_code == 409
    detail = duplicate.json()["detail"]
    assert detail["error"] == "duplicate_submission"
    assert detail["submission_id"] == first.json()["submission"]["id"]


def test_rejected_duplicate_submission_can_resubmit(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("RESEARKA_V2_ADMIN_KEY", "admin-secret-123")
    create_resp = client.post(
        "/ops/keys",
        headers=_ops_headers(),
        json={"agent_id": "agent-1"},
    )
    raw_key = create_resp.json()["raw_key"]
    first = client.post(
        "/submissions",
        headers={"x-api-key": raw_key},
        json=_minimal_submission_payload(),
    )
    assert first.status_code == 200
    first_id = first.json()["submission"]["id"]
    _repository(client).create_object(
        ResearchObject(
            object_type=ObjectType.DECISION,
            parent_object_id=first_id,
            title="Rejected duplicate seed",
            metadata={"decision": Decision.REJECT.value},
        )
    )

    second = client.post(
        "/submissions",
        headers={"x-api-key": raw_key},
        json=_minimal_submission_payload(),
    )

    assert second.status_code == 200
    assert second.json()["submission"]["id"] != first_id


def test_submission_parent_id_links_resubmission(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("RESEARKA_V2_ADMIN_KEY", "admin-secret-123")
    create_resp = client.post(
        "/ops/keys",
        headers=_ops_headers(),
        json={"agent_id": "agent-1"},
    )
    raw_key = create_resp.json()["raw_key"]
    parent = client.post(
        "/submissions",
        headers={"x-api-key": raw_key},
        json=_minimal_submission_payload(),
    ).json()["submission"]
    revised = _minimal_submission_payload()
    revised["title"] = "Rapid Evidence Synthesis: revised topic"
    revised["parent_submission_id"] = parent["id"]
    response = client.post(
        "/submissions",
        headers={"x-api-key": raw_key},
        json=revised,
    )
    assert response.status_code == 200
    metadata = response.json()["submission"]["metadata"]
    assert metadata["parent_submission_id"] == parent["id"]


def test_submission_parent_id_rejects_missing_parent(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("RESEARKA_V2_ADMIN_KEY", "admin-secret-123")
    create_resp = client.post(
        "/ops/keys",
        headers=_ops_headers(),
        json={"agent_id": "agent-1"},
    )
    payload = _minimal_submission_payload()
    payload["parent_submission_id"] = "missing-parent"
    response = client.post(
        "/submissions",
        headers={"x-api-key": create_resp.json()["raw_key"]},
        json=payload,
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "parent_submission_not_found"


def test_agent_backoff_after_consecutive_intake_rejections(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("RESEARKA_V2_ADMIN_KEY", "admin-secret-123")
    monkeypatch.setenv("RESEARKA_V2_INTAKE_REJECTION_BACKOFF", "3")
    create_resp = client.post(
        "/ops/keys",
        headers=_ops_headers(),
        json={"agent_id": "agent-1"},
    )
    repo = _repository(client)
    for index in range(3):
        submission = repo.create_object(
            ResearchObject(
                object_type=ObjectType.SUBMISSION,
                title=f"bad submission {index}",
                metadata={"authenticated_agent_id": "agent-1", "author_agent_id": "agent-1"},
            )
        )
        repo.create_object(
            ResearchObject(
                object_type=ObjectType.DECISION,
                parent_object_id=submission.id,
                title="intake reject",
                metadata={"decision": Decision.REJECT.value, "notes": ["intake gate rejection"]},
            )
        )
    response = client.post(
        "/submissions",
        headers={"x-api-key": create_resp.json()["raw_key"]},
        json=_minimal_submission_payload(),
    )
    assert response.status_code == 429
    assert response.json()["detail"] == "agent_backoff_intake_rejections"


def test_agent_backoff_intake_rejections_decay_after_window(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("RESEARKA_V2_ADMIN_KEY", "admin-secret-123")
    monkeypatch.setenv("RESEARKA_V2_INTAKE_REJECTION_BACKOFF", "3")
    monkeypatch.setenv("RESEARKA_V2_INTAKE_REJECTION_BACKOFF_WINDOW_HOURS", "6")
    create_resp = client.post(
        "/ops/keys",
        headers=_ops_headers(),
        json={"agent_id": "agent-1"},
    )
    repo = _repository(client)
    stale = datetime.now(timezone.utc) - timedelta(hours=7)
    for index in range(3):
        submission = repo.create_object(
            ResearchObject(
                object_type=ObjectType.SUBMISSION,
                title=f"bad submission {index}",
                created_at=stale,
                metadata={"authenticated_agent_id": "agent-1", "author_agent_id": "agent-1"},
            )
        )
        repo.create_object(
            ResearchObject(
                object_type=ObjectType.DECISION,
                parent_object_id=submission.id,
                title="intake reject",
                metadata={"decision": Decision.REJECT.value, "notes": ["intake gate rejection"]},
            )
        )
    response = client.post(
        "/submissions",
        headers={"x-api-key": create_resp.json()["raw_key"]},
        json=_minimal_submission_payload(),
    )
    assert response.status_code == 200


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


def test_disabled_agent_cannot_submit_or_register(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("RESEARKA_DISABLED_AGENT_IDS", "retired-agent")
    payload = {**_minimal_submission_payload(), "author_agent_id": "retired-agent"}

    legacy = client.post("/submissions", json=payload)
    registered = client.post("/agents/register", json={"agent_id": "retired-agent"})

    assert legacy.status_code == registered.status_code == 403
    assert legacy.json()["detail"] == registered.json()["detail"] == "agent_disabled"


def test_disabled_agent_key_cannot_submit(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("RESEARKA_DISABLED_AGENT_IDS", "retired-agent")
    key = _repository(client).create_api_key("retired-agent", daily_limit=10)

    response = client.post("/submissions", headers={"x-api-key": key.raw_key}, json=_minimal_submission_payload())

    assert response.status_code == 403
    assert response.json()["detail"] == "agent_disabled"


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
    assert data["receipt"] == {"status": "missing", "valid": False}


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


def test_default_calibration_receipt_uses_current_working_gold_set(client: TestClient, monkeypatch) -> None:
    from apps.runtime_api.app import reset_calibration_cache

    reset_calibration_cache()
    monkeypatch.delenv("RESEARKA_V2_CALIBRATION_PATH", raising=False)
    monkeypatch.setenv("RESEARKA_V2_CALIBRATION_MAX_AGE_DAYS", "365")

    receipt = client.get("/calibration").json()["receipt"]

    assert receipt["artifact"] == "gold_set_eval_v3_current.json"
    assert receipt["provider"] == "reviewer-panel"
    assert receipt["corpus_status"] == "working"
    assert receipt["case_count"] == 30
    assert receipt["valid"] is False


def _complete_calibration_artifact(release_id: str, generated_at: datetime) -> dict[str, Any]:
    article_types = [item.value for item in ArticleType]
    decisions = [item.value for item in Decision]
    results = [
        {
            "entry_id": f"case-{index}",
            "article_type": article_types[index % len(article_types)],
            "domain_slug": f"domain-{index % 8}",
            "expected_decision": decisions[index % len(decisions)],
            "actual_decision": decisions[index % len(decisions)],
            "duration_s": 1.0,
            "cost_usd": 0.01,
        }
        for index in range(100)
    ]
    return {
        "run_meta": {
            "timestamp": generated_at.isoformat(),
            "corpus_status": "adjudicated",
            "target_judge_release_id": release_id,
            "judge_release_id": release_id,
            "judge_release_consistent": True,
            "judge_release_target_matched": True,
            "inter_adjudicator_decision_agreement": 0.9,
            "inter_adjudicator_kappa": 0.8,
            "labels_frozen_at": (generated_at - timedelta(seconds=2)).isoformat(),
            "labels_revealed_at": (generated_at - timedelta(seconds=1)).isoformat(),
        },
        "summary": summarize_gold_results(results),
        "results": results,
    }


def test_calibration_rejects_inconsistent_derived_metrics() -> None:
    artifact = _complete_calibration_artifact("sha256:" + "0" * 64, datetime.now(timezone.utc))
    artifact["results"][1]["actual_decision"] = Decision.ACCEPT.value
    artifact["summary"] = summarize_gold_results(artifact["results"])

    assert calibration_metrics_complete(artifact) is True

    for mutate in (
        lambda summary: summary["accept_blockers"].update(tampered=1),
        lambda summary: summary["boolean_match_rates"].update(overclaim_verdict=1.0),
        lambda summary: summary["class_metrics"]["accept"].update(precision=0.5),
        lambda summary: next(iter(summary["by_article_type"].values())).update(accuracy=0.5),
        lambda summary: summary.update(false_accept_count=0),
        lambda summary: summary.update(false_accept_rate=0.5),
        lambda summary: summary.update(cohen_kappa=0.5),
        lambda summary: summary["cost"].update(total_usd=0.0),
        lambda summary: summary["latency"].update(mean_s=0.0),
        lambda summary: summary["rubric_mae"].update(source_grounding=0.0),
    ):
        tampered = json.loads(json.dumps(artifact))
        mutate(tampered["summary"])
        assert calibration_metrics_complete(tampered) is False

    stale = json.loads(json.dumps(artifact))
    stale["results"][2]["actual_decision"] = Decision.ACCEPT.value
    assert calibration_metrics_complete(stale) is False

    failed = json.loads(json.dumps(artifact))
    failed["results"][2].update(actual_decision=None, error="provider_crashed")
    failed["summary"] = summarize_gold_results(failed["results"])
    assert calibration_metrics_complete(failed) is False

    missing = json.loads(json.dumps(artifact))
    del missing["summary"]["accept_blockers"]
    assert calibration_metrics_complete(missing) is False

    extra = json.loads(json.dumps(artifact))
    extra["summary"]["unverified_metric"] = 1
    assert calibration_metrics_complete(extra) is False

    for invalid_row in (
        {"duration_s": -1},
        {"cost_usd": -1},
        {"domain_slug": ""},
        {"actual_decision": "unknown"},
    ):
        invalid = json.loads(json.dumps(artifact))
        invalid["results"][2].update(invalid_row)
        invalid["summary"] = summarize_gold_results(invalid["results"])
        assert calibration_metrics_complete(invalid) is False


def _test_judge_release(code_sha: str) -> dict[str, Any]:
    release: dict[str, Any] = {
        "code_sha": code_sha,
        "policy_version": "judge-policy-v1",
        "reviewer_prompt_version": "reviewer-test-v1",
        "editor_prompt_version": "editor-test-v1",
        "provider": "reviewer-panel",
        "models": ["model-a", "model-b"],
        "settings": {"accept_quorum_min": 2},
    }
    return {"id": judge_release_id(release), **release}


def test_calibration_requires_post_evaluation_signoff_and_active_release_binding(
    client: TestClient,
    tmp_path,
    monkeypatch,
) -> None:
    from apps.runtime_api.app import reset_calibration_cache
    from apps.runtime_api.app import _JUDGE_CODE_SHA
    from runtime_core.judge_release import unsigned_calibration_sha256

    release = _test_judge_release(_JUDGE_CODE_SHA)
    release_id = release["id"]
    generated_at = datetime.now(timezone.utc) - timedelta(seconds=2)
    artifact = _complete_calibration_artifact(release_id, generated_at)
    artifact["run_meta"]["human_signoff"] = {
        "approved": True,
        "results_reviewed": True,
        "limitations_reviewed": True,
        "evaluation_sha256": unsigned_calibration_sha256(artifact),
        "judge_release_id": release_id,
        "signed_by": "calibration-chair",
        "signed_at": datetime.now(timezone.utc).isoformat(),
        "statement": "I reviewed the measured results and stated limitations.",
    }
    calibration_path = tmp_path / "signed-calibration.json"
    calibration_path.write_text(__import__("json").dumps(artifact))
    active_release_path = tmp_path / "active-release.json"
    active_release_path.write_text(
        __import__("json").dumps(
            {
                **release,
                "calibration": {
                    "artifact": calibration_path.name,
                    "sha256": hashlib.sha256(calibration_path.read_bytes()).hexdigest(),
                },
            }
        )
    )
    monkeypatch.setenv("RESEARKA_V2_CALIBRATION_PATH", str(calibration_path))
    monkeypatch.setenv("RESEARKA_V2_ACTIVE_JUDGE_RELEASE_PATH", str(active_release_path))
    reset_calibration_cache()

    receipt = client.get("/calibration").json()["receipt"]

    assert receipt["human_signed"] is True
    assert receipt["judge_release_bound"] is True
    assert receipt["valid"] is True

    artifact["summary"]["class_metrics"] = {}
    artifact["run_meta"]["human_signoff"]["evaluation_sha256"] = unsigned_calibration_sha256(artifact)
    calibration_path.write_text(__import__("json").dumps(artifact))
    active_release = __import__("json").loads(active_release_path.read_text())
    active_release["calibration"]["sha256"] = hashlib.sha256(calibration_path.read_bytes()).hexdigest()
    active_release_path.write_text(__import__("json").dumps(active_release))

    receipt = client.get("/calibration").json()["receipt"]

    assert receipt["metrics_complete"] is False
    assert receipt["valid"] is False


def test_calibration_rejects_predated_signoff_and_wrong_release_sha(
    client: TestClient,
    tmp_path,
    monkeypatch,
) -> None:
    from apps.runtime_api.app import reset_calibration_cache
    from apps.runtime_api.app import _JUDGE_CODE_SHA
    from runtime_core.judge_release import unsigned_calibration_sha256

    release = _test_judge_release(_JUDGE_CODE_SHA)
    release_id = release["id"]
    generated_at = datetime.now(timezone.utc)
    artifact = _complete_calibration_artifact(release_id, generated_at)
    artifact["run_meta"]["human_signoff"] = {
        "approved": True,
        "results_reviewed": True,
        "limitations_reviewed": True,
        "evaluation_sha256": unsigned_calibration_sha256(artifact),
        "judge_release_id": release_id,
        "signed_by": "calibration-chair",
        "signed_at": "1900-01-01T00:00:00Z",
        "statement": "Invalid predated signoff.",
    }
    calibration_path = tmp_path / "invalid-calibration.json"
    calibration_path.write_text(__import__("json").dumps(artifact))
    active_release_path = tmp_path / "active-release.json"
    active_release_path.write_text(
        __import__("json").dumps({**release, "calibration": {"sha256": "wrong"}})
    )
    monkeypatch.setenv("RESEARKA_V2_CALIBRATION_PATH", str(calibration_path))
    monkeypatch.setenv("RESEARKA_V2_ACTIVE_JUDGE_RELEASE_PATH", str(active_release_path))
    reset_calibration_cache()

    receipt = client.get("/calibration").json()["receipt"]

    assert receipt["human_signed"] is False
    assert receipt["judge_release_bound"] is False
    assert receipt["valid"] is False


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


def test_adjudicated_calibration_mismatches_do_not_expose_private_titles(
    client: TestClient,
    tmp_path,
    monkeypatch,
) -> None:
    from apps.runtime_api.app import reset_calibration_cache

    reset_calibration_cache()
    artifact = {
        "run_meta": {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "corpus_status": "adjudicated",
        },
        "summary": {
            "total": 1,
            "correct": 0,
            "accuracy": 0.0,
            "confusion_matrix": {},
            "mismatches": [
                {
                    "entry_id": "case-private",
                    "title": "Confidential rejected manuscript title",
                    "article_type": "research_synthesis",
                    "expected": "reject",
                    "actual": "accept",
                }
            ],
        },
        "results": [{"entry_id": "case-private"}],
    }
    path = tmp_path / "adjudicated.json"
    path.write_text(__import__("json").dumps(artifact))
    monkeypatch.setenv("RESEARKA_V2_CALIBRATION_PATH", str(path))

    mismatch = client.get("/calibration/mismatches").json()["mismatches"][0]

    assert mismatch["entry_id"] == "case-private"
    assert "title" not in mismatch


def test_calibration_benchmark_format(client: TestClient, tmp_path, monkeypatch) -> None:
    from apps.runtime_api.app import reset_calibration_cache

    reset_calibration_cache()
    benchmark_data = {
        "run_meta": {
            "run_id": "test-1",
            "provider": "judge_panel",
            "paper_count": 4,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
        "aggregates": {
            "total": 999,
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
    assert "mismatches" not in data["overall"]
    assert data["confusion_matrix"]["revise"]["accept"] == 1
    assert "high" in data["by_category"]
    # Paper 4 has error, counted as gate failure
    assert data["gate_failures"] == {"review": [4]}
    # Paper 3: medium quality, expected revise, actual accept → mismatch
    assert data["mismatch_count"] == 1
    assert data["receipt"]["artifact"] == "benchmark_baseline.json"
    assert data["receipt"]["provider"] == "judge_panel"
    assert data["receipt"]["case_count"] == 4
    assert data["receipt"]["minimum_cases"] == 100
    assert data["receipt"]["valid"] is False
    assert len(data["receipt"]["sha256"]) == 64

    import apps.runtime_api.app as app_module

    assert app_module._calibration_cache is not None
    app_module._calibration_cache["receipt"]["generated_at"] = "2020-01-01T00:00:00"
    assert client.get("/calibration").json()["receipt"]["status"] == "stale_or_insufficient"

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


def test_jobs_queue_requires_admin(client: TestClient) -> None:
    """Operator-only: the queue + event log expose other agents' payloads."""
    assert client.get("/jobs/queue").status_code == 403
    assert client.get("/jobs/queue", headers=_worker_headers()).status_code == 200
