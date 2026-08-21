from __future__ import annotations

import html
import logging
import os
import re
import socket
import time
import urllib.parse
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from ipaddress import ip_address
from typing import Any

import httpx

from .evidence_quality import quantity_tokens
from .urls import validated_service_url

log = logging.getLogger(__name__)

_ARXIV_ID_RE = re.compile(
    r"(?:\d{4}\.\d{4,5}|[a-z-]+(?:\.[a-z-]+)?/\d{7})(?:v\d+)?",
    re.IGNORECASE,
)


class UnsafeSourceLocator(ValueError):
    pass

# Existence is checked against the doi.org handle system, which is
# registrar-agnostic (Crossref, DataCite, mEDRA, ...). A Crossref-only
# resolver would false-flag real DOIs from other registrars, and the local
# corpus is topic-sliced so it cannot arbitrate global existence.


def _base_url() -> str:
    return validated_service_url(
        os.getenv("RESEARKA_DOI_RESOLVER_URL", "https://doi.org/api/handles"),
        label="doi_resolver",
    )


def validate_resolver_urls() -> None:
    for label, env_name, default in (
        ("pubmed", "RESEARKA_PUBMED_URL", "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"),
        ("pmc", "RESEARKA_PMC_URL", "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"),
        (
            "pmc_id_converter",
            "RESEARKA_PMC_ID_CONVERTER_URL",
            "https://pmc.ncbi.nlm.nih.gov/tools/idconv/api/v1/articles/",
        ),
        ("crossref", "RESEARKA_CROSSREF_URL", "https://api.crossref.org/works"),
        ("openalex", "RESEARKA_OPENALEX_URL", "https://api.openalex.org/works"),
        ("clinicaltrials", "RESEARKA_CLINICALTRIALS_URL", "https://clinicaltrials.gov/api/v2/studies"),
        ("arxiv", "RESEARKA_ARXIV_HTML_URL", "https://arxiv.org/html"),
    ):
        validated_service_url(os.getenv(env_name, default), label=label)
    _base_url()


def _enabled() -> bool:
    return os.getenv("RESEARKA_DOI_CHECK_ENABLED", "1") == "1"


def _timeout_s() -> float:
    try:
        return float(os.getenv("RESEARKA_DOI_CHECK_TIMEOUT_S", "3"))
    except ValueError:
        return 3.0


def _max_dois() -> int:
    try:
        return max(1, int(os.getenv("RESEARKA_DOI_CHECK_MAX", "100")))
    except ValueError:
        return 100


def _max_sources() -> int:
    try:
        return max(1, int(os.getenv("RESEARKA_SOURCE_CHECK_MAX", "100")))
    except ValueError:
        return 100


def _max_verify_open_texts() -> int:
    try:
        return min(_max_sources(), max(1, int(os.getenv("RESEARKA_VERIFY_OPEN_TEXT_MAX", "10"))))
    except ValueError:
        return 10


def _source_enabled() -> bool:
    return os.getenv("RESEARKA_SOURCE_CHECK_ENABLED", os.getenv("RESEARKA_DOI_CHECK_ENABLED", "1")) == "1"


def _unavailable_recommendation() -> str:
    # Mirrors the integrity client: outages never silently pass. The result is
    # always stamped available=False; RESEARKA_DOI_CHECK_FAIL_CLOSED=1
    # additionally holds the submission (revise) instead of proceeding.
    return "revise" if os.getenv("RESEARKA_DOI_CHECK_FAIL_CLOSED", "1") == "1" else "pass"


def _metadata_enabled() -> bool:
    return os.getenv("RESEARKA_SOURCE_METADATA_CHECK_ENABLED", "1") == "1"


def _metadata_unavailable_recommendation() -> str:
    value = os.getenv(
        "RESEARKA_SOURCE_METADATA_FAIL_CLOSED",
        os.getenv("RESEARKA_DOI_CHECK_FAIL_CLOSED", "1"),
    )
    return "revise" if value == "1" else "pass"


def _metadata_attempts() -> int:
    try:
        return max(1, int(os.getenv("RESEARKA_SOURCE_METADATA_MAX_ATTEMPTS", "3")))
    except ValueError:
        return 3


def normalize_arxiv_id(value: object) -> str:
    candidate = urllib.parse.unquote(str(value or "")).strip().removesuffix(".pdf")
    return candidate.lower() if _ARXIV_ID_RE.fullmatch(candidate) else ""


def _arxiv_id(source: dict[str, Any]) -> str:
    if value := normalize_arxiv_id(source.get("arxiv_id")):
        return value
    parsed = urllib.parse.urlparse(str(source.get("url") or "").strip())
    path = urllib.parse.unquote(parsed.path).strip("/")
    if _trusted_host(parsed.hostname or "", "arxiv.org"):
        prefix, _, value = path.partition("/")
        if prefix.lower() in {"abs", "html", "pdf"}:
            return normalize_arxiv_id(value)
    return ""


