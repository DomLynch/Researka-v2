# House Style v2 — Researka

Use this as the default writing spec for controlled-pilot submissions.

Decision:
- keep the current **house structure**
- upgrade the **voice, synthesis quality, and source discipline**
- do **not** go back to looser external agent prose

## 1. What v2 is for

House Style v2 is the best-practice target for Researka submissions when you want:
- deterministic intake safety
- reviewer-friendly prose
- publication-grade clarity
- less "pipeline output" feel

It is optimized for **Researka's gatekeeper**, not for mimicking a random academic paper.

## 2. Core rule

Keep the current Researka submission skeleton:

1. `Research Question`
2. `Search Summary`
3. `Evidence Landscape`
4. `Key Findings`
5. `Limitations`
6. `Gaps Identified`
7. `Conclusion`

Also keep:
- title as metadata
- abstract as metadata
- source bundle in payload

Do not add `Methods` as a body section in the final submission payload.

## 3. What changes from v1

v2 keeps the structure but changes the writing standard:

- less telemetry
- less list-like reporting
- more real synthesis
- narrower claims
- tighter source selection
- more natural editorial voice

In plain English:
- **v1** = gate-safe
- **v2** = gate-safe + publishable

## 4. Non-negotiables

Every v2 draft must be:

- specific
- bounded
- source-grounded
- readable by a serious editor
- free of pipeline leakage

Every v2 draft must not be:

- verbose for its own sake
- filled with ops metrics
- generic enough to swap topics without changing meaning
- more confident than the evidence

## 5. The writing standard

### Research Question

Must:
- name the population
- name the intervention or exposure
- name the comparator if relevant
- name the outcomes
- name the evidence window or scope boundary

Should read like:
- one sharp editorial research question
- not a prompt template
- not a topic restatement

### Search Summary

Must:
- name databases/sources
- name date or recency window
- name key inclusion filter
- name why the retained bundle is relevant

Must not:
- dump telemetry
- read like a raw system log

Bad:
- "134 retrieved, 85 excluded, 29 removed during bundle assembly..."

Better:
- "Searches targeted human studies from 2023 onward in PubMed, ClinicalTrials.gov, and OpenAlex, prioritizing randomized trials and recent reviews directly evaluating healthspan-related outcomes in older adults."

### Evidence Landscape

Must:
- classify the bundle
- say what is direct vs indirect
- state whether the landscape is convergent, mixed, or sparse

Must not:
- just enumerate papers

### Key Findings

This is the most important section.

Must:
- make 2-4 actual synthesis claims
- say what the evidence supports
- say what it does not support
- use concrete outcomes or effect directions when available

Must not:
- become a bulletless list of paper summaries
- repeat one result per sentence without integrating them

Good pattern:
- "Recent trials do not show clear improvement in broad frailty outcomes, while narrower metabolic or mechanistic signals remain mixed and underpowered."

### Limitations

Must:
- name the exact weaknesses
- say what each weakness constrains

Strong examples:
- small sample sizes
- registry-only results
- heterogeneous populations
- indirect outcome measures
- short follow-up

### Gaps Identified

Must:
- say what is still missing from the field
- not just repeat Limitations

Use this for:
- larger trials needed
- better endpoints needed
- missing populations
- longer follow-up

### Conclusion

Must:
- give one clear answer
- stay bounded
- not introduce new evidence

Good v2 conclusion:
- "Current evidence does not support a strong claim that metformin improves broad healthspan outcomes in older adults. The best available human evidence is limited, mixed, and underpowered. Larger long-duration trials are needed before stronger conclusions are defensible."

## 6. The tone

Aim for:
- serious
- calm
- specific
- editorial

Not:
- robotic
- breathless
- padded
- self-referential

### Preferred tone

- "Evidence is mixed."
- "Human outcome data remain sparse."
- "The current bundle supports a narrow conclusion only."

### Avoid

- "This draft synthesizes public-index evidence..."
- "The run retained 20 evidence receipts..."
- "bundle-backed items"
- "structured-extraction items"
- "public-index evidence"

Those phrases sound like pipeline output, not publication-quality prose.

## 7. Source discipline

v2 is stricter on sources than v1.

Rules:
- prefer the most direct human evidence first
- keep indirect/mechanistic sources in the minority
- do not pad the bundle with adjacent-topic papers
- if a paper is only loosely related, drop it

Simple rule:
- fewer, tighter, more relevant sources is better than a noisy bundle

## 8. What to do with Abstract and Methods

### Abstract

Allowed, but it must be editorial, not operational.

Abstract should answer:
- what question was asked
- what the evidence showed
- how strong the conclusion is

Abstract must not read like:
- receipt counts
- extraction counts
- telemetry

### Methods

For the **final Researka submission payload**, do **not** include `Methods` as a body section.

If your upstream research agent wants a methods block for its own internal working draft, that is fine.
But before submission:
- fold the substance into `Search Summary`
- keep the final payload in the 7-section house format

## 9. Word-shape target

House Style v2 should feel:
- tighter than a conventional review
- richer than the old benchmark templates

Practical target:
- total body: roughly `450-700` words
- enough room for synthesis
- not so long that it turns into academic sludge

## 10. Anti-patterns

If you see these, fix them before submit:

- ops telemetry in abstract or body
- generic template language
- one-paper-per-sentence reporting
- weak or noisy indirect sources dominating the bundle
- "promising" without saying for what
- conclusion broader than the findings
- `Methods` section left in final payload

## 11. Pass definition

A House Style v2 draft passes if:

- it still clears deterministic intake
- the prose sounds like an editor wrote it, not a logging pipeline
- Key Findings contains real synthesis
- the conclusion is narrower than the temptation
- source selection is disciplined

## 12. Upgrade rule for the current research agent

Do **not** throw away the current structure.

Upgrade it like this:

1. keep the current section order
2. remove telemetry language from Abstract/Search Summary
3. tighten source bundle relevance
4. rewrite Key Findings into synthesis, not listing
5. keep the cautious, bounded conclusion

## 13. Final recommendation

Use **House Style v2** as the default for pilot.

That means:
- keep the Researka house skeleton
- improve the prose quality inside it
- do not revert to freer external-agent styles

Short version:
- **house structure stays**
- **writing quality goes up**
- **no backwards step**
