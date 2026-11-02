"""Governed, bounded Core run-evidence capture.

This module deliberately owns no Observatory client.  It produces the closed
``run-evidence-v1`` envelope and hands it to a bounded local queue.  A
background publisher may forward queued envelopes to the dedicated evidence
subject, but execution never waits for that publisher.
"""
# pyright: reportUnknownArgumentType=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportAttributeAccessIssue=false, reportGeneralTypeIssues=false, reportPossiblyUnboundVariable=false, reportUnnecessaryComparison=false

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import re
import secrets
import struct
import zlib
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from time import perf_counter
from typing import Any, Protocol
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from aura_core.domains.execution.runs.ports import MetricsPort, NullMetrics, RunEvidenceItem

RUN_EVIDENCE_SUBJECT = "aura.runs.evidence.v1"
RUN_EVIDENCE_SCHEMA_VERSION = 1
RUN_EVIDENCE_CONTRACT_ID = "run-evidence-v1"
RUN_EVIDENCE_COMPONENT = "aura.platform.run_evidence_export"
MAX_EVIDENCE_BYTES = 1_048_576
MAX_QUEUE_CAPACITY = 4096
MAX_RETRY_ATTEMPTS = 5
MAX_DRAIN_SECONDS = 30.0
MAX_SAFE_INTEGER = 9_007_199_254_740_991

_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_SECRET_KEY = re.compile(
    r"(?:password|passwd|secret|token|credential|credentials|api[_-]?key|bearer|"
    r"private[_-]?key|authorization|cookie|session)",
    re.IGNORECASE,
)
_SECRET_VALUE = re.compile(
    r"(?:bearer\s+[A-Za-z0-9._~+/=-]{8,}|(?:sk|pk|ghp|xox[baprs])[-_][A-Za-z0-9_-]{12,}|"
    r"AKIA[0-9A-Z]{16}|"
    r"(?:aws_access_key_id|aws_secret_access_key)\s*[:=]\s*[^\s,;]+|"
    r"(?:password|passwd|secret|token|credential|credentials|api[_-]?key|"
    r"private[_-]?key|session(?:[_-]?id)?|authorization|cookie)\s*"
    r"[\"']?\s*[:=]\s*[\"']?(?:basic|bearer)?[^\s,;\"']+|"
    r"\bbasic\s+[A-Za-z0-9+/=]{8,}|"
    r"-----BEGIN[^\r\n-]*(?:PRIVATE KEY|CERTIFICATE)-----)",
    re.IGNORECASE,
)
_MEDIA_TYPES = frozenset(
    {
        "application/json",
        "text/plain",
        "text/markdown",
        "image/png",
        "image/jpeg",
        "image/webp",
        "application/pdf",
    }
)
_PAYLOAD_KEYS: dict[str, frozenset[str]] = {
    "admitted_request": frozenset({"kind", "request"}),
    "admitted_trigger": frozenset({"kind", "triggerType", "content"}),
    "compiled_prompt": frozenset({"kind", "prompt"}),
    "conversation_context": frozenset({"kind", "messages"}),
    "memory_chunk": frozenset({"kind", "chunk"}),
    "document_chunk": frozenset({"kind", "chunk"}),
    "model_visible_attachment": frozenset({"kind", "attachmentId", "displayName", "contentBase64"}),
    "model_routing": frozenset({"kind", "provider", "model", "routeReason"}),
    "displayed_thinking": frozenset({"kind", "text"}),
    "generation_output": frozenset({"kind", "text"}),
    "response_synthesis": frozenset({"kind", "text"}),
    "tool_arguments": frozenset({"kind", "toolName", "content"}),
    "tool_result": frozenset({"kind", "toolName", "content"}),
    "policy_decision": frozenset({"kind", "decision"}),
    "approval_decision": frozenset({"kind", "decision"}),
    "execution_outcome": frozenset({"kind", "outcome", "detail"}),
    "persistence_record": frozenset({"kind", "record"}),
    "delivery_record": frozenset({"kind", "record"}),
    "worker_input": frozenset({"kind", "content"}),
    "worker_output": frozenset({"kind", "content"}),
    "automation_input": frozenset({"kind", "content"}),
    "automation_output": frozenset({"kind", "content"}),
    "schedule_input": frozenset({"kind", "content"}),
    "schedule_output": frozenset({"kind", "content"}),
    "evaluation_input": frozenset({"kind", "content"}),
    "evaluation_output": frozenset({"kind", "content"}),
}
_VARIANTS: dict[str, tuple[str, frozenset[str], frozenset[str]]] = {
    "admitted_request": ("admission", frozenset({"structured"}), frozenset({"application/json"})),
    "admitted_trigger": ("admission", frozenset({"structured"}), frozenset({"application/json"})),
    "compiled_prompt": ("prompt", frozenset({"text"}), frozenset({"text/plain", "text/markdown"})),
    "conversation_context": ("context", frozenset({"structured"}), frozenset({"application/json"})),
    "memory_chunk": ("retrieval", frozenset({"structured"}), frozenset({"application/json"})),
    "document_chunk": ("retrieval", frozenset({"structured"}), frozenset({"application/json"})),
    "model_visible_attachment": (
        "context",
        frozenset({"text", "structured", "image", "document"}),
        frozenset(_MEDIA_TYPES),
    ),
    "model_routing": ("routing", frozenset({"structured"}), frozenset({"application/json"})),
    "displayed_thinking": ("thinking", frozenset({"text"}), frozenset({"text/plain"})),
    "generation_output": (
        "generation",
        frozenset({"text"}),
        frozenset({"text/plain", "text/markdown"}),
    ),
    "response_synthesis": (
        "synthesis",
        frozenset({"text"}),
        frozenset({"text/plain", "text/markdown"}),
    ),
    "tool_arguments": ("tool", frozenset({"structured"}), frozenset({"application/json"})),
    "tool_result": ("tool", frozenset({"structured"}), frozenset({"application/json"})),
    "policy_decision": ("policy", frozenset({"structured"}), frozenset({"application/json"})),
    "approval_decision": ("policy", frozenset({"structured"}), frozenset({"application/json"})),
    "execution_outcome": ("tool", frozenset({"structured"}), frozenset({"application/json"})),
    "persistence_record": (
        "persistence",
        frozenset({"structured"}),
        frozenset({"application/json"}),
    ),
    "delivery_record": ("delivery", frozenset({"structured"}), frozenset({"application/json"})),
    "worker_input": ("worker", frozenset({"structured"}), frozenset({"application/json"})),
    "worker_output": ("worker", frozenset({"structured"}), frozenset({"application/json"})),
    "automation_input": ("automation", frozenset({"structured"}), frozenset({"application/json"})),
    "automation_output": ("automation", frozenset({"structured"}), frozenset({"application/json"})),
    "schedule_input": ("schedule", frozenset({"structured"}), frozenset({"application/json"})),
    "schedule_output": ("schedule", frozenset({"structured"}), frozenset({"application/json"})),
    "evaluation_input": ("evaluation", frozenset({"structured"}), frozenset({"application/json"})),
    "evaluation_output": ("evaluation", frozenset({"structured"}), frozenset({"application/json"})),
}


