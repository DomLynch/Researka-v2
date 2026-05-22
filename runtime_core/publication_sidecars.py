from __future__ import annotations

import csv
import io
import re
from typing import Any

from contracts import ResearchObject

SIDECAR_NAMES = {
    "claim_graph.json",
    "citation_traces.json",
    "evidence_table.csv",
    "risk_of_bias.json",
    "contradiction_map.json",
}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _source_bundle(submission: ResearchObject | None) -> list[dict[str, Any]]:
    if submission is None:
        return []
    return [item for item in _as_list(submission.metadata.get("source_bundle")) if isinstance(item, dict)]


def _references_from_body(body: str) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []
    for line in body.splitlines():
        if not line.startswith("- **"):
            continue
        title_match = re.search(r"\*\*(.+?)\.\*\*", line)
        doi_match = re.search(r"DOI:\s*([^\s]+)", line)
        pmid_match = re.search(r"PMID:\s*([^\s.]+)", line)
        year_match = re.search(r"\*\*.+?\.\*\*\s*(\d{4})\.", line)
        title = title_match.group(1).strip() if title_match else line[2:].strip()
        doi = doi_match.group(1).rstrip(".,") if doi_match else None
        sources.append(
            {
                "title": title,
                "year": int(year_match.group(1)) if year_match else None,
                "doi": doi,
                "pmid": pmid_match.group(1) if pmid_match else None,
                "url": f"https://doi.org/{doi}" if doi else None,
                "evidence_type": "citation",
            }
        )
    return sources


