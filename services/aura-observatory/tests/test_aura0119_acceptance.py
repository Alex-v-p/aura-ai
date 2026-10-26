"""Independent acceptance coverage for the governed Observatory evidence slice.

These tests deliberately use the Observatory-owned public ports.  They do not
import Aura Core, inspect its database, or rely on a browser/session bridge.
"""
# pyright: reportPrivateUsage=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownVariableType=false, reportGeneralTypeIssues=false, reportArgumentType=false, reportIndexIssue=false, reportUnknownParameterType=false

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import httpx
import pytest
import yaml
from aura_observatory.adapters.run_evidence import (
    MemoryRunEvidenceObjectStore,
    run_evidence_object_key,
)
from aura_observatory.auth import (
    MemoryLoginStateBackend,
    MemorySessionBackend,
    ObservatoryAuth,
    Principal,
)
from aura_observatory.entrypoints.api.app import create_app
from aura_observatory.metrics import ObservatoryMetrics
from aura_observatory.run_evidence import (
    MAX_ENVELOPE_BYTES,
    MAX_EVIDENCE_BYTES,
    DuplicateCollision,
    MemoryRunEvidenceRepository,
    RunEvidenceDeletionService,
    RunEvidenceError,
    RunEvidenceIngestionService,
    RunInspectionService,
    validate_run_evidence,
)
from aura_observatory.run_evidence_transport import NatsRunEvidenceTransport
from aura_observatory.settings import Settings

RUN_ID = "11111111-1111-4111-8111-111111111111"
TRACE_ID = "0123456789abcdef0123456789abcdef"
PARENT_SPAN = "0123456789abcdef"
REPOSITORY_ROOT = Path(__file__).parents[3]


def _uuid() -> str:
    return str(uuid4())


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def _payload(kind: str) -> tuple[str, str, object]:
    """Return the public kind discriminator, media classification and payload."""

    if kind == "admitted_request":
        return "structured", "application/json", {
            "kind": kind,
            "request": {"role": "user", "content": "fixture"},
        }
    if kind == "admitted_trigger":
        return "structured", "application/json", {
            "kind": kind,
            "triggerType": "manual",
            "content": "fixture",
        }
    if kind == "compiled_prompt":
        return "text", "text/plain", {"kind": kind, "prompt": "system: answer safely"}
    if kind == "conversation_context":
        return "structured", "application/json", {
            "kind": kind,
            "messages": [{"role": "user", "content": "fixture"}],
        }
    if kind in {"memory_chunk", "document_chunk"}:
        return "structured", "application/json", {
            "kind": kind,
            "chunk": {
                "sourceId": "source-1",
                "ordinal": 0,
                "content": "fixture",
                "score": 0.5,
            },
        }
    if kind in {"generation_output", "response_synthesis", "displayed_thinking"}:
        return "text", "text/plain", {"kind": kind, "text": "visible output"}
    if kind == "model_routing":
        return "structured", "application/json", {
            "kind": kind,
            "provider": "local",
            "model": "fixture-model",
            "routeReason": "fixture",
        }
    if kind == "model_visible_attachment":
        raw = b"%PDF-1.7\nfixture"
        return (
            "document",
            "application/pdf",
            {
                "kind": kind,
                "attachmentId": "attachment-1",
                "displayName": "fixture.pdf",
                "contentBase64": base64.b64encode(raw).decode("ascii"),
            },
        )
    if kind in {"tool_arguments", "tool_result"}:
        return "structured", "application/json", {
            "kind": kind,
            "toolName": "fixture_tool",
            "content": '{"value":"fixture"}',
        }
    if kind in {"policy_decision", "approval_decision"}:
        return "structured", "application/json", {
            "kind": kind,
            "decision": {"decision": "allow", "reason": "fixture"},
        }
    if kind == "execution_outcome":
        return "structured", "application/json", {
            "kind": kind,
            "outcome": "succeeded",
            "detail": "fixture",
        }
    if kind in {"persistence_record", "delivery_record"}:
        return "structured", "application/json", {
            "kind": kind,
            "record": {"name": "fixture", "content": "fixture"},
        }
    if kind in {
        "worker_input",
        "worker_output",
        "automation_input",
        "automation_output",
        "schedule_input",
        "schedule_output",
        "evaluation_input",
        "evaluation_output",
    }:
        return "structured", "application/json", {
            "kind": kind,
            "content": {"name": "fixture", "content": "fixture"},
        }
    return "structured", "application/json", {"kind": kind, "value": "fixture", "count": 1}


