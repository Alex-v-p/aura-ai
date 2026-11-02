"""Inward-facing ports owned by the memory domain.

The memory application layer depends on these small protocols rather than on
provider adapters.  Keeping the ports here also gives the implementation
modules a stable dependency direction: runtime providers implement the ports,
while the domain only consumes them.
"""

from __future__ import annotations

from typing import Protocol

from aura_core.runtime.models.ports import EmbeddingPort, StructuredInferencePort


class MemoryTelemetry(Protocol):
    """Metadata-only memory telemetry sink.

    The protocol deliberately enumerates the fields emitted by memory
    processing.  Values are identifiers, counters, timings, and outcome
    metadata; prompts, memory content, vectors, and provider payloads never
    cross this boundary.
    """

    def __call__(
        self,
        *,
        operation: str,
        duration_ms: float,
        trace_id: str,
        outcome: str,
        error_class: str | None = None,
        memory_id: str | None = None,
        memory_revision_id: str | None = None,
        generation_id: str | None = None,
        attempt_count: int | None = None,
        backlog: int | None = None,
        progress: float | None = None,
    ) -> None: ...


__all__ = ["EmbeddingPort", "MemoryTelemetry", "StructuredInferencePort"]
