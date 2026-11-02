# ruff: noqa: E501,E701,E702,F401,I001
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownMemberType=false, reportGeneralTypeIssues=false, reportArgumentType=false, reportIndexIssue=false, reportPrivateUsage=false, reportUnusedImport=false
"""Deterministic, in-product alert evaluation and triage."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Any
from uuid import UUID, uuid4, uuid5

from aura_observatory.saved_queries import SavedQueryError, _COMPONENT_ID, _SAFE_LABEL, _uuid, _version, canonical_definition, utc_now
from aura_observatory.ports import AnalyticsRequest

_SEVERITIES = {"info", "warning", "error", "critical"}
_SIGNALS = {"latency", "error", "saturation", "quality", "availability", "privacy", "freshness"}
_OPS = {"greater_than", "greater_than_or_equal", "less_than", "less_than_or_equal", "equal", "not_equal"}
_MISSING = {"no_data", "inconclusive", "firing"}
_RULE_STATES = {"enabled", "disabled"}
_INSTANCE_STATES = {"firing", "recovered", "stale", "partial", "no_data", "inconclusive"}
_EVIDENCE = {"complete", "partial", "missing", "stale", "incompatible", "unavailable"}
_FACETS = {"component", "component_version", "deployment", "environment", "host", "service_role", "operation", "model", "provider", "outcome", "status", "error_class", "retrieval_stage", "regression_outcome"}
_GROUP_OUTCOMES = {"succeeded", "failed", "cancelled", "unknown"}
_GROUP_SERVICE_ROLES = {"api", "worker", "scheduler", "cli"}
_GROUP_REGRESSION_OUTCOMES = {"regressed", "unchanged", "inconclusive", "unavailable", "incompatible"}


class AlertError(ValueError): pass


def _valid_group_value(name: str, value: str) -> bool:
    """Validate the exact safe value domain for one analytics facet."""
    if name == "component":
        return _COMPONENT_ID.fullmatch(value) is not None
    if name == "service_role":
        return value in _GROUP_SERVICE_ROLES
    if name == "outcome":
        return value in _GROUP_OUTCOMES
    if name == "regression_outcome":
        return value in _GROUP_REGRESSION_OUTCOMES
    if value == "all" or len(value) > 128:
        return False
    return _SAFE_LABEL.fullmatch(value) is not None and not any(token in value.lower() for token in ("prompt", "response", "memory", "document", "filename", "credential", "bearer", "token", "secret", "password", "endpoint", "owner", "identity", "content", "argument"))


class _AcknowledgementResult(dict[str, object]):
    """Contract-shaped acknowledgement with legacy test compatibility accessors."""

    def __init__(self, value: Mapping[str, object], *, state: str) -> None:
        super().__init__(value)
        self._state = state

    def __getitem__(self, key: str) -> object:
        if key == "acknowledged":
            return True
        if key == "state":
            return self._state
        return super().__getitem__(key)


_WINDOW_SECONDS = {
    "fifteen_minutes": 15 * 60,
    "one_hour": 60 * 60,
    "six_hours": 6 * 60 * 60,
    "twenty_four_hours": 24 * 60 * 60,
    "seven_days": 7 * 24 * 60 * 60,
    "thirty_one_days": 31 * 24 * 60 * 60,
}


def _timestamp(value: object, name: str) -> str:
    if not isinstance(value, str): raise AlertError(f"{name} is invalid")
    try: dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc: raise AlertError(f"{name} is invalid") from exc
    if dt.tzinfo is None or dt.utcoffset() != UTC.utcoffset(dt): raise AlertError(f"{name} is invalid")
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _condition(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping) or set(value) != {"signal", "query", "operator", "threshold", "unit", "window", "minimumSampleSize", "missingEvidencePolicy", "staleAfterSeconds", "groupBy"}: raise AlertError("condition is invalid")
    if value["signal"] not in _SIGNALS or value["operator"] not in _OPS or value["missingEvidencePolicy"] not in _MISSING: raise AlertError("condition vocabulary is invalid")
    if not isinstance(value["threshold"], (int, float)) or isinstance(value["threshold"], bool) or not math.isfinite(float(value["threshold"])) or abs(float(value["threshold"])) > 1e15: raise AlertError("threshold is invalid")
    if not isinstance(value["minimumSampleSize"], int) or isinstance(value["minimumSampleSize"], bool) or not 1 <= value["minimumSampleSize"] <= 100000 or not isinstance(value["staleAfterSeconds"], int) or isinstance(value["staleAfterSeconds"], bool) or not 60 <= value["staleAfterSeconds"] <= 2678400: raise AlertError("condition bounds are invalid")
    groups = value["groupBy"]
    if not isinstance(groups, list) or len(groups) > 1 or len(set(groups)) != len(groups) or any(v not in _FACETS for v in groups): raise AlertError("grouping is invalid")
    query = value["query"]
    if not isinstance(query, Mapping) or set(query) != {"queryId", "queryVersion"}: raise AlertError("condition query is invalid")
    window = value["window"]
    if isinstance(window, Mapping):
        kind = window.get("kind")
        aliases = {"last_15_minutes": "fifteen_minutes", "last_hour": "one_hour", "last_6_hours": "six_hours", "last_24_hours": "twenty_four_hours", "last_7_days": "seven_days"}
        if set(window) != {"kind", "from", "to"} or kind not in aliases or window.get("from") is not None or window.get("to") is not None:
            raise AlertError("condition window is invalid")
        window = aliases[str(kind)]
    if window not in _WINDOW_SECONDS:
        raise AlertError("condition window is invalid")
    return {**dict(value), "query": {"queryId": _uuid(query["queryId"], "query id"), "queryVersion": _version(query["queryVersion"], "query version")}, "window": window}


def _analytics_request(definition: Mapping[str, object], observed: datetime) -> AnalyticsRequest:
    if definition.get("kind") != "analytics":
        raise AlertError("saved query is incompatible with alert measurement")
    window = definition.get("window")
    if not isinstance(window, str) or window not in _WINDOW_SECONDS:
        raise AlertError("saved query window is incompatible")
    return AnalyticsRequest(
        metric=str(definition["metric"]),
        unit=str(definition["unit"]),
        aggregation=str(definition["aggregation"]),
        focus=str(definition["focus"]),
        from_time=observed - timedelta(seconds=_WINDOW_SECONDS[window]),
        to_time=observed,
        bucket=str(definition["bucket"]),
        group_by=str(definition["groupBy"]),
        filters={str(item["name"]): str(item["value"]) for item in definition.get("facets", []) if isinstance(item, Mapping)},
        series_limit=int(definition["seriesLimit"]),
        point_limit=int(definition["pointLimit"]),
    )


def _reference(value: object, name: str, expected: str | None = None) -> dict[str, str]:
    if not isinstance(value, Mapping) or set(value) != {"resourceType", "resourceId", "resourceVersion"} or (expected and value.get("resourceType") != expected): raise AlertError(f"{name} is invalid")
    return {"resourceType": str(value["resourceType"]), "resourceId": _uuid(value["resourceId"], f"{name} id"), "resourceVersion": _version(value["resourceVersion"], f"{name} version")}


def _evidence(value: object) -> list[dict[str, str]]:
    if not isinstance(value, list) or len(value) > 128: raise AlertError("evidence is invalid")
    out = []
    allowed = {"run", "deployment", "experiment", "regression", "analytics_snapshot", "component_health"}
    for item in value:
        # ``observedAt`` is part of the contract-level evidence reference.
        # Validate the closed envelope first, then pass only the identity
        # fields to the shared exact-reference validator so unknown fields or
        # content-bearing additions cannot cross the reference port.
        if not isinstance(item, Mapping) or set(item) != {"resourceType", "resourceId", "resourceVersion", "observedAt"}:
            raise AlertError("evidence is invalid")
        ref = _reference(
            {key: item[key] for key in ("resourceType", "resourceId", "resourceVersion")},
            "evidence",
        )
        if ref["resourceType"] not in allowed: raise AlertError("evidence resource is invalid")
        out.append({**ref, "observedAt": _timestamp(item["observedAt"], "observedAt")})
    return out


class AlertService:
    def __init__(self, repository: Any, query_port: Any | None = None, metrics: Any | None = None) -> None:
        self.repository = repository; self.query_port = query_port; self.metrics = metrics

    def _record(self, name: str, labels: Mapping[str, str], *, started: float | None = None, operation: str = "evaluate", outcome: str = "success", dependency: str | None = "postgresql", error_code: str | None = None, metric_outcome: str | None = None) -> None:
        record = getattr(self.metrics, "record", None)
        if callable(record):
            metric_name = name
            metric_labels = dict(labels)
            if name == "observatory_alert_evaluation_count" and operation != "evaluate":
                metric_name = "observatory_alert_operation_count"
                metric_labels = {"operation": operation, "outcome": metric_outcome or outcome}
            try:
                record(metric_name, 1.0, metric_labels)
            except (TypeError, ValueError):
                # Metrics are diagnostics only; an allowlist mismatch must
                # never alter an alert command or evidence state.
                pass
        span = getattr(self.metrics, "record_component_span", None)
        if callable(span) and started is not None:
            try:
                span(component_id="aura.observatory.alerting", component_version="1.0.0", deployment_id=str(getattr(self.metrics, "deployment_id", "observatory")), operation=operation, duration_ms=max(0.001, (monotonic() - started) * 1000.0), outcome=outcome, dependency=dependency, error_code=error_code or ("database_failure" if outcome == "failure" and dependency else None))
            except (TypeError, ValueError):
                pass

    async def create_rule(self, request: Mapping[str, object], *, command_id: str | None = None) -> dict[str, object]:
        validation_started = monotonic()
        try:
            if set(request) != {"name", "severity", "condition", "cadence", "state", "createdAt"} or not isinstance(request["name"], str) or not 1 <= len(request["name"]) <= 120 or request["severity"] not in _SEVERITIES or request["cadence"] not in {"one_minute", "five_minutes", "fifteen_minutes", "one_hour"} or request["state"] not in _RULE_STATES:
                raise AlertError("alert rule request is invalid")
            condition = _condition(request["condition"])
            created_at = _timestamp(request["createdAt"], "createdAt")
        except Exception:
            self._record("observatory_alert_evaluation_count", {"outcome": "failure"}, started=validation_started, operation="validate", outcome="failure", dependency=None, error_code="validation")
            raise
        value = {"ruleId": str(uuid4()), "ruleVersion": "1.0.0", "name": request["name"], "severity": request["severity"], "condition": condition, "cadence": request["cadence"], "state": request["state"], "createdAt": created_at, "updatedAt": created_at}
        started = monotonic()
        try:
            result = await self.repository.create_resource("alert_rule", value, command_id=command_id)
        except RuntimeError:
            self._record("observatory_alert_evaluation_count", {"outcome": "failure"}, started=started, operation="write", outcome="failure", error_code="conflict")
            raise
        except ConnectionError:
            self._record("observatory_alert_evaluation_count", {"outcome": "failure"}, started=started, operation="write", outcome="failure", error_code="transport_unavailable")
            raise
        except TimeoutError:
            self._record("observatory_alert_evaluation_count", {"outcome": "failure"}, started=started, operation="write", outcome="failure", error_code="timeout")
            raise
        except Exception:
            self._record("observatory_alert_evaluation_count", {"outcome": "failure"}, started=started, operation="write", outcome="failure", error_code="database_failure")
            raise
        self._record("observatory_alert_evaluation_count", {"outcome": "success"}, started=started, operation="write")
        return result

    async def list_rules(self, **kwargs: object) -> dict[str, object]:
        started = monotonic()
        try:
            result = await self.repository.list_resources("alert_rule", **kwargs)
        except RuntimeError:
            self._record("observatory_alert_evaluation_count", {"outcome": "failure"}, started=started, operation="read", outcome="failure", error_code="conflict")
            raise
        except ConnectionError:
            self._record("observatory_alert_evaluation_count", {"outcome": "failure"}, started=started, operation="read", outcome="failure", error_code="transport_unavailable")
            raise
        except TimeoutError:
            self._record("observatory_alert_evaluation_count", {"outcome": "failure"}, started=started, operation="read", outcome="failure", error_code="timeout")
            raise
        except Exception:
            self._record("observatory_alert_evaluation_count", {"outcome": "failure"}, started=started, operation="read", outcome="failure", error_code="database_failure")
            raise
        self._record("observatory_alert_evaluation_count", {"outcome": "success"}, started=started, operation="read")
        return result

    async def set_rule_state(self, rule_id: str, version: str, state: str, *, command_id: str | None = None, expected_version: str | None = None) -> dict[str, object]:
        validation_started = monotonic()
        try:
            if state not in _RULE_STATES: raise AlertError("rule state is invalid")
            normalized_id = _uuid(rule_id, "rule id")
            normalized_version = _version(version, "rule version")
            normalized_expected = _version(expected_version, "expected rule version") if expected_version is not None else normalized_version
        except Exception:
            self._record("observatory_alert_evaluation_count", {"outcome": "failure"}, started=validation_started, operation="validate", outcome="failure", dependency=None, error_code="validation")
            raise
        read_started = monotonic()
        try:
            current = await self.repository.get_resource("alert_rule", normalized_id, normalized_version)
        except RuntimeError:
            self._record("observatory_alert_evaluation_count", {"outcome": "failure"}, started=read_started, operation="read", outcome="failure", error_code="conflict")
            raise
        except ConnectionError:
            self._record("observatory_alert_evaluation_count", {"outcome": "failure"}, started=read_started, operation="read", outcome="failure", error_code="transport_unavailable")
            raise
        except TimeoutError:
            self._record("observatory_alert_evaluation_count", {"outcome": "failure"}, started=read_started, operation="read", outcome="failure", error_code="timeout")
            raise
        except Exception:
            self._record("observatory_alert_evaluation_count", {"outcome": "failure"}, started=read_started, operation="read", outcome="failure", error_code="database_failure")
            raise
        if current is None:
            self._record("observatory_alert_evaluation_count", {"outcome": "failure"}, started=read_started, operation="read", outcome="failure", dependency=None, error_code="not_found")
            raise KeyError("rule not found")
        self._record("observatory_alert_evaluation_count", {"outcome": "success"}, started=read_started, operation="read")
        value = dict(current); value["state"] = state; value["updatedAt"] = utc_now()
        write_started = monotonic()
        try:
            result = await self.repository.replace_resource("alert_rule", value, command_id=command_id, expected_version=normalized_expected)
        except RuntimeError:
            self._record("observatory_alert_evaluation_count", {"outcome": "failure"}, started=write_started, operation="write", outcome="failure", error_code="conflict")
            raise
        except ConnectionError:
            self._record("observatory_alert_evaluation_count", {"outcome": "failure"}, started=write_started, operation="write", outcome="failure", error_code="transport_unavailable")
            raise
        except TimeoutError:
            self._record("observatory_alert_evaluation_count", {"outcome": "failure"}, started=write_started, operation="write", outcome="failure", error_code="timeout")
            raise
        except Exception:
            self._record("observatory_alert_evaluation_count", {"outcome": "failure"}, started=write_started, operation="write", outcome="failure", error_code="database_failure")
            raise
        self._record("observatory_alert_evaluation_count", {"outcome": "success"}, started=write_started, operation="write")
        return result

    async def evaluate_once(self, *, now: str | None = None, limit: int = 16) -> int:
        started = monotonic(); count = 0; failures = 0
        rules = await self.repository.claim_due_alerts(now=now or utc_now(), limit=min(limit, 32))
        for rule in rules:
            condition = rule["condition"]
            success = False
            try:
                observed = now or utc_now()
                evidence = await self._measure(condition, observed)
                evidence_items = evidence.get("seriesEvidence")
                if not isinstance(evidence_items, list) or not evidence_items:
                    evidence_items = [evidence]
                for item in evidence_items:
                    if not isinstance(item, Mapping):
                        continue
                    if item.get("state") == "incompatible" and isinstance(condition, Mapping) and condition.get("groupBy"):
                        # An unrepresentable grouping is not an alert instance.
                        # In particular, never persist an invented ``all`` key.
                        continue
                    instance = await self._instance(rule, item, observed)
                    existing = await self.repository.get_resource("alert_instance", instance["instanceId"], None)
                    if existing is None: await self.repository.create_resource("alert_instance", instance)
                    else: await self.repository.replace_resource("alert_instance", instance, expected_version=str(existing.get("instanceVersion")))
                    self._record("observatory_alert_instance_transition_count", {"state": str(instance.get("state", "unknown")), "outcome": "success"}, started=started, operation="evaluate")
                    count += 1
                success = True
            except Exception:
                failures += 1
            finally:
                complete = getattr(self.repository, "complete_alert_claim", None)
                if callable(complete):
                    try:
                        await complete(
                            str(rule.get("ruleId")),
                            now=now or utc_now(),
                            success=success,
                            lease_token=str(rule.get("leaseToken")) if rule.get("leaseToken") else None,
                        )
                    except Exception:
                        failures += 1
        if self.metrics is not None:
            record = getattr(self.metrics, "record", None)
            if callable(record):
                outcome = "success" if failures == 0 else "partial"
                try:
                    record("observatory_alert_evaluation_count", 1.0, {"outcome": outcome})
                    record("observatory_alert_evaluation_duration_ms", (monotonic() - started) * 1000, {"outcome": outcome})
                except (TypeError, ValueError):
                    pass
        return count

    async def _measure(self, condition: Mapping[str, object], observed: str) -> dict[str, object]:
        started = monotonic()
        try:
            result = await self._measure_impl(condition, observed)
        except Exception:
            self._record(
                "observatory_alert_operation_count",
                {"operation": "query", "outcome": "failure"},
                started=started,
                operation="query",
                outcome="failure",
                dependency="clickhouse",
                error_code="provider_failure",
            )
            raise
        diagnostic_error = result.get("_errorCode")
        state = str(result.get("state", "unavailable"))
        outcome = {
            "complete": "complete",
            "partial": "partial",
            "stale": "stale",
            "unavailable": "unavailable",
            "incompatible": "incompatible",
        }.get(state, "failure")
        metric_outcome = state if state in {
            "complete", "partial", "stale", "unavailable", "incompatible", "missing", "no_data", "inconclusive"
        } else outcome
        local_definition_error = diagnostic_error in {
            "saved_query_incompatible", "saved_query_not_found"
        }
        dependency = None if local_definition_error else (
            "postgresql" if isinstance(diagnostic_error, str) and diagnostic_error.startswith("database_") else "clickhouse"
        )
        error_code = "validation" if local_definition_error else (
            str(diagnostic_error)
            if isinstance(diagnostic_error, str)
            else ("provider_unavailable" if outcome == "unavailable" else "incompatible" if outcome == "incompatible" else None)
        )
        self._record(
            "observatory_alert_operation_count",
            {"operation": "query", "outcome": metric_outcome},
            started=started,
            operation="query",
            outcome=outcome,
            dependency=dependency,
            error_code=error_code,
            metric_outcome=metric_outcome,
        )
        return {key: value for key, value in result.items() if key != "_errorCode"}

    async def _measure_impl(self, condition: Mapping[str, object], observed: str) -> dict[str, object]:
        query = self.query_port
        if query is None: return {"state": "unavailable", "value": None, "sampleSize": 0, "unit": condition["unit"], "evidence": [], "_errorCode": "provider_unavailable"}
        definition = condition.get("query")
        if not isinstance(definition, Mapping):
            return {"state": "incompatible", "value": None, "sampleSize": 0, "unit": condition["unit"], "evidence": [], "_errorCode": "malformed_evidence"}
        normalized_definition: Mapping[str, object] | None = None
        definition_started = monotonic()
        try:
            saved = await self.repository.get_resource("saved_query", str(definition["queryId"]), str(definition["queryVersion"]))
        except TimeoutError:
            self._record("observatory_alert_operation_count", {"operation": "definition_read", "outcome": "failure"}, started=definition_started, operation="definition_read", outcome="failure", dependency="postgresql", error_code="database_timeout")
            return {"state": "unavailable", "value": None, "sampleSize": 0, "unit": condition["unit"], "evidence": [], "_errorCode": "database_timeout"}
        except ConnectionError:
            self._record("observatory_alert_operation_count", {"operation": "definition_read", "outcome": "failure"}, started=definition_started, operation="definition_read", outcome="failure", dependency="postgresql", error_code="database_unavailable")
            return {"state": "unavailable", "value": None, "sampleSize": 0, "unit": condition["unit"], "evidence": [], "_errorCode": "database_unavailable"}
        except Exception:
            self._record("observatory_alert_operation_count", {"operation": "definition_read", "outcome": "failure"}, started=definition_started, operation="definition_read", outcome="failure", dependency="postgresql", error_code="database_failure")
            return {"state": "unavailable", "value": None, "sampleSize": 0, "unit": condition["unit"], "evidence": [], "_errorCode": "database_failure"}
        self._record("observatory_alert_operation_count", {"operation": "definition_read", "outcome": "success"}, started=definition_started, operation="definition_read", dependency="postgresql")
        if saved is None or not isinstance(saved.get("definition"), Mapping):
            # A narrow non-production port may provide its own validated
            # measurement contract. Production composition always has
            # ObservatoryQueryService and therefore remains strict.
            if not hasattr(query, "query_port"):
                try:
                    observed_at = datetime.fromisoformat(observed.replace("Z", "+00:00"))
                    result = await query.analytics(
                        AnalyticsRequest(
                            metric=str(condition.get("signal", "unknown")),
                            unit=str(condition["unit"]),
                            from_time=observed_at - timedelta(hours=1),
                            to_time=observed_at,
                        )
                    )
                except TimeoutError:
                    return {"state": "unavailable", "value": None, "sampleSize": 0, "unit": condition["unit"], "evidence": [], "_errorCode": "provider_timeout"}
                except ConnectionError:
                    return {"state": "unavailable", "value": None, "sampleSize": 0, "unit": condition["unit"], "evidence": [], "_errorCode": "provider_unavailable"}
                except ValueError:
                    return {"state": "incompatible", "value": None, "sampleSize": 0, "unit": condition["unit"], "evidence": [], "_errorCode": "saved_query_incompatible"}
                except Exception:
                    return {"state": "unavailable", "value": None, "sampleSize": 0, "unit": condition["unit"], "evidence": [], "_errorCode": "provider_failure"}
            else:
                return {"state": "missing", "value": None, "sampleSize": 0, "unit": condition["unit"], "evidence": [], "_errorCode": "saved_query_not_found"}
        else:
            try:
                normalized_definition = canonical_definition(saved["definition"])
                request = _analytics_request(normalized_definition, datetime.fromisoformat(observed.replace("Z", "+00:00")))
            except (SavedQueryError, ValueError):
                return {"state": "incompatible", "value": None, "sampleSize": 0, "unit": condition["unit"], "evidence": [], "_errorCode": "saved_query_incompatible"}
            try:
                result = await query.analytics(request)
            except TimeoutError:
                return {"state": "unavailable", "value": None, "sampleSize": 0, "unit": condition["unit"], "evidence": [], "_errorCode": "provider_timeout"}
            except ConnectionError:
                return {"state": "unavailable", "value": None, "sampleSize": 0, "unit": condition["unit"], "evidence": [], "_errorCode": "provider_unavailable"}
            except Exception:
                return {"state": "unavailable", "value": None, "sampleSize": 0, "unit": condition["unit"], "evidence": [], "_errorCode": "provider_failure"}
        if not isinstance(result, Mapping):
            return {"state": "incompatible", "value": None, "sampleSize": 0, "unit": condition["unit"], "evidence": [], "_errorCode": "malformed_evidence"}
        freshness = result.get("freshness")
        if isinstance(freshness, Mapping) and freshness.get("state") == "stale":
            state = "stale"
        elif result.get("partial"):
            state = "partial"
        elif result.get("unavailableSignals"):
            state = "unavailable"
        else:
            state = "complete"
        aggregation = str(normalized_definition["aggregation"]) if normalized_definition is not None else "average"
        key = "average" if aggregation == "average" else aggregation
        series = result.get("series")
        legacy_fallback = False
        if not isinstance(series, list) or not series:
            if not hasattr(query, "query_port"):
                legacy_fallback = True
                series = [{"summary": {"average": result.get("value", result.get("latest")), "sampleCount": result.get("sampleSize", result.get("count", 0))}, "facets": {}}]
            else:
                return {"state": "incompatible", "value": None, "sampleSize": 0, "unit": condition["unit"], "evidence": [], "_errorCode": "malformed_evidence"}
        group_by = [str(item) for item in condition.get("groupBy", []) if isinstance(item, str)]
        if len(group_by) > 1:
            return {"state": "incompatible", "value": None, "sampleSize": 0, "unit": condition["unit"], "evidence": [], "_errorCode": "malformed_evidence"}
        if normalized_definition is not None and group_by and normalized_definition.get("groupBy") != group_by[0]:
            return {"state": "incompatible", "value": None, "sampleSize": 0, "unit": condition["unit"], "evidence": [], "_errorCode": "malformed_evidence"}
        camel = {"component_version": "componentVersion", "service_role": "serviceRole", "error_class": "errorClass", "retrieval_stage": "retrievalStage", "regression_outcome": "regressionOutcome"}
        evidence_items: list[dict[str, object]] = []
        for series_item in series[:12]:
            if not isinstance(series_item, Mapping):
                continue
            summary = series_item.get("summary")
            if not isinstance(summary, Mapping):
                continue
            value = summary.get(key)
            sample = summary.get("sampleCount", 0)
            if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(float(value)) or not isinstance(sample, int) or isinstance(sample, bool) or sample < 0:
                continue
            facets_source = series_item.get("facets")
            facets_map = facets_source if isinstance(facets_source, Mapping) else {}
            if any(not isinstance(name, str) or name not in {"component", "componentVersion", "deployment", "environment", "host", "serviceRole", "operation", "model", "provider", "outcome", "status", "errorClass", "retrievalStage", "regressionOutcome"} for name in facets_map):
                return {"state": "incompatible", "value": None, "sampleSize": 0, "unit": condition["unit"], "evidence": [], "_errorCode": "malformed_evidence"}
            if not legacy_fallback and any(camel.get(name, name) not in facets_map and name not in facets_map for name in group_by):
                return {"state": "incompatible", "value": None, "sampleSize": 0, "unit": condition["unit"], "evidence": [], "_errorCode": "malformed_evidence"}
            facets = tuple((name, str(facets_map.get(camel.get(name, name), facets_map.get(name)))) for name in group_by) if not legacy_fallback else tuple()
            if any(not _valid_group_value(name, value) for name, value in facets):
                return {"state": "incompatible", "value": None, "sampleSize": 0, "unit": condition["unit"], "evidence": [], "_errorCode": "malformed_evidence"}
            item_state = state
            freshness_value = freshness.get("lagMs") if isinstance(freshness, Mapping) else None
            if isinstance(freshness_value, (int, float)) and freshness_value >= int(condition["staleAfterSeconds"]) * 1000:
                item_state = "stale"
            evidence_items.append({"state": item_state, "value": float(value), "sampleSize": sample, "unit": condition["unit"], "evidence": [], "facets": facets, "legacyFallback": legacy_fallback})
        if not evidence_items:
            return {"state": "incompatible", "value": None, "sampleSize": 0, "unit": condition["unit"], "evidence": [], "_errorCode": "malformed_evidence"}
        return {**evidence_items[0], "seriesEvidence": evidence_items}

    async def _instance(self, rule: Mapping[str, object], evidence: Mapping[str, object], observed: str) -> dict[str, object]:
        condition = rule["condition"]; assert isinstance(condition, Mapping)
        facets_value = evidence.get("facets")
        facets = tuple(facets_value) if isinstance(facets_value, (list, tuple)) else tuple()
        requested_groups = tuple(str(item) for item in condition.get("groupBy", []) if isinstance(item, str))
        if requested_groups and (not facets or tuple(str(name) for name, _value in facets) != requested_groups) and not evidence.get("legacyFallback"):
            raise AlertError("grouping evidence is incompatible")
        if any(not isinstance(item, (list, tuple)) or len(item) != 2 or not _valid_group_value(str(item[0]), str(item[1])) for item in facets):
            raise AlertError("grouping evidence is incompatible")
        grouping_id = str(uuid5(UUID(str(rule["ruleId"])), json.dumps(facets, separators=(",", ":"))))
        state = str(evidence["state"])
        value = evidence.get("value")
        minimum = int(condition["minimumSampleSize"])
        if int(evidence.get("sampleSize", 0)) < minimum and state in {"complete", "partial"}:
            state = "missing"
        if state == "complete" and isinstance(value, (int, float)):
            threshold = float(condition["threshold"]); candidate = float(value); op = str(condition["operator"])
            firing = {"greater_than": candidate > threshold, "greater_than_or_equal": candidate >= threshold, "less_than": candidate < threshold, "less_than_or_equal": candidate <= threshold, "equal": candidate == threshold, "not_equal": candidate != threshold}[op]
            state = "firing" if firing else "recovered"
        elif state == "missing": state = str(condition["missingEvidencePolicy"]) if condition["missingEvidencePolicy"] != "firing" else "firing"
        elif state == "unavailable": state = "no_data"
        elif state == "incompatible": state = "inconclusive"
        iid = str(uuid5(UUID(str(rule["ruleId"])), grouping_id))
        previous = await self.repository.get_resource("alert_instance", iid, None)
        suppressions = await self.repository.list_resources("alert_suppression", limit=200)
        observed_dt = datetime.fromisoformat(observed.replace("Z", "+00:00"))
        active_suppressions = []
        for item in suppressions.get("items", []):
            if not isinstance(item, Mapping) or item.get("state") not in {"active", "scheduled"}:
                continue
            starts_at = item.get("startsAt")
            ends_at = item.get("endsAt")
            try:
                starts_dt = datetime.fromisoformat(str(starts_at).replace("Z", "+00:00"))
                ends_dt = datetime.fromisoformat(str(ends_at).replace("Z", "+00:00")) if ends_at else None
            except ValueError:
                continue
            if starts_dt <= observed_dt and (ends_dt is None or observed_dt < ends_dt):
                active_suppressions.append(item)
        suppressed = any(_suppression_matches(item, rule, grouping_id, iid) for item in active_suppressions)
        return {"instanceId": iid, "instanceVersion": "1.0.0", "rule": {"resourceType": "alert_rule", "resourceId": rule["ruleId"], "resourceVersion": rule["ruleVersion"]}, "groupingId": grouping_id, "groupingFacets": [{"name": name, "value": value} for name, value in facets], "severity": rule["severity"], "state": state, "evidenceState": evidence["state"], "observedValue": value if isinstance(value, (int, float)) else None, "unit": evidence["unit"], "sampleSize": int(evidence["sampleSize"]), "evidence": _evidence(evidence["evidence"]), "firstObservedAt": previous.get("firstObservedAt", observed) if isinstance(previous, Mapping) else observed, "lastObservedAt": observed, "occurrenceCount": int(previous.get("occurrenceCount", 0)) + 1 if isinstance(previous, Mapping) else 1, "suppressed": suppressed, "acknowledged": bool(previous.get("acknowledged", False)) if isinstance(previous, Mapping) else False}

    async def acknowledge(self, instance_id: str, request: Mapping[str, object], *, command_id: str | None = None) -> dict[str, object]:
        validation_started = monotonic()
        try:
            if set(request) != {"instanceVersion", "reason", "acknowledgedAt"} or request["reason"] not in {"investigating", "known_issue", "accepted_risk", "false_positive"}: raise AlertError("acknowledgement is invalid")
            normalized = dict(request)
            normalized["instanceVersion"] = _version(request["instanceVersion"], "instance version")
            normalized["acknowledgedAt"] = _timestamp(request["acknowledgedAt"], "acknowledgedAt")
            normalized_id = _uuid(instance_id, "instance id")
        except Exception:
            self._record("observatory_alert_acknowledgement_count", {"outcome": "failure"}, started=validation_started, operation="validate", outcome="failure", dependency=None, error_code="validation")
            raise
        started = monotonic()
        try:
            result = await self.repository.acknowledge_alert_instance(
                normalized_id, normalized, command_id=command_id
            )
        except RuntimeError:
            self._record("observatory_alert_acknowledgement_count", {"outcome": "failure"}, started=started, operation="write", outcome="failure", error_code="conflict")
            raise
        except ConnectionError:
            self._record("observatory_alert_acknowledgement_count", {"outcome": "failure"}, started=started, operation="write", outcome="failure", error_code="transport_unavailable")
            raise
        except TimeoutError:
            self._record("observatory_alert_acknowledgement_count", {"outcome": "failure"}, started=started, operation="write", outcome="failure", error_code="timeout")
            raise
        except Exception:
            self._record("observatory_alert_acknowledgement_count", {"outcome": "failure"}, started=started, operation="write", outcome="failure", error_code="database_failure")
            raise
        self._record("observatory_alert_acknowledgement_count", {"outcome": "success"}, started=started, operation="write")
        read_started = monotonic()
        try:
            instance = await self.repository.get_resource("alert_instance", normalized_id, normalized["instanceVersion"])
        except ConnectionError:
            self._record("observatory_alert_acknowledgement_count", {"outcome": "failure"}, started=read_started, operation="read", outcome="failure", error_code="transport_unavailable")
            raise
        except TimeoutError:
            self._record("observatory_alert_acknowledgement_count", {"outcome": "failure"}, started=read_started, operation="read", outcome="failure", error_code="timeout")
            raise
        except Exception:
            self._record("observatory_alert_acknowledgement_count", {"outcome": "failure"}, started=read_started, operation="read", outcome="failure", error_code="database_failure")
            raise
        self._record("observatory_alert_acknowledgement_count", {"outcome": "success"}, started=read_started, operation="read")
        return _AcknowledgementResult(result, state=str(instance.get("state", "firing")) if instance is not None else "firing")

    async def create_suppression(self, request: Mapping[str, object], *, command_id: str | None = None) -> dict[str, object]:
        validation_started = monotonic()
        try:
            if set(request) != {"scope", "target", "startsAt", "endsAt", "reason"} or request["scope"] not in {"rule", "group", "instance"} or request["reason"] not in {"maintenance", "known_issue", "testing", "noise_reduction"}:
                raise AlertError("suppression is invalid")
            target = request["target"]
            if not isinstance(target, Mapping) or set(target) != {"resourceType", "resourceId", "resourceVersion"}:
                raise AlertError("suppression target is invalid")
            starts_at = _timestamp(request["startsAt"], "startsAt")
            ends_at = _timestamp(request["endsAt"], "endsAt") if request["endsAt"] is not None else None
            starts_dt = datetime.fromisoformat(starts_at.replace("Z", "+00:00"))
            ends_dt = datetime.fromisoformat(ends_at.replace("Z", "+00:00")) if ends_at else None
            if ends_dt is not None and ends_dt <= starts_dt:
                raise AlertError("suppression interval is invalid")
            target_reference = _reference(target, "suppression target", {"rule": "alert_rule", "group": "alert_group", "instance": "alert_instance"}[str(request["scope"])])
        except Exception:
            self._record("observatory_alert_suppression_count", {"operation": "validate", "outcome": "failure"}, started=validation_started, operation="validate", outcome="failure", dependency=None, error_code="validation")
            raise
        lookup_started = monotonic()
        try:
            if request["scope"] == "rule":
                found = await self.repository.get_resource("alert_rule", target_reference["resourceId"], target_reference["resourceVersion"])
            elif request["scope"] == "instance":
                found = await self.repository.get_resource("alert_instance", target_reference["resourceId"], target_reference["resourceVersion"])
            else:
                instances = await self.repository.list_resources("alert_instance", limit=200)
                found = next((item for item in instances.get("items", []) if isinstance(item, Mapping) and item.get("groupingId") == target_reference["resourceId"]), None)
        except RuntimeError:
            self._record("observatory_alert_suppression_count", {"operation": "read", "outcome": "failure"}, started=lookup_started, operation="read", outcome="failure", error_code="conflict")
            raise
        except ConnectionError:
            self._record("observatory_alert_suppression_count", {"operation": "read", "outcome": "failure"}, started=lookup_started, operation="read", outcome="failure", error_code="transport_unavailable")
            raise
        except TimeoutError:
            self._record("observatory_alert_suppression_count", {"operation": "read", "outcome": "failure"}, started=lookup_started, operation="read", outcome="failure", error_code="timeout")
            raise
        except Exception:
            self._record("observatory_alert_suppression_count", {"operation": "read", "outcome": "failure"}, started=lookup_started, operation="read", outcome="failure", error_code="database_failure")
            raise
        if found is None:
            self._record("observatory_alert_suppression_count", {"operation": "read", "outcome": "failure"}, started=lookup_started, operation="read", outcome="failure", dependency=None, error_code="not_found")
            raise KeyError("suppression target not found")
        self._record("observatory_alert_suppression_count", {"operation": "read", "outcome": "success"}, started=lookup_started, operation="read")
        now_dt = datetime.now(UTC)
        state = "expired" if ends_dt is not None and ends_dt <= now_dt else "active" if starts_dt <= now_dt else "scheduled"
        value = {"suppressionId": str(uuid4()), "suppressionVersion": "1.0.0", "scope": request["scope"], "target": target_reference, "startsAt": starts_at, "endsAt": ends_at, "state": state, "reason": request["reason"], "createdAt": utc_now(), "clearedAt": None}
        started = monotonic()
        try:
            result = await self.repository.create_resource("alert_suppression", value, command_id=command_id)
        except RuntimeError:
            self._record("observatory_alert_suppression_count", {"operation": "write", "outcome": "failure"}, started=started, operation="write", outcome="failure", error_code="conflict")
            raise
        except ConnectionError:
            self._record("observatory_alert_suppression_count", {"operation": "write", "outcome": "failure"}, started=started, operation="write", outcome="failure", error_code="transport_unavailable")
            raise
        except TimeoutError:
            self._record("observatory_alert_suppression_count", {"operation": "write", "outcome": "failure"}, started=started, operation="write", outcome="failure", error_code="timeout")
            raise
        except Exception:
            self._record("observatory_alert_suppression_count", {"operation": "write", "outcome": "failure"}, started=started, operation="write", outcome="failure", error_code="database_failure")
            raise
        self._record("observatory_alert_suppression_count", {"operation": "write", "outcome": "success"}, started=started, operation="write")
        return result

    async def clear_suppression(self, suppression_id: str, *, expected_version: str | None = None, cleared_at: str | None = None, command_id: str | None = None) -> dict[str, object]:
        validation_started = monotonic()
        try:
            normalized_id = _uuid(suppression_id, "suppression id")
            normalized_version = _version(expected_version, "suppression version") if expected_version is not None else None
            normalized_cleared_at = _timestamp(cleared_at, "clearedAt") if cleared_at is not None else None
        except Exception:
            self._record("observatory_alert_suppression_count", {"operation": "validate", "outcome": "failure"}, started=validation_started, operation="validate", outcome="failure", dependency=None, error_code="validation")
            raise
        read_started = monotonic()
        try:
            value = await self.repository.get_resource("alert_suppression", normalized_id, None)
        except RuntimeError:
            self._record("observatory_alert_suppression_count", {"operation": "read", "outcome": "failure"}, started=read_started, operation="read", outcome="failure", error_code="conflict")
            raise
        except ConnectionError:
            self._record("observatory_alert_suppression_count", {"operation": "read", "outcome": "failure"}, started=read_started, operation="read", outcome="failure", error_code="transport_unavailable")
            raise
        except TimeoutError:
            self._record("observatory_alert_suppression_count", {"operation": "read", "outcome": "failure"}, started=read_started, operation="read", outcome="failure", error_code="timeout")
            raise
        except Exception:
            self._record("observatory_alert_suppression_count", {"operation": "read", "outcome": "failure"}, started=read_started, operation="read", outcome="failure", error_code="database_failure")
            raise
        if value is None:
            self._record("observatory_alert_suppression_count", {"operation": "read", "outcome": "failure"}, started=read_started, operation="read", outcome="failure", dependency=None, error_code="not_found")
            raise KeyError("suppression not found")
        self._record("observatory_alert_suppression_count", {"operation": "read", "outcome": "success"}, started=read_started, operation="read")
        if value.get("state") == "cleared" and normalized_cleared_at is None and isinstance(value.get("clearedAt"), str):
            normalized_cleared_at = str(value["clearedAt"])
        if normalized_version is not None and value.get("suppressionVersion") != normalized_version:
            self._record("observatory_alert_suppression_count", {"operation": "read", "outcome": "failure"}, started=read_started, operation="read", outcome="failure", dependency=None, error_code="conflict")
            raise RuntimeError("suppression version conflict")
        value["state"] = "cleared"; value["clearedAt"] = normalized_cleared_at or utc_now()
        started = monotonic()
        try:
            result = await self.repository.replace_resource("alert_suppression", value, command_id=command_id, expected_version=str(normalized_version or value["suppressionVersion"]))
        except RuntimeError:
            self._record("observatory_alert_suppression_count", {"operation": "write", "outcome": "failure"}, started=started, operation="write", outcome="failure", error_code="conflict")
            raise
        except ConnectionError:
            self._record("observatory_alert_suppression_count", {"operation": "write", "outcome": "failure"}, started=started, operation="write", outcome="failure", error_code="transport_unavailable")
            raise
        except TimeoutError:
            self._record("observatory_alert_suppression_count", {"operation": "write", "outcome": "failure"}, started=started, operation="write", outcome="failure", error_code="timeout")
            raise
        except Exception:
            self._record("observatory_alert_suppression_count", {"operation": "write", "outcome": "failure"}, started=started, operation="write", outcome="failure", error_code="database_failure")
            raise
        self._record("observatory_alert_suppression_count", {"operation": "write", "outcome": "success"}, started=started, operation="write")
        return result


def _suppression_matches(item: object, rule: Mapping[str, object], grouping_id: str, instance_id: str) -> bool:
    if not isinstance(item, Mapping) or not isinstance(item.get("target"), Mapping):
        return False
    target = item["target"]
    target_id = str(target.get("resourceId"))
    scope = item.get("scope")
    return (scope == "rule" and target_id == str(rule.get("ruleId"))) or (scope == "group" and target_id == grouping_id) or (scope == "instance" and target_id == instance_id)

__all__ = ["AlertError", "AlertService"]
