"""Environment and file-injected settings for the Observatory service."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AURA_OBSERVATORY_", extra="ignore")

    ingest_credential_file: Path | None = Field(default=None)
    database_url: str = "postgresql://localhost/aura_observatory"
    clickhouse_url: str = "http://localhost:8123"
    environment: str = "development"
    deployment_commit: str = "0000000"
    observation_claim_lease_seconds: int = Field(default=60, ge=1, le=3600)
    ingest_max_batch_size: int = Field(default=500, ge=1, le=5000)
    ingest_max_payload_bytes: int = Field(default=1_000_000, ge=1024, le=10_000_000)

    def ingest_credential(self) -> str | None:
        if self.ingest_credential_file is None:
            return None
        try:
            token = self.ingest_credential_file.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        return token or None


__all__ = ["Settings"]
