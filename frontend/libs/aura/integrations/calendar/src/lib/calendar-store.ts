import { Injectable, signal, computed } from '@angular/core';
import { AuraCalendarApi } from './calendar-api';
import { CALENDAR_CONFLICT_LIMIT, CALENDAR_EVENT_LIMIT, CALENDAR_PAGE_LIMIT, MAX_CALENDAR_AGENDA_PAGES, MAX_CALENDAR_PAGES, CalendarClientError, calendarDayRange, handleCalendarAuthorizationFailure, initialCalendarSelectionVersion, projectCalendarAgenda, type CalendarAgendaProjection, type CalendarConnection, type CalendarConflictPage, type CalendarConflictQueryInput, type CalendarEventPage, type CalendarEventQueryInput, type CalendarViewState, type ClientTimeZone, type SelectableCalendar } from './calendar-models';

const todayInBrowserTimeZone = (): string => {
  const now = new Date();
  const formatter = new Intl.DateTimeFormat('en-CA', { year: 'numeric', month: '2-digit', day: '2-digit' });
  return formatter.format(now);
};

const browserTimeZone = (): ClientTimeZone => {
  try { return Intl.DateTimeFormat().resolvedOptions().timeZone as ClientTimeZone; } catch { return 'UTC' as ClientTimeZone; }
};

const statusOf = (error: unknown): number | null => typeof (error as { readonly status?: unknown })?.status === 'number' ? (error as { readonly status: number }).status : null;

@Injectable({ providedIn: 'root' })
export class CalendarStore {
  readonly connection = signal<CalendarConnection | null>(null);
  readonly calendars = signal<ReadonlyArray<SelectableCalendar>>([]);
  readonly selectionVersion = signal<number | null>(null);
  readonly selectedCalendarIds = signal<ReadonlyArray<string>>([]);
  readonly calendarsComplete = signal(false);
  readonly events = signal<CalendarViewState['events']>(null);
  readonly conflicts = signal<CalendarViewState['conflicts']>(null);
  readonly selectedDate = signal(todayInBrowserTimeZone());
  readonly displayTimeZone = signal<ClientTimeZone>(browserTimeZone());
  readonly requestState = signal<'idle' | 'loading' | 'refreshing' | 'saving' | 'error'>('idle');
  readonly error = signal<CalendarClientError | null>(null);
  readonly notice = signal<string | null>(null);
  readonly resetEpoch = signal(0);
  readonly agenda = computed<CalendarAgendaProjection>(() => projectCalendarAgenda(this.events(), this.conflicts(), this.selectedDate(), this.displayTimeZone(), this.requestState() === 'error' && this.events() !== null));
  readonly connected = computed(() => this.connection()?.status === 'connected' && this.connection()?.readsEnabled === true);
  readonly canConnect = computed(() => this.connection()?.availableActions.includes('connect') ?? true);
  readonly canComplete = computed(() => this.connection()?.availableActions.includes('complete_connection') ?? false);
  readonly canReauthorize = computed(() => this.connection()?.availableActions.includes('reauthorize') ?? false);
  readonly canDisconnect = computed(() => this.connection()?.availableActions.includes('disconnect') ?? false);

  private sessionGeneration = 0;
  private connectionGeneration = 0;
  private calendarGeneration = 0;
  private agendaGeneration = 0;
  private selectionGeneration = 0;
  private nextCalendarCursor: string | null = null;
  private pendingAuthorizationPath: string | null = null;
  private sessionResetHandler: ((status?: 401 | 403) => void) | null = null;

  constructor(private readonly api: AuraCalendarApi) {}

  async load(): Promise<void> {
    const session = this.sessionGeneration;
    const generation = ++this.connectionGeneration;
    ++this.calendarGeneration; ++this.agendaGeneration; this.nextCalendarCursor = null; this.calendarsComplete.set(false);
    this.requestState.set('loading'); this.error.set(null);
    try {
      const connection = await this.api.getConnection();
      if (!this.isCurrent(session, generation, this.connectionGeneration)) return;
      this.connection.set(connection);
      if (connection.status === 'connected' && connection.readsEnabled) await this.discoverCalendars(true, false);
    } catch (error) {
      if (this.isCurrent(session, generation, this.connectionGeneration)) this.fail(error, 'Calendar connection status is unavailable.');
    } finally {
      if (this.isCurrent(session, generation, this.connectionGeneration) && this.requestState() !== 'error') this.requestState.set('idle');
    }
  }

