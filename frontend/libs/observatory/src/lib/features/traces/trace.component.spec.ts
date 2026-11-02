import { describe, expect, it, vi } from 'vitest';
import { durationAriaLabel, formatDurationMs } from '../../observatory-models';
import { aggregateInspectionSteps, closeOpenEvidenceDetails, continuationSummary, foregroundPhaseItemsFor, hasParallelBranch, inspectionStepPosition, inspectionStepSetSize, isAtOrBeforeBoundary, lineageLabel, orderPhases, phaseFor, phaseItemsFor, revealBatch, spanLineageLabel, stageOutcomeCounts, stageSummaryLabel, terminalLifecycleAt, terminalObservationBoundary, traceSpanPosition, traceSpanSetSize, type PhaseItem, type PhaseKey } from './trace-inspector.utils';

describe('run inspector presentation', () => {
  it('formats stable durations into bounded readable units', () => {
    expect(formatDurationMs(null)).toBe('Unavailable');
    expect(formatDurationMs(842)).toBe('842 ms');
    expect(formatDurationMs(1_250)).toBe('1.25 s');
    expect(formatDurationMs(12_500)).toBe('12.5 s');
    expect(formatDurationMs(65_100)).toBe('1m 05s');
    expect(formatDurationMs(3_661_000)).toBe('1h 01m');
  });

  it('aggregates repeat delivery before bounding so terminal failure remains visible', () => {
    const base = { parentStepId: null, spanId: null, parentSpanId: null, category: 'thinking' as const, label: 'thinking stream', startedAt: '2026-10-25T12:00:00.000Z', finishedAt: null, durationMs: null, depth: 0, measurements: [], evidence: [], partial: false, partialReasons: [] };
    const steps = Array.from({ length: 205 }, (_, index) => ({ ...base, stepId: 'repeat-0', sequence: index, state: index === 204 ? 'failed' as const : 'running' as const }));
    const aggregated = aggregateInspectionSteps(steps, 1);
    expect(aggregated).toHaveLength(1);
    expect(aggregated[0].state).toBe('failed');
  });

  it('continues through the final loaded step, span, and measurement record after the initial batch', () => {
    const steps = Array.from({ length: 205 }, (_, index) => ({ stepId: `step-${index}`, sequence: index, value: index }));
    const spans = Array.from({ length: 205 }, (_, index) => ({ spanId: `span-${index}`, value: index }));
    const measurements = Array.from({ length: 128 }, (_, index) => ({ name: `measurement-${index}`, value: index }));
    expect(revealBatch(steps, 20)).toHaveLength(20);
    expect(revealBatch(steps, 220).at(-1)?.stepId).toBe('step-204');
    expect(revealBatch(spans, 20)).toHaveLength(20);
    expect(revealBatch(spans, 220).at(-1)?.spanId).toBe('span-204');
    expect(revealBatch(measurements, 20)).toHaveLength(20);
    expect(revealBatch(measurements, 128).at(-1)?.name).toBe('measurement-127');
  });

  it('keeps exact timing available to assistive technology', () => {
    expect(durationAriaLabel(1_251.4)).toBe('1251.4 milliseconds');
    expect(durationAriaLabel(null)).toBe('Duration unavailable');
  });

  it('classifies tool execution before the request fallback', () => {
    expect(phaseFor('aura.tools tool.execute')).toBe('tools');
    expect(phaseFor('aura.execution run.execute')).toBe('request');
    expect(phaseFor('aura.memory memory.persist')).toBe('persistence');
    expect(phaseFor('aura.memory memory.retrieval')).toBe('context');
    expect(phaseFor('aura.model model.route')).toBe('routing');
  });

  it('exposes parent lineage and parallel branch semantics for the inspector', () => {
    expect(lineageLabel('step-parent', null)).toBe('parent step step-parent');
    expect(lineageLabel(null, 'span-parent')).toBe('parent span span-parent');
    expect(lineageLabel(null, null)).toBe('root branch');
    expect(spanLineageLabel('span-parent')).toBe('parent span-parent');
    expect(spanLineageLabel(null)).toBe('root span');
    expect(hasParallelBranch('span-parent', ['span-parent', 'span-parent', 'other'])).toBe(true);
    expect(hasParallelBranch('span-parent', ['span-parent', 'other'])).toBe(false);
  });

  it('computes inspection tree positions among root and nested siblings', () => {
    const step = (stepId: string, depth: number, parentStepId: string | null = null) => ({ stepId, sequence: 0, parentStepId, parentSpanId: null, category: 'request' as const, label: stepId, state: 'succeeded' as const, startedAt: '2026-10-25T12:00:00.000Z', finishedAt: null, durationMs: 1, depth, measurements: [], evidence: [], partial: false, partialReasons: [] });
    const rootA = step('root-a', 0); const rootB = step('root-b', 0); const childA = step('child-a', 1, 'root-a'); const childB = step('child-b', 1, 'root-a'); const childOther = step('child-other', 1, 'root-b');
    const steps = [rootA, rootB, childA, childB, childOther];
    expect([inspectionStepPosition(rootA, steps), inspectionStepSetSize(rootA, steps)]).toEqual([1, 2]);
    expect([inspectionStepPosition(rootB, steps), inspectionStepSetSize(rootB, steps)]).toEqual([2, 2]);
    expect([inspectionStepPosition(childA, steps), inspectionStepSetSize(childA, steps)]).toEqual([1, 2]);
    expect([inspectionStepPosition(childB, steps), inspectionStepSetSize(childB, steps)]).toEqual([2, 2]);
    expect([inspectionStepPosition(childOther, steps), inspectionStepSetSize(childOther, steps)]).toEqual([1, 1]);
  });

  it('computes trace tree positions for parallel spans instead of global indexes', () => {
    const span = (spanId: string, depth: number, parentSpanId: string | null = null) => ({ spanId, parentSpanId, depth, startedAt: '2026-10-25T12:00:00.000Z', durationMs: 1, componentId: 'aura.test', componentVersion: 'v1', operation: spanId, outcome: 'succeeded' as const, partial: false });
    const rootA = span('root-a', 0); const rootB = span('root-b', 0); const childA = span('child-a', 1, 'root-a'); const childB = span('child-b', 1, 'root-a'); const childOther = span('child-other', 1, 'root-b');
    const spans = [rootA, rootB, childA, childB, childOther];
    expect([traceSpanPosition(rootA, spans), traceSpanSetSize(rootA, spans)]).toEqual([1, 2]);
    expect([traceSpanPosition(rootB, spans), traceSpanSetSize(rootB, spans)]).toEqual([2, 2]);
    expect([traceSpanPosition(childA, spans), traceSpanSetSize(childA, spans)]).toEqual([1, 2]);
    expect([traceSpanPosition(childB, spans), traceSpanSetSize(childB, spans)]).toEqual([2, 2]);
    expect([traceSpanPosition(childOther, spans), traceSpanSetSize(childOther, spans)]).toEqual([1, 1]);
  });

  it('closes nested evidence disclosures before releasing their payloads', () => {
    const first = { removeAttribute: vi.fn() };
    const second = { removeAttribute: vi.fn() };
    closeOpenEvidenceDetails({ querySelectorAll: () => [first, second] });
    expect(first.removeAttribute).toHaveBeenCalledWith('open');
    expect(second.removeAttribute).toHaveBeenCalledWith('open');
  });

  it('keeps stage outcome facets independent', () => {
    expect(stageOutcomeCounts(['failed', 'running', 'cancelled', 'unavailable'], [false, true, false, true])).toEqual({ failed: 1, running: 1, partial: 2, cancelled: 1, unavailable: 1 });
  });

  it('qualifies stage highlights as longest operations', () => {
    expect(stageSummaryLabel('succeeded', 1_250)).toBe('succeeded · longest operation 1.25 s');
    expect(stageSummaryLabel('unavailable', null)).toBe('unavailable · longest operation unavailable');
  });


  it('counts one canonical phase record when a span mirrors its observation', () => {
    const base: PhaseItem = {
      id: 'span-1', label: 'tool.execute', operation: 'tool.execute', componentId: 'aura.tools', component: 'aura.tools', outcome: 'succeeded', startedAt: '2026-10-25T12:00:00.000Z', durationMs: 42, measurementValue: null, measurementUnit: null, partial: false, kind: 'span',
    };
    const mirroredObservation: PhaseItem = { ...base, id: 'measurement-1', label: 'duration_ms', component: 'aura.tools · worker', kind: 'measurement' };
    const sameTimestampDifferentDuration: PhaseItem = { ...mirroredObservation, id: 'measurement-3', durationMs: 43, measurementValue: 43, measurementUnit: 'milliseconds' };
    const separateObservation: PhaseItem = { ...base, id: 'measurement-2', label: 'tool_policy_decision', operation: 'tool.policy', durationMs: null, kind: 'measurement' };

    expect(phaseItemsFor('tools', [base], [mirroredObservation, separateObservation]).map((item) => item.id).sort()).toEqual(['measurement-2', 'span-1']);
    expect(phaseItemsFor('tools', [base], [sameTimestampDifferentDuration]).map((item) => item.id).sort()).toEqual(['measurement-3', 'span-1']);
  });

  it('keeps canonical lifecycle order within observed and unobserved groups', () => {
    const phases = ['request', 'context', 'prompt', 'routing', 'generation', 'thinking', 'tools', 'persistence', 'delivery', 'other'].map((key) => ({ key: key as PhaseKey }));
    const ordered = orderPhases(phases, new Set<PhaseKey>(['persistence', 'request']));
    expect(ordered.map((phase) => phase.key)).toEqual(['request', 'persistence', 'context', 'prompt', 'routing', 'generation', 'thinking', 'tools', 'delivery', 'other']);
  });

  it('bounds the foreground timeline at the terminal lifecycle observation', () => {
    const lifecycle = [
      { label: 'run_started', startedAt: '2026-10-25T12:00:00.000Z', outcome: 'succeeded' },
      { label: 'run_status', startedAt: '2026-10-25T12:00:02.600Z', outcome: 'succeeded' },
      { label: 'memory_retry_count', startedAt: '2026-10-25T14:00:00.000Z', outcome: 'failed' },
    ];
    const boundary = terminalLifecycleAt(lifecycle);
    expect(boundary).toBe('2026-10-25T12:00:02.600Z');
    expect(isAtOrBeforeBoundary('2026-10-25T12:00:02.600Z', boundary)).toBe(true);
    expect(isAtOrBeforeBoundary('2026-10-25T14:00:00.000Z', boundary)).toBe(false);
  });

  it('summarizes late correlated metadata without expanding it into request steps', () => {
    const summary = continuationSummary([
      { label: 'run_status', startedAt: '2026-10-25T12:00:02.600Z', outcome: 'succeeded' },
      { label: 'memory_retry_count', startedAt: '2026-10-25T12:01:00.000Z', outcome: 'failed' },
      { label: 'memory_retry_count', startedAt: '2026-10-25T12:02:00.000Z', outcome: 'failed' },
      { label: 'stream_delivery', startedAt: '2026-10-25T12:03:00.000Z', outcome: 'succeeded' },
    ], '2026-10-25T12:00:02.600Z');
    expect(summary).toEqual({ count: 3, startedAt: '2026-10-25T12:01:00.000Z', endedAt: '2026-10-25T12:03:00.000Z', outcomes: ['failed', 'succeeded'], partial: false });
    expect(continuationSummary([
      { label: 'memory_retry_count', startedAt: '2026-10-25T12:01:00.000Z', outcome: 'failed' },
    ], '2026-10-25T12:00:02.600Z', true)).toMatchObject({ count: 1, partial: true });
    expect(continuationSummary([], '2026-10-25T12:00:02.600Z', true)).toEqual({ count: 0, startedAt: null, endedAt: null, outcomes: [], partial: true });
  });

  it('uses a finishedAt fallback only when a terminal status observation is absent', () => {
    expect(terminalLifecycleAt([{ label: 'run_status', startedAt: '2026-10-25T12:00:01.000Z', outcome: 'running' }], '2026-10-25T12:00:02.000Z')).toBe('2026-10-25T12:00:02.000Z');
    expect(terminalLifecycleAt([{ label: 'run_status', startedAt: '2026-10-25T12:00:01.000Z', outcome: 'succeeded' }], '2026-10-25T12:00:02.000Z')).toBe('2026-10-25T12:00:01.000Z');
  });

  it('keeps a duration observation published after terminal status inside the detail boundary', () => {
    const observations = [
      { label: 'run_status', startedAt: '2026-10-25T12:00:02.000Z', outcome: 'succeeded' },
      { label: 'run_duration_ms', startedAt: '2026-10-25T12:00:02.100Z', outcome: 'succeeded' },
    ];
    const boundary = terminalObservationBoundary(observations);
    expect(boundary).toBe('2026-10-25T12:00:02.100Z');
    expect(isAtOrBeforeBoundary('2026-10-25T12:00:02.100Z', boundary)).toBe(true);
    expect(isAtOrBeforeBoundary('2026-10-25T12:00:02.101Z', boundary)).toBe(false);
  });

  it('keeps a selected primary run span foreground while fencing a late worker observation', () => {
    const boundary = '2026-10-25T12:00:02.100Z';
    const runSpan: PhaseItem = {
      id: 'span-run-execute', label: 'run.execute', operation: 'run.execute', componentId: 'aura.execution', component: 'aura.execution', outcome: 'succeeded', startedAt: '2026-10-25T12:00:02.200Z', durationMs: 5000, measurementValue: null, measurementUnit: null, partial: false, kind: 'span',
    };
    const lateWorkerObservation: PhaseItem = {
      id: 'measurement-memory-retry', label: 'memory_retry_count', operation: 'memory.retry', componentId: 'aura.core.worker', component: 'aura.core.worker', outcome: 'failed', startedAt: '2026-10-25T12:01:00.000Z', durationMs: null, measurementValue: 1, measurementUnit: 'count', partial: false, kind: 'measurement',
    };
    expect(foregroundPhaseItemsFor('request', [runSpan], [], boundary).map((item) => item.id)).toEqual(['span-run-execute']);
    expect(foregroundPhaseItemsFor('context', [], [lateWorkerObservation], boundary)).toEqual([]);
    expect(continuationSummary([{ label: lateWorkerObservation.label, startedAt: lateWorkerObservation.startedAt, outcome: lateWorkerObservation.outcome }], boundary)).toMatchObject({ count: 1, startedAt: lateWorkerObservation.startedAt });
  });
});
