"""Runtime prompt compiler telemetry and failure-boundary tests."""

from dataclasses import replace
from uuid import uuid4

import pytest
from aura_core.domains.interaction.agents.public import AgentCatalog
from aura_core.domains.interaction.personas.public import PersonaCatalog
from aura_core.platform.telemetry import MetadataMetrics
from aura_core.runtime.prompting.public import PromptCompiler


def test_prompt_compiler_records_metadata_only_success() -> None:
    metrics = MetadataMetrics()
    personas = PersonaCatalog()
    catalog = AgentCatalog(personas)
    revision = catalog.list_agents()[0].current_revision
    persona = personas.list_personas()[0].current_revision

    compiled = PromptCompiler(
        catalog.bundle,
        metrics,
        trace_id="a" * 32,
        run_id="b" * 32,
        conversation_id="c" * 32,
    ).compile(revision, persona)

    records = [item for item in metrics.snapshot() if item.kind == "span"]
    assert len(records) == 1
    record = records[0]
    assert record.component_id == "aura.runtime.prompt_compilation"
    assert record.metric == "prompt_compile_duration_ms"
    attributes = dict(record.trace_attributes)
    assert attributes["prompt_hash"] == compiled.prompt_hash
    assert attributes["prompt_component_count"] == "4"
    assert attributes["compiled_prompt_size"] == str(len(compiled.text))
    assert "instructions" not in str(record)
    assert "local-first household assistant" not in str(record)


def test_prompt_compiler_records_classified_failure_without_content() -> None:
    metrics = MetadataMetrics()
    personas = PersonaCatalog()
    catalog = AgentCatalog(personas)
    revision = catalog.list_agents()[0].current_revision
    wrong_persona = replace(personas.list_personas()[0].current_revision, id=uuid4())

    with pytest.raises(ValueError):
        PromptCompiler(catalog.bundle, metrics, trace_id="d" * 32).compile(
            revision, wrong_persona
        )

    records = [item for item in metrics.snapshot() if item.kind == "span"]
    assert len(records) == 1
    record = records[0]
    assert dict(record.dimensions)["outcome"] == "error"
    assert dict(record.dimensions)["error_class"] == "validation"
    attributes = dict(record.trace_attributes)
    assert attributes["compiled_prompt_size"] == "0"
    assert attributes["prompt_component_count"] == "0"
    assert "instructions" not in str(record)
