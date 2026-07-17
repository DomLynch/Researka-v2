#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import httpx

SEARCH_URL = "https://f1000research.com/extapi/search"
ARTICLE_URL = "https://f1000research.com/extapi/article/xml"
SOURCE_URL = "https://f1000research.com/developers"
LABEL_POLICY = (
    "accept when F1000 peer-review pass criteria are met; reject when one or more Not Approved reports "
    "remain and no Approved report exists; otherwise revise"
)


def _text(node: ET.Element | None) -> str:
    return " ".join("".join(node.itertext()).split()) if node is not None else ""


def parse_status(status_text: str) -> tuple[str | None, int]:
    value = status_text.lower()
    approved = sum(map(int, re.findall(r"(\d+)\s+approved(?!\s+with\s+reservations)", value)))
    reserved = sum(map(int, re.findall(r"(\d+)\s+approved\s+with\s+reservations", value)))
    not_approved = sum(map(int, re.findall(r"(\d+)\s+not\s+approved", value)))
    reviewer_count = approved + reserved + not_approved
    if reviewer_count < 2:
        return None, reviewer_count
    if approved >= 2 or (approved >= 1 and reserved >= 2):
        return "accept", reviewer_count
    if not_approved and not approved:
        return "reject", reviewer_count
    return "revise", reviewer_count


def parse_article(raw: bytes, doi: str, retrieved_at: str) -> dict | None:
    root = ET.fromstring(raw)
    status_text = _text(root.find("./front/article-meta/title-group/fn-group/fn/p"))
    expected, reviewer_count = parse_status(status_text)
    if expected is None:
        return None
    title = _text(root.find("./front/article-meta/title-group/article-title"))
    abstract = _text(root.find("./front/article-meta/abstract"))
    sections: dict[str, str] = {}
    for index, section in enumerate(root.findall("./body/sec")[:10], start=1):
        heading = _text(section.find("./title")) or f"Section {index}"
        sections[heading[:120]] = _text(section)[:6000]
    references: list[dict[str, object]] = []
    for reference in root.findall(".//ref-list/ref"):
        citation = _text(reference)
        ref_doi = _text(reference.find(".//pub-id[@pub-id-type='doi']"))
        ref_title = _text(reference.find(".//article-title")) or _text(reference.find(".//source"))
        if not ref_title or not citation:
            continue
        references.append({
            "title": ref_title[:500],
            "doi": ref_doi or None,
            "registry_id": None if ref_doi else f"f1000-ref:{doi}:{len(references) + 1}",
            "year": int(year) if (year := _text(reference.find(".//year"))).isdigit() else None,
            "evidence_type": "primary",
            "excerpt": citation[:1500],
        })
        if len(references) == 20:
            break
    if not title or len(abstract.split()) < 30 or len(sections) < 3 or len(references) < 8:
        return None
    source_url = f"https://doi.org/{doi}"
    return {
        "entry_id": f"f1000-{doi.replace('/', '-').replace('.', '-')}",
        "article_type": "empirical_study",
        "submission": {
            "title": title,
            "abstract": abstract[:5000],
            "sections": sections,
            "source_bundle": references,
            "author_agent_id": "external-human-reviewed-corpus",
            "article_type": "empirical_study",
            "domain_slug": "multidisciplinary",
        },
        "expected": {"decision": expected, "rationale": status_text},
        "tags": ["external", "human-peer-review", "f1000research", expected],
        "notes": "Independent open peer-review status; no Researka decision was used as ground truth.",
        "adjudication": {
            "source": "F1000Research open peer review",
            "source_id": doi,
            "source_url": source_url,
            "status_text": status_text,
            "reviewer_count": reviewer_count,
            "retrieved_at": retrieved_at,
            "source_sha256": hashlib.sha256(raw).hexdigest(),
        },
    }


def _get(client: httpx.Client, url: str, *, params: dict, cache: Path | None = None) -> bytes:
    if cache and cache.exists():
        return cache.read_bytes()
    for attempt in range(3):
        try:
            response = client.get(url, params=params)
            response.raise_for_status()
            if cache:
                cache.parent.mkdir(parents=True, exist_ok=True)
                cache.write_bytes(response.content)
            return response.content
        except httpx.HTTPError:
            if attempt == 2:
                raise
            time.sleep(2 ** attempt)
    raise RuntimeError("unreachable")


def build_corpus(*, per_label: int, max_pages: int, delay: float, cache_dir: Path) -> dict:
    targets = {"accept": per_label + 1, "revise": per_label, "reject": per_label}
    entries: list[dict] = []
    seen_articles: set[str] = set()
    retrieved_at = datetime.now(timezone.utc).isoformat()
    headers = {"User-Agent": "ResearkaCalibration/1.0 (+https://researka.org/methods)"}
    with httpx.Client(timeout=30.0, follow_redirects=True, headers=headers) as client:
        for page in range(1, max_pages + 1):
            search = _get(client, SEARCH_URL, params={"q": 'R_TY:"RESEARCH_ARTICLE"', "rows": 100, "page": page})
            dois = [node.text.strip() for node in ET.fromstring(search).findall("doi") if node.text]
            for doi in dois:
                article_id = doi.rsplit(".", 1)[0]
                if article_id in seen_articles:
                    continue
                seen_articles.add(article_id)
                raw = _get(client, ARTICLE_URL, params={"doi": doi}, cache=cache_dir / f"{hashlib.sha256(doi.encode()).hexdigest()}.xml")
                entry = parse_article(raw, doi, retrieved_at)
                if entry and sum(item["expected"]["decision"] == entry["expected"]["decision"] for item in entries) < targets[entry["expected"]["decision"]]:
                    entries.append(entry)
                    counts = Counter(item["expected"]["decision"] for item in entries)
                    print(f"[{len(entries)}/100] {doi} -> {entry['expected']['decision']} {dict(counts)}")
                if all(sum(item["expected"]["decision"] == label for item in entries) >= target for label, target in targets.items()):
                    return _corpus(entries, retrieved_at)
                time.sleep(delay)
    raise RuntimeError(f"insufficient balanced cases: {Counter(item['expected']['decision'] for item in entries)}")


def _corpus(entries: list[dict], generated_at: str) -> dict:
    return {
        "version": "f1000-open-peer-review-v1",
        "corpus_status": "adjudicated",
        "evaluation_scope": "reviewer_only",
        "ground_truth_source": "F1000Research open peer review",
        "ground_truth_url": SOURCE_URL,
        "label_policy": LABEL_POLICY,
        "generated_at": generated_at,
        "entries": entries,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a balanced 100-case independent reviewer calibration corpus.")
    parser.add_argument("--output", default="calibration/f1000_gold_set_v1.json")
    parser.add_argument("--per-label", type=int, default=33)
    parser.add_argument("--max-pages", type=int, default=20)
    parser.add_argument("--delay", type=float, default=0.65)
    parser.add_argument("--cache-dir", default=".tmp/f1000-calibration")
    args = parser.parse_args()
    corpus = build_corpus(per_label=args.per_label, max_pages=args.max_pages, delay=args.delay, cache_dir=Path(args.cache_dir))
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(corpus, indent=2))
    print(f"saved {len(corpus['entries'])} cases to {output}")


if __name__ == "__main__":
    main()
