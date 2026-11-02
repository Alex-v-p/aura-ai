"""Explicit production replay registry and controlled synthetic seam.

The registry is intentionally boring: adapters are registered by bootstrap,
looked up by exact component/version, and invoked through a narrow protocol.
Requests cannot select Python imports, providers, tools, endpoints, or model
credentials.
"""

# Replay adapters are composed at runtime behind the explicit protocols below.
# pyright: reportUnnecessaryIsInstance=false

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID


class ReplayError(RuntimeError):
    """Content-free replay failure."""

    def __init__(self, reason: str, *, dependency: str = "application_validation") -> None:
        super().__init__(reason)
        self.dependency = dependency


def _empty_artifacts() -> dict[UUID, bytes]:
    return {}


def _empty_capture_authorization() -> dict[str, object]:
    return {}


class ArtifactOperationBudget(Protocol):
    def consume(self, count: int = 1) -> None: ...


@dataclass(frozen=True, slots=True)
class ReplayContext:
    attempt_id: UUID
    case_id: UUID
    deadline: datetime
    cancellation: asyncio.Event
    input_artifacts: Mapping[UUID, bytes] = field(default_factory=_empty_artifacts)
    max_input_bytes: int = 0
    max_output_bytes: int = 0
    max_tokens: int = 0
    max_model_calls: int = 0
    max_artifact_operations: int = 0
    capture_authorization: Mapping[str, object] = field(
        default_factory=_empty_capture_authorization
    )
    artifact_budget: ArtifactOperationBudget | None = None
    tokens_used: int = 0
    model_calls: int = 0
    artifact_operations: int = 0

    def checkpoint(self) -> None:
        if self.cancellation.is_set():
            raise ReplayError("cancelled")
        if datetime.now(UTC) >= self.deadline:
            raise ReplayError("deadline")

    def consume(
        self, *, tokens: int = 0, model_calls: int = 0, artifact_operations: int = 0
    ) -> None:
        if tokens < 0 or model_calls < 0 or artifact_operations < 0:
            raise ReplayError("resource limit")
        if self.tokens_used + tokens > self.max_tokens:
            raise ReplayError("resource limit")
        if self.model_calls + model_calls > self.max_model_calls:
            raise ReplayError("resource limit")
        if self.artifact_budget is not None and artifact_operations:
            self.artifact_budget.consume(artifact_operations)
        elif self.artifact_operations + artifact_operations > self.max_artifact_operations:
            raise ReplayError("resource limit", dependency="application_validation")
        object.__setattr__(self, "tokens_used", self.tokens_used + tokens)
        object.__setattr__(self, "model_calls", self.model_calls + model_calls)
        object.__setattr__(
            self, "artifact_operations", self.artifact_operations + artifact_operations
        )


@dataclass(frozen=True, slots=True)
class ReplayResult:
    component_id: str
    component_version: str
    outcome: str
    measurements: Mapping[str, int | float | str]
    output_artifacts: Sequence[tuple[Mapping[str, object], bytes]] = ()
    tokens_used: int = 0
    model_calls: int = 0
    artifact_operations: int = 0

    def __post_init__(self) -> None:
        if self.outcome not in {"pass", "fail"}:
            raise ValueError("replay outcome must be finite")
        if not self.measurements or len(self.measurements) > 32:
            raise ValueError("replay measurements must be bounded")
        for key, value in self.measurements.items():
            if not isinstance(key, str) or len(key) > 64:
                raise ValueError("replay measurement key is invalid")
            if type(value) not in {int, float, str} or isinstance(value, bool):
                raise ValueError("replay measurements must be scalar")


class ReplayAdapter(Protocol):
    component_id: str
    component_version: str
    fixture_revision: str

    async def replay(self, case_id: UUID, context: ReplayContext) -> ReplayResult: ...


class SyntheticScenarioPort(Protocol):
    scenario_id: str
    scenario_version: str
    fixture_revision: str

    async def execute(self, case_id: UUID, context: ReplayContext) -> ReplayResult: ...


class FixtureRevisionProvider(Protocol):
    fixture_revision: str


class RuntimeEvidenceProvider(Protocol):
    def observe(self, *, component_id: str, component_version: str) -> Mapping[str, object]: ...


class MissingRuntimeEvidence:
    """Fail-closed evidence provider used by isolated tests only."""

    def observe(self, *, component_id: str, component_version: str) -> Mapping[str, object]:
        del component_id, component_version
        return {}


ReplayFactory = Callable[[], ReplayAdapter]
SyntheticFactory = Callable[[], SyntheticScenarioPort]


class ReplayRegistry:
    def __init__(self) -> None:
        self._replays: dict[tuple[str, str], ReplayAdapter] = {}
        self._synthetic: dict[tuple[str, str], SyntheticScenarioPort] = {}

    def register_replay(self, adapter: ReplayAdapter) -> None:
        key = (adapter.component_id, adapter.component_version)
        if key in self._replays:
            raise ValueError("replay adapter already registered")
        self._replays[key] = adapter

    def register_synthetic(self, adapter: SyntheticScenarioPort) -> None:
        key = (adapter.scenario_id, adapter.scenario_version)
        if key in self._synthetic:
            raise ValueError("synthetic scenario already registered")
        self._synthetic[key] = adapter

    def require_replay(self, component_id: str, component_version: str) -> ReplayAdapter:
        try:
            return self._replays[(component_id, component_version)]
        except KeyError as exc:
            raise ReplayError("component replay is not registered") from exc

    def require_synthetic(self, scenario_id: str, scenario_version: str) -> SyntheticScenarioPort:
        try:
            return self._synthetic[(scenario_id, scenario_version)]
        except KeyError as exc:
            raise ReplayError("synthetic scenario is not registered") from exc

    async def execute(
        self,
        *,
        execution_kind: str,
        component_id: str,
        component_version: str,
        case_id: UUID,
        context: ReplayContext,
    ) -> ReplayResult:
        context.checkpoint()
        if execution_kind == "module_replay":
            adapter = self.require_replay(component_id, component_version)
            result = await adapter.replay(case_id, context)
        elif execution_kind == "synthetic_end_to_end":
            adapter = self.require_synthetic(component_id, component_version)
            result = await adapter.execute(case_id, context)
        else:
            raise ReplayError("unsupported execution kind")
        context.checkpoint()
        if result.component_id != component_id or result.component_version != component_version:
            raise ReplayError("replay returned an unexpected component version")
        return result


__all__ = [
    "ReplayAdapter",
    "ReplayContext",
    "ReplayError",
    "ReplayRegistry",
    "ReplayResult",
    "RuntimeEvidenceProvider",
    "MissingRuntimeEvidence",
    "SyntheticScenarioPort",
    "FixtureRevisionProvider",
    "ArtifactOperationBudget",
]