def _dedupe_sources(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for source in sources:
        key = str(source.get("doi") or source.get("pmid") or source.get("title") or "").lower()
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(source)
    return deduped


def publication_sources(publication: ResearchObject, submission: ResearchObject | None = None) -> list[dict[str, Any]]:
    return _dedupe_sources(_source_bundle(submission) + _references_from_body(publication.body_markdown or ""))


def _claim_candidates(publication: ResearchObject) -> list[str]:
    body = publication.body_markdown or ""
    candidates: list[str] = []
    for line in body.splitlines():
        clean = line.strip(" -*")
        if len(clean) < 80:
            continue
        lowered = clean.lower()
        if any(marker in lowered for marker in ("support", "suggest", "risk", "increase", "decrease", "null", "evidence")):
            candidates.append(clean)
    if not candidates:
        abstract = str(publication.metadata.get("abstract") or "")
        candidates = [part.strip() for part in re.split(r"\n+|(?<=[.!?])\s+", abstract) if len(part.strip()) >= 80]
    return candidates[:30]


def evidence_rows(publication: ResearchObject, submission: ResearchObject | None = None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for source in publication_sources(publication, submission):
        title = str(source.get("title") or "Untitled source")
        evidence_type = str(source.get("evidence_type") or "source").lower()
        rows.append(
            {
                "study": title,
                "year": source.get("year"),
                "doi": source.get("doi"),
                "url": source.get("url"),
                "population": source.get("population") or source.get("cohort") or "not extracted",
                "intervention_or_exposure": source.get("intervention") or source.get("exposure") or "not extracted",
                "comparator": source.get("comparator") or "not extracted",
                "endpoint": source.get("endpoint") or source.get("outcome") or "not extracted",
                "effect": source.get("effect") or source.get("effect_size") or "not extracted",
                "risk_of_bias": source.get("risk_of_bias") or "not appraised in public sidecar",
                "directness": "review-level" if "review" in evidence_type else evidence_type or "source-traceable",
            }
        )
    return rows


def screening_summary(publication: ResearchObject) -> dict[str, Any]:
    raw_counts = publication.metadata.get("counts")
    counts: dict[str, Any] = raw_counts if isinstance(raw_counts, dict) else {}
    body = publication.body_markdown or ""
    parsed_counts = [int(match) for match in re.findall(r"\b(\d+)\s+(?:curated reference papers|records retrieved|included sources)", body, flags=re.I)]
    candidate_count = max(parsed_counts + [int(counts.get("retrieved_count") or 0), int(counts.get("selected_count") or 0), 0])
    retained_count = candidate_count or int(counts.get("selected_count") or 0)
    return {
        "identified": candidate_count,
        "screened": candidate_count,
        "excluded": 0,
        "included_or_retained": retained_count,
        "wording": (
            f"{retained_count} candidate receipts retained after source retrieval, deduplication, and topic filtering. "
            "This is an evidence-map screening trace, not a PRISMA full-text exclusion audit."
        ),
        "exclusion_reasons": [],
    }


def reviewer_limitations(publication: ResearchObject) -> list[str]:
    label = "alpha memo" if str(publication.metadata.get("author_agent_id", "")).lower().find("alpha") >= 0 else "evidence map"
    return [
        f"This is an agent-assisted {label}, not a PRISMA-complete systematic review or clinical guideline.",
        "It is not PROSPERO-registered and should not be read as medical advice.",
        "Public sidecars expose citation traces and extraction status; empty fields mean not extracted, not assumed absent.",
    ]


def sidecar_manifest(publication_id: str) -> list[dict[str, str]]:
    return [
        {"name": name, "url": f"/publications/{publication_id}/sidecars/{name}"}
        for name in sorted(SIDECAR_NAMES)
    ]


def build_sidecar(publication: ResearchObject, submission: ResearchObject | None, sidecar_name: str) -> tuple[Any, str, str]:
    if sidecar_name not in SIDECAR_NAMES:
        raise KeyError(sidecar_name)
    rows = evidence_rows(publication, submission)
    claims = _claim_candidates(publication)
    if sidecar_name == "evidence_table.csv":
        output = io.StringIO()
        fieldnames = [
            "study",
            "year",
            "doi",
            "url",
            "population",
            "intervention_or_exposure",
            "comparator",
            "endpoint",
            "effect",
            "risk_of_bias",
            "directness",
        ]
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
        return output.getvalue(), "text/csv", sidecar_name
    if sidecar_name == "claim_graph.json":
        return {
            "publication_id": publication.id,
            "content_hash": publication.metadata.get("content_hash") or publication.metadata.get("sha256"),
            "nodes": [
                {"id": publication.id, "type": "publication", "title": publication.title},
                *[{"id": f"claim_{index}", "type": "claim", "text": claim} for index, claim in enumerate(claims, start=1)],
                *[{"id": f"source_{index}", "type": "source", **row} for index, row in enumerate(rows, start=1)],
            ],
            "edges": [
                {"from": publication.id, "to": f"claim_{index}", "type": "contains_claim"}
                for index, _ in enumerate(claims, start=1)
            ],
            "screening": screening_summary(publication),
        }, "application/json", sidecar_name
    if sidecar_name == "citation_traces.json":
        first_sources = [{"study": row["study"], "doi": row["doi"], "url": row["url"]} for row in rows[:5]]
        return {
            "publication_id": publication.id,
            "traces": [
                {"claim_id": f"claim_{index}", "claim": claim, "candidate_sources": first_sources}
                for index, claim in enumerate(claims, start=1)
            ],
        }, "application/json", sidecar_name
    if sidecar_name == "risk_of_bias.json":
        return {
            "publication_id": publication.id,
            "method_note": "Risk-of-bias fields are surfaced when supplied by the submitting agent; otherwise marked as not appraised in public sidecar.",
            "sources": [
                {"study": row["study"], "doi": row["doi"], "risk_of_bias": row["risk_of_bias"], "directness": row["directness"]}
                for row in rows
            ],
        }, "application/json", sidecar_name
    return {
        "publication_id": publication.id,
        "screening": screening_summary(publication),
        "limitations": reviewer_limitations(publication),
        "contradictions": [
            claim for claim in claims if any(word in claim.lower() for word in ("however", "yet", "while", "but", "coexist", "mixed"))
        ],
    }, "application/json", sidecar_name
