import { ChangeDetectionStrategy, ChangeDetectorRef, Component, HostListener, Injector, afterNextRender, computed, effect, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { A11yModule } from '@angular/cdk/a11y';
import { ActivatedRoute, Router } from '@angular/router';
import type { Reminder } from '@aura/aura-api-client';
import type { RecurrenceInput, ReminderSeries, Weekday } from './reminder-models';
import { ReminderStore } from './reminder-store';

type AgendaItem = { readonly kind: 'reminder'; readonly reminder: Reminder } | { readonly kind: 'series'; readonly series: ReminderSeries };
type AgendaGroup = { readonly key: string; readonly label: string; readonly items: ReadonlyArray<AgendaItem> };
type ReturnFocusState = { readonly returnFocusRequested?: boolean; readonly returnFocusId?: string | null };
function localDateTimeValue(date = new Date()): string { const offset = date.getTimezoneOffset() * 60000; return new Date(date.getTime() - offset).toISOString().slice(0, 16); }
function toExplicitUtc(value: string): string | null { const parsed = new Date(value); return Number.isNaN(parsed.getTime()) ? null : parsed.toISOString(); }
function dayStart(value: Date): number { return new Date(value.getFullYear(), value.getMonth(), value.getDate()).getTime(); }
function boundedFocusId(value: unknown, prefix: string): string | null { return typeof value === 'string' && value.startsWith(`${prefix}-`) && value.length <= 160 && /^[A-Za-z0-9_-]+$/.test(value) ? value : null; }
function isIanaTimeZone(value: string): boolean { try { new Intl.DateTimeFormat('en', { timeZone: value }).format(); return value.length > 0; } catch { return false; } }

@Component({ selector: 'aura-reminders-page', standalone: true, imports: [A11yModule, FormsModule], changeDetection: ChangeDetectionStrategy.OnPush, templateUrl: './reminders-page.component.html', styleUrl: './reminders-page.component.css' })
export class RemindersPageComponent {
  readonly store = inject(ReminderStore);
  private readonly route = inject(ActivatedRoute);
  private readonly router = inject(Router);
  private readonly injector = inject(Injector);
  private readonly changeDetector = inject(ChangeDetectorRef);
  private readonly returnFocusState = this.readReturnFocusState();
  private returnFocusId: string | null = this.returnFocusState.returnFocusId ?? null;
  readonly createOpen = signal(false);
  readonly selectedId = signal<string | null>(null);
  readonly selectedSeriesId = signal<string | null>(null);
  readonly seriesEditOpen = signal(false);
  readonly seriesConfirmAction = signal<'skip' | 'cancel' | null>(null);
  readonly recurringCreateInFlight = signal(false);
  private seriesConfirmReturnFocusId: string | null = null;
  readonly view = signal<'upcoming' | 'history'>('upcoming');
  readonly agendaGroups = computed<ReadonlyArray<AgendaGroup>>(() => {
    const today = dayStart(new Date()); const groups: Record<string, AgendaItem[]> = { today: [], tomorrow: [], week: [], later: [] };
    const seriesReminderIds = new Set(this.store.series().items.map((item) => item.nextReminderId).filter((id): id is string => Boolean(id)));
    for (const item of this.store.upcoming().items) { if (seriesReminderIds.has(item.id)) continue; const days = Math.floor((dayStart(new Date(item.dueAt)) - today) / 86_400_000); (days <= 0 ? groups['today'] : days === 1 ? groups['tomorrow'] : days <= 7 ? groups['week'] : groups['later']).push({ kind: 'reminder', reminder: item }); }
    for (const item of this.store.series().items) { if (!item.nextOccurrence || item.status !== 'active') continue; const days = Math.floor((dayStart(new Date(item.nextOccurrence)) - today) / 86_400_000); (days <= 0 ? groups['today'] : days === 1 ? groups['tomorrow'] : days <= 7 ? groups['week'] : groups['later']).push({ kind: 'series', series: item }); }
    Object.values(groups).forEach((items) => items.sort((a, b) => this.agendaDate(a).localeCompare(this.agendaDate(b))));
    return [{ key: 'today', label: 'Today', items: groups['today'] }, { key: 'tomorrow', label: 'Tomorrow', items: groups['tomorrow'] }, { key: 'week', label: 'This week', items: groups['week'] }, { key: 'later', label: 'Later', items: groups['later'] }];
  });
  readonly hasAgendaItems = computed(() => this.agendaGroups().some((group) => group.items.length > 0));
  readonly agendaItemCount = computed(() => this.agendaGroups().reduce((total, group) => total + group.items.length, 0));
  content = '';
  dueAt = localDateTimeValue(new Date(Date.now() + 60 * 60 * 1000));
  readonly minDueAt = localDateTimeValue();

  constructor() {
    effect(() => { if (this.store.resetEpoch() > 0) this.clearLocalState(); });
    void this.store.load();
    this.route.paramMap.subscribe((params) => { const id = params.get('id'); const seriesId = params.get('seriesId'); this.selectedId.set(id); this.selectedSeriesId.set(seriesId); if (seriesId) void this.openSeriesRoute(seriesId); else if (id) void this.openRoute(id); else { this.store.clearDetail(); if (this.returnFocusState.returnFocusRequested) this.scheduleReturnFocus(); } });
  }

  beginCreate(): void { this.createOpen.set(true); if (!this.content) { this.dueAt = localDateTimeValue(new Date(Date.now() + 60 * 60 * 1000)); } }
  cancelCreate(): void { this.createOpen.set(false); this.store.clearCreateDraft(); }
  async saveCreate(): Promise<void> {
    const text = this.content.trim(); const dueAt = toExplicitUtc(this.dueAt);
    if (!text || text.length > 2000 || !dueAt || new Date(dueAt).getTime() <= Date.now()) { this.store.notice.set('Enter a message and a future date and time.'); return; }
    if (this.repeat !== 'none') { const recurrence = this.recurringInput(dueAt); if (!recurrence) { this.focusRecurrenceField(); return; } if (this.recurringCreateInFlight()) return; this.setRecurringCreateInFlight(true); try { if (await this.store.createRecurring(text, recurrence)) { this.createOpen.set(false); this.content = ''; } } finally { this.setRecurringCreateInFlight(false); } return; }
    if (await this.store.create(text, dueAt)) { this.createOpen.set(false); this.content = ''; }
  }
  async reconcileRecurringCreate(): Promise<void> { if (this.recurringCreateInFlight()) return; this.setRecurringCreateInFlight(true); try { if (await this.store.reconcileRecurringCreate()) { this.createOpen.set(false); this.content = ''; } } finally { this.setRecurringCreateInFlight(false); } }
  canReconcileCreate(): boolean { const store = this.store as unknown as { readonly canReconcileRecurringCreate?: () => boolean }; return store.canReconcileRecurringCreate?.() ?? false; }
  inspect(reminder: Reminder, event?: Event): void { const returnFocusId = boundedFocusId((event?.currentTarget as HTMLElement | null)?.id, 'reminder'); this.returnFocusId = returnFocusId; void this.router.navigate(['/reminders', reminder.id], { state: { returnFocusRequested: true, returnFocusId } satisfies ReturnFocusState }); }
  inspectSeries(series: ReminderSeries, event?: Event): void { const returnFocusId = boundedFocusId((event?.currentTarget as HTMLElement | null)?.id, 'series'); this.returnFocusId = returnFocusId; void this.router.navigate(['/reminders/series', series.id], { state: { returnFocusRequested: true, returnFocusId } satisfies ReturnFocusState }); }
  closeDetail(): void { this.store.clearDetail(); this.selectedId.set(null); this.selectedSeriesId.set(null); const returnFocusId = this.returnFocusId; void this.router.navigate(['/reminders'], { state: { returnFocusRequested: true, returnFocusId } satisfies ReturnFocusState }).then(() => this.focusReturnTarget()); }
  async retryNotice(): Promise<void> { if (this.canReconcileCreate()) { await this.store.reconcileRecurringCreateRead(); return; } await this.store.load(); }
  retryDetail(): void { const seriesId = this.selectedSeriesId(); if (seriesId) { void this.store.inspectSeries(seriesId); return; } const id = this.selectedId(); if (id) void this.store.inspect(id); }
  async complete(reminder: Reminder): Promise<void> { await this.store.complete(reminder); }
  async cancel(reminder: Reminder): Promise<void> { await this.store.cancel(reminder); }
  async pauseSeries(): Promise<void> { await this.store.pauseSeries(); }
  async resumeSeries(): Promise<void> { await this.store.resumeSeries(); }
  requestSkipSeries(event?: Event): void { this.seriesConfirmReturnFocusId = (event?.currentTarget as HTMLElement | null)?.id || 'skip-series-action'; this.seriesConfirmAction.set('skip'); }
  requestCancelSeries(event?: Event): void { this.seriesConfirmReturnFocusId = (event?.currentTarget as HTMLElement | null)?.id || 'cancel-series-action'; this.seriesConfirmAction.set('cancel'); }
  skipSeries(event?: Event): void { this.requestSkipSeries(event); }
  cancelRecurring(event?: Event): void { this.requestCancelSeries(event); }
  dismissSeriesConfirmation(): void { const returnFocusId = this.seriesConfirmReturnFocusId; this.seriesConfirmAction.set(null); this.scheduleSeriesConfirmFocus(returnFocusId); }
  async confirmSeriesAction(): Promise<void> { const action = this.seriesConfirmAction(); const returnFocusId = this.seriesConfirmReturnFocusId; this.seriesConfirmAction.set(null); if (action === 'skip') await this.store.skipNextSeriesOccurrence(); if (action === 'cancel') await this.store.cancelSeries(); this.scheduleSeriesConfirmFocus(returnFocusId); }
  beginSeriesEdit(): void { const series = this.store.selectedSeries(); if (!series) return; const recurrence = series.recurrence; const names: Weekday[] = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']; this.seriesEditContent = series.content; this.seriesEditInterval = recurrence.interval; this.seriesEditWeekdayText = (recurrence.weekdays ?? []).map((day) => names[day - 1]).filter((day): day is Weekday => Boolean(day)).join(', '); this.seriesEditMonthDay = recurrence.monthDay ?? null; this.seriesEditTimeZone = recurrence.timeZone || series.timeZone; this.seriesEditLocalStart = recurrence.localStart || series.localStart; this.seriesEditEndMode = recurrence.count != null ? 'count' : recurrence.endDate != null ? 'date' : 'none'; this.seriesEditEndCount = recurrence.count ?? null; this.seriesEditEndDate = recurrence.endDate ?? null; this.recurrenceError = ''; this.recurrenceFieldError = null; this.seriesEditOpen.set(true); }
  cancelSeriesEdit(): void { this.seriesEditOpen.set(false); }
  async saveSeriesEdit(): Promise<void> { const series = this.store.selectedSeries(); if (!series || !this.seriesEditContent.trim()) { this.recurrenceError = 'Enter a reminder message.'; return; } const recurrence = this.seriesEditRecurrence(series); if (!recurrence) { this.focusRecurrenceField(); return; } if (await this.store.updateSeries({ expectedVersion: series.version, content: this.seriesEditContent.trim(), recurrence })) this.seriesEditOpen.set(false); }
  formatDate(value: string): string { return new Intl.DateTimeFormat(undefined, { year: 'numeric', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit', timeZoneName: 'short' }).format(new Date(value)); }
  formatTimeZone(value: string): string { return new Intl.DateTimeFormat(undefined, { timeZoneName: 'short' }).formatToParts(new Date(value)).find((part) => part.type === 'timeZoneName')?.value ?? 'Local time'; }
  statusLabel(status: Reminder['status']): string { return status === 'canceled' ? 'Canceled' : status === 'scheduled' ? 'Scheduled' : status === 'fired' ? 'Due' : 'Completed'; }
  isTerminal(reminder: Reminder): boolean { return reminder.status === 'completed' || reminder.status === 'canceled'; }
  agendaDate(item: AgendaItem): string { return item.kind === 'series' ? (item.series.nextOccurrence ?? '') : item.reminder.dueAt; }
  repeatLabel(series: ReminderSeries): string { return series.readableRule || `Every ${series.recurrence.interval} ${series.recurrence.frequency}`; }
  formatSeriesStatus(status: ReminderSeries['status']): string { return status.charAt(0).toUpperCase() + status.slice(1); }
  repeat: 'none' | 'daily' | 'weekday' | 'weekly' | 'monthly' = 'none';
  interval = 1;
  weekdays: Weekday[] = ['monday'];
  weekdayText = 'Monday';
  monthDay = new Date().getDate();
  timeZone = Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
  endMode: 'none' | 'count' | 'date' = 'none';
  endCount: number | null = null;
  endDate: string | null = null;
  seriesEditContent = '';
  seriesEditInterval = 1;
  seriesEditWeekdayText = '';
  seriesEditMonthDay: number | null = null;
  seriesEditTimeZone = '';
  seriesEditLocalStart = '';
  seriesEditEndMode: 'none' | 'count' | 'date' = 'none';
  seriesEditEndCount: number | null = null;
  seriesEditEndDate: string | null = null;
  private recurrenceFocusTarget: string | null = null;
  recurrenceFieldError: string | null = null;
  recurrenceError = '';
  recurringInput(_dueAt: string): RecurrenceInput | null { const names = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']; const parsed = this.weekdayText.split(',').map((item) => names.indexOf(item.trim().toLowerCase()) + 1).filter((item) => item > 0); this.recurrenceFocusTarget = this.interval < 1 ? 'interval' : ((this.repeat === 'weekday' || this.repeat === 'weekly') && parsed.length === 0) ? 'weekdays' : (this.repeat === 'monthly' && (this.monthDay === null || this.monthDay < 1 || this.monthDay > 31)) ? 'month-day' : (this.endMode === 'count' && (!this.endCount || this.endCount < 1)) ? 'end-count' : (this.endMode === 'date' && !this.endDate) ? 'end-date' : !isIanaTimeZone(this.timeZone) ? 'time-zone' : null; if (this.recurrenceFocusTarget) return this.setRecurrenceError(this.recurrenceFocusTarget); this.recurrenceError = ''; this.recurrenceFieldError = null; return { frequency: this.repeat === 'weekday' ? 'selected_weekday' : this.repeat === 'none' ? 'daily' : this.repeat, interval: this.interval, weekdays: this.repeat === 'weekday' || this.repeat === 'weekly' ? parsed : [], monthDay: this.repeat === 'monthly' ? this.monthDay : null, localStart: this.dueAt, timeZone: this.timeZone, count: this.endMode === 'count' ? this.endCount : null, endDate: this.endMode === 'date' ? this.endDate : null }; }
  private seriesEditRecurrence(series: ReminderSeries): RecurrenceInput | null { const names = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']; const parsed = this.seriesEditWeekdayText.split(',').map((item) => names.indexOf(item.trim().toLowerCase()) + 1).filter((item) => item > 0); const frequency = series.recurrence.frequency; const focus = this.seriesEditInterval < 1 ? 'interval' : ((frequency === 'selected_weekday' || frequency === 'weekly') && parsed.length === 0) ? 'weekdays' : (frequency === 'monthly' && (this.seriesEditMonthDay === null || this.seriesEditMonthDay < 1 || this.seriesEditMonthDay > 31)) ? 'month-day' : (this.seriesEditEndMode === 'count' && (!this.seriesEditEndCount || this.seriesEditEndCount < 1)) ? 'end-count' : (this.seriesEditEndMode === 'date' && !this.seriesEditEndDate) ? 'end-date' : !isIanaTimeZone(this.seriesEditTimeZone) ? 'time-zone' : null; if (focus) { this.recurrenceFocusTarget = `series-edit-${focus}`; return this.setRecurrenceError(this.recurrenceFocusTarget); } this.recurrenceError = ''; this.recurrenceFieldError = null; return { frequency, interval: this.seriesEditInterval, weekdays: frequency === 'selected_weekday' || frequency === 'weekly' ? parsed : [], monthDay: frequency === 'monthly' ? this.seriesEditMonthDay : null, localStart: this.seriesEditLocalStart || series.localStart, timeZone: this.seriesEditTimeZone, count: this.seriesEditEndMode === 'count' ? this.seriesEditEndCount : null, endDate: this.seriesEditEndMode === 'date' ? this.seriesEditEndDate : null }; }
  private setRecurrenceError(field: string): null { const normalized = field.replace(/^series-edit-/, ''); const labels: Record<string, string> = { interval: 'Interval must be at least 1.', weekdays: 'Choose at least one weekday.', 'month-day': 'Choose a valid day of the month.', 'time-zone': 'Enter a valid IANA timezone.', 'end-count': 'Enter at least one occurrence.', 'end-date': 'Choose an end date.' }; this.recurrenceFieldError = field; this.recurrenceError = normalized === 'weekdays' ? 'Choose a valid interval, recurrence day, and IANA timezone.' : labels[normalized] ?? 'Check the recurring reminder details.'; return null; }
  recurrenceValidationHint(): string { if (this.repeat !== 'none' && this.interval < 1) return 'Interval must be at least 1.'; if ((this.repeat === 'weekday' || this.repeat === 'weekly') && this.weekdayText.trim() === '') return 'Choose at least one weekday.'; if (this.repeat === 'monthly' && (this.monthDay === null || this.monthDay < 1 || this.monthDay > 31)) return 'Choose a valid day of the month.'; if (this.repeat !== 'none' && !isIanaTimeZone(this.timeZone)) return 'Enter a valid IANA timezone.'; if (this.endMode === 'count' && (!this.endCount || this.endCount < 1)) return 'Enter at least one occurrence.'; if (this.endMode === 'date' && !this.endDate) return 'Choose an end date.'; return ''; }
  private focusRecurrenceField(prefix = ''): void { const target = this.recurrenceFocusTarget; this.recurrenceFocusTarget = null; if (target && typeof document !== 'undefined') document.getElementById(`${prefix}${target}`)?.focus(); }
  @HostListener('document:keydown.escape') onEscape(): void { if (this.seriesConfirmAction()) { this.dismissSeriesConfirmation(); return; } if (this.seriesEditOpen()) { this.cancelSeriesEdit(); return; } if (this.createOpen()) { this.cancelCreate(); return; } if (this.selectedId() || this.selectedSeriesId()) this.closeDetail(); }
  private async openRoute(id: string): Promise<void> { await this.store.inspect(id); }
  private async openSeriesRoute(id: string): Promise<void> { await this.store.inspectSeries(id); }
  private setRecurringCreateInFlight(value: boolean): void { this.recurringCreateInFlight.set(value); this.changeDetector.markForCheck(); this.changeDetector.detectChanges(); }
  private scheduleSeriesConfirmFocus(returnFocusId: string | null): void { if (!returnFocusId) return; afterNextRender(() => { if (typeof requestAnimationFrame === 'function') requestAnimationFrame(() => document.getElementById(returnFocusId)?.focus()); else document.getElementById(returnFocusId)?.focus(); }, { injector: this.injector }); }
  private focusReturnTarget(attempt = 0): void {
    if (typeof document === 'undefined') return;
    const target = (this.returnFocusId ? document.getElementById(this.returnFocusId) : document.getElementById('reminders-heading')) as HTMLElement | null;
    const backdrop = document.querySelector('.detail-backdrop');
    if (target && (!backdrop || !backdrop.contains(target))) { target.focus(); return; }
    // Router completion can precede the signal-driven removal of the focus trap.
    // Retry on the next paint so focus is restored only after the detail overlay
    // has left the document, rather than being immediately pulled back into it.
    if (attempt < 4 && typeof requestAnimationFrame === 'function') requestAnimationFrame(() => this.focusReturnTarget(attempt + 1));
  }
  private scheduleReturnFocus(): void { afterNextRender(() => this.focusReturnTarget(), { injector: this.injector }); }
  private readReturnFocusState(): ReturnFocusState { const state = this.router.getCurrentNavigation()?.extras.state ?? (typeof history !== 'undefined' ? history.state : null); return { returnFocusRequested: state?.['returnFocusRequested'] === true, returnFocusId: boundedFocusId(state?.['returnFocusId'], 'reminder') ?? boundedFocusId(state?.['returnFocusId'], 'series') }; }
  private clearLocalState(): void { this.createOpen.set(false); this.selectedId.set(null); this.selectedSeriesId.set(null); this.seriesEditOpen.set(false); this.seriesConfirmAction.set(null); this.seriesConfirmReturnFocusId = null; this.recurringCreateInFlight.set(false); this.view.set('upcoming'); this.content = ''; this.repeat = 'none'; this.recurrenceError = ''; this.recurrenceFieldError = null; this.dueAt = localDateTimeValue(new Date(Date.now() + 60 * 60 * 1000)); }
}
