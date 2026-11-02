# ADR-0018: Governed web research providers and durable research boundaries

- **Status:** Accepted
- **Date:** 2026-10-28
- **Scope:** Aura Core research, generated text artifacts, durable-task projection, and search/fetch providers
- **Related work item:** `AURA-0140`

## Context

Aura's technology baseline selects SearXNG, HTTPX, Trafilatura, and isolated
Playwright for governed web research, but selecting those technologies does not
by itself authorize new deployables or define their trust boundary. Web
retrieval crosses a hostile-content boundary and creates SSRF, redirect, parser,
browser, credential, resource-exhaustion, privacy, and prompt-injection risks.
Extended research must also remain durable and inspectable without making a
provider, queue, browser, or client the source of business truth.

Research reports are Aura product artifacts. They are distinct from source
documents, long-term memory, evaluation artifacts, and Observatory production
run evidence. A cross-product task list must likewise not become a second
authority over workflows, briefings, document processing, memory reindexing, or
research.

The owner approved conversational and dedicated research with Quick, Auto,
Deep, and bounded Agent-led modes; Core-owned generated Markdown, plain-text,
and JSON artifacts; and a federated Task Center on 2026-10-28.

## Decision

### Authority and ownership

Aura Core owns research admission, owner authorization, policy, durable task
state, checkpoints, budgets, cancellation, source provenance, citation
validation, synthesis, artifact metadata, and user-visible outcomes in
PostgreSQL. The Core worker executes the shared application use cases. The
transactional outbox and NATS JetStream carry identifier-only durable work and
metadata lifecycle events under at-least-once delivery; neither owns task
truth.

Search, static retrieval, and browser retrieval are replaceable provider ports.
Their responses are untrusted evidence. Provider output can never change
system instructions, grants, permissions, budgets, provider selection, task
transitions, artifact authorization, or memory. Research does not create or
reinforce long-term memory automatically.

### Authorized provider deployables

This decision authorizes three separately deployable provider services:

1. **SearXNG search provider** accepts bounded search requests from the Core
   worker and returns candidate URLs plus bounded result metadata.
2. **Static fetcher** uses HTTPX and Trafilatura to retrieve and extract
   supported public HTTPS content.
3. **Browser fetcher** uses Playwright for pages that require JavaScript or for
   which static extraction returned an explicit inadequate-content outcome.

The services do not import Core domain code. They expose a versioned,
authenticated, closed internal transport contract and receive an opaque
request identifier, validated target or query, deadlines, and provider limits.
They never receive a browser session, owner credential, Aura session cookie,
provider token, conversation, prompt, artifact object key, or database
credential. Only Core application use cases interpret provider results.

Core signs each short-lived request envelope with a research-provider signing
key. Providers receive only the corresponding public verification key, reject
expired or replayed request identifiers, and hold no shared or signing secret.
The network allowlist remains mandatory; signature verification is not a
substitute for destination or caller isolation.

### Trust, network, and destination boundary

The provider services are untrusted relative to Core and run with distinct
least-privilege workload identities. Their inbound listener is reachable only
from the Core worker's provider client network. They have no route to Aura or
Observatory PostgreSQL, Valkey, NATS, Garage, internal APIs, the host network,
container control sockets, or metadata endpoints.

Outbound access is restricted as follows:

- SearXNG may reach only its operator-configured public search-engine endpoints
  over HTTPS and the controlled DNS resolver.
- Each fetcher may reach the controlled DNS resolver and public HTTPS targets
  on TCP 443 only. HTTP, FTP, file, data, blob, websocket, and custom schemes
  are rejected. A future HTTP exception requires a new decision.
- DNS is resolved and every returned address is validated immediately before
  connection. Loopback, unspecified, private, link-local, multicast,
  carrier-grade NAT, documentation, benchmark, reserved, and metadata address
  ranges are rejected. A hostname with any prohibited answer is rejected as a
  mixed-address destination.
- Redirects are followed manually, with scheme, hostname, DNS, address, port,
  and policy revalidated at every hop. Userinfo and non-443 ports are rejected.
  At most five redirects are permitted.
