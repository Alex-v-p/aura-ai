import type {
  ComponentCollection,
  ComponentDefinition,
  ComponentDependencyTopology,
  ComponentHealth,
  CreateRetentionActionRequest,
  DeploymentCollection,
  HealthHistoryPage,
  LiveEvent,
  Overview,
  RetentionAction,
  RunDetail,
  RunPage,
  SelfMonitoringSnapshot,
  TraceDetail,
} from '@aura/observatory-api-client';
import { OBSERVATORY_API_OPERATIONS } from '@aura/observatory-api-client';

export type ObservatoryViewState = 'loading' | 'ready' | 'empty' | 'stale' | 'disconnected' | 'error';
export type ObservatoryResource = 'overview' | 'runs' | 'components' | 'deployments';
export type ObservatoryResourceState = 'loading' | 'ready' | 'empty' | 'stale' | 'error';
export type ObservatoryAuthState = 'checking' | 'authenticated' | 'initial' | 'expired' | 'explicit_logout' | 'logout_unconfirmed' | 'callback_failed' | 'callback_denied' | 'identity_unavailable';
export type ObservatoryAuthRecoveryMarker = 'state_missing' | 'state_invalid' | 'code_missing' | 'provider_denied' | 'callback_failed';

/** Keep an async snapshot from mutating state after a newer load starts. */
export function isCurrentLoadSequence(sequence: number, currentSequence: number): boolean {
  return sequence === currentSequence;
}

/** Identify an authentication loss without class-coupling this state module to a transport. */
export function hasSessionRequiredRejection(
  results: ReadonlyArray<PromiseSettledResult<unknown>>,
  isSessionRequired: (reason: unknown) => boolean,
): boolean {
  return results.some((result) => result.status === 'rejected' && isSessionRequired(result.reason));
}

/**
 * Coalesce bursty live notifications into one active refresh and one trailing
 * refresh. This is deliberately timer-free: the active request provides the
 * backpressure boundary and the trailing request guarantees the latest event
 * is eventually reflected without creating an unbounded queue.
 */
export class LiveRefreshCoordinator {
  private running = false;
  private pending = false;

  get isRunning(): boolean { return this.running; }
  get hasPending(): boolean { return this.pending; }

  request(run: () => Promise<void>, canRun: () => boolean = () => true): void {
    if (!canRun()) return;
    if (this.running) {
      this.pending = true;
      return;
    }
    this.running = true;
    void run().then(() => undefined, () => undefined).finally(() => {
      this.running = false;
      if (!this.pending) return;
      this.pending = false;
      if (canRun()) this.request(run, canRun);
    });
  }

  cancel(): void { this.pending = false; }
}

const MAX_RETURN_PATH_LENGTH = 2048;
const SENSITIVE_RETURN_KEYS = new Set([
  'access', 'accesstoken', 'apikey', 'aud', 'authorization', 'authorisation',
  'authorizationcode', 'authorisationcode', 'authorizationendpoint',
  'authorisationendpoint', 'azp', 'bearer', 'bearertoken', 'clientkey',
  'clientid', 'clientassertion', 'clientsecret', 'cookie', 'cookievalue',
  'code', 'credential', 'credentials', 'credentialvalue', 'endpoint',
  'body', 'content', 'document', 'file', 'filename', 'input', 'memory',
  'message', 'output', 'payload', 'tool', 'toolargument', 'toolarguments',
  'toolresult', 'toolresults',
  'audience', 'error', 'errordescription', 'erroruri', 'endsessionendpoint',
  'idtoken', 'issuer', 'iss', 'jwksuri', 'loginhint', 'jwt', 'nonce',
  'password', 'prompt', 'privatekey', 'provider', 'redirecturi',
  'redirecturl', 'request', 'requesturi', 'response', 'responsemode',
  'responsetype', 'refreshtoken', 'secret', 'secretkey', 'session',
  'sessionid', 'scope', 'state', 'sub', 'subject', 'token', 'tokenhint',
  'tokenendpoint', 'userinfoendpoint', 'owner', 'owneridentity',
  'displayname', 'preferredusername', 'authrecovery',
]);
const SENSITIVE_RETURN_SUFFIXES = ['token', 'credential', 'credentials', 'secret', 'password'];
const SENSITIVE_RETURN_PREFIXES = ['error', 'oauthstate', 'loginstate', 'authcode', 'oauthcode'];

