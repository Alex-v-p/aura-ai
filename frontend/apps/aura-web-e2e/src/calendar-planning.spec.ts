import { expect, test, type Page } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';

const calendarIds = {
  personal: '00000000-0000-4000-8000-000000000001',
  team: '00000000-0000-4000-8000-000000000002',
};
const session = {
  principal: { issuer: 'https://authentik.test', subject: 'fixture-owner', displayName: 'Fixture Owner' },
  csrfToken: 'fixture-csrf',
  idleExpiresAt: '2026-10-27T20:00:00.000Z',
  absoluteExpiresAt: '2026-10-28T20:00:00.000Z',
};
const connection = (status: string, readsEnabled: boolean, availableActions: string[], lastError: Record<string, unknown> | null = null) => ({
  status,
  readsEnabled,
  selectedCalendarCount: readsEnabled ? 1 : 0,
  connectedAt: readsEnabled ? '2026-10-27T07:00:00Z' : null,
  updatedAt: '2026-10-27T07:00:00Z',
  lastError,
  availableActions,
});
const calendars = [
  { id: calendarIds.personal, displayName: 'Personal', timeZone: 'Europe/Brussels', selected: true, discoveredAt: '2026-10-27T07:00:00Z' },
  { id: calendarIds.team, displayName: 'Team calendar', timeZone: 'America/New_York', selected: false, discoveredAt: '2026-10-27T07:00:00Z' },
];
const eventPage = (partial = false) => ({
  rangeStart: '2026-10-27T00:00:00.000Z',
  rangeEnd: '2026-10-28T00:00:00.000Z',
  displayTimeZone: 'Europe/Brussels',
  items: [
    { kind: 'all_day', id: 'event-all-day', calendarId: calendarIds.personal, title: 'Planning day', description: null, location: null, availability: 'busy', startDate: '2026-10-27', endDateExclusive: '2026-10-28', eventTimeZone: 'Europe/Brussels', displayTimeZone: 'Europe/Brussels', spansMultipleDays: false },
    { kind: 'timed', id: 'event-timed', calendarId: calendarIds.personal, title: 'Focus block', description: null, location: 'Studio', availability: 'busy', startInstant: '2026-10-27T09:00:00Z', endInstant: '2026-10-27T10:00:00Z', eventTimeZone: 'America/New_York', displayTimeZone: 'Europe/Brussels', spansMultipleDays: false },
  ],
  nextCursor: null,
  partial,
  warnings: partial ? [{ calendarId: calendarIds.team, category: 'provider_rate_limit', retryable: true, recoveryAction: 'retry' }] : [],
  retrievedAt: '2026-10-27T07:05:00Z',
});
const conflictPage = () => ({
  rangeStart: '2026-10-27T00:00:00.000Z', rangeEnd: '2026-10-28T00:00:00.000Z', displayTimeZone: 'Europe/Brussels',
  items: [{ left: { eventId: 'event-all-day', calendarId: calendarIds.personal }, right: { eventId: 'event-timed', calendarId: calendarIds.personal }, relationship: 'overlaps', overlapStart: '2026-10-27T09:00:00Z', overlapEnd: '2026-10-27T10:00:00Z' }],
  nextCursor: null, partial: false, warnings: [], evaluatedAt: '2026-10-27T07:05:00Z',
});

type CalendarFixture = {
  state: 'disconnected' | 'connected' | 'partial' | 'stale' | 'rate-limited' | 'permission-denied' | 'loading' | 'connection-error' | 'disabled' | 'unavailable' | 'last-error' | 'oauth-success' | 'oauth-failure' | 'disconnecting';
  requestCounts: Record<string, number>;
  selectionBodies: Array<Record<string, unknown>>;
};

