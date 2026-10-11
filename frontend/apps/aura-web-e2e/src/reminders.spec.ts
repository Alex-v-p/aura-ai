import { expect, test } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';

type ReminderFixture = {
  id: string;
  content: string;
  dueAt: string;
  status: 'scheduled' | 'fired' | 'completed' | 'canceled';
  version: number;
  createdAt: string;
  updatedAt: string;
  firedAt: string | null;
  completedAt: string | null;
  canceledAt: string | null;
};

const session = { principal: { issuer: 'https://authentik.test', subject: 'fixture-owner', displayName: 'Fixture Owner' }, csrfToken: 'fixture-csrf', idleExpiresAt: '2099-10-08T10:00:00Z', absoluteExpiresAt: '2099-10-09T10:00:00Z' };
const isoAt = (dayOffset: number, hour = 12): string => {
  const value = new Date();
  value.setHours(hour, 0, 0, 0);
  value.setDate(value.getDate() + dayOffset);
  return value.toISOString();
};
const reminder = (id: string, content: string, dueAt: string, status: ReminderFixture['status'] = 'scheduled'): ReminderFixture => ({ id, content, dueAt, status, version: 1, createdAt: isoAt(-2, 9), updatedAt: isoAt(-2, 9), firedAt: status === 'fired' ? dueAt : null, completedAt: status === 'completed' ? isoAt(-1, 13) : null, canceledAt: status === 'canceled' ? isoAt(-1, 13) : null });

