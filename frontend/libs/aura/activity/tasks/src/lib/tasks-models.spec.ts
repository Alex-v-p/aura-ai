import { describe, expect, it } from 'vitest';
import type { DurableTaskDetail } from '@aura/aura-api-client';
import { resolveTaskSource, taskSourceDetailExplanation, taskSourceLabel } from './tasks-models';

describe('task models', () => { it('normalizes source labels for the Task Center', () => { expect(taskSourceLabel('research')).toBe('Web research'); expect(taskSourceLabel('memory_reindex')).toBe('Memory reindex'); }); });

describe('Task Center source routing', () => {
  const detail = (sourceKind: DurableTaskDetail['sourceKind'], routeKey: DurableTaskDetail['routeKey'], hasSourceDetail: boolean, sourceId = '11111111-1111-4111-8111-111111111111'): DurableTaskDetail => ({ id: '22222222-2222-4222-8222-222222222222', sourceKind, sourceId, sourceVersion: 1, status: 'running', sourceStatus: 'running', sourceOutcome: null, stage: 'working', progress: null, capabilities: { canCancel: false, canRetry: false, hasSourceDetail }, routeKey, createdAt: '2026-10-30T00:00:00Z', updatedAt: '2026-10-30T00:00:00Z', displayLabel: null, failureClass: null, recoveryAction: null, artifactRefs: [] });

  it('constructs only the exact research and document routes', () => {
    expect(resolveTaskSource(detail('research', 'research_detail', true))?.commands).toEqual(['/research', '11111111-1111-4111-8111-111111111111']);
    expect(resolveTaskSource(detail('document_processing', 'document_detail', true))?.commands).toEqual(['/documents', '11111111-1111-4111-8111-111111111111']);
  });

  it('fails closed for unsupported or inconsistent capabilities', () => {
    expect(resolveTaskSource(detail('workflow_instance', 'workflow_instance_detail', true))).toBeNull();
    expect(resolveTaskSource(detail('research', 'research_detail', false))).toBeNull();
    expect(resolveTaskSource(detail('document_processing', 'research_detail', true))).toBeNull();
    expect(taskSourceDetailExplanation(detail('memory_reindex', 'memory_reindex_detail', false))).toContain('does not expose');
    expect(resolveTaskSource(detail('research', 'research_detail', true, 'research-1'))).toBeNull();
  });
});
