# ruff: noqa: E501

from __future__ import annotations

import pytest
from aura_core.bootstrap.artifact_uow import MemoryGeneratedArtifactObjectStore
from aura_core.providers.object_storage.generated_artifacts import (
    GENERATED_ARTIFACT_BUCKET,
    GeneratedArtifactGarageObjectStore,
)


@pytest.mark.asyncio
async def test_memory_artifact_store_round_trip_and_missing_is_absent() -> None:
    store = MemoryGeneratedArtifactObjectStore()
    await store.put_stream(
        "artifact/a/1", _stream(b"bytes"), content_type="text/plain; charset=utf-8"
    )
    assert [chunk async for chunk in store.get("artifact/a/1")] == [b"bytes"]
    await store.delete("artifact/a/1")
    with pytest.raises(FileNotFoundError):
        await store.delete("artifact/a/1")


def test_garage_adapter_has_fixed_dedicated_bucket() -> None:
    assert GENERATED_ARTIFACT_BUCKET == "aura-artifacts"
    store = GeneratedArtifactGarageObjectStore(
        "http://127.0.0.1:3900", access_key="key", secret_key="secret"
    )
    assert store.bucket_name == GENERATED_ARTIFACT_BUCKET


async def _stream(value: bytes):
    yield value
