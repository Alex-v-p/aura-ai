# pyright: reportPrivateUsage=false, reportArgumentType=false
"""Independent acceptance coverage for Core run-evidence capture."""

from __future__ import annotations

import ast
import asyncio
import base64
import hashlib
import importlib.util
import inspect
import json
import struct
import zlib
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import pytest
import yaml
from aura_core.bootstrap import evaluation_runner
from aura_core.domains.execution.runs.dto import Run, RunStatus
from aura_core.domains.execution.runs.service import RunCoordinator
from aura_core.platform import run_evidence
from aura_core.platform.run_evidence import (
    RUN_EVIDENCE_SUBJECT,
    BoundedRunEvidenceRecorder,
    DisabledRunEvidenceRecorder,
    EvidenceCaptureState,
    EvidenceRejected,
    JetStreamRunEvidencePublisher,
    RunEvidenceInput,
    RunEvidencePolicy,
    make_run_evidence_recorder,
    serialize_evidence,
)
from aura_core.platform.telemetry import MetadataMetrics
from aura_core.runtime.models.ports import (
    ModelStreamCompleted,
    ModelTextDelta,
    ModelThinkingDelta,
    ModelToolCall,
)

RUN_ID = UUID("123e4567-e89b-42d3-a456-426614174000")
ROOT = Path(__file__).parents[3]
_CONTRACT_SPEC = importlib.util.spec_from_file_location(
    "aura0117_contract_test",
    ROOT / "contracts/tests/test_aura0117_run_evidence_contract.py",
)
if _CONTRACT_SPEC is None or _CONTRACT_SPEC.loader is None:
    raise RuntimeError("unable to load the authoritative run-evidence schema test")
_CONTRACT_MODULE = importlib.util.module_from_spec(_CONTRACT_SPEC)
_CONTRACT_SPEC.loader.exec_module(_CONTRACT_MODULE)
EVIDENCE_PATH = _CONTRACT_MODULE.EVIDENCE_PATH
_load = _CONTRACT_MODULE._load
_schema_errors = _CONTRACT_MODULE._schema_errors


def _item(
    *,
    sequence: int = 0,
    step_category: str = "generation",
    evidence_kind: str = "generation_output",
    media_classification: str = "text",
    media_type: str = "text/plain",
    span_id: str = "b" * 16,
    parent_span_id: str | None = None,
    observed_at: datetime | None = None,
    payload: dict[str, object] | None = None,
    include_payload: bool = True,
    capture_state: EvidenceCaptureState = EvidenceCaptureState.CAPTURED,
    reason: str | None = None,
    content_admission: dict[str, object] | None = None,
) -> RunEvidenceInput:
    return RunEvidenceInput(
        run_id=RUN_ID,
        trace_id="a" * 32,
        span_id=span_id,
        parent_span_id=parent_span_id,
        sequence=sequence,
        step_category=step_category,
        evidence_kind=evidence_kind,
        media_classification=media_classification,
        media_type=media_type,
        payload=(
            payload if payload is not None else {"kind": evidence_kind, "text": "answer"}
        )
        if include_payload
        else None,
        capture_state=capture_state,
        reason=reason,
        content_admission=content_admission,
        observed_at=observed_at or datetime.now(UTC),
    )


def _text_item(text: str = "answer", **kwargs: Any) -> RunEvidenceInput:
    payload = kwargs.pop("payload", {"kind": "generation_output", "text": text})
    return _item(payload=payload, **kwargs)


class _Metrics:
    def __init__(self) -> None:
        self.increments: list[tuple[str, str, dict[str, str]]] = []
        self.observations: list[tuple[str, str, float, dict[str, str]]] = []

    def increment(self, component: str, name: str, **attributes: str) -> None:
        self.increments.append((component, name, attributes))

    def observe(self, component: str, name: str, value: float, **attributes: str) -> None:
        self.observations.append((component, name, value, attributes))


class _Publisher:
    def __init__(self, *, failures: int = 0, block: bool = False) -> None:
        self.failures = failures
        self.block = block
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.calls: list[tuple[bytes, UUID]] = []
        self.closed = False

    async def publish(self, payload: bytes, *, message_id: UUID) -> None:
        self.calls.append((payload, message_id))
        self.entered.set()
        if self.block:
            await self.release.wait()
        if self.failures:
            self.failures -= 1
            raise RuntimeError("downstream unavailable")

    async def close(self) -> None:
        self.closed = True


def _envelope(serialized: bytes) -> dict[str, Any]:
    return json.loads(serialized)


def test_exact_text_and_structured_canonical_bytes_have_stable_logical_identity() -> None:
    text = _envelope(
        serialize_evidence(_text_item("visible answer"), policy=RunEvidencePolicy(enabled=True))
    )
    assert text["payload"] == {"kind": "generation_output", "text": "visible answer"}
    assert text["byteCount"] == len(b"visible answer")
    assert text["digest"] == "sha256:" + hashlib.sha256(b"visible answer").hexdigest()
    assert text["captureState"] == "captured"
    assert text["reason"] is None

    routing = _item(
        step_category="routing",
        evidence_kind="model_routing",
        media_classification="structured",
        media_type="application/json",
        payload={
            "kind": "model_routing",
            "provider": "ollama",
            "model": "qwen",
            "routeReason": "capacity",
        },
    )
    structured = _envelope(serialize_evidence(routing, policy=RunEvidencePolicy(enabled=True)))
    canonical = (
        b'{"kind":"model_routing","model":"qwen","provider":"ollama",'
        b'"routeReason":"capacity"}'
    )
    assert structured["byteCount"] == len(canonical)
    assert structured["digest"] == "sha256:" + hashlib.sha256(canonical).hexdigest()

    again = _envelope(
        serialize_evidence(_text_item("visible answer"), policy=RunEvidencePolicy(enabled=True))
    )
    assert again["eventId"] == text["eventId"]
    assert again["evidenceId"] == text["evidenceId"]
    assert again["delivery"]["deliveryId"] != text["delivery"]["deliveryId"]


def test_serialized_envelope_validates_against_the_authoritative_contract_schema() -> None:
    schema = _load(EVIDENCE_PATH)
    envelope = _envelope(
        serialize_evidence(
            _item(
                step_category="retrieval",
                evidence_kind="memory_chunk",
                media_classification="structured",
                media_type="application/json",
                payload={
                    "kind": "memory_chunk",
                    "chunk": {
                        "sourceId": "memory",
                        "ordinal": 0,
                        "content": "café 😀",
                        "score": 1e-7,
                    },
                },
            ),
            policy=RunEvidencePolicy(enabled=True),
        )
    )
    assert _schema_errors(envelope, schema, schema) == []


def test_replay_tuple_is_immutable_across_fresh_recorders_and_coordinators() -> None:
    observed_at = datetime(2026, 10, 25, 19, 0, 0, 123000, tzinfo=UTC)
    item = _text_item(
        observed_at=observed_at,
        span_id="b" * 16,
        parent_span_id="c" * 16,
    )
    first = _envelope(serialize_evidence(item, policy=RunEvidencePolicy(enabled=True)))
    second = _envelope(serialize_evidence(item, policy=RunEvidencePolicy(enabled=True)))
    immutable_fields = (
        "schemaVersion",
        "contractId",
        "eventId",
        "evidenceId",
        "runId",
        "traceId",
        "spanId",
        "parentSpanId",
        "sequence",
        "stepCategory",
        "evidenceKind",
        "mediaClassification",
        "mediaType",
        "digest",
        "byteCount",
        "capturePolicyRevision",
        "observedAt",
        "contentAdmission",
        "captureState",
        "reason",
        "payload",
    )
    assert {field: first[field] for field in immutable_fields} == {
        field: second[field] for field in immutable_fields
    }
    assert first["delivery"]["deliveryId"] != second["delivery"]["deliveryId"]

    class Capture:
        def __init__(self) -> None:
            self.items: list[RunEvidenceInput] = []

        def record(self, captured: RunEvidenceInput) -> bool:
            self.items.append(captured)
            return True

    run = Run(
        conversation_id=RUN_ID,
        user_message_id=RUN_ID,
        agent_revision_id=RUN_ID,
        model_policy_revision_id=RUN_ID,
        provider="evaluation",
        model_id="fixture",
        id=RUN_ID,
    )
    first_capture, second_capture = Capture(), Capture()
    for capture in (first_capture, second_capture):
        RunCoordinator(
            cast(Any, None), cast(Any, None), cast(Any, None), run_evidence=cast(Any, capture)
        )._record_evidence(  # noqa: SLF001
            run,
            step_category="admission",
            evidence_kind="admitted_request",
            media_classification="structured",
            media_type="application/json",
            payload={"kind": "admitted_request", "request": {"role": "user", "content": "hi"}},
        )
    assert (first_capture.items[0].sequence, first_capture.items[0].span_id) == (
        second_capture.items[0].sequence,
        second_capture.items[0].span_id,
    )


