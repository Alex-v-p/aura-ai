# Documentation context routing

> **Role:** Routing policy for contributors, coordinating agents, reviewers, and future work items. Begin with the [documentation index](README.md).

## Context-loading policy

1. Do not read every file under `/docs` for every task.
2. Do not load both canonical deep references for every task.
3. Start with `docs/README.md`, the current request or work item, the relevant focused architecture summary, the relevant focused product summary, and applicable ADRs or module documentation when they exist.
4. Consult the full architecture handover only when changing service boundaries, domain or module ownership, a top-level repository area, cross-service communication, or the separation between Aura Core and Aura Observatory; interpreting an unresolved architectural issue; reviewing an architecture-wide change; or verifying a focused summary against its source.
5. Consult the full feature catalogue only when locating or interpreting a feature ID, checking whether a capability is already envisioned, reviewing broad product scope, or updating product-vision documentation.
6. Search a canonical file for the relevant heading, term, or feature ID before reading large unrelated sections.
7. Expand to an entire canonical document only when cross-cutting interpretation genuinely requires it.
8. Ordinary bounded tasks should normally use two to five relevant documents, not the whole documentation set.
9. Give reviewers the work item, changed files, acceptance criteria, and relevant summaries rather than every project document.
10. Coordinating agents should route documentation deliberately instead of forwarding all available context to every subagent.

The canonical handover and feature catalogue are deep references. Their presence in the repository does not make them mandatory reading for every task.

## Task-to-document routing

