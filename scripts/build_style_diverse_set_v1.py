#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from copy import deepcopy
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.generate_benchmark_v3 import compress_to_target, expand_conclusion, expand_gaps, expand_rq
from scripts.run_benchmark import DOMAINS, EXPECTED_BY_QUALITY, _build_source_bundle, _make_sections

STYLE_ORDER = ("house", "terser", "verbose", "external")
QUALITY_PLAN = (("high", 15), ("medium", 15), ("low", 15), ("broken", 5))
VERBOSE_APPENDIX = {
    "Search Summary": " The synthesis also notes how screening decisions narrowed adjacent mechanistic literature so the retained bundle stays aligned to the stated question rather than drifting into background context.",
    "Evidence Landscape": " Across the bundle, the evidentiary signal is described in a more narrative register, but the manuscript still differentiates direct outcome evidence from supporting context and avoids treating volume as proof.",
    "Key Findings": " The narrative is intentionally fuller than house style, yet each sentence still traces back to the retained evidence rather than padding the manuscript with unsupported interpretive leaps.",
    "Limitations": " Even in a longer narrative style, the limitations remain load-bearing: they constrain scope, generalizability, and the force of any downstream operational claim.",
    "Gaps Identified": " The expanded prose is meant to surface the unresolved questions explicitly, not to create the illusion that more words mean more evidence.",
    "Conclusion": " The conclusion can therefore stay longer without becoming broader, because it still closes on the same bounded claim supported by the retained bundle.",
}


def build_base_entry(*, paper_id: int, domain: str, quality: str, bundle_size: int) -> dict:
    return {
        "title": f"Benchmark Paper #{paper_id}: {domain.replace('-', ' ')} evidence synthesis",
        "abstract": (
            f"This Rapid Evidence Synthesis examines whether current evidence supports targeted "
            f"{domain.replace('-', ' ')} strategies, using a bounded source bundle and an explicit question."
        ),
        "domain_slug": domain,
        "author_agent_id": f"style-benchmark-agent-{paper_id}",
        "sections": _make_sections(domain, quality, paper_id),
        "source_bundle": _build_source_bundle(domain, bundle_size, paper_id),
        "_benchmark_quality": quality,
        "_benchmark_bundle_size": bundle_size,
        "_benchmark_paper_id": paper_id,
        "_benchmark_expected_decision": EXPECTED_BY_QUALITY.get(quality, "revise"),
    }


def _tighten(text: str, *, section: str, entry: dict) -> str:
    targets = {
        "Research Question": (220, 320),
        "Search Summary": (160, 220),
        "Evidence Landscape": (170, 240),
        "Key Findings": (220, 320),
        "Limitations": (170, 240),
        "Gaps Identified": (160, 220),
        "Conclusion": (170, 240),
    }
    low, high = targets[section]
    compressed = compress_to_target(text, low, high)
    if section == "Research Question" and len(compressed.split()) < 50:
        compressed = expand_rq(compressed, entry, cap=max(high, 430))
        while len(compressed.split()) < 50:
            compressed = compressed.rstrip(".") + (
                " The synthesis keeps the scope bounded to the retained populations, outcomes, and comparison frame."
            )
    if section == "Gaps Identified" and len(compressed) < 120:
        compressed = expand_gaps(compressed, cap=high)
    if section == "Conclusion" and len(compressed) < 120:
        compressed = expand_conclusion(compressed, cap=high)
    return compressed


def _expand(text: str, *, section: str) -> str:
    if section == "Gaps Identified":
        text = expand_gaps(text, cap=430)
        text = text.rstrip(".") + " These open questions remain central even when the prose is intentionally fuller."
    elif section == "Conclusion":
        text = expand_conclusion(text, cap=460)
        text = text.rstrip(".") + " In this verbose style, the added cadence should still not be mistaken for added support."
    elif section == "Research Question":
        text = text.rstrip(".") + (
            " The framing also makes explicit that the manuscript should privilege directly measured outcomes, bounded conclusions, and transparent uncertainty over broad speculative interpretation."
        )
    else:
        text = text.rstrip(".") + VERBOSE_APPENDIX.get(section, "")
    return text