def _registry_payload(client: httpx.Client, url: str) -> dict[str, Any] | None:
    attempts = _metadata_attempts()
    for attempt in range(1, attempts + 1):
        try:
            response = client.get(url)
            if response.status_code == 404:
                return None
            if response.status_code == 429 or response.status_code >= 500:
                if attempt < attempts:
                    time.sleep(0.5 * attempt)
                    continue
            response.raise_for_status()
            payload = response.json()
            return payload if isinstance(payload, dict) else None
        except httpx.HTTPStatusError:
            return None
        except (httpx.TransportError, OSError, ValueError):
            if attempt == attempts:
                return None
            time.sleep(0.5 * attempt)
    return None


_GENERIC_WORDS = {
    "about", "analysis", "article", "evidence", "effects", "results", "review", "study", "trial",
}


def _tokens(value: object) -> set[str]:
    clean = html.unescape(re.sub(r"<[^>]+>", " ", str(value or ""))).lower()
    return {
        word for word in re.findall(r"[a-z0-9]+", clean)
        if len(word) >= 4 and word not in _GENERIC_WORDS
    }


def _text_matches(left: object, right: object, *, floor: float) -> bool:
    left_tokens, right_tokens = _tokens(left), _tokens(right)
    if not left_tokens or not right_tokens:
        return False
    overlap = len(left_tokens & right_tokens)
    minimum_overlap = 1 if min(len(left_tokens), len(right_tokens)) == 1 else 2
    return overlap >= minimum_overlap and overlap / min(len(left_tokens), len(right_tokens)) >= floor


def _normalized_text(value: object) -> str:
    text = html.unescape(re.sub(r"<[^>]+>", " ", str(value or ""))).lower()
    return " ".join(re.findall(r"[a-z0-9]+", text))


def _openalex_abstract(payload: dict[str, Any]) -> str:
    index = payload.get("abstract_inverted_index")
    if not isinstance(index, dict):
        return ""
    positioned = [
        (position, str(word))
        for word, positions in index.items()
        if isinstance(positions, list)
        for position in positions
        if isinstance(position, int)
    ]
    return " ".join(word for _, word in sorted(positioned))


def _pmc_id_map(client: httpx.Client, sources: list[dict[str, Any]]) -> dict[str, str]:
    base = validated_service_url(
        os.getenv(
            "RESEARKA_PMC_ID_CONVERTER_URL",
            "https://pmc.ncbi.nlm.nih.gov/tools/idconv/api/v1/articles/",
        ),
        label="pmc_id_converter",
    )
    resolved: dict[str, str] = {}
    for field, id_type in (("doi", "doi"), ("pmid", "pmid")):
        identities: dict[str, str] = {}
        for source in sources:
            identity_row = _source_identity(source)
            identifier = str(source.get(field) or "").strip().lower()
            if identity_row and identifier:
                identities[identifier] = identity_row[0]
        identifiers = sorted(identities)
        for start in range(0, len(identifiers), 200):
            query = {
                "ids": ",".join(identifiers[start : start + 200]),
                "idtype": id_type,
                "format": "json",
                "tool": "researka",
            }
            if email := os.getenv("RESEARKA_NCBI_EMAIL", os.getenv("RESEARKA_CROSSREF_MAILTO", "")):
                query["email"] = email
            payload = _registry_payload(client, f"{base}?{urllib.parse.urlencode(query)}") or {}
            for record in payload.get("records", []):
                if not isinstance(record, dict):
                    continue
                identifier = str(record.get(field) or "").strip().lower()
                pmcid = str(record.get("pmcid") or "").strip().upper()
                if source_key := identities.get(identifier):
                    if re.fullmatch(r"PMC\d+", pmcid):
                        resolved[source_key] = pmcid
    return resolved


