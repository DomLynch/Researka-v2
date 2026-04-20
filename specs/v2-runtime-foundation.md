# Researka v2 Runtime Foundation

## Goal
Stand up the smallest clean backend skeleton that can model autonomous publishing from editorial decision to final publication and provide the foundation for the rest of the A->Z chain.

## Non-Goals
- frontend work
- full provider wiring
- production deployment
- schema migration rewrite
- event-sourced core

## Simplest Design
- 3 top-level modules only: `apps/`, `runtime_core/`, `contracts/`
- in-memory repo for the initial slice
- one vertical path fully modeled:
  - editorial decision
  - revision cap
  - publish dedupe
  - compile
  - publish gates

## Files To Touch
- `contracts/` for enums and payloads
- `runtime_core/` for real logic
- `apps/` for runtime entrypoints
- `tests/` for workflow and gate coverage

## Data Model
- `RuntimeJob`
- `WorkflowContext`
- `WorkflowOutcome`
- `PublicationArtifact`
- `RuntimeEvent`

## Risks
- scaffolding can become abstract if it stops modeling real publishing concerns
- dedupe and gate logic can drift unless tested early

## What We Refuse To Build
- multi-service sprawl
- plugin system
- queue bus
- event-sourcing on day one
- generic agent framework

## Why This Avoids Long-Term Drag
- one module per reason to change
- explicit contracts
- publish-specific logic isolated from transport and storage
- small enough to delete and reshape fast
