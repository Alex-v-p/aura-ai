import { describe, expect, it } from 'vitest';
import { canSaveMemoryConfiguration, defaultMemoryPolicySettings, expectedMemoryConfigurationVersion, hasMemoryAdvancedFilters, hydratedMemoryModelIds, hydratedMemoryPolicySettings, isActionableReviewCandidate, isMissingMemoryConfiguration, isSelectableMemoryModel, loadMemorySettings, memoryAgentFilterChange, memoryScopeFilterChange, normalizeMemoryFilters, recommendedMemoryModelId, reconcileMemorySettingsRequests, saveMemorySettings } from './memory-models';

describe('MemoryStore', () => {
  it('bounds literal filters and keeps them in feature state', () => {
    expect(normalizeMemoryFilters({ q: `  ${'x'.repeat(240)}  `, confidenceMin: -1, confidenceMax: 4, includeHistorical: true })).toEqual({ q: 'x'.repeat(200), confidenceMin: 0, confidenceMax: 1, includeHistorical: true });
  });
  it('makes an agent selector an explicit agent scope and clears stale agent ids for user scope', () => {
    expect(normalizeMemoryFilters({ scopeType: 'all' })).toEqual({ scopeType: 'all' });
    expect(normalizeMemoryFilters({ scopeType: 'agent' })).toEqual({ scopeType: 'agent' });
    expect(normalizeMemoryFilters({ scopeType: 'agent', agentProfileId: 'agent-1' })).toEqual({ scopeType: 'agent', agentProfileId: 'agent-1' });
    expect(normalizeMemoryFilters({ agentProfileId: 'agent-1' })).toEqual({ scopeType: 'agent', agentProfileId: 'agent-1' });
    expect(normalizeMemoryFilters({ scopeType: 'user', agentProfileId: 'stale-agent' })).toEqual({ scopeType: 'user' });
    expect(normalizeMemoryFilters({ scopeType: 'all', agentProfileId: 'stale-agent' })).toEqual({ scopeType: 'all' });
    expect(normalizeMemoryFilters({})).toEqual({});
  });
  it('filters model inventory on effective availability and selectability', () => {
    const base = { id: 'model', displayName: 'Model', provider: 'ollama', modelRevision: null, modelDigest: 'a'.repeat(64), capabilities: ['structured_output'] as const, dimension: null, available: true, disabledReason: null };
    expect(isSelectableMemoryModel(base)).toBe(true);
    expect(isSelectableMemoryModel({ ...base, available: false })).toBe(false);
    expect(isSelectableMemoryModel({ ...base, disabledReason: 'provider unavailable' })).toBe(false);
    expect(isSelectableMemoryModel({ ...base, selectable: false })).toBe(false);
  });
  it('keeps scope transitions explicit and valid', () => {
    const all = memoryScopeFilterChange({ scopeType: 'all' }, 'all');
    const allAgents = memoryScopeFilterChange(all, 'agent');
    const oneAgent = memoryAgentFilterChange(allAgents, 'agent-1');
    const cleared = memoryAgentFilterChange(oneAgent, '');
    expect(all).toEqual({ scopeType: 'all' });
    expect(allAgents).toEqual({ scopeType: 'agent' });
    expect(oneAgent).toEqual({ scopeType: 'agent', agentProfileId: 'agent-1' });
    expect(cleared).toEqual({ scopeType: 'all' });
  });
  it('recognizes only the configuration 404 as the expected first-run state', () => {
    expect(isMissingMemoryConfiguration({ status: 404 })).toBe(true);
    expect(isMissingMemoryConfiguration({ status: 500 })).toBe(false);
    expect(isMissingMemoryConfiguration(new Error('not configured'))).toBe(false);
    expect(canSaveMemoryConfiguration(true, false)).toBe(false);
    expect(canSaveMemoryConfiguration(true, true)).toBe(true);
    expect(canSaveMemoryConfiguration(false, false)).toBe(true);
  });
  it('uses version one for the first explicit model configuration save', () => {
    expect(expectedMemoryConfigurationVersion(null)).toBe(1);
    expect(expectedMemoryConfigurationVersion({ version: 4 } as never)).toBe(4);
  });
  it('hydrates configured selections and recommends compatible first-run models', () => {
    const models = [
      { id: 'qwen3:8b', displayName: 'qwen3:8b', provider: 'ollama', modelRevision: null, modelDigest: 'a'.repeat(64), capabilities: ['structured_output'] as const, dimension: null, available: true, disabledReason: null },
      { id: 'qwen3-embedding:4b', displayName: 'qwen3-embedding:4b', provider: 'ollama', modelRevision: null, modelDigest: 'b'.repeat(64), capabilities: ['embedding'] as const, dimension: 2560, available: true, disabledReason: null },
    ];
    expect(recommendedMemoryModelId('structured_output', models)).toBe('qwen3:8b');
    expect(recommendedMemoryModelId('embedding', models)).toBe('qwen3-embedding:4b');
    expect(hydratedMemoryModelIds(null, models)).toEqual({ extractionModelId: 'qwen3:8b', embeddingModelId: 'qwen3-embedding:4b' });
    expect(hydratedMemoryModelIds({ extraction: { modelId: 'custom-extractor' }, embedding: { modelId: 'custom-embedder' } } as never, models)).toEqual({ extractionModelId: 'custom-extractor', embeddingModelId: 'custom-embedder' });
  });
  it('marks advanced filters without changing the quick-filter contract', () => {
    expect(hasMemoryAdvancedFilters({ scopeType: 'all' })).toBe(false);
    expect(hasMemoryAdvancedFilters({ scopeType: 'all', kind: 'preference' })).toBe(true);
    expect(hasMemoryAdvancedFilters({ scopeType: 'all', includeHistorical: true })).toBe(true);
  });
  it('keeps incomplete provider diagnostics out of the actionable review queue', () => {
    const base = { id: 'candidate', jobId: 'job', runId: 'run', version: 1, action: 'review', state: 'review', content: null, kind: null, scope: null, confidence: .7, importance: null, halfLifeDays: null, validTo: null, sensitivity: 'unknown_risk', relatedMemoryId: null, memoryId: null, decisionReason: 'invalid_provider_output', createdAt: '2026-10-08T09:00:00Z', decidedAt: null } as never;
    expect(isActionableReviewCandidate(base)).toBe(false);
    expect(isActionableReviewCandidate({ ...base, content: 'The owner prefers tea.', kind: 'preference', scope: { type: 'user' }, importance: .7, halfLifeDays: 365 })).toBe(true);
  });
  it('reconciles independent settings requests without discarding successful results', () => {
    const result = reconcileMemorySettingsRequests({
      inventory: { status: 'fulfilled', value: { models: [] } },
      configuration: { status: 'rejected', reason: { status: 404 } },
      reindex: { status: 'fulfilled', value: { phase: 'idle' } },
    });
    expect(result.configurationMissing).toBe(true);
    expect(result.failed).toEqual(['configuration']);
    const retry = reconcileMemorySettingsRequests({
      inventory: { status: 'fulfilled', value: { models: [] } },
      configuration: { status: 'fulfilled', value: { version: 1 } },
      reindex: { status: 'rejected', reason: { status: 503 } },
    });
    expect(retry.configurationMissing).toBe(false);
    expect(retry.failed).toEqual(['reindex']);
  });
  it('loads settings through a fake API while retaining successful responses across retryable failures', async () => {
    const inventory = { models: [], observedAt: '2026-10-08T10:00:00Z' };
    const reindex = { phase: 'idle', activeGeneration: null, replacementGeneration: null, processedRevisionCount: 0, totalRevisionCount: 0, startedAt: null, updatedAt: null, completedAt: null, retryable: false };
    let reindexAvailable = false;
    const api = { getModelInventory: async () => inventory, getModelConfiguration: async () => { throw { status: 404 }; }, getReindexStatus: async () => reindexAvailable ? reindex : Promise.reject({ status: 503 }) };
    const first = await loadMemorySettings(api);
    expect(first.inventory).toBe(inventory);
    expect(first.reindex).toBeNull();
    expect(first.configurationMissing).toBe(true);
    expect(first.errors.map(({ key }) => key)).toEqual(['reindex']);
    reindexAvailable = true;
    const retry = await loadMemorySettings(api);
    expect(retry.inventory).toBe(inventory);
    expect(retry.reindex).toBe(reindex);
    expect(retry.configurationMissing).toBe(true);
  });
  it('calls the fake configuration API with expected version one on first save', async () => {
    const calls: unknown[][] = [];
    const configuration = { version: 1 } as never;
    const api = { updateModelConfiguration: async (...args: unknown[]) => { calls.push(args); return configuration; } };
    await expect(saveMemorySettings(api, 'qwen3:8b', 'qwen3-embedding:4b', null)).resolves.toBe(configuration);
    expect(calls).toEqual([['qwen3:8b', 'qwen3-embedding:4b', 1]]);
  });
  it('starts policy drafts conservatively and hydrates an attached immutable revision', () => {
    expect(defaultMemoryPolicySettings()).toMatchObject({ recallMode: 'off', automaticRecallThreshold: .70, maxMemories: 2, contextBudgetFraction: .05 });
    expect(hydratedMemoryPolicySettings({ recallMode: 'automatic', automaticRecallThreshold: .82, maxMemories: 1, contextBudgetFraction: .03, fallbackAgentProfileIds: ['agent-2'] } as never)).toMatchObject({ recallMode: 'automatic', automaticRecallThreshold: .82, maxMemories: 1, contextBudgetFraction: .03, fallbackAgentProfileIds: ['agent-2'] });
  });
});
