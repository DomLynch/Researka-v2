from __future__ import annotations

import hashlib
import hmac
import json
import re
from datetime import datetime, timezone
from typing import Any

from runtime_core.doi_resolver import normalize_arxiv_id, resolve_dois, source_identity, verify_source_metadata
from runtime_core.evidence_quality import claim_units, quantity_tokens, support_for_claim

VERIFY_SCHEMA_VERSION = 2
_DOI_RE = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.IGNORECASE)
_PMID_RE = re.compile(r"(?:\bPMID\s*:?\s*|pubmed\.ncbi\.nlm\.nih\.gov/)(\d{4,10})", re.IGNORECASE)
_ARXIV_RE = re.compile(
    r"(?:\barXiv\s*:\s*|arxiv\.org/(?:abs|html|pdf)/)"
    r"((?:\d{4}\.\d{4,5}|[A-Za-z-]+(?:\.[A-Za-z-]+)?/\d{7})(?:v\d+)?)",
    re.IGNORECASE,
)
_REFERENCE_HEADING_RE = re.compile(r"(?im)^\s*#{0,4}\s*(?:references|bibliography|works cited)\s*$")
_QUOTE_RE = re.compile(r"[\"“]([^\"”\n]{20,500})[\"”]")


def _clean_doi(value: str) -> str:
    value = value.strip().rstrip(".,;:")
    while value.endswith(")") and value.count(")") > value.count("("):
        value = value[:-1]
    return value.lower()


def _reference_first_text(text: str) -> tuple[str, str]:
    match = _REFERENCE_HEADING_RE.search(text)
    return (text[match.end() :], text[: match.start()]) if match else (text, text)


def _identifier_rows(text: str) -> list[dict[str, Any]]:
    reference_text, body_text = _reference_first_text(text)
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for section in (reference_text, body_text):
        matches = [
            (match.start(), "doi", _clean_doi(match.group(0))) for match in _DOI_RE.finditer(section)
        ] + [
            (match.start(), "pmid", match.group(1)) for match in _PMID_RE.finditer(section)
        ] + [
            (match.start(), "arxiv_id", normalize_arxiv_id(match.group(1)))
            for match in _ARXIV_RE.finditer(section)
        ]
        for _, kind, value in sorted(matches):
            identity = f"{kind}:{value}"
            if identity in seen:
                continue
            seen.add(identity)
            row: dict[str, Any] = {kind: value}
            for line in reference_text.splitlines():
                if value not in line.lower():
                    continue
                year = re.search(r"\b(?:19|20)\d{2}\b", line)
                surname = re.match(r"\s*(?:\[?\d+\]?\.?\s*)?([A-Z][A-Za-z'’-]{2,})", line)
                if year and surname:
                    row["_citation_aliases"] = [
                        f"{surname.group(1)} {year.group(0)}",
                        f"{surname.group(1)} et al {year.group(0)}",
                    ]
                break
            rows.append(row)
    return rows


def _merge_sources(text: str, supplied: list[dict[str, Any]], max_sources: int) -> tuple[list[dict[str, Any]], int]:
    sources: list[dict[str, Any]] = []
    by_identity: dict[str, dict[str, Any]] = {}
    for raw_source in [*supplied, *_identifier_rows(text)]:
        source = dict(raw_source)
        if source.get("doi"):
            source["doi"] = _clean_doi(str(source["doi"]))
        identity = source_identity(source)
        if not identity:
            continue
        if identity in by_identity:
            existing = by_identity[identity]
            for key, value in source.items():
                if value and not existing.get(key):
                    existing[key] = value
            continue
        row = {**source, "source_id": f"source_{len(sources) + 1}"}
        by_identity[identity] = row
        sources.append(row)
    total = len(sources)
    return sources[:max_sources], total


def _normalized(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.lower()))


