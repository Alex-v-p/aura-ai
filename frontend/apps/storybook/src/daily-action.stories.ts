import { applicationConfig } from '@storybook/angular';
import type { Meta, StoryObj } from '@storybook/angular';
import { signal } from '@angular/core';
import { provideRouter, withDisabledInitialNavigation } from '@angular/router';
import { ApprovalStore, type ApprovalSummary } from '@aura/aura/governance/approvals';
import { NotificationStore } from '@aura/aura/automation/notifications';
import { ReminderStore } from '@aura/aura/automation/reminders';
import { ConversationStore, type AuthState } from '@aura/aura/interaction/conversations';
import { DAILY_CALENDAR_AGENDA, DailyActionPageComponent, type DailyCalendarAgendaStatus, type DailyCalendarEventProjection } from '@aura/aura/daily-action';

const now = new Date('2026-10-26T09:00:00.000Z');
const reminderId = 'reminder-story';
const reminder = { id: reminderId, content: 'Bring the library books back', dueAt: '2026-10-26T16:30:00.000Z', status: 'scheduled', version: 1, createdAt: now.toISOString(), updatedAt: now.toISOString(), firedAt: null, completedAt: null, canceledAt: null } as never;
const approval: ApprovalSummary = { id: 'approval-story', proposalId: 'proposal-story', runId: 'run-story', conversationId: 'conversation-story', toolCallId: 'call-story', toolId: 'reminder.create', toolVersion: 1, status: 'pending', executionStatus: 'not_started', version: 1, createdAt: now.toISOString(), updatedAt: now.toISOString(), expiresAt: '2026-10-26T10:00:00.000Z', decidedAt: null };
const notification = { id: 'notification-story', reminderId, status: 'delivered', version: 1, createdAt: now.toISOString(), updatedAt: now.toISOString(), deliveredAt: '2026-10-26T08:30:00.000Z', acknowledgedAt: null } as never;

type FixtureOptions = {
  readonly empty?: boolean;
  readonly authState?: AuthState;
  readonly reminderLoading?: boolean;
  readonly notificationLoading?: boolean;
  readonly approvalLoading?: boolean;
  readonly notificationErrorStatus?: number | null;
  readonly notificationNotice?: string | null;
  readonly approvalErrorStatus?: number | null;
  readonly approvalNotice?: string | null;
  readonly notificationView?: 'unread' | 'history';
  readonly notificationConnection?: 'offline' | 'reconnecting' | 'live';
  readonly approvalConnection?: 'offline' | 'reconnecting' | 'live';
  readonly calendarState?: DailyCalendarAgendaStatus;
  readonly calendarNotice?: string | null;
};

const calendarEvents: ReadonlyArray<DailyCalendarEventProjection> = [
  { kind: 'all_day', id: 'story-all-day', calendarId: 'personal', title: 'Planning day', location: null, availability: 'busy', startDate: '2026-10-27', endDateExclusive: '2026-10-28', eventTimeZone: 'Europe/Brussels', displayTimeZone: 'Europe/Brussels', spansMultipleDays: false },
  { kind: 'timed', id: 'story-focus', calendarId: 'team', title: 'Focus block', location: 'Studio', availability: 'busy', startInstant: '2026-10-27T09:00:00Z', endInstant: '2026-10-27T10:00:00Z', eventTimeZone: 'America/New_York', displayTimeZone: 'Europe/Brussels', spansMultipleDays: false },
];

function calendarFixture(options: FixtureOptions): { provide: typeof DAILY_CALENDAR_AGENDA; useValue: unknown } {
  const status = signal<DailyCalendarAgendaStatus>(options.calendarState ?? 'disconnected');
  const events = signal<ReadonlyArray<DailyCalendarEventProjection>>(options.calendarState === 'ready' ? calendarEvents : []);
  return {
    provide: DAILY_CALENDAR_AGENDA,
    useValue: {
      start: () => undefined,
      status,
      events,
      conflicts: signal([]),
      displayTimeZone: signal('Europe/Brussels'),
      visibleDate: signal('2026-10-27'),
      notice: signal<string | null>(options.calendarNotice ?? null),
      retry: () => undefined,
    },
  };
}

function fixtureStores(options: FixtureOptions | boolean = {}): Array<{ provide: unknown; useValue: unknown }> {
  const config = typeof options === 'boolean' ? { empty: options } : options;
  const empty = config.empty ?? false;
  const upcoming = signal({ items: empty ? [] : [reminder], nextCursor: null });
  const series = signal({ items: [], nextCursor: null });
  const reminderStore = { upcoming, history: signal({ items: [], nextCursor: null }), series, loading: signal(config.reminderLoading ?? false), notice: signal<string | null>(null), errorStatus: signal<number | null>(null), load: async () => undefined } as unknown as ReminderStore;
  const notificationCollection = signal({ items: empty ? [] : [notification], nextCursor: null, unacknowledgedCount: empty ? 0 : 1 });
  const notificationView = config.notificationView ?? 'unread';
  const notificationStore = { collection: notificationCollection, unreadCollection: notificationCollection, unreadLoaded: signal(!config.notificationLoading), loading: signal(config.notificationLoading ?? false), loadedView: signal<'unread' | 'history' | null>(config.notificationLoading ? null : notificationView), view: signal<'unread' | 'history'>(notificationView), unreadProjectionLoading: signal(false), unreadProjectionErrorStatus: signal<number | null>(null), unreadProjectionNotice: signal<string | null>(null), errorStatus: signal<number | null>(config.notificationErrorStatus ?? null), notice: signal<string | null>(config.notificationNotice ?? null), connectionState: signal<'live' | 'offline' | 'reconnecting'>(config.notificationConnection ?? 'live'), refreshUnreadProjection: async () => true, retryUnreadProjection: () => undefined, retryConnection: () => undefined } as unknown as NotificationStore;
  const pendingCollection = signal({ items: empty ? [] : [approval], nextCursor: null, pendingCount: empty ? 0 : 1 });
  const approvalStore = { pendingCollection, loading: signal(config.approvalLoading ?? false), loadedView: signal<'pending' | 'history' | null>(config.approvalLoading ? null : 'pending'), notice: signal<string | null>(config.approvalNotice ?? null), errorStatus: signal<number | null>(config.approvalErrorStatus ?? null), connectionState: signal<'live' | 'offline' | 'reconnecting'>(config.approvalConnection ?? 'live'), retryConnection: () => undefined } as unknown as ApprovalStore;
  const conversations = signal([]);
  const conversationStore = { conversations, authState: signal<AuthState>(config.authState ?? 'authenticated'), libraryError: signal<string | null>(null), load: async () => undefined, login: () => undefined } as unknown as ConversationStore;
  return [
    { provide: ReminderStore, useValue: reminderStore },
    { provide: NotificationStore, useValue: notificationStore },
    { provide: ApprovalStore, useValue: approvalStore },
    { provide: ConversationStore, useValue: conversationStore },
    calendarFixture(config),
  ];
}

