"""Bounded metadata-only ingest counters."""

from __future__ import annotations

import json
import math
import os
import tempfile
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import Any, cast


class MetricLabels(tuple[tuple[str, str], ...]):
    """Tuple-compatible labels with a bounded mapping accessor."""

    def get(self, key: str, default: str | None = None) -> str | None:
        return dict(self).get(key, default)


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


class ObservatoryMetrics:
    """Content-free metric sink with bounded, allowlisted labels."""

    HISTOGRAMS = frozenset(
        {"observatory_query_duration_ms", "observatory_auth_callback_duration_ms"}
    )

    ALLOWED = {
        "observatory_query_duration_ms",
        "observatory_query_outcome",
        "observatory_sse_connection_count",
        "observatory_sse_active",
        "observatory_sse_replay_count",
        "observatory_retention_action_count",
        "observatory_retention_deleted_count",
        "observatory_retention_failure_count",
        "observatory_auth_callback_duration_ms",
        "observatory_auth_callback_outcome",
    }
    LABELS = {
        "observatory_query_duration_ms": {"resource"},
        "observatory_query_outcome": {"resource", "outcome"},
        "observatory_sse_connection_count": {"event_type"},
        "observatory_sse_active": {"event_type"},
        "observatory_sse_replay_count": {"event_type"},
        "observatory_retention_action_count": {"action", "status"},
        "observatory_retention_deleted_count": {"action"},
        "observatory_retention_failure_count": {"action", "failure_code"},
        "observatory_auth_callback_duration_ms": {"stage", "outcome"},
        "observatory_auth_callback_outcome": {"stage", "outcome"},
    }
    LABEL_VALUES = {
        "resource": {
            "overview",
            "runs",
            "run_detail",
            "trace_detail",
            "components",
            "component_health",
            "deployments",
            "events",
        },
        "outcome": {
            "succeeded",
            "failed",
            "cancelled",
            "unknown",
            "success",
            "unavailable",
            "missing",
            "invalid",
            "denied",
            "code_missing",
            "exchange_failed",
            "rejected",
        },
        "event_type": {"stream", "connection", "observation.changed", "heartbeat"},
        "action": {"delete_before", "purge_all"},
        "status": {"queued", "running", "succeeded", "failed"},
        "failure_code": {"storage_failure", "conflict", "cancelled", "unknown"},
        "stage": {
            "callback",
            "login_state_store",
            "provider",
            "authorization",
            "session_store",
        },
    }
    DURATION_BUCKETS = (5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000)

    def __init__(self, export_path: Path | None = None) -> None:
        self.values: dict[tuple[str, tuple[tuple[str, str], ...]], float] = defaultdict(float)
        self._histogram_counts: dict[
            tuple[str, tuple[tuple[str, str], ...]], int
        ] = defaultdict(int)
        self._histogram_buckets: dict[
            tuple[str, tuple[tuple[str, str], ...]], dict[int, int]
        ] = defaultdict(lambda: defaultdict(int))
        # The API and dedicated worker are separate processes.  A bounded,
        # content-free compact series file gives both processes a shared
        # Prometheus exposition without making worker metrics depend on the
        # API process being alive.  Deployments place this file below the
        # encrypted Observatory state root.
        self.export_path = export_path
        self._external_write_failed = False

    def record(
        self, name: str, value: float = 1.0, labels: Mapping[str, str] | None = None
    ) -> None:
        if name not in self.ALLOWED:
            raise ValueError("unknown Observatory metric")
        supplied = dict(labels or {})
        self._validate_labels(name, supplied)
        if not math.isfinite(float(value)):
            raise ValueError("metric value must be finite")
        key = (name, MetricLabels(sorted((str(k), str(v)) for k, v in supplied.items())))
        self.values[key] += float(value)
        if name in self.HISTOGRAMS:
            self._histogram_counts[key] += 1
            for bucket in self.DURATION_BUCKETS:
                if value <= bucket:
                    self._histogram_buckets[key][bucket] += 1
        if self.export_path is not None:
            try:
                self._write_external(name, key[1], float(value))
            except OSError:
                # Keep the local series available for diagnostics if the
                # shared sink is temporarily unavailable.
                self._external_write_failed = True

    def _write_external(
        self, name: str, labels: tuple[tuple[str, str], ...], value: float
    ) -> None:
        """Atomically update one bounded series in the shared sink."""

        assert self.export_path is not None
        self.export_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.export_path.with_name(self.export_path.name + ".lock")
        import fcntl

        with lock_path.open("a+", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                records: dict[str, dict[str, Any]] = {}
                try:
                    with self.export_path.open("r", encoding="utf-8") as stream:
                        loaded: object = json.load(stream)
                        if isinstance(loaded, dict):
                            loaded_dict = cast(dict[object, object], loaded)
                            records = {
                                str(key): cast(dict[str, Any], value)
                                for key, value in loaded_dict.items()
                                if isinstance(value, dict)
                            }
                except (FileNotFoundError, json.JSONDecodeError):
                    pass
                series_key = json.dumps([name, list(labels)], separators=(",", ":"))
                current = records.get(series_key, {})
                current["name"] = name
                current["labels"] = dict(labels)
                current["value"] = float(current.get("value", 0.0)) + value
                if name in self.HISTOGRAMS:
                    current["count"] = int(current.get("count", 0)) + 1
                    buckets = current.setdefault("buckets", {})
                    if isinstance(buckets, dict):
                        bucket_values = cast(dict[str, int], buckets)
                        for bucket in self.DURATION_BUCKETS:
                            if value <= bucket:
                                bucket_values[str(bucket)] = (
                                    int(bucket_values.get(str(bucket), 0)) + 1
                                )
                records[series_key] = current
                fd, temporary = tempfile.mkstemp(
                    prefix=f".{self.export_path.name}.", dir=str(self.export_path.parent)
                )
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as stream:
                        json.dump(records, stream, separators=(",", ":"))
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(temporary, self.export_path)
                finally:
                    if os.path.exists(temporary):
                        os.unlink(temporary)
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def snapshot(self) -> dict[str, Any]:
        # Keep each bounded label set as an independent series.  A plain
        # name-keyed dict silently overwrote e.g. succeeded and failed query
        # outcomes and made the in-process snapshot disagree with /metrics.
        return {
            f"{name}|{','.join(f'{key}={label}' for key, label in labels)}": {
                "name": name,
                "labels": dict(labels),
                "value": value,
            }
            for (name, labels), value in self.values.items()
        }

    def prometheus(self, *, exclude_names: set[str] | None = None) -> str:
        """Render the bounded in-process sink for a Prometheus-compatible scrape."""

        lines: list[str] = []
        emitted_types: set[str] = set()
        histogram_counts: dict[tuple[str, tuple[tuple[str, str], ...]], int] = dict(
            self._histogram_counts
        )
        histogram_buckets: dict[
            tuple[str, tuple[tuple[str, str], ...]], dict[int, int]
        ] = {key: dict(value) for key, value in self._histogram_buckets.items()}
        values: dict[tuple[str, tuple[tuple[str, str], ...]], float] = dict(self.values)
        if self.export_path is not None and not self._external_write_failed:
            external_values: defaultdict[
                tuple[str, tuple[tuple[str, str], ...]], float
            ] = defaultdict(float)
            histogram_counts = {}
            try:
                import fcntl

                lock_path = self.export_path.with_name(self.export_path.name + ".lock")
                with lock_path.open("a+", encoding="utf-8") as lock:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_SH)
                    try:
                        with self.export_path.open("r", encoding="utf-8") as stream:
                            loaded: object = json.load(stream)
                        raw_records = (
                            list(cast(dict[str, Any], loaded).values())
                            if isinstance(loaded, dict)
                            else []
                        )
                        for raw_record in raw_records:
                            try:
                                if not isinstance(raw_record, dict):
                                    continue
                                record = cast(dict[str, Any], raw_record)
                                name = str(record["name"])
                                labels = MetricLabels(
                                    sorted(
                                        (str(key), str(value))
                                        for key, value in dict(record.get("labels", {})).items()
                                    )
                                )
                                if name in self.ALLOWED and self._labels_valid(name, labels):
                                    value = float(record["value"])
                                    if not math.isfinite(value):
                                        continue
                                    external_values[(name, labels)] += float(record["value"])
                                    if name in self.HISTOGRAMS:
                                        histogram_counts[(name, labels)] = int(
                                            record.get("count", 0)
                                        )
                                        raw_buckets = record.get("buckets", {})
                                        if isinstance(raw_buckets, dict):
                                            bucket_values = cast(dict[str, object], raw_buckets)
                                            histogram_buckets[(name, labels)] = {
                                                int(bucket): int(count)
                                                for bucket, count in bucket_values.items()
                                                if bucket.isdigit()
                                                and isinstance(count, (int, float))
                                                and int(count) >= 0
                                            }
                            except (ValueError, TypeError, KeyError, json.JSONDecodeError):
                                continue
                    finally:
                        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            except OSError:
                external_values = defaultdict(float, self.values)
            values = dict(external_values)
        if exclude_names:
            values = {key: value for key, value in values.items() if key[0] not in exclude_names}
            histogram_counts = {
                key: count for key, count in histogram_counts.items() if key[0] not in exclude_names
            }
            histogram_buckets = {
                key: buckets
                for key, buckets in histogram_buckets.items()
                if key[0] not in exclude_names
            }
        for (name, labels), value in sorted(values.items()):
            metric_type = (
                "histogram"
                if name in self.HISTOGRAMS
                else "gauge"
                if name
                in {
                    "observatory_sse_active",
                    "observatory_retention_action_count",
                    "observatory_retention_failure_count",
                }
                else "counter"
            )
            if name not in emitted_types:
                lines.append(f"# TYPE {name} {metric_type}")
                emitted_types.add(name)
            label_text = ""
            if labels:
                def escape(label: str) -> str:
                    return label.replace("\\", "\\\\").replace('"', '\\"')

                label_text = "{" + ",".join(
                    f'{key}="{escape(str(label))}"'
                    for key, label in labels
                ) + "}"
            if name in self.HISTOGRAMS:
                suffix = label_text
                lines.append(f"{name}_sum{suffix} {value:g}")
                lines.append(
                    f"{name}_count{suffix} {histogram_counts.get((name, labels), 0)}"
                )
                buckets = histogram_buckets.get((name, labels), {})
                for bucket in self.DURATION_BUCKETS:
                    bucket_labels = label_text[:-1] + (
                        "," if label_text else "{"
                    ) + f'le="{bucket}"}}'
                    lines.append(
                        f"{name}_bucket{bucket_labels} {buckets.get(bucket, 0)}"
                    )
                inf_labels = label_text[:-1] + (
                    "," if label_text else "{"
                ) + 'le="+Inf"}'
                lines.append(f"{name}_bucket{inf_labels} {histogram_counts.get((name, labels), 0)}")
            else:
                lines.append(f"{name}{label_text} {value:g}")
        return "\n".join(lines) + ("\n" if lines else "")

    @classmethod
    def prometheus_for_series(cls, series: list[Mapping[str, object]]) -> str:
        """Render validated service-owned persisted series for API scraping."""

        metrics = cls()
        for item in series:
            name = item.get("name")
            value = item.get("value")
            labels = item.get("labels")
            if not isinstance(name, str) or not isinstance(value, (int, float)):
                continue
            if not isinstance(labels, Mapping):
                continue
            label_mapping = cast(Mapping[object, object], labels)
            if any(
                not isinstance(key, str) or not isinstance(label, str)
                for key, label in label_mapping.items()
            ):
                continue
            try:
                metrics.record(
                    name,
                    float(value),
                    {
                        cast(str, key): cast(str, label)
                        for key, label in label_mapping.items()
                    },
                )
            except (TypeError, ValueError):
                continue
        return metrics.prometheus()

    @classmethod
    def _labels_valid(
        cls,
        name: str,
        labels: Mapping[str, str] | tuple[tuple[str, str], ...],
    ) -> bool:
        normalized = dict(labels)
        allowed = cls.LABELS[name]
        if set(normalized) - allowed:
            return False
        return all(
            value in cls.LABEL_VALUES.get(key, set())
            for key, value in normalized.items()
        )

    @classmethod
    def _validate_labels(cls, name: str, labels: Mapping[str, str]) -> None:
        if not cls._labels_valid(name, labels):
            raise ValueError("metric labels are not bounded")


__all__ = ["IngestCounters", "ObservatoryMetrics"]
