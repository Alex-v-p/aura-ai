"""Small governed tool runtime.

Tool descriptors are stable Core contracts.  Handlers are injected at the
composition boundary, which keeps this module independent from persistence
and provider adapters.  The only registered handler is memory.search; its
callback is intentionally supplied per run so an executor cannot accidentally
reuse another run's principal or policy.
"""

# ruff: noqa: E501, UP041

# Policy implementations are supplied by the agent domain at composition
# time.  Their public DTO is intentionally consumed through a narrow dynamic
# seam so this runtime never imports persistence internals.
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownMemberType=false

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from uuid import UUID, uuid4

MEMORY_SEARCH_TOOL_ID = "memory.search"
MEMORY_SEARCH_TOOL_VERSION = 1
MAX_TOOL_RESULT_ITEMS = 8
MAX_TOOL_RESULT_CONTENT = 4096
_SAFE_ERROR_CLASSES = frozenset(
    {"capability", "validation", "policy", "budget", "timeout", "provider_protocol", "execution", "cancellation"}
)
_SAFE_ERROR_CODES = frozenset(
    {
        "TOOL_ARGUMENTS_INVALID",
        "TOOL_BUDGET_EXHAUSTED",
        "TOOL_CANCELLED",
        "TOOL_CAPABILITY_UNAVAILABLE",
        "TOOL_EXECUTION_FAILED",
        "TOOL_HANDLER_UNAVAILABLE",
        "TOOL_LATE_CALL",
        "TOOL_MIXED_STREAM",
        "TOOL_MULTIPLE_CALLS",
        "TOOL_NOT_GRANTED",
        "TOOL_PROVIDER_PROTOCOL",
        "TOOL_RESULT_INVALID",
        "TOOL_TIMEOUT",
        "TOOL_UNKNOWN",
        "MEMORY_SEARCH_UNAVAILABLE",
    }
)


@dataclass(frozen=True, slots=True)
class ToolDescriptor:
    id: str
    version: int
    display_name: str
    description: str
    classification: str
    permission_scope: str
    confirmation: str
    timeout_ms: int
    retry_limit: int
    idempotency: str
    sensitivity: str
    observable_component: str
    input_schema: Mapping[str, object]
    result_schema: Mapping[str, object]
    handler: Callable[[Mapping[str, object], object], Awaitable[object]] | None = field(
        default=None, repr=False, compare=False
    )

    def summary(self) -> dict[str, object]:
        """Return the safe policy-configuration representation."""

        return {
            "id": self.id,
            "version": self.version,
            "displayName": self.display_name,
            "description": self.description,
            "classification": self.classification,
            "permissionScope": self.permission_scope,
            "confirmation": self.confirmation,
            "timeoutMs": self.timeout_ms,
            "retryLimit": self.retry_limit,
            "idempotency": self.idempotency,
            "sensitivity": self.sensitivity,
            "observableComponent": self.observable_component,
        }


@dataclass(frozen=True, slots=True)
class ToolCall:
    """Provider-normalized model request; provider call ids never leave Core."""

    name: str
    arguments: object
    call_id: UUID = field(default_factory=uuid4)


@dataclass(frozen=True, slots=True)
class MemorySearchContext:
    """Per-run callback context used by the memory.search handler."""

    search: Callable[[str], Awaitable[object]]


@dataclass(frozen=True, slots=True)
class ToolExecutionResult:
    call_id: UUID
    tool_id: str
    tool_version: int
    status: str
    result: object | None = None
    error_class: str | None = None
    error_code: str | None = None

    @property
    def result_count(self) -> int | None:
        if isinstance(self.result, Mapping):
            items = self.result.get("items")
            if isinstance(items, Sequence) and not isinstance(items, (str, bytes)):
                return min(len(items), MAX_TOOL_RESULT_ITEMS)
        if isinstance(self.result, Sequence) and not isinstance(self.result, (str, bytes)):
            return min(len(self.result), MAX_TOOL_RESULT_ITEMS)
        return None


