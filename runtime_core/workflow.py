from __future__ import annotations

import json
from typing import Any

from contracts import ArticleType, Decision, ObjectType, ResearchObject, RuntimeJob, Stage, WorkflowContext, WorkflowOutcome, publication_template_for, run_submission_template_checks

from .compiler import compile_publication
from .derivation_web import emit_decision_to_derivation_web, emit_publication_to_derivation_web
from .integrity_client import check_integrity, index_integrity
from .osf import mint_publication_doi_from_repository, osf_publication_metadata_from_env
from .prompts import EDITOR_PROMPT_VERSION, REVIEWER_PROMPT_VERSION
from .providers import LanguageModelProvider, ProviderRequest
from .review_contract import (
    CLAIM_SUPPORT_VERDICTS,
    OVERCLAIM_VERDICTS,
    REVIEW_RUBRIC_KEYS,
    SYNTHESIS_QUALITY_VERDICTS,
    accept_contract_failure,
    accept_contract_satisfied,
)
from .reviewer_panel import reviewer_from_env
from .repos import RuntimeRepository
from .sanitizer import extract_markdown_section


PUBLICATION_DEDUPE_METADATA_KEYS = (
    "submission_identity_key",
    "submission_payload_hash",
    "content_hash",
    "source_citation_hash",
    "author_signature",
)


def _calibrated_recommendation(
    recommendation: str,
    rubric_scores: dict[str, int],
    *,
    major_issues: list[str],
    required_revisions: list[str],
    claim_support: str,
    overclaim: str,
    synthesis_quality: str,
) -> str:
    if recommendation == "revise" and accept_contract_satisfied(
        rubric_scores,
        major_issues=major_issues,
        required_revisions=required_revisions,
        claim_support=claim_support,
        overclaim=overclaim,
        synthesis_quality=synthesis_quality,
    ):
        return "accept"
    return recommendation


def _publication_identity_metadata(submission_metadata: dict) -> dict:
    keys = (
        *PUBLICATION_DEDUPE_METADATA_KEYS,
        "run_id",
        "topic",
        "domain_slug",
        "category",
        "revision_of",
        "identity_source",
        "authenticated_agent_id",
        "claimed_author_agent_id",
        "human_owner_id",
        "human_owner_name",
        "human_owner_orcid",
        "author_orcid",
        "orcid",
        "orcid_attribution",
        "orcid_verified_at",
        "submitter_name",
        "submitter_orcid",
        "authors",
        "institution_name",
        "institution_ror",
        "ror_id",
        "raid_id",
    )
    metadata = {key: submission_metadata[key] for key in keys if submission_metadata.get(key)}
    if metadata.get("ror_id") and not metadata.get("institution_ror"):
        metadata["institution_ror"] = metadata["ror_id"]
    if metadata.get("orcid"):
        metadata["orcid_at_publication"] = metadata["orcid"]
    return metadata


def _publication_dedupe_markers(metadata: dict) -> set[str]:
    return {
        str(value).strip()
        for key in PUBLICATION_DEDUPE_METADATA_KEYS
        if (value := metadata.get(key)) and str(value).strip()
    }


def _integrity_signal_metadata(integrity: dict[str, Any], recommendation: str) -> dict[str, object]:
    duplication_score = integrity.get("duplication_score")
    similarity_score = integrity.get("similarity_score", duplication_score)
    matched_sources = integrity.get("matched_sources")
    if not isinstance(matched_sources, list):
        matched_sources = []
    return {
        "recommendation": recommendation or integrity.get("recommendation") or "pass",
        "available": bool(integrity.get("available", True)),
        "matched_publication_id": integrity.get("matched_publication_id"),
        "duplication_score": duplication_score,
        "similarity_score": similarity_score,
        "plagiarism_flag": bool(integrity.get("plagiarism_flag") or recommendation in {Decision.REJECT.value, Decision.REVISE.value}),
        "matched_sources": matched_sources[:5],
        "breakdown": integrity.get("breakdown") or {},
        "feedback_for_agent": str(integrity.get("feedback_for_agent") or "").strip() or None,
    }


def _mint_publication_doi(repository: RuntimeRepository, publication: ResearchObject) -> dict:
    return mint_publication_doi_from_repository(repository, publication)


def _integrity_payload_from_submission(submission: ResearchObject) -> dict[str, Any]:
    return {
        "submission_id": submission.id,
        "title": submission.title,
        "abstract": str(submission.metadata.get("abstract", "")).strip(),
        "citations": list(submission.metadata.get("source_bundle", [])),
        "article_type": submission.metadata.get("article_type", ArticleType.RAPID_EVIDENCE_SYNTHESIS.value),
        "domain": submission.metadata.get("domain_slug", "default") or "default",
    }


def _integrity_payload_from_publication(publication: ResearchObject, submission: ResearchObject) -> dict[str, Any]:
    payload = _integrity_payload_from_submission(submission)
    payload.update(
        {
            "publication_id": publication.id,
            "title": publication.title,
            "abstract": str(publication.metadata.get("abstract") or payload["abstract"]).strip(),
            "article_type": publication.metadata.get("article_type", payload["article_type"]),
        }
    )
    return payload


