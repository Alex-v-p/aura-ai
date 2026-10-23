import { ChangeDetectionStrategy, Component, EventEmitter, Input, Output, inject } from '@angular/core';
import { DatePipe } from '@angular/common';
import { ObservatoryStore } from '../../observatory-store';

@Component({
  selector: 'observatory-trace', standalone: true, imports: [DatePipe], changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <h2 id="trace-title" tabindex="-1">Trace</h2>@if (store.selectedRun().detail; as run) { <section class="run-card"><div><p class="eyebrow">Selected run</p><h3>{{ run.runId }}</h3><p class="muted">{{ run.status }} · {{ run.startedAt | date:'medium' }} · {{ run.durationMs === null ? 'duration pending' : run.durationMs + ' ms' }}</p></div><span class="status">{{ run.partial || store.selectedRun().trace?.partial ? 'Partial trace' : 'Complete trace' }}</span></section> }
    @if (store.selectedRunState() === 'loading') { <div class="route-skeleton trace-skeleton" role="status" aria-label="Loading the selected run trace"><span></span><span></span><span></span></div> } @else if (store.selectedRunState() === 'error') { <p class="failure" role="alert">{{ store.selectedRunError() }} <button type="button" (click)="retry()">Retry trace</button></p> } @else if (!store.selectedRun().trace) { <section class="empty empty-state" role="status" aria-live="polite"><p>Select a run with a trace to inspect its parent-child spans.</p><button type="button" (click)="browseRuns.emit()">Browse runs</button></section> } @else { <section class="trace-card" aria-labelledby="trace-id"><div class="trace-heading"><div><p class="eyebrow">Trace waterfall</p><h3 id="trace-id">{{ store.selectedRun().trace?.traceId }}</h3></div><span class="muted">{{ store.selectedRun().trace?.spans?.length ?? 0 }} spans</span></div><div class="waterfall-legend" aria-label="Trace waterfall scale"><span><i aria-hidden="true"></i>Relative duration</span><span>Longest span: {{ maxDuration() }} ms</span></div><ul class="trace-tree" aria-label="Ordered trace spans">@for (span of store.selectedRun().trace?.spans ?? []; track span.spanId) { <li class="span-row" [style.--depth]="span.depth"><div class="span-name"><span class="sr-only">Level {{ span.depth + 1 }}. </span><span class="branch" aria-hidden="true">{{ span.depth ? '↳' : '•' }}</span><strong>{{ span.operation }}</strong><small>{{ span.componentId }}</small>@if (span.partial) { <span class="partial">partial / missing parent</span> }</div><div class="waterfall" [attr.aria-label]="'Duration bar: ' + span.durationMs + ' milliseconds'"><span [style.width.%]="barWidth(span.durationMs)"></span></div><time>{{ span.durationMs }} ms</time><span class="outcome" [class]="'outcome-' + span.outcome">{{ span.outcome }}</span></li> }</ul></section> }
  `,
  styles: [`
    :host { display:block; } .run-card,.trace-card { background:var(--surface); border:1px solid var(--border); border-radius:1rem; margin-bottom:1rem; padding:1.25rem; } .run-card,.trace-heading { align-items:flex-start; display:flex; justify-content:space-between; gap:1rem; } h2 { font-size:1.1rem; margin:0 0 1rem; overflow-wrap:anywhere; } h3 { font-size:1.1rem; margin:0; overflow-wrap:anywhere; } h2:focus-visible { outline:3px solid var(--focus); outline-offset:3px; } .eyebrow { color:var(--text-muted); font-size:.75rem; font-weight:750; letter-spacing:.08em; margin:0 0 .4rem; text-transform:uppercase; } .muted,small { color:var(--text-muted); } .muted { margin:.45rem 0 0; } .status,.outcome,.partial { border-radius:999px; font-size:.75rem; font-weight:700; padding:.25rem .5rem; text-transform:capitalize; } .status { background:var(--info-surface); color:var(--info-text); } .trace-tree { border-top:1px solid var(--border); list-style:none; margin:1rem 0 0; overflow:hidden; padding:0; } .span-row { align-items:center; border-bottom:1px solid var(--border); display:grid; gap:.75rem; grid-template-columns:minmax(12rem,1fr) minmax(8rem,2fr) 5rem 6rem; padding:.8rem .25rem .8rem calc(.25rem + (var(--depth) * 1.15rem)); } .span-name { display:grid; gap:.15rem; min-width:0; } .span-name strong { overflow-wrap:anywhere; } .branch { color:var(--accent); float:left; margin-right:.3rem; } .waterfall { background:var(--surface-subtle); border-radius:999px; height:.55rem; overflow:hidden; } .waterfall span { background:var(--accent); border-radius:inherit; display:block; height:100%; min-width:.35rem; } .outcome-succeeded { background:var(--success-surface); color:var(--success-text); } .outcome-failed { background:var(--danger-surface); color:var(--danger-text); } .outcome-cancelled,.outcome-unknown { background:var(--surface-subtle); color:var(--text-muted); } .partial { background:var(--warning-surface); color:var(--warning-text); justify-self:start; } .failure { background:var(--danger-surface); color:var(--danger-text); padding:.7rem; } .failure button { background:var(--accent); border:0; border-radius:.4rem; color:var(--on-accent); padding:.45rem .7rem; } .empty { color:var(--text-muted); padding:1rem 0; } .sr-only { height:1px; margin:-1px; overflow:hidden; position:absolute; width:1px; clip:rect(0,0,0,0); } @media (max-width:700px) { .span-row { align-items:start; grid-template-columns:1fr auto; padding-left:calc(.5rem + (var(--depth) * 1.15rem)); } .span-name { grid-column:1 / -1; } .waterfall { grid-column:1; } .span-row time,.span-row .outcome { align-self:center; justify-self:end; } }
  `, `
    .run-card,.trace-card { background:transparent; border:0; border-top:2px solid var(--control-border); border-radius:0; padding:1.1rem 0; }
    .waterfall-legend { align-items:center; border-bottom:1px solid var(--border); color:var(--text-muted); display:flex; flex-wrap:wrap; font-size:.78rem; gap:.5rem 1.2rem; justify-content:space-between; margin-top:1rem; padding-bottom:.55rem; }
    .waterfall-legend i { background:var(--accent); display:inline-block; height:.55rem; margin-right:.35rem; width:1rem; }
    .waterfall { background:repeating-linear-gradient(to right,var(--surface-subtle) 0,var(--surface-subtle) calc(25% - 1px),var(--border) 25%); border-radius:.2rem; }
    .waterfall span { background:var(--accent); border-radius:.1rem; }
    @media (max-width:700px) { .trace-heading { flex-direction:column; } .waterfall-legend { align-items:flex-start; flex-direction:column; } }
    .route-skeleton { display:grid; gap:.8rem; margin-top:1rem; }
    .route-skeleton span { background:var(--surface-subtle); border:1px solid var(--border); border-radius:.25rem; display:block; height:2.3rem; }
    .trace-skeleton span:first-child { height:4rem; }
    .trace-skeleton span:nth-child(2) { margin-inline:1rem; }
  `, `
    h2 { font-size:clamp(1.35rem,2vw,1.7rem); margin-bottom:1.25rem; }
    .run-card,.trace-card { border-radius:.65rem; margin-bottom:1.5rem; padding:clamp(1.25rem,2vw,1.75rem); }
    .empty { align-items:flex-start; background:var(--surface); border:1px dashed var(--control-border); border-radius:.65rem; display:flex; min-height:9rem; padding:2rem; }
    .empty-state { display:grid; gap:.8rem; justify-items:start; max-width:40rem; }
    .empty-state p { margin:0; }
    .failure { border-left:3px solid var(--danger-text); border-radius:.45rem; padding:1rem; }
    .trace-tree { margin-top:1.25rem; }
    .span-row { gap:1rem; padding-block:1rem; }
    @media (max-width:700px) { .run-card,.trace-card { padding:1rem; } .empty { min-height:7rem; padding:1.25rem; } }
  `],
})
export class ObservatoryTraceComponent {
  readonly store = inject(ObservatoryStore);
  @Output() readonly browseRuns = new EventEmitter<void>();
  @Input() set runId(value: string | null) { if (value) void this.store.openRun(value); }
  barWidth(duration: number): number { const spans = this.store.selectedRun().trace?.spans ?? []; const max = Math.max(...spans.map((span) => span.durationMs), 1); return Math.max(2, Math.round((duration / max) * 100)); }
  maxDuration(): number { return Math.max(...(this.store.selectedRun().trace?.spans ?? []).map((span) => span.durationMs), 1); }
  retry(): void { const runId = this.store.selectedRunId(); if (runId) void this.store.openRun(runId); }
}
