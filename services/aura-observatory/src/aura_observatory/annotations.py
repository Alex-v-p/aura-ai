# ruff: noqa: E501,E701,E702,F401,I001
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownMemberType=false, reportGeneralTypeIssues=false, reportArgumentType=false, reportIndexIssue=false, reportPrivateUsage=false, reportUnusedImport=false
"""Bounded owner-authored annotations, excluded from operational telemetry."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol
from time import monotonic
from uuid import uuid4

from aura_observatory.saved_queries import _uuid, _version, utc_now

_TARGETS = {"run", "deployment", "experiment", "regression", "incident"}


class AnnotationError(ValueError): pass


class AnnotationTargetNotFound(AnnotationError):
    """The exact opaque target identity is absent."""


class AnnotationTargetUnavailable(AnnotationError):
    """The public reference provider cannot currently answer."""


class AnnotationReferencePort(Protocol):
    async def validate_annotation_target(self, target: Mapping[str, str]) -> None: ...


def _target(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping) or set(value) != {"targetType", "targetId", "targetVersion"} or value["targetType"] not in _TARGETS: raise AnnotationError("annotation target is invalid")
    return {"targetType": str(value["targetType"]), "targetId": _uuid(value["targetId"], "target id"), "targetVersion": _version(value["targetVersion"], "target version")}


class AnnotationService:
    def __init__(self, repository: Any, reference_port: AnnotationReferencePort | None = None, metrics: Any | None = None) -> None:
        self.repository = repository
        self.metrics = metrics
        self.reference_port = reference_port or repository

    def _record(self, operation: str, started: float, outcome: str, dependency: str | None = "postgresql", error_code: str | None = None) -> None:
        record = getattr(self.metrics, "record", None)
        if callable(record): record("observatory_annotation_operation_count", 1.0, {"operation": operation, "outcome": outcome})
        span = getattr(self.metrics, "record_component_span", None)
        if callable(span): span(component_id="aura.observatory.annotations", component_version="1.0.0", deployment_id=str(getattr(self.metrics, "deployment_id", "observatory")), operation=operation, duration_ms=max(0.001, (monotonic() - started) * 1000.0), outcome=outcome, dependency=dependency, error_code=error_code or ("database_failure" if outcome == "failure" and dependency else None))

    async def create(self, request: Mapping[str, object], *, command_id: str | None = None) -> dict[str, object]:
        started = monotonic()
        if set(request) != {"target", "text", "createdAt"} or not isinstance(request["text"], str) or not 1 <= len(request["text"]) <= 2000:
            self._record("validate", started, "failure", dependency=None, error_code="validation")
            raise AnnotationError("annotation request is invalid")
        try:
            target = _target(request["target"])
        except Exception:
            self._record("validate", started, "failure", dependency=None, error_code="validation")
            raise
        try:
            await self.reference_port.validate_annotation_target(target)
        except KeyError as exc:
            self._record("validate", started, "failure", dependency=None, error_code="not_found")
            raise AnnotationTargetNotFound("annotation target not found") from exc
        except (RuntimeError, ConnectionError) as exc:
            self._record("validate", started, "unavailable", dependency=None, error_code="provider_unavailable")
            raise AnnotationTargetUnavailable("annotation reference provider unavailable") from exc
        except TimeoutError as exc:
            self._record("validate", started, "unavailable", dependency=None, error_code="provider_timeout")
            raise AnnotationTargetUnavailable("annotation reference provider unavailable") from exc
        value = {"annotationId": str(uuid4()), "annotationVersion": "1.0.0", "target": target, "text": request["text"], "createdAt": str(request["createdAt"]), "updatedAt": str(request["createdAt"])}
        try:
            result = await self.repository.create_resource("annotation", value, command_id=command_id)
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
            self._record("write", started, "failure", error_code="database_failure")
            raise
        self._record("write", started, "success")
        return result

    async def list(self, *, target_type: str | None = None, target_id: str | None = None, target_version: str | None = None, cursor: str | None = None, limit: int = 50) -> dict[str, object]:
        target = None
        started = monotonic()
        try:
            if target_type or target_id or target_version:
                if not all((target_type, target_id, target_version)):
                    raise AnnotationError("annotation target filter is invalid")
                target = {"targetType": str(target_type), "targetId": _uuid(target_id, "target id"), "targetVersion": _version(target_version, "target version")}
        except Exception:
            self._record("validate", started, "failure", dependency=None, error_code="validation")
            raise
        try:
            result = await self.repository.list_resources("annotation", target=target, cursor=cursor, limit=limit)
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
            self._record("read", started, "failure", error_code="database_failure")
            raise
        self._record("read", started, "success")
        return result


__all__ = ["AnnotationError", "AnnotationReferencePort", "AnnotationService", "AnnotationTargetNotFound", "AnnotationTargetUnavailable"]
