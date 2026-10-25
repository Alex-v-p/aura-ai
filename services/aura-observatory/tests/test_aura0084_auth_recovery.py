"""AURA-0084 sanitized owner callback recovery regressions."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
import yaml
from aura_observatory.auth import (
    LoginState,
    MemoryLoginStateBackend,
    MemorySessionBackend,
    ObservatoryAuth,
    sanitize_return_to,
)
from aura_observatory.entrypoints.api.app import create_app
from aura_observatory.settings import Settings


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "oidc_issuer": "https://authentik.example.invalid/application/o/aura-observatory/",
        "oidc_audience": "aura-observatory",
        "oidc_client_id": "aura-observatory",
        "owner_subject": "owner-subject",
    }
    values.update(overrides)
    return Settings(**cast(Any, values))


async def _client(auth: ObservatoryAuth, settings: Settings) -> httpx.AsyncClient:
    app = create_app(settings, auth=auth, testing=True)
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=settings.public_origin,
        follow_redirects=False,
    )


async def _state(auth: ObservatoryAuth, return_to: str = "/") -> str:
    state, _nonce = await auth.begin_login(return_to)
    return state


class _RecordingLoginStateBackend(MemoryLoginStateBackend):
    def __init__(self) -> None:
        super().__init__()
        self.return_paths: list[str] = []

    async def put(self, state: str, value: LoginState, ttl_seconds: int) -> None:
        self.return_paths.append(value.return_to)
        await super().put(state, value, ttl_seconds)


def _location(response: httpx.Response) -> str:
    return response.headers["location"]


def _assert_clears_cookie(response: httpx.Response, settings: Settings) -> None:
    cookie = response.headers.get("set-cookie", "")
    assert f"{settings.effective_session_cookie_name}=;" in cookie
    assert "Max-Age=0" in cookie
    assert "HttpOnly" in cookie


def _callback_metric_labels(app: Any) -> set[tuple[str, str]]:
    return {
        (str(dict(labels)["stage"]), str(dict(labels)["outcome"]))
        for (name, labels), _value in app.state.query.metrics.values.items()
        if name.startswith("observatory_auth_callback_")
    }


def test_return_to_sanitizer_allowlists_relative_paths_and_strips_sensitive_variants() -> None:
    sensitive = (
        "access_token",
        "apiKey",
        "bearer_token",
        "cookie_value",
        "refresh-token",
        "idToken",
        "authorization",
        "client_secret",
        "client_id",
        "client_assertion",
        "credential_value",
        "Body",
        "content",
        "Document",
        "file",
        "FILE-name",
        "input",
        "memory",
        "message",
        "output",
        "payload",
        "tool",
        "tool-argument",
        "tool_arguments",
        "tool-result",
        "tool_results",
        "audience",
        "aud",
        "azp",
        "provider",
        "redirect_uri",
        "authorization_code",
        "authorization_endpoint",
        "token_endpoint",
        "userinfo_endpoint",
        "jwks_uri",
        "end_session_endpoint",
        "password",
        "nonce",
        "code",
        "login_hint",
        "response_type",
        "response_mode",
        "scope",
        "prompt",
        "request",
        "request_uri",
        "response",
        "iss",
        "sub",
        "state",
        "session_id",
        "token_hint",
        "issuer",
        "subject",
        "endpoint",
        "error_description",
        "error_uri",
        "auth_recovery",
    )
    value = (
        "/runs?selected=trace&status=running&component=aura.runtime.api&model=small"
        "&deployment=local&filter=recent&from=2026-01-01&to=2026-01-02&"
        + "&".join(f"{key}=PRIVATE_VALUE" for key in sensitive)
    )

    sanitized = sanitize_return_to(value + "#private-fragment")

    assert sanitized == (
        "/runs?selected=trace&status=running&component=aura.runtime.api&model=small"
        "&deployment=local&filter=recent&from=2026-01-01&to=2026-01-02"
    )
    assert "PRIVATE_VALUE" not in sanitized
    assert "fragment" not in sanitized
    assert sanitize_return_to("https://evil.invalid/exfiltrate") == "/"
    assert sanitize_return_to("//evil.invalid/exfiltrate") == "/"
    assert sanitize_return_to("/" + "a" * 2048) == "/"


@pytest.mark.asyncio
async def test_return_to_is_sanitized_before_login_state_persistence() -> None:
    settings = _settings()
    login_states = _RecordingLoginStateBackend()
    auth = ObservatoryAuth(settings, MemorySessionBackend(), login_states)

    await auth.begin_login(
        "/runs?filter=recent&from=2026-01-01&to=2026-01-02"
        "&sub=PRIVATE_SUB&token_endpoint=PRIVATE_ENDPOINT"
        "&Body=PRIVATE_BODY&tool-arguments=PRIVATE_TOOL#fragment"
    )

    assert login_states.return_paths == [
        "/runs?filter=recent&from=2026-01-01&to=2026-01-02"
    ]


@pytest.mark.asyncio
async def test_callback_reapplies_sanitizer_before_success_redirect() -> None:
    settings = _settings()
    login_states = MemoryLoginStateBackend()
    auth = ObservatoryAuth(settings, MemorySessionBackend(), login_states)
    await login_states.put(
        "raw-state",
        LoginState(
            "raw-nonce",
            "/runs?password=PRIVATE_PASSWORD&sub=PRIVATE_SUB&from=2026-01-01"
            "&to=2026-01-02&filter=recent&FILE-name=PRIVATE_FILENAME"
            "&tool_results=PRIVATE_TOOL_RESULTS#private-fragment",
            datetime.now(UTC) + timedelta(minutes=5),
        ),
        300,
    )

    async def authenticate(
        _code: str, received_nonce: str
    ) -> tuple[dict[str, str], Mapping[str, object]]:
        assert received_nonce == "raw-nonce"
        return {}, {
            "iss": settings.oidc_issuer,
            "sub": settings.owner_subject,
            "aud": [settings.oidc_audience],
        }

    auth.authenticate_code = authenticate  # type: ignore[method-assign]
    async with await _client(auth, settings) as client:
        response = await client.get(
            "/api/v1/auth/callback",
            params={"state": "raw-state", "code": "PRIVATE_CODE"},
        )

    assert response.status_code == 303
    assert _location(response) == "/runs?from=2026-01-01&to=2026-01-02&filter=recent"
    assert "PRIVATE_PASSWORD" not in response.headers["location"]
    assert "PRIVATE_" not in response.headers["location"]


@pytest.mark.asyncio
async def test_callback_reapplies_sanitizer_before_recovery_redirect() -> None:
    settings = _settings()
    login_states = MemoryLoginStateBackend()
    auth = ObservatoryAuth(settings, MemorySessionBackend(), login_states)
    await login_states.put(
        "raw-state",
        LoginState(
            "raw-nonce",
            "/runs?client_secret=PRIVATE_SECRET&request_uri=PRIVATE_REQUEST&"
            "from=2026-01-01&to=2026-01-02&filter=recent&document=PRIVATE_DOCUMENT&"
            "payload=PRIVATE_PAYLOAD#private-fragment",
            datetime.now(UTC) + timedelta(minutes=5),
        ),
        300,
    )

    async with await _client(auth, settings) as client:
        response = await client.get(
            "/api/v1/auth/callback",
            params={"state": "raw-state", "error": "PRIVATE_PROVIDER_ERROR"},
        )

    assert response.status_code == 303
    assert (
        _location(response)
        == "/runs?from=2026-01-01&to=2026-01-02&filter=recent&auth_recovery=provider_denied"
    )
    assert "PRIVATE_SECRET" not in response.headers["location"]
    assert "PRIVATE_" not in response.headers["location"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("query", "marker"),
    [
        ("", "state_missing"),
        ("state=expired-state", "state_invalid"),
    ],
)
async def test_missing_or_expired_state_returns_sanitized_same_origin_recovery(
    query: str, marker: str
) -> None:
    settings = _settings()
    auth = ObservatoryAuth(settings, MemorySessionBackend(), MemoryLoginStateBackend())
    app = create_app(settings, auth=auth, testing=True)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=settings.public_origin,
        follow_redirects=False,
    ) as client:
        response = await client.get(f"/api/v1/auth/callback?{query}")

    assert response.status_code == 303
    assert _location(response) == f"/?auth_recovery={marker}"
    _assert_clears_cookie(response, settings)
    assert "expired-state" not in response.text
    expected = ("login_state_store", "missing" if marker == "state_missing" else "invalid")
    assert _callback_metric_labels(app) == {expected}


@pytest.mark.asyncio
async def test_provider_denial_uses_stored_local_path_and_never_reflects_provider_values() -> None:
    settings = _settings()
    auth = ObservatoryAuth(settings, MemorySessionBackend(), MemoryLoginStateBackend())
    state = await _state(
        auth,
        "/runs?filter=recent&code=provider-code&state=provider-state&auth_recovery=attacker-value",
    )

    async with await _client(auth, settings) as client:
        response = await client.get(
            "/api/v1/auth/callback",
            params={
                "state": state,
                "error": "access_denied",
                "error_description": "PROVIDER_SECRET_DESCRIPTION",
                "error_uri": "https://provider.invalid/private",
            },
        )

    assert response.status_code == 303
    assert _location(response) == "/runs?filter=recent&auth_recovery=provider_denied"
    assert "PROVIDER_SECRET_DESCRIPTION" not in response.text
    assert "provider.invalid" not in response.headers["location"]
    _assert_clears_cookie(response, settings)


@pytest.mark.asyncio
async def test_missing_code_is_distinct_and_keeps_only_bounded_local_return_path() -> None:
    settings = _settings()
    auth = ObservatoryAuth(settings, MemorySessionBackend(), MemoryLoginStateBackend())
    state = await _state(auth, "/runs?filter=recent")

    app = create_app(settings, auth=auth, testing=True)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=settings.public_origin,
        follow_redirects=False,
    ) as client:
        response = await client.get("/api/v1/auth/callback", params={"state": state})

    assert response.status_code == 303
    assert _location(response) == "/runs?filter=recent&auth_recovery=code_missing"
    _assert_clears_cookie(response, settings)
    assert _callback_metric_labels(app) == {("provider", "code_missing")}


@pytest.mark.asyncio
async def test_token_claim_or_session_failure_is_sanitized_and_clears_stale_cookie() -> None:
    settings = _settings()
    sessions = MemorySessionBackend()
    auth = ObservatoryAuth(settings, sessions, MemoryLoginStateBackend())
    state = await _state(auth, "/traces?selected=trace")

    async def fail_authenticate(
        _code: str, _nonce: str
    ) -> tuple[dict[str, str], Mapping[str, object]]:
        raise RuntimeError("PRIVATE_PROVIDER_EXCEPTION")

    auth.authenticate_code = fail_authenticate  # type: ignore[method-assign]
    app = create_app(settings, auth=auth, testing=True)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=settings.public_origin,
        follow_redirects=False,
    ) as client:
        client.cookies.set(settings.effective_session_cookie_name, "stale-session")
        response = await client.get(
            "/api/v1/auth/callback",
            params={"state": state, "code": "PRIVATE_AUTHORIZATION_CODE"},
        )

    assert response.status_code == 303
    assert _location(response) == "/traces?selected=trace&auth_recovery=callback_failed"
    assert "PRIVATE_PROVIDER_EXCEPTION" not in response.text
    assert "PRIVATE_AUTHORIZATION_CODE" not in response.headers["location"]
    _assert_clears_cookie(response, settings)
    assert _callback_metric_labels(app) == {("provider", "exchange_failed")}


@pytest.mark.asyncio
async def test_successful_callback_is_unchanged_and_rotates_prior_session() -> None:
    settings = _settings()
    sessions = MemorySessionBackend()
    auth = ObservatoryAuth(settings, sessions, MemoryLoginStateBackend())
    old_session, _ = await auth.create_session(
        auth.authorize_claims(
            {
                "iss": settings.oidc_issuer,
                "sub": settings.owner_subject,
                "aud": [settings.oidc_audience],
            }
        )
    )
    state, nonce = await auth.begin_login("/runs?status=running")

    async def authenticate(
        _code: str, received_nonce: str
    ) -> tuple[dict[str, str], Mapping[str, object]]:
        assert received_nonce == nonce
        return {}, {
            "iss": settings.oidc_issuer,
            "sub": settings.owner_subject,
            "aud": [settings.oidc_audience],
        }

    auth.authenticate_code = authenticate  # type: ignore[method-assign]
    async with await _client(auth, settings) as client:
        client.cookies.set(settings.effective_session_cookie_name, old_session)
        response = await client.get(
            "/api/v1/auth/callback",
            params={"state": state, "code": "one-time-code"},
        )

    assert response.status_code == 303
    assert _location(response) == "/runs?status=running"
    assert "auth_recovery" not in response.headers["location"]
    assert "PRIVATE_" not in response.headers["location"]
    assert await auth.get_session(old_session) is None
    set_cookie = response.headers["set-cookie"]
    assert f"{settings.effective_session_cookie_name}=" in set_cookie
    assert "Max-Age=0" not in set_cookie


def test_callback_is_excluded_from_openapi_and_manifest_declares_recovery_evaluator() -> None:
    settings = _settings()
    auth = ObservatoryAuth(settings, MemorySessionBackend(), MemoryLoginStateBackend())
    app = create_app(settings, auth=auth, testing=True)
    assert "/api/v1/auth/callback" not in app.openapi()["paths"]

    manifest_path = (
        Path(__file__).parents[1] / "resources/component-manifests/query-api.yaml"
    )
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    assert "observatory_auth_recovery" in manifest["evaluators"]
    assert {
        "observatory_auth_callback_duration_ms",
        "observatory_auth_callback_outcome",
    } <= set(manifest["metrics"])
    callback_definitions = {
        item["name"]: item
        for item in manifest["metric_definitions"]
        if item["name"].startswith("observatory_auth_callback_")
    }
    assert callback_definitions["observatory_auth_callback_outcome"]["labels"] == [
        "stage",
        "outcome",
    ]
    assert callback_definitions["observatory_auth_callback_duration_ms"]["dependencies"] == [
        "oidc_provider",
        "login_state_store",
        "session_store",
    ]
    assert all(
        definition["trace"] == "auth_callback"
        and definition["correlation"] == "request_id"
        for definition in callback_definitions.values()
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ({}, ("login_state_store", "missing")),
        ({"state": "unknown-state"}, ("login_state_store", "invalid")),
    ],
)
async def test_callback_recovery_metrics_are_finite_and_content_free(
    query: dict[str, str], expected: tuple[str, str]
) -> None:
    settings = _settings()
    auth = ObservatoryAuth(settings, MemorySessionBackend(), MemoryLoginStateBackend())
    app = create_app(settings, auth=auth, testing=True)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=settings.public_origin,
        follow_redirects=False,
    ) as client:
        response = await client.get("/api/v1/auth/callback", params=query)

    assert response.status_code == 303
    values = app.state.query.metrics.values
    callback_series = [
        (name, dict(labels), value)
        for (name, labels), value in values.items()
        if name.startswith("observatory_auth_callback_")
    ]
    assert {name for name, _labels, _value in callback_series} == {
        "observatory_auth_callback_duration_ms",
        "observatory_auth_callback_outcome",
    }
    assert all(set(labels) == {"stage", "outcome"} for _name, labels, _value in callback_series)
    assert all(
        labels == {"stage": expected[0], "outcome": expected[1]}
        for _name, labels, _value in callback_series
    )
    assert "PRIVATE" not in repr(callback_series)


@pytest.mark.asyncio
async def test_callback_metrics_cover_success_and_provider_denial_without_input_values() -> None:
    settings = _settings()

    denial_auth = ObservatoryAuth(settings, MemorySessionBackend(), MemoryLoginStateBackend())
    denial_state = await _state(denial_auth, "/runs?filter=recent")
    denial_app = create_app(settings, auth=denial_auth, testing=True)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=denial_app),
        base_url=settings.public_origin,
        follow_redirects=False,
    ) as client:
        denial = await client.get(
            "/api/v1/auth/callback",
            params={
                "state": denial_state,
                "error": "PRIVATE_PROVIDER_ERROR",
                "error_description": "PRIVATE_PROVIDER_DESCRIPTION",
            },
        )

    success_auth = ObservatoryAuth(settings, MemorySessionBackend(), MemoryLoginStateBackend())
    success_state, nonce = await success_auth.begin_login("/runs")

    async def authenticate(
        _code: str, received_nonce: str
    ) -> tuple[dict[str, str], Mapping[str, object]]:
        assert received_nonce == nonce
        return {}, {
            "iss": settings.oidc_issuer,
            "sub": settings.owner_subject,
            "aud": [settings.oidc_audience],
        }

    success_auth.authenticate_code = authenticate  # type: ignore[method-assign]
    success_app = create_app(settings, auth=success_auth, testing=True)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=success_app),
        base_url=settings.public_origin,
        follow_redirects=False,
    ) as client:
        success = await client.get(
            "/api/v1/auth/callback",
            params={"state": success_state, "code": "PRIVATE_CODE"},
        )

    assert denial.status_code == 303
    assert success.status_code == 303
    for app, expected in (
        (denial_app, ("provider", "denied")),
        (success_app, ("callback", "success")),
    ):
        series = app.state.query.metrics.snapshot()
        assert {
            (item["labels"]["stage"], item["labels"]["outcome"])
            for item in series.values()
            if item["name"].startswith("observatory_auth_callback_")
        } == {expected}
        assert "PRIVATE_PROVIDER" not in repr(series)
        assert "PRIVATE_CODE" not in repr(series)


@pytest.mark.asyncio
async def test_callback_metrics_distinguish_state_store_unavailability() -> None:
    settings = _settings()

    class UnavailableLoginStateBackend(MemoryLoginStateBackend):
        async def consume(self, state: str) -> LoginState | None:
            del state
            raise RuntimeError("PRIVATE_STATE_STORE_ERROR")

    auth = ObservatoryAuth(
        settings,
        MemorySessionBackend(),
        UnavailableLoginStateBackend(),
    )
    app = create_app(settings, auth=auth, testing=True)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=settings.public_origin,
        follow_redirects=False,
    ) as client:
        response = await client.get(
            "/api/v1/auth/callback",
            params={"state": "state-store-failure"},
        )

    assert response.status_code == 303
    assert _callback_metric_labels(app) == {("login_state_store", "unavailable")}
    assert "PRIVATE_STATE_STORE_ERROR" not in repr(app.state.query.metrics.snapshot())


@pytest.mark.asyncio
async def test_callback_metrics_distinguish_authorization_rejection() -> None:
    settings = _settings()
    auth = ObservatoryAuth(settings, MemorySessionBackend(), MemoryLoginStateBackend())
    state = await _state(auth, "/runs")

    async def authenticate(
        _code: str, _nonce: str
    ) -> tuple[dict[str, str], Mapping[str, object]]:
        return {}, {
            "iss": settings.oidc_issuer,
            "sub": "not-the-owner",
            "aud": [settings.oidc_audience],
        }

    auth.authenticate_code = authenticate  # type: ignore[method-assign]
    app = create_app(settings, auth=auth, testing=True)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=settings.public_origin,
        follow_redirects=False,
    ) as client:
        response = await client.get(
            "/api/v1/auth/callback",
            params={"state": state, "code": "PRIVATE_CODE"},
        )

    assert response.status_code == 303
    assert _callback_metric_labels(app) == {("authorization", "rejected")}
    assert "not-the-owner" not in repr(app.state.query.metrics.snapshot())


@pytest.mark.asyncio
async def test_callback_metrics_distinguish_session_store_failure() -> None:
    settings = _settings()

    class UnavailableSessionBackend(MemorySessionBackend):
        async def put(self, session_id: str, session: Any, ttl_seconds: int) -> None:
            del session_id, session, ttl_seconds
            raise RuntimeError("PRIVATE_SESSION_STORE_ERROR")

    auth = ObservatoryAuth(
        settings,
        UnavailableSessionBackend(),
        MemoryLoginStateBackend(),
    )
    state, nonce = await auth.begin_login("/runs")

    async def authenticate(
        _code: str, received_nonce: str
    ) -> tuple[dict[str, str], Mapping[str, object]]:
        assert received_nonce == nonce
        return {}, {
            "iss": settings.oidc_issuer,
            "sub": settings.owner_subject,
            "aud": [settings.oidc_audience],
        }

    auth.authenticate_code = authenticate  # type: ignore[method-assign]
    app = create_app(settings, auth=auth, testing=True)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=settings.public_origin,
        follow_redirects=False,
    ) as client:
        response = await client.get(
            "/api/v1/auth/callback",
            params={"state": state, "code": "PRIVATE_CODE"},
        )

    assert response.status_code == 303
    assert _callback_metric_labels(app) == {("session_store", "failed")}
    assert "PRIVATE_SESSION_STORE_ERROR" not in repr(app.state.query.metrics.snapshot())
