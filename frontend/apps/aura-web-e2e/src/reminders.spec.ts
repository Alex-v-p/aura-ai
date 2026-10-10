import { expect, test } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';

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
});
