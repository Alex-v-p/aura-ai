"""Governed run-evidence ingestion, inspection, and deletion use cases.

This module is deliberately independent from Aura Core.  It consumes the
versioned wire envelope as bytes and exposes only Observatory-owned ports.
Evidence bytes are never placed in metrics, logs, live events, or inspection
snapshots; callers fetch them through the separate bounded stream port.
"""
# ruff: noqa: E501,B007,F401,F841
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownMemberType=false, reportGeneralTypeIssues=false, reportArgumentType=false, reportPrivateUsage=false, reportUnnecessaryIsInstance=false, reportUnnecessaryCast=false, reportUnnecessaryComparison=false

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from collections import defaultdict
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from time import monotonic
from typing import Any, Protocol, cast
from uuid import UUID, uuid4, uuid5

MAX_EVIDENCE_BYTES = 1_048_576
# Wire envelopes may carry a 1 MiB evidence item plus bounded JSON/base64
# framing.  This is deliberately separate from the stored evidence limit.
MAX_ENVELOPE_BYTES = 1_500_000
MAX_PAGE_SIZE = 1000
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
_TRACE_RE = re.compile(r"^[0-9a-f]{32}$")
_SPAN_RE = re.compile(r"^[0-9a-f]{16}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_MEDIA = frozenset(
    {"application/json", "text/plain", "text/markdown", "image/png", "image/jpeg", "image/webp", "application/pdf"}
)
_CATEGORIES = frozenset(
    {"admission", "context", "retrieval", "prompt", "routing", "generation", "thinking", "tool", "policy", "synthesis", "persistence", "delivery", "worker", "automation", "schedule", "evaluation"}
)
_KINDS = frozenset(
    {"admitted_request", "admitted_trigger", "compiled_prompt", "conversation_context", "memory_chunk", "document_chunk", "model_visible_attachment", "model_routing", "displayed_thinking", "generation_output", "tool_arguments", "tool_result", "policy_decision", "approval_decision", "execution_outcome", "response_synthesis", "persistence_record", "delivery_record", "worker_input", "worker_output", "automation_input", "automation_output", "schedule_input", "schedule_output", "evaluation_input", "evaluation_output"}
)
_KIND_VARIANTS: dict[str, tuple[str, str, frozenset[str]]] = {
    "admitted_request": ("admission", "structured", frozenset({"application/json"})),
    "admitted_trigger": ("admission", "structured", frozenset({"application/json"})),
    "compiled_prompt": ("prompt", "text", frozenset({"text/plain", "text/markdown"})),
    "conversation_context": ("context", "structured", frozenset({"application/json"})),
    "memory_chunk": ("retrieval", "structured", frozenset({"application/json"})),
    "document_chunk": ("retrieval", "structured", frozenset({"application/json"})),
    # Attachments are special-cased below because their contract permits
    # structured, text, image, and document media, while always remaining
    # context evidence.
    "model_visible_attachment": ("context", "structured", frozenset(_MEDIA)),
    "model_routing": ("routing", "structured", frozenset({"application/json"})),
    "displayed_thinking": ("thinking", "text", frozenset({"text/plain"})),
    "generation_output": ("generation", "text", frozenset({"text/plain", "text/markdown"})),
    "tool_arguments": ("tool", "structured", frozenset({"application/json"})),
    "tool_result": ("tool", "structured", frozenset({"application/json"})),
    "policy_decision": ("policy", "structured", frozenset({"application/json"})),
    "approval_decision": ("policy", "structured", frozenset({"application/json"})),
    "execution_outcome": ("tool", "structured", frozenset({"application/json"})),
    "response_synthesis": ("synthesis", "text", frozenset({"text/plain", "text/markdown"})),
    "persistence_record": ("persistence", "structured", frozenset({"application/json"})),
    "delivery_record": ("delivery", "structured", frozenset({"application/json"})),
    "worker_input": ("worker", "structured", frozenset({"application/json"})),
    "worker_output": ("worker", "structured", frozenset({"application/json"})),
    "automation_input": ("automation", "structured", frozenset({"application/json"})),
    "automation_output": ("automation", "structured", frozenset({"application/json"})),
    "schedule_input": ("schedule", "structured", frozenset({"application/json"})),
    "schedule_output": ("schedule", "structured", frozenset({"application/json"})),
    "evaluation_input": ("evaluation", "structured", frozenset({"application/json"})),
    "evaluation_output": ("evaluation", "structured", frozenset({"application/json"})),
}
_ATTACHMENT_TYPES = frozenset({"text/plain", "text/markdown", "application/json", "image/png", "image/jpeg", "image/webp", "application/pdf"})
_REASONS = frozenset({"capture_disabled", "credential_material_rejected", "secret_bearing_field_rejected", "bearer_material_rejected", "provider_hidden_reasoning_excluded", "unsupported_media_type", "payload_limit_exceeded", "queue_pressure", "export_unavailable", "transport_failure", "ingest_rejected", "storage_failure", "manually_deleted", "unknown_capture_gap"})
# These are the producer revisions declared by Core's run-evidence serializer.
# Observatory accepts only this explicitly governed pair; unknown revisions
# remain terminally rejected rather than being treated as compatible.
_SCANNER_REVISIONS = frozenset({"core-evidence-scanner-v1"})
_POLICY_REVISIONS = frozenset({"core-evidence-policy-v1"})
_STEP_NAMESPACE = UUID("6f2df1fb-9c5d-4d2d-8dc8-5bdcbd08beba")
RUN_EVIDENCE_EVALUATORS = frozenset(
    {
        "run_evidence_ingestion_integrity",
        "run_evidence_schema_compatibility",
        "run_evidence_failure_isolation",
        "run_inspection_projection_truth",
        "run_evidence_deletion_recovery",
        "observatory_run_evidence_privacy",
        "metadata_v2_allowlist",
    }
)
_SECRET_RE = re.compile(r"(?:password|passwd|secret|token|credential|api[_-]?key|bearer|private[_-]?key|authorization|cookie|session(?:[_-]?id)?)", re.I)
_SECRET_VALUE_RE = re.compile(
    r"(?:\bbearer\s+[A-Za-z0-9._~+/=-]{8,}|\bbasic\s+[A-Za-z0-9+/]{16,}={0,2}|\b(?:AKIA|ASIA)[A-Z0-9]{16}\b|\bgh[pousr]_[A-Za-z0-9_]{20,}\b|\bgithub_pat_[A-Za-z0-9_]{20,}\b|-----BEGIN\s+(?:RSA|OPENSSH|EC|DSA)?\s*PRIVATE KEY-----)",
    re.I,
)
_HIDDEN_REASONING_RE = re.compile(r"(?:chain[_-]?of[_-]?thought|hidden[_-]?(?:reasoning|thought)|provider[_-]?(?:reasoning|thought))", re.I)


class RunEvidenceError(ValueError):
    """Content-free bounded validation or state error."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class DuplicateCollision(RunEvidenceError):
    def __init__(self) -> None:
        super().__init__("duplicate_collision")


class RunEvidenceDeletionInProgress(RuntimeError):
    """Retryable ingest fence while a run's evidence is being deleted."""


class RunEvidenceDeletionClosed(RunEvidenceError):
    """Terminal tombstone: a manually deleted run cannot capture new bytes."""

    def __init__(self) -> None:
        super().__init__("ingest_rejected")


class EvidenceUnavailable(RuntimeError):
    def __init__(self, code: str = "service_unavailable") -> None:
        self.code = code
        super().__init__(code)


class RunEvidenceProblem(RuntimeError):
    """Endpoint-scoped failure that must never expose provider diagnostics."""

    def __init__(self, code: str = "service_unavailable") -> None:
        self.code = code
        super().__init__(code)


