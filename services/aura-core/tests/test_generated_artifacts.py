# ruff: noqa: E501

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest
from aura_core.bootstrap.artifact_uow import artifact_service
from aura_core.domains.artifacts.generated.evaluation import (
    EVALUATORS,
    evaluate,
)
from aura_core.domains.artifacts.generated.public import (
    GeneratedArtifact,
    GeneratedArtifactConflict,
    GeneratedArtifactNotFound,
    GeneratedArtifactValidationError,
)
from aura_core.platform.telemetry import MetadataMetrics


@pytest.mark.asyncio
async def test_generated_artifact_revisions_are_immutable_and_owner_scoped() -> None:
    service = artifact_service(testing=True)
    created = await service.create_generated_artifact(
        "issuer",
        "subject",
        kind="generated_text",
        title="A report",
        media_type="text/plain; charset=utf-8",
        content="first",
        creator_component="aura.research.coordinator@1",
    )
    appended = await service.append_revision(
        "issuer",
        "subject",
        created.id,
        expected_version=1,
        media_type="text/plain; charset=utf-8",
        content="second",
        creator_component="aura.research.coordinator@1",
    )
    assert appended.current_revision == 2
    revisions = await service.revisions("issuer", "subject", created.id)
    assert [item.revision for item in revisions] == [1, 2]
    with pytest.raises(GeneratedArtifactNotFound):
        await service.get("other", "subject", created.id)


@pytest.mark.asyncio
async def test_json_is_canonical_and_preview_is_inert() -> None:
    service = artifact_service(testing=True)
    created = await service.create_generated_artifact(
        "issuer",
        "subject",
        kind="generated_data",
        title="Data",
        media_type="application/json",
        content={"z": 1, "a": "two"},
        creator_component="aura.research.coordinator@1",
    )
    _, text, truncated = await service.preview("issuer", "subject", created.id)
    assert text == '{"a":"two","z":1}'
    assert not truncated


@pytest.mark.asyncio
async def test_invalid_media_and_component_are_rejected() -> None:
    service = artifact_service(testing=True)
    with pytest.raises(GeneratedArtifactValidationError):
        await service.create_generated_artifact(
            "issuer", "subject", kind="generated_text", title="x",
            media_type="application/pdf", content="x", creator_component="aura.bad",
        )


@pytest.mark.asyncio
async def test_digest_qualified_key_and_conflict_cannot_overwrite_bytes() -> None:
    service = artifact_service(testing=True)
    artifact_id = uuid4()
    first = await service.create_generated_artifact(
        "issuer", "subject", kind="generated_text", title="x",
        media_type="text/plain; charset=utf-8", content="first",
        creator_component="aura.research.coordinator@1", artifact_id=artifact_id,
    )
    assert first.revision is not None
    assert any(
        key.endswith(f"/{first.revision.sha256}")
        for key in getattr(service.object_store, "objects", {})
    )
    with pytest.raises(GeneratedArtifactConflict):
        await service.create_generated_artifact(
            "issuer", "subject", kind="generated_text", title="x",
            media_type="text/plain; charset=utf-8", content="second",
            creator_component="aura.research.coordinator@1", artifact_id=artifact_id,
        )
    _, _, content = await service.content("issuer", "subject", artifact_id)
    assert content == b"first"


def test_json_duplicate_keys_and_nonfinite_numbers_are_rejected() -> None:
    from aura_core.domains.artifacts.generated.public import canonicalize_content

    with pytest.raises(GeneratedArtifactValidationError):
        canonicalize_content(b'{"a":1,"a":2}', "application/json")
    with pytest.raises(GeneratedArtifactValidationError):
        canonicalize_content("NaN", "application/json")
    with pytest.raises(GeneratedArtifactValidationError):
        canonicalize_content("Infinity", "application/json")
    with pytest.raises(GeneratedArtifactValidationError):
        canonicalize_content('"\\ud800"', "application/json")
    with pytest.raises(GeneratedArtifactValidationError):
        canonicalize_content({"nested": "\ud800"}, "application/json")
    assert canonicalize_content('{"café":"é"}', "application/json") == '{"café":"é"}'.encode()


