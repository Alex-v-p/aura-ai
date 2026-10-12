import { Injectable } from '@angular/core';
import { AURA_API_OPERATIONS, AuraApiClient, type ApprovalEvent, type AuraOperationDescriptor, type AuraOperationId, type AuraOperationInput, type AuraOperationResponse } from '@aura/aura-api-client';
import { approvalStreamBlockWithinBounds, approvalStreamBufferWithinBounds, isValidApprovalEvent, ApprovalApiError, type ApprovalDetail, type ApprovalPage, type ApprovalStream, type ApprovalStreamEvent, type ApprovalSummary, type ApprovalsApi, type ApprovalStatus } from './approval-models';
import { ApprovalSessionHydrator } from './approval-session-hydration';

type RequestInput = { path: Record<string, string>; query: Record<string, unknown>; headers: Record<string, string>; body: unknown };
const emptyInput = (): RequestInput => ({ path: {}, query: {}, headers: {}, body: undefined });
const key = (): string => typeof crypto !== 'undefined' && 'randomUUID' in crypto ? crypto.randomUUID() : `aura-${Date.now()}-${Math.random().toString(36).slice(2)}`;

@Injectable({ providedIn: 'root' })
export class AuraApprovalsApi implements ApprovalsApi {
  private readonly client = new AuraApiClient(new GeneratedTransport());
  private readonly session = new ApprovalSessionHydrator(() => this.client.execute('getSession', emptyInput() as never).then((value) => (value as { csrfToken: string }).csrfToken), () => new ApprovalApiError(401, 'Session changed.'));

  clearSession(): void { this.session.clearSession(); }
  async list(cursor?: string, status?: ApprovalStatus): Promise<ApprovalPage> { await this.ensureSession(); return this.client.execute('listApprovals' as AuraOperationId, { ...emptyInput(), query: { limit: 50, ...(cursor ? { cursor } : {}), ...(status ? { status } : {}) } } as never) as Promise<ApprovalPage>; }
  async get(id: string): Promise<ApprovalDetail> { await this.ensureSession(); return this.client.execute('getApproval' as AuraOperationId, { ...emptyInput(), path: { approval_id: id } } as never) as Promise<ApprovalDetail>; }
  async approve(id: string, expectedVersion: number): Promise<ApprovalSummary> { await this.ensureSession(); return this.client.execute('approveApproval' as AuraOperationId, { ...emptyInput(), path: { approval_id: id }, headers: this.mutationHeaders(), body: { expectedVersion } } as never) as Promise<ApprovalSummary>; }
  async reject(id: string, expectedVersion: number): Promise<ApprovalSummary> { await this.ensureSession(); return this.client.execute('rejectApproval' as AuraOperationId, { ...emptyInput(), path: { approval_id: id }, headers: this.mutationHeaders(), body: { expectedVersion } } as never) as Promise<ApprovalSummary>; }
  stream(lastEventId: string | undefined, onEvent: (event: ApprovalStreamEvent) => void, onError: (error: ApprovalApiError) => void): ApprovalStream { const controller = new AbortController(); void this.readStream(lastEventId, onEvent, onError, controller.signal); return { close: () => controller.abort() }; }

  private async readStream(lastEventId: string | undefined, onEvent: (event: ApprovalStreamEvent) => void, onError: (error: ApprovalApiError) => void, signal: AbortSignal): Promise<void> {
    try {
      await this.ensureSession();
      const response = await fetch(AURA_API_OPERATIONS.streamApprovals.pathTemplate, { credentials: 'include', headers: { Accept: 'text/event-stream', ...(lastEventId ? { 'Last-Event-ID': lastEventId } : {}) }, signal });
      if (!response.ok) { onError(new ApprovalApiError(response.status, response.status === 401 ? 'Your session has expired.' : 'The approval stream is temporarily unavailable.', response.status >= 500 || response.status === 410 || response.status === 429)); return; }
      if (!response.body) { onError(new ApprovalApiError(null, 'The approval stream returned no body.')); return; }
      const reader = response.body.getReader(); const decoder = new TextDecoder(); let buffer = '';
      while (!signal.aborted) {
        const chunk = await reader.read();
        if (signal.aborted) return;
        if (chunk.done) break;
        const decoded = decoder.decode(chunk.value, { stream: true });
        if (!approvalStreamBufferWithinBounds(buffer, decoded)) { onError(new ApprovalApiError(null, 'The approval stream exceeded its safety limit.', false)); return; }
        buffer += decoded;
        const blocks = buffer.split(/\r?\n\r?\n/); buffer = blocks.pop() ?? '';
        for (const block of blocks) { const result = this.parseEvent(block, onEvent); if (result === 'overflow') { onError(new ApprovalApiError(null, 'The approval event exceeded its safety limit.', false)); return; } }
      }
      if (!signal.aborted) onError(new ApprovalApiError(null, 'The approval stream closed.'));
    } catch (error: unknown) { if (!signal.aborted) onError(error instanceof ApprovalApiError ? error : new ApprovalApiError(null, 'Aura could not keep the approval stream open.')); }
  }

  private parseEvent(block: string, onEvent: (event: ApprovalStreamEvent) => void): 'ok' | 'ignored' | 'overflow' {
    if (!approvalStreamBlockWithinBounds(block)) return 'overflow';
    const fields = new Map<string, string>(); const data: string[] = [];
    for (const line of block.split(/\r?\n/)) { const separator = line.indexOf(':'); const field = separator < 0 ? line : line.slice(0, separator); const value = separator < 0 ? '' : line.slice(separator + 1).replace(/^ /, ''); if (field === 'data') data.push(value); else if (field === 'event' || field === 'id') fields.set(field, value); }
    if (!data.length || fields.get('event') === 'heartbeat') return 'ignored';
    const payloadText = data.join('\n');
    try { const payload = JSON.parse(payloadText) as ApprovalStreamEvent; if (isValidApprovalEvent(payload)) onEvent(payload as ApprovalEvent); } catch { /* Authoritative reconciliation handles malformed replay data. */ }
    return 'ok';
  }
  private async ensureSession(): Promise<void> { await this.session.ensure(); }
  private mutationHeaders(): { readonly 'X-CSRF-Token': string; readonly 'Idempotency-Key': string } { return { 'X-CSRF-Token': this.session.currentToken(), 'Idempotency-Key': key() }; }
}

class GeneratedTransport {
  async execute<K extends AuraOperationId>(descriptor: AuraOperationDescriptor, input: AuraOperationInput<K>): Promise<AuraOperationResponse<K>> {
    const request = input as unknown as RequestInput; let path = descriptor.pathTemplate; for (const [name, value] of Object.entries(request.path)) path = path.replace(`{${name}}`, encodeURIComponent(value));
    const query = new URLSearchParams(); for (const [name, value] of Object.entries(request.query)) if (value !== undefined && value !== null && value !== '') query.set(name, String(value));
    const response = await fetch(`${path}${query.size ? `?${query.toString()}` : ''}`, { method: descriptor.method, credentials: 'include', headers: { Accept: 'application/json', ...(request.body !== undefined ? { 'Content-Type': 'application/json' } : {}), ...request.headers }, body: request.body === undefined ? undefined : JSON.stringify(request.body) });
    if (!response.ok) { let detail = ''; try { const body = await response.json() as { detail?: string; message?: string }; detail = body.message ?? body.detail ?? ''; } catch { /* safe fallback */ } throw new ApprovalApiError(response.status, detail || 'The approval request could not be completed.', response.status >= 500 || response.status === 409 || response.status === 429); }
    return (response.status === 204 ? undefined : await response.json()) as AuraOperationResponse<K>;
  }
}