test.describe('reminder workspace', () => {
  test('creates and renders an owner reminder in local time', async ({ page }) => {
    const reminder = { id: 'reminder-e2e', content: 'Water the plants', dueAt: '2099-10-09T16:30:00Z', status: 'scheduled', version: 1, createdAt: '2099-10-08T09:00:00Z', updatedAt: '2099-10-08T09:00:00Z', firedAt: null, completedAt: null, canceledAt: null };
    await page.route('**/api/v1/**', async (route) => {
      const url = new URL(route.request().url());
      if (url.pathname.endsWith('/auth/session')) return route.fulfill({ json: { principal: { issuer: 'https://authentik.test', subject: 'fixture-owner', displayName: 'Fixture Owner' }, csrfToken: 'fixture-csrf', idleExpiresAt: '2099-10-08T10:00:00Z', absoluteExpiresAt: '2099-10-09T10:00:00Z' } });
      if (url.pathname.endsWith('/conversations')) return route.fulfill({ json: { items: [], nextCursor: null } });
      if (url.pathname.endsWith('/agents')) return route.fulfill({ json: { items: [] } });
      if (url.pathname.endsWith('/models')) return route.fulfill({ json: { models: [], defaultModelId: null, observedAt: '2099-10-08T09:00:00Z' } });
      if (url.pathname.endsWith('/reminders') && route.request().method() === 'GET') return route.fulfill({ json: url.searchParams.get('view') === 'history' ? { items: [], nextCursor: null } : { items: [reminder], nextCursor: null } });
      if (url.pathname.endsWith('/reminders') && route.request().method() === 'POST') {
        const body = route.request().postDataJSON() as { content?: string; dueAt?: string };
        return route.fulfill({ json: { ...reminder, content: body.content ?? reminder.content, dueAt: body.dueAt ?? reminder.dueAt } });
      }
      return route.fulfill({ status: 404, json: { detail: 'fixture endpoint not implemented' } });
    });
    await page.goto('/reminders');
    await expect(page.getByRole('heading', { name: 'Reminders', exact: true })).toBeVisible();
    await expect(page.getByText('Water the plants', { exact: true })).toBeVisible();
    await page.getByRole('button', { name: 'New reminder' }).click();
    await page.getByLabel('Message').fill('Take the recycling out');
    await page.getByRole('button', { name: 'Save reminder' }).click();
    await expect(page.getByRole('button', { name: 'Inspect reminder: Take the recycling out', exact: true })).toHaveCount(1);
    const results = await new AxeBuilder({ page }).include('.reminders-page').analyze();
    expect(results.violations).toEqual([]);
  });

  test('groups the agenda, keeps history separate, and gates transitions behind detail review', async ({ page }) => {
    const today = reminder('today', 'Today commitment', isoAt(0, 9));
    const tomorrow = reminder('tomorrow', 'Tomorrow commitment', isoAt(1, 10));
    const thisWeek = reminder('this-week', 'This week commitment', isoAt(3, 11));
    const later = reminder('later', 'Later commitment', isoAt(10, 12));
    const history = reminder('history', 'Finished commitment', isoAt(-1, 12), 'completed');
    const fixtures = [today, tomorrow, thisWeek, later];
    let completed = false;

    await page.route('**/api/v1/**', async (route) => {
      const request = route.request();
      const url = new URL(request.url());
      if (url.pathname.endsWith('/auth/session')) return route.fulfill({ json: session });
      if (url.pathname.endsWith('/conversations')) return route.fulfill({ json: { items: [], nextCursor: null } });
      if (url.pathname.endsWith('/agents')) return route.fulfill({ json: { items: [] } });
      if (url.pathname.endsWith('/models')) return route.fulfill({ json: { models: [], defaultModelId: null, observedAt: isoAt(0, 8) } });
      if (url.pathname.endsWith('/reminders') && request.method() === 'GET') return route.fulfill({ json: url.searchParams.get('view') === 'history' ? { items: [history], nextCursor: null } : { items: fixtures, nextCursor: null } });
      if (url.pathname.match(/\/reminders\/(today|tomorrow|this-week|later|history)$/) && request.method() === 'GET') {
        const id = url.pathname.split('/').pop();
        return route.fulfill({ json: fixtures.find((item) => item.id === id) ?? history });
      }
      if (url.pathname.endsWith('/reminders/today/complete') && request.method() === 'POST') { completed = true; return route.fulfill({ json: { ...today, status: 'completed', completedAt: isoAt(0, 13), version: 2 } }); }
      if (url.pathname.endsWith('/reminders') && request.method() === 'POST') return route.fulfill({ json: today });
      return route.fulfill({ status: 404, json: { detail: 'fixture endpoint not implemented' } });
    });

    await page.goto('/reminders');
    await expect(page.getByRole('heading', { name: 'Today', exact: true })).toBeVisible();
    await expect(page.getByRole('heading', { name: 'Tomorrow', exact: true })).toBeVisible();
    await expect(page.getByRole('heading', { name: 'This week', exact: true })).toBeVisible();
    await expect(page.getByRole('heading', { name: 'Later', exact: true })).toBeVisible();

    // This is an intentional contract assertion: transitions require detail review.
    const todayRow = page.locator('.reminder-item').filter({ hasText: today.content });
    await expect.soft(todayRow.getByRole('button', { name: 'Complete', exact: true })).toHaveCount(0);
    await expect.soft(todayRow.getByRole('button', { name: 'Cancel', exact: true })).toHaveCount(0);

    const opener = page.getByRole('button', { name: `Inspect reminder: ${today.content}`, exact: true });
    await opener.click();
    const detail = page.getByRole('dialog', { name: today.content });
    await expect(detail).toBeVisible();
    await expect(detail.getByText('Timezone', { exact: true })).toBeVisible();
    await expect(detail.getByRole('button', { name: 'Mark complete', exact: true })).toBeVisible();
    await expect(detail.getByRole('button', { name: 'Cancel reminder', exact: true })).toBeVisible();
    await page.keyboard.press('Escape');
    await expect(detail).toBeHidden();
    await expect(opener).toBeFocused();

    await opener.click();
    await page.getByRole('dialog', { name: today.content }).getByRole('button', { name: 'Mark complete', exact: true }).click();
    await expect.poll(() => completed).toBe(true);
    await expect(page.getByRole('dialog', { name: today.content }).getByRole('button', { name: 'Cancel reminder', exact: true })).toHaveCount(0);
    await page.getByRole('dialog', { name: today.content }).getByRole('button', { name: 'Close reminder details', exact: true }).click();

    await page.getByRole('button', { name: /History/ }).click();
    await expect(page.getByRole('heading', { name: 'History', exact: true })).toBeVisible();
    await expect(page.getByText(history.content, { exact: true })).toBeVisible();
    await expect(page.getByText(today.content, { exact: true })).toHaveCount(0);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);

    const results = await new AxeBuilder({ page }).include('.reminders-page').analyze();
    expect(results.violations).toEqual([]);
  });

  test('recovers a delayed direct-detail request and preserves a create draft after a recoverable failure', async ({ page }) => {
    const target = reminder('retry-reminder', 'Review the recovery path', isoAt(1, 15));
    let detailAttempts = 0;
    let createAttempts = 0;
    let releaseDetailFailure: (() => void) | undefined;
    const detailFailureReady = new Promise<void>((resolve) => { releaseDetailFailure = resolve; });
    await page.route('**/api/v1/**', async (route) => {
      const request = route.request();
      const url = new URL(request.url());
      if (url.pathname.endsWith('/auth/session')) return route.fulfill({ json: session });
      if (url.pathname.endsWith('/conversations')) return route.fulfill({ json: { items: [], nextCursor: null } });
      if (url.pathname.endsWith('/agents')) return route.fulfill({ json: { items: [] } });
      if (url.pathname.endsWith('/models')) return route.fulfill({ json: { models: [], defaultModelId: null, observedAt: isoAt(0, 8) } });
      if (url.pathname.endsWith('/reminders') && request.method() === 'GET') return route.fulfill({ json: { items: [], nextCursor: null } });
      if (url.pathname.endsWith('/reminders/retry-reminder') && request.method() === 'GET') {
        detailAttempts += 1;
        if (detailAttempts === 1) { await detailFailureReady; return route.fulfill({ status: 503, json: { detail: 'Detail service unavailable' } }); }
        return route.fulfill({ json: target });
      }
      if (url.pathname.endsWith('/reminders') && request.method() === 'POST') {
        createAttempts += 1;
        return route.fulfill({ status: 503, json: { detail: 'Create service unavailable' } });
      }
      return route.fulfill({ status: 404, json: { detail: 'fixture endpoint not implemented' } });
    });

    await page.goto('/reminders/retry-reminder');
    await expect(page.getByRole('status').filter({ hasText: 'Loading reminder…' })).toBeVisible();
    releaseDetailFailure?.();
    await expect(page.getByRole('alert').filter({ hasText: 'Reminder details are unavailable' })).toBeVisible();
    await page.getByRole('button', { name: 'Retry details', exact: true }).click();
    await expect(page.getByRole('dialog', { name: target.content })).toBeVisible();
    await page.getByRole('dialog', { name: target.content }).getByRole('button', { name: 'Close reminder details', exact: true }).click();
    await expect(page.getByRole('dialog')).toHaveCount(0);

    await page.getByRole('button', { name: 'New reminder' }).click();
    await page.getByLabel('Message').fill('Keep this draft while the service recovers');
    await page.getByRole('button', { name: 'Save reminder', exact: true }).click();
    await expect.poll(() => createAttempts).toBe(1);
    await expect(page.getByRole('alert').filter({ hasText: 'temporarily unavailable' })).toBeVisible();
    await expect(page.getByLabel('Message')).toHaveValue('Keep this draft while the service recovers');
  });

  test('does not let an older direct-detail response replace the current route', async ({ page }) => {
    const first = reminder('detail-a', 'First detail', isoAt(0, 14));
    const second = reminder('detail-b', 'Second detail', isoAt(0, 15));
    let resolveFirst: (() => void) | undefined;
    let resolveSecond: (() => void) | undefined;
    const firstReady = new Promise<void>((resolve) => { resolveFirst = resolve; });
    const secondReady = new Promise<void>((resolve) => { resolveSecond = resolve; });
    await page.route('**/api/v1/**', async (route) => {
      const request = route.request();
      const url = new URL(request.url());
      if (url.pathname.endsWith('/auth/session')) return route.fulfill({ json: session });
      if (url.pathname.endsWith('/conversations')) return route.fulfill({ json: { items: [], nextCursor: null } });
      if (url.pathname.endsWith('/agents')) return route.fulfill({ json: { items: [] } });
      if (url.pathname.endsWith('/models')) return route.fulfill({ json: { models: [], defaultModelId: null, observedAt: isoAt(0, 8) } });
      if (url.pathname.endsWith('/reminders') && request.method() === 'GET') return route.fulfill({ json: { items: [], nextCursor: null } });
      if (url.pathname.endsWith('/reminders/detail-a') && request.method() === 'GET') { await firstReady; return route.fulfill({ json: first }); }
      if (url.pathname.endsWith('/reminders/detail-b') && request.method() === 'GET') { await secondReady; return route.fulfill({ json: second }); }
      return route.fulfill({ status: 404, json: { detail: 'fixture endpoint not implemented' } });
    });

    await page.goto('/reminders/detail-a');
    await expect(page.getByRole('status').filter({ hasText: 'Loading reminder…' })).toBeVisible();
    await page.goto('/reminders/detail-b');
    await expect(page.getByRole('status').filter({ hasText: 'Loading reminder…' })).toBeVisible();
    resolveSecond?.();
    await expect(page.getByRole('dialog', { name: second.content })).toBeVisible();
    resolveFirst?.();
    await page.waitForTimeout(50);
    await expect(page.getByRole('dialog', { name: second.content })).toBeVisible();
    await expect(page.getByRole('dialog', { name: first.content })).toHaveCount(0);
  });

  test('clears an unavailable reminder collection and retries it in place', async ({ page }) => {
    const recovered = reminder('collection-retry', 'Recovered reminder', isoAt(1, 10));
    let upcomingAttempts = 0;
    await page.route('**/api/v1/**', async (route) => {
      const request = route.request();
      const url = new URL(request.url());
      if (url.pathname.endsWith('/auth/session')) return route.fulfill({ json: session });
      if (url.pathname.endsWith('/conversations')) return route.fulfill({ json: { items: [], nextCursor: null } });
      if (url.pathname.endsWith('/agents')) return route.fulfill({ json: { items: [] } });
      if (url.pathname.endsWith('/models')) return route.fulfill({ json: { models: [], defaultModelId: null, observedAt: isoAt(0, 8) } });
      if (url.pathname.endsWith('/reminders') && request.method() === 'GET') {
        if (url.searchParams.get('view') === 'history') return route.fulfill({ json: { items: [], nextCursor: null } });
        upcomingAttempts += 1;
        return upcomingAttempts === 1 ? route.fulfill({ status: 503, json: { detail: 'collection unavailable' } }) : route.fulfill({ json: { items: [recovered], nextCursor: null } });
      }
      return route.fulfill({ status: 404, json: { detail: 'fixture endpoint not implemented' } });
    });

    await page.goto('/reminders');
    await expect(page.getByRole('alert').filter({ hasText: 'temporarily unavailable' })).toBeVisible();
    await expect(page.getByText(recovered.content, { exact: true })).toHaveCount(0);
    await page.getByRole('alert').filter({ hasText: 'temporarily unavailable' }).getByRole('button', { name: 'Retry', exact: true }).click();
    await expect(page.getByText(recovered.content, { exact: true })).toBeVisible();
  });

  test('does not apply a delayed transition after the detail route changes', async ({ page }) => {
    const first = reminder('mutation-a', 'First transition', isoAt(0, 14));
    const second = reminder('mutation-b', 'Second transition', isoAt(0, 15));
    let releaseMutation: (() => void) | undefined;
    const mutationReady = new Promise<void>((resolve) => { releaseMutation = resolve; });
    await page.route('**/api/v1/**', async (route) => {
      const request = route.request();
      const url = new URL(request.url());
      if (url.pathname.endsWith('/auth/session')) return route.fulfill({ json: session });
      if (url.pathname.endsWith('/conversations')) return route.fulfill({ json: { items: [], nextCursor: null } });
      if (url.pathname.endsWith('/agents')) return route.fulfill({ json: { items: [] } });
      if (url.pathname.endsWith('/models')) return route.fulfill({ json: { models: [], defaultModelId: null, observedAt: isoAt(0, 8) } });
      if (url.pathname.endsWith('/reminders') && request.method() === 'GET') return route.fulfill({ json: { items: [first, second], nextCursor: null } });
      if (url.pathname.endsWith('/reminders/mutation-a') && request.method() === 'GET') return route.fulfill({ json: first });
      if (url.pathname.endsWith('/reminders/mutation-b') && request.method() === 'GET') return route.fulfill({ json: second });
      if (url.pathname.endsWith('/reminders/mutation-a/complete') && request.method() === 'POST') { await mutationReady; return route.fulfill({ json: { ...first, status: 'completed', version: 2, completedAt: isoAt(0, 16) } }); }
      return route.fulfill({ status: 404, json: { detail: 'fixture endpoint not implemented' } });
    });

    await page.goto('/reminders');
    await page.getByRole('button', { name: `Inspect reminder: ${first.content}`, exact: true }).click();
    const firstDetail = page.getByRole('dialog', { name: first.content });
    await expect(firstDetail).toBeVisible();
    await firstDetail.getByRole('button', { name: 'Mark complete', exact: true }).click();
    await expect(firstDetail.getByRole('button', { name: 'Mark complete', exact: true })).toBeDisabled();
    await firstDetail.getByRole('button', { name: 'Close reminder details', exact: true }).click();
    await page.getByRole('button', { name: `Inspect reminder: ${second.content}`, exact: true }).click();
    const secondDetail = page.getByRole('dialog', { name: second.content });
    await expect(secondDetail).toBeVisible();
    await expect(secondDetail.getByRole('button', { name: 'Mark complete', exact: true })).toBeEnabled();
    await expect(secondDetail.getByRole('button', { name: 'Cancel reminder', exact: true })).toBeEnabled();
    releaseMutation?.();
    await page.waitForTimeout(50);
    await expect(secondDetail).toBeVisible();
    await expect(secondDetail.getByText('Second transition', { exact: true })).toBeVisible();
    await expect(secondDetail.getByRole('button', { name: 'Mark complete', exact: true })).toBeEnabled();
    await expect(secondDetail.getByRole('button', { name: 'Cancel reminder', exact: true })).toBeEnabled();
  });

  test('keeps one idempotency key for an uncertain create and reconciles it after a list refresh', async ({ page }) => {
    const created = reminder('uncertain-create', 'Reconcile this reminder', isoAt(1, 15));
    let createRequests = 0;
    const createKeys: string[] = [];
    let reconciled = false;
    let reconciledDueAt = created.dueAt;
    await page.route('**/api/v1/**', async (route) => {
      const request = route.request();
      const url = new URL(request.url());
      if (url.pathname.endsWith('/auth/session')) return route.fulfill({ json: session });
      if (url.pathname.endsWith('/conversations')) return route.fulfill({ json: { items: [], nextCursor: null } });
      if (url.pathname.endsWith('/agents')) return route.fulfill({ json: { items: [] } });
      if (url.pathname.endsWith('/models')) return route.fulfill({ json: { models: [], defaultModelId: null, observedAt: isoAt(0, 8) } });
      if (url.pathname.endsWith('/reminders') && request.method() === 'GET') return route.fulfill({ json: reconciled ? { items: [{ ...created, dueAt: reconciledDueAt }], nextCursor: null } : { items: [], nextCursor: null } });
      if (url.pathname.endsWith('/reminders') && request.method() === 'POST') {
        createRequests += 1;
        createKeys.push(request.headers()['idempotency-key'] ?? '');
        const body = request.postDataJSON() as { content?: string; dueAt?: string };
        reconciledDueAt = body.dueAt ?? created.dueAt;
        if (createRequests === 1) return route.abort('failed');
        return route.fulfill({ json: { ...created, content: body.content ?? created.content, dueAt: reconciledDueAt } });
      }
      if (url.pathname.endsWith('/notifications/stream')) return route.fulfill({ status: 200, headers: { 'content-type': 'text/event-stream' }, body: 'event: heartbeat\ndata: {"schemaVersion":1,"eventType":"heartbeat","occurredAt":"2099-10-08T09:00:00Z"}\n\n' });
      if (url.pathname.endsWith('/notifications') && request.method() === 'GET') return route.fulfill({ json: { items: [], nextCursor: null, unacknowledgedCount: 0 } });
      return route.fulfill({ status: 404, json: { detail: 'fixture endpoint not implemented' } });
    });

    await page.goto('/reminders');
    await page.getByRole('button', { name: 'New reminder', exact: true }).click();
    await page.getByLabel('Message').fill(created.content);
    await page.getByRole('button', { name: 'Save reminder', exact: true }).click();
    await expect.poll(() => createRequests).toBe(1);
    await expect(page.getByRole('button', { name: 'Reconcile before retrying', exact: true })).toBeDisabled();
    const unknownOutcome = page.getByRole('alert').filter({ hasText: 'outcome is unknown' });
    await expect(unknownOutcome).toBeVisible();
    await expect(page.getByLabel('Message')).toHaveValue(created.content);
    reconciled = true;
    await unknownOutcome.getByRole('button', { name: 'Retry', exact: true }).click();
    await expect(page.getByRole('alert').filter({ hasText: 'safely retry with the same save request' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Retry save safely', exact: true })).toBeEnabled();
    await page.getByRole('button', { name: 'Retry save safely', exact: true }).click();
    await expect.poll(() => createRequests).toBe(2);
    await expect(page.getByText(created.content, { exact: true })).toBeVisible();
    expect(createKeys[0]).toBeTruthy();
    expect(createKeys[0]).toBe(createKeys[1]);
  });

  test('does not auto-confirm an identical pre-existing reminder and offers an explicit same-key retry', async ({ page }) => {
    const existing = reminder('pre-existing-identical', 'Already on the agenda', isoAt(1, 15));
    const created = reminder('created-after-retry', existing.content, existing.dueAt);
    let createAttempts = 0;
    const createKeys: string[] = [];
    const localDateTime = (value: string): string => {
      const date = new Date(value);
      const pad = (part: number): string => String(part).padStart(2, '0');
      return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
    };
    await page.route('**/api/v1/**', async (route) => {
      const request = route.request();
      const url = new URL(request.url());
      if (url.pathname.endsWith('/auth/session')) return route.fulfill({ json: session });
      if (url.pathname.endsWith('/conversations')) return route.fulfill({ json: { items: [], nextCursor: null } });
      if (url.pathname.endsWith('/agents')) return route.fulfill({ json: { items: [] } });
      if (url.pathname.endsWith('/models')) return route.fulfill({ json: { models: [], defaultModelId: null, observedAt: isoAt(0, 8) } });
      if (url.pathname.endsWith('/reminders') && request.method() === 'GET') return route.fulfill({ json: url.searchParams.get('view') === 'history' ? { items: [], nextCursor: null } : { items: [existing], nextCursor: null } });
      if (url.pathname.endsWith('/reminders') && request.method() === 'POST') {
        createAttempts += 1;
        createKeys.push(request.headers()['idempotency-key'] ?? '');
        if (createAttempts === 1) return route.abort('failed');
        const body = request.postDataJSON() as { content?: string; dueAt?: string };
        return route.fulfill({ json: { ...created, content: body.content ?? created.content, dueAt: body.dueAt ?? created.dueAt } });
      }
      return route.fulfill({ status: 404, json: { detail: 'fixture endpoint not implemented' } });
    });

    await page.goto('/reminders');
    await expect(page.getByText(existing.content, { exact: true })).toBeVisible();
    await page.getByRole('button', { name: 'New reminder', exact: true }).click();
    await page.getByLabel('Message').fill(existing.content);
    await page.getByLabel('When').fill(localDateTime(existing.dueAt));
    await page.getByRole('button', { name: 'Save reminder', exact: true }).click();
    await expect.poll(() => createAttempts).toBe(1);
    await expect(page.getByRole('alert').filter({ hasText: 'outcome is unknown' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Reconcile before retrying', exact: true })).toBeDisabled();

    const notice = page.getByRole('alert').filter({ hasText: 'outcome is unknown' });
    await notice.getByRole('button', { name: 'Retry', exact: true }).click();
    await expect(page.getByRole('alert').filter({ hasText: 'safely retry with the same save request' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Retry save safely', exact: true })).toBeEnabled();
    await expect(page.getByText(existing.content, { exact: true })).toBeVisible();

    await page.getByRole('button', { name: 'Retry save safely', exact: true }).click();
    await expect.poll(() => createAttempts).toBe(2);
    await expect(page.getByRole('button', { name: `Inspect reminder: ${created.content}`, exact: true })).toHaveCount(2);
    expect(createKeys[0]).toBeTruthy();
    expect(createKeys[0]).toBe(createKeys[1]);
  });
});
