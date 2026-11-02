import type { Notification, NotificationLifecycleEvent, NotificationPage, NotificationStatus, WorkflowNotification } from '@aura/aura-api-client';

export type NotificationRecord = Notification;
export type NotificationEvent = NotificationLifecycleEvent;
export type { NotificationPage, NotificationStatus };

export type NotificationCollection = NotificationPage;
export type NotificationView = 'unread' | 'history';

/**
 * Source-neutral browser projection for the generated notification union.
 *
 * The generated client remains the transport authority.  This projection is
 * deliberately content-free and exists only so legacy reminder responses can
 * be consumed while the additive `source` field rolls through the API.
 */
export type NotificationSourceView =
  | { readonly kind: 'reminder'; readonly reminderId: string }
  | { readonly kind: 'briefing'; readonly briefingScheduleId: string; readonly briefingOccurrenceId: string; readonly briefingResultId: string; readonly destination: 'briefing_result' }
  | { readonly kind: 'workflow'; readonly workflowId: string; readonly workflowRevisionId: string; readonly workflowInstanceId: string; readonly workflowStepOrdinal: number; readonly workflowStepExecutionId: string; readonly workflowResultId: string };

function isBoundedIdentifier(value: unknown): value is string {
  return typeof value === 'string' && value.length > 0 && value.length <= 160;
}

/** Returns the closed source union, accepting the pre-union reminder shape. */
export function notificationSource(notification: NotificationRecord): NotificationSourceView | null {
  if (isWorkflowNotification(notification)) {
    return { kind: 'workflow', workflowId: notification.workflowId, workflowRevisionId: notification.workflowRevisionId, workflowInstanceId: notification.workflowInstanceId, workflowStepOrdinal: notification.workflowStepOrdinal, workflowStepExecutionId: notification.workflowStepExecutionId, workflowResultId: notification.workflowResultId };
  }
  const source = (notification as unknown as { readonly source?: unknown }).source;
  if (source && typeof source === 'object') {
    const candidate = source as { readonly kind?: unknown; readonly reminderId?: unknown; readonly briefingScheduleId?: unknown; readonly briefingOccurrenceId?: unknown; readonly briefingResultId?: unknown; readonly destination?: unknown };
    if (candidate.kind === 'reminder' && isBoundedIdentifier(candidate.reminderId)) return { kind: 'reminder', reminderId: candidate.reminderId };
    if (candidate.kind === 'briefing' && isBoundedIdentifier(candidate.briefingScheduleId) && isBoundedIdentifier(candidate.briefingOccurrenceId) && isBoundedIdentifier(candidate.briefingResultId) && candidate.destination === 'briefing_result') {
      return briefingSource(candidate.briefingScheduleId, candidate.briefingOccurrenceId, candidate.briefingResultId);
    }
  }
  const briefingResultId = (notification as unknown as { readonly briefingResultId?: unknown }).briefingResultId;
  const briefingScheduleId = (notification as unknown as { readonly briefingScheduleId?: unknown }).briefingScheduleId;
  const briefingOccurrenceId = (notification as unknown as { readonly briefingOccurrenceId?: unknown }).briefingOccurrenceId;
  if (isBoundedIdentifier(briefingScheduleId) && isBoundedIdentifier(briefingOccurrenceId) && isBoundedIdentifier(briefingResultId)) return briefingSource(briefingScheduleId, briefingOccurrenceId, briefingResultId);
  const legacyReminderId = (notification as unknown as { readonly reminderId?: unknown }).reminderId;
  return isBoundedIdentifier(legacyReminderId) ? { kind: 'reminder', reminderId: legacyReminderId } : null;
}

export function isWorkflowNotification(notification: NotificationRecord): notification is WorkflowNotification {
  return 'workflowId' in notification && typeof notification.workflowId === 'string';
}

