import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { ConversationApi, RunEventSubscription } from './conversation-api';
import type { ConversationDetail, ConversationRunAccepted, ConversationSummary, MemoryActivity, ModelCatalog, Run, RunEvent, RunMemoryActivitySnapshot, Session } from '@aura/aura-api-client';
import { ConversationStore, memoryActivityIndicators } from './conversation-store';

const model = { id: 'qwen2.5:7b', displayName: 'Qwen 2.5', provider: 'ollama', capabilities: ['chat'] as const, availability: 'available' as const, selectable: true, disabledReason: null };
const preferredModel = { id: 'llama3.2:3b', displayName: 'Llama 3.2', provider: 'ollama', capabilities: ['chat'] as const, availability: 'available' as const, selectable: true, disabledReason: null };
const session: Session = { principal: { issuer: 'https://authentik.test', subject: 'owner' }, csrfToken: 'csrf', idleExpiresAt: '2026-10-05T20:00:00Z', absoluteExpiresAt: '2026-10-06T12:00:00Z' };

function fakeApi(listConversations: ConversationApi['listConversations'] = async () => ({ items: [], nextCursor: null }), initialDetails: ReadonlyArray<ConversationDetail> = [], initialRuns: ReadonlyArray<Run> = [], streamRunEvents?: ConversationApi['streamRunEvents'], getConversation?: ConversationApi['getConversation'], modelCatalog?: ModelCatalog, createConversationOverride?: ConversationApi['createConversation'], listModelsOverride?: ConversationApi['listModels']): ConversationApi {
  let sequence = 0;
  const runs = new Map<string, Run>();
  const details = new Map<string, ConversationDetail>();
  for (const run of initialRuns) runs.set(run.id, run);
  for (const detail of initialDetails) details.set(detail.id, detail);
  const api: ConversationApi = {
    getSession: async () => session,
    startLogin: () => undefined,
    logout: async () => undefined,
    listModels: listModelsOverride ?? (async (): Promise<ModelCatalog> => modelCatalog ?? ({ models: [model, preferredModel], defaultModelId: preferredModel.id, observedAt: new Date().toISOString() })),
    listConversations,
    getConversation: getConversation ?? (async (id) => { const detail = details.get(id); if (!detail) throw new Error('missing detail'); return detail; }),
    createConversation: createConversationOverride ?? (async (message, modelId) => accepted(`conversation-${++sequence}`, message, modelId)),
    updateConversation: async (id, modelId, version) => { const detail = details.get(id); if (!detail) throw new Error('missing detail'); return { ...detail, modelId, version }; },
    createRun: async (id, message, version) => accepted(id, message, details.get(id)?.modelId ?? model.id, version),
    cancelRun: async (id) => { const current = runs.get(id); if (!current) throw new Error('missing run'); const run = { ...current, status: 'canceled' as const, finishedAt: new Date().toISOString() }; runs.set(id, run); return run; },
    retryRun: async (id) => { const current = runs.get(id); if (!current) throw new Error('missing run'); return accepted(current.conversationId, 'retry', current.modelId); },
    streamRunEvents: streamRunEvents ?? ((id, _last, onEvent): RunEventSubscription => { queueMicrotask(() => { const current = runs.get(id); if (!current) return; onEvent({ schemaVersion: 1, eventId: `event-${id}`, sequence: 1, eventType: 'assistant.delta', runId: id, conversationId: current.conversationId, occurredAt: new Date().toISOString(), data: { messageId: `assistant-${id}`, offset: 0, text: 'A server response' } }); }); return { close: () => undefined }; }),
  };
  function accepted(id: string, message: string, modelId: string, version = 1): ConversationRunAccepted {
    const now = new Date().toISOString(); const run: Run = { id: `run-${id}`, conversationId: id, userMessageId: `user-${id}`, assistantMessageId: null, status: 'running', agentRevisionId: 'agent-rev-1', modelPolicyRevisionId: 'policy-1', provider: 'ollama', modelId, retryOfRunId: null, createdAt: now, startedAt: now, finishedAt: null, error: null }; runs.set(run.id, run);
    const conversation = { id, title: message, agentProfileId: 'general', agentRevisionId: 'agent-rev-1', modelId, version, createdAt: now, updatedAt: now, currentRun: run };
    const userMessage = { id: `user-${id}`, conversationId: id, role: 'user' as const, content: message, state: 'complete' as const, runId: run.id, createdAt: now, updatedAt: now };
    details.set(id, { ...conversation, messages: [userMessage], recentRuns: [run] });
    return { conversation, userMessage, run };
  }
  return api;
}

function summary(id: string): ConversationSummary {
  const now = new Date().toISOString();
  return { id, title: id, agentProfileId: 'general', agentRevisionId: 'agent-rev-1', modelId: model.id, version: 1, createdAt: now, updatedAt: now, currentRun: null };
}

function persistedRecoveryDetail(status: Run['status']): ConversationDetail {
  const now = new Date().toISOString();
  const run: Run = { id: `run-${status}`, conversationId: `conversation-${status}`, userMessageId: `user-${status}`, assistantMessageId: null, status, agentRevisionId: 'agent-rev-1', modelPolicyRevisionId: 'policy-1', provider: 'ollama', modelId: model.id, retryOfRunId: null, createdAt: now, startedAt: now, finishedAt: now, error: status === 'failed' ? { code: 'MODEL_FAILED', message: 'The model failed.', retryable: true, traceId: 'trace' } : null };
  return { ...summary(run.conversationId), messages: [], recentRuns: [run], currentRun: null };
}

