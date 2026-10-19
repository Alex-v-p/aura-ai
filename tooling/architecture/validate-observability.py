#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from common import ROOT, emit, read_yaml, resolve_work_item, validate_document

ManifestEntry = tuple[Path, Any]

# Observatory's health manifests are the first component catalogue entries
# with a typed metric contract.  Keep this set explicit so the stronger checks
# apply only to the manifests that opted into that contract; older Core
# manifests continue to use the repository's compatible catalogue rules.
OBSERVATORY_TYPED_COMPONENTS = frozenset(
    {
        "aura.observatory.telemetry_ingestion",
        "aura.observatory.telemetry_storage",
        "aura.observatory.query_api",
        "aura.observatory.live_updates",
        "aura.observatory.evaluation_artifact_storage",
        "aura.observatory.evaluation_deletion",
        "aura.observatory.evaluation_orchestration",
        "aura.observatory.evaluation_scheduler",
    }
)
TYPED_COMPONENTS = OBSERVATORY_TYPED_COMPONENTS | {"aura.evaluation.runner"}
OBSERVATORY_METRIC_TYPES = frozenset({"counter", "gauge", "histogram"})
RUNNER_METRIC_UNITS = frozenset({"milliseconds", "count", "ratio"})
RUNNER_METRIC_ATTRIBUTES = frozenset({"outcome", "error_class", "dependency"})
RUNNER_METRIC_DEFINITION_FIELDS = frozenset(
    {"name", "type", "unit", "labels", "dependencies", "trace", "correlation", "export"}
)
RUNNER_METRIC_EXPORTS = frozenset({"structured_container_log", "otlp"})
RUNNER_ALLOWED_DEPENDENCIES = frozenset(
    {
        "postgresql",
        "evaluation_nats",
        "evaluation_object_storage",
        "core_public_replay_ports",
        "application_validation",
        "application_unknown",
        "model_provider",
        "object_storage",
        "nats_jetstream",
    }
)
RUNNER_ALLOWED_LABEL_VALUES = {
    "outcome": frozenset(
        {
            "accepted",
            "canceled",
            "completed",
            "delivery",
            "error",
            "expired",
            "fenced",
            "invalid",
            "malformed_envelope",
            "ok",
            "published",
            "recovered",
            "rejected",
            "transport_error",
            "transport_published",
        }
    ),
    "error_class": frozenset(
        {
            "authorization",
            "budget",
            "cancellation",
            "conflict",
            "database",
            "database_programming",
            "delivery",
            "execution",
            "identity",
            "provider",
            "timeout",
            "unknown",
            "validation",
        }
    ),
    "dependency": RUNNER_ALLOWED_DEPENDENCIES,
}
RUNNER_TRACE_ATTRIBUTES = frozenset(
    {"trace_id", "span_id", "parent_span_id", "correlation_id", "attempt_id"}
)
RUNNER_PROHIBITED_FIELDS = frozenset(
    {
        "access_token",
        "argument",
        "arguments",
        "arbitrary_attributes",
        "attributes",
        "authorization",
        "bearer",
        "bearer_token",
        "content",
        "credentials",
        "digest",
        "document_content",
        "prompt",
        "raw_content",
        "raw_result",
        "response",
        "response_content",
        "response_payload",
        "payload",
        "result",
        "results",
        "artifact_bytes",
        "filename",
        "object_key",
        "owner",
        "owner_identity",
        "password",
        "private",
        "refresh_token",
        "secret",
        "token",
        "tokens",
        "tool_argument",
        "tool_arguments",
        "tool_result",
        "tool_results",
        "credential",
        "endpoint",
        "exception",
        "id_token",
        "memory_content",
        "owner_subject",
        "url",
        "uri",
    }
)
RUNNER_EXPORT_HEALTH_COUNTERS = frozenset(
    {"accepted", "rejected", "dropped", "export_failures", "abandoned_batches"}
)
OBSERVATORY_REQUIRED_PROHIBITED_FIELDS = frozenset(
    {
        "prompt",
        "response",
        "content",
        "filename",
        "arguments",
        "result",
        "endpoint",
        "token",
        "credential",
        "owner_identity",
    }
)


def _string_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) and item.strip() for item in value)


def _nonempty_string_list(value: Any) -> bool:
    return _string_list(value) and bool(value)


