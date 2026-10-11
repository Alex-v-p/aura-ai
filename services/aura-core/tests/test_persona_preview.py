"""Contract and privacy tests for the ephemeral Persona preview boundary."""

from collections.abc import AsyncIterator, Sequence
from typing import Any, cast
from uuid import uuid4

import httpx
import pytest
from aura_core.domains.interaction.personas.public import ConfigurationStatus
from aura_core.entrypoints.api.app import create_app
from aura_core.platform.auth import Principal, Settings
from aura_core.platform.telemetry import StructuredContainerLogExporter
from aura_core.runtime.models.gateway import ModelGateway
from aura_core.runtime.models.ports import ChatMessage, ModelDescriptor, ProviderTraceContext
from fastapi import FastAPI

ORIGIN = "https://aura.dev.example"
OWNER = "owner-subject"


class UnexpectedProviderFailure(Exception):
    pass


class PreviewProvider:
    """Deterministic provider fake which records only call counts and inputs."""

    def __init__(self, *, failure: str | None = None, output: str = "A calm preview.") -> None:
        self.failure = failure
        self.output = output
        self.inventory_calls = 0
        self.stream_calls = 0
        self.system_prompt = ""
        self.prompt = ""

    async def list_models(self) -> Sequence[ModelDescriptor]:
        self.inventory_calls += 1
        return (
            ModelDescriptor("chat", "Chat", "test", ("chat", "completion")),
            ModelDescriptor("disabled", "Disabled", "test", ("chat",), selectable=False),
            ModelDescriptor(
                "offline", "Offline", "test", ("chat",), availability="unavailable"
            ),
            ModelDescriptor("embedding", "Embedding", "test", ("embedding",)),
        )

    async def is_ready(self, model_id: str | None = None) -> bool:
        return model_id in {None, "chat", "disabled"}

    async def stream_chat(
        self, model_id: str, messages: Sequence[ChatMessage]
    ) -> AsyncIterator[str]:
        self.stream_calls += 1
        self.system_prompt = messages[0].content
        self.prompt = messages[1].content
        if self.failure == "timeout":
            raise TimeoutError("PROVIDER_TIMEOUT_SECRET")
        if self.failure == "provider":
            raise RuntimeError("PROVIDER_EXCEPTION_SECRET")
        if self.failure == "unexpected":
            raise UnexpectedProviderFailure("UNEXPECTED_PROVIDER_SECRET")
        assert model_id == "chat"
        yield self.output


class GatewayPreviewPort:
    def __init__(self, gateway: ModelGateway) -> None:
        self.gateway = gateway

    async def preview(
        self,
        model_id: str,
        system_prompt: str,
        prompt: str,
        *,
        trace: Any,
    ) -> str:
        return await self.gateway.preview(
            model_id,
            system_prompt,
            prompt,
            context=ProviderTraceContext(trace_id=trace.trace_id, span_id=trace.span_id),
        )


def _settings() -> Settings:
    return Settings(
        public_origin=ORIGIN,
        oidc_issuer="https://authentik.dev.example",
        oidc_client_id="aura-web",
        oidc_audience="aura-web",
        oidc_redirect_uri=f"{ORIGIN}/api/v1/auth/callback",
        owner_subject=OWNER,
        default_model="chat",
        secure_cookies=False,
    )


async def _owner_client(
    app: FastAPI, *, raise_app_exceptions: bool = True
) -> tuple[httpx.AsyncClient, Any]:
    session_id, session = await app.state.aura.sessions.create(
        Principal("https://authentik.dev.example", OWNER, "Test user")
    )
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=raise_app_exceptions),
        base_url=ORIGIN,
        headers={"Origin": ORIGIN},
    )
    client.cookies.set(app.state.aura.settings.session_cookie.name, session_id)
    return client, session


def _install_provider(app: FastAPI, provider: PreviewProvider) -> None:
    app.state.aura.provider = provider
    gateway = ModelGateway(provider, app.state.aura.metrics)
    app.state.aura.gateway = gateway
    # The application service owns the provider-neutral seam. Replacing the
    # port keeps this test independent of the private composition adapter.
    app.state.aura.persona_service.preview_port = GatewayPreviewPort(gateway)


def _draft(prompt: str = "PREVIEW_PROMPT_SECRET") -> dict[str, object]:
    return {
        "modelId": "chat",
        "prompt": prompt,
        "draft": {
            "displayName": "Calm guide",
            "description": "PREVIEW_DRAFT_SECRET",
            "instructions": "Keep answers practical.",
        },
    }