class EvidenceCaptureState(StrEnum):
    CAPTURED = "captured"
    EXCLUDED = "excluded"
    PARTIAL = "partial"
    DROPPED = "dropped"


@dataclass(frozen=True, slots=True)
class RunEvidencePolicy:
    """Owner-controlled capture policy with bounded exporter settings."""

    enabled: bool = False
    revision: str = "full-content-v1"
    queue_capacity: int = 256
    max_attempts: int = 3
    drain_seconds: float = 5.0
    max_bytes: int = MAX_EVIDENCE_BYTES

    def __post_init__(self) -> None:
        if self.revision != "full-content-v1" and not _ID_PATTERN.fullmatch(self.revision):
            raise ValueError("capture policy revision must be a bounded identifier")
        if _contains_secret(self.revision):
            raise ValueError("capture policy revision contains prohibited material")
        if not 1 <= self.queue_capacity <= MAX_QUEUE_CAPACITY:
            raise ValueError("run evidence queue capacity is outside its safe bound")
        if not 1 <= self.max_attempts <= MAX_RETRY_ATTEMPTS:
            raise ValueError("run evidence attempts are outside their safe bound")
        if not 0.1 <= self.drain_seconds <= MAX_DRAIN_SECONDS:
            raise ValueError("run evidence drain timeout is outside its safe bound")
        if not 1 <= self.max_bytes <= MAX_EVIDENCE_BYTES:
            raise ValueError("run evidence payload limit is outside its safe bound")


RunEvidenceInput = RunEvidenceItem


class RunEvidenceRecorder(Protocol):
    """Core's synchronous, bounded, nonblocking evidence port."""

    @property
    def enabled(self) -> bool: ...

    def record(self, item: RunEvidenceInput) -> bool: ...

    def allocate_sequence(self, run_id: UUID) -> int: ...

    async def start(self) -> None: ...

    async def stop(self) -> None: ...


class _EvidencePublisher(Protocol):
    async def publish(self, payload: bytes, *, message_id: UUID) -> None: ...

    async def close(self) -> None: ...


class DisabledRunEvidenceRecorder:
    """Inert default: no private content is serialized or retained."""

    enabled = False

    def record(self, item: RunEvidenceInput) -> bool:
        del item
        return False

    def allocate_sequence(self, run_id: UUID) -> int:
        del run_id
        return 0

    async def start(self) -> None:
        return

    async def stop(self) -> None:
        return


def _utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _canonical_json(value: object) -> bytes:
    """Encode JSON using the RFC 8785/JCS primitive rules.

    The project intentionally keeps this dependency-free because the producer
    runs in the Core process.  The closed payloads contain JSON primitives, so
    implementing the small JCS surface here is safer than accepting a runtime
    serializer whose exponent and Unicode ordering differ between providers.
    """

    def number(item: int | float) -> str:
        if isinstance(item, int):
            return str(item)
        if not (item == item and item not in (float("inf"), float("-inf"))):
            raise ValueError("non-finite JSON number")
        if item == 0:
            return "0"
        raw = repr(item).lower()
        sign = ""
        if raw.startswith("-"):
            sign, raw = "-", raw[1:]
        coefficient, _, exponent = raw.partition("e")
        power = int(exponent) if exponent else 0
        digits = coefficient.replace(".", "")
        decimal_position = (
            coefficient.find(".") if "." in coefficient else len(coefficient)
        ) + power
        absolute = abs(item)
        if 1e-6 <= absolute < 1e21:
            if decimal_position <= 0:
                result = "0." + "0" * (-decimal_position) + digits
            elif decimal_position >= len(digits):
                result = digits + "0" * (decimal_position - len(digits))
            else:
                result = digits[:decimal_position] + "." + digits[decimal_position:]
            if "." in result:
                result = result.rstrip("0").rstrip(".")
            return sign + result
        mantissa = coefficient.rstrip("0").rstrip(".")
        exponent_text = f"{power:+d}" if exponent else "+0"
        if exponent:
            exponent_text = f"{power:+d}"
        return sign + mantissa + "e" + exponent_text

    def encode(item: object) -> str:
        if item is None:
            return "null"
        if item is True:
            return "true"
        if item is False:
            return "false"
        if isinstance(item, (int, float)) and not isinstance(item, bool):
            return number(item)
        if isinstance(item, str):
            return json.dumps(item, ensure_ascii=False, separators=(",", ":"))
        if isinstance(item, Mapping):
            # RFC 8785 orders property names by their UTF-16 code units.
            keys = sorted((str(key) for key in item), key=lambda key: key.encode("utf-16-be"))
            return "{" + ",".join(
                json.dumps(key, ensure_ascii=False) + ":" + encode(item[key]) for key in keys
            ) + "}"
        if isinstance(item, Sequence) and not isinstance(item, (bytes, bytearray)):
            return "[" + ",".join(encode(entry) for entry in item) + "]"
        raise TypeError(f"unsupported JSON value: {type(item).__name__}")

    return encode(value).encode("utf-8")


