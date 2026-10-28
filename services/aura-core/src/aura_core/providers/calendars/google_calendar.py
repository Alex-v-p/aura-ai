"""Bounded Google Calendar adapter and Core-owned credential sealing.

The module deliberately contains no Google SDK types.  Provider JSON is
decoded into small, neutral records and provider failures are reduced to a
closed error category before crossing the adapter boundary.
"""

# Provider JSON is narrowed immediately after decoding; strict pyright cannot
# infer the shape of a deliberately untyped upstream object before that point.
# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import secrets
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from time import monotonic
from typing import Final, Protocol, cast
from urllib.parse import quote, urlencode, urlsplit
from uuid import UUID, uuid5
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from aura_core.domains.integrations.calendar.internal import (
    CalendarDiscoveryPage,
    CalendarDiscoveryRecord,
)
from aura_core.domains.integrations.calendar.public import (
    AllDayCalendarEvent,
    CalendarAuthorizationAttempt,
    CalendarEventAvailability,
    CalendarEventPage,
    CalendarQuery,
    CalendarRequestContext,
    CalendarValidationError,
    TimedCalendarEvent,
    normalize_all_day_event,
    normalize_timed_event,
)
from aura_core.platform.calendar_oauth import (
    GOOGLE_AUTHORIZATION_ENDPOINT as GOOGLE_AUTH_ENDPOINT,
)
from aura_core.platform.calendar_oauth import (
    GOOGLE_CALENDAR_API_ENDPOINT as GOOGLE_CALENDAR_ENDPOINT,
)
from aura_core.platform.calendar_oauth import (
    GOOGLE_CALENDAR_SCOPE,
    GOOGLE_TOKEN_ENDPOINT,
    CredentialKeyRing,
)
from aura_core.platform.calendar_oauth import (
    GOOGLE_TOKEN_REVOCATION_ENDPOINT as GOOGLE_REVOKE_ENDPOINT,
)

KEY_VERSION: Final = 1
NONCE_BYTES: Final = 12
KEY_BYTES: Final = 32
MAX_RESPONSE_BYTES: Final = 2 * 1024 * 1024
MAX_CALENDAR_PAGE: Final = 100
MAX_EVENT_PAGE: Final = 100
MAX_TOKEN_BYTES: Final = 16 * 1024
MAX_CURSOR_BYTES: Final = 4096
MAX_FIELD_CHARS: Final = 10_000


def _credential_material(
    access_token: str, refresh_token: str | None, expires_in: int
) -> CredentialMaterial:
    expires_at = datetime.now(UTC) + timedelta(seconds=expires_in)
    value = {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_at": expires_at.isoformat().replace("+00:00", "Z"),
    }
    return CredentialMaterial(json.dumps(value, separators=(",", ":")).encode("utf-8"))


def _valid_pkce_verifier(value: object) -> bool:
    return (
        isinstance(value, str)
        and 43 <= len(value) <= 128
        and all(char.isalnum() or char in "-._~" for char in value)
    )


def _pkce_challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).decode(
        "ascii"
    ).rstrip("=")


def _check_request_budget(
    deadline: float | None,
    cancel_event: asyncio.Event | None,
    cancelled: Callable[[], bool] | None = None,
) -> None:
    if (cancel_event is not None and cancel_event.is_set()) or (
        cancelled is not None and cancelled()
    ):
        raise CalendarProviderError(ProviderFailureCategory.CANCELED, retryable=False)
    if deadline is not None and deadline <= asyncio.get_running_loop().time():
        raise CalendarProviderError(ProviderFailureCategory.DEADLINE, retryable=True)


async def _poll_cancellation(cancelled: Callable[[], bool]) -> str:
    while not cancelled():
        await asyncio.sleep(0.05)
    return "canceled"


async def _await_with_budget[T](
    awaitable: Awaitable[T],
    *,
    deadline: float | None,
    cancel_event: asyncio.Event | None,
    cancelled: Callable[[], bool] | None,
) -> T:
    """Await one transport operation while actively cancelling on budget loss."""

    operation = asyncio.ensure_future(awaitable)
    watchers: dict[asyncio.Task[object], str] = {}
    if cancel_event is not None:
        watchers[cast(asyncio.Task[object], asyncio.create_task(cancel_event.wait()))] = "canceled"
    if cancelled is not None:
        watchers[cast(asyncio.Task[object], asyncio.create_task(_poll_cancellation(cancelled)))] = (
            "canceled"
        )
    if deadline is not None:
        remaining = max(0.0, deadline - asyncio.get_running_loop().time())
        watchers[cast(asyncio.Task[object], asyncio.create_task(asyncio.sleep(remaining)))] = (
            "deadline"
        )
    try:
        if not watchers:
            return await operation
        done, _ = await asyncio.wait(
            (operation, *watchers), return_when=asyncio.FIRST_COMPLETED
        )
        if operation in done:
            return operation.result()
        reason = next(watchers[task] for task in done if task in watchers)
        operation.cancel()
        await asyncio.gather(operation, return_exceptions=True)
        if reason == "canceled":
            raise CalendarProviderError(ProviderFailureCategory.CANCELED, retryable=False)
        raise CalendarProviderError(ProviderFailureCategory.DEADLINE, retryable=True)
    finally:
        for watcher in watchers:
            if not watcher.done():
                watcher.cancel()
        await asyncio.gather(*watchers, return_exceptions=True)
        if not operation.done():
            operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)


class ProviderFailureCategory(StrEnum):
    """Closed provider outcome categories exposed to the domain."""

    AUTHENTICATION = "provider_auth"
    RATE_LIMIT = "provider_rate_limit"
    UNAVAILABLE = "provider_unavailable"
    MALFORMED = "validation"
    DEADLINE = "deadline"
    CANCELED = "canceled"


class CalendarProviderError(RuntimeError):
    """Sanitized provider failure; never contains upstream response text."""

    def __init__(self, category: ProviderFailureCategory, *, retryable: bool) -> None:
        super().__init__(category.value)
        self.category = category
        self.retryable = retryable


class CredentialMaterial:
    """Opaque plaintext credential wrapper.

    Plaintext is intentionally not a dataclass and cannot be serialized or
    pickled accidentally.  Callers get a short-lived copy only at the adapter
    boundary and should discard it immediately after use.
    """

    __slots__ = ("__value",)

    def __init__(self, value: bytes | bytearray | memoryview) -> None:
        material = bytes(value)
        if not material:
            raise ValueError("credential must not be empty")
        self.__value = material

    def as_bytes(self) -> bytes:
        return bytes(self.__value)

    def __repr__(self) -> str:
        return "CredentialMaterial(<redacted>)"

    __str__ = __repr__

    def __reduce__(self) -> str | tuple[object, ...]:
        raise TypeError("credential material is not serializable")

    def __getstate__(self) -> object:
        raise TypeError("credential material is not serializable")


