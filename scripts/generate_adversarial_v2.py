#!/usr/bin/env python3
"""Generate adversarial_set_v2.json — 100 adversarial fixtures across 10 new categories.

Each category targets a specific failure mode not covered by v1's 9 categories.
Shape matches calibration_set_v1.json exactly.

Usage:
    python3 scripts/generate_adversarial_v2.py
"""

import json
import hashlib
from pathlib import Path
from itertools import product as cart_product

# ---------------------------------------------------------------------------
# Domain / topic matrices
# ---------------------------------------------------------------------------
DOMAINS = ["longevity", "aging", "cardiovascular", "neuroscience", "oncology",
           "metabolic", "respiratory", "gastroenterology", "musculoskeletal",
           "infectious_disease"]

TOPICS = {
    "longevity": (
        "rapamycin and mTOR inhibition",
        "NAD+ supplementation strategies",
        "telomere extension approaches",
        "senolytic drug therapy",
        "caloric restriction mimetics",
    ),
    "aging": (
        "epigenetic reprogramming reversal",
        "proteostasis network maintenance",
        "autophagy modulation in cells",
        "mitochondrial dysfunction repair",
        "oxidative stress reduction",
    ),
    "cardiovascular": (
        "PCSK9 inhibitor outcomes",
        "cardiac fibrosis reversal",
        "blood pressure modulation via NO pathways",
        "atherosclerotic plaque regression",
        "endothelial function restoration",
    ),
    "neuroscience": (
        "amyloid beta clearance mechanisms",
        "neuroinflammation reduction",
        "brain-computer interface safety",
        "cognitive decline prevention",
        "synaptic plasticity restoration",
    ),
    "oncology": (
        "CAR-T cell therapy outcomes",
        "tumor microenvironment remodeling",
        "immunotherapy resistance mechanisms",
        "liquid biopsy accuracy",
        "precision oncology targeting",
    ),
    "metabolic": (
        "GLP-1 receptor agonist effects",
        "insulin sensitization pathways",
        "brown adipose tissue activation",
        "lipid metabolism reprogramming",
        "gut microbiome modulation",
    ),
    "respiratory": (
        "lung surfactant replacement",
        "bronchial inflammation pathways",
        "oxygen therapy optimization",
        "pulmonary fibrosis treatment",
        "airway remodeling prevention",
    ),
    "gastroenterology": (
        "gut barrier integrity restoration",
        "liver regeneration signaling",
        "pancreatic beta cell recovery",
        "microbiome-disease correlations",
        "inflammatory bowel treatment",
    ),
    "musculoskeletal": (
        "cartilage regeneration therapy",
        "muscle hypertrophy signaling",
        "bone density restoration",
        "tendon repair biomaterials",
        "osteoporosis intervention efficacy",
    ),
    "infectious_disease": (
        "mRNA vaccine platform adaptability",
        "antimicrobial resistance mechanisms",
        "pandemic preparedness frameworks",
        "viral evolution tracking",
        "host-pathogen interaction modeling",
    ),
}

# ---------------------------------------------------------------------------
# Source bundle factories
# ---------------------------------------------------------------------------
BASE_DOI_PREFIXES = {
    "longevity": "10.1234/lon",
    "aging": "10.1234/aging",
    "cardiovascular": "10.1234/cardio",
    "neuroscience": "10.1234/neuro",
    "oncology": "10.1234/oncol",
    "metabolic": "10.1234/metab",
    "respiratory": "10.1234/respi",
    "gastroenterology": "10.1234/gastro",
    "musculoskeletal": "10.1234/muscl",
    "infectious_disease": "10.1234/infec",
}

GENERIC_TITLES = {
    "longevity": [
        "Rapamycin longevity effects in mouse models",
        "mTOR pathway inhibition and aging",
        "Senolytic approaches to age-related decline",
        "NAD+ precursor supplementation in aged tissues",
        "Telomere shortening rates across species",
        "Caloric restriction and healthspan",
        "Autophagy induction for age-related disease",
        "Epigenetic clock reversal compounds",
        "Proteostasis maintenance in long-lived organisms",
        "Inflammaging biomarker characterization",
        "Mitochondrial quality control in aging",
        "Glycation product accumulation in aged tissues",
    ],
    "ai": [
        "Scaling laws for large language model training",
        "Emergent capabilities in foundation models",
        "Constitutional AI and preference learning",
        "Mechanistic interpretability of transformer circuits",
        "Reward modeling for RLHF alignment",
        "Chain-of-thought prompting strategies",
        "Multimodal learning architectures",
        "Federated learning privacy guarantees",
        "Adversarial robustness in vision models",
        "Knowledge distillation efficiency",
        "Retrieval-augmented generation quality",
        "Code generation benchmark evaluation",
    ],
}


