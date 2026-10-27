import { applicationConfig } from '@storybook/angular';
import type { Meta, StoryObj } from '@storybook/angular';
import { computed, signal } from '@angular/core';
import { provideRouter, withDisabledInitialNavigation } from '@angular/router';
import {
  CalendarManagementPageComponent,
  CalendarStore,
  type CalendarAgendaProjection,
  type CalendarConnection,
  type CalendarClientError,
  type CalendarEvent,
  type SelectableCalendar,
} from '@aura/aura/integrations/calendar';

const calendar = (id: string, displayName: string, selected = false): SelectableCalendar => ({
  id,
  displayName,
  timeZone: id === 'personal' ? 'Europe/Brussels' : 'America/New_York',
  selected,
  discoveredAt: '2026-10-27T07:00:00Z',
});

const events = [
  {
    kind: 'all_day' as const,
    id: 'event-planning-day',
    calendarId: 'personal',
    title: 'Planning day',
    description: null,
    location: null,
    availability: 'busy' as const,
    startDate: '2026-10-27',
    endDateExclusive: '2026-10-28',
    eventTimeZone: 'Europe/Brussels',
    displayTimeZone: 'Europe/Brussels',
    spansMultipleDays: false,
  },
  {
    kind: 'timed' as const,
    id: 'event-focus-block',
    calendarId: 'team',
    title: 'Focus block',
    description: null,
    location: 'Studio',
    availability: 'busy' as const,
    startInstant: '2026-10-27T09:00:00Z',
    endInstant: '2026-10-27T10:00:00Z',
    eventTimeZone: 'America/New_York',
    displayTimeZone: 'Europe/Brussels',
    spansMultipleDays: false,
  },
];
const longTitleEvents = [{
  ...events[0],
  id: 'event-multi-day-long-title',
  title: 'A deliberately long multi-day planning title that must remain readable without horizontal scrolling at narrow widths',
  endDateExclusive: '2026-10-30',
  spansMultipleDays: true,
}, ...events.slice(1)];

const connectedConnection = {
  status: 'connected',
  readsEnabled: true,
  selectedCalendarCount: 2,
  availableActions: ['disconnect'],
  provider: 'google_calendar',
  connectedAt: '2026-10-26T08:00:00Z',
  lastSuccessfulReadAt: '2026-10-27T06:00:00Z',
  lastError: null,
} as unknown as CalendarConnection;

type StoryState = {
  status?: string;
  connection?: CalendarConnection | null;
  calendars?: ReadonlyArray<SelectableCalendar>;
  events?: ReadonlyArray<CalendarEvent>;
  partial?: boolean;
  stale?: boolean;
  error?: CalendarClientError | null;
  notice?: string | null;
};

function fixtureStore(options: StoryState = {}): Record<string, unknown> {
  const connection = signal<CalendarConnection | null>(options.connection === undefined ? connectedConnection : options.connection);
  const calendarItems = signal<ReadonlyArray<SelectableCalendar>>(options.calendars ?? [calendar('personal', 'Personal', true), calendar('team', 'Team calendar', true)]);
  const selectedCalendarIds = signal<ReadonlyArray<string>>(calendarItems().filter((item) => item.selected).map((item) => item.id));
  const eventItems = options.events ?? events;
  const projection = signal<CalendarAgendaProjection>({
    events: eventItems.map((item) => ({ ...item, sourceLabel: 'Calendar' as const, timeZoneLabel: item.eventTimeZone === item.displayTimeZone ? item.displayTimeZone : `${item.eventTimeZone} · shown in ${item.displayTimeZone}` })),
    allDayEvents: eventItems.filter((item) => item.kind === 'all_day').map((item) => ({ ...item, sourceLabel: 'Calendar' as const, timeZoneLabel: item.displayTimeZone })),
    timedEvents: eventItems.filter((item) => item.kind === 'timed').map((item) => ({ ...item, sourceLabel: 'Calendar' as const, timeZoneLabel: `${item.eventTimeZone} · shown in ${item.displayTimeZone}` })),
    conflicts: [],
    warnings: options.partial ? [{ calendarId: 'team', category: 'rate_limited', recoveryAction: 'retry' }] as never : [],
    partial: options.partial ?? false,
    stale: options.stale ?? false,
    selectedDate: '2026-10-27',
    displayTimeZone: 'Europe/Brussels',
  });
  const requestState = signal(options.status === 'loading' ? 'loading' : 'idle');
  const error = signal<CalendarClientError | null>(options.error ?? null);
  const notice = signal<string | null>(options.notice ?? null);
  return {
    connection,
    calendars: calendarItems,
    selectionVersion: signal(7),
    calendarsComplete: signal(true),
    selectedCalendarIds,
    events: signal(null),
    conflicts: signal(null),
    selectedDate: signal('2026-10-27'),
    displayTimeZone: signal('Europe/Brussels'),
    requestState,
    error,
    notice,
    agenda: computed(() => projection()),
    connected: signal(options.connection?.status === 'connected' || options.connection === undefined),
    canConnect: signal(options.connection?.status === 'disconnected' || options.connection === null),
    canComplete: signal(options.connection?.status === 'connecting'),
    canReauthorize: signal(options.connection?.status === 'reauthorization_required'),
    canDisconnect: signal(options.connection?.status === 'connected' || options.connection === undefined),
    load: async () => undefined,
    discoverCalendars: async () => undefined,
    setCalendarSelection: (ids: ReadonlyArray<string>) => selectedCalendarIds.set(ids),
    replaceSelection: async () => true,
    loadAgenda: async () => undefined,
    connect: async () => false,
    reauthorize: async () => false,
    completeConnection: async () => false,
    disconnect: async () => true,
    retry: async () => undefined,
    clearPrivateContent: () => undefined,
  };
}

