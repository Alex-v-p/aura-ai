import { expect, test, type Page } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';

const now = new Date();
const today = new Date(now);
today.setHours(12, 0, 0, 0);
const tomorrow = new Date(today);
tomorrow.setDate(tomorrow.getDate() + 1);

const session = {
  principal: {
    issuer: 'https://authentik.test',
    subject: 'fixture-owner',
    displayName: 'Fixture Owner',
  },
  csrfToken: 'fixture-csrf',
  idleExpiresAt: new Date(Date.now() + 3_600_000).toISOString(),
  absoluteExpiresAt: new Date(Date.now() + 7_200_000).toISOString(),
};

const reminder = (
  id: string,
  content: string,
  dueAt: string,
  status: 'scheduled' | 'fired' = 'scheduled',
) => ({
  id,
  content,
  dueAt,
  status,
  version: 1,
  createdAt: new Date(Date.now() - 86_400_000).toISOString(),
  updatedAt: new Date(Date.now() - 86_400_000).toISOString(),
  firedAt: status === 'fired' ? dueAt : null,
  completedAt: null,
  canceledAt: null,
});

const conversation = (id: string, title: string, updatedAt: string) => ({
  id,
  title,
  agentProfileId: 'general',
  agentRevisionId: 'agent-rev-1',
  modelId: 'fixture-chat',
  version: 1,
  createdAt: updatedAt,
  updatedAt,
  archivedAt: null,
  currentRun: null,
});

type TodayFixture = {
  sessionStatus?: number;
  approvalsStatus?: number;
  approvalsStatusAfterLoad?: number;
  approvalStreamRefreshAfterLoad?: boolean;
  notificationsStatus?: number;
  remindersStatus?: number;
  conversationsStatus?: number;
  calendarState?: 'disconnected' | 'ready' | 'partial' | 'stale' | 'rate-limited' | 'permission-denied' | 'offline';
  calendarComplex?: boolean;
  calendarLongTitle?: boolean;
  approvals: ReadonlyArray<Record<string, unknown>>;
  notifications: ReadonlyArray<Record<string, unknown>>;
  notificationsAfterHistory?: ReadonlyArray<Record<string, unknown>>;
  reminders: ReadonlyArray<Record<string, unknown>>;
  conversations: ReadonlyArray<Record<string, unknown>>;
  requestCounts: Record<string, number>;
};

