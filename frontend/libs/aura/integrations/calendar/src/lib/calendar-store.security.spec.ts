import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { handleCalendarAuthorizationFailure } from './calendar-models';

const storeSource = readFileSync('libs/aura/integrations/calendar/src/lib/calendar-store.ts', 'utf8');

describe('CalendarStore authorization isolation', () => {
  it('keeps a calendar permission failure scoped without invoking the shared session reset', () => {
    let globalResetCount = 0;
    let calendarClearCount = 0;

    const scope = handleCalendarAuthorizationFailure(403, {
      resetSession: () => { globalResetCount += 1; },
      clearPrivateCalendarState: () => { calendarClearCount += 1; },
    });

    expect(scope).toBe('calendar');
    expect(globalResetCount).toBe(0);
    expect(calendarClearCount).toBe(1);
  });

  it('escalates a true session expiry to the shared session reset boundary', () => {
    let globalResetCount = 0;
    let calendarClearCount = 0;

    const scope = handleCalendarAuthorizationFailure(401, {
      resetSession: () => { globalResetCount += 1; },
      clearPrivateCalendarState: () => { calendarClearCount += 1; },
    });

    expect(scope).toBe('session');
    expect(globalResetCount).toBe(1);
    expect(calendarClearCount).toBe(0);
  });

  it('invalidates every private request generation and clears deferred-response state on teardown', () => {
    expect(storeSource).toContain('++this.connectionGeneration; ++this.calendarGeneration; ++this.agendaGeneration; ++this.selectionGeneration;');
    expect(storeSource).toContain('this.pendingAuthorizationPath = null; this.nextCalendarCursor = null;');
    expect(storeSource).toContain('this.events.set(null); this.conflicts.set(null);');
    expect(storeSource).toContain('this.requestState.set(\'idle\'); this.error.set(null); this.notice.set(null);');
    expect(storeSource).toContain('clearPrivateCalendarState: () => this.clearPrivateContent()');
    expect(storeSource).toContain('if (!this.isCurrent(session, generation, this.agendaGeneration)) throw new CalendarClientError');
  });

});