def _pmc_full_texts(client: httpx.Client, sources: list[dict[str, Any]]) -> dict[str, str]:
    requested: dict[str, tuple[str, dict[str, Any]]] = {}
    for source in sources:
        match = re.search(r"PMC\d+", str(source.get("source_record_locator") or ""), re.I)
        source_identity_tuple = _source_identity(source)
        if match and source_identity_tuple and str(source.get("evidence_origin") or "").strip().lower() == "full_text":
            requested[match.group().upper()] = (source_identity_tuple[0], source)
    by_identity: dict[str, dict[str, Any]] = {}
    for source in sources:
        identity_row = _source_identity(source)
        if identity_row:
            by_identity[identity_row[0]] = source
    verify_sources = [
        source
        for source in sources
        if source.get("verification_claims") or source.get("verification_quotes")
    ]
    if verify_sources:
        for source_key, pmcid in _pmc_id_map(client, verify_sources).items():
            requested.setdefault(pmcid, (source_key, by_identity[source_key]))
    if not requested:
        return {}
    base = validated_service_url(
        os.getenv("RESEARKA_PMC_URL", "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"),
        label="pmc",
    )
    verified: dict[str, str] = {}
    pmc_ids = sorted(requested)
    for start in range(0, len(pmc_ids), 15):
        query = {
            "db": "pmc",
            "id": ",".join(pmc_ids[start : start + 15]),
            "retmode": "xml",
            "tool": "researka",
        }
        if email := os.getenv("RESEARKA_NCBI_EMAIL", os.getenv("RESEARKA_CROSSREF_MAILTO", "")):
            query["email"] = email
        url = f"{base}?{urllib.parse.urlencode(query)}"
        try:
            response = client.get(url)
            response.raise_for_status()
            root = ET.fromstring(response.text)
            articles = [root] if root.tag.rsplit("}", 1)[-1] == "article" else root.findall(".//article")
        except (httpx.HTTPError, OSError, ValueError, ET.ParseError):
            continue
        for article in articles:
            ids = {
                str(node.get("pub-id-type") or "").lower(): "".join(node.itertext()).strip().lower()
                for node in article.findall(".//article-id")
            }
            record = requested.get(ids.get("pmcid", "").upper())
            if not record:
                continue
            source_key, source = record
            doi, pmid = (str(source.get(key) or "").strip().lower() for key in ("doi", "pmid"))
            if not (doi and ids.get("doi") == doi or not doi and pmid and ids.get("pmid") == pmid):
                continue
            verified[source_key] = " ".join(
                " ".join(node.itertext())
                for node in article
                if node.tag.rsplit("}", 1)[-1] in {"front", "body"}
            )
    return verified


def _arxiv_full_texts(client: httpx.Client, sources: list[dict[str, Any]]) -> dict[str, str]:
    requested = {
        arxiv_id: identity_row[0]
        for source in sources
        if (source.get("verification_claims") or source.get("verification_quotes"))
        and (arxiv_id := _arxiv_id(source))
        and (identity_row := _source_identity(source))
    }
    if not requested:
        return {}
    base = validated_service_url(
        os.getenv("RESEARKA_ARXIV_HTML_URL", "https://arxiv.org/html"),
        label="arxiv",
    )
    try:
        max_bytes = min(
            10_000_000,
            max(100_000, int(os.getenv("RESEARKA_VERIFY_SOURCE_MAX_BYTES", "5000000"))),
        )
    except ValueError:
        max_bytes = 5_000_000
    verified: dict[str, str] = {}
    for arxiv_id, source_key in list(requested.items())[:_max_verify_open_texts()]:
        url = f"{base}/{urllib.parse.quote(arxiv_id, safe='/')}"
        for attempt in range(1, _metadata_attempts() + 1):
            try:
                response = client.get(url)
                if response.status_code == 404:
                    break
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt < _metadata_attempts():
                        time.sleep(0.5 * attempt)
                        continue
                response.raise_for_status()
                final_host = urllib.parse.urlparse(str(getattr(response, "url", url))).hostname or ""
                content = response.text.encode(response.encoding or "utf-8", errors="replace")
                if not _trusted_host(final_host, "arxiv.org") or len(content) > max_bytes:
                    break
                article = re.search(r"<article\b.*?</article>", response.text, re.IGNORECASE | re.DOTALL)
                if not article:
                    break
                body = article.group(0)
                body = re.split(
                    r"<section\b[^>]*class=[\"'][^\"']*\bltx_bibliography\b",
                    body,
                    maxsplit=1,
                    flags=re.IGNORECASE,
                )[0]
                body = re.sub(
                    r"<(?:script|style|nav|footer)\b.*?</(?:script|style|nav|footer)>",
                    " ",
                    body,
                    flags=re.IGNORECASE | re.DOTALL,
                )
                if len(text := _plain_text(body)) >= 100:
                    verified[source_key] = text
                break
            except (httpx.HTTPError, OSError, UnicodeError, ValueError):
                if attempt == _metadata_attempts():
                    break
                time.sleep(0.5 * attempt)
    return verified


def _plain_text(value: object) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", str(value or ""))).split())


def _passages(authority_texts: list[str]) -> list[str]:
    return [
        passage.strip()
        for authority in authority_texts
        for passage in re.split(r"\n+|(?<=[.!?])\s+", _plain_text(authority))
        if 20 <= len(passage.strip()) <= 1_500
    ]


def _literal_passage(authority_texts: list[str], value: str) -> str:
    needle = " ".join(value.split())
    for authority in authority_texts:
        text = _plain_text(authority)
        start = text.casefold().find(needle.casefold())
        if start >= 0:
            return text[max(0, start - 120) : min(len(text), start + len(needle) + 120)]
    return ""


