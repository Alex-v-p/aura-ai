"""Acceptance checks for AURA-0141 governed research, artifacts, and tasks."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).parents[2]
OPENAPI = ROOT / "contracts/openapi/aura-v1.yaml"
RUN_EVENTS = ROOT / "contracts/events/runs/v1/run-events.schema.json"
RESEARCH_EVENTS = ROOT / "contracts/events/knowledge/v1/research-events.schema.json"
ARTIFACT_EVENTS = ROOT / "contracts/events/artifacts/v1/generated-artifact-events.schema.json"
TASK_EVENTS = ROOT / "contracts/events/execution/v1/task-events.schema.json"
GENERATED = ROOT / "frontend/libs/platform/aura-api-client/src/lib/generated/aura-api.ts"


def _json(path: Path) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(path.read_text()))


def _openapi() -> dict[str, Any]:
    return _json(OPENAPI)


def _refs(operation: dict[str, Any]) -> set[str]:
    return {
        parameter["$ref"]
        for parameter in operation.get("parameters", [])
        if "$ref" in parameter
    }


def _schema_accepts(name: str, instance: Any, *, document: dict[str, Any] | None = None) -> bool:
    """Exercise the closed subset of JSON Schema used by AURA-0141 fixtures."""
    document = document or _openapi()
    definitions = document.get("$defs", document.get("components", {}).get("schemas", {}))

    def validate(schema: dict[str, Any], value: Any) -> bool:
        if "$ref" in schema:
            return validate(definitions[schema["$ref"].rsplit("/", 1)[1]], value)
        if "oneOf" in schema:
            return sum(validate(option, value) for option in schema["oneOf"]) == 1
        if "allOf" in schema and not all(validate(option, value) for option in schema["allOf"]):
            return False
        if "const" in schema and value != schema["const"]:
            return False
        if "enum" in schema and value not in schema["enum"]:
            return False
        expected = schema.get("type")
        if isinstance(expected, list):
            return any(validate({**schema, "type": item}, value) for item in expected)
        if expected == "null":
            return value is None
        if expected == "object":
            if not isinstance(value, dict):
                return False
            properties = schema.get("properties", {})
            if any(field not in value for field in schema.get("required", [])):
                return False
            if schema.get("additionalProperties") is False and set(value) - set(properties):
                return False
            return all(field not in value or validate(properties[field], value[field]) for field in properties)
        if expected == "array":
            if not isinstance(value, list):
                return False
            if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", len(value)):
                return False
            if schema.get("uniqueItems") and len({json.dumps(item, sort_keys=True) for item in value}) != len(value):
                return False
            return all(validate(schema["items"], item) for item in value)
        if expected in {"integer", "number"}:
            if isinstance(value, bool) or not isinstance(value, int | float):
                return False
            if expected == "integer" and not isinstance(value, int):
                return False
            return value >= schema.get("minimum", value) and value <= schema.get("maximum", value)
        if expected == "boolean":
            return isinstance(value, bool)
        if expected == "string":
            if not isinstance(value, str):
                return False
            if len(value) < schema.get("minLength", 0) or len(value) > schema.get("maxLength", len(value)):
                return False
            return "pattern" not in schema or re.match(schema["pattern"], value) is not None
        return True

    return validate(definitions[name], instance)


UUID1 = "11111111-1111-4111-8111-111111111111"
UUID2 = "22222222-2222-4222-8222-222222222222"
NOW = "2026-10-28T10:00:00Z"


def _research_base(content_state: str = "active") -> dict[str, Any]:
    return {
        "id": UUID1,
        "taskRef": {"taskId": UUID2},
        "mode": "quick",
        "stage": "queued",
        "outcome": None,
        "contentState": content_state,
        "version": 1,
        "capabilities": {
            "canCancel": True,
            "canExtend": True,
            "canDelete": False,
            "canRetryCleanup": False,
            "canReconcileCleanup": False,
        },
        "createdAt": NOW,
        "updatedAt": NOW,
        "finishedAt": None,
    }


def _active_artifact() -> dict[str, Any]:
    return {
        "id": UUID1,
        "kind": "research_report",
        "title": "Governed report",
        "mediaType": "text/markdown; charset=utf-8",
        "contentState": "active",
        "currentRevision": 1,
        "version": 1,
        "capabilities": {"canPreview": True, "canDownload": True, "canDelete": True, "canRetryCleanup": False, "canReconcileCleanup": False},
        "createdAt": NOW,
        "updatedAt": NOW,
        "lineage": {"taskId": UUID2, "runId": None, "conversationId": None, "workspaceId": None},
        "revision": {"revision": 1, "mediaType": "text/markdown; charset=utf-8", "sha256": "a" * 64, "byteCount": 42, "creatorComponent": "aura.research@1", "previousRevision": None, "createdAt": NOW},
    }


def test_additive_resources_and_generated_operations_are_present() -> None:
    document = _openapi()
    assert document["openapi"] == "3.1.0"
    assert document["info"]["version"] == "1.22.0"
    expected = {
        "/api/v1/research/settings",
        "/api/v1/research",
        "/api/v1/research/{research_id}",
        "/api/v1/research/{research_id}/evidence",
        "/api/v1/research/{research_id}/cancel",
        "/api/v1/research/{research_id}/extensions",
        "/api/v1/research/{research_id}/deletions",
        "/api/v1/research/{research_id}/deletions/retry",
        "/api/v1/research/{research_id}/deletions/reconcile",
        "/api/v1/artifacts",
        "/api/v1/artifacts/{artifact_id}",
        "/api/v1/artifacts/{artifact_id}/revisions",
        "/api/v1/artifacts/{artifact_id}/preview",
        "/api/v1/artifacts/{artifact_id}/content",
        "/api/v1/artifacts/{artifact_id}/deletions",
        "/api/v1/artifacts/{artifact_id}/deletions/retry",
        "/api/v1/artifacts/{artifact_id}/deletions/reconcile",
        "/api/v1/tasks",
        "/api/v1/tasks/{task_id}",
        "/api/v1/tasks/{task_id}/events",
        "/api/v1/tasks/{task_id}/cancel",
        "/api/v1/tasks/{task_id}/retry",
    }
    assert expected <= set(document["paths"])
    generated = GENERATED.read_text()
    for operation in (
        "createResearchTask",
        "extendResearchTask",
        "deleteResearchContent",
        "downloadGeneratedArtifact",
        "streamDurableTaskEvents",
        "cancelDurableTask",
        "retryDurableTask",
    ):
        assert f"readonly {operation}:" in generated
        assert f"  {operation}:" in generated


def test_every_added_mutation_is_csrf_idempotent_and_body_bounded() -> None:
    prefixes = ("/api/v1/research", "/api/v1/artifacts", "/api/v1/tasks")
    for path, path_item in _openapi()["paths"].items():
        if not path.startswith(prefixes):
            continue
        for method, operation in path_item.items():
            if method not in {"post", "put", "patch", "delete"}:
                continue
            assert {
                "#/components/parameters/CsrfToken",
                "#/components/parameters/IdempotencyKey",
            } <= _refs(operation), (path, method)
            assert operation["requestBody"]["required"] is True


def test_modes_are_bounded_and_agent_led_is_not_unbounded() -> None:
    schemas = _openapi()["components"]["schemas"]
    assert schemas["ResearchMode"]["enum"] == ["quick", "auto", "deep", "agent_led"]
    budget = schemas["ResearchBudget"]
    assert budget["additionalProperties"] is False
    maximums = {
        "wallTimeSeconds": 3600,
        "fetchedSources": 60,
        "searchRequests": 20,
        "browserPages": 15,
        "modelCalls": 32,
    }
    assert {name: spec["maximum"] for name, spec in budget["properties"].items()} == maximums
    assert schemas["ResearchModeEnvelope"]["additionalProperties"] is False
    assert schemas["ExtendResearchRequest"]["required"] == [
        "expectedVersion",
        "cumulativeBudget",
    ]
    consumption = schemas["ResearchBudgetConsumption"]
    assert all(spec["minimum"] == 0 for spec in consumption["properties"].values())
    zero = {name: 0 for name in consumption["required"]}
    assert _schema_accepts("ResearchBudgetConsumption", zero)
    assert not _schema_accepts("ResearchBudget", zero)
    assert _schema_accepts("ResearchBudgetConsumption", maximums)
    assert not _schema_accepts(
        "ResearchBudgetConsumption", {**maximums, "modelCalls": 33}
    )


def test_research_admission_and_tool_result_cannot_carry_results() -> None:
    schemas = _openapi()["components"]["schemas"]
    for name in ("ResearchAdmission", "ResearchToolAdmissionResult", "ResearchActivity"):
        schema = schemas[name]
        assert schema["additionalProperties"] is False
        fields = set(schema["properties"])
        assert not fields & {
            "question",
            "query",
            "result",
            "report",
            "sources",
            "evidence",
            "excerpt",
            "url",
            "citations",
            "provider",
        }
    assert set(schemas["ResearchToolAdmissionResult"]["required"]) == {
        "taskRef",
        "mode",
        "admissionState",
    }


def test_private_research_input_is_body_only_and_web_citations_are_validated_shape() -> None:
    document = _openapi()
    create = document["paths"]["/api/v1/research"]["post"]
    assert create["requestBody"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/CreateResearchRequest"
    }
    for path, path_item in document["paths"].items():
        if not path.startswith("/api/v1/research"):
            continue
        for operation in path_item.values():
            if not isinstance(operation, dict):
                continue
            query_names = {
                item.get("name")
                for item in operation.get("parameters", [])
                if item.get("in") == "query"
            }
            assert not query_names & {"question", "query", "seedUrl", "seedUrls", "domain", "domains"}
    citation = document["components"]["schemas"]["WebCitation"]
    assert citation["additionalProperties"] is False
    assert citation["properties"]["url"]["pattern"] == "^https://"
    assert not set(citation["properties"]) & {
        "query",
        "excerpt",
        "objectKey",
        "providerPayload",
        "browserState",
        "evidenceId",
    }


def test_deletion_truth_and_non_cascading_artifacts_are_explicit() -> None:
    schemas = _openapi()["components"]["schemas"]
    assert schemas["ContentDeletionState"]["enum"] == [
        "active",
        "deletion_pending",
        "deletion_failed",
        "cleanup_unknown",
        "deleted",
    ]
    deletion = schemas["DeleteResearchRequest"]
    assert deletion["additionalProperties"] is False
    assert set(deletion["required"]) == {"expectedVersion", "deleteLinkedArtifacts"}
    assert deletion["properties"]["deleteLinkedArtifacts"]["default"] is False
    tombstone = schemas["DeletedResearchTombstone"]
    tombstone_properties = tombstone["properties"]
    for private in ("question", "url", "excerpt", "citation", "artifactRef"):
        assert private not in tombstone_properties
    deleted_citation = schemas["DeletedResearchCitation"]
    assert set(deleted_citation["properties"]) == {"kind", "sourceId", "label"}


def test_artifact_contract_has_only_closed_text_types_and_no_public_write() -> None:
    document = _openapi()
    schemas = document["components"]["schemas"]
    assert schemas["GeneratedArtifactMediaType"]["enum"] == [
        "text/markdown; charset=utf-8",
        "text/plain; charset=utf-8",
        "application/json",
    ]
    assert set(document["paths"]["/api/v1/artifacts"]) == {"get"}
    assert set(document["paths"]["/api/v1/artifacts/{artifact_id}/revisions"]) == {"get"}
    revision = schemas["GeneratedArtifactRevision"]
    assert revision["additionalProperties"] is False
    assert revision["properties"]["sha256"]["pattern"] == "^[0-9a-f]{64}$"
    assert "object" not in {name.lower() for name in revision["properties"]}


def test_task_center_preserves_source_authority_and_closed_statuses() -> None:
    schemas = _openapi()["components"]["schemas"]
    assert schemas["TaskSourceKind"]["enum"] == [
        "research",
        "workflow_instance",
        "briefing_occurrence",
        "document_processing",
        "memory_reindex",
    ]
    assert schemas["TaskNormalizedStatus"]["enum"] == [
        "queued",
        "running",
        "waiting",
        "completed",
        "failed",
        "canceled",
        "unknown",
    ]
    summary = schemas["DurableTaskSummary"]
    assert {"sourceStatus", "sourceOutcome", "sourceVersion", "capabilities"} <= set(
        summary["required"]
    )
    assert schemas["TaskCommandReceipt"]["properties"]["state"]["enum"] == [
        "accepted",
        "rejected",
        "unknown",
    ]


def test_research_detail_active_partial_and_all_deletion_states_validate() -> None:
    active = {
        **_research_base(),
        "question": "What changed?",
        "admittedBudget": {"wallTimeSeconds": 90, "fetchedSources": 6, "searchRequests": 2, "browserPages": 1, "modelCalls": 4},
        "consumedBudget": {"wallTimeSeconds": 0, "fetchedSources": 0, "searchRequests": 0, "browserPages": 0, "modelCalls": 0},
        "extensionAccepted": False,
        "sourceCount": 0,
        "failureClass": None,
        "artifactRefs": [],
        "citations": [],
    }
    assert _schema_accepts("ResearchDetail", active)
    assert not _schema_accepts("ResearchDetail", {key: value for key, value in active.items() if key != "citations"})
    assert not _schema_accepts("ResearchDetail", {**active, "unexpected": "rejected"})

    partial = {
        **active,
        "stage": "terminal",
        "outcome": "partial",
        "finishedAt": NOW,
        "sourceCount": 1,
        "consumedBudget": {"wallTimeSeconds": 45, "fetchedSources": 1, "searchRequests": 1, "browserPages": 0, "modelCalls": 2},
        "citations": [{"kind": "web", "sourceId": UUID2, "label": "Source", "url": "https://example.com/report", "retrievedAt": NOW, "locator": None}],
    }
    assert _schema_accepts("ResearchDetail", partial)

    for state in ("deletion_pending", "deletion_failed", "cleanup_unknown", "deleted"):
        tombstone = {**_research_base(state), "stage": "terminal", "outcome": "partial", "finishedAt": NOW, "tombstoneLabel": "Research source deleted"}
        assert _schema_accepts("ResearchDetail", tombstone), state
        assert not _schema_accepts("ResearchDetail", {**tombstone, "question": "must remain unreadable"}), state
    assert not _schema_accepts("DeletedResearchTombstone", {**_research_base("active"), "tombstoneLabel": "Research source deleted"})


def test_artifact_active_and_all_tombstone_states_are_discriminated() -> None:
    active = _active_artifact()
    assert _schema_accepts("GeneratedArtifactDetail", active)
    assert not _schema_accepts("GeneratedArtifactDetail", {**active, "unexpected": True})

    for state in ("deletion_pending", "deletion_failed", "cleanup_unknown", "deleted"):
        tombstone = {
            "id": UUID1,
            "kind": "research_report",
            "title": None,
            "mediaType": None,
            "contentState": state,
            "currentRevision": 1,
            "version": 2,
            "capabilities": {"canPreview": False, "canDownload": False, "canDelete": False, "canRetryCleanup": state == "deletion_failed", "canReconcileCleanup": state == "cleanup_unknown"},
            "createdAt": NOW,
            "updatedAt": NOW,
            "lineage": {"taskId": UUID2, "runId": None, "conversationId": None, "workspaceId": None},
            "integrity": {"revision": 1, "sha256": "a" * 64, "byteCount": 42, "createdAt": NOW},
            "deletionAuditId": UUID2,
        }
        assert _schema_accepts("GeneratedArtifactDetail", tombstone), state
        assert not _schema_accepts("GeneratedArtifactDetail", {**tombstone, "title": "leak"}), state
        assert not _schema_accepts("GeneratedArtifactDetail", {**tombstone, "capabilities": {**tombstone["capabilities"], "canPreview": True}}), state
        assert not _schema_accepts("GeneratedArtifactDetail", {key: value for key, value in tombstone.items() if key != "integrity"}), state


def test_durable_task_detail_is_one_closed_valid_shape() -> None:
    detail = {
        "id": UUID1,
        "sourceKind": "research",
        "sourceId": UUID2,
        "sourceVersion": 3,
        "status": "running",
        "sourceStatus": "fetching",
        "sourceOutcome": None,
        "stage": "fetching",
        "progress": 0.5,
        "capabilities": {"canCancel": True, "canRetry": False, "hasSourceDetail": True},
        "routeKey": "research_detail",
        "createdAt": NOW,
        "updatedAt": NOW,
        "displayLabel": "Research task",
        "failureClass": None,
        "recoveryAction": None,
        "artifactRefs": [],
    }
    assert _schema_accepts("DurableTaskDetail", detail)
    assert not _schema_accepts("DurableTaskDetail", {**detail, "privatePayload": "rejected"})


def test_task_projection_live_and_rebuild_boundary_is_closed_and_content_free() -> None:
    event_contract = _json(TASK_EVENTS)
    event_defs = event_contract["$defs"]
    internal_definitions = {
        "ExactTaskProjectionSnapshotQueryV1",
        "CursorTaskProjectionSnapshotQueryV1",
        "TaskProjectionSnapshotQueryV1",
        "TaskProjectionSnapshotV1",
        "TaskProjectionSnapshotPageV1",
        "TaskProjectionSnapshotResultV1",
    }
    for definition in (
        "TaskProjectionSnapshotQueryV1",
        "TaskProjectionSnapshotV1",
        "TaskProjectionSnapshotPageV1",
        "TaskProjectionSnapshotResultV1",
    ):
        assert definition in event_defs
    public_schemas = _openapi()["components"]["schemas"]
    assert internal_definitions.isdisjoint(public_schemas)
    public_task_schemas = {
        name: schema
        for name, schema in public_schemas.items()
        if name.startswith("Task") or name.startswith("DurableTask")
    }
    assert "ownerScopeId" not in json.dumps(public_task_schemas)
    assert "task_projection:read" not in json.dumps(_openapi())
    generated = GENERATED.read_text()
    assert "TaskProjectionSnapshot" not in generated
    assert "task_projection:read" not in generated

    exact = {"schemaVersion": 1, "mode": "exact", "sourceKind": "research", "sourceId": UUID1, "minimumSourceVersion": 4}
    cursor = {"schemaVersion": 1, "mode": "cursor", "sourceKind": "research", "cursor": None, "pageSize": 100}
    assert _schema_accepts("TaskProjectionSnapshotQueryV1", exact, document=event_contract)
    assert _schema_accepts("TaskProjectionSnapshotQueryV1", cursor, document=event_contract)
    assert not _schema_accepts("TaskProjectionSnapshotQueryV1", {**exact, "cursor": None, "pageSize": 100}, document=event_contract)
    assert not _schema_accepts("TaskProjectionSnapshotQueryV1", {**cursor, "sourceId": UUID1}, document=event_contract)

    snapshot = {
        "schemaVersion": 1,
        "ownerScopeId": UUID1,
        "sourceKind": "research",
        "sourceId": UUID2,
        "sourceVersion": 4,
        "status": "running",
        "sourceStatus": "fetching",
        "sourceOutcome": None,
        "stage": "fetching",
        "progress": 0.5,
        "capabilities": {"canCancel": True, "canRetry": False, "hasSourceDetail": True},
        "routeKey": "research_detail",
        "failureClass": None,
        "createdAt": NOW,
        "updatedAt": NOW,
        "finishedAt": None,
        "sourceDeleted": False,
    }
    assert _schema_accepts("TaskProjectionSnapshotV1", snapshot, document=event_contract)
    assert _schema_accepts("TaskProjectionSnapshotResultV1", snapshot, document=event_contract)
    assert _schema_accepts("TaskProjectionSnapshotResultV1", {"schemaVersion": 1, "items": [snapshot], "nextCursor": "next", "scanComplete": False}, document=event_contract)
    for private in ("title", "question", "report", "excerpt", "url", "providerPayload", "ormRow"):
        assert not _schema_accepts("TaskProjectionSnapshotV1", {**snapshot, private: "forbidden"}, document=event_contract)


def test_task_invalidation_payload_is_exactly_metadata_only() -> None:
    event = _json(TASK_EVENTS)["$defs"]["TaskSourceInvalidated"]
    expected = {
        "schemaVersion",
        "eventId",
        "eventType",
        "sourceKind",
        "sourceId",
        "sourceVersion",
        "occurredAt",
        "correlationId",
        "causationId",
    }
    assert event["additionalProperties"] is False
    assert set(event["required"]) == expected
    assert set(event["properties"]) == expected
    forbidden = {
        "ownerId",
        "status",
        "outcome",
        "progress",
        "stage",
        "capabilities",
        "title",
        "url",
        "digest",
        "objectReference",
        "content",
    }
    assert forbidden.isdisjoint(event["properties"])


def test_lifecycle_events_are_closed_versioned_and_private() -> None:
    contracts = {
        RESEARCH_EVENTS: "ResearchLifecycleEvent",
        ARTIFACT_EVENTS: "GeneratedArtifactLifecycleEvent",
        TASK_EVENTS: "TaskSourceInvalidated",
    }
    forbidden = {
        "ownerId",
        "question",
        "query",
        "url",
        "label",
        "excerpt",
        "title",
        "digest",
        "objectKey",
        "providerPayload",
        "credentials",
        "content",
    }
    for path, definition in contracts.items():
        event = _json(path)["$defs"][definition]
        assert event["additionalProperties"] is False
        assert event["properties"]["schemaVersion"] == {"const": 1}
        assert forbidden.isdisjoint(event["properties"])
        assert {"eventId", "occurredAt", "correlationId", "causationId"} <= set(
            event["required"]
        )


def test_run_and_message_additions_remain_optional_and_event_contract_matches() -> None:
    schemas = _openapi()["components"]["schemas"]
    run = schemas["Run"]
    message = schemas["Message"]
    assert not {"taskRefs", "artifactRefs", "researchOrigin"} & set(run["required"])
    assert not {"taskRefs", "artifactRefs", "researchActivity"} & set(message["required"])
    baseline_result = subprocess.run(
        ["git", "show", "origin/dev:contracts/openapi/aura-v1.yaml"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    baseline = cast(dict[str, Any], json.loads(baseline_result.stdout))["components"]["schemas"]
    assert run["properties"]["citations"] == baseline["Run"]["properties"]["citations"]
    assert message["properties"]["citations"] == baseline["Message"]["properties"]["citations"]
    assert run["properties"]["webCitations"]["items"] == {
        "$ref": "#/components/schemas/ResearchCitation"
    }
    assert message["properties"]["webCitations"]["items"] == {
        "$ref": "#/components/schemas/ResearchCitation"
    }
    event_defs = _json(RUN_EVENTS)["$defs"]
    for field in ("taskRefs", "artifactRefs", "researchOrigin", "webCitations"):
        assert field in event_defs["Run"]["properties"]
    for field in ("taskRefs", "artifactRefs", "researchActivity", "webCitations"):
        assert field in event_defs["Message"]["properties"]
    assert event_defs["ToolStatusData"]["properties"]["researchAdmission"] == {
        "$ref": "#/$defs/ResearchToolAdmissionResult",
        "description": "Present only for the admission-only web.research@1 result.",
    }


def test_contracts_exclude_provider_storage_executor_and_observatory_internals() -> None:
    selected = {
        name: schema
        for name, schema in _openapi()["components"]["schemas"].items()
        if name.startswith(("Research", "GeneratedArtifact", "DurableTask", "Task"))
        or name in {"WebCitation", "DeletedResearchCitation", "ResearchCitation"}
    }
    represented = {
        key.lower()
        for schema in selected.values()
        for key in schema.get("properties", {})
    }
    assert {
        "objectkey",
        "bucket",
        "providerpayload",
        "browserstate",
        "executorstate",
        "observatoryid",
        "credential",
        "token",
    }.isdisjoint(represented)