def _make_source(domain: str, idx: int, prefix: str | None = None) -> dict:
    doi_prefix = prefix or BASE_DOI_PREFIXES.get(domain, "10.1234/gen")
    titles = GENERIC_TITLES.get(domain, GENERIC_TITLES["longevity"])
    return {
        "title": titles[idx % len(titles)],
        "doi": f"{doi_prefix}.{idx:04d}",
        "url": f"https://doi.org/{doi_prefix}.{idx:04d}",
        "year": 2024 + (idx % 3),
        "evidence_type": "primary" if idx % 2 == 0 else "review",
    }


def _make_source_bundle(domain: str, n: int = 12) -> list[dict]:
    return [_make_source(domain, i) for i in range(n)]


# ---------------------------------------------------------------------------
# Section factories — functions that produce the `sections` dict for each category
# ---------------------------------------------------------------------------
MIN_SECTION = 120  # minimum chars per section

def _pad(text: str, target: int = 250) -> str:
    if len(text) >= target:
        return text
    filler = " This aspect requires careful examination of the available evidence base and its methodological quality across included studies."
    while len(text) < target:
        text += filler
    return text[:target + 20]  # allow slight overshoot


def _baseline_sections(topic: str, domain: str) -> dict:
    return {
        "Research Question": _pad(f"What is the current state of evidence for {topic} in {domain}?"),
        "Search Summary": _pad(f"We conducted a systematic search of PubMed, Embase, and Cochrane for studies on {topic} published 2015-2025."),
        "Evidence Landscape": _pad(f"The evidence base for {topic} includes randomized controlled trials, observational studies, and mechanistic work in {domain}."),
        "Methods": _pad(f"Studies were selected using predefined inclusion criteria. Quality was assessed using the Cochrane risk of bias tool for randomized studies."),
        "Key Findings": _pad(f"Current evidence suggests moderate benefit for {topic} outcomes. Effect sizes vary across study designs and populations."),
        "Limitations": _pad(f"Most studies have small sample sizes. Publication bias may affect the overall evidence picture. Heterogeneity in outcome measures limits comparability."),
        "Conclusion": _pad(f"The evidence for {topic} is promising but requires larger confirmatory trials before clinical translation."),
    }


def _bundle_ref(i: int) -> str:
    return f"[bundle:{i}]"


# ---------------------------------------------------------------------------
# Category generators — each returns a list of fixture dicts
# ---------------------------------------------------------------------------

def gen_hallucinated_citation(count: int) -> list[dict]:
    """Key Findings cites sources not present in source_bundle."""
    fixtures = []
    for i in range(count):
        domain = DOMAINS[i % len(DOMAINS)]
        topic = TOPICS[domain][i % len(TOPICS[domain])]
        sections = _baseline_sections(topic, domain)
        # Cite bundle indices that don't exist (12 items in bundle, cite 15, 18, 20)
        sections["Key Findings"] = _pad(
            f"Analysis of {topic} reveals significant effects {_bundle_ref(15)}. "
            f"Subgroup analysis showed benefit {_bundle_ref(18)}. "
            f"Dose-response was observed {_bundle_ref(20)}."
        )
        fixtures.append({
            "title": f"Adversarial-v2: Hallucinated Citation Test {i+1} — {topic} ({domain})",
            "abstract": _pad(f"This review evaluates {topic} in {domain}. We identified multiple studies but some citations reference unavailable sources.", 150),
            "domain_slug": domain,
            "author_agent_id": "adversarial-v2-generator",
            "sections": sections,
            "source_bundle": _make_source_bundle(domain),
            "_calibration_category": "hallucinated_citation",
            "_expected_decision": "reject",
            "_expected_gate_failures": ["citation_out_of_bundle"],
        })
    return fixtures


