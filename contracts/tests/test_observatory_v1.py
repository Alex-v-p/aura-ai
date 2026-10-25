"""AURA-0095 Observatory evaluation API contract invariants."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parents[2]
OPENAPI_PATH = ROOT / "contracts/openapi/observatory-v1.yaml"
EVALUATION_PATH = ROOT / "contracts/evaluation/v1/evaluation.schema.json"
GENERATED_PATH = (
    ROOT
    / "frontend/libs/platform/observatory-api-client/src/lib/generated/observatory-api.ts"
)


def _document() -> dict[str, Any]:
    return json.loads(OPENAPI_PATH.read_text(encoding="utf-8"))


def _resolve_parameter(document: dict[str, Any], parameter: dict[str, Any]) -> dict[str, Any]:
    reference = parameter.get("$ref")
    if reference is None:
        return parameter
    return document["components"]["parameters"][reference.rsplit("/", 1)[-1]]


def _operations(document: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        operation
        for path_item in document["paths"].values()
        for method, operation in path_item.items()
        if method in {"get", "post", "put", "patch", "delete"}
    ]


def test_evaluation_surface_is_owner_only_bounded_and_complete() -> None:
    document = _document()
    assert document["openapi"] == "3.1.0"
    assert document["info"]["x-contract-id"] == "observatory-v1"
    assert document["info"]["version"] == "1.0.0"
    assert document["security"] == [{"observatorySession": []}]

    expected = {
        "listEvaluationDatasets",
        "createEvaluationDataset",
        "getEvaluationDataset",
        "listEvaluationCases",
        "listEvaluationSuites",
        "createEvaluationSuite",
        "getEvaluationSuite",
        "listEvaluationEvaluators",
        "getEvaluationEvaluator",
        "listEvaluationJobs",
        "submitEvaluationJob",
        "getEvaluationJob",
        "listEvaluationAttempts",
        "cancelEvaluationJob",
        "listEvaluationResults",
        "getEvaluationResult",
        "listEvaluationScores",
        "listEvaluationArtifacts",
        "getEvaluationArtifact",
        "createEvaluationCaptureAuthorization",
        "listEvaluationReviews",
        "createEvaluationReview",
        "listEvaluationCalibrations",
        "listEvaluationQualifications",
        "qualifyEvaluationJudge",
        "revokeEvaluationQualification",
        "listEvaluationHistory",
        "createEvaluationDeletionAction",
        "getEvaluationDeletionAction",
        "streamEvaluationEvents",
    }
    operations = {operation["operationId"]: operation for operation in _operations(document)}
    assert expected <= operations.keys()

    for operation_id in expected:
        operation = operations[operation_id]
        assert operation.get("security", [{"observatorySession": []}]) == [
            {"observatorySession": []}
        ]
        assert {"401", "403"} <= operation["responses"].keys()

    for operation_id in (
        "listEvaluationDatasets",
        "listEvaluationCases",
        "listEvaluationSuites",
        "listEvaluationEvaluators",
        "listEvaluationJobs",
        "listEvaluationAttempts",
        "listEvaluationResults",
        "listEvaluationScores",
        "listEvaluationArtifacts",
        "listEvaluationReviews",
        "listEvaluationCalibrations",
        "listEvaluationQualifications",
        "listEvaluationHistory",
    ):
        parameters = [
            _resolve_parameter(document, item)
            for item in operations[operation_id].get("parameters", [])
        ]
        names = {item["name"] for item in parameters}
        assert {"cursor", "page_size"} <= names, operation_id
        page_size = next(item for item in parameters if item["name"] == "page_size")
        assert page_size["schema"]["maximum"] == 200


def test_commands_require_csrf_idempotency_and_closed_confirmation() -> None:
    document = _document()
    operations = {operation["operationId"]: operation for operation in _operations(document)}
    commands = {
        "createEvaluationDataset": False,
        "createEvaluationSuite": False,
        "submitEvaluationJob": False,
        "createEvaluationReview": False,
        "createEvaluationCaptureAuthorization": False,
        "cancelEvaluationJob": True,
        "qualifyEvaluationJudge": True,
        "revokeEvaluationQualification": True,
        "createEvaluationDeletionAction": True,
    }
    for operation_id, destructive in commands.items():
        parameters = [
            _resolve_parameter(document, item)
            for item in operations[operation_id]["parameters"]
        ]
        headers = {item["name"]: item for item in parameters if item["in"] == "header"}
        assert {"X-Observatory-CSRF", "Idempotency-Key"} <= headers.keys()
        assert all(headers[name]["required"] for name in headers)
        if destructive:
            confirmation = headers["X-Observatory-Confirmation"]["schema"]
            assert confirmation.get("enum") or confirmation.get("const")
            assert confirmation.get("additionalProperties") is None


def test_durable_commands_match_existing_domain_admission_shapes() -> None:
    document = _document()
    schemas = document["components"]["schemas"]
    operations = {operation["operationId"]: operation for operation in _operations(document)}

    suite = schemas["CreateEvaluationSuiteRequest"]
    assert {"components", "limits", "provenance", "createdAt"} <= set(suite["required"])
    assert "componentIds" not in suite["properties"]
    assert suite["properties"]["components"]["items"] == {
        "$ref": "#/components/schemas/EvaluationComponentBinding"
    }
    assert set(schemas["EvaluationComponentBinding"]["required"]) == {
        "componentId",
        "componentVersion",
        "codeRevision",
        "imageDigest",
        "configurationRevision",
    }

    job = schemas["SubmitEvaluationJobRequest"]
    assert {
        "requestId",
        "requestVersion",
        "dataset",
        "suite",
        "evaluator",
        "captureClassification",
        "deadlineAt",
        "maxAttempts",
        "requestData",
        "createdAt",
    } <= set(job["required"])
    runner = schemas["EvaluationRunnerRequestData"]
    assert runner["properties"]["provenance"] == {
        "$ref": "#/components/schemas/EvaluationRunnerExecutionProvenance"
    }
    runner_provenance = schemas["EvaluationRunnerExecutionProvenance"]["properties"]
    assert runner_provenance["dataset"] == {
        "$ref": "#/components/schemas/EvaluationDefinitionReference"
    }
    assert runner_provenance["suite"] == {
        "$ref": "#/components/schemas/EvaluationDefinitionReference"
    }
    assert schemas["CancelEvaluationJobRequest"]["properties"]["reason"]["enum"] == [
        "OWNER_REQUESTED",
        "SUPERSEDED",
    ]

    for operation_id in (
        "submitEvaluationJob",
        "cancelEvaluationJob",
        "createEvaluationReview",
        "qualifyEvaluationJudge",
        "revokeEvaluationQualification",
        "createEvaluationDeletionAction",
    ):
        parameters = [
            _resolve_parameter(document, item)
            for item in operations[operation_id]["parameters"]
        ]
        idempotency = next(item for item in parameters if item["name"] == "Idempotency-Key")
        assert idempotency["schema"] == {"$ref": "#/components/schemas/Uuid"}

    deletion = operations["createEvaluationDeletionAction"]
    parameters = [_resolve_parameter(document, item) for item in deletion["parameters"]]
    confirmation = next(
        item for item in parameters if item["name"] == "X-Observatory-Confirmation"
    )
    assert confirmation["schema"]["const"] == "DELETE_EVALUATION_ARTIFACTS"
    for name, kind in {
        "DeleteEvaluationJobRequest": "job",
        "DeleteEvaluationDatasetRequest": "dataset",
        "DeleteEvaluationSuiteRequest": "suite",
        "DeleteEvaluationBeforeRequest": "cutoff",
        "PurgeAllEvaluationRequest": "purge_all",
    }.items():
        assert schemas[name]["properties"]["kind"]["const"] == kind
        assert {"commandId", "requestedAt", "kind"} <= set(schemas[name]["required"])


def test_transport_dtos_are_closed_bounded_and_mapped_to_evaluation_v1() -> None:
    schemas = _document()["components"]["schemas"]
    mapped = {
        "CreateEvaluationDatasetRequest": "Dataset",
        "EvaluationDataset": "Dataset",
        "EvaluationCase": "Case",
        "CreateEvaluationSuiteRequest": "Suite",
        "EvaluationSuite": "Suite",
        "EvaluationEvaluator": "Evaluator",
        "EvaluationResult": "Result",
        "EvaluationScore": "Score",
        "EvaluationArtifact": "Artifact",
        "CreateEvaluationReviewRequest": "HumanReview",
        "EvaluationReview": "HumanReview",
        "EvaluationCalibration": "CalibrationResult",
        "QualifyEvaluationJudgeRequest": "JudgeQualification",
        "EvaluationQualification": "JudgeQualification",
    }
    for schema_name, resource_name in mapped.items():
        schema = schemas[schema_name]
        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False
        assert schema["x-evaluation-v1-resource"] == resource_name

    for schema_name, schema in schemas.items():
        if schema_name.startswith("Evaluation") and schema.get("type") == "object":
            assert schema.get("additionalProperties") is False, schema_name
        items = schema.get("properties", {}).get("items", {}).get("maxItems")
        if schema_name.endswith("Page") and "items" in schema.get("properties", {}):
            assert items == 200, schema_name


def test_transport_mapping_preserves_evaluation_v1_identity_and_finite_values() -> None:
    schemas = _document()["components"]["schemas"]
    evaluation = json.loads(EVALUATION_PATH.read_text(encoding="utf-8"))["$defs"]

    assert schemas["Uuid"] == evaluation["Uuid"]
    assert schemas["EvaluationVersion"] == evaluation["Version"]
    assert schemas["EvaluationScope"]["enum"] == evaluation["Scope"]["enum"]
    assert schemas["EvaluationResourceReference"]["properties"]["resourceId"] == {
        "$ref": "#/components/schemas/Uuid"
    }
    assert schemas["EvaluationEvaluatorKind"]["enum"] == evaluation[
        "EvaluatorReference"
    ]["properties"]["kind"]["enum"]
    assert schemas["EvaluationDataset"]["properties"]["sourceKind"]["enum"] == evaluation[
        "Dataset"
    ]["properties"]["sourceKind"]["enum"]
    assert schemas["EvaluationReviewOutcome"]["enum"] == evaluation["HumanReview"][
        "properties"
    ]["reviewOutcome"]["enum"]
    assert schemas["EvaluationReviewLabel"]["enum"] == evaluation["HumanReview"][
        "properties"
    ]["reviewLabel"]["enum"]
    assert schemas["EvaluationDisagreement"]["enum"] == evaluation["HumanReview"][
        "properties"
    ]["disagreement"]["enum"]
    assert schemas["EvaluationCalibrationEligibility"]["enum"] == evaluation[
        "CalibrationResult"
    ]["properties"]["eligibility"]["enum"]


def test_manual_deletion_request_uses_five_disjoint_closed_scopes() -> None:
    schemas = _document()["components"]["schemas"]
    deletion = schemas["CreateEvaluationDeletionRequest"]
    targets = {
        reference["$ref"].rsplit("/", 1)[-1] for reference in deletion["oneOf"]
    }
    assert targets == {
        "DeleteEvaluationJobRequest",
        "DeleteEvaluationDatasetRequest",
        "DeleteEvaluationSuiteRequest",
        "DeleteEvaluationBeforeRequest",
        "PurgeAllEvaluationRequest",
    }
    for target in targets:
        assert schemas[target]["additionalProperties"] is False


def test_evaluation_rest_and_sse_exclude_private_or_content_fields() -> None:
    document = _document()
    schemas = document["components"]["schemas"]
    forbidden_fields = {
        "content",
        "prompt",
        "rubric",
        "response",
        "input",
        "output",
        "comment",
        "rationale",
        "reasoning",
        "objectKey",
        "bucket",
        "filename",
        "endpoint",
        "credential",
        "token",
        "ownerIdentity",
        "attributes",
    }
    evaluation_names = {
        name
        for name in schemas
        if name.startswith("Evaluation") or name.startswith("CreateEvaluation")
    }
    for name in evaluation_names:
        assert not forbidden_fields.intersection(schemas[name].get("properties", {})), name

    live = schemas["EvaluationLiveEvent"]
    assert live["additionalProperties"] is False
    assert live["properties"]["capturePolicy"] == {
        "type": "string",
        "const": "metadata_only",
    }
    live_properties = set(live["properties"])
    assert not {
        "score",
        "value",
        "digest",
        "authorizationRecordId",
        "modelEndpoint",
        "content",
    }.intersection(live_properties)
    assert live["properties"]["eventType"] == {
        "type": "string",
        "const": "evaluation.changed",
    }
    assert {"change", "state"} <= set(live["required"])
    assert "evaluation_score" not in live["properties"]["resourceType"]["enum"]

    for binding_name in (
        "EvaluationPromptArtifactBinding",
        "EvaluationRubricArtifactBinding",
        "EvaluationReviewCommentArtifactBinding",
    ):
        binding = schemas[binding_name]
        assert binding["additionalProperties"] is False
        assert set(binding["properties"]) <= {
            "artifactId",
            "artifactVersion",
            "purpose",
            "digest",
        }
        assert not {
            "content",
            "objectKey",
            "bucket",
            "filename",
            "endpoint",
            "credential",
        }.intersection(binding["properties"])


def test_domain_command_shapes_reuse_evaluation_v1_bounds() -> None:
    schemas = _document()["components"]["schemas"]
    evaluation = json.loads(EVALUATION_PATH.read_text(encoding="utf-8"))["$defs"]

    component = schemas["EvaluationComponentBinding"]
    assert component["required"] == evaluation["ComponentBinding"]["required"]
    assert component["properties"]["imageDigest"] == {
        "$ref": "#/components/schemas/EvaluationDigest"
    }
    limits = schemas["EvaluationExecutionLimits"]
    assert limits["required"] == evaluation["ExecutionLimits"]["required"]
    for name, source in evaluation["ExecutionLimits"]["properties"].items():
        assert limits["properties"][name]["minimum"] == source["minimum"]
        assert limits["properties"][name]["maximum"] == source["maximum"]

    review = schemas["CreateEvaluationReviewRequest"]
    assert set(evaluation["HumanReview"]["required"]) - {
        "schemaVersion",
        "resourceType",
        "idempotencyKey",
    } <= set(review["required"])
    qualification = schemas["QualifyEvaluationJudgeRequest"]
    assert set(evaluation["JudgeQualification"]["required"]) - {
        "schemaVersion",
        "resourceType",
        "idempotencyKey",
        "state",
    } <= set(qualification["required"])


def test_job_and_attempt_projections_use_authoritative_admission_facts() -> None:
    schemas = _document()["components"]["schemas"]

    job = schemas["EvaluationJob"]
    assert job["properties"]["provenance"]["$ref"] == (
        "#/components/schemas/EvaluationRunnerExecutionProvenance"
    )
    provenance = schemas["EvaluationRunnerExecutionProvenance"]
    assert provenance["additionalProperties"] is False
    assert {"dataset", "suite", "evaluators", "components", "model"} <= set(
        provenance["required"]
    )

    attempt = schemas["EvaluationAttempt"]
    assert attempt["oneOf"] == [
        {"$ref": "#/components/schemas/EvaluationQueuedAttempt"},
        {"$ref": "#/components/schemas/EvaluationClaimedAttempt"},
    ]
    queued = schemas["EvaluationQueuedAttempt"]
    assert queued["properties"]["status"]["const"] == "queued"
    assert "workerComponentId" not in queued["required"]
    worker_types = {
        branch.get("type")
        for branch in queued["properties"]["workerComponentId"]["oneOf"]
    }
    assert worker_types == {
        None,
        "null",
    }
    claimed = schemas["EvaluationClaimedAttempt"]
    assert "workerComponentId" in claimed["required"]
    assert claimed["properties"]["workerComponentId"] == {
        "$ref": "#/components/schemas/ComponentId",
        "description": "Authoritative runner component persisted by the claim transaction.",
    }
    assert "queued" not in claimed["properties"]["status"]["enum"]


def test_generated_client_is_transport_only_and_contains_evaluation_operations() -> None:
    generated = GENERATED_PATH.read_text(encoding="utf-8")
    assert "Generated from contracts/openapi/observatory-v1.yaml" in generated
    assert "readonly listEvaluationDatasets:" in generated
    assert "readonly createEvaluationDeletionAction:" in generated
    assert "readonly streamEvaluationEvents:" in generated
    assert "readonly streamEvents:" in generated
    forbidden_behavior = ("localStorage", "sessionStorage", "indexedDB", "retry(", "EventSource(")
    assert not any(value in generated for value in forbidden_behavior)


def test_generated_evaluation_page_operations_never_erase_success_responses() -> None:
    generated = GENERATED_PATH.read_text(encoding="utf-8")
    expected = {
        "listEvaluationDatasets": "EvaluationDatasetPage",
        "listEvaluationCases": "EvaluationCasePage",
        "listEvaluationSuites": "EvaluationSuitePage",
        "listEvaluationEvaluators": "EvaluationEvaluatorPage",
        "listEvaluationJobs": "EvaluationJobPage",
        "listEvaluationAttempts": "EvaluationAttemptPage",
        "listEvaluationResults": "EvaluationResultPage",
        "listEvaluationScores": "EvaluationScorePage",
        "listEvaluationArtifacts": "EvaluationArtifactPage",
        "listEvaluationReviews": "EvaluationReviewPage",
        "listEvaluationCalibrations": "EvaluationCalibrationPage",
        "listEvaluationQualifications": "EvaluationQualificationPage",
        "listEvaluationHistory": "EvaluationHistoryPage",
    }
    for operation_id, response_type in expected.items():
        assert (
            f"readonly {operation_id}: {{ readonly response: {response_type};" in generated
        )
        assert f"{operation_id}: {{ method: 'GET'" in generated
        descriptor = generated.split(f"{operation_id}: {{ method: 'GET'", 1)[1].split("\n", 1)[0]
        assert "responseMode: 'json'" in descriptor


def test_acceptance_admission_contracts_are_owner_only_closed_and_bounded() -> None:
    document = _document()
    schemas = document["components"]["schemas"]
    operations = {operation["operationId"]: operation for operation in _operations(document)}

    evaluator = operations["createEvaluationLocalJudgeEvaluator"]
    artifact = operations["createEvaluationArtifact"]
    authorization = operations["createEvaluationCaptureAuthorization"]
    for operation in (evaluator, artifact, authorization):
        parameters = [
            _resolve_parameter(document, item) for item in operation["parameters"]
        ]
        headers = {item["name"] for item in parameters if item["in"] == "header"}
        assert headers == {"X-Observatory-CSRF", "Idempotency-Key"}
        assert {"400", "401", "403", "409", "422", "503"} <= operation[
            "responses"
        ].keys()
        success = next(
            response
            for status, response in operation["responses"].items()
            if status.startswith("2")
        )
        assert success["headers"]["Cache-Control"]["schema"] == {
            "type": "string",
            "const": "no-store",
        }

    create_evaluator = schemas["CreateLocalJudgeEvaluatorRequest"]
    assert create_evaluator["additionalProperties"] is False
    assert create_evaluator["x-evaluation-v1-resource"] == "LocalJudgeDefinition"
    assert create_evaluator["properties"]["outputContract"]["const"] == (
        "local_judge_score_v1"
    )
    assert create_evaluator["properties"]["defaultDisposition"]["const"] == (
        "advisory_only"
    )
    assert schemas["EvaluationSuiteEvaluatorReference"]["properties"]["kind"][
        "enum"
    ] == ["deterministic", "local_judge"]

    multipart = artifact["requestBody"]["content"]["multipart/form-data"]
    assert multipart["schema"] == {"$ref": "#/components/schemas/ArtifactUploadForm"}
    form = schemas["ArtifactUploadForm"]
    assert form["additionalProperties"] is False
    assert form["minProperties"] == form["maxProperties"] == 2
    metadata = schemas["EvaluationArtifactUploadMetadata"]
    assert metadata["additionalProperties"] is False
    assert metadata["properties"]["byteCount"] == {
        "type": "integer",
        "minimum": 1,
        "maximum": 1048576,
    }
    assert metadata["properties"]["captureAuthorization"] == {
        "$ref": "#/components/schemas/EvaluationContentCaptureAuthorization"
    }
    assert metadata["properties"]["mediaType"]["enum"] == [
        "application/json",
        "text/plain",
        "text/markdown",
    ]
    assert "application/octet-stream" not in json.dumps(metadata)
    assert form["properties"]["content"]["maxLength"] == 1048576
    assert "contentMediaType" not in form["properties"]["content"]

    authorization_request = schemas["CreateEvaluationCaptureAuthorizationRequest"]
    authorization_record = schemas["EvaluationCaptureAuthorizationRecord"]
    assert authorization_request["additionalProperties"] is False
    assert authorization_record["additionalProperties"] is False
    assert authorization_request["properties"]["dataset"] == {
        "$ref": "#/components/schemas/EvaluationDefinitionReference"
    }
    assert authorization_record["properties"]["status"] == {
        "type": "string",
        "const": "active",
    }
    for schema in (authorization_request, authorization_record):
        assert schema["properties"]["retentionMode"] == {
            "$ref": "#/components/schemas/EvaluationRetentionMode"
        }
        assert schema["properties"]["authorizationContext"] == {
            "type": "string",
            "const": "controlled_evaluation",
        }


def test_core_nats_recovery_measurement_is_additively_allowlisted() -> None:
    schemas = _document()["components"]["schemas"]
    assert "evaluation_runner_nats_recovery_count" in schemas["AnalyticsMetricName"][
        "enum"
    ]


def test_generated_client_supports_bounded_multipart_without_transport_behavior() -> None:
    generated = GENERATED_PATH.read_text(encoding="utf-8")
    assert (
        "readonly createEvaluationArtifact: { readonly response: EvaluationArtifact; "
        "readonly body: ArtifactUploadForm;" in generated
    )
    assert 'readonly "content": Blob;' in generated
    assert (
        "createEvaluationArtifact: { method: 'POST', pathTemplate: "
        "'/api/v1/evaluation/artifacts', requestMode: 'multipart', "
        "responseMode: 'json' }" in generated
    )
    assert "readonly createEvaluationLocalJudgeEvaluator:" in generated
    assert "readonly createEvaluationCaptureAuthorization:" in generated
