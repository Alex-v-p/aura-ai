import { applicationConfig } from '@storybook/angular';
import type { Meta, StoryObj } from '@storybook/angular';
import { signal } from '@angular/core';
import { ConversationPanelComponent, ConversationStore } from '@aura/aura/interaction/conversations';
import type { Model, Run } from '@aura/aura-api-client';
import type { ConversationTurn, RunState } from '@aura/aura/interaction/conversations';

type FixtureState = 'empty' | 'loading' | 'working' | 'interrupted' | 'recoverable-error' | 'completed' | 'archived' | 'run-inspector';

const turns: Record<'completed', ReadonlyArray<ConversationTurn>> = {
  completed: [
    { id: 'fixture-user', role: 'user', text: 'Help me find a small next step.' },
    { id: 'fixture-assistant', role: 'assistant', text: 'Start with one kind, concrete action you can finish today.' },
  ],
};

function fixtureStore(state: FixtureState): ConversationStore {
  const conversationTurns = state === 'completed' ? turns.completed : state === 'empty' || state === 'loading' ? [] : [{ id: 'fixture-user', role: 'user', text: 'Help me find a small next step.' }];
  const runState = signal<RunState>(state === 'working' ? 'working' : state === 'interrupted' ? 'interrupted' : state === 'recoverable-error' ? 'error' : 'idle');
  const notice = signal<string | null>(state === 'interrupted' ? 'Generation stopped. Your message is still in the conversation.' : state === 'recoverable-error' ? 'We could not complete that local preview. Your message is still here. Try again when you are ready.' : null);
  const authState = signal<'authenticated'>('authenticated');
  const models = signal<ReadonlyArray<Model>>([{ id: 'fixture-model', displayName: 'Fixture model', provider: 'fake', capabilities: ['chat'] as const, availability: 'available', selectable: true, disabledReason: null }]);
  const now = '2026-10-06T12:00:00.000Z';
  const fixtureRun: Run = { id: 'fixture-run', conversationId: 'fixture', userMessageId: 'fixture-user', assistantMessageId: null, status: 'failed', agentRevisionId: 'fixture-agent-r1', modelPolicyRevisionId: 'fixture-policy', provider: 'fake', modelId: 'fixture-model', retryOfRunId: null, createdAt: now, startedAt: now, finishedAt: '2026-10-06T12:00:01.000Z', error: { code: 'PROVIDER_ERROR', message: 'Private provider payload', retryable: true, traceId: '0123456789abcdef0123456789abcdef' } };
  const selected = signal({ id: 'fixture', title: state === 'archived' ? 'Archived fixture conversation' : 'Fixture conversation', turns: conversationTurns, updatedAt: 0, archivedAt: state === 'archived' ? now : null, currentRun: null, retryableRun: state === 'run-inspector' ? fixtureRun : null, runs: state === 'run-inspector' ? [fixtureRun] : [] });
  const selectedId = signal('fixture');
  return {
    loading: signal(state === 'loading'),
    selected,
    selectedId,
    runState,
    notice,
    authState,
    authenticated: signal(true),
    models,
    selectedModelId: signal('fixture-model'),
    load: async () => undefined,
    login: () => undefined,
    selectModel: async () => undefined,
    draft: signal(''),
    send: () => true,
    stop: () => runState.set('interrupted'),
    retry: () => { runState.set('idle'); notice.set(null); },
    updateDraft: () => undefined,
    restoreConversation: async () => { selected.update((conversation) => ({ ...conversation, archivedAt: null })); return true; },
    runInspectorRequested: signal(state === 'run-inspector' ? 1 : 0),
    runInspectorFocusId: signal<string | null>(null),
    cancelRunById: () => undefined,
    retryRunById: () => undefined,
  } as unknown as ConversationStore;
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

const story = (state: FixtureState): Story => ({ decorators: [applicationConfig({ providers: [{ provide: ConversationStore, useValue: fixtureStore(state) }] })] });

export const Empty: Story = { ...story('empty'), name: 'Empty' };
export const Loading: Story = { ...story('loading'), name: 'Loading' };
export const Working: Story = { ...story('working'), name: 'Working' };
export const Interrupted: Story = { ...story('interrupted'), name: 'Interrupted' };
export const RecoverableError: Story = { ...story('recoverable-error'), name: 'Recoverable error' };
export const Completed: Story = { ...story('completed'), name: 'Completed' };
export const ArchivedRecovery: Story = { ...story('archived'), name: 'Archived recovery' };
export const RunInspector: Story = { ...story('run-inspector'), name: 'Run inspector' };
