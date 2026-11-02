import { applicationConfig, componentWrapperDecorator } from '@storybook/angular';
import type { Meta, StoryObj } from '@storybook/angular';
import { signal } from '@angular/core';
import { ObservatoryAnalyticsComponent, ObservatoryComponentsComponent, ObservatoryRunsComponent, ObservatoryStore, type AnalyticsResource, type ObservatorySnapshot } from '@aura/observatory';
import type { ComponentCollection, RunPage } from '@aura/observatory-api-client';
import { DEFAULT_ANALYTICS_FILTERS } from '@aura/observatory';

const observedAt = '2026-10-21T09:00:00.000Z';
const shell = componentWrapperDecorator((story) => `<div style="--surface:#fff;--surface-raised:#fffafb;--surface-subtle:#f5eef1;--text:#2b2025;--text-muted:#71636a;--border:#ded2d8;--control-border:#9b808d;--accent:#873f60;--accent-hover:#692d49;--on-accent:#fff;--accent-soft:#ead4de;--focus:#3174ad;--success-surface:#e2f5e8;--success-text:#17663a;--warning-surface:#fff3d4;--warning-text:#704f00;--danger-surface:#fde8e8;--danger-text:#8f2020;--info-surface:#e9f3fc;--info-text:#1d527a;max-width:1280px;padding:24px">${story}</div>`);
const run = (index: number, limited = false) => ({ runId: `run-story-${index}`, status: index % 3 === 0 ? 'failed' : 'succeeded', startedAt: observedAt, finishedAt: observedAt, durationMs: 420 + index * 30, model: limited ? null : 'storybook-model', deploymentId: 'deploy-storybook', componentIds: ['aura.core.api'], traceId: limited ? null : `trace-story-${index}` });
const runsPage = (items: ReadonlyArray<ReturnType<typeof run>>, partial = false): RunPage => ({ items, nextCursor: null, latestObservationId: 'obs-storybook', snapshotAt: observedAt, partial } as unknown as RunPage);
const baseSnapshot = (runs: RunPage | null = null, components: ComponentCollection | null = null): ObservatorySnapshot => ({ overview: null, runs, components, deployments: null, state: 'ready', lastUpdatedAt: observedAt, lastEventId: 'obs-storybook', error: null, partial: false, resourceErrors: [] });

function runsStore(state: 'loading' | 'ready' | 'empty' | 'stale' | 'disconnected' | 'error', items: ReadonlyArray<ReturnType<typeof run>> = [run(1), run(2, true)], error: string | null = null): ObservatoryStore {
  const page = state === 'empty' || state === 'loading' || state === 'error' ? null : runsPage(items, state === 'stale');
  return { snapshot: signal(baseSnapshot(page)), filters: signal({}), resourceState: (resource: string) => resource === 'runs' ? state : 'ready', setFilters: () => undefined, load: async () => undefined, openRun: async () => undefined, activateRunsView: () => undefined, deactivateRunsView: () => undefined } as unknown as ObservatoryStore;
}

const component = (index: number, observedOnly = false) => ({ componentId: `aura.component.${index}`, versions: ['1.0.0'], definitionVersions: observedOnly ? [] : ['1.0.0'], definitionState: observedOnly ? 'observed_only' : 'declared', health: { sampleCount: 20 + index, latencyP50Ms: 18 + index, latencyP95Ms: 45 + index, errorRate: index === 3 ? .2 : 0, outcomes: { succeeded: 19 + index, failed: index === 3 ? 4 : 0, cancelled: 0, unknown: 0 } }, firstSeenAt: observedAt, lastSeenAt: observedAt });
function componentsStore(state: 'loading' | 'ready' | 'empty' | 'stale' | 'error', items = [component(1), component(2, true), component(3)], error: string | null = null): ObservatoryStore {
  const page = state === 'loading' || state === 'empty' || state === 'error' ? null : { items, nextCursor: null, snapshotAt: observedAt, partial: state === 'stale' };
  return { snapshot: signal(baseSnapshot(null, page as never)), components: signal(items), resourceState: (resource: string) => resource === 'components' ? state : 'ready', selectedComponent: signal(null), componentHealthById: signal({}), componentHealthErrors: signal({}), componentHealthLoading: signal({}), isComponentHealthLoading: () => false, load: async () => undefined, componentHealth: async () => null, openComponent: async () => undefined, closeComponent: () => undefined } as unknown as ObservatoryStore;
}