- Core admits only URLs returned by the current task's search provider or
  explicit owner-supplied seed URLs. Core and the selected fetcher independently
  apply the URL policy; a signed or opaque admission record does not bypass
  destination validation.

Search result URLs are candidates, not authorization. Robots, content policy,
domain allow/deny rules, and legal or operator restrictions are evaluated
before retrieval. The v1 default permits public HTTPS destinations unless
denied; it does not authenticate to websites or bypass access controls.

### Secrets, process, filesystem, and resource boundary

Static and browser fetchers have no injected secrets and no ambient cloud,
host, browser-profile, proxy, or user credentials. SearXNG may receive only
operator-owned engine configuration needed for enabled public search engines;
it receives no Aura identity or content-store credential. Sensitive values are
never accepted in target URLs or query strings used for service diagnostics.

All three services run as a non-root user with a read-only root filesystem,
dropped Linux capabilities, `no-new-privileges`, a restrictive seccomp profile,
no host/device/socket mounts, and a private bounded `tmpfs`. Browser profiles
are ephemeral per request; downloads, extensions, password stores, clipboard,
camera, microphone, geolocation, notifications, service-worker persistence,
and cross-request caches are disabled. Temporary bytes and browser state are
destroyed when the request settles.

The baseline ceilings below may be made stricter by deployment configuration;
raising them requires review of this decision:

| Service | CPU | Memory | PIDs | Temporary storage | Concurrency |
|---|---:|---:|---:|---:|---:|
| SearXNG | 2 cores | 1 GiB | 128 | 128 MiB | 8 searches |
| Static fetcher | 2 cores | 1 GiB | 64 | 64 MiB | 4 fetches |
| Browser fetcher | 4 cores | 2 GiB | 256 | 256 MiB | 2 pages |

A search request has a 15-second provider deadline and at most 20 returned
candidates. A static fetch has a 20-second deadline; a browser fetch has a
45-second deadline. A response is rejected after 8 MiB of transferred or
decompressed bytes, 256 KiB of retained extracted UTF-8 text, or an unsupported
media type. The static allowlist is `text/html`, `application/xhtml+xml`, and
`text/plain`; sniffed and declared types must agree sufficiently to select the
parser. Archive, executable, media, PDF, XML other than XHTML, and arbitrary
JSON retrieval are excluded from v1. Browser output contains sanitized source
metadata and bounded extracted text only, never a browser archive, screenshot,
cookie jar, download, or executable response.

### Research modes, budgets, and client subscription

Every admitted request creates one owner-private durable research task. These
presets are the maximum v1 task envelopes:

| Mode | Wall time | Fetched sources | Search requests | Browser pages | Model calls | Interactive behavior |
|---|---:|---:|---:|---:|---:|---|
| Quick | 90 seconds | 6 | 2 | 1 | 4 | Initiating client subscribes through terminal settlement while connected |
| Auto | 10 minutes | 15 | 5 | 3 | 8 | Initiating client subscribes for 30 seconds, then shows background state if unfinished |
| Deep | 30 minutes | 30 | 10 | 8 | Background immediately after admission |
| Agent-led | 60 minutes | 60 | 20 | 15 | 32 | Background; may stop earlier on sufficient evidence |

Owner settings may lower preset defaults. A task request may lower any value
and may raise a saved default only up to the table's envelope. One explicit,
owner-authorized extension is permitted while a task is active, but cumulative
execution still cannot exceed the selected mode's envelope. Retries and
fallbacks consume the same counters. Static retrieval is the preferred path.
Browser retrieval is allowed only when the plan records a JavaScript need or a
static attempt records inadequate extraction, and the browser counter remains.

“Attached” is a client presentation state over the durable task subscription,
not a long-running tool call, run wait, or second task lifecycle. The
`web.research@1` attempt performs admission only, settles within the existing
30-second tool-attempt ceiling, and returns an opaque task reference. Quick and
Auto may keep the initiating client subscribed after the ordinary tools-disabled
synthesis has persisted its acknowledgement; research execution continues in
the worker independently of that run and connection.

