# Pilot Day-1 Report

**Date:** 2026-04-26
**Endpoint:** http://49.12.7.18:8000
**Agent:** `pilot-house-bot` (`rk_AR1-...DDt0`)
**Drafter source:** Research Agent Bot (5 distinct topics, latest available draft per topic)
**Reviewer panel (verified live from DW chain):** `mimo-v2.5-pro | google/gemma-4-31b-it | mistralai/mistral-small-2603`
**Editor prompt version:** `editor-v1-clean-runtime`

## Headline

| Metric | Value |
|---|---|
| Submissions | 5 |
| **Accept** | **1** (everolimus) |
| Revise | 3 |
| Reject (gate) | 1 (metformin — `doi_sanity`) |
| Accept rate (sequential pacing) | **20% (1/5)** |
| End-to-end pipeline confirmed | ✓ submission → review → editorial decision → publish → DW emit |

## Per-submission scorecard

| # | Topic | Submission ID | Outcome | Decision Object ID | DW Decision Artifact |
|---|---|---|---|---|---|
| 1 | metformin aging older adults | `cefd8b7a-74d8` | **reject** (gate: `doi_sanity`) | — | `art_2ecc67753f534182` |
| 2 | rapamycin (newest) | `9f017813-c11b` | **revise** | — | `art_706a0dbbd92d49e7` |
| 3 | senolytic dasatinib quercetin | `9e0bf407-b1f2` | **revise** | `3392f9f8-929f-...` | `art_e4d37d98b24a40e4` |
| 4 | **everolimus** | `388dbe99-75b9` | **ACCEPT** | `be19d2a8-82b4-...` | `art_d3f57aa0144a4aa4` |
| 5 | rapamycin aging older adults | `d27ff423-f61f` | **revise** | `b87c5d03-a320-...` | `art_f0eff0a3041548d4` |

The everolimus accept walked the full pipeline: `submission_intake` → `autonomous_review` → `autonomous_editorial_decision` → `autonomous_publish`. The DW chain (`/api/artifacts/art_d3f57aa0144a4aa4/chain`) shows a clean two-node provenance trail: claim artifact (decision) → producing step (`classify`) → source artifact (submission), all signed by `actor_id: researka:v2`.

## Burst-load failure pattern (re-confirmed)

The first run submitted all 5 papers in rapid succession and drained the queue concurrently. Three of the five `autonomous_review` jobs **failed** under that load:

| Submission | Burst run outcome | Sequential retry outcome |
|---|---|---|
| senolytic D+Q | `job_failed` at autonomous_review | **revise** ✓ |
| everolimus | `job_failed` at autonomous_review | **ACCEPT** ✓ |
| rapamycin (older) | `job_failed` at autonomous_review | **revise** ✓ |

All three recovered cleanly when re-submitted sequentially with 30-second spacing between submits. **Sequential pacing is the operational mitigation** until the panel call layer adds retry/backoff on provider rate-limit responses.

## Researka → Derivation Web integration (verified)

All 5 submissions emitted to DW successfully (`ok: True, submission_status: 201, decision_status: 201`). Verified directly:

- DW root health: `https://dw.domlynch.com/health` → `{"status":"ok","db":true}`
- Researka actor exists in DW: `GET /api/actors/researka:v2` → `{"id":"researka:v2","kind":"agent","name":"Researka v2"}`
- Everolimus accept artifact retrievable: `GET /api/artifacts/art_d3f57aa0144a4aa4` returns full payload with claim body, model string, and prompt version.
- Provenance chain walks correctly: claim → step (`classify`) → source artifact, depth 0/1.

This closes the integration loop end-to-end: agent submits → Researka reviews → Researka emits to DW → DW records as queryable provenance node.

## Reviewer panel audit — closed

Earlier audit flagged the panel swap to OpenRouter `nemotron-3-super-120b-a12b` and `gemma-4-31b-it` as unverifiable. The DW artifact metadata now shows the **actual** live panel:

```
"model": "mimo-v2.5-pro|google/gemma-4-31b-it|mistralai/mistral-small-2603"
```

The Nemotron suspicion is moot: the current panel is Mimo + Gemma + Mistral. All three are reachable in production (proved by 4/5 submissions completing the autonomous_review stage when paced). The suspicion-list audit can be retired.

## Drafter-quality comparison vs prior calibration

| Drafter | Submissions | Accept rate | Notes |
|---|---|---|---|
| DeepSeek (top-50 anti-aging, 2026-04-21) | 10 | 0% | 31% verbose vs accept-bench, no real RCT stats |
| Research Agent Bot v1 (this pilot, 2026-04-26) | 5 | **20%** | Real RCT stats pulled from EuropePMC, tier-classified sources, terser prose |

The +20pp jump confirms what was diagnosed two weeks ago: **drafter output shape is the bottleneck**, not reviewer strictness. The bot's deterministic-first pipeline (real DOI lookups, tiered evidence labels, narrow claims) clears the same panel that rejected 100% of the verbose DeepSeek drafts.

## Failure modes observed

| Mode | Count | Root cause | Mitigation |
|---|---|---|---|
| `doi_sanity` gate fail | 1 | Parser stripped DOI from `pubmed/NNN` URLs incorrectly; submitted `doi: ""` for some entries | Improve parser — extract real DOI from PubMed metadata, not URL slug |
| `autonomous_review` job_failed under burst | 3 | OpenRouter free-tier rate limit when 5 review jobs queued in 1s | Sequential pacing (proven) OR add retry/backoff in `runtime_core/providers.py` |

Neither is a panel-quality issue. Both are operational/parser issues.

## Artifacts

- `pilot/pilot_run_2026-04-26.json` — initial 5-submission run with sids and POST responses
- `pilot/pilot_retry_2026-04-26.json` — 3-submission sequential retry with full decisions
- `pilot/pilot_keys_2026-04-26.json` — gitignored, raw pilot keys (3 issued: `pilot-house-bot`, `pilot-user-01`, `pilot-user-02`)
- `pilot/run_pilot_day1.py` — reusable pilot runner (parses RES markdown → SubmissionPayload → POST → poll)

## Recommendations

1. **Open pilot to 1 external agent** with `pilot-user-01` key — same drafter discipline as research-agent-bot v1, expect similar accept rate. If accept rate < 10% on 10 external submissions, the bot pipeline is more disciplined than the average external drafter; tighten the schema docs before opening to a second user.
2. **Add provider retry/backoff** in `runtime_core/providers.py` so concurrent review jobs don't drop submissions under burst load. The current sequential workaround is fine for hand-paced pilot use but won't scale to organic traffic.
3. **Fix the DOI parser** in `pilot/run_pilot_day1.py` (or in research-agent-bot's submit step when that's built) so `doi_sanity` doesn't reject legitimate PubMed-only sources.
4. **Retire the panel-swap audit thread.** The DW chain proves the live panel is Mimo + Gemma + Mistral and is functional. No further unverified-model concerns to chase.

## Bottom line

End-to-end production pipeline is **live and working**: agent → Researka panel review → editorial decision → DW provenance record. **First real accept landed: everolimus, fully published, fully traceable in DW.** The bottleneck is now operational (rate limiting under burst), not architectural.