def _submission_full_body_markdown(submission: ResearchObject) -> str | None:
    metadata_body = submission.metadata.get("body_markdown")
    if isinstance(metadata_body, str) and metadata_body.strip():
        return metadata_body
    body = str(submission.body_markdown or "")
    if extract_markdown_section(body, "Abstract") and extract_markdown_section(body, "References"):
        return body
    return None


class WorkflowEngine:
    def __init__(self, provider: LanguageModelProvider | None = None) -> None:
        self.provider = provider or reviewer_from_env()

    def _review_system_prompt(self, article_type: str) -> str:
        if article_type == ArticleType.ALPHA_MEMO.value:
            article_specific = (
                "You are the Researka alpha-memo reviewer. Judge this as an Agent-Certified Evidence Map: "
                "a short research-intelligence artifact, not a PRISMA-complete systematic review, clinical guideline, "
                "or full research paper. Reward novelty only when it is bounded, source-grounded, and visibly falsifiable.\n\n"
                "Alpha-memo review checks:\n"
                "- Check whether the memo makes one bounded, source-grounded research signal clear.\n"
                "- Score whether novelty claims stay proportionate to the cited receipts.\n"
                "- Flag unsupported clinical, policy, investment, or broad consensus claims.\n\n"
                "Alpha-memo accept threshold:\n"
                "- Accept can be based on a small source bundle when the claim is narrow, receipt-backed, and honest about limits.\n"
                "- Reject when the memo is source-free, hype-framed, or asks readers to treat a lead signal as settled consensus.\n\n"
            )
        elif article_type == ArticleType.RESEARCH_SYNTHESIS.value:
            article_specific = (
                "You are the Researka research synthesis reviewer. Judge this as a long-form, gatekeeper-tier "
                "research synthesis manuscript with a rich 12+ source evidence corpus, "
                "explicit cross-domain integration, numeric traceability, and clear separation of mechanistic / "
                "preclinical evidence from clinical / human evidence.\n\n"
                "This is the v2 publishing-grade path. The bar is HIGHER than rapid evidence synthesis. Reward depth, "
                "rigour, and verifiable traceability; penalize shallow review, untraceable numerics, and unhedged "
                "preclinical-to-clinical leaps.\n\n"
                "Research-synthesis review checks:\n"
                "- Check whether methods, search corpus, and inclusion logic are explicit enough to audit the synthesis at scale.\n"
                "- Score whether claims, numerics, and conclusions trace cleanly to the cited evidence — reward verifiable traceability.\n"
                "- Reward explicit cross-domain synthesis: how the paper integrates findings across outcome classes, populations, and study designs.\n"
                "- Reward clear separation of mechanistic / preclinical evidence from clinical / human evidence, and appropriate hedging at the bridge.\n"
                "- Flag unsupported escalation from preclinical mechanism to clinical recommendation or from narrow population to broad policy.\n"
                "- Flag absent or generic limitations on a long, ambitious synthesis — substantive scope demands substantive limits.\n\n"
                "Research-synthesis calibration rules:\n"
                "- Section structure differs from RES: expect Abstract, Introduction, Methods, Results, Discussion, Limitations, Conclusion at minimum, with Background, Inferential Bridge, Quantitative Evidence Index, Cross-Domain Synthesis as recommended depth sections.\n"
                "- Treat the Abstract as carrying the research question (synthesis papers do not separate it into its own section).\n"
                "- Reward the presence of recommended depth sections (Background, Inferential Bridge, Quantitative Evidence Index, Cross-Domain Synthesis) — they signal a more rigorous artefact, not bloat.\n"
                "- Reward an explicit Quantitative Evidence Index or numeric tables with study/endpoint/arm/value/CI columns — that is gatekeeper-tier traceability.\n"
                "- Reward explicit identification of cross-domain tensions (e.g. positive in immune, negative in muscle function) over a narrative that smooths them away.\n"
                "- A synthesis paper that says 'mechanistic plausibility coexists with sparse human data' is being honest, not weak — that is the correct verdict for many geroscience topics in 2026.\n"
                "- Do NOT penalize for length, depth, or methodological discussion — those are features for this article type.\n"
                "- DO penalize for: numerics with no source attribution, unhedged extrapolation from animal models to human dosing, missing limitations on a 20k-word claim space, or claims that do not appear in the cited corpus.\n\n"
                "Research-synthesis accept threshold:\n"
                "- Accept requires all the standard accept conditions PLUS substantive depth: at least one recommended section present, identifiable cross-domain integration, numerics that appear traceable.\n"
                "- A short synthesis that meets only the bare required-sections bar should revise, not accept, even if its claims are bounded — the article-type promises depth.\n\n"
            )
        elif article_type == ArticleType.EMPIRICAL_STUDY.value:
            article_specific = (
                "You are the Researka empirical study reviewer. Judge this as a manuscript that reports one study or dataset, "
                "not as a rapid evidence synthesis. Reward clear methods, bounded claims, honest limits, and results that match the stated question.\n\n"
                "Empirical-study review checks:\n"
                "- Check whether methods, measurements, and inclusion logic are explicit enough to audit the study design.\n"
                "- Score whether results and conclusions stay proportionate to the data actually reported in the manuscript.\n"
                "- Flag unsupported leaps from one dataset, cohort, or model system to broad policy, deployment, or causal claims.\n\n"
                "Empirical-study calibration rules:\n"
                "- A terser trial, dataset, or experimental manuscript can still be accept when it reports concrete outcomes, effect sizes or test statistics, honest limits, and a bounded conclusion.\n"
                "- Do not mark claim_support unsupported just because the manuscript reports one primary study rather than a multi-study synthesis. Unsupported means the conclusion outruns the reported results.\n"
                "- When routed benchmark manuscripts use Search Summary and Key Findings headings, treat them as stand-ins for methods and results context rather than as missing empirical structure.\n\n"
            )
        elif article_type == ArticleType.EVIDENCE_MAP.value:
            article_specific = (
                "You are the Researka evidence-map reviewer. Judge this as a faithful landscape of N findings on a "
                "source-rich topic that genuinely does not collapse to one claim (e.g. metformin's scattered, "
                "heterogeneous findings). This is a legitimate publication type, not a failed synthesis.\n\n"
                "Evidence-map review checks:\n"
                "- Check whether scope and search summary make the landscape's boundaries auditable.\n"
                "- Score whether every mapped finding is attributed to specific cited sources.\n"
                "- Reward faithful mapping of heterogeneity, tension, and disagreement across findings.\n"
                "- Flag any collapse of the landscape into one unsupported causal, clinical, or policy conclusion.\n\n"
                "Evidence-map calibration rules:\n"
                "- Do NOT require a single bounded thesis or unifying claim — the article type's value is breadth and honest mapping, not convergence. Absence of one headline claim is correct here, not a flaw.\n"
                "- claim_support is 'supported' when each mapped finding traces to its sources; it is 'unsupported' only when the map asserts links, effects, or a synthesis the cited evidence does not show.\n"
                "- Reward an explicit Tensions and Gaps section that surfaces contradictions rather than smoothing them away.\n"
                "- Do not penalize for not picking a winner among conflicting findings — that is the map's job.\n\n"
                "Evidence-map accept threshold:\n"
                "- Accept when the scope is bounded, the findings are source-attributed, heterogeneity is represented honestly, and nothing is overclaimed into a single conclusion.\n"
                "- Reject when findings are unsourced, fabricated, or the map quietly editorializes a settled answer the evidence does not support.\n\n"
            )
        else:
            article_specific = (
                "You are the Researka rapid evidence synthesis reviewer. Judge this as a source-grounded synthesis, "
                "not as a primary empirical study. Reward explicit search scope, bounded claims, honest limits, and synthesis over summary.\n\n"
                "Rapid-synthesis review checks:\n"
                "- Check whether the search summary is explicit enough to audit the scope of the rapid synthesis.\n"
                "- Score whether key findings stay proportionate to the directly cited evidence.\n"
                "- Flag unsupported escalation from a narrow bundle to broad causal, deployment, or policy claims.\n\n"
            )
        return (
            f"{article_specific}"
            "Calibration triage:\n"
            "- First make a forced triage call: elite-tier accept, competent-but-fixable revise, or fundamentally flawed reject.\n"
            "- Do not use revise as a safe default for unclear cases. Decide whether the paper is closer to accept or closer to reject.\n"
            "- But when the manuscript is credible yet explicitly incomplete — mixed findings, sparse human data, heterogeneous evidence, or no broad population-level proof — revise is the correct answer, not accept.\n"
            "- Reserve revise for papers that are mostly correct and fixable with bounded edits. If the paper needs a scope reset or its claims are materially unsupported, reject instead.\n\n"
            "Style invariance rules:\n"
            "- Judge substance, not house style. Terseness, verbosity, passive voice, or different academic cadence are not defects by themselves.\n"
            "- Do not reward a manuscript for sounding like Researka house style if the evidence is weak.\n"
            "- Do not punish a manuscript for sounding external or compressed if the search scope, claims, and limits are still explicit and bounded.\n\n"
            "Source bundle calibration:\n"
            "- Reference-only source bundles (title + DOI only, no abstracts) are valid and common in elite academic publications.\n"
            "- Score source_grounding >= 4 when citations are accurate, recent (within 5 years), and directly support the manuscript's thesis.\n"
            "- Do not penalize source_grounding for missing abstracts or brief source descriptions. Judge whether the cited sources actually exist and support the claims.\n\n"
            "Hedging language calibration:\n"
            "- Academic hedging language (may, could, suggests, indicates, could suggest, are consistent with, may indicate) is normal scholarly practice.\n"
            "- Hedging does NOT indicate weak evidence, unsupported claims, or overclaim. It is the opposite of overclaim.\n"
            "- Do not penalize claim_evidence_alignment or overclaim for papers that use hedging language appropriately.\n"
            "- Only penalize claim_evidence_alignment when the paper makes strong causal, deployment, or policy claims without hedging, while the cited evidence does not support such claims.\n"
            "- Score claim_evidence_alignment >= 4 when claims are proportionate to cited evidence, even if hedged.\n\n"
            "Synthesis quality calibration:\n"
            "- Elite-style papers may use different organizational structures than the 7-section house format.\n"
            "- A coherent argument that integrates methods, results, or evidence — even if terse or differently structured — should score >= 3 on synthesis_quality.\n"
            "- Reserve score 1 for papers that are truly a loose summary with no integration or coherence.\n\n"
            "Major issues calibration:\n"
            "- Reference-only source bundles, hedging language, and different organizational styles are NOT major issues.\n"
            "- Major issues are: contradictory claims, materially unsupported claims, methodological errors, or missing key limitations on conclusions that are stated with high confidence.\n"
            "- Do not count style differences, hedging, or terse prose as major issues.\n\n"
            "Exact statistics calibration for reference-only bundles:\n"
            "- When source bundles are reference-only (title + DOI, no abstracts), you cannot verify exact statistics from bundle titles.\n"
            "- In reference-only cases, default to assuming exact statistics (percentages, CIs, p-values) reported in the manuscript are accurately drawn from the cited sources, unless the numbers are internally contradictory or obviously implausible.\n"
            "- Do not penalize source_grounding or claim_evidence_alignment for reporting exact statistics that you cannot cross-check against bundle titles.\n"
            "- When source bundles DO contain abstracts or full text, evaluate normally — exact statistics must match the source material.\n\n"
            "Decision anchors:\n"
            "- Anchor A (accept): bounded manuscript, claims directly supported, no major issues, no required revisions, claim_support=supported, overclaim=none, recommendation=accept.\n"
            "- Anchor B (revise): manuscript is mostly correct and salvageable with bounded edits, but still has partial support, mild overclaim, or one materially weak dimension, recommendation=revise.\n"
            "- Anchor C (reject): manuscript is structurally broken, needs a scope reset, or makes materially unsupported claims that require more than bounded edits, recommendation=reject.\n\n"
            "Style exemplars:\n"
            "- House-style accept: seven clean sections, direct sentences, explicit search scope, bounded conclusion, recommendation=accept.\n"
            "- House-style revise: seven clean sections and confident direct prose are still revise when the manuscript itself says findings are mixed, human data are sparse, heterogeneity limits aggregation, or broad population benefit remains unproven.\n"
            "- Terser-style accept: shorter sections and clipped sentences are acceptable when the cited bundle directly supports the bounded claim, recommendation=accept.\n"
            "- Verbose-style accept: longer narrative prose is acceptable when every paragraph still maps back to the evidence bundle and does not overclaim, recommendation=accept.\n"
            "- External-style accept: academic phrasing, passive voice, or different sentence rhythm are acceptable when the manuscript still answers the question directly and stays within the evidence, recommendation=accept.\n\n"
            "Output JSON ONLY. No reasoning. No analysis. No preambles. No markdown fences. No prose. "
            "Output one JSON object, nothing else.\n\n"
            "Rubric (score each 1-5):\n"
            "- research_question_quality: specific and directly answered? Score 1 if vague or absent, 3 if present but broad, 5 if specific and directly answered.\n"
            "- synthesis_quality: does the body integrate methods, results, or evidence into a coherent argument rather than a loose summary? Score 1 if purely a list with no integration, 3 if some integration but uneven, 5 if well-integrated argument.\n"
            "- claim_evidence_alignment: are claims proportionate to the cited bundle or reported results? Score 1 if claims are contradicted by evidence, 3 if claims are supported but hedged, 5 if claims are directly and proportionately supported.\n"
            "- limitations_quality: do limitations materially constrain the conclusion? Score 1 if absent, 3 if present but generic, 5 if specific and material.\n"
            "- gaps_quality: are next-step gaps or unresolved uncertainties real and relevant? Score 1 if absent, 3 if present but generic, 5 if specific and actionable.\n"
            "- source_grounding: do citations or reported results actually support the thesis? Score 1 if sources do not support thesis, 3 if sources partially support, 5 if sources directly and comprehensively support.\n\n"
            "accept = all scores >= 4, zero major_issues, claim_support=supported, overclaim=none. Rare. "
            "Accept is invalid when the manuscript explicitly says evidence is mixed, human data are sparse, broad benefit remains unproven, or the conclusion is only mechanistically credible.\n"
            "If any score is below 4 or major_issues is non-empty, recommendation must be revise or reject, never accept.\n"
            "revise = at least one score < 4 or non-empty major_issues, but the manuscript is still salvageable with bounded edits and required_revisions lists concrete fixes.\n"
            "Do not label accept-quality papers as revise for minor wording polish only; put polish in minor_issues and recommend accept.\n"
            "reject = structurally broken, needs scope reset, or claims materially unsupported beyond bounded edits.\n\n"
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

    def _static_provider_metadata(self, *, prompt_version: str) -> dict[str, str | int | float]:
        provider = getattr(self.provider, "provider", self.provider.__class__.__name__.lower())
        model = getattr(self.provider, "model", provider)
        return {
            "prompt_version": prompt_version,
            "provider": provider,
            "model": model,
            "tokens_in": 0,
            "tokens_out": 0,
            "cost_usd": 0.0,
        }

    def _review_submission(self, submission: ResearchObject) -> tuple[str, str, dict[str, object]]:
        article_type = str(submission.metadata.get("article_type", ArticleType.RAPID_EVIDENCE_SYNTHESIS.value))
        template = publication_template_for(article_type)
        system_prompt = self._review_system_prompt(article_type)
        submission_summary = json.dumps(
            {
                "title": submission.title,
                "article_type": article_type,
                "template_label": template.label,
                "review_checks": list(template.review_checks),
                "abstract": submission.metadata.get("abstract", ""),
                "sections": submission.metadata.get("sections", {}),
                "source_bundle": submission.metadata.get("source_bundle", []),
                "domain_slug": submission.metadata.get("domain_slug", "general"),
            },
            ensure_ascii=False,
        )
        result = self.provider.complete(
            ProviderRequest(
                system_prompt=system_prompt,
                user_prompt=f"Review this submission and return JSON only:\n{submission_summary}",
                prompt_version=REVIEWER_PROMPT_VERSION,
                response_format="json_object",
                max_output_tokens=3000,
            )
        )
        if not result.ok or result.response is None:
            error_class = result.error.error_class.value if result.error else "other"
            message = result.error.message if result.error else "provider_failed"
            raise ValueError(f"provider_error:{error_class}:{message}")
        payload = self._parse_json_object(result.response.text)
        recommendation = str(payload.get("recommendation", "")).strip().lower()
        if recommendation not in {"accept", "revise", "reject"}:
            raise ValueError("provider_error:bad_request:invalid_review_recommendation")
        review_markdown = str(payload.get("review_markdown", "")).strip()
        if not review_markdown:
            raise ValueError("provider_error:bad_request:missing_review_markdown")
        rubric_scores, major_issues, minor_issues, required_revisions, claim_support, overclaim, synthesis_quality = self._validated_review_contract(
            payload,
            recommendation=recommendation,
        )
        original_recommendation = recommendation
        recommendation = _calibrated_recommendation(
            recommendation,
            rubric_scores,
            major_issues=major_issues,
            required_revisions=required_revisions,
            claim_support=claim_support,
            overclaim=overclaim,
            synthesis_quality=synthesis_quality,
        )
        metadata = {
            "prompt_version": REVIEWER_PROMPT_VERSION,
            "provider": result.response.provider,
            "model": result.response.model,
            "tokens_in": result.response.usage.input_tokens,
            "tokens_out": result.response.usage.output_tokens,
            "cost_usd": result.response.usage.cost_usd,
            **result.response.metadata,
            "article_type": article_type,
            "rubric_scores": rubric_scores,
            "major_issues": major_issues,
            "minor_issues": minor_issues,
            "required_revisions": required_revisions,
            "claim_support_verdict": claim_support,
            "overclaim_verdict": overclaim,
            "synthesis_quality_verdict": synthesis_quality,
        }
        if original_recommendation != recommendation:
            metadata["original_recommendation"] = original_recommendation
            metadata["recommendation_calibration"] = "minor_issues_only_accept_contract"
        return recommendation, review_markdown, metadata

    def _integrity_decision_metadata(self, submission: ResearchObject, integrity: dict[str, Any], recommendation: str) -> dict[str, object]:
        feedback = str(integrity.get("feedback_for_agent") or "").strip()
        reason = str(integrity.get("reason") or "integrity_duplicate").strip()
        return {
            "decision": recommendation,
            "notes": ["integrity check decision"],
            "article_type": submission.metadata.get("article_type", ArticleType.RAPID_EVIDENCE_SYNTHESIS.value),
            "failure_category": "integrity_duplicate",
            "failed_checks": [feedback or reason],
            "integrity": _integrity_signal_metadata(integrity, recommendation),
            **self._static_provider_metadata(prompt_version=EDITOR_PROMPT_VERSION),
        }

    def _validated_review_contract(
        self,
        payload: dict[str, object],
        *,
        recommendation: str,
    ) -> tuple[dict[str, int], list[str], list[str], list[str], str, str, str]:
        rubric_scores = payload.get("rubric_scores")
        if not isinstance(rubric_scores, dict):
            raise ValueError("provider_error:bad_request:missing_rubric_scores")
        normalized_scores: dict[str, int] = {}
        for key in REVIEW_RUBRIC_KEYS:
            value = rubric_scores.get(key)
            if not isinstance(value, int) or value < 1 or value > 5:
                raise ValueError(f"provider_error:bad_request:invalid_rubric_score:{key}")
            normalized_scores[key] = value

        def _list_field(name: str) -> list[str]:
            value = payload.get(name)
            if not isinstance(value, list):
                raise ValueError(f"provider_error:bad_request:missing_{name}")
            items = [str(item).strip() for item in value if str(item).strip()]
            return items

        major_issues = _list_field("major_issues")
        minor_issues = _list_field("minor_issues")
        required_revisions = _list_field("required_revisions")

        claim_support = str(payload.get("claim_support_verdict", "")).strip().lower()
        if claim_support not in CLAIM_SUPPORT_VERDICTS:
            raise ValueError("provider_error:bad_request:invalid_claim_support_verdict")
        overclaim = str(payload.get("overclaim_verdict", "")).strip().lower()
        if overclaim not in OVERCLAIM_VERDICTS:
            raise ValueError("provider_error:bad_request:invalid_overclaim_verdict")
        synthesis_quality = str(payload.get("synthesis_quality_verdict", "")).strip().lower()
        if synthesis_quality not in SYNTHESIS_QUALITY_VERDICTS:
            raise ValueError("provider_error:bad_request:invalid_synthesis_quality_verdict")

        if recommendation == "accept":
            failure = accept_contract_failure(
                normalized_scores,
                major_issues=major_issues,
                required_revisions=required_revisions,
                claim_support=claim_support,
                overclaim=overclaim,
                synthesis_quality=synthesis_quality,
            )
            if failure:
                raise ValueError(f"provider_error:bad_request:{failure}")
        if recommendation == "revise" and not required_revisions and not accept_contract_satisfied(
            normalized_scores,
            major_issues=major_issues,
            required_revisions=required_revisions,
            claim_support=claim_support,
            overclaim=overclaim,
            synthesis_quality=synthesis_quality,
        ):
            raise ValueError("provider_error:bad_request:revise_missing_required_revisions")

        return normalized_scores, major_issues, minor_issues, required_revisions, claim_support, overclaim, synthesis_quality

    @staticmethod
    def _parse_json_object(text: str) -> dict:
        raw = str(text or "").strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw
            raw = raw.rsplit("```", 1)[0].strip()
        start = raw.find("{")
        if start == -1:
            raise ValueError("provider_error:bad_request:review_json_missing")
        depth = 0
        end = -1
        in_string = False
        escape = False
        for i in range(start, len(raw)):
            ch = raw[i]
            if escape:
                escape = False
                continue
            if ch == "\\":
                escape = True
                continue
            if ch == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i
                    break
        if end == -1:
            raise ValueError("provider_error:bad_request:review_json_missing")
        try:
            parsed = json.loads(raw[start : end + 1])
        except json.JSONDecodeError:
            raise ValueError("provider_error:bad_request:review_json_parse_failed")
        if not isinstance(parsed, dict):
            raise ValueError("provider_error:bad_request:review_json_not_object")
        return parsed

    def handle_job(self, job: RuntimeJob, repository: RuntimeRepository) -> dict:
        if job.stage == Stage.INTAKE:
            return self._run_intake(job, repository)
        if job.stage == Stage.REVIEW:
            return self._run_review(job, repository)
        if job.stage == Stage.EDITORIAL:
            return self._run_editorial(job, repository)
        if job.stage == Stage.PUBLISH:
            return self._run_publish(job, repository)
        raise ValueError(f"Unsupported stage: {job.stage}")

    def plan_from_editorial(self, context: WorkflowContext, decision: Decision) -> WorkflowOutcome:
        if decision == Decision.ACCEPT:
            return WorkflowOutcome(
                terminal_decision=Decision.ACCEPT,
                next_jobs=[
                    RuntimeJob(
                        target_object_id=context.target_object_id,
                        stage=Stage.PUBLISH,
                        payload={"domain_slug": context.domain_slug},
                    )
                ],
                notes=["accepted and queued for publish"],
            )
        return WorkflowOutcome(
            terminal_decision=decision,
            notes=["editorial decision is terminal; external author must resubmit"],
        )

    def _run_intake(self, job: RuntimeJob, repository: RuntimeRepository) -> dict:
        submission = repository.get_object(job.target_object_id)
        if submission is None:
            raise ValueError(f"Unknown submission: {job.target_object_id}")
        sections = dict(submission.metadata.get("sections", {}))
        source_bundle = list(submission.metadata.get("source_bundle", []))
        failed = [
            gate.model_dump(mode="json")
            for gate in run_submission_template_checks(
                sections=sections,
                source_bundle=source_bundle,
                article_type=str(submission.metadata.get("article_type", ArticleType.RAPID_EVIDENCE_SYNTHESIS.value)),
                evidence_bundle=submission.metadata.get("evidence_bundle", {}),
            )
            if not gate.passed
        ]
        try:
            if not failed:
                artifact = compile_publication(
                    title=submission.title,
                    abstract=str(submission.metadata.get("abstract", "")).strip(),
                    sections=sections,
                    source_bundle=source_bundle,
                    article_type=str(submission.metadata.get("article_type", ArticleType.RAPID_EVIDENCE_SYNTHESIS.value)),
                    core_claims_resolved=bool(submission.metadata.get("core_claims_resolved", True)),
                )
                failed = [gate.model_dump(mode="json") for gate in artifact.gates if not gate.passed]
        except ValueError as exc:
            failed = [{"name": "intake_validation", "passed": False, "reason": str(exc)}]
        if failed:
            decision = repository.create_object(
                ResearchObject(
                    object_type=ObjectType.DECISION,
                    parent_object_id=submission.id,
                    title=f"Decision for {submission.title}",
                    body_markdown="Submission rejected at intake.",
                    metadata={
                        "decision": Decision.REJECT.value,
                        "notes": ["intake gate rejection"],
                        "article_type": submission.metadata.get("article_type", ArticleType.RAPID_EVIDENCE_SYNTHESIS.value),
                        "gate_failures": failed,
                        **self._static_provider_metadata(prompt_version=EDITOR_PROMPT_VERSION),
                    },
                )
            )
            derivation = emit_decision_to_derivation_web(submission=submission, decision=decision)
            return {"created_object_id": decision.id, "terminal_decision": Decision.REJECT.value, "next_jobs": 0, "derivation_web": derivation}
        integrity = check_integrity(_integrity_payload_from_submission(submission))
        recommendation = str(integrity.get("recommendation") or "").strip().lower() if integrity else ""
        if integrity:
            submission = repository.update_object_metadata(
                submission.id,
                {**submission.metadata, "integrity": _integrity_signal_metadata(integrity, recommendation or "pass")},
            ) or submission
        if recommendation in {Decision.REJECT.value, Decision.REVISE.value}:
            decision = repository.create_object(
                ResearchObject(
                    object_type=ObjectType.DECISION,
                    parent_object_id=submission.id,
                    title=f"Decision for {submission.title}",
                    body_markdown=f"Integrity decision: {recommendation}",
                    metadata=self._integrity_decision_metadata(submission, integrity or {}, recommendation),
                )
            )
            derivation = emit_decision_to_derivation_web(submission=submission, decision=decision)
            return {"created_object_id": decision.id, "terminal_decision": recommendation, "next_jobs": 0, "derivation_web": derivation}
        repository.enqueue_job(
            RuntimeJob(
                target_object_id=submission.id,
                stage=Stage.REVIEW,
                payload={"domain_slug": submission.metadata.get("domain_slug", "general")},
            )
        )
        return {"created_object_id": submission.id, "next_stage": Stage.REVIEW.value}

    def _run_review(self, job: RuntimeJob, repository: RuntimeRepository) -> dict:
        submission = repository.get_object(job.target_object_id)
        if submission is None:
            raise ValueError(f"Unknown submission: {job.target_object_id}")
        recommendation, review_markdown, provider_metadata = self._review_submission(submission)
        review = repository.create_object(
            ResearchObject(
                object_type=ObjectType.REVIEW,
                parent_object_id=submission.id,
                title=f"Review for {submission.title}",
                body_markdown=review_markdown,
                metadata={
                    "recommendation": recommendation,
                    "article_type": submission.metadata.get("article_type", ArticleType.RAPID_EVIDENCE_SYNTHESIS.value),
                    "core_claims_resolved": submission.metadata.get("core_claims_resolved", True),
                    **provider_metadata,
                },
            )
        )
        repository.enqueue_job(
            RuntimeJob(
                target_object_id=submission.id,
                stage=Stage.EDITORIAL,
                payload={"review_id": review.id, "domain_slug": submission.metadata.get("domain_slug", "general")},
            )
        )
        return {"created_object_id": review.id, "next_stage": Stage.EDITORIAL.value}

    def _run_editorial(self, job: RuntimeJob, repository: RuntimeRepository) -> dict:
        submission = repository.get_object(job.target_object_id)
        if submission is None:
            raise ValueError(f"Unknown submission: {job.target_object_id}")
        review_id = str(job.payload.get("review_id"))
        review = repository.get_object(review_id)
        if review is None:
            raise ValueError(f"Unknown review: {review_id}")
        if "recommendation" not in review.metadata:
            raise ValueError("invalid_review_recommendation:missing")
        recommendation = str(review.metadata["recommendation"]).strip().lower()
        if recommendation not in {"accept", "revise", "reject"}:
            raise ValueError(f"invalid_review_recommendation:{recommendation}")
        original_recommendation = recommendation
        rubric_scores = review.metadata.get("rubric_scores")
        if isinstance(rubric_scores, dict):
            recommendation = _calibrated_recommendation(
                recommendation,
                {str(key): int(value) for key, value in rubric_scores.items() if isinstance(value, int)},
                major_issues=[str(item) for item in review.metadata.get("major_issues", []) if str(item).strip()],
                required_revisions=[str(item) for item in review.metadata.get("required_revisions", []) if str(item).strip()],
                claim_support=str(review.metadata.get("claim_support_verdict", "")).strip().lower(),
                overclaim=str(review.metadata.get("overclaim_verdict", "")).strip().lower(),
                synthesis_quality=str(review.metadata.get("synthesis_quality_verdict", "")).strip().lower(),
            )
        decision = {
            "accept": Decision.ACCEPT,
            "revise": Decision.REVISE,
            "reject": Decision.REJECT,
        }[recommendation]
        context = WorkflowContext(
            target_object_id=submission.id,
            domain_slug=str(submission.metadata.get("domain_slug", "general")),
            review_ids=[review.id],
        )
        outcome = self.plan_from_editorial(context, decision)
        decision_object = repository.create_object(
            ResearchObject(
                object_type=ObjectType.DECISION,
                parent_object_id=submission.id,
                title=f"Decision for {submission.title}",
                body_markdown=f"Editorial decision: {decision.value}",
                metadata={
                    "decision": decision.value,
                    "article_type": submission.metadata.get("article_type", ArticleType.RAPID_EVIDENCE_SYNTHESIS.value),
                    "notes": outcome.notes,
                    "review_id": review.id,
                    **(
                        {
                            "original_recommendation": original_recommendation,
                            "recommendation_calibration": "minor_issues_only_accept_contract",
                        }
                        if original_recommendation != recommendation
                        else {}
                    ),
                    **self._static_provider_metadata(prompt_version=EDITOR_PROMPT_VERSION),
                },
            )
        )
        for next_job in outcome.next_jobs:
            repository.enqueue_job(next_job)
        derivation = emit_decision_to_derivation_web(submission=submission, review=review, decision=decision_object)
        return {
            "created_object_id": decision_object.id,
            "terminal_decision": decision.value if outcome.terminal_decision else None,
            "next_jobs": len(outcome.next_jobs),
            "derivation_web": derivation,
        }

    def _run_publish(self, job: RuntimeJob, repository: RuntimeRepository) -> dict:
        submission = repository.get_object(job.target_object_id)
        if submission is None:
            raise ValueError(f"Unknown submission: {job.target_object_id}")
        existing = repository.publication_for_target(submission.id)
        if existing is not None:
            return {"publication_id": existing.id, "deduped": True}
        normalized_title = " ".join(str(submission.title or "").lower().split())
        submission_markers = _publication_dedupe_markers(submission.metadata)
        for pub in repository.list_objects(ObjectType.PUBLICATION):
            if (
                submission_markers & _publication_dedupe_markers(pub.metadata)
                or " ".join(str(pub.title or "").lower().split()) == normalized_title
            ):
                return {"publication_id": pub.id, "deduped": True}
        artifact = compile_publication(
            title=submission.title,
            abstract=str(submission.metadata.get("abstract", "")).strip(),
            sections=dict(submission.metadata.get("sections", {})),
            source_bundle=list(submission.metadata.get("source_bundle", [])),
            body_markdown=_submission_full_body_markdown(submission),
            article_type=str(submission.metadata.get("article_type", ArticleType.RAPID_EVIDENCE_SYNTHESIS.value)),
            core_claims_resolved=bool(submission.metadata.get("core_claims_resolved", True)),
        )
        failed = [gate.name for gate in artifact.gates if not gate.passed]
        if failed:
            raise ValueError(f"publish_gates_failed:{','.join(failed)}")
        publication = ResearchObject(
            object_type=ObjectType.PUBLICATION,
            parent_object_id=submission.id,
            title=artifact.title,
            body_markdown=artifact.body_markdown,
            metadata={
                "abstract": artifact.abstract,
                "article_type": submission.metadata.get("article_type", ArticleType.RAPID_EVIDENCE_SYNTHESIS.value),
                "counts": artifact.counts.model_dump(mode="json"),
                "gates": [gate.model_dump(mode="json") for gate in artifact.gates],
                "author_agent_id": submission.metadata.get("author_agent_id"),
                "integrity": submission.metadata.get("integrity"),
                "source_submission_id": submission.id,
                **_publication_identity_metadata(submission.metadata),
                **osf_publication_metadata_from_env(),
                **self._static_provider_metadata(prompt_version=EDITOR_PROMPT_VERSION),
            },
        )
        decisions = [
            obj
            for obj in repository.children_of(submission.id, ObjectType.DECISION)
            if obj.metadata.get("decision") == Decision.ACCEPT.value
        ]
        decision = decisions[-1] if decisions else None
        review = repository.get_object(str(decision.metadata["review_id"])) if decision and decision.metadata.get("review_id") else None
        publication = repository.create_object(publication)
        try:
            osf_metadata = _mint_publication_doi(repository, publication)
            if osf_metadata:
                publication = repository.update_object_metadata(publication.id, {**publication.metadata, **osf_metadata}) or publication
        except Exception as exc:
            publication = repository.update_object_metadata(
                publication.id,
                {**publication.metadata, "osf_status": "failed", "doi_status": "failed", "osf_error": str(exc)[:240]},
            ) or publication

        try:
            dw_metadata = emit_publication_to_derivation_web(
                submission=submission,
                publication=publication,
                review=review,
                decision=decision,
            )
            if dw_metadata:
                publication = repository.update_object_metadata(publication.id, {**publication.metadata, **dw_metadata}) or publication
        except Exception as exc:
            repository.update_object_metadata(publication.id, {**publication.metadata, "dw_status": "failed", "dw_error": str(exc)[:240]})
        index_integrity(_integrity_payload_from_publication(publication, submission))
        return {"publication_id": publication.id, "deduped": False}
