# ruff: noqa: E501

"""Owner-only evaluation REST application boundary.

Routes in this module are deliberately thin.  They authenticate the existing
Observatory BFF session, enforce command headers, and translate the closed
observatory-v1 transport into the existing evaluation application services.
They never return artifact bytes, object locations, comments, credentials, or
provider diagnostics.
"""

# FastAPI request JSON is narrowed by the closed helpers in this module.
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownMemberType=false, reportGeneralTypeIssues=false, reportArgumentType=false, reportPrivateUsage=false, reportUnusedFunction=false, reportUnnecessaryIsInstance=false

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime
from time import monotonic
from typing import Any, cast
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

from aura_observatory.evaluation_calibration import (
    CalibrationError,
    DisagreementClass,
    HumanReview,
    JudgeOutcome,
    Qualification,
)
from aura_observatory.evaluation_definitions import (
    DefinitionCommandPendingError,
    DefinitionConflictError,
    DefinitionNotFoundError,
    DefinitionStorageError,
    DefinitionValidationError,
)
from aura_observatory.evaluation_live import EvaluationLiveEventPort, EvaluationLiveEventStore
from aura_observatory.evaluation_orchestration import EvaluationAdmission, OrchestrationError
from aura_observatory.evaluation_query import (
    EvaluationConflict,
    EvaluationNotFound,
    EvaluationQueryError,
    EvaluationQueryService,
    EvaluationUnavailable,
    project_definition,
    provenance,
    resource_ref,
    utc_now,
)
from aura_observatory.evaluation_storage import (
    ArtifactStorageError,
    DeletionCommand,
    DeletionKind,
    DeletionSelector,
)

_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
_IDEMPOTENCY = re.compile(r"^[A-Za-z0-9._:-]{16,128}$")
_CONFIRMATIONS = frozenset(
    {
        "cancel-job",
        "qualify-judge",
        "revoke-qualification",
        "delete-job",
        "delete-dataset",
        "delete-suite",
        "delete-before",
        "purge-all",
        "DELETE_EVALUATION_ARTIFACTS",
    }
)
_RESOURCE_KINDS = frozenset(
    {
        "datasets",
        "cases",
        "suites",
        "evaluators",
        "jobs",
        "attempts",
        "results",
        "scores",
        "artifacts",
        "reviews",
        "calibrations",
        "qualifications",
        "deletions",
    }
)

