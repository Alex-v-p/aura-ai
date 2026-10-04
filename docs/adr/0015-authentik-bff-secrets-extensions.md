# ADR-0015: Authentik BFF sessions, SOPS/age, and isolated extensions

- **Status:** Accepted
- **Date:** 2026-10-04
- **Scope:** Identity, household authorization, secrets, provider credentials, and third-party extensions

## Decision

Use the OIDC Authorization Code flow through Authentik, with separate Aura and Observatory clients and audiences. The BFF validates issuer, audience, authorization-code state, and nonce before creating a same-origin session. Browser clients use HttpOnly, Secure cookies; server-side components hold provider tokens. Rotate sessions on authentication and privilege changes. Set an explicit SameSite policy and require origin-aware anti-CSRF validation for every state-changing HTTP command. Never put bearer credentials in browser storage, URLs, or SSE parameters. Aura owns household authorization, permissions, approvals, and audit. Observatory has a separate authentication and authorization boundary.

Store deploy-time secrets with SOPS and age and inject them at runtime through files or Docker secrets. Credentials are scoped, revocable, encrypted, and never placed in prompts, source control, ordinary logs, or unredacted tool results.

Execute third-party extensions in isolated processes or containers behind versioned MCP, OpenAPI, or gRPC contracts. Never import untrusted extension packages into Core. An extension remains disabled until its isolation profile defines least-privilege identity, filesystem and device access, network egress, secret delivery, resource limits, and per-extension capability grants. Bare processes are acceptable only with equivalent OS sandbox controls; a process or container boundary alone is not assumed to bound compromise. Extension discovery, sandbox policy, and exact credential retention remain explicit open decisions.

## Consequences

The web UI does not become a token store. Core can apply household and high-impact action policy independent of the identity provider. Extension failures and compromise are addressed by a versioned transport plus an explicit, least-privilege isolation profile; process/container separation alone is insufficient.

## Non-goals

This ADR does not define household role semantics, retention/encryption periods, provider allowlists, or deployment manifests. It does not install Authentik, SOPS, age, or an extension runtime.
