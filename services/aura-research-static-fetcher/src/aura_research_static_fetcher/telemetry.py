"""Bounded metadata-observation-v2 export for the static provider."""

from __future__ import annotations

import json
import os
import re
import sys
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime
from threading import Condition, Thread
from typing import Any, TextIO
from uuid import uuid4

_COMPONENT = "aura.research.extraction"
_VERSION = "1.0.0"
_SAFE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
_LOWER_SAFE = re.compile(r"^[a-z][a-z0-9_.-]{0,31}$")
_COMMIT = re.compile(r"^[0-9a-f]{7,64}$")
_TRACE = re.compile(r"^[0-9a-f]{32}$")
_SPAN = re.compile(r"^[0-9a-f]{16}$")


def _env(name: str, pattern: re.Pattern[str], default: str) -> str:
    value = os.getenv(name, default)
    return value if pattern.fullmatch(value) else default


class BoundedMetadataSink:
    """Nonblocking bounded JSON-lines/OTLP seam with explicit drop accounting."""

    def __init__(
        self,
        *,
        writer: TextIO | None = None,
        exporter: Callable[[bytes], None] | None = None,
        capacity: int = 256,
        background: bool = True,
    ) -> None:
        if capacity < 1:
            raise ValueError("telemetry capacity must be positive")
        self._writer = writer
        self._exporter = exporter
        self._output_path = os.getenv("AURA_RESEARCH_TELEMETRY_OUTPUT", "stdout")
        self._queue: deque[str] = deque(maxlen=capacity)
        self._condition = Condition()
        self._closed = False
        self._dropped = 0
        self._rejected = 0
        self._abandoned_batches = 0
        self._accepted = 0
        self._export_failures = 0
        self._thread: Thread | None = None
        if background:
            self._thread = Thread(target=self._run, name="research-telemetry", daemon=True)
            self._thread.start()

    @property
    def dropped_count(self) -> int:
        with self._condition:
            return self._dropped

    @property
    def stats(self) -> dict[str, int]:
        with self._condition:
            return {
                "accepted": self._accepted,
                "dropped": self._dropped,
                "rejected": self._rejected,
                "export_failures": self._export_failures,
                "abandoned_batches": self._abandoned_batches,
            }

    def record_rejected(self) -> None:
        with self._condition:
            self._rejected += 1

    def emit(self, observation: dict[str, Any]) -> None:
        encoded = json.dumps(observation, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        with self._condition:
            if self._closed:
                self._dropped += 1
                return
            if len(self._queue) == self._queue.maxlen:
                self._queue.popleft()
                self._dropped += 1
            self._queue.append(encoded)
            self._accepted += 1
            self._condition.notify()

    def drain(self) -> int:
        with self._condition:
            lines = list(self._queue)
            self._queue.clear()
        for line in lines:
            self._write(line)
        return len(lines)

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._condition.notify_all()
        if self._thread is not None:
            self._thread.join(timeout=0.5)
            with self._condition:
                if self._queue:
                    self._abandoned_batches += 1
        self.drain()

    def _run(self) -> None:
        while True:
            with self._condition:
                while not self._queue and not self._closed:
                    self._condition.wait(timeout=0.5)
                if not self._queue and self._closed:
                    return
                line = self._queue.popleft()
            self._write(line)

    def _write(self, line: str) -> None:
        def write_target() -> None:
            if self._writer is not None:
                self._writer.write(line + "\n")
                self._writer.flush()
            elif self._output_path == "stdout":
                sys.stdout.write(line + "\n")
                sys.stdout.flush()
            else:
                with open(self._output_path, "a", encoding="utf-8") as output:
                    output.write(line + "\n")

        failed = False
        exporter = self._exporter
        if exporter is not None:
            failed = not self._retry(lambda: exporter(line.encode("utf-8")))
        if self._writer is not None or self._exporter is None:
            failed = not self._retry(write_target) or failed
        if failed:
            with self._condition:
                self._export_failures += 1
                self._dropped += 1

    @staticmethod
    def _retry(operation: Callable[[], None]) -> bool:
        for _attempt in range(3):
            try:
                operation()
                return True
            except Exception:
                continue
        return False


class ProductionExtractionSink:
    def __init__(
        self,
        *,
        writer: TextIO | None = None,
        exporter: Callable[[bytes], None] | None = None,
        capacity: int = 256,
        background: bool = True,
    ) -> None:
        self._sink = BoundedMetadataSink(
            writer=writer, exporter=exporter, capacity=capacity, background=background
        )

    def __call__(
        self,
        outcome: str,
        elapsed_ms: float,
        trace_id: str | None = None,
        parent_span_id: str | None = None,
    ) -> None:
        bounded_outcome = "success" if outcome == "success" else "partial"
        if bounded_outcome != "success":
            self._sink.record_rejected()
        dimensions = {
            "operation": "research.extract",
            "outcome": bounded_outcome,
            "dependency": "content_policy",
            "provider": "none",
            "degradation": "none",
        }
        trace: dict[str, str] | None = None
        if trace_id is not None and _TRACE.fullmatch(trace_id):
            trace = {"traceId": trace_id, "spanId": uuid4().hex[:16]}
            if parent_span_id is not None and _SPAN.fullmatch(parent_span_id):
                trace["parentSpanId"] = parent_span_id
        bounded_elapsed = max(0.0, min(float(elapsed_ms), 120_000.0))
        deployment = {
            "deploymentId": _env("AURA_TELEMETRY_DEPLOYMENT_ID", _SAFE, "local"),
            "gitCommit": _env("AURA_TELEMETRY_GIT_COMMIT", _COMMIT, "0000000"),
            "environment": _env("AURA_TELEMETRY_ENVIRONMENT", _LOWER_SAFE, "development"),
            "hostId": _env("AURA_TELEMETRY_HOST_ID", _SAFE, "local"),
        }

        def observation(kind: str, name: str, value: float, unit: str) -> dict[str, Any]:
            record: dict[str, Any] = {
                "schemaVersion": 2,
                "observationId": str(uuid4()),
                "observedAt": datetime.now(UTC).isoformat(timespec="milliseconds").replace(
                    "+00:00", "Z"
                ),
                "kind": kind,
                "component": {"id": _COMPONENT, "version": _VERSION},
                "operation": "research.extract",
                "measurement": {"name": name, "value": value, "unit": unit},
                "dimensions": dimensions,
                "deployment": deployment,
                "serviceRole": "worker",
                "capturePolicy": "metadata_only",
            }
            if trace is not None and kind == "span":
                record["trace"] = trace
            return record

        self._sink.emit(
            observation("metric", "research_extraction_outcome_count", 1.0, "count")
        )
        self._sink.emit(
            observation(
                "metric", "research_extraction_latency_ms", bounded_elapsed, "milliseconds"
            )
        )
        if bounded_outcome != "success":
            self._sink.emit(
                observation("metric", "research_extraction_rejected_count", 1.0, "count")
            )
        if trace is not None:
            self._sink.emit(
                observation(
                    "span", "research_extraction_latency_ms", bounded_elapsed, "milliseconds"
                )
            )

    @property
    def dropped_count(self) -> int:
        return self._sink.dropped_count

    @property
    def stats(self) -> dict[str, int]:
        return self._sink.stats

    def drain(self) -> int:
        return self._sink.drain()

    def close(self) -> None:
        self._sink.close()


production_extraction_sink = ProductionExtractionSink()

__all__ = ["BoundedMetadataSink", "ProductionExtractionSink", "production_extraction_sink"]
