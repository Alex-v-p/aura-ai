import { Injectable } from '@angular/core';
import {
  AURA_API_OPERATIONS,
  AuraApiClient,
  type AuraOperationInput,
  type AuraOperationId,
  type AuraOperationResponse,
  type AuraOperationDescriptor,
  type ConversationDetail,
  type ConversationPage,
  type ConversationRunAccepted,
  type ConversationSummary,
  type ModelCatalog,
  type Run,
  type RunEvent,
  type Session,
} from '@aura/aura-api-client';

export interface ConversationApiError {
  readonly message: string;
  readonly code?: string;
  readonly retryable: boolean;
  readonly status?: number;
  readonly traceId?: string;
}

export class ConversationRequestError extends Error implements ConversationApiError {
  readonly code?: string;
  readonly retryable: boolean;
  readonly status?: number;
  readonly traceId?: string;

  constructor(error: ConversationApiError) {
    super(error.message);
    this.name = 'ConversationRequestError';
    this.code = error.code;
    this.retryable = error.retryable;
    this.status = error.status;
    this.traceId = error.traceId;
  }
}

export interface RunEventSubscription {
  readonly close: () => void;
}

/**
 * A parsed SSE payload and its transport cursor.  The cursor comes from the
 * SSE `id` field, not from the JSON payload: liveness heartbeats deliberately
 * have no `id` and therefore must never advance reconnect state.
 */
export type RunEventHandler = (event: RunEvent, cursor?: string) => void;

export interface ConversationApi {
  getSession(): Promise<Session>;
  startLogin(returnTo?: string): void;
  logout(csrfToken: string): Promise<void>;
  listModels(): Promise<ModelCatalog>;
  listConversations(cursor?: string): Promise<ConversationPage>;
  getConversation(conversationId: string): Promise<ConversationDetail>;
  createConversation(message: string, modelId: string, idempotencyKey: string, agentRevisionId?: string, personaRevisionId?: string): Promise<ConversationRunAccepted>;
  updateConversation(conversationId: string, modelId: string, version: number, idempotencyKey: string, agentRevisionId?: string, transcriptSharingConfirmed?: boolean, personaRevisionId?: string, useAgentDefaultPersona?: boolean): Promise<ConversationSummary>;
  createRun(conversationId: string, message: string, version: number, idempotencyKey: string): Promise<ConversationRunAccepted>;
  cancelRun(runId: string, csrfToken: string, idempotencyKey: string): Promise<Run>;
  retryRun(runId: string, csrfToken: string, idempotencyKey: string): Promise<ConversationRunAccepted>;
  streamRunEvents(runId: string, lastEventId: string | undefined, onEvent: RunEventHandler, onError: (error: ConversationApiError) => void): RunEventSubscription;
}

type JsonResponse = Record<string, unknown> | ReadonlyArray<unknown> | null;

function emptyInput(): { path: Record<string, never>; query: Record<string, never>; headers: Record<string, never>; body: never } {
  return { path: {}, query: {}, headers: {}, body: undefined as never };
}

