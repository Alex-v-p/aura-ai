import type { Approval, ApprovalDetail as ApiApprovalDetail, ApprovalEvent, ApprovalExecutionStatus, ApprovalPage as ApiApprovalPage, ApprovalStatus, ApprovalStreamEvent as ApiApprovalStreamEvent } from '@aura/aura-api-client';

export type { ApprovalExecutionStatus, ApprovalStatus };
export type ApprovalSummary = Approval;
export type ApprovalDetail = ApiApprovalDetail;
export type ApprovalPage = ApiApprovalPage;
export type ApprovalStreamEvent = ApiApprovalStreamEvent;
export type ApprovalView = 'pending' | 'history';
export function approvalSummaryFromDetail(detail: ApprovalDetail): ApprovalSummary {
  const { proposal, ...summary } = detail;
  void proposal;
  return summary;
}

export interface ApprovalStream { close(): void; }
export class ApprovalApiError extends Error {
  constructor(readonly status: number | null, message: string, readonly retryable = true) { super(message); this.name = 'ApprovalApiError'; }
}

export interface ApprovalCollection extends ApprovalPage { readonly pendingCount: number; }
export function emptyApprovalCollection(): ApprovalCollection { return { items: [], nextCursor: null, pendingCount: 0 }; }
export function isTerminalApproval(approval: ApprovalSummary): boolean { return approval.status !== 'pending'; }
/** A decision transport failure is intentionally phrased as unconfirmed: the server may have applied it. */
export function decisionFailureNotice(status: number | null): string {
  return status === 409
    ? 'This approval changed elsewhere. We refreshed its status; review it before deciding again.'
    : 'The decision outcome is unknown. Refresh to confirm before trying again.';
}
export function appendApprovalPage(current: ApprovalCollection, next: ApprovalPage): ApprovalCollection {
  const items = [...current.items, ...next.items];
  return { items, nextCursor: next.nextCursor, pendingCount: items.filter((item) => item.status === 'pending').length };
}

const boundedUuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const maxStreamBufferBytes = 256 * 1024;
const maxStreamEventBytes = 128 * 1024;
const maxStreamDataBytes = 128 * 1024;
const utf8Bytes = (value: string): number => typeof TextEncoder === 'undefined' ? value.length : new TextEncoder().encode(value).byteLength;
export function approvalStreamBufferWithinBounds(buffer: string, nextChunk: string): boolean { return utf8Bytes(buffer) + utf8Bytes(nextChunk) <= maxStreamBufferBytes; }
export function approvalStreamBlockWithinBounds(block: string): boolean {
  if (utf8Bytes(block) > maxStreamEventBytes) return false;
  let dataBytes = 0;
  for (const line of block.split(/\r?\n/)) {
    const separator = line.indexOf(':');
    const field = separator < 0 ? line : line.slice(0, separator);
    const value = separator < 0 ? '' : line.slice(separator + 1).replace(/^ /, '');
    if (field === 'data') { dataBytes += utf8Bytes(value); if (dataBytes > maxStreamDataBytes) return false; }
  }
  return true;
}
const strictRfc3339 = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d{1,9})?(?:Z|[+-]\d{2}:\d{2})$/;
const approvalEventKeys = ['schemaVersion', 'eventId', 'eventType', 'approvalId', 'proposalId', 'runId', 'conversationId', 'toolId', 'toolVersion', 'status', 'executionStatus', 'version', 'occurredAt'] as const;
const boundedEventId = (value: unknown): value is string => typeof value === 'string' && boundedUuid.test(value);
function isStrictRfc3339(value: unknown): value is string {
  if (typeof value !== 'string') return false;
  const match = strictRfc3339.exec(value);
  if (!match) return false;
  const [, year, month, day, hour, minute, second] = match.map(Number);
  const maximumDay = new Date(Date.UTC(year, month, 0)).getUTCDate();
  return month >= 1 && month <= 12 && day >= 1 && day <= maximumDay && hour <= 23 && minute <= 59 && second <= 59 && Number.isFinite(Date.parse(value));
}
export function isValidApprovalEvent(value: unknown): value is ApprovalEvent {
  if (!value || typeof value !== 'object') return false;
  const event = value as Partial<ApprovalEvent>;
  const keys = Object.keys(value);
  if (keys.length !== approvalEventKeys.length || approvalEventKeys.some((key) => !Object.prototype.hasOwnProperty.call(value, key))) return false;
  return event.schemaVersion === 1 && boundedEventId(event.eventId) &&
    (event.eventType === 'approval.pending' || event.eventType === 'approval.approved' || event.eventType === 'approval.rejected' || event.eventType === 'approval.expired' || event.eventType === 'approval.canceled') &&
    boundedEventId(event.approvalId) && boundedEventId(event.proposalId) && boundedEventId(event.runId) && boundedEventId(event.conversationId) &&
    event.toolId === 'reminder.create' && event.toolVersion === 1 &&
    (event.status === 'pending' || event.status === 'approved' || event.status === 'rejected' || event.status === 'expired' || event.status === 'canceled') &&
    (event.executionStatus === 'not_started' || event.executionStatus === 'queued' || event.executionStatus === 'executing' || event.executionStatus === 'completed' || event.executionStatus === 'failed' || event.executionStatus === 'denied') &&
    Number.isInteger(event.version) && Number(event.version) > 0 && isStrictRfc3339(event.occurredAt);
}

export async function collectApprovalHistory(fetchPage: (cursor?: string) => Promise<ApprovalPage>, initialCursor?: string): Promise<ApprovalCollection> {
  const items: ApprovalSummary[] = [];
  const visited = new Set<string>();
  let cursor: string | undefined = initialCursor;
  for (let pageCount = 0; pageCount < 100; pageCount += 1) {
    if (cursor && visited.has(cursor)) return { items, nextCursor: null, pendingCount: 0 };
    if (cursor) visited.add(cursor);
    const page = await fetchPage(cursor);
    items.push(...page.items.filter(isTerminalApproval));
    if (!page.nextCursor || items.length > 0) return { items, nextCursor: page.nextCursor, pendingCount: 0 };
    cursor = page.nextCursor;
  }
  return { items, nextCursor: cursor ?? null, pendingCount: 0 };
}

export interface ApprovalsApi {
  list(cursor?: string, status?: ApprovalStatus): Promise<ApprovalPage>;
  get(id: string): Promise<ApprovalDetail>;
  approve(id: string, expectedVersion: number): Promise<ApprovalSummary>;
  reject(id: string, expectedVersion: number): Promise<ApprovalSummary>;
  stream(lastEventId: string | undefined, onEvent: (event: ApprovalStreamEvent) => void, onError: (error: ApprovalApiError) => void): ApprovalStream;
  clearSession?(): void;
}
