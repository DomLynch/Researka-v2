# Style Invariance V7 Summary

## Decision

The style-diverse v7 benchmark is a real improvement, but it is **not** a clean end-game pass yet.

## Final Result

| Slice | Threshold | Result | Status |
|---|---:|---:|---|
| overall | `>=70%` | `177/200 = 88.5%` | pass |
| house | `>=85%` | `39/50 = 78.0%` | fail |
| terser | `>=50%` | `40/50 = 80.0%` | pass |
| verbose | `>=50%` | `50/50 = 100.0%` | pass |
| external | `>=50%` | `48/50 = 96.0%` | pass |

## Runtime Summary

- total papers: `200`
- correct: `177`
- errors: `8`
- cost: `$0.2110`
- elapsed: `10445.6s`

## Why It Failed The Strict Gate

The strict benchmark gate failed because the **house** slice missed its `>=85%` threshold.

This is **not** the old failure mode:

- terser is no longer collapsing to `reject`
- verbose is fully clean
- external is strong

So the remaining blocker is not broad style blindness. The follow-up house-only rerun showed the real blocker is the **house accept/revise boundary**, especially medium papers being accepted too easily.

## Interpretation

- **Style invariance:** materially improved
- **Strict style benchmark:** not yet cleared
- **Invited pilot:** still should stay fenced; do not widen intake yet

## Next Move

1. fix the house slice accept/revise boundary
2. rerun the house slice to `>=85%`
3. rerun the strict style benchmark if needed
4. only then rerun the terser elite v3 cross-check

## Follow-Up House Retry

A clean house-only rerun against the deployed `2e0b8b6` state was started to test the timeout-only theory.

- by `26/50` completed papers, the run was only `16/26` correct
- the first `15` high papers all accepted correctly
- the problem reappeared in the house **medium** slice, which over-accepted almost every paper
- best possible final score at that point was only `40/50 = 80%`

The run was stopped early because it could no longer clear the `>=85%` house threshold.

Conclusion: the strict style gate is still blocked by a real **house-style calibration issue**, not just runtime errors.
