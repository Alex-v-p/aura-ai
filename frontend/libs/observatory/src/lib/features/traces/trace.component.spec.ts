import { describe, expect, it } from 'vitest';
import { durationAriaLabel, formatDurationMs } from '../../observatory-models';
import { orderPhases, phaseFor, phaseItemsFor, type PhaseItem, type PhaseKey } from './trace-inspector.utils';

describe('run inspector presentation', () => {
  it('formats stable durations into bounded readable units', () => {
    expect(formatDurationMs(null)).toBe('Unavailable');
    expect(formatDurationMs(842)).toBe('842 ms');
    expect(formatDurationMs(1_250)).toBe('1.25 s');
    expect(formatDurationMs(12_500)).toBe('12.5 s');
    expect(formatDurationMs(65_100)).toBe('1m 05s');
    expect(formatDurationMs(3_661_000)).toBe('1h 01m');
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
});
