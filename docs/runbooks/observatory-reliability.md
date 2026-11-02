---
title: Observatory reliability and acceptance
---

# Observatory reliability

This runbook covers the disposable local acceptance stack. It is intentionally
separate from the normal Aura state root: acceptance creates an ignored,
project-scoped fixture directory, file-injected fixture credentials, named
volumes, and an isolated Docker network. It never reads `.env`, operator
secrets, or production volumes.

## Start and validate

From the repository root, run the regular disposable stack first:

```sh
./deploy/compose/local-http.sh up
```

The full-stack acceptance harness builds its own disposable project and cleans
it up on exit:

```sh
./deploy/compose/run-observatory-full-stack-e2e.sh
```

The evaluation resilience harness exercises the real runner, outbox, NATS,
worker, scheduler, and recovery paths:

```sh
./deploy/compose/run-evaluation-orchestration-e2e.sh
```

Both commands set `COMPOSE_PARALLEL_LIMIT=1`, use bounded timeouts, and remove
only their own Compose project and fixture directory. Set
`AURA_OBSERVATORY_E2E_KEEP_ON_FAILURE=1` for the full-stack runner when a
failed fixture must be inspected; remove that disposable project explicitly
with the project name printed by the runner, then remove the ignored fixture
directory. Never use `docker system prune` or an unscoped volume/network
cleanup.

Static checks can be run without starting a stack:

```sh
bash -n deploy/compose/run-observatory-full-stack-e2e.sh \
  deploy/compose/run-evaluation-orchestration-e2e.sh \
  deploy/compose/e2e/core-availability.sh
docker compose --profile observatory config --quiet
```

## Scoped diagnosis

Use the exact project name emitted by a retained run. These commands expose
service state and bounded, non-secret categories only:

```sh
docker compose -p "$AURA_E2E_PROJECT" ps
docker compose -p "$AURA_E2E_PROJECT" events --json --since=5m
```

Do not collect or print container log lines. Failed harness runs emit only
container inspect state and finite allowlisted diagnostic category counts.

The harness checks Core liveness and an authenticated owner session while each
available dependency is unavailable, then waits for the restarted service's
public health signal. It continues independent phases, but exits non-zero when
a required public capability is absent; a stopped service is never reported as
recovered merely because its container restarted.

Current capability matrix:

| Capability | Status | Evidence or limitation |
| --- | --- | --- |
| Core liveness/readiness and owner session | Verified | Public health and session probes during dependency phases. |
| Collector outage and Core exporter retry/drop | Verified when the persisted ingest and exporter counters transition | Missing transitions remain a blocker. |
| Observatory PostgreSQL, ClickHouse, session, API, and Web outage paths | Verified when their public health signals transition | The harness records bounded failure categories only. |
| Fixture Ollama provider outage | Verified when Core returns the bounded provider-unavailable problem envelope, then succeeds after restart | The probe requires a provider-specific code/category and `retryable=true`; generic 422/503 responses are insufficient. |
| Evaluation orchestration PG/NATS/score path | Verified by the evaluation harness when its disposable migrations complete | Dependency metrics are checked through Observatory-owned persisted metric series. |
| Garage artifact operation signal | Blocked prerequisite | No authorized public artifact-operation signal is currently admitted. |
| Evaluation worker public heartbeat/operation signal | Blocked prerequisite | Container health is not treated as an application signal. |
| Evaluation scheduler heartbeat transition | Blocked prerequisite unless before/stopped/restarted age samples are available | A historical non-negative sample is insufficient. |
| Core evaluation-runner NATS recovery metric | Blocked prerequisite | No authorized Observatory ingest query exists; Observatory `/metrics` is not used as a cross-service substitute. |
| Local-judge provider-failure metric and work admission | Blocked prerequisite | No authorized application admission path is available in this harness. |
| Artifact/operations recovery increments | Artifact is checked against a baseline; operations recovery is blocked unless the authorized browser workflow is explicitly enabled and seeded | When enabled, the harness baselines the exact bounded operation/outcome sample immediately before the workflow and requires its causal increment; it never accepts a historical positive sample. |

Garage, evaluation-worker, scheduler, and local-judge rows therefore remain
explicit blockers rather than claims of proven recovery. Full-stack browser
acceptance is likewise not claimed when the Observatory migration or a public
workflow prerequisite prevents the scenario from running.

## Recovery and lifecycle

Do not restart the host or remove shared Docker resources. A failed disposable
run is recoverable with its own Compose project:

```sh
docker compose -p "$AURA_E2E_PROJECT" start observatory-api
docker compose -p "$AURA_E2E_PROJECT" ps
```

The normal local launcher preserves sessions across `down`; its intentionally
destructive `reset` removes only the launcher-owned disposable state and
invalidates sessions. Use the repository command rather than deleting a broad
volume set:

```sh
./deploy/compose/local-http.sh down
./deploy/compose/local-http.sh reset
./deploy/compose/local-http.sh up
```

## Privacy-safe evidence

Capture only service names, exit codes, health states, bounded metric names,
and sanitized error categories. Do not copy credentials, cookies, bearer
tokens, URLs containing secrets, prompts, responses, tool arguments/results,
artifacts, screenshots containing private data, or raw provider diagnostics
into tickets or reports. The acceptance scans fail if synthetic content
sentinels or private field names appear in logs, metrics, SSE, URLs, or browser
persistence.

## Fixture Ollama limitation

The acceptance stack uses an in-network deterministic Ollama-compatible
fixture to exercise Core's public provider path. This is not evidence that an
authorized Observatory local-judge job, artifact round trip, or score path
was admitted. Those capabilities remain blocked until their existing public
application boundary is available. Host-local Ollama reachability is
intentionally non-blocking: when the host provider is unavailable, the Core
operation must return a bounded provider-specific unavailable result and then
recover after the provider restarts. The harness does not add host networking,
a proxy, privileges, cloud judges, or general egress to work around that
limitation.
