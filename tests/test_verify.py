from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from apps.runtime_api.app import create_app
from contracts import ObjectType
from runtime_core.doi_resolver import verify_source_metadata
from runtime_core.repos import InMemoryRuntimeRepository
from runtime_core.verify import build_evidence_manifest, manifest_signature_valid, sign_manifest


DOCUMENT = """# Draft
The intervention reduced risk by 47% [1]. The authors wrote "The trial enrolled 120 adults with confirmed disease" [1].

## References
1. Smith J. (2024). Trial report. https://doi.org/10.1234/example
"""


def test_database_constraint_covers_all_object_types() -> None:
    migration = (
        Path(__file__).parents[1]
        / "alembic/versions/0012_add_verification_object_type.py"
    ).read_text()
    allowed = set(re.findall(r"'([a-z_]+)'", migration))
    revision = re.search(r'^revision = "([^"]+)"', migration, re.MULTILINE)

    assert {value.value for value in ObjectType} <= allowed
    assert revision is not None and len(revision.group(1)) <= 32


def test_manifest_separates_pass_fail_and_unchecked(monkeypatch) -> None:
    monkeypatch.setattr("runtime_core.verify.resolve_dois", lambda _, **__: {
        "available": True, "checked": ["10.1234/example"], "missing": [],
    })
    monkeypatch.setattr("runtime_core.verify.verify_source_metadata", lambda _, **__: {
        "checked": ["doi:10.1234/example"],
        "source_profiles": [{
            "identity": "doi:10.1234/example",
            "publication_types": ["journal-article"],
            "retracted": False,
            "text_scope": "registry_abstract",
        }],
        "claim_checks": [{
            "identity": "doi:10.1234/example",
            "text": "The intervention reduced risk by 47% [1].",
            "authority_available": True,
            "outcome": "contradicted",
            "passage": "The intervention reduced risk by 12%.",
            "claimed_quantities": [["47", "%"]],
            "passage_quantities": [["12", "%"]],
        }],
        "quote_checks": [{
            "identity": "doi:10.1234/example", "text": "The trial enrolled 120 adults with confirmed disease",
            "authority_available": True, "matched": True,
        }],
        "number_checks": [{
            "identity": "doi:10.1234/example", "text": "The intervention reduced risk by 47% [1].",
            "authority_available": True, "matched": False,
        }],
    })

    manifest = build_evidence_manifest(
        DOCUMENT, [], receipt_id="receipt-1", verifier_release="verify-v1:test"
    )

    assert manifest["overall_status"] == "issues_found"
    assert manifest["checks"]["citation"] == {"found": 1, "checked": 1, "verified": 1, "failed": 0, "not_checked": 0}
    assert manifest["checks"]["quotation"]["verified"] == 1
    assert manifest["checks"]["claim"]["failed"] == 1
    assert manifest["checks"]["number"]["failed"] == 1
    contradiction = next(row for row in manifest["findings"] if row["kind"] == "claim")
    assert contradiction["outcome"] == "contradicted"
    assert contradiction["passage"] == "The intervention reduced risk by 12%."
    citation = next(row for row in manifest["findings"] if row["kind"] == "citation")
    assert citation["source_type"] == "journal-article"
    assert citation["text_scope"] == "registry_abstract"
    assert "text" not in manifest["document"]


def test_registry_outage_never_becomes_a_pass(monkeypatch) -> None:
    monkeypatch.setattr("runtime_core.verify.resolve_dois", lambda _, **__: {
        "available": False, "checked": [], "missing": [],
    })
    monkeypatch.setattr("runtime_core.verify.verify_source_metadata", lambda _, **__: {
        "checked": [], "unverified": ["doi:10.1234/example"], "quote_checks": [], "number_checks": [],
    })

    manifest = build_evidence_manifest(
        DOCUMENT, [], receipt_id="receipt-2", verifier_release="verify-v1:test"
    )

    assert manifest["overall_status"] == "partial"
    assert manifest["checks"]["citation"]["not_checked"] == 1


def test_manifest_signature_detects_tampering() -> None:
    manifest = {"receipt_id": "receipt-3", "overall_status": "checks_passed"}
    signature = sign_manifest(manifest, "s" * 32)

    assert manifest_signature_valid(manifest, signature, "s" * 32)
    assert not manifest_signature_valid({**manifest, "overall_status": "issues_found"}, signature, "s" * 32)