  async discoverCalendars(hydrateAgenda = true, continueDiscovery = true): Promise<void> {
    const session = this.sessionGeneration; const generation = ++this.calendarGeneration;
    ++this.agendaGeneration; this.events.set(null); this.conflicts.set(null);
    const startingCursor = continueDiscovery ? this.nextCalendarCursor : null;
    const discoveredItems = startingCursor ? [...this.calendars()] : [];
    this.calendarsComplete.set(false);
    this.requestState.set('loading'); this.error.set(null);
    const items: SelectableCalendar[] = discoveredItems; let cursor: string | undefined = startingCursor ?? undefined; let pageCount = 0; let version: number | null = initialCalendarSelectionVersion(startingCursor, this.selectionVersion()); const seenCursors = new Set<string>();
    try {
      do {
        if (cursor) {
          if (seenCursors.has(cursor)) break;
          seenCursors.add(cursor);
        }
        const page = await this.api.listCalendars(cursor, CALENDAR_PAGE_LIMIT);
        if (!this.isCurrent(session, generation, this.calendarGeneration)) return;
        items.push(...page.items.slice(0, CALENDAR_PAGE_LIMIT)); version = version ?? page.selectionVersion; cursor = page.nextCursor ?? undefined; pageCount += 1;
      } while (cursor && pageCount < MAX_CALENDAR_PAGES);
      this.nextCalendarCursor = cursor ?? null;
      this.calendarsComplete.set(!cursor);
      this.calendars.set(items); this.selectionVersion.set(version); this.selectedCalendarIds.set(items.filter((item) => item.selected).map((item) => item.id));
      this.notice.set(cursor ? 'Some calendars are not shown yet. Refresh to continue discovery before saving selection.' : null);
      if (hydrateAgenda && !cursor && this.isCurrent(session, generation, this.calendarGeneration)) await this.loadAgenda();
    } catch (error) { if (this.isCurrent(session, generation, this.calendarGeneration)) this.fail(error, 'Calendars could not be discovered.'); }
    finally { if (this.isCurrent(session, generation, this.calendarGeneration) && this.requestState() !== 'error') this.requestState.set('idle'); }
  }

  setDate(date: string): void { if (/^\d{4}-\d{2}-\d{2}$/.test(date)) { ++this.agendaGeneration; this.selectedDate.set(date); this.events.set(null); this.conflicts.set(null); } }
  setDisplayTimeZone(timeZone: ClientTimeZone): void { if (timeZone.trim()) { ++this.agendaGeneration; this.displayTimeZone.set(timeZone); this.events.set(null); this.conflicts.set(null); } }
  setCalendarSelection(calendarIds: ReadonlyArray<string>): void {
    const available = new Set(this.calendars().map((calendar) => calendar.id));
    const bounded = [...new Set(calendarIds)].filter((id) => available.has(id));
    ++this.agendaGeneration; this.events.set(null); this.conflicts.set(null);
    this.selectedCalendarIds.set(bounded);
  }

  async replaceSelection(calendarIds = this.selectedCalendarIds()): Promise<boolean> {
    const expectedVersion = this.selectionVersion();
    if (expectedVersion === null || !this.calendarsComplete() || this.requestState() === 'saving') return false;
    const session = this.sessionGeneration; const generation = ++this.selectionGeneration;
    this.requestState.set('saving'); this.error.set(null);
    try {
      const result = await this.api.replaceSelection([...new Set(calendarIds)].filter((id) => this.calendars().some((calendar) => calendar.id === id)), expectedVersion);
      if (!this.isCurrent(session, generation, this.selectionGeneration)) return false;
      this.selectionVersion.set(result.version); this.selectedCalendarIds.set(result.calendarIds); this.calendars.update((items) => items.map((item) => ({ ...item, selected: result.calendarIds.includes(item.id) })));
      this.notice.set('Calendar selection saved.'); await this.loadAgenda(); return true;
    } catch (error) { if (this.isCurrent(session, generation, this.selectionGeneration)) this.fail(error, statusOf(error) === 409 ? 'Calendar selection changed elsewhere. Refresh and review it before saving again.' : 'Calendar selection could not be saved.'); return false; }
    finally { if (this.isCurrent(session, generation, this.selectionGeneration) && this.requestState() !== 'error') this.requestState.set('idle'); }
  }