def _typed_observatory_errors(path: Path, manifest: dict[str, Any]) -> list[str]:
    """Validate the closed, content-free metric contract for AURA-0085."""

    location = str(path.relative_to(ROOT))
    errors: list[str] = []
    component = manifest.get("component")
    if not isinstance(component, dict) or component.get("id") not in OBSERVATORY_TYPED_COMPONENTS:
        return errors

    metrics = manifest.get("metrics")
    metric_names = set(metrics) if _string_list(metrics) else set()
    definitions = manifest.get("metric_definitions")
    if not isinstance(definitions, list) or not definitions:
        errors.append(f"{location}: typed Observatory manifests require metric_definitions")
        return errors
    definition_names: list[str] = []
    for index, definition in enumerate(definitions):
        prefix = f"{location}: metric_definitions[{index}]"
        if not isinstance(definition, dict):
            errors.append(f"{prefix} must be a mapping")
            continue
        name = definition.get("name")
        if not isinstance(name, str) or not name.strip():
            errors.append(f"{prefix}.name is required")
        else:
            definition_names.append(name)
        if definition.get("type") not in OBSERVATORY_METRIC_TYPES:
            errors.append(f"{prefix}.type must be counter, gauge, or histogram")
        if not isinstance(definition.get("unit"), str) or not definition["unit"].strip():
            errors.append(f"{prefix}.unit is required")
        labels = definition.get("labels")
        if not _string_list(labels):
            errors.append(f"{prefix}.labels must be a bounded string list")
        dependencies = definition.get("dependencies")
        if not _nonempty_string_list(dependencies):
            errors.append(f"{prefix}.dependencies must be a non-empty string list")
        if not isinstance(definition.get("trace"), str) or not definition["trace"].strip():
            errors.append(f"{prefix}.trace is required")
        if (
            not isinstance(definition.get("correlation"), str)
            or not definition["correlation"].strip()
        ):
            errors.append(f"{prefix}.correlation is required")
        if definition.get("export") != "prometheus":
            errors.append(f"{prefix}.export must be prometheus")

    if len(definition_names) != len(set(definition_names)):
        errors.append(f"{location}: metric definition names must be unique")
    if metric_names != set(definition_names):
        errors.append(f"{location}: metrics and metric_definitions must contain the same names")

    semantics = manifest.get("metric_semantics")
    if semantics is not None and (
        not isinstance(semantics, dict) or set(semantics) != metric_names
    ):
        errors.append(f"{location}: metric_semantics must cover each declared metric exactly once")

    top_dependencies = manifest.get("dependencies")
    if top_dependencies is not None and not _string_list(top_dependencies):
        errors.append(f"{location}: dependencies must be a bounded string list when present")

    input_metadata = manifest.get("input")
    output_metadata = manifest.get("output")
    for name, metadata in (("input", input_metadata), ("output", output_metadata)):
        if not isinstance(metadata, dict):
            continue
        if not isinstance(metadata.get("schema"), str) or not metadata["schema"].strip():
            errors.append(f"{location}: {name}.schema is required")
        if "replayable" in metadata and not isinstance(metadata.get("replayable"), bool):
            errors.append(f"{location}: {name}.replayable must be boolean")

    evaluators = manifest.get("evaluators")
    if not _nonempty_string_list(evaluators):
        errors.append(f"{location}: typed Observatory manifests require bounded evaluators")
    if manifest.get("capture_policy") != "metadata_only":
        errors.append(f"{location}: capture_policy must be metadata_only")
    if manifest.get("retention") != "manual_owner_delete_only":
        errors.append(f"{location}: retention must be manual_owner_delete_only")

    cardinality = manifest.get("cardinality")
    if not isinstance(cardinality, dict):
        errors.append(f"{location}: cardinality metadata is required")
        cardinality = {}
    metric_attributes = cardinality.get("metric_attributes")
    trace_only = cardinality.get("trace_only")
    prohibited = cardinality.get("prohibited")
    if not _string_list(metric_attributes):
        errors.append(f"{location}: cardinality.metric_attributes must be a bounded string list")
        metric_attributes = []
    if trace_only is not None and not _string_list(trace_only):
        errors.append(f"{location}: cardinality.trace_only must be a bounded string list")
        trace_only = []
    elif trace_only is None:
        trace_only = []
    if not _string_list(prohibited):
        errors.append(f"{location}: cardinality.prohibited must be a bounded string list")
        prohibited = []
    if not OBSERVATORY_REQUIRED_PROHIBITED_FIELDS <= set(prohibited):
        errors.append(f"{location}: cardinality.prohibited lacks required content/privacy fields")

    metric_attribute_set = set(metric_attributes)
    for index, definition in enumerate(definitions):
        if not isinstance(definition, dict):
            continue
        labels = definition.get("labels")
        if _string_list(labels) and not set(labels) <= metric_attribute_set:
            errors.append(
                f"{location}: metric_definitions[{index}].labels must be declared "
                "in cardinality.metric_attributes"
            )
    return errors