function randomKey(): string {
  if (typeof crypto !== 'undefined' && 'randomUUID' in crypto) return crypto.randomUUID();
  return `aura-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

/** Same-origin implementation for Aura Web. It deliberately keeps provider URLs out of the browser. */
@Injectable({ providedIn: 'root' })
export class AuraConversationApi implements ConversationApi {
  private readonly client = new AuraApiClient(new FetchAuraTransport());

  getSession(): Promise<Session> {
    return this.client.execute('getSession', emptyInput()).then((session) => { this.sessionToken = session.csrfToken; return session; });
  }

  startLogin(returnTo = typeof window === 'undefined' ? '/' : window.location.pathname + window.location.search): void {
    if (typeof window !== 'undefined') {
      const query = new URLSearchParams({ return_to: returnTo });
      window.location.assign(`${AURA_API_OPERATIONS.startLogin.pathTemplate}?${query.toString()}`);
    }
  }

  logout(csrfToken: string): Promise<void> {
    return this.client.execute('logout', { ...emptyInput(), headers: { 'X-CSRF-Token': csrfToken } });
  }

  listModels(): Promise<ModelCatalog> { return this.client.execute('listModels', emptyInput()); }

  listConversations(cursor?: string): Promise<ConversationPage> {
    return this.client.execute('listConversations', { ...emptyInput(), query: cursor ? { cursor } : {} });
  }

  getConversation(conversationId: string): Promise<ConversationDetail> {
    return this.client.execute('getConversation', { ...emptyInput(), path: { conversation_id: conversationId } });
  }

  createConversation(message: string, modelId: string, idempotencyKey = randomKey(), agentRevisionId?: string, personaRevisionId?: string): Promise<ConversationRunAccepted> {
    return this.client.execute('createConversation', { ...emptyInput(), headers: { 'X-CSRF-Token': this.csrfToken(), 'Idempotency-Key': idempotencyKey }, body: { message, modelId, ...(agentRevisionId ? { agentRevisionId } : {}), ...(personaRevisionId ? { personaRevisionId } : {}) } as never });
  }

  updateConversation(conversationId: string, modelId: string, version: number, idempotencyKey = randomKey(), agentRevisionId?: string, transcriptSharingConfirmed?: boolean, personaRevisionId?: string, useAgentDefaultPersona?: boolean): Promise<ConversationSummary> {
    return this.client.execute('updateConversation', { ...emptyInput(), path: { conversation_id: conversationId }, headers: { 'X-CSRF-Token': this.csrfToken(), 'Idempotency-Key': idempotencyKey }, body: { ...(modelId ? { modelId } : {}), version, ...(agentRevisionId ? { agentRevisionId } : {}), ...(personaRevisionId ? { personaRevisionId } : {}), ...(useAgentDefaultPersona ? { useAgentDefaultPersona: true } : {}), ...(transcriptSharingConfirmed ? { transcriptSharingConfirmed } : {}) } as never });
  }

  createRun(conversationId: string, message: string, version: number, idempotencyKey = randomKey()): Promise<ConversationRunAccepted> {
    return this.client.execute('createRun', { ...emptyInput(), path: { conversation_id: conversationId }, headers: { 'X-CSRF-Token': this.csrfToken(), 'Idempotency-Key': idempotencyKey }, body: { message, conversationVersion: version } });
  }

  cancelRun(runId: string, csrfToken = this.csrfToken(), idempotencyKey = randomKey()): Promise<Run> {
    return this.client.execute('cancelRun', { ...emptyInput(), path: { run_id: runId }, headers: { 'X-CSRF-Token': csrfToken, 'Idempotency-Key': idempotencyKey } });
  }

  retryRun(runId: string, csrfToken = this.csrfToken(), idempotencyKey = randomKey()): Promise<ConversationRunAccepted> {
    return this.client.execute('retryRun', { ...emptyInput(), path: { run_id: runId }, headers: { 'X-CSRF-Token': csrfToken, 'Idempotency-Key': idempotencyKey } });
  }

  streamRunEvents(runId: string, lastEventId: string | undefined, onEvent: RunEventHandler, onError: (error: ConversationApiError) => void): RunEventSubscription {
    const controller = new AbortController();
    const headers = lastEventId ? { 'Last-Event-ID': lastEventId } : undefined;
    // The generated client intentionally has no streaming implementation. Use the same-origin
    // adapter here so the client remains transport-only and the feature owns SSE reconciliation.
    void readEventStream(`/api/v1/runs/${encodeURIComponent(runId)}/events`, headers, controller.signal, onEvent, onError);
    return { close: () => controller.abort() };
  }

  private csrfToken(): string {
    return this.sessionToken;
  }

  private sessionToken = '';
}

class FetchAuraTransport {
  execute<K extends AuraOperationId>(descriptor: AuraOperationDescriptor, input: AuraOperationInput<K>): Promise<AuraOperationResponse<K>> {
    return this.request(descriptor, input as { path: Record<string, string>; query: Record<string, unknown>; headers: Record<string, string>; body: unknown }) as Promise<AuraOperationResponse<K>>;
  }

  private async request(descriptor: AuraOperationDescriptor, input: { path: Record<string, string>; query: Record<string, unknown>; headers: Record<string, string>; body: unknown }): Promise<unknown> {
    let path = descriptor.pathTemplate;
    for (const [name, value] of Object.entries(input.path)) path = path.replace(`{${name}}`, encodeURIComponent(value));
    const query = new URLSearchParams();
    for (const [name, value] of Object.entries(input.query)) if (value !== undefined) query.set(name, String(value));
    const url = `${path}${query.size ? `?${query.toString()}` : ''}`;
    const response = await fetch(url, {
      method: descriptor.method,
      credentials: 'include',
      headers: { Accept: descriptor.responseMode === 'event-stream' ? 'text/event-stream' : 'application/json', ...(input.body !== undefined ? { 'Content-Type': 'application/json' } : {}), ...input.headers },
      body: input.body === undefined ? undefined : JSON.stringify(input.body),
    });
    if (!response.ok) throw await toRequestError(response);
    if (descriptor.responseMode === 'event-stream' || response.status === 204) return undefined;
    return response.json() as Promise<JsonResponse>;
  }
}

async function toRequestError(response: Response): Promise<ConversationRequestError> {
  let payload: Partial<ConversationApiError> & { detail?: string } = {};
  try { payload = await response.json() as Partial<ConversationApiError> & { detail?: string }; } catch { /* the status is enough for a safe message */ }
  return new ConversationRequestError({
    message: payload.message ?? payload.detail ?? (response.status === 401 ? 'Please sign in to continue.' : 'We could not complete that request.'),
    code: payload.code,
    retryable: payload.retryable ?? response.status >= 500,
    status: response.status,
    traceId: payload.traceId,
  });
}

function toApiError(error: unknown): ConversationApiError {
  if (error instanceof ConversationRequestError) return error;
  return { message: 'We could not connect to Aura. Your draft is still here.', retryable: true };
}

async function readEventStream(url: string, headers: Record<string, string> | undefined, signal: AbortSignal, onEvent: RunEventHandler, onError: (error: ConversationApiError) => void): Promise<void> {
  try {
    const response = await fetch(url, { credentials: 'include', headers: { Accept: 'text/event-stream', ...(headers ?? {}) }, signal });
    if (!response.ok) throw await toRequestError(response);
    if (!response.body) throw new ConversationRequestError({ message: 'The live response connection is unavailable.', retryable: true });
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let data = '';
    let cursor: string | undefined;
    const flush = (): void => {
      if (!data) return;
      try {
        const event = JSON.parse(data) as RunEvent;
        // The SSE id is the only authoritative reconnect cursor.  Heartbeats
        // are intentionally emitted without an id by Core and must not inherit
        // or manufacture one from their JSON eventId field.
        onEvent(event, event.eventType === 'heartbeat' ? undefined : cursor);
      } catch { onError({ message: 'Aura sent an unreadable live update.', retryable: true }); }
      data = '';
      cursor = undefined;
    };
    while (!signal.aborted) {
      const chunk = await reader.read();
      if (chunk.done) { buffer += decoder.decode(); if (buffer.trim()) buffer += '\n\n'; }
      else buffer += decoder.decode(chunk.value, { stream: true });
      const lines = buffer.split(/\r?\n/);
      buffer = lines.pop() ?? '';
      for (const line of lines) {
        if (!line) { flush(); continue; }
        if (line.startsWith('id:')) {
          const value = line.slice(3).trim();
          cursor = value || undefined;
        }
        else if (line.startsWith('event:')) continue;
        else if (line.startsWith('data:')) data += line.slice(5).trim();
      }
      if (chunk.done) { flush(); break; }
    }
  } catch (error: unknown) {
    if (!signal.aborted) onError(toApiError(error));
  }
}