def _contains_secret(
    value: object, *, key: str | None = None, _depth: int = 0
) -> bool:
    """Detect credential material without retaining or returning its content.

    Tool arguments/results commonly carry JSON as a string because the closed
    evidence shape preserves the provider's model-visible representation. A
    bounded recursive parse makes those strings subject to the same key/value
    checks as native mappings while keeping the scanner deterministic and
    resistant to deeply nested input.
    """

    if key is not None and _SECRET_KEY.search(key):
        return True
    if isinstance(value, str):
        if _SECRET_VALUE.search(value):
            return True
        if len(value) > MAX_EVIDENCE_BYTES:
            return False
        stripped = value.lstrip()
        if not stripped.startswith(("{", "[")):
            return False
        # Do not treat an uninspected nested JSON string as safe. This keeps
        # bounded scanning fail-closed without retaining the parsed content.
        if _depth >= 3:
            return True
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return False
        if isinstance(parsed, (Mapping, list, tuple)):
            return _contains_secret(parsed, _depth=_depth + 1)
        return False
    if isinstance(value, Mapping):
        return any(
            _contains_secret(item, key=str(name), _depth=_depth + 1)
            for name, item in value.items()
        )
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return any(_contains_secret(item, _depth=_depth + 1) for item in value)
    return False


def _contains_secret_bytes(value: bytes) -> bool:
    """Scan decoded text and bounded media metadata/streams for secrets."""

    if _SECRET_VALUE.search(value.decode("utf-8", errors="ignore")):
        return True
    # PNG ancillary chunks and common PDF object streams may be compressed.
    # Decompressing only bounded chunks keeps inspection bounded and avoids
    # claiming an exact capture for an item whose full bytes were not scanned.
    cursor = 8 if value.startswith(b"\x89PNG\r\n\x1a\n") else 0
    while cursor + 12 <= len(value) and cursor < MAX_EVIDENCE_BYTES:
        try:
            length = struct.unpack(">I", value[cursor : cursor + 4])[0]
        except struct.error:
            break
        end = cursor + 12 + length
        if end > len(value) or length > MAX_EVIDENCE_BYTES:
            break
        chunk = value[cursor + 8 : cursor + 8 + length]
        if value[cursor + 4 : cursor + 8] in {b"zTXt", b"iTXt"}:
            try:
                decoded = zlib.decompress(chunk)
            except zlib.error:
                decoded = b""
            if _SECRET_VALUE.search(decoded.decode("utf-8", errors="ignore")):
                return True
        cursor = end
        if value[cursor - 8 : cursor - 4] == b"IEND":
            break
    return False


def _valid_id(value: str, *, length: int) -> bool:
    return len(value) == length and all(c in "0123456789abcdef" for c in value)


def _text(value: object, *, limit: int) -> bool:
    return isinstance(value, str) and len(value) <= limit


def _nonempty_text(value: object, *, limit: int) -> bool:
    return isinstance(value, str) and 0 < len(value) <= limit


def _bounded_id(value: object) -> bool:
    return isinstance(value, str) and bool(_ID_PATTERN.fullmatch(value))


def _validate_payload_shape(kind: str, payload: Mapping[str, object]) -> None:
    """Validate the closed nested payload shapes before canonicalization."""

    def exact_map(value: object, keys: set[str]) -> Mapping[str, object] | None:
        if not isinstance(value, Mapping) or set(value) != keys:
            return None
        return value

    if kind == "admitted_request":
        request = exact_map(payload.get("request"), {"role", "content"})
        if (
            request is None
            or request.get("role") not in {"system", "developer", "user", "assistant", "tool"}
            or not _text(request.get("content"), limit=1_048_576)
        ):
            raise ValueError("invalid admitted request payload")
    elif kind == "admitted_trigger":
        if not _bounded_id(payload.get("triggerType")) or not _text(
            payload.get("content"), limit=1_048_576
        ):
            raise ValueError("invalid admitted trigger payload")
    elif kind == "compiled_prompt":
        if not _text(payload.get("prompt"), limit=1_048_576):
            raise ValueError("invalid compiled prompt payload")
    elif kind == "conversation_context":
        messages = payload.get("messages")
        if not isinstance(messages, list) or not 1 <= len(messages) <= 256:
            raise ValueError("invalid conversation context payload")
        for message in messages:
            value = exact_map(message, {"role", "content"})
            if (
                value is None
                or value.get("role") not in {"system", "developer", "user", "assistant", "tool"}
                or not _text(value.get("content"), limit=1_048_576)
            ):
                raise ValueError("invalid conversation context message")
    elif kind in {"memory_chunk", "document_chunk"}:
        chunk = exact_map(payload.get("chunk"), {"sourceId", "ordinal", "content", "score"})
        score = chunk.get("score") if chunk is not None else None
        ordinal = chunk.get("ordinal") if chunk is not None else None
        score_valid = score is None
        if isinstance(score, (int, float)) and not isinstance(score, bool):
            score_valid = -1_000_000 <= float(score) <= 1_000_000
        if (
            chunk is None
            or not _bounded_id(chunk.get("sourceId"))
            or type(ordinal) is not int
            or not 0 <= ordinal <= 1_000_000
            or not _text(chunk.get("content"), limit=1_048_576)
            or not score_valid
        ):
            raise ValueError("invalid chunk payload")
    elif kind == "model_visible_attachment":
        encoded = payload.get("contentBase64")
        if (
            not _bounded_id(payload.get("attachmentId"))
            or not _nonempty_text(payload.get("displayName"), limit=256)
            or not isinstance(encoded, str)
            or len(encoded) > 1_398_104
        ):
            raise ValueError("invalid model attachment payload")
    elif kind == "model_routing":
        if (
            not _bounded_id(payload.get("provider"))
            or not _bounded_id(payload.get("model"))
            or not _text(payload.get("routeReason"), limit=4096)
        ):
            raise ValueError("invalid model routing payload")
    elif kind in {"displayed_thinking", "generation_output", "response_synthesis"}:
        if not _text(payload.get("text"), limit=1_048_576):
            raise ValueError("invalid text evidence payload")
    elif kind in {"tool_arguments", "tool_result"}:
        if not _bounded_id(payload.get("toolName")) or not _text(
            payload.get("content"), limit=1_048_576
        ):
            raise ValueError("invalid tool evidence payload")
    elif kind in {"policy_decision", "approval_decision"}:
        decision = exact_map(payload.get("decision"), {"decision", "reason"})
        if (
            decision is None
            or not _bounded_id(decision.get("decision"))
            or not _text(decision.get("reason"), limit=4096)
        ):
            raise ValueError("invalid decision payload")
    elif kind == "execution_outcome":
        if payload.get("outcome") not in {
            "succeeded",
            "failed",
            "cancelled",
            "rejected",
            "timed_out",
        } or not _text(payload.get("detail"), limit=4096):
            raise ValueError("invalid execution outcome payload")
    elif kind in {"persistence_record", "delivery_record"}:
        record = exact_map(payload.get("record"), {"name", "content"})
        if (
            record is None
            or not _bounded_id(record.get("name"))
            or not _text(record.get("content"), limit=1_048_576)
        ):
            raise ValueError("invalid record payload")
    elif kind in {
        "worker_input", "worker_output", "automation_input", "automation_output",
        "schedule_input", "schedule_output", "evaluation_input", "evaluation_output",
    }:
        content = exact_map(payload.get("content"), {"name", "content"})
        if (
            content is None
            or not _bounded_id(content.get("name"))
            or not _text(content.get("content"), limit=1_048_576)
        ):
            raise ValueError("invalid boundary payload")