def gen_self_citation_loop(count: int) -> list[dict]:
    """All source_bundle entries from the same fake research group."""
    fixtures = []
    for i in range(count):
        domain = DOMAINS[i % len(DOMAINS)]
        topic = TOPICS[domain][i % len(TOPICS[domain])]
        sections = _baseline_sections(topic, domain)
        lab = f"Chen et al. Laboratory"
        bundle = []
        for j in range(12):
            bundle.append({
                "title": f"Chen et al.: Study {j+1} on {topic}",
                "doi": f"10.1234/chen.{i:02d}{j:02d}",
                "url": f"https://doi.org/10.1234/chen.{i:02d}{j:02d}",
                "year": 2024 + (j % 3),
                "evidence_type": "primary" if j % 2 == 0 else "review",
            })
        sections["Key Findings"] = _pad(
            f"Chen et al. (2024) demonstrated benefit {_bundle_ref(1)}. "
            f"Chen et al. (2025) replicated findings {_bundle_ref(5)}. "
            f"Chen et al. (2024) confirmed mechanism {_bundle_ref(9)}."
        )
        fixtures.append({
            "title": f"Adversarial-v2: Self-Citation Loop Test {i+1} — {topic} ({domain})",
            "abstract": _pad(f"This review evaluates {topic} in {domain}. All sources originate from a single research group.", 150),
            "domain_slug": domain,
            "author_agent_id": "adversarial-v2-generator",
            "sections": sections,
            "source_bundle": bundle,
            "_calibration_category": "self_citation_loop",
            "_expected_decision": "reject",
            "_expected_gate_failures": ["claim_evidence_alignment", "circular_source_reference"],
        })
    return fixtures


def gen_section_content_mismatch(count: int) -> list[dict]:
    """Research Question and Key Findings discuss completely different topics."""
    fixtures = []
    for i in range(count):
        domain = DOMAINS[i % len(DOMAINS)]
        topic = TOPICS[domain][i % len(TOPICS[domain])]
        # Pick a different domain for the question
        other_domain = DOMAINS[(i + 5) % len(DOMAINS)]
        other_topic = TOPICS[other_domain][i % len(TOPICS[other_domain])]
        sections = _baseline_sections(topic, domain)
        sections["Research Question"] = _pad(
            f"What is the current evidence for {other_topic} in {other_domain}?"
        )
        sections["Key Findings"] = _pad(
            f"Studies on {other_topic} in {other_domain} show mixed results. "
            f"Randomized trials report small effect sizes {_bundle_ref(1)}. "
            f"Observational data are confounded {_bundle_ref(5)}."
        )
        fixtures.append({
            "title": f"Adversarial-v2: Section Mismatch Test {i+1} — {topic} ({domain})",
            "abstract": _pad(f"This review evaluates {topic} in {domain}. However, the research question and findings discuss a completely different topic.", 150),
            "domain_slug": domain,
            "author_agent_id": "adversarial-v2-generator",
            "sections": sections,
            "source_bundle": _make_source_bundle(domain),
            "_calibration_category": "section_content_mismatch",
            "_expected_decision": "reject",
            "_expected_gate_failures": ["claim_evidence_alignment"],
        })
    return fixtures


def gen_duplicate_sources(count: int) -> list[dict]:
    """source_bundle has duplicate entries with slightly different titles."""
    fixtures = []
    for i in range(count):
        domain = DOMAINS[i % len(DOMAINS)]
        topic = TOPICS[domain][i % len(TOPICS[domain])]
        sections = _baseline_sections(topic, domain)
        bundle = _make_source_bundle(domain, 6)  # 6 unique
        # Duplicate each with slight title variation
        duped = []
        for src in bundle:
            duped.append(src)
            duped.append({
                **src,
                "title": src["title"] + " — Extended Analysis",
                "url": src.get("url"),
            })
        sections["Key Findings"] = _pad(
            f"Studies on {topic} demonstrate benefit {_bundle_ref(1)}. "
            f"Replication confirmed effects {_bundle_ref(5)}. "
            f"Meta-analysis supports conclusions {_bundle_ref(9)}."
        )
        fixtures.append({
            "title": f"Adversarial-v2: Duplicate Sources Test {i+1} — {topic} ({domain})",
            "abstract": _pad(f"This review evaluates {topic} in {domain}. Source bundle contains duplicated entries.", 150),
            "domain_slug": domain,
            "author_agent_id": "adversarial-v2-generator",
            "sections": sections,
            "source_bundle": duped,
            "_calibration_category": "duplicate_sources",
            "_expected_decision": "reject",
            "_expected_gate_failures": ["claim_evidence_alignment"],
        })
    return fixtures