def test_rfc8785_number_and_unicode_canonicalization_is_stable() -> None:
    tiny = _envelope(
        serialize_evidence(
            _item(
                step_category="retrieval",
                evidence_kind="memory_chunk",
                media_classification="structured",
                media_type="application/json",
                payload={
                    "kind": "memory_chunk",
                    "chunk": {
                        "sourceId": "memory",
                        "ordinal": 0,
                        "content": "é😀",
                        "score": 1e-7,
                    },
                },
            ),
            policy=RunEvidencePolicy(enabled=True),
        )
    )
    tiny_bytes = (
        '{"chunk":{"content":"é😀","ordinal":0,"score":1e-7,"sourceId":"memory"},'
        '"kind":"memory_chunk"}'
    ).encode()
    assert tiny["byteCount"] == len(tiny_bytes)
    assert tiny["digest"] == "sha256:" + hashlib.sha256(tiny_bytes).hexdigest()

    negative_zero = _envelope(
        serialize_evidence(
            _item(
                sequence=1,
                step_category="retrieval",
                evidence_kind="memory_chunk",
                media_classification="structured",
                media_type="application/json",
                payload={
                    "kind": "memory_chunk",
                    "chunk": {
                        "sourceId": "memory",
                        "ordinal": 0,
                        "content": "zero",
                        "score": -0.0,
                    },
                },
            ),
            policy=RunEvidencePolicy(enabled=True),
        )
    )
    zero_bytes = (
        b'{"chunk":{"content":"zero","ordinal":0,"score":0,"sourceId":"memory"},'
        b'"kind":"memory_chunk"}'
    )
    assert negative_zero["byteCount"] == len(zero_bytes)
    assert negative_zero["digest"] == "sha256:" + hashlib.sha256(zero_bytes).hexdigest()


def test_attachment_digest_uses_decoded_bytes_and_visible_thinking_is_the_only_thinking_kind(
) -> None:
    attachment = _item(
        step_category="context",
        evidence_kind="model_visible_attachment",
        media_classification="text",
        media_type="text/plain",
        payload={
            "kind": "model_visible_attachment",
            "attachmentId": "attachment-1",
            "displayName": "notes.txt",
            "contentBase64": base64.b64encode(b"attachment text").decode(),
        },
        content_admission={
            "outcome": "passed",
            "scannerRevision": "scanner-v1",
            "policyRevision": "policy-v1",
            "inspectedAt": "2026-10-25T19:00:00Z",
            "inspectionModes": ["decoded_text", "container_structure"],
        },
    )
    envelope = _envelope(serialize_evidence(attachment, policy=RunEvidencePolicy(enabled=True)))
    assert envelope["byteCount"] == len(b"attachment text")
    assert envelope["digest"] == "sha256:" + hashlib.sha256(b"attachment text").hexdigest()

    thinking = _item(
        evidence_kind="displayed_thinking",
        step_category="thinking",
        payload={"kind": "displayed_thinking", "text": "Displayed progress"},
    )
    thinking_envelope = _envelope(
        serialize_evidence(thinking, policy=RunEvidencePolicy(enabled=True))
    )
    assert thinking_envelope["payload"]["kind"] == "displayed_thinking"

    with pytest.raises((ValueError, EvidenceRejected)):
        serialize_evidence(
            _item(
                evidence_kind="provider_hidden_reasoning",
                payload={"kind": "provider_hidden_reasoning", "text": "hidden"},
            ),
            policy=RunEvidencePolicy(enabled=True),
        )


def test_every_authorized_evidence_kind_has_a_closed_boundary_serializer() -> None:
    text_cases = {
        "compiled_prompt": {"kind": "compiled_prompt", "prompt": "system"},
        "displayed_thinking": {"kind": "displayed_thinking", "text": "visible"},
        "generation_output": {"kind": "generation_output", "text": "answer"},
        "response_synthesis": {"kind": "response_synthesis", "text": "answer"},
    }
    structured_cases = {
        "admitted_request": {
            "kind": "admitted_request",
            "request": {"role": "user", "content": "hi"},
        },
        "admitted_trigger": {"kind": "admitted_trigger", "triggerType": "chat", "content": "hi"},
        "conversation_context": {
            "kind": "conversation_context",
            "messages": [{"role": "user", "content": "hi"}],
        },
        "memory_chunk": {
            "kind": "memory_chunk",
            "chunk": {"sourceId": "memory", "ordinal": 0, "content": "hi", "score": 0.9},
        },
        "document_chunk": {
            "kind": "document_chunk",
            "chunk": {"sourceId": "document", "ordinal": 0, "content": "hi", "score": None},
        },
        "model_routing": {
            "kind": "model_routing",
            "provider": "ollama",
            "model": "qwen",
            "routeReason": "policy",
        },
        "tool_arguments": {"kind": "tool_arguments", "toolName": "search", "content": "{}"},
        "tool_result": {"kind": "tool_result", "toolName": "search", "content": "{}"},
        "policy_decision": {
            "kind": "policy_decision",
            "decision": {"decision": "allowed", "reason": "policy"},
        },
        "approval_decision": {
            "kind": "approval_decision",
            "decision": {"decision": "approved", "reason": "owner"},
        },
        "execution_outcome": {"kind": "execution_outcome", "outcome": "succeeded", "detail": "ok"},
        "persistence_record": {
            "kind": "persistence_record",
            "record": {"name": "message", "content": "saved"},
        },
        "delivery_record": {
            "kind": "delivery_record",
            "record": {"name": "response", "content": "sent"},
        },
    }
    for prefix in ("worker", "automation", "schedule", "evaluation"):
        for suffix in ("input", "output"):
            kind = f"{prefix}_{suffix}"
            structured_cases[kind] = {
                "kind": kind,
                "content": {"name": kind, "content": "bounded"},
            }

    categories = {
        "admitted_request": "admission",
        "admitted_trigger": "admission",
        "conversation_context": "context",
        "memory_chunk": "retrieval",
        "document_chunk": "retrieval",
        "model_routing": "routing",
        "tool_arguments": "tool",
        "tool_result": "tool",
        "policy_decision": "policy",
        "approval_decision": "policy",
        "execution_outcome": "tool",
        "persistence_record": "persistence",
        "delivery_record": "delivery",
        **{
            f"{prefix}_{suffix}": prefix
            for prefix in ("worker", "automation", "schedule", "evaluation")
            for suffix in ("input", "output")
        },
    }

    for index, (kind, payload) in enumerate(text_cases.items()):
        envelope = _envelope(
            serialize_evidence(
                _item(
                    sequence=index,
                    step_category={
                        "compiled_prompt": "prompt",
                        "displayed_thinking": "thinking",
                        "generation_output": "generation",
                        "response_synthesis": "synthesis",
                    }[kind],
                    evidence_kind=kind,
                    media_classification="text",
                    payload=payload,
                ),
                policy=RunEvidencePolicy(enabled=True),
            )
        )
        assert envelope["evidenceKind"] == kind
        assert envelope["payload"] == payload

    for index, (kind, payload) in enumerate(structured_cases.items(), len(text_cases)):
        envelope = _envelope(
            serialize_evidence(
                _item(
                    sequence=index,
                    step_category=categories[kind],
                    evidence_kind=kind,
                    media_classification="structured",
                    media_type="application/json",
                    payload=payload,
                ),
                policy=RunEvidencePolicy(enabled=True),
            )
        )
        assert envelope["evidenceKind"] == kind
        assert envelope["payload"] == payload

    attachment = _item(
        sequence=len(text_cases) + len(structured_cases),
        step_category="context",
        evidence_kind="model_visible_attachment",
        media_classification="text",
        media_type="text/plain",
        payload={
            "kind": "model_visible_attachment",
            "attachmentId": "attachment-1",
            "displayName": "notes.txt",
            "contentBase64": base64.b64encode(b"attachment text").decode(),
        },
        content_admission={
            "outcome": "passed",
            "scannerRevision": "scanner-v1",
            "policyRevision": "policy-v1",
            "inspectedAt": "2026-10-25T19:00:00Z",
            "inspectionModes": ["decoded_text", "container_structure"],
        },
    )
    assert _envelope(serialize_evidence(attachment, policy=RunEvidencePolicy(enabled=True)))[
        "evidenceKind"
    ] == "model_visible_attachment"


@pytest.mark.parametrize(
    "payload",
    [
        {"kind": "generation_output", "password": "hunter2", "text": "answer"},
        {
            "kind": "generation_output",
            "headers": {"authorization": "Bearer secret-value"},
            "text": "answer",
        },
        {"kind": "generation_output", "nested": {"client_secret": "value"}, "text": "answer"},
    ],
)
def test_secret_bearing_payloads_are_rejected_whole_before_queueing(
    payload: dict[str, object],
) -> None:
    with pytest.raises(EvidenceRejected) as error:
        serialize_evidence(_text_item(payload=payload), policy=RunEvidencePolicy(enabled=True))
    assert error.value.reason == "secret_bearing_field_rejected"