def _admission(
    media_classification: str,
    *,
    observed_at: datetime,
    outcome: str = "passed",
) -> dict[str, object]:
    modes = ["decoded_text", "container_structure"]
    return {
        "outcome": outcome,
        "scannerRevision": "core-evidence-scanner-v1",
        "policyRevision": "core-evidence-policy-v1",
        "inspectedAt": _utc(observed_at),
        "inspectionModes": modes,
    }


def _validate_admission(
    admission: Mapping[str, object],
    *,
    media_classification: str,
    captured: bool,
    attachment: bool = False,
) -> dict[str, object]:
    if _contains_secret(dict(admission)):
        raise EvidenceRejected("secret_bearing_field_rejected")
    outcome = admission.get("outcome")
    if not captured and outcome == "not_applicable":
        if admission != {"outcome": "not_applicable"}:
            raise ValueError("non-captured evidence must have a not-applicable admission")
        return {"outcome": "not_applicable"}
    if outcome not in {"passed", "rejected_prohibited_material"}:
        raise EvidenceRejected("unknown_capture_gap")
    scanner = admission.get("scannerRevision")
    revision = admission.get("policyRevision")
    inspected = admission.get("inspectedAt")
    modes = admission.get("inspectionModes")
    if (
        not isinstance(scanner, str)
        or not _ID_PATTERN.fullmatch(scanner)
        or not isinstance(revision, str)
        or not _ID_PATTERN.fullmatch(revision)
        or not isinstance(inspected, str)
        or not re.fullmatch(
            r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,9})?Z",
            inspected,
        )
        or not isinstance(modes, (list, tuple))
        or not modes
        or len(modes) > 4
        or len(set(modes)) != len(modes)
        or any(
            mode not in {"decoded_text", "metadata", "container_structure", "ocr"}
            for mode in modes
        )
    ):
        raise EvidenceRejected("unknown_capture_gap")
    required = {
        "text": {"decoded_text", "container_structure"} if attachment else {"decoded_text"},
        "structured": {"decoded_text", "container_structure"},
        "image": {"metadata", "container_structure", "ocr"},
        "document": {"decoded_text", "metadata", "container_structure", "ocr"},
    }[media_classification]
    if not required.issubset(set(modes)):
        raise EvidenceRejected("unknown_capture_gap")
    return {
        "outcome": outcome,
        "scannerRevision": scanner,
        "policyRevision": revision,
        "inspectedAt": inspected,
        "inspectionModes": list(modes),
    }


