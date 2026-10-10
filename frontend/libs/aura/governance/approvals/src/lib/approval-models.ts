import type { Approval, ApprovalDetail as ApiApprovalDetail, ApprovalExecutionStatus, ApprovalPage as ApiApprovalPage, ApprovalStatus, ApprovalStreamEvent as ApiApprovalStreamEvent } from '@aura/aura-api-client';

export type { ApprovalExecutionStatus, ApprovalStatus };
export type ApprovalSummary = Approval;
export type ApprovalDetail = ApiApprovalDetail;
export type ApprovalPage = ApiApprovalPage;

export type ApprovalStreamEvent = ApiApprovalStreamEvent;

export interface ApprovalStream {
  close(): void;
}

export class ApprovalApiError extends Error {
  constructor(readonly status: number | null, message: string, readonly retryable = true) { super(message); this.name = 'ApprovalApiError'; }
}

export interface ApprovalCollection extends ApprovalPage {
  readonly pendingCount: number;
}

export function emptyApprovalCollection(): ApprovalCollection { return { items: [], nextCursor: null, pendingCount: 0 }; }
export function appendApprovalPage(current: ApprovalCollection, next: ApprovalPage): ApprovalCollection {
  const items = [...current.items, ...next.items];
  return { items, nextCursor: next.nextCursor, pendingCount: items.filter((item) => item.status === 'pending').length };
}

export interface ApprovalsApi {
  list(cursor?: string, status?: ApprovalStatus): Promise<ApprovalPage>;
  get(id: string): Promise<ApprovalDetail>;
  approve(id: string, expectedVersion: number): Promise<ApprovalSummary>;
  reject(id: string, expectedVersion: number): Promise<ApprovalSummary>;
  stream(lastEventId: string | undefined, onEvent: (event: ApprovalStreamEvent) => void, onError: (error: ApprovalApiError) => void): ApprovalStream;
}