def _runner_metric_type(name: str) -> str | None:
    """Return the required type for a runner metric name, if it is known."""

    if name.endswith("_duration_ms") or name.endswith("_time_ms"):
        return "histogram"
    if name.endswith("_reproducibility_completeness"):
        return "gauge"
    if name.startswith("evaluation_runner_"):
        return "counter"
    return None


def _typed_runner_errors(path: Path, manifest: dict[str, Any]) -> list[str]:
    """Validate the closed metadata contract for the Core evaluation runner."""

    location = str(path.relative_to(ROOT))
    component = manifest.get("component")
    if not isinstance(component, dict) or component.get("id") != "aura.evaluation.runner":
        return []

    errors: list[str] = []
    metrics = manifest.get("metrics")
    metric_names = set(metrics) if _string_list(metrics) else set()
    if not metric_names:
        errors.append(f"{location}: runner metrics must be a non-empty string list")
        return errors

    definitions = manifest.get("metric_definitions")
    if not isinstance(definitions, list) or not definitions:
        errors.append(f"{location}: runner metric_definitions are required")
        definitions = []

    semantics = manifest.get("metric_semantics")
    if not isinstance(semantics, dict) or set(semantics) != metric_names:
        errors.append(f"{location}: runner metric_semantics must cover each metric exactly once")
    elif not all(isinstance(value, str) and value.strip() for value in semantics.values()):
        errors.append(f"{location}: runner metric_semantics values must be non-empty strings")

    units = manifest.get("metric_units")
    if not isinstance(units, dict) or set(units) != metric_names:
        errors.append(f"{location}: runner metric_units must cover each metric exactly once")
    else:
        for name in metric_names:
            unit = units.get(name)
            if unit not in RUNNER_METRIC_UNITS:
                errors.append(f"{location}: metric_units[{name}] is not a bounded unit")
            expected_type = _runner_metric_type(name)
            if expected_type is None:
                errors.append(f"{location}: runner metric has no bounded type: {name}")
            elif unit == "milliseconds" and expected_type != "histogram":
                errors.append(f"{location}: duration metric must be a histogram: {name}")
            elif unit == "ratio" and expected_type != "gauge":
                errors.append(f"{location}: ratio metric must be a gauge: {name}")
            elif unit == "count" and expected_type != "counter":
                errors.append(f"{location}: count metric must be a counter: {name}")

    definition_names: list[str] = []
    for index, definition in enumerate(definitions):
        prefix = f"{location}: metric_definitions[{index}]"
        if not isinstance(definition, dict):
            errors.append(f"{prefix} must be a mapping")
            continue
        if set(definition) != RUNNER_METRIC_DEFINITION_FIELDS:
            errors.append(f"{prefix} must use the closed per-metric field set")
        name = definition.get("name")
        if not isinstance(name, str) or name not in metric_names:
            errors.append(f"{prefix}.name must identify a declared runner metric")
        else:
            definition_names.append(name)
            expected_type = _runner_metric_type(name)
            if definition.get("type") != expected_type:
                errors.append(f"{prefix}.type does not match the metric name contract")
            expected_unit = units.get(name) if isinstance(units, dict) else None
            if definition.get("unit") != expected_unit:
                errors.append(f"{prefix}.unit does not match metric_units")
        labels = definition.get("labels")
        if (
            not _string_list(labels)
            or len(labels) != len(set(labels))
            or set(labels) != RUNNER_METRIC_ATTRIBUTES
        ):
            errors.append(f"{prefix}.labels must be the fixed bounded label set")
        else:
            for label in labels:
                if label not in RUNNER_ALLOWED_LABEL_VALUES:
                    errors.append(f"{prefix}.labels contains an unknown label: {label}")
        dependencies_for_metric = definition.get("dependencies")
        if not _nonempty_string_list(dependencies_for_metric) or not set(
            dependencies_for_metric
        ) <= RUNNER_ALLOWED_DEPENDENCIES:
            errors.append(f"{prefix}.dependencies contains an unbounded value")
        if definition.get("trace") != "evaluation_runner":
            errors.append(f"{prefix}.trace must be evaluation_runner")
        if definition.get("correlation") != "attempt_id":
            errors.append(f"{prefix}.correlation must be attempt_id")
        export = definition.get("export")
        if not _string_list(export) or set(export) != RUNNER_METRIC_EXPORTS:
            errors.append(f"{prefix}.export must include structured_container_log and otlp")

    if len(definition_names) != len(set(definition_names)):
        errors.append(f"{location}: runner metric definition names must be unique")
    if set(definition_names) != metric_names:
        errors.append(f"{location}: runner metric_definitions must cover each metric exactly once")

    if manifest.get("capture_policy") != "metadata_only":
        errors.append(f"{location}: runner capture_policy must be metadata_only")

    dependencies = manifest.get("dependencies")
    if not _nonempty_string_list(dependencies):
        errors.append(f"{location}: runner dependencies must be a non-empty string list")

    cardinality = manifest.get("cardinality")
    if not isinstance(cardinality, dict):
        errors.append(f"{location}: runner cardinality metadata is required")
        cardinality = {}
    metric_attributes = cardinality.get("metric_attributes")
    trace_only = cardinality.get("trace_only")
    prohibited = cardinality.get("prohibited")
    if set(metric_attributes or ()) != RUNNER_METRIC_ATTRIBUTES or not _string_list(
        metric_attributes
    ):
        errors.append(f"{location}: runner metric_attributes must be the fixed bounded set")
    if set(trace_only or ()) != RUNNER_TRACE_ATTRIBUTES or not _string_list(trace_only):
        errors.append(f"{location}: runner trace_only must be the fixed bounded set")
    if not _string_list(prohibited) or not RUNNER_PROHIBITED_FIELDS <= set(prohibited):
        errors.append(f"{location}: runner prohibited fields lack required privacy fields")

    export = manifest.get("export")
    if not isinstance(export, dict):
        errors.append(f"{location}: runner export semantics are required")
    else:
        if export.get("sink") != "structured_container_log_and_otlp":
            errors.append(f"{location}: runner export sink is not bounded")
        if export.get("cadence") != "periodic_and_shutdown":
            errors.append(f"{location}: runner export cadence is not bounded")
        if export.get("retry") != "bounded_three_attempts":
            errors.append(f"{location}: runner export retry is not bounded")
        counters = export.get("health_counters")
        if not _nonempty_string_list(counters) or set(counters) != RUNNER_EXPORT_HEALTH_COUNTERS:
            errors.append(f"{location}: runner export health counters are not fixed and bounded")

    retention = manifest.get("retention")
    if not isinstance(retention, dict):
        errors.append(f"{location}: runner process retention must be bounded")
    else:
        expected_retention = {
            "process_buffer": "bounded",
            "overflow": "drop_oldest_and_count",
            "export_failure": "retry_three_times_then_drop_and_count",
        }
        if retention != expected_retention:
            errors.append(f"{location}: runner process retention is not the bounded policy")

    return errors


