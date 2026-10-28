import { applicationConfig } from '@storybook/angular';
import type { Meta, StoryObj } from '@storybook/angular';
import { computed, signal } from '@angular/core';
import { ActivatedRoute, convertToParamMap, provideRouter } from '@angular/router';
import { of } from 'rxjs';
import type { Notification, Reminder } from '@aura/aura-api-client';
import { NotificationsPageComponent, NotificationStore } from '@aura/aura/automation/notifications';
import { ReminderStore } from '@aura/aura/automation/reminders';

const delivered: Notification = { id: 'notification-story', reminderId: 'reminder-story', status: 'delivered', version: 1, createdAt: '2026-10-08T09:00:00.000Z', updatedAt: '2026-10-08T09:00:00.000Z', deliveredAt: '2026-10-08T09:00:00.000Z', acknowledgedAt: null };
const acknowledged: Notification = { ...delivered, id: 'notification-acknowledged', status: 'acknowledged', version: 2, acknowledgedAt: '2026-10-08T09:05:00.000Z' };
const briefing = { id: 'notification-briefing', briefingScheduleId: 'briefing-schedule-story', briefingOccurrenceId: 'briefing-occurrence-story', briefingResultId: 'briefing-result-story', status: 'delivered', version: 1, createdAt: '2026-10-08T09:00:00.000Z', updatedAt: '2026-10-08T09:00:00.000Z', deliveredAt: '2026-10-08T09:00:00.000Z', acknowledgedAt: null } as unknown as Notification;
const workflow = { id: 'notification-workflow', workflowId: 'workflow-story', workflowRevisionId: 'revision-story', workflowInstanceId: 'instance-story', workflowStepOrdinal: 3, workflowStepExecutionId: 'execution-story', workflowResultId: 'result-story', status: 'delivered', version: 1, createdAt: '2026-10-08T09:00:00.000Z', updatedAt: '2026-10-08T09:00:00.000Z', deliveredAt: '2026-10-08T09:00:00.000Z', acknowledgedAt: null } as unknown as Notification;
const sourceReminder: Reminder = { id: 'reminder-story', content: 'Bring the library books back', dueAt: '2026-10-09T16:30:00.000Z', status: 'scheduled', version: 1, createdAt: '2026-10-08T09:00:00.000Z', updatedAt: '2026-10-08T09:00:00.000Z', firedAt: null, completedAt: null, canceledAt: null };

type NotificationFixtureOptions = {
  readonly items?: ReadonlyArray<Notification>;
  readonly selected?: Notification | null;
  readonly loading?: boolean;
  readonly detailLoading?: boolean;
  readonly detailError?: string | null;
  readonly notice?: string | null;
  readonly errorStatus?: number | null;
  readonly view?: 'unread' | 'history';
};

function fixtureStore(defaultItems: ReadonlyArray<Notification> = [delivered], options: NotificationFixtureOptions = {}): NotificationStore {
  const items = options.items ?? defaultItems;
  const collection = signal({ items, nextCursor: null, unacknowledgedCount: items.filter((item) => item.status === 'delivered').length });
  const selected = signal<Notification | null>(options.selected ?? null);
  const view = options.view ?? 'unread';
  return {
    collection, selected, loading: signal(options.loading ?? false), detailLoading: signal(options.detailLoading ?? false), detailError: signal(options.detailError ?? null), saving: signal(false), notice: signal(options.notice ?? null), errorStatus: signal(options.errorStatus ?? null), connectionState: signal<'offline' | 'connecting' | 'reconnecting' | 'live' | 'unavailable'>('live'), view: signal(view), loadedView: signal<'unread' | 'history' | null>(view), filter: signal<'all' | 'delivered' | 'acknowledged'>(view === 'history' ? 'acknowledged' : 'delivered'), unreadCount: computed(() => collection().unacknowledgedCount), resetEpoch: signal(0),
    start: async () => undefined, stop: () => undefined, retryConnection: () => undefined, resumeAfterAcceptedSession: () => undefined, refresh: async () => undefined, loadMore: async () => undefined, inspect: async (id: string) => { const match = selected() ?? items.find((item) => item.id === id) ?? null; selected.set(match); return match; }, clearDetail: () => selected.set(null), acknowledge: async () => true, setView: async () => undefined, setFilter: async () => undefined, clearNotice: () => undefined, registerSessionResetHandler: () => undefined, resetSessionState: () => undefined,
  } as unknown as NotificationStore;
}

