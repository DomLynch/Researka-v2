#!/usr/bin/env python3
"""Debug: capture raw MiniMax response for submission 1."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from runtime_core.providers import MiniMaxProvider, ProviderRequest
from runtime_core.prompts import REVIEWER_PROMPT_VERSION
from runtime_core.workflow import WorkflowEngine


def main() -> None:
    input_path = "/Users/domininclynch/Downloads/researka_5_rapid_evidence_syntheses_2026.json"
    with open(input_path) as f:
        submissions_data = json.load(f)

    sub = submissions_data[0]

    system_prompt = (
        "You are the Researka rapid evidence synthesis reviewer. Return strict JSON only. "
        "Do not output chain-of-thought, analysis tags, prose preambles, or markdown fences.\n\n"
        "Rubric (score each 1-5):\n"
        "- research_question_quality: is the question specific and directly answered?\n"
        "- synthesis_quality: do key findings synthesize evidence rather than just summarizing?\n"
        "- claim_evidence_alignment: are the strongest claims proportionate to bundle strength?\n"
        "- limitations_quality: do limitations materially constrain the conclusion?\n"
        "- gaps_quality: are identified gaps real and relevant?\n"
        "- source_grounding: do bundle citations support the main thesis, not just cite literature?\n\n"
        "Accept requires: all rubric scores >= 4, zero major_issues, claim_support == 'supported', "
        "overclaim == 'none'. Accept should be rare.\n"
        "Revise is the default for structurally valid but substantively weak submissions.\n"
        "Reject triggers on: substantively empty sections, major claims outrunning source bundle, "
        "conclusion depending on speculative extrapolation, or review-shaped paper without real synthesis.\n\n"
        "Return this JSON exactly:\n"
        '{ "recommendation": "accept|revise|reject", '
        '"rubric_scores": { "research_question_quality": 1-5, "synthesis_quality": 1-5, '
        '"claim_evidence_alignment": 1-5, "limitations_quality": 1-5, "gaps_quality": 1-5, '
        '"source_grounding": 1-5 }, '
        '"major_issues": ["..."], "minor_issues": ["..."], "required_revisions": ["..."], '
        '"claim_support_verdict": "supported|partially_supported|unsupported", '
        '"overclaim_verdict": "none|mild|significant", '
        '"synthesis_quality_verdict": "strong|adequate|weak|empty", '
        '"review_markdown": "..." }'
    )

    submission_summary = json.dumps(
        {
            "title": sub["title"],
            "abstract": sub.get("abstract", ""),
            "sections": sub.get("sections", {}),
            "source_bundle": sub.get("source_bundle", []),
            "domain_slug": sub.get("domain_slug", "general"),
        },
        ensure_ascii=False,
    )

    print(f"Title: {sub['title'][:80]}...")
    print(f"Sections: {list(sub.get('sections', {}).keys())}")
    print(f"Source bundle: {len(sub.get('source_bundle', []))} entries")
    print()

    provider = MiniMaxProvider()
    result = provider.complete(ProviderRequest(
        system_prompt=system_prompt,
        user_prompt=f"Review this submission and return strict JSON only:\n{submission_summary}",
        prompt_version=REVIEWER_PROMPT_VERSION,
        response_format="json_object",
        timeout_sec=60,
    ))

    print(f"OK: {result.ok}")
    if result.response:
        raw = result.response.text
        print(f"\n--- RAW RESPONSE ({len(raw)} chars) ---")
        print(raw[:2000])
        if len(raw) > 2000:
            print(f"\n... ({len(raw) - 2000} more chars)")
        print("--- END RAW ---\n")

        # Try parsing like the workflow does
        try:
            parsed = WorkflowEngine._parse_json_object(None, raw)
            print(f"PARSE: OK")
            print(f"  recommendation: {parsed.get('recommendation')}")
            print(f"  rubric_scores: {parsed.get('rubric_scores')}")
            print(f"  major_issues count: {len(parsed.get('major_issues', []))}")
            print(f"  overclaim_verdict: {parsed.get('overclaim_verdict')}")
        except Exception as e:
            print(f"PARSE ERROR: {e}")

        print(f"\nCost: ${result.response.usage.cost_usd:.4f}")
        print(f"Tokens: {result.response.usage.input_tokens}+{result.response.usage.output_tokens}")

    if result.error:
        print(f"ERROR: {result.error}")


if __name__ == "__main__":
    main()
