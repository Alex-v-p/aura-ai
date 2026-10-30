import AxeBuilder from '@axe-core/playwright';
import { expect, test, type Page, type Route } from '@playwright/test';

const NOW = '2026-10-30T10:00:00.000Z';

async function installLandmarkFixture(page: Page): Promise<void> {
  await page.route('**/api/v1/**', async (route: Route) => {
    const request = route.request();
    const url = new URL(request.url());
    const json = (body: unknown): Promise<void> =>
      route.fulfill({
        status: 200,
        headers: { 'cache-control': 'no-store' },
        json: body,
      });

    if (url.pathname === '/api/v1/auth/session') {
      return json({
        principal: { issuer: 'synthetic', subject: 'fixture-owner', displayName: 'Fixture Owner' },
        csrfToken: 'fixture-csrf',
        idleExpiresAt: '2099-10-30T10:00:00Z',
        absoluteExpiresAt: '2099-10-31T10:00:00Z',
      });
    }
    if (url.pathname === '/api/v1/models') {
      return json({ models: [], defaultModelId: null, observedAt: NOW });
    }
    if (url.pathname === '/api/v1/research/settings') {
      const budget = {
        wallTimeSeconds: 90,
        fetchedSources: 6,
        searchRequests: 2,
        browserPages: 1,
        modelCalls: 2,
      };
      return json({
        version: 1,
        presets: [
          { mode: 'quick', maximum: budget, defaults: budget },
          { mode: 'auto', maximum: budget, defaults: budget },
          { mode: 'deep', maximum: budget, defaults: budget },
          { mode: 'agent_led', maximum: budget, defaults: budget },
        ],
        updatedAt: NOW,
      });
    }
    if (url.pathname === '/api/v1/research' && request.method() === 'GET') {
      return json({ items: [], nextCursor: null });
    }
    if (url.pathname === '/api/v1/tasks' && request.method() === 'GET') {
      return json({ items: [], nextCursor: null });
    }
    if (url.pathname === '/api/v1/artifacts' && request.method() === 'GET') {
      return json({ items: [], nextCursor: null });
    }

    return json({ items: [], nextCursor: null });
  });
}

test.describe('AURA-0151 route landmarks', () => {
  test('keeps one shell-owned main landmark and passes the complete-page axe scan', async ({ page }) => {
    await installLandmarkFixture(page);

    for (const [path, heading] of [
      ['/research', 'Research'],
      ['/tasks', 'Task Center'],
      ['/artifacts', 'Artifacts'],
    ] as const) {
      await page.goto(path);
      await expect(page.getByRole('heading', { name: heading, exact: true })).toBeVisible();
      await expect(page.locator('main')).toHaveCount(1);

      const result = await new AxeBuilder({ page }).analyze();
      expect(result.violations, `${path} has accessibility violations`).toEqual([]);
    }
  });
});
