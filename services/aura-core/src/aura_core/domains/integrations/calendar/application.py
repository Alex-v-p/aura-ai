"""Provider-neutral calendar application service.

Entrypoints translate HTTP/session concerns into this service.  The service
owns lifecycle read gating, selection validation, and conflict evaluation;
provider adapters are supplied through :mod:`ports`.
"""

from __future__ import annotations

import asyncio
import hashlib
import secrets
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Any, cast
from uuid import UUID, uuid4

from .internal import (
    CalendarConflictCursorState,
    CalendarConflictQuery,
    CalendarDiscoveryQuery,
)
from .ports import (
    CalendarAttemptStore,
    CalendarConflictObserver,
    CalendarCredentialCodec,
    CalendarCursorStore,
    CalendarProvider,
    CalendarRepository,
)
from .public import (
    CalendarAuthorizationAttempt,
    CalendarAuthorizationRejected,
    CalendarAuthorizationStart,
    CalendarCallbackResult,
    CalendarConflict,
    CalendarConflictPage,
    CalendarConnection,
    CalendarConnectionStatus,
    CalendarEvent,
    CalendarEventPage,
    CalendarNotConnected,
    CalendarQuery,
    CalendarReadWarning,
    CalendarRequestContext,
    CalendarRevocationState,
    CalendarSelection,
    CalendarValidationError,
    CalendarVersionConflict,
    SelectableCalendarPage,
    evaluate_conflicts,
    validate_query,
)