  async loadAgenda(): Promise<void> {
    const connection = this.connection(); const calendarIds = this.selectedCalendarIds();
    if (!connection?.readsEnabled || !calendarIds.length) { this.events.set(null); this.conflicts.set(null); return; }
    const session = this.sessionGeneration; const generation = ++this.agendaGeneration;
    this.requestState.set(this.events() ? 'refreshing' : 'loading'); this.error.set(null);
    const range = calendarDayRange(this.selectedDate(), this.displayTimeZone());
    const input: CalendarEventQueryInput = { ...range, displayTimeZone: this.displayTimeZone(), calendarIds, limit: CALENDAR_EVENT_LIMIT };
    const conflictInput: CalendarConflictQueryInput = { ...range, displayTimeZone: this.displayTimeZone(), calendarIds, conflictLimit: CALENDAR_CONFLICT_LIMIT };
    try {
      const [events, conflicts] = await Promise.all([this.loadEventPages(input, session, generation), this.loadConflictPages(conflictInput, session, generation)]);
      if (!this.isCurrent(session, generation, this.agendaGeneration)) return;
      this.events.set(events); this.conflicts.set(conflicts); this.notice.set(events.partial || conflicts.partial ? 'Some calendars could not be read. Review the warning and retry when ready.' : null);
    } catch (error) { if (this.isCurrent(session, generation, this.agendaGeneration)) this.fail(error, 'Calendar agenda is temporarily unavailable.'); }
    finally { if (this.isCurrent(session, generation, this.agendaGeneration) && this.requestState() !== 'error') this.requestState.set('idle'); }
  }

  async connect(): Promise<boolean> { return this.beginAuthorization('connect'); }
  async reauthorize(): Promise<boolean> { return this.beginAuthorization('reauthorize'); }
  async completeConnection(): Promise<boolean> {
    const session = this.sessionGeneration; const generation = ++this.connectionGeneration; this.requestState.set('loading'); this.error.set(null);
    try { const result = await this.api.completeConnectionCallback(); if (!this.isCurrent(session, generation, this.connectionGeneration)) return false; this.connection.set(result); this.pendingAuthorizationPath = null; this.nextCalendarCursor = null; await this.discoverCalendars(true, false); return true; }
    catch (error) { if (this.isCurrent(session, generation, this.connectionGeneration)) this.fail(error, 'Calendar connection could not be completed.'); return false; }
  }
  async disconnect(): Promise<boolean> {
    const session = this.sessionGeneration; const generation = ++this.connectionGeneration; this.requestState.set('saving'); this.error.set(null);
    try { const result = await this.api.disconnectConnection(); if (!this.isCurrent(session, generation, this.connectionGeneration)) return false; this.connection.set(result); this.clearPrivateContent(); this.notice.set('Calendar access disconnected.'); return true; }
    catch (error) { if (this.isCurrent(session, generation, this.connectionGeneration)) this.fail(error, 'Calendar access could not be disconnected.'); return false; }
    finally { if (this.isCurrent(session, generation, this.connectionGeneration) && this.requestState() !== 'error') this.requestState.set('idle'); }
  }

  openAuthorization(): boolean {
    const path = this.pendingAuthorizationPath;
    if (!path || !this.isSafeAuthorizationPath(path)) return false;
    // This is intentionally only called from an explicit owner click. The opaque
    // path never enters router state, storage, logs, or telemetry.
    if (typeof window !== 'undefined') window.location.assign(path);
    return true;
  }
  hasPendingAuthorization(): boolean { return this.pendingAuthorizationPath !== null; }
  clearPrivateContent(): void {
    ++this.connectionGeneration; ++this.calendarGeneration; ++this.agendaGeneration; ++this.selectionGeneration;
    this.pendingAuthorizationPath = null; this.nextCalendarCursor = null;
    this.events.set(null); this.conflicts.set(null); this.calendars.set([]); this.calendarsComplete.set(false); this.selectionVersion.set(null); this.selectedCalendarIds.set([]);
    this.requestState.set('idle'); this.error.set(null); this.notice.set(null);
  }
  registerSessionResetHandler(handler: ((status?: 401 | 403) => void) | null): void { this.sessionResetHandler = handler; }
  resetSessionState(): void {
    ++this.sessionGeneration; ++this.connectionGeneration; ++this.calendarGeneration; ++this.agendaGeneration; ++this.selectionGeneration;
    this.pendingAuthorizationPath = null; this.api.clearSession?.(); this.connection.set(null); this.calendars.set([]); this.calendarsComplete.set(false); this.selectionVersion.set(null); this.selectedCalendarIds.set([]); this.clearPrivateContent(); this.requestState.set('idle'); this.error.set(null); this.notice.set(null); this.resetEpoch.update((value) => value + 1);
  }

