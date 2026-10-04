# ADR-0014: Aura-owned execution with an optional LangGraph backend

- **Status:** Accepted
- **Date:** 2026-10-04
- **Scope:** Aura agent runtime, runs, workflows, checkpoints, and evaluation

## Decision

Aura owns runtime contracts and domain truth: run lifecycle, lineage, budgets, permissions, tools, jobs, events, model policy, persistence, cancellation, approvals, and audit. Provide a lightweight Aura executor for simple runs. Provide an optional LangGraph executor behind the Aura-owned executor interface for branching, loops, pauses, and resumable graphs.

LangGraph checkpoints are executor-internal implementation state and reference Aura run IDs. LangGraph node/state concepts must not leak into Aura domain contracts, generated clients, events, or user-facing authority. PostgreSQL remains the source of truth for Aura run state; executor recovery reconciles through Aura commands.

## Consequences

Aura can replace or add executors without changing product contracts. Simple runs avoid unnecessary orchestration overhead. Graph features can be adopted only where their measured behavior is useful. Evaluation and observability identify Aura components and runs rather than treating a backend library as a product boundary.

## Non-goals and open decisions

This does not select an exact model inventory, judge model, graph persistence schema, or workflow retention period. Those remain open owner decisions and future implementation work.
