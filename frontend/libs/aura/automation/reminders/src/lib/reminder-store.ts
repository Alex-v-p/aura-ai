import { Injectable, inject, signal } from '@angular/core';
import type { Reminder } from '@aura/aura-api-client';
import { AuraRemindersApi } from './reminders-api';
import type { RecurrenceInput, ReminderCollection, ReminderOccurrencePage, ReminderSeries, ReminderSeriesCommandInput, ReminderSeriesPage, ReminderSeriesUpdateInput, ReminderView, RemindersApi } from './reminder-models';

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

// User-facing recovery vocabulary is intentionally explicit: validation, conflict,
// offline, permission, and unknown-outcome states never trigger a consequential retry.

@Injectable({ providedIn: 'root' })
export class ReminderStore {
  private readonly api: RemindersApi = inject(AuraRemindersApi);
  readonly upcoming = signal<ReminderCollection>(emptyCollection());
  readonly history = signal<ReminderCollection>(emptyCollection());
  readonly series = signal<ReminderSeriesPage>({ items: [], nextCursor: null });
  readonly selectedSeries = signal<ReminderSeries | null>(null);
  readonly occurrences = signal<ReminderOccurrencePage>({ items: [], nextCursor: null });
  readonly seriesDetailLoading = signal(false);
  readonly seriesDetailError = signal<string | null>(null);
  readonly seriesMutationOutcome = signal<'idle' | 'pending' | 'confirmed' | 'failed' | 'unconfirmed'>('idle');
  readonly seriesRecovery = signal<'idle' | 'validation' | 'conflict' | 'offline' | 'permission' | 'unknown'>('idle');
  readonly canReconcileRecurringCreate = signal(false);
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
  private seriesCreateKey: string | null = null;
  private pendingSeriesCreate: { readonly content: string; readonly recurrence: RecurrenceInput } | null = null;
  private optimisticSeries: ReminderSeries | null = null;

