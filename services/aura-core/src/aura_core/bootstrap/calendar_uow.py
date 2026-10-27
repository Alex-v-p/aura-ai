"""Composition helpers for the provider-neutral calendar application boundary.

The API imports this module rather than a repository or provider adapter.  A
deployment with a configured database composes the SQL repository and Google
adapter; tests use a small in-memory metadata repository.  Provider failures
remain behind the domain's inward ``CalendarProvider`` port.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aura_core.domains.integrations.calendar.application import CalendarApplicationService
from aura_core.domains.integrations.calendar.internal import (
    CalendarConflictCursorState,
    CalendarConflictQuery,
    CalendarDiscoveryPage,
    CalendarDiscoveryQuery,
)
from aura_core.domains.integrations.calendar.ports import CalendarProvider
from aura_core.domains.integrations.calendar.public import (
    CalendarAuthorizationAttempt,
    CalendarCallbackResult,
    CalendarConnection,
    CalendarConnectionStatus,
    CalendarCredentialEnvelope,
    CalendarEventPage,
    CalendarQuery,
    CalendarRequestContext,
    CalendarSelection,
    SelectableCalendarPage,
)
from aura_core.domains.integrations.calendar.repository import SqlCalendarRepository
from aura_core.platform.calendar_oauth import (
    GOOGLE_CALENDAR_CALLBACK_PATH,
    GoogleCalendarOAuthConfig,
)
from aura_core.platform.telemetry import (
    MetadataMetrics,
    classify_calendar_terminal,
    current_calendar_trace_context,
    new_span_id,
)
from aura_core.providers.calendars.google_calendar import (
    CredentialMaterial,
    CredentialSealer,
    GoogleCalendarAdapter,
    SealedCredential,
    open_credential,
    seal_credential,
)


class _UnavailableCalendarProvider:
    """Provider port used while the disabled integration has no adapter."""

    async def discover(
        self,
        owner: tuple[str, str],
        *,
        cursor: str | None,
        limit: int,
        request: CalendarRequestContext,
    ) -> CalendarDiscoveryPage:
        del owner, cursor, limit, request
        raise RuntimeError("calendar provider is unavailable")

    async def retrieve(
        self,
        owner: tuple[str, str],
        query: CalendarQuery,
        *,
        request: CalendarRequestContext,
    ) -> CalendarEventPage:
        del owner, query, request
        raise RuntimeError("calendar provider is unavailable")

    async def revoke(self, owner: tuple[str, str], *, request: CalendarRequestContext) -> None:
        del owner, request
        raise RuntimeError("calendar provider is unavailable")


class _ConfiguredProviderFailure(_UnavailableCalendarProvider):
    """Fail-closed boundary when enabled adapter construction returns no port."""


class _MemoryCalendarRepository:
    """Deterministic metadata-only repository for explicit test composition."""

    def __init__(self) -> None:
        self._connections: dict[tuple[str, str], CalendarConnection] = {}
        self._calendars: dict[tuple[str, str], SelectableCalendarPage] = {}
        self._selected: dict[tuple[str, str], tuple[UUID, ...]] = {}
        self._versions: dict[tuple[str, str], int] = {}

    async def get_connection(self, owner: tuple[str, str]) -> CalendarConnection:
        return self._connections.get(
            owner,
            CalendarConnection(
                CalendarConnectionStatus.DISCONNECTED,
                False,
                0,
                None,
                datetime.now(UTC),
            ),
        )

    async def save_connection(
        self,
        owner: tuple[str, str],
        connection: CalendarConnection,
        *,
        expected_version: int | None,
    ) -> CalendarConnection:
        del expected_version
        self._connections[owner] = connection
        return connection

    async def list_calendars(
        self, owner: tuple[str, str], *, cursor: str | None, limit: int
    ) -> SelectableCalendarPage:
        del cursor, limit
        return self._calendars.get(
            owner,
            SelectableCalendarPage((), None, self._versions.get(owner, 1), datetime.now(UTC)),
        )

    async def replace_selection(
        self,
        owner: tuple[str, str],
        calendar_ids: Sequence[UUID],
        *,
        expected_version: int,
    ) -> CalendarSelection:
        current = self._versions.get(owner, 1)
        if expected_version != current:
            raise ValueError("calendar selection version conflict")
        selected = tuple(calendar_ids)
        self._selected[owner] = selected
        self._versions[owner] = current + 1
        return CalendarSelection(selected, current + 1, datetime.now(UTC))

    async def selected_ids(self, owner: tuple[str, str]) -> tuple[UUID, ...]:
        return self._selected.get(owner, ())

    async def provider_calendar_id(self, owner: tuple[str, str], calendar_id: UUID) -> str:
        del owner
        return str(calendar_id)


class _TelemetryAdapter:
    """Translate provider telemetry into the shared metadata-only sink."""

    def __init__(self, metrics: MetadataMetrics | None) -> None:
        self._metrics = metrics

    def record(self, outcome: str, *, retry_count: int, elapsed_ms: float) -> None:
        if self._metrics is None:
            return
        context = current_calendar_trace_context()
        trace_id = context.trace_id if context is not None else __import__("uuid").uuid4().hex
        normalized_outcome, terminal_error = classify_calendar_terminal(outcome)
        retry_class = (
            "rate_limit"
            if outcome == "provider_rate_limit"
            else "transient"
            if outcome == "provider_unavailable"
            else "none"
            if retry_count == 0
            else "unknown"
        )
        rate_limit_state = "limited" if outcome == "provider_rate_limit" else "not_limited"
        self._metrics.record_span(
            "aura.provider.google_calendar",
            "calendar.provider.request",
            elapsed_ms,
            trace_id=trace_id,
            span_id=new_span_id(),
            parent_span_id=context.span_id if context is not None else None,
            dependency="transport_validation",
            outcome=normalized_outcome,
            error_class=terminal_error,
            provider="google_calendar",
            rate_limit_state=rate_limit_state,
            retry_class=retry_class,
        )
        self._metrics.observe(
            "aura.provider.google_calendar",
            "google_calendar_provider_request_outcome",
            1.0,
            trace_id=trace_id,
            dependency="transport_validation",
            outcome=normalized_outcome,
        )
        self._metrics.observe(
            "aura.provider.google_calendar",
            "google_calendar_provider_request_duration_ms",
            elapsed_ms,
            trace_id=trace_id,
            dependency="transport_validation",
            outcome=normalized_outcome,
        )
        self._metrics.observe(
            "aura.provider.google_calendar",
            "google_calendar_provider_retry_count",
            float(max(0, min(retry_count, 3))),
            trace_id=trace_id,
            dependency="transport_validation",
            outcome="ok",
            retry_class=retry_class,
        )
        self._metrics.observe(
            "aura.provider.google_calendar",
            "calendar_rate_limit_state",
            1.0,
            trace_id=trace_id,
            dependency="transport_validation",
            outcome=normalized_outcome,
            rate_limit_state=rate_limit_state,
        )


class _ConflictTelemetryObserver:
    """Record only the domain's local conflict-evaluation work."""

    def __init__(self, metrics: MetadataMetrics | None) -> None:
        self._metrics = metrics

    async def observe_conflict_evaluation(
        self, *, duration_ms: float, outcome: str
    ) -> None:
        if self._metrics is None:
            return
        context = current_calendar_trace_context()
        trace_id = context.trace_id if context is not None else __import__("uuid").uuid4().hex
        normalized_outcome, terminal_error = classify_calendar_terminal(outcome)
        self._metrics.record_span(
            "aura.automation.calendar_conflict",
            "calendar.conflicts.evaluate",
            duration_ms,
            trace_id=trace_id,
            span_id=new_span_id(),
            parent_span_id=context.span_id if context is not None else None,
            dependency="application_validation",
            outcome=normalized_outcome,
            error_class=terminal_error,
        )
        self._metrics.observe(
            "aura.automation.calendar_conflict",
            "calendar_conflict_evaluation_outcome",
            1.0,
            trace_id=trace_id,
            dependency="application_validation",
            outcome=normalized_outcome,
            **({"error_class": terminal_error} if terminal_error is not None else {}),
        )


