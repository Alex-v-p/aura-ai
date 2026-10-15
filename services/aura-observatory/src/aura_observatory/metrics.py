"""Bounded metadata-only ingest counters."""

from __future__ import annotations

from dataclasses import dataclass
from time import monotonic


@dataclass(slots=True)
class IngestCounters:
    accepted: int = 0
    rejected: int = 0
    duplicate: int = 0
    malformed: int = 0
    storage_failure: int = 0
    duration_ms_total: float = 0.0
    def record_duration(self, started: float) -> float:
        """Record and return one batch duration, never a cumulative value."""

        duration_ms = max(0.0, (monotonic() - started) * 1000.0)
        self.duration_ms_total += duration_ms
        return duration_ms

    def as_dict(self) -> dict[str, int | float]:
        return {
            "telemetry_ingest_accepted_count": self.accepted,
            "telemetry_ingest_rejected_count": self.rejected,
            "telemetry_ingest_duplicate_count": self.duplicate,
            "telemetry_ingest_malformed_count": self.malformed,
            "telemetry_ingest_storage_failure_count": self.storage_failure,
            "telemetry_ingest_duration_ms": self.duration_ms_total,
        }


__all__ = ["IngestCounters"]
