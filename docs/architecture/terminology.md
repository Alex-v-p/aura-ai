# Terminology

> **Status:** Architecture baseline. Definitions are condensed from [architecture handover §4](./aura-ai-architecture-handover.md#4-core-terminology). The [feature catalogue](../specifications/aura-ai-envisioned-feature-catalogue.md) uses these terms but does not redefine them.

| Term | Meaning |
|---|---|
| **Platform** | The complete Aura system: shared runtime engines, domains, integrations, clients, and infrastructure. |
| **Agent profile** | A persistent agent identity containing stable identity-level information and revision history. |
| **Agent revision** | An immutable version of an agent's effective persona, prompts, model, tool, memory, execution, workspace, and configuration references. Every run records the revision used. |
| **Persona** | A reusable expression and interaction profile. It may shape presentation but cannot change truth, weaken security, or widen access. |
| **Conversation** | An ordered user-facing message thread associated with an agent. An agent may have many conversations. |
| **Run** | One execution initiated by a message, event, schedule, API request, another agent, or evaluation job, with exact context, lineage, activity, result, and status. |
| **Task** | Durable work that may outlive its initiating request, such as indexing, monitoring, research, waiting, or a multi-step workflow. |
| **Workspace** | A bounded project or activity context with selected memories, documents, tools, policies, and artifacts. |
| **Memory scope** | An ownership and visibility boundary for durable memory. The expected hierarchy is platform, household, user, workspace, agent, conversation, task, and run. |
| **Artifact** | A generated or uploaded file-like result such as a report, patch, image, export, attachment, plan, dataset, or evaluation output. |
| **Tool** | A typed model-facing capability contract describing what a model may request. It does not own application logic. |
| **Port** | A narrow interface defined by Core for a required external capability. |
| **Provider / adapter** | A concrete implementation of a port, such as Ollama, Home Assistant, SearXNG, CalDAV, SMB, or S3-compatible storage. |
| **Observable component** | A versioned, instrumented execution unit with a stable identifier, typed inputs and outputs, operational metrics, quality evaluators, and optional replay support. |

## Documentation status labels

- **Architecture baseline:** a settled boundary or constraint in the handover.
- **Envisioned:** a catalogue capability, not a delivery commitment or settled implementation.
- **Open decision:** a choice intentionally left unresolved; see [open decisions](../specifications/open-decisions.md).

## Related working summaries

[Architecture overview](./overview.md) · [Agents and execution](../specifications/agents-and-execution.md) · [Memory and knowledge](../specifications/memory-and-knowledge.md) · [Open decisions](../specifications/open-decisions.md)
