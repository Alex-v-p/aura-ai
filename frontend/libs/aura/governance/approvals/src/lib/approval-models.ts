import type { Approval, ApprovalDetail as ApiApprovalDetail, ApprovalEvent as ApiApprovalEvent, ApprovalExecutionStatus, ApprovalPage as ApiApprovalPage, ApprovalStatus, ApprovalStreamEvent as ApiApprovalStreamEvent, ReminderApprovalToolId, ReminderCommandProposalDetail as ApiReminderCommandProposalDetail, ReminderProposalDetail as ApiReminderProposalDetail } from '@aura/aura-api-client';

export type { ApprovalExecutionStatus, ApprovalStatus };
/** The approval inbox is intentionally closed over the reminder commands that
 * can be confirmed. Unknown tool ids must never become actionable UI. */
export const APPROVAL_TOOL_IDS = [
  'reminder.create',
  'reminder.series.create',
  'reminder.reschedule',
  'reminder.series.update',
  'reminder.series.pause',
  'reminder.series.resume',
  'reminder.cancel',
] as const;
export type ApprovalToolId = ReminderApprovalToolId;
export function isApprovalToolId(value: unknown): value is ApprovalToolId {
  return typeof value === 'string' && (APPROVAL_TOOL_IDS as readonly string[]).includes(value);
}

export type ReminderProposalDetail = ApiReminderProposalDetail;
export type ReminderCommandProposalDetail = ApiReminderCommandProposalDetail;
export type ApprovalProposal = ReminderProposalDetail | ReminderCommandProposalDetail;
export type ApprovalSummary = Omit<Approval, 'toolId'> & { readonly toolId: ApprovalToolId };
export type ApprovalDetail = Omit<ApiApprovalDetail, 'toolId' | 'proposal'> & { readonly toolId: ApprovalToolId; readonly proposal: ApprovalProposal };
export type ApprovalPage = Omit<ApiApprovalPage, 'items'> & { readonly items: ReadonlyArray<ApprovalSummary> };
export type ApprovalEvent = ApiApprovalEvent;
export type ApprovalStreamEvent = ApiApprovalStreamEvent;
export type ApprovalView = 'pending' | 'history';

export function isReminderCommandProposal(proposal: ApprovalProposal): proposal is ReminderCommandProposalDetail {
  return 'operation' in proposal;
}

function targetLabel(targetType: ReminderCommandProposalDetail['targetType']): string {
  return targetType === 'occurrence' ? 'this reminder occurrence' : targetType === 'series' ? 'this recurring reminder' : 'this reminder';
}

/** Plain-language labels keep consequential scope visible without exposing protocol fields in the primary card. */
export function approvalActionLabel(approval: ApprovalSummary | ApprovalDetail): string {
  const proposal = 'proposal' in approval ? approval.proposal : null;
  if (proposal && isReminderCommandProposal(proposal)) {
    switch (proposal.operation) {
      case 'series.create': return 'Create a recurring reminder';
      case 'reschedule': return `Reschedule ${targetLabel(proposal.targetType)}`;
      case 'series.update': return 'Update this recurring reminder';
      case 'series.pause': return 'Pause this recurring reminder';
      case 'series.resume': return 'Resume this recurring reminder';
      case 'cancel': return `Cancel ${targetLabel(proposal.targetType)}`;
    }
  }
  switch (approval.toolId) {
    case 'reminder.create': return 'Create a reminder';
    case 'reminder.series.create': return 'Create a recurring reminder';
    case 'reminder.reschedule': return 'Reschedule a reminder';
    case 'reminder.series.update': return 'Update a recurring reminder';
    case 'reminder.series.pause': return 'Pause a recurring reminder';
    case 'reminder.series.resume': return 'Resume a recurring reminder';
    case 'reminder.cancel': return 'Cancel a reminder';
    default: return 'Review reminder action';
  }
}

export function reminderCommandConsequence(proposal: ReminderCommandProposalDetail): string {
  switch (proposal.operation) {
    case 'series.create': return 'This creates the recurring reminder described above.';
    case 'reschedule': return proposal.targetType === 'occurrence'
      ? 'Only this reminder occurrence will move; the rest of the recurring schedule stays on its cadence.'
      : 'This moves the reminder to the requested time.';
    case 'series.update': return 'This changes future occurrences; past occurrences remain unchanged.';
    case 'series.pause': return 'Future occurrences will be suppressed until the recurring reminder is resumed.';
    case 'series.resume': return 'Future occurrences will resume on the recurring reminder’s existing cadence.';
    case 'cancel': return proposal.targetType === 'occurrence'
      ? 'Only this reminder occurrence will be canceled; future recurring occurrences remain scheduled.'
      : proposal.targetType === 'series'
        ? 'This cancels the recurring reminder and prevents future occurrences.'
        : 'This cancels the reminder.';
  }
}

export function approvalLeadCopy(approval: ApprovalSummary): string {
  if (approval.status === 'pending') return 'Aura is waiting for your authorization before it takes this action.';
  if (approval.status === 'approved') {
    return approval.executionStatus === 'completed'
      ? 'Approved and completed. This action is no longer awaiting authorization.'
      : 'Approved. Aura no longer needs authorization for this action.';
  }
  if (approval.status === 'rejected') return 'This request was rejected. Aura will not take this action.';
  if (approval.status === 'expired') return 'This request expired before authorization and can no longer be approved.';
  return 'This request was canceled and can no longer be changed.';
}

export interface UuidSource { readonly randomUUID?: () => string; readonly getRandomValues?: (bytes: Uint8Array) => Uint8Array; }
const uuidPattern = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
export function createUuidV4(source?: UuidSource): string {
  const runtimeSource = source ?? (typeof globalThis !== 'undefined' ? globalThis.crypto as UuidSource | undefined : undefined);
  const native = runtimeSource?.randomUUID?.();
  if (native && uuidPattern.test(native)) return native;
  const bytes = new Uint8Array(16);
  if (runtimeSource?.getRandomValues) runtimeSource.getRandomValues(bytes);
  else for (let index = 0; index < bytes.length; index += 1) bytes[index] = Math.floor(Math.random() * 256);
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0')).join('');
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}
export function shouldPreserveReviewedApproval(routeChanged: boolean, reviewedId: string | null, selectedId: string | null, approvalId: string): boolean {
  return !routeChanged && reviewedId === approvalId && selectedId === approvalId;
}
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
    isApprovalToolId(event.toolId) && event.toolVersion === 1 &&
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
