"""AURA-0093 Core public evaluator/replay-boundary verification."""

from __future__ import annotations

import inspect

from aura_core.domains.knowledge.documents import (
    COMPONENT_VERSION,
    DOCUMENT_COMPONENT_VERSIONS,
    EVALUATOR_COMPONENTS,
    EVALUATOR_VERSIONS,
    evaluate_document_recall,
    evaluate_document_retrieval_ranking,
    evaluation,
)


def test_document_evaluators_are_available_only_through_the_public_boundary() -> None:
    assert EVALUATOR_VERSIONS
    assert set(EVALUATOR_VERSIONS) == set(EVALUATOR_COMPONENTS)
    assert all(version == "1.0.0" for version in EVALUATOR_VERSIONS.values())
    assert all(component.startswith("aura.") for component in EVALUATOR_COMPONENTS.values())
    assert COMPONENT_VERSION == DOCUMENT_COMPONENT_VERSIONS["aura.knowledge.document_retrieval"]


def test_replayable_evaluator_results_are_deterministic_and_metadata_only() -> None:
    first = evaluate_document_recall(
        relevant_document_count=2, retrieved_relevant_count=2
    )
    second = evaluate_document_recall(
        relevant_document_count=2, retrieved_relevant_count=2
    )
    assert first == second
    assert first.score == 1.0
    assert first.outcome == "pass"
    assert first.metadata == {
        "relevant_document_count": "2",
        "retrieved_relevant_count": "2",
        "recall": "1.000000",
    }
    assert "content" not in repr(first).casefold()
    assert "filename" not in repr(first).casefold()


def test_invalid_bounded_replay_measurements_fail_closed() -> None:
    assert evaluate_document_recall(
        relevant_document_count=1, retrieved_relevant_count=2
    ).outcome == "fail"
    assert evaluate_document_retrieval_ranking(
        relevant_rank=0, expected_max_rank=3
    ).outcome == "fail"
    assert evaluate_document_retrieval_ranking(
        relevant_rank=1, expected_max_rank=0
    ).outcome == "fail"


def test_core_evaluation_module_has_no_observatory_dependency_or_content_parameter() -> None:
    source = inspect.getsource(evaluation)
    assert "aura_observatory" not in source
    for name, function in vars(evaluation).items():
        if name.startswith("evaluate_") and callable(function):
            assert not {"content", "filename", "owner_identity", "endpoint"}.intersection(
                inspect.signature(function).parameters
            )
