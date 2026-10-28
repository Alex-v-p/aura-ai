from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from aura_core.domains.artifacts.generated.public import (
    GeneratedArtifact,
    GeneratedArtifactLifecycle,
    GeneratedArtifactLineage,
    GeneratedArtifactRevision,
    safe_filename,
)


def test_artifact_transport_hides_object_reference() -> None:
    now = datetime.now(UTC)
    artifact_id = uuid4()
    revision = GeneratedArtifactRevision(
        artifact_id, 1, "text/plain; charset=utf-8", "a" * 64, 4,
        "aura.research.coordinator@1", None, now,
    )
    item = GeneratedArtifact(
        artifact_id, "issuer", "subject", "generated_text", "Title",
        GeneratedArtifactLifecycle.ACTIVE, 1, 1, now, now,
        GeneratedArtifactLineage(), revision=revision,
    )
    assert safe_filename(item.title, revision.media_type, revision.revision) == "Title.txt-r1"
