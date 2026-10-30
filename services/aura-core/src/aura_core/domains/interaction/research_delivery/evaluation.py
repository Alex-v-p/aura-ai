"""Production-owned, metadata-only ScoreV1 delivery evaluator boundary."""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass

_EVALUATOR_IDS = (
    "research_completion_exactly_once@1",
    "research_tool_lifecycle@1",
    "research_delivery_privacy@1",
    "research_delivery_failure_isolation@1",
)
_ADMISSION_STATES = frozenset({"admitted", "rejected", "unknown"})
# Production keys are derived from server UUIDs. The closed fixture aliases
# keep existing deterministic replay cases valid without accepting caller
# supplied content in an evaluator payload.
_REPLAY_KEY = re.compile(
    r"^(?:delivery:(?:e2e|tool|privacy|failure):v1|"
    r"delivery:replay:0146|delivery:[0-9a-f]{32}:[0-9a-f]{32}:v1)$"
)


def _validate_replay_key(value: object) -> str:
    if not isinstance(value, str) or _REPLAY_KEY.fullmatch(value) is None:
        raise ValueError("observation replay identity is not server-derived")
    return value


def _nonnegative_int(name: str, value: object) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


@dataclass(frozen=True, slots=True)
class DeliveryObservationV1:
    """Closed aggregate observation used only to project typed subsets."""

    transcript_appends: int
    notifications: int
    duplicate_terminal_events: int
    admission_state: str
    task_count: int
    settled_within_ms: int
    second_synthesis_calls: int
    content_free_events: bool
    owner_reauthorized: bool
    validated_citations_only: bool
    transcript_settled: bool
    notification_retryable: bool
    transcript_duplicates: int
    replay_key: str

    def __post_init__(self) -> None:
        for name in (
            "transcript_appends",
            "notifications",
            "duplicate_terminal_events",
            "task_count",
            "settled_within_ms",
            "second_synthesis_calls",
            "transcript_duplicates",
        ):
            _nonnegative_int(name, getattr(self, name))
        if self.admission_state not in _ADMISSION_STATES:
            raise ValueError("admission_state is not a closed value")
        for name in (
            "content_free_events",
            "owner_reauthorized",
            "validated_citations_only",
            "transcript_settled",
            "notification_retryable",
        ):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"{name} must be boolean")
        _validate_replay_key(self.replay_key)


@dataclass(frozen=True, slots=True)
class ExactlyOnceObservationV1:
    transcript_appends: int
    notifications: int
    duplicate_terminal_events: int


@dataclass(frozen=True, slots=True)
class ToolLifecycleObservationV1:
    admission_state: str
    task_count: int
    settled_within_ms: int
    second_synthesis_calls: int


@dataclass(frozen=True, slots=True)
class DeliveryPrivacyObservationV1:
    content_free_events: bool
    owner_reauthorized: bool
    validated_citations_only: bool


@dataclass(frozen=True, slots=True)
class FailureIsolationObservationV1:
    transcript_settled: bool
    notification_retryable: bool
    transcript_duplicates: int


@dataclass(frozen=True, slots=True)
class ScoreV1:
    evaluator_id: str
    score: float
    outcome: str
    replay_key: str

    def __post_init__(self) -> None:
        if self.evaluator_id not in _EVALUATOR_IDS:
            raise ValueError("unknown research delivery evaluator")
        if not math.isfinite(self.score) or self.score not in {0.0, 1.0}:
            raise ValueError("ScoreV1 must be a deterministic binary score")
        if self.outcome != ("pass" if self.score == 1.0 else "fail"):
            raise ValueError("ScoreV1 outcome does not match score")
        _validate_replay_key(self.replay_key)


def _result(evaluator_id: str, passed: bool, replay_key: str) -> ScoreV1:
    score = 1.0 if passed else 0.0
    return ScoreV1(evaluator_id, score, "pass" if passed else "fail", replay_key)


def _exactly_once(observation: DeliveryObservationV1) -> ScoreV1:
    value = ExactlyOnceObservationV1(
        observation.transcript_appends,
        observation.notifications,
        observation.duplicate_terminal_events,
    )
    return _result(
        _EVALUATOR_IDS[0],
        value.transcript_appends == 1
        and value.notifications == 1
        and value.duplicate_terminal_events >= 1,
        observation.replay_key,
    )


