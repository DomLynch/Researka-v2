from copy import deepcopy
import hashlib

import pytest

from contracts import ArticleType, ObjectType, ResearchObject
from runtime_core.evidence_quality import (
    agreed_claim_resolutions,
    claim_candidates,
    quantitative_claim_candidates,
    support_for_claim,
    evidence_profile,
)
from runtime_core.workflow import (
    _claim_trace_guard_revisions,
    _canonical_submission_hash,
    _review_claim_text,
)
from runtime_core.review_contract import model_quorum_metadata, model_quorum_attestation
from tests.test_model_quorum import _receipt, SOL, TERRA, SECRET


def _resolution(claim, quote, status="supported"):
    return {
        "claim_id": "claim_" + hashlib.sha256(claim.encode()).hexdigest()[:16],
        "status": status,
        "rationale": "The stated finding is checked against the cited source and its comparator.",
        "axes": {
            axis: "aligned"
            for axis in (
                "intervention",
                "population",
                "comparator",
                "endpoint",
                "direction",
                "negation",
                "number_or_unit",
            )
        },
        "passages": [{"source_id": "source_1", "quote": quote}],
    }


def _votes(row):
    receipts = [_receipt(SOL), _receipt(TERRA)]
    for receipt in receipts:
        receipt["response"]["claim_resolutions"] = [deepcopy(row)]
    return receipts


@pytest.mark.parametrize(
    "text,expected",
    [
        (
            "Aspirin reduced mortality [bundle:1]. Metformin increased glucose [bundle:2].",
            2,
        ),
        (
            "Aspirin reduced mortality. [bundle:1] Metformin increased glucose. [bundle:2]",
            2,
        ),
        (
            "Dr. Smith reported a 0.5% mortality reduction [bundle:1]. 20 adults died [bundle:2].",
            2,
        ),
        (
            '"Aspirin reduced mortality." [bundle:1] Metformin increased glucose [bundle:2].',
            2,
        ),
        ("Aspirin reduced mortality [bundle:1]. Metformin reduced fasting glucose.", 2),
    ],
)
def test_sentence_claims_keep_citations_quantities_and_uncited_outcomes(text, expected):
    claims = claim_candidates(text)
    assert len(claims) == expected
    assert "[bundle:1]" in claims[0]
    assert "[bundle:2]" not in claims[0]
    assert "Metformin" not in claims[0]
    if text.startswith("Dr."):
        assert claims[0].startswith("Dr. Smith")
        assert "0.5%" in claims[0]


def test_claims_after_thirty_are_not_silently_ignored():
    text = "\n".join(
        ["Aspirin reduced mortality [bundle:1]."] * 30
        + ["Metformin reduced fasting glucose by 7%."]
    )
    assert len(claim_candidates(text)) == 31
    assert quantitative_claim_candidates(text) == [
        "Metformin reduced fasting glucose by 7%."
    ]
    assert (
        len(quantitative_claim_candidates("\n".join(["Mortality was 5% [1]."] * 31)))
        == 31
    )


@pytest.mark.parametrize(
    "claim,source",
    [
        (
            "Aspirin did (not) reduce mortality [bundle:1].",
            {"cited_as": "not", "quote": "Aspirin did reduce mortality."},
        ),
        (
            "Aspirin reduced (mortality) [bundle:1].",
            {"cited_as": "mortality", "quote": "Aspirin reduced blood pressure."},
        ),
        (
            ".5 mg aspirin reduced blood pressure [bundle:1].",
            {"quote": "5 mg aspirin reduced blood pressure."},
        ),
        (
            "-5 mmHg was the blood pressure difference [bundle:1].",
            {"quote": "5 mmHg was the blood pressure difference."},
        ),
    ],
)
def test_normalization_cannot_erase_scientific_words_decimal_or_sign(claim, source):
    assert not support_for_claim(claim, [source], require_quantitative_agreement=True)
    assert (
        evidence_profile(text=claim, source_bundle=[source])[
            "quantitative_claim_trace_count"
        ]
        == 0
    )