@pytest.fixture
def preview_app() -> FastAPI:
    app = create_app(_settings(), testing=True)
    export_lines: list[str] = []
    app.state.preview_export_lines = export_lines
    app.state.aura.metrics._exporter = StructuredContainerLogExporter(  # pyright: ignore[reportPrivateUsage]
        export_lines.append
    )
    _install_provider(app, PreviewProvider())
    return app


async def _assert_exported_without_content(app: FastAPI, *sentinels: str) -> None:
    assert await app.state.aura.metrics.flush()
    rendered = "\n".join(cast(list[str], app.state.preview_export_lines))
    assert all(sentinel not in rendered for sentinel in sentinels)


@pytest.mark.asyncio
async def test_preview_success_is_bounded_ephemeral_and_not_cached(preview_app: FastAPI) -> None:
    provider = PreviewProvider(output="<script>OUTPUT_SECRET</script>")
    _install_provider(preview_app, provider)
    client, session = await _owner_client(preview_app)
    try:
        audit_before = list(preview_app.state.aura.store.auth_audit)
        conversations_before = dict(preview_app.state.aura.store._conversations)
        idempotency_before = dict(preview_app.state.aura.store._idempotency)
        response = await client.post(
            "/api/v1/personas/preview", headers={"X-CSRF-Token": session.csrf_token}, json=_draft()
        )
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.json() == {
            "modelId": "chat",
            "content": "<script>OUTPUT_SECRET</script>",
        }
        assert provider.inventory_calls == 1
        assert provider.stream_calls == 1
        assert "PREVIEW_PROMPT_SECRET" in provider.prompt
        assert "PREVIEW_DRAFT_SECRET" in provider.system_prompt
        assert preview_app.state.aura.store.auth_audit == audit_before
        assert preview_app.state.aura.store._conversations == conversations_before
        assert preview_app.state.aura.store._idempotency == idempotency_before
        await _assert_exported_without_content(
            preview_app,
            "PREVIEW_PROMPT_SECRET",
            "PREVIEW_DRAFT_SECRET",
            "OUTPUT_SECRET",
            "endpoint",
            "credential",
        )
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_preview_requires_exactly_one_source(preview_app: FastAPI) -> None:
    client, session = await _owner_client(preview_app)
    try:
        headers = {"X-CSRF-Token": session.csrf_token}
        neither = await client.post(
            "/api/v1/personas/preview", headers=headers, json={"modelId": "chat", "prompt": "test"}
        )
        both = await client.post(
            "/api/v1/personas/preview",
            headers=headers,
            json={**_draft(), "personaRevisionId": str(uuid4())},
        )
        assert neither.status_code == 422
        assert both.status_code == 422
        await _assert_exported_without_content(
            preview_app,
            "PREVIEW_PROMPT_SECRET",
            "PREVIEW_DRAFT_SECRET",
        )
    finally:
        await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("model_id", ["missing", "disabled", "offline", "embedding"])
async def test_preview_rejects_unavailable_or_non_selectable_model(
    preview_app: FastAPI, model_id: str
) -> None:
    provider = PreviewProvider()
    _install_provider(preview_app, provider)
    client, session = await _owner_client(preview_app)
    try:
        payload = _draft()
        payload["modelId"] = model_id
        response = await client.post(
            "/api/v1/personas/preview",
            headers={"X-CSRF-Token": session.csrf_token},
            json=payload,
        )
        assert response.status_code == 422
        assert response.headers["cache-control"] == "no-store"
        assert provider.stream_calls == 0
        assert "PROVIDER" not in response.text
        outcome = next(
            item
            for item in preview_app.state.aura.metrics.snapshot()
            if item.metric == "persona_preview_outcome"
        )
        assert dict(outcome.dimensions)["error_class"] == "capability"
        await _assert_exported_without_content(
            preview_app,
            "PREVIEW_PROMPT_SECRET",
            "PREVIEW_DRAFT_SECRET",
        )
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_preview_rejects_disabled_persona_revision(preview_app: FastAPI) -> None:
    persona = preview_app.state.aura.persona_catalog.list_personas()[0]
    await preview_app.state.aura.persona_service.set_status(
        "https://authentik.dev.example",
        OWNER,
        persona.id,
        persona.version,
        ConfigurationStatus.DISABLED,
        str(uuid4()),
    )
    provider = PreviewProvider()
    _install_provider(preview_app, provider)
    client, session = await _owner_client(preview_app, raise_app_exceptions=False)
    try:
        response = await client.post(
            "/api/v1/personas/preview",
            headers={"X-CSRF-Token": session.csrf_token},
            json={
                "modelId": "chat",
                "prompt": "test",
                "personaRevisionId": str(persona.current_revision.id),
            },
        )
        assert response.status_code == 404
        assert provider.inventory_calls == 0
        assert provider.stream_calls == 0
    finally:
        await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["timeout", "provider", "unexpected"])