def test_recursive_secret_values_are_rejected_before_closed_shape_validation() -> None:
    payload = {
        "kind": "generation_output",
        "text": "answer",
        "nested": [{"metadata": {"api_token": "nested-secret"}}],
    }
    with pytest.raises(EvidenceRejected) as error:
        serialize_evidence(_text_item(payload=payload), policy=RunEvidencePolicy(enabled=True))
    assert error.value.reason == "secret_bearing_field_rejected"


@pytest.mark.parametrize(
    "secret_payload",
    [
        {"kind": "generation_output", "text": "answer", "authorization": "Basic dXNlcjpwYXNz"},
        {"kind": "generation_output", "text": "answer", "cookie": "session=private"},
        {"kind": "generation_output", "text": "answer", "credentials": {"value": "private"}},
    ],
)
def test_basic_cookie_and_credential_fields_are_rejected_before_payload_admission(
    secret_payload: dict[str, object],
) -> None:
    with pytest.raises((EvidenceRejected, ValueError)) as error:
        serialize_evidence(
            _text_item(payload=secret_payload), policy=RunEvidencePolicy(enabled=True)
        )
    if isinstance(error.value, EvidenceRejected):
        assert error.value.reason == "secret_bearing_field_rejected"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("scannerRevision", "Basic dXNlcjpwYXNz"),
        ("policyRevision", "session_id=opaque"),
        ("scannerRevision", "private_key: opaque"),
        ("policyRevision", "authorization=Basic dXNlcjpwYXNz"),
    ],
)
def test_secret_material_in_admission_revisions_is_rejected_without_capture(
    field: str, value: str
) -> None:
    admission = {
        "outcome": "passed",
        "scannerRevision": "scanner-v1",
        "policyRevision": "policy-v1",
        "inspectedAt": "2026-10-25T19:00:00Z",
        "inspectionModes": ["decoded_text"],
    }
    admission[field] = value
    with pytest.raises((EvidenceRejected, ValueError)):
        serialize_evidence(
            _text_item(content_admission=admission), policy=RunEvidencePolicy(enabled=True)
        )


@pytest.mark.parametrize("evidence_kind", ["tool_arguments", "tool_result"])
@pytest.mark.parametrize(
    "content",
    [
        '{"password":"opaque"}',
        '{"authorization":"Basic dXNlcjpwYXNz"}',
        '{"session_id":"opaque"}',
        '{"private_key":"opaque"}',
    ],
)
def test_tool_json_secret_keys_are_rejected_inside_closed_content(
    evidence_kind: str, content: str
) -> None:
    with pytest.raises(EvidenceRejected) as error:
        serialize_evidence(
            _item(
                step_category="tool",
                evidence_kind=evidence_kind,
                media_classification="structured",
                media_type="application/json",
                payload={
                    "kind": evidence_kind,
                    "toolName": "memory.search",
                    "content": content,
                },
            ),
            policy=RunEvidencePolicy(enabled=True),
        )
    assert error.value.reason == "secret_bearing_field_rejected"


@pytest.mark.parametrize("sequence", [True, -1, 2**53, 1.5])
def test_sequence_requires_a_non_bool_safe_integer(sequence: object) -> None:
    with pytest.raises(ValueError, match="safe integer"):
        serialize_evidence(
            _text_item(sequence=cast(Any, sequence)), policy=RunEvidencePolicy(enabled=True)
        )

    accepted = _envelope(
        serialize_evidence(
            _text_item(sequence=2**53 - 1), policy=RunEvidencePolicy(enabled=True)
        )
    )
    assert accepted["sequence"] == 2**53 - 1


@pytest.mark.parametrize("ordinal", [True, -1, 1_000_001, 1.5])
def test_chunk_ordinal_requires_a_non_bool_bounded_integer(ordinal: object) -> None:
    with pytest.raises(ValueError, match="invalid chunk payload"):
        serialize_evidence(
            _item(
                step_category="retrieval",
                evidence_kind="memory_chunk",
                media_classification="structured",
                media_type="application/json",
                payload={
                    "kind": "memory_chunk",
                    "chunk": {
                        "sourceId": "memory",
                        "ordinal": ordinal,
                        "content": "fact",
                        "score": None,
                    },
                },
            ),
            policy=RunEvidencePolicy(enabled=True),
        )

    accepted = _envelope(
        serialize_evidence(
            _item(
                step_category="retrieval",
                evidence_kind="memory_chunk",
                media_classification="structured",
                media_type="application/json",
                payload={
                    "kind": "memory_chunk",
                    "chunk": {
                        "sourceId": "memory",
                        "ordinal": 1_000_000,
                        "content": "fact",
                        "score": None,
                    },
                },
            ),
            policy=RunEvidencePolicy(enabled=True),
        )
    )
    assert accepted["payload"]["chunk"]["ordinal"] == 1_000_000


def _png_with_compressed_text(text: bytes) -> bytes:
    compressed = zlib.compress(text)
    return (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", len(compressed))
        + b"zTXt"
        + compressed
        + b"\x00\x00\x00\x00"
    )


@pytest.mark.parametrize(
    ("media_type", "content"),
    [
        ("image/png", _png_with_compressed_text(b"authorization: Bearer binary-secret-value")),
        ("application/pdf", b"%PDF-1.7\nBearer binary-secret-value"),
    ],
)
def test_binary_media_with_secret_content_is_rejected_as_a_whole(
    media_type: str, content: bytes
) -> None:
    classification = "image" if media_type == "image/png" else "document"
    modes = (
        ["metadata", "container_structure", "ocr"]
        if classification == "image"
        else ["decoded_text", "metadata", "container_structure", "ocr"]
    )
    item = _item(
        step_category="context",
        evidence_kind="model_visible_attachment",
        media_classification=classification,
        media_type=media_type,
        payload={
            "kind": "model_visible_attachment",
            "attachmentId": "binary-secret",
            "displayName": "input",
            "contentBase64": base64.b64encode(content).decode(),
        },
        content_admission={
            "outcome": "passed",
            "scannerRevision": "scanner-v1",
            "policyRevision": "policy-v1",
            "inspectedAt": "2026-10-25T19:00:00Z",
            "inspectionModes": modes,
        },
    )
    with pytest.raises(EvidenceRejected) as error:
        serialize_evidence(item, policy=RunEvidencePolicy(enabled=True))
    assert error.value.reason == "unknown_capture_gap"


@pytest.mark.parametrize(
    ("media_type", "content_base64"),
    [
        ("application/octet-stream", base64.b64encode(b"bytes").decode()),
        ("image/png", "%%%not-base64%%%"),
        ("application/pdf", base64.b64encode(b"not a pdf").decode()),
    ],
)
def test_unsupported_malformed_or_incompletely_inspected_media_is_rejected(
    media_type: str, content_base64: str
) -> None:
    item = _item(
        evidence_kind="model_visible_attachment",
        media_classification="image" if media_type == "image/png" else "document",
        media_type=media_type,
        payload={
            "kind": "model_visible_attachment",
            "attachmentId": "attachment-1",
            "displayName": "input",
            "contentBase64": content_base64,
        },
    )
    with pytest.raises((ValueError, EvidenceRejected)):
        serialize_evidence(item, policy=RunEvidencePolicy(enabled=True))

    incomplete = _item(
        evidence_kind="model_visible_attachment",
        media_classification="image",
        media_type="image/png",
        payload={
            "kind": "model_visible_attachment",
            "attachmentId": "attachment-2",
            "displayName": "input.png",
            "contentBase64": base64.b64encode(b"not png").decode(),
        },
        content_admission={
            "outcome": "passed",
            "scannerRevision": "scanner-v1",
            "policyRevision": "policy-v1",
            "inspectedAt": "2026-10-25T19:00:00Z",
            "inspectionModes": ["metadata"],
        },
    )
    with pytest.raises((ValueError, EvidenceRejected)):
        serialize_evidence(incomplete, policy=RunEvidencePolicy(enabled=True))


def test_payload_limit_rejects_the_complete_item_before_queueing() -> None:
    with pytest.raises(EvidenceRejected) as error:
        serialize_evidence(
            _text_item("0123456789"),
            policy=RunEvidencePolicy(enabled=True, max_bytes=8),
        )
    assert error.value.reason == "payload_limit_exceeded"


def test_preflight_rejects_oversized_decoded_attachment_bytes() -> None:
    content = b"x" * (1_048_576 + 1)
    item = _item(
        step_category="context",
        evidence_kind="model_visible_attachment",
        media_classification="text",
        media_type="text/plain",
        payload={
            "kind": "model_visible_attachment",
            "attachmentId": "oversized",
            "displayName": "large.txt",
            "contentBase64": base64.b64encode(content).decode(),
        },
        content_admission={
            "outcome": "passed",
            "scannerRevision": "scanner-v1",
            "policyRevision": "policy-v1",
            "inspectedAt": "2026-10-25T19:00:00Z",
            "inspectionModes": ["decoded_text", "container_structure"],
        },
    )
    with pytest.raises(EvidenceRejected) as error:
        serialize_evidence(item, policy=RunEvidencePolicy(enabled=True))
    assert error.value.reason == "payload_limit_exceeded"


