"""Focused AURA-0097 backend ownership-zone checks."""
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownMemberType=false, reportGeneralTypeIssues=false, reportArgumentType=false, reportIndexIssue=false, reportPrivateUsage=false

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from aura_observatory.adapters.dashboards import MemoryOperationsRepository
from aura_observatory.alerting import AlertError, AlertService
from aura_observatory.annotations import (
    AnnotationError,
    AnnotationService,
    AnnotationTargetUnavailable,
)
from aura_observatory.composition import CompositeAnnotationTargetReferencePort
from aura_observatory.dashboards import DashboardService
from aura_observatory.incidents import IncidentError, IncidentService
from aura_observatory.metrics import ObservatoryMetrics
from aura_observatory.saved_queries import SavedQueryError, SavedQueryService, normalize_definition

NOW = "2026-10-21T10:00:00Z"


def _query() -> dict[str, object]:
    return {
        "kind": "run_search",
        "window": {"kind": "last_hour", "from": None, "to": None},
        "statuses": [],
        "components": [],
        "deployments": [],
        "models": [],
        "pageSize": 10,
    }


@pytest.mark.asyncio
async def test_saved_queries_are_closed_and_idempotent() -> None:
    repository = MemoryOperationsRepository()
    service = SavedQueryService(repository)
    value = await service.create(
        {"name": "Runs", "definition": _query(), "createdAt": NOW}, command_id="query-command-0001"
    )
    replay = await service.create(
        {"name": "Runs", "definition": _query(), "createdAt": NOW}, command_id="query-command-0001"
    )
    assert replay == value
    with pytest.raises(RuntimeError, match="idempotency key was reused"):
        await service.create(
            {"name": "Other", "definition": _query(), "createdAt": NOW},
            command_id="query-command-0001",
        )
    with pytest.raises(SavedQueryError):
        normalize_definition({**_query(), "sql": "select 1"})


@pytest.mark.asyncio
async def test_dashboard_rejects_duplicate_positions() -> None:
    repository = MemoryOperationsRepository()
    query = await SavedQueryService(repository).create(
        {"name": "Runs", "definition": _query(), "createdAt": NOW}
    )
    panel = {
        "panelId": str(uuid4()),
        "title": "Runs",
        "query": {"queryId": query["queryId"], "queryVersion": "1.0.0"},
        "visualization": "table",
        "size": "medium",
        "position": 0,
    }
    with pytest.raises(ValueError):
        await DashboardService(repository).create(
            {
                "name": "D",
                "description": "",
                "panels": [panel, {**panel, "panelId": str(uuid4())}],
                "createdAt": NOW,
            }
        )


@pytest.mark.asyncio
async def test_dashboard_rejects_orphan_and_mixed_saved_query_references() -> None:
    repository = MemoryOperationsRepository()
    query = await SavedQueryService(repository).create(
        {"name": "Runs", "definition": _query(), "createdAt": NOW}
    )

    def panel(query_id: str, position: int) -> dict[str, object]:
        return {
            "panelId": str(uuid4()),
            "title": f"Panel {position}",
            "query": {"queryId": query_id, "queryVersion": "1.0.0"},
            "visualization": "table",
            "size": "medium",
            "position": position,
        }

    service = DashboardService(repository)
    with pytest.raises(KeyError, match="saved query not found"):
        await service.create(
            {
                "name": "Orphan",
                "description": "",
                "panels": [panel(str(uuid4()), 0)],
                "createdAt": NOW,
            }
        )
    with pytest.raises(KeyError, match="saved query not found"):
        await service.create(
            {
                "name": "Mixed",
                "description": "",
                "panels": [panel(str(query["queryId"]), 0), panel(str(uuid4()), 1)],
                "createdAt": NOW,
            }
        )
    assert (await service.list())["items"] == []


