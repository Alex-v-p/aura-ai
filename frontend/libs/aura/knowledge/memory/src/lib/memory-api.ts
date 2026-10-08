import { Injectable } from '@angular/core';
import { AuraApiClient, type AuraOperationDescriptor, type AuraOperationId, type AuraOperationInput, type AuraOperationResponse, type MemoryScope, type SearchMemoriesRequest } from '@aura/aura-api-client';
import type { MemoryApi, MemoryFilters } from './memory-models';
import type { MemoryCandidateEdit } from '@aura/aura-api-client';

type Input = { path: Record<string, string>; query: Record<string, unknown>; headers: Record<string, string>; body: unknown };
function emptyInput(): { path: Record<string, never>; query: Record<string, never>; headers: Record<string, never>; body: never } { return { path: {}, query: {}, headers: {}, body: undefined as never }; }
function key(): string { return typeof crypto !== 'undefined' && 'randomUUID' in crypto ? crypto.randomUUID() : `aura-${Date.now()}-${Math.random().toString(36).slice(2)}`; }

@Injectable({ providedIn: 'root' })
export class AuraMemoryApi implements MemoryApi {
  private readonly client = new AuraApiClient(new GeneratedAuraTransport());
  private csrf = '';

  async listMemories(cursor?: string, filters: MemoryFilters = {}) {
    await this.ensureSession();
    const { q, ...queryFilters } = filters;
    if (q?.trim()) return this.client.execute('searchMemories', { ...emptyInput(), headers: { 'X-CSRF-Token': this.csrf }, body: { query: q.trim().slice(0, 200), cursor, limit: 30, ...queryFilters } satisfies SearchMemoriesRequest });
    return this.client.execute('listMemories', { ...emptyInput(), query: { ...(cursor ? { cursor } : {}), limit: 30, ...queryFilters } });
  }
  async getMemory(id: string, scope?: MemoryScope) { await this.ensureSession(); return this.client.execute('getMemory', { ...emptyInput(), path: { memory_id: id }, query: scopeQuery(scope) }); }
  async createMemory(body: Parameters<MemoryApi['createMemory']>[0]) { await this.ensureSession(); return this.client.execute('createMemory', { ...emptyInput(), headers: this.headers(), body }); }
  async correctMemory(id: string, scope: MemoryScope, body: Parameters<MemoryApi['correctMemory']>[2]) { await this.ensureSession(); return this.client.execute('correctMemory', { ...emptyInput(), path: { memory_id: id }, query: scopeQuery(scope), headers: this.headers(), body }); }
  async updateStatus(id: string, status: Parameters<MemoryApi['updateStatus']>[1], expectedVersion: number, relatedMemoryId: string | null = null, scope?: MemoryScope) { await this.ensureSession(); return this.client.execute('updateMemoryStatus', { ...emptyInput(), path: { memory_id: id }, query: scopeQuery(scope), headers: this.headers(), body: { status, expectedVersion, relatedMemoryId } }); }
  async updatePin(id: string, pinned: boolean, expectedVersion: number, scope?: MemoryScope) { await this.ensureSession(); return this.client.execute('updateMemoryPin', { ...emptyInput(), path: { memory_id: id }, query: scopeQuery(scope), headers: this.headers(), body: { pinned, expectedVersion } }); }
  async purge(id: string, scope: MemoryScope, confirmation: 'PURGE MEMORY', expectedVersion: number) { await this.ensureSession(); return this.client.execute('purgeMemory', { ...emptyInput(), path: { memory_id: id }, query: scopeQuery(scope), headers: this.headers(), body: { confirmation, expectedVersion } }); }
  async listCandidates(cursor?: string, state?: Parameters<MemoryApi['listCandidates']>[1]) { await this.ensureSession(); return this.client.execute('listMemoryCandidates', { ...emptyInput(), query: { limit: 30, ...(cursor ? { cursor } : {}), ...(state ? { state } : {}) } }); }
  async getCandidate(id: string) { await this.ensureSession(); return this.client.execute('getMemoryCandidate', { ...emptyInput(), path: { candidate_id: id } }); }
  async approveCandidate(id: string, expectedVersion: number, edit?: MemoryCandidateEdit) { await this.ensureSession(); return this.client.execute('approveMemoryCandidate', { ...emptyInput(), path: { candidate_id: id }, headers: this.headers(), body: { expectedVersion, ...(edit ? { edit } : {}) } }); }
  async rejectCandidate(id: string, expectedVersion: number, reason: string) { await this.ensureSession(); return this.client.execute('rejectMemoryCandidate', { ...emptyInput(), path: { candidate_id: id }, headers: this.headers(), body: { expectedVersion, reason } }); }
  async getModelInventory() { await this.ensureSession(); return this.client.execute('getMemoryModelInventory', emptyInput()); }
  async getModelConfiguration() { await this.ensureSession(); return this.client.execute('getMemoryModelConfiguration', emptyInput()); }
  async updateModelConfiguration(extractionModelId: string, embeddingModelId: string, expectedVersion: number) { await this.ensureSession(); return this.client.execute('updateMemoryModelConfiguration', { ...emptyInput(), headers: this.headers(), body: { extractionModelId, embeddingModelId, expectedVersion } }); }
  async getReindexStatus() { await this.ensureSession(); return this.client.execute('getMemoryReindexStatus', emptyInput()); }
  async resumeReindex(generationId: string) { await this.ensureSession(); return this.client.execute('resumeMemoryReindex', { ...emptyInput(), headers: this.headers(), body: { generationId } }); }
  async listAgentPolicies(agentProfileId: string) { await this.ensureSession(); return this.client.execute('listAgentMemoryPolicies', { ...emptyInput(), path: { agent_profile_id: agentProfileId } }); }
  async createAgentPolicy(agentProfileId: string, body: Parameters<MemoryApi['createAgentPolicy']>[1]) { await this.ensureSession(); return this.client.execute('createAgentMemoryPolicy', { ...emptyInput(), path: { agent_profile_id: agentProfileId }, headers: this.headers(), body }); }
  async attachAgentPolicy(agentProfileId: string, policyRevisionId: string, expectedAgentVersion: number) { await this.ensureSession(); return this.client.execute('attachAgentMemoryPolicy', { ...emptyInput(), path: { agent_profile_id: agentProfileId, policy_revision_id: policyRevisionId }, headers: this.headers(), body: { expectedAgentVersion } }); }

