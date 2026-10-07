# Aura API client

Transport-only TypeScript types and operation metadata generated from
`contracts/openapi/aura-v1.yaml`. Feature state, authentication policy, retries,
SSE reconciliation, and other business behavior do not belong in this library.

Generate the canonical output on stdout:

```sh
node libs/platform/aura-api-client/tools/generate-client.mjs
```

Verify the checked-in output deterministically:

```sh
node libs/platform/aura-api-client/tools/generate-client.mjs --check
```