def _record_component_span(
    metrics: Any | None,
    *,
    component_id: str,
    operation: str,
    duration_ms: float,
    outcome: str,
    dependency: str | None = None,
    error_code: str | None = None,
    trace_id: str | None = None,
    parent_span_id: str | None = None,
    run_id: str | None = None,
) -> None:
    recorder = getattr(metrics, "record_component_span", None)
    if not callable(recorder):
        return
    try:
        recorder(
            component_id=component_id,
            component_version="1.0.0",
            deployment_id=str(getattr(metrics, "deployment_id", "observatory")),
            operation=operation,
            duration_ms=max(0.001, duration_ms),
            outcome=outcome,
            dependency=dependency,
            error_code=error_code,
            trace_id=trace_id,
            parent_span_id=parent_span_id,
            run_id=run_id,
        )
    except (TypeError, ValueError):
        return


def _canonical_json(value: object) -> bytes:
    """Encode JSON using RFC 8785/JCS primitive rules.

    The Core producer uses the same dependency-free encoder.  Keeping the
    consumer canonicalization identical prevents a numerically equivalent
    payload from being accepted under a different digest.
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
        decimal_position = (coefficient.find(".") if "." in coefficient else len(coefficient)) + power
        absolute = abs(item)
        if 1e-6 <= absolute < 1e21:
            if decimal_position <= 0:
                result = "0." + "0" * (-decimal_position) + digits
            elif decimal_position >= len(digits):
                result = digits + "0" * (decimal_position - len(digits))
            else:
                result = digits[:decimal_position] + "." + digits[decimal_position:]
            return sign + result.rstrip("0").rstrip(".") if "." in result else sign + result
        mantissa = coefficient.rstrip("0").rstrip(".")
        return sign + mantissa + "e" + f"{power:+d}"

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
            keys = sorted((str(key) for key in item), key=lambda key: key.encode("utf-16-be"))
            return "{" + ",".join(json.dumps(key, ensure_ascii=False) + ":" + encode(item[key]) for key in keys) + "}"
        if isinstance(item, Sequence) and not isinstance(item, (bytes, bytearray)):
            return "[" + ",".join(encode(entry) for entry in item) + "]"
        raise TypeError(f"unsupported JSON value: {type(item).__name__}")

    return encode(value).encode("utf-8")


def _uuid(value: object, field: str) -> str:
    if not isinstance(value, str) or not _UUID_RE.fullmatch(value):
        raise RunEvidenceError(f"invalid_{field}")
    return value


def _utc(value: object, field: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,9})?Z", value):
        raise RunEvidenceError(f"invalid_{field}")
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RunEvidenceError(f"invalid_{field}") from exc
    return value


def _scan_secret(value: object, depth: int = 0) -> bool:
    if depth > 4:
        return True
    if isinstance(value, Mapping):
        for key, child in value.items():
            key_text = str(key)
            if _SECRET_RE.search(key_text) or _HIDDEN_REASONING_RE.search(key_text) or _scan_secret(child, depth + 1):
                return True
        return False
    if isinstance(value, (list, tuple)):
        return any(_scan_secret(child, depth + 1) for child in value)
    if isinstance(value, str):
        if _SECRET_VALUE_RE.search(value) or (_SECRET_RE.search(value) and (":" in value or "=" in value)):
            return True
        stripped = value.lstrip()
        if stripped.startswith(("{", "[")) and depth < 4:
            try:
                return _scan_secret(json.loads(value), depth + 1)
            except (ValueError, TypeError, json.JSONDecodeError):
                return False
    return False


def _attachment_bytes(media_type: str, encoded: object) -> bytes:
    if not isinstance(encoded, str) or len(encoded) > 1_398_104:
        raise RunEvidenceError("payload_limit_exceeded")
    try:
        data = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise RunEvidenceError("unsupported_media_type") from exc
    if len(data) > MAX_EVIDENCE_BYTES:
        raise RunEvidenceError("payload_limit_exceeded")
    valid = {
        "image/png": data.startswith(b"\x89PNG\r\n\x1a\n"),
        "image/jpeg": data.startswith(b"\xff\xd8\xff"),
        "image/webp": data.startswith(b"RIFF") and data[8:12] == b"WEBP",
        "application/pdf": data.startswith(b"%PDF-"),
        "text/plain": True,
        "text/markdown": True,
        "application/json": True,
    }
    if not valid.get(media_type, False):
        raise RunEvidenceError("unsupported_media_type")
    return data


def _validate_payload_shape(kind: str, payload: Mapping[str, object]) -> None:
    """Validate each closed payload variant before canonicalization."""

    def exact(value: object, keys: set[str]) -> Mapping[str, object] | None:
        return value if isinstance(value, Mapping) and set(value) == keys else None

    expected_keys: dict[str, set[str]] = {
        "admitted_request": {"kind", "request"},
        "admitted_trigger": {"kind", "triggerType", "content"},
        "compiled_prompt": {"kind", "prompt"},
        "conversation_context": {"kind", "messages"},
        "memory_chunk": {"kind", "chunk"},
        "document_chunk": {"kind", "chunk"},
        "model_visible_attachment": {"kind", "attachmentId", "displayName", "contentBase64"},
        "model_routing": {"kind", "provider", "model", "routeReason"},
        "displayed_thinking": {"kind", "text"},
        "generation_output": {"kind", "text"},
        "response_synthesis": {"kind", "text"},
        "tool_arguments": {"kind", "toolName", "content"},
        "tool_result": {"kind", "toolName", "content"},
        "policy_decision": {"kind", "decision"},
        "approval_decision": {"kind", "decision"},
        "execution_outcome": {"kind", "outcome", "detail"},
        "persistence_record": {"kind", "record"},
        "delivery_record": {"kind", "record"},
        "worker_input": {"kind", "content"},
        "worker_output": {"kind", "content"},
        "automation_input": {"kind", "content"},
        "automation_output": {"kind", "content"},
        "schedule_input": {"kind", "content"},
        "schedule_output": {"kind", "content"},
        "evaluation_input": {"kind", "content"},
        "evaluation_output": {"kind", "content"},
    }
    if set(payload) != expected_keys[kind]:
        raise RunEvidenceError("ingest_rejected")

    def text(value: object, limit: int = MAX_EVIDENCE_BYTES) -> bool:
        return isinstance(value, str) and len(value) <= limit

    def bounded_id(value: object) -> bool:
        return isinstance(value, str) and bool(_ID_RE.fullmatch(value))

    if kind == "admitted_request":
        request = exact(payload.get("request"), {"role", "content"})
        if request is None or request.get("role") not in {"system", "developer", "user", "assistant", "tool"} or not text(request.get("content")):
            raise RunEvidenceError("ingest_rejected")
    elif kind == "admitted_trigger":
        if not bounded_id(payload.get("triggerType")) or not text(payload.get("content")):
            raise RunEvidenceError("ingest_rejected")
    elif kind == "compiled_prompt":
        if not text(payload.get("prompt")):
            raise RunEvidenceError("ingest_rejected")
    elif kind == "conversation_context":
        messages = payload.get("messages")
        if not isinstance(messages, list) or not 1 <= len(messages) <= 256:
            raise RunEvidenceError("ingest_rejected")
        for message in messages:
            entry = exact(message, {"role", "content"})
            if entry is None or entry.get("role") not in {"system", "developer", "user", "assistant", "tool"} or not text(entry.get("content")):
                raise RunEvidenceError("ingest_rejected")
    elif kind in {"memory_chunk", "document_chunk"}:
        chunk = exact(payload.get("chunk"), {"sourceId", "ordinal", "content", "score"})
        score = chunk.get("score") if chunk is not None else None
        if chunk is None or not bounded_id(chunk.get("sourceId")) or type(chunk.get("ordinal")) is not int or not 0 <= cast(int, chunk.get("ordinal")) <= 1_000_000 or not text(chunk.get("content")) or (score is not None and (not isinstance(score, (int, float)) or isinstance(score, bool) or not -1_000_000 <= float(score) <= 1_000_000)):
            raise RunEvidenceError("ingest_rejected")
    elif kind == "model_visible_attachment":
        if not bounded_id(payload.get("attachmentId")) or not isinstance(payload.get("displayName"), str) or not 0 < len(cast(str, payload.get("displayName"))) <= 256 or not isinstance(payload.get("contentBase64"), str):
            raise RunEvidenceError("ingest_rejected")
    elif kind == "model_routing":
        if not bounded_id(payload.get("provider")) or not bounded_id(payload.get("model")) or not text(payload.get("routeReason"), 4096):
            raise RunEvidenceError("ingest_rejected")
    elif kind in {"displayed_thinking", "generation_output", "response_synthesis"}:
        if not text(payload.get("text")):
            raise RunEvidenceError("ingest_rejected")
    elif kind in {"tool_arguments", "tool_result"}:
        if not bounded_id(payload.get("toolName")) or not text(payload.get("content")):
            raise RunEvidenceError("ingest_rejected")
    elif kind in {"policy_decision", "approval_decision"}:
        decision = exact(payload.get("decision"), {"decision", "reason"})
        if decision is None or not bounded_id(decision.get("decision")) or not text(decision.get("reason"), 4096):
            raise RunEvidenceError("ingest_rejected")
    elif kind == "execution_outcome":
        if payload.get("outcome") not in {"succeeded", "failed", "cancelled", "rejected", "timed_out"} or not text(payload.get("detail"), 4096):
            raise RunEvidenceError("ingest_rejected")
    elif kind in {"persistence_record", "delivery_record"}:
        record = exact(payload.get("record"), {"name", "content"})
        if record is None or not bounded_id(record.get("name")) or not text(record.get("content")):
            raise RunEvidenceError("ingest_rejected")
    else:
        content = exact(payload.get("content"), {"name", "content"})
        if content is None or not bounded_id(content.get("name")) or not text(content.get("content")):
            raise RunEvidenceError("ingest_rejected")


def _validate_content_admission(
    admission: Mapping[str, object],
    *,
    classification: str,
    captured: bool,
    attachment: bool,
) -> None:
    """Validate scanner attestation without accepting caller-controlled claims."""

    outcome = admission.get("outcome")
    if not captured:
        if dict(admission) == {"outcome": "not_applicable"}:
            return
        if outcome != "rejected_prohibited_material":
            raise RunEvidenceError("ingest_rejected")
    required = {"outcome", "scannerRevision", "policyRevision", "inspectedAt", "inspectionModes"}
    if set(admission) != required:
        raise RunEvidenceError("ingest_rejected")
    for field in ("scannerRevision", "policyRevision"):
        value = admission.get(field)
        allowed = _SCANNER_REVISIONS if field == "scannerRevision" else _POLICY_REVISIONS
        if not isinstance(value, str) or value not in allowed or _scan_secret(value):
            raise RunEvidenceError("ingest_rejected")
    _utc(admission.get("inspectedAt"), "inspected_at")
    modes = admission.get("inspectionModes")
    if not isinstance(modes, list) or not modes or len(modes) > 4 or len(set(modes)) != len(modes) or any(mode not in {"decoded_text", "metadata", "container_structure", "ocr"} for mode in modes):
        raise RunEvidenceError("ingest_rejected")
    required_modes = {
        "text": {"decoded_text", "container_structure"} if attachment else {"decoded_text"},
        "structured": {"decoded_text", "container_structure"},
        "image": {"metadata", "container_structure", "ocr"},
        "document": {"decoded_text", "metadata", "container_structure", "ocr"},
    }[classification]
    if not required_modes.issubset(set(cast(list[str], modes))):
        raise RunEvidenceError("ingest_rejected")


@dataclass(frozen=True, slots=True)
class ValidatedRunEvidence:
    envelope: Mapping[str, object]
    canonical_bytes: bytes
    fingerprint: str

    @property
    def run_id(self) -> str:
        return cast(str, self.envelope["runId"])

    @property
    def evidence_id(self) -> str:
        return cast(str, self.envelope["evidenceId"])

    @property
    def event_id(self) -> str:
        return cast(str, self.envelope["eventId"])


def validate_run_evidence(envelope: bytes | bytearray | Mapping[str, object]) -> ValidatedRunEvidence:
    """Validate and canonicalize one complete run-evidence envelope."""

    if isinstance(envelope, (bytes, bytearray)):
        if len(envelope) > MAX_ENVELOPE_BYTES:
            raise RunEvidenceError("payload_limit_exceeded")
        try:
            value = json.loads(bytes(envelope))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RunEvidenceError("ingest_rejected") from exc
    else:
        value = dict(envelope)
    if not isinstance(value, Mapping):
        raise RunEvidenceError("ingest_rejected")
    required = {"schemaVersion", "contractId", "eventId", "evidenceId", "runId", "traceId", "spanId", "parentSpanId", "sequence", "stepCategory", "evidenceKind", "mediaClassification", "mediaType", "digest", "byteCount", "capturePolicyRevision", "observedAt", "emittedAt", "delivery", "contentAdmission", "captureState", "reason"}
    if set(value) - required - {"payload"} or not required.issubset(value):
        raise RunEvidenceError("ingest_rejected")
    if value.get("schemaVersion") != 1 or value.get("contractId") != "run-evidence-v1":
        raise RunEvidenceError("ingest_rejected")
    event_id = _uuid(value.get("eventId"), "event_id")
    evidence_id = _uuid(value.get("evidenceId"), "evidence_id")
    run_id = _uuid(value.get("runId"), "run_id")
    if not isinstance(value.get("traceId"), str) or not _TRACE_RE.fullmatch(cast(str, value.get("traceId"))):
        raise RunEvidenceError("invalid_trace_id")
    if not isinstance(value.get("spanId"), str) or not _SPAN_RE.fullmatch(cast(str, value.get("spanId"))):
        raise RunEvidenceError("invalid_span_id")
    parent = value.get("parentSpanId")
    if parent is not None and (not isinstance(parent, str) or not _SPAN_RE.fullmatch(parent)):
        raise RunEvidenceError("invalid_parent_span_id")
    sequence = value.get("sequence")
    if type(sequence) is not int or not 0 <= cast(int, sequence) <= 9_007_199_254_740_991:
        raise RunEvidenceError("invalid_sequence")
    kind = value.get("evidenceKind")
    category = value.get("stepCategory")
    classification = value.get("mediaClassification")
    media_type = value.get("mediaType")
    if kind not in _KINDS or category not in _CATEGORIES or classification not in {"text", "structured", "image", "document"} or media_type not in _MEDIA:
        raise RunEvidenceError("ingest_rejected")
    expected = _KIND_VARIANTS.get(cast(str, kind))
    if kind == "model_visible_attachment":
        attachment_media = {
            "structured": {"application/json"},
            "text": {"text/plain", "text/markdown"},
            "image": {"image/png", "image/jpeg", "image/webp"},
            "document": {"application/pdf"},
        }
        if category != "context" or cast(str, media_type) not in attachment_media[cast(str, classification)]:
            raise RunEvidenceError("ingest_rejected")
    elif expected and (media_type not in expected[2] or (cast(str, category), cast(str, classification)) != expected[:2]):
        raise RunEvidenceError("ingest_rejected")
    policy = value.get("capturePolicyRevision")
    if not isinstance(policy, str) or not _ID_RE.fullmatch(policy) or _scan_secret(policy):
        raise RunEvidenceError("ingest_rejected")
    _utc(value.get("observedAt"), "observed_at")
    _utc(value.get("emittedAt"), "emitted_at")
    delivery = value.get("delivery")
    if not isinstance(delivery, Mapping) or delivery.get("kind") not in {"initial", "retry"} or not isinstance(delivery.get("producerId"), str) or not _ID_RE.fullmatch(cast(str, delivery.get("producerId"))) or _scan_secret(delivery.get("producerId")) or not _uuid(delivery.get("deliveryId"), "delivery_id"):
        raise RunEvidenceError("ingest_rejected")
    if delivery.get("kind") == "initial" and set(delivery) != {"kind", "producerId", "deliveryId", "attempt"}:
        raise RunEvidenceError("ingest_rejected")
    if delivery.get("kind") == "retry" and set(delivery) != {"kind", "producerId", "deliveryId", "attempt", "firstDeliveryId"}:
        raise RunEvidenceError("ingest_rejected")
    attempt = delivery.get("attempt")
    if type(attempt) is not int or not 1 <= cast(int, attempt) <= 100:
        raise RunEvidenceError("ingest_rejected")
    if delivery.get("kind") == "initial" and attempt != 1:
        raise RunEvidenceError("ingest_rejected")
    if delivery.get("kind") == "retry" and (attempt < 2 or not _uuid(delivery.get("firstDeliveryId"), "delivery_id")):
        raise RunEvidenceError("ingest_rejected")
    admission = value.get("contentAdmission")
    if not isinstance(admission, Mapping) or admission.get("outcome") not in {"passed", "rejected_prohibited_material", "not_applicable"}:
        raise RunEvidenceError("ingest_rejected")
    _validate_content_admission(admission, classification=cast(str, classification), captured=value.get("captureState") == "captured", attachment=kind == "model_visible_attachment")
    state = value.get("captureState")
    reason = value.get("reason")
    if state not in {"captured", "excluded", "partial", "dropped", "deleted"} or (reason is not None and reason not in _REASONS):
        raise RunEvidenceError("ingest_rejected")
    allowed_reasons = {
        "excluded": {"capture_disabled", "provider_hidden_reasoning_excluded"},
        "partial": {"credential_material_rejected", "secret_bearing_field_rejected", "bearer_material_rejected", "unsupported_media_type", "payload_limit_exceeded", "ingest_rejected", "unknown_capture_gap"},
        "dropped": {"queue_pressure", "export_unavailable", "transport_failure", "storage_failure"},
        "deleted": {"manually_deleted"},
    }
    if state != "captured" and reason not in allowed_reasons[cast(str, state)]:
        raise RunEvidenceError("ingest_rejected")
    if state == "captured" and reason is not None:
        raise RunEvidenceError("ingest_rejected")
    if state in {"excluded", "dropped", "deleted"} and admission != {"outcome": "not_applicable"}:
        raise RunEvidenceError("ingest_rejected")
    if reason in {"credential_material_rejected", "secret_bearing_field_rejected", "bearer_material_rejected"} and admission.get("outcome") != "rejected_prohibited_material":
        raise RunEvidenceError("ingest_rejected")
    payload = value.get("payload")
    if state == "captured":
        if not isinstance(payload, Mapping) or payload.get("kind") != kind or reason is not None or admission.get("outcome") != "passed":
            raise RunEvidenceError("ingest_rejected")
        if _scan_secret(payload):
            raise RunEvidenceError("secret_bearing_field_rejected")
        if kind == "compiled_prompt" and isinstance(payload.get("prompt"), str) and len(cast(str, payload.get("prompt"))) > MAX_EVIDENCE_BYTES:
            raise RunEvidenceError("payload_limit_exceeded")
        _validate_payload_shape(cast(str, kind), payload)
        if kind == "model_visible_attachment":
            attachment = payload.get("attachmentId")
            display = payload.get("displayName")
            if not isinstance(attachment, str) or not _ID_RE.fullmatch(attachment) or not isinstance(display, str) or not 0 < len(display) <= 256:
                raise RunEvidenceError("ingest_rejected")
            canonical = _attachment_bytes(cast(str, media_type), payload.get("contentBase64"))
        elif classification == "text":
            text = payload.get("prompt") if kind == "compiled_prompt" else payload.get("text")
            if not isinstance(text, str) or len(text) > MAX_EVIDENCE_BYTES:
                raise RunEvidenceError("payload_limit_exceeded")
            canonical = text.encode("utf-8")
        else:
            canonical = _canonical_json(payload)
    else:
        if payload is not None or value.get("digest") is not None or value.get("byteCount") != 0 or reason is None:
            raise RunEvidenceError("ingest_rejected")
        canonical = b""
        if admission.get("outcome") not in {"not_applicable", "rejected_prohibited_material"}:
            raise RunEvidenceError("ingest_rejected")
    if len(canonical) > MAX_EVIDENCE_BYTES:
        raise RunEvidenceError("payload_limit_exceeded")
    byte_count = value.get("byteCount")
    digest = value.get("digest")
    if type(byte_count) is not int or byte_count != len(canonical):
        raise RunEvidenceError("digest_mismatch")
    if state == "captured":
        if not isinstance(digest, str) or not _DIGEST_RE.fullmatch(digest) or digest != "sha256:" + hashlib.sha256(canonical).hexdigest():
            raise RunEvidenceError("digest_mismatch")
    elif digest is not None:
        raise RunEvidenceError("ingest_rejected")
    immutable = dict(value)
    immutable.pop("delivery", None)
    immutable.pop("emittedAt", None)
    immutable.pop("payload", None)
    immutable["canonicalDigest"] = hashlib.sha256(canonical).hexdigest()
    immutable["canonicalBytes"] = len(canonical)
    fingerprint = hashlib.sha256(_canonical_json(immutable)).hexdigest()
    normalized = dict(value)
    normalized["eventId"] = event_id
    normalized["evidenceId"] = evidence_id
    normalized["runId"] = run_id
    return ValidatedRunEvidence(normalized, canonical, fingerprint)


@dataclass(frozen=True, slots=True)
class RunEvidenceManifest:
    evidence_id: str
    event_id: str
    run_id: str
    trace_id: str
    span_id: str
    parent_span_id: str | None
    sequence: int
    category: str
    kind: str
    capture_state: str
    media_classification: str
    media_type: str
    digest: str | None
    byte_count: int
    captured_at: str
    reason: str | None

    def as_dict(self) -> dict[str, object]:
        return {"evidenceId": self.evidence_id, "eventId": self.event_id, "sequence": self.sequence, "kind": self.kind, "captureState": self.capture_state, "mediaClassification": self.media_classification, "mediaType": self.media_type, "digest": self.digest, "byteCount": self.byte_count, "capturedAt": self.captured_at, "reason": self.reason}


@dataclass(frozen=True, slots=True)
class RunEvidenceDeletion:
    deletion_id: str
    run_id: str
    status: str
    requested_at: str
    completed_at: str | None
    deleted_evidence_count: int
    failure_code: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {"schemaVersion": 1, "deletionId": self.deletion_id, "runId": self.run_id, "status": self.status, "requestedAt": self.requested_at, "completedAt": self.completed_at, "deletedEvidenceCount": self.deleted_evidence_count, "auditMode": "content_free", "failureCode": self.failure_code}


class RunEvidenceRepository(Protocol):
    async def ingest(self, item: ValidatedRunEvidence) -> str: ...
    async def manifests(self, run_id: str, *, cursor: str | None = None, limit: int = MAX_PAGE_SIZE) -> tuple[list[RunEvidenceManifest], str | None]: ...
    async def inspection_page(self, run_id: str, *, cursor: str | None = None, limit: int = MAX_PAGE_SIZE) -> tuple[list[RunEvidenceManifest], str | None, Mapping[str, object]]: ...
    async def stream(self, run_id: str, evidence_id: str) -> tuple[RunEvidenceManifest, AsyncIterator[bytes]]: ...
    async def delete(self, run_id: str, idempotency_key: str) -> RunEvidenceDeletion: ...
    async def events_after(self, last_event_id: str | None = None) -> list[dict[str, object]]: ...
    async def record_rejection(self, payload: bytes | Mapping[str, object], reason: str) -> None: ...
    async def summary(self, run_id: str) -> Mapping[str, object]: ...


class MemoryRunEvidenceRepository:
    """Reference repository used by tests and disposable local Observatory."""

    def __init__(self) -> None:
        self.items: dict[str, tuple[RunEvidenceManifest, bytes, str | None, Mapping[str, object]]] = {}
        self._by_stable: dict[str, str] = {}
        self._deletions: dict[tuple[str, str], RunEvidenceDeletion] = {}
        self.events: list[dict[str, object]] = []
        self.rejections: list[dict[str, object]] = []
        self._last_dedupe_duration_ms = 0.001

    async def ingest(self, item: ValidatedRunEvidence) -> str:
        value = item.envelope
        dedupe_started = monotonic()
        stable_ids = (item.event_id, item.evidence_id)
        if item.envelope.get("captureState") == "captured":
            for deletion in self._deletions.values():
                if deletion.run_id != item.run_id:
                    continue
                if deletion.status == "in_progress":
                    raise RunEvidenceDeletionInProgress()
                if deletion.status == "completed":
                    raise RunEvidenceDeletionClosed()
        for stable_id in stable_ids:
            prior = self._by_stable.get(stable_id)
            if prior is not None:
                if prior != item.fingerprint:
                    raise DuplicateCollision()
                self._last_dedupe_duration_ms = max(0.001, (monotonic() - dedupe_started) * 1000.0)
                return "duplicate"
        if item.evidence_id in self.items:
            prior = self.items[item.evidence_id]
            if prior[2] != item.fingerprint:
                raise DuplicateCollision()
            self._last_dedupe_duration_ms = max(0.001, (monotonic() - dedupe_started) * 1000.0)
            return "duplicate"
        manifest = RunEvidenceManifest(item.evidence_id, item.event_id, item.run_id, cast(str, value["traceId"]), cast(str, value["spanId"]), cast(str | None, value.get("parentSpanId")), cast(int, value["sequence"]), cast(str, value["stepCategory"]), cast(str, value["evidenceKind"]), cast(str, value["captureState"]), cast(str, value["mediaClassification"]), cast(str, value["mediaType"]), cast(str | None, value.get("digest")), cast(int, value["byteCount"]), cast(str, value["observedAt"]), cast(str | None, value.get("reason")))
        self.items[item.evidence_id] = (manifest, item.canonical_bytes, item.fingerprint, dict(value))
        self._by_stable[item.event_id] = item.fingerprint
        self._by_stable[item.evidence_id] = item.fingerprint
        self._last_dedupe_duration_ms = max(0.001, (monotonic() - dedupe_started) * 1000.0)
        event = {"id": str(uuid4()), "type": "observation.changed", "runId": item.run_id, "data": {"resourceType": "run", "resourceId": item.run_id, "capturePolicy": "metadata_only"}}
        self.events.append(event)
        del self.events[:-10000]
        return "accepted"

    async def manifests(self, run_id: str, *, cursor: str | None = None, limit: int = MAX_PAGE_SIZE) -> tuple[list[RunEvidenceManifest], str | None]:
        limit = max(1, min(MAX_PAGE_SIZE, int(limit)))
        decoded = _decode_cursor(cursor)
        values = sorted((row[0] for row in self.items.values() if row[0].run_id == run_id), key=lambda item: (item.sequence, item.captured_at, item.evidence_id))
        if isinstance(decoded, tuple):
            start = next(
                (index for index, value in enumerate(values) if (value.sequence, value.captured_at, value.evidence_id) > decoded),
                len(values),
            )
        else:
            start = decoded
        page = values[start : start + limit]
        next_cursor = _encode_cursor((page[-1].sequence, page[-1].captured_at, page[-1].evidence_id)) if start + limit < len(values) and page else None
        return page, next_cursor

    async def summary(self, run_id: str) -> Mapping[str, object]:
        counts: dict[str, int] = defaultdict(int)
        reasons: set[str] = set()
        for row in self.items.values():
            manifest = row[0]
            if manifest.run_id != run_id:
                continue
            counts[manifest.capture_state] += 1
            if manifest.reason:
                reasons.add(manifest.reason)
        deletion_status = None
        for deletion in self._deletions.values():
            if deletion.run_id == run_id:
                if deletion.status == "completed":
                    deletion_status = "completed"
                elif deletion_status is None:
                    deletion_status = deletion.status
        return {"total": sum(counts.values()), "counts": dict(counts), "reasons": sorted(reasons), "deletionStatus": deletion_status}

    async def inspection_page(self, run_id: str, *, cursor: str | None = None, limit: int = MAX_PAGE_SIZE) -> tuple[list[RunEvidenceManifest], str | None, Mapping[str, object]]:
        manifests, next_cursor = await self.manifests(run_id, cursor=cursor, limit=limit)
        return manifests, next_cursor, await self.summary(run_id)

    async def stream(self, run_id: str, evidence_id: str) -> tuple[RunEvidenceManifest, AsyncIterator[bytes]]:
        row = self.items.get(evidence_id)
        if row is None or row[0].run_id != run_id:
            raise KeyError("evidence not found")
        manifest, data, _fingerprint, _envelope = row
        if manifest.capture_state == "deleted":
            raise FileNotFoundError("evidence deleted")
        async def chunks() -> AsyncIterator[bytes]:
            if len(data) != manifest.byte_count or "sha256:" + hashlib.sha256(data).hexdigest() != manifest.digest:
                raise ValueError("storage integrity failure")
            for offset in range(0, len(data), 64 * 1024):
                yield data[offset : offset + 64 * 1024]
        return manifest, chunks()

    async def delete(self, run_id: str, idempotency_key: str) -> RunEvidenceDeletion:
        key = (run_id, idempotency_key)
        prior = self._deletions.get(key)
        if prior is not None:
            return prior
        run_items = [key_id for key_id, row in self.items.items() if row[0].run_id == run_id]
        now = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        deletion_id = str(uuid4())
        self.events.append({"id": str(uuid4()), "type": "observation.changed", "runId": run_id, "data": {"resourceType": "run", "resourceId": run_id, "capturePolicy": "metadata_only"}})
        for evidence_id in run_items:
            manifest, _data, _fingerprint, envelope = self.items[evidence_id]
            deleted = RunEvidenceManifest(manifest.evidence_id, manifest.event_id, manifest.run_id, manifest.trace_id, manifest.span_id, manifest.parent_span_id, manifest.sequence, manifest.category, manifest.kind, "deleted", manifest.media_classification, manifest.media_type, None, 0, manifest.captured_at, "manually_deleted")
            updated = dict(envelope)
            updated["captureState"] = "deleted"
            updated["digest"] = None
            updated["byteCount"] = 0
            updated["reason"] = "manually_deleted"
            updated["payload"] = None
            self.items[evidence_id] = (deleted, b"", None, updated)
            self._by_stable.pop(manifest.event_id, None)
            self._by_stable.pop(manifest.evidence_id, None)
        result = RunEvidenceDeletion(deletion_id, run_id, "completed", now, now, len(run_items))
        self._deletions[key] = result
        self.events.append({"id": str(uuid4()), "type": "observation.changed", "runId": run_id, "data": {"resourceType": "run", "resourceId": run_id, "capturePolicy": "metadata_only"}})
        return result

    async def events_after(self, last_event_id: str | None = None) -> list[dict[str, object]]:
        if last_event_id is None:
            return list(self.events)
        for index, event in enumerate(self.events):
            if event.get("id") == last_event_id:
                return self.events[index + 1 :]
        # An unknown cursor is not a licence to replay another source's
        # history.  The API uses this distinction to keep run-evidence and
        # telemetry cursors independent.
        return []

    async def has_event(self, event_id: str) -> bool:
        return any(event.get("id") == event_id for event in self.events)

    async def latest_event_id(self) -> str | None:
        return str(self.events[-1]["id"]) if self.events else None

    async def record_rejection(self, payload: bytes | Mapping[str, object], reason: str) -> None:
        wire_size = len(payload) if isinstance(payload, (bytes, bytearray)) else 0
        self.rejections.append({"id": str(uuid4()), "reason": reason if reason in _REASONS else "ingest_rejected", "byteCount": min(MAX_ENVELOPE_BYTES, wire_size), "occurredAt": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")})


def _encode_cursor(value: int | tuple[int, str, str]) -> str:
    """Encode an opaque keyset cursor, retaining legacy offset decoding."""

    if isinstance(value, tuple):
        raw = json.dumps(list(value), separators=(",", ":")).encode("utf-8")
        return "rei_k_" + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return f"rei_{max(0, value):x}"


def _decode_cursor(cursor: str | None) -> int | tuple[int, str, str]:
    if cursor is None:
        return 0
    if isinstance(cursor, str) and cursor.startswith("rei_k_"):
        encoded = cursor[6:]
        try:
            padding = "=" * (-len(encoded) % 4)
            value = json.loads(base64.urlsafe_b64decode(encoded + padding))
        except (ValueError, TypeError, json.JSONDecodeError, binascii.Error) as exc:
            raise ValueError("invalid evidence cursor") from exc
        if (
            not isinstance(value, list)
            or len(value) != 3
            or type(value[0]) is not int
            or value[0] < 0
            or not isinstance(value[1], str)
            or not isinstance(value[2], str)
            or not _UUID_RE.fullmatch(value[2])
        ):
            raise ValueError("invalid evidence cursor")
        return value[0], value[1], value[2]
    if not isinstance(cursor, str) or not re.fullmatch(r"rei_[0-9a-f]{1,8}", cursor):
        raise ValueError("invalid evidence cursor")
    return int(cursor[4:], 16)


class RunEvidenceIngestionService:
    def __init__(self, repository: RunEvidenceRepository, *, metrics: Any | None = None) -> None:
        self.repository = repository
        self.metrics = metrics

    async def ingest(self, payload: bytes | Mapping[str, object]) -> str:
        validation_started = monotonic()
        try:
            item = validate_run_evidence(payload)
        except RunEvidenceError:
            validation_duration = (monotonic() - validation_started) * 1000.0
            self._metric("run_evidence_ingest_validation_latency_ms", validation_duration)
            _record_component_span(self.metrics, component_id="aura.observatory.run_evidence_ingestion", operation="run_evidence.ingest_validate", duration_ms=validation_duration, outcome="rejected", error_code="ingest_rejected")
            record_rejection = getattr(self.repository, "record_rejection", None)
            if callable(record_rejection):
                await record_rejection(payload, "ingest_rejected")
            self._metric("run_evidence_ingest_outcome_count", labels={"outcome": "rejected"})
            raise
        except Exception:
            self._metric("run_evidence_ingest_validation_latency_ms", (monotonic() - validation_started) * 1000.0)
            raise
        validation_duration = (monotonic() - validation_started) * 1000.0
        self._metric("run_evidence_ingest_validation_latency_ms", validation_duration)
        _record_component_span(self.metrics, component_id="aura.observatory.run_evidence_ingestion", operation="run_evidence.ingest_validate", duration_ms=validation_duration, outcome="success", trace_id=cast(str, item.envelope["traceId"]), parent_span_id=cast(str | None, item.envelope.get("parentSpanId")), run_id=item.run_id)
        try:
            trace_id = cast(str, item.envelope["traceId"])
            parent_span_id = cast(str | None, item.envelope.get("parentSpanId"))
            run_id = item.run_id
            native_storage = hasattr(self.repository, "_metrics")
            dedupe_started = monotonic()
            try:
                outcome = await self.repository.ingest(item)
            except RunEvidenceError as exc:
                if not hasattr(self.repository, "_metrics"):
                    _record_component_span(self.metrics, component_id="aura.observatory.run_evidence_storage", operation="run_evidence.manifest_write", duration_ms=(monotonic() - dedupe_started) * 1000.0, outcome="rejected", dependency="postgresql", error_code="duplicate_collision" if exc.code == "duplicate_collision" else "ingest_rejected", trace_id=trace_id, parent_span_id=parent_span_id, run_id=run_id)
                self._metric("run_evidence_ingest_outcome_count", labels={"outcome": "rejected"})
                self._metric("run_evidence_storage_outcome_count", labels={"outcome": "rejected"})
                raise
            except Exception:
                if not hasattr(self.repository, "_metrics"):
                    _record_component_span(self.metrics, component_id="aura.observatory.run_evidence_storage", operation="run_evidence.manifest_write", duration_ms=(monotonic() - dedupe_started) * 1000.0, outcome="failure", dependency="postgresql", error_code="manifest_store_unavailable", trace_id=trace_id, parent_span_id=parent_span_id, run_id=run_id)
                self._metric("run_evidence_ingest_outcome_count", labels={"outcome": "failure"})
                self._metric("run_evidence_storage_outcome_count", labels={"outcome": "failure"})
                raise
            self._metric("run_evidence_ingest_outcome_count", labels={"outcome": outcome})
            self._metric("run_evidence_storage_outcome_count", labels={"outcome": outcome})
            if not native_storage:
                _record_component_span(self.metrics, component_id="aura.observatory.run_evidence_storage", operation="run_evidence.manifest_write", duration_ms=(monotonic() - dedupe_started) * 1000.0, outcome="success", dependency="postgresql", trace_id=trace_id, parent_span_id=parent_span_id, run_id=run_id)
            if item.envelope.get("captureState") == "captured" and not native_storage:
                _record_component_span(self.metrics, component_id="aura.observatory.run_evidence_storage", operation="run_evidence.object_write", duration_ms=(monotonic() - dedupe_started) * 1000.0, outcome="success", dependency="garage", trace_id=trace_id, parent_span_id=parent_span_id, run_id=run_id)
            dedupe_duration = float(getattr(self.repository, "_last_dedupe_duration_ms", (monotonic() - dedupe_started) * 1000.0))
            self._metric("run_evidence_ingest_dedupe_latency_ms", dedupe_duration)
            if not native_storage:
                self._metric("run_evidence_manifest_storage_latency_ms", dedupe_duration)
            if item.envelope.get("captureState") == "captured" and not native_storage:
                self._metric("run_evidence_object_storage_latency_ms", dedupe_duration)
            _record_component_span(self.metrics, component_id="aura.observatory.run_evidence_ingestion", operation="run_evidence.ingest_deduplicate", duration_ms=dedupe_duration, outcome="duplicate" if outcome == "duplicate" else "success", dependency="postgresql", trace_id=trace_id, parent_span_id=parent_span_id, run_id=run_id)
            return outcome
        except Exception:
            raise

    async def ingest_envelope(self, payload: bytes | Mapping[str, object]) -> str:
        return await self.ingest(payload)

    async def record_rejection(self, payload: bytes | Mapping[str, object], reason: str) -> None:
        recorder = getattr(self.repository, "record_rejection", None)
        if callable(recorder):
            await recorder(payload, reason)

    def _metric(self, name: str, value: float = 1.0, labels: Mapping[str, str] | None = None) -> None:
        record = getattr(self.metrics, "record", None)
        if callable(record):
            try:
                record(name, value, labels=dict(labels or {}))
            except (TypeError, ValueError):
                return


class RunInspectionService:
    def __init__(self, repository: RunEvidenceRepository, *, query: Any | None = None, metrics: Any | None = None) -> None:
        self.repository = repository
        self.query = query
        self.metrics = metrics
        self.last_evaluator_results: Mapping[str, str] = {}

    async def inspect(self, run_id: str, *, cursor: str | None = None, page_size: int = 250) -> dict[str, object] | None:
        _uuid(run_id, "run_id")
        query_started = monotonic()
        # A step can expose at most 256 manifests by contract.  Keep the
        # service page bounded to that same ceiling so a large same-span run
        # always receives a continuation cursor instead of silently dropping
        # evidence after the step cap.
        page_fn = getattr(self.repository, "inspection_page", None)
        if callable(page_fn):
            manifests, next_cursor, aggregate = await page_fn(
                run_id, cursor=cursor, limit=min(256, max(1, int(page_size)))
            )
        else:
            manifests, next_cursor = await self.repository.manifests(
                run_id, cursor=cursor, limit=min(256, max(1, int(page_size)))
            )
            summary_fn = getattr(self.repository, "summary", None)
            aggregate = await summary_fn(run_id) if callable(summary_fn) else None
        _record_component_span(self.metrics, component_id="aura.observatory.run_inspection_query", operation="run_inspection.manifest_query", duration_ms=(monotonic() - query_started) * 1000.0, outcome="success", dependency="postgresql", trace_id=manifests[0].trace_id if manifests else None, parent_span_id=manifests[0].parent_span_id if manifests else None, run_id=run_id)
        record = getattr(self.metrics, "record", None)
        if callable(record):
            try:
                record("run_inspection_query_latency_ms", (monotonic() - query_started) * 1000.0)
            except (TypeError, ValueError):
                pass
        metadata: Mapping[str, object] | None = None
        metadata_unavailable = False
        metadata_started = monotonic()
        if self.query is not None:
            try:
                metadata = await self.query.run_detail(run_id, limit=10000)
            except Exception:
                metadata = None
                metadata_unavailable = True
        metadata_duration = (monotonic() - metadata_started) * 1000.0
        if metadata is None and not manifests and not metadata_unavailable:
            return None
        merge_started = monotonic()
        projection = _inspection_projection(
            run_id,
            metadata,
            manifests,
            next_cursor,
            metadata_unavailable=metadata_unavailable,
            aggregate=aggregate,
        )
        self.last_evaluator_results = evaluate_run_evidence_projection(projection)
        merge_duration = (monotonic() - merge_started) * 1000.0
        record = getattr(self.metrics, "record", None)
        if callable(record):
            try:
                record("run_inspection_merge_latency_ms", merge_duration)
            except (TypeError, ValueError):
                pass
        inspection_trace = cast(str | None, metadata.get("traceId")) if metadata else (manifests[0].trace_id if manifests else None)
        inspection_parent = manifests[0].parent_span_id if manifests else None
        _record_component_span(self.metrics, component_id="aura.observatory.run_inspection_query", operation="run_inspection.metadata_query", duration_ms=metadata_duration, outcome="success" if metadata is not None or manifests else "partial", dependency="clickhouse" if self.query is not None else "postgresql", trace_id=inspection_trace, parent_span_id=inspection_parent, run_id=run_id)
        _record_component_span(self.metrics, component_id="aura.observatory.run_inspection_query", operation="run_inspection.merge", duration_ms=merge_duration, outcome="success" if metadata is not None or manifests else "partial", dependency="postgresql", trace_id=inspection_trace, parent_span_id=inspection_parent, run_id=run_id)
        return projection

    async def inspect_run(self, run_id: str, *, cursor: str | None = None, page_size: int = 250) -> dict[str, object] | None:
        return await self.inspect(run_id, cursor=cursor, page_size=page_size)


class RunEvidenceDeletionService:
    """Owner-facing application port for durable, idempotent deletion."""

    def __init__(self, repository: RunEvidenceRepository, *, metrics: Any | None = None) -> None:
        self.repository = repository
        self.metrics = metrics

    async def delete(self, run_id: str, idempotency_key: str) -> RunEvidenceDeletion:
        _uuid(run_id, "run_id")
        _uuid(idempotency_key, "idempotency_key")
        started = monotonic()
        trace_id, parent_span_id = await self._correlation(run_id)
        native_storage = hasattr(self.repository, "_metrics")
        try:
            result = await self.repository.delete(run_id, idempotency_key)
            if result.status == "completed" and not native_storage:
                _record_component_span(self.metrics, component_id="aura.observatory.run_evidence_storage", operation="run_evidence.object_delete", duration_ms=(monotonic() - started) * 1000.0, outcome="success", dependency="garage", trace_id=trace_id, parent_span_id=parent_span_id, run_id=run_id)
            if not native_storage:
                _record_component_span(self.metrics, component_id="aura.observatory.run_evidence_storage", operation="run_evidence.deletion_finalize", duration_ms=(monotonic() - started) * 1000.0, outcome="success" if result.status == "completed" else "failure", dependency="garage" if result.status == "completed" else "postgresql", error_code="deletion_failed" if result.failure_code else None, trace_id=trace_id, parent_span_id=parent_span_id, run_id=run_id)
            record = getattr(self.metrics, "record", None)
            if callable(record):
                try:
                    record("run_evidence_deletion_outcome_count", labels={"outcome": result.status})
                except (TypeError, ValueError):
                    pass
            return result
        except Exception:
            if not native_storage:
                _record_component_span(self.metrics, component_id="aura.observatory.run_evidence_storage", operation="run_evidence.deletion_finalize", duration_ms=(monotonic() - started) * 1000.0, outcome="failure", dependency="postgresql", error_code="manifest_store_unavailable", trace_id=trace_id, parent_span_id=parent_span_id, run_id=run_id)
            record = getattr(self.metrics, "record", None)
            if callable(record):
                try:
                    record("run_evidence_deletion_outcome_count", labels={"outcome": "failure"})
                except (TypeError, ValueError):
                    pass
            raise
        finally:
            record = getattr(self.metrics, "record", None)
            if callable(record):
                try:
                    record("run_evidence_deletion_latency_ms", (monotonic() - started) * 1000.0)
                except (TypeError, ValueError):
                    pass

    async def _correlation(self, run_id: str) -> tuple[str | None, str | None]:
        """Reuse the run's source correlation without making it required."""

        manifests = getattr(self.repository, "manifests", None)
        if callable(manifests):
            try:
                values, _ = await manifests(run_id, limit=1)
                if values:
                    return values[0].trace_id, values[0].parent_span_id
            except Exception:
                pass
        return None, None

    async def delete_run(self, run_id: str, idempotency_key: str) -> RunEvidenceDeletion:
        return await self.delete(run_id, idempotency_key)


