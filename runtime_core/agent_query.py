from __future__ import annotations

import re
from datetime import UTC, datetime

from contracts import ObjectType, ResearchObject
from runtime_core.repos import RuntimeRepository


def run_agent_query_job(repo: RuntimeRepository, job_id: str) -> ResearchObject:
    job = repo.get_object(job_id)
    if job is None or job.object_type != ObjectType.AGENT_QUERY:
        raise ValueError("agent_query_job_not_found")

    metadata = dict(job.metadata)
    now = datetime.now(UTC).isoformat()
    metadata["status"] = "running"
    metadata["updated_at"] = now
    repo.update_object_metadata(job.id, metadata)

    query = str(metadata.get("query") or job.title)
    caps = dict(metadata.get("caps") or {})
    max_sources = int(caps.get("max_sources") or 8)
    publications = [
        item
        for item in repo.list_objects(ObjectType.PUBLICATION)
        if str(item.metadata.get("public_visibility") or "hidden").strip().lower() == "listed"
        and not item.metadata.get("superseded_by")
    ]
    matches = _rank_publications(query, publications)[:max_sources]
    generated_at = datetime.now(UTC).isoformat()
    result = _build_result(query, matches, generated_at=generated_at)

    metadata["status"] = "completed"
    metadata["updated_at"] = generated_at
    metadata["result"] = result
    metadata["result_source"] = "researka_public_records"
    updated = repo.update_object_metadata(job.id, metadata)
    if updated is None:
        raise ValueError("agent_query_job_not_found")
    return updated


def fail_agent_query_job(repo: RuntimeRepository, job_id: str, reason: str) -> None:
    job = repo.get_object(job_id)
    if job is None or job.object_type != ObjectType.AGENT_QUERY:
        return
    metadata = dict(job.metadata)
    metadata["status"] = "failed"
    metadata["updated_at"] = datetime.now(UTC).isoformat()
    metadata["error_message"] = reason[:240]
    repo.update_object_metadata(job.id, metadata)


def _rank_publications(query: str, publications: list[ResearchObject]) -> list[ResearchObject]:
    terms = _terms(query)
    scored: list[tuple[int, datetime, ResearchObject]] = []
    for publication in publications:
        haystack = _publication_text(publication)
        score = sum(3 if term in publication.title.lower() else 1 for term in terms if term in haystack)
        if score > 0:
            scored.append((score, publication.created_at, publication))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [publication for _, _, publication in scored]


def _build_result(query: str, matches: list[ResearchObject], *, generated_at: str) -> dict:
    title = f"Researka agent result: {query}"
    if not matches:
        summary = "No matching public Researka records were found for this query yet."
        answer = (
            f"### {title}\n\n"
            "No matching public Researka records were found. Try a narrower topic, a synonym, "
            "or a known Researka publication title."
        )
        return {"title": title, "summary": summary, "answerMarkdown": answer, "citations": [], "generatedAt": generated_at}

    citations = [_citation_for(publication) for publication in matches]
    top_titles = ", ".join(publication.title for publication in matches[:3])
    summary = f"Found {len(matches)} matching public Researka record(s): {top_titles}."
    bullets = "\n".join(f"- {publication.title}" for publication in matches)
    answer = (
        f"### {title}\n\n"
        f"{summary}\n\n"
        "Matching records:\n"
        f"{bullets}\n\n"
        "This result is bounded to stored public Researka records and does not submit, review, or publish anything."
    )
    return {"title": title, "summary": summary, "answerMarkdown": answer, "citations": citations, "generatedAt": generated_at}


def _terms(query: str) -> list[str]:
    return [term for term in re.findall(r"[a-z0-9]+", query.lower()) if len(term) > 2]


def _publication_text(publication: ResearchObject) -> str:
    metadata = " ".join(str(value) for value in publication.metadata.values() if isinstance(value, (str, int, float)))
    return f"{publication.title} {publication.body_markdown} {metadata}".lower()


def _citation_for(publication: ResearchObject) -> dict:
    metadata = publication.metadata
    url = metadata.get("dw_chain_url") or metadata.get("url") or metadata.get("doi")
    citation = {"title": publication.title, "artifactId": publication.id}
    if isinstance(url, str) and url:
        citation["url"] = url
    return citation
