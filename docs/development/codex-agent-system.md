# Codex agent system

Aura uses a gated plan → implement → review → integrate workflow. The primary thread is the Delivery Lead: it creates a validated work item, selects bounded workers and reviewers, reconciles results, and requests an ADR when the architecture does not authorize a choice. It does not implement product behavior.

## Roles and permissions

Read-only roles are Repository Mapper, General Reviewer, Architecture Guardian, Observability Steward, and Security and Privacy Reviewer. Writing roles are Contract Steward, Core Worker, Observatory Worker, Frontend Worker, Platform Worker, Test and Evaluation Engineer, and Integration Maintainer. Writing permission is further narrowed by the active work item's ownership zone and paths.

Reviewers report findings; the owning worker repairs them; the same reviewer verifies the correction. The Integration Maintainer combines reviewed commits and resolves only mechanical conflicts. A blocking review cannot be downgraded by the Delivery Lead.

The activation matrix is:

| Change | Minimum specialist roles |
|---|---|
| Local product fix | Owning worker, General Reviewer |
| Dependency or boundary | Owning worker, General Reviewer, Architecture Guardian |
| Contract/protocol | Contract Steward, consumers, General Reviewer, contract tests |
| Observable runtime behavior | Owning worker, Observability Steward, Test and Evaluation Engineer |
| Security, privacy, permissions, sensitive data | Owning worker, Security and Privacy Reviewer |
| Multiple writers | Relevant reviewers, Integration Maintainer |
| Governance/enforcement | Platform Worker, General Reviewer, Architecture Guardian, human approval |

## Models and economy

The installed Codex catalog was inspected on 2026-10-04. The handover's proposed `gpt-6-luna` and `gpt-6.1-sol` names were unavailable, so the mapping is:

| Tier | Runtime mapping | Use |
|---|---|---|
| efficient | `gpt-5.6-luna`, high | mapping, contained implementation, routine tests, integration bookkeeping |
| balanced | `gpt-5.6-sol`, medium | planning, contracts, normal reviews, cross-file reasoning |
| deep | `gpt-5.6-sol`, high | architecture, security, privacy, observability, high-risk review |
| exceptional | explicit owner-approved available model and effort | rare escalation only |

Dynamic workers omit a model so an explicit spawn selection or the efficient project default applies. Escalate after evidence of unresolved ambiguity, materially different failed attempts, cross-boundary scope, or security/data/concurrency risk. Record actual model, reasoning effort, and tier in every implementation handoff. Do not spawn agents for trivial work or repeat repository-wide mapping.

Project configuration disables web search, browser use, and sandbox network access by default, and the project defines no MCP servers. Role instructions prohibit external tools unless a task explicitly requires them. Codex has no generic project wildcard that removes arbitrary user- or administrator-configured MCP servers or global skills from a custom agent; managed policy must restrict those inherited surfaces when hard isolation is required. The project skill catalogue is kept within a 2,000-token budget, and agents should activate only the workflow skill relevant to their assignment.

## Delegation and trust

In-thread subagents share the parent filesystem and are capped at eight open spawned threads. Use them mainly for bounded exploration and review. Independent writing uses separate Git worktrees and the work item's parallelism declaration; fanout may use at most fifteen worktree chats.

Project hooks and command rules are loaded only after the repository `.codex` layer is trusted. In Codex CLI, inspect and trust the exact hook definitions with `/hooks`. Hooks are guardrails with incomplete tool coverage; the Python validators and repository merge controls remain authoritative. Sandbox and permission choices on the parent turn may override custom-agent defaults, so read-only review must also be enforced by workflow and changed-path checks.

Official references: [custom agents](https://learn.chatgpt.com/docs/agent-configuration/subagents), [hooks](https://learn.chatgpt.com/docs/hooks), [rules](https://learn.chatgpt.com/docs/agent-configuration/rules), [skills](https://learn.chatgpt.com/docs/build-skills), and [AGENTS.md](https://learn.chatgpt.com/docs/agent-configuration/agents-md).
