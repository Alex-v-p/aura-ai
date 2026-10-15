import { ChangeDetectionStrategy, Component, inject } from '@angular/core';
import { DatePipe, DecimalPipe } from '@angular/common';
import { StatePanelComponent } from '@aura/shared/ui';
import { ObservatoryStore } from '../../observatory-store';
import { ownerLoginHref } from '../../observatory-models';

@Component({
  selector: 'observatory-overview',
  standalone: true,
  imports: [StatePanelComponent, DatePipe, DecimalPipe],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    @if (store.snapshot().state === 'loading') { <aura-state-panel title="Loading Observatory" message="Reading the latest metadata snapshot…" icon="…" mode="status" /> }
    @if (store.snapshot().state === 'error') { <aura-state-panel title="Observatory unavailable" [message]="store.snapshot().error ?? 'Try again shortly.'" icon="!" tone="danger" mode="alert"><button type="button" (click)="store.load()">Retry</button>@if (store.sessionRequired()) { <a [href]="ownerLoginHref()">Sign in as owner</a> }</aura-state-panel> }
    @if (store.snapshot().state !== 'loading' && store.snapshot().state !== 'error') {
      <section class="overview-grid" aria-label="System and ingest health">
        <article class="hero-card"><h2>System health</h2><div class="hero-line"><span class="status-dot" [class]="'dot-' + (store.snapshot().overview?.systemState ?? 'unavailable')" aria-hidden="true"></span><strong>{{ store.snapshot().overview?.systemState ?? 'unavailable' }}</strong></div><p class="muted">Snapshot {{ store.snapshot().lastUpdatedAt | date:'medium' }}</p></article>
        <article class="metric-card"><h2>Observations</h2><strong>{{ store.snapshot().overview?.observationCount ?? 0 | number }}</strong><small>Metadata-only records</small></article>
        <article class="metric-card"><h2>Runs</h2><strong>{{ store.snapshot().overview?.runCount ?? 0 | number }}</strong><small>Derived run projections</small></article>
        <article class="ingest-card"><div class="card-heading"><h2>Ingest health</h2><span class="badge" [class]="'badge-' + (store.snapshot().overview?.ingest?.state ?? 'unavailable')">{{ store.snapshot().overview?.ingest?.state ?? 'unavailable' }}</span></div><dl class="counts"><div><dt>Accepted</dt><dd>{{ store.snapshot().overview?.ingest?.counts?.accepted ?? 0 }}</dd></div><div><dt>Rejected</dt><dd>{{ store.snapshot().overview?.ingest?.counts?.rejected ?? 0 }}</dd></div><div><dt>Duplicate</dt><dd>{{ store.snapshot().overview?.ingest?.counts?.duplicate ?? 0 }}</dd></div><div><dt>Malformed</dt><dd>{{ store.snapshot().overview?.ingest?.counts?.malformed ?? 0 }}</dd></div><div><dt>Storage failures</dt><dd>{{ store.snapshot().overview?.ingest?.counts?.storageFailure ?? 0 }}</dd></div></dl></article>
      </section>
      @if (store.snapshot().state === 'stale' || store.snapshot().state === 'disconnected') { <p class="notice" role="status">{{ store.snapshot().error ?? 'Showing the last known snapshot.' }} @if (store.snapshot().state === 'disconnected') { <button type="button" (click)="store.reconnectLive()">Reconnect live updates</button> }</p> }
      @if (store.snapshot().partial) { <p class="notice" role="status">Partial data: {{ store.snapshot().resourceErrors.join(', ') }} could not be refreshed.</p> }
    }
  `,
  styles: [`
    :host { display:block; } .overview-grid { display:grid; gap:1rem; grid-template-columns:repeat(3,minmax(0,1fr)); } article { background:var(--surface); border:1px solid var(--border); border-radius:1rem; padding:1.25rem; } .hero-card,.ingest-card { grid-column:span 3; } .metric-card { display:grid; gap:.3rem; } .metric-card span,.eyebrow,dt,small,.muted { color:var(--text-muted); } .metric-card strong { font-size:2rem; } .eyebrow { font-size:.75rem; font-weight:750; letter-spacing:.08em; margin:0 0 .65rem; text-transform:uppercase; } .hero-line { align-items:center; display:flex; gap:.55rem; font-size:1.25rem; text-transform:capitalize; } .status-dot { border-radius:50%; height:.75rem; width:.75rem; } .dot-healthy { background:var(--success); } .dot-degraded { background:#c58b27; } .dot-unavailable { background:var(--danger); } .muted { margin:.5rem 0 0; } .card-heading { align-items:center; display:flex; justify-content:space-between; } h2 { font-size:1.1rem; margin:0; } .badge { border-radius:999px; font-size:.75rem; font-weight:700; padding:.3rem .6rem; text-transform:capitalize; } .badge-healthy { background:var(--success-surface); color:var(--success-text); } .badge-degraded { background:var(--warning-surface); color:var(--warning-text); } .badge-unavailable { background:var(--danger-surface); color:var(--danger-text); } .counts { display:grid; gap:.8rem; grid-template-columns:repeat(5,1fr); margin:1.1rem 0 0; } .counts div { border-left:3px solid var(--accent-soft); padding-left:.65rem; } dt { font-size:.78rem; } dd { font-size:1.3rem; font-weight:700; margin:.2rem 0 0; } .notice { background:var(--warning-surface); border:1px solid var(--warning-text); border-radius:.75rem; color:var(--warning-text); margin:1rem 0 0; padding:.75rem 1rem; } button { background:var(--accent); border:0; border-radius:.45rem; color:var(--on-accent); cursor:pointer; padding:.55rem .8rem; } @media (max-width:700px) { .overview-grid { grid-template-columns:1fr; } .hero-card,.ingest-card { grid-column:auto; } .counts { grid-template-columns:repeat(2,1fr); } }
  `],
})
export class ObservatoryOverviewComponent { readonly store = inject(ObservatoryStore); readonly ownerLoginHref = ownerLoginHref; }
