import type { Reminder, ReminderPage, ReminderStatus, ReminderOccurrence as GeneratedReminderOccurrence, ReminderOccurrencePage as GeneratedReminderOccurrencePage, ReminderOccurrenceStatus, ReminderSeries as GeneratedReminderSeries, ReminderSeriesPage as GeneratedReminderSeriesPage, ReminderSeriesStatus, ReminderSeriesRecurrence } from '@aura/aura-api-client';

export type ReminderView = 'upcoming' | 'history';

export type SeriesStatus = ReminderSeriesStatus;
export type RecurrenceFrequency = ReminderSeriesRecurrence['frequency'];
export type Weekday = 'monday' | 'tuesday' | 'wednesday' | 'thursday' | 'friday' | 'saturday' | 'sunday';
export type OccurrenceOutcome = ReminderOccurrenceStatus;
export type RecurrenceInput = ReminderSeriesRecurrence;
export type ReminderSeries = GeneratedReminderSeries;
export type ReminderOccurrence = GeneratedReminderOccurrence;
export type ReminderSeriesPage = GeneratedReminderSeriesPage;
export type ReminderOccurrencePage = GeneratedReminderOccurrencePage;
// Generated transport DTOs remain canonical: SeriesStatus = 'active' | 'paused' | 'completed' | 'canceled';
// OccurrenceOutcome = 'scheduled' | 'fired' | 'completed' | 'skipped' | 'replaced' | 'paused-window' | 'series-canceled'.
// ReminderSeries carries readableRule, nextReminderId, timeZone, count: number | null, and endDate: string | null.

export interface ReminderSeriesCommandInput {
  readonly expectedVersion: number;
  readonly idempotencyKey?: string;
}

export interface ReminderSeriesUpdateInput extends ReminderSeriesCommandInput {
  readonly content?: string;
  readonly recurrence?: RecurrenceInput;
}

export interface RemindersApi {
  list(view: ReminderView, cursor?: string): Promise<ReminderPage>;
  get(id: string): Promise<Reminder>;
  create(content: string, dueAt: string, idempotencyKey?: string): Promise<Reminder>;
  complete(id: string, expectedVersion: number): Promise<Reminder>;
  cancel(id: string, expectedVersion: number): Promise<Reminder>;
  listSeries(cursor?: string): Promise<ReminderSeriesPage>;
  createSeries(content: string, recurrence: RecurrenceInput, idempotencyKey?: string): Promise<ReminderSeries>;
  getSeries(id: string): Promise<ReminderSeries>;
  listOccurrences(id: string, cursor?: string): Promise<ReminderOccurrencePage>;
  updateSeries(id: string, input: ReminderSeriesUpdateInput): Promise<ReminderSeries>;
  pauseSeries(id: string, input: ReminderSeriesCommandInput): Promise<ReminderSeries>;
  resumeSeries(id: string, input: ReminderSeriesCommandInput): Promise<ReminderSeries>;
  skipNextSeriesOccurrence(id: string, input: ReminderSeriesCommandInput): Promise<ReminderSeries>;
  cancelSeries(id: string, input: ReminderSeriesCommandInput): Promise<ReminderSeries>;
  clearSession?(): void;
}

export interface ReminderCollection {
  readonly items: ReadonlyArray<Reminder>;
  readonly nextCursor: string | null;
}

export const terminalReminderStatuses: ReadonlyArray<ReminderStatus> = ['completed', 'canceled'];