@dataclass(frozen=True, slots=True)
class SealedCredential:
    """Ciphertext envelope persisted by the domain repository."""

    key_id: str
    nonce: bytes
    ciphertext: bytes

    def __post_init__(self) -> None:
        if type(self.key_id) is not str or not self.key_id:
            raise ValueError("credential key identifier is invalid")
        if len(self.nonce) != NONCE_BYTES or not self.ciphertext:
            raise ValueError("credential envelope is invalid")


class CredentialSealer:
    """AES-256-GCM implementation for the AURA-0129 key-ring policy."""

    _AAD_PREFIX: Final = b"aura.calendar.credential.v1|owner="

    @classmethod
    def associated_data(cls, owner_id: str, connection_id: str) -> bytes:
        if not owner_id or not connection_id or any(
            ord(char) < 0x21 or ord(char) > 0x7E for char in owner_id + connection_id
        ):
            raise ValueError("credential binding is invalid")
        return (
            cls._AAD_PREFIX
            + owner_id.encode("ascii")
            + b"|connection="
            + connection_id.encode("ascii")
        )

    @classmethod
    def seal(
        cls,
        owner_id: str,
        connection_id: str,
        credential: CredentialMaterial | bytes,
        key_ring: CredentialKeyRing,
    ) -> SealedCredential:
        material = (
            credential
            if isinstance(credential, CredentialMaterial)
            else CredentialMaterial(credential)
        )
        nonce = secrets.token_bytes(NONCE_BYTES)
        ciphertext = AESGCM(key_ring.active.key_bytes).encrypt(
            nonce, material.as_bytes(), cls.associated_data(owner_id, connection_id)
        )
        return SealedCredential(key_ring.active_key_id, nonce, ciphertext)

    @classmethod
    def open(
        cls,
        owner_id: str,
        connection_id: str,
        envelope: SealedCredential,
        key_ring: CredentialKeyRing,
    ) -> CredentialMaterial:
        plaintext: bytes | None = None
        try:
            key = key_ring.key_for_decryption(envelope.key_id)
            if key is None:
                raise ValueError("credential key version is unavailable")
            plaintext = AESGCM(key.key_bytes).decrypt(
                envelope.nonce,
                envelope.ciphertext,
                cls.associated_data(owner_id, connection_id),
            )
        except (InvalidTag, ValueError, TypeError):
            pass
        if plaintext is None:
            raise ValueError("credential authentication failed")
        return CredentialMaterial(plaintext)


class CredentialRewritePort(Protocol):
    async def compare_and_swap(
        self,
        owner_id: str,
        connection_id: str,
        expected: SealedCredential,
        replacement: SealedCredential,
    ) -> bool: ...

    async def reload(
        self, owner_id: str, connection_id: str
    ) -> SealedCredential: ...


async def open_for_use(
    owner_id: str,
    connection_id: str,
    envelope: SealedCredential,
    key_ring: CredentialKeyRing,
    *,
    rewrite: CredentialRewritePort | None = None,
) -> CredentialMaterial:
    """Open a credential and lazily CAS-reseal it under the active key."""

    material = CredentialSealer.open(owner_id, connection_id, envelope, key_ring)
    if rewrite is not None:
        reload = getattr(rewrite, "reload", None)
        if envelope.key_id != key_ring.active_key_id:
            replacement = CredentialSealer.seal(owner_id, connection_id, material, key_ring)
            swapped = await rewrite.compare_and_swap(
                owner_id, connection_id, envelope, replacement
            )
            if not swapped:
                if reload is None:
                    raise ValueError("credential rotation conflict")
                current = await reload(owner_id, connection_id)
                material = CredentialSealer.open(owner_id, connection_id, current, key_ring)
                if current.key_id != key_ring.active_key_id:
                    raise ValueError("credential rotation conflict")
        elif reload is not None:
            # A caller that supplies a rewrite port may observe a newer active
            # envelope after the initial read.  Re-read it before provider use
            # without attempting an unnecessary active-key rewrite.
            current = await reload(owner_id, connection_id)
            if current == envelope:
                raise ValueError("credential rotation conflict")
            material = CredentialSealer.open(owner_id, connection_id, current, key_ring)
    return material


def seal_credential(
    owner_id: str,
    connection_id: str,
    credential: CredentialMaterial | bytes,
    key_ring: CredentialKeyRing,
) -> SealedCredential:
    """Public convenience wrapper for Core credential ports."""

    return CredentialSealer.seal(owner_id, connection_id, credential, key_ring)


def open_credential(
    owner_id: str,
    connection_id: str,
    envelope: SealedCredential,
    key_ring: CredentialKeyRing,
) -> CredentialMaterial:
    """Public convenience wrapper for Core credential ports."""

    return CredentialSealer.open(owner_id, connection_id, envelope, key_ring)


class GoogleCalendarCredential:
    """Provider-neutral token material held only during an outbound call."""

    __slots__ = ("access_token", "refresh_token", "expires_at")

    def __init__(
        self,
        access_token: str,
        refresh_token: str | None = None,
        expires_at: datetime | None = None,
    ) -> None:
        if not access_token or len(access_token) > 4096:
            raise ValueError("access token is invalid")
        if refresh_token is not None and (not refresh_token or len(refresh_token) > 4096):
            raise ValueError("refresh token is invalid")
        if expires_at is not None:
            if expires_at.tzinfo is None or expires_at.utcoffset() is None:
                raise ValueError("credential expiry must be timezone-aware")
            expires_at = expires_at.astimezone(UTC)
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.expires_at = expires_at

    def __repr__(self) -> str:
        return "GoogleCalendarCredential(<redacted>)"

    __str__ = __repr__

    def __reduce__(self) -> str | tuple[object, ...]:
        raise TypeError("credential material is not serializable")

    def __getstate__(self) -> object:
        raise TypeError("credential material is not serializable")

    @classmethod
    def from_material(cls, material: CredentialMaterial) -> GoogleCalendarCredential:
        value: object | None = None
        malformed_json = False
        try:
            value = json.loads(material.as_bytes())
        except (UnicodeDecodeError, json.JSONDecodeError):
            malformed_json = True
        if malformed_json:
            raise ValueError("credential material is malformed")
        if not isinstance(value, dict):
            raise ValueError("credential material is malformed")
        if set(value) - {"access_token", "refresh_token", "expires_at"}:
            raise ValueError("credential material is malformed")
        access = value.get("access_token")
        refresh = value.get("refresh_token")
        expires_at_value = value.get("expires_at")
        if not isinstance(access, str) or (refresh is not None and not isinstance(refresh, str)):
            raise ValueError("credential material is malformed")
        if not isinstance(expires_at_value, str):
            raise ValueError("credential material is malformed")
        expires_at: datetime | None = None
        malformed_expiry = False
        try:
            expires_at = datetime.fromisoformat(expires_at_value.replace("Z", "+00:00"))
        except ValueError:
            malformed_expiry = True
        if malformed_expiry or expires_at is None:
            raise ValueError("credential material is malformed")
        if expires_at.tzinfo is None:
            raise ValueError("credential material is malformed")
        expires_at = expires_at.astimezone(UTC)
        return cls(access, refresh, expires_at)

    def is_expired(self, now: datetime | None = None) -> bool:
        return self.expires_at is not None and self.expires_at <= (now or datetime.now(UTC))


