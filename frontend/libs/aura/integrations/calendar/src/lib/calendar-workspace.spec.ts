import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';

const page = readFileSync('libs/aura/integrations/calendar/src/lib/calendar-management-page.component.html', 'utf8');
const collection = readFileSync('libs/aura/integrations/calendar/src/lib/integrations-page.component.html', 'utf8');
const api = readFileSync('libs/aura/integrations/calendar/src/lib/calendar-api.ts', 'utf8');
const store = readFileSync('libs/aura/integrations/calendar/src/lib/calendar-store.ts', 'utf8');
const pages = readFileSync('libs/aura/integrations/calendar/src/lib/calendar-pages.component.ts', 'utf8');
const managementStyles = readFileSync('libs/aura/integrations/calendar/src/lib/calendar-management-page.component.css', 'utf8');
const collectionStyles = readFileSync('libs/aura/integrations/calendar/src/lib/calendar-pages.component.css', 'utf8');

describe('calendar workspace contracts', () => {
  it('has accessible connection, selection, and agenda landmarks', () => {
    expect(page).toContain('aria-labelledby="calendar-status-heading"');
    expect(page).toContain('aria-labelledby="connection-heading"');
    expect(page).toContain('aria-labelledby="selection-heading"');
    expect(page).toContain('aria-labelledby="agenda-heading"');
    expect(page).toContain('type="checkbox"');
    expect(page).toContain('role="status"');
  });

  it('keeps transport access in the generated Aura client adapter', () => {
    expect(api).toContain("this.client.execute('getCalendarConnection'");
    expect(api).toContain("this.client.execute('replaceCalendarSelection'");
    expect(api).toContain("'X-CSRF-Token': this.session.currentToken()");
    expect(api).toContain("'Idempotency-Key': requestId()");
    expect(api).toContain('crypto.randomUUID()');
    expect(api).not.toContain('Date.now()');
    expect(api).not.toContain('Math.random()');
    expect(api).toContain('Secure mutation identity is unavailable');
    expect(api).toContain("credentials: 'include'");
  });

  it('bounds discovery, preserves authoritative selection, suppresses late responses, and keeps private data ephemeral', () => {
    expect(store).toContain('MAX_CALENDAR_PAGES');
    expect(store).toContain('MAX_CALENDAR_AGENDA_PAGES');
    expect(store).toContain('private sessionGeneration = 0');
    expect(store).toContain('private connectionGeneration = 0');
    expect(store).toContain('private calendarGeneration = 0');
    expect(store).toContain('private agendaGeneration = 0');
    expect(store).toContain('private selectionGeneration = 0');
    expect(store).toContain('this.events.set(null)');
    expect(store).toContain('this.conflicts.set(null)');
    expect(store).toContain('private pendingAuthorizationPath: string | null = null');
    expect(store).toContain('this.pendingAuthorizationPath = null');
    expect(store).toContain('this.calendarsComplete.set(false)');
    expect(store).toContain('if (expectedVersion === null || !this.calendarsComplete()');
    expect(store).toContain('nextCalendarCursor');
    expect(store).toContain('initialCalendarSelectionVersion(startingCursor, this.selectionVersion())');
    expect(store).toContain('version = version ?? page.selectionVersion');
    expect(store).toContain('nextCursor: cursor ?? null');
    expect(store).toContain('partial: truncated || pages.some((page) => page.partial)');
    expect(store).toContain('const conflictInput: CalendarConflictQueryInput');
    expect(store).toContain('conflictLimit: CALENDAR_CONFLICT_LIMIT');
    expect(store).not.toContain('{ ...input, conflictLimit: CALENDAR_CONFLICT_LIMIT }');
    expect(store).toContain('this.isSafeAuthorizationPath');
    expect(store).toContain('await this.discoverCalendars(true, false)');
    expect(store).toContain('if (hydrateAgenda && !cursor && this.isCurrent(session, generation, this.calendarGeneration)) await this.loadAgenda()');
    expect(store).toContain('this.requestState.set(\'idle\')');
  });

  it('renders the authoritative connection error with a closed recovery guide', () => {
    expect(page).toContain('store.connection()?.lastError');
    expect(page).toContain('lastError.message');
    expect(page).toContain('connectionRecoveryGuidance(lastError.recoveryAction)');
    expect(collection).toContain('store.connection()?.availableActions');
    expect(collection).toContain('store.connection()?.readsEnabled');
    expect(collection).toContain('store.connection()?.lastError');
    expect(collection).toContain('connectionRecoveryGuidance(lastError.recoveryAction)');
    expect(page).toContain('store.connection()?.availableActions');
    expect(page).toContain('store.connection()?.readsEnabled');
    expect(page).toContain('confirmingDisconnect()');
    expect(page).toContain('busy()');
    expect(page).toContain('id="refresh-agenda"');
    expect(pages).toContain("this.settleFocus(this.store.error() ? null : 'refresh-agenda')");
    expect(page).toContain("error.recovery !== 'wait'");
    expect(page).toContain("error.recovery !== 'connect'");
    expect(pages).toContain("styleUrl: './calendar-management-page.component.css'");
    expect(pages).toContain('await this.store.load()');
    expect(pages).toContain('this.store.connection()?.status === \'connecting\'');
    expect(pages).toContain('this.store.completeConnection()');
    expect(pages).toContain('afterNextRender(callback, { injector: this.injector })');
    expect(pages).toContain('const needsRecoveryFocus = error?.retryable === true');
    expect(pages).toContain('this.afterRender(() => { const recoveryAction');
    expect(managementStyles).toContain('min-height: 2.75rem');
    expect(managementStyles).toContain('width: 2.75rem; height: 2.75rem');
    expect(collectionStyles).toContain('min-height: 2.75rem');
  });

  it('uses server conflict pages as the only conflict authority', () => {
    expect(store).toContain("this.api.queryConflicts");
    expect(store).not.toContain('calculateConflicts');
    expect(store).not.toContain('overlapStart <');
  });
});
