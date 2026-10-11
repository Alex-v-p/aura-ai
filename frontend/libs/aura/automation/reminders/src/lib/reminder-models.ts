import type { Reminder, ReminderPage, ReminderStatus } from '@aura/aura-api-client';

export type ReminderView = 'upcoming' | 'history';

export interface RemindersApi {
  list(view: ReminderView, cursor?: string): Promise<ReminderPage>;
  get(id: string): Promise<Reminder>;
  create(content: string, dueAt: string, idempotencyKey?: string): Promise<Reminder>;
  complete(id: string, expectedVersion: number): Promise<Reminder>;
  cancel(id: string, expectedVersion: number): Promise<Reminder>;
  clearSession?(): void;
}

export interface ReminderCollection {
  readonly items: ReadonlyArray<Reminder>;
  readonly nextCursor: string | null;
}

export const terminalReminderStatuses: ReadonlyArray<ReminderStatus> = ['completed', 'canceled'];
