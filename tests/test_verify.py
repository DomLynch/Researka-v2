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

    assert {value.value for value in ObjectType} <= allowed


def test_manifest_separates_pass_fail_and_unchecked(monkeypatch) -> None:
    monkeypatch.setattr("runtime_core.verify.resolve_dois", lambda _, **__: {
        "available": True, "checked": ["10.1234/example"], "missing": [],
    })
    monkeypatch.setattr("runtime_core.verify.verify_source_metadata", lambda _, **__: {
        "checked": ["doi:10.1234/example"],
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
    assert manifest["checks"]["number"]["failed"] == 1
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
    assert result["quote_checks"][0]["matched"] is True
    assert result["number_checks"][0]["matched"] is False


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
