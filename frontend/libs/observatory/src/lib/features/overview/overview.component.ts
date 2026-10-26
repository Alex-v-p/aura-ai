import { ChangeDetectionStrategy, Component, inject } from '@angular/core';
import { DatePipe, DecimalPipe } from '@angular/common';
import { StatePanelComponent } from '@aura/shared/ui';
import { ObservatoryStore } from '../../observatory-store';
import { ownerLoginHref } from '../../observatory-models';
import { ObservatoryIconComponent, type ObservatoryIconName } from '../../presentation/observatory-icon.component';

type HealthState = 'healthy' | 'delayed' | 'partial' | 'degraded' | 'unavailable';

@Component({
  selector: 'observatory-overview',
  standalone: true,
  imports: [StatePanelComponent, DatePipe, DecimalPipe, ObservatoryIconComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    @if (store.snapshot().state === 'loading') {
      <div class="loading-route-panel" role="status" aria-live="polite"><p class="eyebrow">Aura Observatory</p><h2>Loading Observatory</h2><p class="muted">Reading the latest metadata snapshot…</p><div class="route-skeleton overview-skeleton" aria-hidden="true"><span></span><span></span><span></span></div></div>
    }
    @if (store.snapshot().state === 'error') {
      <aura-state-panel title="Observatory unavailable" [message]="store.snapshot().error ?? 'Try again shortly.'" icon="!" tone="danger" mode="alert">
        <button type="button" (click)="store.load()">Retry</button>
        @if (store.sessionRequired()) { <a [href]="ownerLoginHref()">Sign in as owner</a> }
      </aura-state-panel>
    }
    @if (store.snapshot().state !== 'loading' && store.snapshot().state !== 'error') {
      <section class="overview-grid" aria-label="System and ingest health">
        <article class="hero-card" aria-labelledby="system-health-title">
          <div class="card-heading"><h2 id="system-health-title">System health</h2><span class="state-badge" [class]="'state-' + stateValue()" role="status"><observatory-icon [name]="stateIcon(stateValue())" /> {{ stateLabel() }}</span></div>
          <p class="health-summary">{{ stateMessage() }}</p>
          <p class="muted">Snapshot observed {{ store.snapshot().lastUpdatedAt | date:'medium' }}. State is based on bounded Observatory metadata.</p>
        </article>
        <article class="metric-card" aria-labelledby="observations-title"><h2 id="observations-title">Observations <span class="metric-context">lifetime</span></h2><strong>{{ store.snapshot().overview?.observationCount ?? 0 | number }}</strong><small>Cumulative retained metadata records</small></article>
        <article class="metric-card" aria-labelledby="runs-title"><h2 id="runs-title">Runs <span class="metric-context">lifetime</span></h2><strong>{{ store.snapshot().overview?.runCount ?? 0 | number }}</strong><small>Cumulative derived run projections</small></article>
        <article class="ingest-card" aria-labelledby="ingest-title">
          <div class="card-heading"><h2 id="ingest-title">Ingest health</h2><span class="state-badge" [class]="'state-' + ingestState()"><observatory-icon [name]="stateIcon(ingestState())" /> {{ stateLabel(ingestState()) }}</span></div>
          <p class="metric-note">These are cumulative ingest counters; the health verdict above is based on current freshness, backlog, and worker signals.</p><dl class="counts"><div><dt>Accepted <span class="metric-context">lifetime</span></dt><dd>{{ store.snapshot().overview?.ingest?.counts?.accepted ?? 0 | number }}</dd></div><div><dt>Rejected <span class="metric-context">lifetime</span></dt><dd>{{ store.snapshot().overview?.ingest?.counts?.rejected ?? 0 | number }}</dd></div><div><dt>Duplicates <span class="metric-context">lifetime</span></dt><dd>{{ store.snapshot().overview?.ingest?.counts?.duplicate ?? 0 | number }}</dd></div><div><dt>Malformed <span class="metric-context">lifetime</span></dt><dd>{{ store.snapshot().overview?.ingest?.counts?.malformed ?? 0 | number }}</dd></div><div><dt>Storage failures <span class="metric-context">lifetime</span></dt><dd>{{ store.snapshot().overview?.ingest?.counts?.storageFailure ?? 0 | number }}</dd></div></dl>
        </article>
      </section>

      @if (historyTrend().length) { <section class="trend-section" aria-labelledby="trend-title"><div class="section-heading"><div><p class="eyebrow">Current signal</p><h2 id="trend-title">Backlog trend</h2><p class="muted">Recent health snapshots, newest first. This compact view is separate from cumulative counters.</p></div><span class="trend-summary">Latest {{ historyTrend()[0].backlogCount | number }} pending</span></div><div class="trend-strip" role="img" [attr.aria-label]="trendAriaLabel()">@for (item of historyTrend(); track item.snapshotId) { <span [style.height.%]="trendHeight(item.backlogCount)" [attr.title]="(item.observedAt | date:'short') + ': ' + item.backlogCount + ' pending'" aria-hidden="true"></span> }</div><p class="trend-legend">Bars show pending receipts; exact values remain available to assistive technology below.</p><ul class="sr-only">@for (item of historyTrend(); track item.snapshotId) { <li>{{ item.observedAt | date:'medium' }}: {{ item.backlogCount | number }} pending receipts</li> }</ul></section> }

      @if (currentHealth(); as health) {
        <section class="health-section" aria-labelledby="self-monitoring-title">
          <div class="section-heading"><div><p class="eyebrow">Self-monitoring</p><h2 id="self-monitoring-title">Ingest quality and worker health</h2></div><p class="muted">Observed {{ health.observedAt | date:'medium' }}</p></div>
          <div class="health-grid">
            <article class="health-card" aria-labelledby="freshness-title"><h3 id="freshness-title">Freshness</h3><dl><div><dt>Intake lag</dt><dd>{{ duration(health.intakeLagMs) }}</dd></div><div><dt>Query freshness lag</dt><dd>{{ duration(health.queryFreshnessLagMs) }}</dd></div><div><dt>Visibility watermark</dt><dd>{{ health.visibilityWatermark | number }} records</dd></div><div><dt>Visibility lag</dt><dd>{{ health.visibilityLagCount | number }} records</dd></div></dl></article>
            <article class="health-card" aria-labelledby="backlog-title"><h3 id="backlog-title">Backlog</h3><dl><div><dt>Pending receipts</dt><dd>{{ health.backlogCount | number }}</dd></div><div><dt>Oldest pending age</dt><dd>{{ duration(health.oldestBacklogAgeMs) }}</dd></div><div><dt>Worker heartbeat age</dt><dd>{{ duration(health.workerHeartbeatAgeMs) }}</dd></div><div><dt>Last heartbeat</dt><dd>{{ health.workerHeartbeatAt ? (health.workerHeartbeatAt | date:'medium') : 'Not reported' }}</dd></div></dl></article>
            <article class="health-card" aria-labelledby="latency-title"><h3 id="latency-title">Storage and query latency</h3><dl><div><dt>Storage latest / P50 / P95</dt><dd>{{ latency(health.storageLatency) }}</dd></div><div><dt>Query latest / P50 / P95</dt><dd>{{ latency(health.queryLatency) }}</dd></div><div><dt>Storage samples</dt><dd>{{ health.storageLatency.sampleCount | number }}</dd></div><div><dt>Query samples</dt><dd>{{ health.queryLatency.sampleCount | number }}</dd></div></dl></article>
            <article class="health-card rejection-card" aria-labelledby="rejections-title"><h3 id="rejections-title">Rejection breakdown</h3><p class="muted">Finite reason codes only; payloads and parser details are never shown.</p><table><caption class="sr-only">Metadata rejection counts by finite reason code</caption><thead><tr><th scope="col">Reason</th><th scope="col">Count</th></tr></thead><tbody>@if (health.rejectionReasons.length) { @for (item of health.rejectionReasons; track item.reason) { <tr><th scope="row">{{ reasonLabel(item.reason) }}</th><td>{{ item.count | number }}</td></tr> } } @else { <tr><td colspan="2">No rejected metadata in this snapshot.</td></tr> }</tbody></table></article>
          </div>
        </section>
      } @else {
        <aura-state-panel title="Self-monitoring is unavailable" message="The current overview does not include a health snapshot yet. Retry to request the latest bounded health data." icon="!" tone="warning" mode="status"><button type="button" (click)="store.load()">Retry health snapshot</button></aura-state-panel>
      }

      <section class="history-section" aria-labelledby="history-title">
        <div class="section-heading"><div><p class="eyebrow">Bounded history</p><h2 id="history-title">Recent health snapshots</h2></div><button type="button" class="secondary-button" (click)="store.refreshHealthHistory()" [disabled]="store.healthHistory().state === 'loading'">Refresh history</button></div>
        @if (store.healthHistory().state === 'loading') { <p role="status" class="muted">Loading recent health snapshots…</p> }
        @if (store.healthHistory().state === 'error') { <p role="alert" class="notice">Health history is unavailable. <button type="button" (click)="store.refreshHealthHistory()">Retry history</button></p> }
        @if (store.healthHistory().state === 'stale') { <p role="status" class="notice">Showing the last known health history while the service catches up. <button type="button" (click)="store.refreshHealthHistory()">Retry</button></p> }
        @if (store.healthHistory().state === 'empty') { <p role="status" class="empty">No health snapshots have been recorded yet.</p> }
        @if (store.healthHistory().page; as page) {
          @if (page.items.length) { <div class="table-wrap" tabindex="0" role="region" aria-label="Scrollable recent health snapshots table"><table><caption class="sr-only">Recent Observatory health snapshots</caption><thead><tr><th scope="col">Observed</th><th scope="col">State</th><th scope="col">Intake lag</th><th scope="col">Backlog</th><th scope="col">Heartbeat age</th><th scope="col">Visibility lag</th></tr></thead><tbody>@for (item of page.items; track item.snapshotId) { <tr><th scope="row">{{ item.observedAt | date:'short' }}</th><td><span class="state-text"><observatory-icon [name]="stateIcon(item.state)" /> {{ stateLabel(item.state) }}</span></td><td>{{ duration(item.intakeLagMs) }}</td><td>{{ item.backlogCount | number }}</td><td>{{ duration(item.workerHeartbeatAgeMs) }}</td><td>{{ item.visibilityLagCount | number }}</td></tr> }</tbody></table></div> }
        }
        @if (store.healthHistory().page; as page) { <p class="muted history-footnote">Showing up to {{ page.items.length | number }} snapshots. Updated {{ page.snapshotAt | date:'medium' }}. History is operational metadata only.</p> }
      </section>

      @if (store.snapshot().state === 'stale' || store.snapshot().state === 'disconnected') { <p class="notice" role="status">{{ store.snapshot().error ?? 'Showing the last known snapshot.' }} @if (store.snapshot().state === 'disconnected') { <button type="button" (click)="store.reconnectLive()">Reconnect live updates</button> }</p> }
      @if (store.snapshot().partial) { <p class="notice" role="status">Partial data: {{ store.snapshot().resourceErrors.join(', ') }} could not be refreshed.</p> }
    }
  `,
  styles: [`
    :host { display:block; min-width:0; } .overview-grid { display:grid; gap:1rem; grid-template-columns:repeat(3,minmax(0,1fr)); } article { background:var(--surface); border:1px solid var(--border); border-radius:1rem; padding:1.25rem; min-width:0; } .hero-card,.ingest-card { grid-column:span 3; } .metric-card { display:grid; gap:.3rem; } .metric-card small,.eyebrow,dt,th,small,.muted { color:var(--text-muted); } .metric-card strong { font-size:2rem; } .eyebrow { font-size:.75rem; font-weight:750; letter-spacing:.08em; margin:0 0 .45rem; text-transform:uppercase; } .card-heading,.section-heading { align-items:flex-start; display:flex; gap:1rem; justify-content:space-between; } .section-heading { margin:1.7rem 0 .85rem; } h2,h3 { margin:0; } h2 { font-size:1.1rem; } h3 { font-size:1rem; } .health-summary { font-size:1.15rem; margin:.85rem 0 .4rem; } .muted { margin:.5rem 0 0; } .state-badge { align-items:center; border:1px solid currentColor; border-radius:999px; display:inline-flex; font-size:.78rem; font-weight:750; gap:.3rem; padding:.3rem .6rem; text-transform:capitalize; white-space:nowrap; } .state-healthy { color:var(--success-text); background:var(--success-surface); } .state-delayed,.state-partial { color:var(--warning-text); background:var(--warning-surface); } .state-degraded,.state-unavailable { color:var(--danger-text); background:var(--danger-surface); } .counts,.health-card dl { display:grid; gap:.8rem; grid-template-columns:repeat(5,1fr); margin:1.1rem 0 0; } .counts div,.health-card dl div { border-left:3px solid var(--accent-soft); padding-left:.65rem; } dt { font-size:.78rem; } dd { font-size:1.1rem; font-weight:700; margin:.2rem 0 0; } .health-section,.history-section { border-top:1px solid var(--border); margin-top:1.6rem; padding-top:.1rem; min-width:0; } .health-grid { display:grid; gap:1rem; grid-template-columns:repeat(2,minmax(0,1fr)); } .health-card dl { grid-template-columns:repeat(2,minmax(0,1fr)); margin:.9rem 0 0; } .rejection-card p { margin:.5rem 0 .8rem; } .rejection-card table { min-width:0; } .table-wrap { min-width:0; overflow-x:auto; } table { border-collapse:collapse; min-width:40rem; width:100%; } th,td { border-bottom:1px solid var(--border); padding:.65rem .55rem; text-align:left; } thead th { font-size:.78rem; } tbody th { font-weight:500; } td { color:var(--text); } .state-text { font-weight:650; white-space:nowrap; } .notice { background:var(--warning-surface); border:1px solid var(--warning-text); border-radius:.75rem; color:var(--warning-text); margin:1rem 0 0; padding:.75rem 1rem; } .empty { background:var(--surface-subtle); border-radius:.75rem; padding:.8rem 1rem; } button { background:var(--accent); border:0; border-radius:.45rem; color:var(--on-accent); cursor:pointer; padding:.55rem .8rem; } button:disabled { cursor:not-allowed; opacity:.6; } .secondary-button { background:transparent; border:1px solid var(--control-border); color:var(--text); } .sr-only { height:1px; margin:-1px; overflow:hidden; padding:0; position:absolute; width:1px; clip:rect(0,0,0,0); white-space:nowrap; } @media (max-width:700px) { .overview-grid,.health-grid { grid-template-columns:1fr; } .hero-card,.ingest-card { grid-column:auto; } .counts { grid-template-columns:repeat(2,1fr); } .card-heading,.section-heading { align-items:flex-start; flex-direction:column; } .health-card dl { grid-template-columns:1fr; } } @media (prefers-reduced-motion:reduce) { * { scroll-behavior:auto !important; transition-duration:.01ms !important; } }
  `, `
    /* Slice-2 workbench hierarchy: one visual system, quiet secondary surfaces. */
    .hero-card,.ingest-card { background:var(--surface-raised); border:0; border-radius:0; border-top:3px solid var(--accent); padding:1.35rem 0; }
    .metric-card { background:var(--surface-subtle); border:0; border-radius:.35rem; border-top:2px solid var(--control-border); padding:1rem; }
    .counts div,.health-card dl div { border-left:0; border-top:1px solid var(--border); padding:.6rem 0 0; }
    .health-card { background:transparent; border:0; border-radius:0; border-top:2px solid var(--control-border); padding:1rem 0; }
    .rejection-card { border-top-color:var(--warning-text); }
    .health-grid { gap:1.4rem 2rem; }
    .history-section { padding-top:1.1rem; }
    @media (max-width:700px) { .hero-card,.ingest-card { padding-block:1rem; } .health-grid { gap:1rem; } }
    .route-skeleton { display:grid; gap:.7rem; }
    .route-skeleton span { background:var(--surface-subtle); border:1px solid var(--border); border-radius:.35rem; display:block; height:1.1rem; }
    .loading-route-panel { background:var(--surface); border-top:2px solid var(--control-border); padding:1.25rem 0; }
    .loading-route-panel h2 { margin:0; }
    .overview-skeleton { grid-template-columns:repeat(3,minmax(0,1fr)); margin-top:1rem; }
    .overview-skeleton span:first-child { grid-column:span 2; height:4rem; }
    @media (max-width:700px) { .overview-skeleton { grid-template-columns:1fr 1fr; } }
  `, `
    .overview-grid { gap:1.25rem; }
    .hero-card { padding-block:1.5rem; }
    .metric-card { min-height:8.5rem; }
    .health-section,.history-section { margin-top:var(--obs-section-gap); }
    .section-heading { margin-bottom:1rem; }
    .health-card { padding-block:1.25rem; }
    .history-section .table-wrap { background:var(--surface); border:1px solid var(--border); border-radius:.6rem; overflow:auto; }
    .notice { border-left:3px solid var(--warning-text); border-radius:.45rem; }
    @media (min-width:1100px) { .overview-grid { grid-template-columns:repeat(2,minmax(0,1fr)); } .hero-card,.ingest-card { grid-column:1 / -1; } }
    @media (max-width:700px) { .overview-grid { gap:1rem; } .hero-card { padding-block:1.25rem; } }
  `, `
    :host { --overview-inset:clamp(1rem,2.2vw,1.5rem); }
    .hero-card,.ingest-card { padding:clamp(1.25rem,2vw,1.5rem) var(--overview-inset); }
    .metric-card { padding-inline:var(--overview-inset); }
    .loading-route-panel { padding-inline:var(--overview-inset); }
    .health-section,.history-section { padding-inline:0; }
    .health-section > .section-heading { padding-inline:var(--overview-inset); }
    .history-section > .section-heading,
    .history-section > .table-wrap,
    .history-section > p { margin-inline:var(--overview-inset); }
    .counts { column-gap:clamp(.8rem,1.8vw,1.5rem); }
    .health-card { padding-inline:var(--overview-inset); }
    @media (max-width:700px) {
      .hero-card,.ingest-card { padding-block:1.25rem; }
    }
  `, `
    .metric-card h2 { align-items:baseline; display:flex; gap:.35rem; }
    .metric-context { color:var(--text-muted); font-size:.68em; font-weight:600; letter-spacing:.02em; text-transform:uppercase; }
    .metric-note { color:var(--text-muted); font-size:.78rem; margin:.85rem 0 -.45rem; }
    .trend-section { border-top:1px solid var(--border); margin-top:var(--obs-section-gap); padding-top:1rem; }
    .trend-summary { background:var(--success-surface); border-radius:999px; color:var(--success-text); font-size:.78rem; font-weight:750; padding:.35rem .6rem; }
    .trend-strip { align-items:end; background:var(--surface-subtle); border-bottom:1px solid var(--border); display:flex; gap:.35rem; height:5rem; padding:.5rem .6rem 0; }
    .trend-strip span { background:var(--accent); border-radius:.2rem .2rem 0 0; flex:1; min-height:.25rem; }
    .trend-legend { color:var(--text-muted); font-size:.75rem; margin:.5rem 0 0; }
    @media (max-width:700px) { .trend-section { margin-top:1.25rem; } .trend-strip { gap:.2rem; } }
  `],
})
export class ObservatoryOverviewComponent {
  readonly store = inject(ObservatoryStore);
  readonly ownerLoginHref = ownerLoginHref;

  currentHealth() { return this.store.snapshot().overview?.health ?? null; }
  stateValue(): HealthState { return this.currentHealth()?.state ?? this.store.snapshot().overview?.systemState ?? 'unavailable'; }
  ingestState(): HealthState { return this.store.snapshot().overview?.ingest?.state ?? 'unavailable'; }
  stateLabel(state: HealthState = this.stateValue()): string { return state.replace('_', ' '); }
  stateIcon(state: HealthState = this.stateValue()): ObservatoryIconName { return state === 'healthy' ? 'success' : state === 'delayed' || state === 'partial' ? 'warning' : state === 'degraded' ? 'danger' : 'overview'; }
  stateMessage(): string { return ({ healthy: 'Telemetry is arriving and the worker is reporting normally.', delayed: 'Telemetry is arriving, but freshness or backlog thresholds need attention.', partial: 'Some health measurements are incomplete; inspect the explicit metrics below.', degraded: 'One or more health measurements are outside the expected operating range.', unavailable: 'No current health snapshot is available.' })[this.stateValue()]; }
  duration(milliseconds: number | null): string { if (milliseconds === null || !Number.isFinite(milliseconds)) return 'Not reported'; if (milliseconds < 1000) return `${Math.round(milliseconds)} ms`; const seconds = milliseconds / 1000; return seconds < 60 ? `${seconds.toFixed(1)} s` : `${(seconds / 60).toFixed(1)} min`; }
  latency(summary: { latestMs: number | null; p50Ms: number | null; p95Ms: number | null }): string { return `${this.duration(summary.latestMs)} / ${this.duration(summary.p50Ms)} / ${this.duration(summary.p95Ms)}`; }
  reasonLabel(reason: string): string { return reason.replaceAll('_', ' '); }
  historyTrend() { return (this.store.healthHistory().page?.items ?? []).slice(0, 12); }
  trendHeight(value: number): number { const max = Math.max(...this.historyTrend().map((item) => item.backlogCount), 1); return Math.max(8, Math.round((value / max) * 100)); }
  trendAriaLabel(): string { const items = this.historyTrend(); return `Backlog trend across ${items.length} health snapshots; latest ${items[0]?.backlogCount ?? 0} pending receipts.`; }
}
