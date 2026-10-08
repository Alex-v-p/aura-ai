import { describe, expect, it } from 'vitest';
import { isSelectableMemoryModel, memoryAgentFilterChange, memoryScopeFilterChange, normalizeMemoryFilters } from './memory-models';

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
});
