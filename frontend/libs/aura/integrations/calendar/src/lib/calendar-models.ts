import type {
  CalendarAuthorizationStart,
  CalendarConnection,
  CalendarConnectionAction,
  CalendarConnectionError,
  CalendarConnectionStatus,
  CalendarConflict,
  CalendarConflictQuery,
  CalendarConflictPage,
  CalendarEvent,
  CalendarEventQuery,
  CalendarEventPage,
  CalendarFailureCategory,
  CalendarReadWarning,
  CalendarSelection,
  ClientTimeZone,
  SelectableCalendar,
  SelectableCalendarPage,
} from '@aura/aura-api-client';

export type {
  CalendarAuthorizationStart,
  CalendarConnection,
  CalendarConnectionAction,
  CalendarConnectionError,
  CalendarConnectionStatus,
  CalendarConflict,
  CalendarConflictQuery,
  CalendarConflictPage,
  CalendarEvent,
  CalendarEventQuery,
  CalendarEventPage,
  CalendarFailureCategory,
  CalendarReadWarning,
  CalendarSelection,
  ClientTimeZone,
  SelectableCalendar,
  SelectableCalendarPage,
};

export type CalendarRequestState = 'idle' | 'loading' | 'refreshing' | 'saving' | 'error';
export type CalendarRecovery = 'none' | 'retry' | 'reauthorize' | 'connect' | 'complete_connection' | 'disconnect' | 'wait' | 'contact_administrator';
export type CalendarFailureScope = 'session' | 'calendar' | 'retry';

export function handleCalendarAuthorizationFailure(status: number | null, handlers: { readonly resetSession: () => void; readonly clearPrivateCalendarState: () => void }): CalendarFailureScope {
  if (status === 401) { handlers.resetSession(); return 'session'; }
  if (status === 403) { handlers.clearPrivateCalendarState(); return 'calendar'; }
  return 'retry';
}

export interface CalendarViewState {
  readonly connection: CalendarConnection | null;
  readonly calendars: ReadonlyArray<SelectableCalendar>;
  readonly selectionVersion: number | null;
  readonly nextCalendarCursor: string | null;
  readonly events: CalendarEventPage | null;
  readonly conflicts: CalendarConflictPage | null;
  readonly selectedDate: string;
  readonly displayTimeZone: ClientTimeZone;
  readonly requestState: CalendarRequestState;
  readonly error: CalendarClientError | null;
}

export class CalendarClientError extends Error {
  readonly status: number | null;
  readonly category: CalendarFailureCategory | null;
  readonly retryable: boolean;
  readonly recovery: CalendarRecovery;

  constructor(message: string, options: { readonly status?: number | null; readonly category?: CalendarFailureCategory | null; readonly retryable?: boolean; readonly recovery?: CalendarRecovery } = {}) {
    super(message);
    this.name = 'CalendarClientError';
    this.status = options.status ?? null;
    this.category = options.category ?? null;
    this.retryable = options.retryable ?? false;
    this.recovery = options.recovery ?? 'none';
  }
}

export interface CalendarApi {
  getConnection(): Promise<CalendarConnection>;
  startConnection(): Promise<CalendarAuthorizationStart>;
  completeConnectionCallback(): Promise<CalendarConnection>;
  reauthorizeConnection(): Promise<CalendarAuthorizationStart>;
  disconnectConnection(): Promise<CalendarConnection>;
  listCalendars(cursor?: string, limit?: number): Promise<SelectableCalendarPage>;
  replaceSelection(calendarIds: ReadonlyArray<string>, expectedVersion: number): Promise<CalendarSelection>;
  queryEvents(input: CalendarEventQueryInput): Promise<CalendarEventPage>;
  queryConflicts(input: CalendarConflictQueryInput): Promise<CalendarConflictPage>;
  clearSession?(): void;
}

export type CalendarEventQueryInput = CalendarEventQuery;
export type CalendarConflictQueryInput = CalendarConflictQuery;

