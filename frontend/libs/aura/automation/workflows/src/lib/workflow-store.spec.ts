import { describe, expect, it } from 'vitest';
import { isValidWorkflowStreamEvent, mergeWorkflowInvalidation, shouldCommitWorkflowAgentLoad, visibleWorkflowInvalidation, WorkflowStreamError } from './workflow-models';

const definitionEvent = { schemaVersion: 1, eventId: '11111111-1111-4111-8111-111111111111', eventType: 'workflow.definition.retired' as const, workflowId: '22222222-2222-4222-8222-222222222222', revisionId: null, version: 3, occurredAt: '2026-10-28T00:00:00Z' };

describe('Workflow live metadata behavior', () => {
  it('accepts only closed-schema lifecycle events before cursor advancement', () => {
    expect(isValidWorkflowStreamEvent(definitionEvent)).toBe(true);
    expect(isValidWorkflowStreamEvent({ ...definitionEvent, schemaVersion: 2 })).toBe(false);
    expect(isValidWorkflowStreamEvent({ ...definitionEvent, eventType: 'workflow.unknown' })).toBe(false);
    expect(isValidWorkflowStreamEvent({ ...definitionEvent, eventId: '' })).toBe(false);
    expect(isValidWorkflowStreamEvent({ ...definitionEvent, version: 0 })).toBe(false);
  });

  it('requires the contract heartbeat schema and rejects private fields', () => {
    expect(isValidWorkflowStreamEvent({ schemaVersion: 1, eventType: 'heartbeat', occurredAt: '2026-10-28T00:00:00Z' })).toBe(true);
    expect(isValidWorkflowStreamEvent({ eventType: 'heartbeat', occurredAt: '2026-10-28T00:00:00Z' })).toBe(false);
    expect(isValidWorkflowStreamEvent({ schemaVersion: 1, eventType: 'heartbeat', occurredAt: '2026-10-28T00:00:00Z', workflowId: 'private' })).toBe(false);
  });

  it('requires UUID lineage, RFC3339 timestamps, and bounded unique recovery capabilities', () => {
    const stepEvent = { schemaVersion: 1, eventId: '11111111-1111-4111-8111-111111111111', eventType: 'workflow.step.reconciled' as const, workflowId: '22222222-2222-4222-8222-222222222222', revisionId: '33333333-3333-4333-8333-333333333333', instanceId: '44444444-4444-4444-8444-444444444444', stepExecutionId: '55555555-5555-4555-8555-555555555555', stepOrdinal: 1, stepKind: 'owner_approval' as const, stepStatus: 'resolution_required' as const, effectOutcome: 'unknown' as const, recoveryCapabilities: ['reconcile'] as const, instanceVersion: 1, occurredAt: '2026-10-28T00:00:00Z' };
    expect(isValidWorkflowStreamEvent(stepEvent)).toBe(true);
    expect(isValidWorkflowStreamEvent({ ...stepEvent, workflowId: 'workflow-1' })).toBe(false);
    expect(isValidWorkflowStreamEvent({ ...stepEvent, occurredAt: '2026-10-28' })).toBe(false);
    expect(isValidWorkflowStreamEvent({ ...stepEvent, occurredAt: '2026-10-28T00:00:00+0100' })).toBe(false);
    expect(isValidWorkflowStreamEvent({ ...stepEvent, occurredAt: '2026-02-30T00:00:00Z' })).toBe(false);
    expect(isValidWorkflowStreamEvent({ ...stepEvent, occurredAt: '2026-13-01T00:00:00Z' })).toBe(false);
    expect(isValidWorkflowStreamEvent({ ...stepEvent, occurredAt: '2026-10-28T25:00:00Z' })).toBe(false);
    expect(isValidWorkflowStreamEvent({ ...stepEvent, occurredAt: '2026-10-28T00:00:00+24:00' })).toBe(false);
    expect(isValidWorkflowStreamEvent({ ...stepEvent, occurredAt: '2026-10-28T00:00:00+01:60' })).toBe(false);
    expect(isValidWorkflowStreamEvent({ ...stepEvent, occurredAt: '2026-10-28T00:00:00+01:00' })).toBe(true);
    expect(isValidWorkflowStreamEvent({ ...stepEvent, recoveryCapabilities: ['reconcile', 'reconcile'] })).toBe(false);
  });

  it('accumulates selected-instance lineage across unrelated events and keeps the latest cursor pending until refresh succeeds', () => {
    const first = mergeWorkflowInvalidation(null, { refreshList: true, refreshWorkflow: false, refreshInstance: false }, 'event-1');
    const selected = mergeWorkflowInvalidation(first, { refreshList: true, refreshWorkflow: true, refreshInstance: true }, 'event-2');
    const unrelated = mergeWorkflowInvalidation(selected, { refreshList: true, refreshWorkflow: false, refreshInstance: false }, 'event-3');
    expect(unrelated).toEqual({ refreshList: true, refreshWorkflow: true, refreshInstance: true, cursor: 'event-3' });
  });

  it('marks direct cursor recovery as a full visible projection refresh', () => {
    expect({ ...visibleWorkflowInvalidation(true, true), cursor: null }).toEqual({ refreshList: true, refreshWorkflow: true, refreshInstance: true, cursor: null });
  });

  it('keeps HTTP, EOF, protocol, and overflow failures typed for bounded recovery', () => {
    expect(new WorkflowStreamError(410, 'cursor expired', 'http')).toMatchObject({ status: 410, reason: 'http' });
    expect(new WorkflowStreamError(0, 'clean EOF', 'eof').reason).toBe('eof');
    expect(new WorkflowStreamError(0, 'oversized event', 'overflow').reason).toBe('overflow');
  });

  it('rejects delayed agent responses from a reset epoch and stale reload generation', () => {
    expect(shouldCommitWorkflowAgentLoad(0, 1, 1, 2, false)).toBe(false);
    expect(shouldCommitWorkflowAgentLoad(1, 1, 1, 2, true)).toBe(false);
    expect(shouldCommitWorkflowAgentLoad(1, 1, 2, 2, true)).toBe(true);
  });
});
