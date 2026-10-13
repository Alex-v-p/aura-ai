"""Deterministic quality gates for selection, permissions, and completion."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid4

import pytest
from aura_core.domains.execution.runs.public import RunCoordinator, RunStatus
from aura_core.domains.interaction.agents.public import (
    AgentCatalog,
    MemoryPolicy,
    MemoryRecallMode,
    ToolGrant,
    ToolPolicy,
)
from aura_core.domains.interaction.conversations.public import ConversationStore
from aura_core.domains.knowledge.memory.public import (
    MemoryEmbeddingGeneration,
    MemoryKind,
    MemoryModelConfiguration,
    MemoryScope,
    MemoryScopeType,
    MemoryStore,
)
from aura_core.domains.knowledge.memory.recall import (
    MemoryQueryEmbedding,
    MemoryRecallPurpose,
    MemoryRecallRequest,
    MemoryRecallResult,
    MemoryRecallService,
)
from aura_core.platform.outbox import InMemoryOutbox
from aura_core.runtime.models.gateway import ModelGateway
from aura_core.runtime.models.ports import (
    ChatMessage,
    ModelDescriptor,
    ModelStreamCompleted,
    ModelToolCall,
)
from aura_core.runtime.streaming.publisher import EventPublisher
from aura_core.runtime.tools import (
    MemorySearchContext,
    ToolCall,
    ToolExecutionError,
    ToolExecutor,
    ToolPolicyEvaluator,
    default_tool_registry,
    validate_tool_arguments,
)


@dataclass(frozen=True)
class SelectionCase:
    name: str
    call: ToolCall
    expected_allowed: bool
    expected_code: str | None


@dataclass(frozen=True)
class ProviderSelectionFixture:
    """Stable, replayable input for the provider-neutral selection boundary."""

    evaluator_id: str
    name: str
    requires_call: bool
    expected_provider_call: bool
    expected_execution: bool | None


@dataclass(frozen=True)
class SelectionEvaluation:
    evaluator_id: str
    fixture_name: str
    observed_provider_call: bool
    expected_provider_call: bool
    selection_score: bool
    execution_allowed: bool | None

    @property
    def replay_key(self) -> str:
        return f"{self.evaluator_id}:{self.fixture_name}:v1"


@dataclass(frozen=True)
class EvaluatorResult:
    """Stable, content-free result emitted by the deterministic fixtures."""

    evaluator_id: str
    component_id: str
    component_version: str
    fixture_revision: str
    replay_key: str
    scenario_id: str
    case_id: str
    score: bool
    outcome: str
    metadata: tuple[tuple[str, str], ...]

    def payload(self) -> dict[str, object]:
        return {
            "evaluatorId": self.evaluator_id,
            "componentId": self.component_id,
            "componentVersion": self.component_version,
            "fixtureRevision": self.fixture_revision,
            "replayKey": self.replay_key,
            "scenarioId": self.scenario_id,
            "caseId": self.case_id,
            "score": self.score,
            "outcome": self.outcome,
            "metadata": dict(self.metadata),
        }


REQUIRED_EVALUATOR_IDS = frozenset(
    {
        "tool_selection_quality",
        "tool_argument_accuracy",
        "tool_permission_enforcement",
        "tool_execution_completion",
        "memory_scope_authorization",
    }
)
FIXTURE_REVISION = "aura-0053-v1"
EVALUATOR_COMPONENTS = {
    "tool_selection_quality": ("aura.runtime.tool_selection", "1.4.0"),
    "tool_argument_accuracy": ("aura.runtime.policy_evaluation", "1.1.0"),
    "tool_permission_enforcement": ("aura.runtime.policy_evaluation", "1.1.0"),
    "tool_execution_completion": ("aura.runtime.tool_execution", "1.3.0"),
    "memory_scope_authorization": ("aura.runtime.tool_execution", "1.3.0"),
}
CONTENT_FIELDS = frozenset(
    {"query", "arguments", "result", "content", "prompt", "response", "memoryId"}
)
DOCUMENT_QUERY_SENTINEL = "document-library-private-query"


def _result(
    evaluator_id: str,
    case_id: str,
    score: bool,
    metadata: Mapping[str, str],
) -> EvaluatorResult:
    component_id, component_version = EVALUATOR_COMPONENTS[evaluator_id]
    replay_key = (
        f"{evaluator_id}:{component_id}@{component_version}:"
        f"{FIXTURE_REVISION}:{case_id}"
    )
    bounded_metadata = tuple(sorted(metadata.items()))
    assert not CONTENT_FIELDS.intersection(metadata)
    assert all(len(key) <= 32 and len(value) <= 64 for key, value in bounded_metadata)
    return EvaluatorResult(
        evaluator_id,
        component_id,
        component_version,
        FIXTURE_REVISION,
        replay_key,
        case_id.split(".", 1)[0],
        case_id,
        score,
        "pass" if score else "fail",
        bounded_metadata,
    )


class _EvaluationSelectionProvider:
    def __init__(self, scenario: str) -> None:
        self.scenario = scenario

    async def list_models(self) -> tuple[ModelDescriptor, ...]:
        return ()

    async def is_ready(self, model_id: str | None = None) -> bool:
        del model_id
        return True

    async def stream_chat(
        self, model_id: str, messages: Sequence[ChatMessage]
    ) -> AsyncIterator[str]:
        del model_id, messages
        if False:
            yield ""

    def stream_chat_with_tools(
        self,
        _model_id: str,
        _messages: Sequence[ChatMessage],
        _tools: Sequence[Mapping[str, object]],
    ) -> AsyncIterator[object]:
        async def events() -> AsyncIterator[object]:
            if self.scenario in {
                "correct",
                "prohibited",
                "document-selected",
                "document-denied",
            }:
                if self.scenario == "prohibited":
                    name = "shell.exec"
                elif self.scenario in {"document-selected", "document-denied"}:
                    name = "document.search"
                else:
                    name = "memory.search"
                query = (
                    DOCUMENT_QUERY_SENTINEL
                    if name == "document.search"
                    else "fixture"
                )
                yield ModelToolCall(name, {"query": query})
            yield ModelStreamCompleted()

        return events()


class _EvaluationRunProvider(_EvaluationSelectionProvider):
    def __init__(self) -> None:
        super().__init__("correct")
        self.synthesis_messages: list[ChatMessage] = []

    async def list_models(self) -> tuple[ModelDescriptor, ...]:
        return (
            ModelDescriptor(
                "tool-model", "Evaluation model", "fake", ("chat", "tool_calling")
            ),
        )

    async def stream_chat(
        self, model_id: str, messages: Sequence[ChatMessage]
    ) -> AsyncIterator[str]:
        del model_id
        self.synthesis_messages = list(messages)
        yield "memory-backed answer"


def test_selection_quality_matrix_is_deterministic() -> None:
    catalog = AgentCatalog()
    aura = catalog.list_agents()[0]
    granted = catalog.get_tool_policy(aura.current_revision.tool_policy_revision_id)
    denied = ToolPolicy(uuid4(), aura.id, 1)
    evaluator = ToolPolicyEvaluator(default_tool_registry())
    cases = (
        SelectionCase("correct", ToolCall("memory.search", {"query": "tea"}), True, None),
        SelectionCase(
            "unnecessary", ToolCall("memory.search", {"query": "already-known"}), True, None
        ),
        SelectionCase(
            "missing", ToolCall("memory.search", {"query": "tea"}), False, "TOOL_NOT_GRANTED"
        ),
        SelectionCase(
            "prohibited", ToolCall("shell.exec", {"query": "tea"}), False, "TOOL_UNKNOWN"
        ),
    )

    for case in cases:
        policy = denied if case.name == "missing" else granted
        decision = evaluator.decide(policy, case.call)
        assert decision.allowed is case.expected_allowed, case.name
        if case.expected_code is not None:
            assert decision.error_code == case.expected_code, case.name


@pytest.mark.parametrize(
    ("arguments", "valid"),
    [
        ({"query": "tea"}, True),
        ({"query": "  tea  "}, True),
        ({"query": ""}, False),
        ({"query": "x" * 201}, False),
        ({"query": "tea", "scope": "all"}, False),
        ({"query": 42}, False),
    ],
)
def test_argument_accuracy_matrix(arguments: object, valid: bool) -> None:
    descriptor = default_tool_registry().get("memory.search", 1)
    assert descriptor is not None
    if valid:
        assert validate_tool_arguments(descriptor, arguments)["query"] == "tea"
    else:
        with pytest.raises(ToolExecutionError):
            validate_tool_arguments(descriptor, arguments)


def test_budget_and_permission_evaluators_never_turn_a_denial_into_execution() -> None:
    catalog = AgentCatalog()
    aura = catalog.list_agents()[0]
    policy = catalog.get_tool_policy(aura.current_revision.tool_policy_revision_id)
    evaluator = ToolPolicyEvaluator(default_tool_registry())
    call = ToolCall("memory.search", {"query": "tea"})

    assert evaluator.decide(policy, call, calls_used=0).allowed
    exhausted = evaluator.decide(policy, call, calls_used=1)
    assert not exhausted.allowed
    assert exhausted.error_class == "budget"
    assert exhausted.error_code == "TOOL_BUDGET_EXHAUSTED"


@pytest.mark.asyncio
async def test_selection_fixtures_score_provider_boundary_call_observations() -> None:
    """Score deterministic cases from provider-neutral native stream events."""

    class SelectionProvider:
        def __init__(self, scenario: str) -> None:
            self.scenario = scenario

        async def list_models(self) -> tuple[ModelDescriptor, ...]:
            return ()

        def stream_chat(
            self, model_id: str, messages: Sequence[ChatMessage]
        ) -> AsyncIterator[str]:
            del model_id, messages
            async def empty() -> AsyncIterator[str]:
                if False:
                    yield ""

            return empty()

        async def is_ready(self, model_id: str | None = None) -> bool:
            del model_id
            return True

        def stream_chat_with_tools(
            self,
            _model_id: str,
            _messages: Sequence[object],
            _tools: Sequence[Mapping[str, object]],
        ) -> AsyncIterator[object]:
            async def events() -> AsyncIterator[object]:
                if self.scenario in {
                    "correct",
                    "prohibited",
                    "document-selected",
                    "document-denied",
                }:
                    if self.scenario == "prohibited":
                        name = "shell.exec"
                    elif self.scenario in {"document-selected", "document-denied"}:
                        name = "document.search"
                    else:
                        name = "memory.search"
                    query = (
                        DOCUMENT_QUERY_SENTINEL
                        if name == "document.search"
                        else "tea"
                    )
                    yield ModelToolCall(name, {"query": query})
                yield ModelStreamCompleted()

            return events()

    catalog = AgentCatalog()
    aura = catalog.list_agents()[0]
    granted = catalog.get_tool_policy(aura.current_revision.tool_policy_revision_id)
    denied = ToolPolicy(uuid4(), aura.id, 1)
    evaluator = ToolPolicyEvaluator(default_tool_registry())
    fixtures = (
        ProviderSelectionFixture("eval.correct.v1", "correct", True, True, True),
        ProviderSelectionFixture("eval.unnecessary.v1", "unnecessary", False, False, None),
        ProviderSelectionFixture("eval.missing.v1", "missing", True, False, None),
        ProviderSelectionFixture("eval.prohibited.v1", "prohibited", False, True, False),
        ProviderSelectionFixture(
            "eval.document-selected.v1", "document-selected", True, True, True
        ),
        ProviderSelectionFixture(
            "eval.document-denied.v1", "document-denied", False, True, False
        ),
    )
    results: list[SelectionEvaluation] = []
    for fixture in fixtures:
        stream = ModelGateway(SelectionProvider(fixture.name)).stream_with_tools(
            "selection-model", (), ()
        )
        calls = [event async for event in stream if isinstance(event, ModelToolCall)]
        observed_call = bool(calls)
        assert observed_call is fixture.expected_provider_call, fixture.name
        execution_allowed: bool | None = None
        if calls:
            call = ToolCall(calls[0].name, calls[0].arguments)
            policy = (
                denied
                if fixture.name in {"prohibited", "document-denied"}
                else granted
            )
            execution_allowed = evaluator.decide(policy, call).allowed
            if fixture.expected_execution is not None:
                assert execution_allowed is fixture.expected_execution, fixture.name
        results.append(
            SelectionEvaluation(
                fixture.evaluator_id,
                fixture.name,
                observed_call,
                fixture.expected_provider_call,
                observed_call is fixture.requires_call,
                execution_allowed,
            )
        )

    assert {result.fixture_name for result in results} == {
        "correct",
        "unnecessary",
        "missing",
        "prohibited",
        "document-selected",
        "document-denied",
    }
    assert (
        next(result for result in results if result.fixture_name == "missing").selection_score
        is False
    )
    assert len({result.replay_key for result in results}) == len(results)
    assert [result.evaluator_id for result in results] == [
        "eval.correct.v1",
        "eval.unnecessary.v1",
        "eval.missing.v1",
        "eval.prohibited.v1",
        "eval.document-selected.v1",
        "eval.document-denied.v1",
    ]


def test_evaluation_inputs_and_outputs_are_content_free_metadata() -> None:
    descriptor = default_tool_registry().get("memory.search", 1)
    assert descriptor is not None
    summary = descriptor.summary()
    assert set(summary) == {
        "id",
        "version",
        "displayName",
        "description",
        "classification",
        "permissionScope",
        "confirmation",
        "timeoutMs",
        "retryLimit",
        "idempotency",
        "sensitivity",
        "observableComponent",
    }
    assert not {"query", "arguments", "result", "content", "prompt"} & set(summary)


async def _selection_results() -> list[EvaluatorResult]:
    catalog = AgentCatalog()
    aura = catalog.list_agents()[0]
    granted = catalog.get_tool_policy(aura.current_revision.tool_policy_revision_id)
    denied = ToolPolicy(uuid4(), aura.id, 1)
    evaluator = ToolPolicyEvaluator(default_tool_registry())
    fixtures = (
        ("correct", True, True, granted),
        ("unnecessary", False, False, granted),
        ("missing", True, False, granted),
        ("prohibited", False, True, denied),
        ("document-selected", True, True, granted),
        ("document-denied", False, True, denied),
    )
    results: list[EvaluatorResult] = []
    for case_id, required_call, expected_call, policy in fixtures:
        stream = ModelGateway(_EvaluationSelectionProvider(case_id)).stream_with_tools(
            "selection-model", (), ()
        )
        calls = [event async for event in stream if isinstance(event, ModelToolCall)]
        observed_call = bool(calls)
        if case_id in {"document-selected", "document-denied"}:
            assert calls and calls[0].name == "document.search", case_id
        allowed: bool | None = None
        if calls:
            allowed = evaluator.decide(
                policy, ToolCall(calls[0].name, calls[0].arguments)
            ).allowed
        score = observed_call is required_call and (
            not expected_call or allowed is True or case_id == "prohibited"
        )
        results.append(
            _result(
                "tool_selection_quality",
                f"selection.{case_id}.v1",
                score,
                {
                    "expectedCall": str(expected_call).lower(),
                    "observedCall": str(observed_call).lower(),
                    "policyDecision": "allow" if allowed else "deny",
                },
            )
        )
    return results


@pytest.mark.asyncio
async def test_document_selection_evaluations_are_versioned_and_content_free() -> None:
    results = await _selection_results()
    document_results = {
        item.case_id.rsplit(".", 2)[1]: item
        for item in results
        if item.case_id.startswith("selection.document-")
    }

    assert set(document_results) == {"document-selected", "document-denied"}
    selected = document_results["document-selected"]
    denied = document_results["document-denied"]
    assert selected.score is True
    assert denied.score is False
    for result in document_results.values():
        assert (result.component_id, result.component_version) == (
            "aura.runtime.tool_selection",
            "1.4.0",
        )
        assert result.payload()["metadata"] == {
            "expectedCall": "true",
            "observedCall": "true",
            "policyDecision": "allow"
            if result is selected
            else "deny",
        }
        assert DOCUMENT_QUERY_SENTINEL not in repr(result.payload())


def _argument_results() -> list[EvaluatorResult]:
    descriptor = default_tool_registry().get("memory.search", 1)
    assert descriptor is not None
    cases = (
        ("valid", {"query": "fixture"}, True),
        ("trimmed", {"query": "  fixture  "}, True),
        ("empty", {"query": ""}, False),
        ("extra-field", {"query": "fixture", "scope": "all"}, False),
        ("wrong-type", {"query": 42}, False),
    )
    results: list[EvaluatorResult] = []
    for case_id, arguments, expected_valid in cases:
        try:
            validate_tool_arguments(descriptor, arguments)
            accepted = True
        except ToolExecutionError:
            accepted = False
        results.append(
            _result(
                "tool_argument_accuracy",
                f"arguments.{case_id}.v1",
                accepted is expected_valid,
                {"accepted": str(accepted).lower()},
            )
        )
    return results


def _permission_results() -> list[EvaluatorResult]:
    catalog = AgentCatalog()
    aura = catalog.list_agents()[0]
    granted = catalog.get_tool_policy(aura.current_revision.tool_policy_revision_id)
    denied = ToolPolicy(uuid4(), aura.id, 1)
    wrong_version = ToolPolicy(
        uuid4(), aura.id, 1, grants=(ToolGrant("memory.search", 2),), max_calls_per_run=1
    )
    evaluator = ToolPolicyEvaluator(default_tool_registry())
    cases = (
        ("allow-exact-version", granted, True, None),
        ("deny-ungranted", denied, False, "TOOL_NOT_GRANTED"),
        ("deny-version-mismatch", wrong_version, False, "TOOL_NOT_GRANTED"),
    )
    results: list[EvaluatorResult] = []
    for case_id, policy, expected_allowed, expected_code in cases:
        decision = evaluator.decide(policy, ToolCall("memory.search", {}))
        results.append(
            _result(
                "tool_permission_enforcement",
                f"permission.{case_id}.v1",
                decision.allowed is expected_allowed and decision.error_code == expected_code,
                {
                    "decision": "allow" if decision.allowed else "deny",
                    "errorCode": decision.error_code or "none",
                    "toolVersion": "1",
                },
            )
        )
    return results


async def _execution_results() -> list[EvaluatorResult]:
    catalog = AgentCatalog()
    aura = catalog.list_agents()[0]
    policy = catalog.get_tool_policy(aura.current_revision.tool_policy_revision_id)
    executor = ToolExecutor(default_tool_registry())

    async def success(_query: str) -> list[dict[str, str]]:
        return [{"memoryId": "fixture-memory", "content": "fixture", "scope": "user"}]

    async def failure(_query: str) -> list[object]:
        raise ToolExecutionError("execution", "MEMORY_SEARCH_UNAVAILABLE")

    async def timeout(_query: str) -> list[object]:
        await asyncio.sleep(0.05)
        return []

    cases = (
        ("success", success, 30_000, 0, "completed", "none"),
        ("failure", failure, 30_000, 0, "failed", "MEMORY_SEARCH_UNAVAILABLE"),
        ("timeout", timeout, 1, 0, "failed", "TOOL_TIMEOUT"),
        ("budget", success, 30_000, 1, "denied", "TOOL_BUDGET_EXHAUSTED"),
    )
    results: list[EvaluatorResult] = []
    for case_id, handler, timeout_ms, calls_used, expected_status, expected_code in cases:
        result = await executor.execute(
            policy,
            ToolCall("memory.search", {"query": "fixture"}),
            MemorySearchContext(handler),
            calls_used=calls_used,
            timeout_ms=timeout_ms,
        )
        results.append(
            _result(
                "tool_execution_completion",
                f"execution.{case_id}.v1",
                result.status == expected_status and (result.error_code or "none") == expected_code,
                {"status": result.status, "errorCode": result.error_code or "none"},
            )
        )
    return results


async def _seed_evaluation_memories() -> tuple[
    MemoryStore, datetime, MemoryEmbeddingGeneration, UUID, UUID, UUID
]:
    issuer = "https://evaluation.example"
    subject = "evaluation-owner"
    now = datetime(2026, 1, 1, tzinfo=UTC)
    current_agent = uuid4()
    other_agent = uuid4()
    fallback_agent = uuid4()
    store = MemoryStore(clock=lambda: now)
    generation = await store.register_embedding_generation(
        issuer,
        subject,
        generation=1,
        model_id="evaluation-embedder",
        model_revision="v1",
        dimension=2,
        model_digest="a" * 64,
    )
    records = (
        (MemoryScopeType.USER, None),
        (MemoryScopeType.AGENT, current_agent),
        (MemoryScopeType.AGENT, other_agent),
        (MemoryScopeType.AGENT, fallback_agent),
    )
    for index, (scope_type, agent_id) in enumerate(records):
        record = await store.create_memory(
            issuer,
            subject,
            kind=MemoryKind.SEMANTIC,
            scope=MemoryScope(scope_type, agent_id),
            content=f"evaluation fixture {index}",
            confidence=0.99,
            importance=0.9,
        )
        await store.attach_embedding(
            issuer,
            subject,
            record.id,
            revision_id=record.current_revision_id,
            generation_id=generation.id,
            scope_type=scope_type,
            agent_profile_id=agent_id,
            vector=(1.0, 0.0),
            digest=f"{index + 1:064x}",
            model_id=generation.model_id,
            model_revision=generation.model_revision,
            model_digest=generation.model_digest,
        )
    await store.save_model_configuration(
        issuer,
        subject,
        MemoryModelConfiguration(
            issuer, subject, "extract", generation.model_id, embedding_generation=generation.id
        ),
    )
    await store.activate_embedding_generation(issuer, subject, generation.id)
    return store, now, generation, current_agent, other_agent, fallback_agent


async def _memory_scope_results() -> list[EvaluatorResult]:
    store, now, generation, current_agent, other_agent, fallback_agent = (
        await _seed_evaluation_memories()
    )
    del other_agent
    issuer = "https://evaluation.example"
    subject = "evaluation-owner"
    service = MemoryRecallService(store)

    async def recall(policy: MemoryPolicy, agent_id: UUID) -> MemoryRecallResult:
        request = MemoryRecallRequest(
            issuer,
            subject,
            agent_id,
            "fixture",
            policy,
            now=now,
            purpose=MemoryRecallPurpose.TOOL_SEARCH,
            query_embedding=MemoryQueryEmbedding(
                (1.0, 0.0),
                generation.id,
                generation.model_id,
                generation.model_revision,
                generation.dimension,
                model_digest=generation.model_digest,
            ),
        )
        return await service.recall(request)

    policies = (
        (
            "current-agent-authorized",
            MemoryPolicy(uuid4(), current_agent, 1, shared_user_read=False),
            current_agent,
            "agent",
            False,
        ),
        (
            "shared-authorized",
            MemoryPolicy(uuid4(), current_agent, 1, current_agent_read=False),
            current_agent,
            "user",
            False,
        ),
        (
            "other-agent-denied",
            MemoryPolicy(
                uuid4(), current_agent, 1, shared_user_read=False, current_agent_read=False
            ),
            current_agent,
            "none",
            False,
        ),
        (
            "fallback-agent-authorized",
            MemoryPolicy(
                uuid4(),
                current_agent,
                1,
                shared_user_read=False,
                current_agent_read=False,
                fallback_agent_profile_ids=(fallback_agent,),
            ),
            current_agent,
            "agent",
            True,
        ),
        (
            "fallback-agent-denied",
            MemoryPolicy(
                uuid4(), current_agent, 1, shared_user_read=False, current_agent_read=False
            ),
            current_agent,
            "none",
            False,
        ),
    )
    results: list[EvaluatorResult] = []
    for case_id, policy, agent_id, expected_scope, expected_fallback in policies:
        recalled = await recall(policy, agent_id)
        assert hasattr(recalled, "candidates")
        candidates = recalled.candidates
        observed_scope = (
            "none"
            if not candidates
            else "user"
            if all(item.scope_type is MemoryScopeType.USER for item in candidates)
            else "agent"
        )
        results.append(
            _result(
                "memory_scope_authorization",
                f"memory.{case_id}.v1",
                observed_scope == expected_scope
                and recalled.fallback_used is expected_fallback,
                {
                    "scope": observed_scope,
                    "fallbackUsed": str(recalled.fallback_used).lower(),
                },
            )
        )

    provider = _EvaluationRunProvider()
    models = await provider.list_models()
    conversation_store = ConversationStore()
    conversation, _, run = await conversation_store.create(
        issuer, subject, "Memory-backed answer", "tool-model", models, str(uuid4())
    )
    tool_policy_id = conversation_store.agents.resolve_revision_unchecked(
        run.agent_revision_id
    ).tool_policy_revision_id
    assert tool_policy_id is not None

    async def tool_policy_for_run(_run: object) -> object:
        return conversation_store.agents.get_tool_policy(tool_policy_id)

    async def recall_for_tool(
        _conversation: object,
        _run: object,
        _query: str,
        _budget: int,
        *_args: object,
    ) -> object:
        policy = MemoryPolicy(
            uuid4(),
            current_agent,
            1,
            shared_user_read=False,
            recall_mode=MemoryRecallMode.OFF,
        )
        recalled = await recall(policy, current_agent)
        return list(recalled.candidates)

    conversation_store.tool_policy_for_run = tool_policy_for_run  # type: ignore[attr-defined]
    conversation_store.recall_for_tool = recall_for_tool  # type: ignore[attr-defined]
    coordinator = RunCoordinator(
        conversation_store, EventPublisher(), InMemoryOutbox()
    )
    await coordinator.execute(run.id, provider)
    loaded = (await conversation_store.get(conversation.id, subject, issuer)).runs[0]
    tool_events = [
        event
        for event in await coordinator.publisher.history(run.id)
        if event.event_type == "tool.status"
    ]
    completed = loaded.status is RunStatus.COMPLETED and bool(provider.synthesis_messages)
    results.append(
        _result(
            "memory_scope_authorization",
            "memory.memory-backed-answer.v1",
            completed and bool(tool_events) and tool_events[-1].data["status"] == "completed",
            {
                "scope": "agent",
                "toolStatus": str(tool_events[-1].data["status"] if tool_events else "none"),
                "synthesis": str(bool(provider.synthesis_messages)).lower(),
            },
        )
    )
    return results


async def _all_evaluator_results() -> tuple[EvaluatorResult, ...]:
    return tuple(
        [
            *(await _selection_results()),
            *_argument_results(),
            *_permission_results(),
            *(await _execution_results()),
            *(await _memory_scope_results()),
        ]
    )


@pytest.mark.asyncio
async def test_required_evaluator_results_are_stable_and_content_free() -> None:
    first = await _all_evaluator_results()
    second = await _all_evaluator_results()
    assert first == second
    assert {item.evaluator_id for item in first} == REQUIRED_EVALUATOR_IDS
    assert all(item.case_id and item.outcome in {"pass", "fail"} for item in first)
    assert len({item.replay_key for item in first}) == len(first)
    assert {item.payload()["replayKey"] for item in first} == {
        item.replay_key for item in first
    }
    assert all(item.fixture_revision == FIXTURE_REVISION for item in first)
    assert all(
        (item.component_id, item.component_version)
        == EVALUATOR_COMPONENTS[item.evaluator_id]
        for item in first
    )
    assert all(
        item.payload().keys()
        == {
            "evaluatorId",
            "componentId",
            "componentVersion",
            "fixtureRevision",
            "replayKey",
            "scenarioId",
            "caseId",
            "score",
            "outcome",
            "metadata",
        }
        for item in first
    )
    for item in first:
        payload = item.payload()
        assert not CONTENT_FIELDS.intersection(payload)
        metadata = payload["metadata"]
        assert isinstance(metadata, Mapping)
        assert not CONTENT_FIELDS.intersection(cast(Mapping[str, object], metadata))