const meta: Meta<DailyActionPageComponent> = {
  title: 'Aura/Today action center',
  component: DailyActionPageComponent,
  parameters: { layout: 'fullscreen' },
  decorators: [applicationConfig({ providers: [provideRouter([], withDisabledInitialNavigation()), ...fixtureStores()] })],
};
export default meta;
type Story = StoryObj<DailyActionPageComponent>;
export const ActionCenter: Story = { name: 'Needs attention and daily reminders' };
export const CalmEmpty: Story = { name: 'All clear', decorators: [applicationConfig({ providers: [provideRouter([], withDisabledInitialNavigation()), ...fixtureStores(true)] })] };
export const Loading: Story = { name: 'Loading sources', decorators: [applicationConfig({ providers: [provideRouter([], withDisabledInitialNavigation()), ...fixtureStores({ empty: true, reminderLoading: true, notificationLoading: true, approvalLoading: true, authState: 'loading' })] })] };
export const PartialFailure: Story = { name: 'Partial source failure', decorators: [applicationConfig({ providers: [provideRouter([], withDisabledInitialNavigation()), ...fixtureStores({ notificationErrorStatus: 503, notificationNotice: 'Notifications are temporarily unavailable.' })] })] };
export const PermissionDenied: Story = { name: 'Permission denied', decorators: [applicationConfig({ providers: [provideRouter([], withDisabledInitialNavigation()), ...fixtureStores({ authState: 'error', empty: true, approvalErrorStatus: 403, approvalNotice: 'You do not have permission to view approvals.', notificationErrorStatus: 403, notificationNotice: 'You do not have permission to view notifications.' })] })] };
export const Offline: Story = { name: 'Offline', decorators: [applicationConfig({ providers: [provideRouter([], withDisabledInitialNavigation()), ...fixtureStores({ notificationConnection: 'offline', approvalConnection: 'offline' })] })] };
export const Reconnecting: Story = { name: 'Reconnecting', decorators: [applicationConfig({ providers: [provideRouter([], withDisabledInitialNavigation()), ...fixtureStores({ notificationConnection: 'reconnecting', approvalConnection: 'reconnecting' })] })] };
export const HistoryLiveUnavailable: Story = { name: 'History live updates unavailable', decorators: [applicationConfig({ providers: [provideRouter([], withDisabledInitialNavigation()), ...fixtureStores({ notificationView: 'history', notificationConnection: 'offline', notificationNotice: 'Live notifications stopped. Retry when you are ready.' })] })] };
export const Unauthenticated: Story = { name: 'Unauthenticated', decorators: [applicationConfig({ providers: [provideRouter([], withDisabledInitialNavigation()), ...fixtureStores({ authState: 'unauthenticated', empty: true, approvalErrorStatus: 401, approvalNotice: 'Your session has expired. Sign in again to view approvals.', notificationErrorStatus: 401, notificationNotice: 'Your session has expired. Sign in again to view notifications.' })] })] };
export const CalendarConnected: Story = { name: 'Calendar agenda connected', decorators: [applicationConfig({ providers: [provideRouter([], withDisabledInitialNavigation()), ...fixtureStores({ calendarState: 'ready' })] })] };
export const CalendarPartial: Story = { name: 'Calendar agenda partial read', decorators: [applicationConfig({ providers: [provideRouter([], withDisabledInitialNavigation()), ...fixtureStores({ calendarState: 'partial', calendarNotice: 'Some calendar events could not be loaded. The available events are shown.' })] })] };
export const CalendarStale: Story = { name: 'Calendar agenda stale snapshot', decorators: [applicationConfig({ providers: [provideRouter([], withDisabledInitialNavigation()), ...fixtureStores({ calendarState: 'stale', calendarNotice: 'Showing the last calendar snapshot. Refresh when you are ready.' })] })] };
export const CalendarRateLimited: Story = { name: 'Calendar agenda rate limited', decorators: [applicationConfig({ providers: [provideRouter([], withDisabledInitialNavigation()), ...fixtureStores({ calendarState: 'rate-limited' })] })] };
export const CalendarPermissionDenied: Story = { name: 'Calendar permission denied', decorators: [applicationConfig({ providers: [provideRouter([], withDisabledInitialNavigation()), ...fixtureStores({ calendarState: 'permission-denied' })] })] };
export const CalendarOffline: Story = { name: 'Calendar offline', decorators: [applicationConfig({ providers: [provideRouter([], withDisabledInitialNavigation()), ...fixtureStores({ calendarState: 'offline' })] })] };
