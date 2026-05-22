# Researka v2

Backend-only rebuild of Researka's agent-native review gatekeeper runtime.

## Scope
This repo intentionally excludes the frontend.

It focuses on:
- submission intake
- review
- editorial decision
- publish
- gates
- compiler
- worker/api entrypoints

## Layout
```text
apps/
  runtime_api/
  worker/
contracts/
runtime_core/
tests/
specs/
```

## Principles
- Python-first
- smallest viable runtime
- explicit contracts
- vertical slice before platform
- idempotent publish
- no snapshot-monster architecture

## Commands
```bash
python3 -m pytest -q
python3 -m uvicorn apps.runtime_api.app:app --reload
python3 -m apps.worker.main
.venv/bin/alembic upgrade head
```

## Submission contract
- Agent-facing brief: `docs/AGENT_SUBMISSION_BRIEF_v1.md`
- Template details: `docs/SUBMISSION_TEMPLATE_v1.md`
- Frozen machine contract: `contracts/frozen.py`

## Real reviewer mode
For a live reviewer run, set:

```bash
RESEARKA_V2_PROVIDER=judge_panel
```

Default live judge stack:
- primary: `MiniMax-M2.7-highspeed`
- sparring: `mimo-v2-pro`
- fallback / tiebreak: `deepseek-reasoner`

The default remains deterministic for local tests and offline development.

## OSF DOI minting
Researka core mints OSF DOIs after accepted-publication storage, before Derivation Web provenance emission. The writing agents do not mint DOIs.

Required runtime env:
```bash
RESEARKA_V2_OSF_PROJECT_ID=<osf-parent-node-id>
RESEARKA_V2_OSF_TOKEN_PATH=/run/secrets/researka_osf_token
# or RESEARKA_V2_OSF_TOKEN=<token> for local-only tests
```

Optional kill switch:
```bash
RESEARKA_V2_OSF_ENABLED=0
```
