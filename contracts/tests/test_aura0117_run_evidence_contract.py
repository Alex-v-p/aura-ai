"""AURA-0117 governed production run-evidence contract invariants."""

from __future__ import annotations

import base64
import binascii
import copy
import hashlib
import json
import re
import struct
import subprocess
import zlib
from pathlib import Path
from typing import Any, Iterator

ROOT = Path(__file__).parents[2]
EVIDENCE_PATH = ROOT / "contracts/events/runs/v1/run-evidence.schema.json"
OPENAPI_PATH = ROOT / "contracts/openapi/observatory-v1.yaml"
GENERATED_PATH = (
    ROOT
    / "frontend/libs/platform/observatory-api-client/src/lib/generated/observatory-api.ts"
)


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _nodes(value: Any) -> Iterator[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _nodes(child)
    elif isinstance(value, list):
        for child in value:
            yield from _nodes(child)


def _operation(document: dict[str, Any], path: str, method: str) -> dict[str, Any]:
    return document["paths"][path][method]


def _inspection_accounting_errors(inspection: dict[str, Any]) -> list[str]:
    completeness = inspection["completeness"]
    expected = completeness["expectedEvidenceCount"]
    accounted = sum(
        completeness[name]
        for name in (
            "capturedEvidenceCount",
            "excludedEvidenceCount",
            "partialEvidenceCount",
            "droppedEvidenceCount",
            "deletedEvidenceCount",
        )
    )
    return [] if expected is None or expected == accounted else ["evidence count mismatch"]


def _parameter(document: dict[str, Any], reference: dict[str, str]) -> dict[str, Any]:
    return document["components"]["parameters"][reference["$ref"].rsplit("/", 1)[-1]]


def _schema_errors(
    instance: Any, schema: Any, root: dict[str, Any], location: str = "$"
) -> list[str]:
    if schema is False:
        return [f"{location}: prohibited"]
    if schema is True:
        return []
    if "$ref" in schema:
        target: Any = root
        for part in schema["$ref"][2:].split("/"):
            target = target[part.replace("~1", "/").replace("~0", "~")]
        return _schema_errors(instance, target, root, location)
    errors: list[str] = []
    if "allOf" in schema:
        for child in schema["allOf"]:
            errors.extend(_schema_errors(instance, child, root, location))
    if "oneOf" in schema:
        matches = sum(not _schema_errors(instance, child, root, location) for child in schema["oneOf"])
        if matches != 1:
            errors.append(f"{location}: expected exactly one variant, got {matches}")
    if "anyOf" in schema and not any(
        not _schema_errors(instance, child, root, location) for child in schema["anyOf"]
    ):
        errors.append(f"{location}: expected an allowed variant")
    if "if" in schema:
        branch = "then" if not _schema_errors(instance, schema["if"], root, location) else "else"
        if branch in schema:
            errors.extend(_schema_errors(instance, schema[branch], root, location))
    if "const" in schema and instance != schema["const"]:
        errors.append(f"{location}: wrong constant")
    if "enum" in schema and instance not in schema["enum"]:
        errors.append(f"{location}: unsupported enum value")
    expected = schema.get("type")
    if expected is not None:
        choices = expected if isinstance(expected, list) else [expected]
        valid = any(
            (
                (choice == "object" and isinstance(instance, dict))
                or (choice == "array" and isinstance(instance, list))
                or (choice == "string" and isinstance(instance, str))
                or (choice == "integer" and isinstance(instance, int) and not isinstance(instance, bool))
                or (choice == "number" and isinstance(instance, (int, float)) and not isinstance(instance, bool))
                or (choice == "boolean" and isinstance(instance, bool))
                or (choice == "null" and instance is None)
            )
            for choice in choices
        )
        if not valid:
            return errors + [f"{location}: wrong type"]
    if isinstance(instance, dict):
        properties = schema.get("properties", {})
        for name in schema.get("required", []):
            if name not in instance:
                errors.append(f"{location}: missing {name}")
        if schema.get("additionalProperties") is False:
            for name in instance.keys() - properties.keys():
                errors.append(f"{location}: undeclared {name}")
        for name, child in properties.items():
            if name in instance:
                errors.extend(_schema_errors(instance[name], child, root, f"{location}.{name}"))
    if isinstance(instance, list):
        if len(instance) < schema.get("minItems", 0):
            errors.append(f"{location}: too few items")
        if len(instance) > schema.get("maxItems", len(instance)):
            errors.append(f"{location}: too many items")
        if schema.get("uniqueItems") and len({json.dumps(item, sort_keys=True) for item in instance}) != len(instance):
            errors.append(f"{location}: duplicate items")
        if "contains" in schema and not any(
            not _schema_errors(item, schema["contains"], root, location) for item in instance
        ):
            errors.append(f"{location}: required item absent")
        for index, item in enumerate(instance):
            if "items" in schema:
                errors.extend(_schema_errors(item, schema["items"], root, f"{location}[{index}]"))
    if isinstance(instance, str):
        if len(instance) < schema.get("minLength", 0):
            errors.append(f"{location}: too short")
        if len(instance) > schema.get("maxLength", len(instance)):
            errors.append(f"{location}: too long")
        if "pattern" in schema and re.fullmatch(schema["pattern"], instance) is None:
            errors.append(f"{location}: lexical mismatch")
    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if instance < schema.get("minimum", instance):
            errors.append(f"{location}: below minimum")
        if instance > schema.get("maximum", instance):
            errors.append(f"{location}: above maximum")
    return errors


def _canonical_evidence_bytes(envelope: dict[str, Any]) -> bytes:
    payload = envelope["payload"]
    if payload.get("kind") == "model_visible_attachment":
        try:
            return base64.b64decode(payload["contentBase64"], validate=True)
        except (binascii.Error, ValueError) as exc:
            raise AssertionError("malformed base64") from exc
    if envelope["mediaType"] == "application/json":
        script = """
const fs = require('node:fs');
const value = JSON.parse(fs.readFileSync(0, 'utf8'));
function canonical(v) {
  if (v === null || typeof v !== 'object') return JSON.stringify(v);
  if (Array.isArray(v)) return '[' + v.map(canonical).join(',') + ']';
  return '{' + Object.keys(v).sort().map(k => JSON.stringify(k) + ':' + canonical(v[k])).join(',') + '}';
}
process.stdout.write(canonical(value));
"""
        result = subprocess.run(
            ["node", "-e", script],
            input=json.dumps(payload, ensure_ascii=False, allow_nan=False),
            text=True,
            capture_output=True,
            check=True,
        )
        return result.stdout.encode()
    for field in ("prompt", "text", "content"):
        value = payload.get(field)
        if isinstance(value, str):
            return value.encode()
    raise AssertionError("text payload has no canonical content field")


def _validate_approved_media(media_type: str, content: bytes) -> list[str]:
    if media_type == "image/png":
        if not content.startswith(b"\x89PNG\r\n\x1a\n"):
            return ["malformed png"]
        offset = 8
        kinds: set[bytes] = set()
        while offset + 12 <= len(content):
            length = int.from_bytes(content[offset : offset + 4], "big")
            kind = content[offset + 4 : offset + 8]
            data_end = offset + 8 + length
            if data_end + 4 > len(content):
                return ["malformed png"]
            expected_crc = int.from_bytes(content[data_end : data_end + 4], "big")
            if zlib.crc32(kind + content[offset + 8 : data_end]) & 0xFFFFFFFF != expected_crc:
                return ["malformed png"]
            kinds.add(kind)
            offset = data_end + 4
            if kind == b"IEND":
                break
        if not {b"IHDR", b"IDAT", b"IEND"} <= kinds or offset != len(content):
            return ["malformed png"]
    elif media_type == "image/jpeg" and not (
        content.startswith(b"\xff\xd8") and content.endswith(b"\xff\xd9")
    ):
        return ["malformed jpeg"]
    elif media_type == "image/webp" and not (
        len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP"
    ):
        return ["malformed webp"]
    elif media_type == "application/pdf" and not (
        content.startswith(b"%PDF-") and content.rstrip().endswith(b"%%EOF")
    ):
        return ["malformed pdf"]
    elif media_type == "application/json":
        try:
            json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return ["malformed json"]
    elif media_type in {"text/plain", "text/markdown"}:
        try:
            content.decode("utf-8")
        except UnicodeDecodeError:
            return ["malformed utf8 text"]
    return []


def _inspection_surfaces(media_type: str, content: bytes) -> list[bytes]:
    """Return format-decoded text/metadata surfaces used by the reference scanner.

    Runtime OCR and parser conformance remain follow-up implementation duties recorded
    by the signed admission attestation; this helper proves that container-encoded
    metadata is not mistakenly treated as covered by a raw-byte scan.
    """
    surfaces = [content]
    if media_type == "image/png":
        offset = 8
        while offset + 12 <= len(content):
            length = int.from_bytes(content[offset : offset + 4], "big")
            kind = content[offset + 4 : offset + 8]
            data = content[offset + 8 : offset + 8 + length]
            if kind == b"tEXt" and b"\x00" in data:
                surfaces.append(data.split(b"\x00", 1)[1])
            elif kind == b"zTXt" and b"\x00" in data:
                remainder = data.split(b"\x00", 1)[1]
                if remainder[:1] == b"\x00":
                    try:
                        surfaces.append(zlib.decompress(remainder[1:]))
                    except zlib.error:
                        pass
            elif kind == b"iTXt":
                parts = data.split(b"\x00", 5)
                if len(parts) == 6:
                    compressed, method, text = parts[1], parts[2], parts[5]
                    if compressed == b"\x01" and method == b"\x00":
                        try:
                            text = zlib.decompress(text)
                        except zlib.error:
                            pass
                    surfaces.append(text)
            offset += 12 + length
            if kind == b"IEND":
                break
    elif media_type == "application/pdf":
        for match in re.finditer(rb"stream\r?\n(.*?)\r?\nendstream", content, re.DOTALL):
            stream = match.group(1)
            surfaces.append(stream)
            try:
                surfaces.append(zlib.decompress(stream))
            except zlib.error:
                pass
    return surfaces


def _png_with_text(text: bytes) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
        + chunk(b"tEXt", b"Comment\x00" + text)
        + chunk(b"IDAT", zlib.compress(b"\x00\x00\x00\x00"))
        + chunk(b"IEND", b"")
    )