class ToolExecutionError(RuntimeError):
    def __init__(self, error_class: str, error_code: str) -> None:
        super().__init__(error_code)
        self.error_class = error_class
        self.error_code = error_code


@dataclass(frozen=True, slots=True)
class ToolPolicyDecision:
    allowed: bool
    error_class: str | None = None
    error_code: str | None = None


class ToolRegistry:
    def __init__(self, descriptors: Sequence[ToolDescriptor] = ()) -> None:
        self._descriptors: dict[tuple[str, int], ToolDescriptor] = {}
        for descriptor in descriptors:
            self.register(descriptor)

    def register(self, descriptor: ToolDescriptor) -> None:
        input_schema = descriptor.input_schema
        result_schema = descriptor.result_schema
        if (descriptor.id, descriptor.version) in self._descriptors:
            raise ValueError("duplicate tool descriptor")
        if (
            not re.fullmatch(r"[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+", descriptor.id)
            or descriptor.version < 1
            or descriptor.classification not in {"read", "mutation"}
            or not re.fullmatch(r"[a-z][a-z0-9_]*:[a-z][a-z0-9_]*", descriptor.permission_scope)
            or descriptor.confirmation not in {"never", "policy", "always"}
            or descriptor.timeout_ms < 1
            or descriptor.timeout_ms > 30_000
            or descriptor.retry_limit < 0
            or descriptor.retry_limit > 3
            or descriptor.idempotency not in {"read_only", "idempotent", "non_idempotent"}
            or descriptor.sensitivity not in {"public", "internal", "private", "restricted"}
            or not re.fullmatch(r"[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+", descriptor.observable_component)
            or not _valid_schema(input_schema)
            or not _valid_schema(result_schema)
        ):
            raise ValueError("invalid tool descriptor limits")
        self._descriptors[(descriptor.id, descriptor.version)] = descriptor

    def get(self, tool_id: str, version: int) -> ToolDescriptor | None:
        return self._descriptors.get((tool_id, version))

    def summaries(self) -> list[dict[str, object]]:
        return [item.summary() for item in self._descriptors.values()]

    def descriptors(self) -> tuple[ToolDescriptor, ...]:
        return tuple(self._descriptors.values())


def _valid_schema(schema: Mapping[str, object]) -> bool:
    return (
        schema.get("$schema") == "https://json-schema.org/draft/2020-12/schema"
        and isinstance(schema.get("type"), str)
        and isinstance(schema.get("properties"), Mapping)
        and isinstance(schema.get("required"), list)
        and schema.get("additionalProperties") is False
    )


def _grants(policy: object | None) -> set[tuple[str, int]]:
    if policy is None:
        return set()
    raw = getattr(policy, "grants", ())
    result: set[tuple[str, int]] = set()
    if isinstance(raw, Mapping):
        raw = raw.get("items", ())
    if not isinstance(raw, (list, tuple, set, frozenset)):
        return result
    for grant in raw:
        if isinstance(grant, Mapping):
            tool_id, version = grant.get("toolId", grant.get("tool_id")), grant.get(
                "toolVersion", grant.get("tool_version")
            )
        else:
            tool_id, version = getattr(grant, "tool_id", None), getattr(grant, "tool_version", None)
        if isinstance(tool_id, str) and type(version) is int:
            result.add((tool_id, version))
    return result


class ToolPolicyEvaluator:
    def __init__(self, registry: ToolRegistry) -> None:
        self.registry = registry

    def decide(self, policy: object | None, call: ToolCall, calls_used: int = 0) -> ToolPolicyDecision:
        descriptor = self.registry.get(call.name, 1)
        if descriptor is None:
            return ToolPolicyDecision(False, "policy", "TOOL_UNKNOWN")
        if (descriptor.id, descriptor.version) not in _grants(policy):
            return ToolPolicyDecision(False, "policy", "TOOL_NOT_GRANTED")
        limit = getattr(policy, "max_calls_per_run", 1)
        if type(limit) is not int or calls_used >= limit:
            return ToolPolicyDecision(False, "budget", "TOOL_BUDGET_EXHAUSTED")
        return ToolPolicyDecision(True)


