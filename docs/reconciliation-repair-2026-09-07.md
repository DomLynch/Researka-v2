# Core reconciliation repair

Scope: Core only; preserve author payloads, historical decisions, acceptance
quorum, evidence thresholds and publication-integrity checks. No model change.

## Audit mapping

| Finding | Repair | Executable evidence |
| --- | --- | --- |
| Authentic spans lost during normalization | Preserve statistical inequalities while stripping markup; exact match cannot also be a mismatch | `test_markup_normalization_retains_inequalities_and_tail`; saved five-source replay |
| Ordinary bundles did not request DOI-derived full text | Existing PMC/arXiv retrieval now serves bundle spans; PMC retries/fallback share article-identity validation | `test_ordinary_bundle_resolves_full_text_by_doi`; `test_pmc_fallback_retries_but_never_accepts_wrong_identity` |
| Partial abstract treated as false evidence | Missing coverage raises a technical retrieval failure, not a manuscript revision | `test_incomplete_source_coverage_is_not_an_author_revision` |
| Numeric lexical false matches | Check effect direction/endpoint, equivalent units and bounded adjacent sentences; preserve wrong-subject regressions | `test_claim_reconciliation_discriminates_outcome_direction_units`; existing wrong-subject tests |
| Table outcomes escaped checks | Bind Endpoint/Value rows to sources; retrieve independent claim passages and give row-specific feedback | `test_table_endpoint_checked_and_feedback_locates_row`; `test_review_path_requests_independent_claim_and_table_passages_without_mutating_bundle` |
| Recovered publish job lacks acceptance ID | Include current acceptance ID and package hash; publisher still validates lineage | `test_accepted_missing_job_recovers_with_real_lineage` |
| Editorial override blamed on reviewer | Report `claim_trace_guard` separately | `test_editorial_override_not_mislabelled_reviewer_failure` |
| Moving revision requirements | Pass server-owned previous decision/issues and changed sections; require explanation of new material blockers | `test_revision_context_is_server_owned_and_contains_original_issues` |
| Valid disagreement treated as provider failure | One bounded GPT reconsideration round; unresolved result is `review_disagreement`/`ESCALATE`, not automatic author revision or provider retry | `test_bounded_adjudication_uses_two_final_valid_votes_no_paid_backup` |
| Stranded PUBLISHING invisible | Include in bounded delivery recovery and stall alerts | parametrized `test_reconciler_retries_external_delivery_once_without_duplicate_work`; `test_publishing_state_is_alerted` |
| Protocol incorrectly counted as a completed study | Explicit protocol/context remains background, outside citation floor and primary-results appraisal | `test_protocol_context_does_not_count_as_primary_results` |
| Historical revise displayed during reassessment | Current decision response excludes decisions older than the latest intake attempt; history remains intact | API reassessment regression |
| Unresolved panel disagreement tells author to retry | Return ESCALATE with review fault domain and no automatic retry/resubmission | API disagreement regression |

## Review passes

1. Reproductions and behavior review: confirmed source-normalization loss,
   wrong-outcome matches and missing acceptance lineage. Tested positive and
   negative cases, including wrong DOI in fallback XML and no paid tiebreaker.
2. Integration/adversarial review: preserved source identities, canonical
   payload hashes, distinct final reviewer votes, owner-bound revision history,
   terminal-failure classification and bounded delivery retries. Existing tests
   caught an unsafe adjacent-sentence join; it was fixed rather than weakening
   the assertions. Refreshed the existing CodeGraph index after integration.
   Final review also aligned reviewer table feedback with the authoritative
   passages used by editorial checks; the request-capture regression confirms
   a supported row is not sent to the reviewer as an unresolved warning.

Commands: `.venv/bin/python -m pytest -q`, `.venv/bin/python -m mypy .`,
`make quality`, `git diff --check`.

## Saved-submission replay

The September 7 private snapshot contains five intake revisions incorrectly
flagging spans in DOI `10.1093/jpids/piae062` or `10.1111/acel.70103`.
Against the saved authoritative XML, all five now have verified spans and zero
evidence mismatches/incomplete-coverage flags for those sources. Three still
contain the 48% mortality row; the table check flags it. This source replay is
not five acceptance decisions. Reassess the original IDs through normal review.

