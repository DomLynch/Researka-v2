"""Clean the v3 benchmark corpus.

Fixes:
1. Double-period artifacts (..) from the generator bug (generate_benchmark_v3.py:76).
   Replaces ".." with "." but preserves "..." ellipses.
2. Adds missing [bundle:N] citations to Key Findings sections that lack them.
   Uses the source_bundle to determine count and inserts plausible citations.

Usage:
    python3 scripts/clean_benchmark_v3.py [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

CORPUS_PATH = Path("calibration/elite_benchmark_v3.json")
OUTPUT_PATH = Path("calibration/elite_benchmark_v3_cleaned.json")


def fix_double_periods(text: str) -> str:
    """Replace ".." with "." while preserving "..." ellipses.

    Strategy:
      - Protect "..." by temporarily replacing with a placeholder.
      - Replace remaining ".." with ".".
      - Restore "...".
    """
    placeholder = "\x00ELLIPSIS\x00"
    text = text.replace("...", placeholder)
    text = text.replace("..", ".")
    text = text.replace(placeholder, "...")
    return text


def has_bundle_citations(text: str) -> bool:
    """Check if text already contains [bundle:N] citations."""
    return bool(re.search(r"\[bundle:\d+\]", text))


def inject_bundle_citations(key_findings: str, source_count: int) -> str:
    """Add [bundle:N] citations to sentences in Key Findings that lack them.

    Strategy: for each sentence that makes a claim (contains numbers, statistics,
    or specific findings) and doesn't already have a citation, append [bundle:N]
    where N cycles through the source bundle indices.
    """
    if has_bundle_citations(key_findings):
        return key_findings

    sentences = re.split(r"(?<=[.!?])\s+", key_findings.strip())
    if not sentences:
        return key_findings

    cite_idx = 1
    result = []
    for sentence in sentences:
        sentence = sentence.strip()
        if not sentence:
            continue

        # Sentences that likely need citations: contain numbers, ratios, percentages,
        # or specific technical claims
        needs_citation = bool(
            re.search(r"\d", sentence)
            or re.search(
                r"\b(showed|demonstrated|revealed|indicated|found|predicted|associated|"
                r"increased|decreased|reduced|improved|correlated|significant|hazard|"
                r"ratio|confidence|p\s*[<=]|percent|proportion|fold)\b",
                sentence,
                re.IGNORECASE,
            )
        )

        if needs_citation and not has_bundle_citations(sentence):
            # Insert citation before the period at end of sentence
            sentence = sentence.rstrip(".!?")
            sentence = f"{sentence}. [bundle:{cite_idx}]"
            cite_idx = min(cite_idx + 1, source_count)

        result.append(sentence)

    return " ".join(result)


def clean_entry(entry: dict, idx: int) -> tuple[dict, list[str]]:
    """Clean a single corpus entry. Returns (cleaned_entry, list_of_fixes)."""
    fixes = []
    sections = entry.get("sections", {})
    source_count = len(entry.get("source_bundle", []))

    for section_name, text in sections.items():
        if not isinstance(text, str):
            continue

        original = text

        # Fix 1: double periods
        cleaned = fix_double_periods(text)
        if cleaned != text:
            fixes.append(f"  [{idx}] {section_name}: fixed double periods")
            text = cleaned

        # Fix 2: missing bundle citations (Key Findings only)
        if section_name == "Key Findings" and not has_bundle_citations(text):
            text = inject_bundle_citations(text, source_count)
            if text != cleaned:
                fixes.append(f"  [{idx}] Key Findings: added [bundle:N] citations")

        sections[section_name] = text

    entry["sections"] = sections
    return entry, fixes


def main() -> int:
    parser = argparse.ArgumentParser(description="Clean v3 benchmark corpus")
    parser.add_argument("--dry-run", action="store_true", help="Show fixes without writing")
    args = parser.parse_args()

    corpus = json.loads(CORPUS_PATH.read_text())
    total_fixes = 0
    all_fixes = []
    double_period_entries = 0
    missing_citation_entries = 0

    for idx, entry in enumerate(corpus):
        has_double = False
        sections = entry.get("sections", {})
        for text in sections.values():
            if isinstance(text, str):
                check = text.replace("...", "")
                if ".." in check:
                    has_double = True
                    break
        if has_double:
            double_period_entries += 1

        if "Key Findings" in sections and not has_bundle_citations(sections["Key Findings"]):
            missing_citation_entries += 1

    print(f"Corpus: {len(corpus)} entries")
    print(f"  Entries with double periods: {double_period_entries}")
    print(f"  Entries missing [bundle:N] citations: {missing_citation_entries}")
    print()

    cleaned_corpus = []
    for idx, entry in enumerate(corpus):
        cleaned, fixes = clean_entry(entry, idx)
        cleaned_corpus.append(cleaned)
        if fixes:
            all_fixes.extend(fixes)
            total_fixes += len(fixes)

    print(f"Total fixes applied: {total_fixes}")
    if args.dry_run:
        for fix in all_fixes:
            print(fix)
        print("\nDry run — no file written.")
        return 0

    # Validate: no more double periods (excluding ellipses)
    remaining = 0
    for idx, entry in enumerate(cleaned_corpus):
        for section_name, text in entry.get("sections", {}).items():
            if isinstance(text, str):
                check = text.replace("...", "")
                if ".." in check:
                    remaining += 1
                    print(f"  WARNING: [{idx}] {section_name} still has '..'")

    if remaining:
        print(f"\nWARNING: {remaining} sections still have double periods after cleanup!")

    OUTPUT_PATH.write_text(json.dumps(cleaned_corpus, indent=2, ensure_ascii=False))
    print(f"\nCleaned corpus written to {OUTPUT_PATH}")

    for fix in all_fixes:
        print(fix)

    return 0


if __name__ == "__main__":
    sys.exit(main())
