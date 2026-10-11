import { Injectable } from '@angular/core';
import { AuraApiClient, type AuraOperationDescriptor, type AuraOperationId, type AuraOperationInput, type AuraOperationResponse } from '@aura/aura-api-client';
import type { CreateReminderRequest, Reminder, ReminderPage, ReminderTransitionRequest } from '@aura/aura-api-client';
import type { ReminderView, RemindersApi } from './reminder-models';
import { ReminderSessionHydrator } from './reminder-session-hydration';

type Input = { path: Record<string, string>; query: Record<string, unknown>; headers: Record<string, string>; body: unknown };

function emptyInput(): { path: Record<string, never>; query: Record<string, never>; headers: Record<string, never>; body: never } {
  return { path: {}, query: {}, headers: {}, body: undefined as never };
}

function idempotencyKey(): string {
  return typeof crypto !== 'undefined' && 'randomUUID' in crypto ? crypto.randomUUID() : `aura-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

export class ReminderApiError extends Error {
  constructor(readonly status: number, message: string) {
    super(message);
    this.name = 'ReminderApiError';
  }
}

@Injectable({ providedIn: 'root' })
export class AuraRemindersApi implements RemindersApi {
  private readonly client = new AuraApiClient(new GeneratedAuraTransport());
  private readonly session = new ReminderSessionHydrator(() => this.client.execute('getSession', emptyInput()).then((value) => value.csrfToken), () => new ReminderApiError(401, 'Session changed.'));

  clearSession(): void { this.session.clearSession(); }

  async list(view: ReminderView = 'upcoming', cursor?: string): Promise<ReminderPage> {
    await this.ensureSession();
    return this.client.execute('listReminders', { ...emptyInput(), query: { view, limit: 50, ...(cursor ? { cursor } : {}) } });
  }

  async get(id: string): Promise<Reminder> {
    await this.ensureSession();
    return this.client.execute('getReminder', { ...emptyInput(), path: { reminder_id: id } });
  }

  async create(content: string, dueAt: string, requestId = idempotencyKey()): Promise<Reminder> {
    await this.ensureSession();
    const body: CreateReminderRequest = { content, dueAt };
    return this.client.execute('createReminder', { ...emptyInput(), headers: this.mutationHeaders(requestId), body });
  }

  async complete(id: string, expectedVersion: number): Promise<Reminder> {
    await this.ensureSession();
    const body: ReminderTransitionRequest = { expectedVersion };
    return this.client.execute('completeReminder', { ...emptyInput(), path: { reminder_id: id }, headers: this.mutationHeaders(), body });
  }

  async cancel(id: string, expectedVersion: number): Promise<Reminder> {
    await this.ensureSession();
    const body: ReminderTransitionRequest = { expectedVersion };
    return this.client.execute('cancelReminder', { ...emptyInput(), path: { reminder_id: id }, headers: this.mutationHeaders(), body });
  }

  private async ensureSession(): Promise<void> {
    await this.session.ensure();
  }

  private mutationHeaders(requestId = idempotencyKey()): { readonly 'X-CSRF-Token': string; readonly 'Idempotency-Key': string } {
    return { 'X-CSRF-Token': this.session.currentToken(), 'Idempotency-Key': requestId };
  }
}

class GeneratedAuraTransport {
  async execute<K extends AuraOperationId>(descriptor: AuraOperationDescriptor, input: AuraOperationInput<K>): Promise<AuraOperationResponse<K>> {
    const request = input as unknown as Input;
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
      try { const body = await response.json() as { detail?: string; message?: string }; detail = body.message ?? body.detail ?? ''; } catch { /* use the status fallback */ }
      throw new ReminderApiError(response.status, detail || (response.status === 409 ? 'This reminder changed elsewhere. Refresh and try again.' : 'We could not complete that reminder request.'));
    }
    return response.status === 204 ? undefined as AuraOperationResponse<K> : await response.json() as AuraOperationResponse<K>;
  }
}
