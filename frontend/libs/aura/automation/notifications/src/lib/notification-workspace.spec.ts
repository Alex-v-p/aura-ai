import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';
import type { Notification } from '@aura/aura-api-client';
import { appendNotificationPage, type NotificationCollection } from './notification-models';

const page = readFileSync('libs/aura/automation/notifications/src/lib/notifications-page.component.html', 'utf8');
const store = readFileSync('libs/aura/automation/notifications/src/lib/notification-store.ts', 'utf8');
const api = readFileSync('libs/aura/automation/notifications/src/lib/notifications-api.ts', 'utf8');

describe('notification centre contracts', () => {
  it('keeps the server-authoritative unread count when loading another page', () => {
    const first: Notification = { id: 'notification-1', reminderId: 'reminder-1', status: 'delivered', version: 1, createdAt: '2026-10-08T09:00:00.000Z', updatedAt: '2026-10-08T09:00:00.000Z', deliveredAt: '2026-10-08T09:00:00.000Z', acknowledgedAt: null };
    const second: Notification = { ...first, id: 'notification-2', reminderId: 'reminder-2' };
    const current: NotificationCollection = { items: [first], nextCursor: 'cursor-1', unacknowledgedCount: 4 };
    const next: NotificationCollection = { items: [second], nextCursor: null, unacknowledgedCount: 3 };
    const merged = appendNotificationPage(current, next);
    expect(merged.items.map((item) => item.id)).toEqual(['notification-1', 'notification-2']);
    expect(merged.unacknowledgedCount).toBe(3);
  });

  it('keeps the inbox actions accessible and durable', () => {
    expect(page).toContain('aria-labelledby="notifications-heading"');
    expect(page).toContain('Acknowledge');
    expect(page).toContain('Mark reminder complete');
    expect(page).toContain('<ul class="notification-list"');
    expect(page).toContain('role="dialog"');
  });

  it('reconciles durable state after metadata-only SSE updates with bounded retry', () => {
    expect(store).toContain('void this.refresh()');
    expect(store).toContain('this.lastEventId = event.eventId');
    expect(store).toContain('retryDelaysMs');
    expect(store).toContain('this.api.stream(this.lastEventId');
  });

  it('uses the generated notification operations and owner mutation headers', () => {
    expect(api).toContain("this.client.execute('listNotifications'");
    expect(api).toContain("this.client.execute('getNotification'");
    expect(api).toContain("this.client.execute('acknowledgeNotification'");
    expect(api).toContain("'Last-Event-ID': lastEventId");
    expect(api).toContain("'X-CSRF-Token': this.csrf");
    expect(api).toContain("'Idempotency-Key': idempotencyKey()");
  });
});