def _inspection_projection(
    run_id: str,
    metadata: Mapping[str, object] | None,
    manifests: Sequence[RunEvidenceManifest],
    next_cursor: str | None,
    *,
    metadata_unavailable: bool = False,
    aggregate: Mapping[str, object] | None = None,
) -> dict[str, object]:
    now = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    observations = None
    if metadata is not None:
        try:
            # The in-memory query compatibility projection exposes spans via
            # an exact virtual accessor while keeping them out of its public
            # serialized mapping; production adapters may provide them as a
            # normal field.
            observations = metadata["spans"]
        except (KeyError, TypeError):
            observations = metadata.get("spans")
    if not isinstance(observations, list):
        observations = metadata.get("observations", []) if metadata else []
    rows = _flatten_span_rows(observations)
    started = cast(str | None, metadata.get("startedAt")) if metadata else None
    finished = cast(str | None, metadata.get("finishedAt")) if metadata else None
    status = cast(str, metadata.get("status", "unknown")) if metadata else "unknown"
    if started is None and manifests:
        started = min(item.captured_at for item in manifests)
    if finished is None and metadata and status in {"succeeded", "failed", "cancelled"}:
        finished = max((item.captured_at for item in manifests), default=None)
    duration = metadata.get("durationMs") if metadata else None
    if duration is None and started and finished:
        try:
            duration = max(0.0, (datetime.fromisoformat(finished.replace("Z", "+00:00")) - datetime.fromisoformat(started.replace("Z", "+00:00"))).total_seconds() * 1000.0)
        except ValueError:
            duration = None
    by_span: dict[str, list[RunEvidenceManifest]] = defaultdict(list)
    for manifest in manifests:
        by_span[manifest.span_id].append(manifest)
    steps: list[dict[str, object]] = []
    for index, row in enumerate(rows):
        span_id = row.get("spanId") or row.get("span_id")
        if not isinstance(span_id, str):
            continue
        started_at = row.get("startedAt") or row.get("observedAt") or now
        if not isinstance(started_at, str):
            started_at = now
        step_id = str(uuid5(_STEP_NAMESPACE, run_id + ":" + span_id))
        evidence = by_span.pop(span_id, [])
        evidence.sort(key=lambda item: (item.sequence, item.evidence_id))
        evidence_page_truncated = len(evidence) > 256
        step_reasons = {item.reason for item in evidence if item.reason}
        if evidence_page_truncated:
            step_reasons.add("page_truncated")
        step = {"stepId": step_id, "parentStepId": None, "spanId": span_id, "parentSpanId": row.get("parentSpanId"), "sequence": index, "category": _category_from_row(row), "label": str(row.get("operation") or "run step")[:128], "state": _step_state(row), "startedAt": started_at, "finishedAt": row.get("finishedAt"), "durationMs": row.get("durationMs"), "depth": int(row.get("depth", 0)) if isinstance(row.get("depth", 0), int) else 0, "measurements": _measurements(row), "evidence": [item.as_dict() for item in evidence[:256]], "partial": evidence_page_truncated or any(item.capture_state in {"partial", "dropped", "deleted"} for item in evidence), "partialReasons": sorted(step_reasons)}
        steps.append(step)
    for span_id, values in by_span.items():
        first = min(values, key=lambda item: (item.sequence, item.evidence_id))
        values.sort(key=lambda item: (item.sequence, item.evidence_id))
        page_truncated = len(values) > 256
        value_reasons = {item.reason for item in values if item.reason}
        if page_truncated:
            value_reasons.add("page_truncated")
        steps.append({"stepId": str(uuid5(_STEP_NAMESPACE, run_id + ":" + span_id)), "parentStepId": None, "spanId": span_id, "parentSpanId": first.parent_span_id, "sequence": first.sequence, "category": first.category, "label": first.kind[:128], "state": "succeeded", "startedAt": first.captured_at, "finishedAt": first.captured_at, "durationMs": 0, "depth": 0, "measurements": [], "evidence": [item.as_dict() for item in values[:256]], "partial": page_truncated or any(item.capture_state in {"partial", "dropped", "deleted"} for item in values), "partialReasons": sorted(value_reasons)})
    step_by_span = {cast(str, item["spanId"]): item for item in steps if isinstance(item.get("spanId"), str)}
    for step in steps:
        parent_span = step.get("parentSpanId")
        if isinstance(parent_span, str):
            parent = step_by_span.get(parent_span)
            if parent is not None:
                step["parentStepId"] = parent["stepId"]
            else:
                step["partial"] = True
                reasons = set(cast(list[str], step.get("partialReasons", [])))
                reasons.add("missing_parent")
                step["partialReasons"] = sorted(reasons)
    steps.sort(key=lambda item: (cast(int, item["sequence"]), cast(str, item["startedAt"]), cast(str, item["stepId"])))
    counts = {"captured": 0, "excluded": 0, "partial": 0, "dropped": 0, "deleted": 0}
    reasons: set[str] = set()
    for item in manifests:
        counts[item.capture_state] = counts.get(item.capture_state, 0) + 1
        if item.reason:
            reasons.add(item.reason)
    if aggregate is not None:
        aggregate_counts = aggregate.get("counts")
        if isinstance(aggregate_counts, Mapping):
            for name in counts:
                value = aggregate_counts.get(name)
                if isinstance(value, int):
                    counts[name] = value
        aggregate_reasons = aggregate.get("reasons")
        if isinstance(aggregate_reasons, list):
            reasons.update(str(value) for value in aggregate_reasons if str(value) in _REASONS)
        if aggregate.get("deletionStatus") in {"in_progress", "failed"}:
            reasons.add("storage_failure")
    for step in steps:
        step_reasons = step.get("partialReasons")
        if isinstance(step_reasons, list):
            reasons.update(str(reason) for reason in step_reasons if str(reason) in _REASONS or str(reason) in {"page_truncated", "missing_parent"})
    if len(steps) > 1000:
        reasons.add("page_truncated")
    if next_cursor:
        reasons.add("page_truncated")
    total_count = sum(counts.values())
    state = "deleted" if counts["deleted"] and counts["deleted"] == total_count else "partial" if reasons or next_cursor else "complete"
    if not manifests and metadata is None:
        state = "unavailable"
        reasons.add("unknown_capture_gap")
    elif not manifests and metadata is not None:
        # A metadata-only run is useful, but cannot claim content capture was
        # complete merely because the trace query succeeded.
        state = "partial"
        reasons.add("unknown_capture_gap")
    if metadata_unavailable:
        state = "unavailable" if not manifests else "partial"
        reasons.add("unknown_capture_gap")
    completeness = {"state": state, "expectedEvidenceCount": sum(counts.values()), "capturedEvidenceCount": counts["captured"], "excludedEvidenceCount": counts["excluded"], "partialEvidenceCount": counts["partial"], "droppedEvidenceCount": counts["dropped"], "deletedEvidenceCount": counts["deleted"], "reasons": sorted(reasons)}
    return {"schemaVersion": 1, "runId": run_id, "traceId": metadata.get("traceId") if metadata else (manifests[0].trace_id if manifests else None), "status": status, "startedAt": started or now, "finishedAt": finished, "durationMs": duration, "steps": steps[:1000], "completeness": completeness, "snapshotAt": now, "partial": state != "complete", "partialReasons": sorted(reasons), "nextCursor": next_cursor}


