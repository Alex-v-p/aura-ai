# ruff: noqa: E501

"""Core public application API for immutable generated text artifacts."""

from __future__ import annotations

import hashlib
import html
import json
import re
import secrets
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from time import monotonic
from typing import Protocol, cast
from uuid import UUID, uuid4, uuid5

from aura_core.domains.artifacts.generated.ports import (
    GeneratedArtifactCleanupUnknown,
    GeneratedArtifactObjectStore,
    GeneratedArtifactObjectStoreError,
    GeneratedArtifactRepository,
)

MAX_ARTIFACT_BYTES = 10_000_000
MAX_PREVIEW_CHARS = 262_144
MAX_TITLE_LENGTH = 300
MAX_FILENAME_LENGTH = 180
ARTIFACT_ID_NAMESPACE = UUID("3c6d24c6-8707-4e31-a403-2bd9d8f6ed7f")
OWNER_RE = re.compile(r"^[^\x00\n\r]{1,1024}$")
COMPONENT_RE = re.compile(r"^aura\.[a-z0-9_.]+@[1-9][0-9]*$")
MEDIA_TYPES = frozenset({
    "text/markdown; charset=utf-8",
    "text/plain; charset=utf-8",
    "application/json",
})
KINDS = frozenset({"research_report", "research_source_manifest", "generated_text", "generated_data"})


class GeneratedArtifactLifecycle(StrEnum):
    ACTIVE = "active"
    DELETION_PENDING = "deletion_pending"
    DELETION_FAILED = "deletion_failed"
    CLEANUP_UNKNOWN = "cleanup_unknown"
    DELETED = "deleted"


class GeneratedArtifactError(RuntimeError):
    pass


class GeneratedArtifactValidationError(GeneratedArtifactError, ValueError):
    pass


class GeneratedArtifactNotFound(GeneratedArtifactError):
    pass


class GeneratedArtifactConflict(GeneratedArtifactError):
    pass


class GeneratedArtifactStorageError(GeneratedArtifactError):
    pass


class GeneratedArtifactCleanupError(GeneratedArtifactError):
    pass


def _valid_trace_id(value: str) -> bool:
    return len(value) == 32 and all(char in "0123456789abcdef" for char in value)


def _valid_span_id(value: str) -> bool:
    return len(value) == 16 and all(char in "0123456789abcdef" for char in value)


@dataclass(frozen=True, slots=True)
class GeneratedArtifactTraceContext:
    """Admitted request lineage forwarded to one artifact operation."""

    trace_id: str
    parent_span_id: str | None = None
    link_span_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not _valid_trace_id(self.trace_id):
            raise ValueError("invalid artifact trace context")
        if self.parent_span_id is not None and not _valid_span_id(self.parent_span_id):
            raise ValueError("invalid artifact trace context")
        if len(self.link_span_ids) > 4 or any(not _valid_span_id(link) for link in self.link_span_ids):
            raise ValueError("invalid artifact trace context")


