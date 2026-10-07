import { applicationConfig } from '@storybook/angular';
import type { Meta, StoryObj } from '@storybook/angular';
import { signal } from '@angular/core';
import { ConversationPanelComponent, ConversationStore } from '@aura/aura/interaction/conversations';
import type { Model } from '@aura/aura-api-client';
import type { ConversationTurn, RunState } from '@aura/aura/interaction/conversations';

type FixtureState = 'empty' | 'loading' | 'working' | 'interrupted' | 'recoverable-error' | 'completed';

const turns: Record<Exclude<FixtureState, 'empty' | 'loading' | 'working' | 'interrupted' | 'recoverable-error'>, ReadonlyArray<ConversationTurn>> = {
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
  return {
    loading: signal(state === 'loading'),
    selected: signal({ id: 'fixture', title: 'Fixture conversation', turns: conversationTurns, updatedAt: 0 }),
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