def test_source_text_checks_require_exact_quote_and_number(monkeypatch) -> None:
    class Response:
        status_code = 200
        text = ""

        def json(self) -> dict[str, Any]:
            return {"message": {
                "title": ["Registered intervention trial"],
                "abstract": "The trial enrolled 120 adults with confirmed disease and reduced risk by 12%.",
            }}

        def raise_for_status(self) -> None:
            return None

    class Client:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def __enter__(self) -> "Client":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def get(self, url: str) -> Response:
            if "openalex" in url:
                response = Response()
                response.status_code = 404
                return response
            return Response()

    monkeypatch.setenv("RESEARKA_SOURCE_METADATA_CHECK_ENABLED", "1")
    monkeypatch.setattr("runtime_core.doi_resolver.httpx.Client", Client)

    result = verify_source_metadata([{
        "doi": "10.1234/example",
        "verification_quotes": ["The trial enrolled 120 adults with confirmed disease"],
        "verification_claims": ["The intervention reduced risk by 47% [1]."],
    }])

    assert result is not None
    assert result["title_mismatches"] == []
    assert result["source_profiles"][0]["text_scope"] == "registry_abstract"
    assert result["quote_checks"][0]["matched"] is True
    assert result["number_checks"][0]["matched"] is False
    assert result["claim_checks"][0]["outcome"] == "contradicted"
    assert "reduced risk by 12%" in result["claim_checks"][0]["passage"]


def test_pmc_lookup_returns_exact_passage_for_supported_claim(monkeypatch) -> None:
    requested_urls: list[str] = []

    class Response:
        def __init__(
            self,
            *,
            payload: dict[str, Any] | None = None,
            text: str = "",
            status_code: int = 200,
        ) -> None:
            self._payload = payload or {}
            self.text = text
            self.status_code = status_code

        def json(self) -> dict[str, Any]:
            return self._payload

        def raise_for_status(self) -> None:
            return None

    class Client:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def __enter__(self) -> "Client":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def get(self, url: str) -> Response:
            requested_urls.append(url)
            if "idconv" in url:
                return Response(payload={"records": [{
                    "doi": "10.1234/example", "pmid": "12345678", "pmcid": "PMC1234567",
                }]})
            if "efetch" in url:
                return Response(text="""<pmc-articleset><article><front><article-meta>
                    <article-id pub-id-type="pmcid">PMC1234567</article-id>
                    <article-id pub-id-type="pmid">12345678</article-id>
                    <article-id pub-id-type="doi">10.1234/example</article-id>
                    </article-meta></front><body><p>The intervention reduced risk by 12% in 120 adults. The database indexed 260 000 records.</p></body>
                    </article></pmc-articleset>""")
            if "esummary" in url:
                return Response(payload={"result": {"12345678": {
                    "title": "Registered intervention trial",
                    "articleids": [{"idtype": "doi", "value": "10.1234/example"}],
                }}})
            if "openalex" in url:
                return Response(status_code=404)
            return Response(payload={"message": {
                "title": ["Registered intervention trial"], "type": "journal-article",
            }})

    monkeypatch.setenv("RESEARKA_SOURCE_METADATA_CHECK_ENABLED", "1")
    monkeypatch.setattr("runtime_core.doi_resolver.httpx.Client", Client)

    result = verify_source_metadata([{
        "doi": "10.1234/example",
        "pmid": "12345678",
        "verification_claims": [
            "The intervention reduced risk by 12% in 120 adults [1].",
            "The database indexed 260,000 records [1].",
        ],
    }])

    assert result is not None
    assert any("idconv" in url and "idtype=doi" in url for url in requested_urls)
    assert any("idconv" in url and "idtype=pmid" in url for url in requested_urls)
    assert any("efetch" in url for url in requested_urls)
    assert result["source_profiles"][0]["text_scope"] == "pmc_full_text"
    assert result["source_profiles"][0]["publication_types"] == ["journal-article"]
    assert result["claim_checks"][0]["outcome"] == "supported"
    assert "reduced risk by 12% in 120 adults" in result["claim_checks"][0]["passage"]
    assert result["claim_checks"][1]["outcome"] == "supported"
    assert "260 000 records" in result["claim_checks"][1]["passage"]
    assert result["number_checks"][0]["matched"] is True
    assert "reduced risk by 12%" in result["number_checks"][0]["passage"]


