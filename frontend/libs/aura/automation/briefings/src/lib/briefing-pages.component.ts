import { ChangeDetectionStrategy, Component, HostListener, inject, signal } from '@angular/core';
import { A11yModule } from '@angular/cdk/a11y';
import { ActivatedRoute, Router, RouterLink } from '@angular/router';
import { AgentStore } from '@aura/aura/interaction/agents';
import { BriefingStore } from './briefing-store';
import { briefingStatusLabel, occurrenceStatusLabel, sourceCompletenessLabel, type BriefingOccurrence, type BriefingResult, type BriefingSchedule } from './briefing-models';

type Confirmation = { readonly kind: 'schedule' | 'history' | 'result' | 'draft'; readonly schedule?: BriefingSchedule; readonly result?: BriefingResult; readonly returnId: string };

@Component({ selector: 'aura-briefings-page', standalone: true, imports: [RouterLink, A11yModule], changeDetection: ChangeDetectionStrategy.OnPush, templateUrl: './briefing-pages.component.html', styleUrl: './briefing-pages.component.css' })
export class BriefingsPageComponent {
  readonly store = inject(BriefingStore);
  readonly agents = inject(AgentStore);
  private readonly route = inject(ActivatedRoute);
  private readonly router = inject(Router);
  readonly confirmation = signal<Confirmation | null>(null);
  constructor() { void this.store.load(); this.route.paramMap.subscribe((params) => { const id = params.get('id'); const occurrenceId = params.get('occurrenceId'); if (id && occurrenceId) void this.store.inspectOccurrence(id, occurrenceId); else if (id) void this.store.inspect(id); else this.store.clearDetail(); }); }
  get isNew(): boolean { return this.router.url.endsWith('/new'); }
  get isOccurrence(): boolean { return this.route.snapshot.paramMap.has('occurrenceId'); }
  get isDetail(): boolean { return Boolean(this.route.snapshot.paramMap.get('id')); }
  scheduleId(): string { return this.route.snapshot.paramMap.get('id') ?? ''; }
  occurrenceId(): string { return this.route.snapshot.paramMap.get('occurrenceId') ?? ''; }
  cancelCreate(): void { if (this.draftIsDirty()) this.openConfirmation({ kind: 'draft', returnId: 'briefing-display-name' }); else this.discardAndNavigate(); }
  async submit(): Promise<void> { const schedule = await this.store.create(); if (schedule) void this.router.navigate(['/briefings', schedule.id]); }
  async toggle(schedule: BriefingSchedule): Promise<void> { await this.store.pauseOrResume(schedule); }
  requestScheduleDelete(schedule: BriefingSchedule, event: Event): void { this.openConfirmation({ kind: 'schedule', schedule, returnId: this.triggerId(event, 'delete-schedule') }); }
  requestHistoryDelete(schedule: BriefingSchedule, event: Event): void { this.openConfirmation({ kind: 'history', schedule, returnId: this.triggerId(event, 'delete-history') }); }
  requestResultDelete(result: BriefingResult, event: Event): void { this.openConfirmation({ kind: 'result', result, returnId: this.triggerId(event, 'delete-result') }); }
  closeConfirmation(returnOverride?: string): void { const returnId = returnOverride ?? this.confirmation()?.returnId; this.confirmation.set(null); if (returnId) queueMicrotask(() => document.getElementById(returnId)?.focus()); }
  async confirmDeletion(): Promise<void> { const confirmation = this.confirmation(); if (!confirmation) return; if (confirmation.kind === 'draft') { this.closeConfirmation(); this.discardAndNavigate(); return; } let accepted = false; if (confirmation.kind === 'schedule' && confirmation.schedule) accepted = await this.store.deleteSchedule(confirmation.schedule); else if (confirmation.kind === 'history' && confirmation.schedule) accepted = await this.store.deleteHistory(confirmation.schedule); else if (confirmation.kind === 'result' && confirmation.result) accepted = await this.store.deleteResult(confirmation.result); if (accepted) { this.closeConfirmation('briefing-heading'); if (confirmation.kind === 'schedule') void this.router.navigate(['/briefings']).then(() => queueMicrotask(() => document.getElementById('briefing-heading')?.focus())); } }
  selectCalendar(id: string): void { this.store.toggleCalendar(id); }
  selectDocument(id: string): void { this.store.toggleDocument(id); }
  selectedCalendar(id: string): boolean { return this.store.draft().calendarIds.includes(id); }
  selectedDocument(id: string): boolean { return this.store.draft().documentIds.includes(id); }
  error(key: string): string | null { return this.store.errors()[key] ?? null; }
  retryDetail(): void { const id = this.scheduleId(); if (this.isNew) void Promise.all([this.store.reloadCapabilities(), this.store.retrySources()]); else if (this.isOccurrence) void this.store.inspectOccurrence(id, this.occurrenceId()); else if (id) void this.store.inspect(id); else void this.store.load(); }
  signIn(): void { if (typeof window !== 'undefined') { const returnTo = `${window.location.pathname}${window.location.search}`; window.location.assign(`/api/v1/auth/login?return_to=${encodeURIComponent(returnTo)}`); } }
  occurrenceExplanation(status: BriefingOccurrence['status']): string { switch (status) { case 'partial': return 'The briefing finished with only some selected sources available.'; case 'failed': return 'The briefing could not complete. No result content is being inferred.'; case 'canceled': return 'The admitted briefing was canceled before completion.'; case 'skipped': return 'This occurrence was skipped and did not run.'; case 'unknown': return 'The outcome is unknown. Refresh to confirm before taking action.'; case 'completed': return 'The briefing completed successfully.'; default: return 'This occurrence is waiting for execution.'; } }
  status(schedule: BriefingSchedule): string { return briefingStatusLabel(schedule.status); }
  occurrenceStatus(occurrence: BriefingOccurrence): string { return occurrenceStatusLabel(occurrence.status); }
  completeness(value: BriefingOccurrence['sourceCompleteness'] | BriefingResult['sourceCompleteness']): string { return sourceCompletenessLabel(value); }
  formatDate(value: string | null): string { return value ? new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' }).format(new Date(value)) : 'Not scheduled'; }
  formatTime(value: string): string { return value; }
  @HostListener('document:keydown.escape') onEscape(): void { if (this.confirmation()) { this.closeConfirmation(); return; } if (this.isDetail || this.isNew) void this.router.navigate(['/briefings']); }
  draftIsDirty(): boolean { const draft = this.store.draft(); return Boolean(draft.displayName || draft.agentRevisionId || draft.calendarIds.length || draft.documentIds.length || draft.timeZone || draft.localTime !== '08:00'); }
  loadMoreDocuments(): void { void this.store.loadMoreDocuments(); }
  loadMoreCalendars(): void { void this.store.loadMoreCalendars(); }
  retrySources(): void { void this.store.retrySources(); }
  sourceNeedsSignIn(): boolean { return this.store.sourceNotice().startsWith('Sign in again'); }
  sourceNeedsCalendarConnection(): boolean { return this.store.sourceNotice().startsWith('Connect an authorized calendar'); }
  sourceNeedsDocumentAccess(): boolean { return this.store.sourceNotice().startsWith('You do not have permission to use these documents'); }
  private discardAndNavigate(): void { this.store.clearDraft(); void this.router.navigate(['/briefings']); }
  private openConfirmation(confirmation: Confirmation): void { this.confirmation.set(confirmation); queueMicrotask(() => document.getElementById('briefing-confirm-cancel')?.focus()); }
  private triggerId(event: Event, fallback: string): string { const id = (event.currentTarget as HTMLElement | null)?.id; return id || fallback; }
}
