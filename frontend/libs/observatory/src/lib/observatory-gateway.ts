import { Injectable, InjectionToken, inject } from '@angular/core';
import { ObservatoryApiClient, type ObservatoryOperationInput, type ObservatoryOperationResponse, type ObservatoryOperationId, OBSERVATORY_API_OPERATIONS } from '@aura/observatory-api-client';
import { extractSessionCsrf } from './observatory-session';

export const OBSERVATORY_API_BASE_URL = new InjectionToken<string>('OBSERVATORY_API_BASE_URL', { providedIn: 'root', factory: () => '' });
export const OBSERVATORY_CSRF_TOKEN = new InjectionToken<string>('OBSERVATORY_CSRF_TOKEN', { providedIn: 'root', factory: () => '' });
export type ObservatoryLiveHandler = (event: Event) => void;
export type ObservatoryLogoutResult = 'confirmed' | 'already_absent';

export interface ObservatoryGatewayPort {
  execute<K extends ObservatoryOperationId>(operation: K, input: ObservatoryOperationInput<K>, signal?: AbortSignal): Promise<ObservatoryOperationResponse<K>>;
  ensureSession(): Promise<void>;
  logout(): Promise<ObservatoryLogoutResult>;
  connectLive(onOpen: () => void, onMessage: ObservatoryLiveHandler, onError: () => void): void;
  reconnectLive(onOpen: () => void, onMessage: ObservatoryLiveHandler, onError: () => void): void;
  disconnectLive(): void;
  connectEvaluationLive(onOpen: () => void, onMessage: ObservatoryLiveHandler, onError: () => void): void;
  reconnectEvaluationLive(onOpen: () => void, onMessage: ObservatoryLiveHandler, onError: () => void): void;
  disconnectEvaluationLive(): void;
  connectOperationsLive(onOpen: () => void, onMessage: ObservatoryLiveHandler, onError: () => void): void;
  reconnectOperationsLive(onOpen: () => void, onMessage: ObservatoryLiveHandler, onError: () => void): void;
  disconnectOperationsLive(): void;
}
export const OBSERVATORY_GATEWAY = new InjectionToken<ObservatoryGatewayPort>('OBSERVATORY_GATEWAY');

export class ObservatorySessionRequiredError extends Error {
  constructor() { super('Owner Observatory session required.'); this.name = 'ObservatorySessionRequiredError'; }
}

export class ObservatoryLogoutUnconfirmedError extends Error {
  constructor() { super('Observatory sign-out could not be confirmed.'); this.name = 'ObservatoryLogoutUnconfirmedError'; }
}

export { extractSessionCsrf } from './observatory-session';

/** Same-origin transport adapter. It deliberately does not cache or persist responses. */
@Injectable({ providedIn: 'root' })
export class FetchObservatoryTransport implements ObservatoryGatewayPort {
  private readonly baseUrl = inject(OBSERVATORY_API_BASE_URL);
  private readonly csrf = inject(OBSERVATORY_CSRF_TOKEN);
  private readonly client = new ObservatoryApiClient({ execute: (operation, input, signal) => this.request(operation, input, signal) });
  private sessionCsrf: string | null = null;
  private source: EventSource | null = null;
  private evaluationSource: EventSource | null = null;
  private operationsSource: EventSource | null = null;

  execute<K extends ObservatoryOperationId>(operation: K, input: ObservatoryOperationInput<K>, signal?: AbortSignal): Promise<ObservatoryOperationResponse<K>> {
    return this.client.execute(operation, input, signal);
  }

  async ensureSession(): Promise<void> {
    if (this.sessionCsrf) return;
    const session = await this.client.execute('getOwnerSession', blankInput('getOwnerSession'));
    this.sessionCsrf = extractSessionCsrf(session);
    if (!this.sessionCsrf) throw new Error('Observatory session did not provide a CSRF token');
  }

  async logout(): Promise<ObservatoryLogoutResult> {
    const csrf = this.sessionCsrf ?? this.csrf;
    if (!csrf) throw new ObservatoryLogoutUnconfirmedError();
    try {
      await this.client.execute('logoutOwnerSession', { ...blankInput('logoutOwnerSession'), headers: { 'X-Observatory-CSRF': csrf } });
      this.sessionCsrf = null;
      return 'confirmed';
    } catch (error) {
      if (error instanceof ObservatorySessionRequiredError) {
        this.sessionCsrf = null;
        return 'already_absent';
      }
      // Keep only the opaque in-memory CSRF value so the owner can retry.
      throw error;
    }
  }

  connectLive(onOpen: () => void, onMessage: ObservatoryLiveHandler, onError: () => void): void {
    if (typeof EventSource === 'undefined' || this.source) return;
    this.source = new EventSource(`${this.baseUrl}${OBSERVATORY_API_OPERATIONS.streamEvents.pathTemplate}`, { withCredentials: true });
    this.source.onopen = onOpen;
    this.source.onmessage = onMessage;
    for (const eventName of ['snapshot', 'observation.changed', 'health.changed', 'retention.changed', 'heartbeat']) this.source.addEventListener(eventName, onMessage);
    this.source.onerror = onError;
  }

