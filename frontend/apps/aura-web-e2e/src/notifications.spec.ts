import { expect, test } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';

test.describe('notification centre', () => {
  test('shows the durable inbox, acknowledges, completes the reminder, and resumes SSE', async ({ page }) => {
    let acknowledged = false;
    let completed = false;
    let streamRequests = 0;
    const streamHeaders: string[] = [];
    const now = '2099-10-08T09:00:00.000Z';
    const reminder = { id: 'reminder-notification', content: 'Water the plants', dueAt: '2099-10-08T08:59:00.000Z', status: 'fired', version: 2, createdAt: now, updatedAt: now, firedAt: now, completedAt: null, canceledAt: null };
    const notification = () => ({ id: 'notification-e2e', reminderId: reminder.id, status: acknowledged ? 'acknowledged' : 'delivered', version: acknowledged ? 2 : 1, createdAt: now, updatedAt: now, deliveredAt: now, acknowledgedAt: acknowledged ? now : null });

    await page.route('**/api/v1/**', async (route) => {
      const request = route.request();
      const url = new URL(request.url());
      if (url.pathname.endsWith('/auth/session')) return route.fulfill({ json: { principal: { issuer: 'https://authentik.test', subject: 'fixture-owner', displayName: 'Fixture Owner' }, csrfToken: 'fixture-csrf', idleExpiresAt: now, absoluteExpiresAt: '2099-10-09T09:00:00.000Z' } });
      if (url.pathname.endsWith('/models')) return route.fulfill({ json: { models: [], defaultModelId: null, observedAt: now } });
      if (url.pathname.endsWith('/agents')) return route.fulfill({ json: { items: [] } });
      if (url.pathname.endsWith('/personas')) return route.fulfill({ json: { items: [] } });
      if (url.pathname.endsWith('/conversations')) return route.fulfill({ json: { items: [], nextCursor: null } });
      if (url.pathname.endsWith('/notifications/stream')) {
        streamRequests += 1;
        streamHeaders.push(request.headers()['last-event-id'] ?? '');
        const body = streamRequests === 1
          ? `id: notification-event-1\nevent: notification.delivered\ndata: ${JSON.stringify({ schemaVersion: 1, eventId: 'notification-event-1', eventType: 'notification.delivered', notificationId: 'notification-e2e', reminderId: reminder.id, status: 'delivered', notificationVersion: 1, occurredAt: now })}\n\n`
          : `event: heartbeat\ndata: ${JSON.stringify({ schemaVersion: 1, eventType: 'heartbeat', occurredAt: now })}\n\n`;
        return route.fulfill({ status: 200, headers: { 'content-type': 'text/event-stream' }, body });
      }
      if (url.pathname === '/api/v1/notifications' && request.method() === 'GET') return route.fulfill({ json: { items: [notification()], nextCursor: null, unacknowledgedCount: acknowledged ? 0 : 1 } });
      if (url.pathname.endsWith('/notifications/notification-e2e') && request.method() === 'GET') return route.fulfill({ json: notification() });
      if (url.pathname.endsWith('/notifications/notification-e2e/acknowledge') && request.method() === 'POST') { acknowledged = true; return route.fulfill({ json: notification() }); }
      if (url.pathname.endsWith('/reminders/reminder-notification') && request.method() === 'GET') return route.fulfill({ json: reminder });
      if (url.pathname.endsWith('/reminders/reminder-notification/complete') && request.method() === 'POST') { completed = true; return route.fulfill({ json: { ...reminder, status: 'completed', version: 3, completedAt: now } }); }
      if (url.pathname.endsWith('/reminders') && request.method() === 'GET') return route.fulfill({ json: { items: [], nextCursor: null } });
      return route.fulfill({ status: 404, json: { detail: 'fixture endpoint not implemented' } });
    });

    await page.goto('/notifications');
    await expect(page.getByRole('heading', { name: 'Notifications', exact: true })).toBeVisible();
    const mobileNavigationTrigger = page.getByRole('button', { name: 'Open navigation' });
    if (await mobileNavigationTrigger.isVisible()) await mobileNavigationTrigger.click();
    await expect(page.getByRole('link', { name: 'Notifications, 1 unread' })).toBeVisible();
    await expect(page.getByText('A reminder is due.', { exact: true })).toBeVisible();

    await page.getByRole('button', { name: 'Open notification: A reminder is due.' }).click();
    await expect(page.getByRole('dialog', { name: 'Water the plants' })).toBeVisible();
    await page.getByRole('button', { name: 'Mark reminder complete' }).click();
    await expect.poll(() => completed).toBe(true);
    await page.getByRole('dialog').getByRole('button', { name: 'Acknowledge' }).click();
    await expect.poll(() => acknowledged).toBe(true);
    await expect(page.getByRole('link', { name: 'Notifications', exact: true })).toBeVisible();
    await expect.poll(() => streamRequests).toBeGreaterThanOrEqual(2);
    expect(streamHeaders[1]).toBe('notification-event-1');

    const results = await new AxeBuilder({ page }).include('.notifications-page').analyze();
    expect(results.violations).toEqual([]);
  });
});
