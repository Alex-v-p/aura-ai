import { applicationConfig } from '@storybook/angular';
import type { Meta, StoryObj } from '@storybook/angular';
import { signal } from '@angular/core';
import { provideRouter } from '@angular/router';
import { ConversationPanelComponent, ConversationStore, type ConversationApi, type ConversationTurn } from '@aura/aura/interaction/conversations';
import type { ConversationDetail, ConversationRunAccepted, ConversationSummary, Message, Model, ModelCatalog, Run, Session } from '@aura/aura-api-client';
import { AGENT_API, AgentStore, InMemoryAgentApi } from '@aura/aura/interaction/agents';
import { PersonaStore } from '@aura/aura/interaction/personas';

type FixtureState = 'empty' | 'loading' | 'working' | 'interrupted' | 'recoverable-error' | 'completed' | 'archived' | 'run-inspector';

const turns: Record<'completed', ReadonlyArray<ConversationTurn>> = {
  completed: [
    { id: 'fixture-user', role: 'user', text: 'Help me find a small next step.' },
    { id: 'fixture-assistant', role: 'assistant', text: '## A small next step\n\nStart with one **kind, concrete action** you can finish today.\n\n```ts\nconst nextStep = "one small action";\n```\n\nSee [the Aura guide](https://example.com/aura) for more.' },
  ],
};

const fixtureModel: Model = { id: 'fixture-model', displayName: 'Fixture model', provider: 'fake', capabilities: ['chat'], availability: 'available', selectable: true, disabledReason: null };
const fixtureSession: Session = { principal: { issuer: 'https://authentik.test', subject: 'storybook' }, csrfToken: 'storybook-csrf', idleExpiresAt: '2026-10-06T20:00:00.000Z', absoluteExpiresAt: '2026-10-07T12:00:00.000Z' };
const fixtureAgent = { profileId: 'fixture-agent', revisionId: 'fixture-agent-r1', revision: 1, displayName: 'Fixture agent', status: 'active' as const, newerRevisionAvailable: false };
const now = '2026-10-06T12:00:00.000Z';

function fixtureRun(state: FixtureState): Run | null {
  if (!['working', 'interrupted', 'recoverable-error', 'run-inspector'].includes(state)) return null;
  const status = state === 'working' ? 'running' : state === 'interrupted' ? 'interrupted' : 'failed';
  return { id: 'fixture-run', conversationId: 'fixture', userMessageId: 'fixture-user', assistantMessageId: null, status, agentRevisionId: fixtureAgent.revisionId, modelPolicyRevisionId: 'fixture-policy', provider: 'fake', modelId: fixtureModel.id, retryOfRunId: null, createdAt: now, startedAt: now, finishedAt: status === 'running' ? null : '2026-10-06T12:00:01.000Z', error: status === 'failed' ? { code: 'PROVIDER_ERROR', message: 'Private provider payload', retryable: true, traceId: '0123456789abcdef0123456789abcdef' } : null };
}

function fixtureSummary(state: FixtureState): ConversationSummary {
  const run = fixtureRun(state);
  return { id: 'fixture', title: state === 'archived' ? 'Archived fixture conversation' : 'Fixture conversation', agentProfileId: fixtureAgent.profileId, agentRevisionId: fixtureAgent.revisionId, agent: fixtureAgent, modelId: fixtureModel.id, version: 1, createdAt: now, updatedAt: now, archivedAt: state === 'archived' ? now : null, currentRun: state === 'working' ? run : null };
}

function fixtureMessages(state: FixtureState): ReadonlyArray<Message> {
  const user: Message = { id: 'fixture-user', conversationId: 'fixture', role: 'user', content: 'Help me find a small next step.', state: 'complete', runId: fixtureRun(state)?.id ?? null, createdAt: now, updatedAt: now };
  if (state === 'completed' || state === 'archived') return [user, { id: 'fixture-assistant', conversationId: 'fixture', role: 'assistant', content: turns.completed[1]?.text ?? '', state: 'complete', runId: null, createdAt: now, updatedAt: now }];
  if (state === 'interrupted') return [user, { id: 'fixture-assistant', conversationId: 'fixture', role: 'assistant', content: 'I started gathering a thought, but the preview was interrupted.', state: 'interrupted', runId: 'fixture-run', createdAt: now, updatedAt: now }];
  return state === 'empty' || state === 'loading' ? [] : [user];
}

