"""Regression coverage for the real Core -> Collector -> Observatory wire.

These tests deliberately send the JSON form of the standard OTLP signal
requests produced by Core.  The old ``{"observations": [...]}`` envelope is
not a Collector protocol and must not be the compatibility contract here.
"""

from __future__ import annotations

from copy import deepcopy
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


def _app() -> tuple[Any, MemoryObservationStore]:
    store = MemoryObservationStore()
    service = TelemetryIngestService(store, MemoryCatalog(), credential=TOKEN)
    return create_app(Settings(), ingest_service=service, testing=True), store


async def _post(app: Any, path: str, payload: dict[str, Any]) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://collector") as client:
        return await client.post(
            path,
            json=payload,
            headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
        )


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
async def test_arbitrary_attributes_events_and_links_are_rejected_before_storage() -> None:
    app, store = _app()

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
