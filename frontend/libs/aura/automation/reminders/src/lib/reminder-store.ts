import { Injectable, inject, signal } from '@angular/core';
import type { Reminder } from '@aura/aura-api-client';
import { AuraRemindersApi } from './reminders-api';
import type { ReminderCollection, ReminderView, RemindersApi } from './reminder-models';

const emptyCollection = (): ReminderCollection => ({ items: [], nextCursor: null });
const newRequestId = (): string => typeof crypto !== 'undefined' && 'randomUUID' in crypto ? crypto.randomUUID() : `aura-${Date.now()}-${Math.random().toString(36).slice(2)}`;
function statusOf(error: unknown): number | null { const status = (error as { readonly status?: unknown })?.status; return typeof status === 'number' ? status : null; }
function safeMessage(error: unknown, fallback: string): string {
  const status = statusOf(error);
  if (status === 401) return 'Your session has expired. Sign in again to view reminders.';
  if (status === 403) return 'You do not have permission to view these reminders.';
  if (status === 404) return 'That reminder is no longer available.';
  if (status === 409) return 'This reminder changed elsewhere. Refresh and try again.';
  if (status === 503) return 'Reminders are temporarily unavailable.';
  return fallback;
}

@Injectable({ providedIn: 'root' })
export class ReminderStore {
  private readonly api: RemindersApi = inject(AuraRemindersApi);
  readonly upcoming = signal<ReminderCollection>(emptyCollection());
  readonly history = signal<ReminderCollection>(emptyCollection());
  readonly selected = signal<Reminder | null>(null);
  readonly loading = signal(false);
  readonly loadingMore = signal(false);
  readonly detailLoading = signal(false);
  readonly detailError = signal<string | null>(null);
  readonly saving = signal(false);
  readonly notice = signal<string | null>(null);
  readonly errorStatus = signal<number | null>(null);
  readonly createOutcome = signal<'idle' | 'pending' | 'confirmed' | 'failed' | 'unconfirmed' | 'reconciled'>('idle');
  readonly resetEpoch = signal(0);
  private sessionResetHandler: ((status?: 401 | 403) => void) | null = null;
  private sessionGeneration = 0;
  private listGeneration = 0;
  private detailGeneration = 0;
  private detailKey = '';
  private mutationGeneration = 0;
  private createKey: string | null = null;
  private pendingCreate: { readonly content: string; readonly dueAt: string } | null = null;

  async load(): Promise<void> {
    const session = this.sessionGeneration;
    const generation = ++this.listGeneration;
    this.loading.set(true); this.notice.set(null); this.errorStatus.set(null);
    try {
      const [upcoming, history] = await Promise.all([this.api.list('upcoming'), this.api.list('history')]);
      if (session !== this.sessionGeneration || generation !== this.listGeneration) return;
      this.upcoming.set(upcoming); this.history.set(history);
      if (this.createOutcome() === 'unconfirmed' && this.pendingCreate) { this.createOutcome.set('reconciled'); this.notice.set('We checked the latest reminders. You can safely retry with the same save request.'); }
    } catch (error: unknown) {
      if (session === this.sessionGeneration && generation === this.listGeneration) this.setError(error, 'Reminders are temporarily unavailable.');
    } finally { if (session === this.sessionGeneration && generation === this.listGeneration) this.loading.set(false); }
  }

  async loadMore(view: ReminderView): Promise<void> {
    const collection = view === 'upcoming' ? this.upcoming() : this.history();
    if (!collection.nextCursor || this.loadingMore()) return;
    const session = this.sessionGeneration; const generation = this.listGeneration; this.loadingMore.set(true);
    try {
      const next = await this.api.list(view, collection.nextCursor);
      if (session !== this.sessionGeneration || generation !== this.listGeneration) return;
      const merged = { items: [...collection.items, ...next.items], nextCursor: next.nextCursor };
      if (view === 'upcoming') this.upcoming.set(merged); else this.history.set(merged);
    } catch (error: unknown) { if (session === this.sessionGeneration && generation === this.listGeneration) this.setError(error, 'More reminders could not be loaded.'); }
    finally { if (session === this.sessionGeneration && generation === this.listGeneration) this.loadingMore.set(false); }
  }

  async inspect(id: string): Promise<Reminder | null> {
    const session = this.sessionGeneration; const generation = ++this.detailGeneration; this.detailKey = id;
    this.selected.set(null); this.detailLoading.set(true); this.detailError.set(null); this.notice.set(null); this.saving.set(false);
    try {
      const reminder = await this.api.get(id);
      if (session !== this.sessionGeneration || generation !== this.detailGeneration || this.detailKey !== id) return null;
      this.selected.set(reminder); return reminder;
    } catch (error: unknown) {
      if (session === this.sessionGeneration && generation === this.detailGeneration && this.detailKey === id) {
        const message = safeMessage(error, 'That reminder could not be loaded.');
        if (statusOf(error) === 401 || statusOf(error) === 403) this.setError(error, message);
        else this.errorStatus.set(statusOf(error));
        this.detailError.set(message);
      }
      return null;
    } finally { if (session === this.sessionGeneration && generation === this.detailGeneration && this.detailKey === id) this.detailLoading.set(false); }
  }

