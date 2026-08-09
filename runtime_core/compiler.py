from __future__ import annotations

import hashlib
import json

from contracts import ArticleType, GateResult, PublicationArtifact, PublicationCounts, publication_template_for

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


def canonical_manuscript_body(*, sections: dict[str, str], article_type: str) -> str:
    """Render the only manuscript body that review and publish may use."""
    template = publication_template_for(article_type)
    return "\n\n".join(
        f"## {name}\n\n{text}" for name, text in _ordered_sections(sections, required_sections=template.required_sections)
    ).strip()


def canonical_package_hash(
    *,
    title: str,
    abstract: str,
    sections: dict[str, str],
    source_bundle: list[dict],
    article_type: str,
) -> str:
    package = {
        "abstract": abstract.strip(),
        "article_type": publication_template_for(article_type).article_type,
        "body_markdown": canonical_manuscript_body(sections=sections, article_type=article_type),
        "source_bundle": source_bundle,
        "title": title.strip(),
    }
    encoded = json.dumps(package, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


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
    compiled_body = canonical_manuscript_body(sections=sections, article_type=template.article_type)
    counts = canonical_bundle_facts(source_bundle)
    gates: list[GateResult] = run_publish_gates(
        body_markdown=compiled_body,
        counts=counts,
        core_claims_resolved=core_claims_resolved,
    )
    leakage_failed = any(gate.name == "leakage_blocker" and not gate.passed for gate in gates)
    if not leakage_failed:
        compiled_body, _ = sanitize_publication_body(compiled_body)
        validate_template_structure(compiled_body, template.required_sections)
        if template.article_type == ArticleType.ALPHA_MEMO.value and not compiled_body.strip():
            raise ValueError("structure_gate: alpha memo body empty")
    return PublicationArtifact(
        title=title,
        abstract=abstract.strip(),
        body_markdown=compiled_body,
        counts=counts,
        gates=gates,
    )
