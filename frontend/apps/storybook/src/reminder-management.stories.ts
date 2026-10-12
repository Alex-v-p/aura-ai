import { applicationConfig } from '@storybook/angular';
import type { Meta, StoryObj } from '@storybook/angular';
import { signal } from '@angular/core';
import { ActivatedRoute, convertToParamMap, provideRouter } from '@angular/router';
import { of } from 'rxjs';
import type { Reminder } from '@aura/aura-api-client';
import { RemindersPageComponent } from '@aura/aura/automation/reminders';
import { ReminderStore } from '@aura/aura/automation/reminders';

const now = '2026-10-08T09:00:00.000Z';
const upcoming: Reminder = { id: 'reminder-story', content: 'Bring the library books back', dueAt: '2026-10-09T16:30:00.000Z', status: 'scheduled', version: 1, createdAt: now, updatedAt: now, firedAt: null, completedAt: null, canceledAt: null };
const history: Reminder = { id: 'reminder-history', content: 'Call the dentist', dueAt: '2026-10-01T08:00:00.000Z', status: 'completed', version: 2, createdAt: now, updatedAt: '2026-10-02T08:00:00.000Z', firedAt: '2026-10-01T08:00:02.000Z', completedAt: '2026-10-01T08:05:00.000Z', canceledAt: null };

type FixtureOptions = {
  readonly upcoming?: ReadonlyArray<Reminder>;
  readonly history?: ReadonlyArray<Reminder>;
  readonly selected?: Reminder | null;
  readonly loading?: boolean;
  readonly detailLoading?: boolean;
  readonly detailError?: string | null;
  readonly notice?: string | null;
  readonly errorStatus?: number | null;
};

function fixtureStore(items = true, options: FixtureOptions = {}): ReminderStore {
  const upcomingItems = options.upcoming ?? (items ? [upcoming] : []);
  const historyItems = options.history ?? (items ? [history] : []);
  const selected = signal<Reminder | null>(options.selected ?? null);
  return {
    upcoming: signal({ items: upcomingItems, nextCursor: null }), history: signal({ items: historyItems, nextCursor: null }), selected, loading: signal(options.loading ?? false), loadingMore: signal(false), detailLoading: signal(options.detailLoading ?? false), detailError: signal(options.detailError ?? null), saving: signal(false), notice: signal(options.notice ?? null), errorStatus: signal(options.errorStatus ?? null), createOutcome: signal<'idle' | 'pending' | 'confirmed' | 'failed' | 'unconfirmed'>('idle'), resetEpoch: signal(0),
    load: async () => undefined, loadMore: async () => undefined, inspect: async (id: string) => { const match = selected() ?? [...upcomingItems, ...historyItems].find((item) => item.id === id) ?? null; selected.set(match); return match; }, clearDetail: () => selected.set(null), create: async () => true, markCreateDraftChanged: () => undefined, clearCreateDraft: () => undefined, complete: async () => true, cancel: async () => true, clearNotice: () => undefined, registerSessionResetHandler: () => undefined, resetSessionState: () => undefined,
  } as unknown as ReminderStore;
}

function detailRoute(id: string): ActivatedRoute {
  return { paramMap: of(convertToParamMap({ id })) } as unknown as ActivatedRoute;
}

const meta: Meta<RemindersPageComponent> = { title: 'Aura/Reminders product', component: RemindersPageComponent, parameters: { layout: 'fullscreen' }, decorators: [applicationConfig({ providers: [provideRouter([]), { provide: ReminderStore, useValue: fixtureStore() }] })] };
export default meta;
type Story = StoryObj<RemindersPageComponent>;
export const Workspace: Story = { name: 'Upcoming and history' };
export const Empty: Story = { name: 'Empty first use', decorators: [applicationConfig({ providers: [{ provide: ReminderStore, useValue: fixtureStore(false) }] })] };
export const Mobile: Story = { name: 'Mobile responsive workspace', parameters: { viewport: { defaultViewport: 'mobile1' } } };
export const History: Story = { name: 'History view', decorators: [applicationConfig({ providers: [{ provide: ReminderStore, useValue: fixtureStore(true, { upcoming: [], history: [history] }) }] })] };
export const Composer: Story = { name: 'Reminder composer', decorators: [applicationConfig({ providers: [{ provide: ReminderStore, useValue: fixtureStore(false) }] })] };
export const Detail: Story = { name: 'Reminder detail', decorators: [applicationConfig({ providers: [{ provide: ActivatedRoute, useValue: detailRoute(upcoming.id) }, { provide: ReminderStore, useValue: fixtureStore(true, { selected: upcoming }) }] })] };
export const Loading: Story = { name: 'Loading agenda', decorators: [applicationConfig({ providers: [{ provide: ReminderStore, useValue: fixtureStore(false, { loading: true }) }] })] };
export const Recovery: Story = { name: 'Recoverable reminder error', decorators: [applicationConfig({ providers: [{ provide: ReminderStore, useValue: fixtureStore(false, { notice: 'Reminders are temporarily unavailable. Retry when you are ready.', errorStatus: 503 }) }] })] };