def _claim_evidence(authority_texts: list[str], claim: str) -> dict[str, Any]:
    claim_quantities = quantity_tokens(claim)
    claim_terms = _tokens(claim)
    ranked: list[tuple[float, str, set[tuple[str, str]]]] = []
    for passage in _passages(authority_texts):
        passage_terms = _tokens(passage)
        overlap = len(claim_terms & passage_terms)
        score = overlap / max(1, min(len(claim_terms), len(passage_terms)))
        required_overlap = 1 if len(claim_terms) <= 2 else 2
        ranked.append((score if overlap >= required_overlap else 0.0, passage, quantity_tokens(passage)))
    ranked.sort(key=lambda row: (row[0], len(row[1])), reverse=True)
    exact = next(
        (
            row
            for row in ranked
            if row[0] >= 0.5 and claim_quantities and claim_quantities <= row[2]
        ),
        None,
    )
    if exact:
        outcome, passage, passage_quantities = "supported", exact[1], exact[2]
    elif ranked and ranked[0][0] >= 0.5:
        _, passage, passage_quantities = ranked[0]
        outcome = (
            "contradicted"
            if claim_quantities and passage_quantities and claim_quantities.isdisjoint(passage_quantities)
            else "unsupported"
            if claim_quantities
            else "passage_found"
        )
    else:
        outcome, passage, passage_quantities = "unsupported" if claim_quantities else "unresolved", "", set()
    return {
        "outcome": outcome,
        "passage": passage[:700],
        "claimed_quantities": [list(value) for value in sorted(claim_quantities)],
        "passage_quantities": [list(value) for value in sorted(passage_quantities)],
    }


def _crossref_retracted(payload: dict[str, Any]) -> bool:
    relation = payload.get("relation")
    if isinstance(relation, dict) and any("retract" in str(key).lower() for key in relation):
        return True
    updates = payload.get("update-to")
    if isinstance(updates, list) and any("retract" in str(item.get("type", "")).lower() for item in updates if isinstance(item, dict)):
        return True
    titles = payload.get("title")
    title = titles[0] if isinstance(titles, list) and titles else titles
    return any(
        word.startswith("retract")
        for word in html.unescape(re.sub(r"<[^>]+>", " ", str(title or ""))).lower().split()[:2]
    )


def _source_identity(source: dict[str, Any]) -> tuple[str, str | None, str | None] | None:
    if doi := str(source.get("doi") or "").strip().lower():
        return f"doi:{doi}", doi, f"https://doi.org/{urllib.parse.quote(doi, safe='')}"
    if pmid := str(source.get("pmid") or "").strip():
        return f"pmid:{pmid}", None, f"pmid:{urllib.parse.quote(pmid, safe='')}"
    if openalex := str(source.get("openalex_id") or "").strip():
        work_id = openalex.rstrip("/").rsplit("/", 1)[-1]
        return f"openalex:{work_id.lower()}", None, urllib.parse.quote(work_id, safe="")
    if arxiv_id := _arxiv_id(source):
        return f"arxiv:{arxiv_id}", None, None
    url = str(source.get("url") or "").strip()
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname or ""
    path = urllib.parse.unquote(parsed.path).strip("/")
    if _trusted_host(host, "doi.org") and re.match(r"^10\.\d{4,}/\S+$", path):
        return f"doi:{path.lower()}", path.lower(), f"https://doi.org/{urllib.parse.quote(path, safe='')}"
    if _trusted_host(host, "pubmed.ncbi.nlm.nih.gov") and path.isdigit():
        return f"pmid:{path}", None, f"pmid:{path}"
    if _trusted_host(host, "openalex.org") and re.fullmatch(r"W\d+", path, re.I):
        return f"openalex:{path.lower()}", None, urllib.parse.quote(path, safe="")
    if registry := str(source.get("registry_id") or "").strip():
        return f"registry:{registry.lower()}", None, None
    if url:
        return f"url:{url.lower().rstrip('/')}", None, None
    return None


def source_identity(source: dict[str, Any]) -> str | None:
    identity = _source_identity(source)
    return identity[0] if identity else None


def _source_aliases(source: dict[str, Any]) -> set[str]:
    aliases: set[str] = set()
    for key, prefix in (("doi", "doi"), ("pmid", "pmid"), ("registry_id", "registry")):
        if value := str(source.get(key) or "").strip().lower():
            aliases.add(f"{prefix}:{value}")
    if value := str(source.get("openalex_id") or "").strip().lower():
        aliases.add(f"openalex:{value.rstrip('/').rsplit('/', 1)[-1]}")
    if value := _arxiv_id(source):
        aliases.add(f"arxiv:{value}")
    if identity := _source_identity(source):
        aliases.add(identity[0])
    return aliases