def _externalize(text: str, *, section: str) -> str:
    replacements = {
        "The evidence supports": "The present synthesis indicates",
        "The evidence base": "The reviewed literature",
        "This Rapid Evidence Synthesis": "This manuscript",
        "The conclusion": "In conclusion",
        "The search": "The review process",
        "The manuscript": "The submission",
    }
    out = text
    for old, new in replacements.items():
        out = out.replace(old, new)
    if section == "Search Summary":
        out = "This review process identifies the retained studies and describes the selection logic before interpreting the evidence. " + out
    elif section == "Evidence Landscape":
        out = "Taken together, the reviewed literature spans multiple evidence types and does not rely on a single paper to carry the central claim. " + out
    elif section == "Key Findings":
        out = "Across the included evidence, the substantive signal can be stated directly without assuming that stylistic brevity or stylistic formality changes what the bundle actually shows. " + out
    elif section == "Limitations":
        out = "These constraints should be read as operative limits on inference rather than routine caveats. " + out
    elif section == "Gaps Identified":
        out = "Several unresolved questions remain before the field can justify broader deployment claims. " + out
    elif section == "Conclusion":
        out = "Overall, the manuscript remains bounded to the retained evidence and avoids treating stylistic polish as a substitute for support. " + out
    return out


def apply_style(entry: dict, style: str) -> dict:
    styled = deepcopy(entry)
    styled["_style_tag"] = style
    styled["_benchmark_source"] = f"style_diverse:{style}"
    if style == "house" or styled["_benchmark_quality"] == "broken":
        return styled

    sections = {}
    for name, text in styled["sections"].items():
        if style == "terser":
            sections[name] = _tighten(text, section=name, entry=styled)
        elif style == "verbose":
            sections[name] = _expand(text, section=name)
        elif style == "external":
            sections[name] = _externalize(text, section=name)
        else:
            sections[name] = text
    styled["sections"] = sections

    if styled["_benchmark_quality"] == "medium" and style in {"verbose", "external"}:
        styled["sections"]["Limitations"] = styled["sections"]["Limitations"].rstrip(".") + (
            " One materially weak dimension still needs bounded revision before the manuscript is publication-ready."
        )
        styled["sections"]["Conclusion"] = styled["sections"]["Conclusion"].rstrip(".") + (
            " Taken together, the current version is closer to revise than accept because at least one support dimension remains partial."
        )

    if style == "terser":
        styled["abstract"] = compress_to_target(styled["abstract"], 120, 220)
    elif style == "verbose":
        styled["abstract"] = styled["abstract"].rstrip(".") + (
            " The prose is intentionally fuller, but the scope remains bounded to directly retained evidence and measured outcomes."
        )
    elif style == "external":
        styled["abstract"] = (
            "This manuscript evaluates a bounded evidence bundle using an academic synthesis style that differs from Researka house cadence while preserving the same underlying claim. "
            + styled["abstract"]
        )
    return styled


def build_style_diverse_set() -> list[dict]:
    papers: list[dict] = []
    global_id = 1
    domain_cursor = 0
    bundle_sizes = [12, 14, 16, 20]
    for style in STYLE_ORDER:
        style_index = STYLE_ORDER.index(style)
        for quality, count in QUALITY_PLAN:
            for slot in range(count):
                domain = DOMAINS[domain_cursor % len(DOMAINS)]
                bundle_size = bundle_sizes[(slot + style_index) % len(bundle_sizes)]
                base = build_base_entry(
                    paper_id=global_id,
                    domain=domain,
                    quality=quality,
                    bundle_size=bundle_size,
                )
                papers.append(apply_style(base, style))
                global_id += 1
                domain_cursor += 1
    return papers


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the style-diverse benchmark corpus")
    parser.add_argument("--output", default="calibration/style_diverse_set_v1.json")
    args = parser.parse_args()

    papers = build_style_diverse_set()
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(papers, indent=2))
    print(f"Wrote {len(papers)} entries to {path}")


if __name__ == "__main__":
    main()
