import { ChangeDetectionStrategy, ChangeDetectorRef, Component, HostListener, Injector, afterNextRender, effect, inject, signal } from '@angular/core';
import { A11yModule } from '@angular/cdk/a11y';
import { ActivatedRoute, Router, RouterLink } from '@angular/router';
import { ApprovalStore } from './approval-store';
import { approvalActionLabel, approvalLeadCopy, isReminderCommandProposal, reminderCommandConsequence, type ApprovalDetail, type ApprovalProposal, type ApprovalSummary, type ReminderCommandProposalDetail } from './approval-models';

type ReturnFocusState = { readonly returnFocusRequested?: boolean; readonly returnFocusId?: string | null };
function boundedFocusId(value: unknown): string | null { return typeof value === 'string' && value.startsWith('approval-') && value.length <= 160 && /^[A-Za-z0-9_-]+$/.test(value) ? value : null; }

@Component({ selector: 'aura-approvals-page', standalone: true, imports: [A11yModule, RouterLink], changeDetection: ChangeDetectionStrategy.OnPush, templateUrl: './approvals-page.component.html', styleUrl: './approvals-page.component.css' })
export class ApprovalsPageComponent {
  readonly store = inject(ApprovalStore);
  readonly selectedId = signal<string | null>(null);
  readonly decisionInFlight = signal<string | null>(null);
  private readonly route = inject(ActivatedRoute);
  private readonly router = inject(Router);
  private readonly injector = inject(Injector);
  private readonly changeDetector = inject(ChangeDetectorRef);
  private returnFocusState = this.readReturnFocusState();
  private routeGeneration = 0;
  private readonly initialResetEpoch = this.store.resetEpoch();
  private readonly resetWatcher = effect(() => {
    const resetEpoch = this.store.resetEpoch();
    if (resetEpoch === this.initialResetEpoch || !this.selectedId() || !this.route.snapshot.paramMap.get('id')) return;
    this.routeGeneration += 1;
    this.decisionInFlight.set(null);
    this.changeDetector.detectChanges();
    this.selectedId.set(null);
    this.returnFocusState = { returnFocusRequested: false, returnFocusId: null };
    void this.router.navigate(['/approvals'], { replaceUrl: true, state: this.returnFocusState });
  });

  constructor() {
    this.route.paramMap.subscribe((params) => { const id = params.get('id'); if (id !== this.selectedId()) { this.routeGeneration += 1; this.decisionInFlight.set(null); } this.selectedId.set(id); if (id) void this.openRoute(id); else { this.returnFocusState = this.readReturnFocusState(); this.store.clearDetail(); if (this.returnFocusState.returnFocusRequested) this.scheduleReturnFocus(); } });
  }
  inspect(approval: ApprovalSummary, event?: Event): void { const returnFocusId = boundedFocusId((event?.currentTarget as HTMLElement | null)?.id); this.returnFocusState = { returnFocusRequested: true, returnFocusId }; void this.router.navigate(['/approvals', approval.id], { state: this.returnFocusState }); }
  closeDetail(): void { this.routeGeneration += 1; this.decisionInFlight.set(null); this.changeDetector.detectChanges(); this.store.clearDetail(); this.selectedId.set(null); void this.router.navigate(['/approvals'], { state: { ...this.returnFocusState, returnFocusRequested: true } satisfies ReturnFocusState }); }
  retryDetail(): void { const id = this.selectedId(); if (id) void this.store.inspect(id); }
  retryDetailRecovery(): void { if (!this.selectedId()) return; const reconciliation = this.store.retry(); this.focusDetailPanel(); void reconciliation.finally(() => this.focusDetailPanel()); }
  async approve(approval: ApprovalDetail): Promise<void> { await this.runDecision(approval, () => this.store.approve(approval)); }
  async reject(approval: ApprovalDetail): Promise<void> { await this.runDecision(approval, () => this.store.reject(approval)); }
  statusLabel(status: ApprovalSummary['status']): string { return this.store.statusLabel(status); }
  executionLabel(status: ApprovalSummary['executionStatus']): string { return this.store.executionLabel(status); }
  actionLabel(approval: ApprovalSummary | ApprovalDetail): string { return approvalActionLabel(approval); }
  leadCopy(approval: ApprovalSummary): string { return approvalLeadCopy(approval); }
  isCommandProposal(proposal: ApprovalProposal): proposal is ReminderCommandProposalDetail { return isReminderCommandProposal(proposal); }
  commandSummary(_approval: ApprovalDetail, proposal: ReminderCommandProposalDetail): string { return proposal.summary; }
  commandConsequence(proposal: ReminderCommandProposalDetail): string { return reminderCommandConsequence(proposal); }
  formatDate(value: string | null | undefined): string { return value ? new Intl.DateTimeFormat(undefined, { year: 'numeric', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit', timeZoneName: 'short' }).format(new Date(value)) : 'Unknown time'; }
  isTerminal(approval: ApprovalSummary): boolean { return approval.status !== 'pending'; }
  private async runDecision(approval: ApprovalDetail, decision: () => Promise<boolean>): Promise<void> {
    const routeId = this.selectedId();
    if (routeId !== approval.id || this.decisionInFlight() !== null) return;
    const routeGeneration = this.routeGeneration;
    this.decisionInFlight.set(approval.id);
    this.changeDetector.detectChanges();
    try { await decision(); }
    finally { if (routeGeneration === this.routeGeneration && this.selectedId() === approval.id && this.decisionInFlight() === approval.id) { this.decisionInFlight.set(null); this.changeDetector.detectChanges(); } }
  }
  private async openRoute(id: string): Promise<void> { await this.store.inspect(id); }
  private focusDetailPanel(): void { afterNextRender(() => { if (typeof document === 'undefined' || !this.selectedId()) return; (document.getElementById('approval-detail-panel') as HTMLElement | null)?.focus(); }, { injector: this.injector }); }
  private scheduleReturnFocus(): void { afterNextRender(() => { if (typeof document === 'undefined') return; const target = this.returnFocusState.returnFocusId ? document.getElementById(this.returnFocusState.returnFocusId) : document.getElementById('approvals-heading'); (target as HTMLElement | null)?.focus(); }, { injector: this.injector }); }
  private readReturnFocusState(): ReturnFocusState { const state = this.router.getCurrentNavigation()?.extras.state ?? (typeof history !== 'undefined' ? history.state : null); return { returnFocusRequested: state?.['returnFocusRequested'] === true, returnFocusId: boundedFocusId(state?.['returnFocusId']) }; }
  @HostListener('document:keydown.escape') onEscape(): void { if (this.selectedId()) this.closeDetail(); }
}