def serialize_evidence(item: RunEvidenceInput, *, policy: RunEvidencePolicy) -> bytes:
    """Serialize one closed item and reject it before queueing if unsafe."""

    capture_state = EvidenceCaptureState(item.capture_state)
    if not _valid_id(item.trace_id, length=32) or not _valid_id(item.span_id, length=16):
        raise ValueError("invalid trace correlation identifiers")
    if item.parent_span_id is not None and not _valid_id(item.parent_span_id, length=16):
        raise ValueError("invalid parent span identifier")
    if type(item.sequence) is not int or not 0 <= item.sequence <= MAX_SAFE_INTEGER:
        raise ValueError("evidence sequence must be a safe integer")
    if capture_state is EvidenceCaptureState.CAPTURED and item.reason is not None:
        raise ValueError("captured evidence cannot carry a rejection reason")
    if capture_state is not EvidenceCaptureState.CAPTURED:
        allowed_reasons = {
            EvidenceCaptureState.EXCLUDED: {
                "capture_disabled",
                "provider_hidden_reasoning_excluded",
            },
            EvidenceCaptureState.PARTIAL: {
                "credential_material_rejected",
                "secret_bearing_field_rejected",
                "bearer_material_rejected",
                "unsupported_media_type",
                "payload_limit_exceeded",
                "ingest_rejected",
                "unknown_capture_gap",
            },
            EvidenceCaptureState.DROPPED: {
                "queue_pressure",
                "export_unavailable",
                "transport_failure",
                "storage_failure",
            },
        }[capture_state]
        if item.reason not in allowed_reasons:
            raise EvidenceRejected("unknown_capture_gap")
    if item.payload is not None and _contains_secret(item.payload):
        raise EvidenceRejected("secret_bearing_field_rejected")
    if capture_state is EvidenceCaptureState.CAPTURED and item.payload is None:
        raise ValueError("captured evidence requires a payload")
    if capture_state is not EvidenceCaptureState.CAPTURED and item.payload is not None:
        raise ValueError("non-captured evidence cannot contain a payload")

    payload = dict(item.payload) if item.payload is not None else None
    if payload is not None and payload.get("kind") != item.evidence_kind:
        raise ValueError("payload kind does not match evidence kind")
    if payload is not None and item.evidence_kind not in _PAYLOAD_KEYS:
        raise EvidenceRejected("provider_hidden_reasoning_excluded")
    if payload is not None and set(payload) != set(_PAYLOAD_KEYS[item.evidence_kind]):
        raise ValueError("payload contains fields outside its closed evidence shape")
    if payload is not None:
        _validate_payload_shape(item.evidence_kind, payload)
    if item.evidence_kind not in _VARIANTS:
        raise EvidenceRejected("provider_hidden_reasoning_excluded")
    (
        expected_category,
        expected_classifications,
        expected_media_types,
    ) = _VARIANTS[item.evidence_kind]
    if (
        item.step_category != expected_category
        or item.media_classification not in expected_classifications
        or item.media_type not in expected_media_types
    ):
        raise ValueError("evidence kind variant does not match its closed schema")
    if item.media_type not in _MEDIA_TYPES:
        raise EvidenceRejected("unsupported_media_type")
    expected_media = {
        "text": {"text/plain", "text/markdown"},
        "structured": {"application/json"},
        "image": {"image/png", "image/jpeg", "image/webp"},
        "document": {"application/pdf"},
    }.get(item.media_classification)
    if expected_media is None or item.media_type not in expected_media:
        raise EvidenceRejected("unsupported_media_type")
    if (
        item.evidence_kind == "model_visible_attachment"
        and capture_state is EvidenceCaptureState.CAPTURED
        and item.content_admission is None
    ):
        raise EvidenceRejected("unknown_capture_gap")
    admission = _validate_admission(
        dict(
            item.content_admission
            or (
                {"outcome": "not_applicable"}
                if capture_state is not EvidenceCaptureState.CAPTURED
                else _admission(item.media_classification, observed_at=item.observed_at)
            )
        ),
        media_classification=item.media_classification,
        captured=capture_state is EvidenceCaptureState.CAPTURED,
        attachment=item.evidence_kind == "model_visible_attachment",
    )
    if payload is not None and item.evidence_kind == "model_visible_attachment":
        encoded = payload.get("contentBase64")
        if not isinstance(encoded, str):
            raise ValueError("attachment evidence requires base64 content")
        # Reject oversized encoded input before allocating decoded bytes.  The
        # base64 wrapper is at most four bytes for every three evidence bytes.
        if len(encoded) > ((policy.max_bytes + 2) // 3) * 4:
            raise EvidenceRejected("payload_limit_exceeded")
        try:
            attachment_bytes = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise EvidenceRejected("unsupported_media_type") from exc
        if not _valid_attachment_bytes(item.media_type, attachment_bytes):
            raise EvidenceRejected("unsupported_media_type")
        # Core has no authorized format parser/OCR implementation.  A caller
        # assertion cannot turn image/PDF bytes into exact admitted evidence.
        if item.media_classification in {"image", "document"}:
            raise EvidenceRejected("unknown_capture_gap")
        if _contains_secret_bytes(attachment_bytes):
            raise EvidenceRejected("secret_bearing_field_rejected")
    if payload is not None and item.evidence_kind == "model_visible_attachment":
        canonical = attachment_bytes
    elif payload is not None and item.media_classification == "text":
        raw = (
            payload.get("prompt")
            if item.evidence_kind == "compiled_prompt"
            else payload.get("text")
        )
        if not isinstance(raw, str):
            raise ValueError("text evidence requires its text field")
        canonical = raw.encode("utf-8")
    elif payload is not None:
        canonical = _canonical_json(payload)
    else:
        canonical = b""
    if len(canonical) > policy.max_bytes:
        raise EvidenceRejected("payload_limit_exceeded")
    digest = (
        "sha256:" + hashlib.sha256(canonical).hexdigest()
        if capture_state is EvidenceCaptureState.CAPTURED
        else None
    )
    if capture_state is EvidenceCaptureState.CAPTURED and admission.get("outcome") != "passed":
        raise EvidenceRejected("unknown_capture_gap")
    if capture_state is not EvidenceCaptureState.CAPTURED:
        admission_outcome = admission.get("outcome")
        if capture_state is EvidenceCaptureState.DROPPED and admission_outcome != "not_applicable":
            raise EvidenceRejected("unknown_capture_gap")
        if capture_state is EvidenceCaptureState.EXCLUDED and admission_outcome != "not_applicable":
            raise EvidenceRejected("unknown_capture_gap")
        if capture_state is EvidenceCaptureState.PARTIAL and admission_outcome not in {
            "not_applicable",
            "rejected_prohibited_material",
        }:
            raise EvidenceRejected("unknown_capture_gap")
    # Logical identity names the execution boundary, not the mutable bytes.
    # Reusing this identity with changed digest/admission is therefore a
    # consumer-visible collision instead of a second apparently-new item.
    logical = f"{item.run_id}:{item.sequence}:{item.step_category}:{item.evidence_kind}"
    event_id = uuid5(NAMESPACE_URL, "aura-run-evidence-event:" + logical)
    evidence_id = uuid5(NAMESPACE_URL, "aura-run-evidence-item:" + logical)
    envelope: dict[str, object] = {
        "schemaVersion": RUN_EVIDENCE_SCHEMA_VERSION,
        "contractId": RUN_EVIDENCE_CONTRACT_ID,
        "eventId": str(event_id),
        "evidenceId": str(evidence_id),
        "runId": str(item.run_id),
        "traceId": item.trace_id,
        "spanId": item.span_id,
        "parentSpanId": item.parent_span_id,
        "sequence": item.sequence,
        "stepCategory": item.step_category,
        "evidenceKind": item.evidence_kind,
        "mediaClassification": item.media_classification,
        "mediaType": item.media_type,
        "digest": digest,
        "byteCount": len(canonical),
        "capturePolicyRevision": policy.revision,
        "observedAt": _utc(item.observed_at),
        "emittedAt": _utc(datetime.now(UTC)),
        "delivery": {
            "kind": "initial",
            "producerId": "aura-core",
            "deliveryId": str(uuid4()),
            "attempt": 1,
        },
        "contentAdmission": admission,
        "captureState": capture_state.value,
        "reason": item.reason,
    }
    if payload is not None:
        envelope["payload"] = payload
    return _canonical_json(envelope)


def _valid_attachment_bytes(media_type: str, value: bytes) -> bool:
    if not value:
        return False
    if media_type == "image/png":
        return value.startswith(b"\x89PNG\r\n\x1a\n")
    if media_type == "image/jpeg":
        return value.startswith(b"\xff\xd8\xff")
    if media_type == "image/webp":
        return len(value) >= 12 and value[:4] == b"RIFF" and value[8:12] == b"WEBP"
    if media_type == "application/pdf":
        return value.startswith(b"%PDF-")
    if media_type in {"text/plain", "text/markdown", "application/json"}:
        try:
            decoded = value.decode("utf-8")
            if media_type == "application/json":
                parsed = json.loads(decoded)
                if _contains_secret(parsed):
                    return False
            elif _SECRET_VALUE.search(decoded):
                return False
            return True
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
            return False
    return False


class EvidenceRejected(ValueError):
    """Content-free serializer rejection classification."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class BoundedRunEvidenceRecorder:
    """Bounded producer queue and detached asynchronous JetStream exporter."""

    def __init__(
        self,
        policy: RunEvidencePolicy,
        publisher: _EvidencePublisher | None = None,
        metrics: MetricsPort | None = None,
    ) -> None:
        self.policy = policy
        self.publisher = publisher
        self.metrics = metrics or NullMetrics()
        self._queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=policy.queue_capacity)
        self._task: asyncio.Task[None] | None = None
        self._stopping = False
        self._last_sequence: OrderedDict[UUID, int] = OrderedDict()
        self._sequence_identities: OrderedDict[
            tuple[UUID, int], tuple[str, str, str]
        ] = OrderedDict()
        self._pending_payloads: OrderedDict[str, bytes] = OrderedDict()

    @property
    def enabled(self) -> bool:
        return self.policy.enabled

    def allocate_sequence(self, run_id: UUID) -> int:
        """Reserve one process-wide sequence for all Core composition roots."""

        current = self._last_sequence.get(run_id)
        sequence = 0 if current is None else current + 1
        self._last_sequence[run_id] = sequence
        self._last_sequence.move_to_end(run_id)
        while len(self._last_sequence) > 1024:
            self._last_sequence.popitem(last=False)
        return sequence

    @staticmethod
    def _telemetry_error(reason: str | None) -> str | None:
        if reason is None:
            return None
        if reason in {"queue_pressure"}:
            return "queue_pressure"
        if reason in {"transport_failure", "transport_unavailable", "export_unavailable"}:
            return "transport_unavailable"
        if reason == "shutdown":
            return "cancel"
        if reason in {
            "secret_bearing_field_rejected",
            "credential_material_rejected",
            "bearer_material_rejected",
            "unsupported_media_type",
            "payload_limit_exceeded",
            "unknown_capture_gap",
            "serialization_rejected",
        }:
            return "serialization_rejected"
        return "unknown"

    @property
    def queue_depth(self) -> int:
        return self._queue.qsize()

    def bind_publisher(self, publisher: _EvidencePublisher) -> None:
        """Bind transport after the composition root has connected NATS."""

        if self._task is not None:
            raise RuntimeError("run evidence publisher is already running")
        self.publisher = publisher

    def _metric(
        self,
        name: str,
        *,
        outcome: str,
        error_class: str | None = None,
        operation: str = "transport_publish",
        trace_id: str | None = None,
        run_id: str | None = None,
    ) -> None:
        attrs = {"operation": operation, "outcome": outcome}
        if error_class is not None:
            attrs["error_class"] = self._telemetry_error(error_class) or "unknown"
        if trace_id is not None:
            attrs["trace_id"] = trace_id
        if run_id is not None:
            attrs["run_id"] = run_id
        try:
            self.metrics.increment(
                RUN_EVIDENCE_COMPONENT,
                name,
                **attrs,
            )
        except Exception:
            return

    def _measurement(self, name: str, value: float, *, operation: str) -> None:
        try:
            self.metrics.observe(
                RUN_EVIDENCE_COMPONENT,
                name,
                value,
                operation=operation,
                outcome="accepted",
            )
        except Exception:
            return

    @staticmethod
    def _item_from_envelope(payload: bytes) -> RunEvidenceInput | None:
        """Rebuild only content-free lineage for a transport diagnosis."""

        try:
            envelope = json.loads(payload)
            return RunEvidenceItem(
                run_id=UUID(str(envelope["runId"])),
                trace_id=str(envelope["traceId"]),
                span_id=str(envelope["spanId"]),
                parent_span_id=(
                    str(envelope["parentSpanId"])
                    if envelope.get("parentSpanId") is not None
                    else None
                ),
                sequence=int(envelope["sequence"]),
                step_category=str(envelope["stepCategory"]),
                evidence_kind=str(envelope["evidenceKind"]),
                media_classification=str(envelope["mediaClassification"]),
                media_type=str(envelope["mediaType"]),
                payload=None,
                capture_state=str(envelope.get("captureState", "captured")),
            )
        except (TypeError, ValueError, KeyError, json.JSONDecodeError):
            return None

    def _span(
        self,
        operation: str,
        duration_ms: float,
        *,
        item: RunEvidenceInput | None = None,
        payload: bytes | None = None,
        outcome: str,
        error_class: str | None = None,
        metric_name: str,
    ) -> None:
        metrics_span = getattr(self.metrics, "record_span", None)
        if not callable(metrics_span):
            return
        trace_id = item.trace_id if item is not None else "0" * 32
        source_span_id = item.span_id if item is not None else "0" * 16
        run_id = str(item.run_id) if item is not None else None
        if payload is not None:
            try:
                envelope = json.loads(payload)
                trace_id = str(envelope["traceId"])
                source_span_id = str(envelope["spanId"])
                run_id = str(envelope["runId"])
            except (TypeError, ValueError, KeyError):
                pass
        try:
            metrics_span(
                RUN_EVIDENCE_COMPONENT,
                operation,
                duration_ms,
                trace_id=trace_id,
                span_id=secrets.token_hex(8),
                parent_span_id=source_span_id,
                dependency=(
                    "application_validation"
                    if operation != "run_evidence.transport_publish"
                    else "nats_jetstream"
                ),
                outcome=outcome,
                error_class=self._telemetry_error(error_class),
                run_id=run_id,
                metric_name=metric_name,
            )
        except Exception:
            return

    def _completeness_signal(self, item: RunEvidenceInput, reason: str, *, dropped: bool) -> None:
        """Best-effort content-free diagnosis for an item that could not export."""

        if item.evidence_kind not in _VARIANTS or self._stopping or not self.policy.enabled:
            return
        rejected = reason in {
            "credential_material_rejected",
            "secret_bearing_field_rejected",
            "bearer_material_rejected",
        }
        signal = RunEvidenceItem(
            run_id=item.run_id,
            trace_id=item.trace_id,
            span_id=item.span_id,
            parent_span_id=item.parent_span_id,
            sequence=item.sequence,
            step_category=item.step_category,
            evidence_kind=item.evidence_kind,
            media_classification=item.media_classification,
            media_type=item.media_type,
            payload=None,
            observed_at=item.observed_at,
            capture_state=(
                EvidenceCaptureState.DROPPED.value
                if dropped
                else EvidenceCaptureState.PARTIAL.value
            ),
            reason=reason,
            content_admission=(
                _admission(
                    item.media_classification,
                    observed_at=item.observed_at,
                    outcome="rejected_prohibited_material",
                )
                if rejected
                else None
            ),
        )
        try:
            encoded = serialize_evidence(signal, policy=self.policy)
            self._queue.put_nowait(encoded)
            try:
                event_id = str(json.loads(encoded)["eventId"])
                self._pending_payloads[event_id] = encoded
                self._pending_payloads.move_to_end(event_id)
                while len(self._pending_payloads) > self.policy.queue_capacity + 1:
                    self._pending_payloads.popitem(last=False)
            except (TypeError, ValueError, KeyError):
                pass
            self._measurement(
                "run_evidence_queue_depth",
                float(self._queue.qsize()),
                operation="queue_offer",
            )
        except (asyncio.QueueFull, EvidenceRejected, ValueError, TypeError):
            self._metric(
                "run_evidence_export_dropped_count",
                outcome="failed",
                error_class="queue_pressure",
                operation="queue_offer",
                trace_id=item.trace_id,
                run_id=str(item.run_id),
            )
            return

    def record(self, item: RunEvidenceInput) -> bool:
        if not self.policy.enabled or self._stopping:
            return False
        serialize_started = perf_counter()
        try:
            payload = serialize_evidence(item, policy=self.policy)
        except EvidenceRejected as exc:
            self._metric(
                "run_evidence_export_dropped_count",
                outcome="rejected",
                error_class=exc.reason,
                operation="serialize",
                trace_id=item.trace_id,
                run_id=str(item.run_id),
            )
            self._span(
                "run_evidence.serialize",
                (perf_counter() - serialize_started) * 1000,
                item=item,
                outcome="rejected",
                error_class=exc.reason,
                metric_name="run_evidence_serialize_latency_ms",
            )
            self._completeness_signal(item, exc.reason, dropped=False)
            return False
        except Exception:
            self._metric(
                "run_evidence_export_dropped_count",
                outcome="rejected",
                error_class="serialization_rejected",
                operation="serialize",
                trace_id=item.trace_id,
                run_id=str(item.run_id),
            )
            self._span(
                "run_evidence.serialize",
                (perf_counter() - serialize_started) * 1000,
                item=item,
                outcome="rejected",
                error_class="serialization_rejected",
                metric_name="run_evidence_serialize_latency_ms",
            )
            self._completeness_signal(item, "unknown_capture_gap", dropped=False)
            return False
        self._span(
            "run_evidence.serialize",
            (perf_counter() - serialize_started) * 1000,
            item=item,
            outcome="accepted",
            metric_name="run_evidence_serialize_latency_ms",
        )
        envelope = json.loads(payload)
        immutable = dict(envelope)
        immutable.pop("delivery", None)
        immutable.pop("emittedAt", None)
        identity = (
            str(envelope["eventId"]),
            str(envelope["evidenceId"]),
            hashlib.sha256(_canonical_json(immutable)).hexdigest(),
        )
        key = (item.run_id, item.sequence)
        previous = self._last_sequence.get(item.run_id)
        existing = self._sequence_identities.get(key)
        if existing is not None and existing != identity:
            self._metric(
                "run_evidence_export_dropped_count",
                outcome="rejected",
                error_class="serialization_rejected",
                operation="serialize",
                trace_id=item.trace_id,
                run_id=str(item.run_id),
            )
            self._completeness_signal(item, "unknown_capture_gap", dropped=False)
            return False
        if previous is not None and item.sequence < previous and existing is None:
            self._metric(
                "run_evidence_export_dropped_count",
                outcome="rejected",
                error_class="serialization_rejected",
                operation="serialize",
                trace_id=item.trace_id,
                run_id=str(item.run_id),
            )
            self._completeness_signal(item, "unknown_capture_gap", dropped=False)
            return False
        offer_started = perf_counter()
        try:
            self._queue.put_nowait(payload)
        except asyncio.QueueFull:
            self._metric(
                "run_evidence_export_dropped_count",
                outcome="failed",
                error_class="queue_pressure",
                operation="queue_offer",
                trace_id=item.trace_id,
                run_id=str(item.run_id),
            )
            self._span(
                "run_evidence.queue_offer",
                (perf_counter() - offer_started) * 1000,
                item=item,
                outcome="failed",
                error_class="queue_pressure",
                metric_name="run_evidence_queue_offer_latency_ms",
            )
            self._completeness_signal(item, "queue_pressure", dropped=True)
            return False
        self._pending_payloads[str(envelope["eventId"])] = payload
        self._pending_payloads.move_to_end(str(envelope["eventId"]))
        self._span(
            "run_evidence.queue_offer",
            (perf_counter() - offer_started) * 1000,
            item=item,
            outcome="accepted",
            metric_name="run_evidence_queue_offer_latency_ms",
        )
        self._measurement(
            "run_evidence_queue_depth",
            float(self._queue.qsize()),
            operation="queue_offer",
        )
        self._last_sequence[item.run_id] = max(item.sequence, previous or item.sequence)
        self._last_sequence.move_to_end(item.run_id)
        self._sequence_identities[key] = identity
        self._sequence_identities.move_to_end(key)
        while len(self._last_sequence) > 1024:
            self._last_sequence.popitem(last=False)
        while len(self._sequence_identities) > 4096:
            self._sequence_identities.popitem(last=False)
        # Keep one additional slot for the item currently being published. A
        # queue-sized bound alone can evict that in-flight item and make a
        # bounded shutdown report lose its run correlation.
        while len(self._pending_payloads) > self.policy.queue_capacity + 1:
            self._pending_payloads.popitem(last=False)
        return True

    async def start(self) -> None:
        if self._task is None and self.policy.enabled and self.publisher is not None:
            self._stopping = False
            try:
                self._measurement(
                    "run_evidence_queue_capacity",
                    float(self.policy.queue_capacity),
                    operation="queue_offer",
                )
            except Exception:
                pass
            self._task = asyncio.create_task(self._run(), name="aura-run-evidence-export")

    async def stop(self) -> None:
        self._stopping = True
        if self._task is not None:
            try:
                await asyncio.wait_for(self._queue.join(), timeout=self.policy.drain_seconds)
            except TimeoutError:
                for queued_payload in tuple(self._pending_payloads.values()):
                    self._span(
                        "run_evidence.transport_publish",
                        0.0,
                        payload=queued_payload,
                        outcome="failed",
                        error_class="shutdown",
                        metric_name="run_evidence_transport_latency_ms",
                    )
                    try:
                        envelope = json.loads(queued_payload)
                        self._metric(
                            "run_evidence_export_dropped_count",
                            outcome="failed",
                            error_class="shutdown",
                            operation="transport_publish",
                            trace_id=str(envelope["traceId"]),
                            run_id=str(envelope["runId"]),
                        )
                    except (TypeError, ValueError, KeyError):
                        continue
                self._metric(
                    "run_evidence_export_dropped_count",
                    outcome="failed",
                    error_class="shutdown",
                    operation="transport_publish",
                )
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._measurement(
                "run_evidence_queue_depth",
                float(self._queue.qsize()),
                operation="queue_offer",
            )
            self._task = None
        if self.publisher is not None:
            try:
                await self.publisher.close()
            except Exception:
                self._metric(
                    "run_evidence_export_outcome_count",
                    outcome="error",
                    error_class="transport_failure",
                    operation="transport_publish",
                )

    async def _run(self) -> None:
        assert self.publisher is not None
        while True:
            payload = await self._queue.get()
            try:
                message_id = UUID(str(json.loads(payload)["eventId"]))
                delivered = False
                first_delivery_id = str(json.loads(payload)["delivery"]["deliveryId"])
                for attempt in range(1, self.policy.max_attempts + 1):
                    try:
                        transport_started = perf_counter()
                        attempt_payload = _delivery_attempt_payload(
                            payload,
                            attempt=attempt,
                            first_delivery_id=first_delivery_id,
                        )
                        await self.publisher.publish(attempt_payload, message_id=message_id)
                        self._span(
                            "run_evidence.transport_publish",
                            (perf_counter() - transport_started) * 1000,
                            payload=attempt_payload,
                            outcome="accepted",
                            metric_name="run_evidence_transport_latency_ms",
                        )
                        delivered = True
                        self._metric(
                            "run_evidence_export_outcome_count",
                            outcome="ok",
                            operation="transport_publish",
                        )
                        break
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        self._span(
                            "run_evidence.transport_publish",
                            (perf_counter() - transport_started) * 1000,
                            payload=attempt_payload,
                            outcome="failed",
                            error_class="transport_failure",
                            metric_name="run_evidence_transport_latency_ms",
                        )
                        if attempt < self.policy.max_attempts:
                            await asyncio.sleep(0)
                if not delivered:
                    failed_item = self._item_from_envelope(payload)
                    if failed_item is not None:
                        self._metric(
                            "run_evidence_export_dropped_count",
                            outcome="failed",
                            error_class="transport_unavailable",
                            operation="transport_publish",
                            trace_id=failed_item.trace_id,
                            run_id=str(failed_item.run_id),
                        )
                    else:
                        self._metric(
                            "run_evidence_export_dropped_count",
                            outcome="failed",
                            error_class="transport_unavailable",
                            operation="transport_publish",
                        )
            finally:
                try:
                    event_id = str(json.loads(payload)["eventId"])
                    self._pending_payloads.pop(event_id, None)
                except (TypeError, ValueError, KeyError):
                    pass
                self._measurement(
                    "run_evidence_queue_depth",
                    float(self._queue.qsize()),
                    operation="queue_offer",
                )
                self._queue.task_done()


def _delivery_attempt_payload(payload: bytes, *, attempt: int, first_delivery_id: str) -> bytes:
    """Give every transport attempt its own delivery identity."""

    envelope = json.loads(payload)
    envelope["delivery"] = {
        "kind": "initial" if attempt == 1 else "retry",
        "producerId": "aura-core",
        "deliveryId": first_delivery_id if attempt == 1 else str(uuid4()),
        "attempt": attempt,
        **({"firstDeliveryId": first_delivery_id} if attempt > 1 else {}),
    }
    return _canonical_json(envelope)


class JetStreamRunEvidencePublisher:
    """Small adapter around an already-created NATS connection."""

    def __init__(
        self,
        connection: Any,
        *,
        subject: str = RUN_EVIDENCE_SUBJECT,
        close_connection: bool = False,
    ) -> None:
        self.connection = connection
        self.subject = subject
        self.close_connection = close_connection

    async def publish(self, payload: bytes, *, message_id: UUID) -> None:
        jetstream = self.connection.jetstream()
        await jetstream.publish(self.subject, payload, headers={"Nats-Msg-Id": str(message_id)})

    async def close(self) -> None:
        close = getattr(self.connection, "close", None)
        if self.close_connection and callable(close):
            await close()


def make_run_evidence_recorder(
    settings: Any, *, metrics: MetricsPort | None = None
) -> RunEvidenceRecorder:
    """Build the policy boundary without allowing OTLP to enable capture."""

    policy = RunEvidencePolicy(
        enabled=bool(getattr(settings, "run_evidence_capture_policy", "") == "full_content"),
        revision=str(getattr(settings, "run_evidence_capture_revision", "full-content-v1")),
        queue_capacity=int(getattr(settings, "run_evidence_queue_capacity", 256)),
        max_attempts=int(getattr(settings, "run_evidence_max_attempts", 3)),
        drain_seconds=float(getattr(settings, "run_evidence_drain_seconds", 5.0)),
        max_bytes=int(getattr(settings, "run_evidence_max_bytes", MAX_EVIDENCE_BYTES)),
    )
    if not policy.enabled:
        return DisabledRunEvidenceRecorder()
    # Composition roots bind the concrete NATS publisher once their transport
    # connection is ready.  No connection is created in this constructor.
    return BoundedRunEvidenceRecorder(policy, metrics=metrics)


__all__ = [
    "BoundedRunEvidenceRecorder",
    "DisabledRunEvidenceRecorder",
    "EvidenceCaptureState",
    "EvidenceRejected",
    "JetStreamRunEvidencePublisher",
    "RunEvidenceInput",
    "RunEvidencePolicy",
    "RunEvidenceRecorder",
    "RUN_EVIDENCE_SUBJECT",
    "make_run_evidence_recorder",
    "serialize_evidence",
]
