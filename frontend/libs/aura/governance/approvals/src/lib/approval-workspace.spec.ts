import { describe, expect, it } from 'vitest';
import { appendApprovalPage, approvalStreamBlockWithinBounds, approvalStreamBufferWithinBounds, approvalSummaryFromDetail, collectApprovalHistory, decisionFailureNotice, emptyApprovalCollection, isValidApprovalEvent, type ApprovalDetail, type ApprovalSummary } from './approval-models';
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
