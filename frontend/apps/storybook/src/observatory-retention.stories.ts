import { applicationConfig, componentWrapperDecorator } from '@storybook/angular';
import type { Meta, StoryObj } from '@storybook/angular';
import { signal } from '@angular/core';
import { ObservatoryRetentionComponent, ObservatoryStore, type ObservatorySnapshot, type RetentionState } from '@aura/observatory';

const observedAt = '2026-10-15T09:00:00.000Z';
const snapshot: ObservatorySnapshot = { overview: { observedAt, systemState: 'healthy', observationCount: 128, runCount: 12, latestObservationId: 'obs-128', ingest: { state: 'healthy', counts: { accepted: 128, rejected: 1, duplicate: 3, malformed: 0, storageFailure: 0 }, lastAcceptedAt: observedAt, lastFailureAt: null } }, runs: null, components: null, deployments: null, state: 'ready', lastUpdatedAt: observedAt, lastEventId: 'obs-128', error: null, partial: false, resourceErrors: [] };
function retentionStore(retention: RetentionState): ObservatoryStore {
  return { retention: signal(retention), snapshot: signal(snapshot), createRetention: async () => undefined } as unknown as ObservatoryStore;
}

const meta: Meta<ObservatoryRetentionComponent> = {
  title: 'Observatory/Retention states',
  component: ObservatoryRetentionComponent,
  decorators: [componentWrapperDecorator((story) => `<div style="--surface:#fff;--surface-raised:#faf7f8;--surface-subtle:#f3eef0;--text:#241d21;--text-muted:#756970;--border:#dfd4d8;--control-border:#b9aab0;--accent:#7a3b58;--on-accent:#fff;--danger:#a52828;--danger-surface:#fde8e8;--danger-text:#8d1f1f;--info-surface:#e8f2fb;--info-text:#16466d;--focus:#315d9a;padding:24px;max-width:720px">${story}</div>`)],
};
export default meta;
type Story = StoryObj<ObservatoryRetentionComponent>;

export const RetentionProgress: Story = { name: 'Deletion in progress', decorators: [applicationConfig({ providers: [{ provide: ObservatoryStore, useValue: retentionStore({ action: null, running: true, error: null }) }] })] };
export const RetentionFailure: Story = { name: 'Deletion failed with recovery guidance', decorators: [applicationConfig({ providers: [{ provide: ObservatoryStore, useValue: retentionStore({ action: null, running: false, error: 'storage_failure' }) }] })] };
