"""Runner-specific artifact adapter.

Only opaque UUID references cross the evaluation transport.  Object locators
are derived in this adapter and are never accepted from a request, result,
log, or metric.  The adapter is deliberately separate from the owner document
and attachment stores so the runner credential can be scoped independently.
"""

# Provider implementations intentionally expose only the narrow object-store
# protocol; concrete adapters are selected by bootstrap.
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownMemberType=false, reportArgumentType=false, reportIndexIssue=false, reportGeneralTypeIssues=false, reportReturnType=false

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterable, AsyncIterator
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from aura_core.platform.evaluation_transport import TransportValidationError

MAX_ARTIFACT_BYTES = 1_073_741_824
ALLOWED_MEDIA_TYPES = frozenset(
    {
        "application/json",
        "application/octet-stream",
        "text/plain",
        "text/markdown",
        "image/png",
        "image/jpeg",
        "audio/wav",
    }
)
ALLOWED_INPUT_CLASSES = frozenset({"evaluation_input", "evaluation_context", "evaluation_evidence"})
ALLOWED_OUTPUT_CLASSES = frozenset(
    {"evaluation_output", "evaluation_evidence", "evaluation_report"}
)


class EvaluationArtifactError(RuntimeError):
    """Content-free artifact provider error."""


class EvaluationObjectStore(Protocol):
    def get(self, key: str) -> AsyncIterator[bytes]: ...

    async def put_stream(
        self, key: str, stream: AsyncIterable[bytes], *, content_type: str
    ) -> int: ...

    async def delete(self, key: str) -> None: ...


@dataclass(frozen=True, slots=True)
class EvaluationArtifact:
    reference_id: UUID
    media_classification: str
    media_type: str
    byte_count: int
    digest: str
    capture_classification: str


def _key(reference_id: UUID) -> str:
    # A UUID is the only caller-controlled locator component.  The prefix is
    # fixed and contains no dataset, case, filename, or owner information.
    return f"runner/{reference_id.hex}"


def _verify_reference(reference: dict[str, object], *, output: bool) -> EvaluationArtifact:
    required = {
        "artifactReferenceId",
        "mediaClassification",
        "mediaType",
        "byteCount",
        "integrity",
        "captureAuthorization",
    }
    if set(reference) != required:
        raise TransportValidationError("artifact reference is not closed")
    try:
        reference_id = UUID(str(reference["artifactReferenceId"]))
    except (TypeError, ValueError) as exc:
        raise TransportValidationError("artifact reference id is invalid") from exc
    classes = ALLOWED_OUTPUT_CLASSES if output else ALLOWED_INPUT_CLASSES
    media_classification = reference["mediaClassification"]
    media_type = reference["mediaType"]
    if media_classification not in classes or media_type not in ALLOWED_MEDIA_TYPES:
        raise TransportValidationError("artifact media declaration is invalid")
    byte_count = reference["byteCount"]
    if type(byte_count) is not int or not 0 <= byte_count <= MAX_ARTIFACT_BYTES:
        raise TransportValidationError("artifact byte bound is invalid")
    integrity = reference["integrity"]
    if (
        not isinstance(integrity, dict)
        or set(integrity) != {"algorithm", "digest"}
        or integrity["algorithm"] != "sha256"
    ):
        raise TransportValidationError("artifact integrity declaration is invalid")
    digest = integrity["digest"]
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(c not in "0123456789abcdef" for c in digest)
    ):
        raise TransportValidationError("artifact digest is invalid")
    capture = reference["captureAuthorization"]
    if not isinstance(capture, dict):
        raise TransportValidationError("artifact capture authorization is invalid")
    classification = capture.get("captureClassification")
    if classification in {"metadata_only", "hashed"}:
        raise TransportValidationError("content artifact requires explicit authorization")
    return EvaluationArtifact(
        reference_id,
        str(media_classification),
        str(media_type),
        byte_count,
        digest,
        str(classification),
    )


class EvaluationArtifactAdapter:
    def __init__(
        self, store: EvaluationObjectStore, *, max_bytes: int = MAX_ARTIFACT_BYTES
    ) -> None:
        if not 1 <= max_bytes <= MAX_ARTIFACT_BYTES:
            raise ValueError("artifact limit is invalid")
        self._store = store
        self._max_bytes = max_bytes

    async def read(self, reference: dict[str, object], *, max_bytes: int | None = None) -> bytes:
        artifact = _verify_reference(reference, output=False)
        read_limit = self._max_bytes if max_bytes is None else min(self._max_bytes, max_bytes)
        if artifact.byte_count > read_limit:
            raise EvaluationArtifactError("artifact exceeds runner limit")
        chunks: list[bytes] = []
        total = 0
        digest = hashlib.sha256()
        try:
            stream = self._store.get(_key(artifact.reference_id))
            async for chunk in stream:
                if type(chunk) is not bytes or not chunk:
                    raise EvaluationArtifactError("artifact provider returned an invalid chunk")
                total += len(chunk)
                if total > read_limit or total > artifact.byte_count:
                    raise EvaluationArtifactError("artifact exceeds declared byte bound")
                digest.update(chunk)
                chunks.append(chunk)
        except EvaluationArtifactError:
            raise
        except Exception as exc:
            raise EvaluationArtifactError("artifact read failed") from exc
        if total != artifact.byte_count or digest.hexdigest() != artifact.digest:
            raise EvaluationArtifactError("artifact integrity verification failed")
        return b"".join(chunks)

    async def write(self, reference: dict[str, object], content: bytes) -> EvaluationArtifact:
        artifact = _verify_reference(reference, output=True)
        if len(content) > self._max_bytes or len(content) != artifact.byte_count:
            raise EvaluationArtifactError("artifact output exceeds declared byte bound")
        digest = hashlib.sha256(content).hexdigest()
        if digest != artifact.digest:
            raise EvaluationArtifactError("artifact output integrity verification failed")

        async def stream() -> AsyncIterator[bytes]:
            if content:
                yield content

        try:
            await self._store.put_stream(
                _key(artifact.reference_id), stream(), content_type=artifact.media_type
            )
        except Exception as exc:
            raise EvaluationArtifactError("artifact write failed") from exc
        return artifact

    async def delete(self, reference: dict[str, object]) -> None:
        artifact = _verify_reference(reference, output=True)
        try:
            await self._store.delete(_key(artifact.reference_id))
        except Exception as exc:
            raise EvaluationArtifactError("artifact deletion failed") from exc


__all__ = [
    "EvaluationArtifact",
    "EvaluationArtifactAdapter",
    "EvaluationArtifactError",
    "EvaluationObjectStore",
]
