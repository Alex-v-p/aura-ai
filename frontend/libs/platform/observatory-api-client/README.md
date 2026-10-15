# Observatory API client

Transport-only TypeScript types and operation metadata generated from
`contracts/openapi/observatory-v1.yaml`. Authentication, authorization,
feature state, retries, and SSE snapshot reconciliation stay outside this
library.

Generate the checked-in client with `pnpm exec nx run observatory-api-client:generate`
and verify drift with `pnpm exec nx run observatory-api-client:check-generated`.