Agent-led evidence sufficiency is a bounded outcome, not permission to run
indefinitely. The coordinator may settle early only after it records that the
main claims have claim-linked support, no known material contradiction remains
unreported, and further retrieval is unlikely to change the answer within the
remaining budget. If that condition is not reached, the task settles as
`budget_exhausted` with partial evidence; it is never labeled complete merely
because its maximum was reached.

### Kill switches and degraded operation

Admission, all static retrieval, and all browser retrieval have independent
operator kill switches. They default disabled until the corresponding
deployment and acceptance checks pass. Core checks a versioned capability
snapshot at task admission and before each provider dispatch. An admission
disable rejects the request before a task exists. A graceful provider disable
blocks new dispatch, places admitted work in the nonterminal `draining` stage,
and allows admitted calls to settle until their deadlines. An immediate stop
also requests provider cancellation; after bounded reconciliation, the task
settles with the canonical `interrupted` outcome if Core cannot finish safely.
There is no paused research lifecycle. With sufficient validated evidence the
coordinator may still settle `completed`; with usable incomplete evidence it
settles `partial`; without usable evidence it settles `failed` with bounded
failure class `provider_disabled` or `provider_unavailable`. Owner cancellation
alone settles `canceled`; exhausting an admitted envelope settles
`budget_exhausted`. A kill switch never deletes history or artifacts and never
implies that an in-flight network operation was reversed.

### Generated text artifacts

Core's artifacts domain owns generated Markdown, UTF-8 plain text, and JSON
artifacts. PostgreSQL owns owner scope, media type, immutable revision number,
SHA-256 digest, byte count, provenance, lineage, lifecycle, deletion truth,
and links to task, run, conversation, and optional workspace. Bytes use a
dedicated `aura-artifacts` Garage bucket and least-privilege Core identity,
separate from documents, evaluation artifacts, and Observatory run evidence.

Revisions are immutable and ordered; a new revision never overwrites prior
bytes. JSON uses canonical UTF-8 serialization for digesting. Preview is
owner-authorized and sanitized; download is owner-authorized, `no-store`, and
uses a safe filename and declared supported type. Artifact creation is an
internal Core application API in v1: user upload, image, PDF, archive, code
execution, and arbitrary binary content are excluded.

Retention is manual-only. Explicit deletion is CSRF-protected, idempotent, and
marks the artifact unavailable before or atomically with object deletion work.
PostgreSQL retains content-free deletion and lineage truth. Admitted work is
`deletion_pending`; a known retryable failure is `deletion_failed`; and a
storage timeout or ambiguous provider response is `cleanup_unknown` until
reconciliation. None is a false success or permission to re-upload.

### Federated Task Center

The execution domain owns an owner-private, rebuildable task read projection.
Research, workflow instances, briefing occurrences, document processing, and
memory reindexing remain authoritative in their source modules. A source emits
only an identifier-and-version invalidation through the transactional outbox.
The live consumer then calls that source's public
`query_task_projection_snapshots` query with a narrow internal
`task_projection:read` capability. The same query accepts either an exact
source reference for live reconciliation or a cursor for rebuild and returns
the same owner-scoped metadata DTO;
ordinary principals and other modules cannot call it. Projection consumers are
idempotent by source identity and source version and never read another
module's ORM model or table. Owner identity and private display values never
enter the invalidation event.

The projection normalizes display state to `queued`, `running`, `waiting`,
`completed`, `failed`, `canceled`, or `unknown`, while retaining a bounded
source kind, source status, progress, capability flags, timestamps, and opaque
deep-link identifiers. It contains no question, report, excerpt, document
name, reminder text, workflow input, object key, URL, or provider payload.
Details are fetched from the authoritative source after owner authorization.

Research stages `queued` map to `queued`; all active stages, including
`draining`, map to `running`; `completed` maps to `completed`; `canceled` maps
to `canceled`; and `partial`, `budget_exhausted`, `failed`, and `interrupted`
map to `failed`. Every task row and detail retains and displays the authoritative
source outcome, so normalization never hides partial or budget-exhausted work.
Research never maps to `waiting`; that state remains available to source types
with a real authoritative wait lifecycle. Projection gaps or ambiguous command
outcomes map to `unknown`.