def component_manifests() -> list[ManifestEntry]:
    manifests: list[ManifestEntry] = []
    patterns = [
        "services/*/resources/component-manifests/*.yaml",
        "services/*/resources/component-manifests/*.yml",
    ]
    for pattern in patterns:
        for path in ROOT.glob(pattern):
            document = read_yaml(path)
            manifests.append((path, document))
    return manifests


def component_catalog(
    manifests: list[ManifestEntry] | None = None,
) -> dict[str, ManifestEntry]:
    """Build the valid-ID lookup used by work-item-specific checks."""

    if manifests is None:
        manifests = component_manifests()
    catalog: dict[str, ManifestEntry] = {}
    for path, manifest in manifests:
        if not isinstance(manifest, dict):
            continue
        component = manifest.get("component")
        if not isinstance(component, dict):
            continue
        identifier = component.get("id")
        if isinstance(identifier, str) and identifier.strip():
            catalog[identifier] = (path, manifest)
    return catalog


def catalogue_errors(
    manifests: list[ManifestEntry] | dict[str, ManifestEntry],
) -> list[str]:
    """Check the complete manifest catalogue independently of a work item."""

    entries = (
        manifests
        if isinstance(manifests, list)
        else [(path, manifest) for path, manifest in manifests.values()]
    )
    errors: list[str] = []
    seen: set[str] = set()
    for path, manifest in entries:
        location = str(path.relative_to(ROOT))
        if not isinstance(manifest, dict):
            errors.append(f"{location}: manifest must be a mapping")
            continue
        component = manifest.get("component")
        if not isinstance(component, dict):
            errors.append(f"{location}: component metadata is required")
            continue
        identifier = component.get("id")
        if not isinstance(identifier, str) or not identifier.strip():
            errors.append(f"{location}: component.id is required")
        elif identifier in seen:
            errors.append(f"{location}: duplicate component id {identifier}")
        else:
            seen.add(identifier)
        for field in ("version", "owner", "category"):
            if not isinstance(component.get(field), str) or not component[field].strip():
                errors.append(f"{location}: component.{field} is required")
        for section in ("input", "output"):
            if not isinstance(manifest.get(section), dict):
                errors.append(f"{location}: {section} metadata is required")
        metrics = manifest.get("metrics")
        if (
            not isinstance(metrics, list)
            or not metrics
            or not all(isinstance(metric, str) and metric.strip() for metric in metrics)
        ):
            errors.append(f"{location}: at least one non-empty metric is required")
        if (
            not isinstance(manifest.get("capture_policy"), str)
            or not manifest["capture_policy"].strip()
        ):
            errors.append(f"{location}: capture_policy is required")
        errors.extend(_typed_observatory_errors(path, manifest))
        errors.extend(_typed_runner_errors(path, manifest))
    if not entries:
        errors.append("No observable component manifests exist in the component catalogue.")
    return sorted(set(errors))