def _envelope(
    *,
    kind: str = "admitted_request",
    sequence: int = 1,
    run_id: str = RUN_ID,
    evidence_id: str | None = None,
    event_id: str | None = None,
    span_id: str = "0123456789abcdef",
    parent_span_id: str | None = None,
    delivery_kind: str = "initial",
    attempt: int = 1,
    first_delivery_id: str | None = None,
    payload: object | None = None,
    capture_state: str = "captured",
    reason: str | None = None,
    admission: str = "passed",
) -> dict[str, object]:
    classification, media_type, default_payload = _payload(kind)
    body = default_payload if payload is None and capture_state == "captured" else payload
    if capture_state != "captured":
        body = None
        admission = "not_applicable"
    canonical = b""
    if capture_state == "captured":
        if classification == "text":
            assert isinstance(body, Mapping)
            canonical = str(body["prompt"] if kind == "compiled_prompt" else body["text"]).encode()
        elif kind == "model_visible_attachment":
            assert isinstance(body, Mapping)
            canonical = base64.b64decode(str(body["contentBase64"]))
        else:
            canonical = _canonical_bytes(body)
    delivery: dict[str, object] = {
        "kind": delivery_kind,
        "producerId": "aura-core-producer",
        "deliveryId": _uuid(),
        "attempt": attempt,
    }
    if first_delivery_id is not None:
        delivery["firstDeliveryId"] = first_delivery_id
    admission_payload: dict[str, object] = {"outcome": admission}
    if capture_state == "captured":
        admission_payload.update(
            {
                "scannerRevision": "core-evidence-scanner-v1",
                "policyRevision": "core-evidence-policy-v1",
                "inspectedAt": "2026-10-25T12:00:00.002Z",
                "inspectionModes": {
                    "text": ["decoded_text"],
                    "structured": ["decoded_text", "container_structure"],
                    "document": [
                        "decoded_text",
                        "metadata",
                        "container_structure",
                        "ocr",
                    ],
                }[classification],
            }
        )
    return {
        "schemaVersion": 1,
        "contractId": "run-evidence-v1",
        "eventId": event_id or _uuid(),
        "evidenceId": evidence_id or _uuid(),
        "runId": run_id,
        "traceId": TRACE_ID,
        "spanId": span_id,
        "parentSpanId": parent_span_id,
        "sequence": sequence,
        "stepCategory": {
            "text": "generation",
            "structured": "admission",
            "image": "retrieval",
            "document": "context",
        }[classification],
        "evidenceKind": kind,
        "mediaClassification": classification,
        "mediaType": media_type,
        "digest": (
            "sha256:" + hashlib.sha256(canonical).hexdigest()
            if capture_state == "captured"
            else None
        ),
        "byteCount": len(canonical),
        "capturePolicyRevision": "owner-v1",
        "observedAt": "2026-10-25T12:00:00.000Z",
        "emittedAt": "2026-10-25T12:00:00.001Z",
        "delivery": delivery,
        "contentAdmission": admission_payload,
        "captureState": capture_state,
        "reason": reason,
        "payload": body,
    }


def _fix_category(value: dict[str, object], category: str) -> dict[str, object]:
    value["stepCategory"] = category
    return value


Mutator = Callable[[dict[str, object]], None]


def _bad_sequence_bool(value: dict[str, object]) -> None:
    value["sequence"] = True


def _bad_sequence_negative(value: dict[str, object]) -> None:
    value["sequence"] = -1


def _bad_sequence_too_large(value: dict[str, object]) -> None:
    value["sequence"] = 9_007_199_254_740_992


def _bad_trace(value: dict[str, object]) -> None:
    value["traceId"] = "bad"


def _bad_media(value: dict[str, object]) -> None:
    value["mediaType"] = "application/octet-stream"


def _bad_category(value: dict[str, object]) -> None:
    value["stepCategory"] = "not-a-category"


def _bad_digest(value: dict[str, object]) -> None:
    value["digest"] = "sha256:00"


def _bad_byte_count(value: dict[str, object]) -> None:
    value["byteCount"] = 999


def _bad_delivery_attempt(value: dict[str, object]) -> None:
    value["delivery"] = {
        "kind": "initial",
        "producerId": "p",
        "deliveryId": _uuid(),
        "attempt": 2,
    }


async def _owner_app() -> tuple[Any, Settings, str, Any]:
    settings = Settings(owner_subject="owner-subject")
    auth = ObservatoryAuth(settings, MemorySessionBackend(), MemoryLoginStateBackend())
    session_id, session = await auth.create_session(
        Principal(
            "https://identity.example.invalid/application/o/aura-observatory/",
            "owner-subject",
            "Owner",
        )
    )
    return create_app(settings, auth=auth, testing=True), settings, session_id, session