function isSensitiveReturnKey(key: string): boolean {
  const normalized = key.toLowerCase().replace(/[^a-z0-9]/g, '');
  return SENSITIVE_RETURN_KEYS.has(normalized)
    || SENSITIVE_RETURN_PREFIXES.some((prefix) => normalized.startsWith(prefix))
    || SENSITIVE_RETURN_SUFFIXES.some((suffix) => normalized.endsWith(suffix));
}

/** Keep callback recovery finite and content-free at the browser boundary. */
export function authStateForRecoveryMarker(marker: string | null): ObservatoryAuthState | null {
  switch (marker) {
    case 'provider_denied': return 'callback_denied';
    case 'state_missing':
    case 'state_invalid':
    case 'code_missing':
    case 'callback_failed': return 'callback_failed';
    default: return null;
  }
}

/** Consume the one-shot callback marker without retaining it in browser history. */
export function consumeAuthRecoveryMarker(): ObservatoryAuthState | null {
  if (typeof window === 'undefined') return null;
  const url = new URL(window.location.href);
  if (!url.searchParams.has('auth_recovery')) return null;
  const state = authStateForRecoveryMarker(url.searchParams.get('auth_recovery'));
  window.history.replaceState(null, '', boundedReturnPath(`${url.pathname}${url.search}`));
  return state;
}

export function boundedReturnPath(returnTo: string): string {
  if (typeof returnTo !== 'string' || returnTo.length > MAX_RETURN_PATH_LENGTH || !returnTo.startsWith('/') || returnTo.startsWith('//')) return '/';
  try {
    const url = new URL(returnTo, 'http://observatory.invalid');
    if (url.origin !== 'http://observatory.invalid' || !url.pathname.startsWith('/')) return '/';
    const query = new URLSearchParams();
    for (const [key, value] of url.searchParams.entries()) if (!isSensitiveReturnKey(key)) query.append(key, value);
    const sanitized = `${url.pathname}${query.toString() ? `?${query.toString()}` : ''}`;
    return sanitized.length <= MAX_RETURN_PATH_LENGTH ? sanitized : '/';
  } catch {
    return '/';
  }
}

export function ownerLoginHref(baseUrl = '', returnTo = '/'): string {
  return `${baseUrl}${OBSERVATORY_API_OPERATIONS.startOwnerLogin.pathTemplate}?${new URLSearchParams({ return_to: boundedReturnPath(returnTo) }).toString()}`;
}

