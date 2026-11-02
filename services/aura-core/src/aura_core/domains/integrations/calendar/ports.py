"""Inward ports used by the calendar application boundary.

The calendar domain deliberately knows nothing about Google (or any other
provider).  Adapters implement these small protocols at composition time.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from .internal import (
    CalendarCursorBinding,
    CalendarDiscoveryPage,
    CalendarDiscoveryRecord,
)
from .public import (
    CalendarAuthorizationAttempt,
    CalendarCallbackResult,
    CalendarConnection,
    CalendarCredentialEnvelope,
    CalendarEventPage,
    CalendarQuery,
    CalendarRequestContext,
    CalendarRevocationLease,
    CalendarRevocationState,
    CalendarSelection,
    SelectableCalendarPage,
)


class CalendarProvider(Protocol):
    async def discover(
        self,
        owner: tuple[str, str],
        *,
        cursor: str | None,
        limit: int,
        request: CalendarRequestContext,
    ) -> CalendarDiscoveryPage: ...

    async def retrieve(
        self,
        owner: tuple[str, str],
        query: CalendarQuery,
        *,
        request: CalendarRequestContext,
    ) -> CalendarEventPage: ...

    async def revoke(
        self, owner: tuple[str, str], *, request: CalendarRequestContext
    ) -> None: ...


class CalendarConflictObserver(Protocol):
    """Content-free observer for local conflict evaluation work."""

    async def observe_conflict_evaluation(
        self, *, duration_ms: float, outcome: str
    ) -> None: ...


class CalendarLifecycleProvider(Protocol):
    async def authorization_url(self, attempt: CalendarAuthorizationAttempt) -> str: ...

    async def exchange_code(
        self,
        *,
        code: str,
        redirect_uri: str,
        nonce: str,
        verifier: str,
        request: CalendarRequestContext,
    ) -> object: ...

    async def revoke(self, owner: tuple[str, str]) -> None: ...


class CalendarAttemptStore(Protocol):
    """Valkey-only, owner/session/attempt-bound ephemeral state."""

    async def create(self, attempt: CalendarAuthorizationAttempt) -> None: ...

    async def consume_navigation(
        self, owner: tuple[str, str], session_id: str, handle: str
    ) -> CalendarAuthorizationAttempt: ...

    async def consume_state(
        self, state: str, *, owner: tuple[str, str] | None = None
    ) -> CalendarAuthorizationAttempt: ...

    async def put_callback(self, result: CalendarCallbackResult) -> None: ...

    async def consume_callback(
        self, owner: tuple[str, str], session_id: str, attempt_id: UUID
    ) -> CalendarCallbackResult: ...

    async def peek_callback(
        self, owner: tuple[str, str], session_id: str
    ) -> CalendarCallbackResult: ...

    async def load_attempt(
        self, owner: tuple[str, str], session_id: str, attempt_id: UUID
    ) -> CalendarAuthorizationAttempt: ...

    async def finalize_navigation_attempt(
        self,
        owner: tuple[str, str],
        session_id: str,
        attempt_id: UUID,
        handle: str,
    ) -> None: ...


class CalendarCredentialCodec(Protocol):
    async def seal(
        self, owner: tuple[str, str], connection_id: UUID, credential: object
    ) -> CalendarCredentialEnvelope: ...

    async def open(
        self, owner: tuple[str, str], connection_id: UUID, envelope: CalendarCredentialEnvelope
    ) -> object: ...


class CalendarCursorStore(Protocol):
    async def put(
        self, owner: tuple[str, str], query: CalendarCursorBinding, value: object
    ) -> str: ...

    async def consume(
        self, owner: tuple[str, str], query: CalendarCursorBinding, cursor: str
    ) -> object: ...


class CalendarAuditSink(Protocol):
    async def record(
        self,
        *,
        action: str,
        outcome: str,
        category: str | None,
        occurred_at: datetime,
    ) -> None: ...


class CalendarRepository(Protocol):
    async def get_connection(self, owner: tuple[str, str]) -> CalendarConnection: ...

    async def save_connection(
        self,
        owner: tuple[str, str],
        connection: CalendarConnection,
        *,
        expected_version: int | None,
    ) -> CalendarConnection: ...

    async def list_calendars(
        self, owner: tuple[str, str], *, cursor: str | None, limit: int
    ) -> SelectableCalendarPage: ...

    async def replace_selection(
        self,
        owner: tuple[str, str],
        calendar_ids: Sequence[UUID],
        *,
        expected_version: int,
    ) -> CalendarSelection: ...

    async def selected_ids(self, owner: tuple[str, str]) -> tuple[UUID, ...]: ...

    async def connection_id(self, owner: tuple[str, str]) -> UUID | None: ...

    async def commit_connected(
        self,
        owner: tuple[str, str],
        credential: CalendarCredentialEnvelope,
        *,
        connected_at: datetime,
        expected_version: int | None = None,
    ) -> CalendarConnection: ...

    async def load_credential(
        self, owner: tuple[str, str]
    ) -> tuple[UUID, CalendarCredentialEnvelope] | None: ...

    async def compare_and_swap_credential(
        self,
        owner: tuple[str, str],
        connection_id: UUID,
        expected: CalendarCredentialEnvelope,
        replacement: CalendarCredentialEnvelope,
    ) -> bool: ...

    async def destroy_credential(self, owner: tuple[str, str], connection_id: UUID) -> None: ...

    async def mark_revocation_uncertain(
        self, owner: tuple[str, str], *, now: datetime, deadline: datetime
    ) -> CalendarConnection: ...

    async def mark_reauthorization_required(
        self, owner: tuple[str, str], *, now: datetime, category: str
    ) -> CalendarConnection: ...

    async def record_revocation_attempt(
        self, owner: tuple[str, str], *, now: datetime, success: bool
    ) -> CalendarConnection: ...

    async def prepare_revocation(
        self, owner: tuple[str, str], *, now: datetime, deadline: datetime
    ) -> tuple[CalendarConnection, CalendarRevocationState]: ...

    async def reserve_revocation_attempt(
        self,
        owner: tuple[str, str],
        *,
        now: datetime,
    ) -> tuple[CalendarConnection, CalendarRevocationState, CalendarRevocationLease | None]: ...

    async def finalize_revocation_attempt(
        self,
        owner: tuple[str, str],
        *,
        now: datetime,
        success: bool,
        reservation: CalendarRevocationLease,
        idempotency_key: str | None = None,
        fingerprint: str | None = None,
    ) -> CalendarConnection: ...

    async def claim_idempotency(
        self,
        owner: tuple[str, str],
        operation: str,
        key: str,
        fingerprint: str,
        response: object,
    ) -> tuple[bool, object]: ...

    async def complete_idempotency(
        self,
        owner: tuple[str, str],
        operation: str,
        key: str,
        fingerprint: str,
        response: object,
    ) -> None: ...

    async def replace_selection_idempotent(
        self,
        owner: tuple[str, str],
        calendar_ids: Sequence[UUID],
        *,
        expected_version: int,
        operation: str,
        key: str,
        fingerprint: str,
    ) -> CalendarSelection: ...

    async def commit_connected_idempotent(
        self,
        owner: tuple[str, str],
        credential: CalendarCredentialEnvelope,
        *,
        connected_at: datetime,
        operation: str,
        key: str,
        fingerprint: str,
        attempt_id: UUID,
    ) -> CalendarConnection: ...

    async def upsert_discovered(
        self,
        owner: tuple[str, str],
        calendars: Sequence[CalendarDiscoveryRecord],
        *,
        observed_at: datetime,
    ) -> SelectableCalendarPage: ...

    async def provider_calendar_id(self, owner: tuple[str, str], calendar_id: UUID) -> str: ...

    async def record_idempotency(
        self, owner: tuple[str, str], operation: str, key: str, fingerprint: str, response: Any
    ) -> None: ...

    async def find_idempotency(
        self, owner: tuple[str, str], operation: str, key: str, fingerprint: str
    ) -> Any | None: ...

    async def record_audit(
        self,
        *,
        action: str,
        outcome: str,
        category: str | None,
        occurred_at: datetime,
    ) -> None: ...


class CalendarCredentialStore(Protocol):
    async def load(self, owner: tuple[str, str]) -> object: ...

    async def save(self, owner: tuple[str, str], credential: object) -> None: ...

    async def destroy(self, owner: tuple[str, str]) -> None: ...


class CalendarClock(Protocol):
    def __call__(self) -> datetime: ...
