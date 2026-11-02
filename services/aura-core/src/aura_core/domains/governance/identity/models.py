"""Identity-owned session and OIDC DTOs."""

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class Principal:
    issuer: str
    subject: str
    display_name: str | None = None


@dataclass(frozen=True, slots=True)
class ProviderTokens:
    access_token: str | None = None
    refresh_token: str | None = None
    token_type: str | None = None


@dataclass(frozen=True, slots=True)
class Session:
    principal: Principal
    csrf_token: str
    idle_expires_at: datetime
    absolute_expires_at: datetime
    provider_tokens: ProviderTokens | None = None


@dataclass(frozen=True, slots=True)
class OidcLoginState:
    return_to: str
    nonce: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class ValidatedIdentity:
    issuer: str
    subject: str
    display_name: str | None
    provider_tokens: ProviderTokens | None = None
