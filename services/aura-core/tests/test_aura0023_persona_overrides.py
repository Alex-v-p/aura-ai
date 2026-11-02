"""Domain and runtime regression coverage for AURA-0023.

The conversation lifecycle is covered at the HTTP boundary in the companion
system module.  These tests protect the lower-level public persona and prompt
seams that must remain deterministic regardless of the storage adapter.
"""

from collections.abc import AsyncIterator, Sequence
from dataclasses import replace
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest
import yaml
from aura_core.domains.execution.runs.public import RunCoordinator
from aura_core.domains.interaction.agents.public import (
    AgentCatalog,
    ConfigurationDisabled,
    ConfigurationStatus,
)
from aura_core.domains.interaction.conversations.public import ConversationStore
from aura_core.domains.interaction.personas.public import (
    PersonaCatalog,
    PersonaConfigurationService,
    PersonaMemoryRepository,
)
from aura_core.platform.outbox import InMemoryOutbox
from aura_core.platform.telemetry import COMPONENT_VERSIONS, MetadataMetrics
from aura_core.runtime.models.ports import ChatMessage, ModelDescriptor
from aura_core.runtime.prompting.public import PromptCompiler
from aura_core.runtime.streaming.publisher import EventPublisher


class _PromptLifecycleProvider:
    async def list_models(self) -> tuple[ModelDescriptor, ...]:
        return (ModelDescriptor("chat", "Chat", "ollama", ("chat", "completion")),)

    async def is_ready(self, model_id: str | None = None) -> bool:
        return model_id in (None, "chat")

    async def stream_chat(
        self, model_id: str, messages: Sequence[ChatMessage]
    ) -> AsyncIterator[str]:
        del messages
        assert model_id == "chat"
        yield "done"


def test_an_agent_default_is_immutable_when_a_persona_revision_changes() -> None:
    personas = PersonaCatalog()
    agents = AgentCatalog(personas)
    persona = personas.list_personas()[0]
    agent = agents.list_agents()[0]
    original = agent.current_revision

    revised_profile = personas.revise_persona(
        "issuer",
        "owner",
        persona.id,
        persona.version,
        persona.display_name,
        "A changed reusable style.",
        "Use changed instructions.",
        str(uuid4()),
    )

    assert revised_profile.current_revision.id != original.persona_revision_id
    assert agent.current_revision_id == original.id
    assert agents.resolve_revision(original.id).persona_revision_id == persona.revisions[0].id


def test_prompt_compilation_accepts_independent_persona_and_orders_components() -> None:
    personas = PersonaCatalog()
    agents = AgentCatalog(personas)
    agent = agents.list_agents()[0].current_revision
    persona = personas.list_personas()[0]
    alternate = personas.revise_persona(
        "issuer",
        "owner",
        persona.id,
        persona.version,
        persona.display_name,
        "Independent override.",
        "OVERRIDE PERSONA INSTRUCTIONS",
        str(uuid4()),
    ).current_revision

    compilation = PromptCompiler(agents.bundle, allow_persona_override=True).compile(
        agent, alternate
    )

    expected = [
        agents.bundle.platform.content,
        agents.bundle.governance.content,
        "\n".join(filter(None, (agent.purpose, agent.instructions))),
        alternate.instructions,
    ]
    assert compilation.text.split("\n\n") == [item for item in expected if item]
    assert compilation.component_revision_ids[-1] == alternate.id
    assert compilation.component_count == 4


def test_persona_configuration_manifests_match_runtime_component_dependencies() -> None:
    manifest_dir = Path(__file__).parents[1] / "resources" / "component-manifests"
    expectations = {
        "agent-configuration.yaml": {
            "id": "aura.interaction.agent_configuration",
            "dependencies": {"configuration_store", "postgresql"},
        },
        "prompt-compilation.yaml": {
            "id": "aura.runtime.prompt_compilation",
            "dependencies": {"prompt_compiler"},
        },
    }
    for filename, expected in expectations.items():
        manifest = yaml.safe_load((manifest_dir / filename).read_text())
        component = manifest["component"]
        component_id = component["id"]
        assert component_id == expected["id"]
        assert component["version"] == COMPONENT_VERSIONS[component_id]
        assert set(manifest["dependencies"]) == expected["dependencies"]
        assert "capture_policy" in manifest
        assert "prohibited" in manifest["cardinality"]


