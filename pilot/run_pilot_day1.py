"""Pilot day-1 runner.

Take the 5 most recent distinct-topic Rapid Evidence Synthesis drafts produced
by Research Agent Bot, convert each into a Researka SubmissionPayload, POST to
the live VPS, drain the job queue, and capture the resulting panel decisions.

Usage:
    python pilot/run_pilot_day1.py

Inputs:
    - pilot/pilot_keys_2026-04-26.json  (gitignored; contains pilot-house-bot raw key)
    - 5 hardcoded RES file paths under ~/Downloads/

Outputs:
    - pilot/pilot_run_2026-04-26.json
    - pilot/PILOT_DAY_1_REPORT.md
"""
from __future__ import annotations

import json
import re
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

URL = "http://49.12.7.18:8000"
ADMIN_KEY = "ResearkaAdmin2026!"
KEYS_FILE = Path(__file__).parent / "pilot_keys_2026-04-26.json"
OUT_JSON = Path(__file__).parent / "pilot_run_2026-04-26.json"
REPORT_MD = Path(__file__).parent / "PILOT_DAY_1_REPORT.md"

DOWNLOADS = Path("/Users/domininclynch/Downloads")

# Latest distinct-topic drafts to submit. The 5th slot drops the 0-source
# senolytics-and-healthspan draft (different/older format) in favour of an
# everolimus retry that we know parses well.
DRAFTS = [
    DOWNLOADS / "2026-04-25T12-24-12.303160Z-metformin-aging-older-adults.md",
    DOWNLOADS / "2026-04-26T12-35-03.381551Z-rapamycin.md",
    DOWNLOADS / "2026-04-25T05-55-28.888145Z-senolytic-dasatinib-quercetin-older-adults.md",
    DOWNLOADS / "2026-04-21T17-31-57.116209Z-everolimus.md",
    DOWNLOADS / "2026-04-23T15-01-03.560069Z-rapamycin-aging-older-adults.md",
]


def load_pilot_key() -> str:
    keys = json.loads(KEYS_FILE.read_text())["keys"]
    house = next(k for k in keys if k["agent_id"] == "pilot-house-bot")
    return house["raw"]


SECTION_HEADER = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)


def parse_md(path: Path) -> dict:
    """Parse a Research Agent Bot RES draft into a Researka SubmissionPayload."""
    text = path.read_text()

    # Title from first H1
    m = re.search(r"^#\s+(.+?)\s*$", text, re.MULTILINE)
    title = m.group(1).strip() if m else path.stem

    # Topic + domain from the metadata bullet list at the top
    topic_match = re.search(r"^- Topic:\s*(.+?)\s*$", text, re.MULTILINE)
    domain_match = re.search(r"^- Domain:\s*(.+?)\s*$", text, re.MULTILINE)
    topic = topic_match.group(1).strip() if topic_match else title
    domain = domain_match.group(1).strip().lower() if domain_match else "longevity"

    # Carve sections by ## headers
    sections: dict[str, str] = {}
    headers = list(SECTION_HEADER.finditer(text))
    for i, h in enumerate(headers):
        name = h.group(1).strip()
        start = h.end()
        end = headers[i + 1].start() if i + 1 < len(headers) else len(text)
        body = text[start:end].strip()
        sections[name] = body

    abstract = sections.pop("Abstract", "")
    methods = sections.pop("Methods", "")
    sections.pop("Adjudication Notes", None)
    sections.pop("Evidence Table", None)
    sources_block = sections.pop("Sources", "")

    # Parse the [N] ... — URL lines from the Sources block
    source_bundle = []
    for line in sources_block.splitlines():
        line = line.strip()
        if not line or not line.startswith("["):
            continue
        # Extract URL at the end after " — "
        url_split = line.rsplit(" — ", 1)
        if len(url_split) != 2:
            continue
        meta_part, url = url_split[0], url_split[1].strip()
        # Extract title between "[N] " and " (YYYY)"
        title_match = re.match(r"^\[\d+\]\s+(.+?)\s+\((\d{4})\),", meta_part)
        if not title_match:
            continue
        src_title = title_match.group(1).strip()
        src_year = int(title_match.group(2))
        # Extract DOI ONLY from doi.org URLs and only if it matches Researka's
        # `10.XXXX/suffix` shape. PubMed (`pubmed.ncbi.nlm.nih.gov/<pmid>`),
        # EuropePMC (`/MED/<pmid>`), and ClinicalTrials.gov URLs carry PMIDs
        # or NCT IDs, not DOIs — sending those as `doi=...` trips the
        # doi_sanity gate. Better to send `doi: None` (the contract allows it).
        doi: str | None = None
        doi_match = re.search(r"doi\.org/(.+?)/?$", url)
        if doi_match:
            candidate = doi_match.group(1).strip()
            if re.match(r"^10\.\d{4,}/\S+$", candidate):
                doi = candidate
        # Researka's contract: evidence_type is Literal["primary", "review"]
        # Map: systematic-review/meta-analysis/narrative review -> "review"
        # Everything else (rct, primary, cohort, observational, protocol) -> "primary"
        meta_lower = meta_part.lower()
        if (
            "systematic-review" in meta_lower
            or "systematic_review" in meta_lower
            or "meta-analysis" in meta_lower
            or "review" in meta_lower
        ) and "narrative" not in meta_lower:
            # Catches systematic-review and meta-analysis. Plain "review" only if
            # it's a review article, which we infer from the bot's tier label.
            if "systematic" in meta_lower or "meta-analysis" in meta_lower or "| review |" in meta_lower:
                evidence_type = "review"
            else:
                evidence_type = "primary"
        else:
            evidence_type = "primary"
        source_bundle.append({
            "title": src_title,
            "year": src_year,
            "url": url,
            "doi": doi or None,
            "evidence_type": evidence_type,
        })

    payload = {
        "title": title,
        "abstract": abstract,
        "sections": sections,
        "methods": methods,
        "source_bundle": source_bundle,
        "author_agent_id": "pilot-house-bot",
        "domain_slug": domain,
        "core_claims_resolved": True,
    }
    return {"topic": topic, "path": str(path), "payload": payload}


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
    except Exception as exc:
        return {"status": 0, "error": str(exc)[:500]}


