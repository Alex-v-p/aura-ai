import { InjectionToken } from '@angular/core';
import type {
  AgentMemoryPolicy, MemoryCandidateDetail, MemoryCandidateEdit, MemoryCandidatePage,
  MemoryCandidateState, MemoryCompatibleModel, MemoryDetail, MemoryLifecycleStatus,
  MemoryModelConfiguration, MemoryModelInventory, MemoryPage, MemoryReindexStatus,
  MemoryScope, MemoryCollectionScopeType, MemoryKind, MemoryProvenanceType, MemoryRecallMode,
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
  createAgentPolicy(agentProfileId: string, body: { readonly recallMode?: MemoryRecallMode; readonly automaticRecallThreshold?: number; readonly sharedUserRead: boolean; readonly currentAgentRead: boolean; readonly sharedUserPromotion: boolean; readonly fallbackRelevanceThreshold: number; readonly maxMemories: number; readonly contextBudgetFraction: number; readonly fallbackAgentProfileIds: ReadonlyArray<string>; readonly expectedRevision: number }): Promise<AgentMemoryPolicy>;
  attachAgentPolicy(agentProfileId: string, policyRevisionId: string, expectedAgentVersion: number): Promise<unknown>;
}

export const MEMORY_API = new InjectionToken<MemoryApi>('AURA_MEMORY_API');
export type MemoryModelOption = MemoryCompatibleModel;

export interface MemoryPolicySettings {
  readonly recallMode: MemoryRecallMode;
  readonly automaticRecallThreshold: number;
  readonly sharedUserRead: boolean;
  readonly currentAgentRead: boolean;
  readonly sharedUserPromotion: boolean;
  readonly fallbackRelevanceThreshold: number;
  readonly maxMemories: number;
  readonly contextBudgetFraction: number;
  readonly fallbackAgentProfileIds: ReadonlyArray<string>;
}

export function defaultMemoryPolicySettings(): MemoryPolicySettings {
  return { recallMode: 'off', automaticRecallThreshold: 0.70, sharedUserRead: true, currentAgentRead: true, sharedUserPromotion: false, fallbackRelevanceThreshold: 0.45, maxMemories: 2, contextBudgetFraction: 0.05, fallbackAgentProfileIds: [] };
}

export function hydratedMemoryPolicySettings(policy: AgentMemoryPolicy): MemoryPolicySettings {
  return { recallMode: policy.recallMode, automaticRecallThreshold: policy.automaticRecallThreshold, sharedUserRead: policy.sharedUserRead, currentAgentRead: policy.currentAgentRead, sharedUserPromotion: policy.sharedUserPromotion, fallbackRelevanceThreshold: policy.fallbackRelevanceThreshold, maxMemories: policy.maxMemories, contextBudgetFraction: policy.contextBudgetFraction, fallbackAgentProfileIds: policy.fallbackAgentProfileIds };
}

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

/** Core uses 404 to mean the owner has not saved a memory model configuration. */
export function isMissingMemoryConfiguration(error: unknown): boolean {
  return typeof error === 'object' && error !== null && 'status' in error && (error as { readonly status?: unknown }).status === 404;
}

/** Initial configuration is an explicit version-one write; later saves use the server version. */
export function expectedMemoryConfigurationVersion(configuration: MemoryModelConfiguration | null): number {
  return configuration?.version ?? 1;
}

export function canSaveMemoryConfiguration(configurationMissing: boolean, confirmed: boolean): boolean {
  return !configurationMissing || confirmed;
}

export type MemorySettingsRequestKey = 'inventory' | 'configuration' | 'reindex';
export function reconcileMemorySettingsRequests(results: Readonly<Record<MemorySettingsRequestKey, PromiseSettledResult<unknown>>>): { readonly configurationMissing: boolean; readonly failed: ReadonlyArray<MemorySettingsRequestKey> } {
  const failed = (Object.keys(results) as MemorySettingsRequestKey[]).filter((key) => results[key].status === 'rejected');
  return {
    configurationMissing: results.configuration.status === 'rejected' && isMissingMemoryConfiguration(results.configuration.reason),
    failed,
  };
}

export interface MemorySettingsLoadResult {
  readonly inventory: MemoryModelInventory | null;
  readonly configuration: MemoryModelConfiguration | null;
  readonly reindex: MemoryReindexStatus | null;
  readonly configurationMissing: boolean;
  readonly errors: ReadonlyArray<{ readonly key: MemorySettingsRequestKey; readonly reason: unknown }>;
}

export async function loadMemorySettings(api: Pick<MemoryApi, 'getModelInventory' | 'getModelConfiguration' | 'getReindexStatus'>): Promise<MemorySettingsLoadResult> {
  const [inventory, configuration, reindex] = await Promise.allSettled([api.getModelInventory(), api.getModelConfiguration(), api.getReindexStatus()]);
  const results = { inventory, configuration, reindex };
  const reconciliation = reconcileMemorySettingsRequests(results);
  const errors: Array<{ readonly key: MemorySettingsRequestKey; readonly reason: unknown }> = [];
  for (const key of Object.keys(results) as MemorySettingsRequestKey[]) {
    const result = results[key];
    if (result.status === 'rejected' && !(key === 'configuration' && reconciliation.configurationMissing)) errors.push({ key, reason: result.reason });
  }
  return {
    inventory: inventory.status === 'fulfilled' ? inventory.value : null,
    configuration: configuration.status === 'fulfilled' ? configuration.value : null,
    reindex: reindex.status === 'fulfilled' ? reindex.value : null,
    configurationMissing: reconciliation.configurationMissing,
    errors,
  };
}

export function saveMemorySettings(api: Pick<MemoryApi, 'updateModelConfiguration'>, extractionModelId: string, embeddingModelId: string, configuration: MemoryModelConfiguration | null): Promise<MemoryModelConfiguration> {
  return api.updateModelConfiguration(extractionModelId, embeddingModelId, expectedMemoryConfigurationVersion(configuration));
}

export function hasMemoryAdvancedFilters(filters: MemoryFilters): boolean {
  return Boolean(filters.agentProfileId || filters.kind || filters.provenanceType || filters.confidenceMin !== undefined || filters.createdFrom || filters.createdTo || filters.includeHistorical);
}

export function recommendedMemoryModelId(capability: 'structured_output' | 'embedding', models: ReadonlyArray<MemoryCompatibleModel>): string | null {
  const preferredId = capability === 'structured_output' ? 'qwen3:8b' : 'qwen3-embedding:4b';
  return models.filter((model) => isSelectableMemoryModel(model) && model.capabilities.includes(capability)).find((model) => model.id === preferredId || model.displayName === preferredId)?.id
    ?? models.find((model) => isSelectableMemoryModel(model) && model.capabilities.includes(capability))?.id
    ?? null;
}

export function hydratedMemoryModelIds(configuration: MemoryModelConfiguration | null, models: ReadonlyArray<MemoryCompatibleModel>): { readonly extractionModelId: string; readonly embeddingModelId: string } {
  return {
    extractionModelId: configuration?.extraction.modelId ?? recommendedMemoryModelId('structured_output', models) ?? '',
    embeddingModelId: configuration?.embedding.modelId ?? recommendedMemoryModelId('embedding', models) ?? '',
  };
}