def _trusted_host(host: str, expected: str) -> bool:
    normalized = host.strip().lower().rstrip(".")
    return normalized == expected or normalized.endswith(f".{expected}")


def _pubmed_identifier_checks(
    client: httpx.Client,
    sources: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    rows = [(index, source, str(source.get("pmid") or "").strip()) for index, source in enumerate(sources)]
    rows = [(index, source, pmid) for index, source, pmid in rows if pmid]
    if not rows:
        return []

    valid_pmids = sorted({pmid for _, _, pmid in rows if pmid.isdigit()})
    records: dict[str, Any] = {}
    if valid_pmids:
        query = {
            "db": "pubmed",
            "id": ",".join(valid_pmids),
            "retmode": "json",
            "tool": "researka",
        }
        if email := os.getenv("RESEARKA_NCBI_EMAIL", os.getenv("RESEARKA_CROSSREF_MAILTO", "")):
            query["email"] = email
        base = validated_service_url(
            os.getenv(
                "RESEARKA_PUBMED_URL",
                "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi",
            ),
            label="pubmed",
        )
        payload = _registry_payload(client, f"{base}?{urllib.parse.urlencode(query)}")
        result = payload.get("result") if payload else None
        records = result if isinstance(result, dict) else {}

    checks: list[dict[str, Any]] = []
    for source_index, source, pmid in rows:
        identity = f"pmid:{pmid}"
        record = records.get(pmid) if pmid.isdigit() else None
        if not isinstance(record, dict) or not str(record.get("title") or "").strip():
            checks.append({
                "identity": identity,
                "source_index": source_index,
                "registered_dois": [],
                "checked": False,
                "mismatch": not pmid.isdigit(),
            })
            continue
        articleids = record.get("articleids")
        articleids = articleids if isinstance(articleids, list) else []
        registered_dois = {
            str(item.get("value") or "").strip().lower()
            for item in articleids
            if isinstance(item, dict) and str(item.get("idtype") or "").lower() == "doi"
        }
        submitted_doi = str(source.get("doi") or "").strip().lower()
        checks.append({
            "identity": identity,
            "source_index": source_index,
            "registered_dois": sorted(registered_dois),
            "checked": True,
            "mismatch": (
                bool(str(source.get("title") or "").strip())
                and not _text_matches(source.get("title"), record.get("title"), floor=0.6)
                or bool(submitted_doi and registered_dois and submitted_doi not in registered_dois)
            ),
        })
    return checks


def _canonical_duplicate_indices(
    sources: list[dict[str, Any]], identifier_checks: list[dict[str, Any]]
) -> list[int]:
    aliases = [_source_aliases(source) for source in sources]
    for row in identifier_checks:
        index = row.get("source_index")
        if not isinstance(index, int) or not 0 <= index < len(aliases):
            continue
        aliases[index].update(f"doi:{doi}" for doi in row.get("registered_dois", []) if doi)
    seen: set[str] = set()
    duplicates: list[int] = []
    for index, values in enumerate(aliases):
        if values & seen:
            duplicates.append(index)
        seen.update(values)
    return duplicates


def verify_source_metadata(sources: list[dict[str, Any]], *, parallel: bool = False) -> dict[str, Any] | None:
    """Verify registered source identity, evidence text, and retraction state."""
    if not _metadata_enabled():
        return None
    candidates = [(source, identity) for source in sources if (identity := _source_identity(source))]
    if not candidates:
        return None
    if len(candidates) > _max_sources():
        return {
            "verification_version": 2,
            "available": False,
            "recommendation": "revise",
            "reason": f"source_metadata_capacity_exceeded:{len(candidates)}>{_max_sources()}",
            "checked": [],
            "unverified": [identity[0] for _, identity in candidates],
        }

    crossref_base = validated_service_url(
        os.getenv("RESEARKA_CROSSREF_URL", "https://api.crossref.org/works"),
        label="crossref",
    )
    openalex_base = validated_service_url(
        os.getenv("RESEARKA_OPENALEX_URL", "https://api.openalex.org/works"),
        label="openalex",
    )
    clinicaltrials_base = validated_service_url(
        os.getenv("RESEARKA_CLINICALTRIALS_URL", "https://clinicaltrials.gov/api/v2/studies"),
        label="clinicaltrials",
    )

    def check(client: httpx.Client, item: tuple[dict[str, Any], tuple[str, str | None, str | None]]) -> dict[str, Any]:
        source, (identity, doi, openalex_id) = item
        titles: list[str] = []
        abstracts: list[str] = []
        publication_types: set[str] = set()
        retracted = False
        authority_count = 0
        if doi:
            url = f"{crossref_base}/{urllib.parse.quote(doi, safe='')}"
            if mailto := os.getenv("RESEARKA_CROSSREF_MAILTO"):
                url += "?" + urllib.parse.urlencode({"mailto": mailto})
            payload = _registry_payload(client, url)
            message = payload.get("message", {}) if payload else {}
            if isinstance(message, dict) and message:
                authority_count += 1
                raw_titles = message.get("title")
                if isinstance(raw_titles, list):
                    titles.extend(str(value) for value in raw_titles if value)
                if message.get("abstract"):
                    abstracts.append(str(message["abstract"]))
                if message.get("type"):
                    publication_types.add(str(message["type"]).strip().lower())
                retracted = retracted or _crossref_retracted(message)
        if openalex_id and (not authority_count or not abstracts):
            payload = _registry_payload(client, f"{openalex_base}/{openalex_id}")
            if payload:
                authority_count += 1
                if payload.get("title") or payload.get("display_name"):
                    titles.append(str(payload.get("title") or payload.get("display_name")))
                if abstract := _openalex_abstract(payload):
                    abstracts.append(abstract)
                if payload.get("type"):
                    publication_types.add(str(payload["type"]).strip().lower())
                retracted = retracted or bool(payload.get("is_retracted"))
        registry_id = str(source.get("registry_id") or "").strip().upper()
        if re.fullmatch(r"NCT\d{8}", registry_id):
            payload = _registry_payload(
                client,
                f"{clinicaltrials_base}/{urllib.parse.quote(registry_id, safe='')}",
            ) or {}
            protocol = payload.get("protocolSection")
            protocol = protocol if isinstance(protocol, dict) else {}
            identification = protocol.get("identificationModule")
            identification = identification if isinstance(identification, dict) else {}
            if str(identification.get("nctId") or "").upper() == registry_id:
                authority_count += 1
                titles.extend(
                    str(identification[key])
                    for key in ("briefTitle", "officialTitle")
                    if identification.get(key)
                )
                description = protocol.get("descriptionModule")
                if isinstance(description, dict) and description.get("briefSummary"):
                    abstracts.append(str(description["briefSummary"]))
                publication_types.add("clinical_trial")
        evidence = [
            str(source.get(key) or "").strip()
            for key in ("quote", "evidence_span", "excerpt")
            if str(source.get(key) or "").strip()
        ]
        verification_quotes = [
            str(value).strip()[:500]
            for value in source.get("verification_quotes", [])
            if str(value).strip()
        ]
        verification_claims = [
            str(value).strip()[:500]
            for value in source.get("verification_claims", [])
            if str(value).strip()
        ]
        full_text = full_texts.get(identity, "")
        if identity in arxiv_full_texts:
            publication_types.add("preprint")
        authority_texts = [full_text] if full_text else abstracts
        evidence_text_available = bool(evidence and authority_texts)
        evidence_text_verified = evidence_text_available and any(
            len(normalized) >= 20 and normalized in _normalized_text(authority)
            for value in evidence
            if (normalized := _normalized_text(value))
            for authority in authority_texts
        )
        evidence_mismatch = bool(evidence and authority_texts) and not any(
            _text_matches(value, authority, floor=0.35)
            for value in evidence for authority in authority_texts
        )
        quote_checks = [
            {
                "identity": identity,
                "text": value,
                "authority_available": bool(authority_texts),
                "matched": bool(authority_texts) and _normalized_text(value) in " ".join(
                    _normalized_text(authority) for authority in authority_texts
                ),
                "passage": _literal_passage(authority_texts, value),
            }
            for value in verification_quotes
        ]
        claim_checks = []
        for value in verification_claims:
            evidence_check = _claim_evidence(authority_texts, value) if authority_texts else {
                "outcome": "not_checked",
                "passage": "",
                "claimed_quantities": [list(token) for token in sorted(quantity_tokens(value))],
                "passage_quantities": [],
            }
            claim_checks.append({
                "identity": identity,
                "text": value,
                "authority_available": bool(authority_texts),
                **evidence_check,
            })
        claims_by_text = {row["text"]: row for row in claim_checks}
        number_checks = [
            {
                "identity": identity,
                "text": value,
                "authority_available": bool(authority_texts),
                "matched": claims_by_text[value]["outcome"] == "supported",
                "outcome": claims_by_text[value]["outcome"],
                "passage": claims_by_text[value]["passage"],
                "claimed_quantities": claims_by_text[value]["claimed_quantities"],
                "passage_quantities": claims_by_text[value]["passage_quantities"],
            }
            for value in verification_claims
            if quantity_tokens(value)
        ]
        return {
            "identity": identity,
            "checked": authority_count > 0 or bool(full_text),
            "retracted": retracted,
            "title_mismatch": bool(str(source.get("title") or "").strip()) and bool(titles) and not any(
                _text_matches(source.get("title"), title, floor=0.6) for title in titles
            ),
            "evidence_mismatch": evidence_mismatch,
            "evidence_authority_unavailable": bool(evidence) and not authority_texts,
            "evidence_text_submitted": bool(evidence),
            "evidence_text_available": evidence_text_available,
            "evidence_text_verified": evidence_text_verified,
            "source_profile": {
                "identity": identity,
                "publication_types": sorted(publication_types),
                "retracted": retracted,
                "text_scope": (
                    full_text_scopes.get(identity, "registry_abstract" if abstracts else "unavailable")
                ),
            },
            "quote_checks": quote_checks,
            "number_checks": number_checks,
            "claim_checks": claim_checks,
        }

    try:
        with httpx.Client(
            timeout=_timeout_s(),
            follow_redirects=True,
            headers={"User-Agent": "Researka/1.0 (https://researka.org)"},
            event_hooks={"request": [_require_public_source]},
        ) as client:
            candidate_sources = [source for source, _ in candidates]
            pmc_full_texts = _pmc_full_texts(client, candidate_sources)
            arxiv_full_texts = _arxiv_full_texts(client, candidate_sources)
            full_texts = {**arxiv_full_texts, **pmc_full_texts}
            full_text_scopes = {
                **{identity: "arxiv_full_text" for identity in arxiv_full_texts},
                **{identity: "pmc_full_text" for identity in pmc_full_texts},
            }
            if parallel and len(candidates) > 1:
                with ThreadPoolExecutor(max_workers=min(8, len(candidates))) as pool:
                    results = list(pool.map(lambda item: check(client, item), candidates))
            else:
                results = [check(client, item) for item in candidates]
            identifier_results = _pubmed_identifier_checks(
                client,
                [source for source, _ in candidates],
            )
    except Exception as exc:
        log.warning("source_metadata_unavailable", extra={"error": str(exc)})
        results = [{"identity": identity[0], "checked": False} for _, identity in candidates]
        identifier_results = [
            {
                "identity": f"pmid:{pmid}",
                "source_index": index,
                "registered_dois": [],
                "checked": False,
                "mismatch": False,
            }
            for index, source in enumerate(sources)
            if (pmid := str(source.get("pmid") or "").strip())
        ]

    checked = [row["identity"] for row in results if row.get("checked")]
    unverified = [row["identity"] for row in results if not row.get("checked")]
    retracted = [row["identity"] for row in results if row.get("retracted")]
    title_mismatches = [row["identity"] for row in results if row.get("title_mismatch")]
    evidence_mismatches = [row["identity"] for row in results if row.get("evidence_mismatch")]
    evidence_text_verified = [row["identity"] for row in results if row.get("evidence_text_verified")]
    evidence_text_unverified = [
        row["identity"]
        for row in results
        if row.get("evidence_text_submitted") and not row.get("evidence_text_verified")
    ]
    evidence_authority_unavailable = [
        row["identity"] for row in results if row.get("evidence_authority_unavailable")
    ]
    identifier_checked = sorted({row["identity"] for row in identifier_results if row.get("checked")})
    identifier_mismatches = sorted({row["identity"] for row in identifier_results if row.get("mismatch")})
    identifier_unverified = sorted({
        row["identity"]
        for row in identifier_results
        if not row.get("checked") and not row.get("mismatch")
    })
    canonical_duplicate_indices = _canonical_duplicate_indices(sources, identifier_results)
    blocked = retracted or title_mismatches or identifier_mismatches
    uncertain = evidence_mismatches or evidence_authority_unavailable or unverified or identifier_unverified or canonical_duplicate_indices
    recommendation = "reject" if blocked else _metadata_unavailable_recommendation() if uncertain else "pass"
    return {
        "verification_version": 2,
        "available": not unverified and not identifier_unverified,
        "recommendation": recommendation,
        "checked": checked,
        "unverified": unverified,
        "retracted": retracted,
        "title_mismatches": title_mismatches,
        "evidence_mismatches": evidence_mismatches,
        "evidence_text_verified": evidence_text_verified,
        "evidence_text_unverified": evidence_text_unverified,
        "evidence_authority_unavailable": evidence_authority_unavailable,
        "identifier_checked": identifier_checked,
        "identifier_unverified": identifier_unverified,
        "identifier_mismatches": identifier_mismatches,
        "canonical_duplicate_indices": canonical_duplicate_indices,
        "source_profiles": [row["source_profile"] for row in results if row.get("source_profile")],
        "quote_checks": [check for row in results for check in row.get("quote_checks", [])],
        "number_checks": [check for row in results for check in row.get("number_checks", [])],
        "claim_checks": [check for row in results for check in row.get("claim_checks", [])],
    }


def resolve_dois(dois: list[str], *, parallel: bool = False) -> dict[str, Any] | None:
    """Check that every DOI is registered in the global handle system.

    Returns None when disabled or nothing to check; otherwise a stamped result:
    {available, recommendation, checked, missing[, reason]}.
    """
    if not _enabled() or not dois:
        return None
    if len(dois) > _max_dois():
        return {
            "available": False,
            "recommendation": "revise",
            "reason": f"doi_check_capacity_exceeded:{len(dois)}>{_max_dois()}",
            "checked": [],
            "missing": [],
        }
    checked: list[str] = []
    missing: list[str] = []
    def check(client: httpx.Client, doi: str) -> tuple[str, bool]:
        response = client.get(f"{_base_url()}/{urllib.parse.quote(doi, safe='/')}")
        if response.status_code == 404:
            return doi, False
        response.raise_for_status()
        return doi, True

    try:
        with httpx.Client(timeout=_timeout_s(), follow_redirects=True) as client:
            if parallel and len(dois) > 1:
                with ThreadPoolExecutor(max_workers=min(8, len(dois))) as pool:
                    results = list(pool.map(lambda doi: check(client, doi), dois))
                checked = [doi for doi, _ in results]
                missing = [doi for doi, exists in results if not exists]
            else:
                for doi in dois:
                    checked_doi, exists = check(client, doi)
                    checked.append(checked_doi)
                    if not exists:
                        missing.append(checked_doi)
    except Exception as exc:
        log.warning("doi_resolution_unavailable", extra={"error": str(exc)})
        return {
            "available": False,
            "recommendation": _unavailable_recommendation(),
            "reason": f"doi_resolver_unavailable: {exc}",
            "checked": checked,
            "missing": missing,
        }
    return {
        "available": True,
        "recommendation": "reject" if missing else "pass",
        "checked": checked,
        "missing": missing,
    }


def _non_doi_locator(source: dict[str, Any]) -> str | None:
    if source.get("doi"):
        return None
    if pmid := str(source.get("pmid") or "").strip():
        return f"https://pubmed.ncbi.nlm.nih.gov/{urllib.parse.quote(pmid, safe='')}/"
    if openalex := str(source.get("openalex_id") or "").strip():
        return openalex if openalex.startswith("http") else f"https://openalex.org/{urllib.parse.quote(openalex, safe='')}"
    if registry := str(source.get("registry_id") or "").strip():
        return str(source.get("url") or "").strip() or (
            "https://trialsearch.who.int/Trial2.aspx?TrialID=" + urllib.parse.quote(registry, safe="")
        )
    url = str(source.get("url") or "").strip()
    return url or None


def _require_public_source(request: httpx.Request) -> None:
    host = request.url.host
    port = request.url.port or (443 if request.url.scheme == "https" else 80)
    if request.url.scheme not in {"http", "https"} or not host or port not in {80, 443}:
        raise UnsafeSourceLocator("source locator must use public HTTP(S)")
    addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if not addresses or any(not ip_address(address[4][0]).is_global for address in addresses):
        raise UnsafeSourceLocator("source locator resolves to a non-public address")


def resolve_source_locators(sources: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Resolve every non-DOI source locator without serially blocking intake."""
    if not _source_enabled():
        return None
    locators = list(dict.fromkeys(filter(None, (_non_doi_locator(source) for source in sources))))
    if not locators:
        return None
    if len(locators) > _max_sources():
        return {
            "available": False,
            "recommendation": "revise",
            "reason": f"source_check_capacity_exceeded:{len(locators)}>{_max_sources()}",
            "checked": [],
            "missing": [],
        }

    def check(client: httpx.Client, locator: str) -> tuple[str, str]:
        try:
            with client.stream("GET", locator, headers={"Range": "bytes=0-0"}) as response:
                status = response.status_code
        except UnsafeSourceLocator:
            return locator, "missing"
        except Exception:
            return locator, "unavailable"
        if status in {404, 410}:
            return locator, "missing"
        if status == 429 or status >= 500:
            return locator, "unavailable"
        return locator, "ok"

    try:
        with httpx.Client(
            timeout=_timeout_s(),
            follow_redirects=True,
            event_hooks={"request": [_require_public_source]},
        ) as client:
            with ThreadPoolExecutor(max_workers=min(8, len(locators))) as pool:
                results = list(pool.map(lambda locator: check(client, locator), locators))
    except Exception as exc:
        log.warning("source_resolution_unavailable", extra={"error": str(exc)})
        return {
            "available": False,
            "recommendation": _unavailable_recommendation(),
            "reason": f"source_resolver_unavailable: {exc}",
            "checked": [],
            "missing": [],
        }
    missing = [locator for locator, status in results if status == "missing"]
    unavailable = [locator for locator, status in results if status == "unavailable"]
    return {
        "available": not unavailable,
        "recommendation": _unavailable_recommendation() if unavailable else "reject" if missing else "pass",
        "checked": [locator for locator, status in results if status != "unavailable"],
        "missing": missing,
        **({"reason": f"source_resolver_unavailable:{len(unavailable)}"} if unavailable else {}),
    }
