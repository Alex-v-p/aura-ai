"""Deterministic metadata-only evaluators for generated artifacts.

The evaluator boundary deliberately accepts only bounded booleans, counts, and
lifecycle labels.  It never receives artifact bytes, titles, owner identities,
object keys, or exception text, so replay and export remain privacy-safe.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from typing import Literal

FIXTURE_REVISION = "generated-artifacts-eval-fixtures-v1"
EVALUATORS = (
    "generated_artifact_integrity@1",
    "generated_artifact_authorization@1",
    "generated_artifact_deletion_truth@1",
    "generated_artifact_privacy@1",
)
EVALUATOR_COMPONENTS = {
    "generated_artifact_integrity@1": "aura.artifacts.persistence@1",
    "generated_artifact_authorization@1": "aura.artifacts.persistence@1",
    "generated_artifact_deletion_truth@1": "aura.artifacts.deletion@1",
    "generated_artifact_privacy@1": "aura.artifacts.deletion@1",
}


@dataclass(frozen=True, slots=True)
class GeneratedArtifactEvaluationResult:
    evaluator_id: str
    evaluator_version: str
    component_id: str
    component_version: str
    fixture_revision: str
    replay_key: str
    score: float
    outcome: Literal["pass", "fail"]
    metadata: tuple[tuple[str, str], ...]

    def payload(self) -> dict[str, object]:
        """Return an allowlisted, content-free export shape."""

        return {
            "evaluatorId": self.evaluator_id,
            "evaluatorVersion": self.evaluator_version,
            "componentId": self.component_id,
            "componentVersion": self.component_version,
            "fixtureRevision": self.fixture_revision,
            "replayKey": self.replay_key,
            "score": self.score,
            "outcome": self.outcome,
            "metadata": dict(self.metadata),
        }


def _result(evaluator_id: str, passed: bool, **metadata: str) -> GeneratedArtifactEvaluationResult:
    if evaluator_id not in EVALUATORS:
        raise ValueError("generated-artifact evaluator is not registered")
    component_id = EVALUATOR_COMPONENTS[evaluator_id]
    replay_key = sha256(
        json.dumps(
            {"component": component_id, "evaluator": evaluator_id, "fixture": FIXTURE_REVISION},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return GeneratedArtifactEvaluationResult(
        evaluator_id=evaluator_id,
        evaluator_version="1.0.0",
        component_id=component_id,
        component_version="1.0.0",
        fixture_revision=FIXTURE_REVISION,
        replay_key=replay_key,
        score=1.0 if passed else 0.0,
        outcome="pass" if passed else "fail",
        metadata=tuple(sorted(metadata.items())),
    )


def evaluate_generated_artifact_integrity(
    *, digest_matches: bool, byte_count_matches: bool, immutable: bool
) -> GeneratedArtifactEvaluationResult:
    values = (digest_matches, byte_count_matches, immutable)
    valid = all(type(value) is bool for value in values)
    return _result(
        "generated_artifact_integrity@1",
        valid and all(values),
        check_1=str(digest_matches).lower(),
        check_2=str(byte_count_matches).lower(),
        check_3=str(immutable).lower(),
    )


def evaluate_generated_artifact_authorization(
    *, owner_scope_valid: bool, foreign_scope_reads: int, unauthorized_mutations: int
) -> GeneratedArtifactEvaluationResult:
    values = (foreign_scope_reads, unauthorized_mutations)
    valid = all(type(value) is int and value >= 0 for value in values)
    return _result(
        "generated_artifact_authorization@1",
        type(owner_scope_valid) is bool
        and valid
        and owner_scope_valid
        and foreign_scope_reads == 0
        and unauthorized_mutations == 0,
        check_1=str(owner_scope_valid).lower(),
        check_2=str(foreign_scope_reads),
        check_3=str(unauthorized_mutations),
    )


def evaluate_generated_artifact_deletion_truth(
    *, lifecycle: str, object_absent: bool, preview_blocked: bool
) -> GeneratedArtifactEvaluationResult:
    valid_lifecycles = {"deletion_pending", "deletion_failed", "cleanup_unknown", "deleted"}
    return _result(
        "generated_artifact_deletion_truth@1",
        lifecycle in valid_lifecycles
        and type(object_absent) is bool
        and type(preview_blocked) is bool
        and (
            lifecycle == "deleted" and object_absent
            or lifecycle != "deleted" and preview_blocked
        ),
        check_1=lifecycle if lifecycle in valid_lifecycles else "invalid",
        check_2=str(object_absent).lower(),
        check_3=str(preview_blocked).lower(),
    )


def evaluate_generated_artifact_privacy(
    *, prohibited_fields_seen: int, content_captures: int, identifier_labels: int
) -> GeneratedArtifactEvaluationResult:
    values = (prohibited_fields_seen, content_captures, identifier_labels)
    valid = all(type(value) is int and value >= 0 for value in values)
    return _result(
        "generated_artifact_privacy@1",
        valid and all(value == 0 for value in values),
        check_1=str(prohibited_fields_seen),
        check_2=str(content_captures),
        check_3=str(identifier_labels),
    )


def evaluate(evaluator_id: str, **kwargs: bool | int | str) -> GeneratedArtifactEvaluationResult:
    """Dispatch one registered evaluator without accepting unbounded payloads."""

    functions = {
        "generated_artifact_integrity@1": evaluate_generated_artifact_integrity,
        "generated_artifact_authorization@1": evaluate_generated_artifact_authorization,
        "generated_artifact_deletion_truth@1": evaluate_generated_artifact_deletion_truth,
        "generated_artifact_privacy@1": evaluate_generated_artifact_privacy,
    }
    function = functions.get(evaluator_id)
    if function is None:
        raise ValueError("generated-artifact evaluator is not registered")
    return function(**kwargs)  # type: ignore[arg-type]


__all__ = [
    "EVALUATORS",
    "EVALUATOR_COMPONENTS",
    "FIXTURE_REVISION",
    "GeneratedArtifactEvaluationResult",
    "evaluate",
    "evaluate_generated_artifact_authorization",
    "evaluate_generated_artifact_deletion_truth",
    "evaluate_generated_artifact_integrity",
    "evaluate_generated_artifact_privacy",
]
