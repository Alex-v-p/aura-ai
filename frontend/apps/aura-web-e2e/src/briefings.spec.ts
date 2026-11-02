import { expect, test, type Page } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';

const now = '2099-10-08T09:00:00.000Z';
const ids = {
  agent: '00000000-0000-4000-8000-000000000001',
  revision: '00000000-0000-4000-8000-000000000002',
  prompt: '00000000-0000-4000-8000-000000000003',
  model: '00000000-0000-4000-8000-000000000004',
  tool: '00000000-0000-4000-8000-000000000005',
  schedule: '00000000-0000-4000-8000-000000000006',
  occurrence: '00000000-0000-4000-8000-000000000007',
  result: '00000000-0000-4000-8000-000000000008',
  calendar: '00000000-0000-4000-8000-000000000009',
  document: '00000000-0000-4000-8000-000000000010',
  notification: '00000000-0000-4000-8000-000000000011',
} as const;

const session = {
  principal: {
    issuer: 'https://authentik.test',
    subject: 'fixture-owner',
    displayName: 'Fixture Owner',
  },
  csrfToken: 'fixture-csrf',
  idleExpiresAt: '2099-10-08T10:00:00Z',
  absoluteExpiresAt: '2099-10-09T09:00:00Z',
};

const capabilities = {
  executionPolicyRevisionId: 'e80e8c61-575a-5a8c-862e-0c7cc91baa61',
  executionPolicyRevision: 'briefing-execution-v1',
  deadlineSeconds: 300,
  maxOutputTokens: 16_384,
  maxOutputChars: 32_768,
  cadenceKind: 'daily',
  memoryEnabled: false,
  toolsEnabled: false,
};

const agent = {
  id: ids.agent,
  status: 'active',
  version: 4,
  currentRevision: {
    id: ids.revision,
    profileId: ids.agent,
    revision: 3,
    displayName: 'Daily planner',
    purpose: 'Plans the owner’s day.',
    instructions: 'Use only the authorized briefing sources.',
    personaRevisionId: '00000000-0000-4000-8000-000000000012',
    promptBundleRevisionId: ids.prompt,
    modelPolicyRevisionId: ids.model,
    memoryPolicyRevisionId: '00000000-0000-4000-8000-000000000013',
    toolPolicyRevisionId: ids.tool,
    createdAt: now,
  },
  createdAt: now,
  updatedAt: now,
};

const schedule = (status: 'active' | 'paused' | 'deleted' = 'active', version = 1) => ({
  id: ids.schedule,
  displayName: 'My daily briefing',
  status,
  version,
  cadence: { kind: 'daily', localTime: '08:30', timeZone: 'Europe/Brussels' },
  sources: { includeReminders: true, calendarIds: [ids.calendar], documentIds: [ids.document] },
  agentRevisionId: ids.revision,
  executionPolicies: {
    executionPolicyRevisionId: capabilities.executionPolicyRevisionId,
    promptBundleRevisionId: ids.prompt,
    modelPolicyRevisionId: ids.model,
    toolPolicyRevisionId: ids.tool,
  },
  nextDueAt: '2099-10-09T06:30:00Z',
  createdAt: now,
  updatedAt: now,
  deletedAt: status === 'deleted' ? now : null,
});

const occurrence = (resultId: string | null = ids.result) => ({
  id: ids.occurrence,
  scheduleId: ids.schedule,
  sequence: 1,
  status: 'completed',
  version: 2,
  dueAt: '2099-10-08T06:30:00Z',
  dueLocalDate: '2099-10-08',
  dueLocalTime: '08:30',
  timeZone: 'Europe/Brussels',
  sourceCompleteness: 'complete',
  runId: '00000000-0000-4000-8000-000000000014',
  resultId,
  createdAt: now,
  updatedAt: now,
  startedAt: '2099-10-08T06:30:02Z',
  finishedAt: '2099-10-08T06:31:02Z',
});

const result = (deleted = false) => ({
  id: ids.result,
  scheduleId: ids.schedule,
  occurrenceId: ids.occurrence,
  runId: '00000000-0000-4000-8000-000000000014',
  status: deleted ? 'deleted' : 'completed',
  version: deleted ? 2 : 1,
  sourceCompleteness: 'complete',
  content: deleted ? null : 'Private result content that must stay on the detail route.',
  createdAt: now,
  updatedAt: now,
  deletedAt: deleted ? now : null,
});

