#!/usr/bin/env python3
"""Test MiniMax with tightened prompt + 3000 max_tokens."""
import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from runtime_core.providers import MiniMaxProvider, ProviderRequest
from runtime_core.prompts import REVIEWER_PROMPT_VERSION
from runtime_core.workflow import WorkflowEngine

input_path = "/Users/domininclynch/Downloads/researka_5_rapid_evidence_syntheses_2026.json"
with open(input_path) as f:
    sub = json.load(f)[0]

system_prompt = (
    "You are the Researka rapid evidence synthesis reviewer. Output JSON ONLY. "
    "No reasoning. No analysis. No preambles. No markdown fences. No prose. "
    "Output one JSON object, nothing else.\n\n"
    "Rubric (score each 1-5):\n"
    "- research_question_quality: specific and directly answered?\n"
    "- synthesis_quality: synthesizes, not just summarizes?\n"
    "- claim_evidence_alignment: claims proportionate to bundle?\n"
    "- limitations_quality: materially constrains conclusion?\n"
    "- gaps_quality: real and relevant?\n"
    "- source_grounding: citations support thesis?\n\n"
    "accept = all scores >= 4, zero major_issues, claim_support=supported, overclaim=none. Rare.\n"
    "revise = default for valid but weak.\n"
    "reject = empty sections, claims outrun bundle, speculative extrapolation.\n\n"
    '{"recommendation":"accept|revise|reject","rubric_scores":{'
    '"research_question_quality":1-5,"synthesis_quality":1-5,'
    '"claim_evidence_alignment":1-5,"limitations_quality":1-5,'
    '"gaps_quality":1-5,"source_grounding":1-5},'
    '"major_issues":["..."],"minor_issues":["..."],"required_revisions":["..."],'
    '"claim_support_verdict":"supported|partially_supported|unsupported",'
    '"overclaim_verdict":"none|mild|significant",'
    '"synthesis_quality_verdict":"strong|adequate|weak|empty",'
    '"review_markdown":"..."}'
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
    user_prompt=f"Review this submission and return JSON only:\n{submission_summary}",
    prompt_version=REVIEWER_PROMPT_VERSION,
    response_format="json_object",
    timeout_sec=60,
    max_output_tokens=3000,
))

print(f"OK: {result.ok}")
if result.response:
    raw = result.response.text
    print(f"Output tokens: {result.response.usage.output_tokens}")
    print(f"Response length: {len(raw)} chars")
    print()

    try:
        parsed = WorkflowEngine._parse_json_object(None, raw)
        print(f"PARSE: OK")
        print(f"  recommendation: {parsed.get('recommendation')}")
        print(f"  rubric_scores: {parsed.get('rubric_scores')}")
        print(f"  major_issues: {parsed.get('major_issues')}")
        print(f"  claim_support_verdict: {parsed.get('claim_support_verdict')}")
        print(f"  overclaim_verdict: {parsed.get('overclaim_verdict')}")
        print(f"  synthesis_quality_verdict: {parsed.get('synthesis_quality_verdict')}")
        print(f"  review_markdown: {parsed.get('review_markdown', '')[:200]}...")
    except Exception as e:
        print(f"PARSE ERROR: {e}")
        print(f"\nLast 500 chars of response:")
        print(raw[-500:])

    print(f"\nCost: ${result.response.usage.cost_usd:.4f}")
if result.error:
    print(f"ERROR: {result.error}")
