"""Provider-neutral persona DTOs owned by the personas boundary."""

from __future__ import annotations

import hashlib
import inspect
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID, uuid4, uuid5

PERSONA_NAMESPACE = UUID("a8a6b450-20fb-4c6a-b0af-e7cb0f9c7b8a")
NEUTRAL_PERSONA_ID = uuid5(PERSONA_NAMESPACE, "neutral-persona")
NEUTRAL_PERSONA_REVISION_ID = uuid5(PERSONA_NAMESPACE, "neutral-persona-revision-1")
PREVIEW_GOVERNANCE = (
    "This is an ephemeral Persona preview. Answer the supplied test prompt directly; "
    "do not use tools, memory, conversation history, files, or credentials."
)
_PREVIEW_ERROR_CLASSES = frozenset(
    {
        "authorization",
        "validation",
        "not_found",
        "disabled",
        "capability",
        "provider",
        "timeout",
        "unknown",
    }
)


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
class PersonaPreviewTrace:
    trace_id: str
    span_id: str


@dataclass(frozen=True, slots=True)
class PersonaPreviewResult:
    """Bounded provider metadata and content returned by the preview port."""

    content: str
    provider: str | None = None


class PersonaPreviewPort(Protocol):
    async def preview(
        self,
        model_id: str,
        system_prompt: str,
        prompt: str,
        *,
        trace: PersonaPreviewTrace,
    ) -> PersonaPreviewResult | str: ...


class PersonaPreviewMetricsPort(Protocol):
    def increment(
        self, component: str, name: str, *, trace_id: str | None = None, **attributes: str
    ) -> None: ...
    def record_span(
        self,
        component: str,
        operation: str,
        duration_ms: float,
        *,
        trace_id: str,
        span_id: str,
        parent_span_id: str | None,
        dependency: str,
        outcome: str,
        error_class: str | None = None,
        provider: str | None = None,
        **attributes: str,
    ) -> None: ...


class PersonaPreviewError(RuntimeError):
    """Stable, content-free preview failure."""

    def __init__(self, error_class: str, *, provider: str | None = None) -> None:
        super().__init__("persona preview unavailable")
        self.error_class = error_class
        self.provider = provider


def _bounded_provider(value: str | None) -> str | None:
    if value is None:
        return None
    return value if re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", value) else "unknown"