def test_oversized_raw_attachment_is_rejected_before_base64_decode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    def forbidden_decode(*args: object, **kwargs: object) -> bytes:
        nonlocal called
        called = True
        del args, kwargs
        raise AssertionError("oversized attachment reached base64 decoding")

    monkeypatch.setattr(run_evidence.base64, "b64decode", forbidden_decode)
    item = _item(
        step_category="context",
        evidence_kind="model_visible_attachment",
        media_classification="text",
        media_type="text/plain",
        payload={
            "kind": "model_visible_attachment",
            "attachmentId": "oversized-raw",
            "displayName": "large.txt",
            "contentBase64": "A" * 1_398_105,
        },
        content_admission={
            "outcome": "passed",
            "scannerRevision": "scanner-v1",
            "policyRevision": "policy-v1",
            "inspectedAt": "2026-10-25T19:00:00Z",
            "inspectionModes": ["decoded_text", "container_structure"],
        },
    )
    with pytest.raises(ValueError, match="invalid model attachment payload"):
        serialize_evidence(item, policy=RunEvidencePolicy(enabled=True))
    assert called is False


@pytest.mark.parametrize("outcome", ["succeeded", "failed", "cancelled", "rejected", "timed_out"])
def test_execution_outcome_variants_are_closed_and_schema_valid(outcome: str) -> None:
    envelope = _envelope(
        serialize_evidence(
            _item(
                step_category="tool",
                evidence_kind="execution_outcome",
                media_classification="structured",
                media_type="application/json",
                payload={"kind": "execution_outcome", "outcome": outcome, "detail": "bounded"},
            ),
            policy=RunEvidencePolicy(enabled=True),
        )
    )
    schema = _load(EVIDENCE_PATH)
    assert _schema_errors(envelope, schema, schema) == []


def test_closed_payloads_reject_kind_mismatch_and_arbitrary_object_fields() -> None:
    with pytest.raises(ValueError, match="payload kind"):
        serialize_evidence(
            _text_item(payload={"kind": "displayed_thinking", "text": "wrong"}),
            policy=RunEvidencePolicy(enabled=True),
        )
    with pytest.raises((ValueError, TypeError)):
        serialize_evidence(
            _text_item(
                payload={
                    "kind": "generation_output",
                    "text": "answer",
                    "extra": {"runtime": object()},
                }
            ),
            policy=RunEvidencePolicy(enabled=True),
        )


def test_non_captured_states_are_content_free_and_disabled_capture_is_inert() -> None:
    policy = RunEvidencePolicy(enabled=True)
    for state, reason in (
        (EvidenceCaptureState.EXCLUDED, "provider_hidden_reasoning_excluded"),
        (EvidenceCaptureState.PARTIAL, "unknown_capture_gap"),
        (EvidenceCaptureState.DROPPED, "queue_pressure"),
    ):
        item = _item(capture_state=state, reason=reason, include_payload=False)
        envelope = _envelope(serialize_evidence(item, policy=policy))
        assert "payload" not in envelope
        assert envelope["digest"] is None
        assert envelope["byteCount"] == 0
        assert envelope["reason"] == reason

    settings = SimpleNamespace(
        run_evidence_capture_policy="",
        run_evidence_queue_capacity=1,
        run_evidence_max_attempts=1,
        run_evidence_drain_seconds=0.1,
        AURA_OBSERVATORY_EXPORT_ENABLED=True,
    )
    recorder = make_run_evidence_recorder(settings)
    assert isinstance(recorder, DisabledRunEvidenceRecorder)
    assert recorder.record(_text_item()) is False


def test_policy_bounds_are_safe_and_ordinary_observatory_or_otlp_settings_do_not_enable_capture(
) -> None:
    with pytest.raises(ValueError):
        RunEvidencePolicy(enabled=True, queue_capacity=0)
    with pytest.raises(ValueError):
        RunEvidencePolicy(enabled=True, queue_capacity=4097)
    with pytest.raises(ValueError):
        RunEvidencePolicy(enabled=True, max_attempts=6)
    with pytest.raises(ValueError):
        RunEvidencePolicy(enabled=True, drain_seconds=30.1)
    with pytest.raises(ValueError):
        RunEvidencePolicy(enabled=True, max_bytes=1_048_577)

    settings = SimpleNamespace(
        run_evidence_capture_policy="metadata_only",
        AURA_OBSERVATORY_EXPORT_ENABLED=True,
        OTEL_EXPORTER_OTLP_ENDPOINT="http://collector",
    )
    assert isinstance(make_run_evidence_recorder(settings), DisabledRunEvidenceRecorder)


@pytest.mark.asyncio
async def test_queue_offer_is_bounded_nonblocking_and_pressure_does_not_change_execution() -> None:
    metrics = _Metrics()
    recorder = BoundedRunEvidenceRecorder(
        RunEvidencePolicy(enabled=True, queue_capacity=1), metrics=metrics
    )
    assert recorder.record(_text_item(sequence=0)) is True
    assert recorder.record(_text_item(sequence=1)) is False
    assert recorder.queue_depth == 1
    assert any(
        name == "run_evidence_export_dropped_count" and attrs.get("error_class") == "queue_pressure"
        for _, name, attrs in metrics.increments
    )


def test_per_run_sequences_are_strictly_forward_and_transport_subject_is_dedicated() -> None:
    recorder = BoundedRunEvidenceRecorder(RunEvidencePolicy(enabled=True, queue_capacity=4))
    assert recorder.record(_text_item(sequence=0)) is True
    assert recorder.record(_text_item(sequence=2)) is True
    assert recorder.record(_text_item(sequence=1)) is False

    class JetStream:
        def __init__(self) -> None:
            self.calls: list[tuple[str, bytes, dict[str, str]]] = []

        async def publish(self, subject: str, payload: bytes, *, headers: dict[str, str]) -> None:
            self.calls.append((subject, payload, headers))

    class Connection:
        def __init__(self) -> None:
            self.stream = JetStream()

        def jetstream(self) -> JetStream:
            return self.stream

    connection = Connection()
    publisher = JetStreamRunEvidencePublisher(connection)
    message_id = UUID("123e4567-e89b-42d3-a456-426614174099")
    asyncio.run(publisher.publish(b"evidence", message_id=message_id))
    assert connection.stream.calls == [
        (RUN_EVIDENCE_SUBJECT, b"evidence", {"Nats-Msg-Id": str(message_id)})
    ]
    assert RUN_EVIDENCE_SUBJECT != "aura.runs.execute.v1"


def test_shared_allocator_carries_sequence_across_boundaries_and_rejects_collisions() -> None:
    recorder = BoundedRunEvidenceRecorder(RunEvidencePolicy(enabled=True, queue_capacity=8))
    items: list[RunEvidenceInput] = []
    builders: tuple[Callable[[int], RunEvidenceInput], ...] = (
        lambda sequence: _text_item(sequence=sequence),
        lambda sequence: _item(
            sequence=sequence,
            step_category="thinking",
            evidence_kind="displayed_thinking",
            payload={"kind": "displayed_thinking", "text": "visible"},
        ),
        lambda sequence: _item(
            sequence=sequence,
            step_category="evaluation",
            evidence_kind="evaluation_input",
            media_classification="structured",
            media_type="application/json",
            payload={
                "kind": "evaluation_input",
                "content": {"name": "case", "content": "input"},
            },
        ),
    )
    for builder in builders:
        sequence = recorder.allocate_sequence(RUN_ID)
        item = builder(sequence)
        items.append(item)
        assert recorder.record(item)
    assert [item.sequence for item in items] == [0, 1, 2]
    sequence = recorder.allocate_sequence(RUN_ID)
    assert sequence == 3
    assert recorder.record(
        _item(
            sequence=sequence,
            step_category="evaluation",
            evidence_kind="evaluation_output",
            media_classification="structured",
            media_type="application/json",
            payload={
                "kind": "evaluation_output",
                "content": {"name": "case", "content": "output"},
            },
        )
    )
    assert recorder.record(_text_item(sequence=sequence, text="changed")) is False


@pytest.mark.asyncio
async def test_retry_delivery_identity_preserves_the_actual_first_delivery_id() -> None:
    publisher = _Publisher(failures=1)
    recorder = BoundedRunEvidenceRecorder(
        RunEvidencePolicy(enabled=True, max_attempts=2), publisher=publisher
    )
    assert recorder.record(_text_item()) is True
    await recorder.start()
    await asyncio.wait_for(recorder._queue.join(), timeout=1.0)  # noqa: SLF001
    deliveries = [json.loads(payload)["delivery"] for payload, _ in publisher.calls]
    assert deliveries[0]["kind"] == "initial"
    assert deliveries[0]["attempt"] == 1
    assert deliveries[1]["kind"] == "retry"
    assert deliveries[1]["attempt"] == 2
    assert deliveries[1]["firstDeliveryId"] == deliveries[0]["deliveryId"]
    await recorder.stop()


