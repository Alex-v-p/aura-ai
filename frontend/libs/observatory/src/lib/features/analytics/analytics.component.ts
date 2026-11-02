import { ChangeDetectionStrategy, Component, effect, inject } from '@angular/core';
import { DatePipe, DecimalPipe } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { type AnalyticsAggregation, type AnalyticsBucket, type AnalyticsFocus, type AnalyticsGroupBy, type AnalyticsMetricDefinition, type AnalyticsMetricName, type AnalyticsSeries } from '@aura/observatory-api-client';
import { DEFAULT_ANALYTICS_FILTERS, datetimeLocalToUtc, type AnalyticsFilters } from '../../observatory-models';
import { ObservatoryStore } from '../../observatory-store';

type FocusOption = { readonly value: AnalyticsFocus; readonly label: string; readonly description: string };

const FOCUS_OPTIONS: ReadonlyArray<FocusOption> = [
  { value: 'all', label: 'All operations', description: 'Cross-component operational measurements' },
  { value: 'model', label: 'Model', description: 'Inference and routing timing; identity may be unavailable' },
  { value: 'queue', label: 'Queue', description: 'Wait time and bounded backlog measurements' },
  { value: 'retrieval', label: 'Retrieval', description: 'Document and memory retrieval timing and outcomes' },
  { value: 'tool', label: 'Tool', description: 'Tool selection, policy, and execution measurements' },
  { value: 'memory', label: 'Memory', description: 'Memory extraction and operation measurements' },
  { value: 'automation', label: 'Automation', description: 'Reminder, notification, and workflow operational measurements' },
];

const FOCUS_DEFAULTS: Readonly<Record<AnalyticsFocus, AnalyticsMetricName>> = {
  all: 'operation_duration_ms', model: 'inference_duration_ms', queue: 'queue_wait_ms', retrieval: 'document_retrieval_duration_ms',
  tool: 'tool_execution_duration_ms', memory: 'memory_operation_duration_ms', automation: 'reminder_scheduler_scan_duration_ms',
};
const MAX_ANALYTICS_POINTS = 200;

