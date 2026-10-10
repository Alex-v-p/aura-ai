"""Contract and policy guards for Aura's first governed tool slice.

The runtime implementation is intentionally exercised through the public
agent-policy boundary and versioned transport contract.  These tests do not
reach into executor internals or provider request types.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from aura_core.domains.execution.runs.events import new_event
from aura_core.domains.interaction.agents.public import (
    AgentCatalog,
    ConfigurationIdempotencyConflict,
    ConfigurationNotFound,
    ConfigurationVersionConflict,
    ToolGrant,
    ToolPolicy,
)
from aura_core.runtime.tools import (
    MemorySearchContext,
    ToolCall,
    ToolExecutionError,
    ToolExecutor,
    ToolPolicyEvaluator,
    ToolRegistry,
    default_tool_registry,
    validate_tool_arguments,
)

ROOT = Path(__file__).parents[3]


def _openapi() -> dict[str, Any]:
    return cast(
        dict[str, Any],
        json.loads((ROOT / "contracts/openapi/aura-v1.yaml").read_text(encoding="utf-8")),
    )


def _events() -> dict[str, Any]:
    return cast(
        dict[str, Any],
        json.loads(
            (ROOT / "contracts/events/runs/v1/run-events.schema.json").read_text(
                encoding="utf-8"
            )
        ),
    )


def test_memory_search_policy_is_bounded_and_read_only() -> None:
    catalog = AgentCatalog()
    aura = catalog.list_agents()[0]
    policy = catalog.get_tool_policy(aura.current_revision.tool_policy_revision_id)

    assert policy.grants == (ToolGrant("memory.search", 1),)
    assert policy.max_calls_per_run == 1
    assert policy.timeout_ms == 30_000
    assert policy.retry_limit == 0

    with pytest.raises(ValueError):
        ToolPolicy(uuid4(), aura.id, 1, max_calls_per_run=2)
    with pytest.raises(ValueError):
        ToolPolicy(uuid4(), aura.id, 1, grants=policy.grants, max_calls_per_run=0)
    with pytest.raises(ValueError):
        ToolPolicy(uuid4(), aura.id, 1, retry_limit=1)


def test_registry_rejects_duplicate_descriptor_and_exposes_safe_summary() -> None:
    registry = default_tool_registry()
    descriptor = registry.get("memory.search", 1)
    assert descriptor is not None
    assert descriptor.summary()["id"] == "memory.search"
    assert "handler" not in descriptor.summary()
    with pytest.raises(ValueError, match="duplicate tool descriptor"):
        registry.register(descriptor)


def test_policy_evaluator_fails_closed_for_unknown_ungranted_and_over_budget_calls() -> None:
    registry = default_tool_registry()
    evaluator = ToolPolicyEvaluator(registry)
    catalog = AgentCatalog()
    aura = catalog.list_agents()[0]
    policy = catalog.get_tool_policy(aura.current_revision.tool_policy_revision_id)

    assert evaluator.decide(policy, ToolCall("memory.search", {"query": "tea"})).allowed
    assert evaluator.decide(policy, ToolCall("memory.search", {"query": "tea"}), 1).error_code == (
        "TOOL_BUDGET_EXHAUSTED"
    )
    assert evaluator.decide(policy, ToolCall("network.fetch", {"query": "tea"})).error_code == (
        "TOOL_UNKNOWN"
    )
    assert evaluator.decide(None, ToolCall("memory.search", {"query": "tea"})).error_code == (
        "TOOL_NOT_GRANTED"
    )


def test_memory_search_arguments_are_strictly_bounded_and_normalized() -> None:
    descriptor = default_tool_registry().get("memory.search", 1)
    assert descriptor is not None
    assert validate_tool_arguments(descriptor, '{"query":"  tea  "}') == {"query": "tea"}
    for arguments in (
        {},
        {"query": ""},
        {"query": "x" * 201},
        {"query": "tea", "scope": "all"},
        "not-json",
    ):
        with pytest.raises(ToolExecutionError, match="TOOL_ARGUMENTS_INVALID"):
            validate_tool_arguments(descriptor, arguments)


@pytest.mark.asyncio
async def test_executor_success_timeout_and_cancellation_are_typed_and_fail_closed() -> None:
    registry = default_tool_registry()
    executor = ToolExecutor(registry)
    catalog = AgentCatalog()
    aura = catalog.list_agents()[0]
    policy = catalog.get_tool_policy(aura.current_revision.tool_policy_revision_id)

    seen: list[str] = []

    async def search(query: str) -> list[dict[str, str]]:
        seen.append(query)
        return [{"memoryId": "m1", "content": "tea", "scope": "user"}]

    success = await executor.execute(
        policy,
        ToolCall("memory.search", {"query": "tea"}),
        MemorySearchContext(search),
    )
    assert success.status == "completed"
    assert success.result_count == 1
    assert seen == ["tea"]

    async def slow(_query: str) -> list[object]:
        await asyncio.sleep(0.05)
        return []

    timeout = await executor.execute(
        policy,
        ToolCall("memory.search", {"query": "slow"}),
        MemorySearchContext(slow),
        timeout_ms=1,
    )
    assert (timeout.status, timeout.error_class, timeout.error_code) == (
        "failed",
        "timeout",
        "TOOL_TIMEOUT",
    )

    async def cancelled(_query: str) -> list[object]:
        await asyncio.sleep(1)
        return []

    task = asyncio.create_task(
        executor.execute(
            policy,
            ToolCall("memory.search", {"query": "cancel"}),
            MemorySearchContext(cancelled),
        )
    )
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_result",
    [
        {"items": [{"memoryId": "m1", "content": 42, "scope": "user"}], "truncated": False},
        {"items": [{"memoryId": "m1", "content": "memory", "scope": "system"}], "truncated": False},
        {
            "items": [
                {"memoryId": "m1", "content": "memory", "scope": "user", "extra": True}
            ],
            "truncated": False,
        },
        {"items": [], "truncated": "false"},
    ],
)
async def test_executor_rejects_results_that_do_not_match_the_descriptor_schema(
    invalid_result: object,
) -> None:
    """Handlers cannot widen the provider-neutral memory.search result contract."""

    descriptor = default_tool_registry().get("memory.search", 1)
    assert descriptor is not None

    async def malformed(_arguments: object, _context: object) -> object:
        return invalid_result

    registry = ToolRegistry((replace(descriptor, handler=malformed),))
    executor = ToolExecutor(registry)
    policy = ToolPolicy(
        uuid4(), uuid4(), 1, grants=(ToolGrant("memory.search", 1),), max_calls_per_run=1
    )
    result = await executor.execute(
        policy,
        ToolCall("memory.search", {"query": "tea"}),
        MemorySearchContext(lambda _query: malformed({}, None)),
    )

    assert (result.status, result.error_class, result.error_code) == (
        "failed",
        "validation",
        "TOOL_RESULT_INVALID",
    )


def test_policy_grants_are_exactly_versioned_and_deny_unknown_tools() -> None:
    catalog = AgentCatalog()
    aura = catalog.list_agents()[0]

    with pytest.raises(ValueError, match="unknown tool grant"):
        catalog.create_tool_policy(aura.id, (ToolGrant("network.fetch", 1),))
    with pytest.raises(ValueError, match="unknown tool grant"):
        catalog.create_tool_policy(aura.id, (ToolGrant("memory.search", 2),))


def test_policy_creation_idempotency_and_expected_revision_are_deterministic() -> None:
    catalog = AgentCatalog()
    aura = catalog.list_agents()[0]
    grant = (ToolGrant("memory.search", 1),)

    first = catalog.create_tool_policy(
        aura.id, grant, expected_revision=1, idempotency_key="same-request"
    )
    replay = catalog.create_tool_policy(
        aura.id, grant, expected_revision=1, idempotency_key="same-request"
    )
    assert replay is first

    with pytest.raises(ConfigurationIdempotencyConflict):
        catalog.create_tool_policy(
            aura.id, (), expected_revision=1, idempotency_key="same-request"
        )
    with pytest.raises(ConfigurationVersionConflict):
        catalog.create_tool_policy(aura.id, grant, expected_revision=1)


def test_policy_is_owner_scoped_and_attach_requires_new_agent_revision() -> None:
    catalog = AgentCatalog()
    aura = catalog.list_agents()[0]
    custom = catalog.create_agent("custom", "Custom", "purpose", "instructions")
    policy = catalog.create_tool_policy(custom.id, (ToolGrant("memory.search", 1),))
    original = custom.current_revision

    with pytest.raises(ConfigurationNotFound):
        catalog.attach_tool_policy(aura.id, policy.id, aura.version)

    updated = catalog.attach_tool_policy(custom.id, policy.id, custom.version)
    assert updated.current_revision_id != original.id
    assert updated.current_revision.tool_policy_revision_id == policy.id
    assert updated.revisions[0].tool_policy_revision_id != policy.id


def test_tool_catalog_contract_exposes_metadata_without_provider_shapes() -> None:
    spec = _openapi()
    summary = spec["components"]["schemas"]["ToolDescriptorSummary"]
    required = set(summary["required"])
    assert required == {
        "id",
        "version",
        "displayName",
        "description",
        "classification",
        "permissionScope",
        "confirmation",
        "timeoutMs",
        "retryLimit",
        "idempotency",
        "sensitivity",
        "observableComponent",
    }
    assert not {"schema", "handler", "endpoint", "credentials", "result"} & set(
        summary["properties"]
    )
    assert spec["paths"]["/api/v1/tools"]["get"]["operationId"] == "listTools"


def test_tool_status_contract_is_metadata_only_and_replayable() -> None:
    schema = _events()
    status = schema["$defs"]["ToolStatusData"]
    assert status["additionalProperties"] is False
    assert set(status["required"]) == {"callId", "toolId", "toolVersion", "status"}
    assert not {
        "query",
        "arguments",
        "args",
        "content",
        "result",
        "prompt",
        "response",
        "reasoning",
    } & set(status["properties"])
    assert schema["$defs"]["ToolStatus"]["enum"] == [
        "pending",
        "executing",
        "completed",
        "denied",
        "failed",
    ]

    event = new_event(
        "tool.status",
        uuid4(),
        uuid4(),
        0,
        {
            "callId": uuid4(),
            "toolId": "memory.search",
            "toolVersion": 1,
            "status": "completed",
            "resultCount": 1,
        },
    )
    assert event.payload()["eventType"] == "tool.status"
    assert event.payload()["data"]["resultCount"] == 1
    assert isinstance(event.payload()["data"]["callId"], str)