async def test_preview_failure_is_generic_single_call_and_content_free(
    preview_app: FastAPI, failure: str
) -> None:
    provider = PreviewProvider(failure=failure)
    _install_provider(preview_app, provider)
    client, session = await _owner_client(preview_app)
    try:
        response = await client.post(
            "/api/v1/personas/preview",
            headers={"X-CSRF-Token": session.csrf_token},
            json=_draft(),
        )
        assert response.status_code == 503
        assert response.headers["cache-control"] == "no-store"
        body = response.json()
        assert body["detail"] == "persona preview is temporarily unavailable"
        assert body["code"] == "DEPENDENCY_UNAVAILABLE"
        assert "PROVIDER_TIMEOUT_SECRET" not in repr(body)
        assert "PROVIDER_EXCEPTION_SECRET" not in repr(body)
        assert "UNEXPECTED_PROVIDER_SECRET" not in repr(body)
        assert provider.inventory_calls == 1
        assert provider.stream_calls == 1
        assert "PROVIDER_TIMEOUT_SECRET" not in response.text
        assert "PROVIDER_EXCEPTION_SECRET" not in response.text
        assert "UNEXPECTED_PROVIDER_SECRET" not in response.text
        serialized = repr(preview_app.state.aura.metrics.snapshot())
        assert "PREVIEW_PROMPT_SECRET" not in serialized
        assert "PREVIEW_DRAFT_SECRET" not in serialized
        assert "PROVIDER_TIMEOUT_SECRET" not in serialized
        assert "PROVIDER_EXCEPTION_SECRET" not in serialized
        assert "UNEXPECTED_PROVIDER_SECRET" not in serialized
        outcome = next(
            item
            for item in preview_app.state.aura.metrics.snapshot()
            if item.metric == "persona_preview_outcome"
        )
        expected_class = "unknown" if failure == "unexpected" else failure
        assert dict(outcome.dimensions)["error_class"] == expected_class
        errors = [
            item
            for item in preview_app.state.aura.metrics.snapshot()
            if item.metric == "errors"
        ]
        assert errors and dict(errors[-1].dimensions)["error_class"] == expected_class
        provider_errors = [
            item
            for item in preview_app.state.aura.metrics.snapshot()
            if item.metric == "provider_errors"
        ]
        assert provider_errors
        assert dict(provider_errors[-1].dimensions)["provider"] == "unknown"
        await _assert_exported_without_content(
            preview_app,
            "PREVIEW_PROMPT_SECRET",
            "PREVIEW_DRAFT_SECRET",
            "PROVIDER_TIMEOUT_SECRET",
            "PROVIDER_EXCEPTION_SECRET",
            "UNEXPECTED_PROVIDER_SECRET",
        )
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_preview_telemetry_is_parented_metadata_only_and_cardinality_bounded(
    preview_app: FastAPI,
) -> None:
    provider = PreviewProvider(output="x" * 65_536)
    _install_provider(preview_app, provider)
    client, session = await _owner_client(preview_app)
    try:
        response = await client.post(
            "/api/v1/personas/preview",
            headers={"X-CSRF-Token": session.csrf_token},
            json=_draft(),
        )
        assert response.status_code == 200
        assert len(response.json()["content"]) == 32_768
        measurements = preview_app.state.aura.metrics.snapshot()
        trace_ids = {
            item.trace_id
            for item in measurements
            if item.component_id == "aura.interaction.persona_preview"
        }
        assert len(trace_ids) == 1
        trace_id = next(iter(trace_ids))
        assert trace_id is not None
        root = next(
            item
            for item in measurements
            if item.component_id == "aura.interaction.persona_preview"
            and item.kind == "span"
        )
        assert root.metric == "persona_preview_duration_ms"
        assert root.parent_span_id is None
        inference = next(
            item
            for item in measurements
            if item.component_id == "aura.runtime.model_inference"
            and item.kind == "span"
            and item.metric == "inference_duration_ms"
        )
        assert inference.trace_id == trace_id
        assert inference.parent_span_id == root.span_id
        # Inventory telemetry is emitted by the concrete provider adapter. The
        # provider-neutral fake used here intentionally exposes only the
        # gateway's observable child; Ollama adapter coverage owns the cache
        # hit/miss inventory span contract.
        assert "PREVIEW_PROMPT_SECRET" not in repr(measurements)
        assert "PREVIEW_DRAFT_SECRET" not in repr(measurements)
    finally:
        await client.aclose()