class _CalendarCredentialCodec:
    """Bridge the platform key ring to the domain's opaque envelope port."""

    def __init__(self, settings: Any) -> None:
        config = settings.calendar_oauth_config()
        if config.key_ring is None:
            raise RuntimeError("calendar credential key ring is unavailable")
        self._key_ring = config.key_ring

    @property
    def active_key_id(self) -> str:
        return self._key_ring.active_key_id

    @staticmethod
    def _owner(owner: tuple[str, str]) -> str:
        return f"{owner[0]}:{owner[1]}"

    async def seal(
        self, owner: tuple[str, str], connection_id: UUID, credential: object
    ) -> CalendarCredentialEnvelope:
        if not isinstance(credential, CredentialMaterial):
            raise RuntimeError("calendar credential material is unavailable")
        envelope = seal_credential(
            self._owner(owner), str(connection_id), credential, self._key_ring
        )
        digest = hashlib.sha256(
            CredentialSealer.associated_data(self._owner(owner), str(connection_id))
        ).digest()
        return CalendarCredentialEnvelope(
            envelope.ciphertext, envelope.nonce, envelope.key_id, digest
        )

    async def open(
        self, owner: tuple[str, str], connection_id: UUID, envelope: CalendarCredentialEnvelope
    ) -> object:
        expected = hashlib.sha256(
            CredentialSealer.associated_data(self._owner(owner), str(connection_id))
        ).digest()
        if envelope.associated_data_digest != expected:
            raise RuntimeError("calendar credential authentication failed")
        return open_credential(
            self._owner(owner),
            str(connection_id),
            SealedCredential(envelope.key_id, envelope.nonce, envelope.ciphertext),
            self._key_ring,
        )


class _RepositoryCredentialLoader:
    """Open credentials through the domain repository and CAS key rotation."""

    def __init__(self, repository: Any, codec: _CalendarCredentialCodec) -> None:
        self._repository = repository
        self._codec = codec

    async def __call__(self, owner: tuple[str, str]) -> CredentialMaterial:
        stored = await self._repository.load_credential(owner)
        if stored is None:
            raise RuntimeError("calendar credential unavailable")
        connection_id, envelope = stored
        material = await self._codec.open(owner, connection_id, envelope)
        replacement = await self._codec.seal(owner, connection_id, material)
        if replacement != envelope:
            swapped = await self._repository.compare_and_swap_credential(
                owner, connection_id, envelope, replacement
            )
            if not swapped:
                # A concurrent rotation won the CAS.  Never continue using a
                # retained-key plaintext from the stale read: reload the
                # current envelope, require the active key, then authenticate
                # and decrypt that exact current value.
                current = await self._repository.load_credential(owner)
                if current is None or current[0] != connection_id:
                    raise RuntimeError("calendar credential rotation conflict")
                current_envelope = current[1]
                if current_envelope.key_id != self._codec.active_key_id:
                    raise RuntimeError("calendar credential rotation conflict")
                material = await self._codec.open(owner, connection_id, current_envelope)
        if not isinstance(material, CredentialMaterial):
            raise RuntimeError("calendar credential unavailable")
        return material