type BriefingFixture = {
  requests: Array<{ method: string; path: string; body: unknown }>;
  state: { schedule: ReturnType<typeof schedule>; result: ReturnType<typeof result> };
  createStatus?: number;
  pauseConflict?: boolean;
  authStatus?: number;
  scheduleStatus?: number;
  calendarPagination?: 'complete' | 'incomplete' | 'recoverable';
  documentPagination?: 'complete' | 'incomplete' | 'recoverable';
};

const json = (value: unknown, headers: Record<string, string> = {}) => ({
  status: 200,
  headers: { 'cache-control': 'no-store', ...headers },
  json: value,
});

async function installBriefingFixture(
  page: Page,
  options: Omit<BriefingFixture, 'requests' | 'state'> = {},
): Promise<BriefingFixture> {
  const fixture: BriefingFixture = {
    requests: [],
    state: { schedule: schedule(), result: result() },
    calendarPagination: 'complete',
    documentPagination: 'complete',
    ...options,
  };
  let calendarListCalls = 0;
  let documentListCalls = 0;

  await page.route('**/api/v1/**', async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const body = request.postData() ? (request.postDataJSON() as unknown) : null;
    fixture.requests.push({ method: request.method(), path: url.pathname, body });

    if (url.pathname === '/api/v1/auth/session') {
      return route.fulfill(
        fixture.authStatus
          ? { status: fixture.authStatus, json: { detail: 'Session expired.' } }
          : json(session),
      );
    }
    if (url.pathname === '/api/v1/models')
      return route.fulfill(json({ models: [], defaultModelId: null, observedAt: now }));
    if (url.pathname === '/api/v1/personas') return route.fulfill(json({ items: [] }));
    if (url.pathname === '/api/v1/conversations')
      return route.fulfill(json({ items: [], nextCursor: null }));
    if (url.pathname === '/api/v1/agents') return route.fulfill(json({ items: [agent] }));
    if (url.pathname === `/api/v1/agents/${ids.agent}`)
      return route.fulfill(json({ ...agent, revisions: [agent.currentRevision] }));
    if (url.pathname === `/api/v1/agents/${ids.agent}/tool-policies`) {
      return route.fulfill(
        json({
          items: [
            {
              id: ids.tool,
              agentProfileId: ids.agent,
              revision: 1,
              grants: [],
              maxCallsPerRun: 0,
              timeoutMs: 0,
              retryLimit: 0,
              createdAt: now,
            },
          ],
          attachedPolicyRevisionId: ids.tool,
          agentVersion: agent.version,
        }),
      );
    }
    if (url.pathname === '/api/v1/briefing-capabilities') return route.fulfill(json(capabilities));
    if (url.pathname === '/api/v1/integrations/calendar/connection')
      return route.fulfill(
        json({
          status: 'connected',
          readsEnabled: true,
          selectedCalendarCount: 1,
          connectedAt: now,
          updatedAt: now,
          lastError: null,
          availableActions: ['disconnect'],
        }),
      );
    if (url.pathname === '/api/v1/integrations/calendar/calendars') {
      calendarListCalls += 1;
      const pagination = fixture.calendarPagination ?? 'complete';
      const nextCursor =
        pagination === 'complete' || (pagination === 'recoverable' && calendarListCalls > 2)
          ? null
          : 'calendar-page-2';
      return route.fulfill(
        json({
          items: [
            {
              id: ids.calendar,
              displayName: 'Owner planning calendar',
              timeZone: 'Europe/Brussels',
              selected: true,
              discoveredAt: now,
            },
          ],
          nextCursor,
          selectionVersion: 1,
          observedAt: now,
        }),
      );
    }
    if (
      (url.pathname === '/api/v1/integrations/calendar/events/query' ||
        url.pathname === '/api/v1/integrations/calendar/conflicts/query') &&
      request.method() === 'POST'
    )
      return route.fulfill(
        json({
          rangeStart: '2099-10-08T00:00:00.000Z',
          rangeEnd: '2099-10-09T00:00:00.000Z',
          displayTimeZone: 'Europe/Brussels',
          items: [],
          nextCursor: null,
          partial: false,
          warnings: [],
          ...(url.pathname.endsWith('/events/query') ? { retrievedAt: now } : { evaluatedAt: now }),
        }),
      );
    if (url.pathname === '/api/v1/documents') {
      documentListCalls += 1;
      const pagination = fixture.documentPagination ?? 'complete';
      const nextCursor =
        pagination === 'complete' || (pagination === 'recoverable' && documentListCalls > 1)
          ? null
          : 'documents-page-2';
      return route.fulfill(
        json({
          items: [
            {
              id: ids.document,
              filename: 'Owner-selected project brief',
              mediaType: 'text/plain',
              status: 'ready',
              version: 1,
              createdAt: now,
              updatedAt: now,
            },
          ],
          nextCursor,
        }),
      );
    }
    if (url.pathname === '/api/v1/reminders')
      return route.fulfill(
        json({
          items: [
            {
              id: '00000000-0000-4000-8000-000000000015',
              content: 'Take the recycling out',
              dueAt: now,
              status: 'scheduled',
              version: 1,
              createdAt: now,
              updatedAt: now,
              firedAt: null,
              completedAt: null,
              canceledAt: null,
            },
          ],
          nextCursor: null,
        }),
      );

    if (url.pathname === '/api/v1/briefing-schedules' && request.method() === 'GET') {
      if (fixture.scheduleStatus)
        return route.fulfill({
          status: fixture.scheduleStatus,
          headers: { 'cache-control': 'no-store' },
          json: { detail: 'Permission denied.' },
        });
      return route.fulfill(json({ items: [fixture.state.schedule], nextCursor: null }));
    }
    if (url.pathname === '/api/v1/briefing-schedules' && request.method() === 'POST') {
      if (fixture.createStatus)
        return route.fulfill({
          status: fixture.createStatus,
          headers: { 'cache-control': 'no-store' },
          json: { detail: 'The briefing could not be created yet.' },
        });
      fixture.state.schedule = schedule();
      return route.fulfill({ ...json(fixture.state.schedule), status: 201 });
    }
    if (url.pathname === `/api/v1/briefing-schedules/${ids.schedule}` && request.method() === 'GET')
      return route.fulfill(json(fixture.state.schedule));
    if (
      url.pathname === `/api/v1/briefing-schedules/${ids.schedule}/occurrences` &&
      request.method() === 'GET'
    )
      return route.fulfill(
        json({
          items: [occurrence(fixture.state.result.deletedAt ? null : ids.result)],
          nextCursor: null,
        }),
      );
    if (url.pathname === `/api/v1/briefing-schedules/${ids.schedule}/occurrences/${ids.occurrence}`)
      return route.fulfill(json(occurrence(fixture.state.result.deletedAt ? null : ids.result)));
    if (url.pathname === `/api/v1/briefing-results/${ids.result}` && request.method() === 'GET')
      return route.fulfill(json(fixture.state.result));
    if (url.pathname === `/api/v1/briefing-schedules/${ids.schedule}/pause`) {
      if (fixture.pauseConflict)
        return route.fulfill({
          status: 409,
          headers: { 'cache-control': 'no-store' },
          json: { detail: 'This briefing changed elsewhere.' },
        });
      fixture.state.schedule = schedule('paused', fixture.state.schedule.version + 1);
      return route.fulfill(json(fixture.state.schedule));
    }
    if (url.pathname === `/api/v1/briefing-schedules/${ids.schedule}/resume`) {
      fixture.state.schedule = schedule('active', fixture.state.schedule.version + 1);
      return route.fulfill(json(fixture.state.schedule));
    }
    if (url.pathname === `/api/v1/briefing-schedules/${ids.schedule}/deletions`) {
      fixture.state.schedule = schedule('deleted', fixture.state.schedule.version + 1);
      return route.fulfill(
        json({
          deletionId: ids.notification,
          scope: 'schedule',
          scheduleId: ids.schedule,
          occurrenceId: null,
          resultId: null,
          deletedAt: now,
        }),
      );
    }
    if (url.pathname === `/api/v1/briefing-schedules/${ids.schedule}/history-deletions`) {
      return route.fulfill(
        json({
          deletionId: ids.notification,
          scope: 'occurrence_history',
          scheduleId: ids.schedule,
          occurrenceId: ids.occurrence,
          resultId: null,
          deletedAt: now,
        }),
      );
    }
    if (url.pathname === `/api/v1/briefing-results/${ids.result}/deletions`) {
      fixture.state.result = result(true);
      return route.fulfill(
        json({
          deletionId: ids.notification,
          scope: 'result_content',
          scheduleId: ids.schedule,
          occurrenceId: ids.occurrence,
          resultId: ids.result,
          deletedAt: now,
        }),
      );
    }

    if (url.pathname === '/api/v1/notifications')
      return route.fulfill(
        json({
          items: [
            {
              id: ids.notification,
              briefingScheduleId: ids.schedule,
              briefingOccurrenceId: ids.occurrence,
              briefingResultId: ids.result,
              status: 'delivered',
              version: 1,
              createdAt: now,
              updatedAt: now,
              deliveredAt: now,
              acknowledgedAt: null,
            },
          ],
          nextCursor: null,
          unacknowledgedCount: 1,
        }),
      );
    if (url.pathname === `/api/v1/notifications/${ids.notification}`)
      return route.fulfill(
        json({
          id: ids.notification,
          briefingScheduleId: ids.schedule,
          briefingOccurrenceId: ids.occurrence,
          briefingResultId: ids.result,
          status: 'delivered',
          version: 1,
          createdAt: now,
          updatedAt: now,
          deliveredAt: now,
          acknowledgedAt: null,
        }),
      );
    if (url.pathname.endsWith('/notifications/stream'))
      return route.fulfill({
        status: 200,
        headers: { 'content-type': 'text/event-stream', 'cache-control': 'no-store' },
        body: 'event: heartbeat\ndata: {"schemaVersion":1,"eventType":"heartbeat","occurredAt":"2099-10-08T09:00:00.000Z"}\n\n',
      });
    return route.fulfill({
      status: 404,
      headers: { 'cache-control': 'no-store' },
      json: { detail: 'Fixture endpoint not implemented.' },
    });
  });
  return fixture;
}

