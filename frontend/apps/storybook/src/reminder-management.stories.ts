import { applicationConfig } from '@storybook/angular';
import type { Meta, StoryObj } from '@storybook/angular';
import { signal } from '@angular/core';
import type { Reminder } from '@aura/aura-api-client';
import { RemindersPageComponent } from '@aura/aura/automation/reminders';
import { ReminderStore } from '@aura/aura/automation/reminders';

const now = '2026-10-08T09:00:00.000Z';
const upcoming: Reminder = { id: 'reminder-story', content: 'Bring the library books back', dueAt: '2026-10-09T16:30:00.000Z', status: 'scheduled', version: 1, createdAt: now, updatedAt: now, firedAt: null, completedAt: null, canceledAt: null };
const history: Reminder = { id: 'reminder-history', content: 'Call the dentist', dueAt: '2026-10-01T08:00:00.000Z', status: 'completed', version: 2, createdAt: now, updatedAt: '2026-10-02T08:00:00.000Z', firedAt: '2026-10-01T08:00:02.000Z', completedAt: '2026-10-01T08:05:00.000Z', canceledAt: null };

function fixtureStore(items = true): ReminderStore {
  return {
    upcoming: signal({ items: items ? [upcoming] : [], nextCursor: null }), history: signal({ items: items ? [history] : [], nextCursor: null }), selected: signal(null), loading: signal(false), loadingMore: signal(false), saving: signal(false), notice: signal(null), errorStatus: signal(null),
    load: async () => undefined, loadMore: async () => undefined, inspect: async () => null, create: async () => true, complete: async () => true, cancel: async () => true, clearNotice: () => undefined,
  } as unknown as ReminderStore;
}

const meta: Meta<RemindersPageComponent> = { title: 'Aura/Reminders product', component: RemindersPageComponent, parameters: { layout: 'fullscreen' }, decorators: [applicationConfig({ providers: [{ provide: ReminderStore, useValue: fixtureStore() }] })] };
export default meta;
type Story = StoryObj<RemindersPageComponent>;
export const Workspace: Story = { name: 'Upcoming and history' };
export const Empty: Story = { name: 'Empty first use', decorators: [applicationConfig({ providers: [{ provide: ReminderStore, useValue: fixtureStore(false) }] })] };
export const Mobile: Story = { name: 'Mobile responsive workspace', parameters: { viewport: { defaultViewport: 'mobile1' } } };
