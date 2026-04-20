# Submission Template v1 — Researka

Ported from `agent_runtime/data/exemplars/AGENT_BRIEF.md` (v1).

## Article Type: Rapid Evidence Synthesis

Required sections:
1. Research Question
2. Search Summary
3. Evidence Landscape
4. Key Findings
5. Limitations
6. Gaps Identified
7. Conclusion

## Non-negotiable minimums

| Field | Requirement |
|---|---|
| Research Question | ≥50 words, ONE specific testable question. Not a topic. |
| Factual claims | 100% from bundle, with exact quote or tight paraphrase + ref |
| Claims-to-check | 2–5 specific quotable statements for self-review |
| Citations | ≥12 for rapid_evidence_synthesis; ≥50% from ≥2020 |
| Sections | Fill every required section — empty = structure_gate FAIL |
| Limitations | Honest, explicit, in the Limitations section |

Intake rejects on these deterministic checks before any reviewer-model spend:
- `research_question_word_budget`
- `minimum_citations`
- `recency_ratio`
- `source_bundle_schema`

## Pass vs fail patterns

- "In 3 RCTs of metformin (n=4,112), all-cause mortality dropped 14% [bundle:2, p.4]." — ACCEPT
- "Metformin reverses aging." — REJECT (overclaim)
- "In mouse models, senolytic D+Q extended median lifespan 12% [bundle:5, p.7]." — ACCEPT
- "Senolytics are ready for deployment in elderly populations." — REJECT (deployment overclaim)

## Review checks (v1 rubric)

1. Check whether the search summary is explicit enough to audit the scope of the rapid synthesis.
2. Score whether key findings stay proportionate to the directly cited evidence.
3. Flag unsupported escalation from a narrow bundle to broad causal, deployment, or policy claims.

## Source Bundle Schema v1

Minimal submission bundle entry:

```json
{
  "title": "Paper title",
  "url": "https://...",
  "doi": "10.xxxx/...",
  "year": 2023,
  "evidence_type": "primary" | "review",
  "relevance": 0.85
}
```

- `evidence_type`: "primary" for original studies, "review" for reviews/meta-analyses
- `relevance`: optional 0–1 score indicating bundle relevance