def _png_with_compressed_text(text: bytes) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
        + chunk(b"zTXt", b"Comment\x00\x00" + zlib.compress(text))
        + chunk(b"IDAT", zlib.compress(b"\x00\x00\x00\x00"))
        + chunk(b"IEND", b"")
    )


def _pdf_with_text(text: str) -> bytes:
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream = f"BT /F1 12 Tf 72 72 Td ({escaped}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    result = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, obj in enumerate(objects, 1):
        offsets.append(len(result))
        result.extend(f"{index} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref = len(result)
    result.extend(f"xref\n0 {len(objects) + 1}\n".encode())
    result.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        result.extend(f"{offset:010d} 00000 n \n".encode())
    result.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return bytes(result)


def _passed_admission(*modes: str) -> dict[str, Any]:
    return {
        "outcome": "passed",
        "scannerRevision": "secret-scanner-v1",
        "policyRevision": "run-evidence-admission-v1",
        "inspectedAt": "2026-10-25T18:00:00Z",
        "inspectionModes": list(modes),
    }


def _validate_captured_reference(
    envelope: dict[str, Any], seen: dict[str, tuple[Any, ...]]
) -> list[str]:
    schema = _load(EVIDENCE_PATH)
    errors = _schema_errors(envelope, schema, schema)
    if errors or envelope.get("captureState") != "captured":
        return errors
    try:
        canonical = _canonical_evidence_bytes(envelope)
    except AssertionError as exc:
        return [str(exc)]
    if len(canonical) > 1048576:
        errors.append("canonical bytes exceed one MiB")
    errors.extend(_validate_approved_media(envelope["mediaType"], canonical))
    inspection_bytes = b"\n".join(_inspection_surfaces(envelope["mediaType"], canonical))
    lowered = inspection_bytes.lower()
    lexical_markers = (
        b"bearer ", b"password=", b'"password":', b"password:",
        b"authorization: basic", b'"authorization":"basic ', b"api_key=",
        b"client_secret", b"-----begin private key-----",
    )
    token_patterns = (rb"\bAKIA[0-9A-Z]{16}\b", rb"\bghp_[A-Za-z0-9]{20,255}\b")
    if any(marker in lowered for marker in lexical_markers) or any(
        re.search(pattern, inspection_bytes) for pattern in token_patterns
    ):
        errors.append("prohibited secret-bearing evidence")
    if envelope["byteCount"] != len(canonical):
        errors.append("byteCount mismatch")
    if envelope["digest"] != f"sha256:{hashlib.sha256(canonical).hexdigest()}":
        errors.append("digest mismatch")
    immutable = tuple(
        json.dumps(envelope[name], sort_keys=True)
        for name in (
            "eventId", "evidenceId", "runId", "traceId", "spanId", "parentSpanId", "sequence", "stepCategory",
            "evidenceKind", "mediaClassification", "mediaType", "digest", "byteCount",
            "capturePolicyRevision", "observedAt", "contentAdmission", "payload",
        )
    )
    for stable_id in (f"event:{envelope['eventId']}", f"evidence:{envelope['evidenceId']}"):
        if stable_id in seen and seen[stable_id] != immutable:
            errors.append("duplicate collision")
        seen.setdefault(stable_id, immutable)
    return errors


def _captured_generation() -> dict[str, Any]:
    payload = {"kind": "generation_output", "text": "hello"}
    canonical = b"hello"
    return {
        "schemaVersion": 1,
        "contractId": "run-evidence-v1",
        "eventId": "123e4567-e89b-42d3-a456-426614174000",
        "evidenceId": "123e4567-e89b-42d3-a456-426614174001",
        "runId": "123e4567-e89b-42d3-a456-426614174002",
        "traceId": "a" * 32,
        "spanId": "b" * 16,
        "parentSpanId": None,
        "sequence": 4,
        "stepCategory": "generation",
        "evidenceKind": "generation_output",
        "mediaClassification": "text",
        "mediaType": "text/plain",
        "digest": f"sha256:{hashlib.sha256(canonical).hexdigest()}",
        "byteCount": len(canonical),
        "capturePolicyRevision": "owner-full-content-v1",
        "observedAt": "2026-10-25T18:00:00Z",
        "emittedAt": "2026-10-25T18:00:00.001Z",
        "delivery": {
            "kind": "initial",
            "producerId": "aura-core-api",
            "deliveryId": "123e4567-e89b-42d3-a456-426614174003",
            "attempt": 1,
        },
        "contentAdmission": _passed_admission("decoded_text"),
        "captureState": "captured",
        "reason": None,
        "payload": payload,
    }


def test_run_evidence_v1_is_closed_and_bounded() -> None:
    schema = _load(EVIDENCE_PATH)
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["$ref"] == "#/$defs/envelope"
    envelope = schema["$defs"]["envelope"]
    assert envelope["additionalProperties"] is False
    assert envelope["properties"]["schemaVersion"]["const"] == 1
    assert envelope["properties"]["contractId"]["const"] == "run-evidence-v1"
    assert envelope["properties"]["sequence"]["maximum"] == 9007199254740991
    assert envelope["properties"]["byteCount"]["maximum"] == 1048576

    for node in _nodes(schema):
        if node.get("type") == "object" and "properties" in node:
            assert node.get("additionalProperties") is False, node
        if node.get("type") == "array":
            assert "maxItems" in node, node
        if node.get("type") == "string" and not {
            "const",
            "enum",
        }.intersection(node):
            assert "maxLength" in node, node


def test_concrete_reference_validation_accepts_exact_evidence_and_rejects_bad_instances() -> None:
    seen: dict[str, tuple[Any, ...]] = {}
    valid = _captured_generation()
    assert _validate_captured_reference(valid, seen) == []
    under_inspected_text = copy.deepcopy(valid)
    under_inspected_text["contentAdmission"] = _passed_admission("metadata")
    assert _schema_errors(under_inspected_text, _load(EVIDENCE_PATH), _load(EVIDENCE_PATH))

    mismatched_kind = copy.deepcopy(valid)
    mismatched_kind["evidenceId"] = "123e4567-e89b-42d3-a456-426614174010"
    mismatched_kind["payload"]["kind"] = "displayed_thinking"
    assert _validate_captured_reference(mismatched_kind, {})

    mismatched_media = copy.deepcopy(valid)
    mismatched_media["mediaType"] = "image/png"
    mismatched_media["mediaClassification"] = "image"
    assert _validate_captured_reference(mismatched_media, {})

    unsupported_media = copy.deepcopy(valid)
    unsupported_media["mediaType"] = "application/octet-stream"
    assert _validate_captured_reference(unsupported_media, {})

    oversized = copy.deepcopy(valid)
    oversized["payload"]["text"] = "x" * 1048577
    oversized["byteCount"] = 1048577
    oversized["digest"] = f"sha256:{hashlib.sha256(oversized['payload']['text'].encode()).hexdigest()}"
    assert _validate_captured_reference(oversized, {})

    malformed_attachment = copy.deepcopy(valid)
    malformed_attachment.update(
        evidenceKind="model_visible_attachment",
        stepCategory="context",
        mediaClassification="image",
        mediaType="image/png",
        payload={
            "kind": "model_visible_attachment",
            "attachmentId": "attachment-1",
            "displayName": "image.png",
            "contentBase64": "%%%not-base64%%%",
        },
        contentAdmission=_passed_admission("metadata", "container_structure", "ocr"),
    )
    assert _validate_captured_reference(malformed_attachment, {})

    oversized_attachment = copy.deepcopy(malformed_attachment)
    oversized_attachment["payload"]["contentBase64"] = base64.b64encode(
        b"x" * 1048577
    ).decode()
    oversized_attachment["byteCount"] = 1048576
    oversized_attachment["digest"] = f"sha256:{hashlib.sha256(b'x' * 1048577).hexdigest()}"
    assert "canonical bytes exceed one MiB" in _validate_captured_reference(
        oversized_attachment, {}
    )

    bad_timestamp = copy.deepcopy(valid)
    bad_timestamp["observedAt"] = "not-a-time"
    assert _validate_captured_reference(bad_timestamp, {})
    bad_identifier = copy.deepcopy(valid)
    bad_identifier["eventId"] = "not-a-uuid"
    assert _validate_captured_reference(bad_identifier, {})


def test_reference_canonicalizer_matches_rfc8785_json_fixture() -> None:
    envelope = {
        "mediaType": "application/json",
        "payload": {
            "string": "€\n",
            "numbers": [333333333.33333329, 1e30, 4.50, 2e-3, 1e-27],
            "literals": [None, True, False],
        },
    }
    assert _canonical_evidence_bytes(envelope) == (
        '{"literals":[null,true,false],"numbers":[333333333.3333333,1e+30,4.5,0.002,1e-27],'
        '"string":"€\\n"}'
    ).encode()


def test_reference_validation_enforces_digest_byte_count_and_capture_state_invariants() -> None:
    schema = _load(EVIDENCE_PATH)
    original = _captured_generation()

    bad_digest = copy.deepcopy(original)
    bad_digest["digest"] = "sha256:" + "0" * 64
    assert "digest mismatch" in _validate_captured_reference(bad_digest, {})

    bad_byte_count = copy.deepcopy(original)
    bad_byte_count["byteCount"] += 1
    assert "byteCount mismatch" in _validate_captured_reference(bad_byte_count, {})

    def non_captured(state: str, reason: str, admission: dict[str, Any]) -> dict[str, Any]:
        value = copy.deepcopy(original)
        value.pop("payload")
        value.update(
            captureState=state,
            reason=reason,
            digest=None,
            byteCount=0,
            contentAdmission=admission,
        )
        return value

    rejected = {
        "outcome": "rejected_prohibited_material",
        "scannerRevision": "secret-scanner-v1",
        "policyRevision": "run-evidence-admission-v1",
        "inspectedAt": "2026-10-25T18:00:00Z",
        "inspectionModes": ["decoded_text"],
    }
    for state, reason, admission in (
        ("partial", "bearer_material_rejected", rejected),
        ("dropped", "queue_pressure", {"outcome": "not_applicable"}),
        ("excluded", "provider_hidden_reasoning_excluded", {"outcome": "not_applicable"}),
        ("deleted", "manually_deleted", {"outcome": "not_applicable"}),
    ):
        assert _schema_errors(non_captured(state, reason, admission), schema, schema) == []

    invalid_non_captured = non_captured(
        "dropped", "queue_pressure", {"outcome": "not_applicable"}
    )
    invalid_non_captured["payload"] = original["payload"]
    assert _schema_errors(invalid_non_captured, schema, schema)

    invalid_non_captured = non_captured(
        "deleted", "manually_deleted", {"outcome": "not_applicable"}
    )
    invalid_non_captured["digest"] = original["digest"]
    assert _schema_errors(invalid_non_captured, schema, schema)

    invalid_non_captured = non_captured(
        "excluded", "provider_hidden_reasoning_excluded", {"outcome": "not_applicable"}
    )
    invalid_non_captured["byteCount"] = 1
    assert _schema_errors(invalid_non_captured, schema, schema)


def test_reference_validation_rejects_secrets_and_duplicate_collisions_but_accepts_retry() -> None:
    seen: dict[str, tuple[Any, ...]] = {}
    original = _captured_generation()
    assert _validate_captured_reference(original, seen) == []

    retry = copy.deepcopy(original)
    retry["emittedAt"] = "2026-10-25T18:00:01Z"
    retry["delivery"] = {
        "kind": "retry",
        "producerId": "aura-core-api",
        "deliveryId": "123e4567-e89b-42d3-a456-426614174004",
        "attempt": 2,
        "firstDeliveryId": original["delivery"]["deliveryId"],
    }
    assert _validate_captured_reference(retry, seen) == []

    collision = copy.deepcopy(original)
    collision["payload"]["text"] = "different"
    collision["byteCount"] = len(b"different")
    collision["digest"] = f"sha256:{hashlib.sha256(b'different').hexdigest()}"
    assert "duplicate collision" in _validate_captured_reference(collision, seen)

    event_collision = copy.deepcopy(original)
    event_collision["evidenceId"] = "123e4567-e89b-42d3-a456-426614174030"
    assert "duplicate collision" in _validate_captured_reference(event_collision, seen)

    evidence_collision = copy.deepcopy(original)
    evidence_collision["eventId"] = "123e4567-e89b-42d3-a456-426614174031"
    assert "duplicate collision" in _validate_captured_reference(evidence_collision, seen)

    admission_collision = copy.deepcopy(original)
    admission_collision["contentAdmission"]["scannerRevision"] = "secret-scanner-v2"
    assert "duplicate collision" in _validate_captured_reference(admission_collision, seen)

    secret = copy.deepcopy(original)
    secret["eventId"] = "123e4567-e89b-42d3-a456-426614174020"
    secret["evidenceId"] = "123e4567-e89b-42d3-a456-426614174021"
    secret["payload"]["text"] = "Bearer abc.def.ghi"
    secret["byteCount"] = len(secret["payload"]["text"].encode())
    secret["digest"] = f"sha256:{hashlib.sha256(secret['payload']['text'].encode()).hexdigest()}"
    assert "prohibited secret-bearing evidence" in _validate_captured_reference(secret, {})

    attachment_secret = copy.deepcopy(original)
    png_secret = _png_with_text(b"password=hunter2")
    attachment_secret.update(
        eventId="123e4567-e89b-42d3-a456-426614174022",
        evidenceId="123e4567-e89b-42d3-a456-426614174023",
        evidenceKind="model_visible_attachment",
        stepCategory="context",
        mediaClassification="image",
        mediaType="image/png",
        payload={
            "kind": "model_visible_attachment",
            "attachmentId": "attachment-secret",
            "displayName": "image.png",
            "contentBase64": base64.b64encode(png_secret).decode(),
        },
        contentAdmission=_passed_admission("metadata", "container_structure", "ocr"),
    )
    attachment_secret["byteCount"] = len(png_secret)
    attachment_secret["digest"] = f"sha256:{hashlib.sha256(png_secret).hexdigest()}"
    assert "prohibited secret-bearing evidence" in _validate_captured_reference(
        attachment_secret, {}
    )

    compressed_metadata_secret = copy.deepcopy(attachment_secret)
    compressed_png = _png_with_compressed_text(b"password=hunter2")
    compressed_metadata_secret["eventId"] = "123e4567-e89b-42d3-a456-426614174024"
    compressed_metadata_secret["evidenceId"] = "123e4567-e89b-42d3-a456-426614174025"
    compressed_metadata_secret["payload"]["contentBase64"] = base64.b64encode(
        compressed_png
    ).decode()
    compressed_metadata_secret["byteCount"] = len(compressed_png)
    compressed_metadata_secret["digest"] = (
        f"sha256:{hashlib.sha256(compressed_png).hexdigest()}"
    )
    assert "prohibited secret-bearing evidence" in _validate_captured_reference(
        compressed_metadata_secret, {}
    )

    rejected = copy.deepcopy(secret)
    rejected.pop("payload")
    rejected.update(
        captureState="partial",
        reason="bearer_material_rejected",
        digest=None,
        byteCount=0,
        contentAdmission={
            "outcome": "rejected_prohibited_material",
            "scannerRevision": "secret-scanner-v1",
            "policyRevision": "run-evidence-admission-v1",
            "inspectedAt": "2026-10-25T18:00:00Z",
            "inspectionModes": ["decoded_text"],
        },
    )
    schema = _load(EVIDENCE_PATH)
    assert _schema_errors(rejected, schema, schema) == []

    for index, prohibited in enumerate(
        (
            '{"password":"hunter2"}',
            "Authorization: Basic dXNlcjpwYXNz",
            "client_secret: value",
            "AKIAIOSFODNN7EXAMPLE",
            "ghp_" + "a" * 36,
        ),
        40,
    ):
        candidate = _captured_generation()
        candidate["eventId"] = f"123e4567-e89b-42d3-a456-426614174{index:03d}"
        candidate["evidenceId"] = f"123e4567-e89b-42d3-a456-426614175{index:03d}"
        candidate["payload"]["text"] = prohibited
        encoded = prohibited.encode()
        candidate["byteCount"] = len(encoded)
        candidate["digest"] = f"sha256:{hashlib.sha256(encoded).hexdigest()}"
        assert "prohibited secret-bearing evidence" in _validate_captured_reference(
            candidate, {}
        )


def test_binary_admission_requires_complete_inspection_and_valid_media() -> None:
    schema = _load(EVIDENCE_PATH)

    def attachment(content: bytes, classification: str, media_type: str) -> dict[str, Any]:
        value = _captured_generation()
        value.update(
            evidenceKind="model_visible_attachment",
            stepCategory="context",
            mediaClassification=classification,
            mediaType=media_type,
            payload={
                "kind": "model_visible_attachment",
                "attachmentId": "attachment-safe",
                "displayName": "attachment",
                "contentBase64": base64.b64encode(content).decode(),
            },
            contentAdmission=_passed_admission(
                *("metadata", "container_structure", "ocr")
                if classification == "image"
                else ("decoded_text", "metadata", "container_structure", "ocr")
            ),
            byteCount=len(content),
            digest=f"sha256:{hashlib.sha256(content).hexdigest()}",
        )
        return value

    safe_png = attachment(_png_with_text(b"safe caption"), "image", "image/png")
    assert _validate_captured_reference(safe_png, {}) == []
    under_inspected = copy.deepcopy(safe_png)
    under_inspected["contentAdmission"] = _passed_admission("metadata")
    assert _schema_errors(under_inspected, schema, schema)

    safe_pdf = attachment(_pdf_with_text("safe document"), "document", "application/pdf")
    assert _validate_captured_reference(safe_pdf, {}) == []
    secret_pdf = attachment(
        _pdf_with_text("client_secret: embedded-value"), "document", "application/pdf"
    )
    assert "prohibited secret-bearing evidence" in _validate_captured_reference(
        secret_pdf, {}
    )

    dropped_binary = copy.deepcopy(safe_png)
    dropped_binary.pop("payload")
    dropped_binary.update(
        captureState="dropped",
        reason="queue_pressure",
        digest=None,
        byteCount=0,
        contentAdmission={"outcome": "not_applicable"},
    )
    assert _schema_errors(dropped_binary, schema, schema) == []


def test_evidence_payloads_are_explicit_and_hidden_reasoning_is_unrepresentable() -> None:
    schema = _load(EVIDENCE_PATH)
    payload = schema["$defs"]["payload"]["oneOf"]
    represented: set[str] = set()
    property_names: set[str] = set()
    for variant in payload:
        property_names.update(variant["properties"])
        kind = variant["properties"]["kind"]
        represented.update([kind["const"]] if "const" in kind else kind["enum"])

    assert represented == set(schema["$defs"]["evidenceKind"]["enum"])
    assert {
        "admitted_request",
        "compiled_prompt",
        "conversation_context",
        "memory_chunk",
        "document_chunk",
        "displayed_thinking",
        "generation_output",
        "tool_arguments",
        "tool_result",
        "automation_input",
        "schedule_output",
        "evaluation_output",
    } <= represented
    assert "provider_hidden_reasoning" not in represented
    assert not {
        "credential",
        "credentials",
        "secret",
        "password",
        "bearer",
        "token",
        "authorization",
    }.intersection(property_names)

    non_captured = schema["$defs"]["envelope"]["allOf"][0]["else"]["properties"]
    assert non_captured["payload"] is False
    assert "credential_material_rejected" in schema["$defs"]["boundedReason"]["enum"]
    assert "provider_hidden_reasoning_excluded" in schema["$defs"]["boundedReason"]["enum"]
    assert non_captured["byteCount"]["const"] == 0


def test_every_evidence_kind_has_a_closed_payload_variant_with_bounded_fields() -> None:
    schema = _load(EVIDENCE_PATH)
    evidence_kinds = set(schema["$defs"]["evidenceKind"]["enum"])
    variants = schema["$defs"]["payload"]["oneOf"]
    variant_kinds: set[str] = set()
    for variant in variants:
        properties = variant["properties"]
        kind = properties["kind"]
        values = {kind["const"]} if "const" in kind else set(kind["enum"])
        assert values <= evidence_kinds
        assert set(variant["required"]) == set(properties)
        assert variant["additionalProperties"] is False
        variant_kinds |= values
    assert variant_kinds == evidence_kinds
    # Grouped variants still constrain their payload-specific fields rather
    # than falling back to an untyped object or arbitrary JSON dump.
    assert schema["$defs"]["contentText"]["maxLength"] == 1048576
    assert schema["$defs"]["payload"]["oneOf"][5]["properties"]["contentBase64"]["maxLength"] == 1398104


def test_prohibited_and_unsupported_evidence_is_whole_item_rejection_shape() -> None:
    schema = _load(EVIDENCE_PATH)
    envelope = schema["$defs"]["envelope"]
    rejected = envelope["allOf"][0]["else"]
    reasons = set(schema["$defs"]["boundedReason"]["enum"])
    assert {
        "credential_material_rejected",
        "secret_bearing_field_rejected",
        "bearer_material_rejected",
        "provider_hidden_reasoning_excluded",
        "unsupported_media_type",
        "payload_limit_exceeded",
    } <= reasons
    assert rejected["properties"]["payload"] is False
    assert rejected["properties"]["byteCount"]["const"] == 0
    assert rejected["properties"]["digest"]["type"] == "null"
    media_types = set(schema["$defs"]["mediaType"]["enum"])
    assert "application/octet-stream" not in media_types
    assert "application/x-pem-file" not in media_types
    assert schema["$defs"]["contentText"]["maxLength"] == 1048576
    assert schema["$defs"]["envelope"]["properties"]["byteCount"]["maximum"] == 1048576


def test_inspection_contract_is_owner_authorized_bounded_and_additive() -> None:
    document = _load(OPENAPI_PATH)
    operations = {
        operation_id: _operation(document, path, method)
        for path, method, operation_id in (
            ("/api/v1/runs/{run_id}/inspection", "get", "getRunInspection"),
            ("/api/v1/runs/{run_id}/evidence/{evidence_id}", "get", "getRunEvidence"),
            ("/api/v1/runs/{run_id}/evidence", "delete", "deleteRunEvidence"),
        )
    }
    for operation in operations.values():
        assert operation.get("security", document["security"]) == [{"observatorySession": []}]
        assert {"401", "403"} <= set(operation["responses"])
    for operation_id in ("getRunInspection", "getRunEvidence"):
        assert operation_id in operations
        assert operations[operation_id]["responses"]["200"]["headers"]["Cache-Control"]["schema"]["const"] == "no-store"

    deletion = operations["deleteRunEvidence"]
    parameters = [_parameter(document, item) for item in deletion["parameters"]]
    headers = {item["name"]: item for item in parameters if item["in"] == "header"}
    idempotency_schema = headers["Idempotency-Key"]["schema"]
    assert idempotency_schema == {"$ref": "#/components/schemas/Uuid"}
    assert headers["X-Observatory-CSRF"]["required"] is True
    assert headers["X-Observatory-Confirmation"]["schema"]["const"] == "DELETE_RUN_EVIDENCE"
    assert "requestBody" not in deletion
    assert deletion["responses"]["202"]["headers"]["Cache-Control"]["schema"]["const"] == "no-store"
    assert document["paths"]["/api/v1/events"]["get"]["responses"]["200"]["content"]["text/event-stream"]["schema"] == {"$ref": "#/components/schemas/SsePayload"}
    assert document["paths"]["/api/v1/events"]["get"][
        "x-canonical-run-invalidation-schema"
    ] == "#/components/schemas/RunInspectionInvalidationEvent"


def test_identifiers_retry_and_media_make_duplicate_collision_handling_explicit() -> None:
    schema = _load(EVIDENCE_PATH)
    envelope = schema["$defs"]["envelope"]
    assert {"eventId", "evidenceId", "sequence", "delivery"} <= set(envelope["required"])
    delivery = schema["$defs"]["deliveryIdentity"]["oneOf"]
    assert delivery[0]["properties"]["kind"]["const"] == "initial"
    assert delivery[0]["properties"]["attempt"]["const"] == 1
    assert delivery[1]["properties"]["kind"]["const"] == "retry"
    assert delivery[1]["properties"]["attempt"]["minimum"] == 2
    assert envelope["properties"]["digest"]["oneOf"][0] == {"$ref": "#/$defs/digest"}
    assert "application/octet-stream" not in schema["$defs"]["mediaType"]["enum"]
    assert schema["$defs"]["contentText"]["maxLength"] == 1048576
    assert schema["$defs"]["mediaType"]["enum"] == [
        "application/json",
        "text/plain",
        "text/markdown",
        "image/png",
        "image/jpeg",
        "image/webp",
        "application/pdf",
    ]
    description = schema["description"]
    assert "stable across delivery attempts" in description
    assert "duplicate collision" in description
    assert "out-of-order" in description


def test_inspection_read_and_streaming_evidence_are_owner_only_and_no_store() -> None:
    document = _load(OPENAPI_PATH)
    assert document["info"]["version"] == "1.0.0"
    assert document["info"]["x-run-evidence-contract"] == "run-evidence-v1"
    assert document["info"]["x-capture-policy"] == "metadata_only"
    assert document["security"] == [{"observatorySession": []}]

    inspection = _operation(document, "/api/v1/runs/{run_id}/inspection", "get")
    assert inspection["operationId"] == "getRunInspection"
    assert inspection["responses"]["200"]["headers"]["Cache-Control"]["schema"][
        "const"
    ] == "no-store"
    inspection_schema = document["components"]["schemas"]["RunInspection"]
    assert inspection_schema["additionalProperties"] is False
    assert inspection_schema["properties"]["steps"]["maxItems"] == 1000
    assert {"completeness", "partial", "partialReasons", "nextCursor"} <= set(
        inspection_schema["required"]
    )

    evidence = _operation(
        document, "/api/v1/runs/{run_id}/evidence/{evidence_id}", "get"
    )
    response = evidence["responses"]["200"]
    assert response["headers"]["Cache-Control"]["schema"]["const"] == "no-store"
    assert response["headers"]["Content-Length"]["schema"]["maximum"] == 1048576
    assert response["headers"]["X-Content-Type-Options"]["schema"]["const"] == "nosniff"
    assert "Digest" not in response["headers"]
    assert response["headers"]["X-Observatory-Evidence-Digest"]["schema"] == {
        "$ref": "#/components/schemas/EvidenceDigest"
    }
    assert set(response["content"]) == set(
        document["components"]["schemas"]["RunEvidenceMediaType"]["enum"]
    )
    assert "application/octet-stream" not in response["content"]
    assert all(
        media["schema"] == {"$ref": "#/components/schemas/BoundedEvidenceBytes"}
        for media in response["content"].values()
    )
    assert {"401", "403", "404", "410"} <= set(evidence["responses"])


def test_deletion_is_csrf_protected_idempotent_and_content_free() -> None:
    document = _load(OPENAPI_PATH)
    deletion = _operation(document, "/api/v1/runs/{run_id}/evidence", "delete")
    assert deletion["operationId"] == "deleteRunEvidence"
    parameters = [_parameter(document, item) for item in deletion["parameters"]]
    headers = {item["name"]: item for item in parameters if item["in"] == "header"}
    assert {"X-Observatory-CSRF", "Idempotency-Key", "X-Observatory-Confirmation"} <= set(
        headers
    )
    assert all(item["required"] for item in headers.values())
    assert headers["X-Observatory-Confirmation"]["schema"]["const"] == (
        "DELETE_RUN_EVIDENCE"
    )
    response = deletion["responses"]["202"]
    assert response["headers"]["Cache-Control"]["schema"]["const"] == "no-store"
    status = document["components"]["schemas"]["RunEvidenceDeletion"]
    assert status["additionalProperties"] is False
    assert status["properties"]["auditMode"]["const"] == "content_free"
    assert "failureCode" in status["required"]
    assert status["oneOf"]
    assert "content" not in status["properties"]

    base = {
        "schemaVersion": 1,
        "deletionId": "123e4567-e89b-42d3-a456-426614174010",
        "runId": "123e4567-e89b-42d3-a456-426614174011",
        "status": "accepted",
        "requestedAt": "2026-10-25T18:00:00Z",
        "completedAt": None,
        "deletedEvidenceCount": 0,
        "auditMode": "content_free",
        "failureCode": None,
    }
    assert _schema_errors(base, status, document) == []
    completed = copy.deepcopy(base)
    completed.update(status="completed", completedAt="2026-10-25T18:00:01Z")
    assert _schema_errors(completed, status, document) == []
    completed["completedAt"] = None
    assert _schema_errors(completed, status, document)
    accepted_with_completion = copy.deepcopy(base)
    accepted_with_completion["completedAt"] = "2026-10-25T18:00:01Z"
    assert _schema_errors(accepted_with_completion, status, document)


def test_openapi_projection_rejects_contradictory_manifest_and_completeness_states() -> None:
    document = _load(OPENAPI_PATH)
    schemas = document["components"]["schemas"]
    manifest = {
        "evidenceId": "123e4567-e89b-42d3-a456-426614174001",
        "eventId": "123e4567-e89b-42d3-a456-426614174000",
        "sequence": 1,
        "kind": "generation_output",
        "captureState": "captured",
        "mediaClassification": "text",
        "mediaType": "text/plain",
        "digest": "sha256:" + "a" * 64,
        "byteCount": 5,
        "capturedAt": "2026-10-25T18:00:00Z",
        "reason": None,
    }
    assert _schema_errors(manifest, schemas["RunEvidenceManifest"], document) == []
    contradictory_manifest = copy.deepcopy(manifest)
    contradictory_manifest.update(captureState="deleted", reason="manually_deleted")
    assert _schema_errors(contradictory_manifest, schemas["RunEvidenceManifest"], document)
    deleted_wrong_reason = copy.deepcopy(manifest)
    deleted_wrong_reason.update(
        captureState="deleted", digest=None, byteCount=0, reason="storage_failure"
    )
    assert _schema_errors(deleted_wrong_reason, schemas["RunEvidenceManifest"], document)

    inspection = {
        "schemaVersion": 1,
        "runId": "123e4567-e89b-42d3-a456-426614174002",
        "traceId": "a" * 32,
        "status": "succeeded",
        "startedAt": "2026-10-25T18:00:00Z",
        "finishedAt": "2026-10-25T18:00:01Z",
        "durationMs": 1000,
        "steps": [],
        "completeness": {
            "state": "complete",
            "expectedEvidenceCount": 0,
            "capturedEvidenceCount": 0,
            "excludedEvidenceCount": 0,
            "partialEvidenceCount": 0,
            "droppedEvidenceCount": 0,
            "deletedEvidenceCount": 0,
            "reasons": [],
        },
        "snapshotAt": "2026-10-25T18:00:02Z",
        "partial": False,
        "partialReasons": [],
        "nextCursor": None,
    }
    assert _schema_errors(inspection, schemas["RunInspection"], document) == []
    assert _inspection_accounting_errors(inspection) == []
    contradictory_inspection = copy.deepcopy(inspection)
    contradictory_inspection["partialReasons"] = ["page_truncated"]
    assert _schema_errors(contradictory_inspection, schemas["RunInspection"], document)
    contradictory_complete = copy.deepcopy(inspection)
    contradictory_complete["completeness"]["droppedEvidenceCount"] = 1
    assert _schema_errors(contradictory_complete, schemas["RunInspection"], document)
    deleted_complete = copy.deepcopy(inspection)
    deleted_complete["completeness"]["deletedEvidenceCount"] = 1
    assert _schema_errors(deleted_complete, schemas["RunInspection"], document)

    unavailable = copy.deepcopy(inspection)
    unavailable["completeness"].update(
        state="unavailable", reasons=["export_unavailable"]
    )
    unavailable.update(partial=True, partialReasons=["export_unavailable"])
    assert _schema_errors(unavailable, schemas["RunInspection"], document) == []
    unavailable["partialReasons"] = []
    assert _schema_errors(unavailable, schemas["RunInspection"], document)
    mismatched_accounting = copy.deepcopy(inspection)
    mismatched_accounting["completeness"]["expectedEvidenceCount"] = 1
    assert _inspection_accounting_errors(mismatched_accounting)


def test_follow_up_observability_names_are_bounded_and_content_free() -> None:
    sources = (
        (ROOT / "docs/adr/0017-governed-production-run-evidence.md").read_text(),
        (ROOT / "docs/specifications/observatory-and-evaluation.md").read_text(),
    )
    operation_names = {
        "run_evidence.serialize",
        "run_evidence.queue_offer",
        "run_evidence.transport_publish",
        "run_evidence.ingest_validate",
        "run_evidence.ingest_deduplicate",
        "run_evidence.manifest_write",
        "run_evidence.object_write",
        "run_evidence.object_delete",
        "run_evidence.deletion_finalize",
        "run_inspection.metadata_query",
        "run_inspection.manifest_query",
        "run_inspection.merge",
        "run_evidence.object_stream",
    }
    measurements = {
        "run_evidence_serialize_latency_ms",
        "run_evidence_queue_offer_latency_ms",
        "run_evidence_transport_latency_ms",
        "run_evidence_ingest_validation_latency_ms",
        "run_evidence_ingest_dedupe_latency_ms",
        "run_evidence_manifest_storage_latency_ms",
        "run_evidence_object_storage_latency_ms",
        "run_evidence_deletion_latency_ms",
        "run_inspection_query_latency_ms",
        "run_inspection_merge_latency_ms",
        "run_evidence_export_queue_depth",
        "run_evidence_export_queue_capacity",
    }
    for source in sources:
        assert operation_names <= {name for name in operation_names if name in source}
        assert measurements <= {name for name in measurements if name in source}
        assert "metric label" in source or "metric\nlabels" in source
        assert "content-derived" in source or "evidence content" in source
        assert "future separately versioned metadata" in source

    telemetry = _load(ROOT / "contracts/telemetry/v2/metadata-observation.schema.json")
    dimensions = set(telemetry["$defs"]["Dimensions"]["properties"])
    assert {"operation", "outcome", "status", "dependency", "errorClass"} <= dimensions
    assert {"eventId", "evidenceId", "captureState", "evidenceKind", "mediaClassification"}.isdisjoint(
        dimensions
    )


def test_evidence_errors_are_closed_content_free_and_no_store() -> None:
    document = _load(OPENAPI_PATH)
    problem = document["components"]["schemas"]["RunEvidenceProblem"]
    assert problem["additionalProperties"] is False
    assert set(problem["properties"]) == {"code", "retryable", "traceId"}
    for path, method in (
        ("/api/v1/runs/{run_id}/inspection", "get"),
        ("/api/v1/runs/{run_id}/evidence/{evidence_id}", "get"),
        ("/api/v1/runs/{run_id}/evidence", "delete"),
    ):
        operation = _operation(document, path, method)
        for status, response in operation["responses"].items():
            if status.startswith("2"):
                continue
            resolved = document["components"]["responses"][response["$ref"].rsplit("/", 1)[-1]]
            assert resolved["headers"]["Cache-Control"]["schema"]["const"] == "no-store"
            assert resolved["content"]["application/json"]["schema"] == {
                "$ref": "#/components/schemas/RunEvidenceProblem"
            }


def test_sse_stays_metadata_only_and_existing_contract_is_additive() -> None:
    document = _load(OPENAPI_PATH)
    generic = document["components"]["schemas"]["LiveEvent"]
    run = document["components"]["schemas"]["RunInspectionInvalidationEvent"]
    assert generic["properties"]["capturePolicy"]["const"] == "metadata_only"
    assert "run" in generic["properties"]["resourceType"]["enum"]
    assert run["properties"]["resourceType"]["const"] == "run"
    assert run["properties"]["resourceId"] == {"$ref": "#/components/schemas/Uuid"}
    assert run["properties"]["capturePolicy"]["const"] == "metadata_only"
    assert set(run["properties"]) == {
        "schemaVersion",
        "eventId",
        "eventType",
        "occurredAt",
        "resourceType",
        "resourceId",
        "capturePolicy",
    }
    legacy_run_event = {
        "schemaVersion": 1,
        "eventId": "123e4567-e89b-42d3-a456-426614174050",
        "eventType": "heartbeat",
        "occurredAt": "2026-10-25T18:00:00Z",
        "resourceType": "run",
        "resourceId": None,
        "capturePolicy": "metadata_only",
    }
    assert _schema_errors(legacy_run_event, generic, document) == []
    assert _operation(document, "/api/v1/runs", "get")["operationId"] == "listRuns"
    assert _operation(document, "/api/v1/runs/{run_id}", "get")["operationId"] == (
        "getRun"
    )
    assert _operation(document, "/api/v1/traces/{trace_id}", "get")[
        "operationId"
    ] == "getTrace"


def test_generated_transport_streams_only_the_approved_bounded_response() -> None:
    generated = GENERATED_PATH.read_text(encoding="utf-8")
    assert "readonly getRunInspection:" in generated
    assert "readonly getRunEvidence: { readonly response: Blob;" in generated
    assert "getRunEvidence: { method: 'GET'" in generated
    assert "responseMode: 'blob'" in generated
    assert "readonly deleteRunEvidence:" in generated
    evidence_media_type = next(
        line for line in generated.splitlines() if line.startswith("export type RunEvidenceMediaType =")
    )
    assert "application/octet-stream" not in evidence_media_type


def test_aura_web_and_aura_client_do_not_depend_on_observatory_evidence() -> None:
    forbidden = ("run-evidence", "run_evidence", "observatory-api-client")
    for root in (
        ROOT / "frontend/apps/aura-web",
        ROOT / "frontend/libs/aura",
        ROOT / "frontend/libs/platform/aura-api-client",
    ):
        for path in root.rglob("*"):
            if not path.is_file() or "node_modules" in path.parts:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            assert not any(value in text.lower() for value in forbidden), path
