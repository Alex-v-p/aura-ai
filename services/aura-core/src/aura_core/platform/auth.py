"""OIDC BFF session primitives. Provider tokens never leave this module."""

import hashlib
import ipaddress
import math
import os
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Protocol, cast
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from aura_core.domains.governance.identity.models import (
    OidcLoginState,
    Principal,
    ProviderTokens,
    Session,
)

_TELEMETRY_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
_TELEMETRY_ENVIRONMENT = re.compile(r"^[a-z][a-z0-9_.-]{0,31}$")
_TELEMETRY_COMMIT = re.compile(r"^[0-9a-f]{7,64}$")
_TELEMETRY_REVISION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
_TELEMETRY_PRIVATE_SHAPES = (
    "password",
    "passwd",
    "secret",
    "token",
    "credential",
    "credentials",
    "bearer",
    "owner",
    "identity",
    "subject",
    "private",
    "session",
    "cookie",
    "endpoint",
    "api_key",
    "api-key",
    "prompt",
    "response",
    "memory",
    "document",
    "filename",
    "argument",
    "result",
    "content",
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
    # Observatory export is deliberately opt-in.  The structured container
    # exporter remains enabled independently when this path is disabled or
    # unavailable.
    telemetry_otlp_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices(
            "AURA_TELEMETRY_OTLP_ENABLED", "AURA_OBSERVATORY_EXPORT_ENABLED"
        ),
    )
    telemetry_otlp_endpoint: str = Field(
        default="http://127.0.0.1:4318",
        validation_alias=AliasChoices(
            "AURA_TELEMETRY_OTLP_ENDPOINT", "AURA_OBSERVATORY_EXPORT_ENDPOINT"
        ),
    )
    telemetry_otlp_credential: str = Field(default="", repr=False)
    telemetry_otlp_credential_file: str = Field(default="", repr=False)
    telemetry_otlp_queue_capacity: int = Field(default=32, ge=1, le=1024)
    telemetry_otlp_max_attempts: int = Field(default=3, ge=1, le=5)
    telemetry_otlp_timeout_seconds: float = Field(default=2.0, gt=0, le=30)
    telemetry_deployment_id: str = Field(default="local")
    telemetry_git_commit: str = Field(default="0000000")
    telemetry_host_id: str = Field(default_factory=lambda: os.environ.get("HOSTNAME", "local"))
    telemetry_image_version: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "AURA_TELEMETRY_IMAGE_VERSION", "AURA_OBSERVATORY_IMAGE_VERSION"
        ),
    )
    telemetry_configuration_version: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "AURA_TELEMETRY_CONFIGURATION_VERSION", "AURA_OBSERVATORY_CONFIGURATION_VERSION"
        ),
    )
    public_origin: str = "https://aura.localhost"
    ollama_url: str = Field(
        default="http://127.0.0.1:11434",
        validation_alias=AliasChoices("AURA_OLLAMA_BASE_URL", "AURA_OLLAMA_URL"),
    )
    default_model: str = Field(
        default="", validation_alias=AliasChoices("AURA_OLLAMA_DEFAULT_MODEL", "AURA_DEFAULT_MODEL")
    )
    # Document vectors use the installed Qwen Matryoshka model at the
    # dimension of the existing pgvector index.  Memory has its own model
    # selection and embedding provider composition and must not inherit this
    # document-specific setting.
    document_embedding_model: str = Field(
        default="qwen3-embedding:4b",
        validation_alias=AliasChoices("AURA_DOCUMENT_EMBEDDING_MODEL"),
    )
    document_embedding_dimension: int = Field(
        default=768,
        validation_alias=AliasChoices("AURA_DOCUMENT_EMBEDDING_DIMENSION"),
    )
    context_token_budget: int = Field(default=8192, gt=0)
    ollama_run_timeout_seconds: float = 300.0
    # Inventory reads use provider metadata and should fail quickly without
    # inheriting the much longer inference deadline. Selecting a memory model
    # may perform a separate bounded provider verification, and a short cache
    # prevents duplicate scans during one UI refresh.
    ollama_inventory_timeout_seconds: float = Field(default=5.0, gt=0, le=60)
    ollama_verification_timeout_seconds: float = Field(default=60.0, gt=0, le=300)
    ollama_inventory_cache_ttl_seconds: float = Field(default=15.0, ge=0, le=300)
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
    object_storage_endpoint: str = Field(
        default="http://127.0.0.1:3900",
        validation_alias=AliasChoices("AURA_OBJECT_STORAGE_ENDPOINT", "AURA_DOCUMENT_STORE_URL"),
    )
    object_storage_region: str = Field(
        default="garage",
        validation_alias=AliasChoices("AURA_OBJECT_STORAGE_REGION", "AURA_GARAGE_REGION"),
    )
    object_storage_bucket: str = Field(
        default="aura-documents",
        validation_alias=AliasChoices("AURA_OBJECT_STORAGE_BUCKET", "AURA_GARAGE_BUCKET"),
    )
    object_storage_access_key: str = Field(
        default="",
        validation_alias=AliasChoices("AURA_OBJECT_STORAGE_ACCESS_KEY"),
    )
    object_storage_access_key_file: str = Field(
        default="",
        validation_alias=AliasChoices("AURA_OBJECT_STORAGE_ACCESS_KEY_FILE"),
    )
    object_storage_secret_key: str = Field(
        default="",
        validation_alias=AliasChoices("AURA_OBJECT_STORAGE_SECRET_KEY"),
    )
    object_storage_secret_key_file: str = Field(
        default="",
        validation_alias=AliasChoices("AURA_OBJECT_STORAGE_SECRET_KEY_FILE"),
    )
    document_parser_url: str = Field(
        default="http://document-parser:8080",
        validation_alias=AliasChoices("AURA_DOCUMENT_PARSER_URL"),
    )
    secure_cookies: bool = Field(
        default=True,
        validation_alias=AliasChoices("AURA_SESSION_COOKIE_SECURE", "AURA_SECURE_COOKIES"),
    )

    @field_validator("document_embedding_dimension")
    @classmethod
    def validate_document_embedding_dimension(cls, value: int) -> int:
        if value != 768:
            raise ValueError("document embedding dimension must be exactly 768")
        return value

    @field_validator("telemetry_deployment_id", "telemetry_host_id")
    @classmethod
    def validate_telemetry_safe_name(cls, value: str) -> str:
        lowered = value.casefold()
        if (
            not _TELEMETRY_SAFE_NAME.fullmatch(value)
            or any(character.isspace() for character in value)
            or "://" in value
            or "/" in value
            or "\\" in value
            or "@" in value
            or any(shape in lowered for shape in _TELEMETRY_PRIVATE_SHAPES)
        ):
            raise ValueError("telemetry metadata must be a bounded non-sensitive identifier")
        return value

    @field_validator("telemetry_git_commit")
    @classmethod
    def validate_telemetry_commit(cls, value: str) -> str:
        if not _TELEMETRY_COMMIT.fullmatch(value):
            raise ValueError("telemetry git commit must be 7-64 lowercase hexadecimal characters")
        return value

    @field_validator("telemetry_image_version", "telemetry_configuration_version", mode="before")
    @classmethod
    def validate_telemetry_revision(cls, value: object) -> str | None:
        # Compose commonly supplies an empty optional marker. Treat it as
        # absent while failing closed for every non-empty value.
        if value is None or value == "":
            return None
        if not isinstance(value, str):
            raise ValueError("telemetry revision must be a bounded identifier")
        lowered = value.casefold()
        if (
            not _TELEMETRY_REVISION.fullmatch(value)
            or any(character.isspace() for character in value)
            or "://" in value
            or "/" in value
            or "\\" in value
            or "@" in value
            or any(shape in lowered for shape in _TELEMETRY_PRIVATE_SHAPES)
        ):
            raise ValueError("telemetry revision must be a bounded non-sensitive identifier")
        return value

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
        self.telemetry_otlp_credential = read_secret(
            self.telemetry_otlp_credential, self.telemetry_otlp_credential_file
        )
        telemetry_endpoint = urlsplit(self.telemetry_otlp_endpoint)
        if (
            telemetry_endpoint.scheme not in {"http", "https"}
            or telemetry_endpoint.hostname is None
            or telemetry_endpoint.username is not None
            or telemetry_endpoint.password is not None
            or telemetry_endpoint.fragment
        ):
            raise ValueError("telemetry OTLP endpoint must not contain credentials or fragments")
        if not _TELEMETRY_ENVIRONMENT.fullmatch(self.environment) or any(
            shape in self.environment.casefold() for shape in _TELEMETRY_PRIVATE_SHAPES
        ):
            raise ValueError("telemetry environment must be a bounded lower-case identifier")
        self.nats_password = read_secret(self.nats_password, self.nats_password_file)
        self.valkey_password = read_secret(self.valkey_password, self.valkey_password_file)
        self.object_storage_access_key = read_secret(
            self.object_storage_access_key, self.object_storage_access_key_file
        )
        self.object_storage_secret_key = read_secret(
            self.object_storage_secret_key, self.object_storage_secret_key_file
        )
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
            expected_redirect = f"{self.public_origin.rstrip('/')}/api/v1/auth/callback"
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