def gen_wrong_evidence_type(count: int) -> list[dict]:
    """source_bundle claims 'primary' but titles clearly indicate review/meta-analysis."""
    fixtures = []
    for i in range(count):
        domain = DOMAINS[i % len(DOMAINS)]
        topic = TOPICS[domain][i % len(TOPICS[domain])]
        sections = _baseline_sections(topic, domain)
        bundle = _make_source_bundle(domain)
        # Override evidence_type to "primary" for entries whose titles suggest review
        for j, src in enumerate(bundle):
            if j % 3 == 0:
                src["evidence_type"] = "primary"
                src["title"] = f"Systematic Review and Meta-Analysis of {topic}"
            elif j % 3 == 1:
                src["evidence_type"] = "primary"
                src["title"] = f"Narrative Review: State of {topic} Research"
        fixtures.append({
            "title": f"Adversarial-v2: Wrong Evidence Type Test {i+1} — {topic} ({domain})",
            "abstract": _pad(f"This review evaluates {topic} in {domain}. Evidence types are incorrectly labeled.", 150),
            "domain_slug": domain,
            "author_agent_id": "adversarial-v2-generator",
            "sections": sections,
            "source_bundle": bundle,
            "_calibration_category": "wrong_evidence_type",
            "_expected_decision": "reject",
            "_expected_gate_failures": ["claim_evidence_alignment"],
        })
    return fixtures


def gen_domain_hallucination(count: int) -> list[dict]:
    """Content discusses one domain, domain_slug claims another."""
    fixtures = []
    for i in range(count):
        domain = DOMAINS[i % len(DOMAINS)]
        topic = TOPICS[domain][i % len(TOPICS[domain])]
        claimed_domain = DOMAINS[(i + 3) % len(DOMAINS)]
        sections = _baseline_sections(topic, claimed_domain)  # mismatched
        sections["Research Question"] = _pad(
            f"What is the current state of evidence for {topic}?"
        )
        fixtures.append({
            "title": f"Adversarial-v2: Domain Hallucination Test {i+1} — {topic} ({claimed_domain})",
            "abstract": _pad(f"This review evaluates {topic}. The domain slug claims {claimed_domain} but content is about {domain}.", 150),
            "domain_slug": claimed_domain,
            "author_agent_id": "adversarial-v2-generator",
            "sections": sections,
            "source_bundle": _make_source_bundle(domain),
            "_calibration_category": "domain_hallucination",
            "_expected_decision": "reject",
            "_expected_gate_failures": ["claim_evidence_alignment"],
        })
    return fixtures


def gen_adversarial_overlength(count: int) -> list[dict]:
    """Sections are excessively long with padding and repetition."""
    fixtures = []
    for i in range(count):
        domain = DOMAINS[i % len(DOMAINS)]
        topic = TOPICS[domain][i % len(TOPICS[domain])]
        mega_filler = (
            f"This extensive analysis of {topic} examines every facet of the evidence base "
            f"with rigorous methodological attention to detail across multiple dimensions. "
            f"The findings indicate a complex interplay of factors that merit continued investigation "
            f"and warrant careful consideration by the scientific community. "
        ) * 8
        sections = {
            "Research Question": mega_filler[:400],
            "Search Summary": mega_filler[:400],
            "Evidence Landscape": mega_filler[:400],
            "Methods": mega_filler[:400],
            "Key Findings": mega_filler[:400],
            "Limitations": mega_filler[:400],
            "Conclusion": mega_filler[:400],
        }
        fixtures.append({
            "title": f"Adversarial-v2: Overlength Test {i+1} — {topic} ({domain})",
            "abstract": mega_filler[:200],
            "domain_slug": domain,
            "author_agent_id": "adversarial-v2-generator",
            "sections": sections,
            "source_bundle": _make_source_bundle(domain),
            "_calibration_category": "adversarial_overlength",
            "_expected_decision": "reject",
            "_expected_gate_failures": ["structure_gate"],
        })
    return fixtures