function fixtureDetail(state: FixtureState): ConversationDetail {
  const summary = fixtureSummary(state);
  const run = fixtureRun(state);
  return { ...summary, messages: fixtureMessages(state), recentRuns: run && run.status !== 'running' ? [run] : [] };
}

function fixtureApi(state: FixtureState): ConversationApi {
  const detail = fixtureDetail(state);
  const page: ReadonlyArray<ConversationSummary> = state === 'empty' || state === 'loading' ? [] : [detail];
  const catalog: ModelCatalog = { models: [fixtureModel], defaultModelId: fixtureModel.id, observedAt: now };
  const api: ConversationApi = {
    getSession: async () => fixtureSession,
    startLogin: () => undefined,
    logout: async () => undefined,
    listModels: async () => catalog,
    listConversations: async () => state === 'loading' ? new Promise<{ items: ReadonlyArray<ConversationSummary>; nextCursor: string | null }>(() => undefined) : ({ items: page, nextCursor: null }),
    getConversation: async () => detail,
    updateConversationMetadata: async (_id, metadata) => ({ ...detail, title: metadata.title ?? detail.title, archivedAt: metadata.archived === true ? now : metadata.archived === false ? null : detail.archivedAt }),
    createConversation: async (message, modelId) => acceptedRun('fixture-created', message, modelId),
    updateConversation: async (_id, modelId) => ({ ...detail, modelId }),
    createRun: async (_id, message) => acceptedRun('fixture', message, fixtureModel.id),
    cancelRun: async () => {
      const run = fixtureRun('working');
      if (!run) throw new Error('Working fixture run is unavailable.');
      return { ...run, status: 'canceled' as const, finishedAt: now };
    },
    retryRun: async () => acceptedRun('fixture', 'Retry fixture', fixtureModel.id),
    streamRunEvents: () => ({ close: () => undefined }),
  };
  return api;

  function acceptedRun(conversationId: string, message: string, modelId: string): ConversationRunAccepted {
    const run: Run = { id: `run-${conversationId}`, conversationId, userMessageId: `user-${conversationId}`, assistantMessageId: null, status: 'running', agentRevisionId: fixtureAgent.revisionId, modelPolicyRevisionId: 'fixture-policy', provider: 'fake', modelId, retryOfRunId: null, createdAt: now, startedAt: now, finishedAt: null, error: null };
    const summary: ConversationSummary = { ...fixtureSummary('working'), id: conversationId, currentRun: run, modelId };
    const userMessage: Message = { id: run.userMessageId, conversationId, role: 'user', content: message, state: 'complete', runId: run.id, createdAt: now, updatedAt: now };
    return { conversation: summary, userMessage, run };
  }
}

function fixtureStore(state: FixtureState): ConversationStore {
  const store = new ConversationStore(fixtureApi(state));
  if (state === 'loading') store.loading.set(true);
  if (state === 'run-inspector') store.runInspectorRequested.set(1);
  return store;
}

const meta: Meta<ConversationPanelComponent> = {
  title: 'Aura/Conversation panel',
  component: ConversationPanelComponent,
  // The browser matrix owns deterministic automated axe coverage for these six
  // fixture states. Keep the addon available for manual inspection without
  // racing its scan against the component-scoped Playwright scan.
  parameters: { layout: 'fullscreen', a11y: { manual: true } },
};
export default meta;

type Story = StoryObj<ConversationPanelComponent>;

const story = (state: FixtureState): Story => ({ decorators: [applicationConfig({ providers: [{ provide: ConversationStore, useValue: fixtureStore(state) }, { provide: AGENT_API, useFactory: () => new InMemoryAgentApi() }, { provide: AgentStore, useClass: AgentStore }, { provide: PersonaStore, useValue: { activePersonas: signal([]) } }, provideRouter([])] })] });

export const Empty: Story = { ...story('empty'), name: 'Empty' };
export const Loading: Story = { ...story('loading'), name: 'Loading' };
export const Working: Story = { ...story('working'), name: 'Working' };
export const Interrupted: Story = { ...story('interrupted'), name: 'Interrupted' };
export const RecoverableError: Story = { ...story('recoverable-error'), name: 'Recoverable error' };
export const Completed: Story = { ...story('completed'), name: 'Completed' };
export const ArchivedRecovery: Story = { ...story('archived'), name: 'Archived recovery' };
export const RunInspector: Story = { ...story('run-inspector'), name: 'Run inspector' };