  clearDetail(): void { ++this.detailGeneration; this.detailKey = ''; this.selected.set(null); this.detailLoading.set(false); this.detailError.set(null); this.saving.set(false); }

  /** Keep one create key while a draft is unchanged, including recoverable failures. */
  markCreateDraftChanged(): void { if (this.createOutcome() !== 'pending') { this.createKey = null; this.pendingCreate = null; this.createOutcome.set('idle'); } }
  clearCreateDraft(): void { this.createKey = null; this.pendingCreate = null; this.createOutcome.set('idle'); }

  async create(content: string, dueAt: string): Promise<boolean> {
    if (this.createOutcome() === 'unconfirmed') { this.notice.set('The previous save outcome is unknown. Refresh to reconcile it before trying again.'); this.errorStatus.set(null); return false; }
    const session = this.sessionGeneration; const mutation = ++this.mutationGeneration; const requestId = this.createKey ?? (this.createKey = newRequestId());
    this.pendingCreate = { content, dueAt };
    this.createOutcome.set('pending'); this.saving.set(true); this.notice.set(null); this.errorStatus.set(null);
    try {
      const reminder = await this.api.create(content, dueAt, requestId);
      if (session !== this.sessionGeneration || mutation !== this.mutationGeneration) return false;
      this.upcoming.update((page) => ({ ...page, items: [...page.items, reminder].sort((a, b) => a.dueAt.localeCompare(b.dueAt)) }));
      this.selected.set(reminder); this.createKey = null; this.pendingCreate = null; this.createOutcome.set('confirmed'); return true;
    } catch (error: unknown) {
      if (session === this.sessionGeneration && mutation === this.mutationGeneration) {
        const unknown = statusOf(error) === null;
        this.createOutcome.set(unknown ? 'unconfirmed' : 'failed');
        this.setError(error, unknown ? 'The save outcome is unknown. Your draft is still here; refresh to reconcile before trying again.' : statusOf(error) === 503 ? 'Reminders are temporarily unavailable. Your draft is still here.' : 'The reminder could not be created. Your draft is still here.');
      }
      return false;
    } finally { if (session === this.sessionGeneration && mutation === this.mutationGeneration) this.saving.set(false); }
  }

  async complete(reminder: Reminder): Promise<boolean> { return this.transition(reminder, 'complete'); }
  async cancel(reminder: Reminder): Promise<boolean> { return this.transition(reminder, 'cancel'); }
  clearNotice(): void { this.notice.set(null); this.errorStatus.set(null); }
  registerSessionResetHandler(handler: ((status?: 401 | 403) => void) | null): void { this.sessionResetHandler = handler; }

  resetSessionState(): void {
    ++this.sessionGeneration; ++this.listGeneration; ++this.detailGeneration; ++this.mutationGeneration; this.detailKey = ''; this.createKey = null; this.pendingCreate = null; this.api.clearSession?.();
    this.upcoming.set(emptyCollection()); this.history.set(emptyCollection()); this.clearDetail(); this.loading.set(false); this.loadingMore.set(false); this.detailLoading.set(false); this.saving.set(false); this.createOutcome.set('idle'); this.notice.set(null); this.errorStatus.set(null); this.resetEpoch.update((value) => value + 1);
  }

  private async transition(reminder: Reminder, action: 'complete' | 'cancel'): Promise<boolean> {
    const session = this.sessionGeneration; const detailGeneration = this.detailGeneration; const detailKey = this.detailKey; const mutation = ++this.mutationGeneration;
    this.saving.set(true); this.notice.set(null); this.errorStatus.set(null);
    const current = (): boolean => session === this.sessionGeneration && mutation === this.mutationGeneration && detailGeneration === this.detailGeneration && detailKey === this.detailKey && this.selected()?.id === reminder.id;
    try {
      const updated = action === 'complete' ? await this.api.complete(reminder.id, reminder.version) : await this.api.cancel(reminder.id, reminder.version);
      if (!current()) return false;
      this.selected.set(updated); await this.load(); return current();
    } catch (error: unknown) {
      if (current()) this.setError(error, statusOf(error) === null ? 'The reminder action outcome is unknown. Check the reminder before trying again.' : action === 'complete' ? 'This reminder could not be completed.' : 'This reminder could not be canceled.');
      return false;
    } finally { if (current()) this.saving.set(false); }
  }

  private setError(error: unknown, fallback: string): void {
    const status = statusOf(error);
    if (status === 401 || status === 403) {
      const message = status === 401 ? 'Your session has expired. Sign in again to view reminders.' : 'You do not have permission to view reminders.';
      this.resetSessionState(); this.sessionResetHandler?.(status); this.errorStatus.set(status); this.notice.set(message); return;
    }
    this.errorStatus.set(status); this.notice.set(safeMessage(error, fallback));
  }
}