def test_pmc_lookup_flags_context_matched_numeric_contradiction(monkeypatch) -> None:
    class Response:
        status_code = 200

        def __init__(self, url: str) -> None:
            self.url = url
            self.text = (
                "<article><front><article-meta>"
                '<article-id pub-id-type="pmcid">PMC1234567</article-id>'
                '<article-id pub-id-type="doi">10.1234/example</article-id>'
                "</article-meta></front><body><p>The intervention reduced risk by 12%.</p></body>"
                "<back><ref><article-title>The intervention reduced risk by 47%.</article-title>"
                "</ref></back></article>"
                if "efetch" in url else ""
            )

        def json(self) -> dict[str, Any]:
            if "idconv" in self.url:
                return {"records": [{"doi": "10.1234/example", "pmcid": "PMC1234567"}]}
            return {"message": {"title": ["Registered intervention trial"], "type": "journal-article"}}

        def raise_for_status(self) -> None:
            return None

    class Client:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def __enter__(self) -> "Client":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def get(self, url: str) -> Response:
            return Response(url)

    monkeypatch.setenv("RESEARKA_SOURCE_METADATA_CHECK_ENABLED", "1")
    monkeypatch.setattr("runtime_core.doi_resolver.httpx.Client", Client)

    result = verify_source_metadata([{
        "doi": "10.1234/example",
        "verification_claims": ["The intervention reduced risk by 47% [1]."],
    }])

    assert result is not None
    assert result["claim_checks"][0]["outcome"] == "contradicted"
    assert result["claim_checks"][0]["claimed_quantities"] == [["47", "%"]]
    assert result["claim_checks"][0]["passage_quantities"] == [["12", "%"]]
    assert "reduced risk by 12%" in result["claim_checks"][0]["passage"]


def test_agent_metadata_path_does_not_trigger_automatic_pmc_fetch(monkeypatch) -> None:
    class Response:
        status_code = 200

        def json(self) -> dict[str, Any]:
            return {"message": {"title": ["Registered intervention trial"]}}

        def raise_for_status(self) -> None:
            return None

    class Client:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def __enter__(self) -> "Client":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def get(self, url: str) -> Response:
            return Response()

    def fail_if_called(*args: object) -> dict[str, str]:
        raise AssertionError("automatic PMC lookup leaked into the agent path")

    monkeypatch.setenv("RESEARKA_SOURCE_METADATA_CHECK_ENABLED", "1")
    monkeypatch.setattr("runtime_core.doi_resolver.httpx.Client", Client)
    monkeypatch.setattr("runtime_core.doi_resolver._pmc_id_map", fail_if_called)

    result = verify_source_metadata([{"doi": "10.1234/example"}])

    assert result is not None
    assert result["checked"] == ["doi:10.1234/example"]


def test_document_api_stores_only_signed_manifest(monkeypatch, tmp_path) -> None:
    repo = InMemoryRuntimeRepository()
    monkeypatch.setenv("RESEARKA_VERIFY_SIGNING_SECRET", "s" * 32)
    monkeypatch.setenv("RESEARKA_V2_RATE_LIMIT_DB_PATH", str(tmp_path / "rates.db"))
    monkeypatch.setattr("runtime_core.verify.resolve_dois", lambda _, **__: {
        "available": True, "checked": ["10.1234/example"], "missing": [],
    })
    monkeypatch.setattr("runtime_core.verify.verify_source_metadata", lambda _, **__: {
        "checked": ["doi:10.1234/example"], "quote_checks": [], "number_checks": [],
    })
    client = TestClient(create_app(repo))

    created = client.post("/verify/documents", json={"text": DOCUMENT})
    receipt_id = created.json()["manifest"]["receipt_id"]
    loaded = client.get(f"/verify/receipts/{receipt_id}")
    stored = repo.get_object(receipt_id)

    assert created.status_code == 200
    assert loaded.status_code == 200 and loaded.json()["signature_valid"] is True
    assert stored is not None and stored.object_type == ObjectType.VERIFICATION
    assert stored.body_markdown == ""
    assert DOCUMENT not in str(stored.metadata)


def test_document_api_fails_closed_without_signing_key(monkeypatch) -> None:
    monkeypatch.delenv("RESEARKA_VERIFY_SIGNING_SECRET", raising=False)
    monkeypatch.delenv("RESEARKA_VERIFY_SIGNING_SECRET_PATH", raising=False)
    response = TestClient(create_app(InMemoryRuntimeRepository())).post(
        "/verify/documents", json={"text": DOCUMENT}
    )
    assert response.status_code == 503
    assert response.json()["detail"] == "verification_signing_unavailable"