def evaluate_run_evidence_projection(projection: Mapping[str, object]) -> Mapping[str, str]:
    """Execute bounded content-free checks declared by run-evidence components."""

    completeness = projection.get("completeness")
    valid_shape = isinstance(completeness, Mapping) and isinstance(projection.get("steps"), list)
    forbidden = {"payload", "objectkey", "object_key", "prompt", "response", "arguments", "result", "credential", "password"}

    def metadata_only(value: object) -> bool:
        if isinstance(value, Mapping):
            return all(str(key).lower() not in forbidden and metadata_only(child) for key, child in value.items())
        if isinstance(value, list):
            return all(metadata_only(child) for child in value)
        return True

    safe = metadata_only(projection)
    state = completeness.get("state") if isinstance(completeness, Mapping) else None
    truthful = state in {"complete", "partial", "deleted", "unavailable"} and (not bool(projection.get("partial")) or state != "complete")
    shape_outcome = "pass" if valid_shape else "inconclusive"
    return {
        "run_evidence_ingestion_integrity": shape_outcome,
        "run_evidence_schema_compatibility": shape_outcome,
        "run_evidence_failure_isolation": shape_outcome,
        "run_inspection_projection_truth": "pass" if truthful else "inconclusive",
        "run_evidence_deletion_recovery": "pass" if state != "deleting" else "inconclusive",
        "observatory_run_evidence_privacy": "pass" if safe else "fail",
        "metadata_v2_allowlist": "pass" if safe else "fail",
    }


