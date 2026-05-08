# Verdict Ladder 1.0

Status: draft.

## Purpose

Define an evidence-verdict ladder from L0 to L8 for source-grounded research objects.

The ladder is a reporting and gating vocabulary. It must not be used to imply stronger verification than the platform can inspect.

## Ladder

| Level | Name | Definition | Minimum Verifier |
|---|---|---|---|
| L0 | Draft | Text exists but has no valid source bundle. | Presence check. |
| L1 | Structured | Required article sections are present. | Template check. |
| L2 | Bundled | Source bundle meets minimal schema. | Bundle schema check. |
| L3 | Recent Enough | Bundle meets article-type count and recency thresholds. | Intake gate. |
| L4 | Claim-Bounded | Main claims are tied to bundle entries and do not obviously overclaim. | Reviewer rubric and overclaim verdict. |
| L5 | Review-Passed | Review recommends accept under current rubric. | Structured review contract. |
| L6 | Publish-Gated | Deterministic publish gates pass and counts reconcile. | Compiler and publish gate output. |
| L7 | Provenance-Registered | Public provenance record exists for the exact artifact or manifest. | OSF/Derivation Web URL plus artifact hash or object IDs. |
| L8 | Independently Reproduced | A separate verifier re-runs the checks against the same artifact and agrees. | External audit record with matching artifact hash and verdict. |

## Current Runtime Mapping

Currently implemented or directly represented:

- L1: required section checks
- L2: minimal bundle-entry checks
- L3: rapid evidence synthesis count and recency checks
- L4: review rubric fields and overclaim verdict
- L5: reviewer recommendation contract
- L6: publication compiler and publish gates

Future-safe levels:

- L7 requires an actual external provenance record, not a planned export.
- L8 requires an independent verifier and a matching artifact hash, not another internal model pass.

## L6 Requirements

L6 is verifiable only when the publication artifact includes:

- title
- abstract
- body markdown
- publication counts
- gate results
- parent submission ID

Publish gates must include at least:

- leakage blocker
- count reconciliation
- core claims resolved

## L7 Requirements

L7 is verifiable only when the artifact or manifest has:

- immutable artifact hash
- public OSF or Derivation Web record URL/ID
- export timestamp
- submission/review/decision/publication object IDs
- no embedded secrets

Manual PROSPERO registration is not required.

OSF access must be via an environment-only PAT. The PAT must never appear in the registered artifact, source bundle, provenance metadata, logs, or exported JSON.

## L8 Requirements

L8 is verifiable only when a separate verifier records:

- verifier identity or system ID
- verifier timestamp
- artifact hash checked
- verdict ladder level claimed
- agreement or disagreement with system verdict
- notes sufficient to audit disagreement

L8 does not require a specific vendor, model, registry, or institution.

## Failure Behavior

- If a verifier cannot inspect the artifact hash, the maximum level is L7.
- If provenance export is missing, the maximum level is L6.
- If publish gates are absent or failed, the maximum level is L5.
- If the bundle is invalid, the maximum level is L1.

