"""OIDC BFF session primitives. Provider tokens never leave this module."""

import hashlib
import ipaddress
import math
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Protocol, cast
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from pydantic import AliasChoices, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from aura_core.domains.governance.identity.models import (
    OidcLoginState,
    Principal,
    ProviderTokens,
    Session,
)


@dataclass(frozen=True, slots=True)
class SessionCookiePolicy:
    name: str
    secure: bool
    httponly: bool = True
    samesite: Literal["lax", "strict", "none"] = "lax"
    path: str = "/"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AURA_", extra="ignore", populate_by_name=True)

    environment: str = "development"
    role: str = "api"
    public_origin: str = "https://aura.localhost"
    ollama_url: str = Field(
        default="http://127.0.0.1:11434",
        validation_alias=AliasChoices("AURA_OLLAMA_BASE_URL", "AURA_OLLAMA_URL"),
    )
    default_model: str = Field(
        default="", validation_alias=AliasChoices("AURA_OLLAMA_DEFAULT_MODEL", "AURA_DEFAULT_MODEL")
    )
    context_token_budget: int = Field(default=8192, gt=0)
    ollama_run_timeout_seconds: float = 300.0
    database_url: str = ""
    database_host: str = "127.0.0.1"
    database_port: int = 5432
    database_name: str = "aura"
    database_user: str = "aura"
    database_password: str = ""
    database_password_file: str = ""
    nats_url: str = "nats://127.0.0.1:4222"
    nats_user: str = ""
    nats_password: str = ""
    nats_password_file: str = ""
    valkey_url: str = "redis://127.0.0.1:6379/0"
    valkey_user: str = ""
    valkey_password: str = ""
    valkey_password_file: str = ""
    oidc_issuer: str = "https://authentik.local"
    oidc_client_id: str = ""
    oidc_client_secret: str = ""
    oidc_client_secret_file: str = ""
    oidc_audience: str = ""
    oidc_redirect_uri: str = "https://aura.local/api/v1/auth/callback"
    owner_subject: str = ""
    secure_cookies: bool = Field(
        default=True,
        validation_alias=AliasChoices("AURA_SESSION_COOKIE_SECURE", "AURA_SECURE_COOKIES"),
    )

    @property
    def session_cookie(self) -> SessionCookiePolicy:
        return SessionCookiePolicy(
            name="__Host-aura_session" if self.secure_cookies else "aura_session",
            secure=self.secure_cookies,
        )

    @model_validator(mode="after")
    def compose_runtime_secrets(self) -> Settings:
        password = read_secret(self.database_password, self.database_password_file)
        self.database_password = password
        self.oidc_client_secret = read_secret(self.oidc_client_secret, self.oidc_client_secret_file)
        self.nats_password = read_secret(self.nats_password, self.nats_password_file)
        self.valkey_password = read_secret(self.valkey_password, self.valkey_password_file)
        if not self.database_url:
            self.database_url = (
                f"postgresql+psycopg://{quote(self.database_user)}:{quote(password, safe='')}"
                f"@{self.database_host}:{self.database_port}/{self.database_name}"
            )
        self.nats_url = credential_url(self.nats_url, self.nats_user, self.nats_password)
        self.valkey_url = credential_url(self.valkey_url, self.valkey_user, self.valkey_password)
        if self.environment.casefold() in {"prod", "production"} and self.role == "api":
            validate_stable_https_url(self.oidc_issuer, "OIDC issuer")
            validate_stable_https_url(self.public_origin, "public origin", origin_only=True)
            expected_redirect = (
                f"{self.public_origin.rstrip('/')}/api/v1/auth/callback"
            )
            if self.oidc_redirect_uri != expected_redirect:
                raise ValueError("OIDC redirect URI must match the public callback URL")
            if not self.secure_cookies:
                raise ValueError("secure session cookies are required in production")
            if not self.owner_subject.strip():
                raise ValueError("owner subject is required in production")
        return self


