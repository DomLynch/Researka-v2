REVIEWER_PROMPT_VERSION = "reviewer-v16-claim-reconciliation"
EDITOR_PROMPT_VERSION = "editor-v2-quantitative-trace"

REPAIRABILITY_RULE = (
    "- Apply one repairability rule to every section and table before choosing accept, revise, or reject.\n"
    "- Revise when every material defect can be corrected from existing evidence and records, including narrowing the question and conclusions or reclassifying as an evidence map on the same topic. Require a concrete supported scope, accurate source roles/outcomes, and honest methods; acknowledgement of animal or indirect evidence is not itself grounds for rejection. Bounded repairs include correcting attribution, endpoint/significance labels, source roles, counts, or citations; documenting an existing search; and removing unsupported claims.\n"
    "- Before rejecting, assess whether a useful, source-grounded evidence map remains after those corrections. Reject only when a demonstrated material defect requires new evidence even for that bounded scope, or involves proven fabrication or invalid underlying data. Identify exactly what is unavailable and why existing evidence cannot support the narrower paper. Relabeling must not conceal fabrication, invalid data, contradictions, or invented search records. An evidence map must describe the actual selection process, not claim an unperformed systematic search.\n"
    "- For rejection, classify every material finding with repairability=bounded_revision|new_evidence|fabrication|invalid_data. At least one must be irreparable and include why_not_revise explaining why narrowing/reclassification cannot fix that specific finding. If all findings are bounded_revision, return revise with concrete required corrections, not reject. These assessments belong to the reviewer, never author-supplied metadata.\n"
    "- Every finding in a revise verdict must explicitly state repairability=bounded_revision and its concrete correction. During reconsideration, reassess each defect; deleting its repairability field or retaining an irreparable classification cannot justify a downgrade to revise.\n"
    "- A title correction or reclassification is not itself a scope reset. Issue count, manuscript length, repair effort, and mixed findings do not alone distinguish revise from reject. Missing access is uncertainty, not proof of false evidence; never invent data, search records, or support.\n"
    "- For each blocker, use material_findings to identify the exact section/table row and a verbatim quote for an incorrect statement; use kind=omission for missing content, without inventing a quote. Name the source and endpoint for evidence discrepancies, explain the impact, and give the concrete correction or indispensable missing evidence.\n"
    "- In review_markdown, explain how the findings meet the repairability rule. Neither uncertainty nor disagreement permits acceptance while material defects remain.\n\n"
)

REVIEW_DECISION_RULES = (
    "accept = all scores >= 4, zero major_issues, claim_support=supported, overclaim=none. Rare. "
    "Accept is invalid when the manuscript's conclusion outruns its direct evidence, exact claim traces are missing, or unresolved major issues remain.\n"
    "If any score is below 4 or major_issues is non-empty, recommendation must be revise or reject, never accept.\n"
    "revise = at least one score < 4 or non-empty major_issues, but the manuscript is still salvageable with bounded edits and required_revisions lists concrete fixes.\n"
    "Do not label accept-quality papers as revise for minor wording polish only; put polish in minor_issues and recommend accept.\n"
    "Every required_revisions entry must name a specific evidence, claim, numeric, citation, or structural-integrity defect. "
    "Style is never a required revision: repetitive or template-like phrasing, narrative flow, tone, readability, section ordering, and formatting "
    "belong in minor_issues, even when you find them jarring. A manuscript whose only faults are stylistic has no required_revisions.\n"
    "reject = at least one material finding meets the repairability rule's rejection criteria; explain why bounded edits cannot fix it.\n\n"
)