function fixtureReminders(options: { readonly selected?: Reminder | null; readonly detailLoading?: boolean; readonly detailError?: string | null } = {}): ReminderStore {
  const selected = signal(options.selected ?? null);
  return { inspect: async () => selected(), complete: async () => true, selected, detailLoading: signal(options.detailLoading ?? false), detailError: signal(options.detailError ?? null), saving: signal(false), notice: signal<string | null>(null), clearDetail: () => selected.set(null) } as unknown as ReminderStore;
}

function detailRoute(id: string): ActivatedRoute {
  return { paramMap: of(convertToParamMap({ id })) } as unknown as ActivatedRoute;
}

const meta: Meta<NotificationsPageComponent> = { title: 'Aura/Notifications centre', component: NotificationsPageComponent, parameters: { layout: 'fullscreen' }, decorators: [applicationConfig({ providers: [provideRouter([]), { provide: NotificationStore, useValue: fixtureStore() }, { provide: ReminderStore, useValue: fixtureReminders() }] })] };
export default meta;
type Story = StoryObj<NotificationsPageComponent>;
export const Inbox: Story = { name: 'Unread notifications' };
export const Empty: Story = { name: 'Empty inbox', decorators: [applicationConfig({ providers: [{ provide: NotificationStore, useValue: fixtureStore([]) }, { provide: ReminderStore, useValue: fixtureReminders() }] })] };
export const Mobile: Story = { name: 'Mobile responsive inbox', parameters: { viewport: { defaultViewport: 'mobile1' } } };
export const History: Story = { name: 'Acknowledged history', decorators: [applicationConfig({ providers: [{ provide: NotificationStore, useValue: fixtureStore([acknowledged], { view: 'history' }) }, { provide: ReminderStore, useValue: fixtureReminders() }] })] };
export const Detail: Story = { name: 'Notification detail', decorators: [applicationConfig({ providers: [{ provide: ActivatedRoute, useValue: detailRoute(delivered.id) }, { provide: NotificationStore, useValue: fixtureStore([delivered], { selected: delivered }) }, { provide: ReminderStore, useValue: fixtureReminders({ selected: sourceReminder }) }] })] };
export const Briefing: Story = { name: 'Briefing notification with deferred result destination', decorators: [applicationConfig({ providers: [{ provide: ActivatedRoute, useValue: detailRoute(briefing.id) }, { provide: NotificationStore, useValue: fixtureStore([briefing], { selected: briefing }) }, { provide: ReminderStore, useValue: fixtureReminders() }] })] };
export const Workflow: Story = { name: 'Content-free workflow notification', decorators: [applicationConfig({ providers: [{ provide: ActivatedRoute, useValue: detailRoute(workflow.id) }, { provide: NotificationStore, useValue: fixtureStore([workflow], { selected: workflow }) }, { provide: ReminderStore, useValue: fixtureReminders() }] })] };
export const Loading: Story = { name: 'Loading inbox', decorators: [applicationConfig({ providers: [{ provide: NotificationStore, useValue: fixtureStore([], { loading: true }) }, { provide: ReminderStore, useValue: fixtureReminders() }] })] };
export const Recovery: Story = { name: 'Recoverable notification error', decorators: [applicationConfig({ providers: [{ provide: NotificationStore, useValue: fixtureStore([], { notice: 'Notifications are temporarily unavailable.', errorStatus: 503 }) }, { provide: ReminderStore, useValue: fixtureReminders() }] })] };
