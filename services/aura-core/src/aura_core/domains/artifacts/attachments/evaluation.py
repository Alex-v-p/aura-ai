"""Deterministic metadata-only attachment evaluators."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

FIXTURE_REVISION = "conversation-attachments-eval-fixtures-v1"
COMPONENT_VERSIONS = {
    "aura.artifacts.attachment_processing": "1.0.0",
    "aura.models.inference": "1.0.0",
    "aura.interaction.conversation_persistence": "1.7.0",
    "aura.knowledge.document_retrieval": "1.2.0",
}
EVALUATOR_VERSIONS = {
    "attachment_capture_redaction": "1.0.0",
    "vision_capability_and_image_preservation": "1.0.0",
    "conversation_attachment_branch_isolation": "1.0.0",
    "attached_document_grounding_and_citation": "1.0.0",
}
EVALUATOR_COMPONENTS = {
    "attachment_capture_redaction": "aura.artifacts.attachment_processing",
    "vision_capability_and_image_preservation": "aura.models.inference",
    "conversation_attachment_branch_isolation": "aura.interaction.conversation_persistence",
    "attached_document_grounding_and_citation": "aura.knowledge.document_retrieval",
}


class AttachmentSummaryOutput(Protocol):
    """The metadata-only summary emitted by conversation branch selection."""

    @property
    def link_id(self) -> UUID: ...

    @property
    def kind(self) -> str: ...

    @property
    def source_id(self) -> UUID: ...

    @property
    def availability(self) -> str: ...


class ProviderMessageOutput(Protocol):
    """Provider-neutral message sent to initial or synthesis inference."""

    @property
    def image_parts(self) -> Sequence[object]: ...


class VisionDescriptorOutput(Protocol):
    @property
    def capabilities(self) -> Sequence[str]: ...

    @property
    def model_revision(self) -> str | None: ...

    @property
    def model_digest(self) -> str | None: ...


class DocumentHitOutput(Protocol):
    @property
    def document_id(self) -> UUID: ...


class CitationOutput(Protocol):
    @property
    def document_id(self) -> UUID: ...


class CaptureOutput(Protocol):
    @property
    def dimensions(self) -> Sequence[tuple[str, str]]: ...

    @property
    def trace_attributes(self) -> Sequence[tuple[str, str]]: ...


@dataclass(frozen=True, slots=True)
class AttachmentEvaluationResult:
    evaluator_id: str
    evaluator_version: str
    component_id: str
    component_version: str
    fixture_revision: str
    replay_key: str
    score: float
    outcome: str
    metadata: dict[str, str]

    def payload(self) -> dict[str, object]:
        return {
            "evaluatorId": self.evaluator_id,
            "evaluatorVersion": self.evaluator_version,
            "componentId": self.component_id,
            "componentVersion": self.component_version,
            "fixtureRevision": self.fixture_revision,
            "replayKey": self.replay_key,
            "score": self.score,
            "outcome": self.outcome,
            "metadata": dict(self.metadata),
        }


def _result(evaluator_id: str, passed: bool, **metadata: str) -> AttachmentEvaluationResult:
    component = EVALUATOR_COMPONENTS[evaluator_id]
    version = EVALUATOR_VERSIONS[evaluator_id]
    component_version = COMPONENT_VERSIONS[component]
    replay_key = str(
        uuid5(NAMESPACE_URL, f"{component}:{component_version}:{FIXTURE_REVISION}:{evaluator_id}")
    )
    return AttachmentEvaluationResult(
        evaluator_id,
        version,
        component,
        component_version,
        FIXTURE_REVISION,
        replay_key,
        1.0 if passed else 0.0,
        "pass" if passed else "fail",
        metadata,
    )


def evaluate_attachment_capture_redaction(
    *, prohibited_fields_seen: int
) -> AttachmentEvaluationResult:
    return _result(
        "attachment_capture_redaction",
        type(prohibited_fields_seen) is int and prohibited_fields_seen == 0,
        prohibited_fields_seen=str(prohibited_fields_seen),
    )


def evaluate_vision_capability_and_image_preservation(
    *, verified: bool, image_parts: int, expected_parts: int
) -> AttachmentEvaluationResult:
    valid = (
        all(type(value) is int for value in (image_parts, expected_parts))
        and image_parts >= 0
        and expected_parts >= 0
    )
    return _result(
        "vision_capability_and_image_preservation",
        valid and verified and image_parts == expected_parts,
        verified=str(verified).lower(),
        image_parts=str(image_parts),
        expected_parts=str(expected_parts),
    )


def evaluate_conversation_attachment_branch_isolation(
    *, authorized_links: int, foreign_links: int
) -> AttachmentEvaluationResult:
    valid = all(type(value) is int and value >= 0 for value in (authorized_links, foreign_links))
    return _result(
        "conversation_attachment_branch_isolation",
        valid and foreign_links == 0,
        authorized_links=str(authorized_links),
        foreign_links=str(foreign_links),
    )


def evaluate_attached_document_grounding_and_citation(
    *, retrieved_documents: int, cited_documents: int, invalid_citations: int = 0
) -> AttachmentEvaluationResult:
    values = (retrieved_documents, cited_documents, invalid_citations)
    valid = (
        all(type(value) is int and value >= 0 for value in values)
        and cited_documents <= retrieved_documents
    )
    return _result(
        "attached_document_grounding_and_citation",
        valid and cited_documents == retrieved_documents and invalid_citations == 0,
        retrieved_documents=str(retrieved_documents),
        cited_documents=str(cited_documents),
        invalid_citations=str(invalid_citations),
    )


def evaluate_production_fixture(
    *,
    branch_attachment_links: Sequence[AttachmentSummaryOutput],
    authorized_attachment_links: Sequence[AttachmentSummaryOutput],
    selected_images: Sequence[AttachmentSummaryOutput],
    provider_messages: Sequence[ProviderMessageOutput],
    vision_descriptor: VisionDescriptorOutput | None,
    retrieval_hits: Sequence[DocumentHitOutput],
    citations: Sequence[CitationOutput],
    capture_records: Sequence[CaptureOutput],
) -> tuple[AttachmentEvaluationResult, ...]:
    """Replay all attachment evaluators from production seam outputs.

    Inputs are the bounded outputs of the production seams: branch selection,
    provider-neutral inference messages, model verification, document search,
    citation settlement, and metadata-only capture.  Bytes, prompts,
    filenames, and excerpts cannot enter this fixture runner.
    """

    prohibited = {"bytes", "filename", "prompt", "payload", "content", "exif"}
    capture_metadata = {
        key: value
        for record in capture_records
        for key, value in (*record.dimensions, *record.trace_attributes)
    }
    prohibited_fields_seen = sum(key in prohibited for key in capture_metadata)
    authorized = {item.link_id for item in authorized_attachment_links}
    observed = {item.link_id for item in branch_attachment_links}
    retrieved = {item.document_id for item in retrieval_hits}
    cited = {item.document_id for item in citations}
    image_messages = [message for message in provider_messages if message.image_parts]
    expected_image_count = len(selected_images) * len(image_messages)
    observed_image_count = sum(len(message.image_parts) for message in image_messages)
    vision_verified = (
        vision_descriptor is not None
        and "vision" in vision_descriptor.capabilities
        and bool(vision_descriptor.model_revision)
        and bool(vision_descriptor.model_digest)
    )
    return (
        evaluate_attachment_capture_redaction(prohibited_fields_seen=prohibited_fields_seen),
        evaluate_vision_capability_and_image_preservation(
            verified=vision_verified,
            image_parts=observed_image_count,
            expected_parts=expected_image_count,
        ),
        evaluate_conversation_attachment_branch_isolation(
            authorized_links=len(authorized),
            foreign_links=len(observed - authorized),
        ),
        evaluate_attached_document_grounding_and_citation(
            retrieved_documents=len(retrieved),
            cited_documents=len(cited & retrieved),
            invalid_citations=len(cited - retrieved),
        ),
    )