export type ObservatoryAuthCopy = { readonly title: string; readonly message: string; readonly action: string; readonly announcement: string; readonly retryAction: 'session' | 'logout' | null };
export function authCopyForState(state: ObservatoryAuthState): ObservatoryAuthCopy {
  switch (state) {
    case 'expired': return { title: 'Your Observatory session expired', message: 'Sign in again to continue viewing owner-only operational metadata.', action: 'Sign in as owner', announcement: 'Your Observatory session expired. Sign in again to continue.', retryAction: null };
    case 'explicit_logout': return { title: 'You are signed out of Observatory', message: 'Sign in again when you are ready to view owner-only operational metadata.', action: 'Sign in as owner', announcement: 'You are signed out of Observatory.', retryAction: null };
    case 'callback_denied': return { title: 'Owner sign-in was not authorized', message: 'No Observatory session was created. You can try signing in again.', action: 'Try owner sign-in again', announcement: 'Owner sign-in was not authorized. You can try again.', retryAction: null };
    case 'callback_failed': return { title: 'Owner sign-in could not be completed', message: 'No Observatory session was created. Try signing in again.', action: 'Try owner sign-in again', announcement: 'Owner sign-in could not be completed. You can try again.', retryAction: null };
    case 'logout_unconfirmed': return { title: 'Sign-out could not be confirmed', message: 'The Observatory service did not confirm sign-out. Your session may still be active; try again.', action: 'Try sign-out again', announcement: 'Sign-out could not be confirmed. Your session may still be active.', retryAction: 'logout' };
    case 'identity_unavailable': return { title: 'Owner sign-in is temporarily unavailable', message: 'The identity service did not respond. Check your session again, or use sign-in when it is available.', action: 'Check session again', announcement: 'Owner sign-in is temporarily unavailable.', retryAction: 'session' };
    case 'initial': return { title: 'Sign in to Observatory', message: 'This owner-only workspace requires an Observatory session.', action: 'Sign in as owner', announcement: 'Sign in to Observatory to continue.', retryAction: null };
    default: return { title: 'Checking your Observatory session', message: 'Your owner session is being checked.', action: 'Sign in as owner', announcement: '', retryAction: null };
  }
}
export type RequestToken = { readonly key: string; readonly generation: number };
/** Small in-memory gate for deduplicating and invalidating async panel requests. */
export class RequestGenerationGate {
  private generation = 0;
  private readonly active = new Map<string, number>();
  begin(key: string): RequestToken | null {
    if (this.active.has(key)) return null;
    const token = { key, generation: this.generation } as const;
    this.active.set(key, token.generation);
    return token;
  }
  invalidate(): void { this.generation += 1; this.active.clear(); }
  has(key: string): boolean { return this.active.has(key); }
  isCurrent(token: RequestToken): boolean { return token.generation === this.generation && this.active.get(token.key) === token.generation; }
  finish(token: RequestToken): void { if (this.active.get(token.key) === token.generation) this.active.delete(token.key); }
}
export type ComponentDetailRequestToken = { readonly componentId: string; readonly componentVersion: string; readonly generation: number };
/** Only the latest selected component detail request may publish or focus. */
export class ComponentDetailRequestGate {
  private generation = 0;
  private current: ComponentDetailRequestToken | null = null;
  begin(componentId: string, componentVersion: string): ComponentDetailRequestToken {
    const token = { componentId, componentVersion, generation: ++this.generation };
    this.current = token;
    return token;
  }
  invalidate(): void { this.generation += 1; this.current = null; }
  isCurrent(token: ComponentDetailRequestToken): boolean { return this.current?.generation === token.generation && this.current.componentId === token.componentId && this.current.componentVersion === token.componentVersion; }
}
export type RunFilters = { status?: RunPage['items'][number]['status']; component?: string; model?: string; deployment?: string; from?: string; to?: string };
export type ObservatorySnapshot = {
  overview: Overview | null;
  runs: RunPage | null;
  components: ComponentCollection | null;
  deployments: DeploymentCollection | null;
  state: ObservatoryViewState;
  lastUpdatedAt: string | null;
  lastEventId: string | null;
  error: string | null;
  partial: boolean;
  resourceErrors: ReadonlyArray<string>;
};

export type ComponentCatalogueSelection = {
  readonly componentId: string;
  readonly componentVersion: string;
  readonly definition: ComponentDefinition | null;
  readonly topology: ComponentDependencyTopology | null;
  readonly state: 'idle' | 'loading' | 'ready' | 'partial' | 'error';
  readonly error: string | null;
};

