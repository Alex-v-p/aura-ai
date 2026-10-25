# pyright: reportPrivateUsage=false, reportUnknownArgumentType=false, reportUnknownVariableType=false
"""Regression coverage for Core-owned AURA_RUNS stream reconciliation."""

from types import SimpleNamespace
from typing import Any

import pytest
from aura_core.platform.outbox.nats import (
    AURA_RUNS_STREAM,
    _ensure_command_stream,
)
from aura_core.platform.outbox.nats import (
    asyncio as nats_asyncio,
)
from aura_core.platform.outbox.service import RUN_COMMAND_TOPIC


def _stream_info(name: str, subjects: object) -> Any:
    return SimpleNamespace(config=SimpleNamespace(name=name, subjects=subjects))


class RecordingJetStream:
    """Small provider double that records every stream mutation attempt."""

    def __init__(self, *, add_error: Exception, lookups: list[object]) -> None:
        self.add_error = add_error
        self.lookups = list(lookups)
        self.add_calls: list[tuple[str, tuple[str, ...]]] = []
        self.lookup_calls: list[str] = []
        self.update_calls: list[tuple[str, tuple[str, ...]]] = []
        self.delete_calls: list[str] = []

    async def add_stream(self, *, name: str, subjects: tuple[str, ...]) -> None:
        self.add_calls.append((name, subjects))
        raise self.add_error

    async def stream_info(self, name: str) -> Any:
        self.lookup_calls.append(name)
        result = self.lookups.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    async def update_stream(self, *, name: str, subjects: list[str]) -> None:
        self.update_calls.append((name, tuple(subjects)))

    async def delete_stream(self, name: str) -> None:
        self.delete_calls.append(name)


@pytest.mark.asyncio
async def test_fresh_stream_deduplicates_default_subject() -> None:
    class FreshJetStream(RecordingJetStream):
        async def add_stream(self, *, name: str, subjects: tuple[str, ...]) -> None:
            self.add_calls.append((name, subjects))

    jetstream = FreshJetStream(add_error=RuntimeError("unused"), lookups=[])
    await _ensure_command_stream(
        jetstream, (RUN_COMMAND_TOPIC, RUN_COMMAND_TOPIC)
    )

    assert jetstream.add_calls == [(AURA_RUNS_STREAM, (RUN_COMMAND_TOPIC,))]
    assert jetstream.lookup_calls == []
    assert jetstream.update_calls == []
    assert jetstream.delete_calls == []


@pytest.mark.asyncio
async def test_fresh_stream_keeps_custom_subject_after_default_in_stable_order() -> None:
    custom_subject = "aura.runs.execute.custom.v1"

    class FreshJetStream(RecordingJetStream):
        async def add_stream(self, *, name: str, subjects: tuple[str, ...]) -> None:
            self.add_calls.append((name, subjects))

    jetstream = FreshJetStream(add_error=RuntimeError("unused"), lookups=[])
    await _ensure_command_stream(
        jetstream,
        (RUN_COMMAND_TOPIC, custom_subject, RUN_COMMAND_TOPIC, custom_subject),
    )

    assert jetstream.add_calls == [
        (AURA_RUNS_STREAM, (RUN_COMMAND_TOPIC, custom_subject))
    ]


@pytest.mark.asyncio
async def test_existing_stream_adds_only_missing_subjects_in_place() -> None:
    custom_subject = "aura.runs.execute.custom.v1"
    creation_error = RuntimeError("stream already exists")
    existing_subjects = ("aura.runs.execute.legacy.v1", RUN_COMMAND_TOPIC)
    jetstream = RecordingJetStream(
        add_error=creation_error,
        lookups=[_stream_info(AURA_RUNS_STREAM, existing_subjects)],
    )

    await _ensure_command_stream(
        jetstream, (RUN_COMMAND_TOPIC, custom_subject, custom_subject)
    )

    assert jetstream.update_calls == [
        (
            AURA_RUNS_STREAM,
            existing_subjects + (custom_subject,),
        )
    ]
    assert jetstream.delete_calls == []
    assert jetstream.lookup_calls == [AURA_RUNS_STREAM]


@pytest.mark.asyncio
async def test_lost_create_race_retries_until_named_stream_is_visible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    creation_error = RuntimeError("concurrent create lost")
    sleeps: list[float] = []

    async def no_wait(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(nats_asyncio, "sleep", no_wait)
    jetstream = RecordingJetStream(
        add_error=creation_error,
        lookups=[
            RuntimeError("stream not visible yet"),
            _stream_info(AURA_RUNS_STREAM, (RUN_COMMAND_TOPIC,)),
        ],
    )

    await _ensure_command_stream(jetstream, (RUN_COMMAND_TOPIC,))

    assert jetstream.lookup_calls == [AURA_RUNS_STREAM, AURA_RUNS_STREAM]
    assert sleeps == [0.1]
    assert jetstream.update_calls == []
    assert jetstream.delete_calls == []


@pytest.mark.asyncio
async def test_permanent_stream_absence_reraises_original_provider_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    creation_error = RuntimeError("provider rejected stream creation")

    async def no_wait(_delay: float) -> None:
        return None

    monkeypatch.setattr(nats_asyncio, "sleep", no_wait)
    jetstream = RecordingJetStream(
        add_error=creation_error,
        lookups=[
            RuntimeError("not found") for _ in range(3)
        ],
    )

    with pytest.raises(RuntimeError) as caught:
        await _ensure_command_stream(jetstream, (RUN_COMMAND_TOPIC,))

    assert caught.value is creation_error
    assert jetstream.lookup_calls == [AURA_RUNS_STREAM] * 3
    assert jetstream.update_calls == []
    assert jetstream.delete_calls == []


@pytest.mark.asyncio
async def test_malformed_subject_metadata_fails_closed_without_mutation() -> None:
    creation_error = RuntimeError("stream already exists")
    jetstream = RecordingJetStream(
        add_error=creation_error,
        lookups=[_stream_info(AURA_RUNS_STREAM, None)],
    )

    with pytest.raises(RuntimeError, match="subject configuration is unavailable"):
        await _ensure_command_stream(jetstream, (RUN_COMMAND_TOPIC,))

    assert jetstream.update_calls == []
    assert jetstream.delete_calls == []


@pytest.mark.asyncio
async def test_differently_named_stream_fails_closed_and_preserves_provider_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    creation_error = RuntimeError("provider rejected stream creation")

    async def no_wait(_delay: float) -> None:
        return None

    monkeypatch.setattr(nats_asyncio, "sleep", no_wait)
    jetstream = RecordingJetStream(
        add_error=creation_error,
        lookups=[
            _stream_info("OTHER_STREAM", (RUN_COMMAND_TOPIC,))
            for _ in range(3)
        ],
    )

    with pytest.raises(RuntimeError) as caught:
        await _ensure_command_stream(jetstream, (RUN_COMMAND_TOPIC,))

    assert caught.value is creation_error
    assert jetstream.update_calls == []
    assert jetstream.delete_calls == []