@dataclass(frozen=True, slots=True)
class GoogleCalendarRecord:
    provider_id: str
    display_name: str
    time_zone: str | None
    readable: bool


@dataclass(frozen=True, slots=True)
class GoogleEventRecord:
    provider_id: str
    calendar_provider_id: str
    summary: str
    description: str | None
    location: str | None
    status: str | None
    start: Mapping[str, str]
    end: Mapping[str, str]
    transparency: str | None


@dataclass(frozen=True, slots=True)
class GoogleCalendarPage:
    items: tuple[GoogleCalendarRecord, ...]
    next_page_token: str | None


@dataclass(frozen=True, slots=True)
class GoogleEventPage:
    items: tuple[GoogleEventRecord, ...]
    next_page_token: str | None


@dataclass(frozen=True, slots=True)
class _EventContinuation:
    """Raw provider pagination state held by the Core cursor store.

    This is deliberately encoded as a bounded JSON value at the inward port.
    It contains the Aura calendar position and Google's page token, but never
    an Aura cursor handle or a provider identifier for another calendar.
    """

    calendar_index: int
    calendar_id: UUID
    page_token: str | None


class ProviderTelemetry(Protocol):
    def record(self, outcome: str, *, retry_count: int, elapsed_ms: float) -> None: ...


class CredentialLoader(Protocol):
    async def __call__(
        self, owner: tuple[str, str]
    ) -> GoogleCalendarCredential | CredentialMaterial: ...


class CalendarIdResolver(Protocol):
    async def __call__(self, owner: tuple[str, str], calendar_id: UUID) -> str: ...


