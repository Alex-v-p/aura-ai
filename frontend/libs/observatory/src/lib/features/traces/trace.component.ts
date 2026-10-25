import { ChangeDetectionStrategy, Component, EventEmitter, Input, Output, inject } from '@angular/core';
import { DatePipe } from '@angular/common';
import type { RunDetail, TraceDetail } from '@aura/observatory-api-client';
import { ObservatoryStore } from '../../observatory-store';
import { durationAriaLabel, formatDurationMs } from '../../observatory-models';
import { continuationSummary as summarizeContinuation, foregroundPhaseItemsFor, isAtOrBeforeBoundary, orderPhases, terminalObservationBoundary, type ContinuationSummary, type PhaseItem, type PhaseKey } from './trace-inspector.utils';

type PhaseDefinition = { readonly key: PhaseKey; readonly icon: string; readonly label: string; readonly description: string };
type Observation = RunDetail['observations'][number];
type Span = TraceDetail['spans'][number];
const TERMINAL_STATUSES = new Set(['succeeded', 'failed', 'cancelled']);
const MAX_CONTINUATION_PREVIEW = 25;

const PHASES: ReadonlyArray<PhaseDefinition> = [
  { key: 'request', icon: '↓', label: 'Request admission', description: 'Run admission and lifecycle boundaries' },
  { key: 'context', icon: '⌘', label: 'Context & retrieval', description: 'Memory, documents, search, and ranking timings' },
  { key: 'prompt', icon: '✦', label: 'Prompt construction', description: 'Prompt, persona, and context assembly metadata' },
  { key: 'routing', icon: '⇢', label: 'Model routing', description: 'Provider and model selection metadata' },
  { key: 'generation', icon: '✹', label: 'Generation', description: 'Inference and response generation timings' },
  { key: 'thinking', icon: '∴', label: 'Displayed thinking', description: 'Displayed thinking lifecycle metadata' },
  { key: 'tools', icon: '⚙', label: 'Tools & policy', description: 'Tool policy, approval, and execution metadata' },
  { key: 'persistence', icon: '▣', label: 'Persistence', description: 'Database, memory, and outbox timings' },
  { key: 'delivery', icon: '↗', label: 'Streaming & delivery', description: 'Streaming, SSE, and notification metadata' },
  { key: 'other', icon: '•', label: 'Other metadata', description: 'Related observations without a phase classifier' },
];

