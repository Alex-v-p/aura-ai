# Agents and execution

> **Status:** Architecture baseline plus envisioned capabilities. Sources: [architecture handover §§4 and 7](../architecture/aura-ai-architecture-handover.md#4-core-terminology) and catalogue groups [`AGT`](./aura-ai-envisioned-feature-catalogue.md#3-agents-and-personas), [`RUN`](./aura-ai-envisioned-feature-catalogue.md#4-conversations-runs-and-tasks), [`DLG`](./aura-ai-envisioned-feature-catalogue.md#5-delegation-and-multi-agent-collaboration), [`MOD`](./aura-ai-envisioned-feature-catalogue.md#6-model-runtime-and-ai-workloads), and [`CTX`](./aura-ai-envisioned-feature-catalogue.md#7-prompting-and-context-assembly).

## Baseline model

An agent is a persistent profile with immutable revisions, not a separate implementation. A revision references its persona, prompt bundle, model policy, tool policy, memory policy, execution policy, optional workspace, and configuration metadata. Every run records the exact revision and policy versions it used.

Personas control expression and interaction style. They do not change factual truth, override platform governance, or grant access. Conversations are user-facing message threads; runs are individual executions; durable tasks may outlive a request.

Every run receives an explicit, lifetime-immutable context containing the principal, household, workspace, agent revision, conversation, run lineage, task, channel, device, room, memory scopes, tool grants, model policy, execution budget, deadline, and trace identity. New state is recorded explicitly rather than through mutable global context.

## Execution rules

- Independent conversations and agents may run concurrently within resource limits.
- Mutating work within one conversation is ordered unless an explicit branch exists.
- Independent read-only tools may run concurrently; conflicting mutations require serialization, locks, version checks, or idempotency.
- Runs have explicit time, step, token, tool, retry, cost, and resource limits with cancellation and structured failure records.
- Prompts are compiled from versioned platform, governance, agent, persona, context, memory, document, tool, task, and output components. Selected context and compiled provenance remain traceable.
- Agents select capability and workload policies, not concrete GPUs. Scheduling accounts for provider availability and asymmetric resources.

## Envisioned capabilities

The catalogue envisions user-defined agents and personas, revision comparison, ownership and visibility controls, conversation branching and search, resumable tasks, scheduled and event-driven runs, structured outputs, capability-aware model routing, capacity isolation, context ranking and budgeting, and detailed run inspection.

Delegation creates bounded child runs with a precise objective, explicitly transferred context, narrowed permissions, independent budgets, lineage, and a typed result or artifact. Delegation must never silently widen the parent's access.

## Related working summaries

[Terminology](../architecture/terminology.md) · [Module boundaries](../architecture/module-boundaries.md) · [Memory and knowledge](./memory-and-knowledge.md) · [Tools, automation, and integrations](./tools-automation-and-integrations.md) · [Open decisions](./open-decisions.md)