describe('ConversationStore', () => {
  it('keeps recall and creation/update indicators independent', () => {
    const recalled = { action: 'recalled' } as MemoryActivity;
    const created = { action: 'created' } as MemoryActivity;
    expect(memoryActivityIndicators([recalled])).toEqual({ recalled: true, updated: false });
    expect(memoryActivityIndicators([created])).toEqual({ recalled: false, updated: true });
    expect(memoryActivityIndicators([recalled, created])).toEqual({ recalled: true, updated: true });
  });
  let store: ConversationStore;
  beforeEach(async () => { store = new ConversationStore(fakeApi()); await new Promise<void>((resolve) => queueMicrotask(resolve)); });

  it('keeps drafts in memory and validates empty sends', () => { store.updateDraft(''); expect(store.send()).toBe(false); expect(store.notice()).toContain('Write a message'); });
  it('blocks keyboard-style sends while the current agent is disabled', async () => {
    const disabled = { profileId: 'agent-profile', revisionId: 'agent-revision-1', revision: 1, displayName: 'Aura', status: 'disabled' as const };
    const detail = { ...summary('disabled-conversation'), agent: disabled, agentAssignments: [], messages: [], recentRuns: [], currentRun: null } as ConversationDetail;
    const disabledStore = new ConversationStore(fakeApi(async () => ({ items: [detail], nextCursor: null }), [detail]));
    await new Promise<void>((resolve) => setTimeout(resolve, 0));
    disabledStore.updateDraft('  Keep this exact draft  ');

    expect(disabledStore.send()).toBe(false);
    expect(disabledStore.draft()).toBe('  Keep this exact draft  ');
    expect(disabledStore.notice()).toContain('disabled');
  });
  it('restores the draft and refreshes agent state when disablement races submission', async () => {
    const active = { profileId: 'agent-profile', revisionId: 'agent-revision-1', revision: 1, displayName: 'Aura', status: 'active' as const };
    const disabled = { ...active, status: 'disabled' as const };
    let serverDetail = { ...summary('race-conversation'), agent: active, agentAssignments: [], messages: [], recentRuns: [], currentRun: null } as ConversationDetail;
    const api = fakeApi(async () => ({ items: [serverDetail], nextCursor: null }), [serverDetail], [], undefined, async () => serverDetail);
    api.createRun = async () => {
      serverDetail = { ...serverDetail, agent: disabled };
      throw { status: 409, code: 'AGENT_DISABLED', message: 'agent is disabled', retryable: false };
    };
    const raceStore = new ConversationStore(api);
    await new Promise<void>((resolve) => setTimeout(resolve, 0));
    raceStore.updateDraft('  Preserve this exact text  \n');

    expect(raceStore.send()).toBe(true);
    await new Promise<void>((resolve) => setTimeout(resolve, 0));

    expect(raceStore.draft()).toBe('  Preserve this exact text  \n');
    expect(raceStore.selected().turns).toHaveLength(0);
    expect(raceStore.selectedAgent().status).toBe('disabled');
    expect(raceStore.notice()).toContain('disabled');
  });
  it('uses the server model catalog default for new drafts', () => { expect(store.selectedModelId()).toBe(preferredModel.id); store.create(); expect(store.selectedModelId()).toBe(preferredModel.id); });
  it('hydrates the transcript without waiting for model discovery', async () => {
    let resolveModels!: (catalog: ModelCatalog) => void;
    const now = new Date().toISOString();
    const detail = { ...summary('hydrated-before-models'), messages: [{ id: 'message-hydrated', conversationId: 'hydrated-before-models', role: 'user' as const, content: 'Loaded while Ollama is unavailable', state: 'complete' as const, runId: null, createdAt: now, updatedAt: now }], recentRuns: [], currentRun: null } as ConversationDetail;
    const api = fakeApi(async () => ({ items: [detail], nextCursor: null }), [detail], [], undefined, undefined, undefined, undefined, () => new Promise<ModelCatalog>((resolve) => { resolveModels = resolve; }));
    const hydratedStore = new ConversationStore(api);

    await vi.waitFor(() => expect(hydratedStore.loading()).toBe(false));

    expect(hydratedStore.selected().id).toBe(detail.id);
    expect(hydratedStore.selected().turns[0]?.text).toBe('Loaded while Ollama is unavailable');
    expect(hydratedStore.modelCatalogNotice()).toBeNull();
    resolveModels({ models: [model, preferredModel], defaultModelId: preferredModel.id, observedAt: now });
    await vi.waitFor(() => expect(hydratedStore.models()).toHaveLength(2));
    hydratedStore.create();
    expect(hydratedStore.selectedModelId()).toBe(preferredModel.id);
  });
  it('keeps conversation loading non-blocking when model discovery fails', async () => {
    const detail = { ...summary('loaded-without-models'), messages: [], recentRuns: [], currentRun: null } as ConversationDetail;
    const api = fakeApi(async () => ({ items: [detail], nextCursor: null }), [detail], [], undefined, undefined, undefined, undefined, async () => { throw { status: 503, message: 'Ollama is unavailable' }; });
    const degradedStore = new ConversationStore(api);

    await vi.waitFor(() => expect(degradedStore.loading()).toBe(false));

    expect(degradedStore.selected().id).toBe(detail.id);
    expect(degradedStore.authState()).toBe('authenticated');
    expect(degradedStore.notice()).toContain('Model discovery is temporarily unavailable');
  });
  it('does not replace a conversation error with a model discovery notice', async () => {
    const api = fakeApi(async () => { throw { status: 503, message: 'Conversation history is unavailable' }; }, [], [], undefined, undefined, undefined, undefined, async () => { throw { status: 503, message: 'Ollama is unavailable' }; });
    const failedStore = new ConversationStore(api);

    await vi.waitFor(() => expect(failedStore.loading()).toBe(false));

    expect(failedStore.notice()).toBe('Conversation history is unavailable');
    expect(failedStore.notice()).not.toContain('Model discovery');
  });
  it('ignores a stale model catalog success after a newer load fails discovery', async () => {
    let calls = 0;
    let resolveFirst!: (catalog: ModelCatalog) => void;
    const api = fakeApi(undefined, [], [], undefined, undefined, undefined, undefined, () => {
      calls += 1;
      if (calls === 1) return new Promise<ModelCatalog>((resolve) => { resolveFirst = resolve; });
      return Promise.reject({ status: 503, message: 'Ollama is unavailable' });
    });
    const raceStore = new ConversationStore(api);
    await vi.waitFor(() => expect(calls).toBe(1));

    const currentLoad = raceStore.load();
    await vi.waitFor(() => expect(calls).toBe(2));
    await currentLoad;
    await vi.waitFor(() => expect(raceStore.modelCatalogNotice()).toContain('Model discovery is temporarily unavailable'));

    resolveFirst({ models: [model, preferredModel], defaultModelId: preferredModel.id, observedAt: new Date().toISOString() });
    await Promise.resolve();
    expect(raceStore.models()).toEqual([]);
    expect(raceStore.modelCatalogNotice()).toContain('Model discovery is temporarily unavailable');
  });
  it('ignores a stale model catalog auth failure after a newer load succeeds', async () => {
    let calls = 0;
    let rejectFirst!: (error: unknown) => void;
    const catalog: ModelCatalog = { models: [model, preferredModel], defaultModelId: preferredModel.id, observedAt: new Date().toISOString() };
    const api = fakeApi(undefined, [], [], undefined, undefined, undefined, undefined, () => {
      calls += 1;
      if (calls === 1) return new Promise<ModelCatalog>((_resolve, reject) => { rejectFirst = reject; });
      return Promise.resolve(catalog);
    });
    const raceStore = new ConversationStore(api);
    await vi.waitFor(() => expect(calls).toBe(1));

    const currentLoad = raceStore.load();
    await vi.waitFor(() => expect(calls).toBe(2));
    await currentLoad;
    await vi.waitFor(() => expect(raceStore.models()).toEqual(catalog.models));
    expect(raceStore.authState()).toBe('authenticated');

    rejectFirst({ status: 401, message: 'Session expired' });
    await Promise.resolve();
    expect(raceStore.authState()).toBe('authenticated');
    expect(raceStore.models()).toEqual(catalog.models);
  });
  it.each([401, 403])('keeps current model discovery $0 responses as authentication failures', async (status) => {
    const api = fakeApi(undefined, [], [], undefined, undefined, undefined, undefined, async () => { throw { status, message: 'Authentication required' }; });
    const authStore = new ConversationStore(api);

    await vi.waitFor(() => expect(authStore.authState()).toBe('unauthenticated'));
    expect(authStore.loading()).toBe(false);
  });
  it('preserves an explicitly selected draft model and notice when delayed discovery completes', async () => {
    let calls = 0;
    let resolveSecond!: (catalog: ModelCatalog) => void;
    const catalog: ModelCatalog = { models: [model, preferredModel], defaultModelId: preferredModel.id, observedAt: new Date().toISOString() };
    const api = fakeApi(undefined, [], [], undefined, undefined, undefined, undefined, () => {
      calls += 1;
      if (calls === 1) return Promise.resolve(catalog);
      return new Promise<ModelCatalog>((resolve) => { resolveSecond = resolve; });
    });
    const draftStore = new ConversationStore(api);
    await vi.waitFor(() => expect(draftStore.models()).toEqual(catalog.models));
    draftStore.create();
    await draftStore.selectModel(model.id);
    draftStore.showRouteNotice('Keep this conversation notice');

    const currentLoad = draftStore.load();
    await vi.waitFor(() => expect(calls).toBe(2));
    await currentLoad;
    resolveSecond(catalog);
    await vi.waitFor(() => expect(draftStore.models()).toEqual(catalog.models));

    expect(draftStore.selectedModelId()).toBe(model.id);
    expect(draftStore.notice()).toBe('Keep this conversation notice');
  });
  it('reuses one draft while preserving its text and configuration', async () => {
    const agent = { profileId: 'agent-profile', revisionId: 'agent-revision-2', revision: 2, displayName: 'Researcher', status: 'active' as const };
    const persona = { profileId: 'persona-profile', revisionId: 'persona-revision-2', revision: 2, displayName: 'Focused', status: 'active' as const, newerRevisionAvailable: false };
    await store.selectModel(model.id);
    store.requestAgent(agent);
    store.requestPersona(persona);
    store.updateDraft('Keep this unsent thought');
    const draftId = store.selectedId();

    store.create();

    expect(store.selectedId()).toBe(draftId);
    expect(store.draft()).toBe('Keep this unsent thought');
    expect(store.selectedModelId()).toBe(model.id);
    expect(store.selectedAgent().revisionId).toBe(agent.revisionId);
    expect(store.selectedPersona()?.revisionId).toBe(persona.revisionId);
  });
  it('retains a rejected first-send draft outside the library', async () => {
    const rejectedApi = fakeApi(undefined, [], [], undefined, undefined, undefined, async () => { throw { status: 503, message: 'Conversation service unavailable', retryable: true }; });
    const rejectedStore = new ConversationStore(rejectedApi);
    await new Promise<void>((resolve) => setTimeout(resolve, 0));
    rejectedStore.updateDraft('Keep this after rejection');

    expect(rejectedStore.send()).toBe(true);
    await new Promise<void>((resolve) => setTimeout(resolve, 0));

    expect(rejectedStore.selectedId()).toMatch(/^draft-/);
    expect(rejectedStore.draft()).toBe('Keep this after rejection');
    expect(rejectedStore.selected().turns).toHaveLength(0);
    expect(rejectedStore.conversations().filter((conversation) => !conversation.id.startsWith('draft-'))).toHaveLength(0);
  });
  it('replaces a draft with exactly one durable conversation after acceptance', async () => {
    let createCalls = 0;
    const acceptedApi = fakeApi(undefined, [], [], undefined, undefined, undefined, async (message, modelId) => {
      createCalls += 1;
      return fakeApi().createConversation(message, modelId, 'accepted-once');
    });
    const acceptedStore = new ConversationStore(acceptedApi);
    await new Promise<void>((resolve) => setTimeout(resolve, 0));
    acceptedStore.updateDraft('Persist this once');

    expect(acceptedStore.send()).toBe(true);
    await new Promise<void>((resolve) => setTimeout(resolve, 0));

    expect(createCalls).toBe(1);
    expect(acceptedStore.selectedId()).toMatch(/^conversation-/);
    expect(acceptedStore.conversations().filter((conversation) => !conversation.id.startsWith('draft-'))).toHaveLength(1);
    expect(acceptedStore.conversations().filter((conversation) => conversation.id.startsWith('draft-'))).toHaveLength(0);
  });
  it.each([
    { name: 'missing', defaultModelId: null },
    { name: 'incompatible', defaultModelId: 'embedding-only' },
  ])('does not silently fall back when the configured default is $name', async ({ defaultModelId }) => {
    const degradedCatalog: ModelCatalog = { models: [model], defaultModelId, observedAt: new Date().toISOString() };
    const degradedStore = new ConversationStore(fakeApi(undefined, [], [], undefined, undefined, degradedCatalog));
    await new Promise<void>((resolve) => setTimeout(resolve, 0));

    expect(degradedStore.selectedModelId()).toBe('');
    degradedStore.updateDraft('Require an explicit model');
    expect(degradedStore.send()).toBe(false);
    expect(degradedStore.notice()).toContain('Choose an available model');
    await degradedStore.selectModel(model.id);
    expect(degradedStore.selectedModelId()).toBe(model.id);
    expect(degradedStore.send()).toBe(true);
  });
  it('creates a persisted conversation only on first send', async () => { expect(store.selected().id).toMatch(/^draft-/); store.updateDraft('A small thought'); expect(store.send()).toBe(true); await new Promise<void>((resolve) => queueMicrotask(resolve)); expect(store.selected().id).toMatch(/^conversation-/); expect(store.selected().turns[0].text).toBe('A small thought'); });
  it('shows the deterministic first-message fallback when the server title is still pending', async () => {
    const api = fakeApi(undefined, [], [], undefined, undefined, undefined, async (message, modelId) => {
      const accepted = await fakeApi().createConversation(message, modelId, 'pending-title');
      return { ...accepted, conversation: { ...accepted.conversation, title: 'New conversation' } };
    });
    const pendingTitleStore = new ConversationStore(api);
    await new Promise<void>((resolve) => setTimeout(resolve, 0));
    pendingTitleStore.updateDraft('A first message about nearby observatories');
    expect(pendingTitleStore.send()).toBe(true);
    await new Promise<void>((resolve) => setTimeout(resolve, 0));
    await new Promise<void>((resolve) => queueMicrotask(resolve));

    expect(pendingTitleStore.selected().title).toBe('A first message about nearby obs…');
    expect(pendingTitleStore.draft()).toBe('');
  });
  it('refreshes generated title and sidebar summary after terminal status without losing a draft', async () => {
    const now = new Date().toISOString();
    const running: Run = { id: 'run-title-refresh', conversationId: 'title-refresh', userMessageId: 'user-title-refresh', assistantMessageId: null, status: 'running', agentRevisionId: 'agent-rev-1', modelPolicyRevisionId: 'policy-1', provider: 'ollama', modelId: model.id, retryOfRunId: null, createdAt: now, startedAt: now, finishedAt: null, error: null };
    const completed: Run = { ...running, status: 'completed', finishedAt: now, assistantMessageId: 'assistant-title-refresh' };
    const user = { id: running.userMessageId, conversationId: running.conversationId, role: 'user' as const, content: 'A first message fallback', state: 'complete' as const, runId: running.id, createdAt: now, updatedAt: now };
    let serverDetail = { ...summary('title-refresh'), title: 'A first message fallback', currentRun: running, messages: [user], recentRuns: [running] } as ConversationDetail;
    let emitTerminal: (() => void) | undefined;
    const api = fakeApi(
      async () => ({ items: [serverDetail], nextCursor: null }),
      [serverDetail],
      [running],
      (_runId, _lastEventId, onEvent) => {
        emitTerminal = () => onEvent({ schemaVersion: 1, eventId: 'title-terminal', sequence: 1, eventType: 'run.status', runId: running.id, conversationId: running.conversationId, occurredAt: now, data: { status: 'completed', startedAt: now, finishedAt: now } } as RunEvent, 'title-terminal');
        return { close: () => undefined };
      },
      async () => serverDetail,
    );
    const titleStore = new ConversationStore(api);
    await new Promise<void>((resolve) => setTimeout(resolve, 0));
    titleStore.updateDraft('Keep this draft during title refresh');
    serverDetail = { ...serverDetail, title: 'Generated observatory topic', currentRun: null, recentRuns: [completed], messages: [user, { id: completed.assistantMessageId!, conversationId: running.conversationId, role: 'assistant', content: 'The completed answer', state: 'complete', runId: running.id, createdAt: now, updatedAt: now }] } as ConversationDetail;
    emitTerminal?.();
    await new Promise<void>((resolve) => setTimeout(resolve, 0));

    expect(titleStore.selected().title).toBe('Generated observatory topic');
    expect(titleStore.conversations().find((item) => item.id === 'title-refresh')?.title).toBe('Generated observatory topic');
    expect(titleStore.draft()).toBe('Keep this draft during title refresh');
  });
  it('refreshes a terminal title in place without reordering the sidebar', async () => {
    const now = new Date().toISOString();
    const first = { ...summary('first-title'), title: 'First conversation', messages: [], recentRuns: [], currentRun: null } as ConversationDetail;
    const running: Run = { id: 'run-second-title', conversationId: 'second-title', userMessageId: 'user-second-title', assistantMessageId: null, status: 'running', agentRevisionId: 'agent-rev-1', modelPolicyRevisionId: 'policy-1', provider: 'ollama', modelId: model.id, retryOfRunId: null, createdAt: now, startedAt: now, finishedAt: null, error: null };
    const completed: Run = { ...running, status: 'completed', finishedAt: now, assistantMessageId: 'assistant-second-title' };
    const user = { id: running.userMessageId, conversationId: running.conversationId, role: 'user' as const, content: 'Second fallback title', state: 'complete' as const, runId: running.id, createdAt: now, updatedAt: now };
    let second = { ...summary('second-title'), title: 'Second fallback title', currentRun: running, messages: [user], recentRuns: [running] } as ConversationDetail;
    let emitTerminal: (() => void) | undefined;
    const api = fakeApi(
      async () => ({ items: [first, second], nextCursor: null }),
      [first, second],
      [running],
      (_runId, _lastEventId, onEvent) => {
        emitTerminal = () => onEvent({ schemaVersion: 1, eventId: 'second-title-terminal', sequence: 1, eventType: 'run.status', runId: running.id, conversationId: running.conversationId, occurredAt: now, data: { status: 'completed', startedAt: now, finishedAt: now } } as RunEvent, 'second-title-terminal');
        return { close: () => undefined };
      },
      async (id) => id === second.id ? second : first,
    );
    const titleStore = new ConversationStore(api);
    await new Promise<void>((resolve) => setTimeout(resolve, 0));
    titleStore.select(second.id);
    await new Promise<void>((resolve) => setTimeout(resolve, 0));

    second = { ...second, title: 'Generated second title', currentRun: null, recentRuns: [completed], messages: [user, { id: 'assistant-second-title', conversationId: second.id, role: 'assistant', content: 'Completed answer', state: 'complete', runId: running.id, createdAt: now, updatedAt: now }] } as ConversationDetail;
    emitTerminal?.();
    await new Promise<void>((resolve) => setTimeout(resolve, 0));

    expect(titleStore.conversations().map((conversation) => conversation.id)).toEqual(['first-title', 'second-title']);
    expect(titleStore.conversations().find((conversation) => conversation.id === second.id)?.title).toBe('Generated second title');
  });
  it('loads a generated title when a persisted conversation is opened directly', async () => {
    const detail = { ...summary('generated-route'), title: 'Generated observatory topic', messages: [], recentRuns: [], currentRun: null } as ConversationDetail;
    const routedStore = new ConversationStore(fakeApi(async () => ({ items: [], nextCursor: null }), [detail]));
    await new Promise<void>((resolve) => setTimeout(resolve, 0));

    const selection = await routedStore.selectFromRoute('generated-route');

    expect(selection.status).toBe('selected');
    expect(routedStore.selected().title).toBe('Generated observatory topic');
    expect(routedStore.conversations().find((item) => item.id === 'generated-route')?.title).toBe('Generated observatory topic');
  });
  it('preserves conversation order when opening and refreshing a route', async () => {
    const first = { ...summary('first'), title: 'First conversation', messages: [], recentRuns: [], currentRun: null } as ConversationDetail;
    const second = { ...summary('second'), title: 'Second conversation', messages: [], recentRuns: [], currentRun: null } as ConversationDetail;
    const routedStore = new ConversationStore(fakeApi(async () => ({ items: [first, second], nextCursor: null }), [first, second]));
    await new Promise<void>((resolve) => setTimeout(resolve, 0));

    await routedStore.selectFromRoute('second');

    expect(routedStore.conversations().map((conversation) => conversation.id)).toEqual(['first', 'second']);
    expect(routedStore.selectedId()).toBe('second');
  });
  it('promotes an existing conversation only after Core accepts a new user send', async () => {
    const first = { ...summary('first'), title: 'First conversation', messages: [], recentRuns: [], currentRun: null } as ConversationDetail;
    const second = { ...summary('second'), title: 'Second conversation', messages: [], recentRuns: [], currentRun: null } as ConversationDetail;
    const api = fakeApi(async () => ({ items: [first, second], nextCursor: null }), [first, second]);
    const orderedStore = new ConversationStore(api);
    await new Promise<void>((resolve) => setTimeout(resolve, 0));
    orderedStore.select('second');
    await new Promise<void>((resolve) => setTimeout(resolve, 0));
    orderedStore.updateDraft('Accepted user message');

    expect(orderedStore.send()).toBe(true);
    expect(orderedStore.conversations().map((conversation) => conversation.id)).toEqual(['first', 'second']);
    await new Promise<void>((resolve) => setTimeout(resolve, 0));

    expect(orderedStore.conversations().map((conversation) => conversation.id)).toEqual(['second', 'first']);
  });
  it('preserves conversation order when Core rejects a user send', async () => {
    const first = { ...summary('first'), title: 'First conversation', messages: [], recentRuns: [], currentRun: null } as ConversationDetail;
    const second = { ...summary('second'), title: 'Second conversation', messages: [], recentRuns: [], currentRun: null } as ConversationDetail;
    const api = fakeApi(async () => ({ items: [first, second], nextCursor: null }), [first, second]);
    api.createRun = async () => { throw { status: 503, message: 'unavailable', retryable: true }; };
    const rejectedStore = new ConversationStore(api);
    await new Promise<void>((resolve) => setTimeout(resolve, 0));
    rejectedStore.select('second');
    await new Promise<void>((resolve) => setTimeout(resolve, 0));
    rejectedStore.updateDraft('Rejected user message');

    expect(rejectedStore.send()).toBe(true);
    await new Promise<void>((resolve) => setTimeout(resolve, 0));

    expect(rejectedStore.conversations().map((conversation) => conversation.id)).toEqual(['first', 'second']);
  });
  it('omits an agent identifier for an unconfigured draft', async () => {
    let selectedAgentRevision: string | undefined = 'unexpected';
    const api = fakeApi(undefined, [], [], undefined, undefined, undefined, async (message, modelId, _idempotencyKey, agentRevisionId) => {
      selectedAgentRevision = agentRevisionId;
      return fakeApi().createConversation(message, modelId, 'test');
    });
    const draftStore = new ConversationStore(api);
    await new Promise<void>((resolve) => setTimeout(resolve, 0));
    draftStore.updateDraft('Use the seeded default');
    draftStore.send();
    await new Promise<void>((resolve) => queueMicrotask(resolve));
    expect(selectedAgentRevision).toBeUndefined();
  });
  it('sends an explicitly selected UUID agent revision for a draft', async () => {
    let selectedAgentRevision: string | undefined;
    const api = fakeApi(undefined, [], [], undefined, undefined, undefined, async (message, modelId, _idempotencyKey, agentRevisionId) => {
      selectedAgentRevision = agentRevisionId;
      return fakeApi().createConversation(message, modelId, 'test');
    });
    const draftStore = new ConversationStore(api);
    await new Promise<void>((resolve) => setTimeout(resolve, 0));
    draftStore.requestAgent({ profileId: '4d8b3df7-7ea0-4b31-95c7-4de2dcb5c80a', revisionId: '89a7ab75-124f-4f33-b0ea-85bcfb04d201', revision: 2, displayName: 'Researcher', status: 'active' });
    await draftStore.confirmAgentSwitch();
    draftStore.updateDraft('Use this explicit revision');
    draftStore.send();
    await new Promise<void>((resolve) => queueMicrotask(resolve));
    expect(selectedAgentRevision).toBe('89a7ab75-124f-4f33-b0ea-85bcfb04d201');
  });
  it('sends an explicitly selected persona revision for a draft', async () => {
    let selectedPersonaRevision: string | undefined;
    const api = fakeApi(undefined, [], [], undefined, undefined, undefined, async (message, modelId, _idempotencyKey, _agentRevisionId, personaRevisionId) => {
      selectedPersonaRevision = personaRevisionId;
      return fakeApi().createConversation(message, modelId, 'test');
    });
    const draftStore = new ConversationStore(api);
    await new Promise<void>((resolve) => setTimeout(resolve, 0));
    draftStore.requestPersona({ profileId: 'persona-profile', revisionId: 'persona-revision-2', revision: 2, displayName: 'Researcher', status: 'active', newerRevisionAvailable: false });
    draftStore.updateDraft('Use this persona');
    draftStore.send();
    await new Promise<void>((resolve) => queueMicrotask(resolve));
    expect(selectedPersonaRevision).toBe('persona-revision-2');
  });
  it('applies draft agent and persona changes immediately without confirmation', async () => {
    const draftStore = new ConversationStore(fakeApi());
    await new Promise<void>((resolve) => setTimeout(resolve, 0));
    draftStore.requestAgent({ profileId: 'agent-profile', revisionId: 'agent-revision-2', revision: 2, displayName: 'Researcher', status: 'active' });
    draftStore.requestPersona({ profileId: 'persona-profile', revisionId: 'persona-revision-2', revision: 2, displayName: 'Researcher', status: 'active', newerRevisionAvailable: false });
    expect(draftStore.pendingAgent()).toBeNull();
    expect(draftStore.pendingPersona()).toBeNull();
    expect(draftStore.selectedAgent().revisionId).toBe('agent-revision-2');
    expect(draftStore.selectedPersona()?.revisionId).toBe('persona-revision-2');
    draftStore.requestUseAgentDefaultPersona();
    expect(draftStore.selected().personaOverride).toBe(false);
  });
  it('stages agent and persona together and submits one atomic configuration update', async () => {
    const now = new Date().toISOString();
    const currentAgent = { profileId: 'agent-profile', revisionId: 'agent-revision-1', revision: 1, displayName: 'Aura', status: 'active' as const };
    const nextAgent = { ...currentAgent, revisionId: 'agent-revision-2', revision: 2, displayName: 'Researcher' };
    const currentPersona = { profileId: 'persona-profile', revisionId: 'persona-revision-1', revision: 1, displayName: 'Neutral', status: 'active' as const, newerRevisionAvailable: false };
    const nextPersona = { ...currentPersona, revisionId: 'persona-revision-2', revision: 2, displayName: 'Focused' };
    const detail = { ...summary('combined-configuration'), agent: currentAgent, persona: currentPersona, personaOverride: false, agentAssignments: [], personaAssignments: [], messages: [], recentRuns: [], currentRun: null } as ConversationDetail;
    const api = fakeApi(async () => ({ items: [detail], nextCursor: null }), [detail]);
    let updateCalls = 0;
    let updateArgs: unknown[] = [];
    api.updateConversation = async (...args) => { updateCalls += 1; updateArgs = args; return { ...detail, agent: nextAgent, persona: nextPersona, personaOverride: true, version: 2, updatedAt: now }; };
    const combinedStore = new ConversationStore(api);
    await new Promise<void>((resolve) => setTimeout(resolve, 0));
    combinedStore.stageConfiguration(nextAgent, nextPersona, false);
    expect(combinedStore.pendingConfiguration()?.agentChanged).toBe(true);
    expect(combinedStore.pendingConfiguration()?.personaChanged).toBe(true);
    await combinedStore.confirmConfigurationChange();
    expect(updateCalls).toBe(1);
    expect(updateArgs[4]).toBe(nextAgent.revisionId);
    expect(updateArgs[6]).toBe(nextPersona.revisionId);
    expect(updateArgs[5]).toBe(true);
  });
  it('cancels configuration staged for conversation A when selecting conversation B', async () => {
    const first = { ...summary('conversation-a'), agent: { profileId: 'agent-a', revisionId: 'agent-a-r1', revision: 1, displayName: 'Aura', status: 'active' as const }, messages: [], recentRuns: [], currentRun: null } as ConversationDetail;
    const second = { ...summary('conversation-b'), agent: { profileId: 'agent-b', revisionId: 'agent-b-r1', revision: 1, displayName: 'Researcher', status: 'active' as const }, messages: [], recentRuns: [], currentRun: null } as ConversationDetail;
    const firstAgent = first.agent;
    if (!firstAgent) throw new Error('Expected an agent.');
    const api = fakeApi(async () => ({ items: [first, second], nextCursor: null }), [first, second]);
    let updateCalls = 0;
    api.updateConversation = async (...args) => { updateCalls += 1; return args as never; };
    const selectionStore = new ConversationStore(api);
    await new Promise<void>((resolve) => setTimeout(resolve, 0));
    selectionStore.stageConfiguration({ ...firstAgent, revisionId: 'agent-a-r2', revision: 2, displayName: 'Aura Updated' }, null, true);
    expect(selectionStore.pendingConfiguration()?.conversationId).toBe('conversation-a');
    selectionStore.select('conversation-b');
    await selectionStore.confirmConfigurationChange();
    expect(selectionStore.pendingConfiguration()).toBeNull();
    expect(updateCalls).toBe(0);
    expect(selectionStore.notice()).toBeNull();
  });
  it('rejects a pending configuration after the origin conversation version changes', async () => {
    const detail = { ...summary('versioned-configuration'), agent: { profileId: 'agent-profile', revisionId: 'agent-revision-1', revision: 1, displayName: 'Aura', status: 'active' as const }, messages: [], recentRuns: [], currentRun: null } as ConversationDetail;
    const detailAgent = detail.agent;
    if (!detailAgent) throw new Error('Expected an agent.');
    const api = fakeApi(async () => ({ items: [detail], nextCursor: null }), [detail]);
    let updateCalls = 0;
    api.updateConversation = async (...args) => { updateCalls += 1; return args as never; };
    const versionStore = new ConversationStore(api);
    await new Promise<void>((resolve) => setTimeout(resolve, 0));
    versionStore.stageConfiguration({ ...detailAgent, revisionId: 'agent-revision-2', revision: 2, displayName: 'Researcher' }, null, true);
    versionStore.conversations.update((items) => items.map((item) => item.id === detail.id ? { ...item, version: item.version + 1 } : item));
    await versionStore.confirmConfigurationChange();
    expect(versionStore.pendingConfiguration()).toBeNull();
    expect(updateCalls).toBe(0);
    expect(versionStore.notice()).toContain('changed while the configuration was waiting');
  });
  it('guards configuration and model changes while a run is active', async () => {
    const detail = persistedRecoveryDetail('failed');
    const running = { ...detail.recentRuns[0], status: 'running' as const, finishedAt: null };
    const activeDetail = { ...detail, currentRun: running, recentRuns: [running] } as ConversationDetail;
    const api = fakeApi(async () => ({ items: [activeDetail], nextCursor: null }), [activeDetail]);
    let updateCalls = 0;
    api.updateConversation = async (...args) => { updateCalls += 1; return args as never; };
    const activeStore = new ConversationStore(api);
    await new Promise<void>((resolve) => setTimeout(resolve, 0));
    activeStore.stageConfiguration(null, null, false);
    await activeStore.selectModel(preferredModel.id);
    expect(activeStore.pendingConfiguration()).toBeNull();
    expect(updateCalls).toBe(0);
    expect(activeStore.notice()).toContain('active run');
  });
  it('confirms a persisted persona override with transcript sharing', async () => {
    const now = new Date().toISOString();
    const persona = { profileId: 'persona-profile', revisionId: 'persona-revision-1', revision: 1, displayName: 'Neutral', status: 'active' as const, newerRevisionAvailable: false };
    const detail = { ...summary('persona-conversation'), persona, personaOverride: false, personaAssignments: [], messages: [], recentRuns: [], currentRun: null } as ConversationDetail;
    let updateArgs: unknown[] = [];
    const api = fakeApi(async () => ({ items: [detail], nextCursor: null }), [detail]);
    api.updateConversation = async (...args) => { updateArgs = args; return { ...detail, persona, personaOverride: true, updatedAt: now }; };
    const personaStore = new ConversationStore(api);
    await new Promise<void>((resolve) => setTimeout(resolve, 0));
    personaStore.requestPersona({ ...persona, revisionId: 'persona-revision-2', revision: 2, displayName: 'Researcher' });
    await personaStore.confirmPersonaChange();
    expect(updateArgs[5]).toBe(true);
    expect(updateArgs[6]).toBe('persona-revision-2');
    expect(personaStore.selected().personaOverride).toBe(true);
  });
  it.each(['failed', 'interrupted'] as const)('preserves %s retry recovery when applying a persona override', async (status) => {
    const detail = { ...persistedRecoveryDetail(status), persona: { profileId: 'persona-profile', revisionId: 'persona-revision-1', revision: 1, displayName: 'Neutral', status: 'active' as const, newerRevisionAvailable: false }, personaOverride: false, personaAssignments: [] } as ConversationDetail;
    const originalRun = detail.recentRuns[0];
    if (!originalRun) throw new Error('Expected a retryable run.');
    const originalPersona = detail.persona;
    if (!originalPersona) throw new Error('Expected a persona.');
    const api = fakeApi(async () => ({ items: [detail], nextCursor: null }), [detail]);
    let retriedRunId = '';
    api.updateConversation = async () => ({ ...detail, currentRun: null, personaOverride: true, persona: { ...originalPersona, revisionId: 'persona-revision-2', revision: 2, displayName: 'Researcher' } });
    api.retryRun = async (runId) => { retriedRunId = runId; return { conversation: detail, userMessage: detail.messages[0] ?? { id: 'message', conversationId: detail.id, role: 'user' as const, content: 'retry', state: 'complete' as const, runId, createdAt: new Date().toISOString(), updatedAt: new Date().toISOString() }, run: originalRun }; };
    const recoveryStore = new ConversationStore(api);
    await new Promise<void>((resolve) => setTimeout(resolve, 0));
    recoveryStore.requestPersona({ profileId: 'persona-profile', revisionId: 'persona-revision-2', revision: 2, displayName: 'Researcher', status: 'active', newerRevisionAvailable: false });
    await recoveryStore.confirmPersonaChange();

    expect(recoveryStore.selected().retryableRun?.id).toBe(originalRun.id);
    expect(recoveryStore.selected().runs.map((run) => run.id)).toContain(originalRun.id);
    recoveryStore.retry();
    await new Promise<void>((resolve) => queueMicrotask(resolve));
    expect(retriedRunId).toBe(originalRun.id);
  });
  it.each(['failed', 'interrupted'] as const)('preserves %s retry recovery when resetting to the agent default persona', async (status) => {
    const detail = { ...persistedRecoveryDetail(status), persona: { profileId: 'persona-profile', revisionId: 'persona-revision-2', revision: 2, displayName: 'Researcher', status: 'active' as const, newerRevisionAvailable: false }, personaOverride: true, personaAssignments: [] } as ConversationDetail;
    const originalRun = detail.recentRuns[0];
    if (!originalRun) throw new Error('Expected a retryable run.');
    const originalPersona = detail.persona;
    if (!originalPersona) throw new Error('Expected a persona.');
    const api = fakeApi(async () => ({ items: [detail], nextCursor: null }), [detail]);
    let retriedRunId = '';
    api.updateConversation = async () => ({ ...detail, currentRun: null, personaOverride: false, persona: { ...originalPersona, revisionId: 'persona-revision-1', revision: 1, displayName: 'Neutral' } });
    api.retryRun = async (runId) => { retriedRunId = runId; return { conversation: detail, userMessage: detail.messages[0] ?? { id: 'message', conversationId: detail.id, role: 'user' as const, content: 'retry', state: 'complete' as const, runId, createdAt: new Date().toISOString(), updatedAt: new Date().toISOString() }, run: originalRun }; };
    const recoveryStore = new ConversationStore(api);
    await new Promise<void>((resolve) => setTimeout(resolve, 0));
    recoveryStore.requestUseAgentDefaultPersona();
    await recoveryStore.confirmPersonaChange();

    expect(recoveryStore.selected().retryableRun?.id).toBe(originalRun.id);
    recoveryStore.retry();
    await new Promise<void>((resolve) => queueMicrotask(resolve));
    expect(retriedRunId).toBe(originalRun.id);
  });
  it('uses assignment boundaries and run provenance for historical metadata', async () => {
    const now = new Date().toISOString();
    const aura = { profileId: 'aura-profile', revisionId: 'aura-revision-1', revision: 1, displayName: 'Aura', status: 'active' as const, newerRevisionAvailable: false };
    const researcher = { profileId: 'research-profile', revisionId: 'research-revision-1', revision: 1, displayName: 'Researcher', status: 'active' as const, newerRevisionAvailable: false };
    const run: Run = { id: 'history-run', conversationId: 'history', userMessageId: 'message-one', assistantMessageId: 'message-two', status: 'completed', agentRevisionId: researcher.revisionId, modelPolicyRevisionId: 'policy-1', provider: 'ollama', modelId: model.id, retryOfRunId: null, createdAt: now, startedAt: now, finishedAt: now, error: null };
    const detail = { ...summary('history'), agent: researcher, agentAssignments: [{ id: 'assignment-initial', agent: aura, reason: 'initial' as const, effectiveAfterMessageId: null, createdAt: now }, { id: 'assignment-switch', agent: researcher, reason: 'manual_switch' as const, effectiveAfterMessageId: 'message-one', createdAt: now }], messages: [{ id: 'message-one', conversationId: 'history', role: 'user' as const, content: 'Question', state: 'complete' as const, runId: null, createdAt: now, updatedAt: now }, { id: 'message-two', conversationId: 'history', role: 'assistant' as const, content: 'Answer', state: 'complete' as const, runId: run.id, createdAt: now, updatedAt: now }], recentRuns: [run], currentRun: null } as ConversationDetail;
    const historyStore = new ConversationStore(fakeApi(async () => ({ items: [detail], nextCursor: null }), [detail], [run]));
    await new Promise<void>((resolve) => setTimeout(resolve, 0));
    expect(historyStore.agentForTurn(historyStore.selected().turns[0]).displayName).toBe('Aura');
    expect(historyStore.agentForTurn(historyStore.selected().turns[1]).displayName).toBe('Researcher');
    expect(historyStore.transitionMarkerFor('message-one')).toContain('Aura r1 to Researcher r1');
    expect(historyStore.transitionMarkerFor('message-two')).toBeNull();
  });
  it('reuses the transient draft without stopping its background run', () => { store.updateDraft('Keep this running'); store.send(); const draftId = store.selectedId(); store.create(); expect(store.selectedId()).toBe(draftId); expect(store.selected().turns[0].text).toBe('Keep this running'); });
  it('preserves user work and sends an idempotent cancellation command', async () => { store.updateDraft('Keep this visible'); store.send(); await new Promise<void>((resolve) => queueMicrotask(resolve)); store.stop(); await new Promise<void>((resolve) => queueMicrotask(resolve)); expect(store.selected().turns[0].text).toBe('Keep this visible'); expect(store.runState()).toBe('interrupted'); expect(store.notice()).toContain('stopped'); });
  it('consumes the complete accepted response when retrying a run', async () => { store.updateDraft('Retry this safely'); store.send(); await new Promise<void>((resolve) => queueMicrotask(resolve)); store.stop(); await new Promise<void>((resolve) => queueMicrotask(resolve)); store.retry(); await new Promise<void>((resolve) => queueMicrotask(resolve)); expect(store.selected().currentRun?.status).toBe('running'); expect(store.selected().currentRun?.agentRevisionId).toBe('agent-rev-1'); expect(store.selected().currentRun?.modelPolicyRevisionId).toBe('policy-1'); expect(store.selected().currentRun?.provider).toBe('ollama'); });
  it('keeps a retried conversation in place through acceptance and completion', async () => {
    const now = new Date().toISOString();
    const first = { ...summary('first-retry'), title: 'First conversation', messages: [], recentRuns: [], currentRun: null } as ConversationDetail;
    const failed: Run = { id: 'run-second-retry-failed', conversationId: 'second-retry', userMessageId: 'user-second-retry', assistantMessageId: null, status: 'failed', agentRevisionId: 'agent-rev-1', modelPolicyRevisionId: 'policy-1', provider: 'ollama', modelId: model.id, retryOfRunId: null, createdAt: now, startedAt: now, finishedAt: now, error: { code: 'MODEL_FAILED', message: 'The model failed.', retryable: true, traceId: 'trace' } };
    const originalUser = { id: failed.userMessageId, conversationId: failed.conversationId, role: 'user' as const, content: 'Retry this conversation', state: 'complete' as const, runId: failed.id, createdAt: now, updatedAt: now };
    let second = { ...summary('second-retry'), title: 'Second conversation', currentRun: null, messages: [originalUser], recentRuns: [failed] } as ConversationDetail;
    let emitCompletion: (() => void) | undefined;
    const api = fakeApi(
      async () => ({ items: [first, second], nextCursor: null }),
      [first, second],
      [failed],
      (runId, _lastEventId, onEvent) => {
        emitCompletion = () => onEvent({ schemaVersion: 1, eventId: 'second-retry-complete', sequence: 1, eventType: 'run.status', runId, conversationId: second.id, occurredAt: now, data: { status: 'completed', startedAt: now, finishedAt: now } } as RunEvent, 'second-retry-complete');
        return { close: () => undefined };
      },
      async (id) => id === second.id ? second : first,
    );
    const retriedRun: Run = { id: 'run-second-retry-accepted', conversationId: second.id, userMessageId: 'user-second-retry', assistantMessageId: null, status: 'running', agentRevisionId: 'agent-rev-1', modelPolicyRevisionId: 'policy-1', provider: 'ollama', modelId: model.id, retryOfRunId: failed.id, createdAt: now, startedAt: now, finishedAt: null, error: null };
    api.retryRun = async (runId) => {
      expect(runId).toBe(failed.id);
      second = { ...second, currentRun: retriedRun, recentRuns: [failed, retriedRun] };
      return { conversation: second, userMessage: originalUser, run: retriedRun };
    };
    const retryStore = new ConversationStore(api);
    await new Promise<void>((resolve) => setTimeout(resolve, 0));
    retryStore.select(second.id);
    await new Promise<void>((resolve) => setTimeout(resolve, 0));
    retryStore.retry();
    await new Promise<void>((resolve) => setTimeout(resolve, 0));

    expect(retryStore.conversations().map((conversation) => conversation.id)).toEqual(['first-retry', 'second-retry']);
    const completed = { ...retriedRun, status: 'completed' as const, finishedAt: now, assistantMessageId: 'assistant-second-retry' };
    second = { ...second, currentRun: null, recentRuns: [failed, completed], messages: [originalUser, { id: 'assistant-second-retry', conversationId: second.id, role: 'assistant', content: 'Completed retry', state: 'complete', runId: completed.id, createdAt: now, updatedAt: now }] } as ConversationDetail;
    emitCompletion?.();
    await new Promise<void>((resolve) => setTimeout(resolve, 0));

    expect(retryStore.conversations().map((conversation) => conversation.id)).toEqual(['first-retry', 'second-retry']);
  });
  it('updates model selection for future runs', async () => { await store.selectModel('qwen2.5:7b'); expect(store.selectedModelId()).toBe('qwen2.5:7b'); store.updateDraft('Choose a model'); expect(store.send()).toBe(true); });
  it('submits the selected model when a draft becomes a persisted conversation', async () => {
    let submittedModelId = '';
    const api = fakeApi(undefined, [], [], undefined, undefined, undefined, async (message, modelId) => {
      submittedModelId = modelId;
      return fakeApi().createConversation(message, modelId, 'selected-model');
    });
    const modelStore = new ConversationStore(api);
    await new Promise<void>((resolve) => setTimeout(resolve, 0));
    await modelStore.selectModel(model.id);
    modelStore.updateDraft('Persist this model choice');

    expect(modelStore.selectedModelId()).toBe(model.id);
    expect(modelStore.send()).toBe(true);
    await new Promise<void>((resolve) => setTimeout(resolve, 0));

    expect(submittedModelId).toBe(model.id);
  });
  it('loads older conversation pages only after an explicit request', async () => {
    const cursors: Array<string | undefined> = [];
    const totalPages = 27;
    const pagedApi = fakeApi(async (cursor) => {
      cursors.push(cursor);
      const pageNumber = cursor ? Number(cursor.replace('page-', '')) : 0;
      return { items: [summary(pageNumber === 0 ? 'newest' : `older-${pageNumber}`)], nextCursor: pageNumber + 1 < totalPages ? `page-${pageNumber + 1}` : null };
    });
    const pagedStore = new ConversationStore(pagedApi);
    await new Promise<void>((resolve) => setTimeout(resolve, 0));

    expect(cursors).toEqual([undefined]);
    expect(pagedStore.conversations()).toHaveLength(1);
    while (pagedStore.hasMoreConversations()) await pagedStore.loadMoreConversations();
    expect(cursors).toHaveLength(totalPages);
    expect(pagedStore.conversations()).toHaveLength(totalPages);
    expect(pagedStore.conversations().at(-1)?.id).toBe('older-26');
  });
  it('deduplicates repeated items when a Load more cursor is replayed', async () => {
    const cycleApi = fakeApi(async (cursor) => ({ items: [summary(cursor ? 'cycle-older' : 'cycle-newest')], nextCursor: 'cycle' }));
    const cycleStore = new ConversationStore(cycleApi);
    await new Promise<void>((resolve) => setTimeout(resolve, 0));

    expect(cycleStore.conversations().map((conversation) => conversation.id)).toEqual(['cycle-newest']);
    await cycleStore.loadMoreConversations();
    await cycleStore.loadMoreConversations();
    expect(cycleStore.conversations().map((conversation) => conversation.id)).toEqual(['cycle-newest', 'cycle-older']);
  });
  it.each(['canceled', 'failed'] as const)('retains a persisted %s run for retry after refresh', async (status) => {
    const detail = persistedRecoveryDetail(status);
    const recoveryStore = new ConversationStore(fakeApi(async () => ({ items: [detail], nextCursor: null }), [detail], detail.recentRuns));
    await new Promise<void>((resolve) => setTimeout(resolve, 0));

    expect(recoveryStore.selected().retryableRun?.id).toBe(detail.recentRuns[0].id);
    expect(recoveryStore.runState()).toBe(status === 'failed' ? 'error' : 'interrupted');
    recoveryStore.retry();
    await new Promise<void>((resolve) => queueMicrotask(resolve));
    expect(recoveryStore.selected().currentRun?.status).toBe('running');
  });
  it('reconciles and resumes an active run after a multi-second API and stream outage', async () => {
    vi.useFakeTimers();
    const now = new Date().toISOString();
    const run: Run = { id: 'run-reconnect', conversationId: 'conversation-reconnect', userMessageId: 'user-reconnect', assistantMessageId: null, status: 'running', agentRevisionId: 'agent-rev-1', modelPolicyRevisionId: 'policy-1', provider: 'ollama', modelId: model.id, retryOfRunId: null, createdAt: now, startedAt: now, finishedAt: null, error: null };
    const detail: ConversationDetail = { ...summary(run.conversationId), currentRun: run, messages: [], recentRuns: [run] };
    let streamAttempts = 0;
    let detailReads = 0;
    const api = fakeApi(async () => ({ items: [{ ...summary(run.conversationId), currentRun: null }], nextCursor: null }), [detail], [run], (id, _last, onEvent, onError) => {
      streamAttempts += 1;
      queueMicrotask(() => {
        if (streamAttempts < 4) onError({ message: 'Aura is temporarily offline.', retryable: true });
        else onEvent({ schemaVersion: 1, eventId: `event-${streamAttempts}`, sequence: 1, eventType: 'assistant.delta', runId: id, conversationId: run.conversationId, occurredAt: now, data: { messageId: 'assistant-reconnect', offset: 0, text: 'recovered' } }, `cursor-${streamAttempts}`);
      });
      return { close: () => undefined };
    }, async () => {
      detailReads += 1;
      if (detailReads === 2) throw new Error('Aura API restarting');
      return detail;
    });
    const reconnectStore = new ConversationStore(api);
    await vi.runAllTicks();
    await vi.advanceTimersByTimeAsync(500);
    await vi.advanceTimersByTimeAsync(1000);
    await vi.advanceTimersByTimeAsync(2000);
    await vi.advanceTimersByTimeAsync(4000);
    await vi.runAllTicks();

    expect(streamAttempts).toBe(4);
    expect(detailReads).toBe(5);
    expect(reconnectStore.selected().currentRun?.status).toBe('running');
    expect(reconnectStore.selected().turns.some((turn) => turn.text === 'recovered')).toBe(true);
    expect(reconnectStore.runState()).toBe('working');
    vi.useRealTimers();
  });
  it('clears an expired SSE cursor and recovers from a fresh snapshot', async () => {
    vi.useFakeTimers();
    const now = new Date().toISOString();
    const run: Run = { id: 'run-expired-cursor', conversationId: 'conversation-expired-cursor', userMessageId: 'user-expired-cursor', assistantMessageId: null, status: 'running', agentRevisionId: 'agent-rev-1', modelPolicyRevisionId: 'policy-1', provider: 'ollama', modelId: model.id, retryOfRunId: null, createdAt: now, startedAt: now, finishedAt: null, error: null };
    const detail: ConversationDetail = { ...summary(run.conversationId), currentRun: run, messages: [], recentRuns: [run] };
    const cursors: Array<string | undefined> = [];
    const api = fakeApi(async () => ({ items: [{ ...summary(run.conversationId), currentRun: null }], nextCursor: null }), [detail], [run], (id, last, onEvent, onError) => {
      cursors.push(last);
      queueMicrotask(() => {
        if (cursors.length === 1) {
          onEvent({ schemaVersion: 1, eventId: 'old-event', sequence: 1, eventType: 'assistant.delta', runId: id, conversationId: run.conversationId, occurredAt: now, data: { messageId: 'assistant-expired', offset: 0, text: 'partial' } }, 'expired-cursor');
          onError({ message: 'The event cursor has expired.', status: 410, retryable: true });
        } else {
          onEvent({ schemaVersion: 1, eventId: 'fresh-event', sequence: 1, eventType: 'run.snapshot', runId: id, conversationId: run.conversationId, occurredAt: now, data: { run, assistantMessage: null } }, 'fresh-cursor');
        }
      });
      return { close: () => undefined };
    });
    const recoveryStore = new ConversationStore(api);
    await vi.runAllTicks();
    await vi.advanceTimersByTimeAsync(500);
    await vi.runAllTicks();

    expect(cursors).toEqual([undefined, undefined]);
    expect(recoveryStore.selected().currentRun?.id).toBe(run.id);
    expect(recoveryStore.runState()).toBe('working');
    vi.useRealTimers();
  });
  it('keeps library pagination explicit and preserves selection and drafts while loading more', async () => {
    const first = { ...summary('library-first'), title: 'First library item', messages: [], recentRuns: [], currentRun: null } as ConversationDetail;
    const second = { ...summary('library-second'), title: 'Second library item', messages: [], recentRuns: [], currentRun: null } as ConversationDetail;
    const third = { ...summary('library-third'), title: 'Third library item', messages: [], recentRuns: [], currentRun: null } as ConversationDetail;
    const cursors: Array<string | undefined> = [];
    const api = fakeApi(async (cursor) => {
      cursors.push(cursor);
      return cursor ? { items: [third], nextCursor: null } : { items: [first, second], nextCursor: 'library-cursor' };
    }, [first, second, third]);
    const libraryStore = new ConversationStore(api);
    await new Promise<void>((resolve) => setTimeout(resolve, 0));
    libraryStore.select(second.id);
    await new Promise<void>((resolve) => setTimeout(resolve, 0));
    libraryStore.updateDraft('Keep this draft while loading another page');

    expect(cursors).toEqual([undefined]);
    expect(libraryStore.hasMoreConversations()).toBe(true);
    await libraryStore.loadMoreConversations();

    expect(cursors).toEqual([undefined, 'library-cursor']);
    expect(libraryStore.conversations().map((item) => item.id)).toEqual([first.id, second.id, third.id]);
    expect(libraryStore.selectedId()).toBe(second.id);
    expect(libraryStore.draft()).toBe('Keep this draft while loading another page');
    expect(libraryStore.hasMoreConversations()).toBe(false);
  });
  it('debounces and normalizes session-only library filters without changing selection', async () => {
    const item = { ...summary('filter-item'), title: 'Filter item', messages: [], recentRuns: [], currentRun: null } as ConversationDetail;
    const filters: Array<Record<string, unknown> | undefined> = [];
    const api = fakeApi(async (_cursor, requestedFilters) => {
      filters.push(requestedFilters as Record<string, unknown> | undefined);
      return { items: [item], nextCursor: null };
    }, [item]);
    const filterStore = new ConversationStore(api);
    await new Promise<void>((resolve) => setTimeout(resolve, 0));
    filterStore.updateDraft('A draft must survive filter refresh');
    filterStore.setLibraryFilters({ q: '  title fragment  ', archiveState: 'archived', activityFrom: '2026-10-01T00:00:00Z' });

    expect(filters).toHaveLength(1);
    await new Promise<void>((resolve) => setTimeout(resolve, 275));

    expect(filters).toHaveLength(2);
    expect(filters[1]).toEqual({ q: 'title fragment', archiveState: 'archived', activityFrom: '2026-10-01T00:00:00Z' });
    expect(filterStore.libraryFilters()).toEqual(filters[1]);
    expect(filterStore.selectedId()).toBe(item.id);
    expect(filterStore.draft()).toBe('A draft must survive filter refresh');
  });
  it('retains a selected conversation when a new filter excludes it from the library page', async () => {
    const first = { ...summary('filter-first'), title: 'First result', messages: [], recentRuns: [], currentRun: null } as ConversationDetail;
    const second = { ...summary('filter-second'), title: 'Second result', messages: [], recentRuns: [], currentRun: null } as ConversationDetail;
    const api = fakeApi(async (_cursor, requestedFilters) => {
      const q = requestedFilters?.q?.toLocaleLowerCase() ?? '';
      return { items: [first, second].filter((item) => item.title.toLocaleLowerCase().includes(q)), nextCursor: null };
    }, [first, second]);
    const filterStore = new ConversationStore(api);
    await new Promise<void>((resolve) => setTimeout(resolve, 0));
    filterStore.select(second.id);
    await new Promise<void>((resolve) => setTimeout(resolve, 0));
    filterStore.updateDraft('Keep selected draft visible while filtered');
    filterStore.setLibraryFilters({ q: 'first' });
    await new Promise<void>((resolve) => setTimeout(resolve, 275));

    expect(filterStore.selectedId()).toBe(second.id);
    expect(filterStore.draft()).toBe('Keep selected draft visible while filtered');
  });
  it('keeps rejected archive and rename mutations non-destructive to the current draft', async () => {
    const item = { ...summary('rejected-metadata'), title: 'Original title', messages: [], recentRuns: [], currentRun: null } as ConversationDetail;
    const api = fakeApi(async () => ({ items: [item], nextCursor: null }), [item]);
    api.updateConversationMetadata = async () => { throw { status: 409, message: 'The conversation changed while you were editing.', retryable: false }; };
    const mutationStore = new ConversationStore(api);
    await new Promise<void>((resolve) => setTimeout(resolve, 0));
    mutationStore.updateDraft('Draft remains after rejected metadata');

    await expect(mutationStore.renameConversation(item.id, 'Rejected title')).resolves.toBe(false);
    expect(mutationStore.selectedId()).toBe(item.id);
    expect(mutationStore.draft()).toBe('Draft remains after rejected metadata');
    expect(mutationStore.selected().title).toBe('Original title');
    expect(mutationStore.notice()).toContain('changed');
  });
  it('makes archived conversations read-only and restores them without losing selection', async () => {
    const archived = { ...summary('archived-library-item'), title: 'Archived item', archivedAt: '2026-10-05T12:00:00Z', messages: [], recentRuns: [], currentRun: null } as ConversationDetail;
    const api = fakeApi(async () => ({ items: [archived], nextCursor: null }), [archived]);
    let metadataCalls = 0;
    api.updateConversationMetadata = async (id, metadata) => { metadataCalls += 1; return { ...summary(id), title: archived.title, archivedAt: metadata.archived === false ? null : archived.archivedAt, version: archived.version + 1 }; };
    const archivedStore = new ConversationStore(api);
    await new Promise<void>((resolve) => setTimeout(resolve, 0));
    archivedStore.updateDraft('Draft on archived conversation');

    expect(archivedStore.selected().archivedAt).toBeTruthy();
    expect(archivedStore.send()).toBe(false);
    expect(archivedStore.draft()).toBe('Draft on archived conversation');
    await expect(archivedStore.renameConversation(archived.id, 'Should not rename')).resolves.toBe(false);
    expect(metadataCalls).toBe(0);
    expect(archivedStore.selected().title).toBe('Archived item');
    await expect(archivedStore.restoreConversation(archived.id)).resolves.toBe(true);
    expect(metadataCalls).toBe(1);
    expect(archivedStore.selected().archivedAt).toBeNull();
    expect(archivedStore.selectedId()).toBe(archived.id);
    expect(archivedStore.draft()).toBe('Draft on archived conversation');
  });
  it('ignores stale filter responses after a newer filter request starts', async () => {
    const initial = { ...summary('initial-filter-item'), title: 'Initial', messages: [], recentRuns: [], currentRun: null } as ConversationDetail;
    const latest = { ...summary('latest-filter-item'), title: 'Latest', messages: [], recentRuns: [], currentRun: null } as ConversationDetail;
    let requestCount = 0;
    let resolveStale: ((page: { items: ReadonlyArray<ConversationSummary>; nextCursor: string | null }) => void) | undefined;
    const stalePage = new Promise<{ items: ReadonlyArray<ConversationSummary>; nextCursor: string | null }>((resolve) => { resolveStale = resolve; });
    const api = fakeApi(async (_cursor, filters) => {
      requestCount += 1;
      if (requestCount === 2) return stalePage;
      return { items: filters?.q === 'latest' ? [latest] : [initial], nextCursor: null };
    }, [initial]);
    const filterStore = new ConversationStore(api);
    await new Promise<void>((resolve) => setTimeout(resolve, 0));
    filterStore.setLibraryFilters({ q: 'stale' });
    await new Promise<void>((resolve) => setTimeout(resolve, 275));
    filterStore.setLibraryFilters({ q: 'latest' });
    await new Promise<void>((resolve) => setTimeout(resolve, 275));
    expect(filterStore.conversations().some((item) => item.id === latest.id)).toBe(true);
    resolveStale?.({ items: [{ ...summary('stale-filter-item'), title: 'Stale', currentRun: null }], nextCursor: null });
    await new Promise<void>((resolve) => setTimeout(resolve, 0));
    expect(filterStore.conversations().some((item) => item.id === 'stale-filter-item')).toBe(false);
  });
  it('reconciles archive visibility while preserving an intentionally selected conversation', async () => {
    const first = { ...summary('archive-first'), title: 'First', messages: [], recentRuns: [], currentRun: null } as ConversationDetail;
    const second = { ...summary('archive-second'), title: 'Second', messages: [], recentRuns: [], currentRun: null } as ConversationDetail;
    const api = fakeApi(async () => ({ items: [first, second], nextCursor: 'cursor' }), [first, second]);
    api.updateConversationMetadata = async (id, metadata) => ({ ...summary(id), title: id === first.id ? first.title : second.title, archivedAt: metadata.archived ? '2026-10-06T12:00:00Z' : null, version: 2 });
    const archiveStore = new ConversationStore(api);
    await new Promise<void>((resolve) => setTimeout(resolve, 0));
    expect(archiveStore.selectedId()).toBe(first.id);
    await expect(archiveStore.archiveConversation(second.id)).resolves.toBe(true);
    expect(archiveStore.conversations().map((item) => item.id)).toEqual([first.id]);
    await expect(archiveStore.archiveConversation(first.id)).resolves.toBe(true);
    expect(archiveStore.conversations().map((item) => item.id)).toEqual([first.id]);
  });

  it('removes a restored nonselected row from the Archived view', async () => {
    const archived = { ...summary('restore-archived'), title: 'Archived', archivedAt: '2026-10-06T12:00:00Z', messages: [], recentRuns: [], currentRun: null } as ConversationDetail;
    const other = { ...summary('restore-other'), title: 'Other', archivedAt: '2026-10-06T11:00:00Z', messages: [], recentRuns: [], currentRun: null } as ConversationDetail;
    const api = fakeApi(async () => ({ items: [archived, other], nextCursor: null }), [archived, other]);
    api.updateConversationMetadata = async (id, metadata) => ({ ...summary(id), title: id === archived.id ? archived.title : other.title, archivedAt: metadata.archived === false ? null : '2026-10-06T12:00:00Z', version: 2 });
    const restoreStore = new ConversationStore(api);
    await new Promise<void>((resolve) => setTimeout(resolve, 0));
    restoreStore.setLibraryFilters({ archiveState: 'archived' });
    await new Promise<void>((resolve) => setTimeout(resolve, 10));
    restoreStore.select(other.id);
    await new Promise<void>((resolve) => setTimeout(resolve, 0));
    expect(restoreStore.conversations().map((item) => [item.id, item.archivedAt])).toEqual([[archived.id, archived.archivedAt], [other.id, other.archivedAt]]);
    await expect(restoreStore.restoreConversation(archived.id)).resolves.toBe(true);
    expect(restoreStore.conversations().map((item) => item.id)).toEqual([other.id]);
  });
  it('authoritatively replaces queued memory activity after reconnect reconciliation', async () => {
    const now = new Date().toISOString();
    const assistantMessageId = 'assistant-memory-reconcile';
    const run: Run = { id: 'run-memory-reconcile', conversationId: 'memory-reconcile', userMessageId: 'user-memory-reconcile', assistantMessageId, status: 'completed', agentRevisionId: 'agent-rev-1', modelPolicyRevisionId: 'policy-1', provider: 'ollama', modelId: model.id, retryOfRunId: null, createdAt: now, startedAt: now, finishedAt: now, error: null };
    const detail = { ...summary(run.conversationId), messages: [{ id: assistantMessageId, conversationId: run.conversationId, role: 'assistant' as const, content: 'Answer', state: 'complete' as const, runId: run.id, createdAt: now, updatedAt: now }], recentRuns: [run], currentRun: null } as ConversationDetail;
    const queued: MemoryActivity = { id: 'memory-activity-1', action: 'queued_for_review', status: 'queued', scope: { type: 'user' }, candidateId: 'candidate-1', memoryId: null, memoryRevisionId: null, policyRevisionId: null, embeddingGenerationId: null, reconciliationStatus: 'pending', occurredAt: now };
    const authoritative: MemoryActivity = { ...queued, action: 'created', status: 'completed', memoryId: 'memory-1', memoryRevisionId: 'revision-1', reconciliationStatus: 'authoritative' };
    const candidateActivity: MemoryActivity = { ...queued, id: 'memory-activity-candidate', memoryId: null, candidateId: 'candidate-2' };
    let snapshot: RunMemoryActivitySnapshot = { runId: run.id, processingStatus: 'queued', items: [queued], lastEventId: 'activity-1', reconciledAt: now };
    const api = fakeApi(async () => ({ items: [detail], nextCursor: null }), [detail], [run]);
    const assistantId = detail.messages[0]?.id;
    if (!assistantId) throw new Error('Expected an assistant message.');
    api.getRunMemoryActivity = async () => snapshot;
    const memoryReads: string[] = [];
    const candidateReads: string[] = [];
    api.getMemoryDetail = async (id) => { memoryReads.push(id); return { id, provenance: [{ type: 'run' }] } as never; };
    api.getMemoryCandidateDetail = async (id) => { candidateReads.push(id); return { id, runId: run.id, groundedMessageIds: ['message-1'] } as never; };
    const reconcileStore = new ConversationStore(api);
    await new Promise<void>((resolve) => setTimeout(resolve, 0));
    expect(reconcileStore.selected().turns[0]?.memoryActivities?.[0]?.status).toBe('queued');
    snapshot = { ...snapshot, processingStatus: 'settled', items: [authoritative], lastEventId: 'activity-2' };
    await reconcileStore.retryMemoryActivity(run.id);
    expect(reconcileStore.selected().turns[0]?.memoryActivities).toEqual([authoritative]);
    expect(reconcileStore.memoryActivityNotice()).toBeNull();
    await reconcileStore.loadMemoryPopup(assistantId);
    expect(memoryReads).toEqual(['memory-1']);
    expect(reconcileStore.memoryPopupRecord(authoritative.id)?.kind).toBe('memory');
    snapshot = { ...snapshot, items: [candidateActivity] };
    await reconcileStore.retryMemoryActivity(run.id);
    await reconcileStore.loadMemoryPopup(assistantId);
    expect(candidateReads).toEqual(['candidate-2']);
    expect(reconcileStore.memoryPopupRecord(candidateActivity.id)?.kind).toBe('candidate');
    api.getMemoryCandidateDetail = async () => { throw new Error('private candidate evidence'); };
    await reconcileStore.loadMemoryPopup(assistantId);
    expect(reconcileStore.memoryPopupError(candidateActivity.id)).toBe('Record details are temporarily unavailable.');
    api.getRunMemoryActivity = async () => { throw new Error('temporarily unavailable'); };
    await reconcileStore.retryMemoryActivity(run.id);
    expect(reconcileStore.memoryActivityNotice()).toContain('transcript is preserved');
  });
});
