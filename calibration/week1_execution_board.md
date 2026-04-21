# Researka v2 Week 1 Execution Board

Starting point:
- SHA: `6834877`
- Tests: `95 passed, 1 skipped`
- Gold-set baseline: `10` entries
- Live gold-set eval: `80%` overall
- `rapid_evidence_synthesis`: `71.4%`
- `empirical_study`: `100%`

Current status:
- Day 1 audit passed: `gold_set_v1` expanded to `30` entries (`gold-set-v1-working`)
- Day 2 audit passed: explicit rationales preserved, deterministic smoke artifact saved
- Day 3 baseline passed on the working corpus: live judge-panel artifact frozen at `30/30` (`artifacts/gold_set_eval_v2_working_baseline.json`)
- Immediate next focus: broaden from the working corpus to a less curated human/domain-labeled gold set, then rerun the 200-paper benchmark

Rule:
- Do not advance to the next day until the audit gate at the end of the current day passes.

## Day 1 — Expand the Working Gold Set
1. Inventory the current `gold_set_v1.json` by article type, verdict, and source mix.
2. Inventory all persisted real-draft sources in `calibration/top50_antiaging_drafts_2026-04-21.json`.
3. Inventory all persisted real outcomes in `calibration/top50_antiaging_run_2026-04-21.json`.
4. Map current gold-set entries to source files so duplication is explicit.
5. Select 10 additional rapid-evidence-synthesis real-draft candidates from the top50 anti-aging set.
6. Select 5 additional synthetic rapid-evidence-synthesis controls with explicit target verdicts.
7. Draft 5 additional empirical-study controls with clear accept / revise / reject shapes.
8. Add tags for source class: `real-draft`, `synthetic-control`, `empirical-study`.
9. Add tags for target verdict: `accept-control`, `revise-control`, `reject-control`.
10. Add tags for article type and domain where missing.
11. Normalize `article_type` on every entry.
12. Normalize `core_claims_resolved` on every entry.
13. Validate that every entry loads via the gold-set schema.
14. Check that every entry has an explicit rationale string.
15. Freeze the expanded corpus to disk.

Audit gate:
- corpus count >= `25`
- schema load passes
- no duplicate `entry_id`
- no missing `expected.decision`

## Day 2 — Harden Labels and Rationales
16. Review every `accept` label for over-optimism.
17. Review every `reject` label for accidental intake-vs-substance confusion.
18. Split contested RES entries into `revise` vs `reject` with clear logic.
19. Mark any corpus items that are only weak seed labels.
20. Tighten rationale text on every real-draft entry.
21. Tighten rationale text on every synthetic control.
22. Tighten rationale text on every empirical-study control.
23. Add `notes` for known ambiguity or expected controversy.
24. Verify verdict distribution is not skewed beyond usefulness.
25. Verify article-type distribution is still intentional.
26. Verify RES dominates the corpus, since RES is the actual problem.
27. Run a deterministic smoke eval on the expanded corpus.
28. Save the smoke artifact.

Audit gate:
- every entry has rationale
- no unlabeled ambiguity
- smoke eval artifact saved
- corpus remains loadable and reproducible

## Day 3 — Baseline Measurement and Blocker Map
29. Run the full live gold-set eval on VPS against `judge_panel`.
30. Save the new live artifact.
31. Extract overall accuracy.
32. Extract RES-only accuracy.
33. Extract empirical-study-only accuracy.
34. Extract full confusion matrix.
35. Extract mismatch list.
36. Rank top accept blockers by frequency.
37. Separate RES blockers from empirical-study blockers.
38. Identify whether failures are mostly false rejects, false accepts, or revise-collapse.
39. Freeze the blocker summary into a markdown note.

Audit gate:
- live eval artifact exists
- mismatch list exists
- blocker ranking exists
- RES vs empirical split is explicit

## Day 4 — Tune the RES Reviewer
40. Inspect the top 5 RES mismatches line by line.
41. Identify prompt language that over-triggers `claim_support_verdict`.
42. Identify prompt language that over-triggers `major_issues`.
43. Identify prompt language that over-triggers `required_revisions`.
44. Tighten the RES reviewer prompt so acceptable synthesis is not collapsed into `revise` by default.
45. Leave `empirical_study` prompt untouched unless evidence shows regression.
46. Add or update tests that pin the tuned behavior.
47. Run the full local test suite.

Audit gate:
- tests green
- prompt diff is intentional and small
- no empirical-study regression in tests

## Day 5 — Rerun and Compare
48. Rerun the full live gold-set eval on VPS with the tuned RES prompt.
49. Compare tuned vs baseline and make the go / no-go call on further reviewer tuning before pilot.

Audit gate:
- new live artifact saved
- before/after comparison is explicit
- one winner is selected

## Day 6 — System Validation
- Run the 200-paper benchmark again with the current winning reviewer behavior.
- Compare the benchmark artifact to the gold-set direction.
- Verify `/calibration`, `/provenance`, `/audit-summary`, and auth paths still behave.
- Freeze resulting artifacts.

Audit gate:
- benchmark artifact saved
- service still healthy
- no regression in auth, provenance, or audit routes

## Day 7 — Pilot Decision Package
- Update `PROJECT_STATE.md` with final numbers.
- Write the pilot go / no-go memo.
- Name remaining known risks.
- Define explicit pilot restrictions if going live.
- Define rollback rule if pilot opens.

Audit gate:
- one binary decision
- frozen artifacts on disk
- repo and VPS synced
