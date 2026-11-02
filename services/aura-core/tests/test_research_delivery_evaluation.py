"""Typed, deterministic evaluators for conversational research delivery."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml
from aura_core.domains.interaction.research_delivery.evaluation import (
    DeliveryObservationV1,
    ScoreV1EvaluatorRegistry,
)

EVALUATOR_IDS = (
    "research_completion_exactly_once@1",
    "research_tool_lifecycle@1",
    "research_delivery_privacy@1",
    "research_delivery_failure_isolation@1",
)


@dataclass(frozen=True, slots=True)
class CompletionExactlyOnceScoreV1:
    transcript_appends: int
    notifications: int
    duplicate_terminal_events: int
    score: float


@dataclass(frozen=True, slots=True)
class ToolLifecycleScoreV1:
    admission_state: str
    task_count: int
    settled_within_ms: int
    second_synthesis_calls: int
    score: float


@dataclass(frozen=True, slots=True)
class DeliveryPrivacyScoreV1:
    content_free_events: bool
    owner_reauthorized: bool
    validated_citations_only: bool
    score: float


@dataclass(frozen=True, slots=True)
class FailureIsolationScoreV1:
    transcript_settled: bool
    notification_retryable: bool
    transcript_duplicates: int
    score: float


@dataclass(frozen=True, slots=True)
class DeliveryEvaluationResult:
    evaluator_id: str
    score_v1: object
    outcome: str
    replay_key: str


def evaluate_completion_exactly_once(observation: dict[str, object]) -> DeliveryEvaluationResult:
    score = CompletionExactlyOnceScoreV1(
        int(observation["transcript_appends"]),
        int(observation["notifications"]),
        int(observation["duplicate_terminal_events"]),
        1.0
        if observation["transcript_appends"] == 1
        and observation["notifications"] == 1
        and observation["duplicate_terminal_events"] >= 1
        else 0.0,
    )
    return DeliveryEvaluationResult(
        EVALUATOR_IDS[0], score, "pass" if score.score else "fail", "delivery:e2e:v1"
    )


def evaluate_tool_lifecycle(observation: dict[str, object]) -> DeliveryEvaluationResult:
    score = ToolLifecycleScoreV1(
        str(observation["admission_state"]),
        int(observation["task_count"]),
        int(observation["settled_within_ms"]),
        int(observation["second_synthesis_calls"]),
        1.0
        if observation["admission_state"] == "admitted"
        and observation["task_count"] == 1
        and int(observation["settled_within_ms"]) <= 30_000
        and observation["second_synthesis_calls"] == 0
        else 0.0,
    )
    return DeliveryEvaluationResult(
        EVALUATOR_IDS[1], score, "pass" if score.score else "fail", "delivery:tool:v1"
    )


def evaluate_delivery_privacy(observation: dict[str, object]) -> DeliveryEvaluationResult:
    score = DeliveryPrivacyScoreV1(
        bool(observation["content_free_events"]),
        bool(observation["owner_reauthorized"]),
        bool(observation["validated_citations_only"]),
        1.0
        if all(
            bool(observation[key])
            for key in (
                "content_free_events",
                "owner_reauthorized",
                "validated_citations_only",
            )
        )
        else 0.0,
    )
    return DeliveryEvaluationResult(
        EVALUATOR_IDS[2], score, "pass" if score.score else "fail", "delivery:privacy:v1"
    )


def evaluate_failure_isolation(observation: dict[str, object]) -> DeliveryEvaluationResult:
    score = FailureIsolationScoreV1(
        bool(observation["transcript_settled"]),
        bool(observation["notification_retryable"]),
        int(observation["transcript_duplicates"]),
        1.0
        if observation["transcript_settled"]
        and observation["notification_retryable"]
        and observation["transcript_duplicates"] == 0
        else 0.0,
    )
    return DeliveryEvaluationResult(
        EVALUATOR_IDS[3], score, "pass" if score.score else "fail", "delivery:failure:v1"
    )


EVALUATORS: dict[str, Callable[[dict[str, object]], DeliveryEvaluationResult]] = {
    EVALUATOR_IDS[0]: evaluate_completion_exactly_once,
    EVALUATOR_IDS[1]: evaluate_tool_lifecycle,
    EVALUATOR_IDS[2]: evaluate_delivery_privacy,
    EVALUATOR_IDS[3]: evaluate_failure_isolation,
}


@pytest.mark.parametrize("evaluator_id", EVALUATOR_IDS)
def test_required_delivery_evaluator_has_typed_score_and_replay_identity(evaluator_id: str) -> None:
    assert evaluator_id in EVALUATORS
    result = EVALUATORS[evaluator_id](
        {
            "transcript_appends": 1,
            "notifications": 1,
            "duplicate_terminal_events": 2,
            "admission_state": "admitted",
            "task_count": 1,
            "settled_within_ms": 100,
            "second_synthesis_calls": 0,
            "content_free_events": True,
            "owner_reauthorized": True,
            "validated_citations_only": True,
            "transcript_settled": True,
            "notification_retryable": True,
            "transcript_duplicates": 0,
        }
    )
    assert result.outcome == "pass"
    assert result.replay_key.startswith("delivery:")
    assert result.score_v1.score == 1.0


@pytest.mark.parametrize("evaluator_id", EVALUATOR_IDS)
def test_required_delivery_evaluator_fails_adversarial_observation(evaluator_id: str) -> None:
    observation = {
        "transcript_appends": 2,
        "notifications": 2,
        "duplicate_terminal_events": 1,
        "admission_state": "admitted",
        "task_count": 2,
        "settled_within_ms": 30_001,
        "second_synthesis_calls": 1,
        "content_free_events": False,
        "owner_reauthorized": False,
        "validated_citations_only": False,
        "transcript_settled": False,
        "notification_retryable": False,
        "transcript_duplicates": 1,
    }
    result = EVALUATORS[evaluator_id](observation)
    assert result.outcome == "fail"
    assert result.score_v1.score == 0.0


def _production_observation() -> DeliveryObservationV1:
    return DeliveryObservationV1(
        transcript_appends=1,
        notifications=1,
        duplicate_terminal_events=2,
        admission_state="admitted",
        task_count=1,
        settled_within_ms=100,
        second_synthesis_calls=0,
        content_free_events=True,
        owner_reauthorized=True,
        validated_citations_only=True,
        transcript_settled=True,
        notification_retryable=True,
        transcript_duplicates=0,
        replay_key="delivery:replay:0146",
    )


def test_production_evaluator_registry_has_closed_inputs_and_deterministic_replay() -> None:
    registry = ScoreV1EvaluatorRegistry()
    assert registry.evaluator_ids == EVALUATOR_IDS
    observation = _production_observation()
    first = registry.evaluate(EVALUATOR_IDS[0], observation)
    second = registry.evaluate(EVALUATOR_IDS[0], observation)
    assert first == second
    assert first.score == 1.0
    assert first.outcome == "pass"
    assert first.replay_key == observation.replay_key
    with pytest.raises(ValueError, match="unknown"):
        registry.evaluate("research_delivery_unknown@1", observation)


@pytest.mark.parametrize(
    "sensitive_key", ("answer", "url", "owner_identity", "payload", "exception")
)
def test_production_evaluator_rejects_privacy_adversarial_payloads(sensitive_key: str) -> None:
    payload = {
        field: getattr(_production_observation(), field)
        for field in DeliveryObservationV1.__dataclass_fields__
    }
    payload[sensitive_key] = "untrusted content"
    with pytest.raises(ValueError):
        ScoreV1EvaluatorRegistry.observation(payload)


def test_production_evaluator_rejects_nested_content_and_negative_counts() -> None:
    payload = {
        field: getattr(_production_observation(), field)
        for field in DeliveryObservationV1.__dataclass_fields__
    }
    payload["replay_key"] = {"answer": "must not enter telemetry"}
    with pytest.raises((TypeError, ValueError)):
        ScoreV1EvaluatorRegistry.observation(payload)
    payload = {
        field: getattr(_production_observation(), field)
        for field in DeliveryObservationV1.__dataclass_fields__
    }
    payload["transcript_appends"] = -1
    with pytest.raises(ValueError):
        ScoreV1EvaluatorRegistry.observation(payload)


@pytest.mark.parametrize(
    ("evaluator_id", "field", "value"),
    (
        (EVALUATOR_IDS[0], "transcript_appends", 2),
        (EVALUATOR_IDS[1], "task_count", 2),
        (EVALUATOR_IDS[2], "content_free_events", False),
        (EVALUATOR_IDS[3], "transcript_settled", False),
    ),
)
def test_production_evaluators_score_isolated_failure_fields(
    evaluator_id: str, field: str, value: object
) -> None:
    registry = ScoreV1EvaluatorRegistry()
    baseline = _production_observation()
    payload = {
        name: getattr(baseline, name)
        for name in DeliveryObservationV1.__dataclass_fields__
    }
    payload[field] = value
    changed = registry.observation(payload)
    for candidate in EVALUATOR_IDS:
        score = registry.evaluate(candidate, changed)
        assert score.score == (0.0 if candidate == evaluator_id else 1.0), candidate


@pytest.mark.parametrize("state", ("", "admitted-with-content", "pending"))
def test_production_evaluator_rejects_open_admission_states(state: str) -> None:
    baseline = _production_observation()
    payload = {
        name: getattr(baseline, name)
        for name in DeliveryObservationV1.__dataclass_fields__
    }
    payload["admission_state"] = state
    with pytest.raises(ValueError, match="admission_state"):
        ScoreV1EvaluatorRegistry.observation(payload)


@pytest.mark.parametrize(
    "replay_key",
    ("delivery:answer", "delivery:https://example.com", "delivery:owner@example.com"),
)
def test_production_evaluator_rejects_sensitive_or_caller_derived_replay_keys(
    replay_key: str,
) -> None:
    baseline = _production_observation()
    payload = {
        name: getattr(baseline, name)
        for name in DeliveryObservationV1.__dataclass_fields__
    }
    payload["replay_key"] = replay_key
    with pytest.raises(ValueError, match="server-derived"):
        ScoreV1EvaluatorRegistry.observation(payload)


def test_research_delivery_manifest_attributes_all_evaluators_to_canonical_component() -> None:
    manifest = yaml.safe_load(
        (
            Path(__file__).parents[1]
            / "resources"
            / "component-manifests"
            / "research-completion-delivery.yaml"
        ).read_text(encoding="utf-8")
    )
    assert manifest["component"]["id"] == "aura.interaction.research_completion_delivery@1"
    assert tuple(manifest["evaluators"]) == EVALUATOR_IDS
