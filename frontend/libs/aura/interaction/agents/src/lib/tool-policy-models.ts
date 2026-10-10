import { InjectionToken } from '@angular/core';
import type { AgentToolPolicy, ToolCatalog, ToolDescriptorSummary, ToolGrant } from '@aura/aura-api-client';

export type { AgentToolPolicy, ToolDescriptorSummary, ToolGrant };

export interface ToolPolicyApi {
  listCatalog(): Promise<ToolCatalog>;
  listPolicies(agentProfileId: string): Promise<{
    readonly items: ReadonlyArray<AgentToolPolicy>;
    readonly attachedPolicyRevisionId: string;
    readonly agentVersion: number;
  }>;
  createPolicy(agentProfileId: string, grants: ReadonlyArray<ToolGrant>, expectedRevision: number): Promise<AgentToolPolicy>;
  attachPolicy(agentProfileId: string, policyRevisionId: string, expectedAgentVersion: number): Promise<void>;
}

export const TOOL_POLICY_API = new InjectionToken<ToolPolicyApi>('AURA_TOOL_POLICY_API');

export const MEMORY_SEARCH_TOOL_ID = 'memory.search';
export const MEMORY_SEARCH_TOOL_VERSION = 1;

export function grantsMemorySearch(policy: AgentToolPolicy | null | undefined): boolean {
  return Boolean(policy?.grants.some((grant) => grant.toolId === MEMORY_SEARCH_TOOL_ID && grant.toolVersion === MEMORY_SEARCH_TOOL_VERSION));
}

const memorySearchDescriptor: ToolDescriptorSummary = {
  id: MEMORY_SEARCH_TOOL_ID,
  version: MEMORY_SEARCH_TOOL_VERSION,
  displayName: 'Memory search',
  description: 'Search authorized memory for context relevant to the current request.',
  classification: 'read',
  permissionScope: 'memory:read',
  confirmation: 'never',
  timeoutMs: 30_000,
  retryLimit: 0,
  idempotency: 'read_only',
  sensitivity: 'private',
  observableComponent: 'aura.runtime.tool_execution',
};

/** Deterministic preview implementation for the agent-management surface. */
export class InMemoryToolPolicyApi implements ToolPolicyApi {
  private readonly policies = new Map<string, AgentToolPolicy[]>();
  private readonly attached = new Map<string, string>();
  private readonly versions = new Map<string, number>();

  constructor() {
    const builtIn: AgentToolPolicy = { id: 'aura-tool-policy-r1', agentProfileId: '7e1d98ee-65b2-5bc7-9ca9-edd7ad85b540', revision: 1, grants: [{ toolId: MEMORY_SEARCH_TOOL_ID, toolVersion: MEMORY_SEARCH_TOOL_VERSION }], maxCallsPerRun: 1, timeoutMs: 30_000, retryLimit: 0, createdAt: new Date().toISOString() };
    this.policies.set(builtIn.agentProfileId, [builtIn]);
    this.attached.set(builtIn.agentProfileId, builtIn.id);
    this.versions.set(builtIn.agentProfileId, 1);
  }

  listCatalog(): Promise<ToolCatalog> { return Promise.resolve({ items: [memorySearchDescriptor] }); }

  listPolicies(agentProfileId: string): Promise<{ items: ReadonlyArray<AgentToolPolicy>; attachedPolicyRevisionId: string; agentVersion: number }> {
    return Promise.resolve({ items: [...(this.policies.get(agentProfileId) ?? [])], attachedPolicyRevisionId: this.attached.get(agentProfileId) ?? '', agentVersion: this.versions.get(agentProfileId) ?? 1 });
  }

  createPolicy(agentProfileId: string, grants: ReadonlyArray<ToolGrant>, expectedRevision: number): Promise<AgentToolPolicy> {
    const items = this.policies.get(agentProfileId) ?? [];
    const currentRevision = items.at(-1)?.revision ?? 0;
    if (expectedRevision !== currentRevision) return Promise.reject(new Error('This tool policy changed elsewhere. Refresh and try again.'));
    const policy: AgentToolPolicy = { id: `tool-policy-${agentProfileId}-${currentRevision + 1}`, agentProfileId, revision: currentRevision + 1, grants: grants.map((grant) => ({ ...grant })), maxCallsPerRun: grants.length ? 1 : 0, timeoutMs: 30_000, retryLimit: 0, createdAt: new Date().toISOString() };
    this.policies.set(agentProfileId, [...items, policy]);
    return Promise.resolve(policy);
  }

  attachPolicy(agentProfileId: string, policyRevisionId: string, expectedAgentVersion: number): Promise<void> {
    const version = this.versions.get(agentProfileId) ?? 1;
    if (version !== expectedAgentVersion) return Promise.reject(new Error('This agent changed elsewhere. Refresh and try again.'));
    if (!(this.policies.get(agentProfileId) ?? []).some((policy) => policy.id === policyRevisionId)) return Promise.reject(new Error('That tool policy revision is unavailable.'));
    this.attached.set(agentProfileId, policyRevisionId);
    this.versions.set(agentProfileId, version + 1);
    return Promise.resolve();
  }
}
