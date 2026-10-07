"""Provider-neutral persona DTOs owned by the personas boundary."""

from __future__ import annotations

import hashlib
import inspect
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID, uuid4, uuid5

PERSONA_NAMESPACE = UUID("a8a6b450-20fb-4c6a-b0af-e7cb0f9c7b8a")
NEUTRAL_PERSONA_ID = uuid5(PERSONA_NAMESPACE, "neutral-persona")
NEUTRAL_PERSONA_REVISION_ID = uuid5(PERSONA_NAMESPACE, "neutral-persona-revision-1")


class ConfigurationStatus(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"


class ConfigurationNotFound(LookupError):
    """The requested persona profile or revision does not exist."""


class ConfigurationDisabled(ValueError):
    """A disabled persona cannot be selected for new configuration."""


class ConfigurationVersionConflict(RuntimeError):
    """The optimistic configuration version no longer matches."""


class ConfigurationIdempotencyConflict(RuntimeError):
    """An idempotency key was replayed with a different payload."""


@dataclass(frozen=True, slots=True)
class PersonaRevision:
    id: UUID
    profile_id: UUID
    revision: int
    description: str
    instructions: str
    display_name: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(slots=True)
class PersonaProfile:
    id: UUID
    slug: str
    display_name: str
    status: ConfigurationStatus = ConfigurationStatus.ACTIVE
    version: int = 1
    current_revision_id: UUID | None = None
    revisions: list[PersonaRevision] = field(default_factory=lambda: list[PersonaRevision]())
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def current_revision(self) -> PersonaRevision:
        if self.current_revision_id is None or not self.revisions:
            raise LookupError("persona has no revision")
        return next(item for item in self.revisions if item.id == self.current_revision_id)


class PersonaConfigurationQueryPort(Protocol):
    """Public read/admission seam consumed by the agents boundary."""

    async def list_personas(self) -> list[PersonaProfile]: ...

    async def require_active_revision_in_transaction(
        self, session: Any, identifier: UUID
    ) -> PersonaRevision: ...


class PersonaRevisionQueryPort(Protocol):
    """Synchronous read-model seam used by runtime prompt resolution."""

    def list_personas(self) -> list[PersonaProfile]: ...

    def find_revision(self, identifier: UUID) -> tuple[PersonaProfile, PersonaRevision]: ...

    def replace(self, profiles: list[PersonaProfile]) -> None: ...


class PersonaConfigurationRepository(Protocol):
    async def list_personas(self) -> list[PersonaProfile]: ...
    async def get_persona(self, identifier: UUID) -> PersonaProfile: ...
    async def require_active_revision(self, identifier: UUID) -> PersonaRevision: ...
    async def require_active_revision_in_transaction(
        self, session: Any, identifier: UUID
    ) -> PersonaRevision: ...
    async def create_persona(
        self,
        issuer: str,
        subject: str,
        slug: str,
        display_name: str,
        description: str,
        instructions: str,
        key: str,
    ) -> PersonaProfile: ...
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
    ) -> PersonaProfile: ...
    async def set_status(
        self,
        issuer: str,
        subject: str,
        identifier: UUID,
        expected: int,
        status: ConfigurationStatus,
        key: str,
    ) -> PersonaProfile: ...