class GoogleCalendarAdapter:
    """Fixed-endpoint, bounded, redirect-free Google Calendar adapter."""

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        http_client: httpx.AsyncClient | None = None,
        telemetry: ProviderTelemetry | None = None,
        max_attempts: int = 3,
        response_limit: int = MAX_RESPONSE_BYTES,
        callback_uri: str | None = None,
        credential_loader: CredentialLoader | None = None,
        calendar_id_resolver: CalendarIdResolver | None = None,
    ) -> None:
        if not client_id or not client_secret:
            raise ValueError("provider client configuration is incomplete")
        if not 1 <= max_attempts <= 3 or not 1024 <= response_limit <= MAX_RESPONSE_BYTES:
            raise ValueError("provider bounds are invalid")
        if callback_uri is not None and not _valid_callback_uri(callback_uri):
            raise ValueError("provider callback configuration is invalid")
        self._client_id = client_id
        self._client_secret = client_secret
        self._client = http_client
        self._telemetry = telemetry
        self._max_attempts = max_attempts
        self._response_limit = response_limit
        self._callback_uri = callback_uri
        self._credential_loader = credential_loader
        self._calendar_id_resolver = calendar_id_resolver

    async def _load_credential(
        self,
        owner: tuple[str, str],
        *,
        deadline: float | None = None,
        cancel_event: asyncio.Event | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> GoogleCalendarCredential:
        _check_request_budget(deadline, cancel_event, cancelled)
        if self._credential_loader is None:
            raise CalendarProviderError(ProviderFailureCategory.UNAVAILABLE, retryable=True)
        loaded: object | None = None
        loader_failed = False
        try:
            loaded = await _await_with_budget(
                self._credential_loader(owner),
                deadline=deadline,
                cancel_event=cancel_event,
                cancelled=cancelled,
            )
        except CalendarProviderError:
            raise
        except asyncio.CancelledError:
            raise
        except Exception:
            loader_failed = True
        if loader_failed:
            raise CalendarProviderError(ProviderFailureCategory.AUTHENTICATION, retryable=False)
        if isinstance(loaded, GoogleCalendarCredential):
            credential = loaded
        elif type(loaded) is CredentialMaterial:
            malformed = False
            credential = None
            try:
                credential = GoogleCalendarCredential.from_material(loaded)
            except ValueError:
                malformed = True
            if malformed or credential is None:
                raise CalendarProviderError(
                    ProviderFailureCategory.AUTHENTICATION, retryable=False
                )
        else:
            raise CalendarProviderError(ProviderFailureCategory.AUTHENTICATION, retryable=False)
        # Refresh is an explicit lifecycle operation.  Reads must never make
        # an outbound token call after the persisted absolute expiry.
        if credential.expires_at is None or credential.is_expired():
            raise CalendarProviderError(ProviderFailureCategory.AUTHENTICATION, retryable=False)
        return credential

    async def discover(
        self,
        owner: tuple[str, str],
        *,
        cursor: str | None,
        limit: int,
        deadline: float | None = None,
        cancel_event: asyncio.Event | None = None,
        request: CalendarRequestContext | None = None,
    ) -> CalendarDiscoveryPage:
        """Implement the provider-neutral discovery port when composed with a loader."""

        if not 1 <= limit <= MAX_CALENDAR_PAGE:
            raise CalendarProviderError(ProviderFailureCategory.UNAVAILABLE, retryable=True)
        if request is not None:
            deadline = request.deadline_monotonic
            cancelled = request.cancelled
        else:
            cancelled = None
        credential = await self._load_credential(
            owner,
            deadline=deadline,
            cancel_event=cancel_event,
            cancelled=cancelled,
        )
        if cursor is not None and _validate_page_token(cursor) is None:
            raise CalendarProviderError(ProviderFailureCategory.MALFORMED, retryable=False)
        page = await self.list_calendars(
            credential,
            page_token=cursor,
            max_results=limit,
            deadline=deadline,
            cancel_event=cancel_event,
            cancelled=cancelled,
        )
        # The discovery port is server-only: the repository needs the
        # provider_id to upsert metadata, while the application projects the
        # resulting Aura-owned rows before they reach an API response.
        items = tuple(
            CalendarDiscoveryRecord(
                item.provider_id,
                item.display_name,
                item.time_zone,
                item.readable,
            )
            for item in page.items[:limit]
            if item.readable
        )
        next_cursor = _validate_page_token(page.next_page_token)
        return CalendarDiscoveryPage(items, next_cursor)

    async def retrieve(
        self,
        owner: tuple[str, str],
        query: CalendarQuery,
        *,
        deadline: float | None = None,
        cancel_event: asyncio.Event | None = None,
        request: CalendarRequestContext | None = None,
    ) -> CalendarEventPage:
        """Retrieve and normalize a bounded page through the Core read port."""

        if self._credential_loader is None or self._calendar_id_resolver is None:
            raise CalendarProviderError(ProviderFailureCategory.UNAVAILABLE, retryable=True)
        if not _valid_zone_info(query.display_time_zone):
            raise CalendarProviderError(
                ProviderFailureCategory.MALFORMED, retryable=False
            )
        request_deadline = deadline
        request_cancelled: Callable[[], bool] | None = None
        if request is not None:
            request_deadline = request.deadline_monotonic
            request_cancelled = request.cancelled
        if request_deadline is None:
            request_deadline = asyncio.get_running_loop().time() + query.timeout_ms / 1000.0
        credential = await self._load_credential(
            owner,
            deadline=request_deadline,
            cancel_event=cancel_event,
            cancelled=request_cancelled,
        )
        events: list[TimedCalendarEvent | AllDayCalendarEvent] = []
        continuation = _decode_event_continuation(query.cursor, query.calendar_ids)
        start_index = continuation.calendar_index if continuation is not None else 0
        first_page_token = continuation.page_token if continuation is not None else None
        next_continuation: _EventContinuation | None = None
        for calendar_index in range(start_index, len(query.calendar_ids)):
            calendar_id = query.calendar_ids[calendar_index]
            provider_id = await _await_with_budget(
                self._calendar_id_resolver(owner, calendar_id),
                deadline=request_deadline,
                cancel_event=cancel_event,
                cancelled=request_cancelled,
            )
            provider_cursor = first_page_token if calendar_index == start_index else None
            remaining = query.limit - len(events)
            page = await self.list_events(
                credential,
                provider_id,
                time_min=query.range_start,
                time_max=query.range_end,
                page_token=provider_cursor,
                max_results=remaining,
                deadline=request_deadline,
                cancel_event=cancel_event,
                cancelled=request_cancelled,
            )
            for item in page.items:
                normalized = _normalize_event(
                    item, calendar_id, query.display_time_zone, owner
                )
                events.append(normalized)
                if len(events) >= query.limit:
                    break

            next_page_token = _validate_page_token(page.next_page_token)
            if next_page_token is not None:
                # A token always belongs to the calendar that produced it.
                # Never apply it to a later selected calendar.
                next_continuation = _EventContinuation(
                    calendar_index, calendar_id, next_page_token
                )
                break
            if len(events) >= query.limit and calendar_index + 1 < len(query.calendar_ids):
                next_calendar_id = query.calendar_ids[calendar_index + 1]
                next_continuation = _EventContinuation(calendar_index + 1, next_calendar_id, None)
                break
            first_page_token = None
        next_cursor = _encode_event_continuation(next_continuation)
        return CalendarEventPage(
            query.range_start,
            query.range_end,
            query.display_time_zone,
            tuple(events[: query.limit]),
            next_cursor,
            False,
            (),
            datetime.now(UTC),
        )

    async def revoke(
        self,
        owner: tuple[str, str],
        *,
        deadline: float | None = None,
        cancel_event: asyncio.Event | None = None,
        request: CalendarRequestContext | None = None,
    ) -> None:
        if request is not None:
            deadline = request.deadline_monotonic
            cancelled = request.cancelled
        else:
            cancelled = None
        await self.revoke_credential(
            await self._load_credential(
                owner,
                deadline=deadline,
                cancel_event=cancel_event,
                cancelled=cancelled,
            ),
            deadline=deadline,
            cancel_event=cancel_event,
            cancelled=cancelled,
        )

    async def revoke_credential(
        self,
        credential: GoogleCalendarCredential,
        *,
        deadline: float | None = None,
        cancel_event: asyncio.Event | None = None,
        request: CalendarRequestContext | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> None:
        if request is not None:
            deadline = request.deadline_monotonic
            cancelled = request.cancelled
        await self._revoke(
            credential,
            deadline=deadline,
            cancel_event=cancel_event,
            cancelled=cancelled,
        )

    async def _revoke(
        self,
        credential: GoogleCalendarCredential,
        *,
        deadline: float | None = None,
        cancel_event: asyncio.Event | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> None:
        await self._request_json(
            "POST",
            GOOGLE_REVOKE_ENDPOINT,
            data={"token": credential.refresh_token or credential.access_token},
            operation="revoke",
            retry=False,
            allow_empty=True,
            deadline=deadline,
            cancel_event=cancel_event,
            cancelled=cancelled,
        )

    async def exchange_code(
        self,
        *,
        code: str,
        redirect_uri: str,
        code_verifier: str | None = None,
        verifier: str | None = None,
        nonce: str | None = None,
        deadline: float | None = None,
        cancel_event: asyncio.Event | None = None,
        request: CalendarRequestContext | None = None,
    ) -> CredentialMaterial:
        cancelled: Callable[[], bool] | None = None
        if request is not None:
            deadline = request.deadline_monotonic
            cancelled = request.cancelled
        if code_verifier is not None and verifier is not None and code_verifier != verifier:
            raise CalendarProviderError(ProviderFailureCategory.MALFORMED, retryable=False)
        effective_verifier = code_verifier if code_verifier is not None else verifier
        if (
            not code
            or len(code) > 4096
            or not _valid_pkce_verifier(effective_verifier)
            or (nonce is not None and not _valid_opaque_value(nonce))
            or not _valid_callback_uri(redirect_uri)
            or (self._callback_uri is not None and redirect_uri != self._callback_uri)
        ):
            raise CalendarProviderError(ProviderFailureCategory.MALFORMED, retryable=False)
        assert effective_verifier is not None
        payload = await self._request_json(
            "POST",
            GOOGLE_TOKEN_ENDPOINT,
            data={
                "code": code,
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
                "scope": GOOGLE_CALENDAR_SCOPE,
                "code_verifier": effective_verifier,
            },
            operation="token_exchange",
            retry=False,
            deadline=deadline,
            cancel_event=cancel_event,
            cancelled=cancelled,
        )
        if not isinstance(payload, dict):
            raise CalendarProviderError(ProviderFailureCategory.MALFORMED, retryable=False)
        access = payload.get("access_token")
        refresh = payload.get("refresh_token")
        expires = payload.get("expires_in")
        scope = payload.get("scope")
        if not isinstance(access, str) or not access or len(access) > 4096:
            raise CalendarProviderError(ProviderFailureCategory.AUTHENTICATION, retryable=False)
        if refresh is not None and (
            not isinstance(refresh, str) or not refresh or len(refresh) > 4096
        ):
            raise CalendarProviderError(ProviderFailureCategory.MALFORMED, retryable=False)
        if not isinstance(scope, str) or set(scope.split()) != {GOOGLE_CALENDAR_SCOPE}:
            raise CalendarProviderError(ProviderFailureCategory.AUTHENTICATION, retryable=False)
        if type(expires) is not int or not 0 < expires <= 86_400:
            raise CalendarProviderError(ProviderFailureCategory.MALFORMED, retryable=False)
        return _credential_material(access, refresh, expires)

    async def authorization_url(self, attempt: CalendarAuthorizationAttempt) -> str:
        """Implement the lifecycle port using the configured fixed callback."""

        if self._callback_uri is None:
            raise CalendarProviderError(ProviderFailureCategory.UNAVAILABLE, retryable=True)
        verifier = attempt.verifier or attempt.nonce
        return self.build_authorization_url(
            state=attempt.state,
            nonce=attempt.nonce,
            redirect_uri=self._callback_uri,
            verifier=verifier,
        )

    def build_authorization_url(
        self,
        *,
        state: str,
        nonce: str,
        redirect_uri: str,
        verifier: str | None = None,
    ) -> str:
        """Build the only provider authorization navigation accepted by Core."""

        effective_verifier = nonce if verifier is None else verifier
        if (
            not _valid_opaque_value(state)
            or not _valid_opaque_value(nonce)
            or not _valid_pkce_verifier(effective_verifier)
            or not _valid_callback_uri(redirect_uri)
            or (self._callback_uri is not None and redirect_uri != self._callback_uri)
        ):
            raise CalendarProviderError(ProviderFailureCategory.MALFORMED, retryable=False)
        query = urlencode(
            {
                "client_id": self._client_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": GOOGLE_CALENDAR_SCOPE,
                "code_challenge": _pkce_challenge(effective_verifier),
                "code_challenge_method": "S256",
                "state": state,
                "nonce": nonce,
                "access_type": "offline",
                "prompt": "consent",
            }
        )
        return f"{GOOGLE_AUTH_ENDPOINT}?{query}"

    async def refresh_access_token(
        self,
        credential: GoogleCalendarCredential,
        *,
        deadline: float | None = None,
        cancel_event: asyncio.Event | None = None,
        request: CalendarRequestContext | None = None,
    ) -> CredentialMaterial:
        cancelled: Callable[[], bool] | None = None
        if request is not None:
            deadline = request.deadline_monotonic
            cancelled = request.cancelled
        if not credential.refresh_token:
            raise CalendarProviderError(ProviderFailureCategory.AUTHENTICATION, retryable=False)
        payload = await self._request_json(
            "POST",
            GOOGLE_TOKEN_ENDPOINT,
            data={
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "refresh_token": credential.refresh_token,
                "grant_type": "refresh_token",
                "scope": GOOGLE_CALENDAR_SCOPE,
            },
            operation="token_refresh",
            retry=False,
            deadline=deadline,
            cancel_event=cancel_event,
            cancelled=cancelled,
        )
        if not isinstance(payload, dict):
            raise CalendarProviderError(ProviderFailureCategory.MALFORMED, retryable=False)
        access = payload.get("access_token")
        scope = payload.get("scope", GOOGLE_CALENDAR_SCOPE)
        expires = payload.get("expires_in")
        if (
            not isinstance(access, str)
            or not access
            or len(access) > 4096
            or not isinstance(scope, str)
            or set(scope.split()) != {GOOGLE_CALENDAR_SCOPE}
            or type(expires) is not int
            or not 0 < expires <= 86_400
        ):
            raise CalendarProviderError(ProviderFailureCategory.AUTHENTICATION, retryable=False)
        return _credential_material(access, credential.refresh_token, expires)

    async def list_calendars(
        self,
        credential: GoogleCalendarCredential,
        *,
        page_token: str | None = None,
        max_results: int = MAX_CALENDAR_PAGE,
        deadline: float | None = None,
        cancel_event: asyncio.Event | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> GoogleCalendarPage:
        if (
            not 1 <= max_results <= MAX_CALENDAR_PAGE
            or (page_token is not None and _validate_page_token(page_token) is None)
        ):
            raise CalendarProviderError(ProviderFailureCategory.MALFORMED, retryable=False)
        params: dict[str, str] = {"maxResults": str(max_results), "showHidden": "false"}
        if page_token:
            params["pageToken"] = page_token
        payload = await self._request_json(
            "GET", f"{GOOGLE_CALENDAR_ENDPOINT}/users/me/calendarList", params=params,
            operation="list_calendars", deadline=deadline, cancel_event=cancel_event,
            cancelled=cancelled,
            access_token=credential.access_token,
        )
        page = _parse_calendar_page(payload)
        if len(page.items) > max_results:
            raise CalendarProviderError(ProviderFailureCategory.MALFORMED, retryable=False)
        return page

    async def list_events(
        self,
        credential: GoogleCalendarCredential,
        calendar_provider_id: str,
        *,
        time_min: datetime,
        time_max: datetime,
        page_token: str | None = None,
        max_results: int = MAX_EVENT_PAGE,
        deadline: float | None = None,
        cancel_event: asyncio.Event | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> GoogleEventPage:
        if (
            not calendar_provider_id
            or len(calendar_provider_id) > 512
            or time_min.tzinfo is None
            or time_max.tzinfo is None
            or time_max <= time_min
            or time_max - time_min > timedelta(days=31)
            or not 1 <= max_results <= MAX_EVENT_PAGE
            or (page_token is not None and _validate_page_token(page_token) is None)
        ):
            raise CalendarProviderError(ProviderFailureCategory.MALFORMED, retryable=False)
        params: dict[str, str] = {
            "timeMin": time_min.isoformat(),
            "timeMax": time_max.isoformat(),
            "singleEvents": "true",
            "showDeleted": "false",
            "maxResults": str(max_results),
        }
        if page_token:
            params["pageToken"] = page_token
        path_id = quote(calendar_provider_id, safe="")
        payload = await self._request_json(
            "GET", f"{GOOGLE_CALENDAR_ENDPOINT}/calendars/{path_id}/events", params=params,
            operation="list_events", deadline=deadline, cancel_event=cancel_event,
            cancelled=cancelled,
            access_token=credential.access_token,
        )
        page = _parse_event_page(payload, calendar_provider_id)
        if len(page.items) > max_results:
            raise CalendarProviderError(ProviderFailureCategory.MALFORMED, retryable=False)
        return page

    async def _request_json(
        self,
        method: str,
        url: str,
        *,
        operation: str,
        data: Mapping[str, str] | None = None,
        params: Mapping[str, str] | None = None,
        deadline: float | None = None,
        cancel_event: asyncio.Event | None = None,
        cancelled: Callable[[], bool] | None = None,
        retry: bool = True,
        allow_empty: bool = False,
        access_token: str | None = None,
    ) -> object:
        retry_count = 0
        started = monotonic()
        for attempt in range(self._max_attempts if retry else 1):
            _check_request_budget(deadline, cancel_event, cancelled)
            timeout: httpx.Timeout | float | None = None
            if deadline is not None:
                timeout = max(0.001, deadline - asyncio.get_running_loop().time())
            terminal_failure: ProviderFailureCategory | None = None
            try:
                status_code, body = await _await_with_budget(
                    self._request_once(
                        method,
                        url,
                        data=data,
                        params=params,
                        timeout=timeout,
                        access_token=access_token,
                        response_limit=self._response_limit,
                        deadline=deadline,
                        cancel_event=cancel_event,
                        cancelled=cancelled,
                    ),
                    deadline=deadline,
                    cancel_event=cancel_event,
                    cancelled=cancelled,
                )
                if status_code in (401, 403):
                    raise CalendarProviderError(
                        ProviderFailureCategory.AUTHENTICATION, retryable=False
                    )
                if status_code == 429:
                    category = ProviderFailureCategory.RATE_LIMIT
                    if attempt + 1 < (self._max_attempts if retry else 1):
                        retry_count += 1
                        await self._backoff(
                            attempt,
                            deadline=deadline,
                            cancel_event=cancel_event,
                            cancelled=cancelled,
                        )
                        continue
                    raise CalendarProviderError(category, retryable=True)
                if 500 <= status_code <= 599:
                    if attempt + 1 < (self._max_attempts if retry else 1):
                        retry_count += 1
                        await self._backoff(
                            attempt,
                            deadline=deadline,
                            cancel_event=cancel_event,
                            cancelled=cancelled,
                        )
                        continue
                    raise CalendarProviderError(
                        ProviderFailureCategory.UNAVAILABLE, retryable=True
                    )
                if status_code < 200 or status_code >= 300:
                    raise CalendarProviderError(
                        ProviderFailureCategory.MALFORMED, retryable=False
                    )
                if not body and allow_empty:
                    self._record_telemetry("ok", retry_count=retry_count, started=started)
                    return {}
                if not body:
                    raise CalendarProviderError(
                        ProviderFailureCategory.MALFORMED, retryable=False
                    )
                malformed_json = False
                result: object | None = None
                try:
                    result = json.loads(body)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    malformed_json = True
                if malformed_json:
                    raise CalendarProviderError(
                        ProviderFailureCategory.MALFORMED, retryable=False
                    )
                assert result is not None
                self._record_telemetry("ok", retry_count=retry_count, started=started)
                return result
            except CalendarProviderError as exc:
                self._record_telemetry(
                    exc.category.value,
                    retry_count=retry_count,
                    started=started,
                )
                raise
            except asyncio.CancelledError:
                self._record_telemetry(
                    ProviderFailureCategory.CANCELED.value,
                    retry_count=retry_count,
                    started=started,
                )
                raise
            except (httpx.TimeoutException, TimeoutError):
                if attempt + 1 < (self._max_attempts if retry else 1):
                    retry_count += 1
                    await self._backoff(
                        attempt,
                        deadline=deadline,
                        cancel_event=cancel_event,
                        cancelled=cancelled,
                    )
                    continue
                self._record_telemetry(
                    ProviderFailureCategory.DEADLINE.value,
                    retry_count=retry_count,
                    started=started,
                )
                terminal_failure = ProviderFailureCategory.DEADLINE
            except httpx.HTTPError:
                if attempt + 1 < (self._max_attempts if retry else 1):
                    retry_count += 1
                    await self._backoff(
                        attempt,
                        deadline=deadline,
                        cancel_event=cancel_event,
                        cancelled=cancelled,
                    )
                    continue
                self._record_telemetry(
                    ProviderFailureCategory.UNAVAILABLE.value,
                    retry_count=retry_count,
                    started=started,
                )
                terminal_failure = ProviderFailureCategory.UNAVAILABLE
            raise CalendarProviderError(terminal_failure, retryable=True)
        raise CalendarProviderError(ProviderFailureCategory.UNAVAILABLE, retryable=True)

    async def _request_once(
        self,
        method: str,
        url: str,
        *,
        data: Mapping[str, str] | None,
        params: Mapping[str, str] | None,
        timeout: httpx.Timeout | float | None,
        access_token: str | None,
        response_limit: int,
        deadline: float | None,
        cancel_event: asyncio.Event | None,
        cancelled: Callable[[], bool] | None,
    ) -> tuple[int, bytes]:
        async with self._stream(
            method,
            url,
            data=data,
            params=params,
            timeout=timeout,
            access_token=access_token,
        ) as response:
            body = b""
            if 200 <= response.status_code < 300:
                body = await _bounded_body(
                    response,
                    response_limit,
                    deadline=deadline,
                    cancel_event=cancel_event,
                    cancelled=cancelled,
                )
            return response.status_code, body

    async def _backoff(
        self,
        attempt: int,
        *,
        deadline: float | None,
        cancel_event: asyncio.Event | None,
        cancelled: Callable[[], bool] | None,
    ) -> None:
        delay = min(0.25 * 2**attempt, 1.0)
        _check_request_budget(deadline, cancel_event, cancelled)
        if deadline is not None:
            delay = min(delay, max(0.0, deadline - asyncio.get_running_loop().time()))
        await _await_with_budget(
            asyncio.sleep(delay),
            deadline=deadline,
            cancel_event=cancel_event,
            cancelled=cancelled,
        )
        _check_request_budget(deadline, cancel_event, cancelled)

    def _record_telemetry(self, outcome: str, *, retry_count: int, started: float) -> None:
        if self._telemetry is not None:
            # Preserve the closed provider category through the inward
            # telemetry port. The API/bootstrap adapter owns the final
            # mapping to the shared terminal outcome set and must retain
            # provider_rate_limit/provider_unavailable as error classes rather
            # than collapsing them into retry_class.
            self._telemetry.record(
                outcome,
                retry_count=retry_count,
                elapsed_ms=max(0.0, (monotonic() - started) * 1000.0),
            )

    @asynccontextmanager
    async def _stream(
        self,
        method: str,
        url: str,
        *,
        data: Mapping[str, str] | None,
        params: Mapping[str, str] | None,
        timeout: httpx.Timeout | float | None,
        access_token: str | None,
    ) -> AsyncGenerator[httpx.Response]:
        headers = {"Authorization": f"Bearer {access_token}"} if access_token else None
        if self._client is not None:
            async with self._client.stream(
                method,
                url,
                data=data,
                params=params,
                headers=headers,
                timeout=timeout,
                follow_redirects=False,
            ) as response:
                yield response
            return
        async with httpx.AsyncClient(follow_redirects=False, timeout=timeout) as client:
            async with client.stream(
                method,
                url,
                data=data,
                params=params,
                headers=headers,
                follow_redirects=False,
            ) as response:
                yield response


async def _bounded_body(
    response: httpx.Response,
    limit: int,
    *,
    deadline: float | None = None,
    cancel_event: asyncio.Event | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> bytes:
    _check_request_budget(deadline, cancel_event, cancelled)
    content_length = response.headers.get("content-length")
    if content_length is not None:
        declared: int | None = None
        try:
            declared = int(content_length)
        except ValueError:
            pass
        if declared is None or declared < 0 or declared > limit:
            raise CalendarProviderError(ProviderFailureCategory.MALFORMED, retryable=False)
    body = bytearray()
    stream = response.aiter_bytes()
    try:
        async for chunk in stream:
            _check_request_budget(deadline, cancel_event, cancelled)
            body.extend(chunk)
            if len(body) > limit:
                raise CalendarProviderError(ProviderFailureCategory.MALFORMED, retryable=False)
    finally:
        close = getattr(stream, "aclose", None)
        if close is not None:
            await close()
    return bytes(body)


def _validate_page_token(value: object) -> str | None:
    """Return a bounded opaque Google continuation token or ``None``."""

    if value is None:
        return None
    if not isinstance(value, str) or not value:
        return None
    encoded = value.encode("utf-8", errors="ignore")
    if len(encoded) > 1024 or any(ord(char) < 0x21 or ord(char) > 0x7E for char in value):
        return None
    return value


def _encode_event_continuation(state: _EventContinuation | None) -> str | None:
    if state is None or (
        state.page_token is not None and _validate_page_token(state.page_token) is None
    ):
        return None
    payload = json.dumps(
        {
            "calendarId": str(state.calendar_id),
            "calendarIndex": state.calendar_index,
            "pageToken": state.page_token,
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    if len(payload.encode("utf-8")) > MAX_CURSOR_BYTES:
        raise CalendarProviderError(ProviderFailureCategory.MALFORMED, retryable=False)
    return payload


def _decode_event_continuation(
    cursor: str | None, calendar_ids: tuple[UUID, ...]
) -> _EventContinuation | None:
    if cursor is None:
        return None
    if len(cursor.encode("utf-8")) > MAX_CURSOR_BYTES:
        raise CalendarProviderError(ProviderFailureCategory.MALFORMED, retryable=False)
    payload: object | None = None
    malformed_json = False
    try:
        payload = json.loads(cursor)
    except (UnicodeDecodeError, json.JSONDecodeError):
        malformed_json = True
    if malformed_json:
        raise CalendarProviderError(ProviderFailureCategory.MALFORMED, retryable=False)
    if not isinstance(payload, dict) or set(payload) != {
        "calendarId",
        "calendarIndex",
        "pageToken",
    }:
        raise CalendarProviderError(ProviderFailureCategory.MALFORMED, retryable=False)
    calendar_index = payload.get("calendarIndex")
    calendar_id_value = payload.get("calendarId")
    page_token = payload.get("pageToken")
    if (
        type(calendar_index) is not int
        or not 0 <= calendar_index < len(calendar_ids)
        or not isinstance(calendar_id_value, str)
        or (page_token is not None and _validate_page_token(page_token) is None)
    ):
        raise CalendarProviderError(ProviderFailureCategory.MALFORMED, retryable=False)
    calendar_id: UUID | None = None
    malformed_id = False
    try:
        calendar_id = UUID(calendar_id_value)
    except (AttributeError, ValueError):
        malformed_id = True
    if malformed_id or calendar_id is None:
        raise CalendarProviderError(ProviderFailureCategory.MALFORMED, retryable=False)
    if calendar_ids[calendar_index] != calendar_id:
        raise CalendarProviderError(ProviderFailureCategory.MALFORMED, retryable=False)
    return _EventContinuation(calendar_index, calendar_id, page_token)


def _parse_calendar_page(payload: object) -> GoogleCalendarPage:
    if not isinstance(payload, dict) or not isinstance(payload.get("items", []), list):
        raise CalendarProviderError(ProviderFailureCategory.MALFORMED, retryable=False)
    raw_items = payload["items"]
    assert isinstance(raw_items, list)
    if len(raw_items) > MAX_CALENDAR_PAGE:
        raise CalendarProviderError(ProviderFailureCategory.MALFORMED, retryable=False)
    result: list[GoogleCalendarRecord] = []
    for item in raw_items:
        if not isinstance(item, dict):
            raise CalendarProviderError(ProviderFailureCategory.MALFORMED, retryable=False)
        provider_id, name = item.get("id"), item.get("summary")
        access = item.get("accessRole")
        tz = item.get("timeZone")
        if not isinstance(provider_id, str) or not provider_id or len(provider_id) > 512:
            raise CalendarProviderError(ProviderFailureCategory.MALFORMED, retryable=False)
        if not isinstance(name, str) or not 0 < len(name) <= 255:
            raise CalendarProviderError(ProviderFailureCategory.MALFORMED, retryable=False)
        if tz is not None and (not isinstance(tz, str) or len(tz) > 128):
            raise CalendarProviderError(ProviderFailureCategory.MALFORMED, retryable=False)
        if tz is not None and not _valid_zone_info(tz):
            raise CalendarProviderError(ProviderFailureCategory.MALFORMED, retryable=False)
        result.append(
            GoogleCalendarRecord(provider_id, name, tz, access in {"reader", "writer", "owner"})
        )
    token = payload.get("nextPageToken")
    if token is not None and _validate_page_token(token) is None:
        raise CalendarProviderError(ProviderFailureCategory.MALFORMED, retryable=False)
    return GoogleCalendarPage(tuple(result), token)


def _parse_event_page(payload: object, calendar_provider_id: str) -> GoogleEventPage:
    if not isinstance(payload, dict) or not isinstance(payload.get("items", []), list):
        raise CalendarProviderError(ProviderFailureCategory.MALFORMED, retryable=False)
    raw_items = payload["items"]
    assert isinstance(raw_items, list)
    if len(raw_items) > MAX_EVENT_PAGE:
        raise CalendarProviderError(ProviderFailureCategory.MALFORMED, retryable=False)
    result: list[GoogleEventRecord] = []
    for item in raw_items:
        if not isinstance(item, dict):
            raise CalendarProviderError(ProviderFailureCategory.MALFORMED, retryable=False)
        event_id = item.get("id")
        summary = item.get("summary", "")
        if not isinstance(event_id, str) or not event_id or len(event_id) > 512:
            raise CalendarProviderError(ProviderFailureCategory.MALFORMED, retryable=False)
        if not isinstance(summary, str) or len(summary) > 1000:
            raise CalendarProviderError(ProviderFailureCategory.MALFORMED, retryable=False)
        description = item.get("description")
        location = item.get("location")
        status = item.get("status")
        transparency = item.get("transparency")
        if description is not None and (
            not isinstance(description, str) or len(description) > MAX_FIELD_CHARS
        ):
            raise CalendarProviderError(ProviderFailureCategory.MALFORMED, retryable=False)
        if location is not None and (not isinstance(location, str) or len(location) > 1000):
            raise CalendarProviderError(ProviderFailureCategory.MALFORMED, retryable=False)
        if status is not None and (not isinstance(status, str) or len(status) > 32):
            raise CalendarProviderError(ProviderFailureCategory.MALFORMED, retryable=False)
        if transparency is not None and (
            not isinstance(transparency, str) or len(transparency) > 32
        ):
            raise CalendarProviderError(ProviderFailureCategory.MALFORMED, retryable=False)
        start, end = item.get("start"), item.get("end")
        if not _valid_time_object(start) or not _valid_time_object(end):
            raise CalendarProviderError(ProviderFailureCategory.MALFORMED, retryable=False)
        assert isinstance(start, dict) and isinstance(end, dict)
        result.append(
            GoogleEventRecord(
                event_id,
                calendar_provider_id,
                summary,
                description,
                location,
                status,
                {str(key): str(value) for key, value in start.items()},
                {str(key): str(value) for key, value in end.items()},
                transparency,
            )
        )
    token = payload.get("nextPageToken")
    if token is not None and _validate_page_token(token) is None:
        raise CalendarProviderError(ProviderFailureCategory.MALFORMED, retryable=False)
    return GoogleEventPage(tuple(result), token)


def _valid_time_object(value: object) -> bool:
    if not isinstance(value, dict) or len(value) > 3:
        return False
    date = value.get("date")
    date_time = value.get("dateTime")
    timezone = value.get("timeZone")
    if date is not None and (not isinstance(date, str) or len(date) > 32):
        return False
    if date_time is not None and (not isinstance(date_time, str) or len(date_time) > 128):
        return False
    if timezone is not None and (not isinstance(timezone, str) or len(timezone) > 128):
        return False
    return (isinstance(date, str) and date != "") ^ (isinstance(date_time, str) and date_time != "")


def _normalize_event(
    item: GoogleEventRecord,
    calendar_id: UUID,
    display_time_zone: str,
    owner: tuple[str, str],
) -> TimedCalendarEvent | AllDayCalendarEvent:
    """Convert the bounded provider record to the domain's neutral event value."""

    if not _valid_zone_info(display_time_zone):
        raise CalendarProviderError(ProviderFailureCategory.MALFORMED, retryable=False)
    start_date = item.start.get("date")
    end_date = item.end.get("date")
    availability = (
        CalendarEventAvailability.FREE
        if item.transparency == "transparent"
        else CalendarEventAvailability.BUSY
    )
    event_id = str(
        uuid5(
            UUID("4c3fef10-5e6e-4b22-9b2d-9df9c3f1f8c2"),
            f"event:{owner[0]}:{owner[1]}:{item.provider_id}",
        )
    )
    if start_date is not None and end_date is not None:
        malformed_dates = False
        first: date | None = None
        last: date | None = None
        try:
            first = date.fromisoformat(start_date)
            last = date.fromisoformat(end_date)
        except ValueError:
            malformed_dates = True
        if malformed_dates or first is None or last is None:
            raise CalendarProviderError(ProviderFailureCategory.MALFORMED, retryable=False)
        if last <= first:
            raise CalendarProviderError(ProviderFailureCategory.MALFORMED, retryable=False)
        event_zone = _event_timezone(
            item.start.get("timeZone"), item.end.get("timeZone"), display_time_zone
        )
        normalized: TimedCalendarEvent | AllDayCalendarEvent | None = None
        invalid = False
        try:
            normalized = normalize_all_day_event(
                event_id=event_id,
                calendar_id=calendar_id,
                title=item.summary,
                description=item.description,
                location=item.location,
                availability=availability,
                start_date=first,
                end_date_exclusive=last,
                event_time_zone=event_zone,
                display_time_zone=display_time_zone,
            )
        except CalendarValidationError:
            invalid = True
        if invalid or normalized is None:
            raise CalendarProviderError(ProviderFailureCategory.MALFORMED, retryable=False)
        return normalized
    start_value = item.start.get("dateTime")
    end_value = item.end.get("dateTime")
    if start_value is None or end_value is None:
        raise CalendarProviderError(ProviderFailureCategory.MALFORMED, retryable=False)
    malformed_datetimes = False
    start: datetime | None = None
    end: datetime | None = None
    try:
        start = _parse_provider_datetime(start_value)
        end = _parse_provider_datetime(end_value)
    except ValueError:
        malformed_datetimes = True
    if malformed_datetimes or start is None or end is None:
        raise CalendarProviderError(ProviderFailureCategory.MALFORMED, retryable=False)
    if end <= start:
        raise CalendarProviderError(ProviderFailureCategory.MALFORMED, retryable=False)
    event_zone = _event_timezone(
        item.start.get("timeZone"), item.end.get("timeZone"), display_time_zone
    )
    normalized_timed: TimedCalendarEvent | AllDayCalendarEvent | None = None
    invalid_timed = False
    try:
        normalized_timed = normalize_timed_event(
            event_id=event_id,
            calendar_id=calendar_id,
            title=item.summary,
            description=item.description,
            location=item.location,
            availability=availability,
            start_instant=start,
            end_instant=end,
            event_time_zone=event_zone,
            display_time_zone=display_time_zone,
        )
    except CalendarValidationError:
        invalid_timed = True
    if invalid_timed or normalized_timed is None:
        raise CalendarProviderError(ProviderFailureCategory.MALFORMED, retryable=False)
    return normalized_timed


def _event_timezone(
    start_timezone: str | None,
    end_timezone: str | None,
    display_timezone: str,
) -> str:
    candidate = start_timezone or end_timezone or display_timezone
    if not _valid_zone_info(candidate):
        raise CalendarProviderError(ProviderFailureCategory.MALFORMED, retryable=False)
    for value in (start_timezone, end_timezone):
        if value is not None:
            if not value:
                raise CalendarProviderError(ProviderFailureCategory.MALFORMED, retryable=False)
            if not _valid_zone_info(value):
                raise CalendarProviderError(ProviderFailureCategory.MALFORMED, retryable=False)
    return candidate


def _parse_provider_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("provider datetime lacks timezone")
    return parsed


def _valid_zone_info(value: str) -> bool:
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return True


def _valid_opaque_value(value: str) -> bool:
    return 43 <= len(value) <= 256 and all(
        char.isalnum() or char in "-_" for char in value
    )


def _valid_callback_uri(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and bool(parsed.netloc)
        and parsed.path == "/api/v1/integrations/calendar/callback"
        and not parsed.query
        and not parsed.fragment
        and parsed.username is None
        and parsed.password is None
    )