@pytest.mark.asyncio
async def test_incident_transition_requires_valid_state() -> None:
    repository = MemoryOperationsRepository()
    rule_id = str(uuid4())
    incident = await IncidentService(repository).create(
        {
            "title": "degraded",
            "severity": "warning",
            "impact": "limited",
            "relatedAlerts": [
                {
                    "resourceType": "alert_instance",
                    "resourceId": rule_id,
                    "resourceVersion": "1.0.0",
                }
            ],
            "evidence": [],
            "createdAt": NOW,
        }
    )
    with pytest.raises(IncidentError):
        await IncidentService(repository).transition(
            incident["incidentId"],
            {
                "expectedVersion": "1.0.0",
                "targetState": "closed",
                "impact": "limited",
                "resolutionNote": None,
                "transitionedAt": NOW,
            },
        )


@pytest.mark.asyncio
async def test_invalid_incident_transition_keeps_error_and_records_validation_span() -> None:
    repository = MemoryOperationsRepository()
    incident = await IncidentService(repository).create(
        {
            "title": "degraded",
            "severity": "warning",
            "impact": "limited",
            "relatedAlerts": [
                {
                    "resourceType": "alert_instance",
                    "resourceId": str(uuid4()),
                    "resourceVersion": "1.0.0",
                }
            ],
            "evidence": [],
            "createdAt": NOW,
        }
    )
    metrics = ObservatoryMetrics()
    with pytest.raises(IncidentError):
        await IncidentService(repository, metrics=metrics).transition(
            incident["incidentId"],
            {
                "expectedVersion": "1.0.0",
                "targetState": "closed",
                "impact": "limited",
                "resolutionNote": None,
                "transitionedAt": NOW,
            },
        )
    span = metrics.component_spans[-1]
    assert (span.operation, span.outcome, span.dependency, span.error_code) == (
        "validate",
        "failure",
        None,
        "validation",
    )
    assert span.duration_ms > 0
    assert not any(
        name == "observatory_incident_transition_count"
        for (name, _labels) in metrics.values
    )


@pytest.mark.asyncio
async def test_annotation_bounds_and_live_event_are_content_free() -> None:
    repository = MemoryOperationsRepository()
    target_id = str(uuid4())
    incident = await IncidentService(repository).create(
        {
            "title": "degraded",
            "severity": "warning",
            "impact": "limited",
            "relatedAlerts": [
                {
                    "resourceType": "alert_instance",
                    "resourceId": target_id,
                    "resourceVersion": "1.0.0",
                }
            ],
            "evidence": [],
            "createdAt": NOW,
        }
    )
    annotation = await AnnotationService(repository).create(
        {
            "target": {
                "targetType": "incident",
                "targetId": incident["incidentId"],
                "targetVersion": incident["incidentVersion"],
            },
            "text": "owner note",
            "createdAt": NOW,
        }
    )
    assert "text" not in (await repository.events_after(None))[0]
    with pytest.raises(AnnotationError):
        await AnnotationService(repository).create(
            {
                "target": {
                    "targetType": "incident",
                    "targetId": incident["incidentId"],
                    "targetVersion": incident["incidentVersion"],
                },
                "text": "x" * 2001,
                "createdAt": NOW,
            }
        )
    assert annotation["annotationId"]


@pytest.mark.asyncio
async def test_alert_rule_command_and_grouping_are_deterministic() -> None:
    repository = MemoryOperationsRepository()
    query_id = str(uuid4())
    request = {
        "name": "latency",
        "severity": "error",
        "condition": {
            "signal": "latency",
            "query": {"queryId": query_id, "queryVersion": "1.0.0"},
            "operator": "greater_than",
            "threshold": 10,
            "unit": "milliseconds",
            "window": {"kind": "last_hour", "from": None, "to": None},
            "minimumSampleSize": 1,
            "missingEvidencePolicy": "inconclusive",
            "staleAfterSeconds": 300,
            "groupBy": [],
        },
        "cadence": "five_minutes",
        "state": "enabled",
        "createdAt": NOW,
    }
    service = AlertService(repository)
    first = await service.create_rule(request, command_id="alert-command-0001")
    second = await service.create_rule(
        dict(request), command_id="alert-command-0001"
    )
    assert first == second
    with pytest.raises(RuntimeError, match="idempotency key was reused"):
        await service.create_rule(
            {**request, "name": "different"}, command_id="alert-command-0001"
        )
    with pytest.raises(AlertError):
        await service.create_rule(
            {**request, "condition": {**request["condition"], "groupBy": ["not-a-facet"]}}
        )


