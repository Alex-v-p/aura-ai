# ruff: noqa: E501,E701,E702,F401
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownMemberType=false, reportGeneralTypeIssues=false, reportArgumentType=false, reportIndexIssue=false, reportPrivateUsage=false, reportUnusedImport=false, reportAttributeAccessIssue=false
"""Durable Observatory operations state adapter.

All operation resources use one narrow adapter boundary.  PostgreSQL stores
the authoritative closed JSON representation plus searchable identity columns;
the application validators remain responsible for the shape and vocabulary.
The in-memory implementation is deliberately useful for isolated API tests,
never selected by a production composition.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

from aura_observatory.saved_queries import decode_cursor, encode_cursor, utc_now

_TABLES = {
    "dashboard": "observatory_dashboards",
    "saved_query": "observatory_saved_queries",
    "alert_rule": "observatory_alert_rules",
    "alert_instance": "observatory_alert_instances",
    "alert_acknowledgement": "observatory_alert_acknowledgements",
    "alert_suppression": "observatory_alert_suppressions",
    "incident": "observatory_incidents",
    "annotation": "observatory_annotations",
    "audit": "observatory_operations_audit",
}
OperationResourceType = Literal[
    "dashboard", "saved_query", "alert_rule", "alert_instance",
    "alert_acknowledgement", "alert_suppression", "incident", "annotation",
]


def _filter_hash(resource_type: str, *, state: str | None = None, kind: str | None = None, severity: str | None = None, rule_id: str | None = None, target: Mapping[str, str] | None = None) -> str:
    payload = {"resourceType": resource_type, "state": state, "kind": kind, "severity": severity, "ruleId": rule_id, "target": dict(target) if target else None}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:32]


def _identity(resource_type: str, value: Mapping[str, object]) -> tuple[str, str]:
    prefixes = {
        "dashboard": ("dashboardId", "dashboardVersion"),
        "saved_query": ("queryId", "queryVersion"),
        "alert_rule": ("ruleId", "ruleVersion"),
        "alert_instance": ("instanceId", "instanceVersion"),
        "alert_acknowledgement": ("acknowledgementId", "acknowledgementVersion"),
        "alert_suppression": ("suppressionId", "suppressionVersion"),
        "incident": ("incidentId", "incidentVersion"),
        "annotation": ("annotationId", "annotationVersion"),
        "audit": ("auditId", "auditVersion"),
    }
    keys = prefixes[resource_type]
    return str(value[keys[0]]), str(value[keys[1]])


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _request_hash(value: object) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _command_fingerprint(operation: str, value: object) -> object:
    """Hash caller intent while ignoring server-generated create identities."""

    if not operation.startswith("create:") or not isinstance(value, Mapping):
        return value
    ignored = {
        "dashboardId", "dashboardVersion", "queryId", "queryVersion",
        "ruleId", "ruleVersion", "instanceId", "instanceVersion",
        "acknowledgementId", "acknowledgementVersion", "suppressionId",
        "suppressionVersion", "incidentId", "incidentVersion", "annotationId",
        "annotationVersion", "updatedAt",
    }
    return {key: item for key, item in value.items() if key not in ignored}


def _version_key(value: object) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in str(value).split("."))
    except (TypeError, ValueError):
        return (0,)


def _history_entry(event: Mapping[str, object]) -> dict[str, object]:
    """Project persisted events to the closed, content-free history contract."""
    outcome = str(event.get("outcome") or "accepted")
    change = str(event.get("change") or "updated")
    state = str(event.get("state") or "unknown")
    if outcome == "failed":
        change = "evaluated"
    elif outcome == "conflict":
        change = "state_changed"
    elif outcome == "recovered":
        change = "recovered"
    elif change not in {"created", "updated", "state_changed", "evaluated", "firing", "recovered", "suppressed", "suppression_cleared", "acknowledged", "transitioned", "deleted"}:
        change = "updated"
    return {
        "historyId": event["eventId"],
        "resourceType": event["resourceType"],
        "resourceId": event["resourceId"],
        "resourceVersion": event["resourceVersion"],
        "change": change,
        "state": state,
        "occurredAt": event["occurredAt"],
    }


class OperationsRepository:
    """Small interface shared by domain services and HTTP composition."""

    async def create_resource(self, resource_type: OperationResourceType, resource: Mapping[str, object], *, command_id: str | None = None) -> dict[str, object]: ...
    async def list_resources(self, resource_type: OperationResourceType, *, cursor: str | None = None, limit: int = 50, state: str | None = None, kind: str | None = None, severity: str | None = None, rule_id: str | None = None, target: Mapping[str, str] | None = None) -> dict[str, object]: ...
    async def get_resource(self, resource_type: OperationResourceType, resource_id: str, version: str | None = None) -> dict[str, object] | None: ...
    async def replace_resource(self, resource_type: OperationResourceType, resource: Mapping[str, object], *, command_id: str | None = None, expected_version: str | None = None) -> dict[str, object]: ...
    async def delete_resource(self, resource_type: OperationResourceType, resource_id: str, version: str, *, command_id: str | None = None) -> None: ...
    async def append_event(self, resource_type: OperationResourceType, resource_id: str, version: str, change: str, state: str | None = None) -> dict[str, object]: ...
    async def events_after(self, last_event_id: str | None, *, limit: int = 1000) -> list[dict[str, object]]: ...
    async def claim_due_alerts(self, *, now: str, limit: int = 16, owner: str = "evaluation_scheduler", lease_seconds: int = 120) -> list[dict[str, object]]: ...
    async def complete_alert_claim(self, rule_id: str, *, now: str, success: bool, owner: str = "evaluation_scheduler", lease_token: str | None = None) -> None: ...
    async def acknowledge_alert_instance(self, instance_id: str, request: Mapping[str, object], *, command_id: str | None = None) -> dict[str, object]: ...
    async def list_history(self, *, cursor: str | None = None, limit: int = 50, resource_type: str | None = None, resource_id: str | None = None, resource_version: str | None = None) -> dict[str, object]: ...
    async def validate_annotation_target(self, target: Mapping[str, str]) -> None: ...


class MemoryOperationsRepository(OperationsRepository):
    def __init__(self, metrics: Any | None = None) -> None:
        self.resources: dict[str, dict[tuple[str, str], dict[str, object]]] = {name: {} for name in _TABLES}
        self.commands: dict[str, dict[str, object]] = {}
        self.events: list[dict[str, object]] = []
        self.alert_claims: dict[str, dict[str, object]] = {}
        self.alert_schedule: dict[str, dict[str, object]] = {}
        self.metrics = metrics
        self._lock = asyncio.Lock()
        self.revision = 0
        self.resource_revisions: dict[tuple[str, str, str], int] = {}
        self.deletion_revisions: dict[str, int] = {}
        self.replacement_revisions: dict[tuple[str, str], int] = {}

    def _next_revision(self) -> int:
        self.revision += 1
        return self.revision

    def _admit_command(
        self,
        command_id: str | None,
        operation: str,
        target_key: str,
        request: object,
    ) -> dict[str, object] | None:
        if command_id is None:
            return None
        request_hash = _request_hash(_command_fingerprint(operation, request))
        existing = self.commands.get(command_id)
        if existing is not None:
            if (
                existing.get("operation") != operation
                or existing.get("targetKey") != target_key
                or existing.get("requestHash") != request_hash
            ):
                raise RuntimeError("idempotency key was reused for a different operation")
            if existing.get("status") == "completed":
                replay = existing.get("replay")
                return dict(replay) if isinstance(replay, Mapping) else {"deleted": True}
            raise RuntimeError("idempotency command is already in progress")
        self.commands[command_id] = {
            "operation": operation,
            "targetKey": target_key,
            "requestHash": request_hash,
            "status": "pending",
        }
        return None

    def _complete_command(self, command_id: str | None, value: Mapping[str, object]) -> None:
        if command_id is not None and command_id in self.commands:
            self.commands[command_id].update(status="completed", replay=dict(value))

    async def create_resource(self, resource_type: str, resource: Mapping[str, object], *, command_id: str | None = None) -> dict[str, object]:
        async with self._lock:
            key = _identity(resource_type, resource)
            replay = self._admit_command(command_id, f"create:{resource_type}", resource_type, resource)
            if replay is not None:
                return replay
            if key in self.resources[resource_type]:
                if command_id is not None:
                    self.commands.pop(command_id, None)
                raise ValueError("resource already exists")
            value = dict(resource)
            prior_versions = [item_key for item_key in self.resources[resource_type] if item_key[0] == key[0]]
            self.resources[resource_type][key] = value
            revision = self._next_revision()
            self.resource_revisions[(resource_type, key[0], key[1])] = revision
            if prior_versions:
                self.replacement_revisions[(resource_type, key[0])] = revision
            self._complete_command(command_id, value)
            await self._event(resource_type, key[0], key[1], "created", str(value.get("state")) if value.get("state") else None)
            return dict(value)

    async def replace_resource(self, resource_type: str, resource: Mapping[str, object], *, command_id: str | None = None, expected_version: str | None = None) -> dict[str, object]:
        async with self._lock:
            key = _identity(resource_type, resource)
            replay = self._admit_command(command_id, f"replace:{resource_type}", f"{resource_type}:{key[0]}", resource)
            if replay is not None:
                return replay
            current = self._current_locked(resource_type, key[0])
            if current is None:
                raise KeyError("resource not found")
            if expected_version is not None and _identity(resource_type, current)[1] != expected_version:
                if command_id is not None:
                    self.commands.pop(command_id, None)
                raise RuntimeError("resource version conflict")
            value = dict(resource)
            if resource_type == "alert_instance" and expected_version is not None:
                value["firstObservedAt"] = current.get("firstObservedAt", value.get("firstObservedAt"))
                is_new_observation = value.get("lastObservedAt") != current.get("lastObservedAt")
                value["occurrenceCount"] = int(current.get("occurrenceCount", 0)) + (1 if is_new_observation else 0)
                value["acknowledged"] = bool(current.get("acknowledged", False) or value.get("acknowledged", False))
                # Suppression is an evaluation-time fact.  Do not carry an
                # expired/cleared suppression into the next observation;
                # acknowledgement and occurrence history remain sticky.
                value["suppressed"] = bool(value.get("suppressed", False))
            self.resources[resource_type][key] = value
            self.resource_revisions[(resource_type, key[0], key[1])] = self._next_revision()
            self._complete_command(command_id, value)
            await self._event(resource_type, key[0], key[1], "updated", str(value.get("state")) if value.get("state") else None)
            return dict(value)

    def _current_locked(self, resource_type: str, resource_id: str) -> dict[str, object] | None:
        matches = [
            value
            for (rid, _), value in self.resources[resource_type].items()
            if rid == resource_id
        ]
        return max(matches, key=lambda item: _version_key(_identity(resource_type, item)[1])) if matches else None

    async def list_resources(self, resource_type: str, *, cursor: str | None = None, limit: int = 50, state: str | None = None, kind: str | None = None, severity: str | None = None, rule_id: str | None = None, target: Mapping[str, str] | None = None) -> dict[str, object]:
        async with self._lock:
            marker = decode_cursor(cursor)
            snapshot_revision = int(marker["snapshotRevision"]) if marker and marker.get("snapshotRevision") is not None else self.revision
            if marker and self.deletion_revisions.get(resource_type, 0) > snapshot_revision:
                raise RuntimeError("resource snapshot is no longer available")
            if marker and any(
                revision > snapshot_revision
                for (replaced_type, _resource_id), revision in self.replacement_revisions.items()
                if replaced_type == resource_type
            ):
                raise RuntimeError("resource snapshot is no longer available")
            current: dict[str, dict[str, object]] = {}
            for value in self.resources[resource_type].values():
                rid, _ = _identity(resource_type, value)
                revision = self.resource_revisions.get((resource_type, rid, _identity(resource_type, value)[1]), 0)
                if revision > snapshot_revision:
                    continue
                if rid not in current or _version_key(_identity(resource_type, value)[1]) > _version_key(_identity(resource_type, current[rid])[1]):
                    current[rid] = value
            values = list(current.values())
        values.sort(key=lambda item: (str(item.get("createdAt", "")), str(next(iter(_identity(resource_type, item))))))
        if state: values = [v for v in values if v.get("state") == state]
        if kind: values = [v for v in values if isinstance(v.get("definition"), Mapping) and v["definition"].get("kind") == kind]
        if severity: values = [v for v in values if v.get("severity") == severity]
        if rule_id: values = [v for v in values if isinstance(v.get("rule"), Mapping) and v["rule"].get("resourceId") == rule_id]
        if target: values = [v for v in values if v.get("target") == dict(target)]
        snapshot = utc_now(); start = 0
        # Deterministic/reference fixtures may carry a producer timestamp a
        # little ahead of the local wall clock.  The first page snapshot must
        # include every row already visible to this read, while later pages
        # still use that same immutable boundary.
        if values:
            snapshot = max(snapshot, max(str(value.get("createdAt", "")) for value in values))
        marker = marker or decode_cursor(cursor)
        if marker:
            expected_hash = _filter_hash(resource_type, state=state, kind=kind, severity=severity, rule_id=rule_id, target=target)
            if marker.get("filterHash") not in {None, expected_hash}:
                raise ValueError("cursor filter does not match request")
            snapshot = str(marker.get("snapshotAt") or snapshot)
            values = [value for value in values if str(value.get("createdAt", "")) <= snapshot]
            for index, item in enumerate(values):
                ident = _identity(resource_type, item)
                if (str(item.get("createdAt", "")), ident[0], ident[1]) > (marker["createdAt"], marker["resourceId"], marker["version"]): start = index; break
            else: start = len(values)
        page = values[start : start + limit]
        next_cursor = None
        if start + limit < len(values):
            item = page[-1]; ident = _identity(resource_type, item); next_cursor = encode_cursor(str(item.get("createdAt", "")), ident[0], ident[1], snapshot_at=snapshot, snapshot_revision=snapshot_revision, filter_hash=_filter_hash(resource_type, state=state, kind=kind, severity=severity, rule_id=rule_id, target=target))
        return {"items": [dict(value) for value in page], "nextCursor": next_cursor, "snapshotAt": snapshot, "partial": False}

    async def get_resource(self, resource_type: str, resource_id: str, version: str | None = None) -> dict[str, object] | None:
        async with self._lock:
            values = self.resources[resource_type]
            matches = [value for (rid, rv), value in values.items() if rid == resource_id and (version is None or rv == version)]
            if not matches:
                return None
            return dict(max(matches, key=lambda item: _version_key(_identity(resource_type, item)[1])))

    async def delete_resource(self, resource_type: str, resource_id: str, version: str, *, command_id: str | None = None) -> None:
        async with self._lock:
            replay = self._admit_command(command_id, f"delete:{resource_type}", f"{resource_type}:{resource_id}", {"resourceId": resource_id, "resourceVersion": version})
            if replay is not None: return
            if self.resources[resource_type].pop((resource_id, version), None) is None: raise KeyError("resource not found")
            self.resource_revisions.pop((resource_type, resource_id, version), None)
            self.deletion_revisions[resource_type] = self._next_revision()
            if resource_type == "annotation":
                for command in self.commands.values():
                    replay = command.get("replay")
                    if isinstance(replay, Mapping) and replay.get("annotationId") == resource_id:
                        command["replay"] = {"deleted": True, "resourceId": resource_id, "resourceVersion": version}
            self._complete_command(command_id, {"deleted": True, "resourceId": resource_id, "resourceVersion": version})
            await self._event(resource_type, resource_id, version, "deleted", None)

    async def append_event(self, resource_type: str, resource_id: str, version: str, change: str, state: str | None = None) -> dict[str, object]:
        async with self._lock: return await self._event(resource_type, resource_id, version, change, state)

    async def acknowledge_alert_instance(self, instance_id: str, request: Mapping[str, object], *, command_id: str | None = None) -> dict[str, object]:
        """Fence acknowledgement admission before mutating instance state."""
        async with self._lock:
            operation = "acknowledge:alert_instance"
            replay = self._admit_command(command_id, operation, f"alert_instance:{instance_id}", request)
            if replay is not None:
                return replay
            current = self._current_locked("alert_instance", instance_id)
            expected = str(request.get("instanceVersion", ""))
            if current is None or _identity("alert_instance", current)[1] != expected:
                if command_id is not None:
                    self.commands.pop(command_id, None)
                if current is None:
                    raise KeyError("instance not found")
                raise RuntimeError("alert instance version conflict")
            acknowledgement = {
                "acknowledgementId": str(uuid4()),
                "acknowledgementVersion": "1.0.0",
                "instance": {"resourceType": "alert_instance", "resourceId": instance_id, "resourceVersion": expected},
                "reason": request["reason"],
                "acknowledgedAt": request["acknowledgedAt"],
            }
            updated = dict(current)
            updated["acknowledged"] = True
            self.resources["alert_instance"][(instance_id, expected)] = updated
            self.resource_revisions[("alert_instance", instance_id, expected)] = self._next_revision()
            self.resources["alert_acknowledgement"][_identity("alert_acknowledgement", acknowledgement)] = acknowledgement
            await self._event("alert_instance", instance_id, expected, "acknowledged", str(updated.get("state", "unknown")))
            await self._event("alert_acknowledgement", str(acknowledgement["acknowledgementId"]), "1.0.0", "created", "acknowledged")
            self._complete_command(command_id, acknowledgement)
            return dict(acknowledgement)

    async def _event(self, resource_type: str, resource_id: str, version: str, change: str, state: str | None) -> dict[str, object]:
        event = {"schemaVersion": 1, "eventId": str(__import__("uuid").uuid4()), "eventType": "operations.changed", "resourceType": resource_type, "resourceId": resource_id, "resourceVersion": version, "change": {"created": "created", "updated": "updated", "deleted": "deleted", "acknowledged": "acknowledged", "recovered": "recovered", "evaluated": "evaluated"}.get(change, "updated"), "state": state or "unknown", "occurredAt": utc_now(), "capturePolicy": "metadata_only"}
        self.events.append(event); self.events = self.events[-10_000:]
        self.resources["audit"][(event["eventId"], "1.0.0")] = {
            "auditId": event["eventId"], "auditVersion": "1.0.0", "operation": event["change"],
            "resourceType": resource_type, "resourceId": resource_id,
            "resourceVersion": version, "outcome": "accepted", "occurredAt": event["occurredAt"],
        }
        return dict(event)

    async def events_after(self, last_event_id: str | None, *, limit: int = 1000) -> list[dict[str, object]]:
        async with self._lock:
            if last_event_id is None: return [dict(v) for v in self.events[-limit:]]
            for index, value in enumerate(self.events):
                if value["eventId"] == last_event_id: return [dict(v) for v in self.events[index + 1 : index + 1 + limit]]
            return [dict(v) for v in self.events[-limit:]]

    async def claim_due_alerts(self, *, now: str, limit: int = 16, owner: str = "evaluation_scheduler", lease_seconds: int = 120) -> list[dict[str, object]]:
        now_dt = datetime.fromisoformat(now.replace("Z", "+00:00"))
        rules = await self.list_resources("alert_rule", limit=min(limit, 32), state="enabled")
        claimed: list[dict[str, object]] = []
        due_count = 0
        oldest_due: datetime | None = None
        async with self._lock:
            for rule in rules["items"]:
                rid = str(rule["ruleId"])
                claim = self.alert_claims.get(rid)
                if claim and datetime.fromisoformat(str(claim["expiresAt"]).replace("Z", "+00:00")) > now_dt:
                    continue
                if claim is not None:
                    self._record_recovery("lease_expired", "recovered")
                    await self._audit(rid, str(rule.get("ruleVersion", "1.0.0")), "lease_recovered", "recovered")
                next_due = self.alert_schedule.get(rid, {}).get("nextDueAt")
                if isinstance(next_due, str) and datetime.fromisoformat(next_due.replace("Z", "+00:00")) > now_dt:
                    continue
                due_count += 1
                due_at = datetime.fromisoformat(next_due.replace("Z", "+00:00")) if isinstance(next_due, str) else now_dt
                oldest_due = min(oldest_due, due_at) if oldest_due is not None else due_at
                token = str(uuid4())
                expiry = datetime.fromtimestamp(now_dt.timestamp() + lease_seconds, UTC).isoformat().replace("+00:00", "Z")
                self.alert_claims[rid] = {"owner": owner, "token": token, "expiresAt": expiry}
                value = dict(rule)
                value["leaseToken"] = token
                claimed.append(value)
                await self._audit(rid, str(rule.get("ruleVersion", "1.0.0")), "lease_acquired", "accepted")
        self._record_scheduler_health(due_count, oldest_due, len(claimed))
        return claimed

    def _record_scheduler_health(self, backlog: int, oldest_due: datetime | None, acquired: int) -> None:
        record = getattr(self.metrics, "record", None)
        setter = getattr(self.metrics, "set", None)
        if not callable(record) and not callable(setter):
            return
        lag = max(0.0, (datetime.now(UTC) - oldest_due).total_seconds() * 1000.0) if oldest_due else 0.0
        if callable(setter):
            setter("observatory_operations_backlog_count", float(backlog))
            setter("observatory_operations_oldest_due_lag_ms", lag)
        elif callable(record):
            record("observatory_operations_backlog_count", float(backlog))
            record("observatory_operations_oldest_due_lag_ms", lag)
        if callable(record):
            record("observatory_operations_lease_outcome_count", float(acquired), {"outcome": "success"})

    async def complete_alert_claim(self, rule_id: str, *, now: str, success: bool, owner: str = "evaluation_scheduler", lease_token: str | None = None) -> None:
        async with self._lock:
            claim = self.alert_claims.get(rule_id)
            expired = claim is not None and datetime.fromisoformat(str(claim.get("expiresAt")).replace("Z", "+00:00")) <= datetime.fromisoformat(now.replace("Z", "+00:00"))
            if claim is None or claim.get("owner") != owner or claim.get("token") != lease_token or expired:
                self._record_recovery("recover", "conflict")
                await self._audit(rule_id, "1.0.0", "lease_conflict", "conflict")
                return
            rule = self._current_locked("alert_rule", rule_id)
            if rule is not None:
                cadence = {"one_minute": 60, "five_minutes": 300, "fifteen_minutes": 900, "one_hour": 3600}.get(str(rule.get("cadence")), 300)
                schedule = self.alert_schedule.setdefault(rule_id, {})
                attempt = int(schedule.get("attemptCount", 0))
                delay = cadence if success else min(cadence * (2 ** min(attempt, 4)), 3600)
                schedule["nextDueAt"] = datetime.fromtimestamp(datetime.fromisoformat(now.replace("Z", "+00:00")).timestamp() + delay, UTC).isoformat().replace("+00:00", "Z")
                schedule["attemptCount"] = 0 if success else attempt + 1
            self.alert_claims.pop(rule_id, None)
            self._record_recovery("complete", "success" if success else "failed")
            await self._audit(rule_id, str(rule.get("ruleVersion", "1.0.0")) if rule is not None else "1.0.0", "lease_completed", "accepted" if success else "failed")

    def _record_recovery(self, operation: str, outcome: str = "success") -> None:
        record = getattr(self.metrics, "record", None)
        if callable(record):
            record("observatory_operations_recovery_count", 1.0, {"operation": operation, "outcome": outcome})

    async def _audit(self, resource_id: str, resource_version: str, state: str, outcome: str) -> None:
        audit_id = str(uuid4())
        occurred_at = utc_now()
        self.resources["audit"][(audit_id, "1.0.0")] = {
            "auditId": audit_id,
            "auditVersion": "1.0.0",
            "operation": "updated",
            "resourceType": "alert_rule",
            "resourceId": resource_id,
            "resourceVersion": resource_version,
            "outcome": outcome,
            "state": state,
            "occurredAt": occurred_at,
        }

    async def list_history(self, *, cursor: str | None = None, limit: int = 50, resource_type: str | None = None, resource_id: str | None = None, resource_version: str | None = None) -> dict[str, object]:
        values = [
            {
                "eventId": value.get("auditId"),
                "resourceType": value.get("resourceType"),
                "resourceId": value.get("resourceId"),
                "resourceVersion": value.get("resourceVersion"),
                "change": value.get("operation", "updated"),
                "state": value.get("state") or "unknown",
                "outcome": value.get("outcome", "accepted"),
                "occurredAt": value.get("occurredAt"),
            }
            for value in self.resources["audit"].values()
        ]
        values.sort(key=lambda value: (str(value.get("occurredAt", "")), str(value.get("eventId", ""))))
        if resource_type: values = [item for item in values if item.get("resourceType") == resource_type]
        if resource_id: values = [item for item in values if item.get("resourceId") == resource_id]
        if resource_version: values = [item for item in values if item.get("resourceVersion") == resource_version]
        start = 0
        marker = decode_cursor(cursor)
        if marker:
            for index, item in enumerate(values):
                key = (str(item.get("occurredAt", "")), str(item.get("eventId", "")))
                if key > (marker["createdAt"], marker["resourceId"]):
                    start = index
                    break
            else:
                start = len(values)
        page = values[start:start + min(200, limit)]
        next_cursor = None
        if len(page) == min(200, limit) and page:
            last = page[-1]
            next_cursor = encode_cursor(str(last["occurredAt"]), str(last["eventId"]), "1.0.0")
        return {"items": [_history_entry(item) for item in page], "nextCursor": next_cursor, "snapshotAt": utc_now(), "partial": False}

    async def validate_annotation_target(self, target: Mapping[str, str]) -> None:
        if target.get("targetType") == "incident":
            if await self.get_resource("incident", target["targetId"], target["targetVersion"]) is None:
                raise KeyError("annotation target not found")
            return
        raise RuntimeError("annotation reference provider unavailable")


class PostgresOperationsRepository(OperationsRepository):
    """PostgreSQL implementation; JSON is persisted only after validation."""

    def __init__(self, connection_factory: Any, metrics: Any | None = None) -> None:
        self._connection_factory = connection_factory
        self.metrics = metrics

    @staticmethod
    async def _revision_lock(connection: Any) -> None:
        """Serialize revision allocation and snapshot capture in PostgreSQL.

        Sequences are intentionally retained for monotonic identifiers, but
        their values are not visibility barriers.  This transaction-scoped
        advisory lock makes every mutation and first-page snapshot capture
        share one committed ordering without holding a process-local lock.
        """
        await connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended('aura.observatory.operations.revisions', 0))"
        )

    @staticmethod
    async def _resource_lock(connection: Any, resource_type: str, resource_id: str) -> None:
        await connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"aura.observatory.operations.resource:{resource_type}:{resource_id}",),
        )

    async def _admit_command(self, connection: Any, command_id: str | None, operation: str, target_key: str, request: object) -> dict[str, object] | None:
        if command_id is None:
            return None
        request_hash = _request_hash(_command_fingerprint(operation, request))
        await connection.execute(
            "INSERT INTO observatory_operations_commands(command_id, replay_json, operation, target_key, request_hash, status) VALUES (%s,%s::jsonb,%s,%s,%s,'pending') ON CONFLICT (command_id) DO NOTHING",
            (command_id, "{}", operation, target_key, request_hash),
        )
        result = await connection.execute(
            "SELECT replay_json, operation, target_key, request_hash, status FROM observatory_operations_commands WHERE command_id=%s FOR UPDATE",
            (command_id,),
        )
        row = await result.fetchone()
        if row is None:
            raise RuntimeError("idempotency command admission failed")
        if row[1] != operation or row[2] != target_key or row[3] != request_hash:
            raise RuntimeError("idempotency key was reused for a different operation")
        if row[4] == "completed":
            value = row[0]
            return json.loads(value) if isinstance(value, str) else dict(value)
        # A pending row is owned by this transaction after the SELECT lock. A
        # crashed transaction rolls back its reservation; a committed pending
        # row is treated as a deterministic in-progress conflict.
        if row[4] != "pending":
            raise RuntimeError("idempotency command is unavailable")
        return None

    async def _complete_command(self, connection: Any, command_id: str | None, value: Mapping[str, object]) -> None:
        if command_id:
            await connection.execute("UPDATE observatory_operations_commands SET replay_json=%s::jsonb, status='completed' WHERE command_id=%s", (_json(value), command_id))

    async def create_resource(self, resource_type: str, resource: Mapping[str, object], *, command_id: str | None = None) -> dict[str, object]:
        table = _TABLES[resource_type]; rid, version = _identity(resource_type, resource)
        async with self._connection_factory() as connection:
            await self._revision_lock(connection)
            await self._resource_lock(connection, resource_type, rid)
            replay = await self._admit_command(connection, command_id, f"create:{resource_type}", resource_type, resource)
            if replay is not None: return replay
            current = await connection.execute(
                f"SELECT resource_version FROM {table} WHERE resource_id=%s AND current_version=resource_version ORDER BY resource_version DESC LIMIT 1 FOR UPDATE",
                (rid,),
            )
            current_row = await current.fetchone()
            if current_row is not None:
                await connection.execute(
                    f"UPDATE {table} SET current_version=%s WHERE resource_id=%s AND current_version=resource_version",
                    (version, rid),
                )
            await connection.execute(f"INSERT INTO {table}(resource_id, resource_version, current_version, state, kind, severity, resource_json, created_at, updated_at, visibility_revision) VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,nextval('observatory_operations_revision_seq'))", (rid, version, version, resource.get("state"), resource.get("definition",{}).get("kind") if isinstance(resource.get("definition"), Mapping) else None, resource.get("severity"), _json(resource), resource.get("createdAt", utc_now()), resource.get("updatedAt", resource.get("createdAt", utc_now()))))
            await self._complete_command(connection, command_id, resource)
            await self._append(connection, resource_type, rid, version, "created", resource.get("state"))
        return dict(resource)

    async def replace_resource(self, resource_type: str, resource: Mapping[str, object], *, command_id: str | None = None, expected_version: str | None = None) -> dict[str, object]:
        table = _TABLES[resource_type]; rid, version = _identity(resource_type, resource)
        async with self._connection_factory() as connection:
            await self._revision_lock(connection)
            await self._resource_lock(connection, resource_type, rid)
            replay = await self._admit_command(connection, command_id, f"replace:{resource_type}", f"{resource_type}:{rid}", resource)
            if replay is not None: return replay
            result = await connection.execute(f"SELECT current_version, resource_version, resource_json FROM {table} WHERE resource_id=%s AND (current_version=resource_version OR current_version IS NULL) ORDER BY resource_version DESC LIMIT 1 FOR UPDATE", (rid,))
            current_row = await result.fetchone()
            if current_row is None:
                if command_id is not None:
                    raise KeyError("resource not found")
                raise KeyError("resource not found")
            current_version = str(current_row[0] or current_row[1])
            if expected_version is not None and current_version != expected_version:
                raise RuntimeError("resource version conflict")
            if expected_version is not None:
                check = await connection.execute(f"UPDATE {table} SET current_version=%s WHERE resource_id=%s AND current_version=%s", (version, rid, expected_version))
                if getattr(check, "rowcount", 1) == 0:
                    raise RuntimeError("resource version conflict")
            else:
                await connection.execute(f"UPDATE {table} SET current_version=%s WHERE resource_id=%s", (version, rid))
            value = dict(resource)
            if resource_type == "alert_instance" and expected_version is not None:
                current_json = json.loads(current_row[2]) if isinstance(current_row[2], str) else dict(current_row[2])
                value["firstObservedAt"] = current_json.get("firstObservedAt", value.get("firstObservedAt"))
                is_new_observation = value.get("lastObservedAt") != current_json.get("lastObservedAt")
                value["occurrenceCount"] = int(current_json.get("occurrenceCount", 0)) + (1 if is_new_observation else 0)
                value["acknowledged"] = bool(current_json.get("acknowledged", False) or value.get("acknowledged", False))
                value["suppressed"] = bool(value.get("suppressed", False))
            if current_row is None:
                raise KeyError("resource not found")
            await connection.execute(
                f"INSERT INTO {table}(resource_id, resource_version, current_version, state, kind, severity, resource_json, created_at, updated_at, visibility_revision) VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,nextval('observatory_operations_revision_seq')) ON CONFLICT (resource_id, resource_version) DO UPDATE SET current_version=EXCLUDED.current_version, state=EXCLUDED.state, severity=EXCLUDED.severity, resource_json=EXCLUDED.resource_json, updated_at=EXCLUDED.updated_at, visibility_revision=EXCLUDED.visibility_revision",
                (
                    rid, version, version, value.get("state"),
                    value.get("definition", {}).get("kind")
                    if isinstance(value.get("definition"), Mapping) else None,
                    value.get("severity"), _json(value),
                    value.get("createdAt", utc_now()),
                    value.get("updatedAt", utc_now()),
                ),
            )
            await self._complete_command(connection, command_id, value); await self._append(connection, resource_type, rid, version, "updated", value.get("state"))
        return dict(value)

    async def list_resources(self, resource_type: str, *, cursor: str | None = None, limit: int = 50, state: str | None = None, kind: str | None = None, severity: str | None = None, rule_id: str | None = None, target: Mapping[str, str] | None = None) -> dict[str, object]:
        table = _TABLES[resource_type]; clauses = []; params: list[Any] = []
        clauses.append("current_version = resource_version")
        if state: clauses.append("state=%s"); params.append(state)
        if kind: clauses.append("kind=%s"); params.append(kind)
        if severity: clauses.append("severity=%s"); params.append(severity)
        if rule_id:
            clauses.append("resource_json->'rule'->>'resourceId'=%s")
            params.append(rule_id)
        if target:
            clauses.append("resource_json->'target'->>'targetType'=%s AND resource_json->'target'->>'targetId'=%s AND resource_json->'target'->>'targetVersion'=%s")
            params.extend([target.get("targetType"), target.get("targetId"), target.get("targetVersion")])
        marker = decode_cursor(cursor)
        snapshot = str(marker.get("snapshotAt")) if marker and marker.get("snapshotAt") else utc_now()
        snapshot_revision = int(marker["snapshotRevision"]) if marker and marker.get("snapshotRevision") is not None else None
        expected_hash = _filter_hash(resource_type, state=state, kind=kind, severity=severity, rule_id=rule_id, target=target)
        if marker and marker.get("filterHash") not in {None, expected_hash}:
            raise ValueError("cursor filter does not match request")
        clauses.append("visibility_revision <= %s")
        params.append(snapshot_revision)
        if marker:
            clauses.append("(created_at, resource_id::text, resource_version) > (%s,%s,%s)")
            params.extend([marker["createdAt"], marker["resourceId"], marker["version"]])
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        async with self._connection_factory() as connection:
            await self._revision_lock(connection)
            if snapshot_revision is None:
                revision_result = await connection.execute("SELECT last_value FROM observatory_operations_revision_seq")
                revision_row = await revision_result.fetchone()
                snapshot_revision = int(revision_row[0]) if revision_row and revision_row[0] is not None else 0
                clauses[-1] = "visibility_revision <= %s"
                params[-1] = snapshot_revision
            if marker:
                mutation_result = await connection.execute(
                    """
                    SELECT 1
                    FROM observatory_operations_audit current_change
                    WHERE current_change.mutation_revision > %s
                      AND current_change.resource_type = %s
                      AND (
                        current_change.operation = 'deleted'
                        OR (
                          current_change.operation IN ('created', 'updated')
                          AND EXISTS (
                            SELECT 1
                            FROM observatory_operations_audit prior_change
                            WHERE prior_change.resource_type = current_change.resource_type
                              AND prior_change.resource_id = current_change.resource_id
                              AND prior_change.mutation_revision <= %s
                              AND prior_change.operation IN ('created', 'updated')
                          )
                        )
                      )
                    LIMIT 1
                    """,
                    (snapshot_revision, resource_type, snapshot_revision),
                )
                if await mutation_result.fetchone():
                    raise RuntimeError("resource snapshot is no longer available")
            result = await connection.execute(f"SELECT resource_json FROM {table}{where} ORDER BY created_at, resource_id, resource_version LIMIT %s", tuple(params + [min(200, limit + 1)]))
            rows = await result.fetchall()
        values = [json.loads(row[0]) if isinstance(row[0], str) else row[0] for row in rows]
        page = values[:limit]; next_cursor = None
        if len(values) > limit:
            item = page[-1]; rid, rv = _identity(resource_type, item); next_cursor = encode_cursor(str(item.get("createdAt", "")), rid, rv, snapshot_at=snapshot, snapshot_revision=snapshot_revision, filter_hash=expected_hash)
        return {"items": page, "nextCursor": next_cursor, "snapshotAt": snapshot, "partial": False}

    async def get_resource(self, resource_type: str, resource_id: str, version: str | None = None) -> dict[str, object] | None:
        table = _TABLES[resource_type]; sql = f"SELECT resource_json FROM {table} WHERE resource_id=%s"; params: list[Any] = [resource_id]
        if version is not None: sql += " AND resource_version=%s"; params.append(version)
        else: sql += " AND current_version=resource_version"
        sql += " ORDER BY resource_version DESC LIMIT 1"
        async with self._connection_factory() as connection:
            result = await connection.execute(sql, tuple(params)); row = await result.fetchone()
        return (json.loads(row[0]) if isinstance(row[0], str) else dict(row[0])) if row else None

    async def delete_resource(self, resource_type: str, resource_id: str, version: str, *, command_id: str | None = None) -> None:
        table = _TABLES[resource_type]
        async with self._connection_factory() as connection:
            await self._revision_lock(connection)
            await self._resource_lock(connection, resource_type, resource_id)
            replay = await self._admit_command(connection, command_id, f"delete:{resource_type}", f"{resource_type}:{resource_id}", {"resourceId": resource_id, "resourceVersion": version})
            if replay is not None: return
            result = await connection.execute(f"DELETE FROM {table} WHERE resource_id=%s AND resource_version=%s", (resource_id, version))
            if getattr(result, "rowcount", 1) == 0:
                raise KeyError("resource not found")
            if resource_type == "annotation":
                await connection.execute("UPDATE observatory_operations_commands SET replay_json=jsonb_build_object('deleted',true,'resourceId',%s,'resourceVersion',%s) WHERE replay_json->>'annotationId'=%s", (resource_id, version, resource_id))
            await self._complete_command(connection, command_id, {"deleted": True, "resourceId": resource_id, "resourceVersion": version}); await self._append(connection, resource_type, resource_id, version, "deleted", None)

    async def _append(self, connection: Any, resource_type: str, resource_id: str, version: str, change: str, state: object) -> dict[str, object]:
        event = {"schemaVersion": 1, "eventId": str(__import__("uuid").uuid4()), "eventType": "operations.changed", "resourceType": resource_type, "resourceId": resource_id, "resourceVersion": version, "change": {"created": "created", "updated": "updated", "deleted": "deleted", "acknowledged": "acknowledged", "recovered": "recovered", "evaluated": "evaluated"}.get(change, "updated"), "state": state or "unknown", "occurredAt": utc_now(), "capturePolicy": "metadata_only"}
        await connection.execute("INSERT INTO observatory_operations_events(event_id, resource_type, resource_id, resource_version, change, state, event_json, occurred_at) VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s)", (event["eventId"], resource_type, resource_id, version, change, state, _json(event), event["occurredAt"]))
        await connection.execute("INSERT INTO observatory_operations_audit(audit_id, audit_version, operation, resource_type, resource_id, resource_version, outcome, state, occurred_at, mutation_revision) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,nextval('observatory_operations_revision_seq'))", (event["eventId"], "1.0.0", event["change"], resource_type, resource_id, version, "accepted", state, event["occurredAt"]))
        return event

    async def _append_audit(
        self,
        connection: Any,
        resource_type: str,
        resource_id: str,
        resource_version: str,
        outcome: str,
        state: str,
    ) -> None:
        """Write content-free scheduler and recovery audit state."""
        await connection.execute(
            "INSERT INTO observatory_operations_audit(audit_id, audit_version, operation, resource_type, resource_id, resource_version, outcome, state, occurred_at, mutation_revision) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,nextval('observatory_operations_revision_seq'))",
            (
                str(uuid4()),
                "1.0.0",
                "updated",
                resource_type,
                resource_id,
                resource_version,
                outcome,
                state,
                utc_now(),
            ),
        )

    async def append_event(self, resource_type: str, resource_id: str, version: str, change: str, state: str | None = None) -> dict[str, object]:
        async with self._connection_factory() as connection:
            await self._revision_lock(connection)
            return await self._append(connection, resource_type, resource_id, version, change, state)

    async def acknowledge_alert_instance(self, instance_id: str, request: Mapping[str, object], *, command_id: str | None = None) -> dict[str, object]:
        """Atomically update the instance and acknowledgement in one transaction."""
        operation = "acknowledge:alert_instance"
        async with self._connection_factory() as connection:
            await self._revision_lock(connection)
            await self._resource_lock(connection, "alert_instance", instance_id)
            replay = await self._admit_command(connection, command_id, operation, f"alert_instance:{instance_id}", request)
            if replay is not None:
                return replay
            result = await connection.execute(
                "SELECT resource_version, resource_json FROM observatory_alert_instances WHERE resource_id=%s AND current_version=resource_version FOR UPDATE",
                (instance_id,),
            )
            row = await result.fetchone()
            expected = str(request.get("instanceVersion", ""))
            if row is None:
                if command_id is not None:
                    await connection.execute("DELETE FROM observatory_operations_commands WHERE command_id=%s AND status='pending'", (command_id,))
                raise KeyError("instance not found")
            if str(row[0]) != expected:
                if command_id is not None:
                    await connection.execute("DELETE FROM observatory_operations_commands WHERE command_id=%s AND status='pending'", (command_id,))
                raise RuntimeError("alert instance version conflict")
            current = json.loads(row[1]) if isinstance(row[1], str) else dict(row[1])
            acknowledgement = {
                "acknowledgementId": str(uuid4()),
                "acknowledgementVersion": "1.0.0",
                "instance": {"resourceType": "alert_instance", "resourceId": instance_id, "resourceVersion": expected},
                "reason": request["reason"],
                "acknowledgedAt": request["acknowledgedAt"],
            }
            updated = dict(current)
            updated["acknowledged"] = True
            await connection.execute(
                "UPDATE observatory_alert_instances SET resource_json=%s::jsonb, updated_at=%s WHERE resource_id=%s AND resource_version=%s AND current_version=resource_version",
                (_json(updated), utc_now(), instance_id, expected),
            )
            await connection.execute(
                "INSERT INTO observatory_alert_acknowledgements(resource_id, resource_version, current_version, state, kind, severity, resource_json, created_at, updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s)",
                (acknowledgement["acknowledgementId"], "1.0.0", "1.0.0", "acknowledged", None, None, _json(acknowledgement), acknowledgement["acknowledgedAt"], acknowledgement["acknowledgedAt"]),
            )
            await self._complete_command(connection, command_id, acknowledgement)
            await self._append(connection, "alert_instance", instance_id, expected, "acknowledged", current.get("state"))
            await self._append(connection, "alert_acknowledgement", str(acknowledgement["acknowledgementId"]), "1.0.0", "created", "acknowledged")
            return dict(acknowledgement)

    async def events_after(self, last_event_id: str | None, *, limit: int = 1000) -> list[dict[str, object]]:
        async with self._connection_factory() as connection:
            if last_event_id:
                result = await connection.execute("SELECT event_json FROM observatory_operations_events WHERE sequence > COALESCE((SELECT sequence FROM observatory_operations_events WHERE event_id=%s),0) ORDER BY sequence LIMIT %s", (last_event_id, min(1000, limit)))
            else:
                result = await connection.execute("SELECT event_json FROM observatory_operations_events ORDER BY sequence DESC LIMIT %s", (min(1000, limit),))
            rows = await result.fetchall()
        values = [json.loads(r[0]) if isinstance(r[0], str) else dict(r[0]) for r in rows]; return list(reversed(values)) if last_event_id is None else values

    async def claim_due_alerts(self, *, now: str, limit: int = 16, owner: str = "evaluation_scheduler", lease_seconds: int = 120) -> list[dict[str, object]]:
        async with self._connection_factory() as connection:
            await self._revision_lock(connection)
            health_result = await connection.execute("SELECT COUNT(*), EXTRACT(EPOCH FROM (%s::timestamptz - MIN(next_due_at))) * 1000 FROM observatory_alert_rules WHERE state='enabled' AND current_version=resource_version AND (next_due_at IS NULL OR next_due_at <= %s) AND (lease_expires_at IS NULL OR lease_expires_at <= %s::timestamptz)", (now, now, now))
            health_row = await health_result.fetchone()
            result = await connection.execute(
                "SELECT resource_id, resource_json, lease_expires_at FROM observatory_alert_rules WHERE state='enabled' AND current_version=resource_version AND (next_due_at IS NULL OR next_due_at <= %s) AND (lease_expires_at IS NULL OR lease_expires_at <= %s::timestamptz) ORDER BY next_due_at NULLS FIRST, resource_id FOR UPDATE SKIP LOCKED LIMIT %s",
                (now, now, min(32, limit)),
            )
            rows = await result.fetchall()
            claimed: list[dict[str, object]] = []
            for row in rows:
                prior_expiry = row[2]
                if prior_expiry is not None:
                    expired = prior_expiry <= datetime.fromisoformat(now.replace("Z", "+00:00")) if isinstance(prior_expiry, datetime) else str(prior_expiry) <= now
                    if expired:
                        value_for_audit = json.loads(row[1]) if isinstance(row[1], str) else dict(row[1])
                        self._record_recovery("lease_expired", "recovered")
                        await self._append_audit(connection, "alert_rule", str(row[0]), str(value_for_audit.get("ruleVersion", "1.0.0")), "recovered", "lease_recovered")
                token = str(uuid4())
                await connection.execute("UPDATE observatory_alert_rules SET lease_token=%s, lease_owner=%s, lease_expires_at=(CURRENT_TIMESTAMP + (%s || ' seconds')::interval), attempt_count=attempt_count+1 WHERE resource_id=%s AND current_version=resource_version", (token, owner, lease_seconds, row[0]))
                value = json.loads(row[1]) if isinstance(row[1], str) else dict(row[1])
                value["leaseToken"] = token
                claimed.append(value)
                await self._append_audit(
                    connection,
                    "alert_rule",
                    str(row[0]),
                    str(value.get("ruleVersion", "1.0.0")),
                    "accepted",
                    "lease_acquired",
                )
            record = getattr(self.metrics, "record", None)
            setter = getattr(self.metrics, "set", None)
            if callable(record) or callable(setter):
                backlog = int(health_row[0] or 0) if health_row else 0
                oldest_lag = max(0.0, float(health_row[1] or 0.0)) if health_row else 0.0
                if callable(setter):
                    setter("observatory_operations_backlog_count", float(backlog))
                    setter("observatory_operations_oldest_due_lag_ms", oldest_lag)
                elif callable(record):
                    record("observatory_operations_backlog_count", float(backlog))
                    record("observatory_operations_oldest_due_lag_ms", oldest_lag)
                if callable(record):
                    record("observatory_operations_lease_outcome_count", float(len(claimed)), {"outcome": "success"})
            return claimed

    async def complete_alert_claim(self, rule_id: str, *, now: str, success: bool, owner: str = "evaluation_scheduler", lease_token: str | None = None) -> None:
        async with self._connection_factory() as connection:
            await self._revision_lock(connection)
            await self._resource_lock(connection, "alert_rule", rule_id)
            result = await connection.execute("SELECT resource_json, lease_token, lease_expires_at FROM observatory_alert_rules WHERE resource_id=%s AND current_version=resource_version AND lease_owner=%s AND lease_token=%s AND lease_expires_at > CURRENT_TIMESTAMP FOR UPDATE", (rule_id, owner, lease_token))
            row = await result.fetchone()
            if row is None:
                self._record_recovery("recover", "conflict")
                await self._append_audit(connection, "alert_rule", rule_id, "1.0.0", "conflict", "lease_conflict")
                return
            resource = json.loads(row[0]) if isinstance(row[0], str) else dict(row[0])
            cadence_seconds = {"one_minute": 60, "five_minutes": 300, "fifteen_minutes": 900, "one_hour": 3600}.get(str(resource.get("cadence")), 300)
            # Failure retries are bounded and progressively backed off, while
            # success returns to the validated cadence. The lease fence is
            # repeated in the UPDATE so a stale completion cannot advance a
            # schedule after the row lock is released.
            attempt_result = await connection.execute("SELECT attempt_count FROM observatory_alert_rules WHERE resource_id=%s AND current_version=resource_version FOR UPDATE", (rule_id,))
            attempt_row = await attempt_result.fetchone()
            attempt = int(attempt_row[0] or 0) if attempt_row else 0
            delay = cadence_seconds if success else min(cadence_seconds * (2 ** min(attempt, 4)), 3600)
            failure = ", last_failure_code=NULL, attempt_count=0" if success else ", last_failure_code='evaluation_failed'"
            update = await connection.execute(f"UPDATE observatory_alert_rules SET next_due_at=(%s::timestamptz + (%s || ' seconds')::interval), lease_token=NULL, lease_owner=NULL, lease_expires_at=NULL{failure} WHERE resource_id=%s AND current_version=resource_version AND lease_owner=%s AND lease_token=%s AND lease_expires_at > CURRENT_TIMESTAMP", (now, delay, rule_id, owner, lease_token))
            outcome = "success" if getattr(update, "rowcount", 1) else "conflict"
            self._record_recovery("recover", outcome)
            await self._append_audit(
                connection,
                "alert_rule",
                rule_id,
                str(resource.get("ruleVersion", "1.0.0")),
                "accepted" if outcome == "success" and success else "failed" if outcome == "success" else "conflict",
                "lease_completed",
            )

    def _record_recovery(self, operation: str, outcome: str = "success") -> None:
        record = getattr(self.metrics, "record", None)
        if callable(record):
            record("observatory_operations_recovery_count", 1.0, {"operation": operation, "outcome": outcome})

    async def list_history(self, *, cursor: str | None = None, limit: int = 50, resource_type: str | None = None, resource_id: str | None = None, resource_version: str | None = None) -> dict[str, object]:
        clauses = []
        params: list[Any] = []
        if resource_type: clauses.append("resource_type=%s"); params.append(resource_type)
        if resource_id: clauses.append("resource_id=%s"); params.append(resource_id)
        if resource_version: clauses.append("resource_version=%s"); params.append(resource_version)
        marker = decode_cursor(cursor)
        if marker:
            clauses.append("(occurred_at, audit_id) > (%s::timestamptz,%s::uuid)")
            params.extend([marker["createdAt"], marker["resourceId"]])
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        async with self._connection_factory() as connection:
            result = await connection.execute(f"SELECT audit_id, resource_type, resource_id, resource_version, operation, state, outcome, occurred_at FROM observatory_operations_audit{where} ORDER BY occurred_at, audit_id LIMIT %s", tuple(params + [min(200, limit + 1)]))
            rows = await result.fetchall()
        values = [{"eventId": str(row[0]), "resourceType": row[1], "resourceId": str(row[2]), "resourceVersion": row[3], "change": row[4], "state": row[5] or "unknown", "outcome": row[6] or "accepted", "occurredAt": row[7].isoformat().replace("+00:00", "Z") if hasattr(row[7], "isoformat") else str(row[7])} for row in rows]
        page = values[:min(200, limit)]
        next_cursor = None
        if len(values) > len(page) and page:
            last = page[-1]
            next_cursor = encode_cursor(str(last["occurredAt"]), str(last["eventId"]), "1.0.0")
        return {"items": [_history_entry(item) for item in page], "nextCursor": next_cursor, "snapshotAt": utc_now(), "partial": False}

    async def validate_annotation_target(self, target: Mapping[str, str]) -> None:
        if target.get("targetType") == "incident":
            if await self.get_resource("incident", target["targetId"], target["targetVersion"]) is None:
                raise KeyError("annotation target not found")
            return
        raise RuntimeError("annotation reference provider unavailable")


__all__ = ["MemoryOperationsRepository", "OperationsRepository", "PostgresOperationsRepository"]
