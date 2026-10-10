import { ChangeDetectionStrategy, Component, inject, signal } from '@angular/core';
import { RouterLink } from '@angular/router';
import { ApprovalStore } from './approval-store';
import type { ApprovalDetail, ApprovalSummary, ApprovalStatus } from './approval-models';

@Component({
  selector: 'aura-approvals-page',
  standalone: true,
  imports: [RouterLink],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './approvals-page.component.html',
  styleUrl: './approvals-page.component.css',
})
export class ApprovalsPageComponent {
  readonly store = inject(ApprovalStore);
  readonly selectedId = signal<string | null>(null);
  readonly statuses: ReadonlyArray<'all' | ApprovalStatus> = ['all', 'pending', 'approved', 'rejected', 'expired', 'canceled'];
  constructor() { void this.store.start(); }
  async inspect(approval: ApprovalSummary): Promise<void> { this.selectedId.set(approval.id); await this.store.inspect(approval.id); }
  closeDetail(): void { this.selectedId.set(null); this.store.selected.set(null); }
  async approve(approval: ApprovalSummary): Promise<void> { await this.store.approve(approval); }
  async reject(approval: ApprovalSummary): Promise<void> { await this.store.reject(approval); }
  label(status: ApprovalStatus): string { return this.store.statusLabel(status); }
  executionLabel(status: ApprovalSummary['executionStatus']): string { return status === 'not_started' ? 'Waiting to run' : status === 'queued' ? 'Queued' : status === 'executing' ? 'Executing' : status === 'completed' ? 'Completed' : status === 'denied' ? 'Denied' : 'Failed'; }
  formatDate(value: string | null | undefined): string { return value ? new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' }).format(new Date(value)) : 'Unknown time'; }
  canDecide(approval: ApprovalSummary): boolean { return approval.status === 'pending'; }
  isTerminal(approval: ApprovalSummary): boolean { return approval.status !== 'pending'; }
  trackApproval(_: number, approval: ApprovalSummary): string { return approval.id; }
  asSummary(detail: ApprovalDetail): ApprovalSummary { return detail; }
}
