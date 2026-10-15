import { ChangeDetectionStrategy, Component, DoCheck, inject } from '@angular/core';
import { DatePipe, PercentPipe } from '@angular/common';
import { ObservatoryStore } from '../../observatory-store';

@Component({
  selector: 'observatory-components', standalone: true, imports: [DatePipe, PercentPipe], changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    @if (store.resourceState('components') === 'loading') { <p class="muted" role="status">Loading component health…</p> } @else if (store.resourceState('components') === 'error') { <p class="failure" role="alert">Component health is unavailable. <button type="button" (click)="retry()">Retry component health</button></p> } @else if (store.resourceState('components') === 'stale') { <p class="notice" role="status">Component catalogue could not be refreshed; showing the last known health. <button type="button" (click)="retry()">Retry</button></p> } @else if (store.resourceState('components') === 'empty') { <p class="empty" role="status">No component catalogue entries are available yet.</p> } @else { <div class="component-grid">@for (component of store.components(); track component.componentId) { <article class="component-card"><div class="card-heading"><div><h2>{{ component.componentId }}</h2><p class="muted">{{ component.versions.join(', ') }}</p></div><span class="outcome" [class]="component.health.errorRate !== null && component.health.errorRate > .1 ? 'bad' : 'good'">{{ component.health.errorRate === null ? '—' : (component.health.errorRate | percent:'1.0-1') }} errors</span></div><dl><div><dt>Samples</dt><dd>{{ component.health.sampleCount }}</dd></div><div><dt>P50 latency</dt><dd>{{ component.health.latencyP50Ms === null ? '—' : component.health.latencyP50Ms + ' ms' }}</dd></div><div><dt>P95 latency</dt><dd>{{ component.health.latencyP95Ms === null ? '—' : component.health.latencyP95Ms + ' ms' }}</dd></div><div><dt>Success</dt><dd>{{ component.health.outcomes.succeeded }}</dd></div></dl>@if (store.isComponentHealthLoading(component.componentId)) { <p class="muted" role="status">Loading sample-count trend…</p> } @else { <div class="trend" aria-label="Sample-count trend; bar height scales with sample count">@for (point of trend(component.componentId); track point.startAt) { <span [style.height.%]="bar(point.summary.sampleCount)" [attr.title]="point.startAt | date:'short'" [attr.aria-label]="point.summary.sampleCount + ' samples at ' + (point.startAt | date:'short')" role="img">{{ point.summary.sampleCount }}</span> }</div> }@if (healthError(component.componentId)) { <p class="failure" role="status">Trend unavailable: {{ healthError(component.componentId) }}</p> }<p class="muted">Seen {{ component.firstSeenAt | date:'short' }} – {{ component.lastSeenAt | date:'short' }}</p></article> }</div> }
  `,
  styles: [`
    :host { display:block; } .component-grid { display:grid; gap:1rem; grid-template-columns:repeat(auto-fit,minmax(min(17rem,100%),1fr)); } .component-card { background:var(--surface); border:1px solid var(--border); border-radius:1rem; padding:1.15rem; } .card-heading { align-items:flex-start; display:flex; flex-wrap:wrap; gap:.7rem; justify-content:space-between; } h2 { font-size:1rem; margin:0; overflow-wrap:anywhere; } .muted,dt { color:var(--text-muted); } .muted { font-size:.8rem; margin:.35rem 0 0; } .outcome { border-radius:999px; font-size:.72rem; font-weight:700; padding:.25rem .45rem; white-space:nowrap; } .good { background:var(--success-surface); color:var(--success-text); } .bad { background:var(--danger-surface); color:var(--danger-text); } dl { display:grid; gap:.8rem; grid-template-columns:repeat(2,1fr); margin:1.1rem 0; } dt { font-size:.72rem; } dd { font-weight:700; margin:.2rem 0 0; } .trend { align-items:end; border-bottom:1px solid var(--border); display:flex; gap:.25rem; height:3.5rem; padding:0 .25rem; } .trend span { align-items:end; background:var(--accent); color:var(--on-accent); display:flex; flex:1; font-size:.62rem; justify-content:center; min-height:.2rem; overflow:hidden; } .failure,.notice { background:var(--danger-surface); color:var(--danger-text); font-size:.8rem; padding:.55rem; } .notice { background:var(--warning-surface); color:var(--warning-text); } .empty { color:var(--text-muted); padding:1rem 0; } button { background:var(--accent); border:0; border-radius:.4rem; color:var(--on-accent); cursor:pointer; padding:.35rem .6rem; }
  `],
})
export class ObservatoryComponentsComponent implements DoCheck {
  readonly store = inject(ObservatoryStore);
  private readonly prefetchPending = new Set<string>();
  ngDoCheck(): void {
    for (const component of this.store.components()) {
      const id = component.componentId;
      if (this.store.componentHealthById()[id] || this.store.componentHealthErrors()[id] || this.store.isComponentHealthLoading(id) || this.prefetchPending.has(id)) continue;
      this.prefetchPending.add(id);
      queueMicrotask(() => { this.prefetchPending.delete(id); if (!this.store.componentHealthById()[id] && !this.store.isComponentHealthLoading(id)) void this.store.componentHealth(id); });
    }
  }
  trend(componentId: string) { return this.store.componentHealthById()[componentId]?.points ?? []; }
  bar(sampleCount: number): number { const max = Math.max(...this.store.components().map((component) => component.health.sampleCount), 1); return Math.max(5, Math.round((sampleCount / max) * 100)); }
  healthError(componentId: string): string | null { return this.store.componentHealthErrors()[componentId] ?? null; }
  retry(): void { void this.store.load(); }
}