async function installTodayFixture(
  page: Page,
  overrides: Partial<TodayFixture> = {},
): Promise<TodayFixture> {
  const fixture: TodayFixture = {
    approvals: [
      {
        id: 'approval-1',
        proposalId: 'proposal-private',
        runId: 'run-1',
        conversationId: 'conversation-1',
        toolCallId: 'call-1',
        toolId: 'reminder.create',
        toolVersion: 1,
        status: 'pending',
        executionStatus: 'not_started',
        version: 1,
        createdAt: today.toISOString(),
        updatedAt: today.toISOString(),
        expiresAt: new Date(today.getTime() + 3_600_000).toISOString(),
        decidedAt: null,
      },
    ],
    notifications: [
      {
        id: 'notification-1',
        reminderId: 'reminder-fired',
        status: 'delivered',
        version: 1,
        createdAt: today.toISOString(),
        updatedAt: today.toISOString(),
        deliveredAt: today.toISOString(),
        acknowledgedAt: null,
      },
    ],
    notificationsAfterHistory: undefined,
    reminders: [
      reminder('reminder-fired', 'Call the dentist', today.toISOString(), 'fired'),
      reminder('reminder-next', 'Pack for tomorrow', tomorrow.toISOString()),
    ],
    conversations: [
      conversation(
        'conversation-1',
        'Plan the weekend',
        new Date(Date.now() - 10 * 60_000).toISOString(),
      ),
      {
        ...conversation(
          'conversation-2',
          'Archived private title',
          new Date(Date.now() - 20 * 60_000).toISOString(),
        ),
        archivedAt: new Date(Date.now() - 10 * 60_000).toISOString(),
      },
    ],
    calendarState: 'disconnected',
    requestCounts: {},
    ...overrides,
  };

  let historyVisited = false;
  await page.route('**/api/v1/**', async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const key = `${request.method()} ${url.pathname}`;
    fixture.requestCounts[key] = (fixture.requestCounts[key] ?? 0) + 1;

    if (url.pathname === '/api/v1/auth/session') {
      if (fixture.sessionStatus)
        return route.fulfill({
          status: fixture.sessionStatus,
          json: { detail: 'authentication required' },
        });
      return route.fulfill({ json: session });
    }
    if (url.pathname === '/api/v1/models') {
      return route.fulfill({
        json: { models: [], defaultModelId: null, observedAt: today.toISOString() },
      });
    }
    if (url.pathname === '/api/v1/agents' || url.pathname === '/api/v1/personas') {
      return route.fulfill({ json: { items: [] } });
    }
    if (url.pathname === '/api/v1/conversations') {
      if (fixture.conversationsStatus)
        return route.fulfill({
          status: fixture.conversationsStatus,
          json: { detail: 'conversation source unavailable' },
        });
      return route.fulfill({ json: { items: fixture.conversations, nextCursor: null } });
    }
    if (url.pathname === '/api/v1/reminders') {
      if (fixture.remindersStatus)
        return route.fulfill({
          status: fixture.remindersStatus,
          json: { detail: 'reminder source unavailable' },
        });
      return route.fulfill({
        json: {
          items: url.searchParams.get('view') === 'history' ? [] : fixture.reminders,
          nextCursor: null,
        },
      });
    }
    if (url.pathname === '/api/v1/reminder-series') {
      return route.fulfill({ json: { items: [], nextCursor: null } });
    }
    if (url.pathname === '/api/v1/integrations/calendar/connection' && request.method() === 'GET') {
      if (fixture.calendarState === 'permission-denied') return route.fulfill({ status: 403, json: { category: 'authorization', message: 'Calendar permission was denied.', retryable: false, recoveryAction: 'contact_administrator' } });
      if (fixture.calendarState === 'rate-limited') return route.fulfill({ status: 429, json: { category: 'provider_rate_limit', message: 'Calendar reads are temporarily rate-limited.', retryable: true, recoveryAction: 'wait' } });
      if (fixture.calendarState === 'offline') return route.abort();
      return route.fulfill({ json: { status: fixture.calendarState === 'ready' || fixture.calendarState === 'partial' || fixture.calendarState === 'stale' ? 'connected' : 'disconnected', readsEnabled: fixture.calendarState !== 'disconnected', selectedCalendarCount: fixture.calendarState === 'disconnected' ? 0 : 1, connectedAt: fixture.calendarState === 'disconnected' ? null : today.toISOString(), updatedAt: today.toISOString(), lastError: null, availableActions: fixture.calendarState === 'disconnected' ? ['connect'] : ['disconnect'] } });
    }
    if (url.pathname === '/api/v1/integrations/calendar/calendars') {
      return route.fulfill({ json: { items: [{ id: '00000000-0000-4000-8000-000000000001', displayName: 'Personal', timeZone: 'Europe/Brussels', selected: true, discoveredAt: today.toISOString() }], nextCursor: null, selectionVersion: 1, observedAt: today.toISOString() } });
    }
    if (url.pathname === '/api/v1/integrations/calendar/events/query') {
      if (fixture.calendarState === 'stale') return route.fulfill({ status: 503, json: { category: 'provider_unavailable', message: 'Calendar agenda is temporarily unavailable.', retryable: true, recoveryAction: 'retry' } });
      const baseEvents = [
        { kind: 'all_day', id: 'today-calendar-all-day', calendarId: '00000000-0000-4000-8000-000000000001', title: 'Planning day', description: null, location: null, availability: 'busy', startDate: today.toISOString().slice(0, 10), endDateExclusive: tomorrow.toISOString().slice(0, 10), eventTimeZone: 'Europe/Brussels', displayTimeZone: 'Europe/Brussels', spansMultipleDays: false },
        { kind: 'timed', id: 'today-calendar-timed', calendarId: '00000000-0000-4000-8000-000000000001', title: 'Focus block', description: null, location: null, availability: 'busy', startInstant: today.toISOString(), endInstant: new Date(today.getTime() + 3_600_000).toISOString(), eventTimeZone: 'Europe/Brussels', displayTimeZone: 'Europe/Brussels', spansMultipleDays: false },
      ];
      const complexEvents = fixture.calendarComplex || fixture.calendarLongTitle ? [
        { kind: 'all_day', id: 'shared-event', calendarId: '00000000-0000-4000-8000-000000000002', title: fixture.calendarLongTitle ? 'A deliberately long multi-day planning title that remains readable on narrow screens and at two hundred percent zoom' : 'Team offsite', description: null, location: 'Brussels', availability: 'busy', startDate: today.toISOString().slice(0, 10), endDateExclusive: new Date(today.getTime() + 3 * 86_400_000).toISOString().slice(0, 10), eventTimeZone: 'Europe/Brussels', displayTimeZone: 'Europe/Brussels', spansMultipleDays: true },
        { kind: 'timed', id: 'shared-event', calendarId: '00000000-0000-4000-8000-000000000001', title: 'Focus block', description: null, location: null, availability: 'busy', startInstant: today.toISOString(), endInstant: new Date(today.getTime() + 3_600_000).toISOString(), eventTimeZone: 'Europe/Brussels', displayTimeZone: 'Europe/Brussels', spansMultipleDays: false },
      ] : [];
      return route.fulfill({ json: { rangeStart: today.toISOString(), rangeEnd: tomorrow.toISOString(), displayTimeZone: 'Europe/Brussels', items: [...baseEvents, ...complexEvents], nextCursor: null, partial: fixture.calendarState === 'partial', warnings: fixture.calendarState === 'partial' ? [{ calendarId: '00000000-0000-4000-8000-000000000001', category: 'provider_unavailable', retryable: true, recoveryAction: 'retry' }] : [], retrievedAt: today.toISOString() } });
    }
    if (url.pathname === '/api/v1/integrations/calendar/conflicts/query') {
      const conflicts = fixture.calendarComplex ? [
        { left: { eventId: 'shared-event', calendarId: '00000000-0000-4000-8000-000000000001' }, right: { eventId: 'shared-event', calendarId: '00000000-0000-4000-8000-000000000002' }, relationship: 'same_interval', overlapStart: today.toISOString(), overlapEnd: new Date(today.getTime() + 3_600_000).toISOString() },
        { left: { eventId: 'today-calendar-all-day', calendarId: '00000000-0000-4000-8000-000000000001' }, right: { eventId: 'today-calendar-timed', calendarId: '00000000-0000-4000-8000-000000000001' }, relationship: 'contains', overlapStart: today.toISOString(), overlapEnd: new Date(today.getTime() + 3_600_000).toISOString() },
      ] : [];
      return route.fulfill({ json: { rangeStart: today.toISOString(), rangeEnd: tomorrow.toISOString(), displayTimeZone: 'Europe/Brussels', items: conflicts, nextCursor: null, partial: false, warnings: [], evaluatedAt: today.toISOString() } });
    }
    if (
      url.pathname === '/api/v1/approvals/stream' ||
      url.pathname === '/api/v1/notifications/stream'
    ) {
      const streamRequestCount = fixture.requestCounts[`GET ${url.pathname}`] ?? 0;
      if (
        url.pathname === '/api/v1/approvals/stream' &&
        fixture.approvalStreamRefreshAfterLoad &&
        streamRequestCount === 1
      ) {
        await new Promise((resolve) => setTimeout(resolve, 300));
        return route.fulfill({
          headers: { 'Content-Type': 'text/event-stream' },
          body: [
            'event: approval.pending',
            'data: {"schemaVersion":1,"eventId":"00000000-0000-4000-8000-000000000001","eventType":"approval.pending","approvalId":"00000000-0000-4000-8000-000000000002","proposalId":"00000000-0000-4000-8000-000000000003","runId":"00000000-0000-4000-8000-000000000004","conversationId":"00000000-0000-4000-8000-000000000005","toolId":"reminder.create","toolVersion":1,"status":"pending","executionStatus":"not_started","version":1,"occurredAt":"2026-10-26T12:00:00Z"}',
            '',
            '',
          ].join('\n'),
        });
      }
      // Keep the owner-scoped stream open for the duration of the page. A
      // closed fixture stream would intentionally move a healthy source into
      // reconnecting state before the action-center assertions run.
      await new Promise<void>(() => undefined);
      return route.abort();
    }
    if (url.pathname === '/api/v1/approvals') {
      const approvalRequestCount = fixture.requestCounts['GET /api/v1/approvals'] ?? 0;
      const approvalStatus =
        fixture.approvalsStatus ??
        (fixture.approvalsStatusAfterLoad && approvalRequestCount > 1
          ? fixture.approvalsStatusAfterLoad
          : undefined);
      if (approvalStatus)
        return route.fulfill({
          status: approvalStatus,
          json: { detail: 'approval source unavailable' },
        });
      return route.fulfill({ json: { items: fixture.approvals, nextCursor: null } });
    }
    if (url.pathname === '/api/v1/notifications') {
      if (fixture.notificationsStatus)
        return route.fulfill({
          status: fixture.notificationsStatus,
          json: { detail: 'notification source unavailable' },
        });
      return route.fulfill({
        json: {
          items:
            url.searchParams.get('status') === 'acknowledged'
              ? ((historyVisited = true),
                fixture.notifications.map((notification) => ({
                  ...notification,
                  status: 'acknowledged',
                  acknowledgedAt: notification.updatedAt,
                })))
              : historyVisited
                ? (fixture.notificationsAfterHistory ?? fixture.notifications)
                : fixture.notifications,
          nextCursor: null,
          unacknowledgedCount:
            url.searchParams.get('status') === 'acknowledged'
              ? 0
              : (historyVisited
                  ? (fixture.notificationsAfterHistory ?? fixture.notifications)
                  : fixture.notifications
                ).length,
        },
      });
    }
    return route.fulfill({ status: 404, json: { detail: 'fixture endpoint not implemented' } });
  });

  return fixture;
}