const metricCatalogue = { snapshotAt: observedAt, capturePolicy: 'metadata_only', limits: { defaultWindowHours: 24, maximumWindowDays: 31, maximumPointsPerSeries: 200, maximumSeries: 12, maximumBreakdowns: 50, maximumScannedRows: 1000000, executionTimeoutMs: 5000 }, items: [{ metric: 'operation_duration_ms', unit: 'milliseconds', focusAreas: ['all'], supportedAggregations: ['average', 'p95'], supportedGroupings: ['none', 'component'], availability: 'available', unavailableSignals: [] }] };
const analyticsSnapshot = { snapshotAt: observedAt, percentileMethod: 'clickhouse_quantile_exact', capturePolicy: 'metadata_only', query: { metric: 'operation_duration_ms', unit: 'milliseconds', aggregation: 'p95', focus: 'all', from: observedAt, to: observedAt, bucket: 'auto', bucketSeconds: 3600, groupBy: 'component', filters: {}, seriesLimit: 12, pointLimit: 200 }, series: [], breakdowns: [], freshness: { latestObservedAt: observedAt, lagMs: 0, state: 'fresh' }, partial: false, partialReasons: [], unavailableSignals: [], scan: { scannedRows: 0, scanLimit: 1000000, executionMs: 2, executionLimitMs: 5000 } };
function analyticsStore(state: 'loading' | 'empty' | 'ready' | 'stale' | 'disconnected' | 'error', error: string | null = null, partial = false): ObservatoryStore {
  const resource: AnalyticsResource = { catalogue: metricCatalogue as never, snapshot: state === 'loading' || state === 'error' ? null : { ...analyticsSnapshot, partial } as never, state, error };
  return { analytics: signal(resource), analyticsFilters: signal({ ...DEFAULT_ANALYTICS_FILTERS }), setAnalyticsFilters: () => undefined, refreshAnalytics: () => undefined } as unknown as ObservatoryStore;
}

const meta: Meta = { title: 'Observatory/Destination state matrix', decorators: [shell], parameters: { a11y: { test: 'error' } } };
export default meta;
type RunsStory = StoryObj<ObservatoryRunsComponent> & { component?: typeof ObservatoryRunsComponent };
type ComponentsStory = StoryObj<ObservatoryComponentsComponent> & { component?: typeof ObservatoryComponentsComponent };
type AnalyticsStory = StoryObj<ObservatoryAnalyticsComponent> & { component?: typeof ObservatoryAnalyticsComponent };

export const RunsReadyLimitedProjection: RunsStory = { name: 'Runs · ready with limited historical projection', component: ObservatoryRunsComponent, decorators: [applicationConfig({ providers: [{ provide: ObservatoryStore, useValue: runsStore('ready') }] })] };
export const RunsLoading: RunsStory = { name: 'Runs · loading', component: ObservatoryRunsComponent, decorators: [applicationConfig({ providers: [{ provide: ObservatoryStore, useValue: runsStore('loading') }] })] };
export const RunsEmpty: RunsStory = { name: 'Runs · empty', component: ObservatoryRunsComponent, decorators: [applicationConfig({ providers: [{ provide: ObservatoryStore, useValue: runsStore('empty') }] })] };
export const RunsStale: RunsStory = { name: 'Runs · stale', component: ObservatoryRunsComponent, decorators: [applicationConfig({ providers: [{ provide: ObservatoryStore, useValue: runsStore('stale') }] })] };
export const RunsDisconnected: RunsStory = { name: 'Runs · disconnected', component: ObservatoryRunsComponent, decorators: [applicationConfig({ providers: [{ provide: ObservatoryStore, useValue: runsStore('disconnected') }] })] };
export const RunsError: RunsStory = { name: 'Runs · error', component: ObservatoryRunsComponent, decorators: [applicationConfig({ providers: [{ provide: ObservatoryStore, useValue: runsStore('error', [], 'The historical runs projection is unavailable.') }] })] };
export const RunsLargeBounded: RunsStory = { name: 'Runs · large bounded catalogue', component: ObservatoryRunsComponent, parameters: { viewport: { defaultViewport: 'desktop' } }, decorators: [applicationConfig({ providers: [{ provide: ObservatoryStore, useValue: runsStore('ready', Array.from({ length: 60 }, (_, index) => run(index + 1, index % 5 === 0))) }] })] };
export const RunsNarrow: RunsStory = { name: 'Runs · narrow screen', component: ObservatoryRunsComponent, parameters: { viewport: { defaultViewport: 'mobile1' } }, decorators: [applicationConfig({ providers: [{ provide: ObservatoryStore, useValue: runsStore('ready') }] })] };