async def _accepted(repo: MemoryRunEvidenceRepository, value: dict[str, object]) -> str:
    return await RunEvidenceIngestionService(repo).ingest(value)


@pytest.mark.parametrize(
    "kind,category",
    [
        ("admitted_request", "admission"),
        ("admitted_trigger", "admission"),
        ("compiled_prompt", "prompt"),
        ("conversation_context", "context"),
        ("memory_chunk", "retrieval"),
        ("document_chunk", "retrieval"),
        ("model_visible_attachment", "context"),
        ("model_routing", "routing"),
        ("displayed_thinking", "thinking"),
        ("generation_output", "generation"),
        ("tool_arguments", "tool"),
        ("tool_result", "tool"),
        ("policy_decision", "policy"),
        ("approval_decision", "policy"),
        ("execution_outcome", "tool"),
        ("response_synthesis", "synthesis"),
        ("persistence_record", "persistence"),
        ("delivery_record", "delivery"),
        ("worker_input", "worker"),
        ("worker_output", "worker"),
        ("automation_input", "automation"),
        ("automation_output", "automation"),
        ("schedule_input", "schedule"),
        ("schedule_output", "schedule"),
        ("evaluation_input", "evaluation"),
        ("evaluation_output", "evaluation"),
    ],
)
def test_every_closed_evidence_kind_has_a_validated_shape(kind: str, category: str) -> None:
    value = _fix_category(_envelope(kind=kind), category)
    validated = validate_run_evidence(value)
    assert validated.evidence_id == value["evidenceId"]
    assert validated.canonical_bytes


def test_attachment_requires_approved_magic_bytes_and_exact_digest() -> None:
    value = _fix_category(_envelope(kind="model_visible_attachment"), "context")
    assert validate_run_evidence(value).canonical_bytes.startswith(b"%PDF-")
    bad = dict(value)
    bad["payload"] = {**value["payload"], "contentBase64": base64.b64encode(b"not a PDF").decode()}
    bad["digest"] = "sha256:" + hashlib.sha256(b"not a PDF").hexdigest()
    with pytest.raises(RunEvidenceError, match="unsupported_media_type"):
        validate_run_evidence(bad)


@pytest.mark.parametrize(
    "mutator",
    [
        _bad_sequence_bool,
        _bad_sequence_negative,
        _bad_sequence_too_large,
        _bad_trace,
        _bad_media,
        _bad_category,
        _bad_digest,
        _bad_byte_count,
        _bad_delivery_attempt,
    ],
)
def test_malformed_or_unbounded_envelopes_are_rejected(mutator: Mutator) -> None:
    value = _fix_category(_envelope(), "admission")
    mutator(value)
    with pytest.raises(RunEvidenceError):
        validate_run_evidence(value)


def test_producer_id_cannot_enable_a_fixture_or_attestation_bypass() -> None:
    value = _fix_category(_envelope(), "admission")
    delivery = dict(cast(Mapping[str, object], value["delivery"]))
    delivery["producerId"] = "aura-core-test"
    value["delivery"] = delivery
    value["contentAdmission"] = {"outcome": "passed"}
    with pytest.raises(RunEvidenceError, match="ingest_rejected"):
        validate_run_evidence(value)


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("delivery", {"kind": "initial", "producerId": "aura-core-producer"}),
        ("capturePolicyRevision", "policy-v1\nsecret"),
        ("stepCategory", "generation"),
        ("captureState", "partial"),
        ("reason", "capture_disabled"),
    ],
)
def test_delivery_admission_revision_category_and_state_bindings_are_exact(
    field: str, replacement: object
) -> None:
    value = _fix_category(_envelope(), "admission")
    if field == "delivery":
        value[field] = replacement
    else:
        value[field] = replacement
    with pytest.raises(RunEvidenceError):
        validate_run_evidence(value)


def test_wire_envelope_and_encoded_attachment_limits_are_bounded() -> None:
    with pytest.raises(RunEvidenceError, match="payload_limit_exceeded"):
        validate_run_evidence(b"x" * (MAX_ENVELOPE_BYTES + 1))

    oversized = _fix_category(_envelope(kind="model_visible_attachment"), "context")
    oversized_payload = dict(cast(Mapping[str, object], oversized["payload"]))
    oversized_payload["contentBase64"] = "A" * 1_398_105
    oversized["payload"] = oversized_payload
    with pytest.raises(RunEvidenceError, match="payload_limit_exceeded"):
        validate_run_evidence(oversized)


