# Observatory API client

Transport-only TypeScript types and operation metadata generated from
`contracts/openapi/observatory-v1.yaml`. Authentication, authorization,
feature state, retries, and SSE snapshot reconciliation stay outside this
library.

Run inspection snapshots remain JSON. `getRunEvidence` is generated as a
`Blob` response with `responseMode: 'blob'` only because the OpenAPI operation
enumerates the approved, one-MiB evidence media types. The generator rejects
generic `application/octet-stream`, unbounded schemas, or media drift. Content
rendering, persistence prevention, digest verification, and deletion state
belong to the Observatory feature/runtime layers, not this transport client.

Generate the checked-in client with `pnpm exec nx run observatory-api-client:generate`
and verify drift with `pnpm exec nx run observatory-api-client:check-generated`.