def test_document_api_rate_limit_is_durable(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("RESEARKA_VERIFY_SIGNING_SECRET", "s" * 32)
    monkeypatch.setenv("RESEARKA_V2_RATE_LIMIT_DB_PATH", str(tmp_path / "rates.db"))
    monkeypatch.setenv("RESEARKA_VERIFY_GLOBAL_PER_DAY", "1")
    monkeypatch.setattr("runtime_core.verify.resolve_dois", lambda _, **__: {})
    monkeypatch.setattr("runtime_core.verify.verify_source_metadata", lambda _, **__: {})
    client = TestClient(create_app(InMemoryRuntimeRepository()))

    assert client.post("/verify/documents", json={"text": DOCUMENT}).status_code == 200
    assert client.post("/verify/documents", json={"text": DOCUMENT}).status_code == 429


def test_document_api_reads_signing_secret_file(monkeypatch, tmp_path) -> None:
    secret_path = tmp_path / "verify.key"
    secret_path.write_text("f" * 32)
    monkeypatch.setenv("RESEARKA_VERIFY_SIGNING_SECRET_PATH", str(secret_path))
    monkeypatch.delenv("RESEARKA_VERIFY_SIGNING_SECRET", raising=False)
    monkeypatch.setenv("RESEARKA_V2_RATE_LIMIT_DB_PATH", str(tmp_path / "rates.db"))
    monkeypatch.setattr("runtime_core.verify.resolve_dois", lambda _, **__: {})
    monkeypatch.setattr("runtime_core.verify.verify_source_metadata", lambda _, **__: {})

    response = TestClient(create_app(InMemoryRuntimeRepository())).post(
        "/verify/documents", json={"text": DOCUMENT}
    )

    assert response.status_code == 200
    assert response.json()["signature_valid"] is True


def test_document_api_rate_limit_uses_final_proxy_address(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("RESEARKA_VERIFY_SIGNING_SECRET", "s" * 32)
    monkeypatch.setenv("RESEARKA_VERIFY_PER_IP_PER_DAY", "1")
    monkeypatch.setenv("RESEARKA_VERIFY_GLOBAL_PER_DAY", "10")
    monkeypatch.setenv("RESEARKA_V2_RATE_LIMIT_DB_PATH", str(tmp_path / "rates.db"))
    monkeypatch.setattr("runtime_core.verify.resolve_dois", lambda _, **__: {})
    monkeypatch.setattr("runtime_core.verify.verify_source_metadata", lambda _, **__: {})
    client = TestClient(create_app(InMemoryRuntimeRepository()))

    first = client.post(
        "/verify/documents",
        json={"text": DOCUMENT},
        headers={"x-forwarded-for": "198.51.100.1, 203.0.113.8"},
    )
    forged = client.post(
        "/verify/documents",
        json={"text": DOCUMENT},
        headers={"x-forwarded-for": "198.51.100.2, 203.0.113.8"},
    )

    assert first.status_code == 200
    assert forged.status_code == 429


def test_manifest_marks_unchecked_claims_and_omitted_sources_partial(monkeypatch) -> None:
    monkeypatch.setattr("runtime_core.verify.resolve_dois", lambda _, **__: {})
    monkeypatch.setattr("runtime_core.verify.verify_source_metadata", lambda _, **__: {})
    document = DOCUMENT + "\n2. Jones J. (2023). Other report. https://doi.org/10.1234/other"

    manifest = build_evidence_manifest(
        document, [], receipt_id="receipt-4", verifier_release="verify-v1:test", max_sources=1
    )

    assert manifest["overall_status"] == "partial"
    assert manifest["document"]["sources_omitted"] == 1
    assert manifest["checks"]["quotation"]["not_checked"] == 1
    assert manifest["checks"]["number"]["not_checked"] == 2


def test_manifest_caps_literal_checks_and_reports_omissions(monkeypatch) -> None:
    monkeypatch.setattr("runtime_core.verify.resolve_dois", lambda _, **__: {})
    monkeypatch.setattr("runtime_core.verify.verify_source_metadata", lambda _, **__: {})

    manifest = build_evidence_manifest(
        DOCUMENT,
        [],
        receipt_id="receipt-5",
        verifier_release="verify-v1:test",
        max_literal_checks=1,
    )

    assert manifest["overall_status"] == "partial"
    assert manifest["document"]["literal_checks_omitted"] == 2
    assert manifest["checks"]["quotation"]["found"] == 1
    assert manifest["checks"]["number"]["found"] == 0