def validate_tool_arguments(descriptor: ToolDescriptor, arguments: object) -> dict[str, object]:
    """Validate the deliberately tiny v1 memory.search input schema."""

    value: object = arguments
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ToolExecutionError("validation", "TOOL_ARGUMENTS_INVALID") from exc
    if not isinstance(value, Mapping):
        raise ToolExecutionError("validation", "TOOL_ARGUMENTS_INVALID")
    if set(value) != {"query"} or not isinstance(value.get("query"), str):
        raise ToolExecutionError("validation", "TOOL_ARGUMENTS_INVALID")
    query = value["query"].strip()
    if not 1 <= len(query) <= 200:
        raise ToolExecutionError("validation", "TOOL_ARGUMENTS_INVALID")
    return {"query": query}


def validate_tool_result(descriptor: ToolDescriptor, result: object) -> dict[str, object]:
    """Validate the bounded, typed result contract before completion is emitted."""

    if descriptor.id != MEMORY_SEARCH_TOOL_ID or descriptor.version != MEMORY_SEARCH_TOOL_VERSION:
        if not isinstance(result, Mapping):
            raise ToolExecutionError("validation", "TOOL_RESULT_INVALID")
        return dict(result)
    if not isinstance(result, Mapping) or set(result) != {"items", "truncated"}:
        raise ToolExecutionError("validation", "TOOL_RESULT_INVALID")
    raw_items = result.get("items")
    truncated = result.get("truncated")
    if not isinstance(raw_items, Sequence) or isinstance(raw_items, (str, bytes)):
        raise ToolExecutionError("validation", "TOOL_RESULT_INVALID")
    if len(raw_items) > MAX_TOOL_RESULT_ITEMS or type(truncated) is not bool:
        raise ToolExecutionError("validation", "TOOL_RESULT_INVALID")
    items: list[dict[str, str]] = []
    for item in raw_items:
        if not isinstance(item, Mapping) or set(item) != {"memoryId", "content", "scope"}:
            raise ToolExecutionError("validation", "TOOL_RESULT_INVALID")
        memory_id, content, scope = item["memoryId"], item["content"], item["scope"]
        if (
            not isinstance(memory_id, str)
            or not 1 <= len(memory_id) <= 255
            or not isinstance(content, str)
            or not 1 <= len(content) <= MAX_TOOL_RESULT_CONTENT
            or scope not in {"user", "agent"}
        ):
            raise ToolExecutionError("validation", "TOOL_RESULT_INVALID")
        items.append({"memoryId": memory_id, "content": content, "scope": scope})
    return {"items": items, "truncated": truncated}


class ToolExecutor:
    def __init__(self, registry: ToolRegistry, *, evaluator: ToolPolicyEvaluator | None = None) -> None:
        self.registry = registry
        self.evaluator = evaluator or ToolPolicyEvaluator(registry)

    async def execute(
        self,
        policy: object | None,
        call: ToolCall,
        context: object,
        *,
        calls_used: int = 0,
        timeout_ms: int | None = None,
    ) -> ToolExecutionResult:
        descriptor = self.registry.get(call.name, 1)
        decision = self.evaluator.decide(policy, call, calls_used)
        if not decision.allowed or descriptor is None:
            return ToolExecutionResult(
                call.call_id,
                call.name,
                descriptor.version if descriptor else 1,
                "denied",
                error_class=decision.error_class,
                error_code=decision.error_code,
            )
        try:
            arguments = validate_tool_arguments(descriptor, call.arguments)
            if descriptor.handler is None:
                raise ToolExecutionError("execution", "TOOL_HANDLER_UNAVAILABLE")
            timeout = max(1, min(timeout_ms or descriptor.timeout_ms, descriptor.timeout_ms)) / 1000
            value = await asyncio.wait_for(descriptor.handler(arguments, context), timeout=timeout)
            value = validate_tool_result(descriptor, value)
            return ToolExecutionResult(call.call_id, descriptor.id, descriptor.version, "completed", value)
        except asyncio.TimeoutError:
            return ToolExecutionResult(call.call_id, descriptor.id, descriptor.version, "failed", error_class="timeout", error_code="TOOL_TIMEOUT")
        except asyncio.CancelledError:
            raise
        except ToolExecutionError as exc:
            error_class = (
                exc.error_class if exc.error_class in _SAFE_ERROR_CLASSES else "execution"
            )
            error_code = (
                exc.error_code
                if exc.error_code in _SAFE_ERROR_CODES
                else "TOOL_EXECUTION_FAILED"
            )
            return ToolExecutionResult(
                call.call_id,
                descriptor.id,
                descriptor.version,
                "failed",
                error_class=error_class,
                error_code=error_code,
            )
        except Exception:
            return ToolExecutionResult(call.call_id, descriptor.id, descriptor.version, "failed", error_class="execution", error_code="TOOL_EXECUTION_FAILED")


