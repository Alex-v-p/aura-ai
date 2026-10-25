import { AfterViewChecked, AfterViewInit, ChangeDetectionStrategy, Component, DestroyRef, ElementRef, EventEmitter, Output, inject } from '@angular/core';
import { DatePipe } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { type RunStatus } from '@aura/observatory-api-client';
import { ObservatoryStore } from '../../observatory-store';
import { durationAriaLabel, formatDurationMs, normalizeRunFilters, type RunFilters } from '../../observatory-models';

@Component({
  selector: 'observatory-runs', standalone: true, imports: [FormsModule, DatePipe], changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <h2 class="panel-title">Recent metadata runs</h2><form class="filters" (ngSubmit)="apply()" aria-label="Run filters"><label>Status <select name="status" [(ngModel)]="draft.status"><option [ngValue]="undefined">All statuses</option>@for (status of statuses; track status) { <option [value]="status">{{ status }}</option> }</select></label><label>Component <input name="component" [(ngModel)]="draft.component" placeholder="component id" /></label><details class="advanced-filters"><summary>More filters</summary><div class="advanced-filter-grid"><label>Model <input name="model" [(ngModel)]="draft.model" placeholder="model label" /></label><label>Deployment <input name="deployment" [(ngModel)]="draft.deployment" placeholder="deployment id" /></label><label>From (UTC) <input name="from" type="datetime-local" [(ngModel)]="draft.from" /></label><label>To (UTC) <input name="to" type="datetime-local" [(ngModel)]="draft.to" /></label></div></details><button type="submit">Apply filters</button><button type="button" class="clear-button" (click)="clearFilters()">Clear filters</button></form><p class="active-filter-summary" role="status" aria-live="polite">{{ activeFilterSummary() }}</p>
    @if (store.resourceState('runs') === 'loading') { <div class="route-skeleton runs-skeleton" role="status" aria-label="Loading recent runs"><span></span><span></span><span></span></div> } @else if (store.resourceState('runs') === 'error') { <p class="failure" role="alert">Recent runs are unavailable. <button type="button" (click)="retry()">Retry recent runs</button></p> } @else if (store.resourceState('runs') === 'empty') { <section class="empty empty-state" role="status" aria-live="polite"><strong>No metadata runs match these filters.</strong><p>{{ hasActiveFilters() ? 'Adjust the filters or clear them to browse recent runs.' : 'No recent runs are available in the current snapshot.' }}</p>@if (hasActiveFilters()) { <button type="button" class="clear-button" (click)="clearFilters()">Clear filters</button> } @else { <button type="button" (click)="retry()">Refresh runs</button> }</section> } @else { @if (store.resourceState('runs') === 'stale') { <p class="notice" role="status">Recent runs could not be refreshed; showing the last known results. <button type="button" (click)="retry()">Retry</button></p> } <div class="table-wrap"><table><caption class="sr-only">Recent Observatory runs</caption><thead><tr><th scope="col">Run</th><th scope="col">Status</th><th scope="col">Started</th><th scope="col">Duration</th><th scope="col">Model</th><th scope="col">Deployment</th><th scope="col"><span class="sr-only">Actions</span></th></tr></thead><tbody>@for (run of store.snapshot().runs?.items ?? []; track run.runId) { <tr><th scope="row"><button class="link" type="button" (click)="select(run.runId)">{{ run.runId }}</button></th><td><span class="status" [class]="'status-' + run.status"><span class="status-icon" aria-hidden="true">{{ statusIcon(run.status) }}</span>{{ run.status }}</span></td><td>{{ run.startedAt | date:'short' }}</td><td><span [attr.aria-label]="durationAriaLabel(run.durationMs)">{{ formatDuration(run.durationMs) }}</span></td><td>{{ run.model ?? '—' }}</td><td>{{ run.deploymentId }}</td><td>@if (run.traceId) { <button class="link trace-action" type="button" (click)="select(run.runId)"><span aria-hidden="true">◌</span> Trace</button> }</td></tr> }</tbody></table></div> }
    @if (store.snapshot().state === 'stale' || store.snapshot().state === 'disconnected') { <p class="notice" role="status">Data may be stale: {{ store.snapshot().error }}</p> }
  `,
  styles: [`
    :host { display:block; } .panel-title { font-size:1.15rem; margin:0 0 .8rem; } .filters { align-items:end; background:var(--surface); border:1px solid var(--border); border-radius:1rem; display:grid; gap:.75rem; grid-template-columns:repeat(3,minmax(0,1fr)); margin-bottom:1rem; padding:1rem; } label { color:var(--text-muted); display:grid; font-size:.8rem; gap:.3rem; } input,select { background:var(--surface-raised); border:1px solid var(--control-border); border-radius:.4rem; color:var(--text); min-height:2.2rem; padding:.35rem .5rem; } input:focus,select:focus,button:focus-visible { outline:3px solid var(--focus); outline-offset:2px; } button { background:var(--accent); border:0; border-radius:.4rem; color:var(--on-accent); cursor:pointer; min-height:2.2rem; padding:.4rem .75rem; } .link { background:transparent; color:var(--accent-hover); font-weight:700; padding:0; text-decoration:underline; } .table-wrap { background:var(--surface); border:1px solid var(--border); border-radius:1rem; overflow:auto; } table { border-collapse:collapse; width:100%; } th,td { border-bottom:1px solid var(--border); padding:.8rem .75rem; text-align:left; } thead th { color:var(--text-muted); font-size:.75rem; letter-spacing:.04em; text-transform:uppercase; } tbody th { font-weight:500; } .status { align-items:center; border-radius:999px; display:inline-flex; gap:.3rem; font-size:.75rem; font-weight:700; padding:.25rem .5rem; text-transform:capitalize; } .status-icon,.trace-action [aria-hidden] { font-size:1rem; line-height:1; } .status-succeeded { background:var(--success-surface); color:var(--success-text); } .status-failed { background:var(--danger-surface); color:var(--danger-text); } .status-running,.status-queued { background:var(--warning-surface); color:var(--warning-text); } .status-cancelled,.status-unknown { background:var(--surface-subtle); color:var(--text-muted); } .muted,.empty { color:var(--text-muted); padding:1.5rem; text-align:center; } .notice { background:var(--warning-surface); color:var(--warning-text); padding:.75rem 1rem; } .sr-only { height:1px; margin:-1px; overflow:hidden; position:absolute; width:1px; clip:rect(0,0,0,0); } @media (max-width:700px) { .filters { grid-template-columns:1fr; } .table-wrap { overflow:visible; } table,thead,tbody,tr,th,td { display:block; } thead { height:1px; overflow:hidden; position:absolute; width:1px; } tbody tr { border-bottom:1px solid var(--border); padding:.5rem .75rem; } tbody th,tbody td { align-items:baseline; border:0; display:flex; gap:.5rem; justify-content:space-between; padding:.4rem 0; text-align:right; } tbody th::before,tbody td::before { color:var(--text-muted); content:'Run'; font-size:.72rem; font-weight:600; text-align:left; } tbody td:nth-child(2)::before { content:'Status'; } tbody td:nth-child(3)::before { content:'Started'; } tbody td:nth-child(4)::before { content:'Duration'; } tbody td:nth-child(5)::before { content:'Model'; } tbody td:nth-child(6)::before { content:'Deployment'; } tbody td:nth-child(7)::before { content:'Action'; } }
  `, `
    .filters { background:var(--surface-subtle); border:0; border-radius:.25rem; border-block:1px solid var(--border); padding:1rem 0; }
    .table-wrap { background:var(--surface); border:0; border-top:2px solid var(--control-border); border-radius:0; }
    th,td { padding:.7rem .6rem; }
    tbody th { font-family:ui-monospace,SFMono-Regular,Consolas,monospace; font-size:.82rem; }
    @media (min-width:701px) and (max-width:980px) { .filters { grid-template-columns:repeat(2,minmax(0,1fr)); } th,td { padding:.65rem .45rem; } }
    @media (max-width:700px) { .filters { padding-block:.8rem; } tbody tr { background:var(--surface); border-left:2px solid var(--control-border); } }
    .advanced-filters { align-self:stretch; min-width:0; }
    .advanced-filters summary { color:var(--accent-hover); cursor:pointer; font-size:.8rem; font-weight:700; padding:.55rem 0; }
    .advanced-filter-grid { display:grid; gap:.75rem; grid-template-columns:repeat(4,minmax(0,1fr)); margin-top:.35rem; }
    .clear-button { background:transparent; border:1px solid var(--control-border); color:var(--text); }
    .active-filter-summary { color:var(--text-muted); font-size:.78rem; margin:-.5rem 0 1rem; }
    @media (min-width:701px) { .advanced-filters { grid-column:1 / -1; } .advanced-filters summary { max-width:max-content; } }
    @media (max-width:700px) { .advanced-filter-grid { grid-template-columns:1fr; } .active-filter-summary { margin-top:-.25rem; } }
    .route-skeleton { display:grid; gap:.65rem; }
    .route-skeleton span { background:var(--surface-subtle); border:1px solid var(--border); border-radius:.35rem; display:block; height:2.4rem; }
    .runs-skeleton span:first-child { width:82%; }
    .runs-skeleton span:nth-child(2) { width:94%; }
    .runs-skeleton span:nth-child(3) { width:68%; }
  `, `
    .panel-title { font-size:clamp(1.35rem,2vw,1.7rem); margin-bottom:1.25rem; }
    .filters { align-items:end; background:var(--surface); border:1px solid var(--border); border-radius:.65rem; gap:1rem; margin-bottom:.65rem; max-width:var(--obs-control-max); padding:1.25rem; }
    .filters > button { justify-self:start; min-width:8.5rem; }
    .filters > .clear-button { justify-self:start; }
    .advanced-filter-grid { gap:1rem; }
    .active-filter-summary { margin:.25rem 0 1.5rem; }
    .table-wrap { border-radius:.65rem; }
    .empty { background:var(--surface); border:1px dashed var(--control-border); border-radius:.65rem; margin-top:1.5rem; padding:2.5rem 1.5rem; }
    .empty-state { display:grid; gap:.6rem; justify-items:start; max-width:40rem; text-align:left; }
    .empty-state p { color:var(--text-muted); margin:0; }
    .empty-state button { margin-top:.35rem; }
    .notice,.failure { border-radius:.45rem; margin-top:1.5rem; padding:.8rem 1rem; }
    @media (min-width:701px) { .filters { grid-template-columns:repeat(2,minmax(12rem,22rem)); } .filters .advanced-filters { grid-column:1 / -1; } }
    @media (max-width:700px) { .filters { padding:1rem; } .filters > button,.filters > .clear-button { justify-self:stretch; } }
  `],
})
export class ObservatoryRunsComponent implements AfterViewChecked, AfterViewInit {
  readonly store = inject(ObservatoryStore);
  private readonly host = inject(ElementRef<HTMLElement>);
  private readonly destroyRef = inject(DestroyRef);
  private visibilityObserver: MutationObserver | null = null;
  private active = false;
  readonly statuses: RunStatus[] = ['queued', 'running', 'succeeded', 'failed', 'cancelled', 'unknown'];
  draft: RunFilters = {};
  @Output() readonly runSelected = new EventEmitter<string>();
  ngAfterViewInit(): void {
    this.syncActivation();
    if (typeof MutationObserver !== 'undefined') {
      this.visibilityObserver = new MutationObserver(() => this.syncActivation());
      this.visibilityObserver.observe(this.host.nativeElement, { attributes: true, attributeFilter: ['hidden', 'style'] });
    }
    this.destroyRef.onDestroy(() => {
      this.visibilityObserver?.disconnect();
      this.visibilityObserver = null;
      this.store.deactivateRunsView();
    });
  }
  ngAfterViewChecked(): void {
    // The shell keeps this workspace mounted and toggles its host visibility
    // during the parent's change-detection pass. A view check observes that
    // route boundary synchronously, even when a browser does not deliver the
    // corresponding attribute mutation before the first catalogue response.
    this.syncActivation();
  }
  private syncActivation(): void {
    const element = this.host.nativeElement;
    const active = !element.hidden && element.style.display !== 'none';
    if (active === this.active) return;
    this.active = active;
    if (active) this.store.activateRunsView();
    else this.store.deactivateRunsView();
  }
  apply(): void { this.store.setFilters(normalizeRunFilters(this.draft)); }
  retry(): void { void this.store.load(); }
  clearFilters(): void { this.draft = {}; this.apply(); }
  hasActiveFilters(): boolean { const applied = this.store.filters(); return [applied.status, applied.component, applied.model, applied.deployment, applied.from, applied.to].some(Boolean); }
  activeFilterSummary(): string { const applied = this.store.filters(); const values = [applied.status, applied.component, applied.model, applied.deployment, applied.from, applied.to].filter(Boolean); return values.length ? `${values.length} filter${values.length === 1 ? '' : 's'} active` : 'No filters applied'; }
  formatDuration(durationMs: number | null): string { return formatDurationMs(durationMs); }
  durationAriaLabel(durationMs: number | null): string { return durationAriaLabel(durationMs); }
  statusIcon(status: RunStatus): string { return status === 'succeeded' ? '✓' : status === 'failed' ? '!' : status === 'cancelled' ? '×' : status === 'running' ? '◌' : status === 'queued' ? '…' : '?'; }
  select(id: string): void { void this.store.openRun(id); this.runSelected.emit(id); }
}