def read_secret(value: str, file_path: str) -> str:
    """Resolve a value with Docker/SOPS ``*_FILE`` support.

    The file form wins only when the inline value is absent.  This keeps local
    development convenient while ensuring production containers never need to
    put credentials in their environment.
    """

    if value:
        return value
    if not file_path:
        return ""
    try:
        return Path(file_path).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ValueError(f"configured secret file is unavailable: {file_path}") from exc


def credential_url(value: str, username: str, password: str) -> str:
    """Inject runtime credentials into a NATS/Valkey URL without logging them."""

    if not username and not password:
        return value
    parsed = urlsplit(value)
    if not parsed.scheme or parsed.hostname is None:
        raise ValueError("credential URL must include a scheme and host")
    effective_user = username or unquote(parsed.username or "")
    effective_password = password or unquote(parsed.password or "")
    credentials = ""
    if effective_user or effective_password:
        credentials = f"{quote(effective_user, safe='')}:{quote(effective_password, safe='')}@"
    hostname = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    port = f":{parsed.port}" if parsed.port is not None else ""
    return urlunsplit(
        (
            parsed.scheme,
            f"{credentials}{hostname}{port}",
            parsed.path,
            parsed.query,
            parsed.fragment,
        )
    )


def validate_stable_https_url(value: str, label: str, *, origin_only: bool = False) -> None:
    parsed = urlsplit(value)
    hostname = parsed.hostname
    if parsed.scheme != "https" or hostname is None:
        raise ValueError(f"{label} must be an absolute HTTPS URL")
    lowered = hostname.casefold().rstrip(".")
    is_loopback = False
    try:
        is_loopback = ipaddress.ip_address(lowered).is_loopback
    except ValueError:
        pass
    if lowered == "localhost" or lowered.endswith(".localhost") or is_loopback:
        raise ValueError(f"{label} must not use localhost or a loopback address")
    if parsed.username or parsed.password or parsed.fragment:
        raise ValueError(f"{label} must not include credentials or a fragment")
    if origin_only and (parsed.path not in {"", "/"} or parsed.query):
        raise ValueError(f"{label} must be an origin without a path or query")


class SessionBackend(Protocol):
    async def put(self, session_id: str, session: Session) -> None: ...
    async def get(self, session_id: str) -> Session | None: ...
    async def delete(self, session_id: str) -> None: ...


class LoginStateBackend(Protocol):
    async def put(self, state: str, value: OidcLoginState, ttl_seconds: int) -> None: ...
    async def consume(self, state: str) -> OidcLoginState | None: ...


class MemoryLoginStateBackend:
    def __init__(self) -> None:
        self._states: dict[str, OidcLoginState] = {}

    async def put(self, state: str, value: OidcLoginState, ttl_seconds: int) -> None:
        del ttl_seconds
        self._states[state] = value

    async def consume(self, state: str) -> OidcLoginState | None:
        value = self._states.pop(state, None)
        if value is None or value.expires_at <= datetime.now(UTC):
            return None
        return value


class RedisLoginStateBackend:
    def __init__(self, client: Any) -> None:
        self.client = client

    async def put(self, state: str, value: OidcLoginState, ttl_seconds: int) -> None:
        import json

        await self.client.setex(
            f"aura:oidc-state:{hashlib.sha256(state.encode()).hexdigest()}",
            ttl_seconds,
            json.dumps(
                {
                    "returnTo": value.return_to,
                    "nonce": value.nonce,
                    "expiresAt": value.expires_at.isoformat(),
                }
            ),
        )

    async def consume(self, state: str) -> OidcLoginState | None:
        import json

        key = f"aura:oidc-state:{hashlib.sha256(state.encode()).hexdigest()}"
        # Redis/Valkey's GETDEL is atomic and guarantees callback replay is
        # rejected even when two API workers receive it concurrently.
        value = await self.client.getdel(key)
        if value is None:
            return None
        data = json.loads(value)
        result = OidcLoginState(
            data["returnTo"], data["nonce"], datetime.fromisoformat(data["expiresAt"])
        )
        return result if result.expires_at > datetime.now(UTC) else None