class PersonaCatalog:
    """In-memory persona repository used by the test composition.

    Persona state belongs to this boundary.  The agent catalog consumes the
    query methods through the public persona API and never owns persona rows.
    The optional issuer/subject arguments make this repository compatible with
    the durable repository port while preserving the small synchronous API
    used by the domain tests.
    """

    def __init__(self) -> None:
        revision = PersonaRevision(
            NEUTRAL_PERSONA_REVISION_ID,
            NEUTRAL_PERSONA_ID,
            1,
            "A neutral, clear conversational style.",
            "Use a clear, calm and direct conversational style.",
            "Neutral",
        )
        self.personas: dict[UUID, PersonaProfile] = {
            NEUTRAL_PERSONA_ID: PersonaProfile(
                NEUTRAL_PERSONA_ID,
                "neutral",
                "Neutral",
                current_revision_id=revision.id,
                revisions=[revision],
            )
        }
        self._idempotency: dict[str, tuple[str, object]] = {}

    def replace(self, profiles: list[PersonaProfile]) -> None:
        self.personas = {profile.id: profile for profile in profiles}

    def list_personas(self) -> list[PersonaProfile]:
        return list(self.personas.values())

    def get_persona(self, identifier: UUID) -> PersonaProfile:
        try:
            return self.personas[identifier]
        except KeyError as exc:
            raise ConfigurationNotFound("persona not found") from exc

    def require_active_revision(self, identifier: UUID) -> PersonaRevision:
        profile, revision = self.find_revision(identifier)
        if profile.status != ConfigurationStatus.ACTIVE:
            raise ConfigurationDisabled("persona is disabled")
        return revision

    def create_persona(
        self,
        issuer: str,
        subject: str,
        slug: str,
        display_name: str,
        description: str,
        instructions: str,
        key: str,
    ) -> PersonaProfile:
        fingerprint = _canonical_fingerprint("persona", dict(zip(
            ("issuer", "subject", "slug", "displayName", "description", "instructions"),
            (issuer, subject, slug, display_name, description, instructions),
            strict=True,
        )))
        if key in self._idempotency:
            previous, result = self._idempotency[key]
            if previous != fingerprint or not isinstance(result, PersonaProfile):
                raise ConfigurationIdempotencyConflict("idempotency key payload conflict")
            return result
        profile_id = uuid4()
        revision = PersonaRevision(uuid4(), profile_id, 1, description, instructions, display_name)
        profile = PersonaProfile(
            profile_id,
            slug,
            display_name,
            current_revision_id=revision.id,
            revisions=[revision],
        )
        self.personas[profile_id] = profile
        self._idempotency[key] = (fingerprint, profile)
        return profile

    def revise_persona(
        self,
        issuer: str,
        subject: str,
        profile_id: UUID,
        expected: int,
        display_name: str,
        description: str,
        instructions: str,
        key: str,
    ) -> PersonaProfile:
        profile = self.get_persona(profile_id)
        fingerprint = _canonical_fingerprint(
            "persona-revision",
            {
                "issuer": issuer,
                "subject": subject,
                "profileId": str(profile_id),
                "expectedVersion": expected,
                "displayName": display_name,
                "description": description,
                "instructions": instructions,
            },
        )
        if key in self._idempotency:
            previous, result = self._idempotency[key]
            if previous != fingerprint or not isinstance(result, PersonaProfile):
                raise ConfigurationIdempotencyConflict("idempotency key payload conflict")
            return result
        _check_version(profile.version, expected)
        revision = PersonaRevision(
            uuid4(), profile.id, len(profile.revisions) + 1, description,
            instructions, display_name
        )
        profile.revisions.append(revision)
        profile.current_revision_id = revision.id
        profile.version += 1
        profile.updated_at = datetime.now(UTC)
        self._idempotency[key] = (fingerprint, profile)
        return profile

    def set_status(
        self,
        issuer: str,
        subject: str,
        profile_id: UUID,
        expected: int,
        status: ConfigurationStatus,
        key: str,
    ) -> PersonaProfile:
        profile = self.get_persona(profile_id)
        fingerprint = _canonical_fingerprint(
            "persona-status",
            {
                "issuer": issuer,
                "subject": subject,
                "profileId": str(profile_id),
                "expectedVersion": expected,
                "status": status.value,
            },
        )
        if key in self._idempotency:
            previous, result = self._idempotency[key]
            if previous != fingerprint or not isinstance(result, PersonaProfile):
                raise ConfigurationIdempotencyConflict("idempotency key payload conflict")
            return result
        _check_version(profile.version, expected)
        profile.status = status
        profile.version += 1
        profile.updated_at = datetime.now(UTC)
        self._idempotency[key] = (fingerprint, profile)
        return profile

    def find_revision(self, identifier: UUID) -> tuple[PersonaProfile, PersonaRevision]:
        for profile in self.personas.values():
            for revision in profile.revisions:
                if revision.id == identifier:
                    return profile, revision
        raise ConfigurationNotFound("persona revision not found")


