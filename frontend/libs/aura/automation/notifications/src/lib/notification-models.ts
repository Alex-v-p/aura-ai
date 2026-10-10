import type { Notification, NotificationLifecycleEvent, NotificationPage, NotificationStatus } from '@aura/aura-api-client';

export type NotificationRecord = Notification;
export type NotificationEvent = NotificationLifecycleEvent;
export type { NotificationPage, NotificationStatus };

export type NotificationCollection = NotificationPage;

export function appendNotificationPage(current: NotificationCollection, next: NotificationCollection): NotificationCollection {
  return { items: [...current.items, ...next.items], nextCursor: next.nextCursor, unacknowledgedCount: next.unacknowledgedCount };
}

export interface NotificationsApi {
  list(cursor?: string, status?: NotificationStatus): Promise<NotificationPage>;
  get(id: string): Promise<NotificationRecord>;
  acknowledge(id: string, expectedVersion: number): Promise<NotificationRecord>;
  stream(lastEventId: string | undefined, onEvent: (event: NotificationEvent) => void, onError: (error: NotificationApiError) => void): NotificationStream;
}

export interface NotificationStream {
  close(): void;
}

export class NotificationApiError extends Error {
  constructor(readonly status: number | null, message: string, readonly retryable = true) {
    super(message);
    this.name = 'NotificationApiError';
  }
}