@pytest.mark.asyncio
async def test_each_export_span_has_unique_id_and_item_parent_lineage() -> None:
    metrics = MetadataMetrics()
    publisher = _Publisher(failures=2)
    recorder = BoundedRunEvidenceRecorder(
        RunEvidencePolicy(enabled=True, max_attempts=3), publisher=publisher, metrics=metrics
    )
    item = _text_item(parent_span_id="c" * 16, span_id="b" * 16)
    assert recorder.record(item) is True
    await recorder.start()
    await asyncio.wait_for(recorder._queue.join(), timeout=1.0)  # noqa: SLF001
    await recorder.stop()

    spans = [measurement for measurement in metrics._measurements if measurement.kind == "span"]  # noqa: SLF001
    by_operation: dict[str, list[Any]] = {}
    for span in spans:
        by_operation.setdefault(dict(span.trace_attributes)["operation"], []).append(span)
    expected = [
        *by_operation["run_evidence.serialize"],
        *by_operation["run_evidence.queue_offer"],
        *by_operation["run_evidence.transport_publish"],
    ]
    assert len(by_operation["run_evidence.serialize"]) == 1
    assert len(by_operation["run_evidence.queue_offer"]) == 1
    assert len(by_operation["run_evidence.transport_publish"]) == 3
    assert len({span.span_id for span in expected}) == len(expected)
    assert all(span.trace_id == item.trace_id for span in expected)
    assert all(span.parent_span_id == item.span_id for span in expected)


def test_fresh_coordinator_instances_keep_run_slots_for_resume_and_evaluation() -> None:
    recorder = BoundedRunEvidenceRecorder(RunEvidencePolicy(enabled=True, queue_capacity=8))
    run = Run(
        conversation_id=RUN_ID,
        user_message_id=RUN_ID,
        agent_revision_id=RUN_ID,
        model_policy_revision_id=RUN_ID,
        provider="evaluation",
        model_id="fixture",
        approval_id=RUN_ID,
        id=RUN_ID,
    )
    first = RunCoordinator(
        cast(Any, None), cast(Any, None), cast(Any, None), run_evidence=cast(Any, recorder)
    )
    first._record_evidence(  # noqa: SLF001
        run,
        step_category="admission",
        evidence_kind="admitted_request",
        media_classification="structured",
        media_type="application/json",
        payload={
            "kind": "admitted_request",
            "request": {"role": "user", "content": "resume"},
        },
    )
    resumed = RunCoordinator(
        cast(Any, None), cast(Any, None), cast(Any, None), run_evidence=cast(Any, recorder)
    )
    resumed._record_evidence(  # noqa: SLF001
        run,
        step_category="policy",
        evidence_kind="approval_decision",
        media_classification="structured",
        media_type="application/json",
        payload={
            "kind": "approval_decision",
            "decision": {"decision": "approved", "reason": "owner"},
        },
    )
    resumed._record_evidence(  # noqa: SLF001
        run,
        step_category="tool",
        evidence_kind="execution_outcome",
        media_classification="structured",
        media_type="application/json",
        payload={"kind": "execution_outcome", "outcome": "succeeded", "detail": "resumed"},
    )
    resumed._record_evidence(  # noqa: SLF001
        run,
        step_category="evaluation",
        evidence_kind="evaluation_input",
        media_classification="structured",
        media_type="application/json",
        payload={"kind": "evaluation_input", "content": {"name": "case", "content": "in"}},
    )
    resumed._record_evidence(  # noqa: SLF001
        run,
        step_category="evaluation",
        evidence_kind="evaluation_output",
        media_classification="structured",
        media_type="application/json",
        payload={"kind": "evaluation_output", "content": {"name": "case", "content": "out"}},
    )
    sequences = [
        json.loads(recorder._queue.get_nowait())["sequence"]  # noqa: SLF001
        for _ in range(5)
    ]
    assert sequences == [100, 10_000_000, 11_000_000, 11_000_001, 11_000_002]


def test_production_boundary_slots_preserve_routing_context_retrieval_prompt_and_tool_order(
) -> None:
    recorder = BoundedRunEvidenceRecorder(RunEvidencePolicy(enabled=True, queue_capacity=8))
    coordinator = RunCoordinator(
        cast(Any, None), cast(Any, None), cast(Any, None), run_evidence=cast(Any, recorder)
    )
    run = Run(
        conversation_id=RUN_ID,
        user_message_id=RUN_ID,
        agent_revision_id=RUN_ID,
        model_policy_revision_id=RUN_ID,
        provider="evaluation",
        model_id="fixture",
        id=RUN_ID,
    )
    boundaries = [
        (
            "routing",
            "model_routing",
            {
                "kind": "model_routing",
                "provider": "evaluation",
                "model": "fixture",
                "routeReason": "policy",
            },
        ),
        (
            "context",
            "conversation_context",
            {"kind": "conversation_context", "messages": [{"role": "user", "content": "hi"}]},
        ),
        (
            "retrieval",
            "memory_chunk",
            {
                "kind": "memory_chunk",
                "chunk": {"sourceId": "memory", "ordinal": 0, "content": "fact", "score": None},
            },
        ),
        ("prompt", "compiled_prompt", {"kind": "compiled_prompt", "prompt": "system"}),
        (
            "tool",
            "tool_arguments",
            {"kind": "tool_arguments", "toolName": "search", "content": "{}"},
        ),
        (
            "policy",
            "policy_decision",
            {"kind": "policy_decision", "decision": {"decision": "allowed", "reason": "policy"}},
        ),
        (
            "tool",
            "tool_result",
            {"kind": "tool_result", "toolName": "search", "content": "{}"},
        ),
    ]
    for category, kind, payload in boundaries:
        coordinator._record_evidence(  # noqa: SLF001
            run,
            step_category=category,
            evidence_kind=kind,
            media_classification="text" if kind == "compiled_prompt" else "structured",
            media_type="text/plain" if kind == "compiled_prompt" else "application/json",
            payload=payload,
        )
    envelopes = [_envelope(recorder._queue.get_nowait()) for _ in boundaries]  # noqa: SLF001
    assert [envelope["evidenceKind"] for envelope in envelopes] == [item[1] for item in boundaries]
    sequences = [envelope["sequence"] for envelope in envelopes]
    assert sequences == sorted(sequences)
    assert len(set(sequences)) == len(sequences)
    assert sequences == [200, 300, 400, 1_000, 40_000, 40_500, 41_000]


def test_repeated_tool_calls_use_unique_monotonic_blocks_and_settlement_slots() -> None:
    recorder = BoundedRunEvidenceRecorder(RunEvidencePolicy(enabled=True, queue_capacity=32))
    coordinator = RunCoordinator(
        cast(Any, None), cast(Any, None), cast(Any, None), run_evidence=cast(Any, recorder)
    )
    run = Run(
        conversation_id=RUN_ID,
        user_message_id=RUN_ID,
        agent_revision_id=RUN_ID,
        model_policy_revision_id=RUN_ID,
        provider="evaluation",
        model_id="fixture",
        id=RUN_ID,
    )
    for completed_calls in range(3):
        run.tool_calls_used = completed_calls
        coordinator._record_evidence(  # noqa: SLF001
            run,
            step_category="tool",
            evidence_kind="tool_arguments",
            media_classification="structured",
            media_type="application/json",
            payload={
                "kind": "tool_arguments",
                "toolName": "memory.search",
                "content": "{}",
            },
        )
        run.tool_calls_used = completed_calls + 1
        coordinator._record_evidence(  # noqa: SLF001
            run,
            step_category="tool",
            evidence_kind="tool_result",
            media_classification="structured",
            media_type="application/json",
            payload={
                "kind": "tool_result",
                "toolName": "memory.search",
                "content": "{}",
            },
        )
        coordinator._record_evidence(  # noqa: SLF001
            run,
            step_category="tool",
            evidence_kind="execution_outcome",
            media_classification="structured",
            media_type="application/json",
            payload={"kind": "execution_outcome", "outcome": "succeeded", "detail": "ok"},
        )
    for _ in range(3):
        coordinator._record_evidence(  # noqa: SLF001
            run,
            step_category="generation",
            evidence_kind="response_synthesis",
            media_classification="text",
            media_type="text/plain",
            payload={"kind": "response_synthesis", "text": "answer"},
        )
    coordinator._terminal(run, 0.0)  # noqa: SLF001
    assert recorder.queue_depth == 15
    envelopes = [
        _envelope(recorder._queue.get_nowait())  # noqa: SLF001
        for _ in range(15)
    ]
    tool_sequences = [
        envelope["sequence"]
        for envelope in envelopes
        if envelope["evidenceKind"] in {"tool_arguments", "tool_result", "execution_outcome"}
    ]
    assert tool_sequences == sorted(tool_sequences)
    assert len(set(tool_sequences)) == len(tool_sequences)
    assert tool_sequences == [
        40_000,
        41_000,
        41_100,
        1_000_040_000,
        1_000_041_000,
        1_000_041_100,
        2_000_040_000,
        2_000_041_000,
        2_000_041_100,
    ]
    synthesis_sequences = [
        envelope["sequence"]
        for envelope in envelopes
        if envelope["evidenceKind"] == "response_synthesis"
    ]
    terminal_sequences = [
        envelope["sequence"]
        for envelope in envelopes
        if envelope["evidenceKind"] in {"persistence_record", "delivery_record", "worker_output"}
    ]
    assert synthesis_sequences == sorted(synthesis_sequences)
    assert terminal_sequences == sorted(terminal_sequences)
    assert min(synthesis_sequences) > max(tool_sequences)
    assert min(terminal_sequences) > max(synthesis_sequences)
    assert len({envelope["sequence"] for envelope in envelopes}) == len(envelopes)


