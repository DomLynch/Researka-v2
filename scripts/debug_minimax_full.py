#!/usr/bin/env python3
"""Debug: get full MiniMax response."""
import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from runtime_core.providers import MiniMaxProvider, ProviderRequest
from runtime_core.prompts import REVIEWER_PROMPT_VERSION

input_path = "/Users/domininclynch/Downloads/researka_5_rapid_evidence_syntheses_2026.json"
with open(input_path) as f:
    sub = json.load(f)[0]

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

submission_summary = json.dumps({
    "title": sub["title"],
    "abstract": sub.get("abstract", ""),
    "sections": sub.get("sections", {}),
    "source_bundle": sub.get("source_bundle", []),
    "domain_slug": sub.get("domain_slug", "general"),
}, ensure_ascii=False)

provider = MiniMaxProvider()
result = provider.complete(ProviderRequest(
    system_prompt=system_prompt,
    user_prompt=f"Review this submission and return strict JSON only:\n{submission_summary}",
    prompt_version=REVIEWER_PROMPT_VERSION,
    response_format="json_object",
    timeout_sec=60,
))

if result.response:
    raw = result.response.text
    print(f"FULL RESPONSE ({len(raw)} chars, {result.response.usage.output_tokens} output tokens):")
    print("=" * 60)
    print(raw)
    print("=" * 60)
    # Show last 500 chars
    print(f"\nLAST 500 CHARS:")
    print(raw[-500:])
