import { ChangeDetectionStrategy, Component, ElementRef, ViewChild, inject } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { ObservatoryStore } from '../../observatory-store';
import { datetimeLocalToUtc } from '../../observatory-models';

export function retentionFailureMessage(code: string): string {
  switch (code) {
    case 'storage_failure': return 'Observations could not be deleted because storage is temporarily unavailable. Retry when the service recovers.';
    case 'conflict': return 'Another retention action is already in progress. Wait for it to finish, then retry.';
    case 'cancelled': return 'The retention action was cancelled before it completed. No further action was taken.';
    default: return 'The retention action could not be completed. Retry or contact the Observatory administrator.';
  }
}

@Component({
  selector: 'observatory-retention', standalone: true, imports: [FormsModule], changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <section class="retention-card"><p class="eyebrow">Manual retention</p><h2>Delete stored observations</h2><p class="muted">Observatory retention is indefinite until you authorize a deletion. Component and deployment catalogues remain available.</p><label>Delete observations before (UTC)<input type="datetime-local" [(ngModel)]="cutoff" [disabled]="store.retention().running" /></label>@if (effectiveUtc) { <p class="effective">Effective UTC boundary: <strong>{{ effectiveUtc }}</strong></p> }<div class="actions"><button type="button" (click)="confirmCutoff()" [disabled]="!cutoff || store.retention().running">Delete before cutoff</button><button class="danger" type="button" (click)="confirmPurge()" [disabled]="store.retention().running">Purge all observations</button></div>@if (store.retention().running) { <p class="progress" role="status">Deletion is running. Views will refresh when the worker completes…</p> } @if (store.retention().action; as action) { <dl class="result"><div><dt>Action</dt><dd>{{ action.action }}</dd></div><div><dt>Status</dt><dd>{{ action.status }}</dd></div><div><dt>Deleted observations</dt><dd>{{ action.deletedObservationCount }}</dd></div></dl> } @if (store.retention().error; as failure) { <p class="failure" role="alert">{{ retentionFailureMessage(failure) }} <small>Reference code: {{ failure }}</small></p> }</section><dialog #confirmDialog aria-labelledby="confirm-title"><form method="dialog" (submit)="cancel()"><h3 id="confirm-title">Confirm destructive retention action</h3>@if (isDeleteBeforePending()) { <p>Delete observations before <strong>{{ pendingCutoff() }}</strong> (UTC).</p> } @else { <p>Permanently purge all stored observations.</p> }<p>Up to <strong>{{ store.snapshot().overview?.observationCount ?? 0 }}</strong> currently stored observations may be affected. Component and deployment catalogues, configuration, sessions, and deletion audit records are preserved.</p><div class="dialog-actions"><button type="submit" autofocus>Cancel</button><button class="danger" type="button" (click)="execute()">Confirm {{ isPurgeAllPending() ? 'purge-all' : 'delete-before' }}</button></div></form></dialog>
  `,
  styles: [`
    :host { display:block; } .retention-card { background:var(--surface); border:1px solid var(--border); border-radius:1rem; max-width:44rem; padding:1.35rem; } .eyebrow { color:var(--text-muted); font-size:.75rem; font-weight:750; letter-spacing:.08em; margin:0 0 .45rem; text-transform:uppercase; } h2 { font-size:1.2rem; margin:0; } .muted { color:var(--text-muted); line-height:1.5; } label { display:grid; color:var(--text-muted); font-size:.82rem; gap:.35rem; margin-top:1.1rem; } input { background:var(--surface-raised); border:1px solid var(--control-border); border-radius:.4rem; color:var(--text); min-height:2.3rem; padding:.4rem .5rem; } .effective { background:var(--info-surface); color:var(--info-text); padding:.55rem .7rem; } .actions,.dialog-actions { display:flex; flex-wrap:wrap; gap:.7rem; margin-top:1rem; } button { background:var(--accent); border:0; border-radius:.45rem; color:var(--on-accent); cursor:pointer; padding:.65rem .8rem; } button.danger { background:var(--danger); } button:disabled { cursor:not-allowed; opacity:.55; } button:focus-visible,input:focus-visible { outline:3px solid var(--focus); outline-offset:2px; } .progress { background:var(--info-surface); color:var(--info-text); padding:.7rem; } .failure { background:var(--danger-surface); color:var(--danger-text); padding:.7rem; } .failure small { color:var(--text-muted); display:block; margin-top:.4rem; } .result { border-top:1px solid var(--border); display:flex; flex-wrap:wrap; gap:1.2rem; margin-top:1rem; padding-top:1rem; } dt { color:var(--text-muted); font-size:.75rem; } dd { font-weight:700; margin:.2rem 0 0; } dialog { background:var(--surface); border:1px solid var(--border); border-radius:1rem; color:var(--text); max-width:32rem; padding:1.25rem; } dialog::backdrop { background:#1b121880; } h3 { margin-top:0; }
  `, `
    .retention-card { background:var(--surface); border:0; border-top:3px solid var(--danger); border-radius:0; padding:1.25rem 0; }
    .actions button { border:1px solid var(--accent); }
    .actions button.danger { background:var(--danger); border-color:var(--danger); }
    dialog { border:2px solid var(--danger); border-radius:.35rem; box-shadow:0 1rem 3rem #0008; }
    dialog::backdrop { background:rgb(0 0 0 / .72); }
    .dialog-actions button[type="submit"] { background:var(--surface); border:1px solid var(--control-border); color:var(--text); }
    .dialog-actions button.danger { background:var(--danger); border-color:var(--danger); color:var(--on-accent); }
    .progress { border-left:3px solid var(--info-text); }
    dialog { margin:auto; max-height:min(calc(100dvh - 2rem), 36rem); max-width:min(32rem, calc(100vw - 2rem)); overflow:auto; width:calc(100vw - 2rem); }
    @media (max-width:520px) { .dialog-actions { align-items:stretch; flex-direction:column; } }
  `],
})
export class ObservatoryRetentionComponent {
  readonly store = inject(ObservatoryStore);
  readonly retentionFailureMessage = retentionFailureMessage;
  @ViewChild('confirmDialog') private readonly dialog?: ElementRef<HTMLDialogElement>;
  private returnFocus: HTMLElement | undefined;
  cutoff = '';
  effectiveUtc = '';
  pending: { action: 'delete_before'; cutoff: string; confirmation: 'delete-before' } | { action: 'purge_all'; confirmation: 'purge-all' } | null = null;
  confirmCutoff(): void { const value = datetimeLocalToUtc(this.cutoff); if (!value) return; this.returnFocus = document.activeElement instanceof HTMLElement ? document.activeElement : undefined; this.effectiveUtc = value; this.pending = { action: 'delete_before', cutoff: value, confirmation: 'delete-before' }; this.dialog?.nativeElement.showModal(); }
  confirmPurge(): void { this.returnFocus = document.activeElement instanceof HTMLElement ? document.activeElement : undefined; this.effectiveUtc = ''; this.pending = { action: 'purge_all', confirmation: 'purge-all' }; this.dialog?.nativeElement.showModal(); }
  isDeleteBeforePending(): boolean { return this.pending?.action === 'delete_before'; }
  isPurgeAllPending(): boolean { return this.pending?.action === 'purge_all'; }
  pendingCutoff(): string { return this.pending?.action === 'delete_before' ? this.pending.cutoff : ''; }
  cancel(): void { this.pending = null; this.dialog?.nativeElement.close(); this.restoreFocus(); }
  execute(): void { const request = this.pending; if (!request) return; this.pending = null; this.dialog?.nativeElement.close(); this.restoreFocus(); void this.store.createRetention(request); }
  private restoreFocus(): void { const target = this.returnFocus; this.returnFocus = undefined; setTimeout(() => target?.focus(), 0); }
}