@pytest.mark.asyncio
async def test_native_tool_path_records_result_before_execution_outcome() -> None:
    class Capture:
        enabled = True

        def __init__(self) -> None:
            self.items: list[RunEvidenceInput] = []

        def record(self, item: RunEvidenceInput) -> bool:
            self.items.append(item)
            return True

    class Provider:
        async def stream_chat_with_tools(
            self, model_id: str, messages: object, tool_specs: object
        ) -> Any:
            del model_id, messages, tool_specs
            yield ModelToolCall("memory.search", {"query": "tea"})
            yield ModelStreamCompleted()

    class Store:
        async def find_run_any(self, run_id: UUID) -> tuple[None, Run]:
            del run_id
            return None, run

        async def recall_for_tool(
            self,
            conversation_value: object,
            run_value: Run,
            query: str,
            limit: int,
            trace_id: str,
            span_id: str,
        ) -> list[dict[str, str]]:
            del conversation_value, run_value, limit, trace_id, span_id
            return [{"query": query}]

    capture = Capture()
    coordinator = RunCoordinator(
        cast(Any, Store()),
        cast(Any, None),
        cast(Any, None),
        run_evidence=cast(Any, capture),
    )
    run = Run(
        conversation_id=RUN_ID,
        user_message_id=RUN_ID,
        agent_revision_id=RUN_ID,
        model_policy_revision_id=RUN_ID,
        provider="evaluation",
        model_id="fixture",
        id=RUN_ID,
    )
    conversation = SimpleNamespace(
        id=RUN_ID,
        principal_issuer="issuer",
        principal_subject="subject",
    )
    policy = SimpleNamespace(
        grants=(SimpleNamespace(tool_id="memory.search", tool_version=1),),
        max_calls_per_run=3,
        permission_scopes=frozenset(),
    )

    async def passthrough_images(*args: object) -> list[object]:
        return cast(list[object], args[-1])

    async def no_status(*args: object, **kwargs: object) -> None:
        del args, kwargs

    async def claim(*args: object, **kwargs: object) -> int:
        del args, kwargs
        return 1

    async def synthesize(*args: object, **kwargs: object) -> bool:
        del args, kwargs
        return True

    coordinator._with_image_parts = passthrough_images  # type: ignore[method-assign]  # noqa: SLF001
    coordinator._publish_tool_status = no_status  # type: ignore[method-assign]  # noqa: SLF001
    coordinator._claim_tool_call = claim  # type: ignore[method-assign]  # noqa: SLF001
    coordinator._consume_synthesis = synthesize  # type: ignore[method-assign]  # noqa: SLF001
    async def keep_attempt(*args: object, **kwargs: object) -> bool:
        del args, kwargs
        await asyncio.sleep(0.01)
        return False

    coordinator._wait_until_attempt_stops = keep_attempt  # type: ignore[method-assign]  # noqa: SLF001

    result = await coordinator._consume_native_provider(  # noqa: SLF001
        conversation,
        run,
        RUN_ID,
        Provider(),
        [],
        policy,
        0.0,
        [0],
        "w" * 16,
        0.2,
    )
    assert result is True
    assert [item.evidence_kind for item in capture.items] == [
        "tool_arguments",
        "policy_decision",
        "tool_result",
        "execution_outcome",
    ]
    assert [item.sequence for item in capture.items] == [40_000, 40_500, 41_000, 41_100]


@pytest.mark.asyncio
async def test_native_direct_text_emits_each_generation_chunk_once_without_tool_duplicates(
) -> None:
    class Capture:
        enabled = True

        def __init__(self) -> None:
            self.items: list[RunEvidenceInput] = []
            self.next_sequence = 0

        def allocate_sequence(self, run_id: UUID) -> int:
            del run_id
            sequence = self.next_sequence
            self.next_sequence += 1
            return sequence

        def record(self, item: RunEvidenceInput) -> bool:
            self.items.append(item)
            return True

    class Provider:
        async def stream_chat_with_tools(
            self, model_id: str, messages: object, tool_specs: object
        ) -> Any:
            del model_id, messages, tool_specs
            yield ModelTextDelta("first")
            yield ModelTextDelta("second")
            yield ModelStreamCompleted()

    capture = Capture()
    coordinator = RunCoordinator(
        cast(Any, None), cast(Any, None), cast(Any, None), run_evidence=cast(Any, capture)
    )
    run = Run(
        conversation_id=RUN_ID,
        user_message_id=RUN_ID,
        agent_revision_id=RUN_ID,
        model_policy_revision_id=RUN_ID,
        provider="evaluation",
        model_id="fixture",
        id=RUN_ID,
    )
    conversation = SimpleNamespace(id=RUN_ID)
    policy = SimpleNamespace(
        grants=(SimpleNamespace(tool_id="memory.search", tool_version=1),),
        max_calls_per_run=3,
        permission_scopes=frozenset(),
    )

    async def passthrough_images(*args: object) -> list[object]:
        return cast(list[object], args[-1])

    async def append_text(*args: object, **kwargs: object) -> None:
        del args, kwargs

    async def keep_attempt(*args: object, **kwargs: object) -> bool:
        del args, kwargs
        await asyncio.sleep(0.01)
        return False

    coordinator._with_image_parts = passthrough_images  # type: ignore[method-assign]  # noqa: SLF001
    coordinator._append_text_delta = append_text  # type: ignore[method-assign]  # noqa: SLF001
    coordinator._wait_until_attempt_stops = keep_attempt  # type: ignore[method-assign]  # noqa: SLF001

    result = await coordinator._consume_native_provider(  # noqa: SLF001
        conversation,
        run,
        RUN_ID,
        Provider(),
        [],
        policy,
        0.0,
        [0],
        "w" * 16,
        0.2,
    )
    assert result is True
    assert [item.evidence_kind for item in capture.items] == [
        "generation_output",
        "generation_output",
    ]
    assert [item.payload for item in capture.items] == [
        {"kind": "generation_output", "text": "first"},
        {"kind": "generation_output", "text": "second"},
    ]
    assert [item.sequence for item in capture.items] == [0, 1]


@pytest.mark.parametrize("evidence_kind", ["tool_arguments", "tool_result"])
def test_nested_unicode_escaped_credentials_fail_closed_at_scan_depth(
    evidence_kind: str,
) -> None:
    credential_json = '{"\\u0061uthorization":"\\u0042asic dXNlcjpwYXNz"}'
    nested = json.dumps(
        {"level1": json.dumps({"level2": json.dumps({"level3": credential_json})})}
    )
    with pytest.raises(EvidenceRejected) as error:
        serialize_evidence(
            _item(
                step_category="tool",
                evidence_kind=evidence_kind,
                media_classification="structured",
                media_type="application/json",
                payload={
                    "kind": evidence_kind,
                    "toolName": "memory.search",
                    "content": nested,
                },
            ),
            policy=RunEvidencePolicy(enabled=True),
        )
    assert error.value.reason == "secret_bearing_field_rejected"


def test_ordinary_chat_terminal_settlement_never_emits_tool_execution_outcome() -> None:
    class Capture:
        def __init__(self) -> None:
            self.items: list[RunEvidenceInput] = []

        def record(self, item: RunEvidenceInput) -> bool:
            self.items.append(item)
            return True

    capture = Capture()
    coordinator = RunCoordinator(
        cast(Any, None), cast(Any, None), cast(Any, None), run_evidence=cast(Any, capture)
    )
    run = Run(
        conversation_id=RUN_ID,
        user_message_id=RUN_ID,
        agent_revision_id=RUN_ID,
        model_policy_revision_id=RUN_ID,
        provider="evaluation",
        model_id="fixture",
        status=RunStatus.COMPLETED,
        id=RUN_ID,
    )
    coordinator._terminal(run, 0.0)  # noqa: SLF001
    assert "execution_outcome" not in {item.evidence_kind for item in capture.items}

    coordinator._record_tool_execution(  # noqa: SLF001
        run,
        {
            "execution_started": 0.0,
            "execution_span_id": "e" * 16,
            "parent_span_id": "p" * 16,
            "execution_duration": 0.01,
        },
        "completed",
        None,
    )
    assert [item.evidence_kind for item in capture.items].count("execution_outcome") == 1


