"""AURA-0110 evaluation reference projection compatibility tests."""

from __future__ import annotations

import re
from collections.abc import Mapping
from copy import deepcopy

import pytest
from aura_observatory.evaluation_api import (
    _project_definition_replay,  # pyright: ignore[reportPrivateUsage]
)
from aura_observatory.evaluation_query import EvaluationQueryError, project_definition

DATASET = "00000000-0000-4000-8000-000000000101"
CASE = "00000000-0000-4000-8000-000000000102"
SUITE = "00000000-0000-4000-8000-000000000103"
EVALUATOR = "00000000-0000-4000-8000-000000000104"
CREATED = "2026-10-24T00:00:00Z"
PROVENANCE = {
    "createdByComponent": "aura.observatory.test",
    "createdByVersion": "1.0.0",
    "sourceDigest": "0" * 64,
}
EVALUATION_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")


def _case(reference: Mapping[str, object]) -> dict[str, object]:
    return {
        "resourceType": "case",
        "caseId": CASE,
        "caseVersion": "1.0.0",
        "dataset": reference,
        "sourceKind": "synthetic",
        "scopes": ["private"],
        "captureClassification": "metadata_only",
        "inputArtifactIds": [],
        "retentionMode": "manual_only",
        "provenance": PROVENANCE,
        "createdAt": CREATED,
    }


def _suite(reference: Mapping[str, object]) -> dict[str, object]:
    return {
        "resourceType": "suite",
        "suiteId": SUITE,
        "suiteVersion": "1.0.0",
        "dataset": reference,
        "evaluators": [
            {"evaluatorId": EVALUATOR, "evaluatorVersion": "1.0.0", "kind": "deterministic"}
        ],
        "componentIds": ["aura.core"],
        "limits": {"maxCases": 1, "timeoutMs": 1000},
        "retentionMode": "manual_only",
        "provenance": PROVENANCE,
        "createdAt": CREATED,
    }


@pytest.mark.parametrize(
    "reference",
    [
        {"id": DATASET, "version": "1.0.0"},
        {"resourceId": DATASET, "resourceVersion": "1.0.0"},
    ],
)
def test_case_projection_accepts_both_closed_reference_shapes(reference: dict[str, object]) -> None:
    projected = project_definition(_case(reference))
    assert projected["dataset"] == {
        "resourceId": DATASET,
        "resourceVersion": "1.0.0",
    }
    assert EVALUATION_VERSION.fullmatch(projected["dataset"]["resourceVersion"]) is not None  # type: ignore[index]


@pytest.mark.parametrize(
    "reference",
    [
        {"id": DATASET, "version": "1.0.0"},
        {"resourceId": DATASET, "resourceVersion": "1.0.0"},
    ],
)
def test_suite_projection_accepts_both_closed_reference_shapes(
    reference: dict[str, object],
) -> None:
    projected = project_definition(_suite(reference))
    assert projected["dataset"] == {
        "resourceId": DATASET,
        "resourceVersion": "1.0.0",
    }
    assert EVALUATION_VERSION.fullmatch(projected["dataset"]["resourceVersion"]) is not None  # type: ignore[index]


def test_list_and_detail_projection_are_identical_and_do_not_mutate_stored_reference() -> None:
    stored = {"id": DATASET, "version": "1.0.0"}
    case_payload = _case(stored)
    suite_payload = _suite(stored)
    case_before = deepcopy(case_payload)
    suite_before = deepcopy(suite_payload)

    case_list = project_definition(case_payload)
    case_detail = project_definition(case_payload)
    suite_list = project_definition(suite_payload)
    suite_detail = project_definition(suite_payload)

    assert case_list == case_detail
    assert suite_list == suite_detail
    assert case_payload == case_before
    assert suite_payload == suite_before


def test_already_projected_suite_replay_remains_canonical() -> None:
    projected = project_definition(_suite({"resourceId": DATASET, "resourceVersion": "1.0.0"}))
    replay = _project_definition_replay(
        "suite", projected, SUITE, "1.0.0"
    )
    assert replay["dataset"] == {
        "resourceId": DATASET,
        "resourceVersion": "1.0.0",
    }


def test_non_reference_versions_retain_grammar_only_behavior() -> None:
    payload = _suite({"id": DATASET, "version": "1.0.0"})
    payload["evaluators"] = [
        {"evaluatorId": EVALUATOR, "evaluatorVersion": "private", "kind": "deterministic"}
    ]
    projected = project_definition(payload)
    assert projected["evaluators"] == [
        {"evaluatorId": EVALUATOR, "evaluatorVersion": "private", "kind": "deterministic"}
    ]


@pytest.mark.parametrize(
    "reference",
    [
        {"id": DATASET},
        {"version": "1.0.0"},
        {"id": DATASET, "resourceVersion": "1.0.0"},
        {"resourceId": DATASET, "version": "1.0.0"},
        {"id": DATASET, "version": "1.0.0", "resourceId": DATASET, "resourceVersion": "1.0.0"},
        {"id": DATASET, "version": "1.0.0", "private": "must-not-cross-boundary"},
        {"resourceId": DATASET, "resourceVersion": "1.0.0", "secret": "must-not-cross-boundary"},
        {"id": DATASET, "version": None},
        {"id": "not-a-uuid", "version": "1.0.0"},
        {"id": "00000000-0000-4000-8000-00000000010A", "version": "1.0.0"},
        {"id": DATASET, "version": ":2026"},
        {"resourceId": DATASET, "resourceVersion": ".2026"},
        {"id": DATASET, "version": "a" * 65},
        *({"id": DATASET, "version": value} for value in (
            "credential",
            "identity",
            "bearer",
            "private",
            "content",
            "endpoint",
            "url",
            "uri",
            "filename",
            "path",
        )),
        *({"resourceId": DATASET, "resourceVersion": value} for value in (
            "credential",
            "identity",
            "bearer",
            "private",
            "content",
            "endpoint",
            "url",
            "uri",
            "filename",
            "path",
        )),
    ],
)
@pytest.mark.parametrize("resource_factory", [_case, _suite])
def test_invalid_reference_shapes_fail_closed_without_echoing_values(
    reference: dict[str, object], resource_factory: object
) -> None:
    with pytest.raises(EvaluationQueryError) as error:
        project_definition(resource_factory(reference))  # type: ignore[operator]
    assert str(error.value) in {
        "stored dataset is unavailable",
    }
    assert "secret" not in str(error.value).casefold()
    assert "private" not in str(error.value).casefold()
