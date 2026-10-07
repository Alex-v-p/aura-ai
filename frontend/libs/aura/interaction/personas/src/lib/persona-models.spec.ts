import { describe, expect, it } from 'vitest';
import { InMemoryPersonaApi } from './persona-models';

describe('InMemoryPersonaApi', () => {
  it('seeds Neutral and preserves prior persona revisions', async () => {
    const api = new InMemoryPersonaApi();
    const initial = (await api.listPersonas())[0];
    expect(initial.displayName).toBe('Neutral');
    const revised = await api.createRevision(initial.id, { displayName: 'Neutral', description: 'Updated description.', behavioralInstructions: 'Remain concise.' }, initial.version);
    expect(revised.revisions.map((item) => item.revision)).toEqual([1, 2]);
  });

  it('uses optimistic version conflicts for status changes', async () => {
    const api = new InMemoryPersonaApi();
    const initial = (await api.listPersonas())[0];
    await api.setStatus(initial.id, 'disabled', initial.version);
    await expect(api.setStatus(initial.id, 'active', initial.version)).rejects.toThrow('changed elsewhere');
  });
});
