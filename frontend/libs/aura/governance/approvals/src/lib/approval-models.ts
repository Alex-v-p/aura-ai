import type { Approval, ApprovalEvent as ApiApprovalEvent, ApprovalExecutionStatus, ApprovalPage as ApiApprovalPage, ApprovalStatus, ApprovalStreamEvent as ApiApprovalStreamEvent, ReminderApproval, ReminderApprovalToolId, ReminderCommandProposalDetail as ApiReminderCommandProposalDetail, ReminderProposalDetail as ApiReminderProposalDetail, WorkflowApproval, WorkflowApprovalDetail, WorkflowApprovalProposal as ApiWorkflowApprovalProposal } from '@aura/aura-api-client';

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
export type WorkflowApprovalProposal = ApiWorkflowApprovalProposal;
export type ApprovalProposal = ReminderProposalDetail | ReminderCommandProposalDetail | WorkflowApprovalProposal;
export type ApprovalSummary = Approval;
export type ApprovalDetail = (ReminderApproval & { readonly proposal: ReminderProposalDetail | ReminderCommandProposalDetail }) | WorkflowApprovalDetail;
export type ApprovalPage = Omit<ApiApprovalPage, 'items'> & { readonly items: ReadonlyArray<ApprovalSummary> };
export type ApprovalEvent = ApiApprovalEvent;
export type ApprovalStreamEvent = ApiApprovalStreamEvent;
export type ApprovalView = 'pending' | 'history';

export function isReminderCommandProposal(proposal: ApprovalProposal): proposal is ReminderCommandProposalDetail {
  return 'operation' in proposal;
}

export function isWorkflowApproval(approval: ApprovalSummary): approval is WorkflowApproval {
  return 'origin' in approval && approval.origin === 'manual_workflow_instance';
}

export function isWorkflowApprovalDetail(approval: ApprovalSummary | ApprovalDetail): approval is WorkflowApprovalDetail {
  return 'origin' in approval && approval.origin === 'manual_workflow_instance' && 'proposal' in approval;
}

export function workflowApprovalProposal(approval: ApprovalDetail): WorkflowApprovalProposal | null {
  return isWorkflowApprovalDetail(approval) ? approval.proposal : null;
}

function targetLabel(targetType: ReminderCommandProposalDetail['targetType']): string {
  return targetType === 'occurrence' ? 'this reminder occurrence' : targetType === 'series' ? 'this recurring reminder' : 'this reminder';
}

/** Plain-language labels keep consequential scope visible without exposing protocol fields in the primary card. */
export function approvalActionLabel(approval: ApprovalSummary | ApprovalDetail): string {
  if (isWorkflowApprovalDetail(approval)) return `Approve ${approval.proposal.workflowDisplayName} · step ${approval.proposal.stepOrdinal}`;
  if ('origin' in approval && approval.origin === 'manual_workflow_instance') return `Approve workflow step ${approval.workflowStepOrdinal}`;
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

export function approvalOriginLabel(approval: ApprovalSummary | ApprovalDetail): string {
  return 'origin' in approval && approval.origin === 'manual_workflow_instance' ? 'Manual workflow instance' : `Conversation ${(approval as ReminderApproval).conversationId}`;
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
  const { proposal: _proposal, ...summary } = detail;
  void _proposal;
  return summary as ApprovalSummary;
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
const approvalEventBaseKeys = ['schemaVersion', 'eventId', 'eventType', 'approvalId', 'proposalId', 'toolVersion', 'status', 'executionStatus', 'version', 'occurredAt'] as const;
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
  const event = value as { readonly schemaVersion?: unknown; readonly eventId?: unknown; readonly eventType?: unknown; readonly approvalId?: unknown; readonly proposalId?: unknown; readonly toolVersion?: unknown; readonly status?: unknown; readonly executionStatus?: unknown; readonly version?: unknown; readonly occurredAt?: unknown; readonly origin?: unknown; readonly runId?: unknown; readonly conversationId?: unknown; readonly toolId?: unknown; readonly workflowId?: unknown; readonly workflowRevisionId?: unknown; readonly workflowInstanceId?: unknown; readonly workflowStepOrdinal?: unknown; readonly workflowStepExecutionId?: unknown };
  const hasBase = approvalEventBaseKeys.every((key) => Object.prototype.hasOwnProperty.call(event, key));
  if (!hasBase || event.schemaVersion !== 1 || !boundedEventId(event.eventId) ||
    !['approval.pending', 'approval.approved', 'approval.rejected', 'approval.expired', 'approval.canceled'].includes(String(event.eventType)) ||
    !boundedEventId(event.approvalId) || !boundedEventId(event.proposalId) || event.toolVersion !== 1 ||
    !['pending', 'approved', 'rejected', 'expired', 'canceled'].includes(String(event.status)) ||
    !['not_started', 'queued', 'executing', 'completed', 'failed', 'denied'].includes(String(event.executionStatus)) ||
    !Number.isInteger(event.version) || Number(event.version) <= 0 || !isStrictRfc3339(event.occurredAt)) return false;
  if (event.origin === 'manual_workflow_instance') {
    return Object.keys(event).length === approvalEventBaseKeys.length + 9 && event.runId === null && event.conversationId === null && event.toolId === 'workflow.owner_approval' && boundedEventId(event.workflowId) && boundedEventId(event.workflowRevisionId) && boundedEventId(event.workflowInstanceId) && Number.isInteger(event.workflowStepOrdinal) && Number(event.workflowStepOrdinal) >= 1 && boundedEventId(event.workflowStepExecutionId);
  }
  const legacy = Object.prototype.hasOwnProperty.call(event, 'runId') || Object.prototype.hasOwnProperty.call(event, 'conversationId') || Object.prototype.hasOwnProperty.call(event, 'toolId');
  if (legacy) {
    return Object.keys(event).length === approvalEventBaseKeys.length + 3 && boundedEventId(event.runId) && boundedEventId(event.conversationId) && isApprovalToolId(event.toolId);
  }
  return false;
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
