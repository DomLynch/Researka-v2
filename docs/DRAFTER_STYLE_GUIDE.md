# Drafter Style Guide — Researka v2

_Reverse-engineered from live publications and agent draft outputs. Applies to all drafter prompts._

---

## 1. Document Structure

A Researka synthesis has exactly 7 sections, each under an `## ` markdown heading. The section order is fixed:

1. Research Question
2. Search Summary
3. Evidence Landscape
4. Key Findings
5. Limitations
6. Gaps Identified
7. Conclusion

**There is no Methods section.** If your drafter prompt produces one, remove it. The live pipeline deletes Methods before publication. Do not waste drafter tokens generating it.

The title and abstract are separate metadata fields, not part of the body.

### Section length targets

| Section | Target words | Minimum chars (validation) |
|---------|-------------|---------------------------|
| Research Question | 60–100 | 120 |
| Search Summary | 40–80 | 120 |
| Evidence Landscape | 50–90 | 120 |
| Key Findings | 100–180 | 120 |
| Limitations | 60–120 | 120 |
| Gaps Identified | 50–100 | 120 |
| Conclusion | 50–100 | 120 |

Total body (title excluded): **400–600 words**. Benchmark papers that pass all gates average ~514 words (see IF publication). Benchmark templates that fail gate checks average ~260 words — too thin.

---

## 2. Tone Rules

### The golden standard

Read the live IF publication: "Does intermittent fasting improve cardiometabolic markers in adults with obesity?" This is the single reference point for acceptable output. Every drafter should produce work indistinguishable from it.

### DO

- **State facts directly.** "Intermittent fasting produces statistically significant reductions in fasting glucose." Not: "There is some evidence suggesting intermittent fasting may contribute to improvements in fasting glucose."
- **Name numbers.** "effect sizes ranging from 5 to 15 mg/dL." Not: "effect sizes that varied across studies."
- **Name databases, protocols, populations.** "PubMed, Cochrane CENTRAL, and ClinicalTrials.gov." Not: "major databases."
- **Use specific counts.** "Fourteen studies met inclusion criteria; twelve provided extractable data." Not: "Several studies were identified."
- **Separate claims by evidence strength.** "Evidence for LDL cholesterol reduction is weaker and less consistent across studies." Shows you know what's strong and what isn't.
- **End Limitations and Gaps with concrete details, not abstractions.** "Populations studied are predominantly from high-income countries limiting generalizability." Not: "Generalizability may be limited."

### DON'T

- **No hedging language as filler.** "may," "could," "potentially," "it is possible that" — use these only when uncertainty is genuinely warranted, not as a safety blanket.
- **No meta-commentary.** "This synthesis examined..." or "The evidence was reviewed..." — the reader already knows. State your findings.
- **No passive construction unless necessary.** "The evidence supports..." not "It is supported by the evidence that..."
- **No softening of established findings.** If 6 of 8 trials agree, say "six of eight trials." Don't say "most trials suggest."
- **No empty transitions.** "Furthermore," "Additionally," "Moreover" — cut them. Start with the claim.

---

## 3. Sentence Patterns

### Research Question

**Pattern:** Single compound sentence specifying PICO elements (Population, Intervention, Comparison, Outcome), date range, inclusion criteria.

> _Good:_ "This submission examines whether 2023-2026 evidence from randomized controlled trials demonstrates that intermittent fasting protocols improve cardiometabolic markers including fasting glucose, HbA1c, LDL cholesterol, triglycerides, and blood pressure in adults with obesity defined as BMI greater than 30, compared to either standard caloric restriction or ad-libitum control diets, with a minimum intervention duration of 12 weeks and at least one primary cardiometabolic endpoint measured."

> _Bad (from benchmark template):_ "Among recent studies on heat resilience, does the evidence support implementing targeted heat resilience strategies to improve measurable outcomes in relevant populations?"

The bad version is generic — swap "heat resilience" for any topic and it reads the same. The good version names specific endpoints, comparators, populations, and duration criteria.

### Search Summary

**Pattern:** State databases, date range, population filter, inclusion/exclusion rules, outcome count.

> _Good:_ "PubMed, Cochrane CENTRAL, and ClinicalTrials.gov were searched for 2023-2026 randomized controlled trials of intermittent fasting in adults with BMI greater than 30. Inclusion required at least one primary cardiometabolic endpoint measured at 12 weeks or longer."

> _Bad:_ "The search prioritized 2024-2026 peer-reviewed studies on [topic]. Evidence was included when it directly addressed [topic] efficacy, safety, or implementation."

### Evidence Landscape

**Pattern:** Classify sources by type, state counts, note direction of evidence (convergent, mixed, sparse).

> _Good:_ "The evidence base consists of twelve sources: six systematic reviews and meta-analyses synthesizing data from multiple RCTs across 2023-2026, and six primary randomized controlled trials reporting individual trial outcomes."

### Key Findings

**Pattern:** One claim per sentence. Each claim names the finding, the evidence count, and a specific effect size or direction. Use `[bundle:N]` citations when available.

> _Good:_ "Intermittent fasting produces statistically significant reductions in fasting glucose compared to ad-libitum controls across six of eight trials reporting this endpoint, with effect sizes ranging from 5 to 15 mg/dL."

> _Bad (from benchmark template):_ "First, [topic] remains a credible intervention target with consistent signals across studies."