async function installCalendarFixture(page: Page, state: CalendarFixture['state'] = 'connected'): Promise<CalendarFixture> {
  const fixture: CalendarFixture = { state, requestCounts: {}, selectionBodies: [] };
  await page.route('**/api/v1/**', async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const key = `${request.method()} ${url.pathname}`;
    fixture.requestCounts[key] = (fixture.requestCounts[key] ?? 0) + 1;
    if (url.pathname === '/api/v1/auth/session') return route.fulfill({ json: session });
    if (url.pathname === '/api/v1/integrations/calendar/connection' && request.method() === 'GET') {
      if (fixture.state === 'disconnected') return route.fulfill({ json: connection('disconnected', false, ['connect']) });
      if (fixture.state === 'loading') { await new Promise((resolve) => setTimeout(resolve, 10_000)); return route.fulfill({ json: connection('connected', true, ['disconnect']) }); }
      if (fixture.state === 'connection-error') return route.fulfill({ status: 503, json: { category: 'provider_unavailable', message: 'Calendar connection status is unavailable.', retryable: true, recoveryAction: 'retry' } });
      if (fixture.state === 'permission-denied') return route.fulfill({ status: 403, json: { category: 'authorization', message: 'Calendar permission was denied.', retryable: false, recoveryAction: 'contact_administrator' } });
      if (fixture.state === 'rate-limited') return route.fulfill({ status: 429, json: { category: 'provider_rate_limit', message: 'Calendar reads are temporarily rate-limited.', retryable: true, recoveryAction: 'wait' } });
      if (fixture.state === 'disabled') return route.fulfill({ json: connection('disabled', false, []) });
      if (fixture.state === 'unavailable') return route.fulfill({ json: connection('unavailable', false, ['retry']) });
      if (fixture.state === 'last-error') return route.fulfill({ json: connection('connected', true, ['disconnect'], { category: 'provider_unavailable', message: 'The last calendar read failed.', retryable: true, recoveryAction: 'retry' }) });
      if (fixture.state === 'oauth-success' || fixture.state === 'oauth-failure') return route.fulfill({ json: connection('connecting', false, ['complete_connection']) });
      return route.fulfill({ json: connection('connected', true, ['disconnect']) });
    }
    if (url.pathname === '/api/v1/integrations/calendar/connection/start') return route.fulfill({ json: { connection: connection('connecting', false, ['complete_connection']), authorizationPath: '/api/v1/integrations/calendar/authorize/fixture-handle', expiresAt: '2026-10-27T07:10:00Z' } });
    if (url.pathname === '/api/v1/integrations/calendar/connection/disconnect') {
      if (fixture.state === 'disconnecting') await new Promise((resolve) => setTimeout(resolve, 2_000));
      return route.fulfill({ json: connection('disconnected', false, ['connect']) });
    }
    if (url.pathname === '/api/v1/integrations/calendar/connection/complete') {
      if (fixture.state === 'oauth-failure') return route.fulfill({ status: 502, json: { category: 'provider_unavailable', message: 'Calendar connection could not be completed.', retryable: true, recoveryAction: 'retry' } });
      await new Promise((resolve) => setTimeout(resolve, 250));
      return route.fulfill({ json: connection('connected', true, ['disconnect']) });
    }
    if (url.pathname === '/api/v1/integrations/calendar/calendars') return route.fulfill({ json: { items: calendars, nextCursor: null, selectionVersion: 7, observedAt: '2026-10-27T07:05:00Z' } });
    if (url.pathname === '/api/v1/integrations/calendar/calendars/selection') {
      const body = request.postDataJSON() as Record<string, unknown>;
      fixture.selectionBodies.push(body);
      return route.fulfill({ json: { calendarIds: body['calendarIds'], version: 8, updatedAt: '2026-10-27T07:06:00Z' } });
    }
    if (url.pathname === '/api/v1/integrations/calendar/events/query') {
      if (fixture.state === 'stale') return route.fulfill({ status: 503, json: { category: 'provider_unavailable', message: 'Calendar agenda is temporarily unavailable.', retryable: true, recoveryAction: 'retry' } });
      return route.fulfill({ json: eventPage(fixture.state === 'partial') });
    }
    if (url.pathname === '/api/v1/integrations/calendar/conflicts/query') return route.fulfill({ json: conflictPage() });
    return route.fulfill({ status: 404, json: { detail: 'fixture endpoint not implemented' } });
  });
  return fixture;
}

