"""Shared deterministic fixtures for assembled Core API system tests."""

import sys
from collections.abc import AsyncIterator, Sequence
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

sys.path.insert(0, str(Path(__file__).parents[2] / "src"))


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "integration: uses an operator-provided disposable service dependency"
    )

from aura_core.entrypoints.api.app import create_app  # noqa: E402
from aura_core.platform.auth import Principal, Session, Settings  # noqa: E402
from aura_core.runtime.models.gateway import ModelGateway  # noqa: E402
from aura_core.runtime.models.ports import ChatMessage, ModelDescriptor  # noqa: E402


class ScriptedModel:
    """A provider fake that never performs I/O and can pause after a delta."""

    def __init__(self, chunks: Sequence[str] = ("Hello", " from Aura.")) -> None:
        self.chunks = tuple(chunks)

    async def list_models(self) -> Sequence[ModelDescriptor]:
        return (
            ModelDescriptor("chat", "Chat model", "ollama", ("chat", "completion")),
            ModelDescriptor(
                "embed",
                "Embedding model",
                "ollama",
                ("embedding",),
                selectable=False,
                disabled_reason="model does not advertise chat capability",
            ),
            ModelDescriptor("chat-plus", "Chat Plus", "ollama", ("chat", "completion")),
        )

    async def stream_chat(
        self, model_id: str, messages: Sequence[ChatMessage]
    ) -> AsyncIterator[str]:
        del messages
        if model_id != "chat":
            return
        for chunk in self.chunks:
            yield chunk

    async def is_ready(self, model_id: str | None = None) -> bool:
        return model_id in (None, "chat", "chat-plus")


@pytest.fixture
def api_app() -> FastAPI:
    settings = Settings(
        public_origin="https://aura.dev.example",
        oidc_issuer="https://authentik.dev.example",
        oidc_client_id="aura-web",
        oidc_audience="aura-web",
        oidc_redirect_uri="https://aura.dev.example/api/v1/auth/callback",
        owner_subject="owner-subject",
        default_model="chat",
        secure_cookies=False,
    )
    app = create_app(settings, testing=True)
    provider = ScriptedModel()
    app.state.aura.provider = provider
    app.state.aura.gateway = ModelGateway(provider)
    return app


async def owner_client(
    app: FastAPI, subject: str = "owner-subject", issuer: str = "https://authentik.dev.example"
) -> tuple[httpx.AsyncClient, Session]:
    session_id, session = await app.state.aura.sessions.create(
        Principal(issuer, subject, "Test user")
    )
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://aura.dev.example",
        headers={"Origin": "https://aura.dev.example"},
    )
    client.cookies.set(app.state.aura.settings.session_cookie.name, session_id)
    return client, session
