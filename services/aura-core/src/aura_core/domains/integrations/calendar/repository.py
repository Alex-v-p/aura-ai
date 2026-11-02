"""PostgreSQL repository for calendar metadata and lifecycle state."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid5

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .internal import CalendarDiscoveryRecord
from .persistence import (
    CalendarAuditRow,
    CalendarConnectionRow,
    CalendarCredentialRow,
    CalendarIdempotencyRow,
    CalendarMetadataRow,
    CalendarSelectionRow,
    CalendarSelectionVersionRow,
)
from .public import (
    CalendarAuthorizationRejected,
    CalendarConnection,
    CalendarConnectionStatus,
    CalendarCredentialEnvelope,
    CalendarRevocationLease,
    CalendarRevocationState,
    CalendarSelection,
    CalendarValidationError,
    CalendarVersionConflict,
    SelectableCalendar,
    SelectableCalendarPage,
)


class SqlCalendarRepository:
    """Owns calendar connection, discovery, and explicit selection writes."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def _connection_row(
        self, session: AsyncSession, owner: tuple[str, str], *, lock: bool = False
    ) -> CalendarConnectionRow | None:
        statement = select(CalendarConnectionRow).where(
            CalendarConnectionRow.principal_issuer == owner[0],
            CalendarConnectionRow.principal_subject == owner[1],
            CalendarConnectionRow.provider == "google",
        )
        if lock:
            statement = statement.with_for_update()
        return await session.scalar(statement)

    async def get_connection(self, owner: tuple[str, str]) -> CalendarConnection:
        async with self._sessions() as session:
            row = await self._connection_row(session, owner)
            now = datetime.now(UTC)
            if row is None:
                return CalendarConnection(
                    CalendarConnectionStatus.DISCONNECTED, False, 0, None, now
                )
            selected = await session.execute(
                select(CalendarSelectionRow.calendar_id).where(
                    CalendarSelectionRow.connection_id == row.id
                )
            )
            return CalendarConnection(
                CalendarConnectionStatus(row.status),
                row.status == "connected",
                len(selected.all()),
                row.connected_at,
                row.updated_at,
                revocation_terminal=(
                    row.status == CalendarConnectionStatus.REVOCATION_UNCERTAIN.value
                    and (
                        row.revocation_attempts >= 6
                        or (
                            row.revocation_deadline is not None
                            and now >= row.revocation_deadline
                        )
                    )
                ),
            )

    async def save_connection(
        self,
        owner: tuple[str, str],
        connection: CalendarConnection,
        *,
        expected_version: int | None = None,
    ) -> CalendarConnection:
        if connection.status is CalendarConnectionStatus.CONNECTED and not connection.reads_enabled:
            raise CalendarValidationError("connected state must enable reads")
        async with self._sessions() as session:
            async with session.begin():
                row = await self._connection_row(session, owner, lock=True)
                now = datetime.now(UTC)
                if row is None:
                    if expected_version not in (None, 0):
                        raise CalendarVersionConflict("connection version conflict")
                    row = CalendarConnectionRow(
                        principal_issuer=owner[0],
                        principal_subject=owner[1],
                        provider="google",
                        status=connection.status.value,
                        version=1,
                        connected_at=connection.connected_at,
                        updated_at=now,
                    )
                    session.add(row)
                else:
                    if expected_version is not None and row.version != expected_version:
                        raise CalendarVersionConflict("connection version conflict")
                    row.status = connection.status.value
                    row.version += 1
                    row.connected_at = connection.connected_at
                    row.updated_at = now
            return connection

    async def list_calendars(
        self, owner: tuple[str, str], *, cursor: str | None = None, limit: int = 50
    ) -> SelectableCalendarPage:
        if not 1 <= limit <= 100:
            raise CalendarValidationError("calendar limit is outside bounds")
        async with self._sessions() as session:
            row = await self._connection_row(session, owner)
            now = datetime.now(UTC)
            if row is None:
                return SelectableCalendarPage((), None, 1, now)
            selected_result = await session.execute(
                select(CalendarSelectionRow.calendar_id).where(
                    CalendarSelectionRow.connection_id == row.id
                )
            )
            selected = {item[0] for item in selected_result.all()}
            query = (
                select(CalendarMetadataRow)
                .where(
                    CalendarMetadataRow.connection_id == row.id,
                    CalendarMetadataRow.readable.is_(True),
                )
                .order_by(CalendarMetadataRow.id)
                .limit(limit + 1)
            )
            result = list((await session.scalars(query)).all())
            next_cursor = str(result.pop().id) if len(result) > limit else None
            return SelectableCalendarPage(
                tuple(
                    SelectableCalendar(
                        item.id,
                        item.display_name,
                        item.time_zone,
                        item.id in selected,
                        item.discovered_at,
                    )
                    for item in result
                ),
                next_cursor,
                1,
                now,
            )

    async def replace_selection(
        self, owner: tuple[str, str], calendar_ids: Sequence[UUID], *, expected_version: int
    ) -> CalendarSelection:
        ids = tuple(calendar_ids)
        if len(ids) > 50 or len(set(ids)) != len(ids):
            raise CalendarValidationError("selection is outside bounds")
        async with self._sessions() as session:
            async with session.begin():
                connection = await self._connection_row(session, owner, lock=True)
                if connection is None:
                    raise CalendarValidationError("calendar connection is unavailable")
                version_row = await session.scalar(
                    select(CalendarSelectionVersionRow)
                    .where(CalendarSelectionVersionRow.connection_id == connection.id)
                    .with_for_update()
                )
                current = version_row.version if version_row else 1
                if current != expected_version:
                    raise CalendarVersionConflict("calendar selection version conflict")
                if ids:
                    readable = set(
                        (
                            await session.scalars(
                                select(CalendarMetadataRow.id).where(
                                    CalendarMetadataRow.connection_id == connection.id,
                                    CalendarMetadataRow.id.in_(ids),
                                    CalendarMetadataRow.readable.is_(True),
                                )
                            )
                        ).all()
                    )
                    if readable != set(ids):
                        raise CalendarValidationError("selection contains an unavailable calendar")
                await session.execute(
                    delete(CalendarSelectionRow).where(
                        CalendarSelectionRow.connection_id == connection.id
                    )
                )
                for calendar_id in ids:
                    session.add(
                        CalendarSelectionRow(connection_id=connection.id, calendar_id=calendar_id)
                    )
                if version_row is None:
                    version_row = CalendarSelectionVersionRow(
                        connection_id=connection.id, version=2, updated_at=datetime.now(UTC)
                    )
                    session.add(version_row)
                else:
                    version_row.version = current + 1
                    version_row.updated_at = datetime.now(UTC)
                return CalendarSelection(ids, current + 1, datetime.now(UTC))

    async def selected_ids(self, owner: tuple[str, str]) -> tuple[UUID, ...]:
        async with self._sessions() as session:
            row = await session.scalar(
                select(CalendarConnectionRow.id).where(
                    CalendarConnectionRow.principal_issuer == owner[0],
                    CalendarConnectionRow.principal_subject == owner[1],
                    CalendarConnectionRow.provider == "google",
                )
            )
            if row is None:
                return ()
            result = await session.scalars(
                select(CalendarSelectionRow.calendar_id).where(
                    CalendarSelectionRow.connection_id == row
                )
            )
            return tuple(result.all())

    async def connection_id(self, owner: tuple[str, str]) -> UUID | None:
        async with self._sessions() as session:
            return await session.scalar(
                select(CalendarConnectionRow.id).where(
                    CalendarConnectionRow.principal_issuer == owner[0],
                    CalendarConnectionRow.principal_subject == owner[1],
                    CalendarConnectionRow.provider == "google",
                )
            )

    async def commit_connected(
        self,
        owner: tuple[str, str],
        credential: CalendarCredentialEnvelope,
        *,
        connected_at: datetime,
        expected_version: int | None = None,
    ) -> CalendarConnection:
        """Atomically commit the connected state and sealed credential."""
        async with self._sessions() as session:
            async with session.begin():
                row = await self._connection_row(session, owner, lock=True)
                if row is None:
                    if expected_version not in (None, 0):
                        raise CalendarVersionConflict("connection version conflict")
                    row = CalendarConnectionRow(
                        principal_issuer=owner[0],
                        principal_subject=owner[1],
                        provider="google",
                        status=CalendarConnectionStatus.CONNECTED.value,
                        version=1,
                        connected_at=connected_at,
                        updated_at=connected_at,
                    )
                    session.add(row)
                    await session.flush()
                else:
                    if expected_version is not None and row.version != expected_version:
                        raise CalendarVersionConflict("connection version conflict")
                    row.status = CalendarConnectionStatus.CONNECTED.value
                    row.version += 1
                    row.connected_at = connected_at
                    row.updated_at = connected_at
                    row.revocation_attempts = 0
                    row.revocation_deadline = None
                    row.next_revocation_at = None
                existing = await session.get(CalendarCredentialRow, row.id, with_for_update=True)
                if existing is None:
                    session.add(
                        CalendarCredentialRow(
                            connection_id=row.id,
                            ciphertext=credential.ciphertext,
                            nonce=credential.nonce,
                            key_version=credential.key_id,
                            associated_data_digest=credential.associated_data_digest,
                            updated_at=connected_at,
                        )
                    )
                else:
                    existing.ciphertext = credential.ciphertext
                    existing.nonce = credential.nonce
                    existing.key_version = credential.key_id
                    existing.associated_data_digest = credential.associated_data_digest
                    existing.updated_at = connected_at
                return CalendarConnection(
                    CalendarConnectionStatus.CONNECTED,
                    True,
                    0,
                    connected_at,
                    connected_at,
                )

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
    ) -> CalendarConnection:
        """Commit credentials and the content-free completion receipt atomically."""
        from uuid import UUID as UUIDType

        try:
            idempotency_key = UUIDType(key)
        except (TypeError, ValueError) as exc:
            raise CalendarValidationError("idempotency key is invalid") from exc
        async with self._sessions() as session:
            async with session.begin():
                row = await self._connection_row(session, owner, lock=True)
                if row is None:
                    row = CalendarConnectionRow(
                        principal_issuer=owner[0],
                        principal_subject=owner[1],
                        provider="google",
                        status=CalendarConnectionStatus.DISCONNECTED.value,
                        version=1,
                        updated_at=connected_at,
                    )
                    session.add(row)
                    await session.flush()
                receipt = await session.get(
                    CalendarIdempotencyRow,
                    (owner[0], owner[1], operation, idempotency_key),
                    with_for_update=True,
                )
                if receipt is not None:
                    if receipt.fingerprint != fingerprint:
                        raise CalendarVersionConflict("idempotency key payload conflict")
                    if receipt.response.get("status") == "connected":
                        return CalendarConnection(
                            CalendarConnectionStatus.CONNECTED,
                            True,
                            0,
                            row.connected_at,
                            row.updated_at,
                        )
                    if str(receipt.response.get("attempt_id", "")) != str(attempt_id):
                        raise CalendarAuthorizationRejected("authorization callback is invalid")
                else:
                    receipt = CalendarIdempotencyRow(
                        principal_issuer=owner[0],
                        principal_subject=owner[1],
                        operation=operation,
                        idempotency_key=idempotency_key,
                        fingerprint=fingerprint,
                        response={"status": "pending", "attempt_id": str(attempt_id)},
                    )
                    session.add(receipt)
                row.status = CalendarConnectionStatus.CONNECTED.value
                row.version += 1
                row.connected_at = connected_at
                row.updated_at = connected_at
                row.revocation_attempts = 0
                row.revocation_deadline = None
                row.next_revocation_at = None
                existing = await session.get(CalendarCredentialRow, row.id, with_for_update=True)
                if existing is None:
                    session.add(
                        CalendarCredentialRow(
                            connection_id=row.id,
                            ciphertext=credential.ciphertext,
                            nonce=credential.nonce,
                            key_version=credential.key_id,
                            associated_data_digest=credential.associated_data_digest,
                            updated_at=connected_at,
                        )
                    )
                else:
                    existing.ciphertext = credential.ciphertext
                    existing.nonce = credential.nonce
                    existing.key_version = credential.key_id
                    existing.associated_data_digest = credential.associated_data_digest
                    existing.updated_at = connected_at
                assert receipt is not None
                receipt.response = {"status": "connected", "attempt_id": str(attempt_id)}
                return CalendarConnection(
                    CalendarConnectionStatus.CONNECTED,
                    True,
                    0,
                    connected_at,
                    connected_at,
                )

    async def load_credential(
        self, owner: tuple[str, str]
    ) -> tuple[UUID, CalendarCredentialEnvelope] | None:
        async with self._sessions() as session:
            connection = await self._connection_row(session, owner)
            if connection is None:
                return None
            row = await session.get(CalendarCredentialRow, connection.id)
            if row is None:
                return None
            return row.connection_id, CalendarCredentialEnvelope(
                row.ciphertext,
                row.nonce,
                row.key_version,
                row.associated_data_digest,
            )

    async def compare_and_swap_credential(
        self,
        owner: tuple[str, str],
        connection_id: UUID,
        expected: CalendarCredentialEnvelope,
        replacement: CalendarCredentialEnvelope,
    ) -> bool:
        async with self._sessions() as session:
            async with session.begin():
                connection = await self._connection_row(session, owner, lock=True)
                if connection is None or connection.id != connection_id:
                    return False
                row = await session.get(CalendarCredentialRow, connection_id, with_for_update=True)
                if row is None or (
                    row.ciphertext != expected.ciphertext
                    or row.nonce != expected.nonce
                    or row.key_version != expected.key_id
                    or row.associated_data_digest != expected.associated_data_digest
                ):
                    return False
                row.ciphertext = replacement.ciphertext
                row.nonce = replacement.nonce
                row.key_version = replacement.key_id
                row.associated_data_digest = replacement.associated_data_digest
                row.updated_at = datetime.now(UTC)
                return True

    async def destroy_credential(self, owner: tuple[str, str], connection_id: UUID) -> None:
        async with self._sessions() as session:
            async with session.begin():
                connection = await self._connection_row(session, owner, lock=True)
                if connection is None or connection.id != connection_id:
                    return
                await session.execute(
                    delete(CalendarCredentialRow).where(
                        CalendarCredentialRow.connection_id == connection_id
                    )
                )

    async def prepare_revocation(
        self, owner: tuple[str, str], *, now: datetime, deadline: datetime
    ) -> tuple[CalendarConnection, CalendarRevocationState]:
        """Disable reads and establish one fixed revocation retry deadline."""
        async with self._sessions() as session:
            async with session.begin():
                row = await self._connection_row(session, owner, lock=True)
                if row is None:
                    return (
                    CalendarConnection(
                        CalendarConnectionStatus.DISCONNECTED, False, 0, None, now
                    ),
                        CalendarRevocationState(0, None, None),
                    )
                if row.status not in {
                    CalendarConnectionStatus.CONNECTED.value,
                    CalendarConnectionStatus.REAUTHORIZATION_REQUIRED.value,
                    CalendarConnectionStatus.REVOCATION_UNCERTAIN.value,
                }:
                    return (
                        CalendarConnection(
                            CalendarConnectionStatus(row.status),
                            False,
                            0,
                            row.connected_at,
                            row.updated_at,
                        ),
                        CalendarRevocationState(
                            row.revocation_attempts,
                            row.revocation_deadline,
                            row.next_revocation_at,
                        ),
                    )
                if row.status != CalendarConnectionStatus.REVOCATION_UNCERTAIN.value:
                    row.status = CalendarConnectionStatus.REVOCATION_UNCERTAIN.value
                    row.version += 1
                    row.updated_at = now
                    row.revocation_deadline = deadline
                    row.next_revocation_at = now
                elif row.revocation_deadline is None:
                    row.revocation_deadline = deadline
                    row.next_revocation_at = now
                revocation_deadline = cast(datetime | None, row.revocation_deadline)
                if row.revocation_attempts >= 6 or (
                    revocation_deadline is not None and now >= revocation_deadline
                ):
                    await session.execute(
                        delete(CalendarCredentialRow).where(
                            CalendarCredentialRow.connection_id == row.id
                        )
                    )
                    row.status = CalendarConnectionStatus.REVOCATION_UNCERTAIN.value
                    row.revocation_lease_attempt = None
                    row.revocation_lease_expires_at = None
                    row.next_revocation_at = None
                    row.updated_at = now
                    session.add(
                        CalendarAuditRow(
                            action="disconnect",
                            outcome="revocation_uncertain",
                            category="revocation_uncertain",
                            occurred_at=now,
                        )
                    )
                terminal = row.revocation_attempts >= 6 or (
                    revocation_deadline is not None and now >= revocation_deadline
                )
                connection = CalendarConnection(
                    CalendarConnectionStatus(row.status),
                    False,
                    0,
                    row.connected_at,
                    now,
                    revocation_terminal=(
                        row.status == CalendarConnectionStatus.REVOCATION_UNCERTAIN.value
                        and terminal
                    ),
                )
                return connection, CalendarRevocationState(
                    row.revocation_attempts,
                    row.revocation_deadline,
                    row.next_revocation_at,
                )

    async def reserve_revocation_attempt(
        self,
        owner: tuple[str, str],
        *,
        now: datetime,
    ) -> tuple[CalendarConnection, CalendarRevocationState, CalendarRevocationLease | None]:
        """Atomically reserve an outbound provider attempt before dispatch."""
        async with self._sessions() as session:
            async with session.begin():
                row = await self._connection_row(session, owner, lock=True)
                if row is None:
                    return (
                        CalendarConnection(
                            CalendarConnectionStatus.DISCONNECTED, False, 0, None, now
                        ),
                        CalendarRevocationState(0, None, None),
                        None,
                    )
                if row.status in {
                    CalendarConnectionStatus.CONNECTED.value,
                    CalendarConnectionStatus.REAUTHORIZATION_REQUIRED.value,
                }:
                    row.status = CalendarConnectionStatus.REVOCATION_UNCERTAIN.value
                    row.version += 1
                    row.updated_at = now
                    row.revocation_deadline = now + timedelta(hours=24)
                    row.next_revocation_at = now
                elif row.status != CalendarConnectionStatus.REVOCATION_UNCERTAIN.value:
                    return (
                        CalendarConnection(
                            CalendarConnectionStatus(row.status),
                            False,
                            0,
                            row.connected_at,
                            row.updated_at,
                        ),
                        CalendarRevocationState(
                            row.revocation_attempts,
                            row.revocation_deadline,
                            row.next_revocation_at,
                        ),
                        None,
                    )
                if row.revocation_deadline is None:
                    row.revocation_deadline = now + timedelta(hours=24)
                terminal = row.revocation_attempts >= 6 or now >= row.revocation_deadline
                if terminal:
                    terminalizing = (
                        row.next_revocation_at is not None
                        or row.revocation_lease_attempt is not None
                    )
                    await session.execute(
                        delete(CalendarCredentialRow).where(
                            CalendarCredentialRow.connection_id == row.id
                        )
                    )
                    row.revocation_lease_attempt = None
                    row.revocation_lease_expires_at = None
                    row.next_revocation_at = None
                    row.updated_at = now
                    if terminalizing:
                        session.add(
                            CalendarAuditRow(
                                action="disconnect",
                                outcome="revocation_uncertain",
                                category="revocation_uncertain",
                                occurred_at=now,
                            )
                        )
                    return (
                        CalendarConnection(
                            CalendarConnectionStatus.REVOCATION_UNCERTAIN,
                            False,
                            0,
                            row.connected_at,
                            now,
                            revocation_terminal=True,
                        ),
                        CalendarRevocationState(
                            row.revocation_attempts,
                            row.revocation_deadline,
                            None,
                        ),
                        None,
                    )
                if (
                    row.revocation_lease_attempt is not None
                    and row.revocation_lease_expires_at is not None
                    and now < row.revocation_lease_expires_at
                ):
                    next_at = row.revocation_lease_expires_at
                    row.next_revocation_at = next_at
                    return (
                        CalendarConnection(
                            CalendarConnectionStatus.REVOCATION_UNCERTAIN,
                            False,
                            0,
                            row.connected_at,
                            now,
                        ),
                        CalendarRevocationState(
                            row.revocation_attempts, row.revocation_deadline, next_at
                        ),
                        None,
                    )
                if row.next_revocation_at is not None and now < row.next_revocation_at:
                    return (
                        CalendarConnection(
                            CalendarConnectionStatus.REVOCATION_UNCERTAIN,
                            False,
                            0,
                            row.connected_at,
                            now,
                        ),
                        CalendarRevocationState(
                            row.revocation_attempts,
                            row.revocation_deadline,
                            row.next_revocation_at,
                        ),
                        None,
                    )
                row.revocation_attempts += 1
                lease_expires = min(now + timedelta(minutes=5), row.revocation_deadline)
                row.revocation_lease_attempt = row.revocation_attempts
                row.revocation_lease_expires_at = lease_expires
                row.next_revocation_at = lease_expires
                row.updated_at = now
                lease = CalendarRevocationLease(row.revocation_attempts, lease_expires)
                return (
                    CalendarConnection(
                        CalendarConnectionStatus.REVOCATION_UNCERTAIN,
                        False,
                        0,
                        row.connected_at,
                        now,
                    ),
                    CalendarRevocationState(
                        row.revocation_attempts,
                        row.revocation_deadline,
                        lease_expires,
                    ),
                    lease,
                )

    async def finalize_revocation_attempt(
        self,
        owner: tuple[str, str],
        *,
        now: datetime,
        success: bool,
        reservation: CalendarRevocationLease,
        idempotency_key: str | None = None,
        fingerprint: str | None = None,
    ) -> CalendarConnection:
        """Record provider outcome, cleanup, and audit in one transaction."""
        from uuid import UUID as UUIDType

        async with self._sessions() as session:
            async with session.begin():
                row = await self._connection_row(session, owner, lock=True)
                if row is None:
                    return CalendarConnection(
                        CalendarConnectionStatus.DISCONNECTED, False, 0, None, now
                    )
                if row.status != CalendarConnectionStatus.REVOCATION_UNCERTAIN.value:
                    return CalendarConnection(
                        CalendarConnectionStatus(row.status), False, 0, row.connected_at, now
                    )
                if (
                    row.revocation_lease_attempt != reservation.attempt
                    or row.revocation_lease_expires_at != reservation.lease_expires_at
                ):
                    return CalendarConnection(
                        CalendarConnectionStatus.REVOCATION_UNCERTAIN,
                        False,
                        0,
                        row.connected_at,
                        now,
                        revocation_terminal=(
                            row.revocation_attempts >= 6
                            or (
                                row.revocation_deadline is not None
                                and now >= row.revocation_deadline
                            )
                        ),
                    )
                row.revocation_lease_attempt = None
                row.revocation_lease_expires_at = None
                terminal = (not success) and (
                    row.revocation_attempts >= 6
                    or (
                        row.revocation_deadline is not None and now >= row.revocation_deadline
                    )
                )
                if success:
                    row.status = CalendarConnectionStatus.DISCONNECTED.value
                    outcome = "disconnected"
                    category = None
                elif terminal:
                    row.status = CalendarConnectionStatus.REVOCATION_UNCERTAIN.value
                    outcome = "revocation_uncertain"
                    category = "revocation_uncertain"
                else:
                    row.status = CalendarConnectionStatus.REVOCATION_UNCERTAIN.value
                    outcome = "revocation_uncertain"
                    category = "revocation_uncertain"
                row.next_revocation_at = (
                    None if success or terminal else now + timedelta(minutes=30)
                )
                row.updated_at = now
                if success or terminal:
                    await session.execute(
                        delete(CalendarCredentialRow).where(
                            CalendarCredentialRow.connection_id == row.id
                        )
                    )
                session.add(
                    CalendarAuditRow(
                        action="disconnect",
                        outcome=outcome,
                        category=category,
                        occurred_at=now,
                    )
                )
                if idempotency_key is not None and fingerprint is not None:
                    try:
                        key = UUIDType(idempotency_key)
                    except (TypeError, ValueError) as exc:
                        raise CalendarValidationError("idempotency key is invalid") from exc
                    receipt = await session.get(
                        CalendarIdempotencyRow,
                        (owner[0], owner[1], "calendar.connection.disconnect", key),
                        with_for_update=True,
                    )
                    if receipt is None or receipt.fingerprint != fingerprint:
                        raise CalendarVersionConflict("idempotency key payload conflict")
                    receipt.response = {"status": outcome}
                return CalendarConnection(
                    CalendarConnectionStatus(row.status),
                    False,
                    0,
                    row.connected_at,
                    now,
                    revocation_terminal=(
                        row.status == CalendarConnectionStatus.REVOCATION_UNCERTAIN.value
                        and terminal
                    ),
                )

    async def mark_revocation_uncertain(
        self, owner: tuple[str, str], *, now: datetime, deadline: datetime
    ) -> CalendarConnection:
        async with self._sessions() as session:
            async with session.begin():
                row = await self._connection_row(session, owner, lock=True)
                if row is None:
                    return CalendarConnection(
                        CalendarConnectionStatus.DISCONNECTED, False, 0, None, now
                    )
                row.status = CalendarConnectionStatus.REVOCATION_UNCERTAIN.value
                row.version += 1
                row.updated_at = now
                row.revocation_deadline = deadline
                row.next_revocation_at = now
                return CalendarConnection(
                    CalendarConnectionStatus.REVOCATION_UNCERTAIN,
                    False,
                    0,
                    row.connected_at,
                    now,
                )

    async def mark_reauthorization_required(
        self, owner: tuple[str, str], *, now: datetime, category: str
    ) -> CalendarConnection:
        async with self._sessions() as session:
            async with session.begin():
                row = await self._connection_row(session, owner, lock=True)
                if row is None:
                    return CalendarConnection(
                        CalendarConnectionStatus.DISCONNECTED, False, 0, None, now
                    )
                row.status = CalendarConnectionStatus.REAUTHORIZATION_REQUIRED.value
                row.version += 1
                row.updated_at = now
                row.last_error_category = category[:48]
                row.last_error_retryable = False
                row.last_error_recovery_action = "reauthorize"
                row.last_error_at = now
                return CalendarConnection(
                    CalendarConnectionStatus.REAUTHORIZATION_REQUIRED,
                    False,
                    0,
                    row.connected_at,
                    now,
                )

    async def record_revocation_attempt(
        self, owner: tuple[str, str], *, now: datetime, success: bool
    ) -> CalendarConnection:
        async with self._sessions() as session:
            async with session.begin():
                row = await self._connection_row(session, owner, lock=True)
                if row is None:
                    return CalendarConnection(
                        CalendarConnectionStatus.DISCONNECTED, False, 0, None, now
                    )
                row.revocation_attempts += 1
                row.updated_at = now
                if success:
                    row.status = CalendarConnectionStatus.DISCONNECTED.value
                    row.next_revocation_at = None
                elif row.revocation_attempts >= 6 or (
                    row.revocation_deadline and now >= row.revocation_deadline
                ):
                    row.status = CalendarConnectionStatus.DISCONNECTED.value
                    row.next_revocation_at = None
                else:
                    row.status = CalendarConnectionStatus.REVOCATION_UNCERTAIN.value
                    row.next_revocation_at = now + timedelta(minutes=30)
                return CalendarConnection(
                    CalendarConnectionStatus(row.status),
                    False,
                    0,
                    row.connected_at,
                    now,
                )

    async def upsert_discovered(
        self,
        owner: tuple[str, str],
        calendars: Sequence[CalendarDiscoveryRecord],
        *,
        observed_at: datetime,
    ) -> SelectableCalendarPage:
        """Atomically stabilize Aura metadata for one provider page.

        Provider identifiers are used only inside this transaction.  The
        returned projection is rebuilt from Aura rows and cannot leak them
        through the application boundary.
        """
        provider_ids: list[str] = []
        async with self._sessions() as session:
            async with session.begin():
                connection = await self._connection_row(session, owner, lock=True)
                if connection is None:
                    raise CalendarValidationError("calendar connection is unavailable")
                for item in calendars[:100]:
                    provider_id = item.provider_id
                    display_name = item.display_name
                    time_zone = item.time_zone or "UTC"
                    if not provider_id or not display_name or not item.readable:
                        continue
                    if provider_id in provider_ids:
                        continue
                    provider_ids.append(provider_id)
                    calendar_id = uuid5(
                        UUID("4c3fef10-5e6e-4b22-9b2d-9df9c3f1f8c2"),
                        f"{owner[0]}:{owner[1]}:{provider_id}",
                    )
                    row = await session.scalar(
                        select(CalendarMetadataRow)
                        .where(
                            CalendarMetadataRow.connection_id == connection.id,
                            CalendarMetadataRow.provider_calendar_id == provider_id,
                        )
                        .with_for_update()
                    )
                    if row is None:
                        session.add(
                            CalendarMetadataRow(
                                id=calendar_id,
                                connection_id=connection.id,
                                provider_calendar_id=provider_id,
                                display_name=display_name[:255],
                                time_zone=time_zone[:128],
                                readable=True,
                                discovered_at=observed_at,
                                updated_at=observed_at,
                            )
                        )
                    else:
                        row.display_name = display_name[:255]
                        row.time_zone = time_zone[:128]
                        row.readable = True
                        row.updated_at = observed_at
                await session.flush()
                selected_result = await session.execute(
                    select(CalendarSelectionRow.calendar_id).where(
                        CalendarSelectionRow.connection_id == connection.id
                    )
                )
                selected = {item[0] for item in selected_result.all()}
                version_row = await session.scalar(
                    select(CalendarSelectionVersionRow).where(
                        CalendarSelectionVersionRow.connection_id == connection.id
                    )
                )
                selection_version = version_row.version if version_row is not None else 1
                rows = (
                    list(
                        (
                            await session.scalars(
                                select(CalendarMetadataRow).where(
                                    CalendarMetadataRow.connection_id == connection.id,
                                    CalendarMetadataRow.provider_calendar_id.in_(provider_ids),
                                    CalendarMetadataRow.readable.is_(True),
                                )
                            )
                        ).all()
                    )
                    if provider_ids
                    else []
                )
                by_provider_id = {row.provider_calendar_id: row for row in rows}
                items = tuple(
                    SelectableCalendar(
                        by_provider_id[provider_id].id,
                        by_provider_id[provider_id].display_name,
                        by_provider_id[provider_id].time_zone,
                        by_provider_id[provider_id].id in selected,
                        by_provider_id[provider_id].discovered_at,
                    )
                    for provider_id in provider_ids
                    if provider_id in by_provider_id
                )
                return SelectableCalendarPage(items, None, selection_version, observed_at)

    async def provider_calendar_id(self, owner: tuple[str, str], calendar_id: UUID) -> str:
        async with self._sessions() as session:
            value = await session.scalar(
                select(CalendarMetadataRow.provider_calendar_id)
                .join(
                    CalendarConnectionRow,
                    CalendarConnectionRow.id == CalendarMetadataRow.connection_id,
                )
                .where(
                    CalendarConnectionRow.principal_issuer == owner[0],
                    CalendarConnectionRow.principal_subject == owner[1],
                    CalendarMetadataRow.id == calendar_id,
                    CalendarMetadataRow.readable.is_(True),
                )
            )
            if value is None:
                raise CalendarValidationError("calendar is unavailable")
            return value

    async def claim_idempotency(
        self,
        owner: tuple[str, str],
        operation: str,
        key: str,
        fingerprint: str,
        response: object,
    ) -> tuple[bool, object]:
        """Atomically claim or replay one owner/operation/key command."""
        from uuid import UUID as UUIDType

        try:
            idempotency_key = UUIDType(key)
        except (TypeError, ValueError) as exc:
            raise CalendarValidationError("idempotency key is invalid") from exc
        if not operation or len(operation) > 64 or len(fingerprint) != 64:
            raise CalendarValidationError("idempotency metadata is invalid")
        async with self._sessions() as session:
            async with session.begin():
                statement = (
                    pg_insert(CalendarIdempotencyRow)
                    .values(
                        principal_issuer=owner[0],
                        principal_subject=owner[1],
                        operation=operation,
                        idempotency_key=idempotency_key,
                        fingerprint=fingerprint,
                        response=response if isinstance(response, dict) else {},
                    )
                    .on_conflict_do_nothing(
                        index_elements=[
                            CalendarIdempotencyRow.principal_issuer,
                            CalendarIdempotencyRow.principal_subject,
                            CalendarIdempotencyRow.operation,
                            CalendarIdempotencyRow.idempotency_key,
                        ]
                    )
                )
                result = await session.execute(statement)
                row = await session.get(
                    CalendarIdempotencyRow,
                    (owner[0], owner[1], operation, idempotency_key),
                    with_for_update=True,
                )
                if row is None:
                    raise CalendarValidationError("idempotency claim is unavailable")
                if row.fingerprint != fingerprint:
                    raise CalendarVersionConflict("idempotency key payload conflict")
                rowcount = cast(Any, result).rowcount
                return rowcount == 1, row.response

    async def complete_idempotency(
        self,
        owner: tuple[str, str],
        operation: str,
        key: str,
        fingerprint: str,
        response: object,
    ) -> None:
        from uuid import UUID as UUIDType

        try:
            idempotency_key = UUIDType(key)
        except (TypeError, ValueError) as exc:
            raise CalendarValidationError("idempotency key is invalid") from exc
        async with self._sessions() as session:
            async with session.begin():
                row = await session.get(
                    CalendarIdempotencyRow,
                    (owner[0], owner[1], operation, idempotency_key),
                    with_for_update=True,
                )
                if row is None or row.fingerprint != fingerprint:
                    raise CalendarVersionConflict("idempotency key payload conflict")
                row.response = response if isinstance(response, dict) else {}

    @staticmethod
    def _selection_from_receipt(response: object) -> CalendarSelection:
        if not isinstance(response, dict):
            raise CalendarVersionConflict("stored selection receipt is invalid")
        receipt = cast(dict[str, object], response)
        raw_ids = receipt.get("calendar_ids")
        if not isinstance(raw_ids, (list, tuple)):
            raise CalendarVersionConflict("stored selection receipt is invalid")
        try:
            raw_values = cast(tuple[object, ...] | list[object], raw_ids)
            ids = tuple(UUID(str(value)) for value in raw_values)
            version = int(str(receipt["version"]))
            updated_at = datetime.fromisoformat(str(receipt["updated_at"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise CalendarVersionConflict("stored selection receipt is invalid") from exc
        return CalendarSelection(ids, version, updated_at)

    async def replace_selection_idempotent(
        self,
        owner: tuple[str, str],
        calendar_ids: Sequence[UUID],
        *,
        expected_version: int,
        operation: str,
        key: str,
        fingerprint: str,
    ) -> CalendarSelection:
        """Mutate selection and receipt under one owner-locked transaction."""
        from uuid import UUID as UUIDType

        ids = tuple(calendar_ids)
        if len(ids) > 50 or len(set(ids)) != len(ids):
            raise CalendarValidationError("selection is outside bounds")
        try:
            idempotency_key = UUIDType(key)
        except (TypeError, ValueError) as exc:
            raise CalendarValidationError("idempotency key is invalid") from exc
        async with self._sessions() as session:
            async with session.begin():
                connection = await self._connection_row(session, owner, lock=True)
                if connection is None:
                    raise CalendarValidationError("calendar connection is unavailable")
                existing = await session.get(
                    CalendarIdempotencyRow,
                    (owner[0], owner[1], operation, idempotency_key),
                    with_for_update=True,
                )
                if existing is not None:
                    if existing.fingerprint != fingerprint:
                        raise CalendarVersionConflict("idempotency key payload conflict")
                    return self._selection_from_receipt(existing.response)
                version_row = await session.scalar(
                    select(CalendarSelectionVersionRow)
                    .where(CalendarSelectionVersionRow.connection_id == connection.id)
                    .with_for_update()
                )
                current = version_row.version if version_row else 1
                if current != expected_version:
                    raise CalendarVersionConflict("calendar selection version conflict")
                if ids:
                    readable = set(
                        (
                            await session.scalars(
                                select(CalendarMetadataRow.id).where(
                                    CalendarMetadataRow.connection_id == connection.id,
                                    CalendarMetadataRow.id.in_(ids),
                                    CalendarMetadataRow.readable.is_(True),
                                )
                            )
                        ).all()
                    )
                    if readable != set(ids):
                        raise CalendarValidationError("selection contains an unavailable calendar")
                await session.execute(
                    delete(CalendarSelectionRow).where(
                        CalendarSelectionRow.connection_id == connection.id
                    )
                )
                for calendar_id in ids:
                    session.add(
                        CalendarSelectionRow(connection_id=connection.id, calendar_id=calendar_id)
                    )
                updated_at = datetime.now(UTC)
                if version_row is None:
                    session.add(
                        CalendarSelectionVersionRow(
                            connection_id=connection.id, version=2, updated_at=updated_at
                        )
                    )
                else:
                    version_row.version = current + 1
                    version_row.updated_at = updated_at
                selection = CalendarSelection(ids, current + 1, updated_at)
                session.add(
                    CalendarIdempotencyRow(
                        principal_issuer=owner[0],
                        principal_subject=owner[1],
                        operation=operation,
                        idempotency_key=idempotency_key,
                        fingerprint=fingerprint,
                        response={
                            "calendar_ids": [str(value) for value in ids],
                            "version": selection.version,
                            "updated_at": updated_at.isoformat(),
                        },
                    )
                )
                return selection

    async def record_idempotency(
        self, owner: tuple[str, str], operation: str, key: str, fingerprint: str, response: object
    ) -> None:
        async with self._sessions() as session:
            async with session.begin():
                from uuid import UUID as UUIDType

                session.add(
                    CalendarIdempotencyRow(
                        principal_issuer=owner[0],
                        principal_subject=owner[1],
                        operation=operation,
                        idempotency_key=UUIDType(key),
                        fingerprint=fingerprint,
                        response=response if isinstance(response, dict) else {},
                    )
                )

    async def find_idempotency(
        self, owner: tuple[str, str], operation: str, key: str, fingerprint: str
    ) -> object | None:
        async with self._sessions() as session:
            from uuid import UUID as UUIDType

            row = await session.get(
                CalendarIdempotencyRow,
                (owner[0], owner[1], operation, UUIDType(key)),
            )
            if row is None:
                return None
            if row.fingerprint != fingerprint:
                raise CalendarVersionConflict("idempotency key payload conflict")
            return row.response

    async def record_audit(
        self,
        *,
        action: str,
        outcome: str,
        category: str | None,
        occurred_at: datetime,
    ) -> None:
        if len(action) > 48 or len(outcome) > 32 or (category is not None and len(category) > 48):
            raise CalendarValidationError("audit metadata is outside bounds")
        async with self._sessions() as session:
            async with session.begin():
                session.add(
                    CalendarAuditRow(
                        action=action,
                        outcome=outcome,
                        category=category,
                        occurred_at=occurred_at,
                    )
                )