const meta: Meta<CalendarManagementPageComponent> = {
  title: 'Aura/Calendar planning',
  component: CalendarManagementPageComponent,
  parameters: { layout: 'fullscreen' },
  decorators: [applicationConfig({ providers: [provideRouter([], withDisabledInitialNavigation()), { provide: CalendarStore, useValue: fixtureStore() }] })],
};
export default meta;
type Story = StoryObj<CalendarManagementPageComponent>;

const withStore = (state: StoryState) => applicationConfig({ providers: [{ provide: CalendarStore, useValue: fixtureStore(state) }] });

export const Connected: Story = { name: 'Connected agenda and explicit selection' };
export const Loading: Story = { name: 'Loading connection', decorators: [withStore({ status: 'loading', connection: null, events: [] })] };
export const Disconnected: Story = { name: 'Disconnected and ready to connect', decorators: [withStore({ connection: null, events: [], notice: null })] };
export const ReauthorizationRequired: Story = { name: 'Reauthorization required', decorators: [withStore({ connection: { ...connectedConnection, status: 'reauthorization_required', readsEnabled: false, availableActions: ['reauthorize'] } as unknown as CalendarConnection, events: [], error: { message: 'Calendar access needs your attention.', recovery: 'reauthorize', retryable: false } as CalendarClientError })] };
export const RevocationUncertain: Story = { name: 'Revocation uncertain', decorators: [withStore({ connection: { ...connectedConnection, status: 'revocation_uncertain', readsEnabled: false, availableActions: [] } as unknown as CalendarConnection, events: [] })] };
export const PartialRead: Story = { name: 'Partial calendar read', decorators: [withStore({ partial: true, notice: 'Some calendars could not be read. Review the warning and retry when ready.' })] };
export const StaleSnapshot: Story = { name: 'Stale snapshot after retryable failure', decorators: [withStore({ stale: true, notice: 'Showing the last calendar snapshot. Refresh when ready.', error: { message: 'Calendar agenda is temporarily unavailable.', recovery: 'retry', retryable: true } as CalendarClientError })] };
export const RateLimited: Story = { name: 'Rate limited recovery', decorators: [withStore({ connection: connectedConnection, events: [], notice: 'Calendar reads are temporarily rate-limited.', error: { message: 'Calendar reads are temporarily rate-limited.', recovery: 'wait', retryable: true } as CalendarClientError })] };
export const EmptySelection: Story = { name: 'Explicit empty selection', decorators: [withStore({ calendars: [calendar('personal', 'Personal'), calendar('team', 'Team calendar')], events: [], notice: 'Empty selection is allowed.' })] };
export const LongTitles: Story = { name: 'Long multi-day event titles', decorators: [withStore({ events: longTitleEvents })] };
