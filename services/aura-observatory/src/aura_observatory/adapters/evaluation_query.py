# ruff: noqa: E501
# pyright: reportPrivateUsage=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownMemberType=false, reportGeneralTypeIssues=false, reportArgumentType=false, reportUnnecessaryIsInstance=false

"""Durable PostgreSQL and ClickHouse evaluation query adapter.

Provider-specific I/O lives here; the application query module exposes only
transport-neutral ports, projections, and validation.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from functools import wraps
from typing import Any

import httpx

from aura_observatory.evaluation_query import (
    EvaluationQueryError,
    EvaluationUnavailable,
    _date,
    _instant_filter,
    _json_mapping,
    _required_uuid,
    iso,
    page,
    project_definition,
    resource_ref,
)

_COMPONENT = re.compile(r"^aura\.[a-z][a-z0-9_.-]{0,90}$")
_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


def _clickhouse_timestamp(value: object) -> str:
    """Format an aware UTC instant for ClickHouse DateTime64 parameters."""

    if not isinstance(value, datetime) or value.tzinfo is None:
        raise EvaluationQueryError("cursor is invalid")
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")


def _canonical_cursor_instant(value: object) -> str | None:
    """Return one public UTC representation for a cursor instant.

    Cursor timestamps may be read from PostgreSQL/ClickHouse rows or from a
    legacy opaque cursor.  Accept both aware datetime values and parseable
    aware ISO/RFC3339 strings, but never let a naive or malformed value reach
    a provider query.  ``iso`` is the shared public serializer for the final
    RFC3339 ``T``/``Z`` representation.
    """

    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise EvaluationQueryError("cursor is invalid") from exc
    else:
        raise EvaluationQueryError("cursor is invalid")
    try:
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise EvaluationQueryError("cursor is invalid")
        canonical = iso(parsed.astimezone(UTC))
    except (OverflowError, TypeError, ValueError) as exc:
        raise EvaluationQueryError("cursor is invalid") from exc
    if canonical is None:
        raise EvaluationQueryError("cursor is invalid")
    return canonical


def _canonical_clickhouse_score_instant(value: object) -> str:
    """Canonicalize a trusted ClickHouse UTC DateTime64 score value.

    ClickHouse's JSON format commonly returns UTC ``DateTime64`` values with
    a space separator and no offset.  That representation is provider data,
    not client cursor input, so the missing offset is explicitly interpreted
    as UTC at this boundary before using the shared cursor serializer.
    """

    if not isinstance(value, str):
        raise EvaluationUnavailable("stored score timestamp is unavailable")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        canonical = _canonical_cursor_instant(parsed)
    except (EvaluationQueryError, ValueError, TypeError, OverflowError) as exc:
        raise EvaluationUnavailable("stored score timestamp is unavailable") from exc
    if canonical is None:
        raise EvaluationUnavailable("stored score timestamp is unavailable")
    return canonical


def _stored_simple_provenance(value: object) -> dict[str, str]:
    """Project only a persisted EvaluationProvenance, never synthesize one."""

    if not isinstance(value, Mapping) or set(value) != {
        "createdByComponent",
        "createdByVersion",
        "sourceDigest",
    }:
        raise EvaluationUnavailable("stored evaluation provenance is unavailable")
    component = value.get("createdByComponent")
    version = value.get("createdByVersion")
    digest = value.get("sourceDigest")
    if (
        not isinstance(component, str)
        or _COMPONENT.fullmatch(component) is None
        or not isinstance(version, str)
        or _VERSION.fullmatch(version) is None
        or not isinstance(digest, str)
        or _DIGEST.fullmatch(digest) is None
    ):
        raise EvaluationUnavailable("stored evaluation provenance is unavailable")
    return {"createdByComponent": component, "createdByVersion": version, "sourceDigest": digest}


def _stored_simple_provenance_columns(
    component: object, version: object, digest: object
) -> dict[str, str]:
    return _stored_simple_provenance(
        {
            "createdByComponent": component,
            "createdByVersion": version,
            "sourceDigest": digest,
        }
    )


def _stored_execution_provenance(value: object) -> dict[str, object]:
    """Rebuild the closed execution provenance shape from persisted metadata."""

    if not isinstance(value, Mapping) or set(value) != {
        "dataset", "suite", "evaluators", "components", "model", "deploymentRevision", "reproducibility"
    }:
        raise EvaluationUnavailable("stored execution provenance is unavailable")
    result: dict[str, object] = {}
    for key in ("dataset", "suite"):
        ref = value.get(key)
        if not isinstance(ref, Mapping) or set(ref) not in ({"id", "version"}, {"resourceId", "resourceVersion"}):
            raise EvaluationUnavailable("stored evaluation provenance is unavailable")
        resource_id = _required_uuid(ref.get("id", ref.get("resourceId")), f"provenance.{key}.id")
        resource_version = _safe_version(ref.get("version", ref.get("resourceVersion")), f"provenance.{key}.version")
        result[key] = {"resourceId": resource_id, "resourceVersion": resource_version}
    evaluators = value.get("evaluators")
    if not isinstance(evaluators, list) or not 1 <= len(evaluators) <= 32:
        raise EvaluationUnavailable("stored evaluation provenance is unavailable")
    evaluator_values: list[dict[str, str]] = []
    for evaluator in evaluators:
        if not isinstance(evaluator, Mapping) or set(evaluator) != {"evaluatorId", "evaluatorVersion", "kind"}:
            raise EvaluationUnavailable("stored evaluation provenance is unavailable")
        evaluator_values.append({
            "evaluatorId": _required_uuid(evaluator.get("evaluatorId"), "provenance.evaluatorId"),
            "evaluatorVersion": _safe_version(evaluator.get("evaluatorVersion"), "provenance.evaluatorVersion"),
            "kind": _safe_value(evaluator.get("kind"), {"deterministic", "local_judge", "human_review"}, "provenance.evaluator.kind"),
        })
    result["evaluators"] = evaluator_values
    components = value.get("components")
    if not isinstance(components, list) or not 1 <= len(components) <= 32:
        raise EvaluationUnavailable("stored evaluation provenance is unavailable")
    component_values: list[dict[str, str]] = []
    for component in components:
        if not isinstance(component, Mapping) or set(component) != {"componentId", "componentVersion", "codeRevision", "imageDigest", "configurationRevision"}:
            raise EvaluationUnavailable("stored evaluation provenance is unavailable")
        component_id = component.get("componentId")
        if not isinstance(component_id, str) or _COMPONENT.fullmatch(component_id) is None:
            raise EvaluationUnavailable("stored evaluation provenance is unavailable")
        component_values.append({
            "componentId": component_id,
            "componentVersion": _safe_version(component.get("componentVersion"), "provenance.componentVersion"),
            "codeRevision": _safe_revision(component.get("codeRevision"), "provenance.codeRevision"),
            "imageDigest": _safe_digest(component.get("imageDigest"), "provenance.imageDigest"),
            "configurationRevision": _safe_revision(component.get("configurationRevision"), "provenance.configurationRevision"),
        })
    result["components"] = component_values
    model = value.get("model")
    if not isinstance(model, Mapping) or set(model) != {"modelId", "modelVersion", "modelDigest"}:
        raise EvaluationUnavailable("stored evaluation provenance is unavailable")
    model_id = model.get("modelId")
    if not isinstance(model_id, str) or not 1 <= len(model_id) <= 193 or re.fullmatch(r"[a-z0-9][a-z0-9._+-]{0,127}(?::[a-z0-9._+-]+)?", model_id) is None or any(marker in model_id.casefold() for marker in ("owner", "identity", "credential", "bearer", "token", "secret", "password", "private", "content", "hash", "digest", "endpoint")):
        raise EvaluationUnavailable("stored evaluation provenance is unavailable")
    result["model"] = {
        "modelId": model_id,
        "modelVersion": _safe_version(model.get("modelVersion"), "provenance.modelVersion"),
        "modelDigest": _safe_digest(model.get("modelDigest"), "provenance.modelDigest"),
    }
    result["deploymentRevision"] = _safe_revision(value.get("deploymentRevision"), "provenance.deploymentRevision")
    reproducibility = value.get("reproducibility")
    if not isinstance(reproducibility, Mapping) or set(reproducibility) != {"seed", "clockMode", "timeZone", "locale", "fixtureRevision", "promptRevision", "policyRevision"}:
        raise EvaluationUnavailable("stored evaluation provenance is unavailable")
    seed = reproducibility.get("seed")
    locale = reproducibility.get("locale")
    if type(seed) is not int or not 0 <= seed <= 2_147_483_647 or reproducibility.get("timeZone") != "UTC" or not isinstance(locale, str) or re.fullmatch(r"[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*", locale) is None:
        raise EvaluationUnavailable("stored evaluation provenance is unavailable")
    result["reproducibility"] = {
        "seed": seed,
        "clockMode": _safe_value(reproducibility.get("clockMode"), {"fixed", "recorded"}, "provenance.clockMode"),
        "timeZone": "UTC",
        "locale": locale,
        "fixtureRevision": _safe_revision(reproducibility.get("fixtureRevision"), "provenance.fixtureRevision"),
        "promptRevision": _safe_revision(reproducibility.get("promptRevision"), "provenance.promptRevision"),
        "policyRevision": _safe_revision(reproducibility.get("policyRevision"), "provenance.policyRevision"),
    }
    return result


def _stored_runner_execution_provenance(value: object) -> dict[str, object]:
    """Project the runner-specific definition-reference spelling."""

    result = _stored_execution_provenance(value)
    for key in ("dataset", "suite"):
        reference = result[key]
        if not isinstance(reference, Mapping):
            raise EvaluationUnavailable("stored runner provenance is unavailable")
        result[key] = {
            "id": reference["resourceId"],
            "version": reference["resourceVersion"],
        }
    return result


def _safe_version(value: object, name: str) -> str:
    if not isinstance(value, str) or _VERSION.fullmatch(value) is None:
        raise EvaluationUnavailable(f"stored {name} is unavailable")
    return value


def _safe_revision(value: object, name: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 128 or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:+-]*", value) is None or any(marker in value.casefold() for marker in ("owner", "identity", "credential", "bearer", "token", "secret", "password", "private", "content", "hash", "digest", "endpoint")):
        raise EvaluationUnavailable(f"stored {name} is unavailable")
    return value


def _safe_digest(value: object, name: str) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise EvaluationUnavailable(f"stored {name} is unavailable")
    return value


def _safe_value(value: object, allowed: set[str], name: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise EvaluationUnavailable(f"stored {name} is unavailable")
    return value


def _required_nonnegative_count(value: object, name: str) -> int:
    if type(value) is not int or value < 0:
        raise EvaluationUnavailable(f"stored {name} is unavailable")
    return value


def _required_ratio(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EvaluationUnavailable(f"stored {name} is unavailable")
    number = float(value)
    if not 0 <= number <= 1:
        raise EvaluationUnavailable(f"stored {name} is unavailable")
    return number


def _required_enum(value: object, allowed: set[str], name: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise EvaluationUnavailable(f"stored {name} is unavailable")
    return value


def _strict_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, item in pairs:
        if key in result:
            raise ValueError("duplicate cursor key")
        result[key] = item
    return result


def _decode_storage_cursor(
    value: str | None, *, expected_kind: str | None = None
) -> dict[str, object]:
    if value is None:
        return {
            "snapshotAt": datetime.now(UTC),
            "timestamp": None,
            "identifier": None,
            "version": None,
            "kind": expected_kind,
        }
    if not isinstance(value, str) or len(value) > 1024:
        raise EvaluationQueryError("cursor is invalid")
    if value.startswith("ev2_"):
        prefix = "ev2_"
        encoded_value = value[4:]
    elif value.startswith("ev1."):
        prefix = "ev1."
        # Legacy cursors were emitted with a dot separator.  They remain
        # readable so an in-flight client can finish a page traversal while
        # all new cursors use the public Cursor character grammar.
        encoded_value = value[4:]
    else:
        raise EvaluationQueryError("cursor is invalid")
    if not encoded_value or re.fullmatch(r"[A-Za-z0-9_-]+", encoded_value) is None:
        raise EvaluationQueryError("cursor is invalid")
    try:
        encoded = encoded_value + "=" * (-len(encoded_value) % 4)
        raw = base64.b64decode(encoded, altchars=b"-_", validate=True)
        if base64.urlsafe_b64encode(raw).decode().rstrip("=") != encoded_value:
            raise ValueError("non-canonical cursor")
        decoded = json.loads(raw.decode(), object_pairs_hook=_strict_json_object)
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError, binascii.Error) as exc:
        raise EvaluationQueryError("cursor is invalid") from exc
    if not isinstance(decoded, Mapping):
        raise EvaluationQueryError("cursor is invalid")
    if prefix == "ev2_":
        if set(decoded) != {"v", "kind", "snapshotAt", "timestamp", "identifier", "version"}:
            raise EvaluationQueryError("cursor is invalid")
        if decoded.get("v") != 2 or not isinstance(decoded.get("kind"), str):
            raise EvaluationQueryError("cursor is invalid")
        cursor_kind: str | None = decoded["kind"]
        if expected_kind is not None and cursor_kind != expected_kind:
            raise EvaluationQueryError("cursor is invalid")
    else:
        if set(decoded) != {"v", "snapshotAt", "timestamp", "identifier"} or decoded.get("v") != 2:
            raise EvaluationQueryError("cursor is invalid")
        # The legacy payload predates kind binding.  It is therefore safe to
        # resume only the explicitly supported historical audit traversal;
        # never infer a collection kind from an unbound legacy cursor.
        if expected_kind != "history":
            raise EvaluationQueryError("cursor is invalid")
        cursor_kind = "history"
    snapshot_raw = decoded.get("snapshotAt")
    timestamp_raw = decoded.get("timestamp")
    identifier = decoded.get("identifier")
    version = decoded.get("version")
    if (
        not isinstance(snapshot_raw, str)
        or not isinstance(identifier, (str, type(None)))
        or not isinstance(version, (str, type(None)))
        or not isinstance(cursor_kind, (str, type(None)))
    ):
        raise EvaluationQueryError("cursor is invalid")
    if timestamp_raw is not None and not isinstance(timestamp_raw, str):
        raise EvaluationQueryError("cursor is invalid")
    snapshot_text = _canonical_cursor_instant(snapshot_raw)
    if snapshot_text is None:
        raise EvaluationQueryError("cursor is invalid")
    snapshot = datetime.fromisoformat(snapshot_text.replace("Z", "+00:00"))
    timestamp = _canonical_cursor_instant(timestamp_raw)
    return {
        "snapshotAt": snapshot.astimezone(UTC),
        "timestamp": timestamp,
        "identifier": identifier,
        "version": version,
        "kind": cursor_kind,
    }


def _encode_storage_cursor(state: Mapping[str, object], row: Mapping[str, object], kind: str) -> str:
    timestamp_key = "_cursorCreatedAt" if kind in {"attempts", "regressions"} else {
        "deletions": "createdAt",
        "scores": "createdAt",
        "results": "createdAt",
        "history": "occurredAt",
    }.get(kind, "createdAt")
    timestamp = row.get(timestamp_key)
    if timestamp is None:
        timestamp = row.get("updatedAt")
    version = row.get("_cursorVersion") if kind == "regressions" else None
    if version is None and kind == "regressions":
        version = row.get("regressionVersion")
    snapshot_at = _canonical_cursor_instant(state.get("snapshotAt"))
    if snapshot_at is None:
        raise EvaluationQueryError("cursor is invalid")
    cursor_timestamp = _canonical_cursor_instant(timestamp)
    raw = json.dumps(
        {
            "v": 2,
            "kind": kind,
            "snapshotAt": snapshot_at,
            "timestamp": cursor_timestamp,
            "identifier": str(row.get({
                "datasets": "datasetId", "cases": "caseId", "suites": "suiteId",
                "evaluators": "evaluatorId", "jobs": "jobId", "attempts": "attemptId",
                "results": "resultId", "scores": "scoreId", "artifacts": "artifactId",
                "reviews": "reviewId", "calibrations": "calibrationId",
                "qualifications": "qualificationId", "deletions": "actionId",
                "regressions": "regressionId",
                "history": "historyId",
            }.get(kind, "id"))),
            "version": str(version) if version is not None else None,
        },
        separators=(",", ":"),
    ).encode()
    return "ev2_" + base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _keyset_clause(
    state: Mapping[str, object], *, column: str, identifier_column: str,
    version_column: str | None = None, descending: bool = True
) -> tuple[list[str], list[object]]:
    clauses: list[str] = []
    params: list[object] = []
    base_column = column.rsplit(".", 1)[-1]
    if base_column in {"created_at", "received_at", "occurred_at"}:
        clauses.append(f"{column} <= %s")
        params.append(state["snapshotAt"])
    timestamp = state.get("timestamp")
    identifier = state.get("identifier")
    if timestamp is not None and identifier is not None:
        operator = "<" if descending else ">"
        key_value: object = timestamp
        if base_column == "attempt_number":
            try:
                key_value = int(timestamp)
            except ValueError as exc:
                raise EvaluationQueryError("cursor is invalid") from exc
        if version_column is not None:
            version = state.get("version")
            if not isinstance(version, str):
                raise EvaluationQueryError("cursor is invalid")
            clauses.append(
                f"({column} {operator} %s OR ({column} = %s AND "
                f"({identifier_column} {operator} %s OR "
                f"({identifier_column} = %s AND {version_column} {operator} %s))))"
            )
            params.extend([key_value, key_value, identifier, identifier, version])
        else:
            clauses.append(
                f"({column} {operator} %s OR ({column} = %s AND "
                f"{identifier_column} {operator} %s))"
            )
            params.extend([key_value, key_value, identifier])
    return clauses, params


def _provider_boundary(method: Callable[..., Any]) -> Callable[..., Any]:
    """Translate concrete driver failures without leaking diagnostics."""

    @wraps(method)
    async def guarded(*args: Any, **kwargs: Any) -> Any:
        try:
            return await method(*args, **kwargs)
        except EvaluationQueryError:
            raise
        except (httpx.HTTPError, OSError, TimeoutError) as exc:
            raise EvaluationUnavailable("evaluation storage is unavailable") from exc
        except Exception as exc:
            # psycopg and ClickHouse clients expose different exception trees;
            # all unexpected adapter I/O failures are deliberately content-free.
            raise EvaluationUnavailable("evaluation storage is unavailable") from exc

    return guarded


class PostgresEvaluationQueryBackend:
    """Read-only adapter over the service-owned evaluation migrations.

    Queries deliberately select only closed metadata columns and JSON
    projections.  Table names and filter columns are closed constants; values
    always remain bound parameters.  The backend is intentionally separate
    from the in-memory test store so production cannot silently lose state.
    """

    _DEFINITIONS: dict[str, tuple[str, str, str, str]] = {
        "datasets": (
            "observatory_evaluation_datasets",
            "dataset_id",
            "dataset_version",
            "dataset",
        ),
        "cases": (
            "observatory_evaluation_cases",
            "case_id",
            "case_version",
            "case",
        ),
        "suites": (
            "observatory_evaluation_suites",
            "suite_id",
            "suite_version",
            "suite",
        ),
        "evaluators": (
            "observatory_evaluation_evaluators",
            "evaluator_id",
            "evaluator_version",
            "evaluator",
        ),
    }

    def __init__(
        self,
        connection_factory: Callable[[], Any],
        *,
        clickhouse_url: str | None = None,
        clickhouse_database: str = "default",
        clickhouse_user: str = "default",
        clickhouse_password: str = "",
    ) -> None:
        self._connection_factory = connection_factory
        self._clickhouse_url = clickhouse_url
        self._clickhouse_database = clickhouse_database
        self._clickhouse_auth = (clickhouse_user, clickhouse_password)

    @_provider_boundary
    async def collection(
        self,
        kind: str,
        *,
        cursor: str | None,
        limit: int,
        filters: Mapping[str, object] | None,
    ) -> dict[str, object]:
        state = _decode_storage_cursor(cursor, expected_kind=kind)
        if kind == "scores":
            return await self._scores(cursor_state=state, limit=limit, filters=filters)
        if kind in self._DEFINITIONS:
            rows = await self._definition_rows(kind, state, limit, filters)
        elif kind == "jobs":
            rows = await self._jobs(state, limit, filters)
        elif kind == "attempts":
            rows = await self._attempts(state, limit, filters)
        elif kind == "results":
            rows = await self._results(state, limit, filters)
        elif kind == "artifacts":
            rows = await self._artifacts(state, limit, filters)
        elif kind == "reviews":
            rows = await self._reviews(state, limit, filters)
        elif kind == "calibrations":
            rows = await self._calibrations(state, limit, filters)
        elif kind == "qualifications":
            rows = await self._qualifications(state, limit, filters)
        elif kind == "deletions":
            rows = await self._deletions(state, limit)
        elif kind == "regressions":
            rows = await self._regressions(state, limit, filters)
        else:
            raise EvaluationQueryError("resource kind is invalid")
        next_cursor = (
            _encode_storage_cursor(state, rows[-1], kind) if len(rows) == limit and rows else None
        )
        rows = [
            {key: value for key, value in row.items() if not key.startswith("_")}
            for row in rows
        ]
        return {
            "items": rows,
            "nextCursor": next_cursor,
            "snapshotAt": _canonical_cursor_instant(state["snapshotAt"]),
            "partial": False,
        }

    @_provider_boundary
    async def get(
        self, kind: str, identifier: str, version: str | None
    ) -> dict[str, object] | None:
        if kind in self._DEFINITIONS:
            table, id_column, version_column, _ = self._DEFINITIONS[kind]
            where = f"{id_column} = %s"
            params: list[object] = [identifier]
            if version is not None:
                where += f" AND {version_column} = %s"
                params.append(version)
            async with self._connection_factory() as connection:
                result = await connection.execute(
                    f"SELECT definition_json FROM {table} WHERE {where} LIMIT 1", tuple(params)
                )
                row = await result.fetchone()
            if row is None:
                return None
            return project_definition(_json_mapping(row[0]))
        query, detail_params = self._detail_query(kind, identifier)
        if query is None:
            raise EvaluationQueryError("resource kind is invalid")
        async with self._connection_factory() as connection:
            result = await connection.execute(query, detail_params)
            row = await result.fetchone()
        return self._row_projection(kind, row) if row is not None else None

    @_provider_boundary
    async def history_page(
        self,
        *,
        cursor: str | None,
        limit: int,
        resource_type: str | None,
        resource_id: str | None,
    ) -> dict[str, object]:
        state = _decode_storage_cursor(cursor, expected_kind="history")
        clauses: list[str] = []
        params: list[object] = []
        if resource_type is not None:
            clauses.append("resource_type = %s")
            params.append(resource_type.removeprefix("evaluation_"))
        if resource_id is not None:
            clauses.append("resource_id = %s")
            params.append(resource_id)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        cursor_clauses, cursor_params = _keyset_clause(
            state, column="occurred_at", identifier_column="history_id"
        )
        clauses.extend(cursor_clauses)
        params.extend(cursor_params)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        # Every source is an Observatory-owned, content-free lifecycle or
        # authorization audit.  Keep the union closed and project the same
        # keyset tuple so concurrent inserts cannot reorder a page.
        sql = (
            "SELECT history_id, resource_type, resource_id, resource_version, change, state, occurred_at "
            "FROM ("
            " SELECT audit_id AS history_id, resource_type, resource_id::text AS resource_id, "
            " resource_version, CASE WHEN operation = 'create' THEN 'created' ELSE 'updated' END AS change, "
            " outcome AS state, occurred_at FROM observatory_evaluation_definition_audits"
            " UNION ALL SELECT audit_id, 'job', job_id::text, NULL, "
            " CASE WHEN event_type LIKE '%cancellation%' THEN 'cancel_requested' ELSE 'state_changed' END, "
            " outcome, occurred_at FROM observatory_evaluation_lifecycle_audits"
            " UNION ALL SELECT result_id, 'result', result_id::text, NULL, 'state_changed', "
            " status, received_at FROM observatory_evaluation_result_receipts"
            " UNION ALL SELECT artifact_reference_id, 'artifact', artifact_reference_id::text, artifact_version, "
            " 'created', status, created_at FROM observatory_evaluation_artifacts"
            " UNION ALL SELECT audit_id, 'deletion', command_id::text, NULL, "
            " CASE WHEN outcome = 'completed' THEN 'deleted' ELSE 'state_changed' END, "
            " outcome, recorded_at FROM observatory_evaluation_deletion_audits"
            " UNION ALL SELECT audit_id, resource_type, resource_id::text, NULL, "
            " CASE WHEN operation = 'qualify' THEN 'qualified' WHEN operation = 'revoke' THEN 'revoked' "
            " ELSE 'state_changed' END, outcome, occurred_at FROM observatory_local_judge_audits"
            " UNION ALL SELECT history_id, resource_type, resource_id, resource_version, change, state, occurred_at "
            "FROM observatory_evaluation_experiment_history"
            ") AS evaluation_history"
            + where
            + " ORDER BY occurred_at DESC, history_id DESC LIMIT %s"
        )
        params.append(limit)
        async with self._connection_factory() as connection:
            result = await connection.execute(sql, tuple(params))
            rows = await result.fetchall()
        items = [
            {
                "historyId": str(row[0]),
                "resourceType": str(row[1]),
                "resourceId": str(row[2]),
                "resourceVersion": str(row[3]) if row[3] is not None else None,
                "change": str(row[4]),
                "state": str(row[5]),
                "occurredAt": _date(row[6]),
            }
            for row in rows
        ]
        return {
            "items": items,
            "nextCursor": _encode_storage_cursor(state, items[-1], "history")
            if len(items) == limit and items
            else None,
            "snapshotAt": _canonical_cursor_instant(state["snapshotAt"]),
            "partial": False,
        }

    async def _definition_rows(
        self, kind: str, state: Mapping[str, object], limit: int, filters: Mapping[str, object] | None
    ) -> list[dict[str, object]]:
        table, id_column, version_column, _ = self._DEFINITIONS[kind]
        clauses: list[str] = []
        params: list[object] = []
        if kind == "cases" and filters and filters.get("dataset") is not None:
            clauses.append("dataset_id = %s")
            params.append(filters["dataset"])
        if kind == "cases" and filters and filters.get("datasetVersion") is not None:
            clauses.append("dataset_version = %s")
            params.append(filters["datasetVersion"])
        if kind == "suites" and filters and filters.get("dataset") is not None:
            clauses.append("dataset_id = %s")
            params.append(filters["dataset"])
        if kind == "evaluators" and filters and filters.get("kind") is not None:
            clauses.append("kind = %s")
            params.append(filters["kind"])
        key_clauses, key_params = _keyset_clause(
            state, column="created_at", identifier_column=id_column
        )
        clauses.extend(key_clauses)
        params.extend(key_params)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        sql = (
            f"SELECT definition_json FROM {table}{where} "
            f"ORDER BY created_at DESC, {id_column} DESC, {version_column} DESC LIMIT %s"
        )
        params.append(limit)
        async with self._connection_factory() as connection:
            result = await connection.execute(sql, tuple(params))
            rows = await result.fetchall()
        return [project_definition(_json_mapping(row[0])) for row in rows]

    async def _jobs(
        self, state: Mapping[str, object], limit: int, filters: Mapping[str, object] | None
    ) -> list[dict[str, object]]:
        clauses: list[str] = []
        params: list[object] = []
        if filters and filters.get("status") is not None:
            clauses.append("state = %s")
            params.append(filters["status"])
        if filters and filters.get("suite") is not None:
            clauses.append("suite_id::text = %s")
            params.append(filters["suite"])
        if filters and filters.get("from") is not None:
            clauses.append("created_at >= %s")
            params.append(_instant_filter(filters["from"], "from"))
        if filters and filters.get("to") is not None:
            clauses.append("created_at <= %s")
            params.append(_instant_filter(filters["to"], "to"))
        key_clauses, key_params = _keyset_clause(
            state, column="created_at", identifier_column="job_id"
        )
        clauses.extend(key_clauses)
        params.extend(key_params)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        sql = (
            "SELECT job_id, suite_id, suite_version, state, capture_classification, "
            "attempt_count, deadline_at, created_at, updated_at, envelope_json, "
            "(SELECT COUNT(*) FROM observatory_evaluation_result_receipts r "
            " WHERE r.job_id = j.job_id) AS result_count, "
            "(SELECT COUNT(*) FROM observatory_evaluation_artifacts a "
            " WHERE a.job_id = j.job_id) AS artifact_count "
            "FROM observatory_evaluation_jobs j"
            + where
                + " ORDER BY created_at DESC, job_id DESC LIMIT %s"
        )
        params.append(limit)
        async with self._connection_factory() as connection:
            result = await connection.execute(sql, tuple(params))
            rows = await result.fetchall()
        projected: list[dict[str, object]] = []
        for row in rows:
            envelope = _json_mapping(row[9])
            data = _json_mapping(envelope.get("data"))
            provenance_value = data.get("provenance")
            if not isinstance(provenance_value, Mapping):
                raise EvaluationUnavailable("stored job provenance is unavailable")
            projected.append(
                {
                "schemaVersion": 1,
                "resourceType": "job",
                "jobId": str(row[0]),
                "suite": resource_ref(str(row[1]), str(row[2])),
                "status": str(row[3]),
                "captureClassification": str(row[4]),
                "attemptCount": int(row[5]),
                "resultCount": int(row[10]),
                "artifactCount": int(row[11]),
                "deadlineAt": _date(row[6]),
                "failureCode": envelope.get("failureCode"),
                "provenance": _stored_runner_execution_provenance(provenance_value),
                "createdAt": _date(row[7]),
                "updatedAt": _date(row[8]),
                }
            )
        return projected

    async def _attempts(
        self, state: Mapping[str, object], limit: int, filters: Mapping[str, object] | None
    ) -> list[dict[str, object]]:
        job_id = filters.get("jobId") if filters else None
        params: list[object] = []
        where = ""
        if job_id is not None:
            where = " WHERE job_id = %s"
            params.append(job_id)
        key_clauses, key_params = _keyset_clause(
            state, column="created_at", identifier_column="attempt_id", descending=False
        )
        where = (where + " AND " if where else " WHERE ") + " AND ".join(key_clauses)
        params.extend(key_params)
        params.append(limit)
        async with self._connection_factory() as connection:
            result = await connection.execute(
                "SELECT attempt_id, job_id, attempt_number, state, started_at, completed_at, "
                "next_attempt_at, failure_code, worker_component_id, created_at "
                "FROM observatory_evaluation_attempts"
                + where
                + " ORDER BY created_at ASC, attempt_id ASC LIMIT %s",
                tuple(params),
            )
            rows = await result.fetchall()
        projected: list[dict[str, object]] = []
        for r in rows:
            status = str(r[3])
            if r[8] is None and status != "queued":
                raise EvaluationUnavailable("stored claimed attempt worker metadata is unavailable")
            projected.append(
                {
                    "attemptId": str(r[0]),
                    "jobId": str(r[1]),
                    "attemptNumber": int(r[2]),
                    "status": status,
                    # Queued attempts have not been claimed yet.  The worker
                    # identity is absent until the claim transaction records
                    # the stable runner component.
                    "workerComponentId": str(r[8]) if r[8] is not None else None,
                    "startedAt": _date(r[4]),
                    "finishedAt": _date(r[5]),
                    "nextAttemptAt": _date(r[6]),
                    "failureCode": r[7],
                    # Kept internal for the opaque keyset cursor; it is
                    # removed before the closed transport response.
                    "_cursorCreatedAt": _date(r[9]),
                }
            )
        return projected

    async def _results(
        self, state: Mapping[str, object], limit: int, filters: Mapping[str, object] | None
    ) -> list[dict[str, object]]:
        params: list[object] = []
        where = ""
        if filters and filters.get("jobId") is not None:
            where = " WHERE job_id = %s"
            params.append(filters["jobId"])
        if filters and filters.get("status") is not None:
            where = (where + " AND " if where else " WHERE ") + "status = %s"
            params.append(filters["status"])
        key_clauses, key_params = _keyset_clause(
            state, column="received_at", identifier_column="result_id"
        )
        if key_clauses:
            where = (where + " AND " if where else " WHERE ") + " AND ".join(key_clauses)
        params.extend(key_params)
        params.append(limit)
        async with self._connection_factory() as connection:
            result = await connection.execute(
                "SELECT result_id, job_id, attempt_id, status, received_at, envelope_json "
                "FROM observatory_evaluation_result_receipts"
                + where
                + " ORDER BY received_at DESC, result_id DESC LIMIT %s",
                tuple(params),
            )
            rows = await result.fetchall()
        return [self._result_projection(r) for r in rows]

    @staticmethod
    def _result_projection(row: Any) -> dict[str, object]:
        envelope = _json_mapping(row[5])
        provenance_value = envelope.get("provenance")
        required_capture = envelope.get("captureClassification")
        required_score_count = envelope.get("scoreCount")
        required_artifact_count = envelope.get("artifactCount")
        required_partial = envelope.get("partialEvidence")
        required_reproducible = envelope.get("reproducible")
        if (
            not isinstance(required_capture, str)
            or type(required_score_count) is not int
            or type(required_artifact_count) is not int
            or type(required_partial) is not bool
            or type(required_reproducible) is not bool
            or not isinstance(provenance_value, Mapping)
        ):
            raise EvaluationUnavailable("stored result projection is incomplete")
        completed_at = envelope.get("completedAt")
        if completed_at is not None and not isinstance(completed_at, str):
            raise EvaluationUnavailable("stored result completion timestamp is invalid")
        return {
            "schemaVersion": 1,
            "resourceType": "result",
            "resultId": str(row[0]),
            "jobId": str(row[1]),
            "attemptId": str(row[2]),
            "status": str(row[3]),
            "captureClassification": required_capture,
            "scoreCount": required_score_count,
            "artifactCount": required_artifact_count,
            "partialEvidence": required_partial,
            "reproducible": required_reproducible,
            "provenance": _stored_execution_provenance(provenance_value),
            "createdAt": _date(row[4]),
            "completedAt": completed_at,
        }

    async def _artifacts(
        self, state: Mapping[str, object], limit: int, filters: Mapping[str, object] | None
    ) -> list[dict[str, object]]:
        params: list[object] = []
        clauses: list[str] = []
        for key, column in (("jobId", "artifact.job_id"), ("resultId", "artifact.result_id")):
            if filters and filters.get(key) is not None:
                clauses.append(f"{column} = %s")
                params.append(filters[key])
        if filters and filters.get("deletionStatus") is not None:
            clauses.append("artifact.status = %s")
            status = "available" if filters["deletionStatus"] == "retained" else filters["deletionStatus"]
            params.append(status)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        key_clauses, key_params = _keyset_clause(
            state, column="artifact.created_at", identifier_column="artifact.artifact_id"
        )
        clauses.extend(key_clauses)
        params.extend(key_params)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(limit)
        async with self._connection_factory() as connection:
            result = await connection.execute(
                "SELECT artifact.artifact_id, artifact.artifact_version, artifact.job_id, artifact.result_id, artifact.intended_use, artifact.media_classification, "
                "artifact.media_type, artifact.sha256, artifact.byte_count, artifact.capture_classification, artifact.authorization_record_id, artifact.status, artifact.created_at, "
                "artifact.dataset_id, artifact.suite_id, jobs.envelope_json FROM observatory_evaluation_artifacts AS artifact "
                "JOIN observatory_evaluation_jobs AS jobs ON jobs.job_id = artifact.job_id"
                + where
                + " ORDER BY artifact.created_at DESC, artifact.artifact_id DESC LIMIT %s",
                tuple(params),
            )
            rows = await result.fetchall()
        projected: list[dict[str, object]] = []
        for row in rows:
            envelope = _json_mapping(row[15])
            data = envelope.get("data")
            data_mapping = _json_mapping(data)
            provenance_value = _stored_execution_provenance(data_mapping.get("provenance"))
            status = {"available": "retained", "pending": "deletion_queued", "deleting": "deleting", "failed": "deletion_failed"}.get(str(row[11]))
            if status is None:
                raise EvaluationUnavailable("stored artifact status is unavailable")
            projected.append({
                "schemaVersion": 1,
                "resourceType": "artifact",
                "artifactId": _required_uuid(row[0], "artifactId"),
                "artifactVersion": str(row[1]),
                "jobId": _required_uuid(row[2], "jobId"),
                "resultId": _required_uuid(row[3], "resultId"),
                "purpose": str(row[4]),
                "mediaClassification": str(row[5]),
                "mediaType": str(row[6]),
                "digest": str(row[7]),
                "byteCount": int(row[8]),
                "captureClassification": str(row[9]),
                "authorizationRecordId": _required_uuid(row[10], "authorizationRecordId"),
                "retentionMode": "manual_only",
                "deletionStatus": status,
                "provenance": provenance_value,
                "createdAt": _date(row[12]),
            })
        return projected

    async def _reviews(
        self, state: Mapping[str, object], limit: int, filters: Mapping[str, object] | None
    ) -> list[dict[str, object]]:
        clauses: list[str] = []
        params: list[object] = []
        if filters and filters.get("resultId") is not None:
            clauses.append("review.result_id = %s")
            params.append(filters["resultId"])
        if filters and filters.get("calibrationId") is not None:
            clauses.append("review.calibration_id = %s")
            params.append(filters["calibrationId"])
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        key_clauses, key_params = _keyset_clause(
            state, column="review.created_at", identifier_column="review.review_id"
        )
        clauses.extend(key_clauses)
        params.extend(key_params)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(limit)
        async with self._connection_factory() as connection:
            result = await connection.execute(
                "SELECT review.review_id, review.review_version, review.calibration_id, review.calibration_version, review.case_id, review.result_id, review.score_id, review.score, "
                "review.review_outcome, review.review_label, review.disagreement, review.authorization_record_id, review.comment_artifact_id, review.created_at, "
                "review.review_provenance_component, review.review_provenance_version, review.review_provenance_digest "
                "FROM observatory_local_judge_reviews AS review"
                + where
                + " ORDER BY review.created_at DESC, review.review_id DESC LIMIT %s",
                tuple(params),
            )
            rows = await result.fetchall()
        projected: list[dict[str, object]] = []
        for row in rows:
            provenance_value = _stored_simple_provenance_columns(row[14], row[15], row[16])
            projected.append({
                "schemaVersion": 1,
                "resourceType": "human_review",
                "reviewId": _required_uuid(row[0], "reviewId"),
                "reviewVersion": str(row[1]),
                "calibration": resource_ref(str(row[2]), str(row[3])),
                "caseId": _required_uuid(row[4], "caseId"),
                "resultId": _required_uuid(row[5], "resultId"),
                "scoreId": _required_uuid(row[6], "scoreId"),
                "reviewScore": float(row[7]),
                "reviewOutcome": row[8],
                "reviewLabel": row[9],
                "disagreement": row[10],
                "authorizationRecordId": _required_uuid(row[11], "authorizationRecordId"),
                "commentArtifactId": str(row[12]) if row[12] is not None else None,
                "provenance": provenance_value,
                "createdAt": _date(row[13]),
            })
        return projected

    async def _calibrations(
        self, state: Mapping[str, object], limit: int, filters: Mapping[str, object] | None
    ) -> list[dict[str, object]]:
        where = ""
        params: list[object] = []
        if filters and filters.get("eligibility") is not None:
            where = " WHERE result.eligibility = %s"
            params.append(filters["eligibility"])
        key_clauses, key_params = _keyset_clause(
            state, column="definition.created_at", identifier_column="definition.calibration_id"
        )
        where = ((where + " AND ") if where else " WHERE ") + " AND ".join(key_clauses)
        params.extend(key_params)
        params.append(limit)
        async with self._connection_factory() as connection:
            result = await connection.execute(
                "SELECT definition.calibration_id, definition.calibration_version, definition.dataset_id, definition.dataset_version, definition.suite_id, definition.suite_version, "
                "definition.evaluator_id, definition.evaluator_version, definition.judge_id, definition.judge_version, definition.created_at, definition.definition_json, "
                "result.provenance_component, result.provenance_version, result.provenance_digest, result.sample_count, "
                "result.agreement_rate, result.mean_absolute_error, result.disagreement_rate, result.eligibility "
                "FROM observatory_local_judge_calibration_definitions AS definition "
                "LEFT JOIN observatory_local_judge_calibration_results AS result "
                "ON result.calibration_id = definition.calibration_id "
                "AND result.calibration_version = definition.calibration_version"
                + where
                + " ORDER BY definition.created_at DESC, definition.calibration_id DESC LIMIT %s",
                tuple(params),
            )
            rows = await result.fetchall()
        projected: list[dict[str, object]] = []
        for r in rows:
            if any(r[index] is None for index in range(12, 20)):
                raise EvaluationUnavailable("stored calibration result is unavailable")
            projected.append(
                {
                "calibrationId": str(r[0]),
                "calibrationVersion": str(r[1]),
                "dataset": resource_ref(str(r[2]), str(r[3])),
                "suite": resource_ref(str(r[4]), str(r[5])),
                "evaluator": {
                    "evaluatorId": str(r[6]),
                    "evaluatorVersion": str(r[7]),
                    "kind": "local_judge",
                },
                "judgeDefinition": resource_ref(str(r[8]), str(r[9])),
                "sampleCount": _required_nonnegative_count(r[15], "sampleCount"),
                "agreementRate": _required_ratio(r[16], "agreementRate"),
                "meanAbsoluteError": _required_ratio(r[17], "meanAbsoluteError"),
                "disagreementRate": _required_ratio(r[18], "disagreementRate"),
                "eligibility": _required_enum(r[19], {"eligible", "ineligible"}, "eligibility"),
                "advisory": True,
                "provenance": _stored_simple_provenance_columns(r[12], r[13], r[14]),
                "createdAt": _date(r[10]),
                }
            )
        return projected

    async def _qualifications(
        self, state: Mapping[str, object], limit: int, filters: Mapping[str, object] | None
    ) -> list[dict[str, object]]:
        where = ""
        params: list[object] = []
        if filters and filters.get("state") is not None:
            where = " WHERE state = %s"
            params.append(filters["state"])
        key_clauses, key_params = _keyset_clause(
            state, column="created_at", identifier_column="qualification_id"
        )
        where = ((where + " AND ") if where else " WHERE ") + " AND ".join(key_clauses)
        params.extend(key_params)
        params.append(limit)
        async with self._connection_factory() as connection:
            result = await connection.execute(
                "SELECT qualification_id, qualification_version, state, dataset_id, dataset_version, suite_id, suite_version, "
                "evaluator_id, evaluator_version, judge_id, judge_version, calibration_id, calibration_version, calibration_result_id, "
                "authorization_record_id, score_disposition, created_at, provenance_component, provenance_version, provenance_digest "
                "FROM observatory_local_judge_qualifications "
                + where
                + " ORDER BY created_at DESC, qualification_id DESC LIMIT %s",
                tuple(params),
            )
            rows = await result.fetchall()
        projected: list[dict[str, object]] = []
        for r in rows:
            projected.append(
                {
                "schemaVersion": 1,
                "resourceType": "judge_qualification",
                "qualificationId": str(r[0]),
                "qualificationVersion": str(r[1]),
                "state": str(r[2]),
                "dataset": resource_ref(str(r[3]), str(r[4])),
                "suite": resource_ref(str(r[5]), str(r[6])),
                "evaluator": {
                    "evaluatorId": str(r[7]),
                    "evaluatorVersion": str(r[8]),
                    "kind": "local_judge",
                },
                "judgeDefinition": resource_ref(str(r[9]), str(r[10])),
                "calibration": resource_ref(str(r[11]), str(r[12])),
                "calibrationResultId": _required_uuid(r[13], "calibrationResultId"),
                "authorizationRecordId": _required_uuid(r[14], "authorizationRecordId"),
                "scoreDisposition": str(r[15]),
                "provenance": _stored_simple_provenance_columns(r[17], r[18], r[19]),
                "createdAt": _date(r[16]),
                }
            )
        return projected

    async def _deletions(self, state: Mapping[str, object], limit: int) -> list[dict[str, object]]:
        key_clauses, key_params = _keyset_clause(
            state, column="requested_at", identifier_column="command_id"
        )
        where = ["requested_at <= %s", *key_clauses]
        async with self._connection_factory() as connection:
            result = await connection.execute(
                "SELECT command_id, selector_kind, selector_value, status, deleted_count, failure_code, requested_at, started_at, completed_at "
                "FROM observatory_evaluation_deletion_commands WHERE "
                + " AND ".join(where)
                + " ORDER BY requested_at DESC, command_id DESC LIMIT %s",
                (state["snapshotAt"], *key_params, limit),
            )
            rows = await result.fetchall()
        return [
            {
                "actionId": str(r[0]),
                "scope": str(r[1]),
                "resourceId": str(r[2])
                if r[2] and str(r[1]) in {"job", "dataset", "suite"}
                else None,
                "resourceVersion": None,
                "cutoff": str(r[2]) if r[2] and str(r[1]) == "cutoff" else None,
                "status": "succeeded" if str(r[3]) == "completed" else str(r[3]),
                "deletedArtifactCount": int(r[4]),
                "deletedMetadataCount": 0,
                "failureCode": r[5],
                "createdAt": _date(r[6]),
                "startedAt": _date(r[7]),
                "finishedAt": _date(r[8]),
            }
            for r in rows
        ]

    async def _regressions(
        self, state: Mapping[str, object], limit: int, filters: Mapping[str, object] | None
    ) -> list[dict[str, object]]:
        """Read regression decisions through the service-owned evaluation boundary.

        Regression searches are intentionally narrowed before pagination.  In
        particular, an experiment reference is matched only against the
        regression's experiment identity; a baseline identity can therefore
        never be treated as an experiment reference by this provider.
        """

        if not 1 <= limit <= 200:
            raise EvaluationQueryError("page size is invalid")
        clauses: list[str] = []
        params: list[object] = []
        if filters is None:
            filters = {}

        from_value = filters.get("from")
        to_value = filters.get("to")
        if from_value is None or to_value is None:
            raise EvaluationQueryError("regression search window is required")
        from_instant = _instant_filter(from_value, "from")
        to_instant = _instant_filter(to_value, "to")
        if from_instant > to_instant:
            raise EvaluationQueryError("regression search window is invalid")
        clauses.extend(["regression.created_at >= %s", "regression.created_at <= %s"])
        params.extend([from_instant, to_instant])

        decision = filters.get("decision")
        outcomes = filters.get("outcomes")
        if decision is not None:
            if not isinstance(decision, str) or decision not in {
                "regressed", "unchanged", "inconclusive", "unavailable", "incompatible"
            }:
                raise EvaluationQueryError("regression outcome is invalid")
            clauses.append("regression.decision = %s")
            params.append(decision)
        elif outcomes is not None:
            if not isinstance(outcomes, list) or not 1 <= len(outcomes) <= 6:
                raise EvaluationQueryError("regression outcomes are invalid")
            if (
                any(
                    not isinstance(item, str)
                    or item not in {
                        "regressed", "unchanged", "inconclusive", "unavailable", "incompatible"
                    }
                    for item in outcomes
                )
                or len(set(outcomes)) != len(outcomes)
            ):
                raise EvaluationQueryError("regression outcomes are invalid")
            clauses.append("regression.decision = ANY(%s::text[])")
            params.append(outcomes)

        references = filters.get("experimentReferences")
        if references is not None:
            if not isinstance(references, list) or len(references) > 32:
                raise EvaluationQueryError("experiment references are invalid")
            reference_terms: list[str] = []
            for reference in references:
                if not isinstance(reference, Mapping) or set(reference) != {
                    "resourceId", "resourceVersion"
                }:
                    raise EvaluationQueryError("experiment references are invalid")
                resource_id = reference.get("resourceId")
                resource_version = reference.get("resourceVersion")
                if not isinstance(resource_id, str) or not isinstance(resource_version, str):
                    raise EvaluationQueryError("experiment references are invalid")
                _required_uuid(resource_id, "experiment reference id")
                if _VERSION.fullmatch(resource_version) is None:
                    raise EvaluationQueryError("experiment references are invalid")
                # The predicates are deliberately on experiment_id/version,
                # never baseline_id/version.  They are also grouped as exact
                # pairs so a version cannot cross-match another experiment.
                reference_terms.append(
                    "(regression.experiment_id = %s AND regression.experiment_version = %s)"
                )
                params.extend([resource_id, resource_version])
            if reference_terms:
                clauses.append("(" + " OR ".join(reference_terms) + ")")

        key_clauses, key_params = _keyset_clause(
            state,
            column="regression.created_at",
            identifier_column="regression.regression_id",
            version_column="regression.regression_version",
        )
        clauses.extend(key_clauses)
        params.extend(key_params)
        params.append(limit)
        where = " WHERE " + " AND ".join(clauses)
        sql = (
            "SELECT regression.resource_json, regression.regression_id::text, "
            "regression.regression_version, regression.created_at "
            "FROM observatory_evaluation_regressions AS regression"
            + where
            + " ORDER BY regression.created_at DESC, regression.regression_id DESC, "
            "regression.regression_version DESC LIMIT %s"
        )
        async with self._connection_factory() as connection:
            result = await connection.execute(sql, tuple(params))
            rows = await result.fetchall()
        projected: list[dict[str, object]] = []
        for row in rows:
            if len(row) < 4:
                raise EvaluationUnavailable("stored regression projection is unavailable")
            value = _json_mapping(row[0])
            if not value:
                raise EvaluationUnavailable("stored regression projection is unavailable")
            value["_cursorCreatedAt"] = _date(row[3])
            value["_cursorVersion"] = str(row[2])
            projected.append(value)
        return projected

    async def _scores(
        self, *, cursor_state: Mapping[str, object], limit: int, filters: Mapping[str, object] | None
    ) -> dict[str, object]:
        if not self._clickhouse_url:
            raise EvaluationUnavailable("evaluation score storage unavailable")
        clauses: list[str] = []
        params: dict[str, str] = {}
        if filters:
            for key, column in (("resultId", "result_id"), ("evaluatorId", "evaluator_id")):
                value = filters.get(key)
                if value is not None:
                    clauses.append(f"{column} = {{p_{key}:UUID}}")
                    params[f"p_{key}"] = str(value)
        clauses.append("observed_at <= {cursor_snapshot:DateTime64(6)}")
        params["cursor_snapshot"] = _clickhouse_timestamp(cursor_state["snapshotAt"])
        timestamp = cursor_state.get("timestamp")
        identifier = cursor_state.get("identifier")
        if timestamp is not None and identifier is not None:
            clauses.append(
                "(observed_at < {cursor_timestamp:DateTime64(6)} OR "
                "(observed_at = {cursor_timestamp:DateTime64(6)} AND score_id < {cursor_id:UUID}))"
            )
            cursor_text = _canonical_cursor_instant(timestamp)
            if cursor_text is None:
                raise EvaluationQueryError("cursor is invalid")
            cursor_time = datetime.fromisoformat(cursor_text.replace("Z", "+00:00"))
            params["cursor_timestamp"] = _clickhouse_timestamp(cursor_time)
            params["cursor_id"] = str(identifier)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        query = (
            "SELECT score_id, result_id, case_id, evaluator_id, evaluator_version, evaluator_kind, component_id, component_version, code_revision, image_digest, configuration_revision, provenance_component, provenance_version, provenance_digest, execution_provenance_json, metric, score_value, outcome, evidence_status, uncertainty, score_disposition, observed_at, reproducibility FROM evaluation_scores"
            + where
            + f" ORDER BY observed_at DESC, score_id DESC LIMIT {limit} FORMAT JSON"
        )
        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as client:
            response = await client.post(
                self._clickhouse_url,
                params={
                    "database": self._clickhouse_database,
                    **{f"param_{k}": v for k, v in params.items()},
                },
                auth=self._clickhouse_auth,
                content=query,
            )
            response.raise_for_status()
            payload = response.json()
        raw_rows = payload.get("data", []) if isinstance(payload, Mapping) else []
        items: list[dict[str, object]] = []
        for raw in raw_rows:
            if not isinstance(raw, Mapping):
                continue
            component_values = {
                "componentId": raw.get("component_id"),
                "componentVersion": raw.get("component_version"),
                "codeRevision": raw.get("code_revision"),
                "imageDigest": raw.get("image_digest"),
                "configurationRevision": raw.get("configuration_revision"),
            }
            if any(value is None for value in component_values.values()):
                raise EvaluationUnavailable("stored score component provenance is unavailable")
            provenance_json = raw.get("execution_provenance_json")
            if not isinstance(provenance_json, str):
                raise EvaluationUnavailable("stored score execution provenance is unavailable")
            try:
                provenance_value = json.loads(provenance_json)
            except json.JSONDecodeError as exc:
                raise EvaluationUnavailable("stored score execution provenance is invalid") from exc
            if not isinstance(provenance_value, Mapping):
                raise EvaluationUnavailable("stored score execution provenance is unavailable")
            if len(provenance_json) > 16_384 or set(provenance_value) - {
                "dataset",
                "suite",
                "evaluators",
                "components",
                "model",
                "deploymentRevision",
                "reproducibility",
            } or any(
                marker in provenance_json.casefold()
                for marker in ("response", "content", "credential", "endpoint", "token", "owner")
            ):
                raise EvaluationUnavailable("stored score execution provenance is invalid")
            uncertainty_value = raw.get("uncertainty")
            uncertainty = (
                {"method": "none", "lower": 0, "upper": 0}
                if uncertainty_value is None
                else {"method": "calibrated_interval", "lower": uncertainty_value, "upper": uncertainty_value}
            )
            items.append({
                "schemaVersion": 1,
                "resourceType": "score",
                "scoreId": _required_uuid(raw.get("score_id"), "scoreId"),
                "resultId": _required_uuid(raw.get("result_id"), "resultId"),
                "caseId": _required_uuid(raw.get("case_id"), "caseId"),
                "evaluator": {
                    "evaluatorId": _required_uuid(raw.get("evaluator_id"), "evaluatorId"),
                    "evaluatorVersion": str(raw.get("evaluator_version")),
                    "kind": str(raw.get("evaluator_kind")),
                },
                "component": component_values,
                "metric": str(raw.get("metric")),
                "value": raw.get("score_value"),
                "outcome": raw.get("outcome"),
                "evidenceStatus": str(raw.get("evidence_status")),
                "uncertainty": uncertainty,
                "scoreDisposition": str(raw.get("score_disposition")),
                "provenance": _stored_runner_execution_provenance(provenance_value),
                "createdAt": _canonical_clickhouse_score_instant(raw.get("observed_at")),
            })
        return page(items, cursor=None, limit=max(1, len(items))) | {
            "nextCursor": _encode_storage_cursor(cursor_state, items[-1], "scores")
            if len(items) == limit and items
            else None,
            "snapshotAt": _canonical_cursor_instant(cursor_state["snapshotAt"]),
            "partial": False,
        }

    def _detail_query(self, kind: str, identifier: str) -> tuple[str | None, tuple[object, ...]]:
        specs = {
            "jobs": (
                "SELECT j.job_id, j.suite_id, j.suite_version, j.state, j.capture_classification, j.attempt_count, j.deadline_at, j.created_at, j.updated_at, j.envelope_json, "
                "(SELECT COUNT(*) FROM observatory_evaluation_result_receipts r WHERE r.job_id=j.job_id), "
                "(SELECT COUNT(*) FROM observatory_evaluation_artifacts a WHERE a.job_id=j.job_id) "
                "FROM observatory_evaluation_jobs AS j WHERE j.job_id=%s",
                (identifier,),
            ),
            "results": (
                "SELECT result_id, job_id, attempt_id, status, received_at, envelope_json FROM observatory_evaluation_result_receipts WHERE result_id=%s",
                (identifier,),
            ),
            "artifacts": (
                "SELECT artifact.artifact_id, artifact.artifact_version, artifact.job_id, artifact.result_id, artifact.intended_use, artifact.media_classification, artifact.media_type, artifact.sha256, artifact.byte_count, artifact.capture_classification, artifact.authorization_record_id, artifact.status, artifact.created_at, artifact.dataset_id, artifact.suite_id, jobs.envelope_json "
                "FROM observatory_evaluation_artifacts AS artifact JOIN observatory_evaluation_jobs AS jobs ON jobs.job_id=artifact.job_id WHERE artifact.artifact_id=%s",
                (identifier,),
            ),
            "deletions": (
                "SELECT command_id, selector_kind, selector_value, status, deleted_count, failure_code, requested_at, started_at, completed_at FROM observatory_evaluation_deletion_commands WHERE command_id=%s",
                (identifier,),
            ),
        }
        return specs.get(kind, (None, ()))

    def _row_projection(self, kind: str, row: Any) -> dict[str, object]:
        if kind == "results":
            return self._result_projection(row)
        if kind == "jobs":
            envelope = _json_mapping(row[9])
            data = _json_mapping(envelope.get("data"))
            provenance_value = data.get("provenance")
            return {
                "schemaVersion": 1,
                "resourceType": "job",
                "jobId": _required_uuid(row[0], "jobId"),
                "suite": resource_ref(_required_uuid(row[1], "suiteId"), _safe_version(row[2], "suiteVersion")),
                "status": str(row[3]),
                "captureClassification": str(row[4]),
                "attemptCount": _required_nonnegative_count(row[5], "attemptCount"),
                "resultCount": _required_nonnegative_count(row[10], "resultCount"),
                "artifactCount": _required_nonnegative_count(row[11], "artifactCount"),
                "deadlineAt": _date(row[6]),
                "failureCode": envelope.get("failureCode"),
                "provenance": _stored_runner_execution_provenance(provenance_value),
                "createdAt": _date(row[7]),
                "updatedAt": _date(row[8]),
            }
        if kind == "artifacts":
            envelope = _json_mapping(row[15])
            data_mapping = _json_mapping(envelope.get("data"))
            status = {"available": "retained", "pending": "deletion_queued", "deleting": "deleting", "failed": "deletion_failed"}.get(str(row[11]))
            if status is None:
                raise EvaluationUnavailable("stored artifact status is unavailable")
            return {
                "schemaVersion": 1,
                "resourceType": "artifact",
                "artifactId": _required_uuid(row[0], "artifactId"),
                "artifactVersion": _safe_version(row[1], "artifactVersion"),
                "jobId": _required_uuid(row[2], "jobId"),
                "resultId": _required_uuid(row[3], "resultId"),
                "purpose": str(row[4]),
                "mediaClassification": str(row[5]),
                "mediaType": str(row[6]),
                "digest": _safe_digest(row[7], "digest"),
                "byteCount": _required_nonnegative_count(row[8], "byteCount"),
                "captureClassification": str(row[9]),
                "authorizationRecordId": _required_uuid(row[10], "authorizationRecordId"),
                "retentionMode": "manual_only",
                "deletionStatus": status,
                "provenance": _stored_execution_provenance(data_mapping.get("provenance")),
                "createdAt": _date(row[12]),
            }
        return self._deletion_projection(row)

    @staticmethod
    def _deletion_projection(row: Any) -> dict[str, object]:
        kind = str(row[1])
        return {
            "actionId": str(row[0]),
            "scope": kind,
            "resourceId": str(row[2]) if row[2] and kind in {"job", "dataset", "suite"} else None,
            "resourceVersion": None,
            "cutoff": str(row[2]) if row[2] and kind == "cutoff" else None,
            "status": "succeeded" if str(row[3]) == "completed" else str(row[3]),
            "deletedArtifactCount": int(row[4]),
            "deletedMetadataCount": 0,
            "failureCode": row[5],
            "createdAt": _date(row[6]),
            "startedAt": _date(row[7]),
            "finishedAt": _date(row[8]),
        }

__all__ = ["PostgresEvaluationQueryBackend"]
