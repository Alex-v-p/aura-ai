import { ChangeDetectionStrategy, Component, HostListener, Injector, afterNextRender, computed, effect, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { A11yModule } from '@angular/cdk/a11y';
import { ActivatedRoute, Router } from '@angular/router';
import type { Reminder } from '@aura/aura-api-client';
import { ReminderStore } from './reminder-store';

type AgendaGroup = { readonly key: string; readonly label: string; readonly items: ReadonlyArray<Reminder> };
type ReturnFocusState = { readonly returnFocusRequested?: boolean; readonly returnFocusId?: string | null };
function localDateTimeValue(date = new Date()): string { const offset = date.getTimezoneOffset() * 60000; return new Date(date.getTime() - offset).toISOString().slice(0, 16); }
function toExplicitUtc(value: string): string | null { const parsed = new Date(value); return Number.isNaN(parsed.getTime()) ? null : parsed.toISOString(); }
function dayStart(value: Date): number { return new Date(value.getFullYear(), value.getMonth(), value.getDate()).getTime(); }
function boundedFocusId(value: unknown, prefix: string): string | null { return typeof value === 'string' && value.startsWith(`${prefix}-`) && value.length <= 160 && /^[A-Za-z0-9_-]+$/.test(value) ? value : null; }

@Component({ selector: 'aura-reminders-page', standalone: true, imports: [A11yModule, FormsModule], changeDetection: ChangeDetectionStrategy.OnPush, templateUrl: './reminders-page.component.html', styleUrl: './reminders-page.component.css' })
export class RemindersPageComponent {
  readonly store = inject(ReminderStore);
  private readonly route = inject(ActivatedRoute);
  private readonly router = inject(Router);
  private readonly injector = inject(Injector);
  private readonly returnFocusState = this.readReturnFocusState();
  readonly createOpen = signal(false);
  readonly selectedId = signal<string | null>(null);
  readonly view = signal<'upcoming' | 'history'>('upcoming');
  readonly agendaGroups = computed<ReadonlyArray<AgendaGroup>>(() => {
    const today = dayStart(new Date()); const groups: Record<string, Reminder[]> = { today: [], tomorrow: [], week: [], later: [] };
    for (const item of this.store.upcoming().items) { const days = Math.floor((dayStart(new Date(item.dueAt)) - today) / 86_400_000); (days <= 0 ? groups['today'] : days === 1 ? groups['tomorrow'] : days <= 7 ? groups['week'] : groups['later']).push(item); }
    return [{ key: 'today', label: 'Today', items: groups['today'] }, { key: 'tomorrow', label: 'Tomorrow', items: groups['tomorrow'] }, { key: 'week', label: 'This week', items: groups['week'] }, { key: 'later', label: 'Later', items: groups['later'] }];
  });
  content = '';
  dueAt = localDateTimeValue(new Date(Date.now() + 60 * 60 * 1000));
  readonly minDueAt = localDateTimeValue();

  constructor() {
    effect(() => { if (this.store.resetEpoch() > 0) this.clearLocalState(); });
    void this.store.load();
    this.route.paramMap.subscribe((params) => { const id = params.get('id'); this.selectedId.set(id); if (id) void this.openRoute(id); else { this.store.clearDetail(); if (this.returnFocusState.returnFocusRequested) this.scheduleReturnFocus(); } });
  }

  beginCreate(): void { this.createOpen.set(true); if (!this.content) { this.dueAt = localDateTimeValue(new Date(Date.now() + 60 * 60 * 1000)); } }
  cancelCreate(): void { this.createOpen.set(false); this.store.clearCreateDraft(); }
  async saveCreate(): Promise<void> {
    const text = this.content.trim(); const dueAt = toExplicitUtc(this.dueAt);
    if (!text || text.length > 2000 || !dueAt || new Date(dueAt).getTime() <= Date.now()) { this.store.notice.set('Enter a message and a future date and time.'); return; }
    if (await this.store.create(text, dueAt)) { this.createOpen.set(false); this.content = ''; }
  }
  inspect(reminder: Reminder, event?: Event): void { const returnFocusId = boundedFocusId((event?.currentTarget as HTMLElement | null)?.id, 'reminder'); void this.router.navigate(['/reminders', reminder.id], { state: { returnFocusRequested: true, returnFocusId } satisfies ReturnFocusState }); }
  closeDetail(): void { this.store.clearDetail(); this.selectedId.set(null); void this.router.navigate(['/reminders'], { state: { ...this.returnFocusState, returnFocusRequested: true } satisfies ReturnFocusState }); }
  retryDetail(): void { const id = this.selectedId(); if (id) void this.store.inspect(id); }
  async complete(reminder: Reminder): Promise<void> { await this.store.complete(reminder); }
  async cancel(reminder: Reminder): Promise<void> { await this.store.cancel(reminder); }
  formatDate(value: string): string { return new Intl.DateTimeFormat(undefined, { year: 'numeric', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit', timeZoneName: 'short' }).format(new Date(value)); }
  formatTimeZone(value: string): string { return new Intl.DateTimeFormat(undefined, { timeZoneName: 'short' }).formatToParts(new Date(value)).find((part) => part.type === 'timeZoneName')?.value ?? 'Local time'; }
  statusLabel(status: Reminder['status']): string { return status === 'canceled' ? 'Canceled' : status === 'scheduled' ? 'Scheduled' : status === 'fired' ? 'Due' : 'Completed'; }
  isTerminal(reminder: Reminder): boolean { return reminder.status === 'completed' || reminder.status === 'canceled'; }
  @HostListener('document:keydown.escape') onEscape(): void { if (this.createOpen()) { this.cancelCreate(); return; } if (this.selectedId()) this.closeDetail(); }
  private async openRoute(id: string): Promise<void> { await this.store.inspect(id); }
  private scheduleReturnFocus(): void { afterNextRender(() => { if (typeof document === 'undefined') return; const target = this.returnFocusState.returnFocusId ? document.getElementById(this.returnFocusState.returnFocusId) : document.getElementById('reminders-heading'); (target as HTMLElement | null)?.focus(); }, { injector: this.injector }); }
  private readReturnFocusState(): ReturnFocusState { const state = this.router.getCurrentNavigation()?.extras.state ?? (typeof history !== 'undefined' ? history.state : null); return { returnFocusRequested: state?.['returnFocusRequested'] === true, returnFocusId: boundedFocusId(state?.['returnFocusId'], 'reminder') }; }
  private clearLocalState(): void { this.createOpen.set(false); this.selectedId.set(null); this.view.set('upcoming'); this.content = ''; this.dueAt = localDateTimeValue(new Date(Date.now() + 60 * 60 * 1000)); }
}
