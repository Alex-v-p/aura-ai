"""Application-facing dependency readiness service."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol


@dataclass(frozen=True, slots=True)
class DependencyCheck:
    status: str
    detail: str | None = None


class HealthProbe(Protocol):
    async def check(self) -> DependencyCheck: ...


@dataclass(frozen=True, slots=True)
class ReadinessReport:
    status: str
    checks: dict[str, DependencyCheck]
    checked_at: datetime


class ReadinessService:
    def __init__(self, probes: Mapping[str, HealthProbe]) -> None:
        self.probes = probes

    async def check(self) -> ReadinessReport:
        checks = {name: await probe.check() for name, probe in self.probes.items()}
        ready = all(item.status == "ready" for item in checks.values())
        return ReadinessReport(
            "ready" if ready else "degraded",
            checks,
            datetime.now(UTC),
        )