  private async beginAuthorization(kind: 'connect' | 'reauthorize'): Promise<boolean> {
    const session = this.sessionGeneration; const generation = ++this.connectionGeneration; this.requestState.set('saving'); this.error.set(null);
    try { const result = kind === 'connect' ? await this.api.startConnection() : await this.api.reauthorizeConnection(); if (!this.isCurrent(session, generation, this.connectionGeneration)) return false; this.connection.set(result.connection); this.pendingAuthorizationPath = this.isSafeAuthorizationPath(result.authorizationPath) ? result.authorizationPath : null; if (!this.pendingAuthorizationPath) throw new CalendarClientError('The authorization request was not valid.', { recovery: 'retry' }); this.notice.set('Continue to Google in the new authorization step.'); return true; }
    catch (error) { if (this.isCurrent(session, generation, this.connectionGeneration)) this.fail(error, 'Calendar authorization could not be started.'); return false; }
    finally { if (this.isCurrent(session, generation, this.connectionGeneration) && this.requestState() !== 'error') this.requestState.set('idle'); }
  }

  private fail(error: unknown, fallback: string): void {
    const status = statusOf(error);
    const failureScope = handleCalendarAuthorizationFailure(status, { resetSession: () => this.resetSessionState(), clearPrivateCalendarState: () => this.clearPrivateContent() });
    if (failureScope === 'session') { this.sessionResetHandler?.(401); this.error.set(new CalendarClientError('Your session has expired. Sign in again.', { status, recovery: 'connect' })); return; }
    if (failureScope === 'calendar') { this.error.set(new CalendarClientError('You do not have permission to view calendar data.', { status, recovery: 'contact_administrator' })); this.requestState.set('error'); return; }
    this.error.set(error instanceof CalendarClientError ? error : new CalendarClientError(fallback, { status, retryable: true, recovery: 'retry' })); this.requestState.set('error');
  }
  private isCurrent(session: number, generation: number, current: number): boolean { return session === this.sessionGeneration && generation === current; }
  private async loadEventPages(input: CalendarEventQueryInput, session: number, generation: number): Promise<CalendarEventPage> {
    const pages: CalendarEventPage[] = []; let cursor: string | undefined; const seenCursors = new Set<string>();
    while (pages.length < MAX_CALENDAR_AGENDA_PAGES) {
      const page = await this.api.queryEvents(cursor ? { ...input, cursor } : input);
      if (!this.isCurrent(session, generation, this.agendaGeneration)) throw new CalendarClientError('Calendar agenda refresh was superseded.', { recovery: 'none' });
      pages.push(page); cursor = page.nextCursor ?? undefined;
      if (!cursor || seenCursors.has(cursor)) break;
      seenCursors.add(cursor);
    }
    const first = pages[0]; const truncated = Boolean(cursor);
    return { ...first, items: pages.flatMap((page) => page.items), nextCursor: cursor ?? null, partial: truncated || pages.some((page) => page.partial), warnings: pages.flatMap((page) => page.warnings) };
  }
  private async loadConflictPages(input: CalendarConflictQueryInput, session: number, generation: number): Promise<CalendarConflictPage> {
    const pages: CalendarConflictPage[] = []; let cursor: string | undefined; const seenCursors = new Set<string>();
    while (pages.length < MAX_CALENDAR_AGENDA_PAGES) {
      const page = await this.api.queryConflicts(cursor ? { ...input, cursor } : input);
      if (!this.isCurrent(session, generation, this.agendaGeneration)) throw new CalendarClientError('Calendar agenda refresh was superseded.', { recovery: 'none' });
      pages.push(page); cursor = page.nextCursor ?? undefined;
      if (!cursor || seenCursors.has(cursor)) break;
      seenCursors.add(cursor);
    }
    const first = pages[0]; const truncated = Boolean(cursor);
    return { ...first, items: pages.flatMap((page) => page.items), nextCursor: cursor ?? null, partial: truncated || pages.some((page) => page.partial), warnings: pages.flatMap((page) => page.warnings) };
  }
  private isSafeAuthorizationPath(path: string): boolean { if (!path.startsWith('/') || path.startsWith('//') || path.includes('\\') || path.includes('\n') || path.includes('\r')) return false; try { const url = new URL(path, typeof window !== 'undefined' ? window.location.origin : 'https://aura.invalid'); return url.origin === (typeof window !== 'undefined' ? window.location.origin : 'https://aura.invalid'); } catch { return false; } }
}
