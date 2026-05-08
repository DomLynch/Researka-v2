# Tri-Agent Protocol 1.0

Status: draft.

## Purpose

Define a review protocol where three independent roles produce an inspectable editorial decision from a submitted research object and its source bundle.

This is a protocol spec, not a claim that all roles are currently separate runtime jobs.

## Current Runtime Alignment

The current v2 flow is:

1. `submission_intake`
2. `autonomous_review`
3. `autonomous_editorial_decision`
4. `autonomous_publish` when accepted

Current reviewer output includes:

- `recommendation`: `accept`, `revise`, or `reject`
- six rubric scores from 1 to 5
- `major_issues`
- `minor_issues`
- `required_revisions`
- `claim_support_verdict`
- `overclaim_verdict`
- `synthesis_quality_verdict`
- `review_markdown`

## Roles

### 1. Drafter

Inputs:

- research question
- required article template
- `source_bundle`

Outputs:

- title
- abstract
- sectioned manuscript
- bounded claims tied to bundle entries
- explicit limitations and gaps

The drafter must not invent sources, strengthen unsupported claims, or include pipeline notes.

### 2. Reviewer

Inputs:

- submitted manuscript
- source bundle
- article type template

Outputs:

- structured review JSON
- recommendation
- rubric scores
- claim-support and overclaim verdicts
- required revisions when not accepted

The reviewer judges substance over house style. Reference-only bundles are allowed, but claims must stay proportionate to available source metadata and receipts.

### 3. Arbitrator

Inputs:

- submission
- reviewer output
- deterministic intake/publish gates
- optional external audit records

Outputs:

- terminal decision: `accept`, `revise`, or `reject`
- decision rationale
- provenance pointers
- publish eligibility

The arbitrator may be deterministic, model-backed, or hybrid. IBM Granite is an allowed optional arbitrator model when a model-backed arbitration pass is needed. No other arbitrator model is specified by this protocol.

## Decision Rules

Accept requires all of:

- all rubric scores >= 4
- no major issues
- no required revisions
- `claim_support_verdict = supported`
- `overclaim_verdict = none`
- `synthesis_quality_verdict` is `strong` or `adequate`
- publish gates pass

Revise applies when the submission is mostly correct and fixable with bounded edits.

Reject applies when the submission is structurally broken, unsupported, contradictory, or needs a scope reset.

## Independence Requirements

- Roles must preserve separate outputs.
- Reviewer and arbitrator metadata must include provider/model/prompt version when model-backed.
- A model-backed arbitrator must receive the reviewer output as evidence, not as an instruction to obey.
- The arbitrator must not silently convert unsupported claims into accepted claims.

## Provenance

Each role output should be addressable by ID:

- submission object ID
- review object ID
- decision object ID
- publication object ID, if accepted
- OSF or Derivation Web record URL when exported

OSF credentials are environment-only secrets. Personal access tokens must never be stored in bundles, role outputs, or provenance exports.

## Non-Goals

- No requirement for three different vendors.
- No requirement for a specific model provider.
- No replacement for deterministic intake and publish gates.
- No claim that model arbitration is mandatory.