@pytest.mark.asyncio
async def test_export_retries_are_bounded_and_exact_duplicate_delivery_is_idempotent() -> None:
    publisher = _Publisher(failures=2)
    recorder = BoundedRunEvidenceRecorder(
        RunEvidencePolicy(enabled=True, max_attempts=3), publisher=publisher
    )
    assert recorder.record(_text_item()) is True
    await recorder.start()
    await asyncio.wait_for(recorder._queue.join(), timeout=1.0)  # noqa: SLF001
    assert len(publisher.calls) == 3
    assert len({message_id for _, message_id in publisher.calls}) == 1
    assert len({json.loads(payload)["eventId"] for payload, _ in publisher.calls}) == 1
    delivery_ids = {
        json.loads(payload)["delivery"]["deliveryId"]
        for payload, _ in publisher.calls
    }
    assert len(delivery_ids) == 3
    await recorder.stop()


@pytest.mark.asyncio
async def test_outage_and_cancellation_are_isolated_and_shutdown_is_bounded() -> None:
    publisher = _Publisher(failures=10)
    recorder = BoundedRunEvidenceRecorder(
        RunEvidencePolicy(enabled=True, max_attempts=2, drain_seconds=0.1), publisher=publisher
    )
    assert recorder.record(_text_item()) is True
    await recorder.start()
    await asyncio.wait_for(recorder._queue.join(), timeout=1.0)
    assert len(publisher.calls) == 2
    await recorder.stop()
    assert publisher.closed is True

    blocking = _Publisher(block=True)
    cancelled = BoundedRunEvidenceRecorder(
        RunEvidencePolicy(enabled=True, drain_seconds=0.1), publisher=blocking
    )
    assert cancelled.record(_text_item()) is True
    await cancelled.start()
    await asyncio.wait_for(blocking.entered.wait(), timeout=1.0)
    await asyncio.wait_for(cancelled.stop(), timeout=1.0)
    assert blocking.closed is True
    assert cancelled.record(_text_item(sequence=1)) is False


def test_rejection_emits_only_a_content_free_completeness_signal() -> None:
    recorder = BoundedRunEvidenceRecorder(RunEvidencePolicy(enabled=True, queue_capacity=4))
    rejected = _text_item(text="Bearer binary-secret-value")
    assert recorder.record(rejected) is False
    signal = _envelope(recorder._queue.get_nowait())  # noqa: SLF001
    assert signal["captureState"] == "partial"
    assert signal["reason"] == "secret_bearing_field_rejected"
    assert signal["byteCount"] == 0
    assert signal["digest"] is None
    assert "payload" not in signal
    assert signal["contentAdmission"]["outcome"] == "rejected_prohibited_material"


def test_disabled_capture_has_no_queue_and_remains_bounded() -> None:
    disabled = make_run_evidence_recorder(
        SimpleNamespace(
            run_evidence_capture_policy="metadata_only",
            run_evidence_queue_capacity=1,
            run_evidence_max_attempts=1,
            run_evidence_drain_seconds=0.1,
        )
    )
    assert isinstance(disabled, DisabledRunEvidenceRecorder)
    assert not hasattr(disabled, "_queue")

    recorder = BoundedRunEvidenceRecorder(RunEvidencePolicy(enabled=True, queue_capacity=2))
    assert recorder._queue.maxsize == 2  # noqa: SLF001
    assert recorder.record(_text_item(sequence=0)) is True
    assert recorder.record(_text_item(sequence=1)) is True
    assert recorder.record(_text_item(sequence=2)) is False