export const ComponentsReady: ComponentsStory = { name: 'Components · ready catalogue', component: ObservatoryComponentsComponent, decorators: [applicationConfig({ providers: [{ provide: ObservatoryStore, useValue: componentsStore('ready') }] })] };
export const ComponentsLargeBounded: ComponentsStory = { name: 'Components · 60 component bounded catalogue', component: ObservatoryComponentsComponent, decorators: [applicationConfig({ providers: [{ provide: ObservatoryStore, useValue: componentsStore('ready', Array.from({ length: 60 }, (_, index) => component(index + 1, index % 4 === 0))) }] })] };
export const ComponentsLoading: ComponentsStory = { name: 'Components · loading', component: ObservatoryComponentsComponent, decorators: [applicationConfig({ providers: [{ provide: ObservatoryStore, useValue: componentsStore('loading') }] })] };
export const ComponentsEmpty: ComponentsStory = { name: 'Components · empty', component: ObservatoryComponentsComponent, decorators: [applicationConfig({ providers: [{ provide: ObservatoryStore, useValue: componentsStore('empty', []) }] })] };
export const ComponentsStale: ComponentsStory = { name: 'Components · stale', component: ObservatoryComponentsComponent, decorators: [applicationConfig({ providers: [{ provide: ObservatoryStore, useValue: componentsStore('stale') }] })] };
export const ComponentsPartialError: ComponentsStory = { name: 'Components · partial/error', component: ObservatoryComponentsComponent, decorators: [applicationConfig({ providers: [{ provide: ObservatoryStore, useValue: componentsStore('error', [], 'Component definitions were rejected; observed metadata remains unavailable.') }] })] };
export const ComponentsNarrow: ComponentsStory = { name: 'Components · narrow screen', component: ObservatoryComponentsComponent, parameters: { viewport: { defaultViewport: 'mobile1' } }, decorators: [applicationConfig({ providers: [{ provide: ObservatoryStore, useValue: componentsStore('ready') }] })] };

export const AnalyticsReady: AnalyticsStory = { name: 'Analytics · ready metrics', component: ObservatoryAnalyticsComponent, decorators: [applicationConfig({ providers: [{ provide: ObservatoryStore, useValue: analyticsStore('ready') }] })] };
export const AnalyticsLoading: AnalyticsStory = { name: 'Analytics · loading', component: ObservatoryAnalyticsComponent, decorators: [applicationConfig({ providers: [{ provide: ObservatoryStore, useValue: analyticsStore('loading') }] })] };
export const AnalyticsEmpty: AnalyticsStory = { name: 'Analytics · empty', component: ObservatoryAnalyticsComponent, decorators: [applicationConfig({ providers: [{ provide: ObservatoryStore, useValue: analyticsStore('empty') }] })] };
export const AnalyticsStale: AnalyticsStory = { name: 'Analytics · stale', component: ObservatoryAnalyticsComponent, decorators: [applicationConfig({ providers: [{ provide: ObservatoryStore, useValue: analyticsStore('stale', 'The bounded aggregate is stale.') }] })] };
export const AnalyticsDisconnected: AnalyticsStory = { name: 'Analytics · disconnected', component: ObservatoryAnalyticsComponent, decorators: [applicationConfig({ providers: [{ provide: ObservatoryStore, useValue: analyticsStore('disconnected') }] })] };
export const AnalyticsPartial: AnalyticsStory = { name: 'Analytics · partial', component: ObservatoryAnalyticsComponent, decorators: [applicationConfig({ providers: [{ provide: ObservatoryStore, useValue: analyticsStore('ready', 'Some series were rejected by bounded validation.', true) }] })] };
export const AnalyticsError: AnalyticsStory = { name: 'Analytics · error', component: ObservatoryAnalyticsComponent, decorators: [applicationConfig({ providers: [{ provide: ObservatoryStore, useValue: analyticsStore('error', 'The analytics query is unavailable.') }] })] };
export const AnalyticsNarrow: AnalyticsStory = { name: 'Analytics · narrow screen', component: ObservatoryAnalyticsComponent, parameters: { viewport: { defaultViewport: 'mobile1' } }, decorators: [applicationConfig({ providers: [{ provide: ObservatoryStore, useValue: analyticsStore('ready') }] })] };
