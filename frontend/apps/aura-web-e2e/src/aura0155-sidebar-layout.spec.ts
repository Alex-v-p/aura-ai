import { expect, test } from '@playwright/test';

const now = (): string => new Date().toISOString();

test.beforeEach(async ({ page }) => {
  const summary = (id = 'welcome'): Record<string, unknown> => ({
    id,
    title: 'A thoughtful beginning',
    agentProfileId: 'general',
    agentRevisionId: 'agent-rev-1',
    modelId: 'qwen2.5:7b',
    version: 1,
    createdAt: now(),
    updatedAt: now(),
    currentRun: null,
  });

  await page.route('**/api/**', async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.pathname === '/api/v1/auth/session') {
      return route.fulfill({
        json: {
          principal: { issuer: 'https://authentik.test', subject: 'owner', displayName: 'Owner' },
          csrfToken: 'csrf',
          idleExpiresAt: now(),
          absoluteExpiresAt: now(),
        },
      });
    }
    if (url.pathname === '/api/v1/models') {
      return route.fulfill({
        json: {
          models: [
            {
              id: 'qwen2.5:7b',
              displayName: 'Qwen 2.5',
              provider: 'ollama',
              capabilities: ['chat'],
              availability: 'available',
              selectable: true,
              disabledReason: null,
            },
          ],
          defaultModelId: 'qwen2.5:7b',
          observedAt: now(),
        },
      });
    }
    if (url.pathname === '/api/v1/agents' && request.method() === 'GET') {
      return route.fulfill({
        json: {
          items: [
            {
              id: 'general',
              status: 'active',
              version: 1,
              currentRevision: {
                id: 'agent-rev-1',
                profileId: 'general',
                revision: 1,
                displayName: 'Aura',
              },
              revisions: [],
            },
          ],
        },
      });
    }
    if (url.pathname === '/api/v1/agents/general' && request.method() === 'GET') {
      return route.fulfill({
        json: {
          ...summary('general'),
          displayName: 'Aura',
          currentRevision: {
            id: 'agent-rev-1',
            profileId: 'general',
            revision: 1,
            displayName: 'Aura',
          },
          revisions: [],
        },
      });
    }
    if (url.pathname === '/api/v1/personas' && request.method() === 'GET') {
      return route.fulfill({ json: { items: [] } });
    }
    if (url.pathname === '/api/v1/conversations' && request.method() === 'GET') {
      return route.fulfill({
        json: {
          items: Array.from({ length: 48 }, (_, index) => ({
            ...summary(index === 0 ? 'welcome' : `recent-${index}`),
            title: index === 0 ? 'A thoughtful beginning' : `Recent conversation ${index}`,
          })),
          nextCursor: null,
        },
      });
    }
    if (url.pathname === '/api/v1/conversations/welcome' && request.method() === 'GET') {
      return route.fulfill({ json: { ...summary(), messages: [], recentRuns: [] } });
    }
    return route.fulfill({ json: { items: [], nextCursor: null } });
  });
});

test('keeps expanded product navigation separate from conversation library', async ({
  page,
}, testInfo) => {
  test.skip(
    testInfo.project.name !== 'desktop',
    'Expanded navigation geometry runs once in the desktop project.',
  );
  await page.setViewportSize({ width: 1280, height: 720 });
  await page.goto('/conversation/welcome?fixture=long-sidebar');
  const navigation = page.locator('#primary-navigation');
  const preferences = navigation.getByRole('button', { name: 'Preferences' });
  await expect(navigation.locator('.conversation-nav')).toBeVisible();
  await expect(preferences).toBeVisible();

  const metrics = await page.evaluate(() => {
    const sidebar = document.querySelector<HTMLElement>('#primary-navigation');
    const scrollRegion = document.querySelector<HTMLElement>('.sidebar-scroll-region');
    const productNav = document.querySelector<HTMLElement>('.product-nav');
    const recent = document.querySelector<HTMLElement>('.conversation-nav');
    const preferences = document.querySelector<HTMLElement>('.sidebar-bottom');
    const calendar = document.querySelector<HTMLElement>('.nav-group-connect .product-link');
    const activity = document.querySelector<HTMLElement>(
      '.nav-group[aria-labelledby="nav-group-activity"]',
    );
    const scrollOwners = sidebar
      ? [sidebar, ...Array.from(sidebar.querySelectorAll<HTMLElement>('*'))]
          .filter(
            (element) =>
              ['auto', 'scroll'].includes(getComputedStyle(element).overflowY) &&
              element.scrollHeight > element.clientHeight,
          )
          .map((element) => element.className || element.tagName.toLowerCase())
      : [];
    const sidebarBounds = sidebar?.getBoundingClientRect();
    const scrollRegionBounds = scrollRegion?.getBoundingClientRect();
    const productNavBounds = productNav?.getBoundingClientRect();
    const recentBounds = recent?.getBoundingClientRect();
    const preferencesBounds = preferences?.getBoundingClientRect();
    const calendarBounds = calendar?.getBoundingClientRect();
    const activityBounds = activity?.getBoundingClientRect();
    return {
      scrollOwners,
      recentOverflow: recent ? getComputedStyle(recent).overflowY : 'visible',
      recentScrollable: (recent?.scrollHeight ?? 0) > (recent?.clientHeight ?? 0),
      sidebarBottom: sidebarBounds?.bottom ?? 0,
      scrollRegionBottom: scrollRegionBounds?.bottom ?? 0,
      productNavBottom: productNavBounds?.bottom ?? 0,
      recentTop: recentBounds?.top ?? 0,
      calendarBottom: calendarBounds?.bottom ?? 0,
      activityBottom: activityBounds?.bottom ?? 0,
      preferencesTop: preferencesBounds?.top ?? 0,
      viewportHeight: window.innerHeight,
    };
  });

  expect(metrics.scrollOwners).toEqual(['sidebar-scroll-region']);
  expect(metrics.recentOverflow).toBe('visible');
  expect(metrics.recentScrollable).toBe(true);
  expect(metrics.sidebarBottom).toBeLessThanOrEqual(metrics.viewportHeight + 1);
  expect(metrics.recentTop).toBeGreaterThanOrEqual(metrics.productNavBottom - 1);
  expect(metrics.calendarBottom).toBeLessThanOrEqual(metrics.productNavBottom + 1);
  expect(metrics.activityBottom).toBeLessThanOrEqual(metrics.productNavBottom + 1);
  expect(metrics.scrollRegionBottom).toBeLessThanOrEqual(metrics.preferencesTop + 1);
});