class _CalendarTransientStore:
    """Valkey-backed one-shot attempts, callback results, and opaque cursors."""

    _MAX_TRANSIENT_BYTES = 32 * 1024
    _MAX_CURSOR_STATE_BYTES = 8 * 1024
    _MAX_CURSOR_RELATIONSHIPS = 200
    _MAX_CURSOR_TEXT = 1024

    def __init__(
        self,
        client: Any,
        codec: _CalendarCredentialCodec | None,
        *,
        testing: bool = True,
    ) -> None:
        self._client = client
        self._codec = codec
        self._testing = testing
        self._memory: dict[str, tuple[float, dict[str, Any]]] = {}
        self._memory_lock = asyncio.Lock()

    def _require_backend(self) -> None:
        if self._client is None and not self._testing:
            raise RuntimeError("calendar transient store is unavailable")

    @asynccontextmanager
    async def _mutation(self) -> AsyncGenerator[None]:
        self._require_backend()
        if self._client is None:
            async with self._memory_lock:
                yield
        else:
            yield

    @staticmethod
    def _digest(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    @staticmethod
    def _owner_key(owner: tuple[str, str]) -> str:
        return f"{owner[0]}:{owner[1]}"

    async def _put(self, key: str, value: dict[str, Any], ttl: int = 600) -> None:
        self._require_backend()
        if self._client is not None:
            encoded = json.dumps(value, separators=(",", ":"))
            if len(encoded.encode("utf-8")) > self._MAX_TRANSIENT_BYTES:
                raise RuntimeError("calendar transient value is too large")
            await self._client.setex(key, ttl, encoded)
        else:
            self._memory[key] = (datetime.now(UTC).timestamp() + ttl, value)

    async def _get(self, key: str, *, consume: bool) -> dict[str, Any] | None:
        self._require_backend()
        if self._client is not None:
            raw = await (self._client.getdel(key) if consume else self._client.get(key))
            if raw is None:
                return None
            if isinstance(raw, bytes):
                try:
                    raw = raw.decode("utf-8")
                except UnicodeDecodeError:
                    raw = None
            if not isinstance(raw, str) or len(raw.encode("utf-8")) > self._MAX_TRANSIENT_BYTES:
                raise RuntimeError("calendar transient value is invalid")
            try:
                value = json.loads(raw)
            except (TypeError, ValueError, json.JSONDecodeError):
                value = None
            if not isinstance(value, dict):
                raise RuntimeError("calendar transient value is invalid")
            return cast(dict[str, Any], value)
        entry = self._memory.get(key)
        if entry is None:
            return None
        expires, value = entry
        if expires <= datetime.now(UTC).timestamp():
            self._memory.pop(key, None)
            return None
        if consume:
            self._memory.pop(key, None)
        return value

    async def _delete(self, key: str) -> bool:
        """Delete one transient entry and report whether it still existed."""

        self._require_backend()
        if self._client is not None:
            return await self._client.getdel(key) is not None
        return self._memory.pop(key, None) is not None

    @staticmethod
    def _remaining_ttl(expires_at: datetime) -> int:
        remaining = expires_at.astimezone(UTC).timestamp() - datetime.now(UTC).timestamp()
        if remaining <= 0:
            raise RuntimeError("calendar authorization state is expired")
        return max(1, min(600, int(remaining + 0.999)))

    async def _delete_attempt_receipts(
        self,
        owner_session: str,
        *,
        handle: str,
        state: str,
    ) -> None:
        """Delete every transient receipt associated with one superseded attempt."""

        keys = (
            f"aura:calendar:attempt:{self._digest(handle)}",
            f"aura:calendar:state:{self._digest(state)}",
            f"aura:calendar:active:{owner_session}",
            f"aura:calendar:callback:{owner_session}",
            f"aura:calendar:callback-attempt:{owner_session}",
            f"aura:calendar:completion:{owner_session}",
            f"aura:calendar:idempotency:{owner_session}",
            f"aura:calendar:receipt:{owner_session}",
        )
        for key in keys:
            await self._delete(key)

    async def _create_valkey(self, attempt: CalendarAuthorizationAttempt) -> None:
        """Supersede and publish one attempt in a single Valkey transaction."""

        pipeline_factory = getattr(self._client, "pipeline", None)
        if pipeline_factory is None:
            if not self._testing:
                raise RuntimeError("calendar authorization state is unavailable")
            await self._create(attempt)
            return

        owner_session = self._digest(f"{self._owner_key(attempt.owner)}:{attempt.session_id}")
        current_key = f"aura:calendar:current:{owner_session}"
        active_key = f"aura:calendar:active:{owner_session}"
        callback_key = f"aura:calendar:callback:{owner_session}"
        callback_attempt_key = f"aura:calendar:callback-attempt:{owner_session}"
        completion_key = f"aura:calendar:completion:{owner_session}"
        idempotency_key = f"aura:calendar:idempotency:{owner_session}"
        receipt_key = f"aura:calendar:receipt:{owner_session}"
        shared_keys = (
            current_key,
            active_key,
            callback_key,
            callback_attempt_key,
            completion_key,
            idempotency_key,
            receipt_key,
        )
        attempt_key = f"aura:calendar:attempt:{self._digest(attempt.handle)}"
        state_key = f"aura:calendar:state:{self._digest(attempt.state)}"
        payload = {
            "attemptId": str(attempt.attempt_id),
            "issuer": attempt.owner[0],
            "subject": attempt.owner[1],
            "session": attempt.session_id,
            "state": attempt.state,
            "nonce": attempt.nonce,
            "handle": attempt.handle,
            "expiresAt": attempt.expires_at.isoformat(),
            "verifier": attempt.verifier,
        }
        encoded_payload = json.dumps(payload, separators=(",", ":"))
        if len(encoded_payload.encode("utf-8")) > self._MAX_TRANSIENT_BYTES:
            raise RuntimeError("calendar transient value is too large")
        encoded_current = json.dumps({"handle": attempt.handle}, separators=(",", ":"))
        encoded_active = json.dumps(
            {"handle": attempt.handle, "attemptId": str(attempt.attempt_id)},
            separators=(",", ":"),
        )
        encoded_state = json.dumps({"handle": attempt.handle}, separators=(",", ":"))

        # A bounded retry handles only optimistic transaction contention. Any
        # transport or protocol failure remains a closed authorization error.
        for _ in range(3):
            ttl = self._remaining_ttl(attempt.expires_at)
            pipeline = pipeline_factory(transaction=True)
            old_attempt_key: str | None = None
            old_state_key: str | None = None
            try:
                await pipeline.watch(*shared_keys)
                current = self._raw_json(await pipeline.get(current_key))
                previous_handle = str(current.get("handle", "")) if current else ""
                if previous_handle:
                    old_attempt_key = (
                        f"aura:calendar:attempt:{self._digest(previous_handle)}"
                    )
                    await pipeline.watch(old_attempt_key)
                    previous = self._raw_json(await pipeline.get(old_attempt_key))
                    previous_state = str(previous.get("state", "")) if previous else ""
                    if previous_state:
                        old_state_key = (
                            f"aura:calendar:state:{self._digest(previous_state)}"
                        )
                        await pipeline.watch(old_state_key)

                delete_keys = list(shared_keys)
                if old_attempt_key is not None:
                    delete_keys.append(old_attempt_key)
                if old_state_key is not None:
                    delete_keys.append(old_state_key)
                pipeline.multi()
                pipeline.delete(*delete_keys)
                pipeline.setex(attempt_key, ttl, encoded_payload)
                pipeline.setex(current_key, ttl, encoded_current)
                pipeline.setex(active_key, ttl, encoded_active)
                pipeline.setex(state_key, ttl, encoded_state)
                executed = await pipeline.execute()
                if executed is None:
                    reset = getattr(pipeline, "reset", None)
                    if reset is not None:
                        await reset()
                    continue
                return
            except Exception as exc:
                reset = getattr(pipeline, "reset", None)
                if reset is not None:
                    await reset()
                if exc.__class__.__name__ == "WatchError":
                    continue
                if isinstance(exc, RuntimeError):
                    raise
                raise RuntimeError("calendar authorization state is unavailable") from exc
        raise RuntimeError("calendar authorization state is unavailable")

    async def _create(self, attempt: CalendarAuthorizationAttempt) -> None:
        owner_session = self._digest(f"{self._owner_key(attempt.owner)}:{attempt.session_id}")
        current_key = f"aura:calendar:current:{owner_session}"
        previous = await self._get(current_key, consume=False)
        if previous is not None:
            previous_attempt = await self._get(
                f"aura:calendar:attempt:{self._digest(str(previous.get('handle', '')))}",
                consume=False,
            )
            if previous_attempt is not None:
                await self._delete_attempt_receipts(
                    owner_session,
                    handle=str(previous_attempt.get("handle", "")),
                    state=str(previous_attempt.get("state", "")),
                )
            else:
                await self._delete(current_key)
        # Navigation consumes the current marker before the provider callback
        # completes.  Clear session receipts independently so a newer attempt
        # also invalidates an in-flight callback from that consumed attempt.
        for key in (
            f"aura:calendar:active:{owner_session}",
            f"aura:calendar:callback:{owner_session}",
            f"aura:calendar:callback-attempt:{owner_session}",
            f"aura:calendar:completion:{owner_session}",
            f"aura:calendar:idempotency:{owner_session}",
            f"aura:calendar:receipt:{owner_session}",
        ):
            await self._delete(key)
        ttl = self._remaining_ttl(attempt.expires_at)
        await self._put(
            f"aura:calendar:attempt:{self._digest(attempt.handle)}",
            {
                "attemptId": str(attempt.attempt_id),
                "issuer": attempt.owner[0],
                "subject": attempt.owner[1],
                "session": attempt.session_id,
                "state": attempt.state,
                "nonce": attempt.nonce,
                "handle": attempt.handle,
                "expiresAt": attempt.expires_at.isoformat(),
                "verifier": attempt.verifier,
            },
            ttl=ttl,
        )
        await self._put(current_key, {"handle": attempt.handle}, ttl=ttl)
        await self._put(
            f"aura:calendar:active:{owner_session}",
            {"handle": attempt.handle, "attemptId": str(attempt.attempt_id)},
            ttl=ttl,
        )
        await self._put(
            f"aura:calendar:state:{self._digest(attempt.state)}",
            {"handle": attempt.handle},
            ttl=ttl,
        )

    async def create(self, attempt: CalendarAuthorizationAttempt) -> None:
        async with self._mutation():
            if self._client is not None:
                await self._create_valkey(attempt)
            else:
                await self._create(attempt)

    @staticmethod
    def _attempt(data: dict[str, Any]) -> CalendarAuthorizationAttempt:
        return CalendarAuthorizationAttempt(
            UUID(str(data["attemptId"])),
            (str(data["issuer"]), str(data["subject"])),
            str(data["session"]),
            str(data["state"]),
            str(data["nonce"]),
            str(data["handle"]),
            datetime.fromisoformat(str(data["expiresAt"])),
            str(data.get("verifier", "")),
        )

    async def _consume_navigation(
        self, owner: tuple[str, str], session_id: str, handle: str
    ) -> CalendarAuthorizationAttempt:
        owner_session = self._digest(f"{self._owner_key(owner)}:{session_id}")
        current_key = f"aura:calendar:current:{owner_session}"
        current = await self._get(current_key, consume=False)
        if current is None or current.get("handle") != handle:
            raise RuntimeError("calendar authorization attempt is invalid")
        active = await self._get(
            f"aura:calendar:active:{owner_session}", consume=False
        )
        if active is None or active.get("handle") != handle:
            raise RuntimeError("calendar authorization attempt is invalid")
        # Keep the attempt payload until the provider callback consumes its
        # state marker.  Only the owner/session navigation marker is one-shot.
        data = await self._get(f"aura:calendar:attempt:{self._digest(handle)}", consume=False)
        if (
            data is None
            or data.get("issuer") != owner[0]
            or data.get("subject") != owner[1]
            or data.get("session") != session_id
            or active.get("attemptId") != data.get("attemptId")
        ):
            raise RuntimeError("calendar authorization attempt is invalid")
        attempt = self._attempt(data)
        self._remaining_ttl(attempt.expires_at)
        if not await self._delete(current_key):
            raise RuntimeError("calendar authorization attempt is invalid")
        return attempt

    async def _consume_navigation_valkey(
        self, owner: tuple[str, str], session_id: str, handle: str
    ) -> CalendarAuthorizationAttempt:
        """Compare and consume the exact current handle in one transaction."""

        pipeline_factory = getattr(self._client, "pipeline", None)
        if pipeline_factory is None:
            raise RuntimeError("calendar authorization state is unavailable")
        owner_session = self._digest(f"{self._owner_key(owner)}:{session_id}")
        current_key = f"aura:calendar:current:{owner_session}"
        attempt_key = f"aura:calendar:attempt:{self._digest(handle)}"
        active_key = f"aura:calendar:active:{owner_session}"
        pipeline = pipeline_factory(transaction=True)
        try:
            await pipeline.watch(current_key, attempt_key, active_key)
            current = self._raw_json(await pipeline.get(current_key))
            if current is None or current.get("handle") != handle:
                raise RuntimeError("calendar authorization attempt is invalid")
            active = self._raw_json(await pipeline.get(active_key))
            if active is None or active.get("handle") != handle:
                raise RuntimeError("calendar authorization attempt is invalid")
            data = self._raw_json(await pipeline.get(attempt_key))
            if (
                data is None
                or data.get("issuer") != owner[0]
                or data.get("subject") != owner[1]
                or data.get("session") != session_id
                or active.get("attemptId") != data.get("attemptId")
            ):
                raise RuntimeError("calendar authorization attempt is invalid")
            attempt = self._attempt(data)
            self._remaining_ttl(attempt.expires_at)
            pipeline.multi()
            pipeline.delete(current_key)
            executed = await pipeline.execute()
            if executed is None:
                raise RuntimeError("calendar authorization attempt is invalid")
            return attempt
        except RuntimeError:
            reset = getattr(pipeline, "reset", None)
            if reset is not None:
                await reset()
            raise
        except Exception as exc:
            reset = getattr(pipeline, "reset", None)
            if reset is not None:
                await reset()
            raise RuntimeError("calendar authorization attempt is invalid") from exc

    async def consume_navigation(
        self, owner: tuple[str, str], session_id: str, handle: str
    ) -> CalendarAuthorizationAttempt:
        if self._client is not None:
            return await self._consume_navigation_valkey(owner, session_id, handle)
        async with self._mutation():
            return await self._consume_navigation(owner, session_id, handle)

    async def _consume_state(
        self, state: str, *, owner: tuple[str, str] | None = None
    ) -> CalendarAuthorizationAttempt:
        state_key = f"aura:calendar:state:{self._digest(state)}"
        marker = await self._get(state_key, consume=False)
        if marker is None:
            raise RuntimeError("calendar authorization callback is invalid")
        handle = str(marker.get("handle", ""))
        data = await self._get(
            f"aura:calendar:attempt:{self._digest(handle)}", consume=False
        )
        if (
            data is None
            or data.get("state") != state
            or (owner is not None and (data.get("issuer"), data.get("subject")) != owner)
        ):
            raise RuntimeError("calendar authorization callback is invalid")
        attempt = self._attempt(data)
        self._remaining_ttl(attempt.expires_at)
        active_owner = (str(data["issuer"]), str(data["subject"]))
        active_session = str(data["session"])
        active_digest = self._digest(f"{self._owner_key(active_owner)}:{active_session}")
        active_key = f"aura:calendar:active:{active_digest}"
        active = await self._get(active_key, consume=False)
        if active is None or active.get("handle") != data.get("handle"):
            raise RuntimeError("calendar authorization callback is invalid")
        if not await self._delete(state_key):
            raise RuntimeError("calendar authorization callback is invalid")
        await self._delete(f"aura:calendar:attempt:{self._digest(handle)}")
        return attempt

    async def consume_state(
        self, state: str, *, owner: tuple[str, str] | None = None
    ) -> CalendarAuthorizationAttempt:
        async with self._mutation():
            return await self._consume_state(state, owner=owner)

    async def _callback_payload(
        self, result: CalendarCallbackResult
    ) -> tuple[str, str, dict[str, Any], int]:
        ttl = self._remaining_ttl(result.expires_at)
        owner_session = self._digest(f"{self._owner_key(result.owner)}:{result.session_id}")
        payload: dict[str, Any] = {
            "attemptId": str(result.attempt_id),
            "issuer": result.owner[0],
            "subject": result.owner[1],
            "session": result.session_id,
            "expiresAt": result.expires_at.isoformat(),
            "error": result.error,
        }
        if result.credential is not None:
            if self._codec is None:
                raise RuntimeError("calendar callback storage is unavailable")
            envelope = await self._codec.seal(result.owner, result.attempt_id, result.credential)
            payload.update(
                {
                    "ciphertext": envelope.ciphertext.hex(),
                    "nonce": envelope.nonce.hex(),
                    "keyId": envelope.key_id,
                    "digest": envelope.associated_data_digest.hex(),
                }
            )
        return owner_session, str(result.attempt_id), payload, ttl

    @staticmethod
    def _raw_json(raw: object) -> dict[str, Any] | None:
        if raw is None:
            return None
        if isinstance(raw, bytes):
            try:
                raw = raw.decode("utf-8")
            except UnicodeDecodeError:
                raw = None
        if not isinstance(raw, str) or (
            len(raw.encode("utf-8")) > _CalendarTransientStore._MAX_TRANSIENT_BYTES
        ):
            raise RuntimeError("calendar transient value is invalid")
        try:
            value = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            value = None
        if not isinstance(value, dict):
            raise RuntimeError("calendar transient value is invalid")
        return cast(dict[str, Any], value)

    async def _put_callback_valkey(
        self, owner_session: str, attempt_id: str, payload: dict[str, Any], ttl: int
    ) -> None:
        pipeline_factory = getattr(self._client, "pipeline", None)
        if pipeline_factory is None:
            raise RuntimeError("calendar callback storage is unavailable")
        active_key = f"aura:calendar:active:{owner_session}"
        callback_key = f"aura:calendar:callback:{owner_session}"
        marker_key = f"aura:calendar:callback-attempt:{owner_session}"
        pipeline = pipeline_factory(transaction=True)
        try:
            await pipeline.watch(active_key)
            active = self._raw_json(await pipeline.get(active_key))
            if active is None or active.get("attemptId") != attempt_id:
                raise RuntimeError("calendar authorization callback is invalid")
            pipeline.multi()
            pipeline.setex(callback_key, ttl, json.dumps(payload, separators=(",", ":")))
            pipeline.setex(
                marker_key,
                ttl,
                json.dumps({"attemptId": attempt_id}, separators=(",", ":")),
            )
            executed = await pipeline.execute()
            if executed is None:
                raise RuntimeError("calendar authorization callback is invalid")
        except RuntimeError:
            reset = getattr(pipeline, "reset", None)
            if reset is not None:
                await reset()
            raise
        except Exception as exc:
            reset = getattr(pipeline, "reset", None)
            if reset is not None:
                await reset()
            raise RuntimeError("calendar authorization callback is invalid") from exc

    async def _put_callback(self, result: CalendarCallbackResult) -> None:
        owner_session, attempt_id, payload, ttl = await self._callback_payload(result)
        if self._client is not None:
            await self._put_callback_valkey(owner_session, attempt_id, payload, ttl)
            return
        active = await self._get(f"aura:calendar:active:{owner_session}", consume=False)
        if active is None or active.get("attemptId") != attempt_id:
            raise RuntimeError("calendar authorization callback is invalid")
        await self._put(f"aura:calendar:callback:{owner_session}", payload, ttl=ttl)
        await self._put(
            f"aura:calendar:callback-attempt:{owner_session}",
            {"attemptId": attempt_id},
            ttl=ttl,
        )

    async def put_callback(self, result: CalendarCallbackResult) -> None:
        async with self._mutation():
            await self._put_callback(result)

    async def _callback_data(
        self, owner: tuple[str, str], session_id: str
    ) -> tuple[str, str, dict[str, Any]]:
        owner_session = self._digest(f"{self._owner_key(owner)}:{session_id}")
        callback_key = f"aura:calendar:callback:{owner_session}"
        marker_key = f"aura:calendar:callback-attempt:{owner_session}"
        data = await self._get(callback_key, consume=False)
        marker = await self._get(marker_key, consume=False)
        if (
            data is None
            or marker is None
            or (data.get("issuer"), data.get("subject")) != owner
            or data.get("session") != session_id
            or marker.get("attemptId") != data.get("attemptId")
        ):
            raise RuntimeError("calendar authorization callback is invalid")
        expires_at = datetime.fromisoformat(str(data["expiresAt"]))
        self._remaining_ttl(expires_at)
        return callback_key, marker_key, data

    async def _callback_result(
        self, data: dict[str, Any], owner: tuple[str, str], session_id: str
    ) -> CalendarCallbackResult:
        expires_at = datetime.fromisoformat(str(data["expiresAt"]))
        credential: object | None = None
        if data.get("ciphertext") is not None:
            if self._codec is None:
                raise RuntimeError("calendar callback storage is unavailable")
            credential = await self._codec.open(
                owner,
                UUID(str(data["attemptId"])),
                CalendarCredentialEnvelope(
                    bytes.fromhex(str(data["ciphertext"])),
                    bytes.fromhex(str(data["nonce"])),
                    str(data["keyId"]),
                    bytes.fromhex(str(data["digest"])),
                ),
            )
        return CalendarCallbackResult(
            UUID(str(data["attemptId"])),
            owner,
            session_id,
            credential,
            expires_at,
            data.get("error"),
        )

    async def peek_callback(
        self, owner: tuple[str, str], session_id: str
    ) -> CalendarCallbackResult:
        _, _, data = await self._callback_data(owner, session_id)
        return await self._callback_result(data, owner, session_id)

    async def consume_callback(
        self, owner: tuple[str, str], session_id: str, attempt_id: UUID
    ) -> CalendarCallbackResult:
        if self._client is not None:
            return await self._consume_callback_valkey(owner, session_id, attempt_id)
        async with self._mutation():
            return await self._consume_callback_memory(owner, session_id, attempt_id)

    async def _consume_callback_valkey(
        self, owner: tuple[str, str], session_id: str, attempt_id: UUID
    ) -> CalendarCallbackResult:
        pipeline_factory = getattr(self._client, "pipeline", None)
        if pipeline_factory is None:
            raise RuntimeError("calendar callback storage is unavailable")
        owner_session = self._digest(f"{self._owner_key(owner)}:{session_id}")
        active_key = f"aura:calendar:active:{owner_session}"
        callback_key = f"aura:calendar:callback:{owner_session}"
        marker_key = f"aura:calendar:callback-attempt:{owner_session}"
        pipeline = pipeline_factory(transaction=True)
        try:
            await pipeline.watch(active_key, callback_key, marker_key)
            active = self._raw_json(await pipeline.get(active_key))
            data = self._raw_json(await pipeline.get(callback_key))
            marker = self._raw_json(await pipeline.get(marker_key))
            expected = str(attempt_id)
            if (
                active is None
                or active.get("attemptId") != expected
                or data is None
                or marker is None
                or marker.get("attemptId") != expected
                or (data.get("issuer"), data.get("subject")) != owner
                or data.get("session") != session_id
                or data.get("attemptId") != expected
            ):
                raise RuntimeError("calendar authorization callback is invalid")
            result = await self._callback_result(data, owner, session_id)
            pipeline.multi()
            pipeline.delete(callback_key, marker_key, active_key)
            executed = await pipeline.execute()
            if executed is None:
                raise RuntimeError("calendar authorization callback is invalid")
            return result
        except RuntimeError:
            reset = getattr(pipeline, "reset", None)
            if reset is not None:
                await reset()
            raise
        except Exception as exc:
            reset = getattr(pipeline, "reset", None)
            if reset is not None:
                await reset()
            raise RuntimeError("calendar authorization callback is invalid") from exc

    async def _consume_callback_memory(
        self, owner: tuple[str, str], session_id: str, attempt_id: UUID
    ) -> CalendarCallbackResult:
        callback_key, marker_key, data = await self._callback_data(owner, session_id)
        if str(attempt_id) != str(data["attemptId"]):
            raise RuntimeError("calendar authorization callback is invalid")
        result = await self._callback_result(data, owner, session_id)
        if not await self._delete(callback_key):
            raise RuntimeError("calendar authorization callback is invalid")
        await self._delete(marker_key)
        await self._delete(
            f"aura:calendar:active:{self._digest(f'{self._owner_key(owner)}:{session_id}')}"
        )
        return result

    async def load_attempt(
        self, owner: tuple[str, str], session_id: str, attempt_id: UUID
    ) -> CalendarAuthorizationAttempt:
        owner_session = self._digest(f"{self._owner_key(owner)}:{session_id}")
        current = await self._get(f"aura:calendar:current:{owner_session}", consume=False)
        if current is None:
            raise RuntimeError("calendar authorization attempt is invalid")
        data = await self._get(
            f"aura:calendar:attempt:{self._digest(str(current.get('handle', '')))}",
            consume=False,
        )
        if data is None:
            raise RuntimeError("calendar authorization attempt is invalid")
        attempt = self._attempt(data)
        if (
            attempt.owner != owner
            or attempt.session_id != session_id
            or attempt.attempt_id != attempt_id
        ):
            raise RuntimeError("calendar authorization attempt is invalid")
        self._remaining_ttl(attempt.expires_at)
        return attempt

    async def _ensure_active_valkey(self, attempt: CalendarAuthorizationAttempt) -> None:
        pipeline_factory = getattr(self._client, "pipeline", None)
        if pipeline_factory is None:
            raise RuntimeError("calendar authorization state is unavailable")
        owner_session = self._digest(f"{self._owner_key(attempt.owner)}:{attempt.session_id}")
        active_key = f"aura:calendar:active:{owner_session}"
        pipeline = pipeline_factory(transaction=True)
        try:
            await pipeline.watch(active_key)
            active = self._raw_json(await pipeline.get(active_key))
            if (
                active is None
                or active.get("handle") != attempt.handle
                or active.get("attemptId") != str(attempt.attempt_id)
            ):
                raise RuntimeError("calendar authorization attempt is invalid")
            pipeline.multi()
            pipeline.get(active_key)
            executed = await pipeline.execute()
            if not executed:
                raise RuntimeError("calendar authorization attempt is invalid")
            confirmed = self._raw_json(executed[0])
            if (
                confirmed is None
                or confirmed.get("handle") != attempt.handle
                or confirmed.get("attemptId") != str(attempt.attempt_id)
            ):
                raise RuntimeError("calendar authorization attempt is invalid")
        except RuntimeError:
            reset = getattr(pipeline, "reset", None)
            if reset is not None:
                await reset()
            raise
        except Exception as exc:
            reset = getattr(pipeline, "reset", None)
            if reset is not None:
                await reset()
            raise RuntimeError("calendar authorization attempt is invalid") from exc

    async def ensure_active_attempt(self, attempt: CalendarAuthorizationAttempt) -> None:
        if self._client is not None:
            await self._ensure_active_valkey(attempt)
            return
        async with self._mutation():
            owner_session = self._digest(
                f"{self._owner_key(attempt.owner)}:{attempt.session_id}"
            )
            active = await self._get(
                f"aura:calendar:active:{owner_session}", consume=False
            )
            if (
                active is None
                or active.get("handle") != attempt.handle
                or active.get("attemptId") != str(attempt.attempt_id)
            ):
                raise RuntimeError("calendar authorization attempt is invalid")

    async def finalize_navigation_attempt(
        self,
        owner: tuple[str, str],
        session_id: str,
        attempt_id: UUID,
        handle: str,
    ) -> None:
        """Atomically confirm that URL generation did not outlive supersession."""

        owner_session = self._digest(f"{self._owner_key(owner)}:{session_id}")
        if self._client is not None:
            pipeline_factory = getattr(self._client, "pipeline", None)
            if pipeline_factory is None:
                raise RuntimeError("calendar authorization state is unavailable")
            active_key = f"aura:calendar:active:{owner_session}"
            pipeline = pipeline_factory(transaction=True)
            try:
                await pipeline.watch(active_key)
                active = self._raw_json(await pipeline.get(active_key))
                if (
                    active is None
                    or active.get("handle") != handle
                    or active.get("attemptId") != str(attempt_id)
                ):
                    raise RuntimeError("calendar authorization attempt is invalid")
                pipeline.multi()
                pipeline.get(active_key)
                executed = await pipeline.execute()
                confirmed = self._raw_json(executed[0]) if executed else None
                if (
                    confirmed is None
                    or confirmed.get("handle") != handle
                    or confirmed.get("attemptId") != str(attempt_id)
                ):
                    raise RuntimeError("calendar authorization attempt is invalid")
                return
            except RuntimeError:
                reset = getattr(pipeline, "reset", None)
                if reset is not None:
                    await reset()
                raise
            except Exception as exc:
                reset = getattr(pipeline, "reset", None)
                if reset is not None:
                    await reset()
                raise RuntimeError("calendar authorization attempt is invalid") from exc
        async with self._mutation():
            active = await self._get(
                f"aura:calendar:active:{owner_session}", consume=False
            )
            if (
                active is None
                or active.get("handle") != handle
                or active.get("attemptId") != str(attempt_id)
            ):
                raise RuntimeError("calendar authorization attempt is invalid")

    @staticmethod
    def _query_key(query: object) -> tuple[str, str]:
        """Return a stable scope and normalized binding for a cursor query."""

        def normalized_query(value: CalendarQuery) -> str:
            return "|".join(
                (
                    value.range_start.isoformat(),
                    value.range_end.isoformat(),
                    value.display_time_zone,
                    ",".join(sorted(map(str, value.calendar_ids))),
                    str(value.limit),
                )
            )

        if isinstance(query, CalendarQuery):
            return "events", hashlib.sha256(normalized_query(query).encode("utf-8")).hexdigest()
        if isinstance(query, CalendarDiscoveryQuery):
            return "calendar-list", hashlib.sha256(str(query.limit).encode("ascii")).hexdigest()
        if isinstance(query, CalendarConflictQuery):
            normalized = f"{normalized_query(query.query)}|conflict-limit={query.conflict_limit}"
            return "conflicts", hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        raise RuntimeError("calendar cursor query is invalid")

    @classmethod
    def _encode_cursor_value(cls, value: object) -> tuple[str, str]:
        if isinstance(value, str):
            if not value or len(value) > cls._MAX_CURSOR_TEXT:
                raise RuntimeError("calendar cursor is invalid")
            return "string", value
        if not isinstance(value, CalendarConflictCursorState):
            raise RuntimeError("calendar cursor is invalid")
        if not 0 <= value.pages_consumed <= cls._MAX_CURSOR_RELATIONSHIPS:
            raise RuntimeError("calendar cursor is invalid")
        if value.provider_cursor is not None and (
            not value.provider_cursor
            or len(value.provider_cursor) > cls._MAX_CURSOR_TEXT
        ):
            raise RuntimeError("calendar cursor is invalid")
        if len(value.emitted_relationships) > cls._MAX_CURSOR_RELATIONSHIPS or any(
            not item or len(item) > cls._MAX_CURSOR_TEXT
            for item in value.emitted_relationships
        ):
            raise RuntimeError("calendar cursor is invalid")
        encoded = json.dumps(
            {
                "provider_cursor": value.provider_cursor,
                "pages_consumed": value.pages_consumed,
                "emitted_relationships": list(value.emitted_relationships),
            },
            separators=(",", ":"),
        )
        if len(encoded.encode("utf-8")) > cls._MAX_CURSOR_STATE_BYTES:
            raise RuntimeError("calendar cursor is invalid")
        return "conflict_state", encoded

    @classmethod
    def _decode_cursor_value(cls, value_type: object, value: object) -> object:
        if value_type == "string":
            if not isinstance(value, str) or not value or len(value) > cls._MAX_CURSOR_TEXT:
                raise RuntimeError("calendar cursor is invalid")
            return value
        if value_type != "conflict_state" or not isinstance(value, str):
            raise RuntimeError("calendar cursor is invalid")
        if len(value.encode("utf-8")) > cls._MAX_CURSOR_STATE_BYTES:
            raise RuntimeError("calendar cursor is invalid")
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            decoded = None
        if not isinstance(decoded, dict):
            raise RuntimeError("calendar cursor is invalid")
        decoded_map = cast(dict[str, object], decoded)
        if set(decoded_map) != {
            "provider_cursor",
            "pages_consumed",
            "emitted_relationships",
        }:
            raise RuntimeError("calendar cursor is invalid")
        provider_cursor = decoded_map["provider_cursor"]
        pages_consumed = decoded_map["pages_consumed"]
        emitted_value = decoded_map["emitted_relationships"]
        if provider_cursor is not None and (
            type(provider_cursor) is not str
            or not provider_cursor
            or len(provider_cursor) > cls._MAX_CURSOR_TEXT
        ):
            raise RuntimeError("calendar cursor is invalid")
        if (
            type(pages_consumed) is not int
            or not 0 <= pages_consumed <= cls._MAX_CURSOR_RELATIONSHIPS
        ):
            raise RuntimeError("calendar cursor is invalid")
        if not isinstance(emitted_value, list):
            raise RuntimeError("calendar cursor is invalid")
        emitted = cast(list[object], emitted_value)
        if len(emitted) > cls._MAX_CURSOR_RELATIONSHIPS or any(
            type(item) is not str
            or not item
            or len(item) > cls._MAX_CURSOR_TEXT
            for item in emitted
        ):
            raise RuntimeError("calendar cursor is invalid")
        return CalendarConflictCursorState(
            provider_cursor,
            pages_consumed,
            tuple(cast(str, item) for item in emitted),
        )

    async def put(self, owner: tuple[str, str], query: object, value: object) -> str:
        scope, query_key = self._query_key(query)
        value_type, encoded_value = self._encode_cursor_value(value)
        async with self._mutation():
            if value_type == "string":
                existing = await self._get(
                    f"aura:calendar:cursor:{self._digest(encoded_value)}", consume=False
                )
                if (
                    existing is not None
                    and existing.get("owner") == self._owner_key(owner)
                    and existing.get("scope") == scope
                    and existing.get("query") == query_key
                    and existing.get("valueType") == value_type
                ):
                    return encoded_value
            token = secrets.token_urlsafe(32)
            await self._put(
                f"aura:calendar:cursor:{self._digest(token)}",
                {
                    "owner": self._owner_key(owner),
                    "scope": scope,
                    "query": query_key,
                    "valueType": value_type,
                    "value": encoded_value,
                },
            )
            return token

    async def consume(self, owner: tuple[str, str], query: object, cursor: str) -> object:
        scope, query_key = self._query_key(query)
        # Browser cursors are one-shot capability handles.  Consume the
        # Valkey entry before handing the server-side continuation to the
        # application so a replay, Valkey loss, or owner/query mismatch fails
        # closed without ever exposing provider state to the caller.
        async with self._mutation():
            key = f"aura:calendar:cursor:{self._digest(cursor)}"
            data = await self._get(key, consume=False)
            if (
                data is None
                or data.get("owner") != self._owner_key(owner)
                or data.get("scope") != scope
                or data.get("query") != query_key
            ):
                raise RuntimeError("calendar cursor is invalid")
            if not await self._delete(key):
                raise RuntimeError("calendar cursor is invalid")
            return self._decode_cursor_value(data.get("valueType"), data.get("value"))


def calendar_service(
    sessions: async_sessionmaker[AsyncSession] | None,
    *,
    settings: Any,
    metrics: MetadataMetrics | None = None,
    testing: bool = False,
    transient_client: Any = None,
) -> CalendarApplicationService:
    """Compose the shared application service behind inward public ports."""

    if not testing and transient_client is None:
        raise RuntimeError("calendar transient store is unavailable")
    repository: Any = _MemoryCalendarRepository() if testing else _sql_repository(sessions)
    codec: _CalendarCredentialCodec | None = None
    if bool(getattr(settings, "google_calendar_enabled", False)):
        codec = _CalendarCredentialCodec(settings)
    transient = _CalendarTransientStore(transient_client, codec, testing=testing)
    provider: CalendarProvider = _UnavailableCalendarProvider()
    if bool(getattr(settings, "google_calendar_enabled", False)) and sessions is not None:
        config = settings.calendar_oauth_config()
        if (
            isinstance(config, GoogleCalendarOAuthConfig)
            and config.client_id
            and config.client_secret
        ):
            provider_candidate = cast(
                object,
                GoogleCalendarAdapter(
                    client_id=config.client_id,
                    client_secret=cast(
                        str,
                        getattr(config.client_secret, "value", config.client_secret),
                    ),
                    telemetry=_TelemetryAdapter(metrics),
                    callback_uri=(
                        config.callback_uri
                        or f"{config.public_origin.rstrip('/')}{GOOGLE_CALENDAR_CALLBACK_PATH}"
                    ),
                    credential_loader=_RepositoryCredentialLoader(
                        repository, cast(_CalendarCredentialCodec, codec)
                    ),
                    calendar_id_resolver=repository.provider_calendar_id,
                ),
            )
            provider = (
                _ConfiguredProviderFailure()
                if provider_candidate is None
                else cast(CalendarProvider, provider_candidate)
            )
    return CalendarApplicationService(
        repository,
        provider,
        attempt_store=transient,
        credential_codec=codec,
        cursor_store=transient,
        conflict_observer=_ConflictTelemetryObserver(metrics),
    )


def _sql_repository(
    sessions: async_sessionmaker[AsyncSession] | None,
) -> SqlCalendarRepository:
    if sessions is None:
        raise ValueError("database sessions are required for calendar persistence")
    return SqlCalendarRepository(sessions)