def _claim_checks(
    text: str, sources: list[dict[str, Any]], max_checks: int
) -> tuple[list[str], list[str], int]:
    _, body = _reference_first_text(text)
    unmapped_quotes: list[str] = []
    unmapped_numbers: list[str] = []
    for sentence in claim_units(body):
        sentence = sentence.strip(" #")
        if len(sentence) < 20:
            continue
        mapped_ids = {
            str(row.get("source_id"))
            for row in support_for_claim(sentence, sources, require_evidence_alignment=False)
        }
        normalized_sentence = _normalized(sentence)
        for source in sources:
            aliases = [str(source.get("cited_as") or ""), *source.get("_citation_aliases", [])]
            if any(alias and _normalized(alias) in normalized_sentence for alias in aliases):
                mapped_ids.add(str(source["source_id"]))
        quotes = [" ".join(match.group(1).split()) for match in _QUOTE_RE.finditer(sentence)]
        quantities = quantity_tokens(sentence, sources)
        if not mapped_ids:
            unmapped_quotes.extend(quotes)
            if quantities:
                unmapped_numbers.append(sentence[:500])
            continue
        for source in sources:
            if source["source_id"] not in mapped_ids:
                continue
            if quotes:
                source.setdefault("verification_quotes", []).extend(quotes)
            if len(sentence) >= 40:
                source.setdefault("verification_claims", []).append(sentence[:500])
    remaining = max_checks
    omitted = 0
    for source in sources:
        for key in ("verification_quotes", "verification_claims"):
            if source.get(key):
                values = list(dict.fromkeys(source[key]))
                source[key] = values[:remaining]
                omitted += len(values) - len(source[key])
                remaining -= len(source[key])
    unmapped_quotes = list(dict.fromkeys(unmapped_quotes))
    unmapped_numbers = list(dict.fromkeys(unmapped_numbers))
    kept_quotes = unmapped_quotes[:remaining]
    remaining -= len(kept_quotes)
    kept_numbers = unmapped_numbers[:remaining]
    omitted += len(unmapped_quotes) - len(kept_quotes) + len(unmapped_numbers) - len(kept_numbers)
    return kept_quotes, kept_numbers, omitted


def _check_summary(findings: list[dict[str, Any]], kind: str) -> dict[str, int]:
    rows = [row for row in findings if row["kind"] == kind]
    return {
        "found": len(rows),
        "checked": sum(row["status"] != "not_checked" for row in rows),
        "verified": sum(row["status"] == "pass" for row in rows),
        "failed": sum(row["status"] == "fail" for row in rows),
        "not_checked": sum(row["status"] == "not_checked" for row in rows),
    }