@Component({
  selector: 'observatory-trace', standalone: true, imports: [DatePipe], changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <h2 id="trace-title" tabindex="-1">Run inspector</h2>
    @if (store.selectedRun().detail; as run) {
      <section class="run-card" aria-labelledby="selected-run-title">
        <div class="run-heading"><p class="eyebrow">Selected run</p><h3 id="selected-run-title">{{ run.runId }}</h3><p class="muted">Started {{ run.startedAt | date:'medium' }}</p></div>
        <div class="run-summary"><span class="status" [class]="'status-' + run.status"><span aria-hidden="true">{{ statusIcon(run.status) }}</span>{{ run.status }}</span><span class="summary-value" [attr.aria-label]="durationAriaLabel(run.durationMs)"><strong>{{ formatDuration(run.durationMs) }}</strong><small>authoritative run duration</small></span><span class="capture-badge"><span aria-hidden="true">◈</span> Metadata only</span></div>
      </section>
      <p class="sr-only" role="status" aria-live="polite">Selected run {{ run.runId }} is {{ run.status }}. {{ run.durationMs === null ? 'Duration unavailable.' : 'Duration ' + durationAriaLabel(run.durationMs) + '.' }}</p>
      <p class="capture-note" role="note"><strong>Metadata-only capture.</strong> Timings, outcomes, component lineage, and measurements are available below. Prompts, chunks, generated content, displayed thinking text, and tool payloads are not captured in this Observatory view.</p>
      @if (store.selectedRunState() === 'loading') { <p class="notice" role="status">Refreshing this run; the last known projection remains visible.</p> } @else if (store.selectedRunState() === 'error') { <p class="notice" role="status">The latest run refresh is unavailable; showing the last known projection. <button type="button" (click)="retry()">Retry</button></p> }

      <section class="coverage-card" aria-labelledby="coverage-title"><div class="section-heading"><div><p class="eyebrow">Evidence coverage</p><h3 id="coverage-title">What is available for this run</h3></div><span class="muted">{{ evidenceAvailableCount() }} of {{ coverageItems().length }} evidence types</span></div><div class="coverage-grid">@for (item of coverageItems(); track item.key) { <div class="coverage-item"><span class="coverage-icon" aria-hidden="true">{{ item.icon }}</span><div><strong>{{ item.label }}</strong><p>{{ item.state === 'available' ? 'Metadata available' : item.state === 'not-observed' ? 'Not observed' : 'Not captured' }}</p></div></div> }</div></section>

      <section class="phases-card" aria-labelledby="phases-title"><div class="section-heading"><div><p class="eyebrow">Phase coverage</p><h3 id="phases-title">Captured phases</h3><p class="muted">The foreground timeline ends at the authoritative terminal lifecycle event.</p></div><span class="muted">{{ itemCount() }} metadata records</span></div><ol class="phase-list" aria-label="Run phase coverage">@for (phase of orderedPhases(); track phase.key) { <li class="phase-row" [class.phase-empty]="phaseItems(phase.key).length === 0"><div class="phase-marker" aria-hidden="true">{{ phase.icon }}</div><div class="phase-content"><div class="phase-title"><h4>{{ phase.label }}</h4><span class="phase-count">{{ phaseItems(phase.key).length ? phaseItems(phase.key).length + ' record' + (phaseItems(phase.key).length === 1 ? '' : 's') : 'Not observed' }}</span></div><p class="phase-description">{{ phase.description }}</p>@if (phaseItems(phase.key).length) { <ul class="phase-items">@for (item of phaseItems(phase.key); track item.id) { <li><span class="item-kind" aria-hidden="true">{{ item.kind === 'span' ? '↳' : '·' }}</span><span class="item-label"><strong>{{ item.label }}</strong><small>{{ item.component }}</small><time [attr.datetime]="item.startedAt">{{ item.startedAt | date:'HH:mm:ss.SSS' }} · {{ phaseOffset(item.startedAt, run.startedAt) }}</time></span><span class="item-time" [attr.aria-label]="recordValueAriaLabel(item)">{{ recordValue(item) }}</span><span class="outcome" [class]="'outcome-' + item.outcome">{{ item.outcome }}</span>@if (item.partial) { <span class="partial">partial</span> }</li> }</ul> } @else { <p class="phase-unavailable">Not observed in retained metadata.</p> }</div></li> }</ol></section>

      @if (continuationSummary(); as continuation) { @if (continuation.count > 0 || continuation.partial) { <section class="continuation-card" aria-labelledby="continuation-title"><div class="section-heading"><div><p class="eyebrow">Post-run activity</p><h3 id="continuation-title">Background continuation</h3><p class="muted">Correlated metadata after the request completed. It is not part of the request duration.</p></div><span class="phase-count"><span aria-hidden="true">↗</span> {{ continuation.count > 0 ? continuationCountLabel(continuation) : 'Page incomplete' }}</span></div>@if (continuation.count > 0) { <p class="continuation-range"><strong>{{ formatContinuationRange(continuation) }}</strong>@if (continuation.outcomes.length) { <span> · outcomes: {{ continuation.outcomes.join(', ') }}</span> }</p><details class="continuation-details" [open]="continuationOpen" (toggle)="continuationOpen = $any($event.target).open"><summary><span aria-hidden="true">⌄</span> Inspect a page-bounded preview ({{ continuationPreview().length }} of {{ continuationCountLabel(continuation) }})</summary><ul class="phase-items continuation-items" aria-label="Page-bounded post-run metadata preview">@for (item of continuationPreview(); track item.id) { <li><span class="item-kind" aria-hidden="true">·</span><span class="item-label"><strong>{{ item.label }}</strong><small>{{ item.component }}</small><time [attr.datetime]="item.startedAt">{{ item.startedAt | date:'HH:mm:ss.SSS' }}</time></span><span class="item-time" [attr.aria-label]="recordValueAriaLabel(item)">{{ recordValue(item) }}</span><span class="outcome" [class]="'outcome-' + item.outcome">{{ item.outcome }}</span></li> }</ul>@if (continuation.count > MAX_CONTINUATION_PREVIEW || continuation.partial) { <p class="continuation-limit">Showing at most the first {{ MAX_CONTINUATION_PREVIEW }} records from this loaded metadata page. @if (continuation.partial) { More correlated metadata may exist beyond this page. } @else { Remaining loaded records stay collapsed and are not rendered as request steps. }</p> }</details> } @else { <p class="continuation-limit" role="status">Further correlated metadata was not loaded; no post-run count or range is known from this page.</p> }</section> } }

      @if (store.selectedRun().trace; as trace) { <section class="trace-card" aria-labelledby="trace-id"><div class="trace-heading"><div><p class="eyebrow">Trace lineage</p><h3 id="trace-id">{{ trace.traceId }}</h3><p class="muted">Primary execution spans · {{ trace.spans.length }} retained</p></div><span class="status" [class.status-warning]="trace.partial"><span aria-hidden="true">{{ trace.partial ? '!' : '✓' }}</span>{{ trace.partial ? 'Partial trace' : 'Complete trace' }}</span></div><div class="waterfall-legend" aria-label="Trace waterfall scale"><span><i aria-hidden="true"></i>Relative duration</span><span>Longest span: {{ formatDuration(maxDuration()) }}</span></div><ul class="trace-tree" aria-label="Ordered trace spans">@for (span of trace.spans; track span.spanId) { <li class="span-row" [style.--depth]="span.depth"><div class="span-name"><span class="sr-only">Level {{ span.depth + 1 }}. </span><span class="branch" aria-hidden="true">{{ span.depth ? '↳' : '•' }}</span><strong>{{ span.operation }}</strong><small>{{ span.componentId }}</small>@if (span.partial) { <span class="partial">partial / missing parent</span> }</div><div class="waterfall" role="img" [attr.aria-label]="'Relative duration bar: ' + durationAriaLabel(span.durationMs)"><span aria-hidden="true" [style.width.%]="barWidth(span.durationMs)"></span></div><time [attr.aria-label]="durationAriaLabel(span.durationMs)">{{ formatDuration(span.durationMs) }}</time><span class="outcome" [class]="'outcome-' + span.outcome">{{ span.outcome }}</span></li> }</ul></section> } @else { <section class="trace-card trace-unavailable" aria-labelledby="trace-unavailable-title"><h3 id="trace-unavailable-title">Trace lineage unavailable</h3><p>The run metadata is retained, but its primary execution trace has not arrived or is outside the current retention window.</p><button type="button" (click)="retry()">Retry trace</button></section> }
    } @else if (store.selectedRunState() === 'loading') { @if (store.liveDisconnected()) { <p class="notice" role="status">Live updates are disconnected; retrying the selected run projection.</p> } <div class="route-skeleton trace-skeleton" role="status" aria-label="Loading the selected run inspector"><span></span><span></span><span></span></div> } @else { <section class="empty empty-state" role="status" aria-live="polite"><p>Select a run to inspect its lifecycle, timings, and retained metadata.</p><button type="button" (click)="browseRuns.emit()">Browse runs</button></section> }
  `,
  styles: [`
    :host { display:block; } .run-card,.coverage-card,.phases-card,.trace-card,.continuation-card { background:var(--surface); border:1px solid var(--border); border-radius:1rem; margin-bottom:1rem; padding:1.25rem; } .run-card,.trace-heading,.section-heading { align-items:flex-start; display:flex; justify-content:space-between; gap:1rem; } h2 { font-size:1.1rem; margin:0 0 1rem; overflow-wrap:anywhere; } h3 { font-size:1.1rem; margin:0; overflow-wrap:anywhere; } h4 { font-size:.95rem; margin:0; } h2:focus-visible { outline:3px solid var(--focus); outline-offset:3px; } .eyebrow { color:var(--text-muted); font-size:.75rem; font-weight:750; letter-spacing:.08em; margin:0 0 .4rem; text-transform:uppercase; } .muted,small { color:var(--text-muted); } .muted { margin:.45rem 0 0; } .run-heading h3 { font-family:ui-monospace,SFMono-Regular,Consolas,monospace; font-size:1rem; } .run-summary { align-items:flex-end; display:flex; flex-wrap:wrap; gap:.6rem; justify-content:flex-end; } .status,.outcome,.partial,.phase-count,.capture-badge { align-items:center; border-radius:999px; display:inline-flex; font-size:.75rem; font-weight:700; gap:.25rem; padding:.25rem .5rem; text-transform:capitalize; } .status { background:var(--info-surface); color:var(--info-text); } .status-succeeded,.outcome-succeeded { background:var(--success-surface); color:var(--success-text); } .status-failed,.outcome-failed { background:var(--danger-surface); color:var(--danger-text); } .status-running,.status-queued { background:var(--warning-surface); color:var(--warning-text); } .status-cancelled,.status-unknown,.outcome-cancelled,.outcome-unknown { background:var(--surface-subtle); color:var(--text-muted); } .status-warning,.partial { background:var(--warning-surface); color:var(--warning-text); } .capture-badge { background:var(--surface-subtle); color:var(--text-muted); } .summary-value { display:grid; gap:.1rem; text-align:right; } .summary-value strong { font-size:1.15rem; } .summary-value small { font-size:.68rem; } .capture-note { background:var(--surface-subtle); border-left:3px solid var(--accent); color:var(--text-muted); margin:0 0 1rem; padding:.8rem 1rem; } .section-heading { align-items:center; border-bottom:1px solid var(--border); padding-bottom:.7rem; } .section-heading h3 { margin:0; } .coverage-grid { display:grid; gap:.65rem; grid-template-columns:repeat(3,minmax(0,1fr)); margin-top:1rem; } .coverage-item { align-items:flex-start; border:1px solid var(--border); border-radius:.6rem; display:flex; gap:.55rem; min-height:4.3rem; padding:.7rem; } .coverage-icon { color:var(--accent); font-size:1.15rem; line-height:1.1; } .coverage-item strong { display:block; font-size:.78rem; } .coverage-item p { color:var(--text-muted); font-size:.7rem; margin:.35rem 0 0; } .phase-list { list-style:none; margin:0; padding:1rem 0 0; } .phase-row { border-left:2px solid var(--accent); display:grid; gap:.75rem; grid-template-columns:2rem 1fr; padding:0 0 1rem 1rem; position:relative; } .phase-row:last-child { padding-bottom:0; } .phase-marker { align-items:center; background:var(--accent); border:3px solid var(--surface); border-radius:50%; color:var(--on-accent); display:flex; font-size:.9rem; height:1.85rem; justify-content:center; left:-1rem; position:absolute; top:0; width:1.85rem; } .phase-title { align-items:center; display:flex; flex-wrap:wrap; gap:.5rem; justify-content:space-between; } .phase-count { background:var(--surface-subtle); color:var(--text-muted); font-weight:600; } .phase-description { color:var(--text-muted); font-size:.78rem; margin:.25rem 0 .55rem; } .phase-empty { border-left-color:var(--border); } .phase-empty .phase-marker { background:var(--surface-subtle); color:var(--text-muted); } .phase-unavailable { background:var(--surface-subtle); color:var(--text-muted); font-size:.78rem; margin:0; padding:.6rem .7rem; } .phase-items,.trace-tree { border-top:1px solid var(--border); list-style:none; margin:0; padding:0; } .phase-items li { align-items:center; border-bottom:1px solid var(--border); display:grid; gap:.6rem; grid-template-columns:auto minmax(8rem,1fr) 5rem auto auto; padding:.6rem .25rem; } .item-kind,.branch { color:var(--accent); } .item-label { display:grid; gap:.1rem; min-width:0; } .item-label time { color:var(--text-muted); font-size:.7rem; } .item-label strong,.span-name strong { overflow-wrap:anywhere; } .item-time { color:var(--text-muted); font-variant-numeric:tabular-nums; text-align:right; } .trace-tree { margin-top:1rem; overflow:hidden; } .span-row { align-items:center; border-bottom:1px solid var(--border); display:grid; gap:.75rem; grid-template-columns:minmax(12rem,1fr) minmax(8rem,2fr) 5rem 6rem; padding:.8rem .25rem calc(.8rem + (var(--depth) * 0rem)) clamp(.25rem,calc(.25rem + (var(--depth) * 1.15rem)),7rem); } .span-name { display:grid; gap:.15rem; min-width:0; } .waterfall { background:var(--surface-subtle); border-radius:999px; height:.55rem; overflow:hidden; } .waterfall span { background:var(--accent); border-radius:inherit; display:block; height:100%; min-width:.35rem; } .waterfall-legend { align-items:center; border-bottom:1px solid var(--border); color:var(--text-muted); display:flex; flex-wrap:wrap; font-size:.78rem; gap:.5rem 1.2rem; justify-content:space-between; margin-top:1rem; padding-bottom:.55rem; } .waterfall-legend i { background:var(--accent); display:inline-block; height:.55rem; margin-right:.35rem; width:1rem; } .continuation-range { color:var(--text-muted); font-size:.8rem; margin:.9rem 0 .75rem; } .continuation-range strong { color:var(--text); } .continuation-details { border-top:1px solid var(--border); padding-top:.75rem; } .continuation-details summary { color:var(--accent-hover); cursor:pointer; font-size:.8rem; font-weight:700; list-style:none; } .continuation-details summary::-webkit-details-marker { display:none; } .continuation-items { margin-top:.7rem; } .continuation-limit { color:var(--text-muted); font-size:.75rem; margin:.75rem 0 0; } .notice { background:var(--warning-surface); color:var(--warning-text); padding:.7rem; } .notice button,.trace-unavailable button { background:var(--accent); border:0; border-radius:.4rem; color:var(--on-accent); padding:.45rem .7rem; } .empty { color:var(--text-muted); padding:1rem 0; } .empty-state { align-items:flex-start; background:var(--surface); border:1px dashed var(--control-border); border-radius:.65rem; display:grid; gap:.8rem; justify-items:start; min-height:7rem; padding:2rem; } .trace-unavailable p { color:var(--text-muted); } .sr-only { height:1px; margin:-1px; overflow:hidden; position:absolute; width:1px; clip:rect(0,0,0,0); } .route-skeleton { display:grid; gap:.8rem; margin-top:1rem; } .route-skeleton span { background:var(--surface-subtle); border:1px solid var(--border); border-radius:.25rem; display:block; height:2.3rem; } .trace-skeleton span:first-child { height:4rem; }
  `, `
    .run-card,.coverage-card,.phases-card,.trace-card,.continuation-card { background:transparent; border:0; border-top:2px solid var(--control-border); border-radius:0; padding:1.1rem 0; } .coverage-card,.phases-card,.trace-card,.continuation-card { margin-top:1.5rem; } .trace-heading { align-items:flex-start; } .phase-list { padding-top:1.25rem; } .waterfall { background:repeating-linear-gradient(to right,var(--surface-subtle) 0,var(--surface-subtle) calc(25% - 1px),var(--border) 25%); border-radius:.2rem; } .waterfall span { background:var(--accent); border-radius:.1rem; } .continuation-details summary:focus-visible { outline:3px solid var(--focus); outline-offset:3px; } @media (max-width:700px) { .run-card,.trace-heading,.section-heading { align-items:flex-start; flex-direction:column; } .run-summary { align-items:flex-start; justify-content:flex-start; } .summary-value { text-align:left; } .coverage-grid { grid-template-columns:repeat(2,minmax(0,1fr)); } .phase-items li { grid-template-columns:auto minmax(0,1fr) auto; } .phase-items .outcome,.phase-items .partial { grid-column:2; justify-self:start; } .span-row { align-items:start; grid-template-columns:1fr auto; padding-left:clamp(.25rem,calc(.25rem + (var(--depth) * .55rem)),2.5rem); } .span-name { grid-column:1 / -1; } .waterfall { grid-column:1; } .span-row time,.span-row .outcome { align-self:center; justify-self:end; } }
    @media (max-width:420px) { .coverage-grid { grid-template-columns:1fr; } }
    @media (prefers-reduced-motion:reduce) { *,*::before,*::after { scroll-behavior:auto !important; transition-duration:.01ms !important; } }
    @media (forced-colors:active) { .phase-marker,.waterfall span { forced-color-adjust:none; } .phase-marker { background:Highlight; color:HighlightText; } .waterfall span { background:Highlight; } }
  `],
})
export class ObservatoryTraceComponent {
  readonly store = inject(ObservatoryStore);
  readonly phases = PHASES;
  readonly evidenceItems = [
    { key: 'request', icon: '↓', label: 'Request & lifecycle' },
    { key: 'context', icon: '⌘', label: 'Chunks & context' },
    { key: 'prompt', icon: '✦', label: 'Prompt text' },
    { key: 'generation', icon: '✹', label: 'Generated content' },
    { key: 'thinking', icon: '∴', label: 'Displayed thinking' },
    { key: 'tools', icon: '⚙', label: 'Tool payloads' },
  ] as const;
  readonly MAX_CONTINUATION_PREVIEW = MAX_CONTINUATION_PREVIEW;
  continuationOpen = false;
  @Output() readonly browseRuns = new EventEmitter<void>();
  @Input() set runId(value: string | null) { if (value) void this.store.openRun(value); }
  formatDuration(durationMs: number | null): string { return formatDurationMs(durationMs); }
  durationAriaLabel(durationMs: number | null): string { return durationAriaLabel(durationMs); }
  statusIcon(status: string): string { return status === 'succeeded' ? '✓' : status === 'failed' ? '!' : status === 'cancelled' ? '×' : status === 'running' ? '◌' : status === 'queued' ? '…' : '?'; }
  coverageItems(): ReadonlyArray<(typeof this.evidenceItems)[number] & { readonly state: 'available' | 'not-observed' | 'not-captured' }> {
    const lifecycleObserved = this.phaseItems('request').length > 0;
    return this.evidenceItems.map((item) => ({ ...item, state: item.key === 'request' ? lifecycleObserved ? 'available' : 'not-observed' : 'not-captured' }));
  }
  evidenceAvailableCount(): number { return this.coverageItems().filter((item) => item.state === 'available').length; }
  itemCount(): number { return this.phases.reduce((total, phase) => total + this.phaseItems(phase.key).length, 0); }
  continuationSummary(): ContinuationSummary {
    const detail = this.store.selectedRun().detail;
    if (!detail) return { count: 0, startedAt: null, endedAt: null, outcomes: [], partial: false };
    return summarizeContinuation(detail.observations.map((item) => ({ label: item.measurement.name, startedAt: item.observedAt, outcome: item.outcome })), this.terminalBoundary(detail), detail.partial || detail.nextCursor !== null);
  }
  continuationPreview(): ReadonlyArray<PhaseItem> {
    const detail = this.store.selectedRun().detail;
    if (!detail) return [];
    const boundary = this.terminalBoundary(detail);
    return detail.observations
      .map((item) => this.observationItem(item))
      .filter((item) => !isAtOrBeforeBoundary(item.startedAt, boundary))
      .sort((left, right) => Date.parse(left.startedAt) - Date.parse(right.startedAt) || left.id.localeCompare(right.id))
      .slice(0, MAX_CONTINUATION_PREVIEW);
  }
  formatContinuationRange(summary: ContinuationSummary): string {
    if (!summary.startedAt || !summary.endedAt) return 'Time range unavailable';
    const start = new Date(summary.startedAt);
    const end = new Date(summary.endedAt);
    const prefix = summary.partial ? 'Loaded metadata window: ' : 'Observed ';
    return start.getTime() === end.getTime() ? `${prefix}${start.toISOString()}` : `${prefix}${start.toISOString()} – ${end.toISOString()}`;
  }
  continuationCountLabel(summary: ContinuationSummary): string {
    const count = summary.count.toLocaleString('en-US');
    return summary.partial ? `${count}+ loaded records` : `${count} record${summary.count === 1 ? '' : 's'}`;
  }
  orderedPhases(): ReadonlyArray<PhaseDefinition> {
    const observed = new Set(this.phases.filter((phase) => this.phaseItems(phase.key).length > 0).map((phase) => phase.key));
    return orderPhases(this.phases, observed);
  }
  recordValue(item: PhaseItem): string {
    if (item.kind === 'measurement' && item.measurementValue !== null && item.measurementUnit === 'milliseconds') return formatDurationMs(item.measurementValue);
    if (item.kind === 'measurement' && item.measurementValue !== null) return `${item.measurementValue}${item.measurementUnit ? ` ${item.measurementUnit}` : ''}`;
    return formatDurationMs(item.durationMs);
  }
  recordValueAriaLabel(item: PhaseItem): string {
    if (item.kind === 'measurement' && item.measurementValue !== null) return `${item.measurementValue}${item.measurementUnit ? ` ${item.measurementUnit}` : ''}`;
    return durationAriaLabel(item.durationMs);
  }
  phaseOffset(startedAt: string, runStartedAt: string): string {
    const offsetMs = Date.parse(startedAt) - Date.parse(runStartedAt);
    if (!Number.isFinite(offsetMs)) return 'offset unavailable';
    const sign = offsetMs < 0 ? '−' : '+';
    return `${sign}${formatDurationMs(Math.abs(offsetMs))}`;
  }
  phaseItems(key: PhaseKey): ReadonlyArray<PhaseItem> {
    const detail = this.store.selectedRun().detail;
    const trace = this.store.selectedRun().trace;
    const observations = (detail?.observations ?? []).map((item) => this.observationItem(item));
    const spans = (trace?.spans ?? []).map((item) => this.spanItem(item));
    const boundary = detail ? this.terminalBoundary(detail) : null;
    return foregroundPhaseItemsFor(key, spans, observations, boundary);
  }
  private terminalBoundary(detail: RunDetail): string | null {
    const lifecycle = detail.observations.map((item) => ({ label: item.measurement.name, startedAt: item.observedAt, outcome: item.outcome }));
    return terminalObservationBoundary(lifecycle, TERMINAL_STATUSES.has(detail.status) ? detail.finishedAt : null);
  }
  private observationItem(item: Observation): PhaseItem { return { id: `measurement-${item.observationId}`, label: item.measurement.name, operation: item.operation, componentId: item.componentId, component: `${item.componentId} · ${item.serviceRole}`, outcome: item.outcome, startedAt: item.observedAt, durationMs: item.measurement.unit === 'milliseconds' ? item.measurement.value : null, measurementValue: item.measurement.value, measurementUnit: item.measurement.unit, partial: false, kind: 'measurement' }; }
  private spanItem(item: Span): PhaseItem { return { id: `span-${item.spanId}`, label: item.operation, operation: item.operation, componentId: item.componentId, component: item.componentId, outcome: item.outcome, startedAt: item.startedAt, durationMs: item.durationMs, measurementValue: null, measurementUnit: null, partial: item.partial, kind: 'span' }; }
  barWidth(duration: number): number { const spans = this.store.selectedRun().trace?.spans ?? []; const max = Math.max(...spans.map((span) => span.durationMs), 1); return Math.max(2, Math.round((duration / max) * 100)); }
  maxDuration(): number { return Math.max(...(this.store.selectedRun().trace?.spans ?? []).map((span) => span.durationMs), 0); }
  retry(): void { const runId = this.store.selectedRunId(); if (runId) void this.store.openRun(runId); }
}
