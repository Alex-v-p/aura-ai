"""PostgreSQL-owned persona configuration and hydration."""

from datetime import UTC, datetime
from hashlib import sha256
from json import dumps
from uuid import UUID, uuid4

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aura_core.domains.governance.audit.public import SqlAuditRepository
from aura_core.domains.governance.identity.public import SqlIdentityRepository
from aura_core.domains.interaction.personas.persistence import PersonaProfileRow, PersonaRevisionRow
from aura_core.domains.interaction.personas.public import (
    ConfigurationDisabled,
    ConfigurationIdempotencyConflict,
    ConfigurationNotFound,
    ConfigurationStatus,
    ConfigurationVersionConflict,
    PersonaProfile,
    PersonaRevision,
)


class SqlPersonaStore:
    """SQL-authoritative persona port used by routes and other domains."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions
        self.personas: dict[UUID, PersonaProfile] = {}
        self.audit = SqlAuditRepository()
        self.identities = SqlIdentityRepository()

    async def refresh(self) -> None:
        async with self.sessions() as session:
            profiles = (await session.execute(select(PersonaProfileRow))).scalars().all()
            revisions = (await session.execute(select(PersonaRevisionRow))).scalars().all()
        revisions_by_profile: dict[UUID, list[PersonaRevision]] = {}
        for item in revisions:
            revisions_by_profile.setdefault(item.persona_profile_id, []).append(
                self._revision_dto(item)
            )
        self.personas = {
            row.id: PersonaProfile(
                id=row.id,
                slug=row.slug,
                display_name=row.display_name,
                status=ConfigurationStatus(row.status),
                version=row.version,
                current_revision_id=row.current_revision_id,
                revisions=revisions_by_profile.get(row.id, []),
                created_at=row.created_at or datetime.now(UTC),
                updated_at=row.updated_at or datetime.now(UTC),
            )
            for row in profiles
        }

    async def list_personas(self) -> list[PersonaProfile]:
        await self.refresh()
        return list(self.personas.values())

    async def get_persona(self, identifier: UUID) -> PersonaProfile:
        await self.refresh()
        try:
            return self.personas[identifier]
        except KeyError as exc:
            raise ConfigurationNotFound("persona not found") from exc

    async def require_active_revision(self, identifier: UUID) -> PersonaRevision:
        """Resolve a revision, then enforce the owning profile's active state."""

        async with self.sessions() as session:
            return await self.require_active_revision_in_transaction(session, identifier)

    async def require_active_revision_in_transaction(
        self, session: AsyncSession, identifier: UUID
    ) -> PersonaRevision:
        """Resolve and lock the owning profile in a caller-owned transaction."""

        revision = await session.get(PersonaRevisionRow, identifier)
        if revision is None:
            raise ConfigurationNotFound("persona revision not found")
        profile = await session.get(
            PersonaProfileRow, revision.persona_profile_id, with_for_update=True
        )
        if profile is None:
            raise ConfigurationNotFound("persona profile not found")
        if profile.status != ConfigurationStatus.ACTIVE.value:
            raise ConfigurationDisabled("persona is disabled")
        return self._revision_dto(revision)

    async def create_persona(
        self,
        issuer: str,
        subject: str,
        slug: str,
        display_name: str,
        description: str,
        instructions: str,
        key: str,
    ) -> PersonaProfile:
        fingerprint = self._fingerprint("persona", slug, display_name, description, instructions)
        async with self.sessions() as session, session.begin():
            prior = await self._replay(session, issuer, subject, key, fingerprint)
            if prior is None:
                identifier, revision_id, created = uuid4(), uuid4(), datetime.now(UTC)
                session.add(
                    PersonaProfileRow(
                        id=identifier,
                        slug=slug,
                        display_name=display_name,
                        current_revision_id=revision_id,
                        created_at=created,
                        updated_at=created,
                    )
                )
                session.add(
                    PersonaRevisionRow(
                        id=revision_id,
                        persona_profile_id=identifier,
                        revision=1,
                        display_name=display_name,
                        description=description,
                        instructions=instructions,
                        created_at=created,
                    )
                )
                await self._record(session, issuer, subject, key, fingerprint, identifier)
                await self._stage_audit(
                    session, issuer, subject, "persona.create", identifier, 1
                )
            else:
                identifier = UUID(prior["profileId"])
        return await self.get_persona(identifier)

    async def revise_persona(
        self,
        issuer: str,
        subject: str,
        identifier: UUID,
        expected: int,
        display_name: str,
        description: str,
        instructions: str,
        key: str,
    ) -> PersonaProfile:
        fingerprint = self._fingerprint(
            "persona-revision", identifier, expected, display_name, description, instructions
        )
        async with self.sessions() as session, session.begin():
            prior = await self._replay(session, issuer, subject, key, fingerprint)
            if prior is None:
                row = await session.get(PersonaProfileRow, identifier, with_for_update=True)
                if row is None:
                    raise ConfigurationNotFound("persona not found")
                if row.version != expected:
                    raise ConfigurationVersionConflict("configuration version conflict")
                number = (
                    await session.execute(
                        select(PersonaRevisionRow.revision)
                        .where(PersonaRevisionRow.persona_profile_id == identifier)
                        .order_by(PersonaRevisionRow.revision.desc())
                        .limit(1)
                    )
                ).scalar_one() + 1
                revision_id, now = uuid4(), datetime.now(UTC)
                session.add(
                    PersonaRevisionRow(
                        id=revision_id,
                        persona_profile_id=identifier,
                        revision=number,
                        display_name=display_name,
                        description=description,
                        instructions=instructions,
                        created_at=now,
                    )
                )
                row.current_revision_id, row.version, row.updated_at = (
                    revision_id,
                    row.version + 1,
                    now,
                )
                await self._record(session, issuer, subject, key, fingerprint, identifier)
                await self._stage_audit(
                    session, issuer, subject, "persona.revise", identifier, number
                )
            else:
                identifier = UUID(prior["profileId"])
        return await self.get_persona(identifier)

    async def set_status(
        self,
        issuer: str,
        subject: str,
        identifier: UUID,
        expected: int,
        status: ConfigurationStatus,
        key: str,
    ) -> PersonaProfile:
        fingerprint = self._fingerprint("persona-status", identifier, expected, status.value)
        async with self.sessions() as session, session.begin():
            prior = await self._replay(session, issuer, subject, key, fingerprint)
            row = await session.get(PersonaProfileRow, identifier, with_for_update=True)
            if row is None:
                raise ConfigurationNotFound("persona not found")
            if prior is None:
                if row.version != expected:
                    raise ConfigurationVersionConflict("configuration version conflict")
                row.status, row.version, row.updated_at = (
                    status.value,
                    row.version + 1,
                    datetime.now(UTC),
                )
                await self._record(session, issuer, subject, key, fingerprint, identifier)
                await self._stage_audit(
                    session, issuer, subject, "persona.status", identifier, None
                )
        return await self.get_persona(identifier)

    async def _stage_audit(
        self,
        session: AsyncSession,
        issuer: str,
        subject: str,
        action: str,
        identifier: UUID,
        revision: int | None,
    ) -> None:
        principal = await self.identities.resolve(session, issuer, subject)
        metadata: dict[str, object] = {"personaId": str(identifier)}
        if revision is not None:
            metadata["revision"] = revision
        self.audit.stage(session, principal.id, action, "ok", metadata)

    @staticmethod
    def _revision_dto(row: PersonaRevisionRow) -> PersonaRevision:
        return PersonaRevision(
            id=row.id,
            profile_id=row.persona_profile_id,
            revision=row.revision,
            description=row.description,
            instructions=row.instructions,
            display_name=row.display_name or "",
            created_at=row.created_at or datetime.now(UTC),
        )

    @staticmethod
    def _fingerprint(*parts: object) -> str:
        canonical = dumps(
            {
                "operation": str(parts[0]) if parts else "configuration",
                "arguments": [str(part) for part in parts[1:]],
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return sha256(canonical.encode()).hexdigest()

    async def _replay(
        self, session: AsyncSession, issuer: str, subject: str, key: str, fingerprint: str
    ):
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:command_key, 0))"),
            {"command_key": f"{issuer}:{subject}:{key}"},
        )
        row = (
            (
                await session.execute(
                    text(
                        "SELECT fingerprint, response FROM command_idempotency "
                        "WHERE principal_issuer=:issuer AND principal_subject=:subject "
                        "AND idempotency_key=:key FOR UPDATE"
                    ),
                    {"issuer": issuer, "subject": subject, "key": key},
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            return None
        if row["fingerprint"] != fingerprint:
            raise ConfigurationIdempotencyConflict("idempotency key payload conflict")
        return row["response"]

    async def _record(
        self,
        session: AsyncSession,
        issuer: str,
        subject: str,
        key: str,
        fingerprint: str,
        identifier: UUID,
    ) -> None:
        await session.execute(
            text(
                "INSERT INTO command_idempotency "
                "(principal_issuer, principal_subject, idempotency_key, fingerprint, response) "
                "VALUES (:issuer,:subject,:key,:fingerprint,CAST(:response AS JSONB))"
            ),
            {
                "issuer": issuer,
                "subject": subject,
                "key": key,
                "fingerprint": fingerprint,
                "response": dumps({"profileId": str(identifier)}),
            },
        )