def test_maximum_approved_attachment_round_trips_at_exact_byte_limit() -> None:
    raw = b"%PDF-1.7\n" + b"x" * (MAX_EVIDENCE_BYTES - len(b"%PDF-1.7\n"))
    encoded = base64.b64encode(raw).decode("ascii")
    value = _fix_category(_envelope(kind="model_visible_attachment"), "context")
    value["payload"] = {
        "kind": "model_visible_attachment",
        "attachmentId": "attachment-max",
        "displayName": "maximum.pdf",
        "contentBase64": encoded,
    }
    value["digest"] = "sha256:" + hashlib.sha256(raw).hexdigest()
    value["byteCount"] = len(raw)
    assert validate_run_evidence(value).canonical_bytes == raw


@pytest.mark.parametrize(
    "field", ["password", "authorization", "cookie", "session_id", "private_key"]
)
def test_secret_material_is_rejected_without_persisting_bytes(field: str) -> None:
    value = _fix_category(_envelope(kind="tool_arguments"), "tool")
    value["payload"] = {"kind": "tool_arguments", "args": {field: "secret-value"}}
    with pytest.raises(RunEvidenceError, match="secret_bearing_field_rejected"):
        validate_run_evidence(value)


def test_excluded_provider_reasoning_is_content_free_and_explicit() -> None:
    value = _fix_category(
        _envelope(
            kind="displayed_thinking",
            capture_state="excluded",
            reason="provider_hidden_reasoning_excluded",
        ),
        "thinking",
    )
    validated = validate_run_evidence(value)
    assert validated.canonical_bytes == b""
    assert validated.envelope["byteCount"] == 0


def test_duplicate_is_idempotent_but_changed_immutable_identity_collides() -> None:
    async def scenario() -> None:
        repo = MemoryRunEvidenceRepository()
        value = _fix_category(_envelope(), "admission")
        assert await _accepted(repo, value) == "accepted"
        assert await _accepted(repo, value) == "duplicate"
        changed = dict(value)
        changed["payload"] = {
            "kind": "admitted_request",
            "request": {"role": "user", "content": "changed"},
        }
        changed["digest"] = (
            "sha256:" + hashlib.sha256(_canonical_bytes(changed["payload"])).hexdigest()
        )
        changed["byteCount"] = len(_canonical_bytes(changed["payload"]))
        with pytest.raises(DuplicateCollision):
            await _accepted(repo, changed)
        assert len(repo.items) == 1
        assert len(repo.events) == 1

    asyncio.run(scenario())


def test_rejection_record_is_content_free_even_for_unvalidated_input() -> None:
    async def scenario() -> None:
        repo = MemoryRunEvidenceRepository()
        value = _fix_category(_envelope(), "admission")
        value["secretField"] = "must never be retained"
        with pytest.raises(RunEvidenceError):
            await _accepted(repo, value)
        assert repo.rejections
        record = repo.rejections[-1]
        assert set(record) <= {"id", "reason", "byteCount", "occurredAt"}
        assert {"id", "reason", "byteCount", "occurredAt"} <= set(record)
        assert record["reason"] == "ingest_rejected"
        assert record["byteCount"] == 0
        assert "secretField" not in json.dumps(repo.rejections[-1])

    asyncio.run(scenario())


def test_out_of_order_and_missing_parent_are_retained_and_sorted_deterministically() -> None:
    async def scenario() -> None:
        repo = MemoryRunEvidenceRepository()
        child = _fix_category(
            _envelope(
                sequence=2,
                parent_span_id="fedcba9876543210",
                span_id="1111111111111111",
            ),
            "admission",
        )
        parent = _fix_category(_envelope(sequence=1, span_id="2222222222222222"), "admission")
        await _accepted(repo, child)
        await _accepted(repo, parent)
        manifests, cursor = await repo.manifests(RUN_ID)
        assert cursor is None
        assert [item.sequence for item in manifests] == [1, 2]
        assert manifests[1].parent_span_id == "fedcba9876543210"
        projection = await RunInspectionService(repo).inspect(RUN_ID)
        assert projection is not None
        assert [step["sequence"] for step in projection["steps"]] == [1, 2]

    asyncio.run(scenario())


def test_inspection_paginates_and_never_exposes_payload_or_object_key() -> None:
    async def scenario() -> None:
        repo = MemoryRunEvidenceRepository()
        for sequence in range(3):
            await _accepted(repo, _fix_category(_envelope(sequence=sequence + 1), "admission"))
        first = await RunInspectionService(repo).inspect(RUN_ID, page_size=2)
        assert first is not None
        assert first["nextCursor"]
        rendered = json.dumps(first)
        assert "payload" not in rendered
        assert "runs/" not in rendered
        # Counts describe the run-wide authoritative summary, while the
        # current page contains only two evidence manifests.
        assert first["completeness"]["capturedEvidenceCount"] == 3

    asyncio.run(scenario())