def _preview_dependency(error_class: str | None, provider: str | None) -> str:
    """Return the bounded dependency origin for a preview outcome.

    Preview telemetry has both a failure counter and a root span.  Keeping
    this classification in one function prevents those two records from
    attributing the same failure to different dependencies.
    """

    if error_class is None:
        return "model_provider"
    if error_class in {"capability", "provider", "timeout"}:
        return "model_provider"
    if error_class in {"not_found", "disabled"}:
        return "configuration_store"
    if error_class == "authorization":
        return "identity_authorization"
    if error_class == "validation":
        return "application_validation"
    if error_class == "unknown":
        return "model_provider" if provider is not None else "application_unknown"
    return "application_unknown"


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
        self,
        repository: PersonaConfigurationRepository,
        audit: object | None = None,
        preview_port: PersonaPreviewPort | None = None,
        metrics: PersonaPreviewMetricsPort | None = None,
    ) -> None:
        self.repository = repository
        self.audit = audit
        self.preview_port = preview_port
        self.metrics = metrics

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

    async def preview(
        self,
        issuer: str,
        subject: str,
        model_id: str,
        prompt: str,
        *,
        draft: PersonaRevision | None = None,
        persona_revision_id: UUID | None = None,
    ) -> str:
        del issuer, subject
        trace_id = uuid4().hex
        span_id = uuid4().hex[:16]
        started = datetime.now(UTC)
        outcome = "error"
        error_class: str | None = None
        provider: str | None = None
        try:
            if self.preview_port is None:
                raise PersonaPreviewError("capability")
            if (draft is None) == (persona_revision_id is None):
                raise ValueError("exactly one preview source is required")
            if draft is None:
                draft = await self.repository.require_active_revision(persona_revision_id)  # type: ignore[arg-type]
            preview = await self.preview_port.preview(
                model_id,
                (
                    f"{PREVIEW_GOVERNANCE}\nYou are {draft.display_name}. {draft.description}\n"
                    f"Behavioral guidance: {draft.instructions}"
                )[:35_000],
                prompt,
                trace=PersonaPreviewTrace(trace_id, span_id),
            )
            if isinstance(preview, PersonaPreviewResult):
                content = preview.content
                provider = _bounded_provider(preview.provider)
            else:
                content = preview
            content = content[:32_768]
            outcome = "ok"
        except PersonaPreviewError as exc:
            error_class = (
                exc.error_class
                if exc.error_class in _PREVIEW_ERROR_CLASSES
                else "unknown"
            )
            provider = _bounded_provider(exc.provider)
            raise PersonaPreviewError(error_class, provider=provider) from exc
        except TimeoutError as exc:
            error_class = "timeout"
            provider = _bounded_provider(getattr(exc, "provider", None))
            raise PersonaPreviewError("timeout", provider=provider) from exc
        except ConfigurationNotFound as exc:
            error_class = "not_found"
            raise PersonaPreviewError("not_found") from exc
        except ConfigurationDisabled as exc:
            error_class = "disabled"
            raise PersonaPreviewError("disabled") from exc
        except LookupError as exc:
            error_class = "capability"
            raise PersonaPreviewError("capability", provider=provider) from exc
        except ValueError as exc:
            error_class = "validation"
            raise PersonaPreviewError("validation") from exc
        except RuntimeError as exc:
            error_class = "provider"
            provider = _bounded_provider(getattr(exc, "provider", None))
            raise PersonaPreviewError("provider", provider=provider) from exc
        except Exception as exc:
            error_class = "unknown"
            provider = _bounded_provider(getattr(exc, "provider", None))
            raise PersonaPreviewError("unknown", provider=provider) from exc
        finally:
            elapsed = (datetime.now(UTC) - started).total_seconds() * 1000
            dependency = _preview_dependency(error_class, provider)
            self._metric_increment(
                "persona_preview_outcome",
                trace_id=trace_id,
                outcome=outcome,
                **({"provider": provider} if provider else {}),
                **({"error_class": error_class} if error_class else {}),
            )
            if outcome == "error" and error_class is not None:
                self._metric_increment(
                    "errors",
                    trace_id=trace_id,
                    outcome=outcome,
                    error_class=error_class,
                    dependency=dependency,
                )
                if error_class in {"provider", "timeout", "unknown"} and provider is not None:
                    self._metric_increment(
                        "provider_errors",
                        trace_id=trace_id,
                        outcome=outcome,
                        error_class=error_class,
                        dependency="model_provider",
                        provider=provider or "unknown",
                    )
            self._record_span(
                trace_id,
                span_id,
                elapsed,
                outcome,
                error_class,
                provider,
                dependency,
            )
        return content

    def _metric_increment(self, name: str, *, trace_id: str, **attributes: str) -> None:
        if self.metrics is not None:
            self.metrics.increment(
                "aura.interaction.persona_preview", name, trace_id=trace_id, **attributes
            )

    def _record_span(
        self,
        trace_id: str,
        span_id: str,
        duration_ms: float,
        outcome: str,
        error_class: str | None,
        provider: str | None,
        dependency: str,
    ) -> None:
        if self.metrics is not None:
            self.metrics.record_span(
                "aura.interaction.persona_preview",
                "persona.preview",
                duration_ms,
                trace_id=trace_id,
                span_id=span_id,
                parent_span_id=None,
                dependency=dependency,
                outcome=outcome,
                error_class=error_class,
                provider=provider,
            )

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
    "PersonaPreviewError",
    "PersonaPreviewMetricsPort",
    "PersonaPreviewPort",
    "PersonaPreviewResult",
    "PersonaPreviewTrace",
]
