"""Static document OpenAPI and lifecycle-event assertions."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest
import yaml
from aura_core.domains.knowledge.documents.evaluation import (
    COMPONENT_ID,
    COMPONENT_VERSION,
    DELETION_COMPONENT_ID,
    DOCUMENT_COMPONENT_VERSIONS,
    EVALUATOR_ALLOWED_COMPONENTS,
    EVALUATOR_COMPONENTS,
    EVALUATOR_VERSIONS,
    PROCESSING_COMPONENT_ID,
    RETRIEVAL_COMPONENT_ID,
    UPLOAD_COMPONENT_ID,
)
from aura_core.platform.telemetry import COMPONENT_VERSIONS

ROOT = Path(__file__).parents[3]


def _contract() -> dict[str, Any]:
    return cast(dict[str, Any], json.loads((ROOT / "contracts/openapi/aura-v1.yaml").read_text()))


def _schema_matches(schema: dict[str, Any], value: object, schemas: dict[str, Any]) -> bool:
    """Small contract-test validator for the bounded summary schema subset."""

    if "$ref" in schema:
        return _schema_matches(
            schemas[str(schema["$ref"]).rsplit("/", maxsplit=1)[-1]], value, schemas
        )
    if "oneOf" in schema:
        return sum(
            _schema_matches(branch, value, schemas)
            for branch in cast(list[dict[str, Any]], schema["oneOf"])
        ) == 1
    if "const" in schema and value != schema["const"]:
        return False
    if "enum" in schema and value not in schema["enum"]:
        return False
    schema_type = schema.get("type")
    if schema_type == "object":
        if not isinstance(value, dict):
            return False
        object_value = cast(dict[str, object], value)
        required = cast(list[str], schema.get("required", []))
        if any(key not in object_value for key in required):
            return False
        properties = cast(dict[str, dict[str, Any]], schema.get("properties", {}))
        if schema.get("additionalProperties") is False and set(object_value) - set(properties):
            return False
        return all(
            key not in properties or _schema_matches(properties[key], item, schemas)
            for key, item in object_value.items()
        )
    if schema_type == "string":
        return isinstance(value, str)
    if schema_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    return True


def test_message_attachment_summary_tombstone_one_of_is_content_free() -> None:
    contract = _contract()
    schemas = cast(dict[str, Any], contract["components"]["schemas"])
    summary = cast(dict[str, Any], schemas["MessageAttachmentSummary"])
    assert "oneOf" in summary
    variants = cast(list[dict[str, Any]], summary["oneOf"])
    assert len(variants) == 2
    refs = {str(variant["$ref"]) for variant in variants}
    assert refs == {
        "#/components/schemas/AvailableMessageAttachmentSummary",
        "#/components/schemas/UnavailableMessageAttachmentSummary",
    }
    assert all(
        schemas[ref.rsplit("/", maxsplit=1)[-1]]["additionalProperties"] is False
        for ref in refs
    )

    available = {
        "linkId": "00000000-0000-0000-0000-000000000001",
        "kind": "image",
        "sourceId": "00000000-0000-0000-0000-000000000002",
        "displayFilename": "photo.png",
        "mediaType": "image/png",
        "byteCount": 128,
        "availability": "available",
        "width": 16,
        "height": 16,
    }
    unavailable = {
        "linkId": "00000000-0000-0000-0000-000000000003",
        "kind": "image",
        "sourceId": "00000000-0000-0000-0000-000000000004",
        "displayFilename": "Attachment unavailable",
        "mediaType": "application/octet-stream",
        "byteCount": 0,
        "availability": "unavailable",
    }
    assert _schema_matches(summary, available, schemas)
    assert _schema_matches(summary, unavailable, schemas)

    for invalid in (
        {**unavailable, "width": 16, "height": 16},
        {**unavailable, "mediaType": "image/png"},
        {**unavailable, "byteCount": 1},
    ):
        with pytest.raises(AssertionError):
            assert _schema_matches(summary, invalid, schemas)


def test_document_api_uses_canonical_streaming_paths_and_backward_compatible_schemas() -> None:
    contract = _contract()
    assert contract["info"]["version"] == "1.22.0"
    paths = cast(dict[str, dict[str, Any]], contract["paths"])
    expected = {
        "/api/v1/documents/uploads",
        "/api/v1/documents/uploads/{upload_id}/content",
        "/api/v1/documents",
        "/api/v1/documents/search",
        "/api/v1/documents/{document_id}",
        "/api/v1/documents/{document_id}/content",
        "/api/v1/documents/{document_id}/preview",
        "/api/v1/documents/{document_id}/retry",
    }
    assert expected <= paths.keys()

    create = paths["/api/v1/documents/uploads"]["post"]
    upload = paths["/api/v1/documents/uploads/{upload_id}/content"]["put"]
    for operation in (create, upload, paths["/api/v1/documents/{document_id}/retry"]["post"]):
        refs = {parameter.get("$ref") for parameter in operation["parameters"]}
        assert "#/components/parameters/CsrfToken" in refs
        assert "#/components/parameters/IdempotencyKey" in refs
    assert any(parameter.get("name") == "Content-Length" for parameter in upload["parameters"])
    assert any(parameter.get("name") == "Content-Type" for parameter in upload["parameters"])
    body_schema = upload["requestBody"]["content"]["application/octet-stream"]["schema"]
    assert body_schema["maxLength"] == 100_000_000
    upload_parameters = {parameter.get("name"): parameter for parameter in upload["parameters"]}
    assert upload_parameters["Content-Length"]["schema"]["minimum"] == 1
    assert upload_parameters["Content-Length"]["schema"]["maximum"] == 100_000_000
    assert "#/components/parameters/ExpectedVersion" in {
        parameter.get("$ref") for parameter in upload["parameters"]
    }

    schemas = cast(dict[str, dict[str, Any]], contract["components"]["schemas"])
    assert schemas["DocumentStatus"]["enum"] == [
        "uploading",
        "queued",
        "processing",
        "ready",
        "failed",
        "deleting",
    ]
    assert schemas["DocumentMediaType"]["enum"] == [
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "text/markdown",
        "text/plain",
    ]
    create_properties = schemas["CreateDocumentUploadRequest"]["properties"]
    assert create_properties["byteCount"]["minimum"] == 1
    assert create_properties["byteCount"]["maximum"] == 100_000_000
    search_properties = schemas["SearchDocumentsRequest"]["properties"]
    assert search_properties["limit"]["default"] == 8
    assert search_properties["limit"]["maximum"] == 20
    assert schemas["DocumentSummary"]["properties"]["byteCount"]["maximum"] == 100_000_000
    assert (
        schemas["DocumentDetail"]["properties"]["extractedTextLength"]["maximum"]
        == 10 * 1024 * 1024
    )
    preview = paths["/api/v1/documents/{document_id}/preview"]["get"]
    assert preview["operationId"] == "getDocumentPreview"
    assert preview["responses"]["200"]["headers"]["Cache-Control"] == {
        "required": True,
        "schema": {"type": "string", "const": "no-store"},
    }
    assert "409" in preview["responses"]
    preview_schema = schemas["DocumentPreview"]
    assert preview_schema["additionalProperties"] is False
    assert preview_schema["properties"]["text"]["maxLength"] == 20_000
    assert preview_schema["properties"]["totalCharacters"]["maximum"] == 10 * 1024 * 1024
    assert {"documentId", "text", "truncated", "totalCharacters"} == set(
        preview_schema["required"]
    )
    for name in (
        "DocumentSummary",
        "DocumentDetail",
        "DocumentUploadSession",
        "DocumentSearchResult",
    ):
        assert not {"objectKey", "bucket", "credential", "embedding", "vector"} & set(
            schemas[name]["properties"]
        )

    citation = schemas["DocumentCitation"]
    assert citation["additionalProperties"] is False
    assert set(citation["required"]) == {"documentId", "displayLabel", "locator"}
    assert citation["properties"]["displayLabel"]["maxLength"] == 120
    assert citation["properties"]["locator"]["maxLength"] == 255
    assert not {
        "evidenceId",
        "excerpt",
        "filename",
        "objectKey",
        "url",
    } & set(citation["properties"])
    assert "citations" in schemas["Message"]["properties"]
    assert "citations" in schemas["Run"]["properties"]


def test_document_lifecycle_event_contract_is_metadata_only_and_versioned() -> None:
    event_path = ROOT / "contracts/events/knowledge/v1/document-events.schema.json"
    contract = cast(dict[str, Any], json.loads(event_path.read_text()))
    assert contract["$id"].endswith("/knowledge/v1/document-events.schema.json")
    event_definition = contract["$defs"]["DocumentEvent"]
    assert event_definition["properties"]["schemaVersion"]["const"] == 1
    properties = set(event_definition["properties"])
    assert not {
        "filename",
        "content",
        "extractedText",
        "chunk",
        "digest",
        "hash",
        "embedding",
        "vector",
        "objectKey",
        "owner",
        "credential",
    } & properties
    assert event_definition["additionalProperties"] is False


def test_generated_client_exposes_the_same_document_operations_and_headers() -> None:
    generated = (
        ROOT / "frontend/libs/platform/aura-api-client/src/lib/generated/aura-api.ts"
    ).read_text()
    for operation in (
        "createDocumentUpload",
        "uploadDocumentContent",
        "listDocuments",
        "searchDocuments",
        "getDocument",
        "downloadDocument",
        "getDocumentPreview",
        "retryDocumentProcessing",
        "deleteDocument",
    ):
        assert operation in generated
    assert '"X-Expected-Version": string' in generated
    assert '"Content-Length": string' in generated
    assert "DocumentUploadReceipt" in generated


def test_document_component_manifests_allow_only_metadata_observation() -> None:
    required_metrics = {
        "document_upload_duration_ms",
        "document_upload_outcome",
        "document_processing_duration_ms",
        "document_processing_outcome",
        "document_chunk_count",
        "document_retrieval_duration_ms",
        "document_retrieval_candidate_count",
        "document_deletion_outcome",
        "document_deletion_duration_ms",
        "document_retry_count",
    }
    sensitive = {
        "content",
        "filename",
        "excerpt",
        "digest",
        "object_key",
        "owner",
        "credential",
        "vector",
    }
    manifest_dir = ROOT / "services/aura-core/resources/component-manifests"
    found_metrics: set[str] = set()
    for path in manifest_dir.glob("document-*.yaml"):
        manifest = cast(dict[str, Any], yaml.safe_load(path.read_text()))
        found_metrics.update(cast(list[str], manifest.get("metrics", [])))
        assert manifest.get("capture_policy") == "metadata_only"
        cardinality = cast(dict[str, Any], manifest.get("cardinality", {}))
        manifest_prohibited = {str(item).casefold() for item in cardinality.get("prohibited", [])}
        assert sensitive & manifest_prohibited
        assert set(cardinality.get("trace_only", [])) <= {
            "trace_id",
            "span_id",
            "document_id",
            "evidence_id",
            "job_id",
        }
        if manifest.get("component", {}).get("id") == "aura.knowledge.document_retrieval":
            operations = {
                item["id"]: item for item in cast(list[dict[str, str]], manifest["operations"])
            }
            assert operations == {
                "document.search": {
                    "id": "document.search",
                    "input_schema": "document-search-v1",
                    "output_schema": "document-search-result-v1",
                },
                "document.preview": {
                    "id": "document.preview",
                    "input_schema": "document-preview-request-v1",
                    "output_schema": "document-preview-v1",
                },
                "document.detail": {
                    "id": "document.detail",
                    "input_schema": "document-detail-request-v1",
                    "output_schema": "document-detail-v1",
                },
            }
    assert required_metrics <= found_metrics


def test_document_retrieval_manifest_matches_runtime_and_evaluator_registries() -> None:
    manifest_path = (
        ROOT
        / "services/aura-core/resources/component-manifests/document-retrieval.yaml"
    )
    manifest = cast(dict[str, Any], yaml.safe_load(manifest_path.read_text()))
    component = cast(dict[str, str], manifest["component"])

    assert component["id"] == COMPONENT_ID
    assert component["version"] == COMPONENT_VERSION == COMPONENT_VERSIONS[COMPONENT_ID]
    evaluator_ids = set(cast(list[str], manifest["evaluators"]))
    assert evaluator_ids <= set(EVALUATOR_VERSIONS)
    assert "document_preview_boundedness" in evaluator_ids


def test_document_evaluator_registry_matches_each_manifest_component() -> None:
    expected_components = {
        UPLOAD_COMPONENT_ID,
        PROCESSING_COMPONENT_ID,
        RETRIEVAL_COMPONENT_ID,
        DELETION_COMPONENT_ID,
    }
    manifests: dict[str, dict[str, Any]] = {}
    manifest_dir = ROOT / "services/aura-core/resources/component-manifests"
    for path in manifest_dir.glob("document-*.yaml"):
        manifest = cast(dict[str, Any], yaml.safe_load(path.read_text()))
        component = cast(dict[str, str], manifest["component"])
        component_id = component["id"]
        if component_id in expected_components:
            manifests[component_id] = manifest

    assert set(manifests) == expected_components
    assert {
        component_id: str(manifest["component"]["version"])
        for component_id, manifest in manifests.items()
    } == dict(DOCUMENT_COMPONENT_VERSIONS)
    for component_id, manifest in manifests.items():
        for evaluator_id in cast(list[str], manifest["evaluators"]):
            assert evaluator_id in EVALUATOR_COMPONENTS
            assert component_id in EVALUATOR_ALLOWED_COMPONENTS[evaluator_id]
            assert evaluator_id in EVALUATOR_VERSIONS
