import { expect, test } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';

const session = { principal: { issuer: 'https://authentik.test', subject: 'fixture-owner', displayName: 'Fixture Owner' }, csrfToken: 'fixture-csrf', idleExpiresAt: '2099-10-08T10:00:00Z', absoluteExpiresAt: '2099-10-09T09:00:00Z' };
const now = '2099-10-08T09:00:00.000Z';

test.describe('notification centre', () => {
  test('renders a typed briefing source, acknowledges it, and keeps the deferred result route safe', async ({ page }) => {
    let acknowledged = false;
    let briefingResultRequests = 0;
    const briefing = {
      id: 'notification-briefing',
      briefingScheduleId: 'briefing-schedule-private',
      briefingOccurrenceId: 'briefing-occurrence-private',
      briefingResultId: 'briefing-result-private',
      status: 'delivered',
      version: 1,
      createdAt: now,
      updatedAt: now,
      deliveredAt: now,
      acknowledgedAt: null,
    };
    const current = () => ({
      ...briefing,
      status: acknowledged ? 'acknowledged' : 'delivered',
      version: acknowledged ? 2 : 1,
      acknowledgedAt: acknowledged ? now : null,
    });

    await page.route('**/api/v1/**', async (route) => {
      const request = route.request();
      const url = new URL(request.url());
      if (url.pathname.endsWith('/auth/session')) return route.fulfill({ json: session });
      if (url.pathname.endsWith('/models')) return route.fulfill({ json: { models: [], defaultModelId: null, observedAt: now } });
      if (url.pathname.endsWith('/agents')) return route.fulfill({ json: { items: [] } });
      if (url.pathname.endsWith('/personas')) return route.fulfill({ json: { items: [] } });
      if (url.pathname.endsWith('/conversations')) return route.fulfill({ json: { items: [], nextCursor: null } });
      if (url.pathname.endsWith('/notifications/stream')) return route.fulfill({ status: 200, headers: { 'content-type': 'text/event-stream' }, body: 'event: heartbeat\ndata: {"schemaVersion":1,"eventType":"heartbeat","occurredAt":"2099-10-08T09:00:00.000Z"}\n\n' });
      if (url.pathname === '/api/v1/notifications' && request.method() === 'GET') return route.fulfill({ json: { items: [current()], nextCursor: null, unacknowledgedCount: acknowledged ? 0 : 1 } });
      if (url.pathname.endsWith('/notifications/notification-briefing') && request.method() === 'GET') return route.fulfill({ json: current() });
      if (url.pathname.endsWith('/notifications/notification-briefing/acknowledge') && request.method() === 'POST') { acknowledged = true; return route.fulfill({ json: current() }); }
      if (url.pathname.includes('/briefing-results/')) { briefingResultRequests += 1; return route.fulfill({ status: 404, json: { detail: 'briefing result route is not available in this slice' } }); }
      return route.fulfill({ status: 404, json: { detail: 'fixture endpoint not implemented' } });
    });

    await page.goto('/notifications');
    await expect(page.getByText('Your daily briefing is ready.', { exact: true })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Open notification: Your daily briefing is ready.', exact: true })).toBeVisible();

    await page.getByRole('button', { name: 'Open notification: Your daily briefing is ready.', exact: true }).click();
    const detail = page.getByRole('dialog', { name: 'Your daily briefing is ready.' });
    await expect(detail).toBeVisible();
    await expect(detail.getByRole('heading', { name: 'Dedicated briefing result', exact: true })).toBeVisible();
    const resultLink = detail.getByRole('link', {
      name: 'Open dedicated briefing result',
      exact: true,
    });
    await expect(resultLink).toHaveAttribute(
      'href',
      /briefings\/briefing-schedule-private\/occurrences\/briefing-occurrence-private$/,
    );
    await expect(page).toHaveURL(/\/notifications\/notification-briefing$/);
    expect(page.url()).not.toContain('briefing-result-private');
    expect(briefingResultRequests).toBe(0);

    await detail.getByRole('button', { name: 'Acknowledge', exact: true }).click();
    await expect.poll(() => acknowledged).toBe(true);
    await expect(detail.getByRole('status')).toContainText('Acknowledged');
    expect(page.url()).not.toContain('Fixture Owner');
    const results = await new AxeBuilder({ page }).include('.notifications-page').analyze();
    expect(results.violations).toEqual([]);
    await resultLink.click();
    await expect(page).toHaveURL(
      /\/briefings\/briefing-schedule-private\/occurrences\/briefing-occurrence-private$/,
    );
  });

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
          ? `id: 11111111-1111-4111-8111-111111111111\nevent: notification.delivered\ndata: ${JSON.stringify({ schemaVersion: 1, eventId: '11111111-1111-4111-8111-111111111111', eventType: 'notification.delivered', notificationId: 'notification-e2e', reminderId: reminder.id, status: 'delivered', notificationVersion: 1, occurredAt: now })}\n\n`
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
    // This fixture starts on /notifications. Inspecting the active destination
    // does not produce a NavigationEnd, so close the drawer explicitly before
    // interacting with the content beneath it.
    const mobileNavigationClose = page.locator('#primary-navigation').getByRole('button', { name: 'Close navigation' });
    if (await mobileNavigationClose.isVisible()) await mobileNavigationClose.click();
    await expect(page.locator('#primary-navigation')).not.toHaveClass(/is-mobile-open/);
    await expect(page.getByText('A reminder is due.', { exact: true })).toBeVisible();

    await page.getByRole('button', { name: 'Open notification: A reminder is due.' }).click();
    await expect(page.getByRole('dialog', { name: 'Water the plants' })).toBeVisible();
    await page.getByRole('button', { name: 'Mark reminder complete' }).click();
    await expect.poll(() => completed).toBe(true);
    await page.getByRole('dialog').getByRole('button', { name: 'Acknowledge' }).click();
    await expect.poll(() => acknowledged).toBe(true);
    if (await mobileNavigationTrigger.isVisible()) {
      await page.getByRole('dialog').getByRole('button', { name: 'Close notification details' }).click();
      await mobileNavigationTrigger.click();
      await expect(page.getByRole('link', { name: 'Notifications', exact: true })).toBeVisible();
      await page.locator('#primary-navigation').getByRole('button', { name: 'Close navigation' }).click();
    } else {
      await expect(page.getByRole('link', { name: 'Notifications', exact: true })).toBeVisible();
    }
    await expect.poll(() => streamRequests).toBeGreaterThanOrEqual(2);
    expect(streamHeaders[1]).toBe('11111111-1111-4111-8111-111111111111');

    const results = await new AxeBuilder({ page }).include('.notifications-page').analyze();
    expect(results.violations).toEqual([]);
  });

  test('separates unread and history, exposes source failure, and restores focus after Escape', async ({ page }) => {
    const unread = { id: 'notification-unread', reminderId: 'reminder-source', status: 'delivered', version: 1, createdAt: now, updatedAt: now, deliveredAt: now, acknowledgedAt: null };
    const history = { id: 'notification-history', reminderId: 'reminder-source', status: 'acknowledged', version: 2, createdAt: now, updatedAt: now, deliveredAt: now, acknowledgedAt: now };
    const reminder = { id: 'reminder-source', content: 'Check the source reminder', dueAt: now, status: 'fired', version: 1, createdAt: now, updatedAt: now, firedAt: now, completedAt: null, canceledAt: null };
    let sourceAttempts = 0;
    await page.route('**/api/v1/**', async (route) => {
      const request = route.request();
      const url = new URL(request.url());
      if (url.pathname.endsWith('/auth/session')) return route.fulfill({ json: session });
      if (url.pathname.endsWith('/models')) return route.fulfill({ json: { models: [], defaultModelId: null, observedAt: now } });
      if (url.pathname.endsWith('/agents')) return route.fulfill({ json: { items: [] } });
      if (url.pathname.endsWith('/personas')) return route.fulfill({ json: { items: [] } });
      if (url.pathname.endsWith('/conversations')) return route.fulfill({ json: { items: [], nextCursor: null } });
      if (url.pathname.endsWith('/notifications/stream')) return route.fulfill({ status: 200, headers: { 'content-type': 'text/event-stream' }, body: 'event: heartbeat\ndata: {"schemaVersion":1,"eventType":"heartbeat","occurredAt":"2099-10-08T09:00:00.000Z"}\n\n' });
      if (url.pathname === '/api/v1/notifications' && request.method() === 'GET') return route.fulfill({ json: url.searchParams.get('status') === 'acknowledged' ? { items: [history], nextCursor: null, unacknowledgedCount: 0 } : { items: [unread], nextCursor: null, unacknowledgedCount: 1 } });
      if (url.pathname.endsWith('/notifications/notification-unread') && request.method() === 'GET') return route.fulfill({ json: unread });
      if (url.pathname.endsWith('/notifications/notification-history') && request.method() === 'GET') return route.fulfill({ json: history });
      if (url.pathname.endsWith('/reminders/reminder-source') && request.method() === 'GET') {
        sourceAttempts += 1;
        return sourceAttempts === 1 ? route.fulfill({ status: 503, json: { detail: 'The source reminder is temporarily unavailable.' } }) : route.fulfill({ json: reminder });
      }
      if (url.pathname.endsWith('/reminders') && request.method() === 'GET') return route.fulfill({ json: { items: [], nextCursor: null } });
      return route.fulfill({ status: 404, json: { detail: 'fixture endpoint not implemented' } });
    });

    await page.goto('/notifications');
    await expect(page.getByText('A reminder is due.', { exact: true })).toBeVisible();
    const opener = page.getByRole('button', { name: 'Open notification: A reminder is due.', exact: true });
    await page.getByRole('button', { name: /History/ }).click();
    await expect(page.getByText('A reminder is due.', { exact: true })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Open notification: A reminder is due.', exact: true })).toHaveCount(1);
    await page.getByRole('button', { name: /Unread/ }).click();
    await expect(opener).toBeVisible();

    await opener.click();
    const detail = page.getByRole('dialog');
    await expect(detail).toBeVisible();
    await expect(detail.locator('.source-card').getByRole('alert').filter({ hasText: 'temporarily unavailable' })).toBeVisible();
    await expect(detail.getByRole('button', { name: 'Retry source reminder', exact: true })).toBeVisible();
    await detail.getByRole('button', { name: 'Retry source reminder', exact: true }).click();
    await expect(detail.locator('.source-card').getByText(reminder.content, { exact: true })).toBeVisible();
    await page.keyboard.press('Escape');
    await expect(detail).toBeHidden();
    await expect(opener).toBeFocused();

    const panelBox = await page.locator('.notifications-page').boundingBox();
    expect(panelBox?.width ?? 0).toBeGreaterThan(0);
    expect(panelBox?.width ?? 0).toBeLessThanOrEqual(page.viewportSize()?.width ?? Number.POSITIVE_INFINITY);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    const results = await new AxeBuilder({ page }).include('.notifications-page').analyze();
    expect(results.violations).toEqual([]);

    await opener.click();
    await page.getByRole('dialog').getByRole('button', { name: 'Open reminder', exact: true }).click();
    await expect(page).toHaveURL(/\/reminders\/reminder-source$/);
    await expect(page.getByRole('heading', { name: 'Reminders', exact: true })).toBeVisible();
  });

  test('clears stale notification state after an authorization reset and starts clean on reload', async ({ page }) => {
    const notification = { id: 'notification-reset', reminderId: 'reminder-reset', status: 'delivered', version: 1, createdAt: now, updatedAt: now, deliveredAt: now, acknowledgedAt: null };
    let listAttempts = 0;
    await page.route('**/api/v1/**', async (route) => {
      const request = route.request();
      const url = new URL(request.url());
      if (url.pathname.endsWith('/auth/session')) return route.fulfill({ json: session });
      if (url.pathname.endsWith('/models')) return route.fulfill({ json: { models: [], defaultModelId: null, observedAt: now } });
      if (url.pathname.endsWith('/agents')) return route.fulfill({ json: { items: [] } });
      if (url.pathname.endsWith('/personas')) return route.fulfill({ json: { items: [] } });
      if (url.pathname.endsWith('/conversations')) return route.fulfill({ json: { items: [], nextCursor: null } });
      if (url.pathname.endsWith('/notifications/stream')) return route.fulfill({ status: 200, headers: { 'content-type': 'text/event-stream' }, body: 'event: heartbeat\ndata: {"schemaVersion":1,"eventType":"heartbeat","occurredAt":"2099-10-08T09:00:00.000Z"}\n\n' });
      if (url.pathname === '/api/v1/notifications' && request.method() === 'GET') {
        listAttempts += 1;
        return listAttempts === 1 ? route.fulfill({ status: 401, json: { detail: 'expired session' } }) : route.fulfill({ json: { items: [notification], nextCursor: null, unacknowledgedCount: 1 } });
      }
      if (url.pathname.endsWith('/reminders') && request.method() === 'GET') return route.fulfill({ json: { items: [], nextCursor: null } });
      return route.fulfill({ status: 404, json: { detail: 'fixture endpoint not implemented' } });
    });

    await page.goto('/notifications');
    await expect(page.getByRole('alert').filter({ hasText: 'session has expired' })).toBeVisible();
    await expect(page.getByText('A reminder is due.', { exact: true })).toHaveCount(0);
    await page.reload();
    await expect(page.getByRole('button', { name: 'Open notification: A reminder is due.', exact: true })).toBeVisible();
  });

  test('keeps the newest notification detail when an older route response arrives late', async ({ page }) => {
    const first = { id: 'notification-a', reminderId: 'reminder-a', status: 'delivered', version: 1, createdAt: now, updatedAt: now, deliveredAt: now, acknowledgedAt: null };
    const second = { id: 'notification-b', reminderId: 'reminder-b', status: 'delivered', version: 1, createdAt: now, updatedAt: now, deliveredAt: now, acknowledgedAt: null };
    const firstReminder = { id: 'reminder-a', content: 'First source reminder', dueAt: now, status: 'fired', version: 1, createdAt: now, updatedAt: now, firedAt: now, completedAt: null, canceledAt: null };
    const secondReminder = { id: 'reminder-b', content: 'Second source reminder', dueAt: now, status: 'fired', version: 1, createdAt: now, updatedAt: now, firedAt: now, completedAt: null, canceledAt: null };
    let resolveFirst: (() => void) | undefined;
    let resolveSecond: (() => void) | undefined;
    const firstReady = new Promise<void>((resolve) => { resolveFirst = resolve; });
    const secondReady = new Promise<void>((resolve) => { resolveSecond = resolve; });
    await page.route('**/api/v1/**', async (route) => {
      const request = route.request();
      const url = new URL(request.url());
      if (url.pathname.endsWith('/auth/session')) return route.fulfill({ json: session });
      if (url.pathname.endsWith('/models')) return route.fulfill({ json: { models: [], defaultModelId: null, observedAt: now } });
      if (url.pathname.endsWith('/agents')) return route.fulfill({ json: { items: [] } });
      if (url.pathname.endsWith('/personas')) return route.fulfill({ json: { items: [] } });
      if (url.pathname.endsWith('/conversations')) return route.fulfill({ json: { items: [], nextCursor: null } });
      if (url.pathname.endsWith('/notifications') && request.method() === 'GET') return route.fulfill({ json: { items: [], nextCursor: null, unacknowledgedCount: 0 } });
      if (url.pathname.endsWith('/notifications/notification-a') && request.method() === 'GET') { await firstReady; return route.fulfill({ json: first }); }
      if (url.pathname.endsWith('/notifications/notification-b') && request.method() === 'GET') { await secondReady; return route.fulfill({ json: second }); }
      if (url.pathname.endsWith('/reminders/reminder-a') && request.method() === 'GET') return route.fulfill({ json: firstReminder });
      if (url.pathname.endsWith('/reminders/reminder-b') && request.method() === 'GET') return route.fulfill({ json: secondReminder });
      if (url.pathname.endsWith('/reminders') && request.method() === 'GET') return route.fulfill({ json: { items: [], nextCursor: null } });
      return route.fulfill({ status: 404, json: { detail: 'fixture endpoint not implemented' } });
    });

    await page.goto('/notifications/notification-a');
    await expect(page.getByRole('status').filter({ hasText: 'Loading notification…' })).toBeVisible();
    await page.goto('/notifications/notification-b');
    await expect(page.getByRole('status').filter({ hasText: 'Loading notification…' })).toBeVisible();
    resolveSecond?.();
    await expect(page.getByRole('dialog')).toBeVisible();
    await expect(page.getByRole('dialog').locator('.source-card').getByText(secondReminder.content, { exact: true })).toBeVisible();
    resolveFirst?.();
    await page.waitForTimeout(50);
    await expect(page.getByRole('dialog')).toBeVisible();
    await expect(page.getByRole('dialog').locator('.source-card').getByText(secondReminder.content, { exact: true })).toBeVisible();
  });

  test('does not apply a delayed acknowledgement failure after navigating to another notification', async ({ page }) => {
    const first = { id: 'notification-ack-a', reminderId: 'reminder-ack-a', status: 'delivered', version: 1, createdAt: now, updatedAt: now, deliveredAt: now, acknowledgedAt: null };
    const second = { id: 'notification-ack-b', reminderId: 'reminder-ack-b', status: 'delivered', version: 1, createdAt: now, updatedAt: now, deliveredAt: now, acknowledgedAt: null };
    const firstReminder = { id: 'reminder-ack-a', content: 'First acknowledgement reminder', dueAt: now, status: 'fired', version: 1, createdAt: now, updatedAt: now, firedAt: now, completedAt: null, canceledAt: null };
    const secondReminder = { id: 'reminder-ack-b', content: 'Second acknowledgement reminder', dueAt: now, status: 'fired', version: 1, createdAt: now, updatedAt: now, firedAt: now, completedAt: null, canceledAt: null };
    let acknowledgeStarted = false;
    let releaseAcknowledge: (() => void) | undefined;
    const acknowledgeReady = new Promise<void>((resolve) => { releaseAcknowledge = resolve; });
    await page.route('**/api/v1/**', async (route) => {
      const request = route.request();
      const url = new URL(request.url());
      if (url.pathname.endsWith('/auth/session')) return route.fulfill({ json: session });
      if (url.pathname.endsWith('/models')) return route.fulfill({ json: { models: [], defaultModelId: null, observedAt: now } });
      if (url.pathname.endsWith('/agents')) return route.fulfill({ json: { items: [] } });
      if (url.pathname.endsWith('/personas')) return route.fulfill({ json: { items: [] } });
      if (url.pathname.endsWith('/conversations')) return route.fulfill({ json: { items: [], nextCursor: null } });
      if (url.pathname.endsWith('/notifications/stream')) return route.fulfill({ status: 200, headers: { 'content-type': 'text/event-stream' }, body: 'event: heartbeat\ndata: {"schemaVersion":1,"eventType":"heartbeat","occurredAt":"2099-10-08T09:00:00Z"}\n\n' });
      if (url.pathname === '/api/v1/notifications' && request.method() === 'GET') return route.fulfill({ json: { items: [first, second], nextCursor: null, unacknowledgedCount: 2 } });
      if (url.pathname.endsWith('/notifications/notification-ack-a') && request.method() === 'GET') return route.fulfill({ json: first });
      if (url.pathname.endsWith('/notifications/notification-ack-b') && request.method() === 'GET') return route.fulfill({ json: second });
      if (url.pathname.endsWith('/notifications/notification-ack-a/acknowledge') && request.method() === 'POST') { acknowledgeStarted = true; await acknowledgeReady; return route.fulfill({ status: 503, json: { detail: 'acknowledgement unavailable' } }); }
      if (url.pathname.endsWith('/reminders/reminder-ack-a') && request.method() === 'GET') return route.fulfill({ json: firstReminder });
      if (url.pathname.endsWith('/reminders/reminder-ack-b') && request.method() === 'GET') return route.fulfill({ json: secondReminder });
      if (url.pathname.endsWith('/reminders') && request.method() === 'GET') return route.fulfill({ json: { items: [], nextCursor: null } });
      return route.fulfill({ status: 404, json: { detail: 'fixture endpoint not implemented' } });
    });

    await page.goto('/notifications');
    const openers = page.getByRole('button', { name: 'Open notification: A reminder is due.', exact: true });
    await expect(openers).toHaveCount(2);
    const firstRow = page.locator('#notification-notification-ack-a');
    const secondRow = page.locator('#notification-notification-ack-b');
    await expect(firstRow).toBeVisible();
    await expect(secondRow).toBeVisible();
    await firstRow.click();
    const firstDetail = page.getByRole('dialog');
    await expect(firstDetail.getByRole('heading', { name: firstReminder.content, exact: true })).toBeVisible();
    await expect(firstDetail.locator('.source-card').getByText(firstReminder.content, { exact: true })).toBeVisible();
    await firstDetail.getByRole('button', { name: 'Acknowledge', exact: true }).click();
    await expect.poll(() => acknowledgeStarted).toBe(true);
    await expect(firstDetail.getByRole('button', { name: 'Saving…', exact: true })).toBeDisabled();
    await firstDetail.getByRole('button', { name: 'Close notification details', exact: true }).click();
    await secondRow.click();
    const secondDetail = page.getByRole('dialog');
    await expect(secondDetail.getByRole('heading', { name: secondReminder.content, exact: true })).toBeVisible();
    await expect(secondDetail.getByRole('button', { name: 'Acknowledge', exact: true })).toBeVisible();

    releaseAcknowledge?.();
    await page.waitForTimeout(50);
    await expect(secondDetail).toBeVisible();
    await expect(secondDetail.getByRole('heading', { name: secondReminder.content, exact: true })).toBeVisible();
    await expect(secondDetail.getByRole('button', { name: 'Acknowledge', exact: true })).toBeVisible();
    await expect(secondDetail.getByRole('button', { name: 'Saving…', exact: true })).toHaveCount(0);
    await expect(page.getByRole('alert').filter({ hasText: 'could not be acknowledged' })).toHaveCount(0);
  });

  test('does not show stale rows when a view refresh fails and offers a nearby retry', async ({ page }) => {
    const unread = { id: 'notification-current', reminderId: 'reminder-current', status: 'delivered', version: 1, createdAt: now, updatedAt: now, deliveredAt: now, acknowledgedAt: null };
    const history = { ...unread, id: 'notification-history', status: 'acknowledged', version: 2, acknowledgedAt: now };
    let unreadRequests = 0;
    await page.route('**/api/v1/**', async (route) => {
      const request = route.request();
      const url = new URL(request.url());
      if (url.pathname.endsWith('/auth/session')) return route.fulfill({ json: session });
      if (url.pathname.endsWith('/models')) return route.fulfill({ json: { models: [], defaultModelId: null, observedAt: now } });
      if (url.pathname.endsWith('/agents')) return route.fulfill({ json: { items: [] } });
      if (url.pathname.endsWith('/personas')) return route.fulfill({ json: { items: [] } });
      if (url.pathname.endsWith('/conversations')) return route.fulfill({ json: { items: [], nextCursor: null } });
      if (url.pathname.endsWith('/notifications/stream')) return route.fulfill({ status: 200, headers: { 'content-type': 'text/event-stream' }, body: 'event: heartbeat\ndata: {"schemaVersion":1,"eventType":"heartbeat","occurredAt":"2099-10-08T09:00:00Z"}\n\n' });
      if (url.pathname === '/api/v1/notifications' && request.method() === 'GET') {
        if (url.searchParams.get('status') === 'acknowledged') return route.fulfill({ json: { items: [history], nextCursor: null, unacknowledgedCount: 0 } });
        unreadRequests += 1;
        return unreadRequests === 2 ? route.fulfill({ status: 503, json: { detail: 'refresh unavailable' } }) : route.fulfill({ json: { items: [unread], nextCursor: null, unacknowledgedCount: 1 } });
      }
      if (url.pathname.endsWith('/reminders') && request.method() === 'GET') return route.fulfill({ json: { items: [], nextCursor: null } });
      return route.fulfill({ status: 404, json: { detail: 'fixture endpoint not implemented' } });
    });

    await page.goto('/notifications');
    await expect(page.getByText('A reminder is due.', { exact: true })).toBeVisible();
    await page.getByRole('button', { name: /History/ }).click();
    await expect(page.getByText('Acknowledged', { exact: true })).toBeVisible();
    await page.getByRole('button', { name: /Unread/ }).click();
    const unavailable = page.getByRole('alert').filter({ hasText: 'Unread notifications are unavailable' });
    await expect(unavailable).toBeVisible();
    await expect(page.getByText('Acknowledged', { exact: true })).toHaveCount(0);
    await unavailable.getByRole('button', { name: 'Retry', exact: true }).click();
    await expect(page.getByText('A reminder is due.', { exact: true })).toBeVisible();
  });

  test('removes an acknowledged notification without SSE and reconciles the unread count authoritatively', async ({ page }) => {
    const delivered = { id: 'notification-ack', reminderId: 'reminder-ack', status: 'delivered', version: 1, createdAt: now, updatedAt: now, deliveredAt: now, acknowledgedAt: null };
    const acknowledged = { ...delivered, status: 'acknowledged', version: 2, acknowledgedAt: now };
    let listRequests = 0;
    await page.route('**/api/v1/**', async (route) => {
      const request = route.request();
      const url = new URL(request.url());
      if (url.pathname.endsWith('/auth/session')) return route.fulfill({ json: session });
      if (url.pathname.endsWith('/models')) return route.fulfill({ json: { models: [], defaultModelId: null, observedAt: now } });
      if (url.pathname.endsWith('/agents')) return route.fulfill({ json: { items: [] } });
      if (url.pathname.endsWith('/personas')) return route.fulfill({ json: { items: [] } });
      if (url.pathname.endsWith('/conversations')) return route.fulfill({ json: { items: [], nextCursor: null } });
      if (url.pathname.endsWith('/notifications/stream')) return route.fulfill({ status: 404, json: { detail: 'stream unavailable' } });
      if (url.pathname === '/api/v1/notifications' && request.method() === 'GET') {
        listRequests += 1;
        if (url.searchParams.get('status') === 'acknowledged') return route.fulfill({ json: { items: [acknowledged], nextCursor: null, unacknowledgedCount: 0 } });
        return route.fulfill({ json: { items: listRequests === 1 ? [delivered] : [], nextCursor: null, unacknowledgedCount: 0 } });
      }
      if (url.pathname.endsWith('/notifications/notification-ack/acknowledge') && request.method() === 'POST') return route.fulfill({ json: acknowledged });
      if (url.pathname.endsWith('/reminders') && request.method() === 'GET') return route.fulfill({ json: { items: [], nextCursor: null } });
      return route.fulfill({ status: 404, json: { detail: 'fixture endpoint not implemented' } });
    });

    await page.goto('/notifications');
    await expect(page.getByRole('button', { name: 'Acknowledge', exact: true })).toBeVisible();
    await page.getByRole('button', { name: 'Acknowledge', exact: true }).click();
    await expect(page.getByText('A reminder is due.', { exact: true })).toHaveCount(0);
    await expect(page.getByRole('button', { name: /Unread 0/ })).toBeVisible();
    await page.getByRole('button', { name: /History/ }).click();
    await expect(page.getByText('Acknowledged', { exact: true })).toBeVisible();
  });

  test('keeps the authoritative unread count when a refresh arrives before the acknowledgement response', async ({ page }) => {
    const delivered = { id: 'notification-stream-ack', reminderId: 'reminder-stream-ack', status: 'delivered', version: 1, createdAt: now, updatedAt: now, deliveredAt: now, acknowledgedAt: null };
    const acknowledged = { ...delivered, status: 'acknowledged', version: 2, acknowledgedAt: now };
    let listRequests = 0;
    let acknowledgeStarted = false;
    let releaseAcknowledge: (() => void) | undefined;
    let releaseStream: (() => void) | undefined;
    const acknowledgeReady = new Promise<void>((resolve) => { releaseAcknowledge = resolve; });
    const streamReady = new Promise<void>((resolve) => { releaseStream = resolve; });
    await page.route('**/api/v1/**', async (route) => {
      const request = route.request();
      const url = new URL(request.url());
      if (url.pathname.endsWith('/auth/session')) return route.fulfill({ json: session });
      if (url.pathname.endsWith('/models')) return route.fulfill({ json: { models: [], defaultModelId: null, observedAt: now } });
      if (url.pathname.endsWith('/agents')) return route.fulfill({ json: { items: [] } });
      if (url.pathname.endsWith('/personas')) return route.fulfill({ json: { items: [] } });
      if (url.pathname.endsWith('/conversations')) return route.fulfill({ json: { items: [], nextCursor: null } });
      if (url.pathname.endsWith('/notifications/stream')) {
        await streamReady;
        const eventId = '22222222-2222-4222-8222-222222222222';
        return route.fulfill({ status: 200, headers: { 'content-type': 'text/event-stream' }, body: `id: ${eventId}\nevent: notification.acknowledged\ndata: ${JSON.stringify({ schemaVersion: 1, eventId, eventType: 'notification.acknowledged', notificationId: delivered.id, reminderId: delivered.reminderId, status: 'acknowledged', notificationVersion: 2, occurredAt: now })}\n\n` });
      }
      if (url.pathname === '/api/v1/notifications' && request.method() === 'GET') {
        listRequests += 1;
        return listRequests === 1
          ? route.fulfill({ json: { items: [delivered], nextCursor: null, unacknowledgedCount: 1 } })
          : route.fulfill({ json: { items: [], nextCursor: null, unacknowledgedCount: 0 } });
      }
      if (url.pathname.endsWith('/notifications/notification-stream-ack/acknowledge') && request.method() === 'POST') {
        acknowledgeStarted = true;
        releaseStream?.();
        await acknowledgeReady;
        return route.fulfill({ json: acknowledged });
      }
      if (url.pathname.endsWith('/reminders') && request.method() === 'GET') return route.fulfill({ json: { items: [], nextCursor: null } });
      return route.fulfill({ status: 404, json: { detail: 'fixture endpoint not implemented' } });
    });

    await page.goto('/notifications');
    await expect(page.getByRole('button', { name: 'Acknowledge', exact: true })).toBeVisible();
    await page.getByRole('button', { name: 'Acknowledge', exact: true }).click();
    await expect.poll(() => acknowledgeStarted).toBe(true);
    await expect(page.getByRole('button', { name: /Unread 0/ })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Acknowledge', exact: true })).toHaveCount(0);

    releaseAcknowledge?.();
    await expect(page.getByRole('button', { name: /Unread 0/ })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Acknowledge', exact: true })).toHaveCount(0);
  });

  test('keeps notification-side completion visible while saving and reports a recoverable source failure', async ({ page }) => {
    const notification = { id: 'notification-complete-fail', reminderId: 'reminder-complete-fail', status: 'delivered', version: 1, createdAt: now, updatedAt: now, deliveredAt: now, acknowledgedAt: null };
    const reminder = { id: 'reminder-complete-fail', content: 'Complete me carefully', dueAt: now, status: 'fired', version: 1, createdAt: now, updatedAt: now, firedAt: now, completedAt: null, canceledAt: null };
    let releaseComplete: (() => void) | undefined;
    const completeReady = new Promise<void>((resolve) => { releaseComplete = resolve; });
    await page.route('**/api/v1/**', async (route) => {
      const request = route.request();
      const url = new URL(request.url());
      if (url.pathname.endsWith('/auth/session')) return route.fulfill({ json: session });
      if (url.pathname.endsWith('/models')) return route.fulfill({ json: { models: [], defaultModelId: null, observedAt: now } });
      if (url.pathname.endsWith('/agents')) return route.fulfill({ json: { items: [] } });
      if (url.pathname.endsWith('/personas')) return route.fulfill({ json: { items: [] } });
      if (url.pathname.endsWith('/conversations')) return route.fulfill({ json: { items: [], nextCursor: null } });
      if (url.pathname.endsWith('/notifications/stream')) return route.fulfill({ status: 200, headers: { 'content-type': 'text/event-stream' }, body: 'event: heartbeat\ndata: {"schemaVersion":1,"eventType":"heartbeat","occurredAt":"2099-10-08T09:00:00Z"}\n\n' });
      if (url.pathname === '/api/v1/notifications' && request.method() === 'GET') return route.fulfill({ json: { items: [notification], nextCursor: null, unacknowledgedCount: 1 } });
      if (url.pathname.endsWith('/notifications/notification-complete-fail') && request.method() === 'GET') return route.fulfill({ json: notification });
      if (url.pathname.endsWith('/reminders/reminder-complete-fail') && request.method() === 'GET') return route.fulfill({ json: reminder });
      if (url.pathname.endsWith('/reminders/reminder-complete-fail/complete') && request.method() === 'POST') { await completeReady; return route.fulfill({ status: 503, json: { detail: 'completion unavailable' } }); }
      return route.fulfill({ status: 404, json: { detail: 'fixture endpoint not implemented' } });
    });

    await page.goto('/notifications');
    await page.getByRole('button', { name: 'Open notification: A reminder is due.', exact: true }).click();
    const detail = page.getByRole('dialog');
    await expect(detail.locator('.source-card').getByText(reminder.content, { exact: true })).toBeVisible();
    await detail.getByRole('button', { name: 'Mark reminder complete', exact: true }).click();
    await expect(detail.getByRole('button', { name: 'Saving…', exact: true })).toBeDisabled();
    releaseComplete?.();
    await expect(detail.locator('.source-card').getByRole('alert').filter({ hasText: 'temporarily unavailable' })).toBeVisible();
    await expect(detail.getByRole('button', { name: 'Mark reminder complete', exact: true })).toBeEnabled();
  });

  test('reports an unknown acknowledgement outcome without removing the notification', async ({ page }) => {
    const notification = { id: 'notification-unknown', reminderId: 'reminder-unknown', status: 'delivered', version: 1, createdAt: now, updatedAt: now, deliveredAt: now, acknowledgedAt: null };
    await page.route('**/api/v1/**', async (route) => {
      const request = route.request();
      const url = new URL(request.url());
      if (url.pathname.endsWith('/auth/session')) return route.fulfill({ json: session });
      if (url.pathname.endsWith('/models')) return route.fulfill({ json: { models: [], defaultModelId: null, observedAt: now } });
      if (url.pathname.endsWith('/agents')) return route.fulfill({ json: { items: [] } });
      if (url.pathname.endsWith('/personas')) return route.fulfill({ json: { items: [] } });
      if (url.pathname.endsWith('/conversations')) return route.fulfill({ json: { items: [], nextCursor: null } });
      if (url.pathname.endsWith('/notifications/stream')) return route.fulfill({ status: 200, headers: { 'content-type': 'text/event-stream' }, body: 'event: heartbeat\ndata: {"schemaVersion":1,"eventType":"heartbeat","occurredAt":"2099-10-08T09:00:00Z"}\n\n' });
      if (url.pathname === '/api/v1/notifications' && request.method() === 'GET') return route.fulfill({ json: { items: [notification], nextCursor: null, unacknowledgedCount: 1 } });
      if (url.pathname.endsWith('/notifications/notification-unknown/acknowledge') && request.method() === 'POST') return route.abort('failed');
      if (url.pathname.endsWith('/reminders') && request.method() === 'GET') return route.fulfill({ json: { items: [], nextCursor: null } });
      return route.fulfill({ status: 404, json: { detail: 'fixture endpoint not implemented' } });
    });

    await page.goto('/notifications');
    await page.getByRole('button', { name: 'Acknowledge', exact: true }).click();
    await expect(page.getByRole('alert').filter({ hasText: 'outcome is unknown' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Acknowledge', exact: true })).toBeVisible();
  });

  test('surfaces a notification conflict and keeps the acknowledgement action recoverable', async ({ page }) => {
    const notification = { id: 'notification-conflict', reminderId: 'reminder-conflict', status: 'delivered', version: 3, createdAt: now, updatedAt: now, deliveredAt: now, acknowledgedAt: null };
    await page.route('**/api/v1/**', async (route) => {
      const request = route.request();
      const url = new URL(request.url());
      if (url.pathname.endsWith('/auth/session')) return route.fulfill({ json: session });
      if (url.pathname.endsWith('/models')) return route.fulfill({ json: { models: [], defaultModelId: null, observedAt: now } });
      if (url.pathname.endsWith('/agents')) return route.fulfill({ json: { items: [] } });
      if (url.pathname.endsWith('/personas')) return route.fulfill({ json: { items: [] } });
      if (url.pathname.endsWith('/conversations')) return route.fulfill({ json: { items: [], nextCursor: null } });
      if (url.pathname.endsWith('/notifications/stream')) return route.fulfill({ status: 404, json: { detail: 'stream unavailable' } });
      if (url.pathname === '/api/v1/notifications' && request.method() === 'GET') return route.fulfill({ json: { items: [notification], nextCursor: null, unacknowledgedCount: 1 } });
      if (url.pathname.endsWith('/notifications/notification-conflict/acknowledge') && request.method() === 'POST') return route.fulfill({ status: 409, json: { detail: 'changed elsewhere' } });
      if (url.pathname.endsWith('/reminders') && request.method() === 'GET') return route.fulfill({ json: { items: [], nextCursor: null } });
      return route.fulfill({ status: 404, json: { detail: 'fixture endpoint not implemented' } });
    });

    await page.goto('/notifications');
    await page.getByRole('button', { name: 'Acknowledge', exact: true }).click();
    await expect(page.getByRole('alert').filter({ hasText: 'changed elsewhere' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Acknowledge', exact: true })).toBeVisible();
  });

  test('requires successful 410 reconciliation before reconnecting without the expired cursor', async ({ page }) => {
    let streamRequests = 0;
    const streamHeaders: string[] = [];
    let listRequests = 0;
    await page.route('**/api/v1/**', async (route) => {
      const request = route.request();
      const url = new URL(request.url());
      if (url.pathname.endsWith('/auth/session')) return route.fulfill({ json: session });
      if (url.pathname.endsWith('/models')) return route.fulfill({ json: { models: [], defaultModelId: null, observedAt: now } });
      if (url.pathname.endsWith('/agents')) return route.fulfill({ json: { items: [] } });
      if (url.pathname.endsWith('/personas')) return route.fulfill({ json: { items: [] } });
      if (url.pathname.endsWith('/conversations')) return route.fulfill({ json: { items: [], nextCursor: null } });
      if (url.pathname.endsWith('/notifications/stream')) {
        streamRequests += 1;
        streamHeaders.push(request.headers()['last-event-id'] ?? '');
        return streamRequests === 1
          ? route.fulfill({ status: 410, json: { detail: 'cursor expired' } })
          : route.fulfill({ status: 200, headers: { 'content-type': 'text/event-stream' }, body: 'event: heartbeat\ndata: {"schemaVersion":1,"eventType":"heartbeat","occurredAt":"2099-10-08T09:00:00Z"}\n\n' });
      }
      if (url.pathname === '/api/v1/notifications' && request.method() === 'GET') {
        listRequests += 1;
        return listRequests === 2
          ? route.fulfill({ status: 503, json: { detail: 'reconciliation unavailable' } })
          : route.fulfill({ json: { items: [], nextCursor: null, unacknowledgedCount: 0 } });
      }
      if (url.pathname.endsWith('/reminders') && request.method() === 'GET') return route.fulfill({ json: { items: [], nextCursor: null } });
      return route.fulfill({ status: 404, json: { detail: 'fixture endpoint not implemented' } });
    });

    await page.goto('/notifications');
    await expect(page.getByRole('button', { name: 'Retry live updates', exact: true })).toBeVisible();
    await expect.poll(() => streamRequests).toBe(1);
    await expect.poll(() => listRequests).toBe(2);
    await page.waitForTimeout(50);
    expect(streamRequests).toBe(1);

    await page.getByRole('button', { name: 'Retry live updates', exact: true }).click();
    await expect.poll(() => listRequests).toBe(3);
    await expect.poll(() => streamRequests).toBe(2);
    expect(streamHeaders[0]).toBe('');
    expect(streamHeaders[1]).toBe('');
    await expect(page.locator('.connection')).toBeVisible();
  });

  test('caps repeated stream retries and exposes manual reconnect', async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== 'desktop', 'Timer-heavy stream cap coverage runs once in the desktop project.');
    await page.clock.install();
    await page.route('**/api/v1/**', async (route) => {
      const request = route.request();
      const url = new URL(request.url());
      if (url.pathname.endsWith('/auth/session')) return route.fulfill({ json: session });
      if (url.pathname.endsWith('/models')) return route.fulfill({ json: { models: [], defaultModelId: null, observedAt: now } });
      if (url.pathname.endsWith('/agents')) return route.fulfill({ json: { items: [] } });
      if (url.pathname.endsWith('/personas')) return route.fulfill({ json: { items: [] } });
      if (url.pathname.endsWith('/conversations')) return route.fulfill({ json: { items: [], nextCursor: null } });
      if (url.pathname.endsWith('/notifications/stream')) return route.fulfill({ status: 503, json: { detail: 'stream unavailable' } });
      if (url.pathname === '/api/v1/notifications' && request.method() === 'GET') return route.fulfill({ json: { items: [], nextCursor: null, unacknowledgedCount: 0 } });
      if (url.pathname.endsWith('/reminders') && request.method() === 'GET') return route.fulfill({ json: { items: [], nextCursor: null } });
      return route.fulfill({ status: 404, json: { detail: 'fixture endpoint not implemented' } });
    });

    await page.goto('/notifications');
    await expect(page.locator('.connection')).toBeVisible();
    for (const delay of [500, 1_000, 2_000, 4_000, 8_000, 15_000]) {
      await page.clock.fastForward(delay);
      await page.evaluate(() => undefined);
    }
    await expect(page.getByRole('alert').filter({ hasText: 'Live notifications stopped after several attempts' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Retry live updates', exact: true })).toBeVisible();
  });

  test('keeps workflow notifications content-free and exposes an opaque workflow deep link', async ({ page }) => {
    const workflow = { id: 'notification-workflow', workflowId: 'workflow-private', workflowRevisionId: 'revision-private', workflowInstanceId: 'instance-private', workflowStepOrdinal: 3, workflowStepExecutionId: 'execution-private', workflowResultId: 'result-private', status: 'delivered', version: 1, createdAt: now, updatedAt: now, deliveredAt: now, acknowledgedAt: null };
    const workflowEventBase = { schemaVersion: 1, eventType: 'notification.delivered', notificationId: workflow.id, workflowId: '550e8400-e29b-41d4-a716-446655440001', workflowRevisionId: '550e8400-e29b-41d4-a716-446655440002', workflowInstanceId: '550e8400-e29b-41d4-a716-446655440003', workflowStepOrdinal: 3, workflowStepExecutionId: '550e8400-e29b-41d4-a716-446655440004', workflowResultId: '550e8400-e29b-41d4-a716-446655440005', status: 'delivered', notificationVersion: 1, occurredAt: now };
    const malformedWorkflowEvents = [
      { ...workflowEventBase, eventId: '550e8400-e29b-41d4-a716-446655440011', workflowId: 'not-a-uuid' },
      { ...workflowEventBase, eventId: '550e8400-e29b-41d4-a716-446655440012', workflowStepOrdinal: 0 },
      { ...workflowEventBase, eventId: '550e8400-e29b-41d4-a716-446655440015', workflowStepOrdinal: 9 },
      { ...workflowEventBase, eventId: '550e8400-e29b-41d4-a716-446655440013', reminderId: 'reminder-mixed-source' },
      { ...workflowEventBase, eventId: '550e8400-e29b-41d4-a716-446655440014', extra: 'not-in-contract' },
    ];
    const malformedStream = malformedWorkflowEvents.map((event) => `event: notification.delivered\ndata: ${JSON.stringify(event)}\n\n`).join('');
    await page.route('**/api/v1/**', async (route) => {
      const request = route.request();
      const url = new URL(request.url());
      if (url.pathname.endsWith('/auth/session')) return route.fulfill({ json: session });
      if (url.pathname.endsWith('/notifications/stream')) return route.fulfill({ status: 200, headers: { 'content-type': 'text/event-stream' }, body: `${malformedStream}event: heartbeat\ndata: {"schemaVersion":1,"eventType":"heartbeat","occurredAt":"2099-10-08T09:00:00Z"}\n\n` });
      if (url.pathname === '/api/v1/notifications' && request.method() === 'GET') return route.fulfill({ json: { items: [workflow], nextCursor: null, unacknowledgedCount: 1 } });
      if (url.pathname.endsWith('/notifications/notification-workflow') && request.method() === 'GET') return route.fulfill({ json: workflow });
      return route.fulfill({ status: 404, json: { detail: 'fixture endpoint not implemented' } });
    });
    await page.goto('/notifications');
    await expect(page.getByRole('button', { name: 'Open notification: A workflow update is available.', exact: true })).toBeVisible();
    await page.getByRole('button', { name: 'Open notification: A workflow update is available.', exact: true }).click();
    const dialog = page.getByRole('dialog');
    await expect(dialog.getByRole('heading', { name: 'A workflow update is available.', exact: true })).toBeVisible();
    await expect(dialog).toContainText('contains no workflow input, output, instruction, approval, or result content');
    await expect(dialog).toContainText('This update is recorded privately. Workflow details aren’t available in this version.');
    await expect(dialog).not.toContainText('Step 3');
    const workflowLink = dialog.getByRole('link', { name: 'View workflow run', exact: true });
    await expect(workflowLink).toHaveAttribute('href', `/workflows/${workflow.workflowId}/instances/${workflow.workflowInstanceId}`);
    await expect(workflowLink).not.toContainText(/private|result|execution/);
    await expect(dialog.locator('.detail-actions button')).toHaveCount(1);
    await expect(dialog).not.toContainText('result-private');
  });
});