class CalendarApplicationService:
    """The single public use-case boundary for calendar reads and metadata."""

    def __init__(
        self,
        repository: CalendarRepository,
        provider: CalendarProvider,
        *,
        attempt_store: CalendarAttemptStore | None = None,
        credential_codec: CalendarCredentialCodec | None = None,
        cursor_store: CalendarCursorStore | None = None,
        conflict_observer: CalendarConflictObserver | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._repository = repository
        self._provider = provider
        self._attempt_store = attempt_store
        self._credential_codec = credential_codec
        self._cursor_store = cursor_store
        self._conflict_observer = conflict_observer
        self._clock = clock or (lambda: datetime.now(UTC))
        self._active_attempts: dict[tuple[tuple[str, str], str], UUID] = {}

    def _now(self) -> datetime:
        return self._clock()

    @staticmethod
    def _fingerprint(operation: str, payload: str) -> str:
        return hashlib.sha256(f"{operation}:{payload}".encode()).hexdigest()

    @staticmethod
    def _request_context(
        query: CalendarQuery, cancelled: Callable[[], bool] | None
    ) -> CalendarRequestContext:
        return CalendarRequestContext(monotonic() + query.timeout_ms / 1000, cancelled)

    @staticmethod
    def _operation_request(request: CalendarRequestContext | None) -> CalendarRequestContext:
        # Lifecycle/discovery callers without a query-bound timeout still get
        # a finite, cancellable provider boundary. Routes can supply the
        # request context built from their transport deadline.
        return request or CalendarRequestContext(monotonic() + 10.0)

    @staticmethod
    def _receipt(value: object) -> Mapping[str, object]:
        if not isinstance(value, Mapping):
            raise CalendarAuthorizationRejected("calendar command receipt is invalid")
        return cast(Mapping[str, object], value)

    @staticmethod
    def _revocation_state(value: object) -> CalendarRevocationState | None:
        return value if isinstance(value, CalendarRevocationState) else None

    async def _audit(
        self,
        owner: tuple[str, str],
        *,
        action: str,
        outcome: str,
        category: str | None = None,
    ) -> None:
        method = getattr(self._repository, "record_audit", None)
        if method is not None:
            try:
                await method(
                    action=action,
                    outcome=outcome,
                    category=category,
                    occurred_at=self._now(),
                )
            except TypeError as exc:
                # Compatibility for pre-correction in-memory fixtures. The
                # SQL repository never receives owner identity for audit.
                try:
                    await method(
                        owner,
                        action=action,
                        outcome=outcome,
                        category=category,
                        occurred_at=self._now(),
                    )
                except TypeError:
                    raise exc from None

    async def _complete_idempotency(
        self,
        owner: tuple[str, str],
        operation: str,
        key: str,
        fingerprint: str,
        response: object,
    ) -> None:
        method = getattr(self._repository, "complete_idempotency", None)
        if method is not None:
            await method(owner, operation, key, fingerprint, response)
        else:
            # Compatibility for pre-correction in-memory fixtures.  The SQL
            # repository always takes the atomic method above.
            await self._repository.record_idempotency(
                owner, operation, key, fingerprint, response
            )

    async def connection(self, owner: tuple[str, str]) -> CalendarConnection:
        return await self._repository.get_connection(owner)

    async def discover(
        self,
        owner: tuple[str, str],
        *,
        cursor: str | None = None,
        limit: int = 50,
        request: CalendarRequestContext | None = None,
    ) -> SelectableCalendarPage:
        request = self._operation_request(request)
        request.check()
        connection = await self.connection(owner)
        if connection.status is not CalendarConnectionStatus.CONNECTED:
            raise CalendarNotConnected("calendar reads are unavailable")
        if not 1 <= limit <= 100:
            raise CalendarValidationError("calendar limit is outside bounds")
        cursor_query = CalendarDiscoveryQuery(limit)
        provider_cursor: str | None = None
        if cursor is not None:
            if self._cursor_store is None:
                raise CalendarValidationError("calendar cursor is invalid")
            try:
                value = await self._cursor_store.consume(owner, cursor_query, cursor)
            except Exception as exc:
                raise CalendarValidationError("calendar cursor is invalid") from exc
            if not isinstance(value, str) or not value:
                raise CalendarValidationError("calendar cursor is invalid")
            provider_cursor = value
        elif self._cursor_store is None:
            # A first page does not need a cursor store.  Continuations do.
            provider_cursor = None
        try:
            page = await self._provider.discover(
                owner, cursor=provider_cursor, limit=limit, request=request
            )
        except Exception as exc:
            category = getattr(getattr(exc, "category", None), "value", "")
            if category in {"provider_auth", "authentication", "expired"}:
                marker = getattr(self._repository, "mark_reauthorization_required", None)
                try:
                    if marker is not None:
                        await marker(owner, now=self._now(), category="provider_auth")
                except Exception as marker_exc:
                    raise CalendarNotConnected("calendar reads are unavailable") from marker_exc
                raise CalendarNotConnected("calendar reads are unavailable") from exc
            raise
        projection = await self._repository.upsert_discovered(
            owner, page.items, observed_at=self._now()
        )
        public_cursor: str | None = None
        if page.next_state is not None:
            if self._cursor_store is None:
                raise CalendarValidationError("calendar cursor is unavailable")
            if not page.next_state:
                raise CalendarValidationError("calendar discovery result is invalid")
            public_cursor = await self._cursor_store.put(owner, cursor_query, page.next_state)
        return SelectableCalendarPage(
            projection.items,
            public_cursor,
            projection.selection_version,
            projection.observed_at,
        )

    async def start_connection(
        self,
        owner: tuple[str, str],
        *,
        session_id: str,
        idempotency_key: str | None = None,
    ) -> CalendarAuthorizationStart:
        if self._attempt_store is None:
            raise CalendarNotConnected("calendar authorization is unavailable")
        connection = await self.connection(owner)
        if connection.status not in {
            CalendarConnectionStatus.DISCONNECTED,
            CalendarConnectionStatus.DISABLED,
        }:
            raise CalendarValidationError("calendar connection cannot be started")
        now = self._now()
        attempt = CalendarAuthorizationAttempt(
            uuid4(),
            owner,
            session_id,
            secrets.token_urlsafe(32),
            secrets.token_urlsafe(32),
            secrets.token_urlsafe(32),
            now + timedelta(seconds=600),
            secrets.token_urlsafe(64),
        )
        operation = "calendar.connection.start"
        fingerprint = self._fingerprint(operation, session_id)
        if idempotency_key is not None:
            claimed, receipt = await self._repository.claim_idempotency(
                owner,
                operation,
                idempotency_key,
                fingerprint,
                {
                    "status": "pending",
                    "attempt_id": str(attempt.attempt_id),
                    "expires_at": attempt.expires_at.isoformat(),
                },
            )
            if not claimed:
                receipt_map = self._receipt(receipt)
                if receipt_map.get("status") != "pending":
                    raise CalendarAuthorizationRejected("authorization attempt is unavailable")
                try:
                    attempt_id = UUID(str(receipt_map["attempt_id"]))
                    attempt = await self._attempt_store.load_attempt(
                        owner, session_id, attempt_id
                    )
                except Exception as exc:
                    # The durable claim intentionally contains no path or
                    # handle.  Loss of the transient attempt fails closed.
                    raise CalendarAuthorizationRejected(
                        "authorization attempt is unavailable"
                    ) from exc
                if attempt.expires_at <= now:
                    raise CalendarAuthorizationRejected("authorization attempt is invalid")
                self._active_attempts[(owner, session_id)] = attempt.attempt_id
                return CalendarAuthorizationStart(
                    connection,
                    f"/api/v1/integrations/calendar/authorize/{attempt.handle}",
                    attempt.expires_at,
                )
        await self._attempt_store.create(attempt)
        self._active_attempts[(owner, session_id)] = attempt.attempt_id
        await self._audit(owner, action="connect", outcome="started")
        return CalendarAuthorizationStart(
            connection,
            f"/api/v1/integrations/calendar/authorize/{attempt.handle}",
            attempt.expires_at,
        )

    async def reauthorize_connection(
        self,
        owner: tuple[str, str],
        *,
        session_id: str,
        idempotency_key: str | None = None,
    ) -> CalendarAuthorizationStart:
        connection = await self.connection(owner)
        if connection.status is not CalendarConnectionStatus.REAUTHORIZATION_REQUIRED:
            raise CalendarValidationError("calendar reauthorization is not available")
        if self._attempt_store is None:
            raise CalendarNotConnected("calendar authorization is unavailable")
        now = self._now()
        attempt = CalendarAuthorizationAttempt(
            uuid4(), owner, session_id, secrets.token_urlsafe(32), secrets.token_urlsafe(32),
            secrets.token_urlsafe(32), now + timedelta(seconds=600), secrets.token_urlsafe(64)
        )
        operation = "calendar.connection.reauthorize"
        fingerprint = self._fingerprint(operation, session_id)
        if idempotency_key is not None:
            claimed, receipt = await self._repository.claim_idempotency(
                owner,
                operation,
                idempotency_key,
                fingerprint,
                {
                    "status": "pending",
                    "attempt_id": str(attempt.attempt_id),
                    "expires_at": attempt.expires_at.isoformat(),
                },
            )
            if not claimed:
                receipt_map = self._receipt(receipt)
                if receipt_map.get("status") != "pending":
                    raise CalendarAuthorizationRejected("authorization attempt is unavailable")
                try:
                    attempt = await self._attempt_store.load_attempt(
                        owner, session_id, UUID(str(receipt_map["attempt_id"]))
                    )
                except Exception as exc:
                    raise CalendarAuthorizationRejected(
                        "authorization attempt is unavailable"
                    ) from exc
                if attempt.expires_at <= now:
                    raise CalendarAuthorizationRejected("authorization attempt is invalid")
                self._active_attempts[(owner, session_id)] = attempt.attempt_id
                return CalendarAuthorizationStart(
                    connection,
                    f"/api/v1/integrations/calendar/authorize/{attempt.handle}",
                    attempt.expires_at,
                )
        await self._attempt_store.create(attempt)
        self._active_attempts[(owner, session_id)] = attempt.attempt_id
        await self._audit(owner, action="reauthorize", outcome="started")
        return CalendarAuthorizationStart(
            connection,
            f"/api/v1/integrations/calendar/authorize/{attempt.handle}",
            attempt.expires_at,
        )

    async def authorize_navigation(
        self,
        owner: tuple[str, str],
        handle: str,
        *,
        session_id: str,
        request: CalendarRequestContext | None = None,
    ) -> str:
        request = self._operation_request(request)
        request.check()
        if self._attempt_store is None:
            raise CalendarAuthorizationRejected("authorization attempt is unavailable")
        try:
            attempt = await self._attempt_store.consume_navigation(owner, session_id, handle)
        except Exception as exc:
            # Attempt stores deliberately collapse missing, expired, replayed,
            # superseded, owner, and session mismatches into an opaque failure.
            raise CalendarAuthorizationRejected("authorization attempt is invalid") from exc
        if attempt.expires_at <= self._now():
            raise CalendarAuthorizationRejected("authorization attempt is invalid")
        method = getattr(self._provider, "authorization_url", None)
        if method is None:
            raise CalendarAuthorizationRejected("authorization attempt is unavailable")
        target = await method(attempt)
        request.check()
        validator = getattr(self._attempt_store, "finalize_navigation_attempt", None)
        if validator is None:
            raise CalendarAuthorizationRejected("authorization attempt is unavailable")
        try:
            await validator(owner, session_id, attempt.attempt_id, attempt.handle)
        except Exception as exc:
            raise CalendarAuthorizationRejected("authorization attempt is invalid") from exc
        return target

    async def provider_callback(
        self,
        *,
        code: str | None,
        state: str | None,
        error: str | None,
        callback_origin: str,
        request: CalendarRequestContext | None = None,
    ) -> None:
        request = self._operation_request(request)
        request.check()
        if self._attempt_store is None or not state:
            raise CalendarAuthorizationRejected("authorization callback is invalid")
        try:
            attempt = await self._attempt_store.consume_state(state)
        except Exception as exc:
            raise CalendarAuthorizationRejected("authorization callback is invalid") from exc
        if attempt.expires_at <= self._now():
            raise CalendarAuthorizationRejected("authorization callback is invalid")
        self._active_attempts[(attempt.owner, attempt.session_id)] = attempt.attempt_id
        if error or not code:
            try:
                await self._attempt_store.put_callback(
                    CalendarCallbackResult(
                        attempt.attempt_id,
                        attempt.owner,
                        attempt.session_id,
                        None,
                        attempt.expires_at,
                        "callback_rejected",
                    )
                )
            except Exception as exc:
                raise CalendarAuthorizationRejected(
                    "authorization callback is unavailable"
                ) from exc
            return
        method = getattr(self._provider, "exchange_code", None)
        if method is None:
            raise CalendarAuthorizationRejected("authorization callback is unavailable")
        credential = await method(
            code=code,
            redirect_uri=callback_origin,
            nonce=attempt.nonce,
            verifier=attempt.verifier,
            request=request,
        )
        request.check()
        try:
            await self._attempt_store.put_callback(
                CalendarCallbackResult(
                    attempt.attempt_id,
                    attempt.owner,
                    attempt.session_id,
                    credential,
                    attempt.expires_at,
                )
            )
        except Exception as exc:
            raise CalendarAuthorizationRejected("authorization callback is unavailable") from exc
        await self._audit(attempt.owner, action="complete_connection", outcome="callback_stored")

    async def complete_connection(
        self,
        owner: tuple[str, str],
        *,
        session_id: str,
        idempotency_key: str | None = None,
    ) -> CalendarConnection:
        if self._attempt_store is None or self._credential_codec is None:
            raise CalendarAuthorizationRejected("authorization completion is unavailable")
        connection = await self.connection(owner)
        if connection.status is CalendarConnectionStatus.REVOCATION_UNCERTAIN:
            raise CalendarAuthorizationRejected("authorization completion is unavailable")
        operation = "calendar.connection.complete"
        fingerprint = self._fingerprint(operation, session_id)
        if idempotency_key is not None:
            existing = await self._repository.find_idempotency(
                owner, operation, idempotency_key, fingerprint
            )
            if existing is not None:
                existing_map = self._receipt(existing)
                if existing_map.get("status") != "connected":
                    raise CalendarAuthorizationRejected("authorization completion is unavailable")
                return await self.connection(owner)
            try:
                callback = await self._attempt_store.peek_callback(owner, session_id)
            except Exception as exc:
                raise CalendarAuthorizationRejected("authorization callback is invalid") from exc
            if callback.expires_at <= self._now():
                raise CalendarAuthorizationRejected("authorization callback is invalid")
            claimed, _ = await self._repository.claim_idempotency(
                owner,
                operation,
                idempotency_key,
                fingerprint,
                {"status": "pending", "attempt_id": str(callback.attempt_id)},
            )
            if not claimed:
                raise CalendarAuthorizationRejected("authorization completion is unavailable")
        else:
            callback = None
        try:
            if callback is None:
                callback = await self._attempt_store.peek_callback(owner, session_id)
            callback = await self._attempt_store.consume_callback(
                owner, session_id, callback.attempt_id
            )
        except Exception as exc:
            raise CalendarAuthorizationRejected("authorization callback is invalid") from exc
        active_attempt_id = self._active_attempts.get((owner, session_id))
        if active_attempt_id is not None and callback.attempt_id != active_attempt_id:
            raise CalendarAuthorizationRejected("authorization callback is invalid")
        if (
            callback.owner != owner
            or callback.session_id != session_id
            or callback.expires_at <= self._now()
            or callback.error
            or callback.credential is None
        ):
            raise CalendarAuthorizationRejected("authorization callback was rejected")
        connection_id = await self._repository.connection_id(owner) or uuid4()
        envelope = await self._credential_codec.seal(owner, connection_id, callback.credential)
        if idempotency_key is not None:
            result = await self._repository.commit_connected_idempotent(
                owner,
                envelope,
                connected_at=self._now(),
                operation=operation,
                key=idempotency_key,
                fingerprint=fingerprint,
                attempt_id=callback.attempt_id,
            )
        else:
            result = await self._repository.commit_connected(
                owner, envelope, connected_at=self._now()
            )
        await self._audit(owner, action="complete_connection", outcome="connected")
        self._active_attempts.pop((owner, session_id), None)
        return result

    async def disconnect_connection(
        self,
        owner: tuple[str, str],
        *,
        idempotency_key: str | None = None,
        request: CalendarRequestContext | None = None,
    ) -> CalendarConnection:
        request = self._operation_request(request)
        request.check()
        operation = "calendar.connection.disconnect"
        fingerprint = self._fingerprint(operation, "")
        if idempotency_key is not None:
            existing = await self._repository.find_idempotency(
                owner, operation, idempotency_key, fingerprint
            )
            if existing is not None:
                existing_map = self._receipt(existing)
                if existing_map.get("status") in {"disconnected", "revocation_uncertain"}:
                    return await self.connection(owner)
                raise CalendarAuthorizationRejected("disconnect is unavailable")
            claimed, _ = await self._repository.claim_idempotency(
                owner,
                operation,
                idempotency_key,
                fingerprint,
                {"status": "pending"},
            )
            if not claimed:
                raise CalendarAuthorizationRejected("disconnect is unavailable")
        now = self._now()
        reserve = getattr(self._repository, "reserve_revocation_attempt", None)
        if reserve is None:
            # Compatibility for pre-correction in-memory fixtures. The SQL
            # repository always reserves before dispatch.
            prepared: Any = await self._repository.prepare_revocation(
                owner, now=now, deadline=now + timedelta(hours=24)
            )
            if len(prepared) == 3:
                connection, raw_state, reservation = prepared
            else:
                connection, raw_state = prepared
                reservation = None
            state = self._revocation_state(raw_state)
            legacy_finalize: Any = self._repository.finalize_revocation_attempt
            terminal = state is not None and (
                state.attempts >= 6
                or (state.deadline is not None and now >= state.deadline)
            )
            if reservation is not None and terminal:
                return await legacy_finalize(
                    owner,
                    now=now,
                    success=False,
                    reservation=reservation,
                    idempotency_key=idempotency_key,
                    fingerprint=fingerprint if idempotency_key is not None else None,
                )
            if reservation is None and terminal:
                # Compatibility path for older repositories that cannot return
                # a reservation. Production SQL repositories perform this
                # transition and ciphertext deletion atomically in reserve.
                if connection.status is not CalendarConnectionStatus.REVOCATION_UNCERTAIN:
                    marker = getattr(self._repository, "mark_revocation_uncertain", None)
                    if marker is not None:
                        connection = await marker(
                            owner,
                            now=now,
                            deadline=(state.deadline if state is not None else now) or now,
                        )
                destroy = getattr(self._repository, "destroy_credential", None)
                connection_id = await self._repository.connection_id(owner)
                if destroy is not None and connection_id is not None:
                    await destroy(owner, connection_id)
                connection = replace(connection, revocation_terminal=True)
                if idempotency_key is not None:
                    await self._complete_idempotency(
                        owner,
                        operation,
                        idempotency_key,
                        fingerprint,
                        {"status": connection.status.value},
                    )
                return connection
            if reservation is None and state is not None and (
                state.next_attempt_at is None or state.next_attempt_at > now
            ):
                if idempotency_key is not None:
                    await self._complete_idempotency(
                        owner,
                        operation,
                        idempotency_key,
                        fingerprint,
                        {"status": connection.status.value},
                    )
                return connection
            try:
                request.check()
                await self._provider.revoke(owner, request=request)
            except (asyncio.CancelledError, TimeoutError):
                if reservation is not None:
                    await legacy_finalize(
                        owner,
                        now=self._now(),
                        success=False,
                        reservation=reservation,
                        idempotency_key=idempotency_key,
                        fingerprint=fingerprint if idempotency_key is not None else None,
                    )
                raise
            except Exception:
                kwargs: dict[str, object] = {
                    "now": self._now(),
                    "success": False,
                    "idempotency_key": idempotency_key,
                    "fingerprint": fingerprint if idempotency_key is not None else None,
                }
                if reservation is not None:
                    kwargs["reservation"] = reservation
                return await legacy_finalize(owner, **kwargs)
            kwargs = {
                "now": self._now(),
                "success": True,
                "idempotency_key": idempotency_key,
                "fingerprint": fingerprint if idempotency_key is not None else None,
            }
            if reservation is not None:
                kwargs["reservation"] = reservation
            return await legacy_finalize(owner, **kwargs)
        connection, state, reservation = await reserve(owner, now=now)
        if reservation is None:
            if idempotency_key is not None:
                await self._complete_idempotency(
                    owner,
                    operation,
                    idempotency_key,
                    fingerprint,
                    {"status": connection.status.value},
                )
            return connection
        if state.attempts >= 6 or (
            state.deadline is not None and now >= state.deadline
        ):
            return await self._repository.finalize_revocation_attempt(
                owner,
                now=now,
                success=False,
                reservation=reservation,
                idempotency_key=idempotency_key,
                fingerprint=fingerprint if idempotency_key is not None else None,
            )
        try:
            request.check()
        except (asyncio.CancelledError, TimeoutError):
            try:
                await self._repository.finalize_revocation_attempt(
                    owner,
                    now=self._now(),
                    success=False,
                    reservation=reservation,
                    idempotency_key=idempotency_key,
                    fingerprint=fingerprint if idempotency_key is not None else None,
                )
            finally:
                raise
        try:
            await self._provider.revoke(owner, request=request)
        except (asyncio.CancelledError, TimeoutError):
            await self._repository.finalize_revocation_attempt(
                owner,
                now=self._now(),
                success=False,
                reservation=reservation,
                idempotency_key=idempotency_key,
                fingerprint=fingerprint if idempotency_key is not None else None,
            )
            raise
        except Exception:
            result = await self._repository.finalize_revocation_attempt(
                owner,
                now=self._now(),
                success=False,
                reservation=reservation,
                idempotency_key=idempotency_key,
                fingerprint=fingerprint if idempotency_key is not None else None,
            )
            return result
        result = await self._repository.finalize_revocation_attempt(
            owner,
            now=self._now(),
            success=True,
            reservation=reservation,
            idempotency_key=idempotency_key,
            fingerprint=fingerprint if idempotency_key is not None else None,
        )
        return result

    async def replace_selection(
        self,
        owner: tuple[str, str],
        calendar_ids: Sequence[UUID],
        *,
        expected_version: int,
        idempotency_key: str | None = None,
    ) -> CalendarSelection:
        operation = "calendar.selection.replace"
        fingerprint = self._fingerprint(
            operation,
            f"{expected_version}:{','.join(map(str, calendar_ids))}",
        )
        if idempotency_key is not None:
            return await self._repository.replace_selection_idempotent(
                owner,
                calendar_ids,
                expected_version=expected_version,
                operation=operation,
                key=idempotency_key,
                fingerprint=fingerprint,
            )
        return await self._repository.replace_selection(
            owner, calendar_ids, expected_version=expected_version
        )

    def _selection_from_idempotency(self, response: object) -> CalendarSelection:
        """Decode the content-free durable selection receipt for replay."""
        if isinstance(response, CalendarSelection):
            return response
        if not isinstance(response, Mapping):
            raise CalendarVersionConflict("stored selection receipt is invalid")
        receipt = cast(Mapping[str, object], response)
        raw_ids_value = receipt.get("calendar_ids", ())
        if isinstance(raw_ids_value, (str, bytes)) or not isinstance(raw_ids_value, Sequence):
            raise CalendarVersionConflict("stored selection receipt is invalid")
        raw_ids = cast(Sequence[object], raw_ids_value)
        version_value = receipt.get("version")
        try:
            calendar_ids = tuple(
                item if isinstance(item, UUID) else UUID(str(item)) for item in raw_ids
            )
            version = int(version_value)  # type: ignore[arg-type]
        except (KeyError, TypeError, ValueError) as exc:
            raise CalendarVersionConflict("stored selection receipt is invalid") from exc
        raw_updated_at = receipt.get("updated_at")
        if isinstance(raw_updated_at, datetime):
            updated_at = raw_updated_at
        elif isinstance(raw_updated_at, str):
            try:
                updated_at = datetime.fromisoformat(raw_updated_at)
            except ValueError as exc:
                raise CalendarVersionConflict("stored selection receipt is invalid") from exc
        else:
            updated_at = self._now()
        return CalendarSelection(calendar_ids, version, updated_at)

    async def events(
        self,
        owner: tuple[str, str],
        query: CalendarQuery,
        *,
        cancelled: Callable[[], bool] | None = None,
    ) -> CalendarEventPage:
        connection = await self.connection(owner)
        if (
            connection.status is not CalendarConnectionStatus.CONNECTED
            or not connection.reads_enabled
        ):
            raise CalendarNotConnected("calendar reads are unavailable")
        query = validate_query(query)
        request = self._request_context(query, cancelled)
        request.check()
        cursor_query = replace(query, cursor=None)
        if query.cursor is not None and self._cursor_store is not None:
            try:
                provider_cursor = await self._cursor_store.consume(
                    owner, cursor_query, query.cursor
                )
            except Exception as exc:
                raise CalendarValidationError("calendar cursor is invalid") from exc
            if not isinstance(provider_cursor, str):
                raise CalendarValidationError("calendar cursor is invalid")
            query = replace(query, cursor=provider_cursor)
        elif query.cursor is not None:
            raise CalendarValidationError("calendar cursor is invalid")
        selected = set(await self._repository.selected_ids(owner))
        if any(calendar_id not in selected for calendar_id in query.calendar_ids):
            raise CalendarValidationError("calendar selection is not enabled")
        result = await self._retrieve_events_page(owner, query, request)
        if self._cursor_store is not None and result.next_cursor is not None:
            if not result.next_cursor:
                raise CalendarValidationError("calendar provider cursor is invalid")
            cursor = await self._cursor_store.put(owner, cursor_query, result.next_cursor)
            return CalendarEventPage(
                result.range_start,
                result.range_end,
                result.display_time_zone,
                result.items,
                cursor,
                result.partial,
                result.warnings,
                result.retrieved_at,
            )
        if result.next_cursor is not None:
            raise CalendarValidationError("calendar cursor is unavailable")
        return result

    async def _retrieve_events_page(
        self,
        owner: tuple[str, str],
        query: CalendarQuery,
        request: CalendarRequestContext,
    ) -> CalendarEventPage:
        request.check()
        try:
            result = await self._provider.retrieve(owner, query, request=request)
        except Exception as exc:
            category = getattr(getattr(exc, "category", None), "value", "")
            if category in {"provider_auth", "authentication", "expired"}:
                marker = getattr(self._repository, "mark_reauthorization_required", None)
                try:
                    if marker is not None:
                        await marker(owner, now=self._now(), category="provider_auth")
                except Exception as marker_exc:
                    raise CalendarNotConnected("calendar reads are unavailable") from marker_exc
                raise CalendarNotConnected("calendar reads are unavailable") from exc
            raise
        return result

    async def conflicts(
        self,
        owner: tuple[str, str],
        query: CalendarQuery,
        *,
        conflict_limit: int = 100,
        cancelled: Callable[[], bool] | None = None,
    ) -> CalendarConflictPage:
        connection = await self.connection(owner)
        if (
            connection.status is not CalendarConnectionStatus.CONNECTED
            or not connection.reads_enabled
        ):
            raise CalendarNotConnected("calendar reads are unavailable")
        if not 1 <= conflict_limit <= 200:
            raise CalendarValidationError("conflict limit is outside bounds")
        query = validate_query(query)
        request = self._request_context(query, cancelled)
        request.check()
        selected = set(await self._repository.selected_ids(owner))
        if any(calendar_id not in selected for calendar_id in query.calendar_ids):
            raise CalendarValidationError("calendar selection is not enabled")
        base_query = replace(query, cursor=None)
        cursor_binding = CalendarConflictQuery(base_query, conflict_limit)
        state = CalendarConflictCursorState(None, 0, ())
        if query.cursor is not None:
            if self._cursor_store is None:
                raise CalendarValidationError("calendar cursor is invalid")
            try:
                raw_state = await self._cursor_store.consume(owner, cursor_binding, query.cursor)
            except Exception as exc:
                raise CalendarValidationError("calendar cursor is invalid") from exc
            if not isinstance(raw_state, CalendarConflictCursorState):
                raise CalendarValidationError("calendar cursor is invalid")
            state = raw_state
            if state.pages_consumed < 0 or (
                state.pages_consumed == 0 and state.provider_cursor is not None
            ):
                raise CalendarValidationError("calendar cursor is invalid")
        all_events: list[CalendarEvent] = []
        warnings: list[CalendarReadWarning] = []
        partial = False
        page = CalendarEventPage(
            base_query.range_start,
            base_query.range_end,
            base_query.display_time_zone,
            (),
            None,
            False,
            (),
            self._now(),
        )
        replay_cursor: str | None = None
        for _ in range(state.pages_consumed):
            request.check()
            replay_query = replace(base_query, cursor=replay_cursor)
            page = await self._retrieve_events_page(owner, replay_query, request)
            all_events.extend(page.items)
            warnings.extend(page.warnings)
            partial = partial or page.partial
            replay_cursor = page.next_cursor
            if replay_cursor is None:
                break
        if state.pages_consumed and replay_cursor != state.provider_cursor:
            # The provider's continuation chain changed or the cursor was
            # malformed; do not guess at a relationship boundary.
            raise CalendarValidationError("calendar cursor is invalid")
        pages_consumed = state.pages_consumed
        provider_cursor = state.provider_cursor
        request.check()
        current_query = replace(base_query, cursor=provider_cursor)
        page = await self._retrieve_events_page(owner, current_query, request)
        pages_consumed += 1
        all_events.extend(page.items)
        warnings.extend(page.warnings)
        partial = partial or page.partial
        evaluated = await self._evaluate_conflicts(tuple(all_events), limit=200)
        emitted = set(state.emitted_relationships)
        fresh = [item for item in evaluated if self._conflict_key(item) not in emitted]
        items = tuple(fresh[:conflict_limit])
        emitted_relationships = state.emitted_relationships + tuple(
            self._conflict_key(item) for item in items
        )
        next_cursor: str | None = None
        has_more = (
            len(emitted_relationships) < 200
            and (len(fresh) > len(items) or page.next_cursor is not None)
        )
        if has_more:
            if self._cursor_store is None:
                raise CalendarValidationError("calendar cursor is unavailable")
            next_cursor = await self._cursor_store.put(
                owner,
                cursor_binding,
                CalendarConflictCursorState(
                    page.next_cursor,
                    pages_consumed,
                    emitted_relationships,
                ),
            )
        now = self._now()
        return CalendarConflictPage(
            page.range_start,
            page.range_end,
            page.display_time_zone,
            items,
            next_cursor,
            partial,
            tuple(warnings),
            now,
        )

    async def _evaluate_conflicts(
        self, events: tuple[CalendarEvent, ...], *, limit: int
    ) -> tuple[CalendarConflict, ...]:
        """Evaluate already-retrieved events and observe only that local work."""
        started = monotonic()
        outcome = "ok"
        try:
            return evaluate_conflicts(events, limit=limit)
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        except Exception:
            outcome = "error"
            raise
        finally:
            observer = self._conflict_observer
            if observer is not None:
                try:
                    await observer.observe_conflict_evaluation(
                        duration_ms=(monotonic() - started) * 1000,
                        outcome=outcome,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # Observability must not change the closed conflict result.
                    pass

    @staticmethod
    def _conflict_key(conflict: CalendarConflict) -> str:
        value = conflict
        return "|".join(
            (
                str(value.left.calendar_id),
                value.left.event_id,
                str(value.right.calendar_id),
                value.right.event_id,
                str(value.relationship),
            )
        )

    async def load_provider_credential(self, owner: tuple[str, str]) -> object:
        """Open one credential and lazily CAS-rewrite it through the codec."""
        if self._credential_codec is None:
            raise CalendarNotConnected("calendar credential is unavailable")
        stored = await self._repository.load_credential(owner)
        if stored is None:
            raise CalendarNotConnected("calendar credential is unavailable")
        connection_id, envelope = stored
        credential = await self._credential_codec.open(owner, connection_id, envelope)
        replacement = await self._credential_codec.seal(owner, connection_id, credential)
        if replacement != envelope:
            replaced = await self._repository.compare_and_swap_credential(
                owner, connection_id, envelope, replacement
            )
            if not replaced:
                # Another owner-bound writer won the CAS race. Never return
                # plaintext opened from the stale envelope; reload and
                # authenticate only the current ciphertext instead.
                current = await self._repository.load_credential(owner)
                if current is None or current[0] != connection_id:
                    raise CalendarNotConnected("calendar credential is unavailable")
                current_envelope = current[1]
                if current_envelope.key_id != replacement.key_id:
                    raise CalendarNotConnected("calendar credential is unavailable")
                try:
                    return await self._credential_codec.open(
                        owner, connection_id, current_envelope
                    )
                except Exception as exc:
                    raise CalendarNotConnected(
                        "calendar credential is unavailable"
                    ) from exc
        return credential
