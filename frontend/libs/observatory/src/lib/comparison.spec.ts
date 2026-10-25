import { describe, expect, it } from 'vitest';
import { parseRunComparison } from './observatory-models';

const marker = (value: string | null, availability: 'available' | 'missing' | 'unavailable' = value ? 'available' : 'missing') => ({ availability, value });
const span = (side: 'left' | 'right') => ({ spanId: side === 'left' ? 'aaaaaaaaaaaaaaaa' : 'bbbbbbbbbbbbbbbb', parentSpanId: null, componentId: 'aura.core.api', componentVersion: 'v1', operation: 'request', startedAt: '2026-10-18T09:00:00.000Z', durationMs: side === 'left' ? 100 : 120, outcome: 'succeeded', partial: false });
const comparison = {
  snapshotAt: '2026-10-18T09:00:00.000Z',
  left: { role: 'left', runId: '00000000-0000-4000-8000-000000000001', evidence: 'available', status: 'succeeded', outcome: 'succeeded', startedAt: '2026-10-18T09:00:00.000Z', finishedAt: '2026-10-18T09:00:01.000Z', durationMs: 1000, traceId: '0123456789abcdef0123456789abcdef', modelId: marker('model-a'), modelVersion: marker('v1'), deploymentId: marker('deploy-a'), gitCommit: marker('commit-a'), imageVersion: marker('image-a'), configurationVersion: marker('config-a'), agentRevision: marker('agent-a'), promptBundleRevision: marker('bundle-a'), policyRevision: marker('policy-a'), componentVersions: [{ componentId: 'aura.core.api', version: marker('v1') }], partial: false },
  right: { role: 'right', runId: '00000000-0000-4000-8000-000000000002', evidence: 'available', status: 'succeeded', outcome: 'failed', startedAt: '2026-10-18T09:01:00.000Z', finishedAt: '2026-10-18T09:01:01.000Z', durationMs: 1000, traceId: '1123456789abcdef0123456789abcdef', modelId: marker('model-b'), modelVersion: marker('v1'), deploymentId: marker('deploy-b'), gitCommit: marker('commit-b'), imageVersion: marker('image-b'), configurationVersion: marker('config-b'), agentRevision: marker('agent-b'), promptBundleRevision: marker('bundle-b'), policyRevision: marker('policy-b'), componentVersions: [{ componentId: 'aura.core.api', version: marker('v2') }], partial: false },
  markerCorrelations: [{ marker: 'deployment_id', state: 'changed', left: marker('deploy-a'), right: marker('deploy-b') }],
  components: [{ componentId: 'aura.core.api', operation: 'request', alignment: 'matched', left: { componentVersion: marker('v1'), durationMs: 100, outcome: 'succeeded', spanCount: 1, partial: false }, right: { componentVersion: marker('v2'), durationMs: 120, outcome: 'failed', spanCount: 1, partial: false }, measurements: [{ name: 'duration_ms', unit: 'milliseconds', state: 'changed', left: { availability: 'available', value: 100 }, right: { availability: 'available', value: 120 }, absoluteDelta: 20 }] }],
  trace: { leftTraceId: '0123456789abcdef0123456789abcdef', rightTraceId: '1123456789abcdef0123456789abcdef', leftEvidence: 'available', rightEvidence: 'available', leftIntegrity: ['complete'], rightIntegrity: ['complete'], alignedSpans: [{ alignmentKey: 'aura.core.api:request:0', alignment: 'matched', left: span('left'), right: span('right'), leftIntegrity: ['complete'], rightIntegrity: ['complete'] }], partial: false },
  freshness: { latestObservedAt: '2026-10-18T09:01:01.000Z', lagMs: 10, state: 'fresh' }, partial: false, partialReasons: [], unavailableMarkerCount: 0,
  scan: { scannedRows: 2, scanLimit: 1000000, executionMs: 4, executionLimitMs: 5000 }, limits: { runCount: 2, maximumObservationsPerRun: 10000, maximumSpansPerRun: 5000, maximumMeasurements: 2000, maximumScannedRows: 1000000, executionTimeoutMs: 5000 }, capturePolicy: 'metadata_only',
};

