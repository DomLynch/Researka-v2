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
- primary: `mimo-v2.5-pro`
- sparring: `google/gemma-4-31b-it` via OpenRouter
- fallback / tiebreak: `mistralai/mistral-small-2603` via OpenRouter

The default remains deterministic for local tests and offline development.

## Derivation Web
When `/etc/derivation-web/researka.key` exists, decisions are mirrored to `https://dw.domlynch.com` as non-blocking provenance artifacts. DW failures do not block Researka decisions.