@Component({
  selector: 'observatory-analytics',
  standalone: true,
  imports: [DatePipe, DecimalPipe, FormsModule],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <section aria-labelledby="analytics-title">
        <div class="section-heading">
        <div><p class="eyebrow">Bounded operational analytics</p><h2 id="analytics-title">Metric explorer</h2><p class="muted">Metadata-only aggregates from a bounded UTC window. Private content is never displayed.</p></div>
        <button type="button" class="secondary-button" (click)="refresh()" [disabled]="store.analytics().state === 'loading'">Refresh metrics</button>
      </div>

      <nav class="focus-tabs" aria-label="Analytics focus">
        @for (option of focusOptionsForCatalogue(); track option.value) { <button type="button" [class.active]="filters.focus === option.value" [attr.aria-current]="filters.focus === option.value ? 'page' : null" (click)="setFocus(option.value)">{{ option.label }}</button> }
      </nav>
      <p class="focus-description" role="status">{{ focusDescription() }}</p>

      <form class="analytics-controls" (submit)="apply($event)">
        <div><label for="analytics-metric">Measurement</label><select id="analytics-metric" name="metric" [ngModel]="effectiveMetric()" (ngModelChange)="setMetric($event)"><option [value]="effectiveMetric()">{{ metricLabel(effectiveMetric()) }}</option>@for (metric of metricsForFocus(); track metric.metric) { @if (metric.metric !== effectiveMetric()) { <option [value]="metric.metric">{{ metric.metric }} ({{ metric.unit }})</option> } }</select></div>
        <div><label for="analytics-aggregation">Aggregation</label><select id="analytics-aggregation" name="aggregation" [value]="filters.aggregation" (change)="setAggregation($any($event.target).value)">@for (aggregation of aggregationOptions(); track aggregation) { <option [value]="aggregation">{{ aggregationLabel(aggregation) }}</option> }</select></div>
        <div><label for="analytics-group">Group series by</label><select id="analytics-group" name="group" [value]="filters.groupBy" (change)="setGroup($any($event.target).value)">@for (group of groupOptions(); track group) { <option [value]="group">{{ groupLabel(group) }}</option> }</select></div>
        <div><label for="analytics-bucket">Bucket</label><select id="analytics-bucket" name="bucket" [value]="filters.bucket" (change)="setBucket($any($event.target).value)"><option value="auto">Automatic (≤200 points)</option><option value="one_minute">1 minute</option><option value="five_minutes">5 minutes</option><option value="fifteen_minutes">15 minutes</option><option value="one_hour">1 hour</option><option value="six_hours">6 hours</option><option value="one_day">1 day</option></select></div>
        <div><label for="analytics-from">From (UTC)</label><input id="analytics-from" name="from" type="datetime-local" [value]="fromInput" [attr.aria-invalid]="rangeError ? 'true' : null" [attr.aria-describedby]="rangeError ? 'analytics-range-error' : null" (input)="fromInput = $any($event.target).value; rangeError = null"></div>
        <div><label for="analytics-to">To (UTC)</label><input id="analytics-to" name="to" type="datetime-local" [value]="toInput" [attr.aria-invalid]="rangeError ? 'true' : null" [attr.aria-describedby]="rangeError ? 'analytics-range-error' : null" (input)="toInput = $any($event.target).value; rangeError = null"></div>
        <button type="submit" class="primary-button">Apply bounded query</button>
      </form>
      @if (rangeError) { <p id="analytics-range-error" class="field-error" role="alert">{{ rangeError }}</p> }

      @if (store.analytics().state === 'loading') { <div class="loading-panel" role="status" aria-live="polite"><strong>Loading operational metrics</strong><span>Preparing a bounded aggregate snapshot…</span><div class="route-skeleton analytics-skeleton" aria-hidden="true"><span></span><span></span><span></span></div></div> }
      @if (store.analytics().state === 'error') { <div class="state failure" role="alert"><strong>{{ analyticsErrorTitle() }}</strong><p>{{ analyticsErrorMessage() }}</p><button type="button" (click)="refresh()">Retry metrics</button></div> }
      @if (store.analytics().state === 'stale') { <div class="state notice" role="status"><strong>Showing the last known metric snapshot.</strong><p>{{ store.analytics().error ?? 'The latest bounded query could not be completed.' }}</p><button type="button" (click)="refresh()">Retry metrics</button></div> }
      @if (store.analytics().state === 'disconnected') { <div class="state notice" role="status" aria-live="polite"><strong>Live analytics updates are disconnected.</strong><p>Showing the last known bounded snapshot until the stream reconnects.</p><button type="button" (click)="refresh()">Refresh metrics</button></div> }

      @if (store.analytics().snapshot; as snapshot) {
        <div class="query-summary" aria-label="Applied analytics query"><span>{{ snapshot.query.from | date:'medium':'UTC' }} UTC – {{ snapshot.query.to | date:'medium':'UTC' }}</span><span>{{ bucketLabel(snapshot.query.bucket) }} · {{ snapshot.query.groupBy === 'none' ? 'One series' : groupLabel(snapshot.query.groupBy) }}</span><span><strong>Unit:</strong> {{ unitLabel(snapshot.query.unit) }} · {{ aggregationLabel(snapshot.query.aggregation) }}</span><span>{{ snapshot.scan.scannedRows | number }} rows scanned · max {{ MAX_ANALYTICS_POINTS }} points/series</span></div>
        @if (snapshot.freshness.state !== 'fresh' || snapshot.partial || snapshot.unavailableSignals.length) { <div class="state notice" role="status"><strong>{{ freshnessLabel(snapshot) }}</strong><p>{{ freshnessExplanation(snapshot) }}</p>@if (snapshot.partialReasons.length) { <p>Partial reasons: {{ formatReasons(snapshot.partialReasons) }}.</p> } @if (snapshot.unavailableSignals.length) { <p>Unavailable signals: {{ formatReasons(snapshot.unavailableSignals) }}.</p> }</div> }
        @if (store.analytics().state === 'empty') { <p class="empty" role="status">No measurements match this bounded window and filter set. A zero is only shown when the service reports a measured zero.</p> }
        @if (snapshot.series.length) {
          <section class="series-section" aria-labelledby="series-title"><div class="section-heading"><div><h3 id="series-title">{{ metricLabel(snapshot.query.metric) }}</h3><p class="muted">Values are measured in {{ unitLabel(snapshot.query.unit) }}. Percentiles use {{ snapshot.percentileMethod }}.</p></div><span class="status-label" [class]="snapshot.freshness.state">{{ snapshot.freshness.state }}</span></div>
            <div class="series-grid">@for (series of snapshot.series; track series.seriesKey) {
              <article class="series-card" [attr.aria-labelledby]="'series-' + safeId(series.seriesKey)"><div class="card-heading"><div><h4 [id]="'series-' + safeId(series.seriesKey)">{{ seriesLabel(series) }}</h4>@if (facetLabel(series)) { <p class="facet-label">{{ facetLabel(series) }}</p> }<p class="muted">{{ series.summary.sampleCount | number }} samples · {{ availabilityLabel(series.availability) }}</p></div><span class="status-label" [class]="series.partial ? 'partial' : series.availability">{{ series.partial ? 'Partial' : availabilityLabel(series.availability) }}</span></div>
                <div class="chart" aria-hidden="true">@for (point of series.points.slice(0, MAX_ANALYTICS_POINTS); track point.startAt) { <span class="bar" [style.height.%]="barHeight(point.value, series)"></span> }</div><p class="chart-detail">Decorative trend preview in {{ unitLabel(snapshot.query.unit) }}. Use the adjacent keyboard-accessible table for exact UTC values and samples.</p><div class="chart-meta" aria-label="Chart scale"><span>Relative trend · {{ unitLabel(snapshot.query.unit) }}</span><span>Scale: 0–{{ number(series.summary.maximum) }}</span></div>
                <dl class="summary-grid"><div><dt>Minimum</dt><dd>{{ number(series.summary.minimum) }}</dd></div><div><dt>Average</dt><dd>{{ number(series.summary.average) }}</dd></div><div><dt>Maximum</dt><dd>{{ number(series.summary.maximum) }}</dd></div><div><dt>Samples</dt><dd>{{ series.summary.sampleCount | number }}</dd></div></dl>
                <div class="table-wrap" tabindex="0" role="region" [attr.aria-label]="'Scrollable data table for ' + seriesLabel(series)"><table><caption class="sr-only">{{ seriesLabel(series) }}{{ facetLabel(series) ? ' · ' + facetLabel(series) : '' }} data in {{ unitLabel(snapshot.query.unit) }}</caption><thead><tr><th scope="col">Start (UTC)</th><th scope="col">End (UTC)</th><th scope="col">Value ({{ unitLabel(snapshot.query.unit) }})</th><th scope="col">Samples</th><th scope="col">Availability</th></tr></thead><tbody>@for (point of series.points.slice(0, MAX_ANALYTICS_POINTS); track point.startAt) { <tr><th scope="row">{{ point.startAt | date:'short':'UTC' }} UTC</th><td>{{ point.endAt | date:'short':'UTC' }} UTC</td><td>{{ number(point.value) }}</td><td>{{ point.sampleCount | number }}</td><td>{{ point.partial ? 'Partial' : availabilityLabel(point.availability) }}</td></tr> } @empty { <tr><td colspan="5">No points in this series.</td></tr> }</tbody></table></div>
              </article>
            }</div>
          </section>
        }
        @if (snapshot.breakdowns.length) { <section aria-labelledby="breakdowns-title" class="breakdowns"><h3 id="breakdowns-title">Outcome and error breakdown</h3><p class="muted">Finite metadata groups only; private diagnostic details are not returned.</p><div class="table-wrap" tabindex="0" role="region" aria-label="Scrollable analytics breakdown table"><table><caption class="sr-only">Analytics breakdown groups</caption><thead><tr><th scope="col">Group</th><th scope="col">Value</th><th scope="col">Count</th><th scope="col">State</th></tr></thead><tbody>@for (item of snapshot.breakdowns; track item.groupBy + item.value) { <tr><th scope="row">{{ item.groupBy }}</th><td>{{ item.value }}</td><td>{{ item.count | number }}</td><td>{{ item.partial ? 'Partial' : 'Complete' }}</td></tr> }</tbody></table></div></section> }
      }
    </section>
  `,
  styles: [`
    :host { display:block; min-width:0; } .section-heading,.series-section,.breakdowns,.series-grid,.series-card,.analytics-controls { min-width:0; } .section-heading { align-items:flex-start; display:flex; flex-wrap:wrap; gap:1rem; justify-content:space-between; margin-bottom:1rem; } h2,h3,h4 { margin:0; overflow-wrap:anywhere; } h2 { font-size:1.25rem; } h3 { font-size:1.1rem; } h4 { font-size:1rem; } .eyebrow { color:var(--accent-hover); font-size:.72rem; font-weight:800; letter-spacing:.08em; margin:0 0 .35rem; text-transform:uppercase; } .muted,.facet-label { color:var(--text-muted); font-size:.84rem; margin:.35rem 0 0; overflow-wrap:anywhere; } .facet-label { font-size:.78rem; } button,input,select { border-radius:.45rem; min-height:2.4rem; max-width:100%; min-width:0; } button { cursor:pointer; font-weight:700; padding:.5rem .75rem; } button:focus-visible,input:focus-visible,select:focus-visible,.table-wrap:focus-visible { outline:3px solid var(--focus); outline-offset:3px; } .primary-button { background:var(--accent); border:1px solid var(--accent); color:var(--on-accent); } .secondary-button,.analytics-controls select,.analytics-controls input { background:var(--surface); border:1px solid var(--control-border); color:var(--text); padding:.45rem .55rem; } .focus-tabs { border-bottom:1px solid var(--border); display:flex; gap:.15rem; margin:1.2rem 0 .75rem; max-width:100%; overflow-x:auto; } .focus-tabs button { background:transparent; border:0; border-bottom:3px solid transparent; color:var(--text-muted); white-space:nowrap; } .focus-tabs button.active { border-bottom-color:var(--accent); color:var(--text); } .focus-description { color:var(--text-muted); margin:.35rem 0 1rem; overflow-wrap:anywhere; } .analytics-controls { align-items:end; background:var(--surface-subtle); border:1px solid var(--border); border-radius:.8rem; display:grid; gap:.75rem; grid-template-columns:repeat(4,minmax(0,1fr)); margin-bottom:1rem; padding:1rem; } .analytics-controls div,.card-heading > div { display:grid; gap:.3rem; min-width:0; } label { font-size:.78rem; font-weight:750; } .analytics-controls button { min-height:2.6rem; } .field-error { background:var(--danger-surface); border:1px solid var(--danger); border-radius:.5rem; color:var(--danger-text); margin:.5rem 0 1rem; padding:.65rem .8rem; } .loading-panel,.state,.query-summary { border-radius:.65rem; margin:1rem 0; padding:.8rem 1rem; } .loading-panel { background:var(--surface); border:1px solid var(--border); display:grid; gap:.25rem; } .state p { margin:.35rem 0 .65rem; overflow-wrap:anywhere; } .failure { background:var(--danger-surface); color:var(--danger-text); } .notice { background:var(--warning-surface); color:var(--warning-text); } .query-summary { align-items:center; background:var(--surface); display:flex; flex-wrap:wrap; gap:.55rem 1.2rem; } .query-summary span { font-size:.8rem; overflow-wrap:anywhere; } .series-section,.breakdowns { margin-top:1.5rem; } .status-label { background:var(--info-surface); border-radius:999px; color:var(--info-text); font-size:.75rem; font-weight:750; padding:.3rem .55rem; text-transform:capitalize; white-space:nowrap; } .status-label.fresh,.status-label.available { background:var(--success-surface); color:var(--success-text); } .status-label.partial,.status-label.stale,.status-label.no_data { background:var(--warning-surface); color:var(--warning-text); } .status-label.unavailable { background:var(--danger-surface); color:var(--danger-text); } .series-grid { display:grid; gap:1rem; grid-template-columns:repeat(auto-fit,minmax(min(25rem,100%),1fr)); } .series-card { background:var(--surface); border:1px solid var(--border); border-radius:.8rem; padding:1rem; } .card-heading { align-items:flex-start; display:flex; gap:.8rem; justify-content:space-between; } .chart { align-items:end; background:var(--surface-subtle); border-bottom:2px solid var(--control-border); display:flex; gap:2px; height:9rem; margin:1rem 0 .4rem; max-width:100%; min-width:0; overflow:hidden; padding:.5rem .4rem 0; } .bar { background:var(--accent); border:1px solid var(--accent); border-radius:.2rem .2rem 0 0; flex:1; min-height:2px; min-width:2px; padding:0; } .chart-detail { color:var(--text-muted); font-size:.78rem; margin:.25rem 0 1rem; min-height:1.2rem; overflow-wrap:anywhere; } .summary-grid { display:grid; gap:.6rem; grid-template-columns:repeat(4,minmax(0,1fr)); margin:0 0 1rem; min-width:0; } dt { color:var(--text-muted); font-size:.72rem; } dd { font-weight:750; margin:.15rem 0 0; overflow-wrap:anywhere; } .table-wrap { max-width:100%; min-width:0; overflow:auto; } table { border-collapse:collapse; min-width:34rem; width:100%; } th,td { border-bottom:1px solid var(--border); padding:.55rem .45rem; text-align:left; vertical-align:top; } th { font-size:.75rem; } td { font-size:.8rem; } .empty { color:var(--text-muted); padding:1rem 0; } .sr-only { clip:rect(0,0,0,0); height:1px; margin:-1px; overflow:hidden; position:absolute; white-space:nowrap; width:1px; } @media (max-width:800px) { .analytics-controls { grid-template-columns:repeat(2,minmax(0,1fr)); } } @media (max-width:520px) { .analytics-controls { grid-template-columns:1fr; } .summary-grid { grid-template-columns:repeat(2,minmax(0,1fr)); } .series-card { padding:.8rem; } } @media (prefers-reduced-motion:reduce) { *,*::before,*::after { scroll-behavior:auto !important; transition-duration:.01ms !important; } }
  `, `
    .analytics-controls { background:var(--surface-subtle); border:0; border-block:1px solid var(--border); border-radius:0; padding:1rem 0; }
    .query-summary { background:transparent; border-block:1px solid var(--border); border-radius:0; padding-inline:0; }
    .series-card { background:var(--surface); border:0; border-radius:0; border-top:2px solid var(--control-border); padding:1rem 0; }
    .chart { background:repeating-linear-gradient(to top,var(--surface-subtle) 0,var(--surface-subtle) calc(25% - 1px),var(--border) 25%); border-bottom:2px solid var(--control-border); }
    .chart-meta { align-items:center; border-bottom:1px solid var(--border); color:var(--text-muted); display:flex; flex-wrap:wrap; font-size:.75rem; gap:.5rem 1rem; justify-content:space-between; padding-bottom:.5rem; }
    .breakdowns { border-top:2px solid var(--control-border); padding-top:1rem; }
    @media (max-width:520px) { .series-card { padding-block:.85rem; } .chart-meta { align-items:flex-start; flex-direction:column; } }
    .focus-tabs { flex-wrap:nowrap; min-width:100%; overflow-x:auto; overscroll-behavior-inline:contain; scrollbar-width:thin; }
    .focus-tabs button { flex:0 0 auto; }
    .route-skeleton { display:grid; gap:.65rem; margin-top:1rem; }
    .route-skeleton span { background:var(--surface-subtle); border:1px solid var(--border); border-radius:.35rem; display:block; height:2.8rem; }
    .analytics-skeleton span:first-child { width:96%; }
    .analytics-skeleton span:nth-child(2) { width:74%; }
    .analytics-skeleton span:nth-child(3) { width:88%; }
  `, `
    .section-heading { margin-bottom:1.35rem; }
    .focus-description { max-width:var(--obs-prose-max); line-height:1.5; }
    .analytics-controls { background:var(--surface); border:1px solid var(--border); border-radius:.65rem; gap:1rem; max-width:var(--obs-control-max); padding:1.25rem; }
    .analytics-controls button { justify-self:start; min-width:10rem; }
    .query-summary { gap:.7rem 1.5rem; margin-block:1.5rem; padding-block:.9rem; }
    .state.notice,.state.failure { background:var(--surface-subtle); border:1px solid var(--border); border-left:3px solid var(--warning-text); border-radius:.45rem; color:var(--text); max-width:var(--obs-prose-max); padding:.8rem 1rem; }
    .state.failure { border-left-color:var(--danger-text); }
    .series-section,.breakdowns { margin-top:var(--obs-section-gap); }
    .series-grid { gap:1.5rem; }
    .series-card { padding-block:1.25rem; }
    .summary-grid { gap:1rem; }
    @media (max-width:800px) { .analytics-controls { padding:1rem; } }
    @media (max-width:520px) { .analytics-controls button { justify-self:stretch; } }
  `],
})
export class ObservatoryAnalyticsComponent {
  readonly store = inject(ObservatoryStore);
  readonly MAX_ANALYTICS_POINTS = MAX_ANALYTICS_POINTS;
  readonly focusOptions = FOCUS_OPTIONS;
  filters: AnalyticsFilters = { ...DEFAULT_ANALYTICS_FILTERS };
  fromInput = '';
  toInput = '';
  rangeError: string | null = null;

  constructor() {
    this.filters = this.store.analyticsFilters();
    effect(() => {
      if (!this.store.analytics().catalogue) return;
      const next = this.validatedFilters(this.filters);
      if (next.metric === this.filters.metric && next.unit === this.filters.unit && next.focus === this.filters.focus && next.aggregation === this.filters.aggregation && next.groupBy === this.filters.groupBy) return;
      this.filters = next;
      this.store.setAnalyticsFilters(next);
    });
  }
  metricsForFocus(): ReadonlyArray<AnalyticsMetricDefinition> { return this.store.analytics().catalogue?.items.filter((item) => item.focusAreas.includes(this.filters.focus) || (this.filters.focus === 'all' && item.focusAreas.includes('all'))) ?? []; }
  selectedMetric(): AnalyticsMetricDefinition | null { const metric = this.effectiveMetric(); return this.store.analytics().catalogue?.items.find((item) => item.metric === metric) ?? null; }
  effectiveMetric(): AnalyticsMetricName { return this.catalogueDefinition(this.filters)?.metric ?? this.filters.metric ?? DEFAULT_ANALYTICS_FILTERS.metric; }
  aggregationOptions(): ReadonlyArray<AnalyticsAggregation> { return this.selectedMetric()?.supportedAggregations ?? ['average']; }
  groupOptions(): ReadonlyArray<AnalyticsGroupBy> { return this.selectedMetric()?.supportedGroupings ?? ['none']; }
  focusOptionsForCatalogue(): ReadonlyArray<FocusOption> { const supported = new Set(this.store.analytics().catalogue?.items.flatMap((item) => item.focusAreas) ?? ['all']); return this.focusOptions.filter((option) => supported.has(option.value)); }
  focusDescription(): string { return this.focusOptions.find((option) => option.value === this.filters.focus)?.description ?? ''; }
  setFocus(focus: AnalyticsFocus): void {
    const definition = this.store.analytics().catalogue?.items.find((item) => item.metric === FOCUS_DEFAULTS[focus] && item.focusAreas.includes(focus)) ?? this.store.analytics().catalogue?.items.find((item) => item.focusAreas.includes(focus));
    const metric = definition?.metric ?? this.filters.metric;
    this.filters = this.validatedFilters({ ...this.filters, focus, metric, ...(definition ? { unit: definition.unit } : {}) }, definition);
    this.store.setAnalyticsFilters(this.filters);
  }
  setMetric(metric: AnalyticsMetricName): void { const definition = this.store.analytics().catalogue?.items.find((item) => item.metric === metric); this.filters = this.validatedFilters({ ...this.filters, metric, ...(definition ? { unit: definition.unit } : {}) }, definition); }
  setAggregation(aggregation: AnalyticsAggregation): void { this.filters = this.validatedFilters({ ...this.filters, aggregation }); }
  setGroup(groupBy: AnalyticsGroupBy): void { this.filters = this.validatedFilters({ ...this.filters, groupBy }); }
  setBucket(bucket: AnalyticsBucket): void { this.filters = { ...this.filters, bucket }; }
  apply(event: Event): void {
    event.preventDefault();
    this.rangeError = null;
    const from = datetimeLocalToUtc(this.fromInput);
    const to = datetimeLocalToUtc(this.toInput);
    if ((this.fromInput && !from) || (this.toInput && !to)) { this.rangeError = 'Enter valid From and To UTC times.'; return; }
    if (from && to) {
      const duration = Date.parse(to) - Date.parse(from);
      if (duration <= 0) { this.rangeError = 'From (UTC) must be earlier than To (UTC).'; return; }
      if (duration > 31 * 24 * 60 * 60 * 1000) { this.rangeError = 'The UTC window cannot be longer than 31 days.'; return; }
    }
    this.filters = this.validatedFilters({ ...this.filters, from: from ?? undefined, to: to ?? undefined });
    this.store.setAnalyticsFilters(this.filters);
  }
  refresh(): void { this.store.refreshAnalytics(); }
  metricLabel(metric: AnalyticsMetricName): string { return metric.replaceAll('_', ' '); }
  unitLabel(unit: string): string { return ({ milliseconds: 'milliseconds (ms)', bytes: 'bytes', count: 'count', ratio: 'ratio', score: 'score' })[unit] ?? unit; }
  aggregationLabel(aggregation: AnalyticsAggregation): string { return aggregation === 'average' ? 'Average' : aggregation === 'rate' ? 'Rate' : aggregation.toUpperCase(); }
  groupLabel(group: AnalyticsGroupBy): string { return group === 'none' ? 'One series' : group.replaceAll('_', ' '); }
  bucketLabel(bucket: AnalyticsBucket): string { const labels: Record<AnalyticsBucket, string> = { auto: 'Automatic buckets', one_minute: '1 minute', five_minutes: '5 minutes', fifteen_minutes: '15 minutes', one_hour: '1 hour', six_hours: '6 hours', one_day: '1 day' }; return labels[bucket] ?? this.humanize(bucket); }
  formatReasons(values: ReadonlyArray<string>): string { return values.map((value) => this.humanize(value)).join(', '); }
  private humanize(value: string): string { return value.replaceAll('_', ' ').replace(/(^|\s)\S/g, (character) => character.toUpperCase()); }
  seriesLabel(series: Pick<AnalyticsSeries, 'seriesKey' | 'facets'>): string { return series.seriesKey || 'All measurements'; }
  facetLabel(series: Pick<AnalyticsSeries, 'facets'>): string { return Object.values(series.facets).filter(Boolean).join(' · '); }
  safeId(value: string): string { let hash = 2166136261; for (const character of value) { hash ^= character.codePointAt(0) ?? 0; hash = Math.imul(hash, 16777619); } return `${value.replace(/[^a-zA-Z0-9_-]/g, '-').slice(0, 96)}-${(hash >>> 0).toString(16)}`; }
  number(value: number | null): string { return value === null || !Number.isFinite(value) ? 'Unavailable' : value.toLocaleString(undefined, { maximumFractionDigits: 2 }); }
  availabilityLabel(value: string): string { return value === 'no_data' ? 'No data' : value === 'unavailable' ? 'Unavailable' : 'Available'; }
  freshnessLabel(snapshot: { readonly freshness: { readonly state: string } }): string { return snapshot.freshness.state === 'stale' ? 'Metric data is stale' : snapshot.freshness.state === 'unavailable' ? 'Freshness is unavailable' : 'Metric data is partial'; }
  freshnessExplanation(snapshot: { readonly freshness: { readonly lagMs: number | null; readonly latestObservedAt: string | null } }): string { return snapshot.freshness.latestObservedAt ? `Latest observation ${new Date(snapshot.freshness.latestObservedAt).toLocaleString('en-GB', { timeZone: 'UTC' })} UTC · lag ${this.humanDuration(snapshot.freshness.lagMs)}.` : 'No observation freshness signal was reported.'; }
  humanDuration(milliseconds: number | null): string { if (milliseconds === null || !Number.isFinite(milliseconds)) return 'unavailable'; if (milliseconds < 1000) return `${Math.round(milliseconds)} ms`; const seconds = milliseconds / 1000; if (seconds < 60) return `${seconds.toFixed(1)} s`; const minutes = seconds / 60; if (minutes < 60) return `${minutes.toFixed(1)} min`; const hours = minutes / 60; return hours < 24 ? `${hours.toFixed(1)} hr` : `${(hours / 24).toFixed(1)} days`; }
  analyticsErrorTitle(): string { return this.analyticsFailureKind() === 'invalid_query' ? 'Analytics query rejected or malformed.' : 'Operational analytics are unavailable.'; }
  analyticsErrorMessage(): string { const kind = this.analyticsFailureKind(); return kind === 'invalid_query' ? 'The selected measurement or bounded query was rejected or malformed; this combination is unavailable. Adjust the query and retry.' : kind === 'service_unavailable' ? 'The bounded analytics service is not responding. Retry when Observatory recovers.' : 'The service did not return a usable aggregate snapshot. Retry metrics.'; }
  private analyticsFailureKind(): 'invalid_query' | 'service_unavailable' | 'other' { const message = this.store.analytics().error ?? ''; if (/\(400\)|\(409\)|\(422\)|malformed|unsupported|invalid|rejected/i.test(message)) return 'invalid_query'; if (/\(502\)|\(503\)|\(504\)|unavailable|timeout|network|disconnected/i.test(message)) return 'service_unavailable'; return 'other'; }
  private validatedFilters(filters: AnalyticsFilters, definition = this.catalogueDefinition(filters)): AnalyticsFilters {
    const aggregation = definition?.supportedAggregations.includes(filters.aggregation) ? filters.aggregation : definition?.supportedAggregations[0] ?? 'average';
    const groupBy = definition?.supportedGroupings.includes(filters.groupBy) ? filters.groupBy : definition?.supportedGroupings[0] ?? 'none';
    const focus = definition?.focusAreas.includes(filters.focus) || (filters.focus === 'all' && definition?.focusAreas.includes('all')) ? filters.focus : definition?.focusAreas[0] ?? 'all';
    return { ...filters, metric: definition?.metric ?? filters.metric, unit: definition?.unit ?? filters.unit, aggregation, groupBy, focus };
  }
  private catalogueDefinition(filters: AnalyticsFilters): AnalyticsMetricDefinition | null {
    const catalogue = this.store.analytics().catalogue?.items ?? [];
    return catalogue.find((item) => item.metric === filters.metric && (filters.focus === 'all' || item.focusAreas.includes(filters.focus)))
      ?? catalogue.find((item) => item.metric === FOCUS_DEFAULTS[filters.focus] && item.focusAreas.includes(filters.focus))
      ?? catalogue.find((item) => item.focusAreas.includes(filters.focus) || (filters.focus === 'all' && item.focusAreas.includes('all')))
      ?? null;
  }
  barHeight(value: number | null, series: { readonly points: ReadonlyArray<{ readonly value: number | null }> }): number { const values = series.points.map((point) => point.value ?? 0); const max = Math.max(...values, 1); return value === null ? 3 : Math.max(3, Math.round((value / max) * 100)); }
}
