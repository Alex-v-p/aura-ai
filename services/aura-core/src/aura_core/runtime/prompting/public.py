"""Deterministic prompt compilation and metadata-only provenance."""

from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass
from time import perf_counter
from typing import TYPE_CHECKING, Protocol
from uuid import UUID, uuid4

if TYPE_CHECKING:
    from aura_core.domains.interaction.agents.public import AgentRevision
    from aura_core.domains.interaction.personas.public import PersonaRevision


@dataclass(frozen=True, slots=True)
class PromptComponentRevision:
    id: UUID
    component: str
    revision: int
    content: str


@dataclass(frozen=True, slots=True)
class PromptBundleRevision:
    id: UUID
    revision: int
    platform: PromptComponentRevision
    governance: PromptComponentRevision


@dataclass(frozen=True, slots=True)
class PromptCompilation:
    text: str
    prompt_hash: str
    component_revision_ids: tuple[UUID, ...]
    component_count: int


class PromptMetricsPort(Protocol):
    """Metadata-only telemetry sink for the prompt compiler boundary."""

    def record_span(
        self,
        component: str,
        operation: str,
        duration_ms: float,
        *,
        trace_id: str,
        span_id: str,
        parent_span_id: str | None,
        dependency: str,
        outcome: str,
        error_class: str | None = None,
        provider: str | None = None,
        run_id: str | None = None,
        conversation_id: str | None = None,
        **trace_attributes: str,
    ) -> None: ...


class PromptCompiler:
    """Compile platform, governance, agent, then persona components."""

    def __init__(
        self,
        bundle: PromptBundleRevision,
        metrics: PromptMetricsPort | None = None,
        *,
        trace_id: str | None = None,
        parent_span_id: str | None = None,
        run_id: str | None = None,
        conversation_id: str | None = None,
    ) -> None:
        self.bundle = bundle
        self.metrics = metrics
        self.trace_id = (
            trace_id
            if trace_id is not None
            and len(trace_id) == 32
            and all(c in "0123456789abcdef" for c in trace_id)
            else uuid4().hex
        )
        self.parent_span_id = parent_span_id
        self.run_id = run_id
        self.conversation_id = conversation_id

    def compile(self, revision: AgentRevision, persona: PersonaRevision) -> PromptCompilation:
        started = perf_counter()
        # A revision is immutable provenance.  Never silently compile it with
        # a mutable/current persona or prompt bundle from another revision.
        try:
            if revision.persona_revision_id != persona.id:
                raise ValueError("agent revision references a different persona revision")
            if revision.prompt_bundle_revision_id != self.bundle.id:
                raise ValueError("agent revision references a different prompt bundle revision")
            parts = (
                ("platform", self.bundle.platform.id, self.bundle.platform.content),
                ("governance", self.bundle.governance.id, self.bundle.governance.content),
                (
                    "agent",
                    revision.id,
                    "\n".join(filter(None, (revision.purpose, revision.instructions))),
                ),
                ("persona", persona.id, persona.instructions),
            )
            rendered = "\n\n".join(text for _, _, text in parts if text)
            canonical = json.dumps(
                [
                    {"component": name, "revisionId": str(identifier), "text": text}
                    for name, identifier, text in parts
                ],
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode()
            compilation = PromptCompilation(
                rendered,
                hashlib.sha256(canonical).hexdigest(),
                tuple(identifier for _, identifier, _ in parts),
                len(parts),
            )
        except Exception:
            self._record(
                revision,
                duration_ms=(perf_counter() - started) * 1000,
                outcome="error",
                component_count=0,
                compiled_prompt_size=0,
                error_class="validation",
            )
            raise
        self._record(
            revision,
            duration_ms=(perf_counter() - started) * 1000,
            outcome="ok",
            component_count=compilation.component_count,
            compiled_prompt_size=len(compilation.text),
            prompt_hash=compilation.prompt_hash,
        )
        return compilation

    def _record(
        self,
        revision: AgentRevision,
        *,
        duration_ms: float,
        outcome: str,
        component_count: int,
        compiled_prompt_size: int,
        prompt_hash: str | None = None,
        error_class: str | None = None,
    ) -> None:
        if self.metrics is None:
            return
        attributes = {
            "agent_revision_id": str(revision.id),
            "agent_revision_number": str(revision.revision),
            "persona_revision_id": str(revision.persona_revision_id),
            "prompt_bundle_revision_id": str(revision.prompt_bundle_revision_id),
            "prompt_component_count": str(component_count),
            "compiled_prompt_size": str(compiled_prompt_size),
        }
        if prompt_hash is not None:
            attributes["prompt_hash"] = prompt_hash
        try:
            self.metrics.record_span(
                "aura.runtime.prompt_compilation",
                "prompt.compile",
                max(0.0, duration_ms),
                trace_id=self.trace_id,
                span_id=secrets.token_hex(8),
                parent_span_id=self.parent_span_id,
                dependency="prompt_compiler",
                outcome=outcome,
                error_class=error_class,
                run_id=self.run_id,
                conversation_id=self.conversation_id,
                **attributes,
            )
        except Exception:
            # Telemetry must never change deterministic prompt behavior.
            return


__all__ = [
    "PromptBundleRevision",
    "PromptCompilation",
    "PromptCompiler",
    "PromptComponentRevision",
    "PromptMetricsPort",
]
