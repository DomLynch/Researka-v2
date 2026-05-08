# Citation JSON-LD 1.0

Status: draft.

## Purpose

Define a compact JSON-LD shape for published Researka objects and their source bundles.

This spec is for export metadata. It does not change runtime submission intake.

## Context

Use Schema.org terms where they fit and keep Researka-specific fields under a stable namespace.

```json
{
  "@context": {
    "schema": "https://schema.org/",
    "researka": "https://researka.org/ns#",
    "dw": "https://provenance.researka.org/ns#",
    "title": "schema:name",
    "abstract": "schema:abstract",
    "datePublished": "schema:datePublished",
    "citation": "schema:citation",
    "identifier": "schema:identifier"
  }
}
```

## Publication Object

```json
{
  "@context": {
    "schema": "https://schema.org/",
    "researka": "https://researka.org/ns#",
    "dw": "https://provenance.researka.org/ns#",
    "title": "schema:name",
    "abstract": "schema:abstract",
    "datePublished": "schema:datePublished",
    "citation": "schema:citation",
    "identifier": "schema:identifier"
  },
  "@type": "schema:ScholarlyArticle",
  "@id": "https://researka.org/publications/example-id",
  "title": "Publication title",
  "abstract": "Publication abstract",
  "datePublished": "2026-05-08",
  "identifier": [
    {"@type": "schema:PropertyValue", "propertyID": "researka:publication_id", "value": "pub-id"},
    {"@type": "schema:PropertyValue", "propertyID": "researka:submission_id", "value": "sub-id"}
  ],
  "researka:article_type": "rapid_evidence_synthesis",
  "researka:verdict_level": "L6",
  "researka:artifact_hash": "sha256:...",
  "researka:counts": {
    "selected_count": 12,
    "review_like_count": 6,
    "primary_like_count": 6,
    "year_start": 2020,
    "year_end": 2026
  },
  "citation": []
}
```

## Citation Object

Each `citation` entry maps to one source-bundle item.

```json
{
  "@type": "schema:ScholarlyArticle",
  "@id": "bundle:1",
  "title": "Source title",
  "identifier": [
    {"@type": "schema:PropertyValue", "propertyID": "doi", "value": "10.0000/example"}
  ],
  "schema:url": "https://example.org/source",
  "schema:datePublished": "2024",
  "researka:evidence_type": "primary",
  "researka:relevance": 0.85,
  "researka:retrieved_at": "2026-05-08T00:00:00Z",
  "researka:source_hash": "sha256:..."
}
```

## Provenance Fields

Optional publication-level fields:

```json
{
  "researka:review_id": "review-id",
  "researka:decision_id": "decision-id",
  "researka:osf_record": "https://osf.io/example/",
  "dw:derivation": "https://provenance.researka.org/example"
}
```

Manual PROSPERO registration is not required. OSF and Derivation Web are the preferred automated provenance path when an export is available.

## Secret Handling

OSF personal access tokens are execution secrets only.

Forbidden in JSON-LD:

- OSF PATs
- API keys
- provider credentials
- raw environment variables
- private reviewer prompts unless explicitly published

## Validation Rules

- JSON must parse.
- `@type` must be present.
- Publication `@id` must be stable.
- Bundle citations must preserve source order.
- DOI values must not be prefixed with `https://doi.org/`.
- `researka:artifact_hash` is required for L7 or L8 claims.

## Non-Goals

- No citation-style rendering requirement.
- No guarantee that every optional field is currently available.
- No requirement to expose unpublished reviews publicly.
