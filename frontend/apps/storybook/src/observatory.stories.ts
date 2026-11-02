import { applicationConfig, componentWrapperDecorator } from '@storybook/angular';
import type { Meta, StoryObj } from '@storybook/angular';
import { signal } from '@angular/core';
import { ObservatoryOverviewComponent, ObservatoryStore, type ObservatorySnapshot } from '@aura/observatory';

const observedAt = '2026-10-15T09:00:00.000Z';
const healthyOverview = { observedAt, systemState: 'healthy' as const, observationCount: 128, runCount: 12, latestObservationId: 'obs-128', ingest: { state: 'healthy' as const, counts: { accepted: 128, rejected: 1, duplicate: 3, malformed: 0, storageFailure: 0 }, lastAcceptedAt: observedAt, lastFailureAt: null } };
const baseSnapshot: ObservatorySnapshot = { overview: healthyOverview, runs: null, components: null, deployments: null, state: 'ready', lastUpdatedAt: observedAt, lastEventId: 'obs-128', error: null, partial: false, resourceErrors: [] };
function fixtureStore(snapshot: ObservatorySnapshot, sessionRequired = false): ObservatoryStore { return { snapshot: signal(snapshot), sessionRequired: signal(sessionRequired), load: async () => undefined } as unknown as ObservatoryStore; }
const meta: Meta<ObservatoryOverviewComponent> = { title: 'Observatory/Overview states', component: ObservatoryOverviewComponent, decorators: [componentWrapperDecorator((story) => `<div style="display:block;max-width:900px;padding:24px;background:#fcf9fa">${story}</div>`)] };
export default meta;
type Story = StoryObj<ObservatoryOverviewComponent>;
export const Loading: Story = { name: 'Loading', decorators: [applicationConfig({ providers: [{ provide: ObservatoryStore, useValue: fixtureStore({ ...baseSnapshot, overview: null, state: 'loading' }) }] })] };
export const Healthy: Story = { name: 'Healthy snapshot', decorators: [applicationConfig({ providers: [{ provide: ObservatoryStore, useValue: fixtureStore(baseSnapshot) }] })] };
export const Partial: Story = { name: 'Partial rejected data', decorators: [applicationConfig({ providers: [{ provide: ObservatoryStore, useValue: fixtureStore({ ...baseSnapshot, state: 'stale', partial: true, resourceErrors: ['components'], error: 'Some Observatory data is unavailable: components.' }) }] })] };
export const Disconnected: Story = { name: 'Disconnected live stream', decorators: [applicationConfig({ providers: [{ provide: ObservatoryStore, useValue: fixtureStore({ ...baseSnapshot, state: 'disconnected', error: 'Live updates are disconnected. Reconnecting…' }) }] })] };
export const Empty: Story = { name: 'Empty metadata snapshot', decorators: [applicationConfig({ providers: [{ provide: ObservatoryStore, useValue: fixtureStore({ ...baseSnapshot, overview: { ...healthyOverview, observationCount: 0, runCount: 0, latestObservationId: null }, state: 'empty' }) }] })] };
export const Stale: Story = { name: 'Stale snapshot', decorators: [applicationConfig({ providers: [{ provide: ObservatoryStore, useValue: fixtureStore({ ...baseSnapshot, state: 'stale', error: 'Showing the last known snapshot; the latest refresh is overdue.' }) }] })] };
export const ServiceUnavailable: Story = { name: 'Service unavailable', decorators: [applicationConfig({ providers: [{ provide: ObservatoryStore, useValue: fixtureStore({ ...baseSnapshot, overview: null, state: 'error', error: 'Observatory is temporarily unavailable. Retry shortly.' }) }] })] };
export const Unavailable: Story = { name: 'Owner session required', decorators: [applicationConfig({ providers: [{ provide: ObservatoryStore, useValue: fixtureStore({ ...baseSnapshot, overview: null, state: 'error', error: 'Owner Observatory session required.' }, true) }] })] };