  async load(): Promise<void> {
    const session = this.sessionGeneration;
    const generation = ++this.listGeneration;
    this.loading.set(true); this.notice.set(null); this.errorStatus.set(null);
    try {
      const [upcoming, history] = await Promise.all([this.api.list('upcoming'), this.api.list('history')]);
      let series: ReminderSeriesPage = { items: [], nextCursor: null };
      try { series = await this.api.listSeries(); } catch (error: unknown) { const status = statusOf(error); if (status === 401 || status === 403) { this.setError(error, 'You do not have permission to view recurring reminders.'); return; } if (session === this.sessionGeneration && generation === this.listGeneration) { this.setError(error, 'Recurring reminders are temporarily unavailable.'); this.upcoming.set(upcoming); this.history.set(history); this.series.set(series); } return; }
      if (session !== this.sessionGeneration || generation !== this.listGeneration) return;
      if (this.optimisticSeries) { const committed = series.items.some((item) => item.id === this.optimisticSeries?.id); if (committed) this.optimisticSeries = null; else series = { ...series, items: [...series.items, this.optimisticSeries] }; }
      this.upcoming.set(upcoming); this.history.set(history); this.series.set(series);
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

  async loadMoreSeries(): Promise<void> {
    const cursor = this.series().nextCursor;
    if (!cursor || this.loadingMore()) return;
    const session = this.sessionGeneration; const generation = this.listGeneration; this.loadingMore.set(true);
    try {
      const next = await this.api.listSeries(cursor);
      if (session !== this.sessionGeneration || generation !== this.listGeneration) return;
      this.series.set({ items: [...this.series().items, ...next.items], nextCursor: next.nextCursor });
    } catch (error: unknown) { if (session === this.sessionGeneration && generation === this.listGeneration) this.setError(error, 'More recurring reminders could not be loaded.'); }
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

  async inspectSeries(id: string): Promise<ReminderSeries | null> {
    const session = this.sessionGeneration; const generation = ++this.detailGeneration; this.detailKey = `series:${id}`;
    this.selectedSeries.set(null); this.occurrences.set({ items: [], nextCursor: null }); this.seriesDetailLoading.set(true); this.seriesDetailError.set(null); this.notice.set(null); this.saving.set(false);
    try {
      const [series, occurrences] = await Promise.all([this.api.getSeries(id), this.api.listOccurrences(id)]);
      if (session !== this.sessionGeneration || generation !== this.detailGeneration || this.detailKey !== `series:${id}`) return null;
      this.selectedSeries.set(series); this.occurrences.set(occurrences); if (this.seriesMutationOutcome() === 'unconfirmed' && !this.pendingSeriesCreate) { this.seriesMutationOutcome.set('confirmed'); this.seriesRecovery.set('idle'); this.notice.set('The latest recurring reminder state is loaded. You can continue safely.'); } return series;
    } catch (error: unknown) {
      if (session === this.sessionGeneration && generation === this.detailGeneration && this.detailKey === `series:${id}`) { const message = safeMessage(error, 'That recurring reminder could not be loaded.'); this.seriesDetailError.set(message); this.setError(error, message); }
      return null;
    } finally { if (session === this.sessionGeneration && generation === this.detailGeneration && this.detailKey === `series:${id}`) this.seriesDetailLoading.set(false); }
  }

  async createRecurring(content: string, recurrence: RecurrenceInput, requestId?: string, allowUnconfirmedReplay = false): Promise<boolean> {
    if (this.saving()) return false;
    if (typeof navigator !== 'undefined' && navigator.onLine === false) { this.seriesRecovery.set('offline'); this.seriesMutationOutcome.set('failed'); this.notice.set('You appear to be offline. Your draft is still here; reconnect before submitting again.'); return false; }
    if (this.seriesMutationOutcome() === 'unconfirmed' && !allowUnconfirmedReplay) { this.notice.set('The previous save outcome is unknown. Use the explicit same-request reconciliation action before trying again.'); return false; }
    const session = this.sessionGeneration; const mutation = ++this.mutationGeneration; const key = requestId ?? this.seriesCreateKey ?? newRequestId(); this.seriesCreateKey = key; this.pendingSeriesCreate = { content, recurrence }; this.seriesMutationOutcome.set('pending'); this.saving.set(true); this.notice.set(null); this.errorStatus.set(null);
    let dispatched = false;
    try {
      dispatched = true;
      const created = await this.api.createSeries(content, recurrence, key); if (session !== this.sessionGeneration || mutation !== this.mutationGeneration) return false;
      this.series.update((page) => ({ ...page, items: [...page.items, created].sort((a, b) => (a.nextOccurrence ?? '').localeCompare(b.nextOccurrence ?? '')) })); this.optimisticSeries = created; this.selectedSeries.set(created); this.seriesMutationOutcome.set('confirmed'); this.seriesRecovery.set('idle'); this.seriesCreateKey = null; this.pendingSeriesCreate = null; this.canReconcileRecurringCreate.set(false); void this.load(); return true;
    } catch (error: unknown) { if (session === this.sessionGeneration && mutation === this.mutationGeneration) this.setSeriesError(error, dispatched); return false; }
    finally { if (session === this.sessionGeneration && mutation === this.mutationGeneration) this.saving.set(false); }
  }

  async reconcileRecurringCreate(): Promise<boolean> {
    const pending = this.pendingSeriesCreate; const key = this.seriesCreateKey;
    if (!pending || !key || this.seriesMutationOutcome() !== 'unconfirmed') return false;
    return this.createRecurring(pending.content, pending.recurrence, key, true);
  }

  async reconcileRecurringCreateRead(): Promise<void> {
    if (!this.pendingSeriesCreate || this.seriesMutationOutcome() !== 'unconfirmed') return;
    try { const page = await this.api.listSeries(); this.series.set(page); this.notice.set('The latest recurring reminders are loaded. Use the same-request action only when you are ready to reconcile this save.'); }
    catch (error: unknown) { this.setError(error, 'Recurring reminders are temporarily unavailable.'); }
  }

  async updateSeries(input: ReminderSeriesUpdateInput): Promise<boolean> { const selected = this.selectedSeries(); return selected ? this.seriesCommand((id, command) => this.api.updateSeries(id, { ...input, ...command, content: input.content, recurrence: input.recurrence }), selected, input) : false; }
  async pauseSeries(): Promise<boolean> { return this.runSeriesCommand((id, input) => this.api.pauseSeries(id, input)); }
  async resumeSeries(): Promise<boolean> { return this.runSeriesCommand((id, input) => this.api.resumeSeries(id, input)); }
  async skipNextSeriesOccurrence(): Promise<boolean> { return this.runSeriesCommand((id, input) => this.api.skipNextSeriesOccurrence(id, input)); }
  async cancelSeries(): Promise<boolean> { return this.runSeriesCommand((id, input) => this.api.cancelSeries(id, input)); }

  /** Keep one create key while a draft is unchanged, including recoverable failures. */
  markCreateDraftChanged(): void { if (this.createOutcome() !== 'pending') { this.createKey = null; this.pendingCreate = null; this.createOutcome.set('idle'); } }
  clearCreateDraft(): void { this.createKey = null; this.pendingCreate = null; this.createOutcome.set('idle'); if (this.pendingSeriesCreate) { this.seriesCreateKey = null; this.pendingSeriesCreate = null; this.canReconcileRecurringCreate.set(false); if (this.seriesMutationOutcome() === 'unconfirmed') { this.seriesMutationOutcome.set('idle'); this.seriesRecovery.set('idle'); } } }

  async create(content: string, dueAt: string): Promise<boolean> {
    if (this.saving()) return false;
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
    ++this.sessionGeneration; ++this.listGeneration; ++this.detailGeneration; ++this.mutationGeneration; this.detailKey = ''; this.createKey = null; this.pendingCreate = null; this.seriesCreateKey = null; this.pendingSeriesCreate = null; this.optimisticSeries = null; this.api.clearSession?.();
    this.upcoming.set(emptyCollection()); this.history.set(emptyCollection()); this.series.set({ items: [], nextCursor: null }); this.clearDetail(); this.selectedSeries.set(null); this.occurrences.set({ items: [], nextCursor: null }); this.seriesDetailError.set(null); this.loading.set(false); this.loadingMore.set(false); this.detailLoading.set(false); this.seriesDetailLoading.set(false); this.saving.set(false); this.createOutcome.set('idle'); this.seriesMutationOutcome.set('idle'); this.seriesRecovery.set('idle'); this.canReconcileRecurringCreate.set(false); this.notice.set(null); this.errorStatus.set(null); this.resetEpoch.update((value) => value + 1);
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

  private async runSeriesCommand(command: (id: string, input: ReminderSeriesCommandInput) => Promise<ReminderSeries>): Promise<boolean> {
    const selected = this.selectedSeries(); if (!selected) return false;
    return this.seriesCommand((_id, input) => command(selected.id, input), selected, { expectedVersion: selected.version, idempotencyKey: newRequestId() });
  }

  private async seriesCommand(invoke: (id: string, input: ReminderSeriesCommandInput) => Promise<ReminderSeries>, selected: ReminderSeries, input: ReminderSeriesCommandInput & Partial<ReminderSeriesUpdateInput>): Promise<boolean> {
    if (this.saving() || this.seriesMutationOutcome() === 'unconfirmed') return false;
    if (typeof navigator !== 'undefined' && navigator.onLine === false) { this.seriesRecovery.set('offline'); this.seriesMutationOutcome.set('failed'); this.notice.set('You appear to be offline. Reconnect before submitting this command again.'); return false; }
    const session = this.sessionGeneration; const mutation = ++this.mutationGeneration; this.saving.set(true); this.seriesMutationOutcome.set('pending'); this.seriesRecovery.set('idle'); this.notice.set(null); this.errorStatus.set(null);
    let dispatched = false;
    try { dispatched = true; const updated = await invoke(selected.id, { ...input, idempotencyKey: input.idempotencyKey ?? newRequestId() }); if (session !== this.sessionGeneration || mutation !== this.mutationGeneration) return false; this.selectedSeries.set(updated); this.series.update((page) => ({ ...page, items: page.items.map((item) => item.id === updated.id ? updated : item) })); this.seriesMutationOutcome.set('confirmed'); return true; }
    catch (error: unknown) { if (session === this.sessionGeneration && mutation === this.mutationGeneration) this.setSeriesError(error, dispatched); return false; }
    finally { if (session === this.sessionGeneration && mutation === this.mutationGeneration) this.saving.set(false); }
  }

  private setSeriesError(error: unknown, dispatched: boolean): void {
    const status = statusOf(error);
    const offline = status === 0;
    const preDispatchOffline = offline || (!dispatched && typeof navigator !== 'undefined' && navigator.onLine === false);
    const validation = status === 422;
    const conflict = status === 409;
    const permission = status === 401 || status === 403;
    const unknown = status === null && dispatched && !offline;
    this.errorStatus.set(status);
    if (validation) { if (this.pendingSeriesCreate) { this.seriesCreateKey = null; this.pendingSeriesCreate = null; this.canReconcileRecurringCreate.set(false); } this.seriesRecovery.set('validation'); this.seriesMutationOutcome.set('failed'); this.notice.set('Check the recurring reminder details and try again.'); return; }
    if (conflict) { this.seriesRecovery.set('conflict'); this.seriesMutationOutcome.set('failed'); this.notice.set('This recurring reminder changed elsewhere. Refresh before trying again.'); return; }
    if (permission) { this.seriesRecovery.set('permission'); this.seriesMutationOutcome.set('failed'); this.setError(error, 'You do not have permission to change this recurring reminder.'); return; }
    if (preDispatchOffline) { this.seriesRecovery.set('offline'); this.seriesMutationOutcome.set('failed'); this.notice.set('You appear to be offline. Your draft is still here; reconnect before submitting again.'); return; }
    if (unknown) { this.seriesRecovery.set('unknown'); this.seriesMutationOutcome.set('unconfirmed'); this.canReconcileRecurringCreate.set(Boolean(this.pendingSeriesCreate && this.seriesCreateKey)); this.notice.set(this.pendingSeriesCreate ? 'The save outcome is unknown. Use the explicit same-request reconciliation action before trying again.' : 'The command outcome is unknown. Refresh this series to reconcile before trying again.'); return; }
    this.seriesMutationOutcome.set('failed'); this.notice.set(safeMessage(error, 'The recurring reminder could not be changed.'));
  }
}
