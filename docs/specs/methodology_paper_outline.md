# Methodology Paper Outline

Status: draft.

## Working Title

Open Infrastructure for Agent-Native Source-Grounded Research Review

## Thesis

Agent-authored research artifacts can be made more inspectable by binding manuscripts to source bundles, structured review contracts, deterministic gates, and public provenance records.

## Scope

In scope:

- Researka v2 submission-to-publication runtime
- source-bundle schema
- tri-agent review protocol
- verdict ladder
- citation JSON-LD export
- OSF and Derivation Web provenance path

Out of scope:

- claims of clinical validity
- claims that automated review replaces human peer review
- PROSPERO/manual registration as a requirement
- vendor-specific model benchmarking beyond implementation notes

## Proposed Sections

### 1. Problem

Agent-authored manuscripts can be fluent while hiding weak source grounding, unverifiable review decisions, and unclear provenance.

### 2. System Boundary

Describe the runtime chain:

1. submission intake
2. autonomous review
3. autonomous editorial decision
4. autonomous publish
5. optional external audit and provenance export

Name current supported article types:

- `rapid_evidence_synthesis`
- `empirical_study`

### 3. Source Bundle Contract

Define the ordered `source_bundle` and minimal entry requirements:

- `title`
- `evidence_type`

Explain optional DOI, URL, year, relevance, abstract, excerpt, retrieval timestamp, and hash fields.

### 4. Review Contract

Describe structured reviewer output:

- recommendation
- six rubric scores
- major/minor issues
- required revisions
- claim-support verdict
- overclaim verdict
- synthesis-quality verdict

Explain accept constraints and why style variance is not itself a defect.

### 5. Verdict Ladder

Introduce L0-L8 as a reporting ladder.

Emphasize:

- L6 requires deterministic publish-gate evidence
- L7 requires actual OSF or Derivation Web provenance record plus artifact hash
- L8 requires independent reproduction against the same artifact hash

### 6. Automated Provenance

Position OSF plus Derivation Web as the automated registration/provenance route.

Security note:

- OSF uses a personal access token from environment secrets only.
- The PAT is never stored in bundles, publications, JSON-LD, logs, or provenance artifacts.

### 7. JSON-LD Export

Define publication-level and citation-level JSON-LD fields.

Separate structured metadata from citation display styles.

### 8. Evaluation Plan

Use implementation-aligned metrics:

- deterministic intake failure rates
- review contract parse failures
- reviewer/editorial agreement
- accepted-publication publish-gate failures
- external audit agreement/disagreement
- artifact hash reproducibility

### 9. Limitations

State plainly:

- reference-only bundles limit exact-statistic verification
- automated review is not a substitute for domain peer review
- L7/L8 require external records that may not exist for every publication
- optional model arbitrators do not make claims true

### 10. Implementation Notes

Mention model-backed roles only as replaceable providers.

IBM Granite may be used as an optional arbitrator model. No other arbitrator model is specified by the protocol.

## Claims To Avoid

- "fully automated peer review"
- "PROSPERO-equivalent"
- "guaranteed truth"
- "human-level scientific review"
- "tamper-proof" unless backed by immutable storage proof

## Minimum Evidence For Publication

Before publishing the methodology paper, collect:

- current API object model references
- one accepted publication artifact
- one rejected intake example
- one revise or reject review example
- one JSON-LD export example
- one OSF or Derivation Web provenance example, if L7 is claimed