class PersonaMemoryRepository:
    """Async repository adapter for the deterministic in-memory composition."""

    def __init__(self, catalog: PersonaCatalog) -> None:
        self.catalog = catalog

    async def list_personas(self) -> list[PersonaProfile]:
        return self.catalog.list_personas()

    async def get_persona(self, identifier: UUID) -> PersonaProfile:
        return self.catalog.get_persona(identifier)

    async def require_active_revision(self, identifier: UUID) -> PersonaRevision:
        return self.catalog.require_active_revision(identifier)

    async def require_active_revision_in_transaction(
        self, session: object, identifier: UUID
    ) -> PersonaRevision:
        del session
        return self.catalog.require_active_revision(identifier)

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
        return self.catalog.create_persona(
            issuer, subject, slug, display_name, description, instructions, key
        )

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
        return self.catalog.revise_persona(
            issuer, subject, identifier, expected, display_name, description, instructions, key
        )

    async def set_status(
        self,
        issuer: str,
        subject: str,
        identifier: UUID,
        expected: int,
        status: ConfigurationStatus,
        key: str,
    ) -> PersonaProfile:
        return self.catalog.set_status(issuer, subject, identifier, expected, status, key)


class PersonaConfigurationService:
    """Public application service shared by memory and SQL compositions."""

    def __init__(
        self, repository: PersonaConfigurationRepository, audit: object | None = None
    ) -> None:
        self.repository = repository
        self.audit = audit

    async def list_personas(self) -> list[PersonaProfile]:
        return await self.repository.list_personas()

    async def get_persona(self, identifier: UUID) -> PersonaProfile:
        return await self.repository.get_persona(identifier)

    async def require_active_revision(self, identifier: UUID) -> PersonaRevision:
        return await self.repository.require_active_revision(identifier)

    async def require_active_revision_in_transaction(
        self, session: object, identifier: UUID
    ) -> PersonaRevision:
        return await self.repository.require_active_revision_in_transaction(session, identifier)

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
        result = await self.repository.create_persona(
            issuer, subject, slug, display_name, description, instructions, key
        )
        await self._audit("persona.create", issuer, subject, result)
        return result

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
        result = await self.repository.revise_persona(
            issuer, subject, identifier, expected, display_name, description, instructions, key
        )
        await self._audit("persona.revise", issuer, subject, result)
        return result

    async def set_status(
        self,
        issuer: str,
        subject: str,
        identifier: UUID,
        expected: int,
        status: ConfigurationStatus,
        key: str,
    ) -> PersonaProfile:
        result = await self.repository.set_status(
            issuer, subject, identifier, expected, status, key
        )
        await self._audit("persona.status", issuer, subject, result)
        return result

    async def _audit(self, action: str, issuer: str, subject: str, result: object) -> None:
        if self.audit is None or not isinstance(result, PersonaProfile):
            return
        callback = self.audit
        if callable(callback):
            value = callback(
                action,
                "ok",
                issuer=issuer,
                subject=subject,
                metadata={
                    "personaId": str(result.id),
                    "revision": result.current_revision.revision,
                },
            )
            if inspect.isawaitable(value):
                await value


def _canonical_fingerprint(operation: str, payload: dict[str, object]) -> str:
    canonical = json.dumps(
        {"operation": operation, "payload": payload},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(canonical).hexdigest()


def _check_version(actual: int, expected: int) -> None:
    if actual != expected:
        raise ConfigurationVersionConflict("configuration version conflict")


__all__ = [
    "ConfigurationDisabled",
    "ConfigurationIdempotencyConflict",
    "ConfigurationNotFound",
    "ConfigurationStatus",
    "ConfigurationVersionConflict",
    "NEUTRAL_PERSONA_ID",
    "NEUTRAL_PERSONA_REVISION_ID",
    "PERSONA_NAMESPACE",
    "PersonaProfile",
    "PersonaRevision",
    "PersonaCatalog",
    "PersonaConfigurationRepository",
    "PersonaConfigurationService",
    "PersonaConfigurationQueryPort",
    "PersonaRevisionQueryPort",
    "PersonaMemoryRepository",
]
