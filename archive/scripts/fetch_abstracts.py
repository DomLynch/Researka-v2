#!/usr/bin/env python3
"""Fetch abstracts for DOIs via CrossRef and arXiv APIs.

Usage:
    python3 scripts/fetch_abstracts.py \
        --input calibration/elite_benchmark_v3_cleaned.json \
        --output calibration/elite_benchmark_v4_abstracts.json
"""
import argparse
import json
import re
import time
import urllib.error
import urllib.request
import sys
from pathlib import Path


def clean_jats_abstract(raw: str) -> str:
    """Strip JATS XML tags from a CrossRef abstract."""
    # Remove JATS tags
    text = re.sub(r"<jats:[^>]+>", "", raw)
    text = re.sub(r"</jats:[^>]+>", "", raw)
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text).strip()
    return text


def fetch_crossref(doi: str) -> str | None:
    """Fetch abstract from CrossRef."""
    url = f"https://api.crossref.org/works/{doi}"
    req = urllib.request.Request(
        url, headers={"User-Agent": "Researka/1.0 (mailto:dom@researka.com)"}
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        abstract = data["message"].get("abstract", "")
        if abstract:
            return clean_jats_abstract(abstract)
    except (urllib.error.HTTPError, urllib.error.URLError, KeyError, json.JSONDecodeError) as e:
        print(f"  CrossRef failed for {doi}: {e}", file=sys.stderr)
    return None


def fetch_arxiv(arxiv_id: str) -> str | None:
    """Fetch abstract from arXiv."""
    url = f"http://export.arxiv.org/api/query?id_list={arxiv_id}"
    req = urllib.request.Request(url, headers={"User-Agent": "Researka/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            xml = resp.read().decode()
        m = re.search(r"<summary>(.*?)</summary>", xml, re.DOTALL)
        if m:
            return m.group(1).strip()
    except (urllib.error.HTTPError, urllib.error.URLError) as e:
        print(f"  arXiv failed for {arxiv_id}: {e}", file=sys.stderr)
    return None


def fetch_openalex(doi: str) -> str | None:
    """Fetch abstract from OpenAlex API as fallback."""
    url = f"https://api.openalex.org/works/doi:{doi}"
    req = urllib.request.Request(
        url, headers={"User-Agent": "Researka/1.0 (mailto:dom@researka.com)"}
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        inverted = data.get("abstract_inverted_index")
        if inverted:
            # Reconstruct abstract from inverted index
            positions = {}
            for word, idxs in inverted.items():
                for idx in idxs:
                    positions[idx] = word
            return " ".join(positions[k] for k in sorted(positions))
    except (urllib.error.HTTPError, urllib.error.URLError, KeyError, json.JSONDecodeError) as e:
        print(f"  OpenAlex failed for {doi}: {e}", file=sys.stderr)
    return None


def extract_arxiv_id(doi: str) -> str | None:
    """Extract arXiv ID from DOI like 10.48550/arXiv.2604.17411."""
    m = re.match(r"10\.48550/arXiv\.(\d+\.\d+)", doi)
    if m:
        return m.group(1)
    return None


def fetch_all_abstracts(dois: list[str]) -> dict[str, str]:
    """Fetch abstracts for a list of DOIs."""
    abstracts = {}
    total = len(dois)
    for i, doi in enumerate(dois, 1):
        print(f"[{i}/{total}] {doi}...", end=" ", flush=True)

        arxiv_id = extract_arxiv_id(doi)
        if arxiv_id:
            abstract = fetch_arxiv(arxiv_id)
            source = "arXiv"
            delay = 3.0
        else:
            abstract = fetch_crossref(doi)
            source = "CrossRef"
            delay = 0.3
            if not abstract:
                print("  trying OpenAlex...", end=" ", flush=True)
                abstract = fetch_openalex(doi)
                source = "OpenAlex"

        if abstract:
            abstracts[doi] = abstract
            print(f"OK ({source}, {len(abstract)} chars)")
        else:
            print("MISS")

        time.sleep(delay)

    return abstracts


def patch_corpus(corpus: list[dict], abstracts: dict[str, str]) -> list[dict]:
    """Patch corpus papers with fetched abstracts in source bundles."""
    patched = 0
    missing = []
    for paper in corpus:
        for bundle in paper.get("source_bundle", []):
            doi = bundle.get("doi", "").strip()
            if doi and doi in abstracts:
                bundle["abstract"] = abstracts[doi]
                patched += 1
            elif doi:
                missing.append(doi)
    if missing:
        print(f"\nMissing abstracts for {len(missing)} DOIs:", file=sys.stderr)
        for d in missing:
            print(f"  {d}", file=sys.stderr)
    print(f"\nPatched {patched} source bundles with abstracts.")
    return corpus


def main():
    parser = argparse.ArgumentParser(description="Fetch abstracts for corpus DOIs")
    parser.add_argument("--input", required=True, help="Input corpus JSON")
    parser.add_argument("--output", required=True, help="Output corpus JSON")
    parser.add_argument("--cache", default=None, help="Abstract cache JSON (optional)")
    args = parser.parse_args()

    with open(args.input) as f:
        corpus = json.load(f)

    # Collect unique DOIs
    all_dois = set()
    for paper in corpus:
        for bundle in paper.get("source_bundle", []):
            doi = bundle.get("doi", "").strip()
            if doi:
                all_dois.add(doi)

    print(f"Found {len(all_dois)} unique DOIs across {len(corpus)} papers.")

    # Load cache if exists
    cached = {}
    if args.cache and Path(args.cache).exists():
        with open(args.cache) as f:
            cached = json.load(f)
        print(f"Loaded {len(cached)} cached abstracts.")

    # Fetch missing
    to_fetch = [d for d in sorted(all_dois) if d not in cached]
    if to_fetch:
        print(f"Fetching {len(to_fetch)} abstracts...")
        fetched = fetch_all_abstracts(to_fetch)
        cached.update(fetched)

        # Save cache
        if args.cache:
            with open(args.cache, "w") as f:
                json.dump(cached, f, indent=2)
            print(f"Cache saved ({len(cached)} entries).")
    else:
        print("All abstracts already cached.")

    # Patch corpus
    patched_corpus = patch_corpus(corpus, cached)

    with open(args.output, "w") as f:
        json.dump(patched_corpus, f, indent=2)
    print(f"Output: {args.output}")


if __name__ == "__main__":
    main()