@pytest.mark.asyncio
async def test_alert_crud_metrics_do_not_increment_evaluation_batches() -> None:
    repository = MemoryOperationsRepository()
    metrics = ObservatoryMetrics()
    service = AlertService(repository, metrics=metrics)
    request = {
        "name": "latency",
        "severity": "error",
        "condition": {
            "signal": "latency",
            "query": {"queryId": str(uuid4()), "queryVersion": "1.0.0"},
            "operator": "greater_than",
            "threshold": 10,
            "unit": "milliseconds",
            "window": {"kind": "last_hour", "from": None, "to": None},
            "minimumSampleSize": 1,
            "missingEvidencePolicy": "inconclusive",
            "staleAfterSeconds": 300,
            "groupBy": [],
        },
        "cadence": "five_minutes",
        "state": "enabled",
        "createdAt": NOW,
    }
    rule = await service.create_rule(request)
    await service.list_rules()
    await service.set_rule_state(str(rule["ruleId"]), "1.0.0", "disabled")
    instance_id = str(uuid4())
    await repository.create_resource(
        "alert_instance",
        {
            "instanceId": instance_id,
            "instanceVersion": "1.0.0",
            "state": "firing",
            "rule": {
                "resourceType": "alert_rule",
                "resourceId": rule["ruleId"],
                "resourceVersion": "1.0.0",
            },
            "groupingId": str(uuid4()),
            "groupingFacets": [],
            "severity": "error",
            "evidenceState": "complete",
            "observedValue": 11,
            "unit": "milliseconds",
            "sampleSize": 1,
            "evidence": [],
            "firstObservedAt": NOW,
            "lastObservedAt": NOW,
            "occurrenceCount": 1,
            "suppressed": False,
            "acknowledged": False,
        },
    )
    await service.acknowledge(
        instance_id,
        {"instanceVersion": "1.0.0", "reason": "investigating", "acknowledgedAt": NOW},
    )
    suppression = await service.create_suppression(
        {
            "scope": "rule",
            "target": {
                "resourceType": "alert_rule",
                "resourceId": rule["ruleId"],
                "resourceVersion": "1.0.0",
            },
            "startsAt": NOW,
            "endsAt": "2026-10-21T11:00:00Z",
            "reason": "testing",
        }
    )
    await service.clear_suppression(str(suppression["suppressionId"]))
    assert not any(
        name == "observatory_alert_evaluation_count"
        for (name, _labels) in metrics.values
    )


@pytest.mark.asyncio
async def test_operations_event_replay_uses_persisted_cursor() -> None:
    repository = MemoryOperationsRepository()
    value = await SavedQueryService(repository).create(
        {"name": "Runs", "definition": _query(), "createdAt": NOW}
    )
    events = await repository.events_after(None)
    assert events and events[0]["resourceId"] == value["queryId"]
    assert await repository.events_after(events[0]["eventId"]) == []