def test_run_wide_pagination_has_bounded_pages_and_truthful_terminal_completeness() -> None:
    async def scenario() -> None:
        repo = MemoryRunEvidenceRepository()
        for sequence in range(5):
            await _accepted(
                repo,
                _fix_category(_envelope(sequence=sequence + 1), "admission"),
            )
        service = RunInspectionService(repo)
        cursor: str | None = None
        seen: list[str] = []
        pages = []
        while True:
            page = await service.inspect(RUN_ID, cursor=cursor, page_size=2)
            assert page is not None
            pages.append(page)
            seen.extend(item["evidenceId"] for step in page["steps"] for item in step["evidence"])
            next_cursor = page["nextCursor"]
            assert next_cursor is None or isinstance(next_cursor, str)
            cursor = next_cursor
            if cursor is None:
                break
        assert len(pages) == 3
        assert len(seen) == 5
        assert len(set(seen)) == 5
        assert pages[0]["completeness"]["state"] == "partial"
        assert "page_truncated" in pages[0]["partialReasons"]
        assert pages[-1]["completeness"]["state"] == "complete"
        assert pages[-1]["partial"] is False

    asyncio.run(scenario())


def test_inspection_step_ids_are_canonical_uuid_values_and_stable() -> None:
    async def scenario() -> None:
        repo = MemoryRunEvidenceRepository()
        await _accepted(repo, _fix_category(_envelope(), "admission"))
        first = await RunInspectionService(repo).inspect(RUN_ID)
        second = await RunInspectionService(repo).inspect(RUN_ID)
        assert first is not None and second is not None
        first_ids = [UUID(step["stepId"]) for step in first["steps"]]
        second_ids = [UUID(step["stepId"]) for step in second["steps"]]
        assert first_ids == second_ids
        assert len(first_ids) == len(set(first_ids))

    asyncio.run(scenario())


def test_invalidation_is_run_scoped_and_content_free() -> None:
    async def scenario() -> None:
        repo = MemoryRunEvidenceRepository()
        await _accepted(repo, _fix_category(_envelope(), "admission"))
        event = repo.events[-1]
        assert event["type"] == "observation.changed"
        assert event["runId"] == RUN_ID
        assert event["data"] == {
            "resourceType": "run",
            "resourceId": RUN_ID,
            "capturePolicy": "metadata_only",
        }
        assert not any(
            token in json.dumps(event).lower()
            for token in ("digest", "evidenceid", "payload", "object_key", "reason")
        )

    asyncio.run(scenario())


def test_manual_delete_is_idempotent_and_preserves_deleted_metadata_without_bytes() -> None:
    async def scenario() -> None:
        repo = MemoryRunEvidenceRepository()
        value = _fix_category(_envelope(), "admission")
        await _accepted(repo, value)
        first = await repo.delete(RUN_ID, "delete-once")
        second = await repo.delete(RUN_ID, "delete-once")
        assert first == second
        manifest = (await repo.manifests(RUN_ID))[0][0]
        assert manifest.capture_state == "deleted"
        assert manifest.digest is None and manifest.byte_count == 0
        with pytest.raises(FileNotFoundError):
            await repo.stream(RUN_ID, manifest.evidence_id)

    asyncio.run(scenario())


def test_object_store_is_scoped_to_canonical_run_and_evidence_uuid_keys() -> None:
    async def scenario() -> None:
        store = MemoryRunEvidenceObjectStore()
        data = b"evidence"
        digest = "sha256:" + hashlib.sha256(data).hexdigest()
        evidence_id = "22222222-2222-4222-8222-222222222222"
        await store.put(RUN_ID, evidence_id, data, media_type="text/plain", digest=digest)
        assert list(store.objects) == [run_evidence_object_key(RUN_ID, evidence_id)]
        with pytest.raises(ValueError):
            run_evidence_object_key(RUN_ID, "../../evaluation-artifact")
        assert await store.get(RUN_ID, evidence_id, digest=digest, byte_count=len(data)) == data

    asyncio.run(scenario())


def test_object_store_digest_and_byte_count_fail_closed() -> None:
    async def scenario() -> None:
        store = MemoryRunEvidenceObjectStore()
        with pytest.raises(ValueError):
            await store.put(
                RUN_ID,
                "22222222-2222-4222-8222-222222222222",
                b"bad",
                media_type="text/plain",
                digest="sha256:" + "0" * 64,
            )

    asyncio.run(scenario())


