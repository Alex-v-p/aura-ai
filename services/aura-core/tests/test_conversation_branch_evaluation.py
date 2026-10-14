"""Deterministic, metadata-only evaluations for AURA-0071.

The branch fixtures intentionally contain counts and bounded outcome labels
only.  Conversation text, owner subjects, citations, evidence, and lineage
payloads are never passed to an evaluator or retained in its result.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest
from aura_core.platform.telemetry import COMPONENT_VERSIONS

FIXTURE_REVISION = "conversation-branching-eval-fixtures-v1"
EVALUATOR_VERSIONS = {
    "conversation_branch_context_isolation": "1.0.0",
    "conversation_regeneration_evidence_freshness": "1.0.0",
}
EVALUATOR_COMPONENTS = {
    "conversation_branch_context_isolation": "aura.interaction.conversation_persistence",
    "conversation_regeneration_evidence_freshness": "aura.execution.run_coordinator",
}


@dataclass(frozen=True, slots=True)
class EvaluationResult:
    evaluator_id: str
    evaluator_version: str
    component_id: str
    component_version: str
    fixture_revision: str
    case_id: str
    replay_key: str
    score: float
    outcome: str
    metadata: tuple[tuple[str, str], ...]

    def payload(self) -> dict[str, Any]:
        return {
            "evaluatorId": self.evaluator_id,
            "evaluatorVersion": self.evaluator_version,
            "componentId": self.component_id,
            "componentVersion": self.component_version,
            "fixtureRevision": self.fixture_revision,
            "caseId": self.case_id,
            "replayKey": self.replay_key,
            "score": self.score,
            "outcome": self.outcome,
            "metadata": dict(self.metadata),
        }


def _result(evaluator_id: str, passed: bool, **metadata: str) -> EvaluationResult:
    forbidden = {"content", "message", "prompt", "response", "citation", "evidence", "owner"}
    if forbidden & set(metadata):
        raise ValueError("conversation evaluation metadata must remain content-free")
    component_id = EVALUATOR_COMPONENTS[evaluator_id]
    component_version = COMPONENT_VERSIONS[component_id]
    evaluator_version = EVALUATOR_VERSIONS[evaluator_id]
    canonical_metadata = json.dumps(
        sorted(metadata.items()), separators=(",", ":"), ensure_ascii=True
    )
    metadata_digest = hashlib.sha256(canonical_metadata.encode("ascii")).hexdigest()[:16]
    case_id = f"{evaluator_id}.{metadata_digest}"
    replay_key = str(
        uuid5(
            NAMESPACE_URL,
            f"{component_id}:{component_version}:{evaluator_id}:{FIXTURE_REVISION}:{case_id}",
        )
    )
    return EvaluationResult(
        evaluator_id=evaluator_id,
        evaluator_version=evaluator_version,
        component_id=component_id,
        component_version=component_version,
        fixture_revision=FIXTURE_REVISION,
        case_id=case_id,
        replay_key=replay_key,
        score=1.0 if passed else 0.0,
        outcome="pass" if passed else "fail",
        metadata=tuple(sorted(metadata.items())),
    )


def evaluate_conversation_branch_context_isolation(
    *,
    expected_ancestor_count: int,
    observed_ancestor_count: int,
    leaked_sibling_count: int,
    foreign_owner_results: int,
    boundary_preserved: bool = True,
) -> EvaluationResult:
    """Evaluate branch context using only bounded counts and booleans."""

    passed = (
        expected_ancestor_count >= 0
        and observed_ancestor_count == expected_ancestor_count
        and leaked_sibling_count == 0
        and foreign_owner_results == 0
        and boundary_preserved
    )
    return _result(
        "conversation_branch_context_isolation",
        passed,
        expected_ancestor_count=str(expected_ancestor_count),
        observed_ancestor_count=str(observed_ancestor_count),
        leaked_sibling_count=str(leaked_sibling_count),
        foreign_owner_results=str(foreign_owner_results),
        boundary_preserved=str(boundary_preserved).lower(),
    )


def evaluate_conversation_regeneration_evidence_freshness(
    *,
    source_evidence_count: int,
    regenerated_evidence_count: int,
    reused_source_evidence_count: int,
    invalid_citation_count: int,
    historical_memory_reprocessed_count: int,
    new_owner_evidence_count: int = 0,
) -> EvaluationResult:
    """Evaluate that regenerated evidence is independently validated.

    A regenerated run has no new owner-authored evidence, so its expected
    source evidence count is zero.  The evaluator accepts the new-owner count
    as metadata to make edited-branch fixtures explicit without retaining any
    text or identifier.
    """

    passed = (
        min(
            source_evidence_count,
            regenerated_evidence_count,
            reused_source_evidence_count,
            invalid_citation_count,
            historical_memory_reprocessed_count,
            new_owner_evidence_count,
        )
        >= 0
        and reused_source_evidence_count == 0
        and invalid_citation_count == 0
        and historical_memory_reprocessed_count == 0
        and source_evidence_count == 0
    )
    return _result(
        "conversation_regeneration_evidence_freshness",
        passed,
        source_evidence_count=str(source_evidence_count),
        regenerated_evidence_count=str(regenerated_evidence_count),
        reused_source_evidence_count=str(reused_source_evidence_count),
        invalid_citation_count=str(invalid_citation_count),
        historical_memory_reprocessed_count=str(historical_memory_reprocessed_count),
        new_owner_evidence_count=str(new_owner_evidence_count),
    )


def test_branch_evaluators_have_stable_attribution_and_replay_keys() -> None:
    first = evaluate_conversation_branch_context_isolation(
        expected_ancestor_count=4,
        observed_ancestor_count=4,
        leaked_sibling_count=0,
        foreign_owner_results=0,
    )
    second = evaluate_conversation_branch_context_isolation(
        expected_ancestor_count=4,
        observed_ancestor_count=4,
        leaked_sibling_count=0,
        foreign_owner_results=0,
    )
    assert first.replay_key == second.replay_key
    assert first.case_id == second.case_id
    assert first.evaluator_version == "1.0.0"
    assert first.component_id == "aura.interaction.conversation_persistence"
    assert first.component_version == COMPONENT_VERSIONS[first.component_id]

    freshness = evaluate_conversation_regeneration_evidence_freshness(
        source_evidence_count=0,
        regenerated_evidence_count=2,
        reused_source_evidence_count=0,
        invalid_citation_count=0,
        historical_memory_reprocessed_count=0,
    )
    assert freshness.component_id == "aura.execution.run_coordinator"
    assert freshness.component_version == COMPONENT_VERSIONS[freshness.component_id]


def test_branch_evaluators_cover_positive_and_negative_fixtures() -> None:
    isolated = evaluate_conversation_branch_context_isolation(
        expected_ancestor_count=3,
        observed_ancestor_count=3,
        leaked_sibling_count=0,
        foreign_owner_results=0,
    )
    leaked = evaluate_conversation_branch_context_isolation(
        expected_ancestor_count=3,
        observed_ancestor_count=4,
        leaked_sibling_count=1,
        foreign_owner_results=0,
    )
    fresh = evaluate_conversation_regeneration_evidence_freshness(
        source_evidence_count=0,
        regenerated_evidence_count=1,
        reused_source_evidence_count=0,
        invalid_citation_count=0,
        historical_memory_reprocessed_count=0,
    )
    stale = evaluate_conversation_regeneration_evidence_freshness(
        source_evidence_count=1,
        regenerated_evidence_count=1,
        reused_source_evidence_count=1,
        invalid_citation_count=1,
        historical_memory_reprocessed_count=1,
    )
    assert isolated.outcome == fresh.outcome == "pass"
    assert leaked.outcome == stale.outcome == "fail"
    assert isolated.replay_key != leaked.replay_key
    assert fresh.replay_key != stale.replay_key
    assert isolated.case_id != leaked.case_id
    assert fresh.case_id != stale.case_id


def test_branch_evaluator_payloads_are_metadata_only_and_redacted() -> None:
    results = (
        evaluate_conversation_branch_context_isolation(
            expected_ancestor_count=2,
            observed_ancestor_count=2,
            leaked_sibling_count=0,
            foreign_owner_results=0,
        ),
        evaluate_conversation_regeneration_evidence_freshness(
            source_evidence_count=0,
            regenerated_evidence_count=2,
            reused_source_evidence_count=0,
            invalid_citation_count=0,
            historical_memory_reprocessed_count=0,
        ),
    )
    for result in results:
        payload = result.payload()
        rendered = repr(payload).lower()
        assert not any(
            secret in rendered
            for secret in ("private question", "owner@example", "document evidence")
        )
        assert not {"content", "message", "prompt", "response", "citation", "evidence"} & set(
            payload["metadata"]
        )
        assert UUID(result.replay_key)


@pytest.mark.parametrize(
    "kwargs",
    [
        {
            "expected_ancestor_count": -1,
            "observed_ancestor_count": -1,
            "leaked_sibling_count": 0,
            "foreign_owner_results": 0,
        },
        {
            "expected_ancestor_count": 2,
            "observed_ancestor_count": 2,
            "leaked_sibling_count": 0,
            "foreign_owner_results": 1,
        },
    ],
)
def test_branch_context_evaluator_rejects_invalid_or_foreign_fixture(
    kwargs: dict[str, int],
) -> None:
    result = evaluate_conversation_branch_context_isolation(
        expected_ancestor_count=kwargs["expected_ancestor_count"],
        observed_ancestor_count=kwargs["observed_ancestor_count"],
        leaked_sibling_count=kwargs["leaked_sibling_count"],
        foreign_owner_results=kwargs["foreign_owner_results"],
        boundary_preserved=True,
    )
    assert result.outcome == "fail"


def test_regeneration_evaluator_rejects_stale_evidence_or_reprocessed_history() -> None:
    result = evaluate_conversation_regeneration_evidence_freshness(
        source_evidence_count=0,
        regenerated_evidence_count=1,
        reused_source_evidence_count=0,
        invalid_citation_count=0,
        historical_memory_reprocessed_count=1,
    )
    assert result.outcome == "fail"