def work_item_errors(path: Path) -> list[str]:
    item, errors = validate_document(path, "work-item.schema.json")
    if errors:
        return errors
    config = item.get("observability", {}) if isinstance(item, dict) else {}
    if not config.get("applicable"):
        return []
    manifests = component_manifests()
    catalog = component_catalog(manifests)
    errors.extend(catalogue_errors(manifests))
    if not config.get("affected_components"):
        errors.append("Affected observable components are required.")
    if not config.get("required_metrics") and not config.get("required_evaluators"):
        errors.append("Each affected runtime component needs required metrics or evaluators.")
    if config.get("capture_policy") is None:
        errors.append("An applicable observability change requires an explicit capture_policy.")
    metrics: set[str] = set()
    evaluators: set[str] = set()
    for identifier in config.get("affected_components", []):
        if identifier not in catalog:
            errors.append(f"Affected component has no manifest: {identifier}")
            continue
        path, manifest = catalog[identifier]
        component = manifest.get("component", {})
        if not component.get("version"):
            errors.append(f"{path.relative_to(ROOT)}: component version is required")
        metrics.update(manifest.get("metrics", []))
        evaluators.update(manifest.get("evaluators", []))
    missing_metrics = set(config.get("required_metrics", [])) - metrics
    missing_evaluators = set(config.get("required_evaluators", [])) - evaluators
    if missing_metrics:
        errors.append(f"Component manifests lack required metrics: {sorted(missing_metrics)}")
    if missing_evaluators:
        errors.append(f"Component manifests lack required evaluators: {sorted(missing_evaluators)}")
    return sorted(set(errors))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--work-item")
    args = parser.parse_args()
    if args.work_item:
        path = resolve_work_item(args.work_item)
        if path is None or not path.is_file():
            raise SystemExit(emit("failed", "observability", ["A valid work item is required."]))
        item, item_errors = validate_document(path, "work-item.schema.json")
        config = item.get("observability", {}) if isinstance(item, dict) else {}
        if item_errors:
            raise SystemExit(emit("failed", "observability", item_errors))
        if not config.get("applicable"):
            raise SystemExit(
                emit(
                    "skipped", "observability", ["Work item declares observability not applicable."]
                )
            )
        errors = work_item_errors(path)
        raise SystemExit(
            emit(
                "failed" if errors else "passed",
                "observability",
                errors or ["Observability obligations are declared."],
            )
        )
    errors = catalogue_errors(component_manifests())
    raise SystemExit(
        emit(
            "failed" if errors else "passed",
            "observability",
            errors or ["Observable component catalogue is complete."],
        )
    )
