"""Environment and file-injected settings for the Observatory service.

Observatory deliberately has its own settings namespace.  In particular, its
OIDC client, session store and ingest credential must never accidentally pick
up Aura Core's values from the process environment.
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import quote, urlsplit

from pydantic import AliasChoices, Field, field_validator, model_validator
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
        validation_alias=AliasChoices("AURA_OBSERVATORY_SECURE_COOKIES", "AURA_SECURE_COOKIES"),
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
    metrics_file: Path | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "AURA_OBSERVATORY_METRICS_FILE", "AURA_OBSERVATORY_METRICS_PATH"
        ),
    )
    collector_endpoint: str = "http://observatory-collector:4318"
    telemetry_otlp_enabled: bool = False
    telemetry_otlp_endpoint: str = ""
    telemetry_otlp_queue_capacity: int = Field(default=32, ge=1, le=1024)
    telemetry_otlp_max_attempts: int = Field(default=3, ge=1, le=5)
    telemetry_otlp_timeout_seconds: float = Field(default=2.0, gt=0, le=30)
    observation_claim_lease_seconds: int = Field(default=60, ge=1, le=3600)
    ingest_max_batch_size: int = Field(default=500, ge=1, le=5000)
    ingest_max_payload_bytes: int = Field(default=1_000_000, ge=1024, le=10_000_000)
    # Evaluation artifacts are a separate, least-privilege Garage boundary.
    # These values are deliberately worker-only settings; the API composition
    # does not construct an object-store adapter.
    evaluation_object_endpoint: str = "http://garage:3900"
    evaluation_object_bucket: str = "aura-observatory-evaluation"
    evaluation_object_access_key: str = ""
    evaluation_object_access_key_file: Path | None = None
    evaluation_object_secret_key: str = ""
    evaluation_object_secret_key_file: Path | None = None
    evaluation_object_region: str = "garage"
    evaluation_object_timeout_seconds: float = Field(default=30.0, ge=1.0, le=120.0)
    evaluation_artifact_max_bytes: int = Field(default=16 * 1024 * 1024, ge=1, le=1_073_741_824)
    evaluation_deletion_page_size: int = Field(default=100, ge=1, le=1000)
    evaluation_deletion_poll_seconds: float = Field(default=5.0, ge=0.1, le=300.0)
    # AURA-0092 orchestration is isolated from telemetry ingestion.  The
    # scheduler never reads these NATS settings or receives a broker secret.
    evaluation_nats_url: str = "nats://aura-nats:4222"
    evaluation_nats_user: str = "aura-observatory-dispatch"
    evaluation_nats_password: str = ""
    evaluation_nats_password_file: Path | None = None
    evaluation_worker_id: str = "00000000-0000-4000-8000-000000000092"
    evaluation_scheduler_id: str = "00000000-0000-4000-8000-000000000093"
    evaluation_batch_size: int = Field(default=32, ge=1, le=1000)
    evaluation_poll_seconds: float = Field(default=2.0, ge=0.1, le=300.0)
    evaluation_shutdown_timeout_seconds: float = Field(default=5.0, ge=0.1, le=60.0)
    evaluation_lease_seconds: int = Field(default=120, ge=1, le=3600)
    evaluation_max_attempts: int = Field(default=3, ge=1, le=10)
    evaluation_retry_base_seconds: int = Field(default=10, ge=1, le=3600)
    evaluation_retry_max_seconds: int = Field(default=3600, ge=1, le=86400)
    evaluation_max_envelope_bytes: int = Field(default=1_000_000, ge=1024, le=10_000_000)
    # AURA-0094 worker-only local judge boundary.  The origin is fixed by
    # deployment configuration and is never accepted from a request.
    local_judge_origin: str = "http://ollama:11434"
    local_judge_timeout_seconds: float = Field(default=120.0, ge=0.1, le=120.0)
    local_judge_max_concurrency: int = Field(default=1, ge=1, le=2)
    judge_object_access_key: str = ""
    judge_object_access_key_file: Path | None = None
    judge_object_secret_key: str = ""
    judge_object_secret_key_file: Path | None = None

    @field_validator("evaluation_worker_id", "evaluation_scheduler_id")
    @classmethod
    def validate_evaluation_process_id(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}", value):
            raise ValueError("evaluation process id must be a bounded identifier")
        return value

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
        self.evaluation_object_access_key = read_secret(
            self.evaluation_object_access_key, self.evaluation_object_access_key_file
        )
        self.evaluation_object_secret_key = read_secret(
            self.evaluation_object_secret_key, self.evaluation_object_secret_key_file
        )
        self.evaluation_nats_password = read_secret(
            self.evaluation_nats_password, self.evaluation_nats_password_file
        )
        self.judge_object_access_key = read_secret(
            self.judge_object_access_key, self.judge_object_access_key_file
        )
        self.judge_object_secret_key = read_secret(
            self.judge_object_secret_key, self.judge_object_secret_key_file
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
                and self.oidc_redirect_uri
                == "http://observatory.localhost:4300/api/v1/auth/callback"
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

    def evaluation_nats_credential(self) -> tuple[str, str]:
        """Return the least-privilege dispatch identity for the worker only."""

        return self.evaluation_nats_user, self.evaluation_nats_password

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