export function notificationWorkflow(notification: NotificationRecord): Extract<NotificationSourceView, { readonly kind: 'workflow' }> | null {
  const source = notificationSource(notification);
  return source?.kind === 'workflow' ? source : null;
}

/** Keeps the canonical route lineage available to the feature while retaining
 * the compact source projection's historical enumerable shape. */
function briefingSource(scheduleId: string, occurrenceId: string, resultId: string): Extract<NotificationSourceView, { readonly kind: 'briefing' }> {
  const source = { kind: 'briefing' as const, briefingResultId: resultId, destination: 'briefing_result' as const } as Extract<NotificationSourceView, { readonly kind: 'briefing' }> & { briefingScheduleId: string; briefingOccurrenceId: string };
  Object.defineProperties(source, { briefingScheduleId: { value: scheduleId, enumerable: false }, briefingOccurrenceId: { value: occurrenceId, enumerable: false } });
  return source;
}

export function notificationReminderId(notification: NotificationRecord): string | null {
  const source = notificationSource(notification);
  return source?.kind === 'reminder' ? source.reminderId : null;
}

export function notificationBriefingResult(notification: NotificationRecord): Extract<NotificationSourceView, { readonly kind: 'briefing' }> | null {
  const source = notificationSource(notification);
  return source?.kind === 'briefing' ? source : null;
}

export type NotificationMutationToken = Readonly<{ session: number; mutation: number; detail: number; detailKey: string; notificationId: string }>;
/** Small executable boundary for delayed acknowledgement responses. */
export class NotificationMutationGuard {
  private session = 0;
  private mutation = 0;
  private detail = 0;
  private detailKey = '';
  begin(notificationId: string): NotificationMutationToken { return { session: this.session, mutation: ++this.mutation, detail: this.detail, detailKey: this.detailKey, notificationId }; }
  routeChanged(id: string): void { ++this.detail; this.detailKey = id; }
  clearRoute(): void { ++this.detail; this.detailKey = ''; }
  resetSession(): void { ++this.session; ++this.detail; this.detailKey = ''; }
  isSessionMutationCurrent(token: NotificationMutationToken): boolean { return token.session === this.session && token.mutation === this.mutation; }
  isRouteCurrent(token: NotificationMutationToken): boolean {
    if (!this.isSessionMutationCurrent(token) || token.detail !== this.detail || token.detailKey !== this.detailKey) return false;
    // An empty detail key is the inbox/list surface. Detail tokens must match
    // the currently open record, while list tokens remain valid on the list.
    return token.detailKey === '' || token.notificationId === this.detailKey;
  }
}

const boundedUuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const lifecycleBaseKeys = ['schemaVersion', 'eventId', 'eventType', 'notificationId', 'status', 'notificationVersion', 'occurredAt'] as const;
const reminderLifecycleKeys = new Set<string>([...lifecycleBaseKeys, 'reminderId']);
const briefingLifecycleKeys = new Set<string>([...lifecycleBaseKeys, 'briefingScheduleId', 'briefingOccurrenceId', 'briefingResultId']);
const workflowLifecycleKeys = new Set<string>([...lifecycleBaseKeys, 'workflowId', 'workflowRevisionId', 'workflowInstanceId', 'workflowStepOrdinal', 'workflowStepExecutionId', 'workflowResultId']);
function hasOnlyLifecycleKeys(value: object, allowed: ReadonlySet<string>): boolean { return Object.keys(value).every((key) => allowed.has(key)); }
function isWorkflowUuid(value: unknown): value is string { return typeof value === 'string' && boundedUuid.test(value); }
export function isValidNotificationEvent(value: unknown): value is NotificationEvent {
  if (!value || typeof value !== 'object') return false;
  const event = value as Partial<NotificationEvent>;
  const legacyReminderId = (value as { readonly reminderId?: unknown }).reminderId;
  const briefingScheduleId = (value as { readonly briefingScheduleId?: unknown }).briefingScheduleId;
  const briefingOccurrenceId = (value as { readonly briefingOccurrenceId?: unknown }).briefingOccurrenceId;
  const briefingResultId = (value as { readonly briefingResultId?: unknown }).briefingResultId;
  const workflowId = (value as { readonly workflowId?: unknown }).workflowId;
  const workflowRevisionId = (value as { readonly workflowRevisionId?: unknown }).workflowRevisionId;
  const workflowInstanceId = (value as { readonly workflowInstanceId?: unknown }).workflowInstanceId;
  const workflowStepOrdinal = (value as { readonly workflowStepOrdinal?: unknown }).workflowStepOrdinal;
  const workflowStepExecutionId = (value as { readonly workflowStepExecutionId?: unknown }).workflowStepExecutionId;
  const workflowResultId = (value as { readonly workflowResultId?: unknown }).workflowResultId;
  const commonValid = event.schemaVersion === 1 && typeof event.eventId === 'string' && boundedUuid.test(event.eventId) &&
    ((event.eventType === 'notification.delivered' && event.status === 'delivered') || (event.eventType === 'notification.acknowledged' && event.status === 'acknowledged')) &&
    typeof event.notificationId === 'string' && event.notificationId.length > 0 && event.notificationId.length <= 160 &&
    Number.isInteger(event.notificationVersion) &&
    Number(event.notificationVersion) > 0 && typeof event.occurredAt === 'string' && Number.isFinite(Date.parse(event.occurredAt));
  const hasReminder = isBoundedIdentifier(legacyReminderId) && hasOnlyLifecycleKeys(value, reminderLifecycleKeys);
  const hasBriefing = isBoundedIdentifier(briefingScheduleId) && isBoundedIdentifier(briefingOccurrenceId) && isBoundedIdentifier(briefingResultId) && hasOnlyLifecycleKeys(value, briefingLifecycleKeys);
  const hasWorkflow = isWorkflowUuid(workflowId) && isWorkflowUuid(workflowRevisionId) && isWorkflowUuid(workflowInstanceId) && Number.isInteger(workflowStepOrdinal) && Number(workflowStepOrdinal) >= 1 && Number(workflowStepOrdinal) <= 8 && isWorkflowUuid(workflowStepExecutionId) && isWorkflowUuid(workflowResultId) && hasOnlyLifecycleKeys(value, workflowLifecycleKeys);
  return commonValid && (hasReminder || hasBriefing || hasWorkflow);
}

export function appendNotificationPage(current: NotificationCollection, next: NotificationCollection): NotificationCollection {
  return { items: [...current.items, ...next.items], nextCursor: next.nextCursor, unacknowledgedCount: next.unacknowledgedCount };
}

export function reconcileAcknowledged(current: NotificationCollection, updated: NotificationRecord, view: NotificationView): NotificationCollection {
  return {
    ...current,
    items: view === 'unread' ? current.items.filter((item) => item.id !== updated.id) : current.items.map((item) => item.id === updated.id ? updated : item),
    unacknowledgedCount: Math.max(0, current.unacknowledgedCount - 1),
  };
}

export interface NotificationsApi {
  list(cursor?: string, status?: NotificationStatus): Promise<NotificationPage>;
  get(id: string): Promise<NotificationRecord>;
  acknowledge(id: string, expectedVersion: number): Promise<NotificationRecord>;
  stream(lastEventId: string | undefined, onEvent: (event: NotificationEvent) => void, onError: (error: NotificationApiError) => void): NotificationStream;
  clearSession?(): void;
}

export interface NotificationStream {
  close(): void;
}

/** Fetches the owner-authorized unread projection without changing workspace view. */
export function loadUnreadProjection(api: Pick<NotificationsApi, 'list'>): Promise<NotificationCollection> {
  return api.list(undefined, 'delivered');
}

export class NotificationApiError extends Error {
  constructor(readonly status: number | null, message: string, readonly retryable = true) {
    super(message);
    this.name = 'NotificationApiError';
  }
}
