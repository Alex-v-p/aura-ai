"""Static OpenAPI and lifecycle-event assertions for AURA-0068."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import yaml

ROOT = Path(__file__).parents[3]


def _contract() -> dict[str, Any]:
    return cast(dict[str, Any], json.loads((ROOT / "contracts/openapi/aura-v1.yaml").read_text()))


def test_document_api_uses_canonical_streaming_paths_and_backward_compatible_schemas() -> None:
    contract = _contract()
    assert contract["info"]["version"] == "1.14.0"
    paths = cast(dict[str, dict[str, Any]], contract["paths"])
    expected = {
        "/api/v1/documents/uploads",
        "/api/v1/documents/uploads/{upload_id}/content",
        "/api/v1/documents",
        "/api/v1/documents/search",
        "/api/v1/documents/{document_id}",
        "/api/v1/documents/{document_id}/content",
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
    for name in (
        "DocumentSummary",
        "DocumentDetail",
        "DocumentUploadSession",
        "DocumentSearchResult",
    ):
        assert not {"objectKey", "bucket", "credential", "embedding", "vector"} & set(
            schemas[name]["properties"]
        )


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
    assert required_metrics <= found_metrics
