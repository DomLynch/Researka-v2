# Blinded Judge Calibration Protocol v1

## Purpose

Create a real, independently labeled calibration corpus without exposing prior
Researka decisions, submitter identity, or reviewer output to adjudicators.

## Case selection

- Sample at least 100 production submissions with completed decisions.
- Balance sampling across the hidden historical accept/revise/reject strata.
- Span every supported article type and at least eight research domains.
- Exclude test, benchmark, fixture, and synthetic-agent submissions.
- Deduplicate by the original submission content hash.
- Keep the source submission IDs and historical decisions only in the private
  manifest. They must never appear in adjudicator packets.
- If production has no genuine case for a supported article type, freeze the
  available real cases and list the missing type in the public receipt. Do not
  substitute a synthetic case; certification remains blocked until coverage is
  complete.

## Adjudicator eligibility

Each adjudicator must:

- have relevant research-methods or domain-review experience;
- be independent of the submission authors and producing agents;
- not be a model or reviewer included in the judge release being evaluated;
- disclose material conflicts of interest;
- complete one packet without consulting the other adjudicator;
- not access Researka's historical decision, review, or current judge output.

Each packet records the exact target judge-release ID and model list. Record the
adjudicator ID, type/model where applicable, qualification statement, conflict
disclosure, target judge-release ID, and completion timestamp. The merge tool
requires two distinct qualified identities and rejects any model present in the
target release. Each adjudicator must also attest
`reviewer_outputs_hidden_until_freeze: true`; otherwise merge fails.

## Required labels

Every case requires:

- decision: `accept`, `revise`, or `reject`;
- all six rubric scores, each from 1 to 5;
- claim support: `supported`, `partially_supported`, or `unsupported`;
- overclaim: `none`, `mild`, or `significant`;
- synthesis quality: `strong`, `adequate`, `weak`, or `empty`;
- a case-specific rationale of at least 20 characters.

Rubric dimensions:

1. Research-question quality
2. Synthesis quality
3. Claim-evidence alignment
4. Limitations quality
5. Gaps quality
6. Source grounding

Decision anchors:

- `accept`: publication-ready under the declared article type; no required
  scientific correction remains.
- `revise`: credible and repairable, with explicit required scientific changes.
- `reject`: fabricated, fundamentally unsupported, unsafe, or not repairable
  without replacing the central research claim or evidence base.

## Conflict resolution

Any difference in decision, rubric, or evidence verdict is a conflict. Different
wording in otherwise compatible rationales is retained in the private label
files but does not create a false conflict. Resolve every substantive conflict
using either:

- a qualified third adjudicator who has not seen the platform verdict; or
- documented consensus involving both original adjudicators.

The resolution record must cover exactly the conflicted case IDs. Unresolved
conflicts block corpus creation.

## Freeze, evaluation, and sign-off

1. Complete both packets independently.
2. Reveal the two frozen label sets and retain the reveal timestamp.
3. Resolve all conflicts.
4. Run the merge command. Only then may the corpus become `adjudicated`.
5. Evaluate the exact judge release named in both packets.
6. Review the measured results and limitations, then sign the unsigned
   evaluation hash and judge-release ID.
7. Write the active judge-release receipt with that release ID and the final
   signed artifact SHA. `/calibration` remains invalid unless all three match.

The contracts reject `adjudicated` status when there are fewer than 100 cases,
fewer than two distinct adjudicators, incomplete article-type/domain/decision
coverage, missing packet/label hashes, unresolved conflicts, or invalid event
timestamps. Public validity additionally requires post-evaluation human sign-off
and an active judge-release receipt bound to the signed artifact.

## Data handling

- `calibration/private/` is ignored by Git and must remain mode `0700`.
- Private manifests and packets must remain mode `0600`.
- Commit only code, this protocol, the aggregate freeze receipt, and the final
  adjudicated corpus after consent for publication.
- Do not publish private submission IDs, author identities, or the hidden
  historical-decision mapping.

## Commands

Prepare a 120-case packet on the production host:

```bash
python scripts/prepare_blinded_gold_set.py sample \
  --size 120 \
  --seed researka-real-v1 \
  --judge-release calibration/private/active-judge-release.json \
  --out-dir calibration/private/real-v1 \
  --public-receipt calibration/real_gold_set_v1_freeze_receipt.json
```

After independent labeling and conflict resolution:

```bash
python scripts/prepare_blinded_gold_set.py merge \
  --manifest calibration/private/real-v1/private_manifest.json \
  --freeze-receipt calibration/real_gold_set_v1_freeze_receipt.json \
  --labels adjudicator-a-complete.json adjudicator-b-complete.json \
  --resolutions conflict-resolutions.json \
  --output calibration/gold_set_v2.json
```

After evaluating that corpus against the target judge release, the designated
human reviewer signs the measured results and limitations:

```bash
python scripts/prepare_blinded_gold_set.py sign \
  --artifact /var/lib/researka/calibration/current.json \
  --active-release /var/lib/researka/calibration/active-judge-release.json \
  --signed-by "<reviewer identity>" \
  --statement "<results and limitations review statement>"
```

The final evaluation artifact's `run_meta.human_signoff` must contain
`approved`, `results_reviewed`, `limitations_reviewed`, `evaluation_sha256`,
`judge_release_id`, `signed_by`, `signed_at`, and `statement`. Keep the signed
artifact and active release receipt outside Git if they contain private results.
