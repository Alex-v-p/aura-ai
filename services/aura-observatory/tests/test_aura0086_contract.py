"""AURA-0086 component catalogue and dependency contract regressions."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parents[3]
OPENAPI_PATH = ROOT / "contracts/openapi/observatory-v1.yaml"


def _contract() -> dict[str, Any]:
    return json.loads(OPENAPI_PATH.read_text(encoding="utf-8"))


def _parameters(operation: dict[str, Any]) -> dict[str, dict[str, Any]]:
    document = _contract()
    resolved: dict[str, dict[str, Any]] = {}
    for parameter in operation.get("parameters", []):
        if "$ref" in parameter:
            name = parameter["$ref"].rsplit("/", maxsplit=1)[-1]
            parameter = document["components"]["parameters"][name]
        resolved[parameter["name"]] = parameter
    return resolved


def test_catalogue_extension_is_additive_and_bounded() -> None:
    document = _contract()
    operation = document["paths"]["/api/v1/components"]["get"]
    parameters = _parameters(operation)
    schemas = document["components"]["schemas"]

    assert document["info"]["x-contract-id"] == "observatory-v1"
    assert document["info"]["version"] == "1.0.0"
    assert operation["operationId"] == "listComponents"
    assert parameters["page_size"]["schema"]["maximum"] == 200
    assert parameters["cursor"]["schema"]["$ref"].endswith("/Cursor")

    summary = schemas["ComponentSummary"]
    assert summary["additionalProperties"] is False
    # Existing required fields and their meanings remain intact for old clients.
    assert set(summary["required"]) == {
        "componentId",
        "versions",
        "firstSeenAt",
        "lastSeenAt",
        "health",
    }
    assert "definitionState" not in summary["required"]
    assert "definitionVersions" not in summary["required"]
    assert summary["properties"]["versions"]["maxItems"] == 64
    assert summary["properties"]["definitionVersions"]["maxItems"] == 64

    collection = schemas["ComponentCollection"]
    assert collection["properties"]["items"]["maxItems"] == 1000
    assert "nextCursor" not in collection["required"]


def test_exact_version_definition_is_closed_and_metadata_only() -> None:
    document = _contract()
    path = "/api/v1/components/{component_id}/definitions/{component_version}"
    operation = document["paths"][path]["get"]
    definition = document["components"]["schemas"]["ComponentDefinition"]

    assert operation["operationId"] == "getComponentDefinition"
    assert set(_parameters(operation)) == {"component_id", "component_version"}
    assert set(operation["responses"]) >= {"200", "401", "403", "404", "503"}
    assert definition["additionalProperties"] is False
    assert set(definition["required"]) == set(definition["properties"])
    assert definition["properties"]["measurements"]["maxItems"] == 128
    assert definition["properties"]["evaluators"]["maxItems"] == 128
    assert definition["properties"]["dependencies"]["maxItems"] == 64

    serialized = json.dumps(definition).lower()
    forbidden = {
        "prompt",
        "response",
        "document",
        "filename",
        "argument",
        "result",
        "credential",
        "endpoint",
        "token",
        "owneridentity",
    }
    assert not {name for name in forbidden if f'"{name}"' in serialized}


def test_dependency_topology_is_version_aware_bounded_and_explicit() -> None:
    document = _contract()
    path = (
        "/api/v1/components/{component_id}/definitions/"
        "{component_version}/dependencies"
    )
    operation = document["paths"][path]["get"]
    parameters = _parameters(operation)
    schemas = document["components"]["schemas"]

    assert operation["operationId"] == "getComponentDependencies"
    assert parameters["depth"]["schema"] == {
        "type": "integer",
        "minimum": 1,
        "maximum": 4,
        "default": 1,
    }
    assert parameters["max_nodes"]["schema"]["maximum"] == 200

    topology = schemas["ComponentDependencyTopology"]
    assert topology["additionalProperties"] is False
    assert topology["properties"]["nodes"]["maxItems"] == 200
    assert topology["properties"]["edges"]["maxItems"] == 1000
    assert {"rootComponentId", "rootComponentVersion", "partial"} <= set(
        topology["required"]
    )

    node = schemas["DependencyNode"]
    assert node["additionalProperties"] is False
    assert {"definitionState", "availability", "health", "partialHealth"} <= set(
        node["required"]
    )
    assert schemas["ComponentDefinitionState"]["enum"] == [
        "declared",
        "observed_only",
        "missing",
    ]
    assert schemas["DependencyAvailability"]["enum"] == [
        "observed",
        "unobserved",
        "unavailable",
    ]
    assert schemas["DependencyResolution"]["enum"] == [
        "resolved",
        "missing_definition",
        "version_unavailable",
    ]


def test_component_health_can_be_bounded_to_one_deployed_version() -> None:
    operation = _contract()["paths"]["/api/v1/components/{component_id}/health"][
        "get"
    ]
    parameters = _parameters(operation)

    assert parameters["component_version"]["required"] is False
    assert parameters["point_limit"]["schema"] == {
        "type": "integer",
        "minimum": 1,
        "maximum": 1000,
        "default": 200,
    }