const MAX_DEFINITION_TEXT = 256;
const MAX_DEFINITION_ITEMS = 128;
const MAX_TOPOLOGY_NODES = 100;
const MAX_TOPOLOGY_EDGES = 200;
function record(value: unknown): Record<string, unknown> | null { return typeof value === 'object' && value !== null && !Array.isArray(value) ? value as Record<string, unknown> : null; }
function safeText(value: unknown): value is string { return typeof value === 'string' && value.length > 0 && value.length <= MAX_DEFINITION_TEXT && !Array.from(value).some((character) => { const code = character.codePointAt(0) ?? 0; return code < 0x20 || code === 0x7f; }); }
function boundedTextList(value: unknown): value is ReadonlyArray<string> { return Array.isArray(value) && value.length <= MAX_DEFINITION_ITEMS && value.every((item) => safeText(item)); }
function boundedNullableText(value: unknown): value is string | null { return value === null || safeText(value); }
function validHealth(value: unknown): value is ComponentHealth {
  const item = record(value); const outcomes = record(item?.['outcomes']);
  return Boolean(item && outcomes && Number.isSafeInteger(item['sampleCount']) && (item['sampleCount'] as number) >= 0 && (item['latencyP50Ms'] === null || (typeof item['latencyP50Ms'] === 'number' && Number.isFinite(item['latencyP50Ms']) && item['latencyP50Ms'] >= 0)) && (item['latencyP95Ms'] === null || (typeof item['latencyP95Ms'] === 'number' && Number.isFinite(item['latencyP95Ms']) && item['latencyP95Ms'] >= 0)) && (item['errorRate'] === null || (typeof item['errorRate'] === 'number' && Number.isFinite(item['errorRate']) && item['errorRate'] >= 0 && item['errorRate'] <= 1)) && ['succeeded', 'failed', 'cancelled', 'unknown'].every((key) => Number.isSafeInteger(outcomes[key]) && (outcomes[key] as number) >= 0));
}
/** Validate closed generated responses before metadata reaches the template. */
export function parseComponentDefinition(value: unknown): ComponentDefinition | null {
  const item = record(value); const dependencies = item?.['dependencies'];
  if (!item || !safeText(item['componentId']) || !safeText(item['componentVersion']) || !safeText(item['owner']) || !safeText(item['category']) || !safeText(item['inputSchemaId']) || !safeText(item['outputSchemaId']) || !boundedTextList(item['measurements']) || !boundedTextList(item['evaluators']) || !Array.isArray(dependencies) || dependencies.length > MAX_DEFINITION_ITEMS) return null;
  if (!dependencies.every((dependency) => { const edge = record(dependency); return Boolean(edge && safeText(edge['dependencyId']) && (edge['kind'] === 'component' || edge['kind'] === 'provider') && boundedNullableText(edge['requiredVersion'])); })) return null;
  return item as unknown as ComponentDefinition;
}
export function parseComponentDependencyTopology(value: unknown): ComponentDependencyTopology | null {
  const item = record(value); const nodes = item?.['nodes']; const edges = item?.['edges'];
  if (!item || !safeText(item['rootComponentId']) || !safeText(item['rootComponentVersion']) || !Array.isArray(nodes) || nodes.length > MAX_TOPOLOGY_NODES || !Array.isArray(edges) || edges.length > MAX_TOPOLOGY_EDGES || !boundedTextList(item['directDependencyIds']) || !boundedTextList(item['reverseDependencyIds']) || typeof item['partial'] !== 'boolean' || !safeText(item['snapshotAt'])) return null;
  if (!nodes.every((node) => { const entry = record(node); return Boolean(entry && safeText(entry['dependencyId']) && (entry['kind'] === 'component' || entry['kind'] === 'provider') && boundedNullableText(entry['componentVersion']) && (entry['definitionState'] === 'declared' || entry['definitionState'] === 'observed_only' || entry['definitionState'] === 'missing') && boundedTextList(entry['deployedVersions']) && (entry['availability'] === 'observed' || entry['availability'] === 'unobserved' || entry['availability'] === 'unavailable') && (entry['health'] === null || validHealth(entry['health'])) && typeof entry['partialHealth'] === 'boolean'); })) return null;
  if (!edges.every((edge) => { const entry = record(edge); return Boolean(entry && safeText(entry['sourceId']) && safeText(entry['sourceVersion']) && safeText(entry['targetId']) && boundedNullableText(entry['targetVersion']) && (entry['targetKind'] === 'component' || entry['targetKind'] === 'provider') && (entry['resolution'] === 'resolved' || entry['resolution'] === 'missing_definition' || entry['resolution'] === 'version_unavailable')); })) return null;
  return item as unknown as ComponentDependencyTopology;
}
export function componentAvailabilityLabel(availability: string): string {
  switch (availability) {
    case 'observed': return 'Observed';
    case 'unobserved': return 'Unobserved';
    case 'unavailable': return 'Unavailable';
    default: return 'Unavailable';
  }
}
export type DependencyNodeIdentity = { readonly dependencyId: string; readonly componentVersion: string | null };
export function dependencyNodeForEdge<T extends DependencyNodeIdentity>(nodes: ReadonlyArray<T>, dependencyId: string, componentVersion: string | null): T | undefined {
  return nodes.find((node) => node.dependencyId === dependencyId && node.componentVersion === componentVersion);
}