def _memory_handler(arguments: Mapping[str, object], context: object) -> Awaitable[object]:
    if not isinstance(context, MemorySearchContext):
        async def unavailable() -> Sequence[object]:
            raise ToolExecutionError("execution", "MEMORY_SEARCH_UNAVAILABLE")
        return unavailable()

    async def search() -> Mapping[str, object]:
        raw = await context.search(str(arguments["query"]))
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
            raise ToolExecutionError("validation", "TOOL_RESULT_INVALID")
        items: list[dict[str, object]] = []
        seen_ids: set[str] = set()
        dropped = False
        for item in raw[:MAX_TOOL_RESULT_ITEMS]:
            memory_id = getattr(item, "memory_id", None)
            content = getattr(item, "content", None)
            scope = getattr(item, "scope_type", getattr(item, "scope", "user"))
            if isinstance(item, Mapping):
                memory_id = item.get("memoryId", item.get("memory_id"))
                content = item.get("content")
                scope = item.get("scope", "user")
            scope_value = getattr(scope, "value", scope)
            if (
                not isinstance(memory_id, (str, UUID))
                or not isinstance(content, str)
                or not content.strip()
                or not isinstance(scope_value, str)
            ):
                raise ToolExecutionError("validation", "TOOL_RESULT_INVALID")
            canonical_id = str(memory_id)
            if canonical_id in seen_ids:
                dropped = True
                continue
            seen_ids.add(canonical_id)
            items.append(
                {
                    "memoryId": canonical_id,
                    "content": content[:MAX_TOOL_RESULT_CONTENT],
                    "scope": scope_value,
                }
            )
        return {"items": items, "truncated": len(raw) > MAX_TOOL_RESULT_ITEMS or dropped}
    return search()


_MEMORY_INPUT_SCHEMA: Mapping[str, object] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ["query"],
    "properties": {"query": {"type": "string", "minLength": 1, "maxLength": 200}},
}
_MEMORY_RESULT_SCHEMA: Mapping[str, object] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ["items", "truncated"],
    "properties": {
        "items": {
            "type": "array",
            "maxItems": 8,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["memoryId", "content", "scope"],
                "properties": {
                    "memoryId": {"type": "string", "minLength": 1, "maxLength": 255},
                    "content": {"type": "string", "minLength": 1, "maxLength": 4096},
                    "scope": {"type": "string", "enum": ["user", "agent"]},
                },
            },
        },
        "truncated": {"type": "boolean"},
    },
}


def default_tool_registry() -> ToolRegistry:
    return ToolRegistry(
        (
            ToolDescriptor(
                id=MEMORY_SEARCH_TOOL_ID,
                version=MEMORY_SEARCH_TOOL_VERSION,
                display_name="Search memory",
                description="Search owner-authorized Aura memory for relevant context.",
                classification="read",
                permission_scope="memory:read",
                confirmation="never",
                timeout_ms=30_000,
                retry_limit=0,
                idempotency="read_only",
                sensitivity="private",
                observable_component="aura.runtime.tool_execution",
                input_schema=_MEMORY_INPUT_SCHEMA,
                result_schema=_MEMORY_RESULT_SCHEMA,
                handler=_memory_handler,
            ),
        )
    )