describe('run comparison response boundary', () => {
  it('accepts bounded metadata and keeps zero measurements valid', () => {
    expect(parseRunComparison(comparison)).not.toBeNull();
    const withZero = { ...comparison, components: [{ ...comparison.components[0], measurements: [{ ...comparison.components[0].measurements[0], left: { availability: 'available', value: 0 }, absoluteDelta: 120 }] }] };
    expect(parseRunComparison(withZero)).not.toBeNull();
  });

  it('rejects unknown fields and content-like marker values before rendering', () => {
    expect(parseRunComparison({ ...comparison, left: { ...comparison.left, prompt: 'private content' } })).toBeNull();
    expect(parseRunComparison({ ...comparison, markerCorrelations: [{ ...comparison.markerCorrelations[0], left: marker('private value with spaces') }] })).toBeNull();
    expect(parseRunComparison({ ...comparison, trace: { ...comparison.trace, alignedSpans: [{ ...comparison.trace.alignedSpans[0], leftIntegrity: ['unexpected'] }] } })).toBeNull();
  });

  it('preserves explicit missing and unavailable evidence', () => {
    const missing = { ...comparison, left: { ...comparison.left, policyRevision: marker(null, 'unavailable') }, unavailableMarkerCount: 1, markerCorrelations: [{ marker: 'policy_revision', state: 'unavailable', left: marker(null, 'unavailable'), right: marker('policy-b') }] };
    expect(parseRunComparison(missing)).not.toBeNull();
  });

  it('accepts tagged model identifiers and rejects private or URL-shaped values', () => {
    expect(parseRunComparison({ ...comparison, left: { ...comparison.left, modelId: marker('qwen3:8b') } })).not.toBeNull();
    expect(parseRunComparison({ ...comparison, left: { ...comparison.left, modelId: marker('https://models.invalid/qwen3') } })).toBeNull();
    expect(parseRunComparison({ ...comparison, left: { ...comparison.left, modelId: marker('tenant:token') } })).toBeNull();
    expect(parseRunComparison({ ...comparison, left: { ...comparison.left, policyRevision: marker('release.Content.v1') } })).toBeNull();
    expect(parseRunComparison({ ...comparison, left: { ...comparison.left, policyRevision: marker('release.contenthash.v1') } })).not.toBeNull();
  });

  it('enforces numeric, unit, span-count, freshness, and unique-integrity bounds', () => {
    const sourceComponent = comparison.components[0];
    const sourceLeft = sourceComponent.left;
    const sourceMeasurement = sourceComponent.measurements[0];
    if (!sourceLeft || !sourceMeasurement) throw new Error('comparison fixture is incomplete');
    const bounded = { ...comparison, components: [{ ...sourceComponent, left: { ...sourceLeft, spanCount: 5000 }, measurements: [{ ...sourceMeasurement, unit: 'ratio', left: { availability: 'available', value: 1_000_000_000_000_000 }, absoluteDelta: 2_000_000_000_000_000 }] }] };
    expect(parseRunComparison(bounded)).not.toBeNull();
    const boundedComponent = bounded.components[0];
    const boundedMeasurement = boundedComponent.measurements[0];
    if (!boundedComponent.left || !boundedMeasurement) throw new Error('bounded comparison fixture is incomplete');
    expect(parseRunComparison({ ...bounded, components: [{ ...boundedComponent, measurements: [{ ...boundedMeasurement, unit: 'seconds' }] }] })).toBeNull();
    expect(parseRunComparison({ ...bounded, components: [{ ...boundedComponent, left: { ...boundedComponent.left, spanCount: 5001 } }] })).toBeNull();
    expect(parseRunComparison({ ...bounded, components: [{ ...boundedComponent, measurements: [{ ...boundedMeasurement, left: { availability: 'available', value: 1_000_000_000_000_001 } }] }] })).toBeNull();
    expect(parseRunComparison({ ...bounded, trace: { ...bounded.trace, leftIntegrity: ['complete', 'complete'] } })).toBeNull();
    expect(parseRunComparison({ ...bounded, freshness: { ...bounded.freshness, lagMs: 604_800_001 } })).toBeNull();
    expect(parseRunComparison({ ...bounded, partial: true, partialReasons: ['late_data', 'late_data'] })).toBeNull();
  });

  it('accepts the OpenAPI-aligned trace and key bounds, including duplicate keys', () => {
    const aligned = comparison.trace.alignedSpans[0];
    const key = 'a'.repeat(512);
    const spans = Array.from({ length: 10_000 }, (_, index) => ({ ...aligned, alignmentKey: index < 2 ? 'duplicate-key' : key, left: null, right: null }));
    expect(parseRunComparison({ ...comparison, trace: { ...comparison.trace, alignedSpans: spans } })).not.toBeNull();
    expect(parseRunComparison({ ...comparison, trace: { ...comparison.trace, alignedSpans: [...spans, aligned] } })).toBeNull();
    expect(parseRunComparison({ ...comparison, trace: { ...comparison.trace, alignedSpans: [{ ...aligned, alignmentKey: `${key}a` }] } })).toBeNull();
  });
});
