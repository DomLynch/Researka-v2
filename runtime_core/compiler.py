from __future__ import annotations

from contracts import GateResult, PublicationArtifact, PublicationCounts, publication_template_for

from .gates import run_publish_gates
from .sanitizer import sanitize_publication_body, validate_template_structure


def _ordered_sections(sections: dict[str, str], *, required_sections: tuple[str, ...]) -> list[tuple[str, str]]:
    cleaned = {
        name.strip(): str(text or "").strip()
        for name, text in sections.items()
        if str(name or "").strip() and str(text or "").strip()
    }
    ordered: list[tuple[str, str]] = []
    for heading in required_sections:
        if heading in cleaned:
            ordered.append((heading, cleaned.pop(heading)))
    for name in sorted(cleaned):
        if name.lower() == "abstract":
            continue
        ordered.append((name, cleaned[name]))
    return ordered


def canonical_bundle_facts(source_bundle: list[dict]) -> PublicationCounts:
    selected_count = len(source_bundle)
    review_like_count = sum(1 for item in source_bundle if item.get("evidence_type") == "review")
    primary_like_count = sum(1 for item in source_bundle if item.get("evidence_type") == "primary")
    years = [item["year"] for item in source_bundle if isinstance(item.get("year"), int)]
    return PublicationCounts(
        retrieved_count=selected_count,
        selected_count=selected_count,
        review_like_count=review_like_count,
        primary_like_count=primary_like_count,
        year_start=min(years) if years else None,
        year_end=max(years) if years else None,
    )


def compile_publication(
    *,
    title: str,
    abstract: str,
    sections: dict[str, str],
    source_bundle: list[dict],
    article_type: str = "rapid_evidence_synthesis",
    core_claims_resolved: bool = True,
) -> PublicationArtifact:
    template = publication_template_for(article_type)
    ordered_sections = []
    for name, text in _ordered_sections(sections, required_sections=template.required_sections):
        ordered_sections.append(f"## {name}\n\n{text.strip()}".strip())
    body_markdown = "\n\n".join(ordered_sections).strip()
    body_markdown, _ = sanitize_publication_body(body_markdown)
    validate_template_structure(body_markdown, template.required_sections)
    counts = canonical_bundle_facts(source_bundle)
    gates: list[GateResult] = run_publish_gates(
        body_markdown=body_markdown,
        counts=counts,
        core_claims_resolved=core_claims_resolved,
    )
    return PublicationArtifact(
        title=title,
        abstract=abstract.strip(),
        body_markdown=body_markdown,
        counts=counts,
        gates=gates,
    )
