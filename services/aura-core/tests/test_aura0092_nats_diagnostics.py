"""Core AURA-0092 NATS diagnostic privacy regressions."""

from __future__ import annotations

from typing import Any, cast

import pytest
from aura_core.bootstrap import evaluation_runner
from aura_core.platform.telemetry import MetadataMetrics


def test_nats_error_classification_is_finite_and_does_not_read_exception_text() -> None:
    class AuthorizationFailure(Exception):
        def __str__(self) -> str:
            return "nats://private-user:private-password@private.example:4222"

    class TimeoutFailure(Exception):
        pass

    class ProtocolFailure(Exception):
        pass

    class PrivateProviderFailure(Exception):
        pass

    classify = evaluation_runner._bounded_nats_error_class  # pyright: ignore[reportPrivateUsage]
    assert classify(AuthorizationFailure()) == "authorization"
    assert classify(TimeoutFailure()) == "timeout"
    assert classify(ProtocolFailure()) == "provider_protocol"
    assert classify(PrivateProviderFailure()) == "unknown"
    assert {
        classify(error)
        for error in (
            AuthorizationFailure(),
            TimeoutFailure(),
            ProtocolFailure(),
            PrivateProviderFailure(),
        )
    } <= evaluation_runner._NATS_ERROR_CLASSES  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_pinned_nats_connect_receives_privacy_safe_callbacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, Any] = {}

    async def fake_connect(url: str, **callbacks: Any) -> object:
        calls["url"] = url
        calls.update(callbacks)
        return object()

    monkeypatch.setattr(evaluation_runner.nats, "connect", fake_connect)
    metrics = MetadataMetrics(capacity=8)
    connection = await evaluation_runner._connect_evaluation_nats(  # pyright: ignore[reportPrivateUsage]
        "nats://private-user:private-password@private.example:4222",
        metrics,
    )

    assert connection is not None
    assert calls["url"] == "nats://private-user:private-password@private.example:4222"
    assert set(calls) == {
        "url",
        "error_cb",
        "disconnected_cb",
        "closed_cb",
        "reconnected_cb",
    }

    await calls["error_cb"](RuntimeError("private provider traceback and endpoint"))
    await calls["disconnected_cb"]()
    await calls["closed_cb"]()
    await calls["reconnected_cb"]()

    measurements = metrics.snapshot()
    assert len(measurements) == 4
    assert {
        measurement.metric for measurement in measurements
    } == {
        "evaluation_runner_nats_fetch_failure_count",
        "evaluation_runner_nats_drain_failure_count",
        "evaluation_runner_nats_recovery_count",
    }
    assert all(
        set(dict(measurement.dimensions)) == {"dependency", "outcome", "error_class"}
        for measurement in measurements
    )
    assert "private" not in repr(measurements)
    assert "traceback" not in repr(measurements)
    assert "nats://" not in repr(measurements)


@pytest.mark.asyncio
async def test_nats_diagnostic_callbacks_survive_telemetry_sink_failure() -> None:
    class BrokenMetrics:
        def increment(self, *args: Any, **kwargs: Any) -> None:
            del args, kwargs
            raise RuntimeError("telemetry sink private diagnostic")

    callbacks = evaluation_runner._evaluation_nats_callbacks(  # pyright: ignore[reportPrivateUsage]
        cast(MetadataMetrics, BrokenMetrics())
    )

    await callbacks["error_cb"](RuntimeError("private provider diagnostic"))
    await callbacks["disconnected_cb"]()
    await callbacks["closed_cb"]()
    await callbacks["reconnected_cb"]()
