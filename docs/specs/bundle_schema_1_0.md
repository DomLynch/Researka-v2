# Bundle Schema 1.0

Status: draft.

## Purpose

Define the portable source-bundle format used by Researka submissions, review, publication compilation, and future provenance export.

This spec documents the current minimal runtime contract and names optional fields for open-infrastructure interoperability.

## Current Runtime Contract

`source_bundle` is an ordered array on the submission payload.

Current deterministic intake requires each entry to include:

- `title`
- `evidence_type`

Current accepted `evidence_type` values:

- `primary`
- `review`

Current rapid evidence synthesis intake also expects:

- at least 12 bundle entries
- at least 50% of entries from year 2020 or later when years are supplied
- claims bounded to the bundle
- no reviewer notes, placeholders, revision briefs, or pipeline leakage

## Bundle Entry

Minimum valid entry:

```json
{
  "title": "Paper title",
  "evidence_type": "primary"
}
```

Recommended interoperable entry:

```json
{
  "id": "bundle:1",
  "title": "Paper title",
  "evidence_type": "primary",
  "year": 2024,
  "url": "https://example.org/paper",
  "doi": "10.0000/example",
  "relevance": 0.85,
  "source_type": "article",
  "publisher": "Journal or repository",
  "authors": ["Family, Given"],
  "abstract": "Optional source abstract or receipt text",
  "excerpt": "Optional exact quote or receipt excerpt",
  "retrieved_at": "2026-05-08T00:00:00Z",
  "hash": "sha256:..."
}
```

## Field Rules

- `id`: stable local ID. Use `bundle:N` for ordered citation targets.
- `title`: non-empty source title.
- `evidence_type`: `primary` or `review`; future values must not change the meaning of these two values.
- `year`: integer publication year when known.
- `url`: canonical source URL when available.
- `doi`: DOI without `https://doi.org/` when available.
- `relevance`: optional float from 0 to 1.
- `abstract`: optional; reference-only bundles are valid.
- `excerpt`: optional exact receipt text used for claim checks.
- `retrieved_at`: ISO 8601 timestamp for fetched metadata or receipts.
- `hash`: optional content hash for immutable receipts.

## Citation Notation

Drafted manuscripts may use `[bundle:N]` inline notation, where `N` is the one-based index in `source_bundle`.

Rules:

- Use `[bundle:1]`, not `[1]`.
- Multiple sources: `[bundle:1][bundle:3]`.
- Do not cite a bundle item for claims it cannot support.

## Registration And Provenance

Manual PROSPERO registration is not required by this spec.

For automated registration/provenance, the preferred path is:

1. generate a deterministic bundle manifest
2. persist the submission, review, decision, and publication IDs
3. export the manifest and provenance record to OSF and/or Derivation Web
4. store returned public IDs or URLs in provenance metadata, not in source claims

OSF access must use a personal access token only from an environment secret. The PAT must never be written into `source_bundle`, publication metadata, logs, artifacts, or exported bundles.

## Non-Goals

- No requirement to fetch full text.
- No requirement to register with PROSPERO.
- No claim that optional fields are currently enforced by intake.
- No change to runtime code.

