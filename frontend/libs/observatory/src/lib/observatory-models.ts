import type {
  ComponentCollection,
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
export type { ComponentCollection, ComponentHealth, DeploymentCollection, HealthHistoryPage, LiveEvent, Overview, RetentionAction, RunDetail, RunPage, SelfMonitoringSnapshot, TraceDetail };