@pytest.mark.asyncio
async def test_memory_saved_query_cursor_is_snapshot_stable_across_mutations() -> None:
    repository = MemoryOperationsRepository()
    service = SavedQueryService(repository)
    base = datetime.now(UTC).replace(microsecond=0)

    def timestamp(seconds: int) -> str:
        return (base + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")

    created = []
    for index in range(1, 4):
        created.append(
            await service.create(
                {
                    "name": f"Runs {index}",
                    "definition": _query(),
                    "createdAt": timestamp(index - 4),
                }
            )
        )

    first = await service.list(limit=1)
    assert len(first["items"]) == 1
    cursor = first["nextCursor"]
    snapshot = first["snapshotAt"]
    assert isinstance(cursor, str)
    assert isinstance(snapshot, str)
    assert snapshot >= str(created[2]["createdAt"])

    inserted = dict(created[0])
    inserted["queryId"] = str(uuid4())
    inserted["name"] = "Inserted after snapshot"
    inserted["createdAt"] = timestamp(1)
    await repository.create_resource("saved_query", inserted)

    second = await service.list(cursor=cursor, limit=1)
    assert second["snapshotAt"] == snapshot
    assert [item["queryId"] for item in second["items"]] == [created[1]["queryId"]]
    assert second["nextCursor"] is not None
    third = await service.list(cursor=str(second["nextCursor"]), limit=1)
    assert [item["queryId"] for item in third["items"]] == [created[2]["queryId"]]
    assert third["snapshotAt"] == snapshot
    assert third["nextCursor"] is None

    version_repository = MemoryOperationsRepository()
    version_service = SavedQueryService(version_repository)
    versioned_created = [
        await version_service.create(
            {
                "name": f"Versioned {index}",
                "definition": _query(),
                "createdAt": timestamp(index - 4),
            }
        )
        for index in range(1, 4)
    ]
    version_first = await version_service.list(limit=1)
    versioned = dict(versioned_created[1])
    versioned["queryVersion"] = "2.0.0"
    await version_repository.create_resource("saved_query", versioned)
    with pytest.raises(RuntimeError, match="resource snapshot is no longer available"):
        await version_service.list(cursor=str(version_first["nextCursor"]), limit=1)

    delete_repository = MemoryOperationsRepository()
    delete_service = SavedQueryService(delete_repository)
    deleted_created = [
        await delete_service.create(
            {
                "name": f"Deleted {index}",
                "definition": _query(),
                "createdAt": timestamp(index - 4),
            }
        )
        for index in range(1, 4)
    ]
    delete_first = await delete_service.list(limit=1)
    await delete_repository.delete_resource(
        "saved_query",
        str(deleted_created[2]["queryId"]),
        str(deleted_created[2]["queryVersion"]),
    )
    with pytest.raises(RuntimeError, match="resource snapshot is no longer available"):
        await delete_service.list(cursor=str(delete_first["nextCursor"]), limit=1)


@pytest.mark.asyncio
async def test_composite_annotation_reference_checks_every_target_kind() -> None:
    class ExactProvider:
        def __init__(self, resource_id: str) -> None:
            self.resource_id = resource_id
            self.calls: list[tuple[str, str]] = []

        async def get(self, resource_id: str, resource_version: str) -> dict[str, object] | None:
            self.calls.append((resource_id, resource_version))
            if resource_id != self.resource_id or resource_version != "2.0.0":
                return None
            return {"resourceId": resource_id, "resourceVersion": resource_version}

    class EvaluationProvider:
        def __init__(self, resource_id: str) -> None:
            self.resource_id = resource_id

        async def get(
            self, _resource_type: str, resource_id: str, resource_version: str
        ) -> dict[str, object] | None:
            if resource_id != self.resource_id or resource_version != "2.0.0":
                return None
            return {"resourceId": resource_id, "resourceVersion": resource_version}

    providers = {kind: ExactProvider(str(uuid4())) for kind in ("run", "deployment", "incident")}
    evaluation = EvaluationProvider(str(uuid4()))
    reference = CompositeAnnotationTargetReferencePort(
        run_provider=providers["run"],
        deployment_provider=providers["deployment"],
        experiment_provider=evaluation,
        regression_provider=evaluation,
        incident_provider=providers["incident"],
    )

    for target_type, provider in (
        ("run", providers["run"]),
        ("deployment", providers["deployment"]),
        ("experiment", evaluation),
        ("regression", evaluation),
        ("incident", providers["incident"]),
    ):
        await reference.validate_annotation_target(
            {
                "targetType": target_type,
                "targetId": provider.resource_id,
                "targetVersion": "2.0.0",
            }
        )

    with pytest.raises(KeyError):
        await reference.validate_annotation_target(
            {
                "targetType": "run",
                "targetId": providers["run"].resource_id,
                "targetVersion": "1.0.0",
            }
        )


@pytest.mark.asyncio
async def test_composite_annotation_reference_reports_unavailable_provider() -> None:
    class UnavailableProvider:
        async def get(self, _resource_id: str, _resource_version: str) -> None:
            raise RuntimeError("provider unavailable")

    reference = CompositeAnnotationTargetReferencePort(run_provider=UnavailableProvider())
    with pytest.raises(RuntimeError, match="provider unavailable"):
        await reference.validate_annotation_target(
            {"targetType": "run", "targetId": str(uuid4()), "targetVersion": "1.0.0"}
        )

    reference = CompositeAnnotationTargetReferencePort()
    with pytest.raises(RuntimeError, match="provider unavailable"):
        await reference.validate_annotation_target(
            {
                "targetType": "deployment",
                "targetId": str(uuid4()),
                "targetVersion": "1.0.0",
            }
        )


@pytest.mark.asyncio
async def test_operation_spans_classify_storage_and_public_provider_failures() -> None:
    class BrokenRepository:
        async def create_resource(self, *_args: object, **_kwargs: object) -> dict[str, object]:
            raise ConnectionError("postgres unavailable")

    metrics = ObservatoryMetrics()
    panel = {
        "panelId": str(uuid4()),
        "title": "Runs",
        "query": {"queryId": str(uuid4()), "queryVersion": "1.0.0"},
        "visualization": "table",
        "size": "medium",
        "position": 0,
    }
    with pytest.raises(ConnectionError):
        await DashboardService(BrokenRepository(), metrics=metrics).create(
            {"name": "Runs", "description": "", "panels": [panel], "createdAt": NOW}
        )
    storage_span = metrics.component_spans[-1]
    assert (
        storage_span.operation,
        storage_span.outcome,
        storage_span.dependency,
        storage_span.error_code,
    ) == (
        "write",
        "failure",
        "postgresql",
        "transport_unavailable",
    )
    assert storage_span.duration_ms > 0

    class UnavailableReference:
        async def validate_annotation_target(self, _target: object) -> None:
            raise RuntimeError("reference unavailable")

    metrics = ObservatoryMetrics()
    with pytest.raises(AnnotationTargetUnavailable):
        await AnnotationService(
            MemoryOperationsRepository(), reference_port=UnavailableReference(), metrics=metrics
        ).create(
            {
                "target": {"targetType": "run", "targetId": str(uuid4()), "targetVersion": "1.0.0"},
                "text": "note",
                "createdAt": NOW,
            }
        )
    provider_span = metrics.component_spans[-1]
    assert (
        provider_span.operation,
        provider_span.outcome,
        provider_span.dependency,
        provider_span.error_code,
    ) == (
        "validate",
        "unavailable",
        None,
        "provider_unavailable",
    )
    assert provider_span.duration_ms > 0

    metrics = ObservatoryMetrics()
    with pytest.raises(AlertError):
        await AlertService(MemoryOperationsRepository(), metrics=metrics).create_rule(
            {"name": "bad"}
        )
    validation_span = metrics.component_spans[-1]
    assert (
        validation_span.operation,
        validation_span.outcome,
        validation_span.dependency,
        validation_span.error_code,
    ) == (
        "validate",
        "failure",
        None,
        "validation",
    )
    assert validation_span.duration_ms > 0
