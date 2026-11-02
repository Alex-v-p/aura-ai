import { Injectable } from '@angular/core';
import { AuraApiClient, type AuraOperationDescriptor, type AuraOperationId, type AuraOperationInput, type AuraOperationResponse } from '@aura/aura-api-client';
import { CalendarSessionHydrator } from './calendar-session-hydration';
import { CalendarClientError, type CalendarApi, type CalendarConflictQueryInput, type CalendarEventQueryInput } from './calendar-models';

type RequestInput = { readonly path: Record<string, string>; readonly query: Record<string, unknown>; readonly headers: Record<string, string>; readonly body: unknown };
type EmptyInput = { readonly path: Record<string, never>; readonly query: Record<string, never>; readonly headers: Record<string, never>; readonly body: never };

const emptyInput = (): EmptyInput => ({ path: {}, query: {}, headers: {}, body: undefined as never });
const requestId = (): string => {
  if (typeof crypto === 'undefined' || typeof crypto.randomUUID !== 'function') {
    throw new CalendarClientError('Secure mutation identity is unavailable. Retry when secure randomness is available.', { retryable: true, recovery: 'retry' });
  }
  return crypto.randomUUID();
};

@Injectable({ providedIn: 'root' })
export class AuraCalendarApi implements CalendarApi {
  private readonly client = new AuraApiClient(new GeneratedAuraTransport());
  private readonly session = new CalendarSessionHydrator(
    () => this.client.execute('getSession', emptyInput()).then((value) => value.csrfToken),
    () => new CalendarClientError('Your session changed. Sign in again.', { status: 401, recovery: 'connect' }),
  );

  clearSession(): void { this.session.clearSession(); }

  async getConnection() { return this.client.execute('getCalendarConnection', emptyInput()); }
  async startConnection() { await this.ensureSession(); return this.client.execute('startCalendarConnection', { ...emptyInput(), headers: this.mutationHeaders() }); }
  async completeConnectionCallback() { await this.ensureSession(); return this.client.execute('completeCalendarConnectionCallback', { ...emptyInput(), headers: this.mutationHeaders() }); }
  async reauthorizeConnection() { await this.ensureSession(); return this.client.execute('reauthorizeCalendarConnection', { ...emptyInput(), headers: this.mutationHeaders() }); }
  async disconnectConnection() { await this.ensureSession(); return this.client.execute('disconnectCalendarConnection', { ...emptyInput(), headers: this.mutationHeaders() }); }
  async listCalendars(cursor?: string, limit = 100) { return this.client.execute('listSelectableCalendars', { ...emptyInput(), query: { limit, ...(cursor ? { cursor } : {}) } }); }
  async replaceSelection(calendarIds: ReadonlyArray<string>, expectedVersion: number) { await this.ensureSession(); return this.client.execute('replaceCalendarSelection', { ...emptyInput(), headers: this.mutationHeaders(), body: { calendarIds, expectedVersion } }); }
  async queryEvents(input: CalendarEventQueryInput) { return this.client.execute('queryCalendarEvents', { ...emptyInput(), body: input }); }
  async queryConflicts(input: CalendarConflictQueryInput) { return this.client.execute('queryCalendarConflicts', { ...emptyInput(), body: input }); }

  private async ensureSession(): Promise<void> { await this.session.ensure(); }
  private mutationHeaders(): { readonly 'X-CSRF-Token': string; readonly 'Idempotency-Key': string } { return { 'X-CSRF-Token': this.session.currentToken(), 'Idempotency-Key': requestId() }; }
}

class GeneratedAuraTransport {
  async execute<K extends AuraOperationId>(descriptor: AuraOperationDescriptor, input: AuraOperationInput<K>): Promise<AuraOperationResponse<K>> {
    const request = input as unknown as RequestInput;
    let path = descriptor.pathTemplate;
    for (const [name, value] of Object.entries(request.path)) path = path.replace(`{${name}}`, encodeURIComponent(value));
    const query = new URLSearchParams();
    for (const [name, value] of Object.entries(request.query)) if (value !== undefined && value !== null && value !== '') query.set(name, String(value));
    let response: Response;
    try {
      response = await fetch(`${path}${query.size ? `?${query.toString()}` : ''}`, { method: descriptor.method, credentials: 'include', headers: { Accept: 'application/json', ...(request.body !== undefined ? { 'Content-Type': 'application/json' } : {}), ...request.headers }, body: request.body === undefined ? undefined : JSON.stringify(request.body) });
    } catch {
      throw new CalendarClientError('Calendar is unavailable right now.', { retryable: true, recovery: 'retry' });
    }
    if (!response.ok) {
      let payload: { readonly category?: unknown; readonly detail?: unknown; readonly message?: unknown; readonly retryable?: unknown; readonly recoveryAction?: unknown } = {};
      try { payload = await response.json() as typeof payload; } catch { /* use status fallback */ }
      const category = typeof payload.category === 'string' ? payload.category as CalendarClientError['category'] : null;
      const message = typeof payload.message === 'string' ? payload.message : typeof payload.detail === 'string' ? payload.detail : response.status === 401 ? 'Your session has expired. Sign in again.' : 'Calendar is temporarily unavailable.';
      const recovery = typeof payload.recoveryAction === 'string' && ['none', 'retry', 'reauthorize', 'connect', 'complete_connection', 'disconnect', 'wait', 'contact_administrator'].includes(payload.recoveryAction) ? payload.recoveryAction as CalendarClientError['recovery'] : response.status === 401 ? 'connect' : response.status === 403 ? 'contact_administrator' : response.status === 409 ? 'retry' : 'retry';
      throw new CalendarClientError(message, { status: response.status, category, retryable: payload.retryable === true || response.status >= 500 || response.status === 409 || response.status === 429, recovery });
    }
    return response.status === 204 ? undefined as AuraOperationResponse<K> : await response.json() as AuraOperationResponse<K>;
  }
}
