import type { Notification, NotificationLifecycleEvent, NotificationPage, NotificationStatus } from '@aura/aura-api-client';

export type NotificationRecord = Notification;
export type NotificationEvent = NotificationLifecycleEvent;
export type { NotificationPage, NotificationStatus };

export type NotificationCollection = NotificationPage;
export type NotificationView = 'unread' | 'history';

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
export function isValidNotificationEvent(value: unknown): value is NotificationEvent {
  if (!value || typeof value !== 'object') return false;
  const event = value as Partial<NotificationEvent>;
  return event.schemaVersion === 1 && typeof event.eventId === 'string' && boundedUuid.test(event.eventId) &&
    (event.eventType === 'notification.delivered' || event.eventType === 'notification.acknowledged') &&
    typeof event.notificationId === 'string' && event.notificationId.length > 0 && event.notificationId.length <= 160 &&
    typeof event.reminderId === 'string' && event.reminderId.length > 0 && event.reminderId.length <= 160 &&
    (event.status === 'delivered' || event.status === 'acknowledged') && Number.isInteger(event.notificationVersion) &&
    Number(event.notificationVersion) > 0 && typeof event.occurredAt === 'string' && Number.isFinite(Date.parse(event.occurredAt));
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