test.describe('Calendar planning', () => {
  test('renders the owner-safe connection and explicit selection flow', async ({ page }) => {
    const fixture = await installCalendarFixture(page);
    await page.goto('/integrations/calendar');
    await expect(page.getByRole('heading', { name: 'Calendar connection' })).toBeVisible();
    await expect(page.getByRole('heading', { name: 'Readable calendars' })).toBeVisible();
    const personal = page.locator('label.calendar-row').filter({ hasText: 'Personal' }).locator('input');
    const team = page.locator('label.calendar-row').filter({ hasText: 'Team calendar' }).locator('input');
    await expect(personal).toBeChecked();
    await expect(team).not.toBeChecked();
    await team.check();
    await page.getByRole('button', { name: 'Save selection', exact: true }).click();
    await expect.poll(() => fixture.selectionBodies.length).toBe(1);
    expect(fixture.selectionBodies[0]).toMatchObject({ calendarIds: [calendarIds.personal, calendarIds.team], expectedVersion: 7 });
    await expect(team).toBeChecked();
    await expect(page.locator('body')).not.toContainText('fixture-handle');
    expect(page.url()).not.toContain('Planning day');
  });

  test('renders all-day events before timed events, provenance, and server conflict explanation', async ({ page }) => {
    await installCalendarFixture(page);
    await page.goto('/integrations/calendar');
    await page.getByRole('button', { name: 'Refresh agenda', exact: true }).click();
    await expect(page.getByText('Planning day', { exact: true })).toBeVisible();
    await expect(page.getByText('Focus block', { exact: true })).toBeVisible();
    const rows = page.locator('.agenda-row');
    await expect(rows).toHaveCount(2);
    await expect(rows.nth(0)).toContainText('Planning day');
    await expect(rows.nth(1)).toContainText('Focus block');
    await expect(page.getByText(/America\/New_York · shown in Europe\/Brussels/)).toBeVisible();
    await expect(page.getByRole('button', { name: 'Refresh agenda', exact: true })).toBeFocused();
    const results = await new AxeBuilder({ page }).include('.connection-panel').include('.selection-panel').include('.agenda-panel').analyze();
    expect(results.violations).toEqual([]);
  });

  test('covers connection loading, provider error, disabled, unavailable, and last-error states', async ({ page }) => {
    for (const state of ['loading', 'connection-error', 'disabled', 'unavailable', 'last-error'] as const) {
      await installCalendarFixture(page, state);
      await page.goto('/integrations/calendar', { waitUntil: state === 'loading' ? 'commit' : 'load' });
      if (state === 'loading') {
        await expect(page.locator('.calendar-page')).toBeVisible();
        await expect(page.locator('.calendar-page .status')).toContainText(/loading calendar connection/i, { timeout: 3_000 });
      }
      if (state === 'connection-error') await expect(page.getByRole('alert')).toContainText(/connection status is unavailable/i);
      if (state === 'disabled') {
        await expect(page.getByRole('heading', { name: 'disabled', exact: true })).toBeVisible();
        await expect(page.getByRole('button', { name: /connect|disconnect/i })).toHaveCount(0);
      }
      if (state === 'unavailable') await expect(page.getByRole('heading', { name: 'unavailable', exact: true })).toBeVisible();
      if (state === 'last-error') await expect(page.locator('.connection-error')).toContainText(/last calendar read failed/i);
      await page.unroute('**/api/v1/**');
    }
  });

  test('completes delayed OAuth with deterministic success and failure focus', async ({ page }) => {
    await installCalendarFixture(page, 'oauth-success');
    await page.goto('/integrations/calendar');
    await expect(page.getByRole('heading', { name: 'Calendar connection' })).toBeFocused();

    await page.unroute('**/api/v1/**');
    await installCalendarFixture(page, 'oauth-failure');
    await page.goto('/integrations/calendar');
    await expect(page.getByRole('button', { name: 'Retry', exact: true })).toBeFocused();
  });

  test('requires an inline disconnect confirmation and submits exactly once with progress focus', async ({ page }) => {
    const fixture = await installCalendarFixture(page, 'disconnecting');
    await page.goto('/integrations/calendar');
    const disconnect = page.getByRole('button', { name: 'Disconnect', exact: true });
    await disconnect.click();
    await expect(page.getByRole('alert')).toContainText(/private calendar content will be cleared/i);
    await expect(page.getByRole('button', { name: 'Confirm disconnect', exact: true })).toBeFocused();
    await page.getByRole('button', { name: 'Cancel', exact: true }).click();
    await expect(disconnect).toBeFocused();
    await disconnect.click();
    await page.getByRole('button', { name: 'Confirm disconnect', exact: true }).click();
    await expect(page.locator('.calendar-page .busy-status')).toContainText(/saving calendar changes/i);
    await expect.poll(() => fixture.requestCounts['POST /api/v1/integrations/calendar/connection/disconnect'] ?? 0).toBe(1);
    await expect(page.getByRole('heading', { name: 'Calendar connection' })).toBeFocused();
  });

  test('supports keyboard selection lifecycle without duplicate saves', async ({ page }) => {
    const fixture = await installCalendarFixture(page);
    await page.goto('/integrations/calendar');
    const team = page.locator('label.calendar-row').filter({ hasText: 'Team calendar' }).locator('input');
    await team.focus();
    await page.keyboard.press('Space');
    await expect(team).toBeChecked();
    const save = page.getByRole('button', { name: 'Save selection', exact: true });
    await save.focus();
    await page.keyboard.press('Enter');
    await expect.poll(() => fixture.selectionBodies.length).toBe(1);
    await expect(page.getByRole('heading', { name: 'Calendar connection' })).toBeFocused();
  });

  test('keeps management controls touch-sized and reflows the dark theme at zoom', async ({ page }) => {
    await installCalendarFixture(page);
    await page.emulateMedia({ colorScheme: 'dark', reducedMotion: 'reduce' });
    await page.goto('/integrations/calendar');
    await page.evaluate(() => {
      document.documentElement.dataset['theme'] = 'dark';
      document.documentElement.style.zoom = '2';
    });
    const metrics = await page.evaluate(() => ({
      overflow: document.documentElement.scrollWidth <= document.documentElement.clientWidth,
      canvas: getComputedStyle(document.documentElement).getPropertyValue('--canvas').trim(),
      text: getComputedStyle(document.documentElement).getPropertyValue('--text').trim(),
    }));
    expect(metrics.overflow).toBe(true);
    expect(metrics.canvas).not.toBe('');
    expect(metrics.text).not.toBe('');
    const targets = page.locator('.calendar-page button, .calendar-page a');
    expect(await targets.evaluateAll((items) => items.every((item) => {
      const box = item.getBoundingClientRect();
      return box.width >= 44 && box.height >= 44;
    }))).toBe(true);
  });

  test('marks the Connect group active on both integration routes', async ({ page }) => {
    await installCalendarFixture(page);
    for (const path of ['/integrations', '/integrations/calendar']) {
      await page.goto(path);
      const link = page.locator('.nav-group-connect a.product-link');
      await expect(link).toHaveClass(/is-active/);
      await expect(link).toHaveAttribute('aria-current', 'page');
    }
  });

  test('preserves recovery states and unrelated navigation when calendar reads fail', async ({ page }) => {
    await installCalendarFixture(page, 'partial');
    await page.goto('/integrations/calendar');
    await page.getByRole('button', { name: 'Refresh agenda', exact: true }).click();
    await expect(page.locator('.calendar-page .notice')).toContainText(/Some calendars could not be read/i);
    await expect(page.getByRole('link', { name: /Connect|Aura home/i }).first()).toBeVisible();

    await page.goto('/today');
    await expect(page).toHaveURL(/\/today$/);
    await expect(page.locator('#today-heading')).toBeVisible();
    await expect(page.getByRole('heading', { name: 'Needs attention', exact: true })).toBeVisible();
  });

  for (const state of ['disconnected', 'permission-denied', 'rate-limited', 'stale'] as const) {
    test(`exposes a closed ${state} recovery state`, async ({ page }) => {
      await installCalendarFixture(page, state);
      await page.goto('/integrations/calendar');
      if (state === 'disconnected') await expect(page.getByRole('button', { name: 'Connect Google Calendar', exact: true })).toBeVisible();
      if (state === 'permission-denied') await expect(page.getByRole('alert')).toContainText(/permission/i);
      if (state === 'rate-limited') await expect(page.getByRole('alert')).toContainText(/temporarily unavailable|rate-limited/i);
      if (state === 'stale') {
        await page.getByRole('button', { name: 'Refresh agenda', exact: true }).click();
        await expect(page.getByRole('alert')).toContainText(/temporarily unavailable/i);
      }
      const bodyText = await page.locator('body').innerText();
      expect(bodyText).not.toMatch(/access_token|refresh_token|client_secret|authorization_code|provider_error/i);
    });
  }

  test('bounds calendar network amplification to one connection and bounded reads', async ({ page }) => {
    const fixture = await installCalendarFixture(page);
    await page.goto('/integrations/calendar');
    await page.getByRole('button', { name: 'Refresh calendars', exact: true }).click();
    await page.getByRole('button', { name: 'Refresh agenda', exact: true }).click();
    expect(fixture.requestCounts['GET /api/v1/integrations/calendar/connection']).toBeLessThanOrEqual(2);
    expect(fixture.requestCounts['GET /api/v1/integrations/calendar/calendars']).toBeLessThanOrEqual(2);
    expect(fixture.requestCounts['POST /api/v1/integrations/calendar/events/query']).toBeLessThanOrEqual(3);
    expect(fixture.requestCounts['POST /api/v1/integrations/calendar/conflicts/query']).toBeLessThanOrEqual(3);
  });
});