function request(
  fixture: BriefingFixture,
  method: string,
  path: string,
): { method: string; path: string; body: unknown } | undefined {
  return fixture.requests.find((item) => item.method === method && item.path === path);
}

test.describe('scheduled briefing management', () => {
  test('discovers the closed capability and creates a daily owner briefing from selected sources', async ({
    page,
  }) => {
    const fixture = await installBriefingFixture(page);
    const capabilityResponse = page.waitForResponse(
      (response) => new URL(response.url()).pathname === '/api/v1/briefing-capabilities',
    );
    await page.goto('/briefings/new');
    await expect.poll(() => request(fixture, 'GET', '/api/v1/briefing-capabilities')).toBeTruthy();
    expect((await capabilityResponse).headers()['cache-control']).toBe('no-store');
    await expect(
      page.getByRole('heading', { name: /new briefing|create.*briefing/i }),
    ).toBeVisible();

    await page.getByLabel('Name', { exact: true }).fill('My daily briefing');
    await page.getByLabel(/local time/i).fill('08:30');
    await page.getByLabel(/timezone/i).fill('Europe/Brussels');
    await page.getByLabel(/active agent revision/i).selectOption(ids.revision);
    await page.getByLabel(/owner planning calendar/i).check();
    await page.getByLabel(/owner-selected project brief/i).check();
    await page.getByRole('button', { name: /create briefing|save briefing/i }).click();

    await expect.poll(() => request(fixture, 'POST', '/api/v1/briefing-schedules')).toBeTruthy();
    const create = request(fixture, 'POST', '/api/v1/briefing-schedules');
    expect(create?.body).toMatchObject({
      displayName: 'My daily briefing',
      cadence: { kind: 'daily', localTime: '08:30', timeZone: 'Europe/Brussels' },
      sources: { includeReminders: true, calendarIds: [ids.calendar], documentIds: [ids.document] },
      agentRevisionId: ids.revision,
      executionPolicies: {
        executionPolicyRevisionId: capabilities.executionPolicyRevisionId,
        promptBundleRevisionId: ids.prompt,
        modelPolicyRevisionId: ids.model,
        toolPolicyRevisionId: ids.tool,
      },
    });
    const capability = request(fixture, 'GET', '/api/v1/briefing-capabilities');
    expect(capability).toBeTruthy();
    expect(
      fixture.requests.filter(
        (item) => item.method === 'GET' && item.path === '/api/v1/briefing-capabilities',
      ),
    ).toHaveLength(1);
    expect(capabilities).toEqual({
      executionPolicyRevisionId: capabilities.executionPolicyRevisionId,
      executionPolicyRevision: 'briefing-execution-v1',
      deadlineSeconds: 300,
      maxOutputTokens: 16_384,
      maxOutputChars: 32_768,
      cadenceKind: 'daily',
      memoryEnabled: false,
      toolsEnabled: false,
    });
    expect(Object.keys(capabilities).sort()).toEqual([
      'cadenceKind',
      'deadlineSeconds',
      'executionPolicyRevision',
      'executionPolicyRevisionId',
      'maxOutputChars',
      'maxOutputTokens',
      'memoryEnabled',
      'toolsEnabled',
    ]);
    expect(JSON.stringify(capabilities)).not.toMatch(
      /provider|modelId|prompt|ownerId|source|toolPolicy|runId|resultId/i,
    );
    expect(page.url()).not.toContain('Owner planning calendar');
    expect(page.url()).not.toContain(ids.result);
    expect(
      await page.evaluate(() => JSON.stringify({ local: localStorage, session: sessionStorage })),
    ).not.toContain('Owner-selected');
  });

  test('keeps creation fail-closed while calendar pagination is incomplete and recovers after loading it', async ({
    page,
  }) => {
    await installBriefingFixture(page, { calendarPagination: 'recoverable' });
    await page.goto('/briefings/new');

    await expect(page.getByRole('button', { name: 'Create briefing', exact: true })).toBeDisabled();
    await expect(page.getByRole('status')).toContainText(/load all calendar pages/i);
    const loadMore = page.getByRole('button', { name: 'Load more calendars', exact: true });
    await expect(loadMore).toBeVisible();
    await loadMore.click();

    await expect(loadMore).toBeHidden();
    await expect(
      page.getByRole('checkbox', { name: 'Owner planning calendar' }).first(),
    ).toBeVisible();
  });

  test('keeps creation fail-closed while document pagination is incomplete and recovers after loading it', async ({
    page,
  }) => {
    await installBriefingFixture(page, { documentPagination: 'recoverable' });
    await page.goto('/briefings/new');

    await expect(page.getByRole('button', { name: 'Create briefing', exact: true })).toBeDisabled();
    await expect(page.getByRole('status')).toContainText(/load all document pages/i);
    const loadMore = page.getByRole('button', { name: 'Load more documents', exact: true });
    await expect(loadMore).toBeVisible();
    await loadMore.click();

    await expect(loadMore).toBeHidden();
    await expect(
      page.getByRole('checkbox', { name: 'Owner-selected project brief' }).first(),
    ).toBeVisible();
  });

  test('preserves the creation draft after an unknown server failure and clears it after discard', async ({
    page,
  }) => {
    const fixture = await installBriefingFixture(page, { createStatus: 503 });
    await page.goto('/briefings/new');
    await page.getByLabel('Name', { exact: true }).fill('Draft that must survive recovery');
    await page.getByLabel(/local time/i).fill('08:30');
    await page.getByLabel(/timezone/i).fill('Europe/Brussels');
    await page.getByLabel(/active agent revision/i).selectOption(ids.revision);
    await page.getByLabel(/owner planning calendar/i).check();
    await page.getByLabel(/owner-selected project brief/i).check();
    await page.getByRole('button', { name: /create briefing|save briefing/i }).click();
    await expect(page.getByRole('alert')).toContainText(/could not|try again|unavailable/i);
    await expect(page.getByLabel('Name', { exact: true })).toHaveValue(
      'Draft that must survive recovery',
    );

    await page.getByRole('button', { name: 'Discard draft', exact: true }).last().click();
    const draftDialog = page.getByRole('alertdialog');
    await expect(draftDialog).toContainText(/draft|selected sources/i);
    await expect(draftDialog.getByRole('button').first()).toBeFocused();
    await draftDialog.getByRole('button', { name: 'Discard draft', exact: true }).click();
    await expect(page).toHaveURL(/\/briefings$/);
    expect(
      await page.evaluate(() => JSON.stringify({ local: localStorage, session: sessionStorage })),
    ).not.toContain('Draft that must survive recovery');
    expect(
      fixture.requests.filter(
        (item) => item.method === 'POST' && item.path === '/api/v1/briefing-schedules',
      ),
    ).toHaveLength(1);
  });

  test('lists history, preserves source metadata, and recovers a stale pause before resuming', async ({
    page,
  }) => {
    const fixture = await installBriefingFixture(page, { pauseConflict: true });
    await page.goto('/briefings');
    await expect.poll(() => request(fixture, 'GET', '/api/v1/briefing-schedules')).toBeTruthy();
    await expect(page.getByText('My daily briefing', { exact: true })).toBeVisible();
    await page.getByText('My daily briefing', { exact: true }).click();
    await expect(page.getByText(/08:30|Europe\/Brussels/)).toBeVisible();
    await expect(page.getByText(/history|occurrence/i).first()).toBeVisible();
    await expect(
      page
        .locator('.detail-card')
        .getByText(/calendar|document|reminder/i)
        .first(),
    ).toBeVisible();

    await page.getByRole('button', { name: /pause/i }).click();
    await expect(page.getByRole('alert')).toContainText(/changed elsewhere|refresh|version/i);
    expect(
      fixture.requests.filter((item) => item.method === 'POST' && item.path.endsWith('/pause')),
    ).toHaveLength(1);
    await page.getByRole('button', { name: /refresh/i }).click();
    fixture.pauseConflict = false;
    await page.getByRole('button', { name: /pause/i }).click();
    await expect(page.getByText(/paused/i).first()).toBeVisible();
    await page.getByRole('button', { name: /resume/i }).click();
    await expect(page.getByText(/active/i).first()).toBeVisible();
  });

  test('shows private result content only on the occurrence route and deletes content by exact scope', async ({
    page,
  }) => {
    const fixture = await installBriefingFixture(page);
    const consoleMessages: string[] = [];
    page.on('console', (message) => consoleMessages.push(message.text()));
    await page.goto(`/briefings/${ids.schedule}/occurrences/${ids.occurrence}`);
    await expect(page.getByText('Completed', { exact: true })).toBeVisible();
    await expect(
      page.getByText('Private result content that must stay on the detail route.', { exact: true }),
    ).toBeVisible();
    expect(page.url()).not.toContain(ids.result);
    const privateState = await page.evaluate(async () => {
      const values = [
        window.location.href,
        ...Object.keys(localStorage),
        ...Object.keys(sessionStorage),
        ...Object.values(localStorage),
        ...Object.values(sessionStorage),
      ];
      const cacheNames = 'caches' in window ? await caches.keys() : [];
      const cachedUrls = await Promise.all(
        cacheNames.map(async (name) => (await caches.open(name)).keys()),
      ).then((entries) => entries.flat().map((request) => request.url));
      return { values, cachedUrls };
    });
    expect(privateState.values.join('\n')).not.toContain('Private result content');
    expect(privateState.cachedUrls.some((url) => url.includes('/api/v1/'))).toBe(false);
    expect(consoleMessages.join('\n')).not.toContain('Private result content');

    await page.getByRole('button', { name: /delete result content/i }).click();
    const resultDialog = page.getByRole('alertdialog');
    await expect(resultDialog).toContainText(/result content|cannot be reconstructed/i);
    await expect(resultDialog.getByRole('button').first()).toBeFocused();
    await resultDialog.getByRole('button', { name: /delete result content/i }).click();
    await expect(
      page.getByText('Result content deleted. It will not be reconstructed.', { exact: true }),
    ).toBeVisible();
    const deletion = request(fixture, 'POST', `/api/v1/briefing-results/${ids.result}/deletions`);
    expect(deletion?.body).toMatchObject({ scope: 'result_content', expectedVersion: 1 });
    expect(fixture.requests.some((item) => item.path.endsWith('/history-deletions'))).toBe(false);
  });

  test('keeps schedule, history, and result deletion scopes separate and requires focused confirmation', async ({
    page,
  }) => {
    const fixture = await installBriefingFixture(page);
    await page.goto(`/briefings/${ids.schedule}`);
    await page.getByRole('button', { name: /delete schedule/i }).click();
    const dialog = page.getByRole('alertdialog');
    await expect(dialog).toContainText(/future|history|result/i);
    await expect(dialog.getByRole('button').first()).toBeFocused();
    await dialog.getByRole('button', { name: /delete schedule/i }).click();
    await expect
      .poll(() => request(fixture, 'POST', `/api/v1/briefing-schedules/${ids.schedule}/deletions`))
      .toBeTruthy();
    const deletion = request(
      fixture,
      'POST',
      `/api/v1/briefing-schedules/${ids.schedule}/deletions`,
    );
    expect(deletion?.body).toMatchObject({ scope: 'schedule', expectedVersion: 1 });
    expect(deletion?.body).not.toHaveProperty('occurrenceId');
    expect(deletion?.body).not.toHaveProperty('resultId');
    expect(fixture.requests.some((item) => item.path.endsWith('/history-deletions'))).toBe(false);
  });

  test('sends history deletion only after its own explicit confirmation', async ({ page }) => {
    const fixture = await installBriefingFixture(page);
    await page.goto(`/briefings/${ids.schedule}`);
    await page.getByRole('button', { name: /delete history/i }).click();
    const dialog = page.getByRole('alertdialog');
    await expect(dialog).toContainText(/retained briefing history|future work remain/i);
    await expect(dialog.getByRole('button').first()).toBeFocused();
    await dialog.getByRole('button', { name: /delete history/i }).click();
    await expect
      .poll(() =>
        request(fixture, 'POST', `/api/v1/briefing-schedules/${ids.schedule}/history-deletions`),
      )
      .toBeTruthy();
    const deletion = request(
      fixture,
      'POST',
      `/api/v1/briefing-schedules/${ids.schedule}/history-deletions`,
    );
    expect(deletion?.body).toMatchObject({
      scope: 'occurrence_history',
      expectedScheduleVersion: 1,
    });
    expect(deletion?.body).not.toHaveProperty('expectedVersion');
    expect(
      fixture.requests.some(
        (item) => item.path.endsWith('/deletions') && !item.path.endsWith('/history-deletions'),
      ),
    ).toBe(false);
  });

  test('notification deep-link contains schedule and occurrence only, never a result identifier', async ({
    page,
  }) => {
    await installBriefingFixture(page);
    await page.goto('/notifications');
    await expect(page.getByText(/daily briefing/i).first()).toBeVisible();
    const opener = page.getByRole('button', { name: /daily briefing/i }).first();
    await opener.click();
    const detail = page.getByRole('dialog');
    await expect(detail).toBeVisible();
    const link = detail.getByRole('link', { name: /open|view.*briefing/i });
    await expect(link).toHaveAttribute('href', new RegExp(`${ids.schedule}.*${ids.occurrence}`));
    const href = await link.getAttribute('href');
    expect(href).not.toContain(ids.result);
    await link.click();
    await expect(page).toHaveURL(
      new RegExp(`/briefings/${ids.schedule}/occurrences/${ids.occurrence}$`),
    );
  });

  test('supports permission recovery, reduced motion, dark theme, keyboard focus, and 200% reflow', async ({
    page,
  }) => {
    await installBriefingFixture(page, { scheduleStatus: 403 });
    await page.emulateMedia({ colorScheme: 'dark', reducedMotion: 'reduce' });
    await page.goto('/briefings');
    await expect(page.getByRole('alert')).toContainText(/permission/i);
    await page.evaluate(() => {
      document.documentElement.style.zoom = '2';
    });
    const metrics = await page.evaluate(() => ({
      overflow: document.documentElement.scrollWidth <= document.documentElement.clientWidth,
      reduced: matchMedia('(prefers-reduced-motion: reduce)').matches,
      transition: getComputedStyle(document.body).transitionDuration,
      canvas: getComputedStyle(document.documentElement).getPropertyValue('--canvas').trim(),
      text: getComputedStyle(document.documentElement).getPropertyValue('--text').trim(),
    }));
    expect(metrics.overflow).toBe(true);
    expect(metrics.reduced).toBe(true);
    expect(Number.parseFloat(metrics.transition.replace('s', ''))).toBeLessThan(0.001);
    expect(metrics.canvas).not.toBe('');
    expect(metrics.text).not.toBe('');
    const results = await new AxeBuilder({ page }).include('main').analyze();
    expect(results.violations).toEqual([]);
  });
});
