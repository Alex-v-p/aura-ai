"""Garage adapter protocol evaluations for AURA-0068."""

from __future__ import annotations

from collections.abc import AsyncIterable, AsyncIterator

import httpx
import pytest
from aura_core.providers.object_storage import garage


class FakeResponse:
    def __init__(self, status_code: int = 200, body: bytes = b"object") -> None:
        self.status_code = status_code
        self.headers = {"content-length": str(len(body))}
        self._body = body
        self.closed = False

    async def aiter_bytes(self) -> AsyncIterator[bytes]:
        yield self._body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "garage failure",
                request=httpx.Request("GET", "http://garage"),
                response=httpx.Response(self.status_code),
            )

    async def aclose(self) -> None:
        self.closed = True


class FakeAsyncClient:
    instances: list[FakeAsyncClient] = []

    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        self.requests: list[httpx.Request] = []
        self.responses: list[FakeResponse] = []
        self.__class__.instances.append(self)

    async def __aenter__(self) -> FakeAsyncClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        del args

    def build_request(self, method: str, url: str) -> httpx.Request:
        return httpx.Request(method, url)

    async def send(self, request: httpx.Request, *, stream: bool = False) -> FakeResponse:
        del stream
        self.requests.append(request)
        response = FakeResponse()
        self.responses.append(response)
        return response


async def _stream() -> AsyncIterable[bytes]:
    yield b"payload"


@pytest.mark.asyncio
async def test_garage_put_get_delete_use_sigv4_and_never_send_raw_secret_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeAsyncClient.instances.clear()
    monkeypatch.setattr(garage.httpx, "AsyncClient", FakeAsyncClient)
    store = garage.GarageS3ObjectStore(
        "http://garage:3900/",
        "private-bucket",
        access_key="access",
        secret_key="secret",
    )

    await store.put_stream("documents/owner/with space.txt", _stream(), content_type="text/plain")
    downloaded = [piece async for piece in store.get("documents/owner/with space.txt")]
    await store.delete("documents/owner/with space.txt")

    requests = [request for client in FakeAsyncClient.instances for request in client.requests]
    assert [request.method for request in requests] == ["PUT", "GET", "DELETE"]
    assert all(request.url.host == "garage" for request in requests)
    assert all(
        "private-bucket/documents/owner/with%20space.txt" in str(request.url)
        for request in requests
    )
    assert downloaded == [b"object"]
    for request in requests:
        assert request.headers.get("authorization", "").startswith("AWS4-HMAC-SHA256 ")
        assert request.headers.get("x-amz-date")
        assert request.headers.get("x-amz-content-sha256")
        assert "x-amz-secret-key" not in request.headers
        assert "x-amz-access-key" not in request.headers


@pytest.mark.asyncio
async def test_garage_non_success_responses_are_closed_and_classified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingClient(FakeAsyncClient):
        async def send(self, request: httpx.Request, *, stream: bool = False) -> FakeResponse:
            del stream
            self.requests.append(request)
            response = FakeResponse(503)
            self.responses.append(response)
            return response

    monkeypatch.setattr(garage.httpx, "AsyncClient", FailingClient)
    store = garage.GarageS3ObjectStore(
        "http://garage:3900",
        "bucket",
        access_key="access",
        secret_key="secret",
    )
    with pytest.raises(garage.GarageObjectStoreError):
        await store.put_stream("key", _stream(), content_type="text/plain")
    with pytest.raises(garage.GarageObjectStoreError):
        [piece async for piece in store.get("key")]
    with pytest.raises(garage.GarageObjectStoreError):
        await store.delete("key")
    assert all(
        response.closed
        for client in FakeAsyncClient.instances
        for response in client.responses
    )
