# Baseline V6 Summary

## Decision

The revised synthetic corpus plus `reviewer-v6-triage-anchors` is a real calibration win on the broad benchmark, but not yet a style-invariant win.

## Benchmark Comparison

| Run | Corpus / Prompt | Result | Read |
|---|---|---:|---|
| `benchmark_vps_200_stage1.json` | old broad corpus + pre-v6 prompt | `110/200 = 55.0%` | collapse to `revise` |
| `calibration_micro_fixture_v2_final.json` | revised 20-paper micro-set + `reviewer-v6-triage-anchors` | `20/20 = 100%` | clean separation restored |
| `benchmark_baseline.json` | repaired broad 200 + `reviewer-v6-triage-anchors` | `187/200 = 93.5%` | broad gate cleared |
| `benchmark_v3_vs_v6_prompt.json` | terser elite v3 cross-check + `reviewer-v6-triage-anchors` | `0/40 = 0.0%` | style invariance failed |

## Broad 200 Details

- total: `200`
- correct: `187`
- accuracy: `93.5%`
- accepts: `45`
- revises: `76`
- rejects: `79`
- intake rejected: `22`
- errors: `0`

### By quality

- high: `45/46 = 97.8%` accept
- medium: `76/88 = 86.4%` revise
- low: `44/44 = 100%` reject
- broken: `22/22 = 100%` reject

## What Changed

Two interventions mattered together:

1. the synthetic benchmark corpus was tightened so `high` and `low` fixtures are genuinely separable
2. the reviewer prompt was changed to the triage/anchor version

Prompt-only tuning on the old muddy corpus did not move the score. Corpus-only separation improved things, but not enough. The winning branch was the combination.

## What The v3 Cross-Check Means

The terser elite v3 corpus still got `40/40 reject` in the checkpoint run. That means the reviewer is now calibrated for the revised benchmark house style, but not yet robust to the terser external draft style.

## Go / No-Go

- **Go for invited pilot** only if intake is fenced to the controlled Researka drafter style.
- **No-go for wider style-diverse intake** until the external/terser drafter style is aligned and rerun.