def gen_stub_entry(count: int) -> list[dict]:
    """Sections exist but contain placeholder/boilerplate text."""
    fixtures = []
    placeholders = [
        "[INSERT FINDINGS HERE]",
        "TODO: Add evidence summary",
        "Lorem ipsum dolor sit amet, consectetur adipiscing elit, sed do eiusmod tempor incididunt ut labore et dolore magna aliqua. Ut enim ad minim veniam.",
        "Section pending review. Content will be updated upon completion of data extraction.",
        "TBD — see supplementary materials for complete analysis.",
    ]
    for i in range(count):
        domain = DOMAINS[i % len(DOMAINS)]
        topic = TOPICS[domain][i % len(TOPICS[domain])]
        ph = placeholders[i % len(placeholders)]
        # Pad placeholders to pass length check but keep them obviously fake
        def stub(sec: str) -> str:
            return _pad(f"{sec}: {ph}")
        sections = {
            "Research Question": stub(f"Research Question for {topic}"),
            "Search Summary": stub(f"Search Summary for {topic}"),
            "Evidence Landscape": stub(f"Evidence Landscape for {topic}"),
            "Methods": stub(f"Methods for {topic}"),
            "Key Findings": stub(f"Key Findings for {topic}"),
            "Limitations": stub(f"Limitations for {topic}"),
            "Conclusion": stub(f"Conclusion for {topic}"),
        }
        fixtures.append({
            "title": f"Adversarial-v2: Stub Entry Test {i+1} — {topic} ({domain})",
            "abstract": stub(f"Abstract for {topic}"),
            "domain_slug": domain,
            "author_agent_id": "adversarial-v2-generator",
            "sections": sections,
            "source_bundle": _make_source_bundle(domain),
            "_calibration_category": "stub_entry",
            "_expected_decision": "reject",
            "_expected_gate_failures": ["sanitizer_placeholder_detected"],
        })
    return fixtures


def gen_garbled_citations(count: int) -> list[dict]:
    """[bundle:N] references that don't match source_bundle structure."""
    fixtures = []
    garbled_refs = [
        "[bundle:abc]",
        "[bundle:-1]",
        "[bundle:99999]",
        "[bundle:]",
        "[bundle]",
        "[ref:1]",
        "(see source 7)",
        "bundle:3",
        "[bundle:1.5]",
        "[bundle: 7]",
    ]
    for i in range(count):
        domain = DOMAINS[i % len(DOMAINS)]
        topic = TOPICS[domain][i % len(TOPICS[domain])]
        sections = _baseline_sections(topic, domain)
        ref = garbled_refs[i % len(garbled_refs)]
        sections["Key Findings"] = _pad(
            f"Analysis of {topic} shows benefit {ref}. "
            f"Subgroup analysis confirms effects {ref}. "
            f"Dose-response relationship was observed {ref}."
        )
        fixtures.append({
            "title": f"Adversarial-v2: Garbled Citation Test {i+1} — {topic} ({domain})",
            "abstract": _pad(f"This review evaluates {topic} in {domain}. Citation references are malformed.", 150),
            "domain_slug": domain,
            "author_agent_id": "adversarial-v2-generator",
            "sections": sections,
            "source_bundle": _make_source_bundle(domain),
            "_calibration_category": "garbled_citations",
            "_expected_decision": "reject",
            "_expected_gate_failures": ["citation_out_of_bundle"],
        })
    return fixtures