# These sets mirror the additionalProperties:false definition commands in
# observatory-v1.  Keeping them at the transport boundary is important: a
# domain service must never become an accidental JSON pass-through.
_DATASET_FIELDS = frozenset(
    {
        "datasetId",
        "datasetVersion",
        "sourceKind",
        "scopeIds",
        "captureClassification",
        "cases",
        "retentionMode",
    }
)
_CASE_FIELDS = frozenset({"caseId", "caseVersion", "artifactIds", "captureClassification"})
_SUITE_FIELDS = frozenset(
    {
        "suiteId",
        "suiteVersion",
        "dataset",
        "evaluators",
        "components",
        "limits",
        "manualThresholds",
        "retentionMode",
        "provenance",
        "createdAt",
    }
)
_REFERENCE_FIELDS = frozenset({"id", "version"})
_EVALUATOR_FIELDS = frozenset({"evaluatorId", "evaluatorVersion", "kind"})
_COMPONENT_FIELDS = frozenset(
    {"componentId", "componentVersion", "codeRevision", "imageDigest", "configurationRevision"}
)
_LIMIT_FIELDS = frozenset(
    {
        "timeoutMs",
        "maxConcurrency",
        "maxAttempts",
        "maxCases",
        "maxInputBytes",
        "maxOutputBytes",
        "maxTokens",
    }
)
_PROVENANCE_FIELDS = frozenset(
    {"createdByComponent", "createdByVersion", "sourceDigest"}
)
_REVIEW_FIELDS = frozenset(
    {
        "reviewId",
        "reviewVersion",
        "calibration",
        "caseId",
        "resultId",
        "scoreId",
        "reviewScore",
        "reviewOutcome",
        "reviewLabel",
        "disagreement",
        "authorizationRecordId",
        "commentArtifact",
        "retentionMode",
        "provenance",
        "createdAt",
    }
)
_CALIBRATION_REFERENCE_FIELDS = frozenset({"calibrationId", "calibrationVersion"})
_COMMENT_ARTIFACT_FIELDS = frozenset({"artifactId", "artifactVersion", "purpose", "digest"})
_LOCAL_JUDGE_MODEL_FIELDS = frozenset({"provider", "modelName", "modelVersion", "modelDigest"})
_QUALIFICATION_FIELDS = frozenset(
    {
        "qualificationId",
        "qualificationVersion",
        "action",
        "actionMode",
        "dataset",
        "suite",
        "evaluator",
        "judgeDefinition",
        "model",
        "prompt",
        "rubric",
        "calibration",
        "calibrationResultId",
        "calibrationEligibility",
        "authorizationRecordId",
        "scoreDisposition",
        "qualifiedAt",
        "retentionMode",
        "provenance",
        "createdAt",
    }
)
_SUBMIT_FIELDS = frozenset(
    {
        "requestId", "requestVersion", "dataset", "suite", "evaluator",
        "captureClassification", "deadlineAt", "maxAttempts", "requestData", "createdAt",
    }
)
_RUNNER_DATA_FIELDS = frozenset(
    {
        "executionKind", "requestVersion", "targetComponent", "captureAuthorization",
        "provenance", "limits", "deadlineAt", "caseIds", "inputArtifactReferences",
    }
)
_CAPTURE_AUTH_FIELDS = frozenset({"captureClassification", "authorizationPurpose", "authorizationContext", "retentionMode", "authorizationRecordId", "authorizedAt"})
_RUNNER_LIMIT_FIELDS = frozenset({"execution", "maxQueueTimeMs", "maxModelCalls", "maxArtifactOperations"})
_EXECUTION_LIMIT_FIELDS = _LIMIT_FIELDS
_ARTIFACT_REF_FIELDS = frozenset({"artifactReferenceId", "mediaClassification", "mediaType", "byteCount", "integrity", "captureAuthorization"})
_INTEGRITY_FIELDS = frozenset({"algorithm", "digest"})
_MODEL_FIELDS = frozenset({"modelId", "modelVersion", "modelDigest"})
_REPRODUCIBILITY_FIELDS = frozenset({"seed", "clockMode", "timeZone", "locale", "fixtureRevision", "promptRevision", "policyRevision"})
_REVOKE_FIELDS = frozenset({"action", "authorizationRecordId", "revokedAt"})
_CAPTURE_CLASSES = frozenset(
    {"metadata_only", "hashed", "redacted", "sampled_content", "full_content"}
)
_SOURCE_KINDS = frozenset({"synthetic", "owner_supplied", "captured"})
_SCOPE_IDS = frozenset({"public_project", "private", "sensitive", "environment"})
_EVALUATOR_KINDS = frozenset({"deterministic", "local_judge", "human_review"})
_COMPONENT_ID = re.compile(r"^aura\.[a-z][a-z0-9_.-]{0,90}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


def _uuid(value: object, name: str) -> str:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise EvaluationQueryError(f"{name} is invalid")
    try:
        parsed = UUID(value)
    except ValueError as exc:
        raise EvaluationQueryError(f"{name} is invalid") from exc
    if str(parsed) != value:
        raise EvaluationQueryError(f"{name} is invalid")
    return value


def _version(value: object, name: str) -> str:
    if not isinstance(value, str) or _VERSION.fullmatch(value) is None:
        raise EvaluationQueryError(f"{name} is invalid")
    return value


def _model_identifier(value: object, name: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 193 or re.fullmatch(
        r"[a-z0-9][a-z0-9._+-]{0,127}(?::[a-z0-9._+-]+)?", value
    ) is None or any(
        marker in value.casefold()
        for marker in ("owner", "identity", "credential", "bearer", "token", "secret", "password", "private", "content", "hash", "digest", "endpoint")
    ):
        raise EvaluationQueryError(f"{name} is invalid")
    return value


def _object(body: object, allowed: frozenset[str]) -> dict[str, object]:
    if (
        not isinstance(body, Mapping)
        or any(not isinstance(key, str) for key in body)
        or set(body) - allowed
    ):
        raise EvaluationQueryError("request body is invalid")
    return dict(cast(Mapping[str, object], body))


def _closed_definition_object(
    value: object, allowed: frozenset[str], name: str
) -> dict[str, object]:
    try:
        result = _object(value, allowed)
    except EvaluationQueryError as exc:
        raise EvaluationQueryError(f"{name} is invalid") from exc
    return result


def _bounded_string_list(value: object, name: str, *, maximum: int) -> list[str]:
    if not isinstance(value, list) or not 1 <= len(value) <= maximum:
        raise EvaluationQueryError(f"{name} is invalid")
    if any(not isinstance(item, str) for item in value) or len(set(value)) != len(value):
        raise EvaluationQueryError(f"{name} is invalid")
    return list(cast(list[str], value))


def _validate_provenance(value: object, name: str = "provenance") -> None:
    item = _closed_definition_object(value, _PROVENANCE_FIELDS, name)
    component = item.get("createdByComponent")
    if not isinstance(component, str) or _COMPONENT_ID.fullmatch(component) is None:
        raise EvaluationQueryError(f"{name} is invalid")
    _version(item.get("createdByVersion"), f"{name}.createdByVersion")
    digest = item.get("sourceDigest")
    if not isinstance(digest, str) or _DIGEST.fullmatch(digest) is None:
        raise EvaluationQueryError(f"{name} is invalid")


def _validate_component(value: object, name: str = "component") -> dict[str, object]:
    item = _closed_definition_object(value, _COMPONENT_FIELDS, name)
    component = item.get("componentId")
    if not isinstance(component, str) or _COMPONENT_ID.fullmatch(component) is None:
        raise EvaluationQueryError(f"{name} is invalid")
    _version(item.get("componentVersion"), f"{name}.componentVersion")
    for field in ("codeRevision", "configurationRevision"):
        revision = item.get(field)
        if not isinstance(revision, str) or not 1 <= len(revision) <= 128:
            raise EvaluationQueryError(f"{name}.{field} is invalid")
    digest = item.get("imageDigest")
    if not isinstance(digest, str) or _DIGEST.fullmatch(digest) is None:
        raise EvaluationQueryError(f"{name}.imageDigest is invalid")
    return item


def _validate_definition_request(kind: str, body: Mapping[str, object]) -> None:
    """Validate the entire definition command before invoking a domain port."""

    if kind == "dataset":
        item = _closed_definition_object(body, _DATASET_FIELDS, "dataset")
        _uuid(item.get("datasetId"), "datasetId")
        _version(item.get("datasetVersion"), "datasetVersion")
        if item.get("sourceKind") not in _SOURCE_KINDS:
            raise EvaluationQueryError("sourceKind is invalid")
        scopes = _bounded_string_list(item.get("scopeIds"), "scopeIds", maximum=4)
        if any(scope not in _SCOPE_IDS for scope in scopes):
            raise EvaluationQueryError("scopeIds is invalid")
        if item.get("captureClassification") not in _CAPTURE_CLASSES:
            raise EvaluationQueryError("captureClassification is invalid")
        if item.get("retentionMode") != "manual_only":
            raise EvaluationQueryError("retentionMode is invalid")
        cases = item.get("cases")
        if not isinstance(cases, list) or not 1 <= len(cases) <= 10_000:
            raise EvaluationQueryError("cases are invalid")
        for index, raw_case in enumerate(cases):
            case = _closed_definition_object(raw_case, _CASE_FIELDS, f"cases[{index}]")
            _uuid(case.get("caseId"), f"cases[{index}].caseId")
            if "caseVersion" in case:
                _version(case["caseVersion"], f"cases[{index}].caseVersion")
            if "artifactIds" in case:
                artifact_ids = case["artifactIds"]
                if (
                    not isinstance(artifact_ids, list)
                    or len(artifact_ids) > 32
                    or any(not isinstance(item, str) for item in artifact_ids)
                    or len(set(artifact_ids)) != len(artifact_ids)
                ):
                    raise EvaluationQueryError(f"cases[{index}].artifactIds is invalid")
            if (
                "captureClassification" in case
                and case["captureClassification"] not in _CAPTURE_CLASSES
            ):
                raise EvaluationQueryError(f"cases[{index}].captureClassification is invalid")
        return
    if kind != "suite":
        raise EvaluationQueryError("definition operation is invalid")
    item = _closed_definition_object(body, _SUITE_FIELDS, "suite")
    _uuid(item.get("suiteId"), "suiteId")
    _version(item.get("suiteVersion"), "suiteVersion")
    dataset = _closed_definition_object(item.get("dataset"), _REFERENCE_FIELDS, "dataset")
    _uuid(dataset.get("id"), "dataset.id")
    _version(dataset.get("version"), "dataset.version")
    evaluators = item.get("evaluators")
    if not isinstance(evaluators, list) or not 1 <= len(evaluators) <= 32:
        raise EvaluationQueryError("evaluators are invalid")
    for index, raw_evaluator in enumerate(evaluators):
        evaluator = _closed_definition_object(
            raw_evaluator, _EVALUATOR_FIELDS, f"evaluators[{index}]"
        )
        _uuid(evaluator.get("evaluatorId"), f"evaluators[{index}].evaluatorId")
        _version(evaluator.get("evaluatorVersion"), f"evaluators[{index}].evaluatorVersion")
        if evaluator.get("kind") not in _EVALUATOR_KINDS:
            raise EvaluationQueryError(f"evaluators[{index}].kind is invalid")
    components = item.get("components")
    if not isinstance(components, list) or not 1 <= len(components) <= 32:
        raise EvaluationQueryError("components are invalid")
    for index, component in enumerate(components):
        _validate_component(component, f"components[{index}]")
    limits = _closed_definition_object(item.get("limits"), _LIMIT_FIELDS, "limits")
    for field, value in limits.items():
        if type(value) is not int or value < 0:
            raise EvaluationQueryError(f"limits.{field} is invalid")
    thresholds = item.get("manualThresholds", [])
    if not isinstance(thresholds, (Mapping, list)) or len(thresholds) > 128:
        raise EvaluationQueryError("manualThresholds is invalid")
    if isinstance(thresholds, Mapping):
        for key, value in thresholds.items():
            if (
                not isinstance(key, str)
                or not key
                or len(key) > 128
                or type(value) not in {int, float}
            ):
                raise EvaluationQueryError("manualThresholds is invalid")
    else:
        threshold_fields = frozenset(
            {
                "evaluator",
                "metric",
                "aggregation",
                "operator",
                "value",
                "unit",
                "minimumSampleSize",
                "missingEvidencePolicy",
            }
        )
        for index, threshold in enumerate(thresholds):
            value = _closed_definition_object(
                threshold, threshold_fields, f"manualThresholds[{index}]"
            )
            evaluator = _closed_definition_object(
                value.get("evaluator"), _EVALUATOR_FIELDS, f"manualThresholds[{index}].evaluator"
            )
            _uuid(evaluator.get("evaluatorId"), "manualThresholds.evaluatorId")
            _version(evaluator.get("evaluatorVersion"), "manualThresholds.evaluatorVersion")
            if evaluator.get("kind") != "deterministic":
                raise EvaluationQueryError("manualThresholds.evaluator.kind is invalid")
    if item.get("retentionMode") != "manual_only":
        raise EvaluationQueryError("retentionMode is invalid")
    _validate_provenance(item.get("provenance"))
    _timestamp(item.get("createdAt"), "createdAt")


def _validate_review_request(body: Mapping[str, object]) -> None:
    item = _closed_definition_object(body, _REVIEW_FIELDS, "review")
    for key in ("reviewId", "caseId", "resultId", "scoreId", "authorizationRecordId"):
        _uuid(item.get(key), key)
    _version(item.get("reviewVersion"), "reviewVersion")
    calibration = _closed_definition_object(
        item.get("calibration"), _CALIBRATION_REFERENCE_FIELDS, "calibration"
    )
    _uuid(calibration.get("calibrationId"), "calibration.calibrationId")
    _version(calibration.get("calibrationVersion"), "calibration.calibrationVersion")
    score = item.get("reviewScore")
    if type(score) not in {int, float} or not 0 <= float(score) <= 1:
        raise EvaluationQueryError("reviewScore is invalid")
    if item.get("reviewOutcome") not in {"pass", "fail", "inconclusive"}:
        raise EvaluationQueryError("reviewOutcome is invalid")
    if item.get("reviewLabel") not in {"accepted", "rejected", "needs_review"}:
        raise EvaluationQueryError("reviewLabel is invalid")
    if item.get("disagreement") not in {
        "none",
        "within_tolerance",
        "score_disagreement",
        "outcome_disagreement",
        "insufficient_evidence",
    }:
        raise EvaluationQueryError("disagreement is invalid")
    if item.get("retentionMode") != "manual_only":
        raise EvaluationQueryError("retentionMode is invalid")
    _validate_provenance(item.get("provenance"))
    _timestamp(item.get("createdAt"), "createdAt")
    comment = item.get("commentArtifact")
    if comment is not None:
        binding = _closed_definition_object(comment, _COMMENT_ARTIFACT_FIELDS, "commentArtifact")
        _uuid(binding.get("artifactId"), "commentArtifact.artifactId")
        _version(binding.get("artifactVersion"), "commentArtifact.artifactVersion")
        if binding.get("purpose") != "human_review_comment" or not isinstance(
            binding.get("digest"), str
        ) or _DIGEST.fullmatch(cast(str, binding["digest"])) is None:
            raise EvaluationQueryError("commentArtifact is invalid")


def _validate_qualification_request(body: Mapping[str, object]) -> None:
    item = _closed_definition_object(body, _QUALIFICATION_FIELDS, "qualification")
    for key in ("qualificationId", "calibrationResultId", "authorizationRecordId"):
        _uuid(item.get(key), key)
    _version(item.get("qualificationVersion"), "qualificationVersion")
    if item.get("action") != "qualify" or item.get("actionMode") != "manual_owner_action":
        raise EvaluationQueryError("qualification action is invalid")
    if item.get("calibrationEligibility") != "eligible" or item.get(
        "scoreDisposition"
    ) != "calibrated_enabled":
        raise EvaluationQueryError("qualification state is invalid")
    for key in ("dataset", "suite"):
        reference = _closed_definition_object(item.get(key), _REFERENCE_FIELDS, key)
        _uuid(reference.get("id"), f"{key}.id")
        _version(reference.get("version"), f"{key}.version")
    evaluator = _closed_definition_object(item.get("evaluator"), _EVALUATOR_FIELDS, "evaluator")
    _uuid(evaluator.get("evaluatorId"), "evaluator.evaluatorId")
    _version(evaluator.get("evaluatorVersion"), "evaluator.evaluatorVersion")
    if evaluator.get("kind") != "local_judge":
        raise EvaluationQueryError("evaluator.kind is invalid")
    model = _closed_definition_object(item.get("model"), _LOCAL_JUDGE_MODEL_FIELDS, "model")
    if model.get("provider") != "local_ollama":
        raise EvaluationQueryError("model.provider is invalid")
    _model_identifier(model.get("modelName"), "model.modelName")
    _version(model.get("modelVersion"), "model.modelVersion")
    digest = model.get("modelDigest")
    if not isinstance(digest, str) or _DIGEST.fullmatch(digest) is None:
        raise EvaluationQueryError("model.modelDigest is invalid")
    judge = _closed_definition_object(
        item.get("judgeDefinition"),
        frozenset({"judgeDefinitionId", "judgeDefinitionVersion"}),
        "judgeDefinition",
    )
    _uuid(judge.get("judgeDefinitionId"), "judgeDefinition.judgeDefinitionId")
    _version(judge.get("judgeDefinitionVersion"), "judgeDefinition.judgeDefinitionVersion")
    for key, purpose in (("prompt", "judge_prompt"), ("rubric", "judge_rubric")):
        binding = _closed_definition_object(
            item.get(key), _COMMENT_ARTIFACT_FIELDS, key
        )
        _uuid(binding.get("artifactId"), f"{key}.artifactId")
        _version(binding.get("artifactVersion"), f"{key}.artifactVersion")
        digest = binding.get("digest")
        if (
            binding.get("purpose") != purpose
            or not isinstance(digest, str)
            or _DIGEST.fullmatch(digest) is None
        ):
            raise EvaluationQueryError(f"{key} is invalid")
    calibration = _closed_definition_object(
        item.get("calibration"), _CALIBRATION_REFERENCE_FIELDS, "calibration"
    )
    _uuid(calibration.get("calibrationId"), "calibration.calibrationId")
    _version(calibration.get("calibrationVersion"), "calibration.calibrationVersion")
    if item.get("retentionMode") != "manual_only":
        raise EvaluationQueryError("retentionMode is invalid")
    _validate_provenance(item.get("provenance"))
    _timestamp(item.get("qualifiedAt"), "qualifiedAt")
    _timestamp(item.get("createdAt"), "createdAt")


def _validate_revoke_request(body: Mapping[str, object]) -> None:
    item = _closed_definition_object(body, _REVOKE_FIELDS, "revoke")
    if item.get("action") != "revoke":
        raise EvaluationQueryError("action is invalid")
    _uuid(item.get("authorizationRecordId"), "authorizationRecordId")
    _timestamp(item.get("revokedAt"), "revokedAt")


def _validate_cancel_request(body: Mapping[str, object]) -> None:
    item = _closed_definition_object(body, frozenset({"action", "reason"}), "cancel")
    if item.get("action") != "cancel" or item.get("reason") not in {
        "OWNER_REQUESTED",
        "SUPERSEDED",
    }:
        raise EvaluationQueryError("cancellation request is invalid")


def _validate_submit_request(body: Mapping[str, object]) -> None:
    item = _closed_definition_object(body, _SUBMIT_FIELDS, "submit")
    if set(item) != set(_SUBMIT_FIELDS):
        raise EvaluationQueryError("submit body is invalid")
    for key in ("requestId",):
        _uuid(item.get(key), key)
    _version(item.get("requestVersion"), "requestVersion")
    for key in ("dataset", "suite", "evaluator"):
        reference = _closed_definition_object(item.get(key), _REFERENCE_FIELDS, key)
        _uuid(reference.get("id"), f"{key}.id")
        _version(reference.get("version"), f"{key}.version")
    if item.get("captureClassification") not in _CAPTURE_CLASSES:
        raise EvaluationQueryError("captureClassification is invalid")
    _timestamp(item.get("deadlineAt"), "deadlineAt")
    max_attempts = item.get("maxAttempts")
    if type(max_attempts) is not int or not 1 <= max_attempts <= 10:
        raise EvaluationQueryError("maxAttempts is invalid")
    _timestamp(item.get("createdAt"), "createdAt")
    data = _closed_definition_object(item.get("requestData"), _RUNNER_DATA_FIELDS, "requestData")
    if set(data) != set(_RUNNER_DATA_FIELDS):
        raise EvaluationQueryError("requestData is invalid")
    if data.get("executionKind") not in {"module_replay", "synthetic_end_to_end"}:
        raise EvaluationQueryError("requestData.executionKind is invalid")
    _version(data.get("requestVersion"), "requestData.requestVersion")
    _validate_component(data.get("targetComponent"), "requestData.targetComponent")
    runner_provenance = _closed_definition_object(
        data.get("provenance"),
        {
            "dataset", "suite", "evaluators", "components", "model",
            "deploymentRevision", "reproducibility",
        },
        "requestData.provenance",
    )
    for key in ("dataset", "suite"):
        reference = _closed_definition_object(runner_provenance.get(key), _REFERENCE_FIELDS, key)
        _uuid(reference.get("id"), f"requestData.provenance.{key}.id")
        _version(reference.get("version"), f"requestData.provenance.{key}.version")
    components = runner_provenance.get("components")
    if not isinstance(components, list) or not 1 <= len(components) <= 32:
        raise EvaluationQueryError("requestData.provenance.components is invalid")
    for index, component in enumerate(components):
        _validate_component(component, f"requestData.provenance.components[{index}]")
    evaluators = runner_provenance.get("evaluators")
    if not isinstance(evaluators, list) or not 1 <= len(evaluators) <= 32:
        raise EvaluationQueryError("requestData.provenance.evaluators is invalid")
    for index, raw_evaluator in enumerate(evaluators):
        evaluator = _closed_definition_object(raw_evaluator, _EVALUATOR_FIELDS, f"requestData.provenance.evaluators[{index}]")
        _uuid(evaluator.get("evaluatorId"), f"requestData.provenance.evaluators[{index}].evaluatorId")
        _version(evaluator.get("evaluatorVersion"), f"requestData.provenance.evaluators[{index}].evaluatorVersion")
        if evaluator.get("kind") not in _EVALUATOR_KINDS:
            raise EvaluationQueryError("requestData.provenance evaluator kind is invalid")
    model = _closed_definition_object(runner_provenance.get("model"), _MODEL_FIELDS, "requestData.provenance.model")
    _model_identifier(model.get("modelId"), "requestData.provenance.model.modelId")
    _version(model.get("modelVersion"), "requestData.provenance.model.modelVersion")
    if not isinstance(model.get("modelDigest"), str) or _DIGEST.fullmatch(cast(str, model["modelDigest"])) is None:
        raise EvaluationQueryError("requestData.provenance.model.modelDigest is invalid")
    _version(runner_provenance.get("deploymentRevision"), "requestData.provenance.deploymentRevision")
    reproducibility = _closed_definition_object(runner_provenance.get("reproducibility"), _REPRODUCIBILITY_FIELDS, "requestData.provenance.reproducibility")
    if type(reproducibility.get("seed")) is not int or not 0 <= cast(int, reproducibility["seed"]) <= 2_147_483_647:
        raise EvaluationQueryError("requestData.provenance.reproducibility.seed is invalid")
    if reproducibility.get("clockMode") not in {"fixed", "recorded"} or reproducibility.get("timeZone") != "UTC":
        raise EvaluationQueryError("requestData.provenance.reproducibility is invalid")
    locale = reproducibility.get("locale")
    if not isinstance(locale, str) or re.fullmatch(r"[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*", locale) is None:
        raise EvaluationQueryError("requestData.provenance.reproducibility.locale is invalid")
    for key in ("fixtureRevision", "promptRevision", "policyRevision"):
        _version(reproducibility.get(key), f"requestData.provenance.reproducibility.{key}")
    capture = _closed_definition_object(data.get("captureAuthorization"), _CAPTURE_AUTH_FIELDS, "requestData.captureAuthorization")
    if capture.get("authorizationPurpose") not in {"synthetic_benchmark", "owner_supplied_evaluation", "controlled_replay", "human_review"} or capture.get("authorizationContext") != "controlled_evaluation" or capture.get("retentionMode") != "manual_only":
        raise EvaluationQueryError("requestData.captureAuthorization is invalid")
    capture_classification = capture.get("captureClassification")
    if capture_classification not in _CAPTURE_CLASSES:
        raise EvaluationQueryError("requestData.captureAuthorization.captureClassification is invalid")
    if capture_classification == "metadata_only":
        if set(capture) != {"captureClassification", "authorizationPurpose", "authorizationContext", "retentionMode"}:
            raise EvaluationQueryError("requestData.captureAuthorization is invalid")
    else:
        if set(capture) != set(_CAPTURE_AUTH_FIELDS):
            raise EvaluationQueryError("requestData.captureAuthorization is invalid")
        _uuid(capture.get("authorizationRecordId"), "requestData.captureAuthorization.authorizationRecordId")
        _timestamp(capture.get("authorizedAt"), "requestData.captureAuthorization.authorizedAt")
    limits = _closed_definition_object(data.get("limits"), _RUNNER_LIMIT_FIELDS, "requestData.limits")
    if set(limits) != set(_RUNNER_LIMIT_FIELDS):
        raise EvaluationQueryError("requestData.limits is invalid")
    execution_limits = _closed_definition_object(limits.get("execution"), _EXECUTION_LIMIT_FIELDS, "requestData.limits.execution")
    if set(execution_limits) != set(_EXECUTION_LIMIT_FIELDS):
        raise EvaluationQueryError("requestData.limits.execution is invalid")
    minimums = {
        "timeoutMs": 100,
        "maxConcurrency": 1,
        "maxAttempts": 1,
        "maxCases": 1,
        "maxInputBytes": 0,
        "maxOutputBytes": 0,
        "maxTokens": 0,
    }
    maximums = {
        "timeoutMs": 86_400_000,
        "maxConcurrency": 64,
        "maxAttempts": 10,
        "maxCases": 100_000,
        "maxInputBytes": 1_073_741_824,
        "maxOutputBytes": 1_073_741_824,
        "maxTokens": 100_000_000,
    }
    for field, value in execution_limits.items():
        if (
            type(value) is not int
            or value < minimums[field]
            or value > maximums[field]
        ):
            raise EvaluationQueryError(f"requestData.limits.execution.{field} is invalid")
    for field, maximum in (("maxQueueTimeMs", 3_600_000), ("maxModelCalls", 10_000), ("maxArtifactOperations", 10_000)):
        value = limits.get(field)
        if type(value) is not int or not 0 <= value <= maximum:
            raise EvaluationQueryError(f"requestData.limits.{field} is invalid")
    _timestamp(data.get("deadlineAt"), "requestData.deadlineAt")
    case_ids = data.get("caseIds")
    if not isinstance(case_ids, list) or not 1 <= len(case_ids) <= 1000:
        raise EvaluationQueryError("requestData.caseIds is invalid")
    for index, case_id in enumerate(case_ids):
        _uuid(case_id, f"requestData.caseIds[{index}]")
    if len(set(case_ids)) != len(case_ids):
        raise EvaluationQueryError("requestData.caseIds must be unique")
    artifact_refs = data.get("inputArtifactReferences")
    if not isinstance(artifact_refs, list) or len(artifact_refs) > 32:
        raise EvaluationQueryError("requestData.inputArtifactReferences is invalid")
    for index, raw_ref in enumerate(artifact_refs):
        ref = _closed_definition_object(raw_ref, _ARTIFACT_REF_FIELDS, f"requestData.inputArtifactReferences[{index}]")
        if set(ref) != set(_ARTIFACT_REF_FIELDS):
            raise EvaluationQueryError("input artifact reference is invalid")
        _uuid(ref.get("artifactReferenceId"), f"inputArtifactReferences[{index}].artifactReferenceId")
        if ref.get("mediaClassification") not in {"evaluation_input", "evaluation_context", "evaluation_evidence"} or ref.get("mediaType") not in {"application/json", "application/octet-stream", "text/plain", "text/markdown", "image/png", "image/jpeg", "audio/wav"}:
            raise EvaluationQueryError("input artifact reference is invalid")
        byte_count = ref.get("byteCount")
        if type(byte_count) is not int or not 0 <= byte_count <= 1_073_741_824:
            raise EvaluationQueryError("input artifact byteCount is invalid")
        integrity = _closed_definition_object(ref.get("integrity"), _INTEGRITY_FIELDS, "inputArtifactReferences.integrity")
        if set(integrity) != set(_INTEGRITY_FIELDS) or integrity.get("algorithm") != "sha256" or not isinstance(integrity.get("digest"), str) or _DIGEST.fullmatch(cast(str, integrity["digest"])) is None:
            raise EvaluationQueryError("input artifact integrity is invalid")
        nested_capture = _closed_definition_object(
            ref.get("captureAuthorization"), _CAPTURE_AUTH_FIELDS,
            f"inputArtifactReferences[{index}].captureAuthorization",
        )
        if nested_capture.get("authorizationContext") != "controlled_evaluation" or nested_capture.get("retentionMode") != "manual_only" or nested_capture.get("captureClassification") not in _CAPTURE_CLASSES:
            raise EvaluationQueryError("input artifact capture authorization is invalid")
        if nested_capture.get("captureClassification") == "metadata_only":
            if set(nested_capture) != {"captureClassification", "authorizationPurpose", "authorizationContext", "retentionMode"}:
                raise EvaluationQueryError("input artifact capture authorization is invalid")
        else:
            if set(nested_capture) != set(_CAPTURE_AUTH_FIELDS):
                raise EvaluationQueryError("input artifact capture authorization is invalid")
            _uuid(nested_capture.get("authorizationRecordId"), "inputArtifactReferences.authorizationRecordId")
            _timestamp(nested_capture.get("authorizedAt"), "inputArtifactReferences.authorizedAt")


def _validate_deletion_request(body: Mapping[str, object]) -> None:
    kind = body.get("kind")
    fields = {
        "job": {"commandId", "requestedAt", "kind", "jobId"},
        "dataset": {"commandId", "requestedAt", "kind", "datasetId"},
        "suite": {"commandId", "requestedAt", "kind", "suiteId"},
        "cutoff": {"commandId", "requestedAt", "kind", "cutoff"},
        "purge_all": {"commandId", "requestedAt", "kind"},
    }.get(kind)
    if fields is None or set(body) != fields:
        raise EvaluationQueryError("deletion request is invalid")
    _uuid(body.get("commandId"), "commandId")
    _timestamp(body.get("requestedAt"), "requestedAt")
    if kind == "cutoff":
        _timestamp(body.get("cutoff"), "cutoff")
    elif kind in {"job", "dataset", "suite"}:
        field = {"job": "jobId", "dataset": "datasetId", "suite": "suiteId"}[kind]
        _uuid(body.get(field), field)


def _json_digest(body: Mapping[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _now() -> str:
    return utc_now().isoformat().replace("+00:00", "Z")


def _project_definition_replay(
    kind: str,
    replay: Mapping[str, object],
    resource_id: str,
    resource_version: str,
) -> dict[str, object]:
    """Reconstruct a durable replay through the closed response projector."""

    dataset_fields = frozenset(
        {
                "schemaVersion", "resourceType", "datasetId", "datasetVersion",
                "sourceKind", "scopeIds", "captureClassification", "caseCount",
                "retentionMode", "provenance", "state", "createdAt",
        }
    )
    suite_fields = frozenset(
        {
                "schemaVersion", "resourceType", "suiteId", "suiteVersion", "dataset",
                "evaluators", "componentIds", "caseBudget", "timeoutMs", "retentionMode",
                "provenance", "state", "createdAt",
        }
    )
    if kind == "dataset":
        expected_fields = dataset_fields
    elif kind == "suite":
        expected_fields = suite_fields
    else:
        raise EvaluationUnavailable("stored definition replay is unavailable")
    if set(replay.keys()) != set(expected_fields):
        raise EvaluationUnavailable("stored definition replay is unavailable")
    if replay.get("schemaVersion") != 1 or replay.get("resourceType") != kind:
        raise EvaluationUnavailable("stored definition replay is unavailable")
    identifier_key = "datasetId" if kind == "dataset" else "suiteId"
    version_key = "datasetVersion" if kind == "dataset" else "suiteVersion"
    try:
        if _uuid(replay.get(identifier_key), identifier_key) != resource_id:
            raise EvaluationUnavailable("stored definition replay binding is unavailable")
        if _version(replay.get(version_key), version_key) != resource_version:
            raise EvaluationUnavailable("stored definition replay binding is unavailable")
        state = replay.get("state")
        if state not in {"active", "disabled", "archived"}:
            raise EvaluationUnavailable("stored definition replay state is unavailable")
        internal = dict(replay)
        if kind == "dataset":
            internal["scopes"] = replay.get("scopeIds")
        else:
            internal["limits"] = {
                "maxCases": replay.get("caseBudget"),
                "timeoutMs": replay.get("timeoutMs"),
            }
        projected = project_definition(internal, state=cast(str, state))
    except (EvaluationQueryError, TypeError, ValueError) as exc:
        raise EvaluationUnavailable("stored definition replay is unavailable") from exc
    if projected != dict(replay):
        raise EvaluationUnavailable("stored definition replay is unavailable")
    return projected


def _timestamp(value: object, name: str) -> datetime:
    if not isinstance(value, str):
        raise EvaluationQueryError(f"{name} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EvaluationQueryError(f"{name} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise EvaluationQueryError(f"{name} is invalid")
    return parsed.astimezone(UTC)


def _validate_query_parameters(request: Request, allowed: frozenset[str]) -> None:
    """Reject unknown or repeated query keys before route business logic."""

    seen: set[str] = set()
    for key, _value in request.query_params.multi_items():
        if key not in allowed:
            raise EvaluationQueryError("query parameter is invalid")
        if key in seen:
            raise EvaluationQueryError("query parameter is repeated")
        seen.add(key)


def _project_runner_provenance(value: object) -> dict[str, object]:
    """Reconstruct the closed runner provenance response shape."""

    item = _closed_definition_object(
        value,
        frozenset(
            {"dataset", "suite", "evaluators", "components", "model", "deploymentRevision", "reproducibility"}
        ),
        "provenance",
    )
    result: dict[str, object] = {}
    for key in ("dataset", "suite"):
        reference = _closed_definition_object(item.get(key), _REFERENCE_FIELDS, key)
        result[key] = {
            "id": _uuid(reference.get("id"), f"provenance.{key}.id"),
            "version": _version(reference.get("version"), f"provenance.{key}.version"),
        }
    evaluators_value = item.get("evaluators")
    if not isinstance(evaluators_value, list) or not 1 <= len(evaluators_value) <= 32:
        raise EvaluationQueryError("provenance.evaluators is unavailable")
    evaluators: list[dict[str, object]] = []
    for index, value_item in enumerate(evaluators_value):
        evaluator = _closed_definition_object(value_item, _EVALUATOR_FIELDS, f"provenance.evaluators[{index}]")
        evaluators.append(
            {
                "evaluatorId": _uuid(evaluator.get("evaluatorId"), "provenance.evaluatorId"),
                "evaluatorVersion": _version(evaluator.get("evaluatorVersion"), "provenance.evaluatorVersion"),
                "kind": evaluator.get("kind"),
            }
        )
        if evaluators[-1]["kind"] not in _EVALUATOR_KINDS:
            raise EvaluationQueryError("provenance.evaluator.kind is unavailable")
    result["evaluators"] = evaluators
    components_value = item.get("components")
    if not isinstance(components_value, list) or not 1 <= len(components_value) <= 32:
        raise EvaluationQueryError("provenance.components is unavailable")
    result["components"] = [
        _validate_component(component, f"provenance.components[{index}]")
        for index, component in enumerate(components_value)
    ]
    model = _closed_definition_object(item.get("model"), _MODEL_FIELDS, "provenance.model")
    result["model"] = {
        "modelId": _model_identifier(model.get("modelId"), "provenance.model.modelId"),
        "modelVersion": _version(model.get("modelVersion"), "provenance.model.modelVersion"),
        "modelDigest": cast(str, model.get("modelDigest")),
    }
    if not _DIGEST.fullmatch(cast(str, model.get("modelDigest"))) :
        raise EvaluationQueryError("provenance.model.modelDigest is unavailable")
    result["deploymentRevision"] = _version(item.get("deploymentRevision"), "provenance.deploymentRevision")
    reproducibility = _closed_definition_object(
        item.get("reproducibility"), _REPRODUCIBILITY_FIELDS, "provenance.reproducibility"
    )
    if type(reproducibility.get("seed")) is not int or not 0 <= cast(int, reproducibility["seed"]) <= 2_147_483_647:
        raise EvaluationQueryError("provenance.reproducibility.seed is unavailable")
    if reproducibility.get("clockMode") not in {"fixed", "recorded"} or reproducibility.get("timeZone") != "UTC":
        raise EvaluationQueryError("provenance.reproducibility is unavailable")
    locale = reproducibility.get("locale")
    if not isinstance(locale, str) or re.fullmatch(r"[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*", locale) is None:
        raise EvaluationQueryError("provenance.reproducibility.locale is unavailable")
    result["reproducibility"] = {
        "seed": reproducibility["seed"],
        "clockMode": reproducibility["clockMode"],
        "timeZone": "UTC",
        "locale": locale,
        "fixtureRevision": _version(reproducibility.get("fixtureRevision"), "provenance.fixtureRevision"),
        "promptRevision": _version(reproducibility.get("promptRevision"), "provenance.promptRevision"),
        "policyRevision": _version(reproducibility.get("policyRevision"), "provenance.policyRevision"),
    }
    return result


def _job_projection(job: Any) -> dict[str, object]:
    admission = job.admission
    data = admission.envelope.get("data")
    if not isinstance(data, Mapping):
        # Admission currently persists the runner envelope under ``data``;
        # that execution provenance is intentionally a different contract
        # from the job summary provenance.  Do not hash or otherwise derive a
        # summary value here.  The durable adapter must expose an explicitly
        # persisted job projection provenance before this response is valid.
        raise EvaluationUnavailable("stored job provenance is unavailable")
    try:
        job_provenance = _project_runner_provenance(data.get("provenance"))
    except EvaluationQueryError as exc:
        raise EvaluationUnavailable("stored job provenance is unavailable") from exc
    result_count = getattr(job, "result_count", None)
    artifact_count = getattr(job, "artifact_count", None)
    if type(result_count) is not int or type(artifact_count) is not int:
        raise EvaluationUnavailable("stored job counts are unavailable")
    return {
        "schemaVersion": 1,
        "resourceType": "job",
        "jobId": str(job.job_id),
        "suite": resource_ref(admission.suite_id, admission.suite_version),
        "status": str(job.state.value if hasattr(job.state, "value") else job.state),
        "captureClassification": admission.capture_classification,
        "attemptCount": int(job.attempt_count),
        "resultCount": result_count,
        "artifactCount": artifact_count,
        "deadlineAt": admission.deadline_at.isoformat().replace("+00:00", "Z"),
        "failureCode": None,
        "provenance": job_provenance,
        "createdAt": job.created_at.isoformat().replace("+00:00", "Z"),
        "updatedAt": job.updated_at.isoformat().replace("+00:00", "Z"),
    }


def _review_projection(review: HumanReview) -> dict[str, object]:
    """Project the closed observatory-v1 review response explicitly."""

    review.validate(strict=True)
    if review.provenance_component is not None and review.provenance_version is not None and review.provenance_digest is not None:
        review_provenance = {
            "createdByComponent": review.provenance_component,
            "createdByVersion": review.provenance_version,
            "sourceDigest": review.provenance_digest,
        }
    else:
        raise EvaluationUnavailable("stored review provenance is unavailable")
    return {
        "schemaVersion": 1,
        "resourceType": "human_review",
        "reviewId": review.review_id,
        "reviewVersion": review.review_version,
        "calibration": {
            "resourceId": review.calibration_id,
            "resourceVersion": review.calibration_version,
        },
        "caseId": review.case_id,
        "resultId": review.result_id,
        "scoreId": review.score_id,
        "reviewScore": review.score,
        "reviewOutcome": review.outcome.value if review.outcome else None,
        "reviewLabel": review.review_label,
        "disagreement": review.disagreement.value,
        "authorizationRecordId": review.authorization_record_id,
        "commentArtifactId": review.comment_artifact_reference_id,
        "provenance": review_provenance,
        "createdAt": review.created_at.isoformat().replace("+00:00", "Z"),
    }


def _qualification_projection(qualification: Qualification) -> dict[str, object]:
    """Project only the public qualification fields; never pass domain mappings through."""

    qualification.validate(strict=True)
    return {
        "schemaVersion": 1,
        "resourceType": "judge_qualification",
        "qualificationId": qualification.qualification_id,
        "qualificationVersion": qualification.qualification_version,
        "state": qualification.state,
        "dataset": {
            "resourceId": qualification.dataset_id,
            "resourceVersion": qualification.dataset_version,
        },
        "suite": {
            "resourceId": qualification.suite_id,
            "resourceVersion": qualification.suite_version,
        },
        "evaluator": {
            "evaluatorId": qualification.evaluator_id,
            "evaluatorVersion": qualification.evaluator_version,
            "kind": "local_judge",
        },
        "judgeDefinition": {
            "resourceId": qualification.judge_id,
            "resourceVersion": qualification.judge_version,
        },
        "calibration": {
            "resourceId": qualification.calibration_id,
            "resourceVersion": qualification.calibration_version,
        },
        "calibrationResultId": qualification.calibration_result_id,
        "authorizationRecordId": qualification.authorization_record_id,
        "scoreDisposition": qualification.score_disposition,
        "provenance": provenance(
            source={
                "qualificationId": qualification.qualification_id,
                "qualificationVersion": qualification.qualification_version,
            },
            component="aura.observatory.calibration",
            version=qualification.qualification_version,
        ),
        "createdAt": qualification.created_at.isoformat().replace("+00:00", "Z"),
    }


class EvaluationApplicationService:
    """Command façade over existing domain services plus a bounded read model."""

    def __init__(
        self,
        *,
        query: EvaluationQueryService | None = None,
        definition_service: Any | None = None,
        orchestration: Any | None = None,
        calibration_service: Any | None = None,
        deletion_service: Any | None = None,
        live: EvaluationLiveEventPort | None = None,
        metrics: Any | None = None,
    ) -> None:
        self.query = query or EvaluationQueryService()
        self.definition_service = definition_service
        self.orchestration = orchestration
        self.calibration_service = calibration_service
        self.deletion_service = deletion_service
        self.metrics = metrics
        if live is None and self.query.backend is not None:
            raise EvaluationQueryError("durable evaluation live-event port is required")
        self.live: EvaluationLiveEventPort = live or EvaluationLiveEventStore()

    async def _publish(
        self, resource_type: str, resource_id: object, change: str, state: object
    ) -> None:
        started = monotonic()
        try:
            await self.live.publish(resource_type, str(resource_id), change, str(state))
        except Exception:
            self._record_span(
                operation="live_publish",
                started=started,
                outcome="failure",
                dependency="evaluation_live",
                error_code="metadata_storage_failure",
                component_id="aura.observatory.live_updates",
            )
            self._record_metric(
                "observatory_evaluation_live_event_count",
                labels={"operation": "publish", "outcome": "failure"},
            )
            # Evaluation state remains authoritative even when notifications
            # are temporarily unavailable; the next REST snapshot reconciles.
            return
        self._record_span(
            operation="live_publish",
            started=started,
            outcome="success",
            dependency="evaluation_live",
            component_id="aura.observatory.live_updates",
        )
        self._record_metric(
            "observatory_evaluation_live_event_count",
            labels={"operation": "publish", "outcome": "success"},
        )

    def _record_metric(
        self, name: str, *, value: float = 1.0, labels: Mapping[str, str] | None = None
    ) -> None:
        """Keep observability bounded and independent from request outcomes."""

        recorder = getattr(self.metrics, "record", None)
        if not callable(recorder):
            return
        try:
            recorder(name, value, labels or {})
        except Exception:
            # A telemetry sink must never change the API's authorization or
            # provider-failure behavior.
            return

    def _record_span(
        self,
        *,
        operation: str,
        started: float,
        outcome: str,
        dependency: str | None = None,
        error_code: str | None = None,
        component_id: str = "aura.observatory.evaluation_api",
    ) -> None:
        recorder = getattr(self.metrics, "record_component_span", None)
        if not callable(recorder):
            return
        try:
            recorder(
                component_id=component_id,
                component_version="1.0.0",
                deployment_id="observatory-api",
                operation=operation,
                duration_ms=(monotonic() - started) * 1000.0,
                outcome=outcome,
                dependency=dependency,
                error_code=error_code,
            )
        except Exception:
            return

    def record_api_metric(self, outcome: str, *, operation: str = "read") -> None:
        self._record_metric(
            "observatory_evaluation_api_operation_count",
            labels={"operation": operation, "outcome": outcome},
        )

    def record_definition_command_ledger(
        self,
        started: float,
        outcome: str,
        *,
        phase: str = "definition_completion",
        error_code: str | None = None,
    ) -> None:
        """Record one bounded durable definition-command ledger operation."""

        labels: dict[str, str] = {
            "operation": "evaluation_definition_state",
            "phase": phase,
            "outcome": outcome,
        }
        if error_code is not None:
            labels["failure_code"] = error_code
        self._record_metric(
            "observatory_evaluation_dependency_outcome_count",
            labels=labels,
        )
        self._record_span(
            operation=phase,
            started=started,
            outcome=outcome,
            dependency="postgresql",
            error_code=error_code,
            component_id="aura.observatory.evaluation_definitions",
        )

    def record_binding_validation(
        self,
        outcome: str,
        *,
        started: float | None = None,
        error_code: str | None = None,
    ) -> None:
        """Record one closed job-binding validation outcome."""

        labels: dict[str, str] = {"resource_type": "job", "outcome": outcome}
        if error_code is not None:
            labels["failure_code"] = error_code
        self._record_metric(
            "observatory_evaluation_definition_validation_count", labels=labels
        )
        span_outcome = {
            "accepted": "success",
            "invalid": "failure",
            "not_found": "failure",
            "unavailable": "unavailable",
        }.get(outcome, "failure")
        self._record_span(
            operation="definition_binding_validation",
            started=monotonic() if started is None else started,
            outcome=span_outcome,
            dependency="postgresql",
            error_code=error_code,
            component_id="aura.observatory.evaluation_definitions",
        )

    def record_live_metric(self, operation: str, outcome: str) -> None:
        self._record_metric(
            "observatory_evaluation_live_event_count",
            labels={"operation": operation, "outcome": outcome},
        )

    def record_live_span(
        self,
        operation: str,
        started: float,
        outcome: str,
        *,
        error_code: str | None = None,
    ) -> None:
        self._record_span(
            operation=operation,
            started=started,
            outcome=outcome,
            dependency="evaluation_live",
            error_code=error_code,
            component_id="aura.observatory.live_updates",
        )

    def record_live_replay_metric(self, occurred_at: str) -> None:
        self.record_live_metric("replay", "success")
        try:
            occurred = datetime.fromisoformat(occurred_at.replace("Z", "+00:00"))
            if occurred.tzinfo is None:
                return
            lag = max(0.0, (datetime.now(UTC) - occurred.astimezone(UTC)).total_seconds())
        except (TypeError, ValueError):
            return
        self._record_metric("observatory_evaluation_live_replay_lag_seconds", value=lag)

    async def list_collection(
        self,
        kind: str,
        *,
        cursor: str | None = None,
        limit: int = 50,
        filters: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        """Read one bounded collection through the public application port."""

        started = monotonic()
        try:
            result = await self.query.collection_async(
                kind,
                cursor=cursor,
                limit=limit,
                filters=filters,
            )
        except EvaluationUnavailable:
            self._record_span(
                operation="collection", started=started, outcome="unavailable",
                dependency="postgresql", error_code="provider_unavailable"
            )
            raise
        except Exception:
            self._record_span(
                operation="collection", started=started, outcome="failure",
                dependency="postgresql", error_code="database_failure"
            )
            raise
        self._record_span(
            operation="collection", started=started, outcome="success", dependency="postgresql"
        )
        return result

    async def list_history(
        self,
        *,
        cursor: str | None = None,
        limit: int = 50,
        resource_type: str | None = None,
        resource_id: str | None = None,
    ) -> dict[str, object]:
        """Read content-free evaluation history through the public port."""

        started = monotonic()
        try:
            result = await self.query.history_page_async(
                cursor=cursor,
                limit=limit,
                resource_type=resource_type,
                resource_id=resource_id,
            )
        except EvaluationUnavailable:
            self.record_api_metric("unavailable")
            self._record_span(
                operation="history", started=started, outcome="unavailable",
                dependency="postgresql", error_code="provider_unavailable"
            )
            raise
        except Exception:
            self.record_api_metric("failure")
            self._record_span(
                operation="history", started=started, outcome="failure",
                dependency="postgresql", error_code="database_failure"
            )
            raise
        self._record_span(
            operation="history", started=started, outcome="success", dependency="postgresql"
        )
        self.record_api_metric("success")
        return result

    async def get_resource(
        self, kind: str, identifier: str, version: str | None = None
    ) -> dict[str, object] | None:
        """Read one resource through the instrumented application boundary."""

        started = monotonic()
        try:
            result = await self.query.get_async(kind, identifier, version)
        except EvaluationUnavailable:
            self.record_api_metric("unavailable")
            self._record_span(
                operation="query", started=started, outcome="unavailable",
                dependency="postgresql", error_code="provider_unavailable"
            )
            raise
        except Exception:
            self.record_api_metric("failure")
            self._record_span(
                operation="query", started=started, outcome="failure",
                dependency="postgresql", error_code="database_failure"
            )
            raise
        self._record_span(
            operation="query", started=started, outcome="success", dependency="postgresql"
        )
        self.record_api_metric("success")
        return result

    async def _renew_definition_claim_if_needed(
        self,
        operation: str,
        idempotency_key: str | None,
        request_fingerprint: str,
        claim_token: str | None,
    ) -> None:
        if (
            self.query.backend is None
            or idempotency_key is None
            or claim_token is None
            or self.definition_service is None
        ):
            return
        started = monotonic()
        try:
            await self.definition_service.renew_definition_command(
                operation, idempotency_key, request_fingerprint, claim_token
            )
        except DefinitionCommandPendingError as exc:
            self.record_definition_command_ledger(
                started,
                "unavailable",
                phase="definition_renewal",
                error_code="lease_lost",
            )
            raise EvaluationUnavailable("definition command lease is unavailable") from exc
        except DefinitionStorageError as exc:
            self.record_definition_command_ledger(
                started,
                "unavailable",
                phase="definition_renewal",
                error_code="database_failure",
            )
            raise EvaluationUnavailable("definition command lease is unavailable") from exc
        self.record_definition_command_ledger(
            started,
            "success",
            phase="definition_renewal",
        )

    async def create_definition(
        self, kind: str, body: Mapping[str, object], *, idempotency_key: str | None = None
    ) -> dict[str, object]:
        if kind not in {"dataset", "suite"}:
            raise EvaluationQueryError("definition operation is invalid")
        request_digest = _json_digest(body)
        if idempotency_key is not None and self.query.backend is None:
            prior = self.query.store.idempotency.get(idempotency_key)
            if prior is not None:
                if prior[1] != request_digest:
                    raise EvaluationConflict("idempotency key conflicts with existing state")
                return dict(prior[2])
        # Reject the complete request tree before domain admission.  This is
        # deliberately after replay lookup so an established idempotency key
        # retains its conflict/replay semantics even if a retried payload has
        # an invalid value; no domain service is reached in either case.
        _validate_definition_request(kind, body)
        durable_definition_command = False
        definition_claim_token: str | None = None
        if self.query.backend is not None and idempotency_key is not None:
            if self.definition_service is None:
                raise EvaluationUnavailable("definition service unavailable")
            command_resource_id = _uuid(
                body.get("datasetId" if kind == "dataset" else "suiteId"),
                "datasetId" if kind == "dataset" else "suiteId",
            )
            command_resource_version = _version(
                body.get("datasetVersion" if kind == "dataset" else "suiteVersion"),
                "datasetVersion" if kind == "dataset" else "suiteVersion",
            )
            ledger_started = monotonic()
            try:
                replay = await self.definition_service.begin_definition_command(
                    f"create_{kind}",
                    idempotency_key,
                    request_digest,
                    command_resource_id,
                    command_resource_version,
                )
            except DefinitionConflictError as exc:
                self.record_definition_command_ledger(
                    ledger_started,
                    "failure",
                    phase="definition_claim",
                    error_code="conflict",
                )
                raise EvaluationConflict(
                    "idempotency key conflicts with existing state"
                ) from exc
            except DefinitionCommandPendingError as exc:
                self.record_definition_command_ledger(
                    ledger_started,
                    "unavailable",
                    phase="definition_claim",
                    error_code="conflict",
                )
                raise EvaluationUnavailable("definition command is in progress") from exc
            except DefinitionStorageError as exc:
                self.record_definition_command_ledger(
                    ledger_started,
                    "unavailable",
                    phase="definition_claim",
                    error_code="database_failure",
                )
                raise EvaluationUnavailable("definition command storage is unavailable") from exc
            if replay.replay is not None:
                projected_replay = _project_definition_replay(
                    kind,
                    replay.replay,
                    command_resource_id,
                    command_resource_version,
                )
                self.record_definition_command_ledger(
                    ledger_started,
                    "duplicate",
                    phase="definition_replay",
                )
                return projected_replay
            if not replay.claimed:
                self.record_definition_command_ledger(
                    ledger_started,
                    "unavailable",
                    phase="definition_claim",
                    error_code="conflict",
                )
                raise EvaluationUnavailable("definition command is in progress")
            definition_claim_token = replay.claim_token
            if definition_claim_token is None:
                raise EvaluationUnavailable("definition command claim is unavailable")
            self.record_definition_command_ledger(
                ledger_started,
                "success",
                phase=replay.claim_phase,
            )
            durable_definition_command = True
        self.record_api_metric("accepted", operation="write")
        now = _now()
        if kind == "dataset":
            dataset_id = _uuid(body.get("datasetId"), "datasetId")
            version = _version(body.get("datasetVersion"), "datasetVersion")
            cases = body.get("cases")
            if not isinstance(cases, list) or not 1 <= len(cases) <= 10_000:
                raise EvaluationQueryError("cases are invalid")
            case_ids: list[str] = []
            for item in cases:
                if not isinstance(item, Mapping):
                    raise EvaluationQueryError("case is invalid")
                case_ids.append(_uuid(item.get("caseId"), "caseId"))
            payload: dict[str, object] = {
                "schemaVersion": 1,
                "resourceType": "dataset",
                "datasetId": dataset_id,
                "datasetVersion": version,
                "sourceKind": body.get("sourceKind"),
                "scopes": body.get("scopeIds"),
                "caseIds": case_ids,
                "caseCount": len(case_ids),
                "captureAuthorization": {
                    "captureClassification": body.get("captureClassification"),
                    "authorizationPurpose": "owner_supplied_evaluation",
                    "authorizationContext": "controlled_evaluation",
                    "retentionMode": "manual_only",
                },
                "retentionMode": "manual_only",
                "provenance": provenance(source=body),
                "createdAt": now,
            }
            if self.definition_service is not None:
                admitted = await self.definition_service.admit_dataset(payload)
                payload = cast(dict[str, object], admitted.to_payload())
            if self.query.backend is not None:
                # Production writes go through the durable definition port;
                # never mirror them into the reference read model.
                if self.definition_service is None:
                    raise EvaluationQueryError("definition service unavailable")
                for item in cases:
                    await self._renew_definition_claim_if_needed(
                        "create_dataset",
                        idempotency_key,
                        request_digest,
                        definition_claim_token,
                    )
                    case_body = dict(cast(Mapping[str, object], item))
                    case_payload = {
                        "schemaVersion": 1,
                        "resourceType": "case",
                        "caseId": _uuid(case_body["caseId"], "caseId"),
                        "caseVersion": _version(
                            case_body.get("caseVersion", version), "caseVersion"
                        ),
                        "dataset": resource_ref(dataset_id, version),
                        "sourceKind": body.get("sourceKind"),
                        "scopes": body.get("scopeIds"),
                        "inputArtifactIds": list(case_body.get("artifactIds", [])),
                        "captureAuthorization": payload["captureAuthorization"],
                        "retentionMode": "manual_only",
                        "provenance": provenance(source=case_body),
                        "createdAt": now,
                    }
                    admitted_case = await self.definition_service.admit_case(case_payload)
                    await self._publish(
                        "evaluation_case", admitted_case.resource_id, "created", "active"
                    )
                await self._renew_definition_claim_if_needed(
                    "create_dataset", idempotency_key, request_digest, definition_claim_token
                )
                result = project_definition(payload)
                if durable_definition_command and idempotency_key is not None:
                    if definition_claim_token is None:
                        raise EvaluationUnavailable("definition command claim is unavailable")
                    completion_started = monotonic()
                    try:
                        result = await self.definition_service.complete_definition_command(
                            "create_dataset", idempotency_key, request_digest, result,
                            definition_claim_token,
                        )
                    except DefinitionConflictError as exc:
                        self.record_definition_command_ledger(
                            completion_started,
                            "failure",
                            phase="definition_completion",
                            error_code="conflict",
                        )
                        raise EvaluationConflict(
                            "idempotency key conflicts with existing state"
                        ) from exc
                    except DefinitionCommandPendingError as exc:
                        self.record_definition_command_ledger(
                            completion_started,
                            "unavailable",
                            phase="definition_completion",
                            error_code="lease_lost",
                        )
                        raise EvaluationUnavailable("definition command is in progress") from exc
                    except DefinitionStorageError as exc:
                        self.record_definition_command_ledger(
                            completion_started,
                            "unavailable",
                            phase="definition_completion",
                            error_code="database_failure",
                        )
                        raise EvaluationUnavailable(
                            "definition command storage is unavailable"
                        ) from exc
                    self.record_definition_command_ledger(
                        completion_started,
                        "success",
                        phase="definition_completion",
                    )
                await self._publish("evaluation_dataset", dataset_id, "created", "active")
                return result
            result = project_definition(payload)
            stored = self.query.store.put("datasets", result)
            if idempotency_key is not None and self.query.backend is None:
                self.query.store.idempotency[idempotency_key] = (
                    dataset_id,
                    request_digest,
                    stored,
                )
            await self._publish("evaluation_dataset", dataset_id, "created", "active")
            for item in cases:
                case_body = dict(cast(Mapping[str, object], item))
                case_id = _uuid(case_body["caseId"], "caseId")
                case_version = _version(case_body.get("caseVersion", version), "caseVersion")
                case_payload = {
                    "schemaVersion": 1,
                    "resourceType": "case",
                    "caseId": case_id,
                    "caseVersion": case_version,
                    "dataset": resource_ref(dataset_id, version),
                    "sourceKind": body.get("sourceKind"),
                    "scopes": body.get("scopeIds"),
                    "inputArtifactIds": list(case_body.get("artifactIds", [])),
                    "captureClassification": body.get("captureClassification"),
                    "retentionMode": "manual_only",
                    "provenance": provenance(source=case_body),
                    "createdAt": now,
                }
                case_stored = self.query.store.put("cases", project_definition(case_payload))
                await self._publish(
                    "evaluation_case", case_stored.get("caseId", case_id), "created", "active"
                )
            return stored
        suite_id = _uuid(body.get("suiteId"), "suiteId")
        version = _version(body.get("suiteVersion"), "suiteVersion")
        dataset = body.get("dataset")
        if not isinstance(dataset, Mapping):
            raise EvaluationQueryError("dataset is invalid")
        dataset_id = _uuid(dataset.get("id"), "dataset.id")
        dataset_version = _version(dataset.get("version"), "dataset.version")
        evaluators = body.get("evaluators")
        components = body.get("components")
        if (
            not isinstance(evaluators, list)
            or not 1 <= len(evaluators) <= 32
            or not isinstance(components, list)
            or not 1 <= len(components) <= 32
        ):
            raise EvaluationQueryError("suite bindings are invalid")
        evaluator_projection = [
            {
                "evaluatorId": _uuid(item.get("evaluatorId"), "evaluatorId"),
                "evaluatorVersion": _version(item.get("evaluatorVersion"), "evaluatorVersion"),
                "kind": item.get("kind"),
            }
            for item in (cast(Mapping[str, object], value) for value in evaluators)
        ]
        thresholds_payload = body.get("manualThresholds", [])
        if isinstance(thresholds_payload, Mapping):
            # The transport shorthand is a closed metric-to-value map.  The
            # domain port receives its richer immutable threshold shape with
            # deterministic semantics and the first bound evaluator.
            default_evaluator = evaluator_projection[0]
            thresholds_payload = [
                {
                    "evaluator": default_evaluator,
                    "metric": metric,
                    "aggregation": "mean",
                    "operator": "greater_than_or_equal",
                    "value": value,
                    "unit": "ratio",
                    "minimumSampleSize": 1,
                    "missingEvidencePolicy": "inconclusive",
                }
                for metric, value in thresholds_payload.items()
            ]
        payload = {
            "schemaVersion": 1,
            "resourceType": "suite",
            "suiteId": suite_id,
            "suiteVersion": version,
            "dataset": resource_ref(dataset_id, dataset_version),
            "evaluators": evaluator_projection,
            "components": [dict(cast(Mapping[str, object], item)) for item in components],
            "limits": dict(cast(Mapping[str, object], body.get("limits", {}))),
            "manualThresholds": thresholds_payload,
            "captureAuthorization": {
                "captureClassification": "metadata_only",
                "authorizationPurpose": "synthetic_benchmark",
                "authorizationContext": "controlled_evaluation",
                "retentionMode": "manual_only",
            },
            "retentionMode": "manual_only",
            "provenance": body.get("provenance", provenance(source=body)),
            "createdAt": body.get("createdAt", now),
        }
        if self.query.backend is not None:
            if self.definition_service is None:
                raise EvaluationQueryError("definition service unavailable")
            admitted = await self.definition_service.admit_suite(payload)
            payload = cast(dict[str, object], admitted.to_payload())
        result = project_definition(payload)
        if self.query.backend is not None:
            # Durable definition admission is authoritative in production;
            # never mirror a suite into the in-memory reference store.
            assert self.definition_service is not None
            await self._renew_definition_claim_if_needed(
                "create_suite",
                idempotency_key,
                request_digest,
                definition_claim_token,
            )
            if durable_definition_command and idempotency_key is not None:
                if definition_claim_token is None:
                    raise EvaluationUnavailable("definition command claim is unavailable")
                completion_started = monotonic()
                try:
                    result = await self.definition_service.complete_definition_command(
                        "create_suite", idempotency_key, request_digest, result,
                        definition_claim_token,
                    )
                except DefinitionConflictError as exc:
                    self.record_definition_command_ledger(
                        completion_started,
                        "failure",
                        phase="definition_completion",
                        error_code="conflict",
                    )
                    raise EvaluationConflict(
                        "idempotency key conflicts with existing state"
                    ) from exc
                except DefinitionCommandPendingError as exc:
                    self.record_definition_command_ledger(
                        completion_started,
                        "unavailable",
                        phase="definition_completion",
                        error_code="lease_lost",
                    )
                    raise EvaluationUnavailable("definition command is in progress") from exc
                except DefinitionStorageError as exc:
                    self.record_definition_command_ledger(
                        completion_started,
                        "unavailable",
                        phase="definition_completion",
                        error_code="database_failure",
                    )
                    raise EvaluationUnavailable(
                        "definition command storage is unavailable"
                    ) from exc
                self.record_definition_command_ledger(
                    completion_started,
                    "success",
                    phase="definition_completion",
                )
            await self._publish("evaluation_suite", suite_id, "created", "active")
            return result
        stored = self.query.store.put("suites", result)
        if idempotency_key is not None:
            self.query.store.idempotency[idempotency_key] = (suite_id, request_digest, stored)
        await self._publish("evaluation_suite", suite_id, "created", "active")
        return stored

    async def submit_job(
        self, body: Mapping[str, object], idempotency_key: str
    ) -> dict[str, object]:
        if self.orchestration is None:
            raise EvaluationQueryError("orchestration service unavailable")
        request_id = _uuid(body.get("requestId"), "requestId")
        request_version = _version(body.get("requestVersion"), "requestVersion")
        dataset = body.get("dataset")
        suite = body.get("suite")
        evaluator = body.get("evaluator")
        if (
            not isinstance(dataset, Mapping)
            or not isinstance(suite, Mapping)
            or not isinstance(evaluator, Mapping)
        ):
            raise EvaluationQueryError("definition reference is invalid")
        dataset_id = _uuid(dataset.get("id"), "dataset.id")
        dataset_version = _version(dataset.get("version"), "dataset.version")
        suite_id = _uuid(suite.get("id"), "suite.id")
        suite_version = _version(suite.get("version"), "suite.version")
        evaluator_id = _uuid(evaluator.get("id"), "evaluator.id")
        evaluator_version = _version(evaluator.get("version"), "evaluator.version")
        capture = body.get("captureClassification")
        if capture not in {
            "metadata_only",
            "hashed",
            "redacted",
            "sampled_content",
            "full_content",
        }:
            raise EvaluationQueryError("captureClassification is invalid")
        request_data = body.get("requestData")
        if not isinstance(request_data, Mapping):
            raise EvaluationQueryError("requestData is invalid")
        binding_started = monotonic()
        if self.query.backend is not None:
            if self.definition_service is None:
                self.record_binding_validation(
                    "unavailable",
                    started=binding_started,
                    error_code="provider_unavailable",
                )
                raise EvaluationUnavailable("definition service unavailable")
            runner_provenance = request_data.get("provenance")
            target_component = request_data.get("targetComponent")
            case_ids = request_data.get("caseIds")
            limits = request_data.get("limits")
            execution_limits = limits.get("execution") if isinstance(limits, Mapping) else None
            raw_max_attempts = body.get("maxAttempts")
            if (
                not isinstance(runner_provenance, Mapping)
                or not isinstance(target_component, Mapping)
                or not isinstance(case_ids, list)
                or not all(isinstance(case_id, str) for case_id in case_ids)
                or not isinstance(execution_limits, Mapping)
                or not isinstance(raw_max_attempts, int)
                or isinstance(raw_max_attempts, bool)
            ):
                self.record_binding_validation(
                    "invalid",
                    started=binding_started,
                    error_code="validation",
                )
                raise EvaluationQueryError("runner bindings are invalid")
            try:
                await self.definition_service.validate_execution_bindings(
                    dataset_id=dataset_id,
                    dataset_version=dataset_version,
                    suite_id=suite_id,
                    suite_version=suite_version,
                    evaluator_id=evaluator_id,
                    evaluator_version=evaluator_version,
                    provenance=runner_provenance,
                    target_component=target_component,
                    case_ids=cast(Sequence[str], case_ids),
                    execution_limits=cast(Mapping[str, object], execution_limits),
                    max_attempts=raw_max_attempts,
                )
            except DefinitionStorageError as exc:
                self.record_binding_validation(
                    "unavailable",
                    started=binding_started,
                    error_code="database_failure",
                )
                raise EvaluationUnavailable("definition storage is unavailable") from exc
            except DefinitionNotFoundError as exc:
                self.record_binding_validation(
                    "not_found",
                    started=binding_started,
                    error_code="not_found",
                )
                raise EvaluationQueryError("evaluation bindings are invalid") from exc
            except DefinitionValidationError as exc:
                self.record_binding_validation(
                    "invalid",
                    started=binding_started,
                    error_code="validation",
                )
                raise EvaluationQueryError("evaluation bindings are invalid") from exc
            self.record_binding_validation("accepted", started=binding_started)
        admission = EvaluationAdmission(
            request_id=request_id,
            idempotency_key=idempotency_key,
            request_version=request_version,
            dataset_id=dataset_id,
            dataset_version=dataset_version,
            suite_id=suite_id,
            suite_version=suite_version,
            evaluator_id=evaluator_id,
            evaluator_version=evaluator_version,
            capture_classification=str(capture),
            deadline_at=_timestamp(body.get("deadlineAt"), "deadlineAt"),
            max_attempts=int(body.get("maxAttempts", 0)),
            envelope={"data": dict(request_data)},
            created_at=_timestamp(body.get("createdAt"), "createdAt"),
        )
        try:
            job = await self.orchestration.admit(admission)
        except OrchestrationError as exc:
            if exc.code == "idempotency_conflict":
                raise EvaluationConflict("idempotency key conflicts with existing state") from exc
            if exc.code in {"database_failure", "storage_failure", "transport_unavailable"}:
                raise EvaluationUnavailable("evaluation orchestration is unavailable") from exc
            raise EvaluationQueryError("evaluation admission rejected") from exc
        self.record_api_metric("success", operation="write")
        if self.query.backend is not None:
            # The admission response is read back through the authoritative
            # projection so counts, timestamps, and runner provenance come
            # from the committed PostgreSQL row rather than the in-process
            # admission object.
            projected = await self.query.get_async("jobs", job.job_id)
            if cast(object, projected) is None:
                raise EvaluationUnavailable("admitted job projection is unavailable")
            result = projected
        else:
            result = _job_projection(job)
        # Durable orchestration publishes the committed transition through the
        # shared PostgreSQL live-event boundary.  The API must not duplicate it.
        return result

    async def cancel_job(
        self, job_id: str, *, idempotency_key: str, reason: str = "OWNER_REQUESTED"
    ) -> dict[str, object]:
        if self.orchestration is not None and self.query.backend is not None:
            try:
                accepted = await self.orchestration.cancel(
                    job_id, reason=reason, idempotency_key=idempotency_key
                )
            except OrchestrationError as exc:
                if exc.code == "idempotency_conflict":
                    raise EvaluationConflict(
                        "idempotency key conflicts with existing state"
                    ) from exc
                if exc.code in {"database_failure", "storage_failure", "transport_unavailable"}:
                    raise EvaluationUnavailable("evaluation orchestration is unavailable") from exc
                raise EvaluationQueryError("cancellation rejected") from exc
            if not accepted:
                raise EvaluationNotFound("evaluation job not found")
            self.record_api_metric("success", operation="write")
            return await self.query.get_async("jobs", job_id)
        if self.query.backend is not None:
            raise EvaluationQueryError("orchestration service unavailable")
        job = self.query.get("jobs", job_id)
        if job.get("status") not in {"queued", "running"}:
            return job
        updated = dict(job)
        updated["status"] = "cancelled"
        updated["updatedAt"] = _now()
        items = self.query.store._items("jobs")
        items[:] = [updated if item.get("jobId") == job_id else item for item in items]
        self.query.store.history.insert(
            0,
            {
                "historyId": str(uuid4()),
                "resourceType": "job",
                "resourceId": job_id,
                "resourceVersion": None,
                "change": "cancel_requested",
                "state": "cancelled",
                "occurredAt": _now(),
            },
        )
        await self._publish("evaluation_job", job_id, "cancel_requested", "cancelled")
        self.record_api_metric("success", operation="write")
        if self.orchestration is not None:
            try:
                await self.orchestration.cancel(job_id, idempotency_key=idempotency_key)
            except Exception:
                # The persisted command projection remains authoritative; provider
                # unavailability is surfaced by the next REST snapshot.
                pass
        return updated

    async def create_review(
        self, body: Mapping[str, object], *, idempotency_key: str | None = None
    ) -> dict[str, object]:
        _validate_review_request(body)
        if self.query.backend is not None:
            if self.calibration_service is None:
                raise EvaluationUnavailable("calibration service unavailable")
            calibration = body.get("calibration")
            provenance_value = body.get("provenance")
            if not isinstance(calibration, Mapping) or not isinstance(provenance_value, Mapping):
                raise EvaluationQueryError("review provenance is invalid")
            try:
                report = await self.calibration_service.get_calibration_result(
                    _uuid(calibration.get("calibrationId"), "calibrationId")
                )
            except CalibrationError as exc:
                if exc.code in {"database_failure", "storage_failure"}:
                    raise EvaluationUnavailable("evaluation calibration is unavailable") from exc
                raise EvaluationQueryError("calibration result is invalid") from exc
            if report is None or report.calibration_version != calibration.get(
                "calibrationVersion"
            ):
                raise EvaluationNotFound("calibration result not found")
            try:
                authorized = await self.calibration_service.is_authorized(
                    _uuid(body.get("authorizationRecordId"), "authorizationRecordId"),
                    "human_review",
                    resource_id=_uuid(body.get("reviewId"), "reviewId"),
                    scope_fingerprint=str(provenance_value.get("sourceDigest")),
                )
            except CalibrationError as exc:
                if exc.code in {"database_failure", "storage_failure"}:
                    raise EvaluationUnavailable("evaluation authorization is unavailable") from exc
                raise EvaluationQueryError("review authorization is invalid") from exc
            if not authorized:
                raise EvaluationQueryError("review authorization denied")
            comment = body.get("commentArtifact")
            review_provenance = cast(Mapping[str, object], body["provenance"])
            review = HumanReview(
                review_id=_uuid(body.get("reviewId"), "reviewId"),
                case_id=_uuid(body.get("caseId"), "caseId"),
                result_id=_uuid(body.get("resultId"), "resultId"),
                judge_id=report.judge_id,
                judge_version=report.judge_version,
                prompt_version=report.prompt_version,
                rubric_version=report.rubric_version,
                calibration_version=report.calibration_version,
                label=str(body.get("reviewLabel")),
                score=float(body.get("reviewScore")),
                disagreement=DisagreementClass(str(body.get("disagreement"))),
                comment_artifact_reference_id=(
                    _uuid(comment.get("artifactId"), "commentArtifact.artifactId")
                    if isinstance(comment, Mapping)
                    else None
                ),
                created_at=_timestamp(body.get("createdAt"), "createdAt"),
                review_version=_version(body.get("reviewVersion"), "reviewVersion"),
                idempotency_key=idempotency_key,
                authorization_record_id=_uuid(
                    body.get("authorizationRecordId"), "authorizationRecordId"
                ),
                outcome=JudgeOutcome(str(body.get("reviewOutcome"))),
                calibration_id=str(report.calibration_id),
                score_id=_uuid(body.get("scoreId"), "scoreId"),
                review_label=str(body.get("reviewLabel")),
                comment_artifact_version=(
                    _version(comment.get("artifactVersion"), "commentArtifact.artifactVersion")
                    if isinstance(comment, Mapping)
                    else None
                ),
                comment_artifact_digest=(
                    str(comment.get("digest")) if isinstance(comment, Mapping) else None
                ),
                provenance_component=str(review_provenance["createdByComponent"]),
                provenance_version=str(review_provenance["createdByVersion"]),
                provenance_digest=str(review_provenance["sourceDigest"]),
            )
            try:
                stored = await self.calibration_service.review(review)
            except CalibrationError as exc:
                if exc.code == "idempotency_conflict":
                    raise EvaluationConflict(
                        "idempotency key conflicts with existing state"
                    ) from exc
                if exc.code in {"database_failure", "storage_failure"}:
                    raise EvaluationUnavailable("evaluation review is unavailable") from exc
                raise EvaluationQueryError("review rejected") from exc
            return _review_projection(stored)
        allowed = frozenset(
            {
                "reviewId",
                "reviewVersion",
                "calibration",
                "caseId",
                "resultId",
                "scoreId",
                "reviewScore",
                "reviewOutcome",
                "reviewLabel",
                "disagreement",
                "authorizationRecordId",
                "commentArtifact",
                "provenance",
                "createdAt",
            }
        )
        if set(body) - allowed:
            raise EvaluationQueryError("review body is invalid")
        review_id = _uuid(body.get("reviewId"), "reviewId")
        version = _version(body.get("reviewVersion"), "reviewVersion")
        for key in ("caseId", "resultId", "scoreId", "authorizationRecordId"):
            _uuid(body.get(key), key)
        calibration = body.get("calibration")
        if not isinstance(calibration, Mapping):
            raise EvaluationQueryError("calibration is invalid")
        _uuid(calibration.get("calibrationId"), "calibration.calibrationId")
        _version(calibration.get("calibrationVersion"), "calibration.calibrationVersion")
        now = _now()
        item = {
            "schemaVersion": 1,
            "resourceType": "human_review",
            "reviewId": review_id,
            "reviewVersion": version,
            "calibration": resource_ref(
                str(calibration["calibrationId"]), str(calibration["calibrationVersion"])
            ),
            "caseId": body["caseId"],
            "resultId": body["resultId"],
            "scoreId": body["scoreId"],
            "reviewScore": body.get("reviewScore"),
            "reviewOutcome": body.get("reviewOutcome"),
            "reviewLabel": body.get("reviewLabel"),
            "disagreement": body.get("disagreement"),
            "authorizationRecordId": body["authorizationRecordId"],
            "commentArtifactId": (
                cast(Mapping[str, object], body["commentArtifact"]).get("artifactId")
                if isinstance(body.get("commentArtifact"), Mapping)
                else None
            ),
            "provenance": provenance(
                source={k: v for k, v in body.items() if k != "commentArtifact"},
                component="aura.observatory.calibration",
            ),
            "createdAt": now,
        }
        stored = self.query.store.put("reviews", item)
        self.record_api_metric("success", operation="write")
        await self._publish("evaluation_review", review_id, "created", "completed")
        return stored

    async def create_qualification(
        self, body: Mapping[str, object], *, idempotency_key: str | None = None
    ) -> dict[str, object]:
        _validate_qualification_request(body)
        if self.query.backend is not None:
            if self.calibration_service is None:
                raise EvaluationUnavailable("calibration service unavailable")
            try:
                report = await self.calibration_service.get_calibration_result(
                    _uuid(body.get("calibrationResultId"), "calibrationResultId")
                )
            except CalibrationError as exc:
                if exc.code in {"database_failure", "storage_failure"}:
                    raise EvaluationUnavailable("evaluation calibration is unavailable") from exc
                raise EvaluationQueryError("calibration result is invalid") from exc
            if report is None or not report.eligible:
                raise EvaluationQueryError("calibration is not eligible")
            provenance_value = body.get("provenance")
            if not isinstance(provenance_value, Mapping):
                raise EvaluationQueryError("qualification provenance is invalid")
            qualification_id = _uuid(body.get("qualificationId"), "qualificationId")
            authorization_id = _uuid(body.get("authorizationRecordId"), "authorizationRecordId")
            try:
                authorized = await self.calibration_service.is_authorized(
                    authorization_id,
                    "qualification",
                    resource_id=qualification_id,
                    scope_fingerprint=str(provenance_value.get("sourceDigest")),
                )
            except CalibrationError as exc:
                if exc.code in {"database_failure", "storage_failure"}:
                    raise EvaluationUnavailable("evaluation authorization is unavailable") from exc
                raise EvaluationQueryError("qualification authorization is invalid") from exc
            if not authorized:
                raise EvaluationQueryError("qualification authorization denied")
            dataset = cast(Mapping[str, object], body.get("dataset", {}))
            suite = cast(Mapping[str, object], body.get("suite", {}))
            evaluator = cast(Mapping[str, object], body.get("evaluator", {}))
            judge = cast(Mapping[str, object], body.get("judgeDefinition", {}))
            model = cast(Mapping[str, object], body.get("model", {}))
            prompt = cast(Mapping[str, object], body.get("prompt", {}))
            rubric = cast(Mapping[str, object], body.get("rubric", {}))
            calibration = cast(Mapping[str, object], body.get("calibration", {}))
            qualification = Qualification(
                qualification_id,
                _uuid(dataset.get("id"), "dataset.id"),
                _version(dataset.get("version"), "dataset.version"),
                _uuid(suite.get("id"), "suite.id"),
                _version(suite.get("version"), "suite.version"),
                _uuid(evaluator.get("evaluatorId"), "evaluatorId"),
                _version(evaluator.get("evaluatorVersion"), "evaluatorVersion"),
                _uuid(judge.get("judgeDefinitionId"), "judgeDefinitionId"),
                _version(judge.get("judgeDefinitionVersion"), "judgeDefinitionVersion"),
                _version(str(body.get("promptVersion", report.prompt_version)), "promptVersion"),
                _version(str(body.get("rubricVersion", report.rubric_version)), "rubricVersion"),
                _version(calibration.get("calibrationVersion"), "calibrationVersion"),
                created_at=_timestamp(body.get("createdAt"), "createdAt"),
                qualification_version=_version(
                    body.get("qualificationVersion"), "qualificationVersion"
                ),
                idempotency_key=idempotency_key,
                authorization_record_id=authorization_id,
                calibration_result_id=_uuid(body.get("calibrationResultId"), "calibrationResultId"),
                model_name=str(model.get("modelName")),
                model_version=_version(model.get("modelVersion"), "modelVersion"),
                model_digest=str(model.get("modelDigest")),
                prompt_artifact_reference_id=_uuid(prompt.get("artifactId"), "prompt.artifactId"),
                prompt_artifact_version=_version(
                    prompt.get("artifactVersion"), "prompt.artifactVersion"
                ),
                prompt_artifact_digest=str(prompt.get("digest")),
                rubric_artifact_reference_id=_uuid(rubric.get("artifactId"), "rubric.artifactId"),
                rubric_artifact_version=_version(
                    rubric.get("artifactVersion"), "rubric.artifactVersion"
                ),
                rubric_artifact_digest=str(rubric.get("digest")),
                calibration_eligibility="eligible",
                qualified_at=_timestamp(body.get("qualifiedAt"), "qualifiedAt"),
                seed=report.seed,
                budget=report.budget,
                provenance_component=str(provenance_value.get("createdByComponent")),
                provenance_version=_version(
                    provenance_value.get("createdByVersion"), "createdByVersion"
                ),
                provenance_digest=str(provenance_value.get("sourceDigest")),
                calibration_id=_uuid(calibration.get("calibrationId"), "calibrationId"),
            )
            try:
                stored = await self.calibration_service.qualify(
                    qualification,
                    report,
                    owner_authorized=True,
                    idempotency_key=idempotency_key,
                )
            except CalibrationError as exc:
                if exc.code == "idempotency_conflict":
                    raise EvaluationConflict(
                        "idempotency key conflicts with existing state"
                    ) from exc
                if exc.code in {"database_failure", "storage_failure"}:
                    raise EvaluationUnavailable("evaluation qualification is unavailable") from exc
                raise EvaluationQueryError("qualification rejected") from exc
            return _qualification_projection(stored)
        for key in ("qualificationId", "calibrationResultId", "authorizationRecordId"):
            _uuid(body.get(key), key)
        for key in ("qualificationVersion",):
            _version(body.get(key), key)
        now = _now()
        item = {
            "schemaVersion": 1,
            "resourceType": "judge_qualification",
            "qualificationId": body["qualificationId"],
            "qualificationVersion": body["qualificationVersion"],
            "state": "qualified",
            "dataset": dict(cast(Mapping[str, object], body.get("dataset", {}))),
            "suite": dict(cast(Mapping[str, object], body.get("suite", {}))),
            "evaluator": dict(cast(Mapping[str, object], body.get("evaluator", {}))),
            "judgeDefinition": dict(cast(Mapping[str, object], body.get("judgeDefinition", {}))),
            "calibration": dict(cast(Mapping[str, object], body.get("calibration", {}))),
            "calibrationResultId": body["calibrationResultId"],
            "authorizationRecordId": body["authorizationRecordId"],
            "scoreDisposition": "calibrated_enabled",
            "provenance": provenance(source=body, component="aura.observatory.calibration"),
            "createdAt": now,
        }
        stored = self.query.store.put("qualifications", item)
        await self._publish(
            "evaluation_qualification", body["qualificationId"], "qualified", "qualified"
        )
        self.record_api_metric("success", operation="write")
        return stored

    async def revoke_qualification(
        self,
        qualification_id: str,
        *,
        idempotency_key: str,
        authorization_record_id: str | None = None,
        revoked_at: object | None = None,
    ) -> dict[str, object]:
        _validate_revoke_request(
            {
                "action": "revoke",
                "authorizationRecordId": authorization_record_id,
                "revokedAt": revoked_at,
            }
        )
        if self.query.backend is not None:
            if self.calibration_service is None:
                raise EvaluationUnavailable("calibration service unavailable")
            try:
                stored = await self.calibration_service.revoke(
                    qualification_id,
                    at=_timestamp(revoked_at, "revokedAt"),
                    authorization_record_id=_uuid(
                        authorization_record_id, "authorizationRecordId"
                    ),
                    idempotency_key=idempotency_key,
                )
            except CalibrationError as exc:
                if exc.code == "idempotency_conflict":
                    raise EvaluationConflict(
                        "idempotency key conflicts with existing state"
                    ) from exc
                if exc.code in {"database_failure", "storage_failure"}:
                    raise EvaluationUnavailable("evaluation calibration is unavailable") from exc
                raise EvaluationQueryError("qualification revocation rejected") from exc
            if stored is None:
                raise EvaluationNotFound("qualification not found")
            return _qualification_projection(stored)
        if self.query.backend is not None:
            raise EvaluationQueryError("calibration repository unavailable")
        item = self.query.get("qualifications", qualification_id)
        updated = dict(item)
        updated["state"] = "revoked"
        updated["scoreDisposition"] = "advisory_only"
        items = self.query.store._items("qualifications")
        items[:] = [
            updated if row.get("qualificationId") == qualification_id else row for row in items
        ]
        self.query.store.history.insert(
            0,
            {
                "historyId": str(uuid4()),
                "resourceType": "qualification",
                "resourceId": qualification_id,
                "resourceVersion": updated.get("qualificationVersion"),
                "change": "revoked",
                "state": "revoked",
                "occurredAt": _now(),
            },
        )
        await self._publish("evaluation_qualification", qualification_id, "revoked", "revoked")
        return updated

    async def create_deletion(
        self, body: Mapping[str, object], idempotency_key: str
    ) -> dict[str, object]:
        _validate_deletion_request(body)
        if self.query.backend is not None:
            if self.deletion_service is None:
                raise EvaluationQueryError("deletion service unavailable")
            command_id = _uuid(body.get("commandId"), "commandId")
            requested_at = _timestamp(body.get("requestedAt"), "requestedAt")
            kind_value = str(body.get("kind"))
            try:
                kind = DeletionKind(kind_value)
            except ValueError as exc:
                raise EvaluationQueryError("deletion selector is invalid") from exc
            value: str | datetime | None
            if kind is DeletionKind.CUTOFF:
                value = _timestamp(body.get("cutoff"), "cutoff")
            elif kind is DeletionKind.PURGE_ALL:
                value = None
            else:
                field = {
                    DeletionKind.JOB: "jobId",
                    DeletionKind.DATASET: "datasetId",
                    DeletionKind.SUITE: "suiteId",
                }[kind]
                value = _uuid(body.get(field), field)
            command = DeletionCommand(
                command_id=command_id,
                idempotency_key=_uuid(idempotency_key, "idempotencyKey"),
                selector=DeletionSelector(kind, value),
                confirmation="DELETE_EVALUATION_ARTIFACTS",
                requested_at=requested_at,
            )
            try:
                await self.deletion_service.create_command(command)
            except ArtifactStorageError as exc:
                if exc.failure_code == "idempotency_conflict":
                    raise EvaluationConflict(
                        "idempotency key conflicts with existing state"
                    ) from exc
                if exc.failure_code in {
                    "database_failure",
                    "provider_unavailable",
                }:
                    raise EvaluationUnavailable("evaluation deletion is unavailable") from exc
                raise EvaluationQueryError("deletion command rejected") from exc
            except Exception as exc:
                raise EvaluationUnavailable("evaluation deletion is unavailable") from exc
            action = {
                "actionId": command.command_id,
                "scope": kind.value,
                "resourceId": value if isinstance(value, str) else None,
                "resourceVersion": None,
                "cutoff": value.isoformat().replace("+00:00", "Z")
                if isinstance(value, datetime)
                else None,
                "status": "queued",
                "deletedArtifactCount": 0,
                "deletedMetadataCount": 0,
                "failureCode": None,
                "createdAt": command.requested_at.isoformat().replace("+00:00", "Z"),
                "startedAt": None,
                "finishedAt": None,
            }
            return cast(dict[str, object], action)
        digest = _json_digest(body)
        prior = self.query.store.idempotency.get(idempotency_key)
        if prior is not None:
            if prior[1] != digest:
                raise EvaluationConflict("idempotency key conflicts with existing state")
            return dict(prior[2])
        action_id = str(
            uuid5(NAMESPACE_URL, "aura-observatory:evaluation-deletion:" + idempotency_key)
        )
        scope = str(body.get("kind"))
        resource_id: str | None = None
        resource_version: str | None = None
        if scope == "job":
            resource_id = _uuid(body.get("jobId"), "jobId")
        elif scope in {"dataset", "suite"}:
            resource_id = _uuid(body.get(f"{scope}Id"), f"{scope}Id")
        cutoff = _timestamp(body.get("cutoff"), "cutoff") if scope == "cutoff" else None
        item = {
            "actionId": action_id,
            "scope": scope,
            "resourceId": resource_id,
            "resourceVersion": resource_version,
            "cutoff": cutoff.isoformat().replace("+00:00", "Z") if cutoff else None,
            "status": "queued",
            "deletedArtifactCount": 0,
            "deletedMetadataCount": 0,
            "failureCode": None,
            "createdAt": _now(),
            "startedAt": None,
            "finishedAt": None,
        }
        stored = self.query.store.put("deletions", item, change="deletion_requested")
        self.query.store.idempotency[idempotency_key] = (action_id, digest, stored)
        await self._publish("evaluation_deletion", action_id, "deletion_requested", "queued")
        self.record_api_metric("success", operation="write")
        return stored


def _no_store(value: object, status_code: int = 200) -> JSONResponse:
    return JSONResponse(value, status_code=status_code, headers={"Cache-Control": "no-store"})


def register_evaluation_routes(
    app: FastAPI,
    *,
    owner_session: Callable[[Request], Awaitable[Any]],
    auth_service: Any,
    service: EvaluationApplicationService | None = None,
    resolved: Any | None = None,
) -> EvaluationApplicationService:
    """Register the additive evaluation resources on an existing API app."""

    evaluation = service or EvaluationApplicationService()
    app.state.evaluation = evaluation
    app.state.evaluation_live = evaluation.live

    async def read_owner(request: Request) -> Any:
        return await owner_session(request)

    async def command_headers(
        request: Request, *, confirmation: str | None, idempotency_key: str | None
    ) -> Any:
        session = await owner_session(request)
        csrf = request.headers.get("X-CSRF-Token") or request.headers.get("X-Observatory-CSRF")
        try:
            auth_service.require_csrf(session, csrf, origin=request.headers.get("origin"))
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail="invalid CSRF token") from exc
        if idempotency_key is None or _IDEMPOTENCY.fullmatch(idempotency_key) is None:
            raise HTTPException(status_code=400, detail="invalid request")
        if confirmation is not None and confirmation not in _CONFIRMATIONS:
            raise HTTPException(status_code=400, detail="invalid request")
        return session

    async def body(request: Request) -> dict[str, object]:
        try:
            raw = await request.body()
            if len(raw) > 262_144:
                raise HTTPException(status_code=413, detail="request too large")
            value = json.loads(raw)
        except Exception as exc:
            if isinstance(exc, HTTPException):
                raise
            raise HTTPException(status_code=400, detail="invalid request") from exc
        if not isinstance(value, Mapping):
            raise HTTPException(status_code=422, detail="invalid request")
        return dict(cast(Mapping[str, object], value))

    @app.get("/api/v1/evaluation/events")
    async def evaluation_events(
        request: Request,
        last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    ) -> StreamingResponse:
        """Stream only persisted evaluation metadata changes.

        The operational ``/api/v1/events`` stream deliberately remains owned
        by the Observatory health/telemetry query surface.  This dedicated
        stream pages the evaluation ledger exclusively, then keeps polling it
        with bounded heartbeats so worker-side transitions are observable.
        """

        if resolved is not None:
            origin = request.headers.get("origin")
            if origin is not None and origin != resolved.public_origin:
                raise HTTPException(status_code=403, detail="same-origin request required")
        await read_owner(request)
        if last_event_id is not None:
            try:
                _uuid(last_event_id, "last event identifier")
            except EvaluationQueryError as exc:
                raise HTTPException(status_code=400, detail="invalid request") from exc
        stream_session_id = request.cookies.get(
            getattr(resolved, "effective_session_cookie_name", "aura_session")
        )
        preflight_started = monotonic()
        try:
            prefetched_events = await evaluation.live.after(last_event_id, limit=1000)
        except EvaluationUnavailable as exc:
            evaluation.record_live_metric("poll", "unavailable")
            evaluation.record_live_span("live_page", preflight_started, "unavailable", error_code="provider_unavailable")
            raise HTTPException(status_code=503, detail="service unavailable") from exc
        except Exception as exc:
            evaluation.record_live_metric("poll", "failure")
            evaluation.record_live_span("live_page", preflight_started, "failure", error_code="storage_failure")
            raise HTTPException(status_code=503, detail="service unavailable") from exc
        evaluation.record_live_metric("poll", "success")
        evaluation.record_live_span("live_page", preflight_started, "success")

        async def stream() -> AsyncIterator[str]:
            cursor = last_event_id
            pending_events: list[Any] | None = list(prefetched_events)
            # The replay fence closes only after a page smaller than the
            # bounded catch-up limit (or an empty page).  Events after that
            # point are live notifications and are counted separately.
            replaying = True

            async def authorized() -> bool:
                session = await auth_service.get_session(stream_session_id)
                return session is not None and (
                    resolved is None
                    or not resolved.owner_subject
                    or session.principal.subject == resolved.owner_subject
                )

            async def disconnected() -> bool:
                try:
                    return await request.is_disconnected()
                except RuntimeError:
                    return False

            while await authorized() and not await disconnected():
                poll_started = monotonic()
                if pending_events is not None:
                    events = pending_events
                    pending_events = None
                    evaluation.record_live_span("live_replay", poll_started, "success")
                else:
                    try:
                        events = await evaluation.live.after(cursor, limit=1000)
                    except EvaluationUnavailable:
                        evaluation.record_live_metric("poll", "unavailable")
                        evaluation.record_live_span("live_page", poll_started, "unavailable", error_code="provider_unavailable")
                        return
                    except Exception:
                        evaluation.record_live_metric("poll", "failure")
                        evaluation.record_live_span("live_page", poll_started, "failure", error_code="storage_failure")
                        return
                    evaluation.record_live_metric("poll", "success")
                    evaluation.record_live_span("live_page", poll_started, "success")
                if events:
                    for event in events:
                        if await disconnected() or not await authorized():
                            evaluation.record_live_metric("disconnect", "success")
                            return
                        cursor = event.event_id
                        if replaying:
                            evaluation.record_live_replay_metric(event.occurred_at)
                            evaluation.record_live_span("live_replay", poll_started, "success")
                        else:
                            evaluation.record_live_metric("live", "success")
                            evaluation.record_live_span("live", poll_started, "success")
                        yield (
                            f"id: {event.event_id}\n"
                            "event: evaluation.changed\n"
                            "data: "
                            + json.dumps(event.as_sse(), separators=(",", ":"))
                            + "\n\n"
                        )
                    if len(events) >= 1000:
                        continue
                    replaying = False
                else:
                    replaying = False
                if getattr(app.state, "testing", False):
                    yield ": heartbeat\n\n"
                    return
                wait_started = monotonic()
                try:
                    await evaluation.live.wait(timeout=15.0)
                    evaluation.record_live_metric("wait", "success")
                    evaluation.record_live_span("live_wait", wait_started, "success")
                except TimeoutError:
                    evaluation.record_live_metric("wait", "timeout")
                    evaluation.record_live_span("live_wait", wait_started, "timeout", error_code="timeout")
                except EvaluationUnavailable:
                    evaluation.record_live_metric("wait", "unavailable")
                    evaluation.record_live_span("live_wait", wait_started, "unavailable", error_code="provider_unavailable")
                    return
                except Exception:
                    evaluation.record_live_metric("wait", "failure")
                    evaluation.record_live_span("live_wait", wait_started, "failure", error_code="storage_failure")
                    return
                evaluation.record_live_metric("heartbeat", "success")
                if await disconnected() or not await authorized():
                    evaluation.record_live_metric("disconnect", "success")
                    evaluation.record_live_span("live_disconnect", monotonic(), "success")
                    return
                yield ": heartbeat\n\n"
                if getattr(app.state, "testing", False):
                    return

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    async def collection(
        request: Request,
        kind: str,
        cursor: str | None,
        page_size: str | None,
        filters: Mapping[str, object] | None = None,
    ) -> JSONResponse:
        await read_owner(request)
        try:
            query_keys: dict[str, frozenset[str]] = {
                "datasets": frozenset({"cursor", "page_size"}),
                "cases": frozenset({"cursor", "page_size"}),
                "suites": frozenset({"cursor", "page_size", "dataset_id"}),
                "evaluators": frozenset({"cursor", "page_size", "kind"}),
                "jobs": frozenset({"cursor", "page_size", "status", "suite_id", "from", "to"}),
                "attempts": frozenset({"cursor", "page_size"}),
                "results": frozenset({"cursor", "page_size", "job_id", "status"}),
                "scores": frozenset({"cursor", "page_size", "evaluator_id"}),
                "artifacts": frozenset({"cursor", "page_size", "job_id", "result_id", "deletion_status"}),
                "reviews": frozenset({"cursor", "page_size", "result_id", "calibration_id"}),
                "calibrations": frozenset({"cursor", "page_size", "eligibility"}),
                "qualifications": frozenset({"cursor", "page_size", "state"}),
            }
            permitted_query_keys = query_keys.get(kind)
            if permitted_query_keys is None:
                raise EvaluationQueryError("resource kind is invalid")
            _validate_query_parameters(request, permitted_query_keys)
            allowed_filters: dict[str, frozenset[str]] = {
                "datasets": frozenset(),
                "cases": frozenset({"dataset", "datasetVersion"}),
                "suites": frozenset({"dataset"}),
                "evaluators": frozenset({"kind"}),
                "jobs": frozenset({"status", "suite", "from", "to"}),
                "attempts": frozenset({"jobId"}),
                "results": frozenset({"jobId", "status"}),
                "scores": frozenset({"resultId", "evaluatorId"}),
                "artifacts": frozenset({"jobId", "resultId", "deletionStatus"}),
                "reviews": frozenset({"resultId", "calibrationId"}),
                "calibrations": frozenset({"eligibility"}),
                "qualifications": frozenset({"state"}),
                "deletions": frozenset(),
            }
            permitted = allowed_filters.get(kind)
            if permitted is None or any(key not in permitted for key in (filters or {})):
                raise EvaluationQueryError("filter is invalid")
            if cursor is not None and len(cursor) > 256:
                raise EvaluationQueryError("cursor is invalid")
            if page_size is not None and len(page_size) > 8:
                raise EvaluationQueryError("page size is invalid")
            for value in (filters or {}).values():
                if isinstance(value, str) and len(value) > 256:
                    raise EvaluationQueryError("filter is invalid")
            if filters:
                for key in (
                    "dataset", "suite", "jobId", "resultId", "evaluatorId", "calibrationId"
                ):
                    if filters.get(key) is not None:
                        _uuid(filters[key], key)
                if filters.get("status") is not None and filters["status"] not in (
                    {"completed", "cancelled", "failed"}
                    if kind == "results"
                    else {"queued", "running", "succeeded", "failed", "cancelled", "deadline"}
                ):
                    raise EvaluationQueryError("status filter is invalid")
                if kind == "evaluators" and filters.get("kind") not in {
                    None, "deterministic", "local_judge", "human_review"
                }:
                    raise EvaluationQueryError("evaluator kind filter is invalid")
                if kind == "artifacts" and filters.get("deletionStatus") not in {
                    None, "retained", "deletion_queued", "deleting", "deleted", "deletion_failed"
                }:
                    raise EvaluationQueryError("artifact deletion status filter is invalid")
                if filters.get("state") is not None and filters["state"] not in {
                    "qualified", "revoked"
                }:
                    raise EvaluationQueryError("state filter is invalid")
                if filters.get("eligibility") is not None and filters["eligibility"] not in {
                    "eligible", "ineligible"
                }:
                    raise EvaluationQueryError("eligibility filter is invalid")
                for key in ("from", "to"):
                    if filters.get(key) is not None:
                        _timestamp(filters[key], key)
            limit = min(200, max(1, int(page_size or "50")))
            result = await evaluation.list_collection(
                kind, cursor=cursor, limit=limit, filters=filters
            )
            evaluation.record_api_metric("success")
        except EvaluationUnavailable as exc:
            evaluation.record_api_metric("unavailable")
            raise HTTPException(status_code=503, detail="service unavailable") from exc
        except (ValueError, EvaluationQueryError) as exc:
            evaluation.record_api_metric("invalid")
            raise HTTPException(status_code=400, detail="invalid request") from exc
        return _no_store(result)

    @app.get("/api/v1/evaluation/datasets")
    async def list_datasets(
        request: Request,
        cursor: str | None = None,
        page_size: str | None = None,
    ) -> JSONResponse:
        return await collection(request, "datasets", cursor, page_size)

    @app.post("/api/v1/evaluation/datasets", status_code=201)
    async def create_dataset(
        request: Request,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> JSONResponse:
        await command_headers(request, confirmation=None, idempotency_key=idempotency_key)
        value = await body(request)
        try:
            result = await evaluation.create_definition(
                "dataset", value, idempotency_key=idempotency_key
            )
        except EvaluationConflict as exc:
            raise HTTPException(
                status_code=409, detail="request conflicts with existing state"
            ) from exc
        except EvaluationUnavailable as exc:
            raise HTTPException(status_code=503, detail="service unavailable") from exc
        except EvaluationQueryError as exc:
            raise HTTPException(status_code=422, detail="invalid request") from exc
        return _no_store(result, 201)

    @app.get("/api/v1/evaluation/datasets/{dataset_id}/versions/{resource_version}")
    async def get_dataset(dataset_id: str, resource_version: str, request: Request) -> JSONResponse:
        await read_owner(request)
        try:
            result = await evaluation.get_resource(
                "datasets",
                _uuid(dataset_id, "dataset_id"),
                _version(resource_version, "resource_version"),
            )
        except EvaluationNotFound as exc:
            raise HTTPException(status_code=404, detail="resource not found") from exc
        except EvaluationUnavailable as exc:
            raise HTTPException(status_code=503, detail="service unavailable") from exc
        except EvaluationQueryError as exc:
            raise HTTPException(status_code=400, detail="invalid request") from exc
        return _no_store(result)

    @app.get("/api/v1/evaluation/datasets/{dataset_id}/versions/{resource_version}/cases")
    async def list_cases(
        dataset_id: str,
        resource_version: str,
        request: Request,
        cursor: str | None = None,
        page_size: str | None = None,
    ) -> JSONResponse:
        await read_owner(request)
        _uuid(dataset_id, "dataset_id")
        _version(resource_version, "resource_version")
        return await collection(
            request,
            "cases",
            cursor,
            page_size,
            {"dataset": dataset_id, "datasetVersion": resource_version},
        )

    @app.get("/api/v1/evaluation/suites")
    async def list_suites(
        request: Request,
        cursor: str | None = None,
        page_size: str | None = None,
        dataset_id: str | None = None,
    ) -> JSONResponse:
        return await collection(request, "suites", cursor, page_size, {"dataset": dataset_id})

    @app.post("/api/v1/evaluation/suites", status_code=201)
    async def create_suite(
        request: Request,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> JSONResponse:
        await command_headers(request, confirmation=None, idempotency_key=idempotency_key)
        try:
            result = await evaluation.create_definition(
                "suite", await body(request), idempotency_key=idempotency_key
            )
        except EvaluationConflict as exc:
            raise HTTPException(
                status_code=409, detail="request conflicts with existing state"
            ) from exc
        except EvaluationUnavailable as exc:
            raise HTTPException(status_code=503, detail="service unavailable") from exc
        except EvaluationQueryError as exc:
            raise HTTPException(status_code=422, detail="invalid request") from exc
        return _no_store(result, 201)

    @app.get("/api/v1/evaluation/suites/{suite_id}/versions/{resource_version}")
    async def get_suite(suite_id: str, resource_version: str, request: Request) -> JSONResponse:
        await read_owner(request)
        try:
            result = await evaluation.get_resource(
                "suites",
                _uuid(suite_id, "suite_id"),
                _version(resource_version, "resource_version"),
            )
        except EvaluationNotFound as exc:
            raise HTTPException(status_code=404, detail="resource not found") from exc
        except EvaluationUnavailable as exc:
            raise HTTPException(status_code=503, detail="service unavailable") from exc
        return _no_store(result)

    @app.get("/api/v1/evaluation/evaluators")
    async def list_evaluators(
        request: Request,
        cursor: str | None = None,
        page_size: str | None = None,
        kind: str | None = None,
    ) -> JSONResponse:
        return await collection(request, "evaluators", cursor, page_size, {"kind": kind})

    @app.get("/api/v1/evaluation/evaluators/{evaluator_id}/versions/{resource_version}")
    async def get_evaluator(
        evaluator_id: str, resource_version: str, request: Request
    ) -> JSONResponse:
        await read_owner(request)
        try:
            result = await evaluation.get_resource(
                "evaluators",
                _uuid(evaluator_id, "evaluator_id"),
                _version(resource_version, "resource_version"),
            )
        except EvaluationNotFound as exc:
            raise HTTPException(status_code=404, detail="resource not found") from exc
        except EvaluationUnavailable as exc:
            raise HTTPException(status_code=503, detail="service unavailable") from exc
        return _no_store(result)

    @app.get("/api/v1/evaluation/jobs")
    async def list_jobs(
        request: Request,
        cursor: str | None = None,
        page_size: str | None = None,
        status: str | None = None,
        suite_id: str | None = None,
        from_time: str | None = Query(default=None, alias="from"),
        to_time: str | None = Query(default=None, alias="to"),
    ) -> JSONResponse:
        return await collection(
            request,
            "jobs",
            cursor,
            page_size,
            {"status": status, "suite": suite_id, "from": from_time, "to": to_time},
        )

    @app.post("/api/v1/evaluation/jobs", status_code=202)
    async def submit_job(
        request: Request,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> JSONResponse:
        await command_headers(request, confirmation=None, idempotency_key=idempotency_key)
        try:
            submit_body = await body(request)
            _validate_submit_request(submit_body)
            result = await evaluation.submit_job(submit_body, cast(str, idempotency_key))
        except EvaluationConflict as exc:
            raise HTTPException(
                status_code=409, detail="request conflicts with existing state"
            ) from exc
        except EvaluationUnavailable as exc:
            raise HTTPException(status_code=503, detail="service unavailable") from exc
        except EvaluationQueryError as exc:
            raise HTTPException(status_code=422, detail="invalid request") from exc
        return _no_store(result, 202)

    @app.get("/api/v1/evaluation/jobs/{job_id}")
    async def get_job(job_id: str, request: Request) -> JSONResponse:
        await read_owner(request)
        try:
            result = await evaluation.get_resource("jobs", _uuid(job_id, "job_id"))
        except EvaluationNotFound as exc:
            raise HTTPException(status_code=404, detail="resource not found") from exc
        except EvaluationUnavailable as exc:
            raise HTTPException(status_code=503, detail="service unavailable") from exc
        return _no_store(result)

    @app.get("/api/v1/evaluation/jobs/{job_id}/attempts")
    async def list_attempts(
        job_id: str, request: Request, cursor: str | None = None, page_size: str | None = None
    ) -> JSONResponse:
        _uuid(job_id, "job_id")
        return await collection(request, "attempts", cursor, page_size, {"jobId": job_id})

    @app.post("/api/v1/evaluation/jobs/{job_id}/cancel", status_code=202)
    async def cancel_job(
        job_id: str,
        request: Request,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        confirmation: str | None = Header(default=None, alias="X-Observatory-Confirmation"),
    ) -> JSONResponse:
        await command_headers(request, confirmation=confirmation, idempotency_key=idempotency_key)
        if confirmation != "cancel-job":
            raise HTTPException(status_code=400, detail="invalid request")
        try:
            cancel_body = await body(request)
            _validate_cancel_request(cancel_body)
            result = await evaluation.cancel_job(
                _uuid(job_id, "job_id"),
                idempotency_key=idempotency_key,
                reason=str(cancel_body.get("reason", "")),
            )
        except EvaluationConflict as exc:
            raise HTTPException(
                status_code=409, detail="request conflicts with existing state"
            ) from exc
        except EvaluationNotFound as exc:
            raise HTTPException(status_code=404, detail="resource not found") from exc
        except EvaluationUnavailable as exc:
            raise HTTPException(status_code=503, detail="service unavailable") from exc
        except EvaluationQueryError as exc:
            raise HTTPException(status_code=422, detail="invalid request") from exc
        return _no_store(result, 202)

    @app.get("/api/v1/evaluation/results")
    async def list_results(
        request: Request,
        cursor: str | None = None,
        page_size: str | None = None,
        job_id: str | None = None,
        status: str | None = None,
    ) -> JSONResponse:
        return await collection(
            request, "results", cursor, page_size, {"jobId": job_id, "status": status}
        )

    @app.get("/api/v1/evaluation/artifacts")
    async def list_artifacts(
        request: Request,
        cursor: str | None = None,
        page_size: str | None = None,
        job_id: str | None = None,
        result_id: str | None = None,
        deletion_status: str | None = None,
    ) -> JSONResponse:
        return await collection(
            request,
            "artifacts",
            cursor,
            page_size,
            {"jobId": job_id, "resultId": result_id, "deletionStatus": deletion_status},
        )

    @app.get("/api/v1/evaluation/reviews")
    async def list_reviews(
        request: Request,
        cursor: str | None = None,
        page_size: str | None = None,
        result_id: str | None = None,
        calibration_id: str | None = None,
    ) -> JSONResponse:
        return await collection(
            request,
            "reviews",
            cursor,
            page_size,
            {"resultId": result_id, "calibrationId": calibration_id},
        )

    @app.get("/api/v1/evaluation/calibrations")
    async def list_calibrations(
        request: Request,
        cursor: str | None = None,
        page_size: str | None = None,
        eligibility: str | None = None,
    ) -> JSONResponse:
        return await collection(
            request, "calibrations", cursor, page_size, {"eligibility": eligibility}
        )

    @app.get("/api/v1/evaluation/qualifications")
    async def list_qualifications(
        request: Request,
        cursor: str | None = None,
        page_size: str | None = None,
        state: str | None = None,
    ) -> JSONResponse:
        return await collection(request, "qualifications", cursor, page_size, {"state": state})

    @app.get("/api/v1/evaluation/results/{result_id}")
    async def get_result(result_id: str, request: Request) -> JSONResponse:
        await read_owner(request)
        try:
            result = await evaluation.get_resource("results", _uuid(result_id, "result_id"))
        except EvaluationNotFound as exc:
            raise HTTPException(status_code=404, detail="resource not found") from exc
        except EvaluationUnavailable as exc:
            raise HTTPException(status_code=503, detail="service unavailable") from exc
        return _no_store(result)

    @app.get("/api/v1/evaluation/artifacts/{artifact_id}")
    async def get_artifact(artifact_id: str, request: Request) -> JSONResponse:
        await read_owner(request)
        try:
            result = await evaluation.get_resource(
                "artifacts", _uuid(artifact_id, "artifact_id")
            )
        except EvaluationNotFound as exc:
            raise HTTPException(status_code=404, detail="resource not found") from exc
        except EvaluationUnavailable as exc:
            raise HTTPException(status_code=503, detail="service unavailable") from exc
        return _no_store(result)

    @app.get("/api/v1/evaluation/results/{result_id}/scores")
    async def list_scores(
        result_id: str,
        request: Request,
        cursor: str | None = None,
        page_size: str | None = None,
        evaluator_id: str | None = None,
    ) -> JSONResponse:
        _uuid(result_id, "result_id")
        return await collection(
            request,
            "scores",
            cursor,
            page_size,
            {"resultId": result_id, "evaluatorId": evaluator_id},
        )

    @app.post("/api/v1/evaluation/reviews", status_code=201)
    async def create_review(
        request: Request,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> JSONResponse:
        await command_headers(request, confirmation=None, idempotency_key=idempotency_key)
        try:
            result = await evaluation.create_review(
                await body(request), idempotency_key=idempotency_key
            )
        except EvaluationConflict as exc:
            raise HTTPException(
                status_code=409, detail="request conflicts with existing state"
            ) from exc
        except EvaluationUnavailable as exc:
            raise HTTPException(status_code=503, detail="service unavailable") from exc
        except EvaluationQueryError as exc:
            raise HTTPException(status_code=422, detail="invalid request") from exc
        return _no_store(result, 201)

    @app.post("/api/v1/evaluation/qualifications", status_code=201)
    async def qualify(
        request: Request,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        confirmation: str | None = Header(default=None, alias="X-Observatory-Confirmation"),
    ) -> JSONResponse:
        await command_headers(request, confirmation=confirmation, idempotency_key=idempotency_key)
        if confirmation != "qualify-judge":
            raise HTTPException(status_code=400, detail="invalid request")
        try:
            result = await evaluation.create_qualification(
                await body(request), idempotency_key=idempotency_key
            )
        except EvaluationConflict as exc:
            raise HTTPException(
                status_code=409, detail="request conflicts with existing state"
            ) from exc
        except EvaluationUnavailable as exc:
            raise HTTPException(status_code=503, detail="service unavailable") from exc
        except EvaluationQueryError as exc:
            raise HTTPException(status_code=422, detail="invalid request") from exc
        return _no_store(result, 201)

    @app.post("/api/v1/evaluation/qualifications/{qualification_id}/revoke")
    async def revoke(
        qualification_id: str,
        request: Request,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        confirmation: str | None = Header(default=None, alias="X-Observatory-Confirmation"),
    ) -> JSONResponse:
        await command_headers(request, confirmation=confirmation, idempotency_key=idempotency_key)
        if confirmation != "revoke-qualification":
            raise HTTPException(status_code=400, detail="invalid request")
        try:
            revoke_body = await body(request)
            result = await evaluation.revoke_qualification(
                _uuid(qualification_id, "qualification_id"),
                idempotency_key=cast(str, idempotency_key),
                authorization_record_id=str(revoke_body.get("authorizationRecordId")),
                revoked_at=revoke_body.get("revokedAt"),
            )
        except EvaluationConflict as exc:
            raise HTTPException(
                status_code=409, detail="request conflicts with existing state"
            ) from exc
        except EvaluationNotFound as exc:
            raise HTTPException(status_code=404, detail="resource not found") from exc
        except EvaluationUnavailable as exc:
            raise HTTPException(status_code=503, detail="service unavailable") from exc
        except EvaluationQueryError as exc:
            raise HTTPException(status_code=422, detail="invalid request") from exc
        return _no_store(result)

    @app.get("/api/v1/evaluation/history")
    async def history(
        request: Request,
        cursor: str | None = None,
        page_size: str | None = None,
        resource_type: str | None = None,
        resource_id: str | None = None,
    ) -> JSONResponse:
        await read_owner(request)
        try:
            _validate_query_parameters(
                request, frozenset({"cursor", "page_size", "resource_type", "resource_id"})
            )
            if cursor is not None and len(cursor) > 256:
                raise EvaluationQueryError("cursor is invalid")
            if page_size is not None and len(page_size) > 8:
                raise EvaluationQueryError("page size is invalid")
            if resource_type is not None and len(resource_type) > 128:
                raise EvaluationQueryError("resource type is invalid")
            if resource_id is not None:
                _uuid(resource_id, "resource_id")
            result = await evaluation.list_history(
                cursor=cursor,
                limit=min(200, max(1, int(page_size or "50"))),
                resource_type=resource_type,
                resource_id=resource_id,
            )
        except EvaluationUnavailable as exc:
            raise HTTPException(status_code=503, detail="service unavailable") from exc
        except (ValueError, EvaluationQueryError) as exc:
            raise HTTPException(status_code=400, detail="invalid request") from exc
        return _no_store(result)

    @app.post("/api/v1/evaluation/deletion-actions", status_code=202)
    async def create_deletion(
        request: Request,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        confirmation: str | None = Header(default=None, alias="X-Observatory-Confirmation"),
    ) -> JSONResponse:
        await command_headers(request, confirmation=confirmation, idempotency_key=idempotency_key)
        value = await body(request)
        if confirmation != "DELETE_EVALUATION_ARTIFACTS":
            raise HTTPException(status_code=400, detail="invalid request")
        try:
            _validate_deletion_request(value)
            result = await evaluation.create_deletion(value, cast(str, idempotency_key))
        except EvaluationConflict as exc:
            raise HTTPException(
                status_code=409, detail="request conflicts with existing state"
            ) from exc
        except EvaluationUnavailable as exc:
            raise HTTPException(status_code=503, detail="service unavailable") from exc
        except EvaluationQueryError as exc:
            raise HTTPException(status_code=422, detail="invalid request") from exc
        return _no_store(result, 202)

    @app.get("/api/v1/evaluation/deletion-actions/{action_id}")
    async def get_deletion(action_id: str, request: Request) -> JSONResponse:
        await read_owner(request)
        try:
            result = await evaluation.get_resource("deletions", _uuid(action_id, "action_id"))
        except EvaluationNotFound as exc:
            raise HTTPException(status_code=404, detail="resource not found") from exc
        except EvaluationUnavailable as exc:
            raise HTTPException(status_code=503, detail="service unavailable") from exc
        return _no_store(result)

    return evaluation


__all__ = ["EvaluationApplicationService", "register_evaluation_routes"]
