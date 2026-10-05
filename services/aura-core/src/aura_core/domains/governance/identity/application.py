"""Provider-neutral OIDC login workflow."""

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from aura_core.domains.governance.identity.models import (
    OidcLoginState,
    Principal,
    ProviderTokens,
    Session,
    ValidatedIdentity,
)


class IdentityConfigurationError(RuntimeError):
    pass


class InvalidLoginState(ValueError):
    pass


class IdentityValidationError(ValueError):
    pass


class LoginStatePort(Protocol):
    async def put(self, state: str, value: OidcLoginState, ttl_seconds: int) -> None: ...
    async def consume(self, state: str) -> OidcLoginState | None: ...


class SessionPort(Protocol):
    async def create(
        self, principal: Principal, provider_tokens: ProviderTokens | None = None
    ) -> tuple[str, Session]: ...
    async def get(self, session_id: str | None) -> Session | None: ...
    async def revoke(self, session_id: str | None) -> None: ...


class OidcProviderPort(Protocol):
    async def authorization_url(self, *, state: str, nonce: str) -> str: ...
    async def exchange(
        self, *, code: str, expected_nonce: str
    ) -> ValidatedIdentity: ...


class AuthenticationAuditPort(Protocol):
    async def record_auth_audit(
        self,
        action: str,
        outcome: str,
        *,
        issuer: str | None = None,
        subject: str | None = None,
        metadata: dict[str, object] | None = None,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class LoginConfiguration:
    issuer: str
    client_id: str
    redirect_uri: str
    configured: bool


@dataclass(frozen=True, slots=True)
class LoginStart:
    authorization_url: str


@dataclass(frozen=True, slots=True)
class LoginCompletion:
    return_to: str
    session_id: str


class LoginService:
    def __init__(
        self,
        configuration: LoginConfiguration,
        states: LoginStatePort,
        sessions: SessionPort,
        provider: OidcProviderPort,
        audit: AuthenticationAuditPort,
    ) -> None:
        self.configuration = configuration
        self.states = states
        self.sessions = sessions
        self.provider = provider
        self.audit = audit

    async def start(self, return_to: str) -> LoginStart:
        if not return_to.startswith("/") or return_to.startswith("//"):
            await self.audit.record_auth_audit(
                "auth.login", "denied", metadata={"reason": "invalid_return_path"}
            )
            raise ValueError("invalid return path")
        if not self.configuration.client_id:
            await self.audit.record_auth_audit(
                "auth.login", "denied", metadata={"reason": "provider_not_configured"}
            )
            raise IdentityConfigurationError("OIDC provider is not configured")
        state = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(32)
        await self.states.put(
            state,
            OidcLoginState(return_to, nonce, datetime.now(UTC) + timedelta(minutes=10)),
            600,
        )
        try:
            authorization_url = await self.provider.authorization_url(state=state, nonce=nonce)
        except IdentityValidationError:
            await self.audit.record_auth_audit(
                "auth.login", "denied", metadata={"reason": "provider_discovery_failed"}
            )
            raise
        await self.audit.record_auth_audit("auth.login", "started")
        return LoginStart(authorization_url)

    async def complete(
        self, code: str, state: str, previous_session: str | None
    ) -> LoginCompletion:
        saved = await self.states.consume(state)
        if saved is None:
            await self.audit.record_auth_audit(
                "auth.login", "denied", metadata={"reason": "invalid_state"}
            )
            raise InvalidLoginState("invalid authentication state")
        if not self.configuration.configured:
            await self.audit.record_auth_audit(
                "auth.login", "denied", metadata={"reason": "provider_not_configured"}
            )
            raise IdentityConfigurationError("OIDC provider is not configured")
        try:
            identity = await self.provider.exchange(code=code, expected_nonce=saved.nonce)
        except IdentityValidationError:
            await self.audit.record_auth_audit(
                "auth.login", "denied", metadata={"reason": "identity_validation_failed"}
            )
            raise
        session_id, _ = await self.sessions.create(
            Principal(identity.issuer, identity.subject, identity.display_name),
            identity.provider_tokens,
        )
        await self.sessions.revoke(previous_session)
        await self.audit.record_auth_audit(
            "auth.login",
            "succeeded",
            issuer=identity.issuer,
            subject=identity.subject,
        )
        return LoginCompletion(saved.return_to, session_id)

    async def logout(self, session_id: str | None) -> None:
        session = await self.sessions.get(session_id)
        await self.sessions.revoke(session_id)
        await self.audit.record_auth_audit(
            "auth.logout",
            "succeeded",
            issuer=session.principal.issuer if session else None,
            subject=session.principal.subject if session else None,
            metadata={"session_present": session is not None},
        )
