import { InjectionToken } from '@angular/core';
import type {
  AgentMemoryPolicy, MemoryCandidateDetail, MemoryCandidateEdit, MemoryCandidatePage,
  MemoryCandidateState, MemoryCompatibleModel, MemoryDetail, MemoryLifecycleStatus,
  MemoryModelConfiguration, MemoryModelInventory, MemoryPage, MemoryReindexStatus,
  MemoryScope, MemoryCollectionScopeType, MemoryKind, MemoryProvenanceType,
} from '@aura/aura-api-client';

export interface MemoryFilters {
  readonly q?: string;
  readonly scopeType?: MemoryCollectionScopeType;
  readonly agentProfileId?: string;
  readonly kind?: MemoryKind;
  readonly status?: MemoryLifecycleStatus;
  readonly provenanceType?: MemoryProvenanceType;
  readonly confidenceMin?: number;
  readonly confidenceMax?: number;
  readonly createdFrom?: string;
  readonly createdTo?: string;
  readonly includeHistorical?: boolean;
}

export interface MemoryApi {
  listMemories(cursor?: string, filters?: MemoryFilters): Promise<MemoryPage>;
  getMemory(id: string, scope?: MemoryScope): Promise<MemoryDetail>;
  createMemory(input: { content: string; kind: MemoryKind; scope: MemoryScope; confidence: number; importance: number; halfLifeDays: number; observedAt?: string | null; validFrom?: string | null; validTo?: string | null }): Promise<MemoryDetail>;
  correctMemory(id: string, scope: MemoryScope, input: { content: string; reason: string; kind?: MemoryKind; confidence?: number; importance?: number; halfLifeDays?: number; observedAt?: string | null; validFrom?: string | null; validTo?: string | null; expectedVersion: number }): Promise<MemoryDetail>;
  updateStatus(id: string, status: MemoryLifecycleStatus, expectedVersion: number, relatedMemoryId?: string | null, scope?: MemoryScope): Promise<MemoryDetail>;
  updatePin(id: string, pinned: boolean, expectedVersion: number, scope?: MemoryScope): Promise<MemoryDetail>;
  purge(id: string, scope: MemoryScope, confirmation: 'PURGE MEMORY', expectedVersion: number): Promise<{ readonly memoryId: string; readonly auditId: string; readonly purgedAt: string }>;
  listCandidates(cursor?: string, state?: MemoryCandidateState): Promise<MemoryCandidatePage>;
  getCandidate(id: string): Promise<MemoryCandidateDetail>;
  approveCandidate(id: string, expectedVersion: number, edit?: MemoryCandidateEdit): Promise<{ readonly candidate: MemoryCandidateDetail; readonly activityId: string }>;
  rejectCandidate(id: string, expectedVersion: number, reason: string): Promise<{ readonly candidate: MemoryCandidateDetail; readonly activityId: string }>;
  getModelInventory(): Promise<MemoryModelInventory>;
  getModelConfiguration(): Promise<MemoryModelConfiguration>;
  updateModelConfiguration(extractionModelId: string, embeddingModelId: string, expectedVersion: number): Promise<MemoryModelConfiguration>;
  getReindexStatus(): Promise<MemoryReindexStatus>;
  resumeReindex(generationId: string): Promise<MemoryReindexStatus>;
  listAgentPolicies(agentProfileId: string): Promise<{ readonly items: ReadonlyArray<AgentMemoryPolicy>; readonly attachedPolicyRevisionId: string; readonly agentVersion: number }>;
  createAgentPolicy(agentProfileId: string, body: { readonly sharedUserRead: boolean; readonly currentAgentRead: boolean; readonly sharedUserPromotion: boolean; readonly fallbackRelevanceThreshold: number; readonly maxMemories: number; readonly contextBudgetFraction: number; readonly fallbackAgentProfileIds: ReadonlyArray<string>; readonly expectedRevision: number }): Promise<AgentMemoryPolicy>;
  attachAgentPolicy(agentProfileId: string, policyRevisionId: string, expectedAgentVersion: number): Promise<unknown>;
}

export const MEMORY_API = new InjectionToken<MemoryApi>('AURA_MEMORY_API');
export type MemoryModelOption = MemoryCompatibleModel;

/** Inventory entries are selectable only when Core reports them usable and
 * has not attached a disabling reason. The optional flag keeps the client
 * compatible with the richer provider inventory shape when present. */
export function isSelectableMemoryModel(model: MemoryCompatibleModel & { readonly selectable?: boolean }): boolean {
  return model.available && model.selectable !== false && !model.disabledReason;
}

export function normalizeMemoryFilters(filters: MemoryFilters): MemoryFilters {
  const agentProfileId = filters.agentProfileId?.trim();
  // An agent id is itself a scope selector. Keeping the normalized request
  // explicit prevents the transport from sending an agent id alongside the
  // user-scope default (which Core correctly rejects).
  const scopeType: MemoryCollectionScopeType | undefined = filters.scopeType === 'user' || filters.scopeType === 'all' || filters.scopeType === 'agent'
    ? filters.scopeType
    : agentProfileId
      ? 'agent'
      : filters.scopeType;
  const normalizedAgentProfileId = scopeType === 'agent' ? agentProfileId : undefined;
  return {
    ...(filters.q?.trim() ? { q: filters.q.trim().slice(0, 200) } : {}),
    ...(scopeType ? { scopeType } : {}),
    ...(normalizedAgentProfileId ? { agentProfileId: normalizedAgentProfileId } : {}),
    ...(filters.kind ? { kind: filters.kind } : {}),
    ...(filters.status ? { status: filters.status } : {}),
    ...(filters.provenanceType ? { provenanceType: filters.provenanceType } : {}),
    ...(filters.confidenceMin === undefined ? {} : { confidenceMin: Math.max(0, Math.min(1, filters.confidenceMin)) }),
    ...(filters.confidenceMax === undefined ? {} : { confidenceMax: Math.max(0, Math.min(1, filters.confidenceMax)) }),
    ...(filters.createdFrom ? { createdFrom: filters.createdFrom } : {}),
    ...(filters.createdTo ? { createdTo: filters.createdTo } : {}),
    ...(filters.includeHistorical ? { includeHistorical: true } : {}),
  };
}

export function memoryScopeFilterChange(filters: MemoryFilters, scopeType: MemoryCollectionScopeType | undefined): MemoryFilters {
  return normalizeMemoryFilters({ ...filters, scopeType, ...(scopeType !== 'agent' ? { agentProfileId: undefined } : {}) });
}

export function memoryAgentFilterChange(filters: MemoryFilters, agentProfileId: string): MemoryFilters {
  return normalizeMemoryFilters({
    ...filters,
    agentProfileId: agentProfileId || undefined,
    // Clearing a specific agent returns to the explicit all-scopes view so
    // the UI never emits an agent id with a user/all collection selector.
    scopeType: agentProfileId ? 'agent' : filters.scopeType === 'agent' ? 'all' : filters.scopeType,
  });
}
