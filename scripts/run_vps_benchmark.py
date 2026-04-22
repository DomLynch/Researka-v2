#!/usr/bin/env python3
"""
Run an N-paper benchmark against the live VPS API.

Usage:
    python scripts/run_vps_benchmark.py                          # 200 papers, production
    python scripts/run_vps_benchmark.py --count 10               # 10-paper smoke test
    python scripts/run_vps_benchmark.py --base-url http://localhost:8000

Saves output to artifacts/benchmark_vps_{N}.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.run_benchmark import aggregate, expected_decision_for_paper, generate_papers, submission_payload_for_paper


def load_benchmark_papers(path: str) -> list[dict]:
    raw = json.loads(Path(path).read_text())
    if not isinstance(raw, list):
        raise ValueError("benchmark_input_must_be_list")
    return raw


def submit_and_drain(paper: dict, base_url: str, api_key: str, timeout_s: float = 600.0) -> dict:
    """Submit a paper via the API, drain its jobs, return decision record."""
    headers = {"x-api-key": api_key, "content-type": "application/json"}
    paper_id = paper.get("_benchmark_paper_id", "?")
    quality = paper.get("_benchmark_quality", "medium")
    t0 = time.time()

    record = {
        "paper_id": paper_id,
        "title": paper["title"],
        "domain": paper.get("domain_slug", "general"),
        "quality": quality,
        "style": paper.get("_style_tag", "house"),
        "bundle_size": paper.get("_benchmark_bundle_size", 12),
        "expected_decision": expected_decision_for_paper(paper),
        "stage_reached": None,
        "outcome": None,
        "recommendation": None,
        "decision": None,
        "route": None,
        "rubric_scores": None,
        "major_issues_count": None,
        "overclaim": None,
        "claim_support": None,
        "synthesis_quality": None,
        "cost_usd": 0.0,
        "tokens_in": 0,
        "tokens_out": 0,
        "error": None,
        "duration_s": 0.0,
    }

    # 1. Submit
    try:
        resp = requests.post(
            f"{base_url}/submissions",
            headers=headers,
            json=submission_payload_for_paper(paper),
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        submission_id = data["submission"]["id"]
    except Exception as e:
        record["stage_reached"] = "submit"
        record["outcome"] = "submit_error"
        record["error"] = str(e)
        record["duration_s"] = round(time.time() - t0, 3)
        return record

    # 2. Drain jobs (up to 3 per paper: intake, review, editorial)
    #    target_object_id filter ensures we only claim this submission's jobs.
    stages_completed = []
    drain_loops = 0
    max_drain_loops = 30     # 3 real stages + empty-retry buffer
    consecutive_empty = 0
    max_empty_retries = 6    # keep retrying a few times after empty

    def _log(msg: str) -> None:
        elapsed_now = time.time() - t0
        print(f"    [{drain_loops}] {elapsed_now:.1f}s | {msg}", flush=True)

    while drain_loops < max_drain_loops:
        drain_loops += 1
        elapsed = time.time() - t0
        if elapsed > timeout_s:
            record["stage_reached"] = "timeout"
            record["outcome"] = "timeout"
            record["error"] = (
                f"timed out after {elapsed:.1f}s | loops={drain_loops} "
                f"stages={stages_completed}"
            )
            record["duration_s"] = round(elapsed, 3)
            return record

        try:
            t_call = time.time()
            resp = requests.post(
                f"{base_url}/jobs/run-once",
                headers=headers,
                params={"target_object_id": submission_id},
                timeout=300,
            )
            resp.raise_for_status()
            result = resp.json()
            call_dur = time.time() - t_call
        except Exception as e:
            record["stage_reached"] = "drain_error"
            record["outcome"] = "drain_error"
            record["error"] = f"run-once failed: {e}"
            record["duration_s"] = round(time.time() - t0, 3)
            return record

        claimed = result.get("claimed", 0)
        completed = result.get("completed", 0)
        failed = result.get("failed", 0)
        stage = result.get("stage", "?")

        if claimed == 0:
            consecutive_empty += 1
            _log(f"empty (consecutive={consecutive_empty}/{max_empty_retries})")
            if consecutive_empty >= max_empty_retries:
                break
            time.sleep(2.0)
            continue

        consecutive_empty = 0
        if completed == 1:
            stages_completed.append(stage)
            _log(f"{stage} completed ({call_dur:.1f}s)")
        elif failed == 1:
            stages_completed.append(f"{stage}:failed")
            _log(f"{stage} FAILED ({call_dur:.1f}s)")

        time.sleep(0.2)

    # 3. Get decision
    try:
        resp = requests.get(
            f"{base_url}/submissions/{submission_id}/decision",
            timeout=10,
        )
        resp.raise_for_status()
        decision_data = resp.json()
    except Exception as e:
        record["stage_reached"] = "poll_decision"
        record["outcome"] = "poll_error"
        record["error"] = str(e)
        record["duration_s"] = round(time.time() - t0, 3)
        return record

    status = decision_data.get("status")
    if status == "pending":
        # Check if intake rejected by looking at the submission metadata
        try:
            sub_resp = requests.get(
                f"{base_url}/submissions/{submission_id}",
                headers=headers,
                timeout=10,
            )
            sub_resp.raise_for_status()
            sub_data = sub_resp.json()
            meta = sub_data.get("metadata", {})
            if meta.get("intake_rejected") or meta.get("intake_rejected_reason"):
                record["stage_reached"] = "intake"
                record["outcome"] = "intake_rejected"
                record["decision"] = "reject"
                record["error"] = meta.get("intake_rejected_reason", "intake gate failure")
                record["duration_s"] = round(time.time() - t0, 3)
                return record
        except Exception:
            pass
        record["stage_reached"] = "pending"
        record["outcome"] = "no_decision"
        record["duration_s"] = round(time.time() - t0, 3)
        return record

    decision = decision_data.get("decision", "?")
    gate_failures = decision_data.get("gate_failures", [])

    # Try to get review metadata for richer records
    review_route = None
    rubric_scores = None
    major_issues_count = None
    overclaim = None
    claim_support = None
    synthesis_quality = None

    try:
        prov_resp = requests.get(
            f"{base_url}/submissions/{submission_id}/provenance",
            headers=headers,
            timeout=10,
        )
        if prov_resp.status_code == 200:
            prov_data = prov_resp.json()
            reviews = prov_data.get("reviews", [])
            if reviews:
                last_review = reviews[-1]
                review_route = last_review.get("route")
                rubric_scores = last_review.get("rubric_scores")
                major_issues_count = len(last_review.get("major_issues", []))
                overclaim = last_review.get("overclaim_verdict")
                claim_support = last_review.get("claim_support_verdict")
                synthesis_quality = last_review.get("synthesis_quality_verdict")
                record["tokens_in"] = last_review.get("tokens_in", 0)
                record["tokens_out"] = last_review.get("tokens_out", 0)
                record["cost_usd"] = last_review.get("cost_usd", 0.0)
    except Exception:
        pass

    record["stage_reached"] = "done"
    record["outcome"] = decision
    record["decision"] = decision
    record["route"] = review_route
    record["rubric_scores"] = rubric_scores
    record["major_issues_count"] = major_issues_count
    record["overclaim"] = overclaim
    record["claim_support"] = claim_support
    record["synthesis_quality"] = synthesis_quality
    record["duration_s"] = round(time.time() - t0, 3)

    if gate_failures:
        record["stage_reached"] = "intake"
        record["outcome"] = "intake_rejected"
        record["error"] = gate_failures

    return record


def main() -> None:
    parser = argparse.ArgumentParser(description="Researka v2 VPS benchmark runner")
    parser.add_argument("--count", type=int, default=200, help="Number of synthetic papers")
    parser.add_argument("--base-url", default="http://49.12.7.18:8000", help="VPS API base URL")
    parser.add_argument("--output", default=None, help="Output artifact path (defaults to artifacts/benchmark_baseline.json)")
    parser.add_argument("--resume-from", type=int, default=0, help="Skip first N papers (resume run)")
    parser.add_argument("--input-json", default=None, help="Optional benchmark corpus JSON path")
    args = parser.parse_args()

    api_key = os.environ.get("RESEARKA_V2_API_KEY")
    if not api_key:
        print("RESEARKA_V2_API_KEY must be set for VPS benchmark runs.")
        sys.exit(1)
    base_url = args.base_url.rstrip("/")
    output = args.output or "artifacts/benchmark_baseline.json"

    # Verify health
    try:
        resp = requests.get(f"{base_url}/health", timeout=10)
        resp.raise_for_status()
        print(f"VPS healthy: {resp.json()}")
    except Exception as e:
        print(f"VPS health check failed: {e}")
        sys.exit(1)

    print(f"Provider: judge_panel (VPS)")
    if args.input_json:
        papers = load_benchmark_papers(args.input_json)
        count = len(papers)
        print(f"Input corpus: {args.input_json}")
    else:
        count = args.count
        papers = generate_papers(count)
    print(f"Papers: {count}")
    print(f"Base URL: {base_url}")
    print(f"Resume from: {args.resume_from}")
    print()
    records = []
    t_start = time.time()

    # Resume support: load existing records if resuming
    if args.resume_from > 0 and os.path.exists(output):
        with open(output) as f:
            existing = json.load(f)
        records = existing.get("papers", [])
        if len(records) >= args.resume_from:
            records = records[:args.resume_from]
            print(f"Resumed {args.resume_from} records from {output}")

    for i, paper in enumerate(papers[args.resume_from:], args.resume_from + 1):
        quality = paper.get("_benchmark_quality", "medium")
        domain = paper.get("domain_slug", "general")
        print(f"[{i}/{count}] Q={quality} | {domain}", end="", flush=True)

        record = submit_and_drain(paper, base_url, api_key)
        status = record["decision"] or record["outcome"]
        cost_str = f" ${record['cost_usd']:.4f}" if record["cost_usd"] > 0 else ""
        print(f" -> {status}{cost_str} ({record['duration_s']:.2f}s)")
        records.append(record)

        # Save checkpoint every 20 papers
        if i % 20 == 0:
            elapsed = round(time.time() - t_start, 2)
            agg_so_far = aggregate(records)
            print(f"  [checkpoint] {i}/{count} done. "
                  f"accept={agg_so_far['accepts']} revise={agg_so_far['revises']} "
                  f"reject={agg_so_far['rejects']} intake_rej={agg_so_far['intake_rejected']} "
                  f"cost=${agg_so_far['total_cost_usd']:.4f} elapsed={elapsed:.1f}s")
            _save_checkpoint(output, records, agg_so_far)

        # Rate limiting: 5s between submissions to avoid provider rate-limiting
        time.sleep(5.0)

    elapsed = round(time.time() - t_start, 2)
    agg = aggregate(records)

    artifact = {
        "run_meta": {
            "provider": "judge_panel",
            "target": base_url,
            "paper_count": count,
            "elapsed_s": elapsed,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
        "aggregates": agg,
        "papers": records,
    }

    os.makedirs(os.path.dirname(output), exist_ok=True)
    with open(output, "w") as f:
        json.dump(artifact, f, indent=2)

    print()
    print("=" * 70)
    print("VPS BENCHMARK RESULTS")
    print("=" * 70)
    print(f"Total papers:    {agg['total']}")
    print(f"Accuracy:        {agg['correct']} / {agg['total']} ({agg['accuracy']:.1%})")
    print(f"Accept:          {agg['accepts']} ({agg['accept_rate']:.1%})")
    print(f"Revise:          {agg['revises']} ({agg['revise_rate']:.1%})")
    print(f"Reject:          {agg['rejects']} ({agg['reject_rate']:.1%})")
    print(f"Intake rejected: {agg['intake_rejected']}")
    print(f"Errors:          {agg['errors']} ({agg['error_rate']:.1%})")
    print(f"Disagreement:    {agg['tiebreak_count']} / {agg['completed']} ({agg['disagreement_rate']:.1%})")
    print(f"Total cost:      ${agg['total_cost_usd']:.4f}")
    print(f"Median duration: {agg['median_duration_s']:.3f}s")
    print(f"Elapsed:         {elapsed:.1f}s")
    print()
    print("By quality:")
    for q, stats in agg.get("by_quality", {}).items():
        print(f"  {q:8s}  n={stats['count']:4d}  expected={stats['expected_decision']:6s}  "
              f"accuracy={stats['accuracy']:.0%}  accept={stats['accept_rate']:.0%}  "
              f"revise={stats['revise_rate']:.0%}  reject={stats['reject_rate']:.0%}  "
              f"intake_rej={stats['intake_reject_rate']:.0%}")
    print()
    print("Confusion matrix:")
    for expected, actuals in agg.get("confusion_matrix", {}).items():
        cells = "  ".join(f"{actual}={count}" for actual, count in actuals.items())
        print(f"  expected {expected:6s} -> {cells}")
    print()
    print(f"Artifact saved to: {output}")


def _save_checkpoint(path: str, records: list[dict], agg: dict) -> None:
    """Save current state as checkpoint."""
    checkpoint = {
        "run_meta": {
            "provider": "judge_panel",
            "target": "checkpoint",
            "paper_count": len(records),
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
        "aggregates": agg,
        "papers": records,
    }
    with open(path, "w") as f:
        json.dump(checkpoint, f, indent=2)


if __name__ == "__main__":
    main()