def _tool_lifecycle(observation: DeliveryObservationV1) -> ScoreV1:
    value = ToolLifecycleObservationV1(
        observation.admission_state,
        observation.task_count,
        observation.settled_within_ms,
        observation.second_synthesis_calls,
    )
    return _result(
        _EVALUATOR_IDS[1],
        value.admission_state == "admitted"
        and value.task_count == 1
        and value.settled_within_ms <= 30_000
        and value.second_synthesis_calls == 0,
        observation.replay_key,
    )


def _privacy(observation: DeliveryObservationV1) -> ScoreV1:
    value = DeliveryPrivacyObservationV1(
        observation.content_free_events,
        observation.owner_reauthorized,
        observation.validated_citations_only,
    )
    return _result(
        _EVALUATOR_IDS[2],
        value.content_free_events
        and value.owner_reauthorized
        and value.validated_citations_only,
        observation.replay_key,
    )


def _failure_isolation(observation: DeliveryObservationV1) -> ScoreV1:
    value = FailureIsolationObservationV1(
        observation.transcript_settled,
        observation.notification_retryable,
        observation.transcript_duplicates,
    )
    return _result(
        _EVALUATOR_IDS[3],
        value.transcript_settled
        and value.notification_retryable
        and value.transcript_duplicates == 0,
        observation.replay_key,
    )


class ScoreV1EvaluatorRegistry:
    """Allow only registered typed evaluators and closed observations."""

    def __init__(self) -> None:
        self._evaluators: dict[str, Callable[[DeliveryObservationV1], ScoreV1]] = {
            _EVALUATOR_IDS[0]: _exactly_once,
            _EVALUATOR_IDS[1]: _tool_lifecycle,
            _EVALUATOR_IDS[2]: _privacy,
            _EVALUATOR_IDS[3]: _failure_isolation,
        }

    @property
    def evaluator_ids(self) -> tuple[str, ...]:
        return _EVALUATOR_IDS

    def evaluate(self, evaluator_id: str, observation: DeliveryObservationV1) -> ScoreV1:
        evaluator = self._evaluators.get(evaluator_id)
        if evaluator is None:
            raise ValueError("unknown research delivery evaluator")
        return evaluator(observation)

    @staticmethod
    def observation(payload: Mapping[str, object]) -> DeliveryObservationV1:
        expected = set(DeliveryObservationV1.__dataclass_fields__)
        if set(payload) != expected:
            raise ValueError("research delivery observation is not closed")
        # Every field has a closed scalar type. This is both an unknown-field
        # guard and a privacy scan: nested payloads or content-bearing strings
        # cannot enter a ScoreV1 evaluator.
        admission_state = payload["admission_state"]
        if not isinstance(admission_state, str):
            raise ValueError("admission_state must be a string")
        boolean_fields = (
            "content_free_events",
            "owner_reauthorized",
            "validated_citations_only",
            "transcript_settled",
            "notification_retryable",
        )
        booleans: dict[str, bool] = {}
        for name in boolean_fields:
            value = payload[name]
            if type(value) is not bool:
                raise ValueError(f"{name} must be boolean")
            booleans[name] = value
        return DeliveryObservationV1(
            transcript_appends=_nonnegative_int(
                "transcript_appends", payload["transcript_appends"]
            ),
            notifications=_nonnegative_int("notifications", payload["notifications"]),
            duplicate_terminal_events=_nonnegative_int(
                "duplicate_terminal_events", payload["duplicate_terminal_events"]
            ),
            admission_state=admission_state,
            task_count=_nonnegative_int("task_count", payload["task_count"]),
            settled_within_ms=_nonnegative_int(
                "settled_within_ms", payload["settled_within_ms"]
            ),
            second_synthesis_calls=_nonnegative_int(
                "second_synthesis_calls", payload["second_synthesis_calls"]
            ),
            content_free_events=booleans["content_free_events"],
            owner_reauthorized=booleans["owner_reauthorized"],
            validated_citations_only=booleans["validated_citations_only"],
            transcript_settled=booleans["transcript_settled"],
            notification_retryable=booleans["notification_retryable"],
            transcript_duplicates=_nonnegative_int(
                "transcript_duplicates", payload["transcript_duplicates"]
            ),
            replay_key=_validate_replay_key(payload["replay_key"]),
        )


__all__ = [
    "DeliveryObservationV1",
    "DeliveryPrivacyObservationV1",
    "ExactlyOnceObservationV1",
    "FailureIsolationObservationV1",
    "ScoreV1",
    "ScoreV1EvaluatorRegistry",
    "ToolLifecycleObservationV1",
]
