import { describe, expect, it } from 'vitest';
import { APPROVAL_TOOL_IDS, approvalActionLabel, approvalLeadCopy, appendApprovalPage, approvalStreamBlockWithinBounds, approvalStreamBufferWithinBounds, approvalSummaryFromDetail, collectApprovalHistory, createUuidV4, decisionFailureNotice, emptyApprovalCollection, isReminderCommandProposal, isValidApprovalEvent, reminderCommandConsequence, shouldPreserveReviewedApproval, type ApprovalDetail, type ApprovalSummary } from './approval-models';
import { ApprovalApiError } from './approval-models';
import { ApprovalSessionHydrator } from './approval-session-hydration';

const base: ApprovalSummary = { id: 'approval-1', proposalId: 'proposal-1', runId: 'run-1', conversationId: 'conversation-1', toolCallId: 'call-1', toolId: 'reminder.create', toolVersion: 1, status: 'pending', executionStatus: 'not_started', version: 1, createdAt: '2026-10-10T09:00:00.000Z', updatedAt: '2026-10-10T09:00:00.000Z', expiresAt: '2026-10-10T10:00:00.000Z', decidedAt: null };

describe('approval workspace', () => {
  it('counts pending approvals while appending replay pages', () => {
    const current = { ...emptyApprovalCollection(), items: [base], pendingCount: 1 };
    const next = { items: [{ ...base, id: 'approval-2', status: 'approved' as const, executionStatus: 'completed' as const }], nextCursor: null };
    expect(appendApprovalPage(current, next)).toMatchObject({ items: [{ id: 'approval-1' }, { id: 'approval-2' }], pendingCount: 1 });
  });

  it('projects detail responses to summaries without leaking proposal content', () => {
    const summary = approvalSummaryFromDetail({ ...base, proposal: { id: base.proposalId, content: 'Private proposal content.', dueAt: '2026-10-11T09:00:00Z' } } as ApprovalDetail);
    expect(summary).toMatchObject(base);
    expect(Object.prototype.hasOwnProperty.call(summary, 'proposal')).toBe(false);
    expect(summary.version).toBe(base.version);
    expect(summary.id).toBe(base.id);
  });

  it('accepts only complete bounded approval stream events', () => {
    const base = { schemaVersion: 1, eventType: 'approval.pending', approvalId: '550e8400-e29b-41d4-a716-446655440001', proposalId: '550e8400-e29b-41d4-a716-446655440002', runId: '550e8400-e29b-41d4-a716-446655440003', conversationId: '550e8400-e29b-41d4-a716-446655440004', toolId: 'reminder.create', toolVersion: 1, status: 'pending', executionStatus: 'not_started', version: 1, occurredAt: '2026-10-10T09:00:00Z' } as const;
    const valid = { ...base, eventId: '550e8400-e29b-41d4-a716-446655440000' };
    expect(isValidApprovalEvent({ ...valid, eventId: 'approval-event-1' })).toBe(false);
    expect(isValidApprovalEvent(valid)).toBe(true);
    for (const key of ['approvalId', 'proposalId', 'runId', 'conversationId']) expect(isValidApprovalEvent({ ...valid, [key]: 'not-a-uuid' })).toBe(false);
    expect(isValidApprovalEvent({ ...valid, occurredAt: '2026/10/10 09:00:00' })).toBe(false);
    expect(isValidApprovalEvent({ ...valid, extra: true })).toBe(false);
  });

  it('accepts every supported reminder command in metadata-only events and rejects unknown tools', () => {
    const event = {
      schemaVersion: 1,
      eventId: '550e8400-e29b-41d4-a716-446655440000',
      eventType: 'approval.pending',
      approvalId: '550e8400-e29b-41d4-a716-446655440001',
      proposalId: '550e8400-e29b-41d4-a716-446655440002',
      runId: '550e8400-e29b-41d4-a716-446655440003',
      conversationId: '550e8400-e29b-41d4-a716-446655440004',
      toolVersion: 1,
      status: 'pending',
      executionStatus: 'not_started',
      version: 1,
      occurredAt: '2026-10-10T09:00:00Z',
    } as const;
    for (const toolId of APPROVAL_TOOL_IDS) expect(isValidApprovalEvent({ ...event, toolId })).toBe(true);
    expect(isValidApprovalEvent({ ...event, toolId: 'calendar.delete' })).toBe(false);
  });

  it('keeps typed command proposal details separate from private legacy content', () => {
    const detail = {
      ...base,
      toolId: 'reminder.series.create' as const,
      proposal: { id: 'proposal-1', operation: 'series.create' as const, summary: 'Create a recurring reminder.', targetType: 'series' as const, targetId: null, expectedVersion: null },
    } as ApprovalDetail;
    const summary = approvalSummaryFromDetail(detail);
    expect(summary.toolId).toBe('reminder.series.create');
    expect(Object.prototype.hasOwnProperty.call(summary, 'proposal')).toBe(false);
  });

  it('describes cancel scope and keeps protocol fields out of the primary copy', () => {
    const occurrence = { ...base, toolId: 'reminder.cancel' as const, proposal: { id: 'proposal-1', operation: 'cancel' as const, summary: 'Cancel the next occurrence.', targetType: 'occurrence' as const, targetId: 'occurrence-1', expectedVersion: 3 } } as ApprovalDetail;
    expect(isReminderCommandProposal(occurrence.proposal)).toBe(true);
    expect(approvalActionLabel(occurrence)).toBe('Cancel this reminder occurrence');
    expect(reminderCommandConsequence(occurrence.proposal)).toContain('future recurring occurrences remain scheduled');
    expect(approvalActionLabel({ ...base, toolId: 'reminder.cancel' })).toBe('Cancel a reminder');
    const series = { ...occurrence, proposal: { ...occurrence.proposal, targetType: 'series' as const } };
    expect(approvalActionLabel(series)).toBe('Cancel this recurring reminder');
    expect(reminderCommandConsequence(series.proposal)).toContain('prevents future occurrences');
  });

  it('uses neutral lead copy after a recurring command is terminal', () => {
    const approved = { ...base, toolId: 'reminder.series.update' as const, status: 'approved' as const, executionStatus: 'completed' as const };
    expect(approvalLeadCopy(approved)).toBe('Approved and completed. This action is no longer awaiting authorization.');
    expect(approvalLeadCopy({ ...approved, status: 'rejected', executionStatus: 'denied' })).toContain('will not take this action');
  });

  it('preserves decision readiness while the same approval is reconciled', () => {
    expect(shouldPreserveReviewedApproval(false, base.id, base.id, base.id)).toBe(true);
    expect(shouldPreserveReviewedApproval(true, base.id, base.id, base.id)).toBe(false);
    expect(shouldPreserveReviewedApproval(false, 'another-approval', base.id, base.id)).toBe(false);
  });

  it('always creates UUID idempotency keys when randomUUID is unavailable', () => {
    const native = '550e8400-e29b-41d4-a716-446655440000';
    expect(createUuidV4({ randomUUID: () => native })).toBe(native);
    expect(createUuidV4({ getRandomValues: (bytes) => bytes.fill(0) })).toBe('00000000-0000-4000-8000-000000000000');
    expect(createUuidV4({})).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i);
  });

  it('advances history past pending-only pages before exposing continuation', async () => {
    const calls: Array<string | undefined> = [];
    const history = await collectApprovalHistory(async (cursor) => {
      calls.push(cursor);
      return cursor === undefined
        ? { items: [base], nextCursor: 'history-2' }
        : { items: [{ ...base, id: 'approval-terminal', status: 'approved' as const, executionStatus: 'completed' as const }], nextCursor: 'history-3' };
    });
    expect(calls).toEqual([undefined, 'history-2']);
    expect(history.items.map((item) => item.id)).toEqual(['approval-terminal']);
    expect(history.nextCursor).toBe('history-3');
  });

  it('continues history pagination from the supplied cursor', async () => {
    const calls: Array<string | undefined> = [];
    const history = await collectApprovalHistory(async (cursor) => {
      calls.push(cursor);
      return { items: [{ ...base, id: 'approval-terminal', status: 'approved' as const, executionStatus: 'completed' as const }], nextCursor: null };
    }, 'history-2');
    expect(calls).toEqual(['history-2']);
    expect(history.items).toHaveLength(1);
  });

  it('bounds one hundred pending-only history pages while returning a continuation cursor', async () => {
    const calls: Array<string | undefined> = [];
    const bounded = await collectApprovalHistory(async (cursor) => {
      calls.push(cursor);
      const page = cursor ? Number(cursor.replace('pending-', '')) : 0;
      return { items: [{ ...base, id: `pending-${page}` }], nextCursor: `pending-${page + 1}` };
    });
    expect(calls).toHaveLength(100);
    expect(bounded.items).toHaveLength(0);
    expect(bounded.nextCursor).toBe('pending-100');

    const terminal = { ...base, id: 'approval-terminal-after-bound', status: 'approved' as const, executionStatus: 'completed' as const };
    const continued = await collectApprovalHistory(async (cursor) => {
      expect(cursor).toBe('pending-100');
      return { items: [terminal], nextCursor: null };
    }, bounded.nextCursor ?? undefined);
    expect(continued.items).toEqual([terminal]);
    expect(continued.nextCursor).toBeNull();
  });

  it('fails closed for oversized approval stream input before parsing', () => {
    const unterminated = 'data:' + 'x'.repeat(256 * 1024);
    expect(approvalStreamBufferWithinBounds('', unterminated)).toBe(false);
    expect(approvalStreamBlockWithinBounds('data:' + 'x'.repeat(128 * 1024))).toBe(false);
  });

  it('keeps unconfirmed decision failures distinct from a known conflict', () => {
    expect(decisionFailureNotice(503)).toContain('outcome is unknown');
    expect(decisionFailureNotice(null)).toContain('outcome is unknown');
    expect(decisionFailureNotice(409)).toContain('changed elsewhere');
  });

  it('does not let stale session hydration restore an old CSRF token', async () => {
    let resolveOld!: (token: string) => void;
    let resolveNew!: (token: string) => void;
    const hydrator = new ApprovalSessionHydrator(() => new Promise<string>((resolve) => { if (!resolveOld) resolveOld = resolve; else resolveNew = resolve; }), () => new ApprovalApiError(401, 'Session changed.'));
    const old = hydrator.ensure(); hydrator.clearSession(); const replacement = hydrator.ensure(); resolveNew('new-token'); await replacement; resolveOld('old-token');
    await expect(old).rejects.toMatchObject({ status: 401 }); expect(hydrator.currentToken()).toBe('new-token');
  });
});