def gen_mixed_hallucination(count: int) -> list[dict]:
    """Combination of multiple adversarial patterns in a single entry."""
    fixtures = []
    for i in range(count):
        domain = DOMAINS[i % len(DOMAINS)]
        topic = TOPICS[domain][i % len(TOPICS[domain])]
        other_domain = DOMAINS[(i + 4) % len(DOMAINS)]
        other_topic = TOPICS[other_domain][i % len(TOPICS[other_domain])]
        sections = _baseline_sections(topic, domain)
        # Combine: hallucinated citations + content mismatch + wrong evidence type
        sections["Research Question"] = _pad(
            f"What is the evidence for {other_topic} in {other_domain}?"
        )
        sections["Key Findings"] = _pad(
            f"Studies show benefit {_bundle_ref(15)}. "
            f"Chen et al. replicated {_bundle_ref(25)}. "
            f"Meta-analysis confirms {_bundle_ref(42)}."
        )
        bundle = _make_source_bundle(domain)
        for src in bundle:
            src["evidence_type"] = "primary"  # force all to primary
            src["title"] = f"Systematic Review of {topic}"
        fixtures.append({
            "title": f"Adversarial-v2: Mixed Hallucination Test {i+1} — {topic} ({domain})",
            "abstract": _pad(f"This review evaluates {topic} in {domain}. Multiple adversarial patterns are combined.", 150),
            "domain_slug": domain,
            "author_agent_id": "adversarial-v2-generator",
            "sections": sections,
            "source_bundle": bundle,
            "_calibration_category": "mixed_hallucination",
            "_expected_decision": "reject",
            "_expected_gate_failures": [
                "claim_evidence_alignment",
                "citation_out_of_bundle",
            ],
        })
    return fixtures


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------
def validate_fixtures(fixtures: list[dict]) -> list[str]:
    errors = []
    for i, f in enumerate(fixtures):
        prefix = f"Entry {i} ({f.get('_calibration_category','?')}): "
        if not f.get("title"):
            errors.append(f"{prefix}missing title")
        if not f.get("abstract"):
            errors.append(f"{prefix}missing abstract")
        if not f.get("domain_slug"):
            errors.append(f"{prefix}missing domain_slug")
        if not f.get("sections"):
            errors.append(f"{prefix}missing sections")
        else:
            expected_secs = {"Research Question", "Search Summary", "Evidence Landscape",
                             "Methods", "Key Findings", "Limitations", "Conclusion"}
            missing = expected_secs - set(f["sections"].keys())
            if missing:
                errors.append(f"{prefix}missing sections: {missing}")
            for sec, text in f["sections"].items():
                if len(text) < MIN_SECTION:
                    errors.append(f"{prefix}section '{sec}' too short ({len(text)} < {MIN_SECTION})")
        if not f.get("source_bundle"):
            errors.append(f"{prefix}missing source_bundle")
        elif len(f["source_bundle"]) < 1:
            errors.append(f"{prefix}source_bundle is empty")
        for key in ("_calibration_category", "_expected_decision", "_expected_gate_failures"):
            if key not in f:
                errors.append(f"{prefix}missing {key}")
    return errors


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    generators = [
        ("hallucinated_citation", gen_hallucinated_citation),
        ("self_citation_loop", gen_self_citation_loop),
        ("section_content_mismatch", gen_section_content_mismatch),
        ("duplicate_sources", gen_duplicate_sources),
        ("wrong_evidence_type", gen_wrong_evidence_type),
        ("domain_hallucination", gen_domain_hallucination),
        ("adversarial_overlength", gen_adversarial_overlength),
        ("stub_entry", gen_stub_entry),
        ("garbled_citations", gen_garbled_citations),
        ("mixed_hallucination", gen_mixed_hallucination),
    ]

    all_fixtures = []
    cat_counts = {}
    for name, gen in generators:
        entries = gen(10)
        cat_counts[name] = len(entries)
        all_fixtures.extend(entries)

    print(f"Generated {len(all_fixtures)} adversarial fixtures:")
    for cat, cnt in cat_counts.items():
        print(f"  {cat}: {cnt}")

    errors = validate_fixtures(all_fixtures)
    if errors:
        print(f"\n❌ {len(errors)} validation errors:")
        for e in errors:
            print(f"  {e}")
    else:
        print(f"\n✅ All {len(all_fixtures)} entries pass validation")

    out = Path(__file__).parent.parent / "calibration" / "adversarial_set_v2.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w") as f:
        json.dump(all_fixtures, f, indent=2)
    print(f"\nWritten to {out} ({len(all_fixtures)} entries)")


if __name__ == "__main__":
    main()
