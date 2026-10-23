"""AURA-0104 evaluation-v1 admission mapping invariants."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).parents[2]
EVALUATION_PATH = ROOT / "contracts/evaluation/v1/evaluation.schema.json"
OPENAPI_PATH = ROOT / "contracts/openapi/observatory-v1.yaml"


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def test_local_judge_admission_preserves_evaluation_v1_closed_bounds() -> None:
    evaluation = _load(EVALUATION_PATH)["$defs"]
    transport = _load(OPENAPI_PATH)["components"]["schemas"]
    request = transport["CreateLocalJudgeEvaluatorRequest"]
    definition = evaluation["LocalJudgeDefinition"]

    assert request["additionalProperties"] is False
    for name in (
        "judgeDefinitionId",
        "judgeDefinitionVersion",
        "model",
        "prompt",
        "rubric",
        "calibration",
        "seed",
        "budget",
        "outputContract",
        "defaultDisposition",
        "retentionMode",
        "provenance",
        "createdAt",
    ):
        assert name in request["required"]
        assert name in definition["required"]

    source_budget = evaluation["LocalJudgeBudget"]["properties"]
    transport_budget = transport["EvaluationLocalJudgeBudget"]["properties"]
    assert source_budget == transport_budget
    assert request["properties"]["outputContract"]["const"] == definition[
        "properties"
    ]["outputContract"]["const"]
    assert request["properties"]["defaultDisposition"]["const"] == definition[
        "properties"
    ]["defaultDisposition"]["const"]


def test_content_upload_requires_existing_evaluation_v1_authorization_shape() -> None:
    evaluation = _load(EVALUATION_PATH)["$defs"]
    transport = _load(OPENAPI_PATH)["components"]["schemas"]
    source = evaluation["ContentCaptureAuthorization"]
    admitted = transport["EvaluationContentCaptureAuthorization"]

    assert admitted["additionalProperties"] is False
    assert admitted["required"] == source["required"]
    assert admitted["properties"]["captureClassification"]["enum"] == source[
        "properties"
    ]["captureClassification"]["enum"]
    assert admitted["properties"]["authorizationContext"]["const"] == source[
        "properties"
    ]["authorizationContext"]["const"]
    assert admitted["properties"]["retentionMode"] == {
        "$ref": "#/components/schemas/EvaluationRetentionMode"
    }

    command = transport["CreateEvaluationCaptureAuthorizationRequest"]
    record = transport["EvaluationCaptureAuthorizationRecord"]
    assert command["additionalProperties"] is False
    assert record["additionalProperties"] is False
    for name in source["required"]:
        assert name in command["required"]
        assert name in record["required"]
    assert command["properties"]["captureClassification"]["enum"] == source[
        "properties"
    ]["captureClassification"]["enum"]
    assert record["properties"]["captureClassification"]["enum"] == source[
        "properties"
    ]["captureClassification"]["enum"]


def test_evaluation_v1_contract_identity_remains_stable_and_additive() -> None:
    document = _load(EVALUATION_PATH)
    assert document["$id"] == (
        "https://aura.local/contracts/evaluation/v1/evaluation.schema.json"
    )
    assert document["title"] == "Aura evaluation resources v1"
    assert {"Artifact", "Evaluator", "LocalJudgeDefinition", "Request", "Result", "Score"} <= set(
        document["$defs"]
    )