def build_evidence_manifest(
    text: str,
    supplied_sources: list[dict[str, Any]],
    *,
    receipt_id: str,
    verifier_release: str,
    max_sources: int = 25,
    max_literal_checks: int = 100,
    checked_at: datetime | None = None,
) -> dict[str, Any]:
    sources, source_total = _merge_sources(text, supplied_sources, max_sources)
    unmapped_quotes, unmapped_numbers, checks_omitted = _claim_checks(
        text, sources, max_literal_checks
    )
    doi_result = resolve_dois(
        [str(source["doi"]) for source in sources if source.get("doi")], parallel=True
    ) or {}
    metadata = verify_source_metadata(sources, parallel=True) or {}
    findings: list[dict[str, Any]] = []
    missing_dois = set(doi_result.get("missing", []))
    resolved_dois = set(doi_result.get("checked", [])) - missing_dois if doi_result.get("available") else set()
    metadata_checked = set(metadata.get("checked", [])) | set(metadata.get("identifier_checked", []))
    source_profiles = {
        str(row.get("identity") or ""): row
        for row in metadata.get("source_profiles", [])
        if isinstance(row, dict)
    }
    failed_identities = (
        set(metadata.get("retracted", []))
        | set(metadata.get("title_mismatches", []))
        | set(metadata.get("identifier_mismatches", []))
    )
    for source in sources:
        identity = source_identity(source) or "unknown"
        doi = str(source.get("doi") or "")
        failed = doi in missing_dois or identity in failed_identities
        checked = doi in resolved_dois or identity in metadata_checked or failed
        profile = source_profiles.get(identity, {})
        source_types = profile.get("publication_types", [])
        detail = (
            "Source is marked as retracted by an authoritative registry."
            if identity in set(metadata.get("retracted", []))
            else "Source identifier or supplied title did not match an authoritative registry."
            if failed
            else "Source identifier resolved in an authoritative registry or repository."
            if checked
            else "The source registry was unavailable or returned no authoritative record."
        )
        findings.append({
            "kind": "citation",
            "status": "fail" if failed else "pass" if checked else "not_checked",
            "source": identity,
            "detail": detail,
            "source_type": source_types[0] if source_types else None,
            "text_scope": profile.get("text_scope"),
        })
    returned_claims = {
        (str(row.get("identity") or ""), str(row.get("text") or ""))
        for row in metadata.get("claim_checks", [])
    }
    for row in metadata.get("claim_checks", []):
        outcome = str(row.get("outcome") or "not_checked")
        status = (
            "pass"
            if outcome == "supported"
            else "fail"
            if outcome in {"contradicted", "unsupported"} and row.get("claimed_quantities")
            else "not_checked"
        )
        findings.append({
            "kind": "claim",
            "status": status,
            "source": row.get("identity"),
            "text": str(row.get("text") or "")[:500],
            "passage": str(row.get("passage") or "")[:700] or None,
            "outcome": outcome,
            "detail": (
                "The cited source contains the same quantities in a context-matched passage."
                if outcome == "supported"
                else "The closest context-matched source passage reports different quantities."
                if outcome == "contradicted"
                else "No passage with the claimed quantities was found in the retrieved source text."
                if outcome == "unsupported"
                else "A related passage was found, but deterministic matching cannot prove semantic support."
                if outcome == "passage_found"
                else "No deterministic supporting passage was found; semantic support was not assessed."
            ),
        })
    for source in sources:
        identity = source_identity(source) or "unknown"
        findings.extend(
            {
                "kind": "claim",
                "status": "not_checked",
                "source": identity,
                "text": value,
                "passage": None,
                "outcome": "not_checked",
                "detail": "No authoritative abstract or open full text was available for this check.",
            }
            for value in source.get("verification_claims", [])
            if (identity, value) not in returned_claims
        )
    for kind, key in (("quotation", "quote_checks"), ("number", "number_checks")):
        returned = {
            (str(row.get("identity") or ""), str(row.get("text") or ""))
            for row in metadata.get(key, [])
        }
        for row in metadata.get(key, []):
            available = bool(row.get("authority_available"))
            findings.append({
                "kind": kind,
                "status": "pass" if available and row.get("matched") else "fail" if available else "not_checked",
                "source": row.get("identity"),
                "text": str(row.get("text") or "")[:500],
                "passage": str(row.get("passage") or "")[:700] or None,
                "outcome": row.get("outcome"),
                "detail": (
                    "Exact text found in retrieved source text."
                    if kind == "quotation" and row.get("matched")
                    else "Exact number and unit found in retrieved source text."
                    if kind == "number" and row.get("matched")
                    else "Retrieved source text does not contain the submitted text."
                    if available
                    else "No authoritative abstract or open full text was available for this check."
                ),
            })
        source_key = "verification_quotes" if kind == "quotation" else "verification_claims"
        for source in sources:
            identity = source_identity(source) or "unknown"
            findings.extend(
                {
                    "kind": kind,
                    "status": "not_checked",
                    "source": identity,
                    "text": value,
                    "detail": "No authoritative abstract or open full text was available for this check.",
                }
                for value in source.get(source_key, [])
                if (identity, value) not in returned
            )
    findings.extend(
        {"kind": "quotation", "status": "not_checked", "source": None, "text": value, "detail": "Quotation could not be mapped to a specific citation."}
        for value in unmapped_quotes
    )
    findings.extend(
        {"kind": "number", "status": "not_checked", "source": None, "text": value, "detail": "Numeric claim could not be mapped to a specific citation."}
        for value in unmapped_numbers
    )
    checks = {
        kind: _check_summary(findings, kind)
        for kind in ("citation", "claim", "number", "quotation")
    }
    has_failures = any(row["status"] == "fail" for row in findings)
    has_unknowns = (
        any(row["status"] == "not_checked" for row in findings)
        or not sources
        or source_total > len(sources)
        or checks_omitted > 0
    )
    return {
        "schema_version": VERIFY_SCHEMA_VERSION,
        "receipt_id": receipt_id,
        "checked_at": (checked_at or datetime.now(timezone.utc)).isoformat(),
        "verifier_release": verifier_release,
        "document": {
            "sha256": f"sha256:{hashlib.sha256(text.encode('utf-8')).hexdigest()}",
            "characters": len(text),
            "sources_found": source_total,
            "sources_checked": len(sources),
            "sources_omitted": max(0, source_total - len(sources)),
            "literal_checks_omitted": checks_omitted,
        },
        "overall_status": "issues_found" if has_failures else "partial" if has_unknowns else "checks_passed",
        "checks": checks,
        "findings": findings,
        "limitations": [
            "This receipt verifies source identity and literal text/number agreement where authoritative text is available.",
            "Exact passage and numeric-conflict checks are deterministic; related passages do not prove semantic entailment.",
            "Paywalled or unavailable source text is reported as not checked, never as passed.",
            "Registry type and retraction checks do not replace study-quality or risk-of-bias appraisal.",
            (
                f"{checks_omitted} additional literal checks were omitted by the "
                f"{max_literal_checks}-check receipt cap."
                if checks_omitted
                else f"Receipts retain at most {max_literal_checks} literal checks."
            ),
        ],
    }


def sign_manifest(manifest: dict[str, Any], secret: str) -> str:
    payload = json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return "hmac-sha256:" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


def manifest_signature_valid(manifest: dict[str, Any], signature: str, secret: str) -> bool:
    return hmac.compare_digest(sign_manifest(manifest, secret), signature)