  reconnectLive(onOpen: () => void, onMessage: ObservatoryLiveHandler, onError: () => void): void {
    // Keep the native EventSource instance alive so its automatic reconnect
    // carries Last-Event-ID. A new source would lose the browser-managed
    // catch-up cursor and cannot set a custom header.
    if (this.source) return;
    this.connectLive(onOpen, onMessage, onError);
  }

  disconnectLive(): void { this.source?.close(); this.source = null; }

  connectEvaluationLive(onOpen: () => void, onMessage: ObservatoryLiveHandler, onError: () => void): void {
    if (typeof EventSource === 'undefined' || this.evaluationSource) return;
    this.evaluationSource = new EventSource(`${this.baseUrl}${OBSERVATORY_API_OPERATIONS.streamEvaluationEvents.pathTemplate}`, { withCredentials: true });
    this.evaluationSource.onopen = onOpen;
    this.evaluationSource.onmessage = onMessage;
    this.evaluationSource.addEventListener('evaluation.changed', onMessage);
    this.evaluationSource.onerror = onError;
  }

  reconnectEvaluationLive(onOpen: () => void, onMessage: ObservatoryLiveHandler, onError: () => void): void {
    // Preserve the native EventSource so the browser sends Last-Event-ID on reconnect.
    if (this.evaluationSource) return;
    this.connectEvaluationLive(onOpen, onMessage, onError);
  }

  disconnectEvaluationLive(): void { this.evaluationSource?.close(); this.evaluationSource = null; }

  connectOperationsLive(onOpen: () => void, onMessage: ObservatoryLiveHandler, onError: () => void): void {
    if (typeof EventSource === 'undefined' || this.operationsSource) return;
    this.operationsSource = new EventSource(`${this.baseUrl}${OBSERVATORY_API_OPERATIONS.streamOperationsEvents.pathTemplate}`, { withCredentials: true });
    this.operationsSource.onopen = onOpen;
    this.operationsSource.onmessage = onMessage;
    this.operationsSource.addEventListener('operations.changed', onMessage);
    this.operationsSource.onerror = onError;
  }

  reconnectOperationsLive(onOpen: () => void, onMessage: ObservatoryLiveHandler, onError: () => void): void {
    if (this.operationsSource) return;
    this.connectOperationsLive(onOpen, onMessage, onError);
  }

  disconnectOperationsLive(): void { this.operationsSource?.close(); this.operationsSource = null; }

  private async request<K extends ObservatoryOperationId>(operation: typeof OBSERVATORY_API_OPERATIONS[K], input: ObservatoryOperationInput<K>, signal?: AbortSignal): Promise<ObservatoryOperationResponse<K>> {
    const path = operation.pathTemplate.replace(/\{([^}]+)\}/g, (_, key: string) => encodeURIComponent(String(input.path[key as keyof typeof input.path])));
    const query = Object.entries(input.query).filter((entry): entry is [string, string | number] => entry[1] !== undefined).map(([key, value]) => `${encodeURIComponent(key)}=${encodeURIComponent(String(value))}`).join('&');
    const headers: Record<string, string> = { Accept: 'application/json', ...input.headers };
    if (operation.method !== 'GET') headers['X-Observatory-CSRF'] = this.sessionCsrf ?? this.csrf;
    const response = await fetch(`${this.baseUrl}${path}${query ? `?${query}` : ''}`, {
      method: operation.method,
      credentials: 'same-origin',
      cache: 'no-store',
      headers: { ...headers, ...(operation.requestMode === 'json' ? { 'Content-Type': 'application/json' } : {}) },
      body: operation.requestMode === 'json' ? JSON.stringify(input.body) : undefined,
      signal,
    });
    if (response.status === 401) { this.sessionCsrf = null; throw new ObservatorySessionRequiredError(); }
    if (!response.ok) {
      let category = '';
      try {
        const detail = await response.clone().json() as { readonly code?: unknown };
        if (typeof detail.code === 'string') category = detail.code;
      } catch { /* Keep transport errors bounded and content-free. */ }
      if (category === 'provider_unavailable' || category === 'local_judge_unavailable') throw new Error('local judge provider unavailable');
      throw new Error(`Observatory request failed (${response.status})`);
    }
    if (operation.responseMode === 'none') return undefined as ObservatoryOperationResponse<K>;
    // Evidence is deliberately streamed as a bounded Blob. Keep it out of
    // JSON parsing and let the inspector decide when (and how) to disclose it.
    if (operation.responseMode === 'blob') return (await response.blob()) as ObservatoryOperationResponse<K>;
    return (await response.json()) as ObservatoryOperationResponse<K>;
  }
}

export const blankInput = <K extends ObservatoryOperationId>(_operation: K): ObservatoryOperationInput<K> => ({ path: {}, query: {}, headers: {}, body: undefined } as unknown as ObservatoryOperationInput<K>);
