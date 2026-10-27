import { InjectionToken, type Signal } from '@angular/core';

/**
 * The deliberately small projection that Today consumes from the calendar
 * integration feature. Calendar transport, lifecycle mutations, pagination,
 * and conflict authority remain behind the integrations-calendar public API.
 */
export type DailyCalendarAgendaStatus =
  | 'loading'
  | 'ready'
  | 'disconnected'
  | 'connecting'
  | 'reauthorization-required'
  | 'revocation-uncertain'
  | 'disabled'
  | 'unavailable'
  | 'partial'
  | 'stale'
  | 'rate-limited'
  | 'permission-denied'
  | 'offline'
  | 'reconnecting'
  | 'empty';

export interface DailyCalendarEventProjection {
  readonly kind: 'timed' | 'all_day';
  readonly id: string;
  readonly calendarId: string;
  readonly title: string;
  readonly location: string | null;
  readonly availability: 'busy' | 'free';
  readonly startInstant?: string;
  readonly endInstant?: string;
  readonly startDate?: string;
  readonly endDateExclusive?: string;
  readonly eventTimeZone: string;
  readonly displayTimeZone: string;
  readonly spansMultipleDays: boolean;
}

export interface DailyCalendarConflictProjection {
  readonly left: { readonly eventId: string; readonly calendarId: string };
  readonly right: { readonly eventId: string; readonly calendarId: string };
  readonly relationship: 'overlaps' | 'contains' | 'contained_by' | 'same_interval';
  readonly overlapStart: string;
  readonly overlapEnd: string;
}

export interface DailyCalendarAgendaProjection {
  /** Start the owner-scoped read lifecycle once for the current session. */
  readonly start: () => void | Promise<void>;
  readonly status: Signal<DailyCalendarAgendaStatus>;
  readonly events: Signal<ReadonlyArray<DailyCalendarEventProjection>>;
  readonly conflicts: Signal<ReadonlyArray<DailyCalendarConflictProjection>>;
  readonly displayTimeZone: Signal<string>;
  readonly visibleDate: Signal<string>;
  readonly notice: Signal<string | null>;
  readonly retry: () => void | Promise<void>;
}

export interface DailyCalendarConflictMatch {
  readonly conflict: DailyCalendarConflictProjection;
  readonly other: { readonly eventId: string; readonly calendarId: string };
  readonly relationship: DailyCalendarConflictProjection['relationship'];
}

export function calendarEventIdentity(calendarId: string, eventId: string): string { return `${calendarId}\u0000${eventId}`; }

export function invertCalendarConflictRelationship(relationship: DailyCalendarConflictProjection['relationship']): DailyCalendarConflictProjection['relationship'] {
  if (relationship === 'contains') return 'contained_by';
  if (relationship === 'contained_by') return 'contains';
  return relationship;
}

/** Match only the server-returned relationship for this exact calendar event. */
export function calendarConflictsForEvent(event: Pick<DailyCalendarEventProjection, 'calendarId' | 'id'>, conflicts: ReadonlyArray<DailyCalendarConflictProjection>): ReadonlyArray<DailyCalendarConflictMatch> {
  const eventKey = calendarEventIdentity(event.calendarId, event.id);
  const matches: DailyCalendarConflictMatch[] = [];
  for (const conflict of conflicts) {
    if (calendarEventIdentity(conflict.left.calendarId, conflict.left.eventId) === eventKey) matches.push({ conflict, other: conflict.right, relationship: conflict.relationship });
    else if (calendarEventIdentity(conflict.right.calendarId, conflict.right.eventId) === eventKey) matches.push({ conflict, other: conflict.left, relationship: invertCalendarConflictRelationship(conflict.relationship) });
  }
  return matches;
}

export function calendarConflictForEvent(event: Pick<DailyCalendarEventProjection, 'calendarId' | 'id'>, conflicts: ReadonlyArray<DailyCalendarConflictProjection>): DailyCalendarConflictMatch | null { return calendarConflictsForEvent(event, conflicts)[0] ?? null; }

/** The contract's all-day end is exclusive; this is the final included date. */
export function finalIncludedCalendarDate(endDateExclusive: string): string {
  const date = new Date(`${endDateExclusive}T00:00:00Z`);
  if (Number.isNaN(date.getTime())) return endDateExclusive;
  date.setUTCDate(date.getUTCDate() - 1);
  return date.toISOString().slice(0, 10);
}

/** Optional composition point: Today remains fully useful when the calendar feature is absent. */
export const DAILY_CALENDAR_AGENDA = new InjectionToken<DailyCalendarAgendaProjection | null>('AURA_DAILY_CALENDAR_AGENDA', {
  providedIn: 'root',
  factory: () => null,
});