@pytest.mark.asyncio
async def test_encoded_markup_is_inert_in_preview() -> None:
    service = artifact_service(testing=True)
    created = await service.create_generated_artifact(
        "issuer", "subject", kind="generated_text", title="x",
        media_type="text/markdown; charset=utf-8",
        content="&lt;script&gt;alert(1)&lt;/script&gt; [safe](javascript:alert(1))",
        creator_component="aura.research.coordinator@1",
    )
    _, preview, _ = await service.preview("issuer", "subject", created.id)
    assert "<script" not in preview.lower()
    assert "javascript:" not in preview.lower()


@pytest.mark.asyncio
async def test_append_reserves_source_version_and_same_key_replays() -> None:
    service = artifact_service(testing=True)
    created = await service.create_generated_artifact(
        "issuer", "subject", kind="generated_text", title="x",
        media_type="text/plain; charset=utf-8", content="first",
        creator_component="aura.research.coordinator@1",
    )

    async def append(value: str, key: str):
        return await service.append_revision(
            "issuer", "subject", created.id, expected_version=created.version,
            media_type="text/plain; charset=utf-8", content=value,
            creator_component="aura.research.coordinator@1", idempotency_key=key,
        )

    same = await asyncio.gather(append("second", "same"), append("second", "same"), return_exceptions=True)
    assert all(not isinstance(item, Exception) for item in same)
    successful = [item for item in same if isinstance(item, GeneratedArtifact)]
    assert {item.current_revision for item in successful} == {2}
    with pytest.raises(GeneratedArtifactConflict):
        await append("loser", "different")


@pytest.mark.asyncio
async def test_production_metadata_sink_accepts_artifact_boundaries() -> None:
    metrics = MetadataMetrics()
    service = artifact_service(testing=True, metrics=metrics)
    await service.create_generated_artifact(
        "issuer", "subject", kind="generated_text", title="x",
        media_type="text/plain; charset=utf-8", content="first",
        creator_component="aura.research.coordinator@1",
    )
    names = {item.metric for item in metrics.measurements}
    assert {"artifact_write_outcome_count", "artifact_write_latency_ms"} <= names


def test_generated_artifact_evaluators_are_typed_deterministic_and_private() -> None:
    cases = {
        "generated_artifact_integrity@1": dict(
            digest_matches=True, byte_count_matches=True, immutable=True
        ),
        "generated_artifact_authorization@1": dict(
            owner_scope_valid=True, foreign_scope_reads=0, unauthorized_mutations=0
        ),
        "generated_artifact_deletion_truth@1": dict(
            lifecycle="deleted", object_absent=True, preview_blocked=True
        ),
        "generated_artifact_privacy@1": dict(
            prohibited_fields_seen=0, content_captures=0, identifier_labels=0
        ),
    }
    assert set(EVALUATORS) == set(cases)
    for evaluator_id, kwargs in cases.items():
        result = evaluate(evaluator_id, **kwargs)
        replay = evaluate(evaluator_id, **kwargs)
        assert result.outcome == "pass"
        assert result.replay_key == replay.replay_key
        assert result.payload()["metadata"]
        assert "owner-private" not in repr(result.payload()).lower()
        with pytest.raises((TypeError, ValueError)):
            evaluate(evaluator_id, **{**kwargs, "unexpected": "payload"})


@pytest.mark.asyncio
async def test_reference_nested_autolink_and_entity_obfuscation_are_inert() -> None:
    service = artifact_service(testing=True)
    created = await service.create_generated_artifact(
        "issuer", "subject", kind="generated_text", title="x",
        media_type="text/markdown; charset=utf-8",
        content=(
            "![alt][img]\n[shown][ref]\n[ref]: data:text/html,<script>alert(1)</script>\n"
            "<https://remote.invalid/canary> [nested [label](javascript:bad)](file:///tmp/x) "
            "&#x5b;entity&#x5d;(java&#x73;cript:bad)"
        ),
        creator_component="aura.research.coordinator@1",
    )
    _, preview, _ = await service.preview("issuer", "subject", created.id)
    lowered = preview.lower()
    assert "remote.invalid" not in lowered
    assert "javascript" not in lowered
    assert "data:" not in lowered
    assert "file:" not in lowered
    assert "<script" not in lowered
