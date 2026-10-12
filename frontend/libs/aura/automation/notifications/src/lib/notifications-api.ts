import { Injectable } from '@angular/core';
import { AuraApiClient, type AuraOperationDescriptor, type AuraOperationId, type AuraOperationInput, type AuraOperationResponse, type NotificationStatus, type NotificationStreamEvent } from '@aura/aura-api-client';
import { isValidNotificationEvent, NotificationApiError, type NotificationEvent, type NotificationPage, type NotificationRecord, type NotificationStream, type NotificationsApi } from './notification-models';
import { NotificationSessionHydrator } from './notification-session-hydration';

type RequestInput = { path: Record<string, string>; query: Record<string, unknown>; headers: Record<string, string>; body: unknown };

function emptyInput(): { path: Record<string, never>; query: Record<string, never>; headers: Record<string, never>; body: never } {
  return { path: {}, query: {}, headers: {}, body: undefined as never };
}

function idempotencyKey(): string {
  return typeof crypto !== 'undefined' && 'randomUUID' in crypto ? crypto.randomUUID() : `aura-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

@Injectable({ providedIn: 'root' })
export class AuraNotificationsApi implements NotificationsApi {
  private readonly client = new AuraApiClient(new GeneratedAuraTransport());
  private readonly session = new NotificationSessionHydrator(() => this.client.execute('getSession', emptyInput()).then((value) => value.csrfToken), () => new NotificationApiError(401, 'Session changed.'));

  clearSession(): void { this.session.clearSession(); }

  async list(cursor?: string, status?: NotificationStatus): Promise<NotificationPage> {
    await this.ensureSession();
    return this.client.execute('listNotifications', { ...emptyInput(), query: { limit: 50, ...(cursor ? { cursor } : {}), ...(status ? { status } : {}) } });
  }

  async get(id: string): Promise<NotificationRecord> {
    await this.ensureSession();
    return this.client.execute('getNotification', { ...emptyInput(), path: { notification_id: id } });
  }

  async acknowledge(id: string, expectedVersion: number): Promise<NotificationRecord> {
    await this.ensureSession();
    return this.client.execute('acknowledgeNotification', { ...emptyInput(), path: { notification_id: id }, headers: this.mutationHeaders(), body: { expectedVersion } });
  }

  stream(lastEventId: string | undefined, onEvent: (event: NotificationEvent) => void, onError: (error: NotificationApiError) => void): NotificationStream {
    const controller = new AbortController();
    void this.readStream(lastEventId, onEvent, onError, controller.signal);
    return { close: () => controller.abort() };
  }

  private async readStream(lastEventId: string | undefined, onEvent: (event: NotificationEvent) => void, onError: (error: NotificationApiError) => void, signal: AbortSignal): Promise<void> {
    try {
      await this.ensureSession();
      const response = await fetch('/api/v1/notifications/stream', {
        credentials: 'include',
        headers: { Accept: 'text/event-stream', ...(lastEventId ? { 'Last-Event-ID': lastEventId } : {}) },
        signal,
      });
      if (!response.ok) {
        onError(new NotificationApiError(response.status, response.status === 401 ? 'Your session has expired.' : 'The notification stream is temporarily unavailable.', response.status >= 500 || response.status === 410 || response.status === 429));
        return;
      }
      if (!response.body) {
        onError(new NotificationApiError(null, 'The notification stream returned no body.'));
        return;
      }
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      while (!signal.aborted) {
        const chunk = await reader.read();
        if (signal.aborted) return;
        if (chunk.done) break;
        buffer += decoder.decode(chunk.value, { stream: true });
        const blocks = buffer.split(/\r?\n\r?\n/);
        buffer = blocks.pop() ?? '';
        for (const block of blocks) this.parseEvent(block, onEvent);
      }
      if (!signal.aborted) onError(new NotificationApiError(null, 'The notification stream closed.'));
    } catch (error: unknown) {
      if (signal.aborted) return;
      onError(error instanceof NotificationApiError ? error : new NotificationApiError(null, 'Aura could not keep the notification stream open.'));
    }
  }

  private parseEvent(block: string, onEvent: (event: NotificationEvent) => void): void {
    const fields = new Map<string, string>();
    const data: string[] = [];
    for (const line of block.split(/\r?\n/)) {
      const separator = line.indexOf(':');
      const field = separator < 0 ? line : line.slice(0, separator);
      const value = separator < 0 ? '' : line.slice(separator + 1).replace(/^ /, '');
      if (field === 'data') data.push(value);
      else if (field === 'id' || field === 'event') fields.set(field, value);
    }
    if (!data.length || fields.get('event') === 'heartbeat') return;
    try {
      const payload = JSON.parse(data.join('\n')) as NotificationStreamEvent;
      if (!isValidNotificationEvent(payload)) return;
      onEvent(payload);
    } catch {
      // Durable reconciliation on reconnect is authoritative.
    }
  }

  private async ensureSession(): Promise<void> {
    await this.session.ensure();
  }

  private mutationHeaders(): { readonly 'X-CSRF-Token': string; readonly 'Idempotency-Key': string } {
    return { 'X-CSRF-Token': this.session.currentToken(), 'Idempotency-Key': idempotencyKey() };
  }
}

class GeneratedAuraTransport {
  async execute<K extends AuraOperationId>(descriptor: AuraOperationDescriptor, input: AuraOperationInput<K>): Promise<AuraOperationResponse<K>> {
    const request = input as unknown as RequestInput;
    let path = descriptor.pathTemplate;
    for (const [name, value] of Object.entries(request.path)) path = path.replace(`{${name}}`, encodeURIComponent(value));
    const query = new URLSearchParams();
    for (const [name, value] of Object.entries(request.query)) if (value !== undefined && value !== null && value !== '') query.set(name, String(value));
    const response = await fetch(`${path}${query.size ? `?${query.toString()}` : ''}`, {
      method: descriptor.method,
      credentials: 'include',
      headers: { Accept: 'application/json', ...(request.body !== undefined ? { 'Content-Type': 'application/json' } : {}), ...request.headers },
      body: request.body === undefined ? undefined : JSON.stringify(request.body),
    });
    if (!response.ok) {
      let detail = '';
      try { const body = await response.json() as { detail?: string; message?: string }; detail = body.message ?? body.detail ?? ''; } catch { /* use fallback */ }
      throw new NotificationApiError(response.status, detail || 'The notification request could not be completed.', response.status >= 500 || response.status === 409 || response.status === 429);
    }
    return response.status === 204 ? undefined as AuraOperationResponse<K> : await response.json() as AuraOperationResponse<K>;
  }
}