export type CalendarAgendaEvent = CalendarEvent & { readonly sourceLabel: 'Calendar'; readonly timeZoneLabel: string };
export interface CalendarAgendaProjection {
  readonly events: ReadonlyArray<CalendarAgendaEvent>;
  readonly allDayEvents: ReadonlyArray<CalendarAgendaEvent>;
  readonly timedEvents: ReadonlyArray<CalendarAgendaEvent>;
  readonly conflicts: ReadonlyArray<CalendarConflict>;
  readonly warnings: ReadonlyArray<CalendarReadWarning>;
  readonly partial: boolean;
  readonly stale: boolean;
  readonly selectedDate: string;
  readonly displayTimeZone: ClientTimeZone;
}

export const MAX_CALENDAR_PAGES = 4;
export const MAX_CALENDAR_AGENDA_PAGES = 4;
export const CALENDAR_PAGE_LIMIT = 100;
export const CALENDAR_EVENT_LIMIT = 100;
export const CALENDAR_CONFLICT_LIMIT = 100;

export function isCalendarConnectionActionAvailable(connection: CalendarConnection | null, action: CalendarConnectionAction): boolean {
  return connection?.availableActions.includes(action) ?? false;
}

export function initialCalendarSelectionVersion(startingCursor: string | null | undefined, currentVersion: number | null): number | null {
  return startingCursor ? currentVersion : null;
}

export function displayTimeZoneForEvent(event: CalendarEvent): string {
  return event.eventTimeZone === event.displayTimeZone ? event.displayTimeZone : `${event.eventTimeZone} · shown in ${event.displayTimeZone}`;
}

export function calendarDayRange(date: string, timeZone: ClientTimeZone): { readonly rangeStart: string; readonly rangeEnd: string } {
  const nextDate = new Date(`${date}T00:00:00Z`);
  nextDate.setUTCDate(nextDate.getUTCDate() + 1);
  return { rangeStart: zonedMidnight(date, timeZone).toISOString(), rangeEnd: zonedMidnight(nextDate.toISOString().slice(0, 10), timeZone).toISOString() };
}

function zonedMidnight(date: string, timeZone: ClientTimeZone): Date {
  const guess = Date.parse(`${date}T00:00:00Z`);
  try {
    const parts = new Intl.DateTimeFormat('en-US', { timeZone, hour12: false, year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit' }).formatToParts(new Date(guess));
    const values = Object.fromEntries(parts.filter((part) => part.type !== 'literal').map((part) => [part.type, part.value]));
    const localAtGuess = Date.UTC(Number(values['year']), Number(values['month']) - 1, Number(values['day']), Number(values['hour']) % 24, Number(values['minute']), Number(values['second']));
    return new Date(guess - (localAtGuess - guess));
  } catch {
    return new Date(`${date}T00:00:00Z`);
  }
}

export function projectCalendarAgenda(eventsPage: CalendarEventPage | null, conflictsPage: CalendarConflictPage | null, selectedDate: string, displayTimeZone: ClientTimeZone, stale = false): CalendarAgendaProjection {
  const events = (eventsPage?.items ?? []).map((event) => ({ ...event, sourceLabel: 'Calendar' as const, timeZoneLabel: displayTimeZoneForEvent(event) }));
  const allDayEvents = events.filter((event) => event.kind === 'all_day');
  const timedEvents = events.filter((event) => event.kind === 'timed').sort((left, right) => left.kind === 'timed' && right.kind === 'timed' ? left.startInstant.localeCompare(right.startInstant) : 0);
  return {
    events: [...allDayEvents, ...timedEvents],
    allDayEvents,
    timedEvents,
    conflicts: conflictsPage?.items ?? [],
    warnings: [...(eventsPage?.warnings ?? []), ...(conflictsPage?.warnings ?? [])],
    partial: Boolean(eventsPage?.partial || conflictsPage?.partial),
    stale,
    selectedDate,
    displayTimeZone,
  };
}
