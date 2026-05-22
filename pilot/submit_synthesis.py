"""Submit a Research Agent Bot full-paper run as a research_synthesis to Researka.

The bot's full_paper.md format does not fit the rapid evidence synthesis (RES)
schema — it has Abstract, Introduction, Background, Inferential Bridge,
Quantitative Evidence Index, Methods, Results, Cross-Domain Synthesis,
Discussion, Limitations, Conclusion, References (12 sections, ~20k+ words).

The new article_type=research_synthesis (deployed in commit d19e0b1) accepts
this shape natively. This script:
  1. Reads the bot's full_paper.md + manifest.json
  2. Maps sections to required + recommended for research_synthesis
  3. Builds source_bundle from manifest receipts (best-effort DOI extraction)
  4. POSTs to live VPS as article_type=research_synthesis
  5. Drains queue and reports decision

Usage:
    python pilot/submit_synthesis.py <run_dir>
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

URL = "http://49.12.7.18:8000"
ADMIN_KEY = "ResearkaAdmin2026!"
KEYS_FILE = Path(__file__).parent / "pilot_keys_2026-04-26.json"
SECTION_HEADER = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)


def load_pilot_key() -> str:
    keys = json.loads(KEYS_FILE.read_text())["keys"]
    return next(k for k in keys if k["agent_id"] == "pilot-house-bot")["raw"]


def parse_full_paper(md_path: Path) -> tuple[str, dict[str, str]]:
    text = md_path.read_text()
    m = re.search(r"^#\s+(.+?)\s*$", text, re.MULTILINE)
    title = m.group(1).strip() if m else md_path.stem
    sections: dict[str, str] = {}
    headers = list(SECTION_HEADER.finditer(text))
    for i, h in enumerate(headers):
        name = h.group(1).strip()
        start = h.end()
        end = headers[i + 1].start() if i + 1 < len(headers) else len(text)
        sections[name] = text[start:end].strip()
    return title, sections


def extract_doi_from_receipt_id(receipt_id: str) -> str | None:
    """Best-effort DOI extraction from the bot's receipt_id format.

    Patterns the bot uses:
      DOI_10_1101_2025_08_15_670559_xxx -> 10.1101/2025.08.15.670559
      DOI_10_1056_nejmoa0907419_xxx     -> 10.1056/nejmoa0907419
      DOI_10_3354_ab00673_xxx           -> 10.3354/ab00673
      PMC12074816_xxx                   -> None (no DOI in id)
      PMID25540326_xxx                  -> None (no DOI in id)
    """
    if not receipt_id.startswith("DOI_"):
        return None
    # Strip DOI_ prefix, then anything after the canonical DOI portion
    body = receipt_id[4:]
    parts = body.split("_")
    if len(parts) < 3 or parts[0] != "10":
        return None
    # Reconstruct: 10.<registrant>/<rest>
    registrant = parts[1]
    rest_parts = parts[2:]
    # The DOI suffix is everything until the title slug starts. Heuristic:
    # take the first 4-6 tokens after the registrant. For NEJM/most journals
    # one token suffices; for biorxiv-style DOIs you need year.month.day.id.
    # Easier: take everything until we hit a token that looks like a word
    # (>=4 chars, alphabetic) — those tokens are usually title slugs.
    suffix_tokens: list[str] = []
    for tok in rest_parts:
        if len(tok) >= 4 and tok.isalpha():
            break
        suffix_tokens.append(tok)
    if not suffix_tokens:
        return None
    # Some DOIs have dotted suffixes (biorxiv: 10.1101/2025.08.15.670559)
    suffix = ".".join(suffix_tokens) if len(suffix_tokens) > 1 and suffix_tokens[0].isdigit() else "/".join(suffix_tokens) if False else suffix_tokens[0]
    return f"10.{registrant}/{suffix}"


def extract_year_from_token(citation_token: str) -> int | None:
    m = re.search(r"\b(19|20)\d{2}\b", citation_token)
    return int(m.group(0)) if m else None


def build_source_bundle(manifest: dict) -> list[dict]:
    bundle: list[dict] = []
    for r in manifest.get("receipts", []):
        receipt_id = r.get("receipt_id", "")
        token = r.get("citation_token", "")
        # evidence_type: bot's "directness" maps to schema literal {primary, review}
        directness = (r.get("directness") or "").lower()
        evidence_type = "review" if directness == "review" else "primary"
        # URL: PMC IDs become PubMed Central URLs; DOI ids become doi.org URLs
        url: str | None = None
        if receipt_id.startswith("PMC"):
            pmc_id = receipt_id.split("_")[0]
            url = f"https://www.ncbi.nlm.nih.gov/pmc/articles/{pmc_id}/"
        elif receipt_id.startswith("PMID"):
            pmid = receipt_id[4:].split("_")[0]
            url = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
        # DOI extraction (only set if it round-trips through the strict pattern)
        doi = extract_doi_from_receipt_id(receipt_id)
        if doi and not re.match(r"^10\.\d{4,}/\S+$", doi):
            doi = None
        bundle.append({
            "title": f"{token} — {receipt_id.replace('_', ' ')[:80]}",
            "year": extract_year_from_token(token),
            "url": url,
            "doi": doi,
            "evidence_type": evidence_type,
        })
    return bundle


def post_submission(payload: dict, api_key: str) -> dict:
    req = urllib.request.Request(
        f"{URL}/submissions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Api-Key": api_key},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            body = response.read().decode("utf-8")
            return {"status": response.status, "body": json.loads(body) if body else {}}
    except urllib.error.HTTPError as exc:
        return {"status": exc.code, "error": exc.read().decode("utf-8", errors="replace")[:500]}


def get_decision(sid: str, api_key: str) -> dict:
    req = urllib.request.Request(
        f"{URL}/submissions/{sid}/decision",
        headers={"X-Api-Key": api_key},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.loads(response.read())
    except Exception as exc:
        return {"error": str(exc)[:200]}


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: submit_synthesis.py <run_dir>")
        sys.exit(1)
    run_dir = Path(sys.argv[1])
    md_path = run_dir / "full_paper.md"
    manifest_path = run_dir / "manifest.json"
    if not md_path.exists() or not manifest_path.exists():
        print(f"missing full_paper.md or manifest.json in {run_dir}")
        sys.exit(1)

    title, sections = parse_full_paper(md_path)
    manifest = json.loads(manifest_path.read_text())
    source_bundle = build_source_bundle(manifest)

    # Strip sections we don't want in the payload (the bot's tables/index are
    # already inside Quantitative Evidence Index).
    payload_sections = dict(sections)

    payload = {
        "title": title,
        "abstract": payload_sections.get("Abstract", "")[:8000],
        "sections": payload_sections,
        "methods": payload_sections.get("Methods", "")[:8000],
        "source_bundle": source_bundle,
        "author_agent_id": "pilot-house-bot",
        # SubmissionPayload has article_type as a top-level Pydantic field,
        # not nested in metadata. Without this the workflow defaults to RES
        # and runs the wrong gate set.
        "article_type": "research_synthesis",
        "domain_slug": "longevity",
        "core_claims_resolved": True,
    }

    print("=== Submitting as research_synthesis ===")
    print(f"Title: {title}")
    print(f"Sections: {len(sections)} ({sorted(sections.keys())})")
    print(f"Source bundle: {len(source_bundle)} entries")
    print(f"Total body words: {sum(len(s.split()) for s in sections.values())}")
    print()

    api_key = load_pilot_key()
    result = post_submission(payload, api_key)
    sid = result.get("body", {}).get("submission", {}).get("id")
    print(f"POST: {result.get('status')} sid={sid or 'NONE'}")
    if not sid:
        print(f"ERROR: {result.get('error', result.get('body'))}")
        return

    print()
    print("Draining queue...")
    for i in range(20):
        subprocess.run(
            ["curl", "-s", "-X", "POST", "-H", f"x-api-key: {ADMIN_KEY}", "--max-time", "240", f"{URL}/jobs/run-once"],
            capture_output=True,
            timeout=260,
        )
        dec = get_decision(sid, api_key)
        if dec.get("decision") or dec.get("status") == "complete":
            break
        time.sleep(3)

    dec = get_decision(sid, api_key)
    print()
    print("=== Decision ===")
    print(f"  decision: {dec.get('decision') or dec.get('status')}")
    print(f"  decision_object_id: {dec.get('decision_object_id')}")
    if dec.get("gate_failures"):
        print("  gate_failures:")
        for g in dec["gate_failures"]:
            print(f"    - {g.get('name')}: {g.get('reason')}")


if __name__ == "__main__":
    main()