def test_disabled_capture_does_not_build_nontrivial_payloads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Disabled mode must short-circuit builders, including raw attachments."""

    disabled = DisabledRunEvidenceRecorder()
    coordinator = RunCoordinator(
        cast(Any, None), cast(Any, None), cast(Any, None), run_evidence=cast(Any, disabled)
    )
    run = Run(
        conversation_id=RUN_ID,
        user_message_id=RUN_ID,
        agent_revision_id=RUN_ID,
        model_policy_revision_id=RUN_ID,
        provider="evaluation",
        model_id="fixture",
        id=RUN_ID,
    )
    encoded = False

    def forbidden_encode(*args: object, **kwargs: object) -> bytes:
        nonlocal encoded
        encoded = True
        del args, kwargs
        raise AssertionError("disabled evidence attempted to encode attachment bytes")

    monkeypatch.setattr(run_evidence.base64, "b64encode", forbidden_encode)
    message = SimpleNamespace(
        image_parts=(SimpleNamespace(data=b"image-bytes", media_type="image/png"),)
    )
    coordinator._record_model_attachments(run, [message], None)  # noqa: SLF001
    assert encoded is False

    # Keep this invariant explicit for every boundary: a non-trivial payload
    # must be behind the coordinator's disabled-mode guard. The attachment
    # helper is the sole exception because it has its own early return above.
    source = (
        ROOT / "services/aura-core/src/aura_core/domains/execution/runs/service.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)
    parents: dict[ast.AST, ast.AST] = {
        child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)
    }
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "_record_evidence"
        ):
            continue
        payload = next(
            (keyword.value for keyword in node.keywords if keyword.arg == "payload"), None
        )
        assert payload is not None
        if isinstance(payload, ast.Dict):
            owner = parents.get(node)
            while owner is not None and not isinstance(
                owner, (ast.FunctionDef, ast.AsyncFunctionDef)
            ):
                owner = parents.get(owner)
            assert isinstance(owner, ast.FunctionDef)
            assert owner.name == "_record_model_attachments"
            continue
        if isinstance(payload, ast.Constant):
            assert payload.value is None
            continue
        assert isinstance(payload, ast.IfExp)
        assert any(
            isinstance(candidate, ast.Call)
            and isinstance(candidate.func, ast.Attribute)
            and candidate.func.attr == "_evidence_enabled"
            for candidate in ast.walk(payload.test)
        )


@pytest.mark.asyncio
async def test_disabled_evaluation_runner_never_constructs_evidence_payloads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    disabled = DisabledRunEvidenceRecorder()
    scenario = evaluation_runner.SyntheticEvaluationScenario(disabled)
    constructed = False

    def forbidden_item(*args: object, **kwargs: object) -> object:
        nonlocal constructed
        constructed = True
        del args, kwargs
        raise AssertionError("disabled evaluation attempted to construct evidence")

    monkeypatch.setattr(evaluation_runner, "RunEvidenceItem", forbidden_item)
    context = evaluation_runner.ReplayContext(
        RUN_ID,
        RUN_ID,
        datetime.now(UTC) + timedelta(seconds=5),
        asyncio.Event(),
    )
    result = await scenario.execute(RUN_ID, context)
    assert result.measurements["run_lifecycle"] == "completed"
    assert constructed is False


@pytest.mark.asyncio
async def test_visible_thinking_is_captured_only_after_live_publish_succeeds() -> None:
    class ThinkingPublisher:
        def __init__(self, *, fail: bool) -> None:
            self.fail = fail

        async def publish_thinking_delta(self, **kwargs: object) -> None:
            del kwargs
            if self.fail:
                raise RuntimeError("thinking sink unavailable")

    class Capture:
        def __init__(self) -> None:
            self.items: list[RunEvidenceInput] = []

        def record(self, item: RunEvidenceInput) -> bool:
            self.items.append(item)
            return True

    run = Run(
        conversation_id=RUN_ID,
        user_message_id=RUN_ID,
        agent_revision_id=RUN_ID,
        model_policy_revision_id=RUN_ID,
        provider="evaluation",
        model_id="fixture",
        id=RUN_ID,
    )
    conversation = SimpleNamespace(id=RUN_ID)
    failed_capture = Capture()
    failed_coordinator = RunCoordinator(
        cast(Any, None),
        cast(Any, None),
        cast(Any, None),
        metrics=cast(Any, _Metrics()),
        thinking_publisher=ThinkingPublisher(fail=True),
        run_evidence=cast(Any, failed_capture),
    )
    await failed_coordinator._publish_thinking_delta(  # noqa: SLF001
        run, conversation, ModelThinkingDelta("visible"), "c" * 16
    )
    assert failed_capture.items == []

    successful_capture = Capture()
    successful_coordinator = RunCoordinator(
        cast(Any, None),
        cast(Any, None),
        cast(Any, None),
        metrics=cast(Any, _Metrics()),
        thinking_publisher=ThinkingPublisher(fail=False),
        run_evidence=cast(Any, successful_capture),
    )
    await successful_coordinator._publish_thinking_delta(  # noqa: SLF001
        run, conversation, ModelThinkingDelta("visible"), "c" * 16
    )
    assert len(successful_capture.items) == 1
    assert successful_capture.items[0].evidence_kind == "displayed_thinking"


@pytest.mark.asyncio
async def test_non_acquired_claim_does_not_emit_admission_or_boundary_evidence() -> None:
    class Store:
        async def start_run(self, *args: object, **kwargs: object) -> object:
            del args, kwargs
            return SimpleNamespace(
                acquired=False,
                conversation=SimpleNamespace(id=RUN_ID),
                run=Run(
                    conversation_id=RUN_ID,
                    user_message_id=RUN_ID,
                    agent_revision_id=RUN_ID,
                    model_policy_revision_id=RUN_ID,
                    provider="evaluation",
                    model_id="fixture",
                    status=RunStatus.CANCELED,
                    id=RUN_ID,
                ),
            )

    class Capture:
        def __init__(self) -> None:
            self.items: list[RunEvidenceInput] = []

        def record(self, item: RunEvidenceInput) -> bool:
            self.items.append(item)
            return True

    capture = Capture()
    coordinator = RunCoordinator(
        cast(Any, Store()),
        cast(Any, None),
        cast(Any, None),
        metrics=cast(Any, _Metrics()),
        run_evidence=cast(Any, capture),
    )
    await coordinator._execute(RUN_ID, cast(Any, None), "d" * 16)  # noqa: SLF001
    assert capture.items == []


@pytest.mark.asyncio
async def test_real_metadata_metrics_record_only_declared_content_free_spans() -> None:
    metrics = MetadataMetrics()
    publisher = _Publisher()
    recorder = BoundedRunEvidenceRecorder(
        RunEvidencePolicy(enabled=True), publisher=publisher, metrics=metrics
    )
    assert recorder.record(_text_item()) is True
    await recorder.start()
    await asyncio.wait_for(recorder._queue.join(), timeout=1.0)  # noqa: SLF001
    assert recorder.queue_depth == 0
    await recorder.stop()
    assert recorder.queue_depth == 0
    names = {measurement.metric for measurement in metrics._measurements}  # noqa: SLF001
    assert {
        "run_evidence_serialize_latency_ms",
        "run_evidence_queue_offer_latency_ms",
        "run_evidence_transport_latency_ms",
        "run_evidence_queue_depth",
        "run_evidence_queue_capacity",
        "run_evidence_export_outcome_count",
    } <= names
    for measurement in metrics._measurements:  # noqa: SLF001
        assert not {"payload", "event_id", "evidence_id", "digest", "bytes"}.intersection(
            dict(measurement.dimensions)
        )
        assert not {"payload", "event_id", "evidence_id", "digest", "bytes"}.intersection(
            dict(measurement.trace_attributes)
        )
    assert metrics._rejected == 0  # noqa: SLF001


@pytest.mark.asyncio
async def test_shutdown_reports_each_pending_item_with_correlated_drop_span_and_counter() -> None:
    metrics = MetadataMetrics()
    publisher = _Publisher(block=True)
    recorder = BoundedRunEvidenceRecorder(
        RunEvidencePolicy(enabled=True, queue_capacity=3, drain_seconds=0.1),
        publisher=publisher,
        metrics=metrics,
    )
    items = [
        _text_item(sequence=0, span_id="b" * 16),
        _text_item(sequence=1, span_id="c" * 16),
        _text_item(sequence=2, span_id="d" * 16),
    ]
    assert all(recorder.record(item) for item in items)
    await recorder.start()
    await asyncio.wait_for(publisher.entered.wait(), timeout=1.0)
    await asyncio.wait_for(recorder.stop(), timeout=1.0)

    shutdown_spans = [
        measurement
        for measurement in metrics._measurements  # noqa: SLF001
        if measurement.kind == "span"
        and dict(measurement.trace_attributes).get("operation")
        == "run_evidence.transport_publish"
        and dict(measurement.dimensions).get("outcome") == "failed"
    ]
    assert len(shutdown_spans) >= len(items)
    assert {span.trace_id for span in shutdown_spans} >= {item.trace_id for item in items}
    assert all(span.parent_span_id in {item.span_id for item in items} for span in shutdown_spans)

    shutdown_drops = [
        measurement
        for measurement in metrics._measurements  # noqa: SLF001
        if measurement.kind == "metric"
        and measurement.metric == "run_evidence_export_dropped_count"
        and dict(measurement.dimensions).get("error_class") == "cancel"
    ]
    assert len(shutdown_drops) >= len(items)
    assert {measurement.trace_id for measurement in shutdown_drops} >= {
        item.trace_id for item in items
    }
    # The first item was already claimed by the worker; the remaining queued
    # items stay bounded and are represented by the shutdown drop spans.
    assert recorder.queue_depth == len(items) - 1


def test_metrics_are_content_free_and_module_has_no_observatory_client_dependency() -> None:
    source = inspect.getsource(run_evidence)
    assert "observatory-api-client" not in source
    assert "httpx" not in source
    assert "RunEvent" not in source
    assert "aura_core.platform.telemetry" not in source
    assert "aura_core.platform.outbox" not in source
    metrics = _Metrics()
    recorder = BoundedRunEvidenceRecorder(RunEvidencePolicy(enabled=True), metrics=metrics)
    assert recorder.record(_text_item()) is True
    for _, _, attrs in metrics.increments:
        assert not {"payload", "event_id", "evidence_id", "digest", "bytes"}.intersection(attrs)
    for _, _, _, attrs in metrics.observations:
        assert not {"payload", "event_id", "evidence_id", "digest", "bytes"}.intersection(attrs)


def test_run_coordinator_evidence_offer_is_optional_and_failure_isolated() -> None:
    class Recorder:
        def __init__(self, *, fail: bool = False) -> None:
            self.fail = fail
            self.items: list[Any] = []

        def record(self, item: object) -> bool:
            if self.fail:
                raise RuntimeError("serializer unavailable")
            self.items.append(item)
            return True

    metrics = _Metrics()
    recorder = Recorder()
    coordinator = RunCoordinator(
        cast(Any, None),
        cast(Any, None),
        cast(Any, None),
        metrics=cast(Any, metrics),
        run_evidence=cast(Any, recorder),
    )
    run = SimpleNamespace(id=RUN_ID)
    coordinator._record_evidence(
        run,
        step_category="generation",
        evidence_kind="generation_output",
        media_classification="text",
        media_type="text/plain",
        payload={"kind": "generation_output", "text": "answer"},
    )
    assert len(recorder.items) == 1
    assert recorder.items[0].sequence == 0

    failing_metrics = _Metrics()
    failing = RunCoordinator(
        cast(Any, None),
        cast(Any, None),
        cast(Any, None),
        metrics=cast(Any, failing_metrics),
        run_evidence=cast(Any, Recorder(fail=True)),
    )
    failing._record_evidence(
        run,
        step_category="generation",
        evidence_kind="generation_output",
        media_classification="text",
        media_type="text/plain",
        payload={"kind": "generation_output", "text": "answer"},
    )
    assert any(
        name == "run_evidence_export_dropped_count"
        and attrs.get("error_class") == "serialization_rejected"
        for _, name, attrs in failing_metrics.increments
    )


def test_export_component_manifest_is_bounded_content_free_and_non_replayable() -> None:
    manifest = yaml.safe_load(
        (
            ROOT
            / "services/aura-core/resources/component-manifests/run-evidence-export.yaml"
        ).read_text(encoding="utf-8")
    )
    assert manifest["component"] == {
        "id": "aura.platform.run_evidence_export",
        "version": "1.0.0",
        "owner": "aura-core.platform",
        "category": "observability",
    }
    assert manifest["input"] == {"schema": "run-evidence-v1", "replayable": False}
    assert manifest["output"]["schema"] == "run-evidence-v1"
    assert manifest["output"]["capture"]["content"] == "full_content"
    assert manifest["export"] == {
        "sink": "aura.runs.evidence.v1",
        "cadence": "background_only",
    }
    assert manifest["failure_semantics"]["execution_coupling"] == "none"
    assert manifest["retention"]["queue"]["overflow"] == "drop_new_and_count"
    assert set(manifest["cardinality"]["prohibited"]) >= {
        "event_id",
        "evidence_id",
        "digest",
        "bytes",
        "payload",
        "prompt",
        "response",
        "owner",
        "credential",
        "token",
    }


def test_api_worker_and_evaluation_composition_roots_wire_bounded_lifecycle() -> None:
    roots = {
        "api": ROOT / "services/aura-core/src/aura_core/entrypoints/api/state.py",
        "worker": ROOT / "services/aura-core/src/aura_core/entrypoints/worker/app.py",
        "evaluation": ROOT / "services/aura-core/src/aura_core/bootstrap/evaluation_runner.py",
    }
    for name, path in roots.items():
        source = path.read_text(encoding="utf-8")
        assert "run_evidence" in source, name
        assert "run_evidence=" in source, name
        assert ".start()" in source and ".stop()" in source, name
        assert "observatory-api-client" not in source
