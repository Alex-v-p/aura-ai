import { Injectable, inject, signal } from '@angular/core';
import type { Reminder } from '@aura/aura-api-client';
import { AuraRemindersApi } from './reminders-api';
import type { ReminderCollection, ReminderView, RemindersApi } from './reminder-models';

const emptyCollection = (): ReminderCollection => ({ items: [], nextCursor: null });

@Injectable({ providedIn: 'root' })
export class ReminderStore {
  private readonly api: RemindersApi = inject(AuraRemindersApi);
  readonly upcoming = signal<ReminderCollection>(emptyCollection());
  readonly history = signal<ReminderCollection>(emptyCollection());
  readonly selected = signal<Reminder | null>(null);
  readonly loading = signal(false);
  readonly loadingMore = signal(false);
  readonly saving = signal(false);
  readonly notice = signal<string | null>(null);
  readonly errorStatus = signal<number | null>(null);

  async load(): Promise<void> {
    this.loading.set(true); this.notice.set(null); this.errorStatus.set(null);
    try {
      const [upcoming, history] = await Promise.all([this.api.list('upcoming'), this.api.list('history')]);
      this.upcoming.set(upcoming); this.history.set(history);
    } catch (error: unknown) { this.setError(error, 'Reminders are temporarily unavailable.'); }
    finally { this.loading.set(false); }
  }

  async loadMore(view: ReminderView): Promise<void> {
    const collection = view === 'upcoming' ? this.upcoming() : this.history();
    if (!collection.nextCursor || this.loadingMore()) return;
    this.loadingMore.set(true);
    try {
      const next = await this.api.list(view, collection.nextCursor);
      const merged = { items: [...collection.items, ...next.items], nextCursor: next.nextCursor };
      if (view === 'upcoming') this.upcoming.set(merged); else this.history.set(merged);
    } catch (error: unknown) { this.setError(error, 'More reminders could not be loaded.'); }
    finally { this.loadingMore.set(false); }
  }

  async inspect(id: string): Promise<Reminder | null> {
    this.notice.set(null);
    try { const reminder = await this.api.get(id); this.selected.set(reminder); return reminder; }
    catch (error: unknown) { this.setError(error, 'That reminder could not be loaded.'); return null; }
  }

  async create(content: string, dueAt: string): Promise<boolean> {
    this.saving.set(true); this.notice.set(null);
    try { const reminder = await this.api.create(content, dueAt); this.upcoming.update((page) => ({ ...page, items: [...page.items, reminder].sort((a, b) => a.dueAt.localeCompare(b.dueAt)) })); this.selected.set(reminder); return true; }
    catch (error: unknown) { this.setError(error, 'The reminder could not be created.'); return false; }
    finally { this.saving.set(false); }
  }

  async complete(reminder: Reminder): Promise<boolean> { return this.transition(reminder, 'complete'); }
  async cancel(reminder: Reminder): Promise<boolean> { return this.transition(reminder, 'cancel'); }

  clearNotice(): void { this.notice.set(null); this.errorStatus.set(null); }

  private async transition(reminder: Reminder, action: 'complete' | 'cancel'): Promise<boolean> {
    this.saving.set(true); this.notice.set(null);
    try {
      const updated = action === 'complete' ? await this.api.complete(reminder.id, reminder.version) : await this.api.cancel(reminder.id, reminder.version);
      this.selected.set(updated); await this.load(); return true;
    } catch (error: unknown) { this.setError(error, action === 'complete' ? 'This reminder could not be completed.' : 'This reminder could not be canceled.'); return false; }
    finally { this.saving.set(false); }
  }

  private setError(error: unknown, fallback: string): void {
    const status = typeof error === 'object' && error !== null && 'status' in error && typeof error.status === 'number' ? error.status : null;
    this.errorStatus.set(status); this.notice.set(error instanceof Error && error.message ? error.message : fallback);
  }
}
