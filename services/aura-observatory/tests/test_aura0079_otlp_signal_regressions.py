"""Regression coverage for the real Core -> Collector -> Observatory wire.

These tests deliberately send the JSON form of the standard OTLP signal
requests produced by Core.  The old ``{"observations": [...]}`` envelope is
not a Collector protocol and must not be the compatibility contract here.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from copy import deepcopy
from gzip import compress
from typing import Any

import httpx
import pytest
from aura_observatory.adapters.memory import MemoryCatalog, MemoryObservationStore
from aura_observatory.entrypoints.api.app import create_app
from aura_observatory.ingest import TelemetryIngestService
from aura_observatory.settings import Settings

TOKEN = "collector-test-secret"
OBSERVATION_ID = "01234567-89ab-4cde-8123-0123456789ab"
RUN_ID = "fedcba98-7654-4fed-8123-fedcba987654"
TRACE_ID = "0123456789abcdef0123456789abcdef"
SPAN_ID = "0123456789abcdef"
COMPACT_RUN_ID = RUN_ID.replace("-", "")
COMPACT_CONVERSATION_ID = "0123456789ab4cde81230123456789ab"


def _string_attribute(key: str, value: str) -> dict[str, Any]:
    return {"key": key, "value": {"stringValue": value}}


def _resource() -> dict[str, Any]:
    return {
        "attributes": [
            _string_attribute("service.name", "aura-core"),
            _string_attribute("service.role", "worker"),
            _string_attribute("deployment.id", "deploy-test"),
            _string_attribute("deployment.environment", "test"),
            _string_attribute("deployment.git_commit", "0123456789abcdef01234567"),
            _string_attribute("host.id", "host-test"),
            _string_attribute("capture.policy", "metadata_only"),
        ]
    }


def _metric_payload(observation_id: str = OBSERVATION_ID) -> dict[str, Any]:
    return {
        "resourceMetrics": [
            {
                "resource": _resource(),
                "scopeMetrics": [
                    {
                        "scope": {"name": "aura.core.telemetry", "version": "1.0.0"},
                        "metrics": [
                            {
                                "name": "aura.metadata.measurement",
                                "gauge": {
                                    "dataPoints": [
                                        {
                                            "attributes": [
                                                _string_attribute(
                                                    "aura.observation_id", observation_id
                                                ),
                                                _string_attribute(
                                                    "aura.component_id",
                                                    "aura.runtime.model_routing",
                                                ),
                                                _string_attribute(
                                                    "aura.component_version", "1.2.0"
                                                ),
                                                _string_attribute("aura.operation", "model_routed"),
                                                _string_attribute(
                                                    "aura.metric_name", "model_routed"
                                                ),
                                                _string_attribute(
                                                    "aura.capture_policy", "metadata_only"
                                                ),
                                                _string_attribute("aura.dimension.outcome", "ok"),
                                                _string_attribute("aura.run_id", RUN_ID),
                                            ],
                                            "timeUnixNano": "1735689600123456789",
                                            "asDouble": 12.5,
                                        }
                                    ]
                                },
                            }
                        ],
                    }
                ],
            }
        ]
    }


def _metric_attributes(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return payload["resourceMetrics"][0]["scopeMetrics"][0]["metrics"][0]["gauge"][
        "dataPoints"
    ][0]["attributes"]


def _span_payload(
    *, observation_id: str = OBSERVATION_ID, with_sensitive_fields: bool = False
) -> dict[str, Any]:
    span: dict[str, Any] = {
        "traceId": TRACE_ID,
        "spanId": SPAN_ID,
        "parentSpanId": "fedcba9876543210",
        "name": "model_routed",
        "startTimeUnixNano": "1735689600000000000",
        "endTimeUnixNano": "1735689600123456789",
        "attributes": [
            _string_attribute("aura.observation_id", observation_id),
            _string_attribute("aura.component_id", "aura.runtime.model_routing"),
            _string_attribute("aura.component_version", "1.2.0"),
            _string_attribute("aura.operation", "model_routed"),
            _string_attribute("aura.metric_name", "model_routing_duration_ms"),
            _string_attribute("aura.capture_policy", "metadata_only"),
            _string_attribute("aura.dimension.outcome", "ok"),
            _string_attribute("aura.run_id", RUN_ID),
        ],
        "status": {"code": "STATUS_CODE_OK"},
    }
    if with_sensitive_fields:
        span["attributes"].extend(
            [
                _string_attribute("prompt", "do not persist this"),
                _string_attribute("authorization", "Bearer private"),
                _string_attribute("endpoint", "https://provider.invalid/private"),
                _string_attribute("owner_subject", "owner-private"),
            ]
        )
        span["events"] = [
            {
                "name": "provider.request",
                "timeUnixNano": "1735689600010000000",
                "attributes": [_string_attribute("response", "secret response")],
            }
        ]
        span["links"] = [
            {
                "traceId": "abcdef0123456789abcdef0123456789",
                "spanId": "abcdef0123456789",
                "attributes": [_string_attribute("tool.result", "private result")],
            }
        ]
    return {
        "resourceSpans": [
            {
                "resource": _resource(),
                "scopeSpans": [
                    {
                        "scope": {"name": "aura.core.telemetry", "version": "1.0.0"},
                        "spans": [span],
                    }
                ],
            }
        ]
    }


async def _core_standard_payload(kind: str) -> tuple[str, dict[str, Any]]:
    """Return a frozen standard OTLP fixture without importing Core.

    Observatory's pytest invocation must be independently runnable.  These
    payloads are the wire contract accepted from Core's exporter, so this
    regression remains useful even when the Core package is not installed.
    """

    if kind == "metric":
        return "http://observatory-collector:4318/v1/metrics", _metric_payload()
    if kind == "span":
        return "http://observatory-collector:4318/v1/traces", _span_payload()
    raise AssertionError(f"unsupported frozen OTLP fixture: {kind}")


def _app(settings: Settings | None = None) -> tuple[Any, MemoryObservationStore]:
    store = MemoryObservationStore()
    service = TelemetryIngestService(store, MemoryCatalog(), credential=TOKEN)
    return create_app(settings or Settings(), ingest_service=service, testing=True), store


async def _post(app: Any, path: str, payload: dict[str, Any]) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://collector") as client:
        return await client.post(
            path,
            json=payload,
            headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
        )


async def _post_bytes(
    app: Any, path: str, body: bytes, *, content_encoding: str | None = None
) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    headers = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
    if content_encoding is not None:
        headers["Content-Encoding"] = content_encoding
    async with httpx.AsyncClient(transport=transport, base_url="http://collector") as client:
        return await client.post(path, content=body, headers=headers)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "path"),
    [("metric", "/internal/v1/telemetry/metrics"), ("span", "/internal/v1/telemetry/traces")],
)
async def test_core_standard_otlp_gzip_signals_reach_observatory(
    kind: str, path: str
) -> None:
    app, store = _app()
    payload = _metric_payload() if kind == "metric" else _span_payload()
    response = await _post_bytes(
        app,
        path,
        compress(json.dumps(payload, separators=(",", ":")).encode("utf-8")),
        content_encoding="gzip",
    )

    assert response.status_code == 202, response.text
    assert response.json()["accepted"] == 1
    assert list(store.observations) == [OBSERVATION_ID]


@pytest.mark.asyncio
async def test_otlp_gzip_enforces_compressed_and_decompressed_limits() -> None:
    settings = Settings(ingest_max_payload_bytes=1024)
    compressed_app, _compressed_store = _app(settings)
    compressed_oversized = compress(bytes(range(256)) * 8)
    compressed_response = await _post_bytes(
        compressed_app,
        "/internal/v1/telemetry/metrics",
        compressed_oversized,
        content_encoding="gzip",
    )
    assert compressed_response.status_code == 413

    decompressed_app, _decompressed_store = _app(settings)
    decompressed_oversized = compress(b"a" * 2048)
    decompressed_response = await _post_bytes(
        decompressed_app,
        "/internal/v1/telemetry/metrics",
        decompressed_oversized,
        content_encoding="gzip",
    )
    assert decompressed_response.status_code == 413


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body_and_encoding",
    [
        (b"not-gzip", "gzip"),
        (compress(json.dumps(_metric_payload()).encode("utf-8"))[:-1], "gzip"),
        (compress(json.dumps(_metric_payload()).encode("utf-8")) + b"trailing", "gzip"),
        (
            compress(json.dumps(_metric_payload()).encode("utf-8"))
            + compress(json.dumps(_metric_payload()).encode("utf-8")),
            "gzip",
        ),
        (json.dumps(_metric_payload()).encode("utf-8"), "br"),
        (json.dumps(_metric_payload()).encode("utf-8"), "gzip, identity"),
    ],
)
async def test_otlp_gzip_boundary_rejects_malformed_or_unsupported_encoding(
    body_and_encoding: tuple[bytes, str],
) -> None:
    app, store = _app()
    body, encoding = body_and_encoding
    response = await _post_bytes(
        app,
        "/internal/v1/telemetry/metrics",
        body,
        content_encoding=encoding,
    )

    assert response.status_code == 400
    assert store.observations == {}


@pytest.mark.asyncio
async def test_otlp_mixed_batch_accepts_valid_and_rejects_contract_invalid_records() -> None:
    app, store = _app()
    payload = _metric_payload()
    points = payload["resourceMetrics"][0]["scopeMetrics"][0]["metrics"][0]["gauge"][
        "dataPoints"
    ]
    invalid = deepcopy(points[0])
    for attribute in invalid["attributes"]:
        if attribute["key"] == "aura.component_id":
            attribute["value"]["stringValue"] = "not-an-aura-component"
    points.append(invalid)

    response = await _post(
        app,
        "/internal/v1/telemetry/metrics",
        payload,
    )

    assert response.status_code == 202
    assert response.json()["accepted"] == 1
    assert response.json()["rejected"] == 1
    assert list(store.observations) == [OBSERVATION_ID]


@pytest.mark.asyncio
async def test_core_standard_otlp_metric_reaches_observatory_signal_endpoint() -> None:
    app, store = _app()
    endpoint, payload = await _core_standard_payload("metric")

    assert endpoint.endswith("/v1/metrics")
    response = await _post(app, "/internal/v1/telemetry/metrics", payload)

    assert response.status_code == 202, response.text
    assert response.json()["accepted"] == 1
    observation = store.observations[OBSERVATION_ID]
    assert observation.kind == "metric"
    assert observation.component.id == "aura.runtime.model_routing"
    assert observation.measurement.value == 12.5
    assert observation.capture_policy == "metadata_only"
    assert observation.correlation is not None
    assert observation.correlation.run_id == RUN_ID


@pytest.mark.asyncio
async def test_compact_correlation_uuids_are_canonicalized_before_storage() -> None:
    app, store = _app()
    payload = _metric_payload()
    attributes = _metric_attributes(payload)
    attributes.append(_string_attribute("aura.conversation_id", COMPACT_CONVERSATION_ID))
    for attribute in attributes:
        if attribute["key"] == "aura.run_id":
            attribute["value"]["stringValue"] = COMPACT_RUN_ID

    response = await _post(app, "/internal/v1/telemetry/metrics", payload)

    assert response.status_code == 202, response.text
    observation = store.observations[OBSERVATION_ID]
    assert observation.correlation is not None
    assert observation.correlation.run_id == RUN_ID
    assert observation.correlation.conversation_id == "01234567-89ab-4cde-8123-0123456789ab"


@pytest.mark.asyncio
@pytest.mark.parametrize("correlation_key", ["aura.run_id", "aura.conversation_id"])
async def test_invalid_correlation_uuid_is_rejected_before_storage(correlation_key: str) -> None:
    app, store = _app()
    payload = _metric_payload()
    attributes = _metric_attributes(payload)
    if correlation_key == "aura.run_id":
        for attribute in attributes:
            if attribute["key"] == correlation_key:
                attribute["value"]["stringValue"] = "not-a-uuid"
    else:
        attributes.append(_string_attribute(correlation_key, "not-a-uuid"))

    response = await _post(app, "/internal/v1/telemetry/metrics", payload)

    assert response.status_code == 400
    assert store.observations == {}


@pytest.mark.asyncio
async def test_core_standard_otlp_span_preserves_lineage_without_events_or_links() -> None:
    app, store = _app()
    endpoint, payload = await _core_standard_payload("span")

    assert endpoint.endswith("/v1/traces")
    response = await _post(app, "/internal/v1/telemetry/traces", payload)

    assert response.status_code == 202, response.text
    observation = store.observations[OBSERVATION_ID]
    assert observation.kind == "span"
    assert observation.trace is not None
    assert observation.trace.trace_id == TRACE_ID
    assert observation.trace.span_id == SPAN_ID
    assert observation.trace.parent_span_id == "fedcba9876543210"
    assert observation.trace.linked_span_ids is None


@pytest.mark.asyncio
async def test_collector_normalized_trace_defaults_are_accepted() -> None:
    app, store = _app()
    payload = _span_payload()
    resource_span = payload["resourceSpans"][0]
    resource_span["resource"]["droppedAttributesCount"] = 0
    resource_span["scopeSpans"][0]["scope"].update(
        {"name": "", "version": "", "attributes": [], "droppedAttributesCount": 0}
    )
    span = resource_span["scopeSpans"][0]["spans"][0]
    span.update(
        {
            "parentSpanId": "",
            "droppedAttributesCount": 0,
            "droppedEventsCount": 0,
            "droppedLinksCount": 0,
            "events": [],
            "status": {"code": 1, "message": ""},
            "links": [
                {
                    "traceId": TRACE_ID,
                    "spanId": "fedcba9876543210",
                    "attributes": [],
                    "traceState": "",
                    "flags": 0,
                    "droppedAttributesCount": 0,
                }
            ],
        }
    )

    response = await _post(app, "/internal/v1/telemetry/traces", payload)

    assert response.status_code == 202, response.text
    assert response.json()["accepted"] == 1
    trace = store.observations[OBSERVATION_ID].trace
    assert trace is not None
    assert trace.parent_span_id is None
    assert trace.linked_span_ids == [
        "fedcba9876543210"
    ]


def _drop_span_attributes(span: dict[str, Any]) -> None:
    span.update(droppedAttributesCount=1)


def _drop_span_events(span: dict[str, Any]) -> None:
    span.update(droppedEventsCount=1)


def _drop_span_links(span: dict[str, Any]) -> None:
    span.update(droppedLinksCount=1)


def _add_link_flags(span: dict[str, Any]) -> None:
    span["links"] = [
        {
            "traceId": TRACE_ID,
            "spanId": "fedcba9876543210",
            "attributes": [],
            "flags": 1,
        }
    ]


def _add_unsupported_span_field(span: dict[str, Any]) -> None:
    span["privateField"] = "not logged"


def _drop_link_attributes(span: dict[str, Any]) -> None:
    span["links"] = [
        {
            "traceId": TRACE_ID,
            "spanId": "fedcba9876543210",
            "attributes": [],
            "droppedAttributesCount": 1,
        }
    ]


def _add_span_event(span: dict[str, Any]) -> None:
    span.update(events=[{"name": "private-event"}])


def _add_status_message(span: dict[str, Any]) -> None:
    span.update(status={"code": "STATUS_CODE_ERROR", "message": "private"})


def _add_invalid_numeric_status(span: dict[str, Any]) -> None:
    span.update(status={"code": 3, "message": ""})


def _add_status_shape_error(span: dict[str, Any]) -> None:
    span.update(status=None)


def _empty_trace_id(span: dict[str, Any]) -> None:
    span["traceId"] = ""


def _empty_span_id(span: dict[str, Any]) -> None:
    span["spanId"] = ""


def _empty_attribute_key(span: dict[str, Any]) -> None:
    span["attributes"][0]["key"] = ""


def _empty_attribute_value(span: dict[str, Any]) -> None:
    span["attributes"][0]["value"]["stringValue"] = ""


def _empty_link_trace_id(span: dict[str, Any]) -> None:
    span["links"] = [
        {"traceId": "", "spanId": SPAN_ID, "attributes": []}
    ]


def _empty_link_span_id(span: dict[str, Any]) -> None:
    span["links"] = [
        {"traceId": TRACE_ID, "spanId": "", "attributes": []}
    ]


@pytest.mark.parametrize(
    ("change", "expected_class"),
    [
        (_drop_span_attributes, "dropped_nonzero"),
        (_drop_span_events, "dropped_nonzero"),
        (_drop_span_links, "dropped_nonzero"),
        (_add_link_flags, "link"),
        (_add_unsupported_span_field, "unsupported_fields"),
        (_drop_link_attributes, "dropped_nonzero"),
        (_add_span_event, "event"),
        (_add_status_message, "status_message"),
        (_add_invalid_numeric_status, "status_code"),
        (_add_status_shape_error, "shape_object"),
        (_empty_trace_id, "shape_trace_id"),
        (_empty_span_id, "shape_span_id"),
        (_empty_attribute_key, "shape_attribute_key"),
        (_empty_attribute_value, "shape_attribute_value"),
        (_empty_link_trace_id, "shape_link_trace_id"),
        (_empty_link_span_id, "shape_link_span_id"),
    ],
)
@pytest.mark.asyncio
async def test_collector_trace_defaults_reject_non_default_content(
    change: Callable[[dict[str, Any]], None],
    expected_class: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    app, store = _app()
    payload = _span_payload()
    span = payload["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    change(span)

    with caplog.at_level(logging.WARNING, logger="aura_observatory.entrypoints.api.app"):
        response = await _post(app, "/internal/v1/telemetry/traces", payload)

    assert response.status_code == 400
    assert store.observations == {}
    assert f"class={expected_class}" in caplog.text


@pytest.mark.asyncio
async def test_collector_scope_rejection_diagnostic_is_finite(
    caplog: pytest.LogCaptureFixture,
) -> None:
    app, store = _app()
    payload = _span_payload()
    payload["resourceSpans"][0]["scopeSpans"][0]["scope"]["attributes"] = [
        _string_attribute("private.scope", "not logged")
    ]

    with caplog.at_level(logging.WARNING, logger="aura_observatory.entrypoints.api.app"):
        response = await _post(app, "/internal/v1/telemetry/traces", payload)

    assert response.status_code == 400
    assert store.observations == {}
    assert "class=scope" in caplog.text
    assert "private.scope" not in caplog.text
    assert "not logged" not in caplog.text


@pytest.mark.asyncio
async def test_standard_signal_duplicate_observation_id_is_deduplicated() -> None:
    app, store = _app()
    payload = _metric_payload()

    first = await _post(app, "/internal/v1/telemetry/metrics", payload)
    second = await _post(app, "/internal/v1/telemetry/metrics", deepcopy(payload))

    assert first.status_code == 202
    assert second.status_code == 202
    assert first.json()["accepted"] == 1
    assert second.json()["duplicate"] == 1
    assert list(store.observations) == [OBSERVATION_ID]


@pytest.mark.asyncio
async def test_arbitrary_attributes_events_and_links_are_rejected_before_storage(
    caplog: pytest.LogCaptureFixture,
) -> None:
    app, store = _app()

    with caplog.at_level(logging.WARNING, logger="aura_observatory.entrypoints.api.app"):
        response = await _post(
            app,
            "/internal/v1/telemetry/traces",
            _span_payload(with_sensitive_fields=True),
        )

    assert response.status_code in {202, 400, 422}, response.text
    if response.status_code == 202:
        assert response.json()["rejected"] >= 1
    assert store.observations == {}
    body = response.text.casefold()
    assert "do not persist this" not in body
    assert "bearer private" not in body
    assert "secret response" not in body
    assert "private result" not in body
    assert "class=event" in caplog.text
    assert "do not persist this" not in caplog.text
    assert "bearer private" not in caplog.text
    assert "provider.invalid" not in caplog.text
    assert "owner-private" not in caplog.text


@pytest.mark.asyncio
async def test_old_custom_observations_envelope_is_not_a_collector_signal() -> None:
    app, store = _app()

    response = await _post(
        app,
        "/internal/v1/telemetry/metrics",
        {"observations": [{"observationId": OBSERVATION_ID, "prompt": "private"}]},
    )

    assert response.status_code in {400, 422}
    assert store.observations == {}