  private async ensureSession(): Promise<void> { if (this.csrf) return; const session = await this.client.execute('getSession', emptyInput()); this.csrf = session.csrfToken; }
  private headers(): { readonly 'X-CSRF-Token': string; readonly 'Idempotency-Key': string } { return { 'X-CSRF-Token': this.csrf, 'Idempotency-Key': key() }; }
}

function scopeQuery(scope?: MemoryScope): Record<string, string> { return scope?.type === 'agent' ? { scopeType: scope.type, agentProfileId: scope.agentProfileId } : scope ? { scopeType: scope.type } : {}; }

class GeneratedAuraTransport {
  async execute<K extends AuraOperationId>(descriptor: AuraOperationDescriptor, input: AuraOperationInput<K>): Promise<AuraOperationResponse<K>> {
    const request = input as unknown as Input;
    let path = descriptor.pathTemplate;
    for (const [name, value] of Object.entries(request.path)) path = path.replace(`{${name}}`, encodeURIComponent(value));
    const query = new URLSearchParams();
    for (const [name, value] of Object.entries(request.query)) if (value !== undefined && value !== null && value !== '') query.set(name, String(value));
    const response = await fetch(`${path}${query.size ? `?${query.toString()}` : ''}`, { method: descriptor.method, credentials: 'include', headers: { Accept: 'application/json', ...(request.body !== undefined ? { 'Content-Type': 'application/json' } : {}), ...request.headers }, body: request.body === undefined ? undefined : JSON.stringify(request.body) });
    if (!response.ok) { let detail = ''; try { const body = await response.json() as { detail?: string; message?: string }; detail = body.message ?? body.detail ?? ''; } catch { /* safe fallback */ } throw new Error(detail || (response.status === 401 ? 'Sign in to manage memory.' : 'We could not complete that memory request.')); }
    return response.status === 204 ? undefined as AuraOperationResponse<K> : await response.json() as AuraOperationResponse<K>;
  }
}