| Work type | Read first | Consult on demand |
|---|---|---|
| Aura Core domain change | [Index](README.md), [module boundaries](architecture/module-boundaries.md), relevant focused specification | [Architecture handover](architecture/aura-ai-architecture-handover.md) |
| Agent runtime change | [Agents and execution](specifications/agents-and-execution.md), [module boundaries](architecture/module-boundaries.md) | [Agent and run architecture](architecture/aura-ai-architecture-handover.md#7-agent-and-run-architecture) |
| Memory or retrieval change | [Memory and knowledge](specifications/memory-and-knowledge.md), [module boundaries](architecture/module-boundaries.md) | Catalogue [`MEM`](specifications/aura-ai-envisioned-feature-catalogue.md#8-long-term-memory-and-world-model) and [`KNO`](specifications/aura-ai-envisioned-feature-catalogue.md#9-local-documents-and-filesystem-knowledge) sections |
| Tool or integration change | [Tools, automation, and integrations](specifications/tools-automation-and-integrations.md), [module boundaries](architecture/module-boundaries.md) | Catalogue [`TOL`](specifications/aura-ai-envisioned-feature-catalogue.md#11-tool-runtime-and-integrations) and [`INT`](specifications/aura-ai-envisioned-feature-catalogue.md#24-envisioned-integration-targets) sections |
| Home Assistant change | [Tools, automation, and integrations](specifications/tools-automation-and-integrations.md), [module boundaries](architecture/module-boundaries.md) | Catalogue [`HOM`](specifications/aura-ai-envisioned-feature-catalogue.md#12-home-assistant-and-household-control) section |
| Aura frontend change | [Clients and voice](specifications/clients-and-voice.md), [frontend boundaries](architecture/module-boundaries.md#frontend-boundaries), [Aura Web design foundation](design/README.md) | Catalogue [`AUI`](specifications/aura-ai-envisioned-feature-catalogue.md#15-aura-web-application) or [`WAL`](specifications/aura-ai-envisioned-feature-catalogue.md#16-aura-wall-application) section; focused design document for the surface being changed |
| Observatory change | [Observatory and evaluation](specifications/observatory-and-evaluation.md), [module boundaries](architecture/module-boundaries.md) | [Observatory architecture](architecture/aura-ai-architecture-handover.md#11-aura-observatory-architecture) and catalogue [`OBS`](specifications/aura-ai-envisioned-feature-catalogue.md#19-aura-observatory-operational-monitoring) section |
| Evaluation change | [Observatory and evaluation](specifications/observatory-and-evaluation.md) | Catalogue [`EVA`](specifications/aura-ai-envisioned-feature-catalogue.md#20-evaluation-experiments-and-regression-analysis) and [`CMP`](specifications/aura-ai-envisioned-feature-catalogue.md#21-component-specific-evaluation-coverage) sections |
| Infrastructure change | [Architecture overview](architecture/overview.md), [repository structure](architecture/repository-structure.md) | Catalogue [`OPS`](specifications/aura-ai-envisioned-feature-catalogue.md#23-deployment-infrastructure-and-operations) section |
| Architecture-wide change | [Architecture overview](architecture/overview.md), [module boundaries](architecture/module-boundaries.md), [open decisions](specifications/open-decisions.md), applicable ADRs | [Architecture handover](architecture/aura-ai-architecture-handover.md) |
| Product-scope lookup | [Product overview](specifications/product-overview.md) | [Feature catalogue](specifications/aura-ai-envisioned-feature-catalogue.md) |

## Selective architecture locator

Use these links to enter the canonical handover at the relevant section:

- [Product definition](architecture/aura-ai-architecture-handover.md#2-product-definition)
- [Architectural baseline](architecture/aura-ai-architecture-handover.md#3-architectural-baseline)
- [Core terminology](architecture/aura-ai-architecture-handover.md#4-core-terminology)
- [System context](architecture/aura-ai-architecture-handover.md#5-system-context-and-service-topology)
- [Aura Core logical architecture](architecture/aura-ai-architecture-handover.md#6-aura-core-logical-architecture)
- [Agents and runs](architecture/aura-ai-architecture-handover.md#7-agent-and-run-architecture)
- [Memory and knowledge](architecture/aura-ai-architecture-handover.md#8-memory-and-knowledge-architecture)
- [Tools and integrations](architecture/aura-ai-architecture-handover.md#9-tool-and-integration-architecture)
- [Frontend](architecture/aura-ai-architecture-handover.md#10-frontend-architecture)
- [Aura Observatory](architecture/aura-ai-architecture-handover.md#11-aura-observatory-architecture)
- [Data ownership](architecture/aura-ai-architecture-handover.md#12-data-and-storage-ownership)
- [Contracts and SDKs](architecture/aura-ai-architecture-handover.md#14-contracts-and-shared-sdks)
- [Dependency rules](architecture/aura-ai-architecture-handover.md#15-dependency-and-module-rules)
- [Security and governance](architecture/aura-ai-architecture-handover.md#16-security-and-governance)
- [Target repository tree](architecture/aura-ai-architecture-handover.md#18-full-target-repository-tree)
- [Technology baseline](architecture/aura-ai-architecture-handover.md#20-current-technology-baseline)
- [Unresolved decisions](architecture/aura-ai-architecture-handover.md#21-intentionally-unresolved-decisions)

## Feature-prefix locator

Each prefix links to its section in the canonical envisioned feature catalogue.

| Prefix | Capability area |
|---|---|
| [`GEN`](specifications/aura-ai-envisioned-feature-catalogue.md#2-product-wide-characteristics) | Product-wide characteristics |
| [`AGT`](specifications/aura-ai-envisioned-feature-catalogue.md#3-agents-and-personas) | Agents and personas |
| [`RUN`](specifications/aura-ai-envisioned-feature-catalogue.md#4-conversations-runs-and-tasks) | Conversations, runs, and tasks |
| [`DLG`](specifications/aura-ai-envisioned-feature-catalogue.md#5-delegation-and-multi-agent-collaboration) | Delegation |
| [`MOD`](specifications/aura-ai-envisioned-feature-catalogue.md#6-model-runtime-and-ai-workloads) | Models and inference |
| [`CTX`](specifications/aura-ai-envisioned-feature-catalogue.md#7-prompting-and-context-assembly) | Prompting and context |
| [`MEM`](specifications/aura-ai-envisioned-feature-catalogue.md#8-long-term-memory-and-world-model) | Memory and world model |
| [`KNO`](specifications/aura-ai-envisioned-feature-catalogue.md#9-local-documents-and-filesystem-knowledge) | Document knowledge |
| [`WEB`](specifications/aura-ai-envisioned-feature-catalogue.md#10-web-research) | Web research |
| [`TOL`](specifications/aura-ai-envisioned-feature-catalogue.md#11-tool-runtime-and-integrations) | Tools and integrations |
| [`HOM`](specifications/aura-ai-envisioned-feature-catalogue.md#12-home-assistant-and-household-control) | Home Assistant |
| [`AUT`](specifications/aura-ai-envisioned-feature-catalogue.md#13-timers-reminders-schedules-workflows-and-notifications) | Automation |
| [`VOI`](specifications/aura-ai-envisioned-feature-catalogue.md#14-voice-and-room-aware-interaction) | Voice |
| [`AUI`](specifications/aura-ai-envisioned-feature-catalogue.md#15-aura-web-application) | Aura Web |
| [`WAL`](specifications/aura-ai-envisioned-feature-catalogue.md#16-aura-wall-application) | Aura Wall |
| [`ART`](specifications/aura-ai-envisioned-feature-catalogue.md#17-artifacts-and-attachments) | Artifacts |
| [`GOV`](specifications/aura-ai-envisioned-feature-catalogue.md#18-identity-permissions-approvals-and-privacy) | Governance |
| [`OBS`](specifications/aura-ai-envisioned-feature-catalogue.md#19-aura-observatory-operational-monitoring) | Observatory |
| [`EVA`](specifications/aura-ai-envisioned-feature-catalogue.md#20-evaluation-experiments-and-regression-analysis) | Evaluation |
| [`CMP`](specifications/aura-ai-envisioned-feature-catalogue.md#21-component-specific-evaluation-coverage) | Component evaluation coverage |
| [`DEV`](specifications/aura-ai-envisioned-feature-catalogue.md#22-developer-platform-and-extensibility) | Developer platform |
| [`OPS`](specifications/aura-ai-envisioned-feature-catalogue.md#23-deployment-infrastructure-and-operations) | Deployment and operations |
| [`INT`](specifications/aura-ai-envisioned-feature-catalogue.md#24-envisioned-integration-targets) | Integration targets |

## Context-packet convention

Future work items may declare their documentation context explicitly:

```yaml
required_context:
  - docs/README.md
  - docs/architecture/module-boundaries.md
  - docs/specifications/memory-and-knowledge.md

optional_context:
  - docs/architecture/aura-ai-architecture-handover.md
  - docs/specifications/aura-ai-envisioned-feature-catalogue.md

context_reason:
  - The work affects scoped memory retrieval.
```

- `required_context` lists documents expected to be read before beginning the assignment.
- `optional_context` lists deep references that may be searched or read selectively when needed. It does not mean “load the entire file.”
- `context_reason` explains why each non-default document is relevant.

This convention documents context selection only. It does not define or implement a work-item system.

## Future instruction and agent constraints

- Do not copy either canonical deep reference into `AGENTS.md`.
- Do not configure either canonical reference as an automatically loaded instruction file.
- Do not tell every agent to read all files under `/docs`.
- Do not embed the full repository tree in root instructions.
- Do not embed the complete feature catalogue in an agent profile or skill.
- Root instructions should eventually point to `docs/README.md` and contain only concise non-negotiable invariants.
- Area-specific instructions should point to the smallest relevant summaries.
- Work items should declare `required_context` and `optional_context`.
- Agent profiles should not repeat documentation already available in focused summaries.

No agent configuration, instructions, hooks, skills, rules, profiles, or work-item machinery are created by this policy.