@pytest.mark.parametrize(
    "claim",
    [
        "Only HIRT and BFRT-P significantly increased muscle mass, with HIRT demonstrating the highest growth in both arms",
        "RET significantly ( P < 0.05) increased 1-RM, RTF, and T measures above baselines regardless of group assignment, but the increases were greater in the supplemented groups",
        "A significant time-by-group interaction was observed for inhibitory control when contrasting MRT with BT, t (80) = 3.56, P < 0.001, β = 0.42, 95% CI [0.19, 0.65], indicating improved response inhibition following MRT",
    ],
)
def test_real_displayed_quotes_match_without_citation_words_becoming_endpoints(claim):
    sources = [{"cited_as": "Example 2025", "excerpt": claim.lower() + "."}]
    assert support_for_claim(
        f'"{claim}" [Example 2025] [bundle:1].',
        sources,
        require_quantitative_agreement=True,
    )


@pytest.mark.parametrize(
    "quote",
    [
        "Aspirin did not reduce mortality by 5%.",
        "Aspirin increased mortality by 5%.",
        "Aspirin reduced fasting glucose by 5%.",
        "Aspirin reduced mortality by 50%.",
    ],
)
def test_two_semantic_votes_cannot_override_explicit_contradictions(quote):
    claim = "Aspirin reduced mortality by 5% [bundle:1]."
    row = _resolution(claim, quote)
    assert not agreed_claim_resolutions([claim], [{"quote": quote}], _votes(row))


def test_missing_forged_duplicate_one_sided_or_wrong_source_resolution_stays_open():
    claim = "Combined training improved handgrip strength versus controls [bundle:1]."
    quote = "Combined training produced greater improvement in handgrip strength than controls."
    source = {"excerpt": quote}
    row = _resolution(claim, quote)
    assert agreed_claim_resolutions([claim], [source], _votes(row))
    for mutation in (
        "missing",
        "one_sided",
        "duplicate",
        "wrong_quote",
        "wrong_source",
        "wrong_id",
        "unresolved",
        "axis",
    ):
        votes = _votes(row)
        changed = votes[1]["response"]["claim_resolutions"]
        if mutation == "missing":
            votes = []
        elif mutation == "one_sided":
            changed.clear()
        elif mutation == "duplicate":
            changed.append(deepcopy(changed[0]))
        elif mutation == "wrong_quote":
            changed[0]["passages"][0]["quote"] += " fabricated"
        elif mutation == "wrong_source":
            changed[0]["passages"][0]["source_id"] = "source_2"
        elif mutation == "wrong_id":
            changed[0]["claim_id"] = "claim_wrong"
        elif mutation == "unresolved":
            changed[0]["status"] = "unresolved"
        elif mutation == "axis":
            changed[0]["axes"]["comparator"] = "uncertain"
        assert not agreed_claim_resolutions([claim], [source], votes), mutation


def test_structural_classification_cannot_hide_an_empirical_claim():
    methods = "Methods: We mapped 19 retained sources by population, design and directness, grouped findings without pooling."
    assert agreed_claim_resolutions(
        [methods], [{}] * 19, _votes(_resolution(methods, "", "not_source_claim"))
    )
    assert not agreed_claim_resolutions(
        [methods], [{}] * 18, _votes(_resolution(methods, "", "not_source_claim"))
    )
    for claim in (
        "Methods: Aspirin reduced mortality.",
        "We mapped evidence that aspirin reduced mortality.",
        "We mapped a 5% mortality difference.",
    ):
        assert not agreed_claim_resolutions(
            [claim], [], _votes(_resolution(claim, "", "not_source_claim"))
        )


def _signed_review(submission, row):
    metadata = {
        **model_quorum_metadata(_votes(row)),
        "provider": "reviewer-panel",
        "recommendation": "accept",
        "reviewed_package_hash": _canonical_submission_hash(submission),
        "judge_release_id": "test-release",
    }
    metadata["model_quorum_attestation"] = model_quorum_attestation(
        metadata,
        submission_id=submission.id,
        reviewed_package_hash=metadata["reviewed_package_hash"],
        recommendation="accept",
        judge_release_id="test-release",
        secret=SECRET,
    )
    return ResearchObject(
        object_type=ObjectType.REVIEW,
        parent_object_id=submission.id,
        title="Review",
        metadata=metadata,
    )


