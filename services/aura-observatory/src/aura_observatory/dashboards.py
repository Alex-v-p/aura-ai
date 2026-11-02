# ruff: noqa: E501,E701,E702,F401,I001
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownMemberType=false, reportGeneralTypeIssues=false, reportArgumentType=false, reportIndexIssue=false, reportPrivateUsage=false, reportUnusedImport=false
"""Owner dashboard and immutable Observatory preset application service."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from time import monotonic
from typing import Any
from uuid import uuid4

from aura_observatory.saved_queries import SavedQueryError, _uuid, _version, utc_now

_VISUALIZATIONS = {"line", "area", "bar", "stat", "table", "trace", "comparison", "regression"}
_SIZES = {"small", "medium", "large", "full_width"}


class DashboardError(ValueError):
    pass


def _panel(value: object, *, preset: bool = False) -> dict[str, object]:
    if not isinstance(value, Mapping): raise DashboardError("panel is invalid")
    required = {"panelId", "title", "query", "visualization", "size", "position"}
    if set(value) != required: raise DashboardError("panel is invalid")
    if not isinstance(value["title"], str) or not 1 <= len(value["title"]) <= 120: raise DashboardError("panel title is invalid")
    if value["visualization"] not in _VISUALIZATIONS or value["size"] not in _SIZES or not isinstance(value["position"], int) or isinstance(value["position"], bool) or not 0 <= value["position"] <= 23: raise DashboardError("panel layout is invalid")
    if preset:
        from aura_observatory.saved_queries import canonical_definition
        query = canonical_definition(value["query"])
    else:
        if not isinstance(value["query"], Mapping) or set(value["query"]) != {"queryId", "queryVersion"}: raise DashboardError("panel query is invalid")
        query = {"queryId": _uuid(value["query"]["queryId"], "query id"), "queryVersion": _version(value["query"]["queryVersion"], "query version")}
    return {"panelId": _uuid(value["panelId"], "panel id"), "title": value["title"], "query": query, "visualization": value["visualization"], "size": value["size"], "position": value["position"]}


def _panels(value: object, *, preset: bool = False) -> list[dict[str, object]]:
    if not isinstance(value, list) or not 1 <= len(value) <= 24: raise DashboardError("panels are invalid")
    result = [_panel(item, preset=preset) for item in value]
    if len({int(item["position"]) for item in result}) != len(result): raise DashboardError("panel positions must be unique")
    return result


class DashboardService:
    def __init__(self, repository: Any, metrics: Any | None = None, *, reference_port: Any | None = None) -> None:
        self.repository = repository
        self.reference_port = reference_port or repository
        self.metrics = metrics

    def _record(self, operation: str, started: float, outcome: str, dependency: str | None = "postgresql", error_code: str | None = None) -> None:
        record = getattr(self.metrics, "record", None)
        if callable(record): record("observatory_dashboard_operation_count", 1.0, {"operation": operation, "outcome": outcome})
        span = getattr(self.metrics, "record_component_span", None)
        if callable(span): span(component_id="aura.observatory.dashboards", component_version="1.0.0", deployment_id=str(getattr(self.metrics, "deployment_id", "observatory")), operation=operation, duration_ms=max(0.001, (monotonic() - started) * 1000.0), outcome=outcome, dependency=dependency, error_code=error_code or ("database_failure" if outcome == "failure" and dependency else None))

    async def create(self, request: Mapping[str, object], *, command_id: str | None = None) -> dict[str, object]:
        started = monotonic()
        if set(request) != {"name", "description", "panels", "createdAt"} or not isinstance(request["name"], str) or not 1 <= len(request["name"]) <= 120 or not isinstance(request["description"], str) or len(request["description"]) > 500:
            self._record("validate", started, "failure", dependency=None, error_code="validation")
            raise DashboardError("dashboard request is invalid")
        try:
            panels = _panels(request["panels"])
        except Exception:
            self._record("validate", started, "failure", dependency=None, error_code="validation")
            raise
        resolve_reference = getattr(self.reference_port, "get_resource", None)
        if callable(resolve_reference):
            for panel in panels:
                reference = panel["query"]
                if not isinstance(reference, Mapping):
                    raise DashboardError("panel query is invalid")
                found = await resolve_reference(
                    "saved_query", str(reference["queryId"]), str(reference["queryVersion"])
                )
                if found is None:
                    self._record("validate", started, "failure", dependency="postgresql", error_code="not_found")
                    raise KeyError("saved query not found")
        value = {"dashboardId": str(uuid4()), "dashboardVersion": "1.0.0", "name": request["name"], "description": request["description"], "panels": panels, "createdAt": str(request["createdAt"])}
        started = monotonic()
        try:
            result = await self.repository.create_resource("dashboard", value, command_id=command_id)
        except RuntimeError:
            self._record("write", started, "failure", error_code="conflict")
            raise
        except ConnectionError:
            self._record("write", started, "failure", error_code="transport_unavailable")
            raise
        except TimeoutError:
            self._record("write", started, "failure", error_code="timeout")
            raise
        except Exception:
            self._record("write", started, "failure")
            raise
        self._record("write", started, "success")
        return result

    async def list(self, *, cursor: str | None = None, limit: int = 50) -> dict[str, object]:
        started = monotonic()
        if not 1 <= limit <= 200:
            self._record("validate", started, "failure", dependency=None, error_code="validation")
            raise DashboardError("page size is invalid")
        try:
            result = await self.repository.list_resources("dashboard", cursor=cursor, limit=limit)
        except RuntimeError:
            self._record("read", started, "failure", error_code="conflict")
            raise
        except ConnectionError:
            self._record("read", started, "failure", error_code="transport_unavailable")
            raise
        except TimeoutError:
            self._record("read", started, "failure", error_code="timeout")
            raise
        except Exception:
            self._record("read", started, "failure")
            raise
        self._record("read", started, "success")
        return result

    async def presets(self) -> dict[str, object]:
        # Presets are immutable code-owned definitions.  They reference no
        # mutable owner record and therefore remain available during partial
        # storage degradation.
        now = utc_now()
        kinds = (("system", "System overview"), ("agent", "Agent operations"), ("module", "Module health"), ("model", "Model performance"), ("regression", "Regression watch"))
        items = []
        for index, (kind, name) in enumerate(kinds, 1):
            items.append({"presetId": f"{kind}-overview", "presetVersion": "1.0.0", "kind": kind, "name": name, "panels": [{"panelId": f"00000000-0000-4000-8000-{index:012d}", "title": name, "query": {"kind": "analytics", "metric": "operation_duration_ms", "unit": "milliseconds", "aggregation": "average", "window": "one_hour", "bucket": "auto", "groupBy": "none", "focus": "all", "facets": [], "seriesLimit": 1, "pointLimit": 60}, "visualization": "stat", "size": "medium", "position": 0}], "immutable": True})
        return {"items": items, "nextCursor": None, "snapshotAt": now, "partial": False}


__all__ = ["DashboardError", "DashboardService"]
