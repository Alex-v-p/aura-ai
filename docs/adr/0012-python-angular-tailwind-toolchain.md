# ADR-0012: Python, Angular, Tailwind, workspace, and test toolchain

- **Status:** Accepted
- **Date:** 2026-10-04
- **Scope:** Aura Core, Aura Web, Aura Wall, Aura Observatory, voice satellites, and shared tooling

## Decision

Use Python 3.14 for services, workers, schedulers, CLI, evaluation, and satellites. Use strict TypeScript for browser clients. Python workspaces use `uv`; Core services use FastAPI, Pydantic 2, pydantic-settings, SQLAlchemy 2 async, psycopg 3, Alembic, HTTPX, Typer, and structlog. Ruff, Pyright, pytest, pytest-asyncio, and Hypothesis are the supported quality toolchain.

Use Node 24 LTS, Angular 22, Nx 23, pnpm 12, Tailwind 4, Angular CDK, Signals and RxJS for clients. NgRx Signal Store is reserved for complex shared state. Workbox 7, policy-gated Dexie/IndexedDB, Marked + DOMPurify, Shiki, Apache ECharts 6, Storybook, axe, Vitest 5, and Playwright are approved web support tools.

## Consequences

Shared Python application use cases serve API, workers, schedulers, CLI, and satellites. Angular shells compose applications while feature behavior lives in libraries. Tailwind and CDK remain generic UI primitives. Exact package versions are pinned by future lockfiles; no packages are added by this ADR.

Python and strict TypeScript are the approved languages. A third language requires measured need, owner approval, and a new ADR. Offline storage, Markdown rendering, and browser caching remain subject to the design foundation's privacy and accessibility rules.

## Boundaries and non-goals

This ADR does not create an Nx workspace, frontend package, Python service, manifest, lockfile, generated client, or runtime contract. It does not make catalogue capabilities a roadmap.
