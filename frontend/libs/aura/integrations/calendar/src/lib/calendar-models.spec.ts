import { describe, expect, it } from 'vitest';
import { calendarDayRange, displayTimeZoneForEvent, initialCalendarSelectionVersion, projectCalendarAgenda, type CalendarEventPage } from './calendar-models';

const page = (items: CalendarEventPage['items']): CalendarEventPage => ({ rangeStart: '2026-10-27T00:00:00Z', rangeEnd: '2026-10-28T00:00:00Z', displayTimeZone: 'Europe/Brussels', items, nextCursor: null, partial: false, warnings: [], retrievedAt: '2026-10-27T08:00:00Z' });

describe('calendar public projections', () => {
  it('adopts the refreshed selection version after a stale 409 and preserves cursor-chain versions', () => {
    let version = initialCalendarSelectionVersion(undefined, 7);
    version = version ?? 8;
    expect(version).toBe(8);
    expect(initialCalendarSelectionVersion('cursor-1', 7)).toBe(7);
  });

  it('keeps all-day events before timed events and preserves server conflicts', () => {
    const events = page([
      { kind: 'timed', id: 'timed', calendarId: 'work', title: 'Timed', description: null, location: null, availability: 'busy', startInstant: '2026-10-27T09:00:00Z', endInstant: '2026-10-27T10:00:00Z', eventTimeZone: 'Europe/Brussels', displayTimeZone: 'Europe/Brussels', spansMultipleDays: false },
      { kind: 'all_day', id: 'all-day', calendarId: 'work', title: 'All day', description: null, location: null, availability: 'free', startDate: '2026-10-27', endDateExclusive: '2026-10-28', eventTimeZone: 'America/New_York', displayTimeZone: 'Europe/Brussels', spansMultipleDays: false },
    ]);
    const conflicts = { rangeStart: events.rangeStart, rangeEnd: events.rangeEnd, displayTimeZone: events.displayTimeZone, items: [{ left: { eventId: 'timed', calendarId: 'work' }, right: { eventId: 'all-day', calendarId: 'work' }, relationship: 'overlaps' as const, overlapStart: events.rangeStart, overlapEnd: events.rangeEnd }], nextCursor: null, partial: false, warnings: [], evaluatedAt: events.retrievedAt };
    const projection = projectCalendarAgenda(events, conflicts, '2026-10-27', 'Europe/Brussels');
    expect(projection.events.map((event) => event.id)).toEqual(['all-day', 'timed']);
    expect(projection.conflicts).toHaveLength(1);
    expect(projection.allDayEvents[0].timeZoneLabel).toContain('America/New_York');
  });

  it('does not manufacture a conflict when Core returned none', () => {
    const events = page([{ kind: 'timed', id: 'one', calendarId: 'work', title: 'One', description: null, location: null, availability: 'busy', startInstant: '2026-10-27T09:00:00Z', endInstant: '2026-10-27T10:00:00Z', eventTimeZone: 'UTC', displayTimeZone: 'UTC', spansMultipleDays: false }]);
    expect(projectCalendarAgenda(events, null, '2026-10-27', 'UTC').conflicts).toEqual([]);
  });

  it('uses provider-safe timezone provenance labels', () => {
    const event = page([{ kind: 'timed', id: 'one', calendarId: 'work', title: 'One', description: null, location: null, availability: 'busy', startInstant: '2026-10-27T09:00:00Z', endInstant: '2026-10-27T10:00:00Z', eventTimeZone: 'UTC', displayTimeZone: 'UTC', spansMultipleDays: false }]).items[0];
    expect(displayTimeZoneForEvent(event)).toBe('UTC');
  });

  it('bounds a visible day in the selected IANA timezone across DST', () => {
    const range = calendarDayRange('2026-03-29', 'Europe/Brussels');
    expect(range.rangeStart).toBe('2026-03-28T23:00:00.000Z');
    expect(range.rangeEnd).toBe('2026-03-29T22:00:00.000Z');
  });
});
