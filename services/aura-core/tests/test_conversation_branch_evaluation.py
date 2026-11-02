"""Deterministic, metadata-only evaluations for AURA-0071.

The branch fixtures intentionally contain counts and bounded outcome labels
only.  Conversation text, owner subjects, citations, evidence, and lineage
payloads are never passed to an evaluator or retained in its result.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, cast
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest
from aura_core.domains.execution.runs.public import Run, RunCoordinator, RunRepository
from aura_core.domains.interaction.conversations.dto import DocumentCitation
from aura_core.domains.interaction.conversations.public import (
    Conversation,
    ConversationNotFound,
    ConversationStore,
)
from aura_core.domains.knowledge.documents.public import DocumentSearchHit
from aura_core.platform.outbox import InMemoryOutbox, OutboxCommand
from aura_core.platform.telemetry import COMPONENT_VERSIONS
from aura_core.providers.models.ollama.fake import FakeChatModel
from aura_core.runtime.models.ports import (
    ChatMessage,
    ModelDescriptor,
    ModelStreamCompleted,
    ModelToolCall,
    ProviderTraceContext,
)
from aura_core.runtime.streaming.publisher import EventPublisher

FIXTURE_REVISION = "conversation-branching-eval-fixtures-v2"
EVALUATOR_VERSIONS = {
    "conversation_branch_context_isolation": "1.1.0",
    "conversation_regeneration_evidence_freshness": "1.1.0",
}
EVALUATOR_COMPONENTS = {
    "conversation_branch_context_isolation": "aura.interaction.conversation_persistence",
    "conversation_regeneration_evidence_freshness": "aura.execution.run_coordinator",
}


@dataclass(frozen=True, slots=True)
class EvaluationResult:
    evaluator_id: str
    evaluator_version: str
    component_id: str
    component_version: str
    fixture_revision: str
    case_id: str
    replay_key: str
    score: float
    outcome: str
    metadata: tuple[tuple[str, str], ...]

    def payload(self) -> dict[str, Any]:
        return {
            "evaluatorId": self.evaluator_id,
            "evaluatorVersion": self.evaluator_version,
            "componentId": self.component_id,
            "componentVersion": self.component_version,
            "fixtureRevision": self.fixture_revision,
            "caseId": self.case_id,
            "replayKey": self.replay_key,
            "score": self.score,
            "outcome": self.outcome,
            "metadata": dict(self.metadata),
        }


def _result(evaluator_id: str, passed: bool, **metadata: str) -> EvaluationResult:
    forbidden = {"content", "message", "prompt", "response", "citation", "evidence", "owner"}
    if forbidden & set(metadata):
        raise ValueError("conversation evaluation metadata must remain content-free")
    component_id = EVALUATOR_COMPONENTS[evaluator_id]
    component_version = COMPONENT_VERSIONS[component_id]
    evaluator_version = EVALUATOR_VERSIONS[evaluator_id]
    canonical_metadata = json.dumps(
        sorted(metadata.items()), separators=(",", ":"), ensure_ascii=True
    )
    metadata_digest = hashlib.sha256(canonical_metadata.encode("ascii")).hexdigest()[:16]
    case_id = f"{evaluator_id}.{metadata_digest}"
    replay_key = str(
        uuid5(
            NAMESPACE_URL,
            f"{component_id}:{component_version}:{evaluator_id}:{FIXTURE_REVISION}:{case_id}",
        )
    )
    return EvaluationResult(
        evaluator_id=evaluator_id,
        evaluator_version=evaluator_version,
        component_id=component_id,
        component_version=component_version,
        fixture_revision=FIXTURE_REVISION,
        case_id=case_id,
        replay_key=replay_key,
        score=1.0 if passed else 0.0,
        outcome="pass" if passed else "fail",
        metadata=tuple(sorted(metadata.items())),
    )


def evaluate_conversation_branch_context_isolation(
    *,
    expected_ancestor_count: int,
    observed_ancestor_count: int,
    leaked_sibling_count: int,
    foreign_owner_results: int,
    boundary_preserved: bool = True,
) -> EvaluationResult:
    """Evaluate branch context using only bounded counts and booleans."""

    passed = (
        expected_ancestor_count >= 0
        and observed_ancestor_count == expected_ancestor_count
        and leaked_sibling_count == 0
        and foreign_owner_results == 0
        and boundary_preserved
    )
    return _result(
        "conversation_branch_context_isolation",
        passed,
        expected_ancestor_count=str(expected_ancestor_count),
        observed_ancestor_count=str(observed_ancestor_count),
        leaked_sibling_count=str(leaked_sibling_count),
        foreign_owner_results=str(foreign_owner_results),
        boundary_preserved=str(boundary_preserved).lower(),
    )


def evaluate_conversation_regeneration_evidence_freshness(
    *,
    source_evidence_handles: Iterable[UUID],
    regenerated_evidence_handles: Iterable[UUID],
    invalid_citation_count: int,
    historical_memory_reprocessed_count: int,
    new_owner_evidence_count: int = 0,
) -> EvaluationResult:
    """Evaluate that regenerated evidence is independently validated.

    The source run may contain cited evidence.  Freshness is established by
    proving that the regenerated run has its own disjoint evidence set and no
    stale citations or duplicate historical processing.  Only bounded counts
    enter the evaluator result; evidence identifiers never do.
    """

    source_handles = set(source_evidence_handles)
    regenerated_handles = set(regenerated_evidence_handles)
    reused_source_evidence_count = len(source_handles & regenerated_handles)
    source_evidence_count = len(source_handles)
    regenerated_evidence_count = len(regenerated_handles)
    passed = (
        min(
            source_evidence_count,
            regenerated_evidence_count,
            reused_source_evidence_count,
            invalid_citation_count,
            historical_memory_reprocessed_count,
            new_owner_evidence_count,
        )
        >= 0
        and reused_source_evidence_count == 0
        and invalid_citation_count == 0
        and historical_memory_reprocessed_count == 0
    )
    return _result(
        "conversation_regeneration_evidence_freshness",
        passed,
        source_evidence_count=str(source_evidence_count),
        regenerated_evidence_count=str(regenerated_evidence_count),
        reused_source_evidence_count=str(reused_source_evidence_count),
        invalid_citation_count=str(invalid_citation_count),
        historical_memory_reprocessed_count=str(historical_memory_reprocessed_count),
        new_owner_evidence_count=str(new_owner_evidence_count),
    )


def test_branch_evaluators_have_stable_attribution_and_replay_keys() -> None:
    first = evaluate_conversation_branch_context_isolation(
        expected_ancestor_count=4,
        observed_ancestor_count=4,
        leaked_sibling_count=0,
        foreign_owner_results=0,
    )
    second = evaluate_conversation_branch_context_isolation(
        expected_ancestor_count=4,
        observed_ancestor_count=4,
        leaked_sibling_count=0,
        foreign_owner_results=0,
    )
    assert first.replay_key == second.replay_key
    assert first.case_id == second.case_id
    assert first.evaluator_version == "1.1.0"
    assert first.component_id == "aura.interaction.conversation_persistence"
    assert first.component_version == COMPONENT_VERSIONS[first.component_id]

    freshness = evaluate_conversation_regeneration_evidence_freshness(
        source_evidence_handles=(),
        regenerated_evidence_handles={
            uuid5(NAMESPACE_URL, "fresh-1"),
            uuid5(NAMESPACE_URL, "fresh-2"),
        },
        invalid_citation_count=0,
        historical_memory_reprocessed_count=0,
    )
    assert freshness.component_id == "aura.execution.run_coordinator"
    assert freshness.component_version == COMPONENT_VERSIONS[freshness.component_id]


def test_branch_evaluators_cover_positive_and_negative_fixtures() -> None:
    isolated = evaluate_conversation_branch_context_isolation(
        expected_ancestor_count=3,
        observed_ancestor_count=3,
        leaked_sibling_count=0,
        foreign_owner_results=0,
    )
    leaked = evaluate_conversation_branch_context_isolation(
        expected_ancestor_count=3,
        observed_ancestor_count=4,
        leaked_sibling_count=1,
        foreign_owner_results=0,
    )
    fresh = evaluate_conversation_regeneration_evidence_freshness(
        source_evidence_handles=(),
        regenerated_evidence_handles={uuid5(NAMESPACE_URL, "fresh")},
        invalid_citation_count=0,
        historical_memory_reprocessed_count=0,
    )
    stale = evaluate_conversation_regeneration_evidence_freshness(
        source_evidence_handles={uuid5(NAMESPACE_URL, "stale")},
        regenerated_evidence_handles={uuid5(NAMESPACE_URL, "stale")},
        invalid_citation_count=1,
        historical_memory_reprocessed_count=1,
    )
    assert isolated.outcome == fresh.outcome == "pass"
    assert leaked.outcome == stale.outcome == "fail"
    assert isolated.replay_key != leaked.replay_key
    assert fresh.replay_key != stale.replay_key
    assert isolated.case_id != leaked.case_id
    assert fresh.case_id != stale.case_id


def test_branch_evaluator_payloads_are_metadata_only_and_redacted() -> None:
    results = (
        evaluate_conversation_branch_context_isolation(
            expected_ancestor_count=2,
            observed_ancestor_count=2,
            leaked_sibling_count=0,
            foreign_owner_results=0,
        ),
        evaluate_conversation_regeneration_evidence_freshness(
            source_evidence_handles=(),
            regenerated_evidence_handles={
                uuid5(NAMESPACE_URL, "payload-1"),
                uuid5(NAMESPACE_URL, "payload-2"),
            },
            invalid_citation_count=0,
            historical_memory_reprocessed_count=0,
        ),
    )
    for result in results:
        payload = result.payload()
        rendered = repr(payload).lower()
        assert not any(
            secret in rendered
            for secret in ("private question", "owner@example", "document evidence")
        )
        assert not {"content", "message", "prompt", "response", "citation", "evidence"} & set(
            payload["metadata"]
        )
        assert UUID(result.replay_key)


@pytest.mark.parametrize(
    "kwargs",
    [
        {
            "expected_ancestor_count": -1,
            "observed_ancestor_count": -1,
            "leaked_sibling_count": 0,
            "foreign_owner_results": 0,
        },
        {
            "expected_ancestor_count": 2,
            "observed_ancestor_count": 2,
            "leaked_sibling_count": 0,
            "foreign_owner_results": 1,
        },
    ],
)
def test_branch_context_evaluator_rejects_invalid_or_foreign_fixture(
    kwargs: dict[str, int],
) -> None:
    result = evaluate_conversation_branch_context_isolation(
        expected_ancestor_count=kwargs["expected_ancestor_count"],
        observed_ancestor_count=kwargs["observed_ancestor_count"],
        leaked_sibling_count=kwargs["leaked_sibling_count"],
        foreign_owner_results=kwargs["foreign_owner_results"],
        boundary_preserved=True,
    )
    assert result.outcome == "fail"


def test_regeneration_evaluator_rejects_stale_evidence_or_reprocessed_history() -> None:
    result = evaluate_conversation_regeneration_evidence_freshness(
        source_evidence_handles=(),
        regenerated_evidence_handles={uuid5(NAMESPACE_URL, "history")},
        invalid_citation_count=0,
        historical_memory_reprocessed_count=1,
    )
    assert result.outcome == "fail"


def test_regeneration_evaluator_uses_run_handles_not_document_identity() -> None:
    source_handle = uuid5(NAMESPACE_URL, "same-document-source-handle")
    regenerated_handle = uuid5(NAMESPACE_URL, "same-document-regenerated-handle")
    fresh = evaluate_conversation_regeneration_evidence_freshness(
        source_evidence_handles={source_handle},
        regenerated_evidence_handles={regenerated_handle},
        invalid_citation_count=0,
        historical_memory_reprocessed_count=0,
    )
    reused = evaluate_conversation_regeneration_evidence_freshness(
        source_evidence_handles={source_handle},
        regenerated_evidence_handles={source_handle},
        invalid_citation_count=0,
        historical_memory_reprocessed_count=0,
    )
    assert fresh.outcome == "pass"
    assert reused.outcome == "fail"


class _EvaluationPublisher:
    async def publish(self, _command: OutboxCommand) -> bool:
        return True


class _GovernedDocumentSearchProvider:
    """Provider fixture that completes search and cites the normal protocol."""

    _HANDLE_RE = re.compile(r"Valid same-run evidenceId values: ([0-9a-f-]+)")

    def __init__(self) -> None:
        self.handles_by_run: list[set[UUID]] = []

    async def list_models(self) -> tuple[ModelDescriptor, ...]:
        return (
            ModelDescriptor(
                "eval-doc-model",
                "Evaluation document model",
                "fixture",
                ("chat", "tool_calling"),
            ),
        )

    async def is_ready(self, model_id: str | None = None) -> bool:
        return model_id in (None, "eval-doc-model")

    async def stream_chat_with_tools(
        self, model_id: str, messages: object, tools: object
    ) -> object:
        del model_id, messages, tools
        yield ModelToolCall("document.search", {"query": "retention policy"})
        yield ModelStreamCompleted()

    async def stream_chat(self, model_id: str, messages: list[ChatMessage]) -> object:
        del model_id
        rendered = "\n".join(message.content for message in messages)
        match = self._HANDLE_RE.search(rendered)
        if match is None:
            raise AssertionError("governed synthesis did not receive a run-scoped evidence handle")
        handle = UUID(match.group(1))
        self.handles_by_run.append({handle})
        yield f"Grounded answer [[document:{handle}]]."


@pytest.mark.asyncio
async def test_context_isolation_evaluator_observes_conversation_store_branch_path() -> None:
    models = await FakeChatModel().list_models()
    store = ConversationStore()
    conversation, root_user, root_run = await store.create(
        "https://issuer", "owner", "root question", "fake", models, "eval-root"
    )
    coordinator = RunCoordinator(cast(RunRepository, store), EventPublisher(), InMemoryOutbox())
    await coordinator.execute(root_run.id, FakeChatModel())
    conversation, sibling_user, sibling_run = await store.add_run(
        conversation.id,
        "owner",
        "sibling question",
        conversation.version,
        "eval-sibling",
        "https://issuer",
    )
    await coordinator.execute(sibling_run.id, FakeChatModel())
    conversation, corrected, _branch_run, branch = await store.create_branch(
        conversation.id,
        sibling_user.id,
        "corrected question",
        conversation.version,
        "eval-branch",
        "owner",
        "https://issuer",
    )
    path = await store.branch_path(conversation.id, branch.id, "owner", "https://issuer")
    allowed_ids = {root_user.id, root_run.assistant_message_id, corrected.id}
    sibling_ids = {sibling_user.id, sibling_run.assistant_message_id}
    foreign_owner_results = 0
    try:
        foreign_path = await store.branch_path(
            conversation.id, branch.id, "other-owner", "https://issuer"
        )
        foreign_owner_results = len(foreign_path)
    except ConversationNotFound:
        pass
    observed = evaluate_conversation_branch_context_isolation(
        expected_ancestor_count=len(allowed_ids),
        observed_ancestor_count=len(path),
        leaked_sibling_count=len({item.id for item in path} & sibling_ids),
        foreign_owner_results=foreign_owner_results,
    )
    assert observed.outcome == "pass"


@pytest.mark.asyncio
async def test_regeneration_freshness_evaluator_observes_store_and_coordinator_paths() -> None:
    provider = _GovernedDocumentSearchProvider()
    models = await provider.list_models()
    document_id = uuid5(NAMESPACE_URL, "aura-eval-same-document")
    chunk_id = uuid5(NAMESPACE_URL, "aura-eval-stable-adapter-chunk")

    class DocumentServiceFixture:
        async def search(
            self,
            issuer: str,
            subject: str,
            query: str,
            *,
            limit: int,
            allowed_document_ids: tuple[UUID, ...] | None = None,
            context: ProviderTraceContext | None = None,
        ) -> list[DocumentSearchHit]:
            del allowed_document_ids
            assert (issuer, subject, query, limit, context is not None) == (
                "https://issuer",
                "owner",
                "retention policy",
                8,
                True,
            )
            return [
                DocumentSearchHit(
                    chunk_id,
                    document_id,
                    "private.pdf",
                    "bounded owner document excerpt",
                    "page-1",
                    0.9,
                )
            ]

    store = ConversationStore()
    memory_commands: list[tuple[UUID, UUID | None]] = []

    def memory_factory(_conversation: Conversation, run: Run, message: object) -> OutboxCommand:
        memory_commands.append((run.id, getattr(message, "id", None)))
        return cast(OutboxCommand, object())

    store.set_memory_command_factory(memory_factory, _EvaluationPublisher())
    conversation, _source_user, source_run = await store.create(
        "https://issuer", "owner", "cited source", "eval-doc-model", models, "eval-source"
    )
    coordinator = RunCoordinator(
        cast(RunRepository, store),
        EventPublisher(),
        InMemoryOutbox(),
        document_service=DocumentServiceFixture(),
    )
    await coordinator.execute(source_run.id, provider)
    _conversation, _regenerated_user, regenerated_run, _branch = await store.regenerate(
        source_run.id,
        conversation.version,
        "eval-regeneration",
        "owner",
        "https://issuer",
    )
    await coordinator.execute(regenerated_run.id, provider)
    _owner, source = await store.find_run_any(source_run.id)
    _owner, regenerated = await store.find_run_any(regenerated_run.id)
    source_handles, regenerated_handles = provider.handles_by_run
    invalid_citation_count = int(
        source.citations != (DocumentCitation(document_id, "Document source", "page-1"),)
        or regenerated.citations
        != (DocumentCitation(document_id, "Document source", "page-1"),)
    )
    result = evaluate_conversation_regeneration_evidence_freshness(
        source_evidence_handles=source_handles,
        regenerated_evidence_handles=regenerated_handles,
        invalid_citation_count=invalid_citation_count,
        historical_memory_reprocessed_count=max(0, len(memory_commands) - 1),
    )
    assert source_handles
    assert regenerated_handles
    assert source_handles.isdisjoint(regenerated_handles)
    assert source.citations and regenerated.citations
    assert source.citations[0].document_id == regenerated.citations[0].document_id
    assert invalid_citation_count == 0
    assert result.outcome == "pass"
    assert memory_commands == []
