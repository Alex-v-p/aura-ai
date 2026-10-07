import { Injectable } from '@angular/core';
import { AuraApiClient, type AuraOperationDescriptor, type AuraOperationId, type AuraOperationInput, type AuraOperationResponse, type AgentDetail as TransportAgentDetail, type AgentRevision as TransportAgentRevision } from '@aura/aura-api-client';
import { AgentApi, AgentDraft, AgentProfile, AgentRevision } from './agent-models';

@Injectable()
export class AuraAgentApi implements AgentApi {
  private readonly client = new AuraApiClient(new GeneratedAuraTransport());
  private csrf = '';

  async listAgents(): Promise<ReadonlyArray<AgentProfile>> { await this.ensureSession(); const result = await this.client.execute('listAgents', emptyInput()); return result.items.map((item) => this.mapDetail({ ...item, revisions: [item.currentRevision] })); }
  async getAgent(profileId: string): Promise<AgentProfile> { await this.ensureSession(); return this.mapDetail(await this.client.execute('getAgent', { ...emptyInput(), path: { agent_profile_id: profileId } })); }
  async createAgent(draft: AgentDraft): Promise<AgentProfile> { await this.ensureSession(); const result = await this.client.execute('createAgent', { ...emptyInput(), headers: this.mutationHeaders(), body: { displayName: draft.displayName, purpose: draft.purpose, instructions: draft.behavioralInstructions, personaRevisionId: draft.personaRevisionId } }); return this.mapDetail(result); }
  async createRevision(profileId: string, draft: AgentDraft, expectedVersion: number): Promise<AgentProfile> { await this.ensureSession(); const result = await this.client.execute('createAgentRevision', { ...emptyInput(), path: { agent_profile_id: profileId }, headers: this.mutationHeaders(), body: { displayName: draft.displayName, purpose: draft.purpose, instructions: draft.behavioralInstructions, personaRevisionId: draft.personaRevisionId, expectedVersion } }); return this.mapDetail(result); }
  async setStatus(profileId: string, status: 'active' | 'disabled', expectedVersion: number): Promise<AgentProfile> { await this.ensureSession(); const result = await this.client.execute('updateAgentStatus', { ...emptyInput(), path: { agent_profile_id: profileId }, headers: this.mutationHeaders(), body: { status, expectedVersion } }); return this.mapDetail(result); }

  private async ensureSession(): Promise<void> { if (this.csrf) return; const session = await this.client.execute('getSession', emptyInput()); this.csrf = session.csrfToken; }
  private mutationHeaders(): { readonly 'X-CSRF-Token': string; readonly 'Idempotency-Key': string } { return { 'X-CSRF-Token': this.csrf, 'Idempotency-Key': randomKey() }; }
  private mapDetail(detail: TransportAgentDetail): AgentProfile { return { id: detail.id, displayName: detail.currentRevision.displayName, status: detail.status, version: detail.version, currentRevisionId: detail.currentRevision.id, revisions: detail.revisions.map((revision) => this.mapRevision(revision, detail.status)) }; }
  private mapRevision(revision: TransportAgentRevision, status: 'active' | 'disabled'): AgentRevision { return { profileId: revision.profileId, revisionId: revision.id, revision: revision.revision, displayName: revision.displayName, status, purpose: revision.purpose, behavioralInstructions: revision.instructions, personaRevisionId: revision.personaRevisionId, promptBundleRevisionId: revision.promptBundleRevisionId, modelPolicyRevisionId: revision.modelPolicyRevisionId, createdAt: revision.createdAt }; }
}

function emptyInput(): { path: Record<string, never>; query: Record<string, never>; headers: Record<string, never>; body: never } { return { path: {}, query: {}, headers: {}, body: undefined as never }; }
function randomKey(): string { return typeof crypto !== 'undefined' && 'randomUUID' in crypto ? crypto.randomUUID() : `aura-${Date.now()}-${Math.random().toString(36).slice(2)}`; }

class GeneratedAuraTransport {
  async execute<K extends AuraOperationId>(descriptor: AuraOperationDescriptor, input: AuraOperationInput<K>): Promise<AuraOperationResponse<K>> {
    let path = descriptor.pathTemplate;
    for (const [name, value] of Object.entries(input.path)) path = path.replace(`{${name}}`, encodeURIComponent(value as string));
    const response = await fetch(path, { method: descriptor.method, credentials: 'include', headers: { Accept: 'application/json', ...(input.body !== undefined ? { 'Content-Type': 'application/json' } : {}), ...input.headers }, body: input.body === undefined ? undefined : JSON.stringify(input.body) });
    if (!response.ok) throw new Error(response.status === 401 ? 'Sign in to manage agents.' : 'We could not complete that configuration change.');
    return response.status === 204 ? undefined as AuraOperationResponse<K> : await response.json() as AuraOperationResponse<K>;
  }
}
