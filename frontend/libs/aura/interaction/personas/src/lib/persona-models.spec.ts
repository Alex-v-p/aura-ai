import { describe, expect, it } from 'vitest';
import { InMemoryPersonaApi, previewContextFingerprint, type PersonaPreviewRequest } from './persona-models';

const previewCases: ReadonlyArray<{ readonly name: string; readonly request: PersonaPreviewRequest }> = [
  {
    name: 'a draft',
    request: { modelId: 'fixture-model', prompt: 'Say hello.', draft: { displayName: 'Neutral', description: 'Calm', behavioralInstructions: 'Be concise.' } },
  },
  {
    name: 'a persisted revision',
    request: { modelId: 'fixture-model', prompt: 'Say hello.', personaRevisionId: 'revision-1' },
  },
];

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

  it.each(previewCases)('accepts $name as an exclusive preview source', async ({ request }) => {
    const api = new InMemoryPersonaApi();
    const models = await api.listModels();
    expect(models.map((model) => model.id)).toEqual(['fixture-model', 'fixture-fast']);
    const result = await api.preview({ ...request, modelId: models[0].id });
    expect(result.content).toContain('Say hello.');
  });
});

describe('Persona preview interaction contracts', () => {
  it('changes the client context when prompt, model, revision, or draft changes', () => {
    const revision = { modelId: 'fixture-model', prompt: 'Say hello.', personaRevisionId: 'revision-1' } as const;
    expect(previewContextFingerprint(revision)).not.toBe(previewContextFingerprint({ ...revision, prompt: 'Say goodbye.' }));
    expect(previewContextFingerprint(revision)).not.toBe(previewContextFingerprint({ ...revision, modelId: 'fixture-fast' }));
    expect(previewContextFingerprint(revision)).not.toBe(previewContextFingerprint({ ...revision, personaRevisionId: 'revision-2' }));
    expect(previewContextFingerprint({ modelId: 'fixture-model', prompt: 'Say hello.', draft: { displayName: 'Neutral', description: 'Calm', behavioralInstructions: 'Be concise.' } })).not.toBe(previewContextFingerprint({ modelId: 'fixture-model', prompt: 'Say hello.', draft: { displayName: 'Warm', description: 'Calm', behavioralInstructions: 'Be concise.' } }));
  });
});