def test_prompt_hash_changes_with_effective_persona_but_not_rendered_telemetry() -> None:
    personas = PersonaCatalog()
    agents = AgentCatalog(personas)
    agent = agents.list_agents()[0].current_revision
    original = personas.list_personas()[0].current_revision
    alternate = replace(
        original,
        id=uuid4(),
        instructions="PRIVATE OVERRIDE INSTRUCTIONS",
    )
    metrics = MetadataMetrics()
    compiler = PromptCompiler(
        agents.bundle, metrics, trace_id="a" * 32, allow_persona_override=True
    )

    first = compiler.compile(agent, original)
    second = compiler.compile(agent, alternate)

    assert first.prompt_hash != second.prompt_hash
    records = metrics.snapshot()
    assert records
    rendered = f"{agents.bundle.platform.content}\n\n{agents.bundle.governance.content}"
    assert all(rendered not in str(record) for record in records)
    assert all("PRIVATE OVERRIDE INSTRUCTIONS" not in str(record) for record in records)
    assert all("instructions" not in str(record).lower() for record in records)
    assert any(
        dict(record.trace_attributes).get("persona_revision_id") == str(alternate.id)
        for record in records
    )


def test_disabled_persona_cannot_be_admitted_but_existing_revision_remains_readable() -> None:
    personas = PersonaCatalog()
    persona = personas.list_personas()[0]
    historical = persona.current_revision

    personas.set_status(
        "issuer",
        "owner",
        persona.id,
        persona.version,
        ConfigurationStatus.DISABLED,
        str(uuid4()),
    )

    assert personas.get_persona(persona.id).current_revision.id == historical.id
    with pytest.raises(ConfigurationDisabled):
        personas.require_active_revision(historical.id)


@pytest.mark.asyncio
async def test_persona_public_query_stays_synchronized_through_disable_and_historical_compile(
) -> None:
    audits: list[dict[str, object]] = []
    personas = PersonaCatalog()

    async def record_audit(
        action: str,
        outcome: str,
        *,
        issuer: str,
        subject: str,
        metadata: dict[str, object],
    ) -> None:
        audits.append(
            {
                "action": action,
                "outcome": outcome,
                "issuer": issuer,
                "subject": subject,
                "metadata": metadata,
            }
        )

    service = PersonaConfigurationService(PersonaMemoryRepository(personas), record_audit)
    created = await service.create_persona(
        "issuer",
        "owner",
        f"independent-{uuid4().hex}",
        "Independent style",
        "A reusable independent style.",
        "Use the independent style.",
        str(uuid4()),
    )
    revised = await service.revise_persona(
        "issuer",
        "owner",
        created.id,
        created.version,
        "Independent style",
        "A revised reusable independent style.",
        "Use the revised independent style.",
        str(uuid4()),
    )

    selected = await service.require_active_revision(revised.current_revision.id)
    query_profile, query_revision = personas.find_revision(selected.id)
    assert query_profile.id == revised.id
    assert query_revision.id == selected.id
    agents = AgentCatalog(personas)
    compiled = agents.compilation(agents.list_agents()[0].current_revision.id, selected.id)
    assert compiled.component_revision_ids[-1] == selected.id
    assert selected.instructions not in str(audits)

    disabled = await service.set_status(
        "issuer",
        "owner",
        revised.id,
        revised.version,
        ConfigurationStatus.DISABLED,
        str(uuid4()),
    )
    assert disabled.status is ConfigurationStatus.DISABLED
    refreshed_profile, refreshed_revision = personas.find_revision(selected.id)
    assert refreshed_profile.status is ConfigurationStatus.DISABLED
    assert refreshed_revision.id == selected.id
    with pytest.raises(ConfigurationDisabled):
        await service.require_active_revision(selected.id)

    historical = agents.compilation(agents.list_agents()[0].current_revision.id, selected.id)
    assert historical.prompt_hash == compiled.prompt_hash
    assert [event["action"] for event in audits] == [
        "persona.create",
        "persona.revise",
        "persona.status",
    ]
    assert all(
        set(cast(dict[str, object], event["metadata"])) == {"personaId", "revision"}
        for event in audits
    )


@pytest.mark.asyncio
async def test_prompt_compile_span_is_parented_to_run_execute_span() -> None:
    metrics = MetadataMetrics()
    provider = _PromptLifecycleProvider()
    models = await provider.list_models()
    store = ConversationStore()
    _, _, run = await store.create(
        "issuer", "owner", "prompt parent", "chat", models, str(uuid4())
    )
    coordinator = RunCoordinator(store, EventPublisher(), InMemoryOutbox(), metrics=metrics)

    await coordinator.execute(run.id, provider)

    records = metrics.snapshot()
    execute = next(
        record
        for record in records
        if record.component_id == "aura.execution.run_coordinator"
        and dict(record.trace_attributes).get("operation") == "run.execute"
        and record.trace_id == run.id.hex
    )
    prompt = next(
        record
        for record in records
        if record.component_id == "aura.runtime.prompt_compilation"
        and dict(record.trace_attributes).get("operation") == "prompt.compile"
        and record.trace_id == run.id.hex
    )
    assert prompt.parent_span_id == execute.span_id
