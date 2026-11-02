"""AURA-0098 runtime and manifest observability consistency checks."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import yaml
from aura_observatory.adapters.dashboards import MemoryOperationsRepository

ROOT = Path(__file__).parents[3]
MANIFEST = ROOT / "services/aura-observatory/resources/component-manifests/alerting.yaml"


class _Metrics:
    def __init__(self) -> None:
        self.values: list[tuple[str, dict[str, str]]] = []

    def record(
        self, name: str, _value: float = 1.0, labels: dict[str, str] | None = None
    ) -> None:
        self.values.append((name, dict(labels or {})))


def test_alert_recovery_manifest_contains_every_runtime_outcome() -> None:
    manifest = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
    assert isinstance(manifest, dict)
    metric_definitions = cast(dict[str, dict[str, object]], manifest["metric_definitions"])
    declared = set(
        cast(
            list[str],
            metric_definitions["observatory_operations_recovery_count"]["bounded_outcomes"],
        )
    )

    metrics = _Metrics()
    repository = MemoryOperationsRepository(metrics=metrics)
    repository._record_recovery("complete")  # pyright: ignore[reportPrivateUsage]
    repository._record_recovery("complete", "failed")  # pyright: ignore[reportPrivateUsage]
    repository._record_recovery("recover", "conflict")  # pyright: ignore[reportPrivateUsage]
    repository._record_recovery("lease_expired", "recovered")  # pyright: ignore[reportPrivateUsage]

    runtime_outcomes = {
        labels["outcome"]
        for name, labels in metrics.values
        if name == "observatory_operations_recovery_count"
    }
    assert runtime_outcomes == {"success", "failed", "conflict", "recovered"}
    assert runtime_outcomes <= declared