def drain_queue(api_key: str, max_iters: int = 60) -> int:
    """Drain the queue using admin /jobs/run-once. Returns final queue depth."""
    for it in range(max_iters):
        try:
            req = urllib.request.Request(
                f"{URL}/jobs/queue",
                headers={"X-Api-Key": ADMIN_KEY},
            )
            with urllib.request.urlopen(req, timeout=20) as response:
                q = json.loads(response.read())["queued"]
            if len(q) == 0:
                print(f"  Drained at iter {it}", flush=True)
                return 0
            # Run 4 jobs in parallel via subprocess curl
            procs = []
            for _ in range(4):
                procs.append(subprocess.Popen(
                    ["curl", "-s", "-X", "POST", "-H", f"x-api-key: {ADMIN_KEY}",
                     "--max-time", "180", f"{URL}/jobs/run-once"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                ))
            for p in procs:
                try:
                    p.wait(timeout=200)
                except subprocess.TimeoutExpired:
                    p.kill()
            if it % 3 == 0:
                print(f"  iter {it}: queue={len(q)}", flush=True)
        except Exception as exc:
            print(f"  iter {it}: drain error {exc}", flush=True)
            time.sleep(5)
    return -1


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
    api_key = load_pilot_key()
    print("=== Pilot day-1: 5 distinct topics from research-agent-bot ===")
    print(f"Endpoint: {URL}")
    print(f"Key: pilot-house-bot ({api_key[:10]}...{api_key[-4:]})")
    print()

    submissions: list[dict[str, Any]] = []
    for i, path in enumerate(DRAFTS, 1):
        print(f"[{i}/5] {path.name}")
        if not path.exists():
            print("  SKIP: file missing")
            submissions.append({"path": str(path), "error": "missing_file"})
            continue
        parsed = parse_md(path)
        result = post_submission(parsed["payload"], api_key)
        record = {
            "topic": parsed["topic"],
            "source_file": path.name,
            "post_status": result.get("status"),
            "submission_id": result.get("body", {}).get("submission", {}).get("id"),
            "post_response": result,
            "bundle_size": len(parsed["payload"]["source_bundle"]),
        }
        submissions.append(record)
        sid = record["submission_id"]
        print(f"  topic: {parsed['topic']}")
        print(f"  bundle: {len(parsed['payload']['source_bundle'])} sources")
        print(f"  POST: {result.get('status')} sid={sid[:12] if sid else 'NONE'}")
        if not sid:
            print(f"  ERROR: {result.get('error', result.get('body'))}")

    print()
    print("=== Drain queue ===")
    drain_queue(api_key)

    print()
    print("=== Poll decisions ===")
    for sub in submissions:
        sid = sub.get("submission_id")
        if not sid:
            continue
        dec = get_decision(sid, api_key)
        sub["decision"] = dec
        decision_str = dec.get("decision") or dec.get("status") or "pending"
        gates = [g.get("name", "?") for g in dec.get("gate_failures", [])]
        rubric = dec.get("rubric_scores", {})
        rubric_summary = "/".join(str(v) for v in rubric.values()) if rubric else "no-rubric"
        print(f"  {sub['topic'][:50]:50s} -> {decision_str:8s} {rubric_summary} {','.join(gates) if gates else ''}")

    # Save raw run output
    OUT_JSON.write_text(json.dumps({
        "run_date": "2026-04-26",
        "endpoint": URL,
        "agent_id": "pilot-house-bot",
        "submissions": submissions,
    }, indent=2))
    print(f"\nSaved raw run to {OUT_JSON}")


if __name__ == "__main__":
    main()