The bad version is a template. Fill it with any topic and it's still empty. The good version would not pass if you swapped "intermittent fasting" for "coral restoration" — it's topic-specific.

### Limitations

**Pattern:** Named limitation, one sentence per limitation. State what it constrains.

> _Good:_ "Most trials have sample sizes between 30 and 120 participants limiting statistical power for subgroup analyses. Blinding is impossible for dietary interventions introducing performance and detection bias."

### Gaps Identified

**Pattern:** Specific gap, one sentence per gap. State what is missing and why it matters.

> _Good:_ "Head-to-head comparison between different intermittent fasting protocols for specific cardiometabolic endpoints is absent."

> _Bad:_ "No generalized human randomized evidence yet shows broad benefit from [topic], and the field still lacks stable biomarkers and safety-calibrated trials."

### Conclusion

**Pattern:** Synthesize findings → state defensibility → name constraints. 3-4 sentences max.

> _Good:_ "Current evidence from randomized controlled trials indicates that intermittent fasting produces modest but consistent improvements in fasting glucose, triglycerides, and insulin sensitivity in adults with obesity compared to ad-libitum eating patterns."

---

## 4. Citation Conventions

When the drafter has access to a `source_bundle`, use `[bundle:N]` notation inline in Key Findings only. N is the 1-based index into the bundle array.

Example from a real draft:
```
EVsABPC administration reversed bone loss in aged mice and rhesus macaques [bundle:1] and attenuated cellular senescence phenotypes in vitro through unique factors [bundle:1]. Extracellular vesicles are emerging as therapeutic candidates for aging across multiple studies [bundle:3][bundle:5][bundle:7].
```

**Rules:**
- Citations go in Key Findings only. Other sections are synthesis — they reference the evidence landscape, not individual papers.
- Use `[bundle:1]` not `[1]` or `([bundle:1])`.
- Multiple sources for one claim: `[bundle:1][bundle:3][bundle:5]` (no commas, no spaces).
- One citation per sentence is sufficient; don't decorate every clause.

---

## 5. Common Failures from Agent Drafts

These are patterns observed in 10 real agent-generated drafts that passed gates but are stylistically weak:

| Problem | Example | Fix |
|---------|---------|-----|
| Methods section included | Draft has 8 sections including Methods | Delete Methods entirely. Pipeline expects 7 sections. |
| Search Summary too thin | "Systematic search of 2025-2026 literature identified 12 relevant studies" | Name the databases. State the population filter. State inclusion/exclusion rules. |
| Evidence Landscape too generic | "Recent literature shows increasing focus on..." | Classify source types with counts. "The evidence base consists of twelve sources: six systematic reviews... and six primary RCTs." |
| Key Findings lacks specifics | "promising results for bone regeneration" | Name the effect. "reversed bone loss in aged mice and rhesus macaques with statistically significant improvements in trabecular bone density." |
| Limitations too abstract | "Findings are restricted to animal models with uncertain human translatability" | Name the specific constraint. "animal models (mice and non-human primates) with no human clinical data available." |
| Gaps repeats Limitations | "Human clinical data are absent" appears in both Limitations and Gaps | Limitations = what's weak in current evidence. Gaps = what's completely missing from the field. Don't repeat. |
| Template language | "remains a credible intervention target with consistent signals" | Delete template phrases. State what the specific evidence actually shows. |

---

## 6. Quality Checklist for Drafter Prompts

Before sending a draft through the pipeline, verify:

- [ ] Exactly 7 sections, no Methods
- [ ] Research Question is one compound sentence with PICO + date range + criteria
- [ ] Search Summary names at least 2 databases and specifies population filter
- [ ] Evidence Landscape classifies sources by type with counts
- [ ] Key Findings has at least 3 specific claims with effect sizes or directions
- [ ] Limitations has at least 3 named limitations, each constraining a specific analysis
- [ ] Gaps Identified has at least 2 gaps not repeated from Limitations
- [ ] Conclusion synthesizes findings without introducing new evidence
- [ ] Total body is 400-600 words
- [ ] No hedging filler ("may," "could," "potentially" only when genuinely warranted)
- [ ] No template phrases ("remains a credible target," "consistent signals across studies")
- [ ] Citations use `[bundle:N]` notation in Key Findings only

---

## 7. Side-by-Side Comparison

### Good — passes all gates (from live IF publication)

**Key Findings:**
> Intermittent fasting produces statistically significant reductions in fasting glucose compared to ad-libitum controls across six of eight trials reporting this endpoint, with effect sizes ranging from 5 to 15 mg/dL. Triglycerides show consistent improvement in seven of nine trials. HbA1c reductions are modest but significant in trials lasting 24 weeks or longer. Blood pressure improvements are present but smaller in magnitude. LDL cholesterol results are mixed with some trials showing improvement and others showing no change or slight increase.

### Bad — passes gates but is generic (from benchmark template)

**Key Findings:**
> First, heat resilience remains a credible intervention target with consistent signals across studies. Second, the center of gravity has shifted toward precision approaches. Third, new evidence cuts both ways with both supportive and cautionary findings. Fourth, population-level proof remains limited in the current evidence window.

The difference: swap "heat resilience" for "coral restoration" in the bad version and it still reads perfectly. The good version is locked to its topic — you cannot swap it.

---

_Last updated: 2026-04-22. Source data: 23 live VPS publications, 10 agent drafts, 64 elite benchmark entries._
