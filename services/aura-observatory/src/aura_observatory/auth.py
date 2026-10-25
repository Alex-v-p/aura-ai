"""Owner-only OIDC BFF sessions for Aura Observatory.

This module owns only Observatory identity state.  Provider access and refresh
tokens are kept in the server-side session backend; the browser receives an
opaque, rotated session identifier and a separate CSRF token is never placed
in a URL.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, cast
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
from authlib.jose import JsonWebKey, jwt

from aura_observatory.settings import Settings


@dataclass(frozen=True, slots=True)
class Principal:
    issuer: str
    subject: str
    display_name: str | None = None


@dataclass(frozen=True, slots=True)
class Session:
    principal: Principal
    csrf_token: str
    idle_expires_at: datetime
    absolute_expires_at: datetime
    provider_tokens: Mapping[str, str] | None = None


@dataclass(frozen=True, slots=True)
class LoginState:
    nonce: str
    return_to: str
    expires_at: datetime


_MAX_RETURN_TO_LENGTH = 2048
_SENSITIVE_RETURN_KEYS = frozenset(
    {
        # These are exact normalized names.  Deliberately do not reject
        # arbitrary substrings such as ``id`` or ``auth``: route filters such
        # as ``selected`` must survive the server-owned redirect sanitizer.
        "access",
        "accesstoken",
        "apikey",
        "aud",
        "authorization",
        "authorisation",
        "authorizationcode",
        "authorisationcode",
        "authorizationendpoint",
        "authorisationendpoint",
        "azp",
        "bearer",
        "bearertoken",
        "clientkey",
        "clientid",
        "clientassertion",
        "clientsecret",
        "cookie",
        "cookievalue",
        "code",
        "credential",
        "credentials",
        "credentialvalue",
        "endpoint",
        "body",
        "content",
        "document",
        "file",
        "filename",
        "input",
        "memory",
        "message",
        "output",
        "payload",
        "tool",
        "toolargument",
        "toolarguments",
        "toolresult",
        "toolresults",
        "audience",
        "error",
        "errordescription",
        "erroruri",
        "endsessionendpoint",
        "idtoken",
        "issuer",
        "iss",
        "jwksuri",
        "loginhint",
        "jwt",
        "nonce",
        "password",
        "prompt",
        "privatekey",
        "provider",
        "redirecturi",
        "redirecturl",
        "request",
        "requesturi",
        "response",
        "responsemode",
        "responsetype",
        "refreshtoken",
        "secret",
        "secretkey",
        "session",
        "sessionid",
        "scope",
        "state",
        "sub",
        "subject",
        "token",
        "tokenhint",
        "tokenendpoint",
        "userinfoendpoint",
        "owner",
        "owneridentity",
        "displayname",
        "preferredusername",
        "authrecovery",
    }
)
_SENSITIVE_RETURN_SUFFIXES = ("token", "credential", "credentials", "secret", "password")
_SENSITIVE_RETURN_PREFIXES = ("error", "oauthstate", "loginstate", "authcode", "oauthcode")


def sanitize_return_to(value: str | None) -> str:
    """Return a bounded relative path without callback or credential material."""

    if (
        not isinstance(value, str)
        or not value.startswith("/")
        or value.startswith("//")
        or len(value) > _MAX_RETURN_TO_LENGTH
    ):
        return "/"
    try:
        parsed = urlsplit(value)
    except ValueError:
        return "/"
    if parsed.scheme or parsed.netloc or not parsed.path.startswith("/"):
        return "/"
    query: list[tuple[str, str]] = []
    for key, query_value in parse_qsl(parsed.query, keep_blank_values=True):
        normalized = "".join(character for character in key.casefold() if character.isalnum())
        if _is_sensitive_return_key(normalized):
            continue
        query.append((key, query_value))
    sanitized = urlunsplit(("", "", parsed.path, urlencode(query), ""))
    return sanitized if len(sanitized) <= _MAX_RETURN_TO_LENGTH else "/"


def _is_sensitive_return_key(normalized: str) -> bool:
    if normalized in _SENSITIVE_RETURN_KEYS:
        return True
    if normalized.startswith(_SENSITIVE_RETURN_PREFIXES):
        return True
    return normalized.endswith(_SENSITIVE_RETURN_SUFFIXES)


class SessionBackend(Protocol):
    async def put(self, session_id: str, session: Session, ttl_seconds: int) -> None: ...

    async def get(self, session_id: str) -> Session | None: ...

    async def delete(self, session_id: str) -> None: ...


class LoginStateBackend(Protocol):
    async def put(self, state: str, value: LoginState, ttl_seconds: int) -> None: ...

    async def consume(self, state: str) -> LoginState | None: ...


class MemorySessionBackend:
    """Test/local backend; production composition uses Valkey."""

    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}

    async def put(self, session_id: str, session: Session, ttl_seconds: int) -> None:
        del ttl_seconds
        self._sessions[_digest(session_id)] = session

    async def get(self, session_id: str) -> Session | None:
        return self._sessions.get(_digest(session_id))

    async def delete(self, session_id: str) -> None:
        self._sessions.pop(_digest(session_id), None)


class MemoryLoginStateBackend:
    def __init__(self) -> None:
        self._states: dict[str, LoginState] = {}

    async def put(self, state: str, value: LoginState, ttl_seconds: int) -> None:
        del ttl_seconds
        self._states[_digest(state)] = value

    async def consume(self, state: str) -> LoginState | None:
        value = self._states.pop(_digest(state), None)
        if value is None or value.expires_at <= datetime.now(UTC):
            return None
        return value


class RedisSessionBackend:
    """Valkey backend with a service-specific namespace and hashed keys."""

    def __init__(self, client: Any, *, namespace: str = "aura:observatory") -> None:
        self.client = client
        self.namespace = namespace

    async def put(self, session_id: str, session: Session, ttl_seconds: int) -> None:
        payload = {
            "issuer": session.principal.issuer,
            "subject": session.principal.subject,
            "display_name": session.principal.display_name,
            "csrf": session.csrf_token,
            "idle": session.idle_expires_at.isoformat(),
            "absolute": session.absolute_expires_at.isoformat(),
            "provider_tokens": dict(session.provider_tokens) if session.provider_tokens else None,
        }
        await self.client.setex(self._key(session_id), ttl_seconds, json.dumps(payload))

    async def get(self, session_id: str) -> Session | None:
        value = await self.client.get(self._key(session_id))
        if value is None:
            return None
        if isinstance(value, bytes):
            value = value.decode("utf-8")
        data = json.loads(value)
        token_data = data.get("provider_tokens")
        token_map = cast(dict[object, object], token_data) if isinstance(token_data, dict) else {}
        tokens = {
            str(key): value
            for key, value in token_map.items()
            if isinstance(key, str) and isinstance(value, str)
        }
        return Session(
            Principal(data["issuer"], data["subject"], data.get("display_name")),
            data["csrf"],
            datetime.fromisoformat(data["idle"]),
            datetime.fromisoformat(data["absolute"]),
            tokens or None,
        )

    async def delete(self, session_id: str) -> None:
        await self.client.delete(self._key(session_id))

    def _key(self, value: str) -> str:
        return f"{self.namespace}:session:{_digest(value)}"


class RedisLoginStateBackend:
    def __init__(self, client: Any, *, namespace: str = "aura:observatory") -> None:
        self.client = client
        self.namespace = namespace

    async def put(self, state: str, value: LoginState, ttl_seconds: int) -> None:
        payload = {
            "nonce": value.nonce,
            "return_to": value.return_to,
            "expires": value.expires_at.isoformat(),
        }
        await self.client.setex(
            f"{self.namespace}:oidc:{_digest(state)}", ttl_seconds, json.dumps(payload)
        )

    async def consume(self, state: str) -> LoginState | None:
        key = f"{self.namespace}:oidc:{_digest(state)}"
        value = await self.client.getdel(key)
        if value is None:
            return None
        if isinstance(value, bytes):
            value = value.decode("utf-8")
        data = json.loads(value)
        result = LoginState(
            data["nonce"], data["return_to"], datetime.fromisoformat(data["expires"])
        )
        return result if result.expires_at > datetime.now(UTC) else None


class ObservatoryAuth:
    """Authenticate an already validated OIDC claim set and manage BFF state."""

    def __init__(
        self,
        settings: Settings,
        sessions: SessionBackend,
        login_states: LoginStateBackend,
    ) -> None:
        self.settings = settings
        self.sessions = sessions
        self.login_states = login_states

    async def begin_login(self, return_to: str = "/") -> tuple[str, str]:
        return_to = sanitize_return_to(return_to)
        state = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(32)
        await self.login_states.put(
            state,
            LoginState(
                nonce,
                return_to,
                datetime.now(UTC) + timedelta(seconds=self.settings.login_state_ttl_seconds),
            ),
            self.settings.login_state_ttl_seconds,
        )
        return state, nonce

    async def authorization_url(self, return_to: str = "/") -> str:
        """Create an OIDC authorization URL without exposing provider state."""

        state, nonce = await self.begin_login(return_to)
        discovery = await self._discovery()
        endpoint = discovery.get("authorization_endpoint")
        if not isinstance(endpoint, str):
            raise ValueError("OIDC discovery is missing authorization endpoint")
        query = urlencode(
            {
                "client_id": self.settings.oidc_client_id,
                "redirect_uri": self.settings.oidc_redirect_uri,
                "response_type": "code",
                "scope": "openid profile",
                "state": state,
                "nonce": nonce,
            }
        )
        return f"{endpoint}?{query}"

    async def authenticate_code(
        self, code: str, nonce: str
    ) -> tuple[dict[str, str], Mapping[str, object]]:
        """Exchange and validate an OIDC code behind the auth boundary."""

        discovery = await self._discovery()
        token_endpoint = discovery.get("token_endpoint")
        jwks_uri = discovery.get("jwks_uri")
        if not isinstance(token_endpoint, str) or not isinstance(jwks_uri, str):
            raise ValueError("OIDC discovery is missing token or JWKS endpoint")
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as client:
            response = await client.post(
                token_endpoint,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": self.settings.oidc_redirect_uri,
                    "client_id": self.settings.oidc_client_id,
                    "client_secret": self.settings.oidc_client_secret,
                },
            )
            response.raise_for_status()
            raw_payload: object = response.json()
            if not isinstance(raw_payload, dict):
                raise ValueError("OIDC token response has no ID token")
            token_payload = cast(dict[str, object], raw_payload)
            id_token = token_payload.get("id_token")
            if not isinstance(id_token, str):
                raise ValueError("OIDC token response has no ID token")
            key_response = await client.get(jwks_uri)
            key_response.raise_for_status()
            key_set: Any = key_response.json()
        if not isinstance(key_set, dict):
            raise ValueError("OIDC JWKS response must be an object")
        key_importer: Any = JsonWebKey.import_key_set
        jwt_decoder: Any = jwt.decode  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        claims: Any = jwt_decoder(id_token, key_importer(key_set))
        claims.validate()
        if claims.get("nonce") != nonce:
            raise PermissionError("OIDC nonce mismatch")
        claim_map: Mapping[str, object] = cast(dict[str, object], dict(claims))
        tokens = {
            key: value
            for key, value in token_payload.items()
            if key in {"access_token", "refresh_token", "token_type"} and isinstance(value, str)
        }
        self.authorize_claims(claim_map)
        return tokens, claim_map

    async def _discovery(self) -> Mapping[str, object]:
        async with httpx.AsyncClient(timeout=5.0, follow_redirects=False) as client:
            response = await client.get(self.settings.oidc_discovery_url)
            response.raise_for_status()
            result = response.json()
        if not isinstance(result, dict):
            raise ValueError("OIDC discovery response must be an object")
        return cast(Mapping[str, object], result)

    def authorize_claims(self, claims: Mapping[str, object]) -> Principal:
        issuer = claims.get("iss")
        subject = claims.get("sub")
        audience = claims.get("aud")
        if not isinstance(issuer, str) or not hmac.compare_digest(
            issuer.rstrip("/"), self.settings.oidc_issuer.rstrip("/")
        ):
            raise PermissionError("OIDC issuer is not configured for Observatory")
        if (
            not isinstance(subject, str)
            or not subject
            or not hmac.compare_digest(subject, self.settings.owner_subject)
        ):
            raise PermissionError("OIDC subject is not the configured Observatory owner")
        audiences: list[object] = (
            cast(list[object], audience) if isinstance(audience, list) else [audience]
        )
        if self.settings.oidc_audience not in audiences:
            raise PermissionError("OIDC audience is not configured for Observatory")
        azp = claims.get("azp")
        if len(audiences) > 1 and azp != self.settings.oidc_client_id:
            raise PermissionError("OIDC authorized party is not the Observatory client")
        if len(audiences) == 1 and azp is not None and azp != self.settings.oidc_client_id:
            raise PermissionError("OIDC authorized party is not the Observatory client")
        name = claims.get("name") or claims.get("preferred_username")
        return Principal(issuer, subject, name if isinstance(name, str) else None)

    async def create_session(
        self,
        principal: Principal,
        provider_tokens: Mapping[str, str] | None = None,
        *,
        replace_session_id: str | None = None,
    ) -> tuple[str, Session]:
        if replace_session_id:
            await self.sessions.delete(replace_session_id)
        now = datetime.now(UTC)
        session = Session(
            principal,
            secrets.token_urlsafe(32),
            now + timedelta(hours=8),
            now + timedelta(seconds=self.settings.session_ttl_seconds),
            provider_tokens,
        )
        session_id = secrets.token_urlsafe(48)
        await self.sessions.put(session_id, session, self.settings.session_ttl_seconds)
        return session_id, session

    async def get_session(self, session_id: str | None) -> Session | None:
        if not session_id:
            return None
        session = await self.sessions.get(session_id)
        now = datetime.now(UTC)
        if session is None or session.absolute_expires_at <= now or session.idle_expires_at <= now:
            if session is not None:
                await self.sessions.delete(session_id)
            return None
        refreshed = min(now + timedelta(hours=8), session.absolute_expires_at)
        if refreshed != session.idle_expires_at:
            session = Session(
                session.principal,
                session.csrf_token,
                refreshed,
                session.absolute_expires_at,
                session.provider_tokens,
            )
            await self.sessions.put(
                session_id,
                session,
                max(1, int((session.absolute_expires_at - now).total_seconds())),
            )
        return session

    async def revoke(self, session_id: str | None) -> None:
        if session_id:
            await self.sessions.delete(session_id)

    def require_csrf(
        self, session: Session, presented: str | None, *, origin: str | None = None
    ) -> None:
        if origin is not None and not hmac.compare_digest(
            origin.rstrip("/"), self.settings.public_origin.rstrip("/")
        ):
            raise PermissionError("invalid Observatory origin")
        if origin is None and self.settings.environment != "disposable_local":
            raise PermissionError("missing Observatory origin")
        if presented is None or not hmac.compare_digest(presented, session.csrf_token):
            raise PermissionError("invalid Observatory CSRF token")


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


__all__ = [
    "LoginState",
    "MemoryLoginStateBackend",
    "MemorySessionBackend",
    "ObservatoryAuth",
    "Principal",
    "RedisLoginStateBackend",
    "RedisSessionBackend",
    "Session",
    "sanitize_return_to",
]
