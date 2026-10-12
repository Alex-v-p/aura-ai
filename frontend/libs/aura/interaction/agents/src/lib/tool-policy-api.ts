import { Injectable } from '@angular/core';
import { AuraApiClient, type AuraOperationDescriptor, type AuraOperationId, type AuraOperationInput, type AuraOperationResponse, type ToolGrant } from '@aura/aura-api-client';
import type { ToolPolicyApi } from './tool-policy-models';

@Injectable({ providedIn: 'root' })
export class AuraToolPolicyApi implements ToolPolicyApi {
  private readonly client = new AuraApiClient(new GeneratedAuraTransport());
  private csrf = '';
  private sessionEpoch = 0;
  clearSession(): void { this.sessionEpoch += 1; this.csrf = ''; }

  async listCatalog() { await this.ensureSession(); return this.client.execute('listTools', emptyInput()); }
  async listPolicies(agentProfileId: string) { await this.ensureSession(); return this.client.execute('listAgentToolPolicies', { ...emptyInput(), path: { agent_profile_id: agentProfileId } }); }
  async createPolicy(agentProfileId: string, grants: ReadonlyArray<ToolGrant>, expectedRevision: number) { await this.ensureSession(); return this.client.execute('createAgentToolPolicy', { ...emptyInput(), path: { agent_profile_id: agentProfileId }, headers: this.headers(), body: { grants, expectedRevision } }); }
  async attachPolicy(agentProfileId: string, policyRevisionId: string, expectedAgentVersion: number) { await this.ensureSession(); await this.client.execute('attachAgentToolPolicy', { ...emptyInput(), path: { agent_profile_id: agentProfileId, tool_policy_revision_id: policyRevisionId }, headers: this.headers(), body: { expectedAgentVersion } }); }

  private async ensureSession(): Promise<void> {
    if (this.csrf) return;
    const epoch = this.sessionEpoch;
    const session = await this.client.execute('getSession', emptyInput());
    if (epoch !== this.sessionEpoch) throw new SessionHydrationInvalidatedError();
    this.csrf = session.csrfToken;
  }
  private headers(): { readonly 'X-CSRF-Token': string; readonly 'Idempotency-Key': string } { return { 'X-CSRF-Token': this.csrf, 'Idempotency-Key': randomKey() }; }
}

class SessionHydrationInvalidatedError extends Error {
  constructor() { super('Session hydration was invalidated.'); this.name = 'SessionHydrationInvalidatedError'; }
}

function emptyInput(): { path: Record<string, never>; query: Record<string, never>; headers: Record<string, never>; body: never } { return { path: {}, query: {}, headers: {}, body: undefined as never }; }
function randomKey(): string { return typeof crypto !== 'undefined' && 'randomUUID' in crypto ? crypto.randomUUID() : `aura-${Date.now()}-${Math.random().toString(36).slice(2)}`; }

class GeneratedAuraTransport {
  async execute<K extends AuraOperationId>(descriptor: AuraOperationDescriptor, input: AuraOperationInput<K>): Promise<AuraOperationResponse<K>> {
    const request = input as unknown as { path: Record<string, string>; query: Record<string, unknown>; headers: Record<string, string>; body: unknown };
    let path = descriptor.pathTemplate;
    for (const [name, value] of Object.entries(request.path)) path = path.replace(`{${name}}`, encodeURIComponent(value));
    const response = await fetch(path, { method: descriptor.method, credentials: 'include', headers: { Accept: 'application/json', ...(request.body !== undefined ? { 'Content-Type': 'application/json' } : {}), ...request.headers }, body: request.body === undefined ? undefined : JSON.stringify(request.body) });
    if (!response.ok) {
      let detail = '';
      try { const body = await response.json() as { readonly message?: string; readonly detail?: string }; detail = body.message ?? body.detail ?? ''; } catch { /* status is enough for a safe fallback */ }
      throw new ToolPolicyApiError(response.status, detail || (response.status === 401 ? 'Sign in to manage tool policies.' : response.status === 409 ? 'This agent changed elsewhere. Refresh and try again.' : 'We could not complete that tool policy change.'));
    }
    return response.status === 204 ? undefined as AuraOperationResponse<K> : await response.json() as AuraOperationResponse<K>;
  }
}

export class ToolPolicyApiError extends Error {
  constructor(readonly status: number, message: string) { super(message); this.name = 'ToolPolicyApiError'; }
}
