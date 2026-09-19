# Independent calibration readiness — September 19, 2026

Certification: **not complete**. The public `/calibration` receipt remains `valid=false`; the 30-case working result is not the current judge's certified accuracy.

## Preparation and evidence
- Existing frozen corpus: 120 real submissions, balanced 40 accept/40 revise/40 reject, nine domains, four of five supported article types.
- Both private adjudicator packets contain zero completed decisions. Their historical judge-release binding is not evidence of calibration for the current release.
- A fresh production census initially failed on a historical source bundle exceeding today's 100-source intake limit. The preparation script now records contract-invalid exclusions in both the private sampling manifest and public receipt; it does not truncate papers to make them eligible. Direct validation without an audit counter still raises. A regression verifies exclusion, unchanged source count and receipt/manifest accounting.
- Fresh read-only production census: 1,195 eligible real cases; six contract-invalid historical payloads excluded and counted. A 120-case candidate sample spans nine domains and balanced40/40/40 decisions, but still only four article types (no genuine empirical study). It has not been frozen or labeled.
- Workspace receipts: `../Researka-Review-2026-09-19/calibration-readiness.json` and `calibration-production-census.json`. Private packets remain uncommitted; no labels or human signoff were invented.

## Required inputs and completion sequence
1. Name two qualified independent adjudicators, with qualification/conflict declarations. Keep both packets and historical Core outputs blinded to each reviewer until labels freeze.
2. Supply a genuine `empirical_study` case. Synthetic benchmark rows cannot fill this coverage gap.
3. Freeze a fresh target judge-release manifest and corpus before adjudication. Use `scripts/prepare_blinded_gold_set.py sample --size 120 --seed researka-real-2026-09-19 --judge-release <verified-release.json> --out-dir <new-private-directory> --public-receipt <new-receipt.json>` with read-only production credentials. Never overwrite the old packets.
4. Complete both packets, resolve every conflict independently, and run the script's `merge` command with both labels and resolutions.
5. Evaluate against that exact frozen judge release; obtain the human post-evaluation signoff using `sign`. Verify `/calibration` binds the evaluated release and satisfies every validity field before describing it as certified.

Follow `ADJUDICATION_PROTOCOL_V1.md`; software regression tests and two same-family GPT review calls do not substitute for independent calibration.