@dataclass(frozen=True, slots=True)
class GeneratedArtifactLineage:
    task_id: UUID | None = None
    run_id: UUID | None = None
    conversation_id: UUID | None = None
    workspace_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class GeneratedArtifactStoredRevision:
    """Internal revision carrying the opaque Garage object reference.

    This type never crosses a transport or module boundary.  Keeping the
    storage key here prevents accidental disclosure through public DTOs.
    """
    artifact_id: UUID
    revision: int
    media_type: str
    sha256: str
    byte_count: int
    object_key: str
    creator_component: str
    previous_revision: int | None
    created_at: datetime

    def metadata(self) -> dict[str, object]:
        return {
            "revision": self.revision,
            "mediaType": self.media_type,
            "sha256": self.sha256,
            "byteCount": self.byte_count,
            "creatorComponent": self.creator_component,
            "previousRevision": self.previous_revision,
            "createdAt": self.created_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class GeneratedArtifactRevision:
    """Public revision metadata; storage references are intentionally absent."""

    artifact_id: UUID
    revision: int
    media_type: str
    sha256: str
    byte_count: int
    creator_component: str
    previous_revision: int | None
    created_at: datetime

    def metadata(self) -> dict[str, object]:
        return {
            "revision": self.revision,
            "mediaType": self.media_type,
            "sha256": self.sha256,
            "byteCount": self.byte_count,
            "creatorComponent": self.creator_component,
            "previousRevision": self.previous_revision,
            "createdAt": self.created_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class GeneratedArtifactIntegrity:
    revision: int
    sha256: str
    byte_count: int
    created_at: datetime


@dataclass(frozen=True, slots=True)
class GeneratedArtifactCleanupWork:
    artifact_id: UUID
    operation: str
    expected_version: int
    status: str
    fingerprint: str
    idempotency_key: str
    claim_token: str | None = None
    lease_expires_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class GeneratedArtifact:
    id: UUID
    issuer: str
    subject: str
    kind: str
    title: str | None
    lifecycle: GeneratedArtifactLifecycle
    current_revision: int | None
    version: int
    created_at: datetime
    updated_at: datetime
    lineage: GeneratedArtifactLineage = GeneratedArtifactLineage()
    revision: GeneratedArtifactRevision | None = None
    integrity: GeneratedArtifactIntegrity | None = None
    deletion_audit_id: UUID | None = None
    failure_code: str | None = None

    @property
    def owner(self) -> tuple[str, str]:
        return self.issuer, self.subject

    @property
    def available(self) -> bool:
        return self.lifecycle is GeneratedArtifactLifecycle.ACTIVE


@dataclass(frozen=True, slots=True)
class GeneratedArtifactLifecycleEvent:
    artifact_id: UUID
    artifact_version: int
    lifecycle: GeneratedArtifactLifecycle
    current_revision: int | None
    occurred_at: datetime
    event_type: str
    correlation_id: UUID
    causation_id: UUID

    @property
    def event_id(self) -> UUID:
        return uuid5(
            ARTIFACT_ID_NAMESPACE,
            f"event:{self.artifact_id}:{self.artifact_version}:{self.event_type}",
        )

    def payload(self) -> dict[str, object]:
        return {
            "schemaVersion": 1,
            "eventId": str(self.event_id),
            "eventType": self.event_type,
            "artifactId": str(self.artifact_id),
            "artifactVersion": self.artifact_version,
            "lifecycle": self.lifecycle.value,
            "currentRevision": self.current_revision,
            "occurredAt": self.occurred_at.isoformat(),
            "correlationId": str(self.correlation_id),
            "causationId": str(self.causation_id),
        }


class GeneratedArtifactMetrics(Protocol):
    def increment(self, component: str, name: str, **attributes: str) -> None: ...

    def observe(self, component: str, name: str, value: float, **attributes: str) -> None: ...

    def record_span(self, component: str, operation: str, duration_ms: float, **attributes: object) -> None: ...


def _owner(issuer: str, subject: str) -> tuple[str, str]:
    if not OWNER_RE.fullmatch(issuer) or not OWNER_RE.fullmatch(subject):
        raise GeneratedArtifactValidationError("invalid owner identity")
    return issuer, subject


def _media_type(value: str) -> str:
    if value not in MEDIA_TYPES:
        raise GeneratedArtifactValidationError("unsupported generated artifact media type")
    return value


def _kind(value: str) -> str:
    if value not in KINDS:
        raise GeneratedArtifactValidationError("unsupported generated artifact kind")
    return value


def _title(value: str) -> str:
    if not value.strip() or len(value) > MAX_TITLE_LENGTH:
        raise GeneratedArtifactValidationError("invalid generated artifact title")
    if any(ord(char) < 32 and char not in "\t" for char in value):
        raise GeneratedArtifactValidationError("invalid generated artifact title")
    return value.strip()


def canonicalize_content(content: bytes | str | object, media_type: str) -> bytes:
    """Validate and canonicalize only the three system-generated types."""

    _media_type(media_type)
    if media_type == "application/json":
        def reject_constant(value: str) -> object:
            raise GeneratedArtifactValidationError(f"invalid JSON constant: {value}")

        def reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
            result: dict[str, object] = {}
            for key, value in pairs:
                if key in result:
                    raise GeneratedArtifactValidationError("duplicate JSON object key")
                result[key] = value
            return result

        if isinstance(content, bytes):
            try:
                value = json.loads(
                    content.decode("utf-8"),
                    parse_constant=reject_constant,
                    object_pairs_hook=reject_duplicate_pairs,
                )
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise GeneratedArtifactValidationError("invalid UTF-8 JSON") from exc
        elif isinstance(content, str):
            try:
                value = json.loads(
                    content,
                    parse_constant=reject_constant,
                    object_pairs_hook=reject_duplicate_pairs,
                )
            except json.JSONDecodeError as exc:
                raise GeneratedArtifactValidationError("invalid JSON") from exc
        else:
            value = content
        try:
            result = json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError, UnicodeEncodeError) as exc:
            raise GeneratedArtifactValidationError("JSON is not canonicalizable") from exc
        try:
            encoded = result.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise GeneratedArtifactValidationError("JSON is not valid UTF-8") from exc
    else:
        if not isinstance(content, (bytes, str)):
            raise GeneratedArtifactValidationError("text content is required")
        if isinstance(content, bytes):
            try:
                encoded = content.decode("utf-8").encode("utf-8")
            except UnicodeDecodeError as exc:
                raise GeneratedArtifactValidationError("text must be UTF-8") from exc
        else:
            try:
                encoded = content.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise GeneratedArtifactValidationError("text must be UTF-8") from exc
    if not encoded or len(encoded) > MAX_ARTIFACT_BYTES:
        raise GeneratedArtifactValidationError("artifact size exceeds the bounded limit")
    return encoded


def sanitize_preview(content: bytes, media_type: str) -> tuple[str, bool]:
    encoded = canonicalize_content(content, media_type)
    text = encoded.decode("utf-8")
    if media_type == "application/json":
        # Canonical JSON is already inert; avoid making a structured viewer
        # executable by returning bounded text only.
        safe = text
    elif media_type == "text/markdown; charset=utf-8":
        for _ in range(3):
            decoded = html.unescape(text)
            if decoded == text:
                break
            text = decoded
        safe = re.sub(r"(?is)<!--.*?-->", "", text)
        safe = re.sub(r"(?im)^\s{0,3}\[[^\]]+\]:\s*\S+.*$", "", safe)
        for _ in range(8):
            updated = re.sub(r"!\[([^\]]*)\]\s*(?:\([^)]*\)|\[[^]]*\])", r"\1", safe)
            updated = re.sub(r"\[([^\]]+)\]\s*(?:\([^)]*\)|\[[^]]*\])", r"\1", updated)
            if updated == safe:
                break
            safe = updated
        safe = re.sub(r"<\s*(?:https?|data|file|javascript):[^>]*>", "", safe, flags=re.IGNORECASE)
        safe = re.sub(r"<[^>]*>", "", safe)
        safe = re.sub(r"(?i)(?:javascript|data|file|https?):\s*[^\s)<>]+", "", safe)
        safe = html.escape(safe, quote=False)
    else:
        safe = text.replace("\x00", "")
    truncated = len(safe) > MAX_PREVIEW_CHARS
    return safe[:MAX_PREVIEW_CHARS], truncated


def safe_filename(title: str | None, media_type: str, revision: int) -> str:
    base = title or "artifact"
    base = re.sub(r"[^A-Za-z0-9._ -]+", "", base).strip(" .")[:MAX_FILENAME_LENGTH] or "artifact"
    suffix = ".md" if media_type.startswith("text/markdown") else ".txt" if media_type.startswith("text/") else ".json"
    if not base.lower().endswith(suffix):
        base += suffix
    return f"{base}-r{revision}"


def _command_fingerprint(
    *,
    artifact_id: UUID,
    owner: tuple[str, str],
    kind: str,
    title: str | None,
    media_type: str,
    creator_component: str,
    lineage: GeneratedArtifactLineage,
    expected_version: int,
    digest: str,
    byte_count: int,
) -> str:
    payload = {
        "artifactId": str(artifact_id),
        "owner": [owner[0], owner[1]],
        "kind": kind,
        "title": title,
        "mediaType": media_type,
        "creatorComponent": creator_component,
        "lineage": {
            "taskId": str(lineage.task_id) if lineage.task_id else None,
            "runId": str(lineage.run_id) if lineage.run_id else None,
            "conversationId": str(lineage.conversation_id) if lineage.conversation_id else None,
            "workspaceId": str(lineage.workspace_id) if lineage.workspace_id else None,
        },
        "version": expected_version,
        "sha256": digest,
        "byteCount": byte_count,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


class GeneratedArtifactService:
    """Owner-authorized reads and internal system-generated write use cases."""

    component = "aura.artifacts.persistence@1"
    deletion_component = "aura.artifacts.deletion@1"

    def __init__(
        self,
        repository: GeneratedArtifactRepository,
        object_store: GeneratedArtifactObjectStore,
        *,
        metrics: GeneratedArtifactMetrics | None = None,
        clock: Callable[[], datetime] | None = None,
        lifecycle_sink: Callable[[GeneratedArtifactLifecycleEvent], object] | None = None,
    ) -> None:
        self.repository = repository
        self.object_store = object_store
        self.metrics = metrics
        self.clock = clock or (lambda: datetime.now(UTC))
        self.lifecycle_sink = lifecycle_sink
        self.lifecycle_events: list[GeneratedArtifactLifecycleEvent] = []

    def _metric(
        self,
        action: str,
        outcome: str,
        elapsed_ms: float,
        *,
        deletion: bool = False,
        error_class: str | None = None,
        dependency: str = "core_postgresql",
        trace_id: str | None = None,
        parent_span_id: str | None = None,
        link_span_ids: tuple[str, ...] = (),
        operation: str | None = None,
    ) -> None:
        component = self.deletion_component if deletion else self.component
        metric = "artifact_deletion_outcome_count" if deletion else "artifact_write_outcome_count"
        duration = "artifact_deletion_latency_ms" if deletion else "artifact_write_latency_ms"
        if self.metrics is not None:
            try:
                attributes = {
                    "operation": action,
                    "outcome": outcome,
                    "dependency": dependency,
                }
                if error_class is not None:
                    attributes["error_class"] = error_class
                self.metrics.increment(component, metric, **attributes)
                self.metrics.observe(component, duration, elapsed_ms, **attributes)
                self.metrics.record_span(
                    component, action, elapsed_ms,
                    # The metadata sink accepts fixed-width identifiers.  A
                    # single operation's metric and span share this context;
                    # callers that already have a trace can override it via
                    # the explicit context arguments added at this boundary.
                    # A caller that has not admitted lineage gets the
                    # platform's explicit zero trace sentinel, never a
                    # disconnected random root.  Admitted operations always
                    # provide their validated trace context above.
                    trace_id=trace_id or "0" * 32,
                    span_id=secrets.token_hex(8),
                    parent_span_id=parent_span_id,
                    dependency=dependency, outcome=outcome,
                    error_class=error_class,
                    link_span_ids=link_span_ids,
                )
            except Exception:
                pass

    async def _emit(self, event: GeneratedArtifactLifecycleEvent) -> None:
        self.lifecycle_events.append(event)
        if self.lifecycle_sink is not None:
            result = self.lifecycle_sink(event)
            if hasattr(result, "__await__"):
                await result  # type: ignore[misc]

    async def create_generated_artifact(
        self,
        issuer: str,
        subject: str,
        *,
        kind: str,
        title: str,
        media_type: str,
        content: bytes | str | object,
        creator_component: str,
        lineage: GeneratedArtifactLineage | None = None,
        artifact_id: UUID | None = None,
        idempotency_key: str | None = None,
        trace_context: GeneratedArtifactTraceContext | None = None,
    ) -> GeneratedArtifact:
        started = monotonic()
        result: GeneratedArtifact | None = None
        error_class: str | None = None
        dependency = "garage_port"
        phase = "persistence"
        try:
            owner = _owner(issuer, subject)
            _kind(kind)
            _title(title)
            if not COMPONENT_RE.fullmatch(creator_component):
                raise GeneratedArtifactValidationError("invalid creator component")
            encoded = canonicalize_content(content, media_type)
        except GeneratedArtifactValidationError:
            self._metric("artifact.revision_write", "rejected", (monotonic() - started) * 1000, operation="validation", error_class="validation_failed", trace_id=trace_context.trace_id if trace_context else None, parent_span_id=trace_context.parent_span_id if trace_context else None, link_span_ids=trace_context.link_span_ids if trace_context else ())
            raise
        aid = artifact_id or (
            uuid5(ARTIFACT_ID_NAMESPACE, f"create:{issuer}:{subject}:{idempotency_key}")
            if idempotency_key
            else uuid4()
        )
        now = self.clock().astimezone(UTC)
        digest = hashlib.sha256(encoded).hexdigest()
        stored_revision = GeneratedArtifactStoredRevision(aid, 1, media_type, digest, len(encoded), f"artifact/{aid}/1/{digest}", creator_component, None, now)
        revision = GeneratedArtifactRevision(aid, 1, media_type, digest, len(encoded), creator_component, None, now)
        artifact = GeneratedArtifact(aid, owner[0], owner[1], kind, _title(title), GeneratedArtifactLifecycle.ACTIVE, 1, 1, now, now, lineage or GeneratedArtifactLineage(), revision=revision)
        command_fingerprint = _command_fingerprint(
            artifact_id=aid, owner=owner, kind=kind, title=artifact.title,
            media_type=media_type, creator_component=creator_component,
            lineage=artifact.lineage, expected_version=1, digest=digest,
            byte_count=len(encoded),
        )
        try:
            admitted = await self.repository.admit_create(artifact, stored_revision, idempotency_key=idempotency_key, idempotency_fingerprint=command_fingerprint)
            if admitted is not None:
                result = cast(GeneratedArtifact, admitted)
                phase = "replay"
                return result
            await self._publish_object(stored_revision, encoded)
            publish = getattr(self.repository, "publish_create", None)
            stored = await publish(artifact, stored_revision, idempotency_key=idempotency_key, idempotency_fingerprint=command_fingerprint) if publish is not None else await self.repository.create(artifact, stored_revision, idempotency_key=idempotency_key, idempotency_fingerprint=command_fingerprint)
            result = cast(GeneratedArtifact, stored)
            if result.id == artifact.id and result.version == artifact.version:
                try:
                    await self._emit(self._event(result, "artifact.created"))
                except Exception:
                    result = None
                    error_class = "delivery_failed"
                    dependency = "transactional_outbox"
                    raise
            return result
        except GeneratedArtifactConflict:
            error_class = "conflict"
            dependency = "core_postgresql"
            raise
        except GeneratedArtifactStorageError:
            if error_class is None:
                error_class = "storage_unavailable"
            dependency = "garage_port"
            raise
        except GeneratedArtifactObjectStoreError as exc:
            error_class = "storage_unavailable"
            dependency = "garage_port"
            raise GeneratedArtifactStorageError("artifact storage unavailable") from exc
        except Exception:
            if error_class is None:
                error_class = "settlement_unknown"
            dependency = "core_postgresql"
            raise
        finally:
            self._metric(
                "artifact.revision_write", "success" if result is not None else "failed",
                (monotonic() - started) * 1000, dependency=dependency,
                trace_id=trace_context.trace_id if trace_context else None,
                parent_span_id=trace_context.parent_span_id if trace_context else None,
                link_span_ids=trace_context.link_span_ids if trace_context else (),
                operation=phase, error_class=error_class,
            )

    async def append_revision(
        self,
        issuer: str,
        subject: str,
        artifact_id: UUID,
        *,
        expected_version: int,
        media_type: str,
        content: bytes | str | object,
        creator_component: str,
        idempotency_key: str | None = None,
        trace_context: GeneratedArtifactTraceContext | None = None,
    ) -> GeneratedArtifact:
        started = monotonic()
        try:
            owner = _owner(issuer, subject)
            if expected_version < 1:
                raise GeneratedArtifactValidationError("expected version is required")
            if not COMPONENT_RE.fullmatch(creator_component):
                raise GeneratedArtifactValidationError("invalid creator component")
            encoded = canonicalize_content(content, media_type)
        except GeneratedArtifactValidationError:
            self._metric("artifact.revision_write", "rejected", (monotonic() - started) * 1000, operation="validation", error_class="validation_failed", trace_id=trace_context.trace_id if trace_context else None, parent_span_id=trace_context.parent_span_id if trace_context else None, link_span_ids=trace_context.link_span_ids if trace_context else ())
            raise
        try:
            current = cast(GeneratedArtifact | None, await self.repository.get(owner, artifact_id))
        except TimeoutError:
            self._metric("artifact.revision_write", "timeout", (monotonic() - started) * 1000, operation="persistence", error_class="timeout", dependency="core_postgresql", trace_id=trace_context.trace_id if trace_context else None, parent_span_id=trace_context.parent_span_id if trace_context else None, link_span_ids=trace_context.link_span_ids if trace_context else ())
            raise
        except Exception:
            self._metric("artifact.revision_write", "failed", (monotonic() - started) * 1000, operation="persistence", error_class="settlement_unknown", dependency="core_postgresql", trace_id=trace_context.trace_id if trace_context else None, parent_span_id=trace_context.parent_span_id if trace_context else None, link_span_ids=trace_context.link_span_ids if trace_context else ())
            raise
        if current is None:
            self._metric("artifact.revision_write", "rejected", (monotonic() - started) * 1000, operation="persistence", error_class="conflict", dependency="core_postgresql", trace_id=trace_context.trace_id if trace_context else None, parent_span_id=trace_context.parent_span_id if trace_context else None, link_span_ids=trace_context.link_span_ids if trace_context else ())
            raise GeneratedArtifactNotFound("artifact not found")
        if not current.available or current.current_revision is None:
            self._metric("artifact.revision_write", "rejected", (monotonic() - started) * 1000, operation="persistence", error_class="conflict", dependency="core_postgresql", trace_id=trace_context.trace_id if trace_context else None, parent_span_id=trace_context.parent_span_id if trace_context else None, link_span_ids=trace_context.link_span_ids if trace_context else ())
            raise GeneratedArtifactConflict("artifact version is not appendable")
        now = self.clock().astimezone(UTC)
        next_revision = current.current_revision + 1
        digest = hashlib.sha256(encoded).hexdigest()
        stored_revision = GeneratedArtifactStoredRevision(artifact_id, next_revision, media_type, digest, len(encoded), f"artifact/{artifact_id}/{next_revision}/{digest}", creator_component, current.current_revision, now)
        command_fingerprint = _command_fingerprint(
            artifact_id=artifact_id, owner=owner, kind=current.kind, title=current.title,
            media_type=media_type, creator_component=creator_component,
            lineage=current.lineage, expected_version=expected_version, digest=digest,
            byte_count=len(encoded),
        )
        if current.version != expected_version:
            if idempotency_key is not None:
                replay = await self.repository.replay_append(
                    owner, artifact_id, idempotency_key=idempotency_key,
                    idempotency_fingerprint=command_fingerprint,
                )
                if replay is not None:
                    self._metric("artifact.revision_write", "success", (monotonic() - started) * 1000, operation="replay", dependency="core_postgresql", trace_id=trace_context.trace_id if trace_context else None, parent_span_id=trace_context.parent_span_id if trace_context else None, link_span_ids=trace_context.link_span_ids if trace_context else ())
                    return cast(GeneratedArtifact, replay)
            self._metric("artifact.revision_write", "rejected", (monotonic() - started) * 1000, operation="persistence", error_class="conflict", dependency="core_postgresql", trace_id=trace_context.trace_id if trace_context else None, parent_span_id=trace_context.parent_span_id if trace_context else None, link_span_ids=trace_context.link_span_ids if trace_context else ())
            raise GeneratedArtifactConflict("artifact version is not appendable")
        result: GeneratedArtifact | None = None
        error_class: str | None = None
        dependency = "garage_port"
        phase = "persistence"
        try:
            admitted = await self.repository.admit_append(artifact_id, stored_revision, owner=owner, expected_version=expected_version, idempotency_key=idempotency_key, idempotency_fingerprint=command_fingerprint)
            if admitted is not None:
                result = cast(GeneratedArtifact, admitted)
                phase = "replay"
                return result
            await self._publish_object(stored_revision, encoded)
            publish = getattr(self.repository, "publish_append", None)
            result = cast(GeneratedArtifact, await publish(artifact_id, stored_revision, owner=owner, expected_version=expected_version, idempotency_key=idempotency_key, idempotency_fingerprint=command_fingerprint) if publish is not None else await self.repository.append_revision(artifact_id, stored_revision, owner=owner, expected_version=expected_version, idempotency_key=idempotency_key, idempotency_fingerprint=command_fingerprint))
            try:
                await self._emit(self._event(result, "artifact.revision_published"))
            except Exception:
                result = None
                error_class = "delivery_failed"
                dependency = "transactional_outbox"
                raise
            return result
        except GeneratedArtifactConflict:
            error_class = "conflict"
            dependency = "core_postgresql"
            raise
        except GeneratedArtifactStorageError:
            if error_class is None:
                error_class = "storage_unavailable"
            dependency = "garage_port"
            raise
        except GeneratedArtifactObjectStoreError as exc:
            error_class = "storage_unavailable"
            dependency = "garage_port"
            raise GeneratedArtifactStorageError("artifact storage unavailable") from exc
        except Exception:
            if error_class is None:
                error_class = "settlement_unknown"
            dependency = "core_postgresql"
            raise
        finally:
            self._metric(
                "artifact.revision_write", "success" if result else "failed",
                (monotonic() - started) * 1000, dependency=dependency,
                trace_id=trace_context.trace_id if trace_context else None,
                parent_span_id=trace_context.parent_span_id if trace_context else None,
                link_span_ids=trace_context.link_span_ids if trace_context else (),
                operation=phase, error_class=error_class,
            )

    async def get(self, issuer: str, subject: str, artifact_id: UUID) -> GeneratedArtifact:
        item = cast(GeneratedArtifact | None, await self.repository.get(_owner(issuer, subject), artifact_id))
        if item is None:
            raise GeneratedArtifactNotFound("artifact not found")
        return item

    async def list(self, issuer: str, subject: str, *, limit: int = 25, media_type: str | None = None, offset: int = 0) -> list[GeneratedArtifact]:
        if not 1 <= limit <= 100:
            raise GeneratedArtifactValidationError("invalid artifact page limit")
        if media_type is not None:
            _media_type(media_type)
        return [cast(GeneratedArtifact, item) for item in await self.repository.list(_owner(issuer, subject), limit=limit, offset=offset, media_type=media_type)]

    async def revisions(self, issuer: str, subject: str, artifact_id: UUID) -> list[GeneratedArtifactRevision]:
        await self.get(issuer, subject, artifact_id)
        return [
            GeneratedArtifactRevision(
                item.artifact_id, item.revision, item.media_type, item.sha256,
                item.byte_count, item.creator_component, item.previous_revision,
                item.created_at,
            )
            for item in await self.repository.revisions(_owner(issuer, subject), artifact_id)
        ]

    async def preview(self, issuer: str, subject: str, artifact_id: UUID, revision: int | None = None) -> tuple[GeneratedArtifact, str, bool]:
        item, _, text, truncated = await self.preview_with_revision(issuer, subject, artifact_id, revision)
        return item, text, truncated

    async def preview_with_revision(self, issuer: str, subject: str, artifact_id: UUID, revision: int | None = None) -> tuple[GeneratedArtifact, GeneratedArtifactRevision, str, bool]:
        item = await self.get(issuer, subject, artifact_id)
        if not item.available:
            raise GeneratedArtifactConflict("artifact content is unavailable")
        row = cast(GeneratedArtifactStoredRevision | None, await self.repository.get_revision(_owner(issuer, subject), artifact_id, revision))
        if row is None:
            raise GeneratedArtifactNotFound("artifact revision not found")
        content = await self._read(row.object_key)
        if hashlib.sha256(content).hexdigest() != row.sha256 or len(content) != row.byte_count:
            raise GeneratedArtifactStorageError("artifact integrity verification failed")
        text, truncated = sanitize_preview(content, row.media_type)
        return item, GeneratedArtifactRevision(row.artifact_id, row.revision, row.media_type, row.sha256, row.byte_count, row.creator_component, row.previous_revision, row.created_at), text, truncated

    async def content(self, issuer: str, subject: str, artifact_id: UUID, revision: int | None = None) -> tuple[GeneratedArtifact, GeneratedArtifactRevision, bytes]:
        item = await self.get(issuer, subject, artifact_id)
        if not item.available:
            raise GeneratedArtifactConflict("artifact content is unavailable")
        row = cast(GeneratedArtifactStoredRevision | None, await self.repository.get_revision(_owner(issuer, subject), artifact_id, revision))
        if row is None:
            raise GeneratedArtifactNotFound("artifact revision not found")
        content = await self._read(row.object_key)
        if hashlib.sha256(content).hexdigest() != row.sha256 or len(content) != row.byte_count:
            raise GeneratedArtifactStorageError("artifact integrity verification failed")
        return item, GeneratedArtifactRevision(row.artifact_id, row.revision, row.media_type, row.sha256, row.byte_count, row.creator_component, row.previous_revision, row.created_at), content

    @staticmethod
    def _cleanup_fingerprint(owner: tuple[str, str], artifact_id: UUID, expected_version: int, operation: str) -> str:
        return hashlib.sha256(json.dumps({
            "artifactId": str(artifact_id), "owner": list(owner),
            "operation": operation, "expectedVersion": expected_version,
        }, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()

    async def admit_delete(self, issuer: str, subject: str, artifact_id: UUID, *, expected_version: int, idempotency_key: str, trace_context: GeneratedArtifactTraceContext | None = None) -> GeneratedArtifact:
        """Admit deletion and return; object-store work is executed by the worker."""
        started = monotonic()
        result: GeneratedArtifact | None = None
        error_class: str | None = None
        outcome = "rejected"
        try:
            owner = _owner(issuer, subject)
            fingerprint = self._cleanup_fingerprint(owner, artifact_id, expected_version, "delete")
            result = cast(GeneratedArtifact, await self.repository.begin_delete(
                owner, artifact_id, expected_version=expected_version,
                idempotency_key=idempotency_key, idempotency_fingerprint=fingerprint,
            ))
            return result
        except TimeoutError:
            outcome = "timeout"
            error_class = "timeout"
            raise
        except GeneratedArtifactConflict:
            error_class = "conflict"
            raise
        except GeneratedArtifactValidationError:
            error_class = "validation_failed"
            raise
        except Exception:
            outcome = "unknown"
            error_class = "settlement_unknown"
            raise
        finally:
            self._metric("artifact.delete", "success" if result is not None else outcome, (monotonic() - started) * 1000, deletion=True, dependency="core_postgresql", error_class=error_class, trace_id=trace_context.trace_id if trace_context else None, parent_span_id=trace_context.parent_span_id if trace_context else None, link_span_ids=trace_context.link_span_ids if trace_context else (), operation="admission")

    async def admit_retry_cleanup(self, issuer: str, subject: str, artifact_id: UUID, *, expected_version: int, idempotency_key: str, trace_context: GeneratedArtifactTraceContext | None = None) -> GeneratedArtifact:
        started = monotonic()
        result: GeneratedArtifact | None = None
        error_class: str | None = None
        outcome = "rejected"
        try:
            owner = _owner(issuer, subject)
            fingerprint = self._cleanup_fingerprint(owner, artifact_id, expected_version, "retry_cleanup")
            result = cast(GeneratedArtifact, await self.repository.admit_cleanup(
                owner, artifact_id, expected_version=expected_version,
                operation="retry_cleanup", idempotency_key=idempotency_key,
                idempotency_fingerprint=fingerprint,
            ))
            return result
        except TimeoutError:
            outcome = "timeout"
            error_class = "timeout"
            raise
        except GeneratedArtifactConflict:
            error_class = "conflict"
            raise
        except GeneratedArtifactValidationError:
            error_class = "validation_failed"
            raise
        except Exception:
            outcome = "unknown"
            error_class = "settlement_unknown"
            raise
        finally:
            self._metric("artifact.delete", "success" if result is not None else outcome, (monotonic() - started) * 1000, deletion=True, dependency="core_postgresql", error_class=error_class, trace_id=trace_context.trace_id if trace_context else None, parent_span_id=trace_context.parent_span_id if trace_context else None, link_span_ids=trace_context.link_span_ids if trace_context else (), operation="admission")

    async def admit_reconcile_cleanup(self, issuer: str, subject: str, artifact_id: UUID, *, expected_version: int, idempotency_key: str, trace_context: GeneratedArtifactTraceContext | None = None) -> GeneratedArtifact:
        started = monotonic()
        result: GeneratedArtifact | None = None
        error_class: str | None = None
        outcome = "rejected"
        try:
            owner = _owner(issuer, subject)
            fingerprint = self._cleanup_fingerprint(owner, artifact_id, expected_version, "reconcile_cleanup")
            result = cast(GeneratedArtifact, await self.repository.admit_cleanup(
                owner, artifact_id, expected_version=expected_version,
                operation="reconcile_cleanup", idempotency_key=idempotency_key,
                idempotency_fingerprint=fingerprint,
            ))
            return result
        except TimeoutError:
            outcome = "timeout"
            error_class = "timeout"
            raise
        except GeneratedArtifactConflict:
            error_class = "conflict"
            raise
        except GeneratedArtifactValidationError:
            error_class = "validation_failed"
            raise
        except Exception:
            outcome = "unknown"
            error_class = "settlement_unknown"
            raise
        finally:
            self._metric("artifact.delete", "success" if result is not None else outcome, (monotonic() - started) * 1000, deletion=True, dependency="core_postgresql", error_class=error_class, trace_id=trace_context.trace_id if trace_context else None, parent_span_id=trace_context.parent_span_id if trace_context else None, link_span_ids=trace_context.link_span_ids if trace_context else (), operation="admission")

    async def resume_pending_cleanup(self, issuer: str, subject: str, *, limit: int = 20, trace_context: GeneratedArtifactTraceContext | None = None) -> list[GeneratedArtifact]:
        """Claim durable cleanup admissions and execute them after a restart."""
        started = monotonic()
        results: list[GeneratedArtifact] = []
        error_class: str | None = None
        outcome = "success"
        try:
            owner = _owner(issuer, subject)
            works = await self.repository.claim_pending_cleanup(owner, limit=limit)
            for work in works:
                if work.claim_token is None:
                    raise GeneratedArtifactConflict("cleanup claim is missing a token")
                results.append(await self._settle_cleanup(
                    issuer, subject, work.artifact_id, expected_version=work.expected_version,
                    idempotency_key=work.idempotency_key, operation=work.operation,
                    idempotency_fingerprint=work.fingerprint,
                    claim_token=work.claim_token, trace_context=trace_context,
                ))
            return results
        except TimeoutError:
            outcome = "timeout"
            error_class = "timeout"
            raise
        except GeneratedArtifactConflict:
            outcome = "rejected"
            error_class = "conflict"
            raise
        except Exception:
            outcome = "unknown"
            error_class = "settlement_unknown"
            raise
        finally:
            self._metric("artifact.delete", outcome if error_class is not None else "success", (monotonic() - started) * 1000, deletion=True, dependency="core_postgresql", error_class=error_class, trace_id=trace_context.trace_id if trace_context else None, parent_span_id=trace_context.parent_span_id if trace_context else None, link_span_ids=trace_context.link_span_ids if trace_context else (), operation="claim")

    async def _settle_cleanup(
        self,
        issuer: str,
        subject: str,
        artifact_id: UUID,
        *,
        expected_version: int,
        idempotency_key: str,
        operation: str,
        idempotency_fingerprint: str | None = None,
        claim_token: str,
        trace_context: GeneratedArtifactTraceContext | None = None,
    ) -> GeneratedArtifact:
        started = monotonic()
        outcome = "failed"
        error_class: str | None = None
        dependency = "garage_port"
        owner = _owner(issuer, subject)

        async def settle_state(lifecycle: str, failure_code: str | None) -> GeneratedArtifact:
            """Settle via the repository; the outer boundary owns telemetry."""

            return cast(
                GeneratedArtifact,
                await self.repository.set_cleanup_state(
                    owner,
                    artifact_id,
                    expected_version=expected_version,
                    lifecycle=lifecycle,
                    failure_code=failure_code,
                    idempotency_key=idempotency_key,
                    operation=operation,
                    idempotency_fingerprint=idempotency_fingerprint
                    or f"{operation}:{expected_version}",
                    claim_token=claim_token,
                ),
            )
        item: GeneratedArtifact | None = None
        try:
            # Fence the worker before any replay or provider access.  A
            # terminal receipt never bypasses claim ownership or lease expiry
            # checks; the provider fences below are repeated immediately
            # before every read/delete because claims may be replaced while a
            # worker is running.
            await self.repository.renew_cleanup_claim(
                owner, artifact_id, operation=operation, claim_token=claim_token,
            )
            item = await self.get(issuer, subject, artifact_id)
            if item.lifecycle is GeneratedArtifactLifecycle.DELETED:
                outcome = "success"
                return item
            revisions = [cast(GeneratedArtifactStoredRevision, row) for row in await self.repository.revisions(owner, artifact_id)]
            for row in revisions:
                try:
                    if operation == "reconcile_cleanup":
                        try:
                            await self.repository.renew_cleanup_claim(
                                owner, artifact_id, operation=operation,
                                claim_token=claim_token,
                            )
                            existing = await self._read(row.object_key)
                        except GeneratedArtifactNotFound:
                            continue
                        if hashlib.sha256(existing).hexdigest() != row.sha256 or len(existing) != row.byte_count:
                            raise GeneratedArtifactStorageError("artifact integrity verification failed")
                        # Reconciliation is deliberately read-only.  A present
                        # object means the provider state is not settled; only
                        # an explicitly admitted retry may issue destructive I/O.
                        raise GeneratedArtifactStorageError("cleanup object still exists")
                    await self.repository.renew_cleanup_claim(
                        owner, artifact_id, operation=operation,
                        claim_token=claim_token,
                    )
                    await self.object_store.delete(row.object_key)
                except FileNotFoundError:
                    continue
            outcome = "success"
            item = await settle_state(GeneratedArtifactLifecycle.DELETED.value, None)
        except GeneratedArtifactCleanupUnknown:
            outcome = "unknown"
            error_class = "cleanup_unknown"
            item = await settle_state(GeneratedArtifactLifecycle.CLEANUP_UNKNOWN.value, "cleanup_unknown")
        except TimeoutError:
            outcome = "failed"
            # A provider timeout leaves a bounded deletion failure admission;
            # the cleanup worker must not claim a terminal lifecycle success.
            outcome = "timeout"
            error_class = "timeout"
            item = await settle_state(GeneratedArtifactLifecycle.DELETION_FAILED.value, "deletion_failed")
        except (GeneratedArtifactStorageError, GeneratedArtifactObjectStoreError, OSError):
            outcome = "failed"
            error_class = "storage_unavailable"
            item = await settle_state(GeneratedArtifactLifecycle.DELETION_FAILED.value, "deletion_failed")
        except GeneratedArtifactConflict:
            error_class = "conflict"
            dependency = "core_postgresql"
            self._metric(
                "artifact.delete", "rejected", (monotonic() - started) * 1000,
                deletion=True, error_class=error_class, dependency=dependency,
                trace_id=trace_context.trace_id if trace_context else None,
                parent_span_id=trace_context.parent_span_id if trace_context else None,
                link_span_ids=trace_context.link_span_ids if trace_context else (),
                operation="reconciliation" if operation == "reconcile_cleanup" else "settlement",
            )
            raise
        except GeneratedArtifactNotFound:
            error_class = "conflict"
            dependency = "core_postgresql"
            self._metric(
                "artifact.delete", "rejected", (monotonic() - started) * 1000,
                deletion=True, error_class=error_class, dependency=dependency,
                trace_id=trace_context.trace_id if trace_context else None,
                parent_span_id=trace_context.parent_span_id if trace_context else None,
                link_span_ids=trace_context.link_span_ids if trace_context else (),
                operation="reconciliation" if operation == "reconcile_cleanup" else "settlement",
            )
            raise
        except Exception:
            outcome = "unknown"
            error_class = "settlement_unknown"
            dependency = "core_postgresql"
            self._metric(
                "artifact.delete", outcome, (monotonic() - started) * 1000,
                deletion=True, error_class=error_class, dependency=dependency,
                trace_id=trace_context.trace_id if trace_context else None,
                parent_span_id=trace_context.parent_span_id if trace_context else None,
                link_span_ids=trace_context.link_span_ids if trace_context else (),
                operation="reconciliation" if operation == "reconcile_cleanup" else "settlement",
            )
            raise
        try:
            resolved = item
            await self._emit(self._event(resolved, "artifact.deletion_changed"))
            return resolved
        except Exception:
            # Lifecycle delivery is a terminal failure even when the SQL
            # mutation committed; the transactional outbox remains the source
            # of truth for recovery and the invocation must not be reported as
            # a successful delivery.
            outcome = "failed"
            error_class = "delivery_failed"
            raise
        finally:
            self._metric(
                "artifact.delete",
                outcome,
                (monotonic() - started) * 1000,
                deletion=True,
                error_class=error_class,
                dependency="transactional_outbox" if error_class == "delivery_failed" else dependency,
                trace_id=trace_context.trace_id if trace_context else None,
                parent_span_id=trace_context.parent_span_id if trace_context else None,
                link_span_ids=trace_context.link_span_ids if trace_context else (),
                operation="reconciliation" if operation == "reconcile_cleanup" else "settlement",
            )

    async def _read(self, key: str) -> bytes:
        chunks: list[bytes] = []
        total = 0
        try:
            async for chunk in self.object_store.get(key):
                total += len(chunk)
                if total > MAX_ARTIFACT_BYTES:
                    raise GeneratedArtifactStorageError("stored artifact exceeds safety limit")
                chunks.append(chunk)
        except FileNotFoundError as exc:
            raise GeneratedArtifactNotFound("artifact object not found") from exc
        except GeneratedArtifactObjectStoreError:
            raise
        return b"".join(chunks)

    async def _publish_object(self, revision: GeneratedArtifactStoredRevision, encoded: bytes) -> None:
        """Publish immutably and reconcile a provider timeout by verifying a read."""
        put_if_absent = getattr(self.object_store, "put_if_absent", None)
        try:
            if put_if_absent is not None:
                await put_if_absent(
                    revision.object_key,
                    _one(encoded),
                    content_type=revision.media_type,
                    sha256=revision.sha256,
                    byte_count=revision.byte_count,
                )
            else:
                await self.object_store.put_stream(revision.object_key, _one(encoded), content_type=revision.media_type)
        except (GeneratedArtifactObjectStoreError, TimeoutError, OSError, ValueError) as exc:
            try:
                existing = await self._read(revision.object_key)
            except (GeneratedArtifactError, FileNotFoundError) as read_exc:
                raise GeneratedArtifactStorageError("artifact storage unavailable") from read_exc
            if hashlib.sha256(existing).hexdigest() != revision.sha256 or len(existing) != revision.byte_count:
                raise GeneratedArtifactStorageError("artifact integrity verification failed") from exc
            return
        try:
            existing = await self._read(revision.object_key)
        except GeneratedArtifactError as exc:
            raise GeneratedArtifactStorageError("artifact storage unavailable") from exc
        if hashlib.sha256(existing).hexdigest() != revision.sha256 or len(existing) != revision.byte_count:
            raise GeneratedArtifactStorageError("artifact integrity verification failed")

    @staticmethod
    def _event(item: GeneratedArtifact, event_type: str) -> GeneratedArtifactLifecycleEvent:
        correlation = uuid5(ARTIFACT_ID_NAMESPACE, f"correlation:{item.id}")
        return GeneratedArtifactLifecycleEvent(item.id, item.version, item.lifecycle, item.current_revision, item.updated_at, event_type, correlation, uuid5(ARTIFACT_ID_NAMESPACE, f"causation:{item.id}:{item.version}"))


async def _one(value: bytes) -> AsyncIterator[bytes]:
    yield value