## Limits and remaining validation

- These are bounded deterministic checks, not general semantic entailment.
  `NEEDS_SEMANTIC_REVIEW` does not mean a contradiction was proved. The checker
  still samples prose and recognizes Endpoint/Value markdown tables; it does
  not promise exhaustive coverage of every narrative or table format.
- Revision continuity supplies evidence and reviewer instructions; it cannot
  guarantee that a model never raises an unwarranted new issue.
- Unresolved reviewer disagreement requires editorial adjudication. It must
  not be converted into acceptance or repeatedly charged to OpenRouter.
- Independent calibration remains open: two independent human adjudicators
  must label the frozen real cases, cover every supported article type, resolve
  conflicts, and sign off. Then evaluate the exact deployed release and report
  false accepts, false revisions and false rejections. Do not tune thresholds
  to synthetic accept labels or claim independent validation from unit tests.

## Feedback-contract completion

The follow-up closes the code-side feedback gaps without lowering acceptance
thresholds, changing models, or changing agent code:

- Claim diagnostics carry stable IDs, source IDs, considered passages and
  mismatch axes. Comparisons preserve the source-owned field's surrounding
  context; displayed fields are capped at 2,000 characters and marked when
  truncated. Original authoritative receipts remain in the reviewer input.
- Lexical differences are not labelled proven contradictions. Mixed passages,
  compatible bounds and ambiguous effect wording require semantic review.
  These diagnostics are not an independently validated entailment engine.
- Mandatory model objections require a material-impact assertion, exact
  section/quote for incorrect statements, and a bounded correction. Missing
  sections can be reported as omissions. Short valid fixes remain valid.
- Quotes preserve scientific operators and numeric boundaries: `p > 0.05`
  cannot be grounded in `p < 0.05`, nor `48` in `148`.
- Each previous mandatory issue must be resolved or persist. New issues need
  an explanation; the same issue cannot also be marked resolved. These fields
  persist on the review for inspection.
- Invalid or explicitly nonmaterial feedback uses the existing one-round GPT
  reconsideration. Unresolved feedback escalates, rather than becoming an
  author revision or a new paid-model tiebreaker. This checks reviewer output,
  not a new manuscript eligibility threshold.

Evidence: `tests/test_review_materiality.py` covers normal and malformed
feedback, concise corrections, revision accounting, real workflow persistence,
bounded two-model reconsideration with zero backup calls, scientific quote
operators and context-sensitive diagnostic regressions. Existing
`tests/test_reconciliation_evidence.py` covers recovery, source reconciliation,
table feedback, revision context and editorial-stage reporting.

Two independent review passes found and prompted repairs to length-based
false blockers, resolved-issue overlap, nonmaterial omissions, false lexical
contradictions and punctuation-losing quotation checks. No complexity or
duplication baseline was raised to accommodate the implementation.

## Remaining empirical work: not completed by code

The checked freeze receipt contains 120 cases but lacks `empirical_study`
coverage. It is explicitly `blinded_incomplete_coverage`, not a certified gold
set. Do not execute a paid benchmark or supply invented adjudicator labels.

The existing `scripts/prepare_blinded_gold_set.py` already provides the path:

1. Add genuine empirical-study candidates and freeze a new complete sample
   against the chosen current judge release using `sample --judge-release`.
2. Two independent adjudicators label the blinded packets. Resolve disagreements
   with documented adjudication, then run `merge --labels LABEL_A LABEL_B`
   with the frozen manifest and receipt (and `--resolutions` when required).
3. Evaluate that adjudicated corpus with `scripts/evaluate_gold_set.py` using
   the exact frozen release. Schedule the run only with an explicit usage budget.
4. Inspect false-accept, false-revision and false-rejection rates; obtain genuine
   human sign-off with `prepare_blinded_gold_set.py sign`.

Responsibility: Core owns the existing tooling and release binding; independent
adjudicators own labels and sign-off. No automated completion claim can replace
that missing evidence. This does not disable normal production review.