class MemorySessionBackend:
    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}

    async def put(self, session_id: str, session: Session) -> None:
        self._sessions[hashlib.sha256(session_id.encode()).hexdigest()] = session

    async def get(self, session_id: str) -> Session | None:
        return self._sessions.get(hashlib.sha256(session_id.encode()).hexdigest())

    async def delete(self, session_id: str) -> None:
        self._sessions.pop(hashlib.sha256(session_id.encode()).hexdigest(), None)


class RedisSessionBackend:
    """Valkey implementation. Serialization is private and sessions expire on restart."""

    def __init__(self, client: Any) -> None:
        self.client = client

    async def put(self, session_id: str, session: Session) -> None:
        import json

        payload = json.dumps(
            {
                "issuer": session.principal.issuer,
                "subject": session.principal.subject,
                "display_name": session.principal.display_name,
                "csrf": session.csrf_token,
                "idle": session.idle_expires_at.isoformat(),
                "absolute": session.absolute_expires_at.isoformat(),
                "provider_tokens": {
                    "access_token": session.provider_tokens.access_token,
                    "refresh_token": session.provider_tokens.refresh_token,
                    "token_type": session.provider_tokens.token_type,
                }
                if session.provider_tokens is not None
                else None,
            }
        )
        ttl = max(1, math.ceil((session.absolute_expires_at - datetime.now(UTC)).total_seconds()))
        await self.client.setex(
            f"aura:session:{hashlib.sha256(session_id.encode()).hexdigest()}", ttl, payload
        )

    async def get(self, session_id: str) -> Session | None:
        import json

        value = await self.client.get(
            f"aura:session:{hashlib.sha256(session_id.encode()).hexdigest()}"
        )
        if value is None:
            return None
        data = json.loads(value)
        token_data = data.get("provider_tokens")
        token_map = cast(dict[str, object], token_data) if isinstance(token_data, dict) else None
        return Session(
            Principal(data["issuer"], data["subject"], data.get("display_name")),
            data["csrf"],
            datetime.fromisoformat(data["idle"]),
            datetime.fromisoformat(data["absolute"]),
            ProviderTokens(
                _optional_string(token_map.get("access_token")),
                _optional_string(token_map.get("refresh_token")),
                _optional_string(token_map.get("token_type")),
            )
            if token_map is not None
            else None,
        )

    async def delete(self, session_id: str) -> None:
        await self.client.delete(f"aura:session:{hashlib.sha256(session_id.encode()).hexdigest()}")


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) else None


class SessionService:
    def __init__(self, backend: SessionBackend, settings: Settings) -> None:
        self.backend = backend
        self.settings = settings

    async def create(
        self, principal: Principal, provider_tokens: ProviderTokens | None = None
    ) -> tuple[str, Session]:
        now = datetime.now(UTC)
        session = Session(
            principal,
            secrets.token_urlsafe(32),
            now + timedelta(hours=8),
            now + timedelta(hours=24),
            provider_tokens,
        )
        session_id = secrets.token_urlsafe(48)
        await self.backend.put(session_id, session)
        return session_id, session

    async def get(self, session_id: str | None) -> Session | None:
        if not session_id:
            return None
        session = await self.backend.get(session_id)
        current = datetime.now(UTC)
        if (
            session is None
            or session.absolute_expires_at <= current
            or session.idle_expires_at <= current
        ):
            if session is not None:
                await self.backend.delete(session_id)
            return None
        refreshed_idle = min(current + timedelta(hours=8), session.absolute_expires_at)
        if refreshed_idle != session.idle_expires_at:
            session = Session(
                session.principal,
                session.csrf_token,
                refreshed_idle,
                session.absolute_expires_at,
                session.provider_tokens,
            )
            await self.backend.put(session_id, session)
        return session

    async def revoke(self, session_id: str | None) -> None:
        if session_id:
            await self.backend.delete(session_id)
