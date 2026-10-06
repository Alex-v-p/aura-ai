import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { ConversationApi, RunEventSubscription } from './conversation-api';
import type { ConversationDetail, ConversationRunAccepted, ConversationSummary, ModelCatalog, Run, Session } from '@aura/aura-api-client';
import { ConversationStore } from './conversation-store';

const model = { id: 'qwen2.5:7b', displayName: 'Qwen 2.5', provider: 'ollama', capabilities: ['chat'] as const, availability: 'available' as const, selectable: true, disabledReason: null };
const preferredModel = { id: 'llama3.2:3b', displayName: 'Llama 3.2', provider: 'ollama', capabilities: ['chat'] as const, availability: 'available' as const, selectable: true, disabledReason: null };
const session: Session = { principal: { issuer: 'https://authentik.test', subject: 'owner' }, csrfToken: 'csrf', idleExpiresAt: '2026-10-05T20:00:00Z', absoluteExpiresAt: '2026-10-06T12:00:00Z' };

function fakeApi(listConversations: ConversationApi['listConversations'] = async () => ({ items: [], nextCursor: null }), initialDetails: ReadonlyArray<ConversationDetail> = [], initialRuns: ReadonlyArray<Run> = [], streamRunEvents?: ConversationApi['streamRunEvents'], getConversation?: ConversationApi['getConversation'], modelCatalog?: ModelCatalog, createConversationOverride?: ConversationApi['createConversation']): ConversationApi {
  let sequence = 0;
  const runs = new Map<string, Run>();
  const details = new Map<string, ConversationDetail>();
  for (const run of initialRuns) runs.set(run.id, run);
  for (const detail of initialDetails) details.set(detail.id, detail);
  const api: ConversationApi = {
    getSession: async () => session,
    startLogin: () => undefined,
    logout: async () => undefined,
    listModels: async (): Promise<ModelCatalog> => modelCatalog ?? ({ models: [model, preferredModel], defaultModelId: preferredModel.id, observedAt: new Date().toISOString() }),
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
  it('keeps independent drafts and does not stop a background run when selecting', () => { store.updateDraft('Keep this running'); store.send(); const workingId = store.selectedId(); store.create(); expect(store.selectedId()).not.toBe(workingId); store.select(workingId); expect(store.selected().turns[0].text).toBe('Keep this running'); });
  it('preserves user work and sends an idempotent cancellation command', async () => { store.updateDraft('Keep this visible'); store.send(); await new Promise<void>((resolve) => queueMicrotask(resolve)); store.stop(); await new Promise<void>((resolve) => queueMicrotask(resolve)); expect(store.selected().turns[0].text).toBe('Keep this visible'); expect(store.runState()).toBe('interrupted'); expect(store.notice()).toContain('stopped'); });
  it('consumes the complete accepted response when retrying a run', async () => { store.updateDraft('Retry this safely'); store.send(); await new Promise<void>((resolve) => queueMicrotask(resolve)); store.stop(); await new Promise<void>((resolve) => queueMicrotask(resolve)); store.retry(); await new Promise<void>((resolve) => queueMicrotask(resolve)); expect(store.selected().currentRun?.status).toBe('running'); expect(store.selected().currentRun?.agentRevisionId).toBe('agent-rev-1'); expect(store.selected().currentRun?.modelPolicyRevisionId).toBe('policy-1'); expect(store.selected().currentRun?.provider).toBe('ollama'); });
  it('updates model selection for future runs', async () => { store.updateDraft('Choose a model'); store.send(); await new Promise<void>((resolve) => queueMicrotask(resolve)); await store.selectModel('qwen2.5:7b'); expect(store.selectedModelId()).toBe('qwen2.5:7b'); });
  it('follows all conversation cursors so older conversations remain available', async () => {
    const cursors: Array<string | undefined> = [];
    const totalPages = 27;
    const pagedApi = fakeApi(async (cursor) => {
      cursors.push(cursor);
      const pageNumber = cursor ? Number(cursor.replace('page-', '')) : 0;
      return { items: [summary(pageNumber === 0 ? 'newest' : `older-${pageNumber}`)], nextCursor: pageNumber + 1 < totalPages ? `page-${pageNumber + 1}` : null };
    });
    const pagedStore = new ConversationStore(pagedApi);
    await new Promise<void>((resolve) => setTimeout(resolve, 0));

    expect(cursors).toHaveLength(totalPages);
    expect(pagedStore.conversations()).toHaveLength(totalPages);
    expect(pagedStore.conversations().at(-1)?.id).toBe('older-26');
  });
  it('guards repeated conversation cursors and surfaces a safe loading notice', async () => {
    const cycleApi = fakeApi(async (cursor) => ({ items: [summary(cursor ? 'cycle-older' : 'cycle-newest')], nextCursor: 'cycle' }));
    const cycleStore = new ConversationStore(cycleApi);
    await new Promise<void>((resolve) => setTimeout(resolve, 0));

    expect(cycleStore.conversations().map((conversation) => conversation.id)).toEqual(['cycle-newest', 'cycle-older']);
    expect(cycleStore.notice()).toContain('Some older conversations could not be loaded');
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
});
