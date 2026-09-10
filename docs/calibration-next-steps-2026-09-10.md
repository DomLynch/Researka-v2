# Independent calibration readiness — 10 September 2026

Calibration remains uncertified. This is a preparation receipt, not a judge score.

## Material recovered and verified

- Recovered the existing frozen `real-v1` private manifest, two adjudication
  packets and release manifest from `/root/Researka-v2/calibration/private/real-v1`
  to the same ignored path in the MacBook checkout. No original files were changed.
- Manifest and both packet hashes match the committed freeze receipt.
- Both packets contain 120 cases; all 120 payloads validate against today's
  `SubmissionPayload` schema. Completed labels: **0/120 per adjudicator**.
- Coverage is four of five article types. Genuine `empirical_study` coverage is
  still missing. The original 40 accept / 40 revise / 40 reject strata are hidden
  historical outcomes, not independently established reference labels.
- The frozen target is reviewer v12 with MiniMax/Gemma/Mistral. It does not match
  today's v15 Sol/Terra route. Its empty `observed_models` also fails the current
  release-manifest validator. Do not relabel this old manifest as a current run.

## Fresh audit challenge packets

`calibration/private/audit-2026-09-10/adjudicator-{a,b}.json` contain the same 24
real research-synthesis manuscripts in two different orders: latest 20 submissions
plus four additional submissions behind the five newest publications. Author
identity is redacted with the existing sampler; historical verdicts and prior
review text are excluded. The mapping is private. Each case has a content hash
and blank adjudication. Policy instructions are v15; a fresh observed release
binding is explicitly pending.

This targeted set can test suspected false accepts, rejection proportionality,
and whether requested corrections actually appear in a revised manuscript. It
is not a 100-case certification corpus and does not fill empirical-study coverage.
Keep the detailed operational audit and private mapping away from adjudicators.

Packet SHA256 receipts:

- A: `45ea7f17d7106d2832a2cf4f78019d832f1555f02cbd43283a40f4f7a866712b`
- B: `b95c047f98cfa2d6d60551b074992ec803155c4a36f922128884a852e4bf436a`

Private directories are mode 0700 and files 0600; `calibration/private/` remains
ignored. No unpublished manuscripts, completed reviewer labels or private mapping
are committed by this preparation work.

## Ordered completion path

1. Obtain genuine empirical-study material with provenance. Do not turn a
   synthesis into purported original empirical work or label a fixture real.
2. Obtain a valid observed target release under the deployed v15 route, retaining
   code/prompt hashes, model identities/settings and corpus identity. Preserve the
   old freeze; create a separately versioned current freeze before new labeling.
3. Have two qualified independent adjudicators complete the blinded packets.
   They may be humans or independent models permitted by the existing protocol;
   evaluated panel models cannot adjudicate themselves. Record qualifications,
   independence, conflicts and completion times honestly. Resolve disagreements
   independently before freezing reference labels.
4. Use `scripts/prepare_blinded_gold_set.py merge` to validate provenance and
   combine labels into the gold-set schema. Prepare compatible current packets
   with the existing `sample` command; the targeted audit packets above are
   staging material, not directly interchangeable with the certified format.
5. Evaluate the frozen reference corpus against the current release with
   `scripts/evaluate_gold_set.py`, preserving progress and partial artifacts.
   Set a reviewed execution/spend budget before running the full live panel.
6. Review complete per-type metrics, timeline and release identity. The owner
   supplies actual sign-off through `prepare_blinded_gold_set.py sign`; nobody
   may invent that sign-off. Only publish a calibration artifact when every
   existing certification check passes.

No provider requests, reference-label changes, signature, calibration promotion,
production requeue or editorial override occurred during this preparation.
