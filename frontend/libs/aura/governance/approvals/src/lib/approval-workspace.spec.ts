import { describe, expect, it } from 'vitest';
import { appendApprovalPage, emptyApprovalCollection, type ApprovalSummary } from './approval-models';

const base: ApprovalSummary = { id: 'approval-1', proposalId: 'proposal-1', runId: 'run-1', conversationId: 'conversation-1', toolCallId: 'call-1', toolId: 'reminder.create', toolVersion: 1, status: 'pending', executionStatus: 'not_started', version: 1, createdAt: '2026-10-10T09:00:00.000Z', updatedAt: '2026-10-10T09:00:00.000Z', expiresAt: '2026-10-10T10:00:00.000Z', decidedAt: null };

describe('approval workspace', () => {
  it('counts pending approvals while appending replay pages', () => {
    const current = { ...emptyApprovalCollection(), items: [base], pendingCount: 1 };
    const next = { items: [{ ...base, id: 'approval-2', status: 'approved' as const, executionStatus: 'completed' as const }], nextCursor: null };
    expect(appendApprovalPage(current, next)).toMatchObject({ items: [{ id: 'approval-1' }, { id: 'approval-2' }], pendingCount: 1 });
  });
});
