import { describe, expect, it } from 'vitest';
import { BUILT_IN_AURA_AGENT_PROFILE_ID, InMemoryAgentApi, isBuiltInAuraAgent } from './agent-models';

describe('InMemoryAgentApi', () => {
  it('identifies the built-in agent by immutable identity rather than display name', () => {
    expect(isBuiltInAuraAgent({ id: BUILT_IN_AURA_AGENT_PROFILE_ID, displayName: 'Renamed built-in' })).toBe(true);
    expect(isBuiltInAuraAgent({ id: 'custom-aura-named-agent', displayName: 'Aura' })).toBe(false);
  });
  it('keeps revisions immutable and increments optimistic versions', async () => {
    const api = new InMemoryAgentApi();
    const initial = (await api.listAgents())[0];
    expect(initial.id).toBe(BUILT_IN_AURA_AGENT_PROFILE_ID);
    expect(isBuiltInAuraAgent(initial)).toBe(true);
    const revised = await api.createRevision(initial.id, { displayName: 'Aura', purpose: 'Research carefully.', behavioralInstructions: 'Cite sources.', personaRevisionId: 'f2f4e37e-3e7b-48c3-b3fb-2d92dfb6a146' }, initial.version);
    expect(revised.version).toBe(2);
    expect(revised.revisions.map((item) => item.revision)).toEqual([1, 2]);
    await expect(api.createRevision(initial.id, { displayName: 'Aura', purpose: 'Conflict.', behavioralInstructions: 'Conflict.', personaRevisionId: 'f2f4e37e-3e7b-48c3-b3fb-2d92dfb6a146' }, initial.version)).rejects.toThrow('changed elsewhere');
  });

  it('disables without deleting history', async () => {
    const api = new InMemoryAgentApi();
    const initial = (await api.listAgents())[0];
    const disabled = await api.setStatus(initial.id, 'disabled', initial.version);
    expect(disabled.status).toBe('disabled');
    expect(disabled.revisions).toHaveLength(1);
  });
});
