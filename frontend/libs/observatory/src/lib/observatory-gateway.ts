import { Injectable, InjectionToken, inject } from '@angular/core';
import { ObservatoryApiClient, type ObservatoryOperationInput, type ObservatoryOperationResponse, type ObservatoryOperationId, OBSERVATORY_API_OPERATIONS } from '@aura/observatory-api-client';
import { extractSessionCsrf } from './observatory-session';

export const OBSERVATORY_API_BASE_URL = new InjectionToken<string>('OBSERVATORY_API_BASE_URL', { providedIn: 'root', factory: () => '' });
export const OBSERVATORY_CSRF_TOKEN = new InjectionToken<string>('OBSERVATORY_CSRF_TOKEN', { providedIn: 'root', factory: () => '' });
export type ObservatoryLiveHandler = (event: Event) => void;
export type ObservatoryLogoutResult = 'confirmed' | 'already_absent';

export interface ObservatoryGatewayPort {
  execute<K extends ObservatoryOperationId>(operation: K, input: ObservatoryOperationInput<K>): Promise<ObservatoryOperationResponse<K>>;
  ensureSession(): Promise<void>;
  logout(): Promise<ObservatoryLogoutResult>;
  connectLive(onOpen: () => void, onMessage: ObservatoryLiveHandler, onError: () => void): void;
  reconnectLive(onOpen: () => void, onMessage: ObservatoryLiveHandler, onError: () => void): void;
  disconnectLive(): void;
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
  private readonly client = new ObservatoryApiClient({ execute: (operation, input) => this.request(operation, input) });
  private sessionCsrf: string | null = null;
  private source: EventSource | null = null;

  execute<K extends ObservatoryOperationId>(operation: K, input: ObservatoryOperationInput<K>): Promise<ObservatoryOperationResponse<K>> {
    return this.client.execute(operation, input);
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
    this.disconnectLive();
    this.connectLive(onOpen, onMessage, onError);
  }

  disconnectLive(): void { this.source?.close(); this.source = null; }

  private async request<K extends ObservatoryOperationId>(operation: typeof OBSERVATORY_API_OPERATIONS[K], input: ObservatoryOperationInput<K>): Promise<ObservatoryOperationResponse<K>> {
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
    });
    if (response.status === 401) { this.sessionCsrf = null; throw new ObservatorySessionRequiredError(); }
    if (!response.ok) throw new Error(`Observatory request failed (${response.status})`);
    if (operation.responseMode === 'none') return undefined as ObservatoryOperationResponse<K>;
    return (await response.json()) as ObservatoryOperationResponse<K>;
  }
}

export const blankInput = <K extends ObservatoryOperationId>(_operation: K): ObservatoryOperationInput<K> => ({ path: {}, query: {}, headers: {}, body: undefined } as unknown as ObservatoryOperationInput<K>);
