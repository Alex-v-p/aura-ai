"""Fail-closed settings for the independently switchable research providers."""

from __future__ import annotations

import base64
import binascii
import os
import re
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator


def _read_key(value: str, path: str) -> bytes:
    raw = value.strip()
    if not raw and path:
        raw = Path(path).read_text(encoding="ascii").strip()
    if not raw:
        return b""
    if not re.fullmatch(r"[A-Za-z0-9_-]+", raw) or len(raw) % 4 == 1:
        raise ValueError("research provider signing key is malformed")
    try:
        decoded = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    except (ValueError, TypeError, binascii.Error) as exc:
        raise ValueError("research provider signing key is malformed") from exc
    if base64.urlsafe_b64encode(decoded).decode("ascii").rstrip("=") != raw:
        raise ValueError("research provider signing key is malformed")
    if len(decoded) != 32:
        raise ValueError("research provider signing key must contain 32 bytes")
    return decoded


class ResearchProviderSettings(BaseModel):
    """Deployment settings; values above ADR ceilings cannot be accepted."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    admission_enabled: bool = False
    search_enabled: bool = False
    static_fetch_enabled: bool = False
    browser_fetch_enabled: bool = False
    # These names are the private Compose service DNS names.  Keeping them in
    # one settings seam prevents accidental calls to the public egress
    # network or to a stale service alias.
    search_endpoint: str = "http://aura-research-searxng:8080"
    static_fetch_endpoint: str = "http://aura-research-static-fetcher:8080"
    browser_fetch_endpoint: str = "http://aura-research-browser-fetcher:8080"
    signing_key: SecretStr = SecretStr("")
    signing_key_file: str = ""
    envelope_ttl_seconds: float = Field(default=30.0, gt=0, le=60.0)
    replay_cache_capacity: int = Field(default=4096, ge=64, le=65_536)
    search_deadline_seconds: float = Field(default=15.0, gt=0, le=15.0)
    static_fetch_deadline_seconds: float = Field(default=20.0, gt=0, le=20.0)
    browser_fetch_deadline_seconds: float = Field(default=45.0, gt=0, le=45.0)
    search_concurrency: int = Field(default=8, ge=1, le=8)
    static_fetch_concurrency: int = Field(default=4, ge=1, le=4)
    browser_fetch_concurrency: int = Field(default=2, ge=1, le=2)
    max_candidates: int = Field(default=20, ge=1, le=20)
    max_redirects: int = Field(default=5, ge=0, le=5)
    max_bytes: int = Field(default=8 * 1024 * 1024, ge=1, le=8 * 1024 * 1024)
    max_extracted_text_bytes: int = Field(default=256 * 1024, ge=1, le=256 * 1024)

    @field_validator("search_endpoint", "static_fetch_endpoint", "browser_fetch_endpoint")
    @classmethod
    def validate_endpoint(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("research provider endpoint must use HTTP(S) and include a host")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("research provider endpoint cannot contain credentials or query state")
        return value.rstrip("/")

    @model_validator(mode="after")
    def validate_enabled_key(self) -> ResearchProviderSettings:
        enabled = (
            self.admission_enabled
            or self.search_enabled
            or self.static_fetch_enabled
            or self.browser_fetch_enabled
        )
        if enabled and not self.signing_key.get_secret_value() and not self.signing_key_file:
            raise ValueError("enabled research providers require a file-backed signing key")
        return self

    @property
    def any_provider_enabled(self) -> bool:
        return self.search_enabled or self.static_fetch_enabled or self.browser_fetch_enabled

    @property
    def key_bytes(self) -> bytes:
        return _read_key(self.signing_key.get_secret_value(), self.signing_key_file)

    @classmethod
    def from_environment(cls) -> ResearchProviderSettings:
        """Read the narrow provider namespace without importing Core Settings."""

        def env(name: str, default: object) -> object:
            return os.environ.get(f"AURA_RESEARCH_{name}", default)

        def boolean(name: str, default: bool = False) -> bool:
            value = str(env(name, str(default))).strip().casefold()
            if value not in {"true", "false", "1", "0", "yes", "no"}:
                raise ValueError(f"AURA_RESEARCH_{name} must be boolean")
            return value in {"true", "1", "yes"}

        return cls(
            admission_enabled=boolean("ADMISSION_ENABLED"),
            search_enabled=boolean("SEARCH_ENABLED"),
            static_fetch_enabled=boolean("STATIC_FETCH_ENABLED"),
            browser_fetch_enabled=boolean("BROWSER_FETCH_ENABLED"),
            search_endpoint=str(
                env("SEARCH_ENDPOINT", cls.model_fields["search_endpoint"].default)
            ),
            static_fetch_endpoint=str(
                env("STATIC_FETCH_ENDPOINT", cls.model_fields["static_fetch_endpoint"].default)
            ),
            browser_fetch_endpoint=str(
                env("BROWSER_FETCH_ENDPOINT", cls.model_fields["browser_fetch_endpoint"].default)
            ),
            signing_key=SecretStr(str(env("SIGNING_KEY", ""))),
            signing_key_file=str(env("SIGNING_KEY_FILE", "")),
        )


ResearchCapability = Literal["admission", "search", "static_fetch", "browser_fetch"]


def capability_enabled(settings: ResearchProviderSettings, capability: ResearchCapability) -> bool:
    return {
        "admission": settings.admission_enabled,
        "search": settings.search_enabled,
        "static_fetch": settings.static_fetch_enabled,
        "browser_fetch": settings.browser_fetch_enabled,
    }[capability]