test.describe('Today action center', () => {
  test('merges the read-only calendar agenda without displacing reminder and conversation sections', async ({ page }) => {
    await installTodayFixture(page, { calendarState: 'ready' });
    await page.goto('/today');
    await expect(page.getByRole('heading', { name: 'Calendar agenda', exact: true })).toBeVisible();
    await expect(page.getByText('Planning day', { exact: true })).toBeVisible();
    await expect(page.getByText('Focus block', { exact: true })).toBeVisible();
    await expect(page.locator('.calendar-agenda .agenda-region').nth(0)).toContainText('Planning day');
    await expect(page.locator('.calendar-agenda .agenda-region').nth(1)).toContainText('Focus block');
    await expect(page.getByText('Call the dentist', { exact: true })).toBeVisible();
    await expect(page.getByText('Plan the weekend', { exact: true })).toBeVisible();
    await expect(page.getByText(/Calendar · timed/)).toBeVisible();
    const results = await new AxeBuilder({ page }).include('.calendar-agenda').analyze();
    expect(results.violations).toEqual([]);
  });

  test('keeps calendar partial and unavailable recovery local to the agenda', async ({ page }) => {
    await installTodayFixture(page, { calendarState: 'partial' });
    await page.goto('/today');
    await page.locator('.calendar-agenda').getByRole('button', { name: 'Refresh', exact: true }).click();
    await expect(page.locator('.calendar-agenda')).toContainText(/Some calendars could not be read|Some calendar events could not be loaded/i);
    await expect(page.getByText('Call the dentist', { exact: true })).toBeVisible();

    await page.unroute('**/api/v1/**');
    await installTodayFixture(page, { calendarState: 'stale' });
    await page.goto('/today');
    await page.locator('.calendar-agenda').getByRole('button', { name: 'Retry', exact: true }).click();
    await expect(page.locator('.calendar-agenda')).toContainText(/temporarily unavailable/i);
    await expect(page.getByText('Plan the weekend', { exact: true })).toBeVisible();
  });

  test('renders multi-day events and server conflicts by composite calendar identity', async ({ page }) => {
    await installTodayFixture(page, { calendarState: 'ready', calendarComplex: true });
    await page.goto('/today');
    await expect(page.getByText('Team offsite', { exact: true })).toBeVisible();
    await expect(page.getByText(/Calendar · all day · multi-day/)).toBeVisible();
    await expect(page.getByText(/Conflicts with .*\(same time\)/)).toHaveCount(2);
    await expect(page.getByText(/Conflicts with .*\(contains\)/)).toBeVisible();
    await expect(page.locator('.calendar-agenda .agenda-region').nth(0)).toContainText('Team offsite');
    await expect(page.locator('.calendar-agenda .agenda-region').nth(1)).toContainText('Focus block');
  });

  test('keeps long calendar titles readable on mobile at 200 percent zoom', async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== 'desktop', 'Long-title geometry runs once in desktop.');
    await installTodayFixture(page, { calendarState: 'ready', calendarLongTitle: true });
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto('/today');
    await expect(page.getByText(/A deliberately long multi-day planning title/)).toBeVisible();
    await page.evaluate(() => { document.documentElement.style.zoom = '2'; });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true);
    await expect(page.getByText(/Calendar · all day · multi-day/)).toBeVisible();
  });

  for (const state of ['disconnected', 'rate-limited', 'permission-denied', 'offline'] as const) {
    test(`keeps Today useful when calendar is ${state}`, async ({ page }) => {
      await installTodayFixture(page, { calendarState: state });
      await page.goto('/today');
      await expect(page.locator('.calendar-agenda')).toContainText(/calendar|offline|permission|rate-limited/i);
      await expect(page.getByText('Call the dentist', { exact: true })).toBeVisible();
      await expect(page.getByText('Plan the weekend', { exact: true })).toBeVisible();
      if (state === 'permission-denied') {
        await expect(page.locator('.calendar-agenda')).toContainText(/permission(?: was)? denied/i);
        await expect(page.getByText('Planning day', { exact: true })).toHaveCount(0);
        await expect(page.getByText('Focus block', { exact: true })).toHaveCount(0);
      }
      await expect(page.locator('.action-card')).toHaveCount(5);
    });
  }

  test('redirects the root to Today and composes bounded navigation-only source cards', async ({
    page,
  }) => {
    await installTodayFixture(page);
    await page.goto('/');

    await expect(page).toHaveURL(/\/today$/);
    await expect(page.locator('#today-heading')).toBeVisible();
    await expect(page.getByRole('heading', { name: 'Needs attention', exact: true })).toBeVisible();
    await expect(page.getByRole('heading', { name: 'Continue', exact: true })).toBeVisible();
    await expect(page.getByText('Call the dentist', { exact: true })).toBeVisible();
    await expect(page.getByText('Pack for tomorrow', { exact: true })).toBeVisible();
    await expect(page.getByText('Plan the weekend', { exact: true })).toBeVisible();

    // Archived conversation titles and approval proposal content are not part
    // of the compact action-center projection.
    await expect(page.getByText('Archived private title', { exact: true })).toHaveCount(0);
    await expect(page.getByText('proposal-private', { exact: true })).toHaveCount(0);
    await expect(
      page.locator('a.action-card').filter({ hasText: 'Call the dentist' }),
    ).toHaveAttribute('href', /\/reminders\/reminder-fired$/);
    await expect(
      page.locator('a.action-card').filter({ hasText: 'Plan the weekend' }),
    ).toHaveAttribute('href', /\/conversation\/conversation-1$/);
    await expect(page.getByRole('link', { name: /Approval awaiting review/ })).toHaveAttribute(
      'href',
      /\/approvals\/approval-1$/,
    );
    await expect(
      page.getByRole('link', { name: /Reminder notification delivered/ }),
    ).toHaveAttribute('href', /\/notifications\/notification-1$/);

    const cards = page.locator('.action-card');
    await expect(cards).toHaveCount(5);
    await expect(cards.locator('button')).toHaveCount(0);
    const results = await new AxeBuilder({ page }).include('.daily-action').analyze();
    expect(results.violations).toEqual([]);
  });

  test('keeps available sections when one source fails and exposes scoped retry without request amplification', async ({
    page,
  }) => {
    const fixture = await installTodayFixture(page, { notificationsStatus: 503 });
    await page.goto('/today');

    await expect(
      page.getByText(/notifications (could not be loaded|may be out of date)/i),
    ).toBeVisible();
    await expect(page.getByText('Call the dentist', { exact: true })).toBeVisible();
    await expect(page.getByText('Plan the weekend', { exact: true })).toBeVisible();
    await expect(page.getByText('Approval awaiting review', { exact: true })).toBeVisible();
    await expect(page.getByRole('link', { name: /Reminder notification delivered/ })).toHaveCount(
      0,
    );

    const notificationRequestsBeforeRetry = fixture.requestCounts['GET /api/v1/notifications'] ?? 0;
    await page
      .locator('p.section-status')
      .filter({ hasText: /notifications (could not be loaded|may be out of date)/i })
      .getByRole('button', { name: 'Retry', exact: true })
      .click();
    await expect
      .poll(() => fixture.requestCounts['GET /api/v1/notifications'] ?? 0)
      .toBe(notificationRequestsBeforeRetry + 1);
    await expect(
      page.getByText(/notifications (could not be loaded|may be out of date)/i),
    ).toBeVisible();
    expect(fixture.requestCounts['GET /api/v1/reminders'] ?? 0).toBeLessThanOrEqual(2);
    expect(fixture.requestCounts['GET /api/v1/conversations'] ?? 0).toBeLessThanOrEqual(2);
  });

  test('renders a calm all-clear state when every source is empty', async ({ page }) => {
    await installTodayFixture(page, {
      approvals: [],
      notifications: [],
      reminders: [],
      conversations: [],
    });
    await page.goto('/today');

    await expect(page.getByText('All clear for now.', { exact: true })).toBeVisible();
    await expect(page.getByText('Nothing due today.', { exact: true })).toBeVisible();
    await expect(page.getByText('Start a conversation.', { exact: true })).toBeVisible();
    await expect(page.getByRole('link', { name: 'Ask Aura' })).toHaveAttribute(
      'href',
      /\/conversation$/,
    );
  });

  test('clears private Today data when an owner source rejects the accepted session', async ({
    page,
  }) => {
    await installTodayFixture(page, {
      approvalsStatusAfterLoad: 403,
      approvalStreamRefreshAfterLoad: true,
    });
    await page.goto('/today');

    await expect(page.getByText('Approval awaiting review', { exact: true })).toBeVisible();

    await expect(page.locator('.auth-notice')).toContainText('Today access is unavailable.');
    await expect(page.locator('#today-heading')).toBeVisible();
    await expect(page.locator('.action-card')).toHaveCount(0);
    await expect(page.getByText('Call the dentist', { exact: true })).toHaveCount(0);
    await expect(page.getByText('Plan the weekend', { exact: true })).toHaveCount(0);
    await expect(page.getByText('Archived private title', { exact: true })).toHaveCount(0);
    await expect(page.getByText('proposal-private', { exact: true })).toHaveCount(0);
    await expect(page.getByRole('button', { name: /sign in/i })).toHaveCount(0);
    await expect(page.getByRole('link', { name: /sign in/i })).toHaveCount(0);
    await expect(page.getByRole('button', { name: /^Retry$/ })).toHaveCount(0);
  });

  test('offers the canonical sign-in path when Today has no authenticated session', async ({
    page,
  }) => {
    await installTodayFixture(page, { sessionStatus: 401 });
    await page.goto('/today');

    await expect(page.locator('.auth-notice')).toContainText(
      'Sign in to see your private Today view.',
    );
    const signIn = page.getByRole('button', { name: /sign in/i });
    await expect(signIn).toHaveCount(1);
    await expect(signIn).toBeEnabled();
  });

  test('keeps an accepted session distinct from permission-unavailable Today access', async ({
    page,
  }) => {
    await installTodayFixture(page, {
      approvalsStatusAfterLoad: 403,
      approvalStreamRefreshAfterLoad: true,
    });
    await page.goto('/today');

    await expect(page.getByText('Approval awaiting review', { exact: true })).toBeVisible();

    await expect(page.locator('.auth-notice')).toContainText('Today access is unavailable.');
    await expect(page.locator('.auth-notice')).not.toContainText(/sign in/i);
    await expect(page.locator('.auth-notice').getByRole('button')).toHaveCount(0);
    await expect(page.getByRole('button', { name: /^Retry$/ })).toHaveCount(0);
  });

  test('keeps unread notifications fresh on Today after visiting History', async ({
    page,
  }, testInfo) => {
    test.skip(
      testInfo.project.name !== 'desktop',
      'Cross-workspace freshness runs once in desktop.',
    );
    const freshNotification = {
      id: 'notification-fresh',
      reminderId: 'reminder-next',
      status: 'delivered',
      version: 1,
      createdAt: today.toISOString(),
      updatedAt: today.toISOString(),
      deliveredAt: today.toISOString(),
      acknowledgedAt: null,
    };
    await installTodayFixture(page, {
      notificationsAfterHistory: [
        {
          ...freshNotification,
          id: 'notification-1',
          reminderId: 'reminder-fired',
        },
        freshNotification,
      ],
    });
    await page.setViewportSize({ width: 1280, height: 900 });
    await page.goto('/today');
    await expect(page.getByRole('link', { name: /Reminder notification delivered/ })).toHaveCount(
      1,
    );

    await page.getByRole('link', { name: /^Notifications/ }).click();
    await expect(page).toHaveURL(/\/notifications$/);
    await expect(page.getByRole('button', { name: /History/ })).toBeVisible();
    await page.getByRole('button', { name: /History/ }).click();
    await expect(page.getByRole('button', { name: /History/ })).toHaveAttribute(
      'aria-current',
      'page',
    );
    await expect(page.getByText('A reminder is due.', { exact: true })).toHaveCount(1);
    await expect(page.getByRole('heading', { name: 'No notification history' })).toBeHidden();

    await page.getByRole('link', { name: /^Today$/ }).click();
    await expect(page).toHaveURL(/\/today$/);
    await expect(page.getByRole('link', { name: /Reminder notification delivered/ })).toHaveCount(
      2,
    );
  });

  test('uses a readable single-column Today canvas at 900px', async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== 'desktop', 'Today canvas geometry runs once in desktop.');
    await installTodayFixture(page);
    await page.setViewportSize({ width: 900, height: 1000 });
    await page.goto('/today');

    const metrics = await page.locator('.daily-grid').evaluate((grid) => {
      const style = getComputedStyle(grid);
      const page = document.querySelector<HTMLElement>('.daily-action');
      return {
        columns: style.gridTemplateColumns.split(' ').filter(Boolean).length,
        pageWidth: page?.getBoundingClientRect().width ?? 0,
        viewportWidth: window.innerWidth,
        scrollWidth: document.documentElement.scrollWidth,
      };
    });
    expect(metrics.columns).toBe(1);
    expect(metrics.pageWidth).toBeLessThanOrEqual(metrics.viewportWidth + 1);
    expect(metrics.scrollWidth).toBeLessThanOrEqual(metrics.viewportWidth);
  });

  test('keeps a single mobile reading order, touch-sized cards, and a reduced-motion 200 percent reflow', async ({
    page,
  }, testInfo) => {
    test.skip(
      testInfo.project.name !== 'desktop',
      'Today responsive geometry runs once in desktop.',
    );
    await installTodayFixture(page);
    await page.setViewportSize({ width: 390, height: 844 });
    await page.emulateMedia({ reducedMotion: 'reduce' });
    await page.goto('/today');

    const sectionOrder = await page
      .locator('.daily-section')
      .evaluateAll((sections) =>
        sections.map((section) => section.querySelector('h2')?.textContent?.trim()),
      );
    expect(sectionOrder).toEqual(['Calendar agenda', 'Needs attention', 'Today', 'Continue', 'Later']);
    expect(
      await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth),
    ).toBe(true);
    expect(
      await page
        .locator('.action-card')
        .evaluateAll((cards) => cards.every((card) => card.getBoundingClientRect().height >= 44)),
    ).toBe(true);
    expect(
      await page
        .locator('.action-card')
        .first()
        .evaluate((card) =>
          getComputedStyle(card)
            .transitionDuration.split(',')
            .every((value) => Number.parseFloat(value) < 0.001),
        ),
    ).toBe(true);

    await page.evaluate(() => {
      document.documentElement.style.zoom = '2';
    });
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= document.documentElement.clientWidth,
      ),
    ).toBe(true);
  });
});
