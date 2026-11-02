# ruff: noqa: E501,E701,E702,F401,I001
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownMemberType=false, reportGeneralTypeIssues=false, reportArgumentType=false, reportIndexIssue=false, reportPrivateUsage=false, reportUnusedImport=false
"""Explicit incident state machine with optimistic version checks."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol
from time import monotonic
from uuid import uuid4

from aura_observatory.alerting import AlertError, _evidence, _reference, _timestamp
from aura_observatory.saved_queries import _uuid, _version, utc_now

_STATES = ("open", "investigating", "mitigated", "resolved", "closed")
_TRANSITIONS = {"open": {"investigating", "mitigated", "resolved"}, "investigating": {"mitigated", "resolved", "open"}, "mitigated": {"investigating", "resolved", "open"}, "resolved": {"closed", "investigating"}, "closed": set()}
_SEVERITIES = {"info", "warning", "error", "critical"}
_IMPACTS = {"none", "limited", "degraded", "major", "critical"}


class IncidentError(ValueError): pass


class IncidentReferencePort(Protocol):
    async def validate_incident_references(
        self,
        related_alerts: list[Mapping[str, str]],
        evidence: list[Mapping[str, str]],
    ) -> None: ...


class IncidentService:
    def __init__(self, repository: Any, metrics: Any | None = None, reference_port: IncidentReferencePort | None = None) -> None: self.repository = repository; self.metrics = metrics; self.reference_port = reference_port

    def _record(self, state: str, outcome: str = "success", *, started: float | None = None, operation: str = "write", dependency: str | None = "postgresql", error_code: str | None = None) -> None:
        record = getattr(self.metrics, "record", None)
        if callable(record) and state in _STATES:
            record("observatory_incident_transition_count", 1.0, {"state": state, "outcome": outcome})
        span = getattr(self.metrics, "record_component_span", None)
        if callable(span) and started is not None:
            span(component_id="aura.observatory.incidents", component_version="1.0.0", deployment_id=str(getattr(self.metrics, "deployment_id", "observatory")), operation=operation, duration_ms=max(0.001, (monotonic() - started) * 1000.0), outcome=outcome, dependency=dependency, error_code=error_code or ("database_failure" if outcome == "failure" and dependency else None))

    async def create(self, request: Mapping[str, object], *, command_id: str | None = None) -> dict[str, object]:
        started = monotonic()
        required = {"title", "severity", "impact", "relatedAlerts", "evidence", "createdAt"}
        if set(request) != required or not isinstance(request["title"], str) or not 1 <= len(request["title"]) <= 160 or request["severity"] not in _SEVERITIES or request["impact"] not in _IMPACTS:
            self._record("open", "failure", started=started, operation="validate", dependency=None, error_code="validation")
            raise IncidentError("incident request is invalid")
        refs = request["relatedAlerts"]
        if not isinstance(refs, list) or not 1 <= len(refs) <= 128:
            self._record("open", "failure", started=started, operation="validate", dependency=None, error_code="validation")
            raise IncidentError("related alerts are invalid")
        try:
            related = [_reference(item, "related alert", "alert_instance") for item in refs]
            if len({(r["resourceId"], r["resourceVersion"]) for r in related}) != len(related): raise IncidentError("related alerts are not unique")
            evidence = _evidence(request["evidence"])
        except Exception:
            self._record("open", "failure", started=started, operation="validate", dependency=None, error_code="validation")
            raise
        if self.reference_port is not None:
            try:
                await self.reference_port.validate_incident_references(related, evidence)
            except KeyError as exc:
                self._record("open", "failure", started=started, operation="validate", dependency=None, error_code="not_found")
                raise IncidentError("incident reference not found") from exc
            except (RuntimeError, ConnectionError) as exc:
                self._record("open", "unavailable", started=started, operation="validate", dependency=None, error_code="provider_unavailable")
                raise IncidentError("incident reference unavailable") from exc
            except TimeoutError as exc:
                self._record("open", "unavailable", started=started, operation="validate", dependency=None, error_code="provider_timeout")
                raise IncidentError("incident reference unavailable") from exc
        try:
            created = _timestamp(request["createdAt"], "createdAt")
        except Exception:
            self._record("open", "failure", started=started, operation="validate", dependency=None, error_code="validation")
            raise
        value = {"incidentId": str(uuid4()), "incidentVersion": "1.0.0", "title": request["title"], "state": "open", "severity": request["severity"], "impact": request["impact"], "relatedAlerts": related, "evidence": evidence, "resolutionNote": None, "createdAt": created, "updatedAt": created}
        started = monotonic()
        try:
            result = await self.repository.create_resource("incident", value, command_id=command_id)
        except RuntimeError:
            self._record("open", "failure", started=started, error_code="conflict")
            raise
        except ConnectionError:
            self._record("open", "failure", started=started, error_code="transport_unavailable")
            raise
        except TimeoutError:
            self._record("open", "failure", started=started, error_code="timeout")
            raise
        except Exception:
            self._record("open", "failure", started=started, error_code="database_failure")
            raise
        self._record("open", started=started)
        return result

    async def transition(self, incident_id: str, request: Mapping[str, object], *, command_id: str | None = None) -> dict[str, object]:
        required = {"expectedVersion", "targetState", "impact", "resolutionNote", "transitionedAt"}
        started = monotonic()
        try:
            if set(request) != required or request["targetState"] not in _STATES or request["impact"] not in _IMPACTS or request["resolutionNote"] is not None and (not isinstance(request["resolutionNote"], str) or len(request["resolutionNote"]) > 2000): raise IncidentError("incident transition is invalid")
            incident_uuid = _uuid(incident_id, "incident id")
        except Exception:
            self._record("transition", "failure", started=started, operation="validate", dependency=None, error_code="validation")
            raise
        try:
            value = await self.repository.get_resource("incident", incident_uuid, None)
        except ConnectionError as exc:
            self._record("transition", "failure", started=started, operation="read", error_code="transport_unavailable")
            raise exc
        except TimeoutError as exc:
            self._record("transition", "failure", started=started, operation="read", error_code="timeout")
            raise exc
        except RuntimeError:
            self._record("transition", "failure", started=started, operation="read", error_code="conflict")
            raise
        except Exception:
            self._record("transition", "failure", started=started, operation="read", error_code="database_failure")
            raise
        if value is None:
            self._record("transition", "failure", started=started, operation="read", dependency=None, error_code="not_found")
            raise KeyError("incident not found")
        if value["incidentVersion"] != request["expectedVersion"]:
            self._record("transition", "failure", started=started, operation="read", error_code="conflict")
            raise RuntimeError("stale incident version")
        if request["targetState"] not in _TRANSITIONS[str(value["state"])]:
            self._record("transition", "failure", started=started, operation="validate", dependency=None, error_code="validation")
            raise IncidentError("incident transition is invalid")
        value["state"] = request["targetState"]
        value["impact"] = request["impact"]
        value["resolutionNote"] = request["resolutionNote"]
        try:
            value["updatedAt"] = _timestamp(request["transitionedAt"], "transitionedAt")
        except Exception:
            self._record("transition", "failure", started=started, operation="validate", dependency=None, error_code="validation")
            raise
        try:
            major, minor, patch = (int(part) for part in str(value["incidentVersion"]).split("."))
        except (TypeError, ValueError) as exc:
            raise IncidentError("incident version is invalid") from exc
        value["incidentVersion"] = f"{major}.{minor}.{patch + 1}"
        started = monotonic()
        try:
            result = await self.repository.replace_resource(
                "incident",
                value,
                command_id=command_id,
                expected_version=str(request["expectedVersion"]),
            )
        except RuntimeError:
            self._record(str(request["targetState"]), "failure", started=started, error_code="conflict")
            raise
        except ConnectionError:
            self._record(str(request["targetState"]), "failure", started=started, error_code="transport_unavailable")
            raise
        except TimeoutError:
            self._record(str(request["targetState"]), "failure", started=started, error_code="timeout")
            raise
        except Exception:
            self._record(str(request["targetState"]), "failure", started=started, error_code="database_failure")
            raise
        self._record(str(request["targetState"]), started=started)
        return result


__all__ = ["IncidentError", "IncidentService"]