def test_ingestion_outage_does_not_ack_or_emit_a_false_capture() -> None:
    class FailingRepository(MemoryRunEvidenceRepository):
        async def ingest(self, item: Any) -> str:
            del item
            raise RuntimeError("postgres unavailable")

    async def scenario() -> None:
        repo = FailingRepository()
        with pytest.raises(RuntimeError, match="postgres unavailable"):
            await _accepted(repo, _fix_category(_envelope(), "admission"))
        assert repo.items == {}
        assert repo.events == []

    asyncio.run(scenario())


def test_stream_is_bounded_to_64k_chunks_and_digest_verified() -> None:
    async def scenario() -> None:
        repo = MemoryRunEvidenceRepository()
        text = "x" * 100_000
        value = _fix_category(_envelope(kind="compiled_prompt"), "prompt")
        value["payload"] = {"kind": "compiled_prompt", "prompt": text}
        value["digest"] = "sha256:" + hashlib.sha256(text.encode()).hexdigest()
        value["byteCount"] = len(text)
        await _accepted(repo, value)
        _manifest, stream = await repo.stream(RUN_ID, str(value["evidenceId"]))
        chunks = [chunk async for chunk in stream]
        assert len(chunks) == 2
        assert all(len(chunk) <= 64 * 1024 for chunk in chunks)
        assert b"".join(chunks) == text.encode()

    asyncio.run(scenario())


def test_captured_zero_byte_text_round_trips_without_becoming_excluded() -> None:
    async def scenario() -> None:
        repo = MemoryRunEvidenceRepository()
        value = _fix_category(_envelope(kind="generation_output"), "generation")
        value["payload"] = {"kind": "generation_output", "text": ""}
        value["digest"] = "sha256:" + hashlib.sha256(b"").hexdigest()
        value["byteCount"] = 0
        await _accepted(repo, value)
        manifest, stream = await repo.stream(RUN_ID, str(value["evidenceId"]))
        assert manifest.capture_state == "captured"
        assert manifest.byte_count == 0
        assert [chunk async for chunk in stream] == []

    asyncio.run(scenario())


def test_stream_verifies_digest_before_returning_tampered_response_bytes() -> None:
    async def scenario() -> None:
        repo = MemoryRunEvidenceRepository()
        value = _fix_category(_envelope(), "admission")
        await _accepted(repo, value)
        evidence_id = str(value["evidenceId"])
        manifest, _data, fingerprint, envelope = repo.items[evidence_id]
        repo.items[evidence_id] = (manifest, b"tampered", fingerprint, envelope)
        _manifest, stream = await repo.stream(RUN_ID, evidence_id)
        streamed: list[bytes] = []
        with pytest.raises((ValueError, FileNotFoundError)):
            async for chunk in stream:
                streamed.append(chunk)
        assert streamed == []

    asyncio.run(scenario())


def test_payload_limit_rejects_before_authoritative_persistence() -> None:
    value = _fix_category(_envelope(kind="compiled_prompt"), "prompt")
    text = "x" * (MAX_EVIDENCE_BYTES + 1)
    value["payload"] = {"kind": "compiled_prompt", "prompt": text}
    value["digest"] = "sha256:" + hashlib.sha256(text.encode()).hexdigest()
    value["byteCount"] = len(text)
    with pytest.raises(RunEvidenceError, match="payload_limit_exceeded"):
        validate_run_evidence(value)


def test_deleted_projection_is_truthful_and_content_free() -> None:
    async def scenario() -> None:
        repo = MemoryRunEvidenceRepository()
        await _accepted(repo, _fix_category(_envelope(), "admission"))
        await repo.delete(RUN_ID, "operator-confirmation")
        projection = await RunInspectionService(repo).inspect(RUN_ID)
        assert projection is not None
        assert projection["completeness"]["state"] == "deleted"
        assert projection["steps"][0]["partial"] is True
        assert "operator-confirmation" not in json.dumps(projection)

    asyncio.run(scenario())


def test_capture_disabled_state_has_no_payload_or_digest() -> None:
    value = _fix_category(
        _envelope(
            kind="generation_output",
            capture_state="excluded",
            reason="capture_disabled",
        ),
        "generation",
    )
    validated = validate_run_evidence(value)
    assert validated.canonical_bytes == b""
    assert validated.envelope["payload"] is None
    assert validated.envelope["digest"] is None


