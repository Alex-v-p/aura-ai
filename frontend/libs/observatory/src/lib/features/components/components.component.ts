import { ChangeDetectionStrategy, Component, ElementRef, inject, signal, ViewChild } from '@angular/core';
import { DatePipe } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { componentAvailabilityLabel, componentCatalogueViewFor, dependencyNodeForEdge, formatDurationMs } from '../../observatory-models';
import { ObservatoryStore } from '../../observatory-store';
import { ObservatoryIconComponent } from '../../presentation/observatory-icon.component';

const MAX_COMPONENTS = 20;

@Component({
  selector: 'observatory-components', standalone: true, imports: [DatePipe, FormsModule, ObservatoryIconComponent], changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <section class="catalogue-section" [class.catalogue-hidden-small]="store.selectedComponent()" aria-labelledby="components-title">
      <div class="section-heading"><div><p class="eyebrow">Catalogue and topology</p><h2 id="components-title">Components</h2><p class="muted">Definitions are repository-approved metadata. Observed-only components are shown without inventing ownership or dependencies.</p></div><button type="button" class="secondary-button" (click)="retry()">Refresh catalogue</button></div>
      @if (catalogueView() === 'loading') { <div class="catalogue-skeleton" role="status" aria-live="polite" aria-label="Loading component catalogue"><span></span><span></span><span></span></div> }
      @else if (catalogueView() === 'error') { <div class="state failure" role="alert"><strong>Component catalogue is unavailable.</strong><p>Definitions and observed health could not be loaded.</p><button type="button" (click)="retry()">Retry component catalogue</button></div> }
      @else {
        @if (store.resourceState('components') === 'stale') { <div class="state notice" role="status"><strong>Showing the last known component catalogue.</strong><p>Some component data could not be refreshed.</p><button type="button" (click)="retry()">Retry</button></div> }
        @if (catalogueView() === 'empty') { <p class="empty" role="status">No component definitions or observed components are available yet.</p> }
        @else { <div class="catalogue-kpis" aria-label="Component health summary">@for (kpi of catalogueKpis(); track kpi.key) { <div><strong>{{ kpi.count }}</strong><span>{{ kpi.label }}</span></div> }</div><div class="catalogue-controls" role="search" aria-label="Filter component catalogue"><label for="component-search">Search components<input id="component-search" type="search" [value]="searchTerm()" (input)="searchTerm.set($any($event.target).value)" placeholder="component id or version" /></label><label for="component-definition-filter">Definition state<select id="component-definition-filter" [value]="definitionFilter()" (change)="definitionFilter.set($any($event.target).value)"><option value="all">All states</option><option value="declared">Declared</option><option value="observed_only">Observed only</option><option value="missing">Definition gap (observed only)</option></select></label><p class="catalogue-summary" role="status">Showing {{ visibleComponents().length }} of {{ store.components().length }} components</p></div>@if (!visibleComponents().length) { <p class="empty" role="status">No components match this search or definition filter.</p> } @else { <div class="component-grid" aria-label="Component catalogue">
          @for (component of visibleComponents().slice(0, MAX_COMPONENTS); track component.componentId) {
            <article class="component-card" [attr.aria-labelledby]="'component-' + safeId(component.componentId)">
              <div class="card-heading"><div><h3 [id]="'component-' + safeId(component.componentId)">{{ component.componentId }}</h3><p class="muted">{{ component.versions.length ? component.versions[0] : 'Version not observed' }}</p></div><span class="state-badge" [class.badge-bad]="component.health.errorRate !== null && component.health.errorRate > .1" [class.badge-info]="component.health.errorRate === null" [class.badge-good]="component.health.errorRate !== null && component.health.errorRate <= .1"><observatory-icon [name]="healthIcon(component.health.errorRate)" /> {{ healthLabel(component.health.errorRate) }}</span></div>
              <div class="component-metrics"><span><strong>{{ component.health.sampleCount }}</strong><small>samples</small></span><span><strong>{{ component.health.latencyP95Ms === null ? '—' : formatDuration(component.health.latencyP95Ms) }}</strong><small>P95 latency</small></span><span><strong>{{ errorRateLabel(component.health.errorRate) }}</strong><small>error rate</small></span></div>
              <p class="definition-state" [class.observed-only]="component.definitionState === 'observed_only'"><observatory-icon [name]="component.definitionState === 'declared' ? 'success' : 'document'" /> {{ definitionLabel(component.definitionState) }}</p>
              @if (component.definitionState === 'declared' && (component.definitionVersions?.length || component.versions.length)) { <div class="version-actions"><label class="sr-only" [for]="'version-' + safeId(component.componentId)">Definition version</label><select [id]="'version-' + safeId(component.componentId)" #version><option [value]="versionFor(component)">{{ versionFor(component) }}</option>@for (item of component.definitionVersions ?? []; track item) { @if (item !== versionFor(component)) { <option [value]="item">{{ item }}</option> } }</select><button #detailOpener type="button" class="primary-button" [attr.aria-expanded]="isSelected(component.componentId)" aria-controls="component-details-panel" (click)="open(component.componentId, version.value, detailOpener)">Inspect component</button></div> }
              @else { <p class="observed-copy">Observed metadata only; no approved definition is available.</p> }
            </article>
          }
        </div> } @if (visibleComponents().length > MAX_COMPONENTS) { <p class="catalogue-limit">Showing the first {{ MAX_COMPONENTS }} components to keep this workspace responsive. Refine the search to inspect more.</p> }
      }
      }
    </section>
    @if (store.selectedComponent(); as selected) {
      <section id="component-details-panel" class="details-panel" aria-labelledby="component-details-title">
        <div class="section-heading"><div><p class="eyebrow">Versioned definition</p><h2 #componentDetailsTitle id="component-details-title" tabindex="-1">{{ selected.componentId }} <span class="version">{{ selected.componentVersion }}</span></h2></div><button type="button" class="secondary-button" (click)="close()">Close details</button></div>
        <p class="sr-only" role="status" aria-live="polite">{{ detailAnnouncement(selected.state) }}</p>
        @if (selected.state === 'loading') { <p class="muted" role="status">Loading definition and dependency topology…</p> }
        @else if (selected.state === 'error') { <div class="state failure" role="alert"><strong>Component details are unavailable.</strong><p>{{ selected.error }}</p><button type="button" (click)="open(selected.componentId, selected.componentVersion)">Retry details</button></div> }
        @else {
          @if (selected.error) { <div class="state notice" role="alert"><strong>Partial component details.</strong><p>{{ selected.error }}</p><button type="button" class="secondary-button" (click)="open(selected.componentId, selected.componentVersion)">Retry details</button></div> }
          <section class="selected-health" aria-labelledby="selected-health-title"><div class="section-heading"><div><p class="eyebrow">Observed health</p><h3 id="selected-health-title">Recent component outcomes</h3><p class="muted">Health is loaded only for the selected component.</p></div></div>@if (store.isComponentHealthLoading(selected.componentId)) { <p class="muted" role="status">Loading bounded health trend…</p> } @else if (healthError(selected.componentId)) { <p class="state notice" role="status">Health trend unavailable: {{ healthError(selected.componentId) }}</p> } @else { <div class="selected-health-metrics"><span><strong>{{ selectedSampleCount(selected.componentId) }}</strong><small>samples</small></span><span><strong>{{ selectedP95(selected.componentId) }}</strong><small>P95 latency</small></span><span><strong>{{ selectedErrorRate(selected.componentId) }}</strong><small>error rate</small></span></div><div class="trend" aria-label="Selected component sample trend">@for (point of trend(selected.componentId); track point.startAt) { <span [style.height.%]="bar(point.summary.sampleCount)" [attr.title]="point.startAt | date:'short'" [attr.aria-label]="point.summary.sampleCount + ' samples at ' + (point.startAt | date:'short')" role="img"></span> }</div> }</section>
          @if (selected.definition; as definition) { <dl class="definition-grid"><div><dt>Owner</dt><dd>{{ definition.owner }}</dd></div><div><dt>Category</dt><dd>{{ definition.category }}</dd></div><div><dt>Input schema</dt><dd>{{ definition.inputSchemaId }}</dd></div><div><dt>Output schema</dt><dd>{{ definition.outputSchemaId }}</dd></div><div><dt>Measurements</dt><dd>{{ definition.measurements.join(', ') || 'None declared' }}</dd></div><div><dt>Evaluators</dt><dd>{{ definition.evaluators.join(', ') || 'None declared' }}</dd></div></dl> }
          @if (selected.topology; as topology) { <details class="topology-disclosure"><summary><observatory-icon name="trace" /> Dependency topology <span class="disclosure-hint">{{ topology.nodes.length }} nodes · {{ topology.edges.length }} edges@if (topology.partial) { · partial }</span></summary><div class="topology-summary"><p><strong>Topology snapshot:</strong> {{ topology.nodes.length }} nodes, {{ topology.edges.length }} edges. @if (topology.partial) { <span class="inline-warning">Partial health; some observations are unavailable.</span> }</p><div class="dependency-columns"><div><h3>Direct dependencies</h3><ul>@for (id of topology.directDependencyIds; track id) { <li>{{ id }}</li> } @empty { <li>None declared</li> }</ul></div><div><h3>Reverse dependencies</h3><ul>@for (id of topology.reverseDependencyIds; track id) { <li>{{ id }}</li> } @empty { <li>No observed dependants</li> }</ul></div></div><h3>Dependency relationship table</h3><div #topologyTable class="table-wrap" tabindex="0" role="region" aria-label="Scrollable dependency relationship table" (keydown)="scrollTable($event, topologyTable)"><table><caption class="sr-only">Complete dependency relationships for {{ selected.componentId }} version {{ selected.componentVersion }}</caption><thead><tr><th scope="col">Source</th><th scope="col">Target</th><th scope="col">Kind</th><th scope="col">Resolution</th><th scope="col">Availability</th><th scope="col">Observed health</th></tr></thead><tbody>@for (edge of topology.edges; track edge.sourceId + edge.targetId + edge.targetVersion) { <tr><td>{{ edge.sourceId }} <span class="nowrap">{{ edge.sourceVersion }}</span></td><td>{{ edge.targetId }} <span class="nowrap">{{ edge.targetVersion || 'any version' }}</span></td><td>{{ edge.targetKind }}</td><td>{{ resolutionLabel(edge.resolution) }}</td><td>{{ nodeAvailability(topology, edge.targetId, edge.targetVersion) }}</td><td>{{ nodeHealth(topology, edge.targetId, edge.targetVersion) }}</td></tr> } @empty { <tr><td colspan="6">No dependency edges were declared or observed for this version.</td></tr> }</tbody></table></div></div></details> }
          @if (!selected.definition && !selected.topology) { <p class="empty">No approved definition or dependency topology is available for this version.</p> }
        }
      </section>
    }
  `,
  styles: [`
    :host { display:block; } section + section { margin-top:2rem; } .section-heading { align-items:flex-start; display:flex; flex-wrap:wrap; gap:1rem; justify-content:space-between; margin-bottom:1rem; } h2,h3 { margin:0; overflow-wrap:anywhere; } h2 { font-size:1.25rem; } h3 { font-size:1rem; } .eyebrow { color:var(--accent-hover); font-size:.72rem; font-weight:800; letter-spacing:.08em; margin:0 0 .35rem; text-transform:uppercase; } .muted,dt { color:var(--text-muted); } .muted { font-size:.84rem; margin:.35rem 0 0; } button,select { border-radius:.45rem; cursor:pointer; min-height:2.4rem; } button { font-weight:700; padding:.5rem .75rem; } button:focus-visible,select:focus-visible,.table-wrap:focus-visible { outline:3px solid var(--focus); outline-offset:3px; } .primary-button { background:var(--accent); border:1px solid var(--accent); color:var(--on-accent); } .secondary-button { background:var(--surface); border:1px solid var(--control-border); color:var(--text); } .component-grid { display:grid; gap:1rem; grid-template-columns:repeat(auto-fit,minmax(min(20rem,100%),1fr)); } .component-card,.details-panel { background:var(--surface); border:1px solid var(--border); border-radius:1rem; padding:1.1rem; } .card-heading { align-items:flex-start; display:flex; flex-wrap:wrap; gap:.7rem; justify-content:space-between; } .state-badge { border-radius:999px; font-size:.72rem; font-weight:750; padding:.3rem .5rem; white-space:nowrap; } .badge-good { background:var(--success-surface); color:var(--success-text); } .badge-bad { background:var(--danger-surface); color:var(--danger-text); } .badge-info { background:var(--info-surface); color:var(--info-text); } .definition-state { font-size:.82rem; font-weight:700; margin:.75rem 0 0; } .definition-state.observed-only,.observed-copy { color:var(--warning-text); } dl { display:grid; gap:.8rem; grid-template-columns:repeat(2,1fr); margin:1rem 0; } dt { font-size:.72rem; } dd { font-weight:700; margin:.2rem 0 0; overflow-wrap:anywhere; } .trend { align-items:end; border-bottom:1px solid var(--border); display:flex; gap:.25rem; height:3.5rem; padding:0 .25rem; } .trend span { align-items:end; background:var(--accent); color:var(--on-accent); display:flex; flex:1; font-size:.62rem; justify-content:center; min-height:.2rem; overflow:hidden; } .state { border-radius:.5rem; margin:1rem 0; padding:.7rem; } .state p { margin:.3rem 0 .6rem; } .failure { background:var(--danger-surface); color:var(--danger-text); } .notice { background:var(--warning-surface); color:var(--warning-text); } .failure-inline { background:var(--danger-surface); color:var(--danger-text); font-size:.8rem; padding:.5rem; } .empty { color:var(--text-muted); padding:1rem 0; } .catalogue-skeleton { display:grid; gap:1rem; grid-template-columns:repeat(3,minmax(0,1fr)); } .catalogue-skeleton span { background:var(--surface-subtle); border:1px solid var(--border); border-radius:1rem; height:14rem; } .version-actions { align-items:end; display:flex; flex-wrap:wrap; gap:.5rem; margin-top:1rem; } .version-actions label { flex-basis:100%; font-size:.78rem; font-weight:700; } select { background:var(--surface); border:1px solid var(--control-border); color:var(--text); padding:.4rem; } .observed-copy { font-size:.82rem; } .version { color:var(--text-muted); font-size:.8em; font-weight:500; } .definition-grid { grid-template-columns:repeat(auto-fit,minmax(10rem,1fr)); } .topology-summary { border-top:1px solid var(--border); margin-top:1rem; padding-top:1rem; } .topology-summary p { margin-top:0; } .inline-warning { color:var(--warning-text); } .dependency-columns { display:grid; gap:1rem; grid-template-columns:repeat(2,minmax(0,1fr)); margin:1rem 0; } ul { margin:.5rem 0 0; padding-left:1.25rem; } li { overflow-wrap:anywhere; padding:.2rem 0; } .table-wrap { max-width:100%; overflow-x:auto; } table { border-collapse:collapse; min-width:42rem; width:100%; } th,td { border-bottom:1px solid var(--border); padding:.6rem .5rem; text-align:left; vertical-align:top; } th { font-size:.76rem; } td { font-size:.82rem; } .nowrap { white-space:nowrap; } .sr-only { height:1px; margin:-1px; overflow:hidden; padding:0; position:absolute; width:1px; clip:rect(0,0,0,0); white-space:nowrap; } @media (max-width:640px) { .catalogue-skeleton { grid-template-columns:1fr; } dl { grid-template-columns:1fr; } .dependency-columns { grid-template-columns:1fr; } .component-card,.details-panel { padding:.9rem; } .version-actions > * { width:100%; } .version-actions label { width:auto; } table { min-width:38rem; } } @media (prefers-reduced-motion:reduce) { *,*::before,*::after { scroll-behavior:auto !important; transition-duration:.01ms !important; } }
  `, `
    .component-card,.details-panel { background:var(--surface); border:0; border-radius:0; border-top:2px solid var(--control-border); padding:1rem 0; }
    .component-card { border-top-color:var(--accent-soft); }
    .trend-legend { color:var(--text-muted); font-size:.73rem; margin:.4rem 0 0; }
    .trend { background:var(--surface-subtle); }
    .trend span { background:var(--accent); }
    .topology-summary { border-top:2px solid var(--control-border); }
    @media (max-width:640px) { .component-card,.details-panel { padding-block:.85rem; } }
    .component-card:has(button[aria-expanded="true"]) { background:var(--surface-tint); border-top:3px solid var(--accent); }
    .details-panel { scroll-margin-top:1rem; }
    @media (max-width:640px) { .catalogue-hidden-small { display:none; } .details-panel { margin-top:0; } }
  `, `
    .section-heading { margin-bottom:1.5rem; }
    .section-heading > div { max-width:var(--obs-prose-max); }
    .component-grid { gap:1.5rem; grid-template-columns:repeat(auto-fit,minmax(min(24rem,100%),1fr)); }
    .component-card,.details-panel { border-top-width:3px; padding:1.35rem; }
    .component-card .card-heading { gap:1rem; }
    .component-card dl { gap:1rem; margin-block:1.25rem; }
    .state { border-left:3px solid currentColor; border-radius:.45rem; margin-block:1.25rem; padding:.8rem 1rem; }
    .failure-inline { background:var(--surface-subtle); border-left:2px solid var(--warning-text); border-radius:0; color:var(--text-muted); padding:.55rem .7rem; }
    .topology-summary { margin-top:1.5rem; padding-top:1.25rem; }
    .dependency-columns { gap:1.5rem; }
    @media (max-width:640px) { .component-card,.details-panel { padding:1.1rem; } .component-grid { gap:1rem; } }
  `, `
    .catalogue-kpis { display:grid; gap:.55rem; grid-template-columns:repeat(6,minmax(0,1fr)); margin-bottom:1rem; }
    .catalogue-kpis div { background:var(--surface-subtle); border:1px solid var(--border); border-radius:.55rem; display:grid; gap:.15rem; padding:.65rem; }
    .catalogue-kpis strong { font-size:1rem; }
    .catalogue-kpis span { color:var(--text-muted); font-size:.66rem; }
    .catalogue-controls { align-items:end; background:var(--surface-subtle); border:1px solid var(--border); border-radius:.65rem; display:grid; gap:1rem; grid-template-columns:minmax(12rem,1fr) minmax(10rem,16rem) auto; margin-bottom:1.25rem; padding:1rem; }
    .catalogue-controls label { color:var(--text-muted); display:grid; font-size:.76rem; font-weight:700; gap:.35rem; }
    .catalogue-controls input,.catalogue-controls select { background:var(--surface); border:1px solid var(--control-border); border-radius:.4rem; color:var(--text); min-height:2.35rem; padding:.4rem .55rem; }
    .catalogue-summary,.catalogue-limit { color:var(--text-muted); font-size:.76rem; margin:0; }
    .catalogue-limit { margin-top:1rem; }
    .state-badge observatory-icon,.definition-state observatory-icon { vertical-align:-.1em; }
    .component-grid { gap:.55rem; grid-template-columns:1fr; }
    .component-card { align-items:center; display:grid; gap:.7rem; grid-template-columns:minmax(12rem,1.5fr) minmax(12rem,1fr) minmax(9rem,.8fr) auto; padding:.8rem 1rem; }
    .component-card .card-heading { align-items:center; min-width:0; }
    .component-card .card-heading h3 { font-size:.88rem; }
    .component-card .definition-state { margin:0; }
    .component-metrics { align-items:center; display:flex; gap:.8rem; }
    .component-metrics span,.selected-health-metrics span { display:grid; gap:.1rem; min-width:0; }
    .component-metrics strong,.selected-health-metrics strong { font-size:.8rem; white-space:nowrap; }
    .component-metrics small,.selected-health-metrics small { color:var(--text-muted); font-size:.65rem; }
    .selected-health { border-top:1px solid var(--border); margin:1rem 0; padding-top:1rem; }
    .selected-health-metrics { display:flex; flex-wrap:wrap; gap:1.5rem; margin:.8rem 0; }
    .selected-health .trend { max-width:28rem; }
    .component-card .version-actions { align-items:center; margin:0; }
    .component-card .version-actions select { max-width:7rem; }
    .component-card .version-actions button { white-space:nowrap; }
    .topology-disclosure { border-top:1px solid var(--border); margin-top:1rem; padding-top:1rem; }
    .topology-disclosure > summary { align-items:center; color:var(--accent-hover); cursor:pointer; display:flex; font-size:.82rem; font-weight:750; gap:.45rem; list-style:none; }
    .topology-disclosure > summary { list-style:revert; }
    .topology-disclosure > summary::-webkit-details-marker { display:inline; }
    .topology-disclosure > summary:focus-visible { outline:3px solid var(--focus); outline-offset:3px; }
    .topology-disclosure .disclosure-hint { color:var(--text-muted); font-size:.72rem; margin-left:auto; }
    @media (max-width:840px) { .component-card { align-items:start; grid-template-columns:minmax(10rem,1fr) minmax(10rem,1fr) auto; } .component-card .definition-state { grid-column:1 / 2; } .component-card .version-actions { grid-column:3; grid-row:1 / span 2; } }
    @media (min-width:841px) { :host { display:grid; gap:1.5rem; grid-template-columns:minmax(20rem,1fr) minmax(20rem,1.2fr); align-items:start; } .catalogue-section { grid-column:1; } .details-panel { grid-column:2; grid-row:1; margin:0; } }
    @media (max-width:640px) { .catalogue-kpis { grid-template-columns:repeat(3,minmax(0,1fr)); } .catalogue-controls { grid-template-columns:1fr; } .component-card { align-items:start; display:grid; grid-template-columns:minmax(0,1fr) auto; grid-template-rows:auto auto; } .component-card .card-heading { grid-column:1 / -1; grid-row:1; } .component-card .component-metrics { grid-column:1 / -1; grid-row:2; } .component-card .definition-state { grid-column:1; grid-row:3; } .component-card .version-actions { align-items:stretch; grid-column:2; grid-row:3; } .component-card .version-actions select { max-width:none; } .component-card .version-actions button { min-height:2.2rem; } .topology-disclosure .disclosure-hint { margin-left:auto; } }
    @media (max-width:420px) { .catalogue-kpis { grid-template-columns:repeat(2,minmax(0,1fr)); } .component-metrics { flex-wrap:wrap; gap:.65rem; } }
  `],
})
export class ObservatoryComponentsComponent {
  readonly store = inject(ObservatoryStore);
  readonly MAX_COMPONENTS = MAX_COMPONENTS;
  readonly searchTerm = signal('');
  readonly definitionFilter = signal<'all' | 'declared' | 'observed_only' | 'missing'>('all');
  @ViewChild('componentDetailsTitle') private componentDetailsTitle?: ElementRef<HTMLHeadingElement>;
  private detailOpener: HTMLButtonElement | null = null;
  trend(componentId: string) { return (this.store.componentHealthById()[componentId]?.points ?? []).slice(0, 12); }
  selectedHealth(componentId: string) { return this.store.componentHealthById()[componentId]?.summary ?? null; }
  formatDuration(durationMs: number | null): string { return formatDurationMs(durationMs); }
  selectedSampleCount(componentId: string): string { return String(this.selectedHealth(componentId)?.sampleCount ?? '—'); }
  selectedP95(componentId: string): string { const value = this.selectedHealth(componentId)?.latencyP95Ms; return value === null || value === undefined ? '—' : formatDurationMs(value); }
  selectedErrorRate(componentId: string): string { return this.errorRateLabel(this.selectedHealth(componentId)?.errorRate ?? null); }
  catalogueKpis(): ReadonlyArray<{ readonly key: string; readonly label: string; readonly count: number }> {
    const components = this.store.components();
    const stale = this.store.resourceState('components') === 'stale' ? components.length : 0;
    const degraded = components.filter((component) => component.health.errorRate !== null && component.health.errorRate > .1).length;
    const noData = components.filter((component) => component.health.errorRate === null).length;
    const missingDefinition = components.filter((component) => component.definitionState !== 'declared').length;
    const healthy = components.filter((component) => component.health.errorRate !== null && component.health.errorRate <= .1).length;
    return [{ key: 'total', label: 'Total', count: components.length }, { key: 'healthy', label: 'Healthy', count: healthy }, { key: 'degraded', label: 'Degraded', count: degraded }, { key: 'no-data', label: 'No outcome data', count: noData }, { key: 'missing-definition', label: 'Missing definition', count: missingDefinition }, { key: 'stale', label: 'Stale projection', count: stale }];
  }
  visibleComponents() {
    const search = this.searchTerm().trim().toLowerCase(); const filter = this.definitionFilter();
    return this.store.components().map((component, index) => ({ component, index })).filter(({ component }) => (!search || [component.componentId, ...component.versions].some((value) => value.toLowerCase().includes(search))) && (filter === 'all' || (filter === 'missing' ? component.definitionState !== 'declared' : component.definitionState === filter))).sort((left, right) => this.problemScore(right.component) - this.problemScore(left.component) || left.index - right.index).map(({ component }) => component);
  }
  private problemScore(component: ReturnType<ObservatoryStore['components']>[number]): number {
    if (component.definitionState !== 'declared') return 5;
    if (component.health.errorRate !== null && component.health.errorRate > .1) return 4;
    if (component.health.errorRate === null) return 3;
    if (this.store.resourceState('components') === 'stale') return 2;
    return 1;
  }
  catalogueView() { return componentCatalogueViewFor(this.store.resourceState('components'), this.store.components().length); }
  bar(sampleCount: number): number { const max = Math.max(...this.store.components().map((component) => component.health.sampleCount), 1); return Math.max(5, Math.round((sampleCount / max) * 100)); }
  healthError(componentId: string): string | null { return this.store.componentHealthErrors()[componentId] ?? null; }
  retry(): void { void this.store.load(); }
  open(componentId: string, componentVersion: string, opener?: HTMLButtonElement): void {
    if (opener) this.detailOpener = opener;
    const requestOpener = this.detailOpener;
    if (!this.store.componentHealthById()[componentId] && !this.store.isComponentHealthLoading(componentId)) void this.store.componentHealth(componentId);
    void this.store.openComponent(componentId, componentVersion).then(() => {
      const selection = this.store.selectedComponent();
      if (requestOpener === this.detailOpener && selection?.componentId === componentId && selection.componentVersion === componentVersion && (selection.state === 'ready' || selection.state === 'partial' || selection.state === 'error')) setTimeout(() => this.componentDetailsTitle?.nativeElement.focus(), 0);
    });
  }
  close(): void { const opener = this.detailOpener; this.detailOpener = null; this.store.closeComponent(); setTimeout(() => opener?.focus(), 0); }
  isSelected(componentId: string): boolean { return this.store.selectedComponent()?.componentId === componentId; }
  safeId(value: string): string { return value.replace(/[^a-zA-Z0-9_-]/g, '-'); }
  versionFor(component: { readonly versions: ReadonlyArray<string>; readonly definitionVersions?: ReadonlyArray<string> }): string { return component.definitionVersions?.[0] ?? component.versions[0] ?? 'unknown'; }
  definitionLabel(state: string | undefined): string { return state === 'declared' ? 'Declared definition' : state === 'missing' ? 'Definition unavailable' : 'Observed only'; }
  healthLabel(errorRate: number | null): string { return errorRate === null ? 'No outcome data' : errorRate > .1 ? 'Degraded' : 'Healthy'; }
  healthIcon(errorRate: number | null): 'success' | 'warning' | 'overview' { return errorRate === null ? 'overview' : errorRate > .1 ? 'warning' : 'success'; }
  errorRateLabel(errorRate: number | null): string { return errorRate === null ? 'No outcome data' : `${Math.round(errorRate * 100)}% errors`; }
  detailAnnouncement(state: string): string { return state === 'loading' ? 'Component details loading.' : state === 'partial' ? 'Component details partially loaded.' : state === 'error' ? 'Component details unavailable.' : state === 'ready' ? 'Component details loaded.' : ''; }
  resolutionLabel(resolution: string): string { return resolution === 'resolved' ? 'Resolved' : resolution === 'missing_definition' ? 'Missing definition' : 'Version unavailable'; }
  nodeAvailability(topology: { readonly nodes: ReadonlyArray<{ readonly dependencyId: string; readonly componentVersion: string | null; readonly availability: string }> }, id: string, version: string | null): string { return componentAvailabilityLabel(dependencyNodeForEdge(topology.nodes, id, version)?.availability ?? 'unavailable'); }
  nodeHealth(topology: { readonly nodes: ReadonlyArray<{ readonly dependencyId: string; readonly componentVersion: string | null; readonly health: { readonly sampleCount: number; readonly errorRate: number | null; readonly latencyP95Ms: number | null } | null }> }, id: string, version: string | null): string { const node = dependencyNodeForEdge(topology.nodes, id, version); if (!node) return 'Unavailable'; if (!node.health) return 'No outcome data'; const rate = node.health.errorRate === null ? 'No outcome data' : `${node.health.errorRate * 100}% errors`; const p95 = node.health.latencyP95Ms === null ? 'p95 unavailable' : `p95 ${formatDurationMs(node.health.latencyP95Ms)}`; return `${node.health.sampleCount} samples · ${rate} · ${p95} · ${node.health.errorRate !== null && node.health.errorRate > .1 ? 'Degraded' : node.health.errorRate === null ? 'No outcome data' : 'Healthy'}`; }
  scrollTable(event: KeyboardEvent, table: HTMLElement): void { const amount = event.key === 'ArrowRight' || event.key === 'PageDown' ? 120 : event.key === 'ArrowLeft' || event.key === 'PageUp' ? -120 : 0; if (!amount) return; event.preventDefault(); table.scrollLeft += amount; }
}
