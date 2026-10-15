"""Environment and file-injected settings for the Observatory service.

Observatory deliberately has its own settings namespace.  In particular, its
OIDC client, session store and ingest credential must never accidentally pick
up Aura Core's values from the process environment.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote, urlsplit

from pydantic import AliasChoices, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AURA_OBSERVATORY_", extra="ignore", populate_by_name=True
    )

    environment: str = "disposable_local"
    role: str = "api"
    public_origin: str = "http://observatory.localhost:4300"
    oidc_issuer: str = "https://authentik.example.invalid/application/o/aura-observatory/"
    oidc_audience: str = "aura-observatory"
    oidc_client_id: str = "aura-observatory"
    oidc_client_secret: str = ""
    oidc_client_secret_file: Path | None = None
    oidc_redirect_uri: str = "http://observatory.localhost:4300/api/v1/auth/callback"
    owner_subject: str = ""
    secure_cookies: bool = Field(
        default=False,
        validation_alias=AliasChoices(
            "AURA_OBSERVATORY_SECURE_COOKIES", "AURA_SECURE_COOKIES"
        ),
    )
    session_cookie_name: str = "__Host-observatory_session"
    session_store_url: str = "redis://observatory-session:6379/0"
    session_store_password: str = ""
    session_store_password_file: Path | None = None
    session_namespace: str = "aura:observatory"
    session_ttl_seconds: int = Field(default=86_400, ge=300, le=2_592_000)
    login_state_ttl_seconds: int = Field(default=300, ge=60, le=900)
    csrf_header: str = "X-Observatory-CSRF"
    ingest_credential_file: Path | None = Field(default=None)
    database_url: str = ""
    database_host: str = "observatory-postgres"
    database_port: int = 5432
    database_name: str = "aura_observatory"
    database_user: str = "observatory"
    database_password: str = ""
    database_password_file: Path | None = None
    clickhouse_url: str = "http://localhost:8123"
    clickhouse_database: str = "aura_observatory"
    clickhouse_user: str = "default"
    clickhouse_password: str = ""
    clickhouse_password_file: Path | None = None
    deployment_commit: str = "0000000"
    deployment_id: str = "local"
    deployment_host: str = "local"
    service_role: str = "api"
    collector_endpoint: str = "http://observatory-collector:4318"
    observation_claim_lease_seconds: int = Field(default=60, ge=1, le=3600)
    ingest_max_batch_size: int = Field(default=500, ge=1, le=5000)
    ingest_max_payload_bytes: int = Field(default=1_000_000, ge=1024, le=10_000_000)

    @model_validator(mode="after")
    def compose_runtime_settings(self) -> Settings:
        self.oidc_client_secret = read_secret(self.oidc_client_secret, self.oidc_client_secret_file)
        self.session_store_password = read_secret(
            self.session_store_password, self.session_store_password_file
        )
        self.database_password = read_secret(self.database_password, self.database_password_file)
        self.clickhouse_password = read_secret(
            self.clickhouse_password, self.clickhouse_password_file
        )
        if not self.database_url:
            encoded = quote(self.database_password, safe="")
            self.database_url = (
                f"postgresql://{quote(self.database_user, safe='')}:{encoded}"
                f"@{self.database_host}:{self.database_port}/{self.database_name}"
            )
        if self.role == "api":
            disposable_local = (
                self.environment == "disposable_local"
                and self.public_origin == "http://observatory.localhost:4300"
                and self.oidc_redirect_uri == "http://observatory.localhost:4300/api/v1/auth/callback"
                and not self.secure_cookies
            )
            if not disposable_local:
                validate_stable_https_url(self.public_origin, "public origin", origin_only=True)
                validate_stable_https_url(self.oidc_issuer, "OIDC issuer")
                expected = f"{self.public_origin.rstrip('/')}/api/v1/auth/callback"
                if self.oidc_redirect_uri != expected:
                    raise ValueError("OIDC redirect URI must match the public callback URL")
                if not self.secure_cookies:
                    raise ValueError("secure session cookies are required in production")
                if not self.owner_subject.strip():
                    raise ValueError("owner subject is required outside disposable local mode")
            if self.secure_cookies and not self.session_cookie_name.startswith("__Host-"):
                raise ValueError("secure Observatory cookies must use a __Host- name")
        return self

    def ingest_credential(self) -> str | None:
        if self.ingest_credential_file is None:
            return None
        try:
            token = self.ingest_credential_file.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        return token or None

    @property
    def oidc_discovery_url(self) -> str:
        return self.oidc_issuer.rstrip("/") + "/.well-known/openid-configuration"

    @property
    def effective_session_cookie_name(self) -> str:
        if self.secure_cookies:
            return self.session_cookie_name
        return self.session_cookie_name.removeprefix("__Host-")


def read_secret(value: str, file_path: Path | None) -> str:
    """Read a Docker/SOPS file secret without putting its value in settings files."""

    if value:
        return value
    if file_path is None:
        return ""
    try:
        return file_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ValueError(f"configured secret file is unavailable: {file_path}") from exc


def validate_stable_https_url(value: str, label: str, *, origin_only: bool = False) -> None:
    parsed = urlsplit(value)
    host = parsed.hostname
    if parsed.scheme != "https" or host is None:
        raise ValueError(f"{label} must be an absolute HTTPS URL")
    lowered = host.casefold().rstrip(".")
    if lowered == "localhost" or lowered.endswith(".localhost"):
        raise ValueError(f"{label} must not use localhost")
    if parsed.username or parsed.password or parsed.fragment:
        raise ValueError(f"{label} must not include credentials or a fragment")
    if origin_only and (parsed.path not in {"", "/"} or parsed.query):
        raise ValueError(f"{label} must be an origin without a path or query")


__all__ = ["Settings", "read_secret", "validate_stable_https_url"]
