import { Injectable } from '@angular/core';
import { AuraApiClient, type AuraOperationDescriptor, type AuraOperationId, type AuraOperationInput, type AuraOperationResponse } from '@aura/aura-api-client';
import { type BriefingCapabilities, type BriefingDeletionReceipt, type BriefingOccurrence, type BriefingResult, type BriefingSchedule, type BriefingApi as BriefingApiContract } from './briefing-models';
import type { CreateBriefingScheduleRequest } from '@aura/aura-api-client';

type EmptyInput = { readonly path: Record<string, never>; readonly query: Record<string, never>; readonly headers: Record<string, never>; readonly body: never };
const emptyInput = (): EmptyInput => ({ path: {}, query: {}, headers: {}, body: undefined as never });
const requestId = (): string => {
  if (typeof crypto === 'undefined' || typeof crypto.randomUUID !== 'function') throw new BriefingApiError(0, 'Secure mutation identity is unavailable. Retry when secure randomness is available.');
  return crypto.randomUUID();
};

@Injectable({ providedIn: 'root' })
export class AuraBriefingApi implements BriefingApiContract {
  private readonly client = new AuraApiClient(new GeneratedBriefingTransport());
  private csrf = '';
  private sessionEpoch = 0;
  clearSession(): void { ++this.sessionEpoch; this.csrf = ''; }
  async capabilities(): Promise<BriefingCapabilities> { return this.client.execute('getBriefingCapabilities', emptyInput()); }
  async list(status?: 'active' | 'paused' | 'deleted') { return this.client.execute('listBriefingSchedules', { ...emptyInput(), query: { limit: 50, ...(status ? { status } : {}) } }); }
  async get(id: string): Promise<BriefingSchedule> { return this.client.execute('getBriefingSchedule', { ...emptyInput(), path: { schedule_id: id } }); }
  async create(request: CreateBriefingScheduleRequest, idempotencyKey?: string): Promise<BriefingSchedule> { await this.ensureSession(); return this.client.execute('createBriefingSchedule', { ...emptyInput(), headers: this.mutationHeaders(idempotencyKey), body: request }); }
  async pause(id: string, expectedVersion: number, idempotencyKey?: string): Promise<BriefingSchedule> { return this.transition('pauseBriefingSchedule', id, expectedVersion, idempotencyKey); }
  async resume(id: string, expectedVersion: number, idempotencyKey?: string): Promise<BriefingSchedule> { return this.transition('resumeBriefingSchedule', id, expectedVersion, idempotencyKey); }
  async deleteSchedule(id: string, expectedVersion: number, idempotencyKey?: string): Promise<BriefingDeletionReceipt> { await this.ensureSession(); return this.client.execute('deleteBriefingSchedule', { ...emptyInput(), path: { schedule_id: id }, headers: this.mutationHeaders(idempotencyKey), body: { scope: 'schedule', expectedVersion } }); }
  async occurrences(id: string) { return this.client.execute('listBriefingOccurrences', { ...emptyInput(), path: { schedule_id: id }, query: { limit: 100 } }); }
  async occurrence(scheduleId: string, occurrenceId: string): Promise<BriefingOccurrence> { return this.client.execute('getBriefingOccurrence', { ...emptyInput(), path: { schedule_id: scheduleId, occurrence_id: occurrenceId } }); }
  async deleteHistory(scheduleId: string, expectedScheduleVersion: number, idempotencyKey?: string): Promise<BriefingDeletionReceipt> { await this.ensureSession(); return this.client.execute('deleteBriefingOccurrenceHistory', { ...emptyInput(), path: { schedule_id: scheduleId }, headers: this.mutationHeaders(idempotencyKey), body: { scope: 'occurrence_history', expectedScheduleVersion } }); }
  async result(id: string): Promise<BriefingResult> { return this.client.execute('getBriefingResult', { ...emptyInput(), path: { result_id: id } }); }
  async deleteResult(id: string, expectedVersion: number, idempotencyKey?: string): Promise<BriefingDeletionReceipt> { await this.ensureSession(); return this.client.execute('deleteBriefingResult', { ...emptyInput(), path: { result_id: id }, headers: this.mutationHeaders(idempotencyKey), body: { scope: 'result_content', expectedVersion } }); }
  private async transition(operation: 'pauseBriefingSchedule' | 'resumeBriefingSchedule', id: string, expectedVersion: number, idempotencyKey?: string): Promise<BriefingSchedule> { await this.ensureSession(); return this.client.execute(operation, { ...emptyInput(), path: { schedule_id: id }, headers: this.mutationHeaders(idempotencyKey), body: { expectedVersion } }); }
  private async ensureSession(): Promise<void> { if (this.csrf) return; const epoch = this.sessionEpoch; const session = await this.client.execute('getSession', emptyInput()); if (epoch !== this.sessionEpoch) throw new BriefingApiError(401, 'Your session changed. Sign in again.'); this.csrf = session.csrfToken; }
  private mutationHeaders(key?: string): { readonly 'X-CSRF-Token': string; readonly 'Idempotency-Key': string } { return { 'X-CSRF-Token': this.csrf, 'Idempotency-Key': key ?? requestId() }; }
}

export class BriefingApiError extends Error { constructor(readonly status: number, message: string) { super(message); this.name = 'BriefingApiError'; } }

class GeneratedBriefingTransport {
  async execute<K extends AuraOperationId>(descriptor: AuraOperationDescriptor, input: AuraOperationInput<K>): Promise<AuraOperationResponse<K>> {
    let path = descriptor.pathTemplate;
    for (const [name, value] of Object.entries(input.path)) path = path.replace(`{${name}}`, encodeURIComponent(value as string));
    const query = new URLSearchParams();
    for (const [name, value] of Object.entries(input.query)) if (value !== undefined && value !== null && value !== '') query.set(name, String(value));
    let response: Response;
    try { response = await fetch(`${path}${query.size ? `?${query.toString()}` : ''}`, { method: descriptor.method, credentials: 'include', cache: 'no-store', headers: { Accept: 'application/json', ...(input.body !== undefined ? { 'Content-Type': 'application/json' } : {}), ...input.headers }, body: input.body === undefined ? undefined : JSON.stringify(input.body) }); }
    catch { throw new BriefingApiError(0, 'Briefings are unavailable while you are offline.'); }
    if (!response.ok) { let message = 'Aura could not complete that briefing action.'; try { const body = await response.json() as { readonly detail?: unknown; readonly message?: unknown }; if (typeof body.message === 'string') message = body.message; else if (typeof body.detail === 'string') message = body.detail; } catch { /* public fallback */ } throw new BriefingApiError(response.status, message); }
    return response.status === 204 ? undefined as AuraApiResponse<K> : await response.json() as AuraApiResponse<K>;
  }
}
type AuraApiResponse<K extends AuraOperationId> = AuraOperationResponse<K>;
