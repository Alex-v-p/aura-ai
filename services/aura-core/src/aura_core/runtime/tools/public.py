"""Small governed tool runtime.

Tool descriptors are stable Core contracts.  Handlers are injected at the
composition boundary, which keeps this module independent from persistence
and provider adapters. Executable read callbacks are supplied per run so an
executor cannot accidentally reuse another run's principal or policy.
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
from datetime import UTC, datetime
from uuid import UUID, uuid4

MEMORY_SEARCH_TOOL_ID = "memory.search"
MEMORY_SEARCH_TOOL_VERSION = 1
REMINDER_CREATE_TOOL_ID = "reminder.create"
REMINDER_CREATE_TOOL_VERSION = 1
REMINDER_LIST_TOOL_ID = "reminder.list"
REMINDER_LIST_TOOL_VERSION = 1
REMINDER_INSPECT_TOOL_ID = "reminder.inspect"
REMINDER_INSPECT_TOOL_VERSION = 1
REMINDER_SERIES_CREATE_TOOL_ID = "reminder.series.create"
REMINDER_SERIES_CREATE_TOOL_VERSION = 1
REMINDER_RESCHEDULE_TOOL_ID = "reminder.reschedule"
REMINDER_RESCHEDULE_TOOL_VERSION = 1
REMINDER_SERIES_UPDATE_TOOL_ID = "reminder.series.update"
REMINDER_SERIES_UPDATE_TOOL_VERSION = 1
REMINDER_SERIES_PAUSE_TOOL_ID = "reminder.series.pause"
REMINDER_SERIES_PAUSE_TOOL_VERSION = 1
REMINDER_SERIES_RESUME_TOOL_ID = "reminder.series.resume"
REMINDER_SERIES_RESUME_TOOL_VERSION = 1
REMINDER_CANCEL_TOOL_ID = "reminder.cancel"
REMINDER_CANCEL_TOOL_VERSION = 1
REMINDER_TOOL_IDS = frozenset(
    {
        REMINDER_CREATE_TOOL_ID,
        REMINDER_LIST_TOOL_ID,
        REMINDER_INSPECT_TOOL_ID,
        REMINDER_SERIES_CREATE_TOOL_ID,
        REMINDER_RESCHEDULE_TOOL_ID,
        REMINDER_SERIES_UPDATE_TOOL_ID,
        REMINDER_SERIES_PAUSE_TOOL_ID,
        REMINDER_SERIES_RESUME_TOOL_ID,
        REMINDER_CANCEL_TOOL_ID,
    }
)
REMINDER_MUTATION_TOOL_IDS = frozenset(
    {
        REMINDER_CREATE_TOOL_ID,
        REMINDER_SERIES_CREATE_TOOL_ID,
        REMINDER_RESCHEDULE_TOOL_ID,
        REMINDER_SERIES_UPDATE_TOOL_ID,
        REMINDER_SERIES_PAUSE_TOOL_ID,
        REMINDER_SERIES_RESUME_TOOL_ID,
        REMINDER_CANCEL_TOOL_ID,
    }
)
MAX_TOOL_RESULT_ITEMS = 8
MAX_REMINDER_TOOL_RESULT_ITEMS = 10
MAX_TOOL_RESULT_CONTENT = 4096
_SAFE_ERROR_CLASSES = frozenset(
    {
        "capability",
        "validation",
        "policy",
        "budget",
        "timeout",
        "provider_protocol",
        "execution",
        "cancellation",
    }
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
        "TOOL_NOT_APPROVED",
        "TOOL_PERMISSION_DENIED",
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
class ReminderCreateContext:
    """Proposal-only callback; approval owns all reminder side effects."""

    propose: Callable[[str, datetime, UUID], Awaitable[object]]


@dataclass(frozen=True, slots=True)
class ReminderReadContext:
    """Owner-pinned callbacks for conversational reminder reads.

    The runtime deliberately knows neither principals nor reminder storage.
    The run coordinator supplies callbacks already bound to the authenticated
    owner, which prevents model arguments from selecting an owner boundary.
    """

    list_reminders: Callable[[str | None, str | None, int], Awaitable[object]]
    inspect_reminder: Callable[[str], Awaitable[object]]


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
        # The private reminder.list result may contain ten items, but the
        # closed run-events v1 metadata contract permits resultCount <= 8.
        maximum = MAX_TOOL_RESULT_ITEMS
        if isinstance(self.result, Mapping):
            items = self.result.get("items")
            if isinstance(items, Sequence) and not isinstance(items, (str, bytes)):
                return min(len(items), maximum)
        if isinstance(self.result, Sequence) and not isinstance(self.result, (str, bytes)):
            return min(len(self.result), maximum)
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
            or not re.fullmatch(
                r"[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+", descriptor.observable_component
            )
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
            tool_id, version = (
                grant.get("toolId", grant.get("tool_id")),
                grant.get("toolVersion", grant.get("tool_version")),
            )
        else:
            tool_id, version = getattr(grant, "tool_id", None), getattr(grant, "tool_version", None)
        if isinstance(tool_id, str) and type(version) is int:
            result.add((tool_id, version))
    return result


class ToolPolicyEvaluator:
    def __init__(self, registry: ToolRegistry) -> None:
        self.registry = registry

    def decide(
        self, policy: object | None, call: ToolCall, calls_used: int = 0
    ) -> ToolPolicyDecision:
        descriptor = self.registry.get(call.name, 1)
        if descriptor is None:
            return ToolPolicyDecision(False, "policy", "TOOL_UNKNOWN")
        if (descriptor.id, descriptor.version) not in _grants(policy):
            return ToolPolicyDecision(False, "policy", "TOOL_NOT_GRANTED")
        scopes = getattr(policy, "permission_scopes", frozenset())
        if not isinstance(scopes, (set, frozenset, tuple, list)):
            scopes = ()
        # Legacy policies predate explicit scope snapshots.  Preserve their
        # read-only memory grant while requiring an explicit reminders:write
        # scope for every confirmation-backed mutation.
        if descriptor.permission_scope == "reminders:write" and "reminders:write" not in scopes:
            return ToolPolicyDecision(False, "policy", "TOOL_PERMISSION_DENIED")
        if (
            descriptor.permission_scope != "reminders:write"
            and scopes
            and descriptor.permission_scope not in scopes
        ):
            return ToolPolicyDecision(False, "policy", "TOOL_PERMISSION_DENIED")
        limit = getattr(policy, "max_calls_per_run", 1)
        if type(limit) is not int or calls_used >= limit:
            return ToolPolicyDecision(False, "budget", "TOOL_BUDGET_EXHAUSTED")
        return ToolPolicyDecision(True)


def validate_tool_arguments(descriptor: ToolDescriptor, arguments: object) -> dict[str, object]:
    """Validate typed provider arguments before any domain callback."""

    value: object = arguments
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ToolExecutionError("validation", "TOOL_ARGUMENTS_INVALID") from exc
    if not isinstance(value, Mapping):
        raise ToolExecutionError("validation", "TOOL_ARGUMENTS_INVALID")
    if descriptor.id == MEMORY_SEARCH_TOOL_ID:
        if set(value) != {"query"} or not isinstance(value.get("query"), str):
            raise ToolExecutionError("validation", "TOOL_ARGUMENTS_INVALID")
        query = value["query"].strip()
        if not 1 <= len(query) <= 200:
            raise ToolExecutionError("validation", "TOOL_ARGUMENTS_INVALID")
        return {"query": query}
    if descriptor.id == REMINDER_CREATE_TOOL_ID:
        if set(value) != {"content", "dueAt"} or not isinstance(value.get("content"), str):
            raise ToolExecutionError("validation", "TOOL_ARGUMENTS_INVALID")
        content = value["content"].strip()
        if not 1 <= len(content) <= 2000 or not isinstance(value.get("dueAt"), str):
            raise ToolExecutionError("validation", "TOOL_ARGUMENTS_INVALID")
        try:
            due_at = datetime.fromisoformat(value["dueAt"].replace("Z", "+00:00"))
            if due_at.tzinfo is None or due_at.utcoffset() is None:
                raise ValueError
            due_at = due_at.astimezone(UTC)
            if due_at <= datetime.now(UTC):
                raise ValueError
        except (TypeError, ValueError) as exc:
            raise ToolExecutionError("validation", "TOOL_ARGUMENTS_INVALID") from exc
        return {"content": content, "dueAt": due_at.isoformat().replace("+00:00", "Z")}
    if descriptor.id == REMINDER_LIST_TOOL_ID:
        if not set(value) <= {"query", "state", "limit"}:
            raise ToolExecutionError("validation", "TOOL_ARGUMENTS_INVALID")
        query = value.get("query")
        state = value.get("state")
        limit = value.get("limit", MAX_REMINDER_TOOL_RESULT_ITEMS)
        if query is not None:
            if not isinstance(query, str) or not 1 <= len(query.strip()) <= 200:
                raise ToolExecutionError("validation", "TOOL_ARGUMENTS_INVALID")
            query = query.strip()
        if state is not None and state not in {
            "active",
            "paused",
            "scheduled",
            "fired",
            "completed",
            "canceled",
        }:
            raise ToolExecutionError("validation", "TOOL_ARGUMENTS_INVALID")
        if type(limit) is not int or not 1 <= limit <= MAX_REMINDER_TOOL_RESULT_ITEMS:
            raise ToolExecutionError("validation", "TOOL_ARGUMENTS_INVALID")
        return {"query": query, "state": state, "limit": limit}
    if descriptor.id == REMINDER_INSPECT_TOOL_ID:
        return {"target": _normalized_target(value, {"target"})}
    if descriptor.id == REMINDER_SERIES_CREATE_TOOL_ID:
        if set(value) != {"content", "localStart", "recurrence"}:
            raise ToolExecutionError("validation", "TOOL_ARGUMENTS_INVALID")
        return {
            "content": _normalized_content(value.get("content")),
            "localStart": _normalized_local_datetime(value.get("localStart")),
            "recurrence": _normalized_recurrence(value.get("recurrence")),
        }
    if descriptor.id == REMINDER_RESCHEDULE_TOOL_ID:
        target = _normalized_target(value, {"target", "dueAt"})
        return {"target": target, "dueAt": _normalized_local_datetime(value.get("dueAt"))}
    if descriptor.id == REMINDER_SERIES_UPDATE_TOOL_ID:
        if not set(value) <= {"target", "content", "localStart", "recurrence"} or set(value) == {
            "target"
        }:
            raise ToolExecutionError("validation", "TOOL_ARGUMENTS_INVALID")
        target = _normalized_target(value, set(value), required={"target"})
        normalized: dict[str, object] = {"target": target}
        if "content" in value:
            normalized["content"] = _normalized_content(value.get("content"))
        if "localStart" in value:
            normalized["localStart"] = _normalized_local_datetime(value.get("localStart"))
        if "recurrence" in value:
            normalized["recurrence"] = _normalized_recurrence(value.get("recurrence"))
        return normalized
    if descriptor.id in {REMINDER_SERIES_PAUSE_TOOL_ID, REMINDER_SERIES_RESUME_TOOL_ID}:
        return {"target": _normalized_target(value, {"target"})}
    if descriptor.id == REMINDER_CANCEL_TOOL_ID:
        target = _normalized_target(value, {"target", "scope"})
        scope = value.get("scope")
        if scope not in {"occurrence", "series"}:
            raise ToolExecutionError("validation", "TOOL_ARGUMENTS_INVALID")
        return {"target": target, "scope": scope}
    raise ToolExecutionError("validation", "TOOL_ARGUMENTS_INVALID")


def _normalized_target(
    value: Mapping[str, object],
    allowed: set[str],
    *,
    required: set[str] | None = None,
) -> str:
    if not (required or allowed) <= set(value) or not set(value) <= allowed:
        raise ToolExecutionError("validation", "TOOL_ARGUMENTS_INVALID")
    target = value.get("target")
    if not isinstance(target, str) or not 1 <= len(target.strip()) <= 200:
        raise ToolExecutionError("validation", "TOOL_ARGUMENTS_INVALID")
    return target.strip()


def _normalized_content(value: object) -> str:
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 2000:
        raise ToolExecutionError("validation", "TOOL_ARGUMENTS_INVALID")
    return value.strip()


def _normalized_local_datetime(value: object) -> str:
    if not isinstance(value, str) or len(value) > 64 or "T" not in value:
        raise ToolExecutionError("validation", "TOOL_ARGUMENTS_INVALID")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ToolExecutionError("validation", "TOOL_ARGUMENTS_INVALID") from exc
    # Conversational wall-clock values are interpreted only through the
    # validated timezone pinned on the run. A model-provided offset must not
    # choose a different instant or bypass that authority.
    if parsed.tzinfo is not None or parsed.utcoffset() is not None:
        raise ToolExecutionError("validation", "TOOL_ARGUMENTS_INVALID")
    return parsed.isoformat()


def _normalized_recurrence(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ToolExecutionError("validation", "TOOL_ARGUMENTS_INVALID")
    allowed = {"frequency", "interval", "weekdays", "monthDay", "count", "endDate"}
    if not set(value) <= allowed or "frequency" not in value:
        raise ToolExecutionError("validation", "TOOL_ARGUMENTS_INVALID")
    frequency = value.get("frequency")
    interval = value.get("interval", 1)
    if frequency not in {"daily", "selected_weekday", "weekly", "monthly"}:
        raise ToolExecutionError("validation", "TOOL_ARGUMENTS_INVALID")
    if type(interval) is not int or not 1 <= interval <= 365:
        raise ToolExecutionError("validation", "TOOL_ARGUMENTS_INVALID")
    normalized: dict[str, object] = {"frequency": frequency, "interval": interval}
    weekdays = value.get("weekdays")
    if weekdays is not None:
        if (
            not isinstance(weekdays, Sequence)
            or isinstance(weekdays, (str, bytes))
            or any(type(day) is not int or not 1 <= day <= 7 for day in weekdays)
        ):
            raise ToolExecutionError("validation", "TOOL_ARGUMENTS_INVALID")
        normalized["weekdays"] = sorted(set(weekdays))
    month_day = value.get("monthDay")
    if month_day is not None:
        if type(month_day) is not int or not 1 <= month_day <= 31:
            raise ToolExecutionError("validation", "TOOL_ARGUMENTS_INVALID")
        normalized["monthDay"] = month_day
    count = value.get("count")
    if count is not None:
        if type(count) is not int or not 1 <= count <= 10_000:
            raise ToolExecutionError("validation", "TOOL_ARGUMENTS_INVALID")
        normalized["count"] = count
    end_date = value.get("endDate")
    if end_date is not None:
        if not isinstance(end_date, str):
            raise ToolExecutionError("validation", "TOOL_ARGUMENTS_INVALID")
        try:
            datetime.fromisoformat(end_date)
        except ValueError as exc:
            raise ToolExecutionError("validation", "TOOL_ARGUMENTS_INVALID") from exc
        normalized["endDate"] = end_date
    weekday_frequency = frequency in {"selected_weekday", "weekly"}
    if weekday_frequency != bool(normalized.get("weekdays")):
        raise ToolExecutionError("validation", "TOOL_ARGUMENTS_INVALID")
    monthly = frequency == "monthly"
    if monthly != ("monthDay" in normalized):
        raise ToolExecutionError("validation", "TOOL_ARGUMENTS_INVALID")
    if "count" in normalized and "endDate" in normalized:
        raise ToolExecutionError("validation", "TOOL_ARGUMENTS_INVALID")
    return normalized


def validate_tool_result(descriptor: ToolDescriptor, result: object) -> dict[str, object]:
    """Validate the bounded, typed result contract before completion is emitted."""

    if descriptor.id in {REMINDER_LIST_TOOL_ID, REMINDER_INSPECT_TOOL_ID}:
        return _validate_reminder_read_result(descriptor, result)
    if descriptor.id in REMINDER_MUTATION_TOOL_IDS - {REMINDER_CREATE_TOOL_ID}:
        if (
            not isinstance(result, Mapping)
            or set(result) != {"status", "command"}
            or result.get("status") != "completed"
            or result.get("command") != descriptor.id
        ):
            raise ToolExecutionError("validation", "TOOL_RESULT_INVALID")
        return {"status": "completed", "command": descriptor.id}
    if descriptor.id != MEMORY_SEARCH_TOOL_ID or descriptor.version != MEMORY_SEARCH_TOOL_VERSION:
        if (
            descriptor.id == REMINDER_CREATE_TOOL_ID
            and descriptor.version == REMINDER_CREATE_TOOL_VERSION
        ):
            if not isinstance(result, Mapping) or set(result) != {"reminderId", "status", "dueAt"}:
                raise ToolExecutionError("validation", "TOOL_RESULT_INVALID")
            if not all(isinstance(result.get(key), str) and result.get(key) for key in result):
                raise ToolExecutionError("validation", "TOOL_RESULT_INVALID")
            try:
                UUID(str(result["reminderId"]))
                datetime.fromisoformat(str(result["dueAt"]).replace("Z", "+00:00"))
            except (TypeError, ValueError) as exc:
                raise ToolExecutionError("validation", "TOOL_RESULT_INVALID") from exc
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


def _validate_reminder_read_result(descriptor: ToolDescriptor, result: object) -> dict[str, object]:
    if not isinstance(result, Mapping) or set(result) != {"outcome", "items"}:
        raise ToolExecutionError("validation", "TOOL_RESULT_INVALID")
    outcome = result.get("outcome")
    raw_items = result.get("items")
    if outcome not in {"resolved", "clarification"} or not isinstance(raw_items, Sequence):
        raise ToolExecutionError("validation", "TOOL_RESULT_INVALID")
    if isinstance(raw_items, (str, bytes)):
        raise ToolExecutionError("validation", "TOOL_RESULT_INVALID")
    maximum = 1 if descriptor.id == REMINDER_INSPECT_TOOL_ID else MAX_REMINDER_TOOL_RESULT_ITEMS
    if len(raw_items) > maximum or (outcome == "clarification" and raw_items):
        raise ToolExecutionError("validation", "TOOL_RESULT_INVALID")
    items: list[dict[str, object]] = []
    for raw in raw_items:
        if not isinstance(raw, Mapping):
            raise ToolExecutionError("validation", "TOOL_RESULT_INVALID")
        expected = {"id", "type", "version", "content", "status"}
        if set(raw) != expected:
            raise ToolExecutionError("validation", "TOOL_RESULT_INVALID")
        try:
            UUID(str(raw["id"]))
        except (TypeError, ValueError) as exc:
            raise ToolExecutionError("validation", "TOOL_RESULT_INVALID") from exc
        if raw.get("type") not in {"reminder", "series"}:
            raise ToolExecutionError("validation", "TOOL_RESULT_INVALID")
        version = raw.get("version")
        if type(version) is not int or version < 1:
            raise ToolExecutionError("validation", "TOOL_RESULT_INVALID")
        content = raw.get("content")
        if not isinstance(content, str) or not 1 <= len(content) <= 2000:
            raise ToolExecutionError("validation", "TOOL_RESULT_INVALID")
        status = raw.get("status")
        if not isinstance(status, str) or not status:
            raise ToolExecutionError("validation", "TOOL_RESULT_INVALID")
        items.append(dict(raw))
    if descriptor.id == REMINDER_INSPECT_TOOL_ID and outcome == "resolved" and len(items) != 1:
        raise ToolExecutionError("validation", "TOOL_RESULT_INVALID")
    return {"outcome": outcome, "items": items}


class ToolExecutor:
    def __init__(
        self, registry: ToolRegistry, *, evaluator: ToolPolicyEvaluator | None = None
    ) -> None:
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
        if descriptor.confirmation == "always":
            try:
                validate_tool_arguments(descriptor, call.arguments)
            except ToolExecutionError as exc:
                return ToolExecutionResult(
                    call.call_id,
                    descriptor.id,
                    descriptor.version,
                    "failed",
                    error_class="validation",
                    error_code=exc.error_code,
                )
            return ToolExecutionResult(
                call.call_id,
                descriptor.id,
                descriptor.version,
                "awaiting_confirmation",
            )
        try:
            arguments = validate_tool_arguments(descriptor, call.arguments)
            if descriptor.handler is None:
                raise ToolExecutionError("execution", "TOOL_HANDLER_UNAVAILABLE")
            timeout = max(1, min(timeout_ms or descriptor.timeout_ms, descriptor.timeout_ms)) / 1000
            value = await asyncio.wait_for(descriptor.handler(arguments, context), timeout=timeout)
            value = validate_tool_result(descriptor, value)
            return ToolExecutionResult(
                call.call_id, descriptor.id, descriptor.version, "completed", value
            )
        except asyncio.TimeoutError:
            return ToolExecutionResult(
                call.call_id,
                descriptor.id,
                descriptor.version,
                "failed",
                error_class="timeout",
                error_code="TOOL_TIMEOUT",
            )
        except asyncio.CancelledError:
            raise
        except ToolExecutionError as exc:
            error_class = exc.error_class if exc.error_class in _SAFE_ERROR_CLASSES else "execution"
            error_code = (
                exc.error_code if exc.error_code in _SAFE_ERROR_CODES else "TOOL_EXECUTION_FAILED"
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
            return ToolExecutionResult(
                call.call_id,
                descriptor.id,
                descriptor.version,
                "failed",
                error_class="execution",
                error_code="TOOL_EXECUTION_FAILED",
            )


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


def _reminder_handler(arguments: Mapping[str, object], context: object) -> Awaitable[object]:
    if not isinstance(context, ReminderCreateContext):

        async def unavailable() -> Mapping[str, object]:
            raise ToolExecutionError("execution", "TOOL_HANDLER_UNAVAILABLE")

        return unavailable()

    async def propose() -> object:
        due_at = datetime.fromisoformat(str(arguments["dueAt"]).replace("Z", "+00:00"))
        return await context.propose(str(arguments["content"]), due_at, uuid4())

    return propose()


def _reminder_list_handler(arguments: Mapping[str, object], context: object) -> Awaitable[object]:
    if not isinstance(context, ReminderReadContext):

        async def unavailable() -> Mapping[str, object]:
            raise ToolExecutionError("execution", "TOOL_HANDLER_UNAVAILABLE")

        return unavailable()
    raw_query = arguments.get("query")
    raw_state = arguments.get("state")
    raw_limit = arguments.get("limit")
    query = raw_query if isinstance(raw_query, str) else None
    state = raw_state if isinstance(raw_state, str) else None
    if type(raw_limit) is not int:

        async def invalid() -> Mapping[str, object]:
            raise ToolExecutionError("validation", "TOOL_ARGUMENTS_INVALID")

        return invalid()
    return context.list_reminders(query, state, raw_limit)


def _reminder_inspect_handler(
    arguments: Mapping[str, object], context: object
) -> Awaitable[object]:
    if not isinstance(context, ReminderReadContext):

        async def unavailable() -> Mapping[str, object]:
            raise ToolExecutionError("execution", "TOOL_HANDLER_UNAVAILABLE")

        return unavailable()
    return context.inspect_reminder(str(arguments["target"]))


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
_REMINDER_INPUT_SCHEMA: Mapping[str, object] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ["content", "dueAt"],
    "properties": {
        "content": {"type": "string", "minLength": 1, "maxLength": 2000},
        "dueAt": {"type": "string", "format": "date-time"},
    },
}
_REMINDER_RESULT_SCHEMA: Mapping[str, object] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ["reminderId", "status", "dueAt"],
    "properties": {
        "reminderId": {"type": "string", "format": "uuid"},
        "status": {"type": "string"},
        "dueAt": {"type": "string", "format": "date-time"},
    },
}
_REMINDER_LIST_INPUT_SCHEMA: Mapping[str, object] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": [],
    "properties": {
        "query": {"type": "string", "minLength": 1, "maxLength": 200},
        "state": {
            "type": "string",
            "enum": ["active", "paused", "scheduled", "fired", "completed", "canceled"],
        },
        "limit": {"type": "integer", "minimum": 1, "maximum": 10, "default": 10},
    },
}
_REMINDER_TARGET_INPUT_SCHEMA: Mapping[str, object] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ["target"],
    "properties": {"target": {"type": "string", "minLength": 1, "maxLength": 200}},
}
_REMINDER_READ_RESULT_SCHEMA: Mapping[str, object] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ["outcome", "items"],
    "properties": {
        "outcome": {"type": "string", "enum": ["resolved", "clarification"]},
        "items": {
            "type": "array",
            "maxItems": 10,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "type", "content", "status", "version"],
                "properties": {
                    "id": {"type": "string", "format": "uuid"},
                    "type": {"type": "string", "enum": ["reminder", "series"]},
                    "content": {"type": "string", "minLength": 1, "maxLength": 2000},
                    "status": {"type": "string"},
                    "version": {"type": "integer", "minimum": 1},
                },
            },
        },
    },
}
_REMINDER_INSPECT_RESULT_SCHEMA: Mapping[str, object] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ["outcome", "items"],
    "properties": {
        "outcome": {"type": "string", "enum": ["resolved", "clarification"]},
        "items": {
            "type": "array",
            "maxItems": 1,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "type", "version", "content", "status"],
                "properties": {
                    "id": {"type": "string", "format": "uuid"},
                    "type": {"type": "string", "enum": ["reminder", "series"]},
                    "version": {"type": "integer", "minimum": 1},
                    "content": {"type": "string", "minLength": 1, "maxLength": 2000},
                    "status": {"type": "string"},
                },
            },
        },
    },
}
_RECURRENCE_SCHEMA: Mapping[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["frequency"],
    "properties": {
        "frequency": {
            "type": "string",
            "enum": ["daily", "selected_weekday", "weekly", "monthly"],
        },
        "interval": {"type": "integer", "minimum": 1, "maximum": 365, "default": 1},
        "weekdays": {
            "type": "array",
            "uniqueItems": True,
            "items": {"type": "integer", "minimum": 1, "maximum": 7},
        },
        "monthDay": {"type": "integer", "minimum": 1, "maximum": 31},
        "count": {"type": "integer", "minimum": 1, "maximum": 10000},
        "endDate": {"type": "string", "format": "date"},
    },
}
_LOCAL_WALL_CLOCK_SCHEMA: Mapping[str, object] = {
    "type": "string",
    "minLength": 16,
    "maxLength": 32,
    "pattern": r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?$",
}
_SERIES_CREATE_INPUT_SCHEMA: Mapping[str, object] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ["content", "localStart", "recurrence"],
    "properties": {
        "content": {"type": "string", "minLength": 1, "maxLength": 2000},
        "localStart": _LOCAL_WALL_CLOCK_SCHEMA,
        "recurrence": _RECURRENCE_SCHEMA,
    },
}
_RESCHEDULE_INPUT_SCHEMA: Mapping[str, object] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ["target", "dueAt"],
    "properties": {
        "target": {"type": "string", "minLength": 1, "maxLength": 200},
        "dueAt": _LOCAL_WALL_CLOCK_SCHEMA,
    },
}
_SERIES_UPDATE_INPUT_SCHEMA: Mapping[str, object] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ["target"],
    "properties": {
        "target": {"type": "string", "minLength": 1, "maxLength": 200},
        "content": {"type": "string", "minLength": 1, "maxLength": 2000},
        "localStart": _LOCAL_WALL_CLOCK_SCHEMA,
        "recurrence": _RECURRENCE_SCHEMA,
    },
}
_CANCEL_INPUT_SCHEMA: Mapping[str, object] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ["target", "scope"],
    "properties": {
        "target": {"type": "string", "minLength": 1, "maxLength": 200},
        "scope": {"type": "string", "enum": ["occurrence", "series"]},
    },
}
_REMINDER_COMMAND_RESULT_SCHEMA: Mapping[str, object] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ["status", "command"],
    "properties": {
        "status": {"type": "string", "enum": ["completed"]},
        "command": {
            "type": "string",
            "enum": [
                "reminder.series.create",
                "reminder.reschedule",
                "reminder.series.update",
                "reminder.series.pause",
                "reminder.series.resume",
                "reminder.cancel",
            ],
        },
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
            ToolDescriptor(
                id=REMINDER_CREATE_TOOL_ID,
                version=REMINDER_CREATE_TOOL_VERSION,
                display_name="Create reminder",
                description="Create an owner-scoped one-shot reminder after confirmation.",
                classification="mutation",
                permission_scope="reminders:write",
                confirmation="always",
                timeout_ms=10_000,
                retry_limit=0,
                idempotency="idempotent",
                sensitivity="private",
                observable_component="aura.runtime.tool_execution",
                input_schema=_REMINDER_INPUT_SCHEMA,
                result_schema=_REMINDER_RESULT_SCHEMA,
                handler=_reminder_handler,
            ),
            ToolDescriptor(
                id=REMINDER_LIST_TOOL_ID,
                version=REMINDER_LIST_TOOL_VERSION,
                display_name="List reminders",
                description="List up to ten owner-authorized reminders and recurring series.",
                classification="read",
                permission_scope="reminders:read",
                confirmation="never",
                timeout_ms=10_000,
                retry_limit=0,
                idempotency="read_only",
                sensitivity="private",
                observable_component="aura.runtime.tool_execution",
                input_schema=_REMINDER_LIST_INPUT_SCHEMA,
                result_schema=_REMINDER_READ_RESULT_SCHEMA,
                handler=_reminder_list_handler,
            ),
            ToolDescriptor(
                id=REMINDER_INSPECT_TOOL_ID,
                version=REMINDER_INSPECT_TOOL_VERSION,
                display_name="Inspect reminder",
                description="Inspect one uniquely resolved owner-authorized reminder or series.",
                classification="read",
                permission_scope="reminders:read",
                confirmation="never",
                timeout_ms=10_000,
                retry_limit=0,
                idempotency="read_only",
                sensitivity="private",
                observable_component="aura.runtime.tool_execution",
                input_schema=_REMINDER_TARGET_INPUT_SCHEMA,
                result_schema=_REMINDER_INSPECT_RESULT_SCHEMA,
                handler=_reminder_inspect_handler,
            ),
            ToolDescriptor(
                id=REMINDER_SERIES_CREATE_TOOL_ID,
                version=REMINDER_SERIES_CREATE_TOOL_VERSION,
                display_name="Create recurring reminder",
                description="Create an owner-scoped recurring reminder after confirmation.",
                classification="mutation",
                permission_scope="reminders:write",
                confirmation="always",
                timeout_ms=10_000,
                retry_limit=0,
                idempotency="idempotent",
                sensitivity="private",
                observable_component="aura.runtime.tool_execution",
                input_schema=_SERIES_CREATE_INPUT_SCHEMA,
                result_schema=_REMINDER_COMMAND_RESULT_SCHEMA,
            ),
            ToolDescriptor(
                id=REMINDER_RESCHEDULE_TOOL_ID,
                version=REMINDER_RESCHEDULE_TOOL_VERSION,
                display_name="Reschedule reminder",
                description="Reschedule one uniquely resolved owner reminder after confirmation.",
                classification="mutation",
                permission_scope="reminders:write",
                confirmation="always",
                timeout_ms=10_000,
                retry_limit=0,
                idempotency="idempotent",
                sensitivity="private",
                observable_component="aura.runtime.tool_execution",
                input_schema=_RESCHEDULE_INPUT_SCHEMA,
                result_schema=_REMINDER_COMMAND_RESULT_SCHEMA,
            ),
            ToolDescriptor(
                id=REMINDER_SERIES_UPDATE_TOOL_ID,
                version=REMINDER_SERIES_UPDATE_TOOL_VERSION,
                display_name="Update recurring reminder",
                description="Update one uniquely resolved reminder series after confirmation.",
                classification="mutation",
                permission_scope="reminders:write",
                confirmation="always",
                timeout_ms=10_000,
                retry_limit=0,
                idempotency="idempotent",
                sensitivity="private",
                observable_component="aura.runtime.tool_execution",
                input_schema=_SERIES_UPDATE_INPUT_SCHEMA,
                result_schema=_REMINDER_COMMAND_RESULT_SCHEMA,
            ),
            ToolDescriptor(
                id=REMINDER_SERIES_PAUSE_TOOL_ID,
                version=REMINDER_SERIES_PAUSE_TOOL_VERSION,
                display_name="Pause recurring reminder",
                description="Pause one uniquely resolved reminder series after confirmation.",
                classification="mutation",
                permission_scope="reminders:write",
                confirmation="always",
                timeout_ms=10_000,
                retry_limit=0,
                idempotency="idempotent",
                sensitivity="private",
                observable_component="aura.runtime.tool_execution",
                input_schema=_REMINDER_TARGET_INPUT_SCHEMA,
                result_schema=_REMINDER_COMMAND_RESULT_SCHEMA,
            ),
            ToolDescriptor(
                id=REMINDER_SERIES_RESUME_TOOL_ID,
                version=REMINDER_SERIES_RESUME_TOOL_VERSION,
                display_name="Resume recurring reminder",
                description="Resume one uniquely resolved reminder series after confirmation.",
                classification="mutation",
                permission_scope="reminders:write",
                confirmation="always",
                timeout_ms=10_000,
                retry_limit=0,
                idempotency="idempotent",
                sensitivity="private",
                observable_component="aura.runtime.tool_execution",
                input_schema=_REMINDER_TARGET_INPUT_SCHEMA,
                result_schema=_REMINDER_COMMAND_RESULT_SCHEMA,
            ),
            ToolDescriptor(
                id=REMINDER_CANCEL_TOOL_ID,
                version=REMINDER_CANCEL_TOOL_VERSION,
                display_name="Cancel reminder",
                description="Cancel an explicitly scoped reminder occurrence or series after confirmation.",
                classification="mutation",
                permission_scope="reminders:write",
                confirmation="always",
                timeout_ms=10_000,
                retry_limit=0,
                idempotency="idempotent",
                sensitivity="private",
                observable_component="aura.runtime.tool_execution",
                input_schema=_CANCEL_INPUT_SCHEMA,
                result_schema=_REMINDER_COMMAND_RESULT_SCHEMA,
            ),
        )
    )
