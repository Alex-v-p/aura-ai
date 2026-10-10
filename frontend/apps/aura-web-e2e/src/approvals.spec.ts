import { expect, test } from '@playwright/test';

const now = '2026-10-10T09:00:00.000Z';
const summary = { id: 'approval-1', proposalId: 'proposal-1', runId: 'run-1', conversationId: 'conversation-1', toolId: 'reminder.create', toolVersion: 1, status: 'pending', executionStatus: 'not_started', version: 1, createdAt: now, updatedAt: now, expiresAt: '2026-10-10T10:00:00.000Z', decidedAt: null };

test.describe('approval inbox', () => {
  let emptyFixture = false;
  test.beforeEach(async ({ page }) => {
    emptyFixture = false;
    await page.route('**/api/v1/auth/session', (route) => route.fulfill({ json: { principal: { issuer: 'https://issuer', subject: 'owner' }, csrfToken: 'csrf', idleExpiresAt: now, absoluteExpiresAt: now } }));
    await page.route('**/api/v1/approvals**', async (route) => {
      const pathname = new URL(route.request().url()).pathname;
      if (pathname.endsWith('/stream')) return route.fulfill({ status: 200, contentType: 'text/event-stream', body: ': heartbeat\n\n' });
      if (pathname.endsWith('/approval-1/approve')) return route.fulfill({ json: { ...summary, status: 'approved', executionStatus: 'queued', version: 2, decidedAt: now } });
      if (pathname.endsWith('/approval-1')) return route.fulfill({ json: { ...summary, proposal: { id: 'proposal-1', content: 'Call the dentist', dueAt: '2026-10-11T09:00:00.000Z' } } });
      if (route.request().method() === 'GET') return route.fulfill({ json: { items: emptyFixture ? [] : [summary], nextCursor: null } });
      return route.continue();
    });
  });

  test('shows a durable pending request and owner-authorized proposal detail', async ({ page }) => {
    await page.goto('/approvals');
    await expect(page.getByRole('heading', { name: 'Approvals', exact: true })).toBeVisible();
    await expect(page.getByText('reminder.create v1')).toBeVisible();
    await page.getByRole('button', { name: /open approval/i }).click();
    await expect(page.getByRole('heading', { name: 'Allow Aura to create this reminder?' })).toBeVisible();
    await expect(page.getByText('Call the dentist')).toBeVisible();
    await page.getByRole('button', { name: 'Approve reminder' }).click();
    await expect(page.getByTestId('approval-status')).toHaveText('Approved');
  });

  test('has an empty state that remains usable on a narrow viewport', async ({ page }) => {
    emptyFixture = true;
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto('/approvals');
    await expect(page.getByRole('heading', { name: 'No approvals here' })).toBeVisible();
    await expect(page.getByRole('link', { name: 'Return to conversation' })).toBeVisible();
  });
});