def test_guard_uses_only_same_package_signed_claim_votes(monkeypatch):
    monkeypatch.setenv("RESEARKA_V2_REVIEW_ATTESTATION_SECRET", SECRET)
    claim = "Combined training improved handgrip strength versus controls [bundle:1]."
    quote = "Combined training produced greater improvement in handgrip strength than controls."
    submission = ResearchObject(
        object_type=ObjectType.SUBMISSION,
        title="Bounded training",
        metadata={
            "article_type": ArticleType.EVIDENCE_MAP.value,
            "abstract": claim,
            "source_bundle": [{"excerpt": quote}],
        },
    )
    assert _claim_trace_guard_revisions(submission)
    row = _resolution(claim, quote)
    review = _signed_review(submission, row)
    assert not _claim_trace_guard_revisions(submission, review)
    review.metadata["reviewer_receipts"][1]["response"]["claim_resolutions"][0][
        "rationale"
    ] += " tampered"
    with pytest.raises(ValueError, match="accept_quorum_missing"):
        _claim_trace_guard_revisions(submission, review)
    review = _signed_review(submission, row)
    submission.metadata["abstract"] += " Aspirin reduced mortality."
    with pytest.raises(ValueError, match="accept_quorum_missing"):
        _claim_trace_guard_revisions(submission, review)


def test_review_and_guard_share_evidence_map_section_coverage():
    submission = ResearchObject(
        object_type=ObjectType.SUBMISSION,
        title="Map",
        metadata={
            "article_type": ArticleType.EVIDENCE_MAP.value,
            "sections": {
                " Findings Map ": "Mortality was 5% [1].",
                "Methods": "Not an outcome.",
                "Tensions and Gaps": "Glucose increased [2].",
            },
        },
    )
    assert (
        _review_claim_text(submission)
        == "Mortality was 5% [1].\nGlucose increased [2]."
    )


def test_verifier_uses_same_citation_boundaries_and_keeps_negative_numbers():
    from runtime_core.verify import _claim_checks

    sources = [{"source_id": "source_1"}, {"source_id": "source_2"}]
    _claim_checks(
        "Aspirin reduced mortality. [bundle:1] Metformin increased glucose. [bundle:2]",
        sources,
        100,
    )
    assert "Metformin" not in str(sources[0].get("verification_claims"))
    assert "Aspirin" not in str(sources[1].get("verification_claims"))
    _, unmapped, _ = _claim_checks(
        "-5 mmHg was the blood pressure difference.", [], 100
    )
    assert unmapped[0].startswith("-5 mmHg")


def test_public_traces_distinguish_lexical_match_from_signed_review_resolution():
    from runtime_core.publication_sidecars import build_sidecar

    claim = "Combined training improved handgrip strength versus controls [bundle:1]."
    row = _resolution(claim, "Unused in sidecar rendering")
    receipt = {"review_id": "review-123", "resolutions": {row["claim_id"]: "supported"}}
    publication = ResearchObject(
        object_type=ObjectType.PUBLICATION,
        title="Training",
        body_markdown=claim,
        metadata={"claim_reconciliation": receipt},
    )
    result, _, _ = build_sidecar(publication, None, "citation_traces.json")
    assert result["traces"][0]["review_resolution"] == "supported"
    assert result["traces"][0]["citation_support"] == []
    assert result["claim_reconciliation"]["review_id"] == "review-123"


