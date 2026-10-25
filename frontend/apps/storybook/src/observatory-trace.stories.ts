import { applicationConfig, componentWrapperDecorator } from '@storybook/angular';
import type { Meta, StoryObj } from '@storybook/angular';
import { signal } from '@angular/core';
import { ObservatoryStore, ObservatoryTraceComponent, type SelectedRun } from '@aura/observatory';
import type { RunDetail, TraceDetail } from '@aura/observatory-api-client';

const partialRun = { runId: 'run-partial', status: 'succeeded', startedAt: '2026-10-15T09:00:00.000Z', finishedAt: '2026-10-15T09:00:01.000Z', durationMs: 1000, model: 'model-safe', deploymentId: 'deploy-local', componentIds: ['api', 'worker'], traceId: 'trace-partial', observations: [], partial: false, nextCursor: null } as unknown as RunDetail;
const partialTrace = { traceId: 'trace-partial', runId: 'run-partial', partial: true, nextCursor: null, spans: [{ spanId: 'root', parentSpanId: null, componentId: 'api', operation: 'request', startedAt: '2026-10-15T09:00:00.000Z', durationMs: 1000, outcome: 'succeeded', depth: 0, partial: false }, { spanId: 'child', parentSpanId: 'missing-parent', componentId: 'worker', operation: 'dispatch', startedAt: '2026-10-15T09:00:00.100Z', durationMs: 300, outcome: 'failed', depth: 1, partial: true }] } as unknown as TraceDetail;
function traceStore(selectedRun: SelectedRun, state: 'ready' | 'error', error: string | null = null): ObservatoryStore {
  return { selectedRun: signal(selectedRun), selectedRunState: signal(state), selectedRunError: signal(error), selectedRunId: signal('run-partial'), openRun: async () => undefined } as unknown as ObservatoryStore;
}

const meta: Meta<ObservatoryTraceComponent> = {
  title: 'Observatory/Trace states',
  component: ObservatoryTraceComponent,
  decorators: [componentWrapperDecorator((story) => `<div style="--surface:#fff;--surface-subtle:#f3eef0;--text:#241d21;--text-muted:#756970;--border:#dfd4d8;--accent:#7a3b58;--accent-hover:#5c2941;--on-accent:#fff;--info-surface:#e8f2fb;--info-text:#16466d;--warning-surface:#fff4d6;--warning-text:#694d00;--danger-surface:#fde8e8;--danger-text:#8d1f1f;padding:24px;max-width:960px">${story}</div>`)],
};
export default meta;
type Story = StoryObj<ObservatoryTraceComponent>;

export const PartialTrace: Story = { name: 'Partial trace topology', decorators: [applicationConfig({ providers: [{ provide: ObservatoryStore, useValue: traceStore({ detail: partialRun, trace: partialTrace }, 'ready') }] })] };
export const TraceError: Story = { name: 'Trace unavailable with retry', decorators: [applicationConfig({ providers: [{ provide: ObservatoryStore, useValue: traceStore({ detail: null, trace: null }, 'error', 'The selected trace is unavailable. Retry when the service recovers.') }] })] };