/**
 * The health history surface is deliberately separate from the observation
 * snapshot. It is bounded by the API page size and is never persisted by the
 * web application.
 */
export type ObservatoryHealthHistory = {
  page: HealthHistoryPage | null;
  state: 'loading' | 'ready' | 'empty' | 'stale' | 'error';
  error: string | null;
};

/** A disconnected stream must not turn an already-empty history into stale data. */
export function healthHistoryAfterDisconnect(history: ObservatoryHealthHistory): ObservatoryHealthHistory {
  const hasCachedSnapshots = Boolean(history.page?.items.length);
  return {
    ...history,
    state: hasCachedSnapshots ? 'stale' : history.state,
    error: hasCachedSnapshots ? 'Live updates are disconnected. Showing the last known health history.' : null,
  };
}
export function resourceStateFor(
  resource: ObservatoryResource,
  value: unknown,
  failed: boolean,
  observedAt?: string,
): ObservatoryResourceState {
  if (failed) return value ? 'stale' : 'error';
  if (!value) return 'loading';
  if (resource === 'overview' && observedAt && isOverviewStale(observedAt)) return 'stale';
  if (resource === 'runs' || resource === 'components' || resource === 'deployments') {
    const items = (value as { items?: ReadonlyArray<unknown> }).items;
    return items?.length ? 'ready' : 'empty';
  }
  return 'ready';
}
export type ComponentCatalogueView = 'loading' | 'error' | 'empty' | 'data';
/** A stale catalogue keeps its last successful cards visible; without cards it falls back to empty recovery copy. */
export function componentCatalogueViewFor(state: ObservatoryResourceState, itemCount: number): ComponentCatalogueView {
  if (state === 'loading') return 'loading';
  if (state === 'error') return 'error';
  if (state === 'stale' && itemCount > 0) return 'data';
  if (state === 'ready' && itemCount > 0) return 'data';
  return 'empty';
}
export type RetentionRequest = CreateRetentionActionRequest & { confirmation: 'delete-before' | 'purge-all' };
export type RetentionState = { action: RetentionAction | null; running: boolean; error: string | null };
export type SelectedRun = { detail: RunDetail | null; trace: TraceDetail | null };
export const OBSERVATORY_STALE_AFTER_MS = 5 * 60 * 1000;
export function isOverviewStale(observedAt: string, now = Date.now()): boolean {
  const timestamp = Date.parse(observedAt);
  return !Number.isFinite(timestamp) || now - timestamp > OBSERVATORY_STALE_AFTER_MS;
}
/** Convert the value from a datetime-local control as UTC, never browser-local time. */
export function datetimeLocalToUtc(value: string): string | null {
  const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?$/.exec(value);
  if (!match) return null;
  const iso = `${match[1]}-${match[2]}-${match[3]}T${match[4]}:${match[5]}:${match[6] ?? '00'}Z`;
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? null : date.toISOString();
}
export function normalizeRunFilters(filters: RunFilters): RunFilters {
  const from = filters.from ? datetimeLocalToUtc(filters.from) : undefined;
  const to = filters.to ? datetimeLocalToUtc(filters.to) : undefined;
  return { status: filters.status, component: filters.component, model: filters.model, deployment: filters.deployment, ...(from ? { from } : {}), ...(to ? { to } : {}) };
}
export type { ComponentCollection, ComponentDefinition, ComponentDependencyTopology, ComponentHealth, DeploymentCollection, HealthHistoryPage, LiveEvent, Overview, RetentionAction, RunDetail, RunPage, SelfMonitoringSnapshot, TraceDetail };
