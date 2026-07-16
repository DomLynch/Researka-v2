from contracts import ObjectType, ResearchObject
from runtime_core import InMemoryRuntimeRepository
from scripts.reconcile_publications import reconcile_publications


def _publication_pair(repo: InMemoryRuntimeRepository, *, source_title: str) -> ResearchObject:
    bundle = [
        {
            "title": source_title if index == 0 else f"Primary study {index}",
            "doi": f"10.1234/source.{index}",
            "year": 2024,
            "evidence_type": "primary",
        }
        for index in range(12)
    ]
    submission = repo.create_object(
        ResearchObject(
            object_type=ObjectType.SUBMISSION,
            title="Research Synthesis: bounded intervention",
            metadata={
                "article_type": "research_synthesis",
                "sections": {"Abstract": "A bounded research question with sufficient detail for reconciliation." * 3},
                "source_bundle": bundle,
            },
        )
    )
    return repo.create_object(
        ResearchObject(
            object_type=ObjectType.PUBLICATION,
            title=submission.title,
            body_markdown="The evidence suggests a bounded effect, but important uncertainty remains. " * 8,
            metadata={"source_submission_id": submission.id, "publication_class": "research_synthesis"},
        )
    )


def test_reconcile_publications_dry_run_is_non_mutating() -> None:
    repo = InMemoryRuntimeRepository()
    publication = _publication_pair(repo, source_title="Correction: original intervention study")

    summary = reconcile_publications(repo, publication_ids={publication.id})
    stored = repo.get_object(publication.id)

    assert summary["items"][0]["status"] == "would_hide"
    assert stored is not None
    assert stored.metadata.get("public_visibility") is None


def test_reconcile_publications_hides_unsafe_and_records_prior_state() -> None:
    repo = InMemoryRuntimeRepository()
    publication = _publication_pair(repo, source_title="Correction: original intervention study")

    summary = reconcile_publications(repo, publication_ids={publication.id}, apply=True, audited_at="2026-07-16T00:00:00Z")
    updated = repo.get_object(publication.id)

    assert summary["items"][0]["status"] == "hidden"
    assert updated is not None
    assert updated.metadata["public_visibility"] == "hidden"
    assert updated.metadata["quality_audit"]["previous_public_visibility"] == "listed"
    assert updated.metadata["quality_audit"]["reason"] == "non_load_bearing_primary_source"

    updated.metadata["evidence_profile"] = {}
    repo.update_object_metadata(updated.id, updated.metadata)
    reconcile_publications(repo, publication_ids={publication.id}, apply=True, audited_at="2026-07-17T00:00:00Z")
    repeated = repo.get_object(publication.id)
    assert repeated is not None
    assert repeated.metadata["quality_audit"]["previous_public_visibility"] == "listed"


def test_reconcile_publications_persists_honest_classification() -> None:
    repo = InMemoryRuntimeRepository()
    publication = _publication_pair(repo, source_title="Primary intervention study")

    summary = reconcile_publications(repo, publication_ids={publication.id}, apply=True)
    updated = repo.get_object(publication.id)

    assert summary["items"][0]["status"] == "reclassified"
    assert updated is not None
    assert updated.metadata["publication_class"] == "adjacent_evidence_brief"
    assert updated.metadata.get("public_visibility") is None