def test_binary_payload_is_not_buffered_when_declared_too_large() -> None:
    value = _fix_category(_envelope(kind="model_visible_attachment"), "context")
    value["payload"] = {**value["payload"], "contentBase64": "A" * 1_398_105}
    with pytest.raises(RunEvidenceError, match="payload_limit_exceeded"):
        validate_run_evidence(value)


def test_migration_is_restart_safe_and_keeps_object_bytes_out_of_postgres() -> None:
    sql = (
        REPOSITORY_ROOT
        / "services/aura-observatory/migrations/postgresql/versions/0016_run_evidence.sql"
    ).read_text(encoding="utf-8")
    assert sql.count("CREATE TABLE IF NOT EXISTS") >= 4
    assert sql.count("CREATE INDEX IF NOT EXISTS") >= 4
    assert "INSERT INTO observatory_migration_ledger" in sql
    assert "WHERE NOT EXISTS" in sql
    assert "payload" not in sql.lower()
    assert "object_key" in sql
    assert "content_free" in sql


def test_durable_collision_deletion_and_recovery_state_is_separate_and_resumable() -> None:
    sql = (
        REPOSITORY_ROOT
        / "services/aura-observatory/migrations/postgresql/versions/0016_run_evidence.sql"
    ).read_text(encoding="utf-8")
    # Stable identity collisions are durable, while deletion and object
    # recovery each have independent state machines.  These checks keep a
    # restart from silently turning a collision or partial delete into a
    # captured manifest.
    assert "observatory_run_evidence_collisions" in sql
    assert "observatory_run_evidence_orphans" in sql
    assert "observatory_run_evidence_deletions" in sql
    assert "UNIQUE(event_id)" in sql
    assert "UNIQUE(run_id, evidence_id)" in sql
    assert "UNIQUE(run_id, idempotency_key)" in sql
    assert "status IN ('pending', 'recovered')" in sql
    assert "status IN ('in_progress', 'completed', 'failed')" in sql
    assert "audit_mode VARCHAR(32) NOT NULL DEFAULT 'content_free'" in sql
    assert "observatory_run_evidence_audit" in sql
    assert "delete_requested" in (
        REPOSITORY_ROOT
        / "services/aura-observatory/src/aura_observatory/adapters/run_evidence.py"
    ).read_text(encoding="utf-8")
    assert "object_delete_failed" in (
        REPOSITORY_ROOT
        / "services/aura-observatory/src/aura_observatory/adapters/run_evidence.py"
    ).read_text(encoding="utf-8")


def test_transport_ack_is_explicit_for_terminal_malformed_messages() -> None:
    class Message:
        data = b"not-json"

        def __init__(self) -> None:
            self.acked = False

        async def ack(self) -> None:
            self.acked = True

    class Transport(NatsRunEvidenceTransport):
        def __init__(self, message: Message) -> None:
            super().__init__(None)
            self.message = message

        async def messages(self, stop: asyncio.Event):
            yield self.message
            stop.set()

    async def scenario() -> None:
        message = Message()
        stop = asyncio.Event()
        await Transport(message).consume(
            RunEvidenceIngestionService(MemoryRunEvidenceRepository()), stop
        )
        assert message.acked is True

    asyncio.run(scenario())


@pytest.mark.asyncio
async def test_inspection_and_evidence_routes_are_owner_only_no_store_and_nosniff() -> None:
    app, settings, session_id, _session = await _owner_app()
    value = _fix_category(_envelope(), "admission")
    await app.state.run_evidence_ingest.ingest(value)
    evidence_id = str(value["evidenceId"])
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=settings.public_origin,
        cookies={settings.effective_session_cookie_name: session_id},
    ) as client:
        inspection = await client.get(f"/api/v1/runs/{RUN_ID}/inspection")
        streamed = await client.get(f"/api/v1/runs/{RUN_ID}/evidence/{evidence_id}")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=settings.public_origin
    ) as anonymous_client:
        unauthenticated = await anonymous_client.get(f"/api/v1/runs/{RUN_ID}/inspection")
    assert inspection.status_code == 200
    assert inspection.headers["cache-control"] == "no-store"
    assert streamed.status_code == 200
    assert streamed.headers["cache-control"] == "no-store"
    assert streamed.headers["x-content-type-options"] == "nosniff"
    assert streamed.headers["content-length"] == str(
        len(validate_run_evidence(value).canonical_bytes)
    )
    assert streamed.content == validate_run_evidence(value).canonical_bytes
    assert unauthenticated.status_code == 401
    assert "runs/" not in streamed.text