def _category_from_row(row: Mapping[str, object]) -> str:
    operation = str(row.get("operation", "")).casefold()
    for category in _CATEGORIES:
        if category in operation:
            return category
    return "worker"


def _flatten_span_rows(values: object) -> list[Mapping[str, object]]:
    result: list[Mapping[str, object]] = []
    if not isinstance(values, list):
        return result
    for value in values:
        if not isinstance(value, Mapping):
            continue
        result.append(value)
        children = value.get("children")
        result.extend(_flatten_span_rows(children))
    return result


def _step_state(row: Mapping[str, object]) -> str:
    outcome = str(row.get("outcome") or row.get("status") or "").casefold()
    states = {"succeeded": "succeeded", "success": "succeeded", "complete": "succeeded", "failed": "failed", "error": "failed", "cancelled": "cancelled", "canceled": "cancelled", "rejected": "rejected", "unavailable": "unavailable", "running": "running", "queued": "pending"}
    if outcome in states:
        return states[outcome]
    return "succeeded" if row.get("finishedAt") is not None or row.get("durationMs") is not None else "running"


def _measurements(row: Mapping[str, object]) -> list[dict[str, object]]:
    measurement = row.get("measurement")
    source = measurement if isinstance(measurement, Mapping) else row
    name = source.get("name") or source.get("measurementName") or source.get("measurement_name")
    value = source.get("value") if "value" in source else source.get("measurementValue") or source.get("measurement_value")
    unit = source.get("unit") or source.get("measurementUnit") or source.get("measurement_unit")
    if isinstance(name, str) and isinstance(value, (int, float)) and not isinstance(value, bool) and isinstance(unit, str) and unit in {"count", "milliseconds", "bytes", "tokens", "ratio", "score"}:
        return [{"name": name[:96], "value": value, "unit": unit}]
    return []


__all__ = ["DuplicateCollision", "EvidenceUnavailable", "MAX_ENVELOPE_BYTES", "MAX_EVIDENCE_BYTES", "MemoryRunEvidenceRepository", "MemoryRunEvidenceStore", "RunEvidenceDeletion", "RunEvidenceDeletionService", "RunEvidenceError", "RunEvidenceIngestService", "RunEvidenceIngestionService", "RunEvidenceManifest", "RunEvidenceProblem", "RunEvidenceRepository", "RunInspectionService", "ValidatedRunEvidence", "validate_run_evidence"]

# Compatibility names used by application composition and deterministic tests.
MemoryRunEvidenceStore = MemoryRunEvidenceRepository
RunEvidenceIngestService = RunEvidenceIngestionService