@pytest.mark.parametrize(
    "claim,quote",
    [
        (
            "Aspirin reduced mortality by 5% [bundle:1].",
            "Metformin reduced mortality by 5%.",
        ),
        (
            "Aspirin reduced systolic blood pressure by 5% [bundle:1].",
            "Aspirin reduced fasting blood glucose by 5%.",
        ),
        (
            "Aspirin reduced mortality by 5% [bundle:1].",
            "Aspirin increased mortality by 5% but reduced glucose by 10%.",
        ),
    ],
)
def test_semantic_votes_cannot_borrow_subject_endpoint_or_direction(claim, quote):
    assert not agreed_claim_resolutions(
        [claim], [{"quote": quote}], _votes(_resolution(claim, quote))
    )


def test_structural_prefix_cannot_hide_cures_or_prevention():
    claim = "We mapped evidence showing that aspirin cures cancer and prevents stroke."
    assert not agreed_claim_resolutions(
        [claim], [], _votes(_resolution(claim, "", "not_source_claim"))
    )


def test_semantic_votes_cannot_borrow_number_from_another_intervention():
    claim = "Aspirin reduced mortality by 5% [bundle:1]."
    quote = "Aspirin reduced mortality by 10%. Metformin reduced mortality by 5%."
    assert not agreed_claim_resolutions(
        [claim], [{"quote": quote}], _votes(_resolution(claim, quote))
    )


@pytest.mark.parametrize(
    "claim,quote",
    [
        (
            "Aspirin reduced mortality by 5% [bundle:1].",
            "Aspirin increased mortality by 5% and reduced glucose by 10%.",
        ),
        (
            "Aspirin and Metformin reduced mortality by 5% [bundle:1].",
            "Aspirin reduced mortality by 5%.",
        ),
    ],
)
def test_semantic_votes_cannot_pool_unsplit_effects_or_drop_subjects(claim, quote):
    assert not agreed_claim_resolutions(
        [claim], [{"quote": quote}], _votes(_resolution(claim, quote))
    )


def test_claim_and_vote_order_are_stable():
    import random

    claims = [
        "Combined training improved handgrip strength versus controls [bundle:1].",
        "Mortality was 5% [bundle:1].",
    ]
    quote = "Combined training produced greater improvement in handgrip strength than controls."
    votes = _votes(_resolution(claims[0], quote))
    expected = agreed_claim_resolutions(claims, [{"excerpt": quote}], votes)
    assert expected
    for seed in range(20):
        shuffled_claims, shuffled_votes = list(claims), list(votes)
        random.Random(seed).shuffle(shuffled_claims)
        random.Random(seed).shuffle(shuffled_votes)
        assert (
            agreed_claim_resolutions(
                shuffled_claims, [{"excerpt": quote}], shuffled_votes
            )
            == expected
        )


def test_structural_label_does_not_exempt_a_safety_statement():
    claim = "We mapped evidence: Aspirin is safe for long-term treatment in adults with chronic kidney disease."
    assert claim in claim_candidates(claim)
    assert not agreed_claim_resolutions(
        [claim], [], _votes(_resolution(claim, "", "not_source_claim"))
    )


@pytest.mark.parametrize(
    "claim,quote",
    [
        (
            "Aspirin reduced mortality by 5% [bundle:1].",
            "Mortality was reduced by 5% with Metformin.",
        ),
        ("Mortality was 5% [bundle:1].", "Diabetes incidence was 5%."),
    ],
)
def test_semantic_votes_preserve_passive_subject_and_numeric_endpoint(claim, quote):
    assert not agreed_claim_resolutions(
        [claim], [{"quote": quote}], _votes(_resolution(claim, quote))
    )


def test_research_question_cannot_hide_an_appended_empirical_answer():
    claim = "This evidence map asked whether aspirin was safe and found that it caused no adverse events in adults."
    assert claim in claim_candidates(claim)
    assert not agreed_claim_resolutions(
        [claim], [], _votes(_resolution(claim, "", "not_source_claim"))
    )


def test_arbitrary_citation_metadata_cannot_erase_a_wrong_quantity():
    claim = "Aspirin reduced mortality by 50% [bundle:1]."
    sources = [{"cited_as": "50%", "excerpt": "Aspirin reduced mortality by 5%."}]
    assert not support_for_claim(claim, sources, require_quantitative_agreement=True)