@pytest.mark.asyncio
async def test_mixed_sse_future_cursor_does_not_replay_run_evidence_history() -> None:
    app, settings, session_id, _session = await _owner_app()
    await app.state.run_evidence_ingest.ingest(_fix_category(_envelope(), "admission"))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=settings.public_origin,
        cookies={settings.effective_session_cookie_name: session_id},
    ) as client:
        response = await client.get(
            "/api/v1/events",
            headers={"Last-Event-ID": "99999999-9999-4999-8999-999999999999"},
        )
    assert response.status_code == 200
    assert "event: snapshot" in response.text
    assert "event: observation.changed" not in response.text
    assert "evidenceId" not in response.text


def test_adr_span_operations_correlation_metrics_and_evaluators_are_registered() -> None:
    async def scenario() -> None:
        metrics = ObservatoryMetrics(deployment_id="test-deployment")
        repo = MemoryRunEvidenceRepository()
        value = _fix_category(_envelope(), "admission")
        await RunEvidenceIngestionService(repo, metrics=metrics).ingest(value)
        await RunInspectionService(repo, metrics=metrics).inspect(RUN_ID)
        await RunEvidenceDeletionService(repo, metrics=metrics).delete(
            RUN_ID, "44444444-4444-4444-8444-444444444444"
        )
        spans = metrics.component_spans
        operations = {span.operation for span in spans}
        assert {
            "run_evidence.ingest_validate",
            "run_evidence.ingest_deduplicate",
            "run_evidence.manifest_write",
            "run_evidence.object_write",
            "run_inspection.metadata_query",
            "run_inspection.manifest_query",
            "run_inspection.merge",
            "run_evidence.object_delete",
            "run_evidence.deletion_finalize",
        } <= operations
        assert len({span.span_id for span in spans}) == len(spans)
        assert len({span.trace_id for span in spans}) == 1
        assert all(len(span.trace_id) == 32 for span in spans)
        assert all(len(span.span_id) == 16 for span in spans)
        for span in spans:
            assert span.component_id in {
                "aura.observatory.run_evidence_ingestion",
                "aura.observatory.run_evidence_storage",
                "aura.observatory.run_inspection_query",
            }
            assert span.component_version == "1.0.0"
            assert span.deployment_id == "test-deployment"
            assert span.outcome in {
                "success",
                "duplicate",
                "failure",
                "rejected",
                "partial",
            }
        assert {
            "run_evidence_ingest_outcome_count",
            "run_evidence_storage_outcome_count",
            "run_evidence_deletion_outcome_count",
            "run_evidence_ingest_validation_latency_ms",
            "run_evidence_ingest_dedupe_latency_ms",
            "run_evidence_deletion_latency_ms",
            "run_inspection_query_latency_ms",
            "run_inspection_merge_latency_ms",
        } <= {name for name, _labels in metrics.values}

    asyncio.run(scenario())

    manifest_root = REPOSITORY_ROOT / "services/aura-observatory/resources/component-manifests"
    manifests = [
        yaml.safe_load(path.read_text(encoding="utf-8"))
        for path in manifest_root.glob("run-evidence-*.yaml")
    ]
    evaluators = {
        evaluator
        for manifest in manifests
        for evaluator in manifest["evaluators"]
    }
    assert {
        "run_evidence_schema_compatibility",
        "run_evidence_failure_isolation",
        "observatory_run_evidence_privacy",
        "run_evidence_deletion_recovery",
        "run_inspection_projection_truth",
    } <= evaluators


@pytest.mark.asyncio
async def test_delete_route_requires_confirmation_csrf_and_is_idempotent() -> None:
    app, settings, session_id, session = await _owner_app()
    await app.state.run_evidence_ingest.ingest(_fix_category(_envelope(), "admission"))
    key = "33333333-3333-4333-8333-333333333333"
    base_headers = {
        "Origin": settings.public_origin,
        "Idempotency-Key": key,
        "X-Observatory-Confirmation": "DELETE_RUN_EVIDENCE",
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=settings.public_origin,
        cookies={settings.effective_session_cookie_name: session_id},
    ) as client:
        missing_csrf = await client.request(
            "DELETE", f"/api/v1/runs/{RUN_ID}/evidence", headers=base_headers
        )
        headers = {**base_headers, "X-Observatory-CSRF": session.csrf_token}
        first = await client.request("DELETE", f"/api/v1/runs/{RUN_ID}/evidence", headers=headers)
        second = await client.request("DELETE", f"/api/v1/runs/{RUN_ID}/evidence", headers=headers)
    assert missing_csrf.status_code == 403
    assert first.status_code == 202
    assert second.status_code == 202
    assert first.json() == second.json()
    assert first.headers["cache-control"] == "no-store"
    assert first.json()["auditMode"] == "content_free"
    assert "payload" not in first.text