Generic cancel and retry commands exist only when the source advertises the
capability. They route through the source module's public application API with
idempotency and expected-version checks. An unknown outcome remains unknown
until reconciled; Task Center never infers success or retries a potentially
consequential operation automatically.

### Conversation completion and privacy

Conversation-origin research records the initiating conversation and branch.
The admission-only tool returns an opaque task reference within 30 seconds and
the ordinary tools-disabled synthesis persists one acknowledgement/activity
item. Quick and Auto attachment is only continued client subscription to that
task; Auto changes to background presentation after 30 seconds. It never
changes the research lifecycle, holds the run open, or creates another message
writer. Deep and Agent-led use background presentation immediately.

One durable completion-delivery record is the sole terminal writer for every
conversation-origin mode. It appends exactly one compact cited assistant
message containing the authoritative outcome and owner-authorized task and
report-artifact references, and independently creates one owner notification.
Unique idempotency keys and separate append/notification substates ensure that
a notification retry cannot duplicate the append and an append retry cannot
duplicate either record. Completion at Auto's 30-second presentation boundary
therefore has no ownership race. Failure, cancellation, interruption, and
budget exhaustion append a truthful compact outcome with safe recovery actions
and any authorized partial artifact. Dedicated-page research has no
conversation append and completes through Task Center and one notification.

Queries, plans, URLs, titles, excerpts, reports, and artifact bytes are
owner-private. They are excluded from URLs, ordinary logs, metrics, traces,
metadata events, NATS envelopes, service-worker caches, and browser
persistence. SSE carries identifiers, bounded states, versions, and timestamps
for invalidation only; REST returns authorized detail. Source metadata and only
the bounded excerpts needed to support accepted citations are retained with
the task. Full fetched pages, temporary parser input, and browser state are
discarded at settlement.

Research and retained evidence have manual-only retention and an explicit
owner-authorized deletion command protected by same-origin CSRF, idempotency,
and expected version. Acceptance immediately makes question, plan, sources,
URLs, excerpts, citations, and report linkage unreadable. Durable cleanup uses
the separate content-deletion states `deletion_pending`, `deletion_failed`,
`cleanup_unknown`, or `deleted`; it never changes the historical research
terminal outcome. PostgreSQL retains a content-free tombstone and audit truth.
Linked artifacts are deleted only when the owner explicitly includes them,
using their independent deletion lifecycle. Immutable conversation messages
are not rewritten: their opaque citation references resolve to a content-free deleted
source state, and deleting conversation text remains a separate command.

## Consequences

- Web research can be implemented without giving Core, a browser, or fetched
  content ambient network or credential authority.
- Provider compromise is bounded by independent identities, network segments,
  filesystems, resources, and contracts, although provider output must still be
  treated as hostile.
- Research is durable and reconstructable from Core truth; provider, NATS,
  browser, and client failures cannot become hidden task authority.
- Artifact and Task Center behavior require additive versioned contracts and
  migrations in follow-on work items; this ADR adds neither.
- Browser retrieval is intentionally costlier and narrower than static
  retrieval. Unsupported content is reported rather than silently processed.
- Manual-only retention increases owner control and storage responsibility;
  automatic expiry requires a future decision.

## Required follow-up

Follow-on work must add versioned contracts before runtime implementation,
security tests for destination and sandbox controls, stable observable
components for planning/search/fetch/extraction/citation/synthesis/artifact and
projection operations, evaluation for claim support and injection resistance,
and capability-gated rollout. The detailed ownership and sequence are recorded
in [Governed research, artifacts, and Task Center](../design/governed-research-artifacts-and-tasks.md).

## Non-goals

This decision does not implement providers, deployment, contracts, migrations,
clients, model prompts, or product UI. It does not authorize authenticated web
browsing, arbitrary URL tools, HTTP destinations, cloud research providers,
public artifact sharing, user artifact uploads, PDFs, images, archives,
automatic retention expiry, or Observatory ingestion of research content.
