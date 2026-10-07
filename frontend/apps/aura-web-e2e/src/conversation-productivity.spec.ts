import { expect, test, type Page, type Route } from '@playwright/test';

const now = '2026-10-06T12:00:00.000Z';
const later = '2026-10-06T13:00:00.000Z';
const traceReference = '0123456789abcdef0123456789abcdef';

type RunStatus = 'queued' | 'running' | 'completed' | 'failed' | 'canceled' | 'interrupted';

interface FixtureRun {
  readonly id: string;
  readonly conversationId: string;
  readonly status: RunStatus;
  readonly createdAt: string;
  readonly startedAt: string | null;
  readonly finishedAt: string | null;
  readonly provider: string;
  readonly modelId: string;
  readonly agentRevisionId: string;
  readonly modelPolicyRevisionId: string;
  readonly retryOfRunId: string | null;
  readonly error: { readonly code: string; readonly message: string; readonly retryable: boolean; readonly traceId: string } | null;
}

interface FixtureConversation {
  readonly id: string;
  readonly title: string;
  readonly archivedAt: string | null;
  readonly currentRun: FixtureRun | null;
  readonly recentRuns: ReadonlyArray<FixtureRun>;
  readonly version: number;
  readonly agentProfileId: string;
  readonly agentRevisionId: string;
  readonly modelId: string;
  readonly createdAt: string;
  readonly updatedAt: string;
}

interface FixtureOptions {
  readonly conversations?: ReadonlyArray<FixtureConversation>;
  readonly pageSize?: number;
}

interface FixtureState {
  readonly listRequests: URL[];
  readonly detailRequests: string[];
  readonly metadataRequests: Array<{ readonly method: string; readonly body: string }>;
  readonly runRequests: Array<{ readonly method: string; readonly url: string }>;
}

function run(id: string, conversationId: string, status: RunStatus, overrides: Partial<FixtureRun> = {}): FixtureRun {
  return {
    id,
    conversationId,
    status,
    createdAt: now,
    startedAt: status === 'queued' ? null : now,
    finishedAt: ['completed', 'failed', 'canceled', 'interrupted'].includes(status) ? later : null,
    provider: 'ollama',
    modelId: 'chat',
    agentRevisionId: 'agent-researcher-r1',
    modelPolicyRevisionId: 'policy-1',
    retryOfRunId: null,
    error: status === 'failed' ? { code: 'MODEL_TIMEOUT', message: 'The provider timed out.', retryable: true, traceId: traceReference } : null,
    ...overrides,
  };
}

function conversation(id: string, title: string, overrides: Partial<FixtureConversation> = {}): FixtureConversation {
  return {
    id,
    title,
    archivedAt: null,
    currentRun: null,
    recentRuns: [],
    version: 1,
    agentProfileId: 'agent-researcher',
    agentRevisionId: 'agent-researcher-r1',
    modelId: 'chat',
    createdAt: now,
    updatedAt: now,
    ...overrides,
  };
}

function summary(item: FixtureConversation): Record<string, unknown> {
  return {
    id: item.id,
    title: item.title,
    archivedAt: item.archivedAt,
    agentProfileId: item.agentProfileId,
    agentRevisionId: item.agentRevisionId,
    modelId: item.modelId,
    version: item.version,
    createdAt: item.createdAt,
    updatedAt: item.updatedAt,
    currentRun: item.currentRun,
  };
}

function detail(item: FixtureConversation): Record<string, unknown> {
  return {
    ...summary(item),
    messages: [],
    recentRuns: item.recentRuns,
    agentAssignments: [],
  };
}

async function installFixture(page: Page, options: FixtureOptions = {}): Promise<FixtureState> {
  const firstPage = options.conversations ?? [
    conversation('conversation-alpha', 'Alpha planning'),
    conversation('conversation-beta', 'Beta notes'),
    conversation('conversation-gamma', 'Gamma review'),
  ];
  const pageSize = options.pageSize ?? 30;
  const listRequests: URL[] = [];
  const detailRequests: string[] = [];
  const metadataRequests: Array<{ readonly method: string; readonly body: string }> = [];
  const runRequests: Array<{ readonly method: string; readonly url: string }> = [];
  const current = new Map(firstPage.map((item) => [item.id, item]));

  await page.route('**/api/**', async (route: Route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.pathname === '/api/v1/auth/session') {
      await route.fulfill({ json: { principal: { issuer: 'https://issuer', subject: 'owner', displayName: 'Owner' }, csrfToken: 'csrf', idleExpiresAt: later, absoluteExpiresAt: later } });
      return;
    }
    if (url.pathname === '/api/v1/models') {
      await route.fulfill({ json: { models: [{ id: 'chat', displayName: 'Chat', provider: 'ollama', capabilities: ['chat'], availability: 'available', selectable: true, disabledReason: null }], defaultModelId: 'chat', observedAt: now } });
      return;
    }
    if (url.pathname === '/api/v1/agents' && request.method() === 'GET') {
      await route.fulfill({ json: { items: [{ id: 'agent-researcher', status: 'active', version: 1, currentRevision: { id: 'agent-researcher-r1', profileId: 'agent-researcher', revision: 1, displayName: 'Researcher', purpose: 'Research', instructions: 'Research', personaRevisionId: null, promptBundleRevisionId: 'bundle-1', modelPolicyRevisionId: 'policy-1', createdAt: now }, revisions: [] }] } });
      return;
    }
    if (url.pathname === '/api/v1/agents/agent-researcher' && request.method() === 'GET') {
      const revision = { id: 'agent-researcher-r1', profileId: 'agent-researcher', revision: 1, displayName: 'Researcher', purpose: 'Research', instructions: 'Research', personaRevisionId: null, promptBundleRevisionId: 'bundle-1', modelPolicyRevisionId: 'policy-1', createdAt: now };
      await route.fulfill({ json: { id: 'agent-researcher', status: 'active', version: 1, displayName: 'Researcher', createdAt: now, updatedAt: now, currentRevision: revision, revisions: [revision] } });
      return;
    }
    if (url.pathname === '/api/v1/personas' && request.method() === 'GET') {
      await route.fulfill({ json: { items: [] } });
      return;
    }
    if (url.pathname === '/api/v1/conversations' && request.method() === 'GET') {
      listRequests.push(url);
      const query = (url.searchParams.get('q') ?? '').toLocaleLowerCase();
      const filtered = firstPage.filter((item) => item.title.toLocaleLowerCase().includes(query));
      const offset = url.searchParams.get('cursor') ? pageSize : 0;
      const items = filtered.slice(offset, offset + pageSize);
      await route.fulfill({ json: { items: items.map(summary), nextCursor: offset === 0 && filtered.length > pageSize ? 'next-page' : null } });
      return;
    }
    const conversationMatch = url.pathname.match(/^\/api\/v1\/conversations\/([^/]+)$/);
    if (conversationMatch && request.method() === 'GET') {
      detailRequests.push(conversationMatch[1]);
      const item = current.get(conversationMatch[1]);
      if (!item) { await route.fulfill({ status: 404, json: { detail: 'Not found' } }); return; }
      await route.fulfill({ json: detail(item) });
      return;
    }
    const metadataMatch = url.pathname.match(/^\/api\/v1\/conversations\/([^/]+)\/metadata$/);
    if (metadataMatch && request.method() === 'PATCH') {
      const body = request.postData() ?? '';
      metadataRequests.push({ method: request.method(), body });
      const previous = current.get(metadataMatch[1]);
      if (!previous) { await route.fulfill({ status: 404, json: { detail: 'Not found' } }); return; }
      const patch = JSON.parse(body) as { title?: string; archived?: boolean };
      const next = { ...previous, title: patch.title ?? previous.title, archivedAt: patch.archived === false ? null : patch.archived ? later : previous.archivedAt, version: previous.version + 1 };
      current.set(next.id, next);
      await route.fulfill({ json: summary(next) });
      return;
    }
    const eventsMatch = url.pathname.match(/^\/api\/v1\/runs\/([^/]+)\/events$/);
    if (eventsMatch) {
      const runId = eventsMatch[1];
      if (runId === 'run-reconcile') {
        await new Promise<void>((resolve) => setTimeout(resolve, 500));
        const previous = current.get('conversation-reconcile');
        const reconciled = run('run-reconcile', 'conversation-reconcile', 'failed', { error: { code: 'MODEL_TIMEOUT', message: 'timeout', retryable: true, traceId: 'trace-reconciled' } });
        if (previous) current.set('conversation-reconcile', { ...previous, currentRun: null, recentRuns: [reconciled] });
        await route.fulfill({
          status: 200,
          headers: { 'content-type': 'text/event-stream' },
          body: `id: reconcile-1\nevent: run.status\ndata: ${JSON.stringify({ schemaVersion: 1, eventId: 'reconcile-event', sequence: 2, eventType: 'run.status', runId, conversationId: 'conversation-reconcile', occurredAt: later, data: { status: 'failed', startedAt: now, finishedAt: later, error: { code: 'MODEL_TIMEOUT', message: 'timeout', retryable: true, traceId: 'trace-reconciled' } } })}\n\n`,
        });
      } else {
        await route.fulfill({ status: 200, headers: { 'content-type': 'text/event-stream' }, body: '' });
      }
      return;
    }
    const runMatch = url.pathname.match(/^\/api\/v1\/conversations\/([^/]+)\/runs(?:\/([^/]+))?$/);
    if (runMatch && request.method() !== 'GET') {
      runRequests.push({ method: request.method(), url: request.url() });
      await route.fulfill({ status: 200, json: {} });
      return;
    }
    const runActionMatch = url.pathname.match(/^\/api\/v1\/runs\/([^/]+)\/(?:retry|cancel)$/);
    if (runActionMatch && request.method() !== 'GET') {
      runRequests.push({ method: request.method(), url: request.url() });
      await route.fulfill({ status: 200, json: {} });
      return;
    }
    await route.continue();
  });
  return { listRequests, detailRequests, metadataRequests, runRequests };
}

async function waitForLibrary(page: Page): Promise<void> {
  await page.goto('/conversation');
  await expect(page.getByRole('status', { name: /loading your conversation/i })).toBeHidden();
  await expect(page.getByRole('navigation', { name: /primary navigation/i })).toBeVisible();
}

test.describe('conversation productivity', () => {
  test('requests 30-item pages and only loads the next page after Load more', async ({ page }) => {
    const conversations = Array.from({ length: 31 }, (_, index) => conversation(`conversation-${index}`, `Conversation ${index}`));
    const fixture = await installFixture(page, { conversations });
    await waitForLibrary(page);

    await expect.poll(() => fixture.listRequests.length).toBe(1);
    expect(fixture.listRequests[0].searchParams.get('limit')).toBe('30');
    await expect(page.getByRole('button', { name: /load more/i })).toBeVisible();
    expect(await page.getByRole('link', { name: /conversation \d+/i }).count()).toBe(30);

    await page.getByRole('button', { name: /load more/i }).click();
    await expect.poll(() => fixture.listRequests.length).toBe(2);
    expect(fixture.listRequests[1].searchParams.get('cursor')).toBeTruthy();
    await expect(page.getByRole('link', { name: 'Conversation 30' })).toBeVisible();
  });

  test('debounces title search, opens a connected live filter popup, and keeps terms out of the URL', async ({ page }) => {
    const fixture = await installFixture(page);
    await waitForLibrary(page);
    const search = page.getByRole('searchbox', { name: /search conversation titles/i });
    await expect(search).toHaveAccessibleName('Search conversation titles');
    await expect(page.locator('.library-search .search-icon[aria-hidden="true"] svg')).toHaveCount(1);
    await search.fill('Alpha');
    expect(fixture.listRequests).toHaveLength(1);
    await expect.poll(() => fixture.listRequests.length, { timeout: 2_000 }).toBe(2);
    const filteredRequest = fixture.listRequests[1];
    expect(filteredRequest.searchParams.get('q')).toBe('Alpha');
    expect(page.url()).not.toContain('Alpha');

    const filterTrigger = page.getByRole('button', { name: 'Filter', exact: true });
    const navigation = page.getByRole('navigation', { name: /primary navigation/i });
    const navigationBefore = await navigation.boundingBox();
    await filterTrigger.click();
    const filterPanel = page.locator('#conversation-filters');
    await expect(filterPanel).toBeVisible();
    await expect(filterPanel).toHaveAttribute('role', 'dialog');
    await expect(filterTrigger).toHaveAttribute('aria-haspopup', 'dialog');
    await expect(filterTrigger).toHaveAttribute('aria-expanded', 'true');
    const navigationAfter = await navigation.boundingBox();
    expect(navigationBefore).not.toBeNull();
    expect(navigationAfter).not.toBeNull();
    expect(navigationAfter?.height).toBe(navigationBefore?.height);

    const filterSelects = filterPanel.locator('select');
    await filterSelects.nth(0).selectOption('agent-researcher');
    await expect.poll(() => fixture.listRequests.at(-1)?.searchParams.get('agentProfileId')).toBe('agent-researcher');
    await expect(filterSelects.nth(2).locator('option[value="cancel_requested"]')).toHaveCount(1);
    await filterSelects.nth(3).selectOption('archived');
    await expect.poll(() => fixture.listRequests.at(-1)?.searchParams.get('archiveState')).toBe('archived');

    const dates = page.locator('#conversation-filters input[type="date"]');
    await dates.nth(0).fill('2026-10-01');
    await dates.nth(1).fill('2026-10-31');
    await expect(dates.nth(0)).toHaveValue('2026-10-01');
    await expect(dates.nth(1)).toHaveValue('2026-10-31');
    await expect.poll(() => fixture.listRequests.at(-1)?.searchParams.get('activityFrom')).toMatch(/^2026-10-01T00:00:00(?:\.000)?Z$/);
    await expect.poll(() => fixture.listRequests.at(-1)?.searchParams.get('activityTo')).toMatch(/^2026-11-01T00:00:00(?:\.000)?Z$/);

    await page.getByRole('button', { name: /clear filters/i }).click();
    await expect(filterPanel).toBeVisible();
    await expect(search).toHaveValue('');
    await expect(filterSelects.nth(0)).toHaveValue('');
    await expect(filterSelects.nth(1)).toHaveValue('');
    await expect(filterSelects.nth(2)).toHaveValue('');
    await expect(filterSelects.nth(3)).toHaveValue('active');
    await expect(dates.nth(0)).toHaveValue('');
    await expect(dates.nth(1)).toHaveValue('');

    await page.keyboard.press('Escape');
    await expect(filterPanel).toBeHidden();
    await expect(filterTrigger).toHaveAttribute('aria-expanded', 'false');
    await expect(filterTrigger).toBeFocused();

    await filterTrigger.click();
    await expect(filterPanel).toBeVisible();
    await page.getByRole('heading', { name: /new conversation/i }).click();
    await expect(filterPanel).toBeHidden();
    await expect(filterTrigger).toBeFocused();
  });

  test('closes the filter popup when the mobile navigation closes', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await installFixture(page);
    await page.goto('/conversation');
    const openNavigation = page.getByRole('button', { name: /open navigation/i });
    await openNavigation.click();
    const navigation = page.getByRole('navigation', { name: /primary navigation/i });
    await expect(navigation).toBeVisible();

    const filterTrigger = page.getByRole('button', { name: 'Filter', exact: true });
    await filterTrigger.click();
    const filterPanel = page.locator('#conversation-filters');
    await expect(filterPanel).toBeVisible();
    await navigation.getByRole('button', { name: 'Close navigation' }).click();
    await expect(navigation).toBeHidden();
    await expect(filterPanel).toBeHidden();
    await expect(openNavigation).toBeFocused();
  });

  test('preserves selection and draft through filtering and rejected metadata mutation', async ({ page }) => {
    const fixture = await installFixture(page);
    await waitForLibrary(page);
    const composer = page.getByRole('textbox', { name: /message aura/i });
    await composer.fill('Keep this private draft');
    await page.getByRole('link', { name: /beta notes/i }).click();
    await expect(page).toHaveURL(/conversation\/conversation-beta/);
    await expect(page.getByRole('link', { name: /beta notes/i })).toHaveAttribute('aria-current', 'page');
    await expect(page.getByRole('heading', { name: 'Beta notes' })).toBeVisible();
    await composer.fill('Keep the selected draft too');

    const betaRow = page.getByRole('link', { name: /beta notes/i });
    const actionButton = betaRow.locator('..').getByRole('button', { name: /conversation actions|more actions/i });
    await actionButton.click();
    await page.getByRole('menuitem', { name: /rename/i }).click();
    const rename = page.getByRole('textbox', { name: /conversation title/i });
    await rename.fill('Rejected rename');
    await page.route('**/api/v1/conversations/conversation-beta/metadata', async (route) => route.fulfill({ status: 409, json: { detail: 'stale version' } }));
    await page.getByRole('button', { name: 'Save conversation name' }).click();
    await expect(page.locator('#composer-notice')).toContainText(/changed|stale|try again/i);
    await expect(composer).toHaveValue('Keep the selected draft too');
    expect(fixture.metadataRequests).toHaveLength(0);
  });

  test('keeps an archived route read-only until Restore succeeds', async ({ page }) => {
    const archivedRun = run('run-archived-failed', 'conversation-archived', 'failed');
    await installFixture(page, { conversations: [conversation('conversation-archived', 'Archived research', { archivedAt: later, recentRuns: [archivedRun] })] });
    await page.goto('/conversation/conversation-archived');
    await expect(page.getByRole('status', { name: /read.only|archived/i })).toBeVisible();
    await expect(page.getByRole('button', { name: /restore/i })).toBeVisible();
    await expect(page.getByRole('textbox', { name: /message aura/i })).toBeDisabled();
    await expect(page.getByRole('button', { name: /retry/i })).toHaveCount(0);

    const restoreRequest = page.waitForRequest('**/api/v1/conversations/conversation-archived/metadata');
    await page.getByRole('button', { name: /restore/i }).click();
    await expect.poll(() => page.getByRole('button', { name: /restore/i }).count()).toBe(0);
    expect(JSON.parse((await restoreRequest).postData() ?? '{}')).toMatchObject({ archived: false });
    await expect(page.getByRole('textbox', { name: /message aura/i })).toBeEnabled();
  });

  test('opens run details with safe metadata and exposes only eligible actions', async ({ page }) => {
    const activeRun = run('run-active', 'conversation-runs', 'running');
    const failedRun = run('run-failed', 'conversation-runs', 'failed', { retryOfRunId: 'run-old' });
    await installFixture(page, { conversations: [conversation('conversation-runs', 'Run inspector', { currentRun: activeRun, recentRuns: [activeRun, failedRun] })] });
    await page.goto('/conversation/conversation-runs');
    await expect(page.getByRole('button', { name: /run details|runs/i })).toBeVisible();
    const opener = page.getByRole('button', { name: /run details|runs/i });
    await opener.click();
    const drawer = page.getByRole('dialog', { name: /recent runs/i });
    await expect(drawer).toBeVisible();
    await expect(drawer.getByText('MODEL_TIMEOUT')).toBeVisible();
    await expect(drawer.getByText(traceReference)).toBeVisible();
    await expect(drawer).not.toContainText(/prompt|hidden reasoning|stack trace|lease|worker attempt/i);
    await expect(drawer.getByRole('button', { name: /cancel/i })).toBeVisible();
    await expect(drawer.getByRole('button', { name: /retry/i })).toBeVisible();

    await page.keyboard.press('Tab');
    await expect.poll(async () => page.evaluate(() => document.activeElement?.closest('[role="dialog"]') !== null)).toBeTruthy();
    await page.keyboard.press('Escape');
    await expect(drawer).toBeHidden();
    await expect(opener).toBeFocused();
  });

  test('inspects a nonselected row, loads historical runs, and restores row-menu focus', async ({ page }) => {
    const olderRun = run('run-beta-completed', 'conversation-beta', 'completed', {
      createdAt: '2026-10-05T10:00:00.000Z',
      startedAt: '2026-10-05T10:01:00.000Z',
      finishedAt: '2026-10-05T10:02:00.000Z',
    });
    const newestRun = run('run-beta-failed', 'conversation-beta', 'failed', {
      createdAt: '2026-10-06T11:00:00.000Z',
      startedAt: '2026-10-06T11:01:00.000Z',
      finishedAt: '2026-10-06T11:02:00.000Z',
    });
    const fixture = await installFixture(page, {
      conversations: [
        conversation('conversation-alpha', 'Alpha planning'),
        conversation('conversation-beta', 'Beta notes', { recentRuns: [newestRun, olderRun] }),
      ],
    });
    await waitForLibrary(page);

    const inspectTrigger = page.getByRole('button', { name: 'Conversation actions for Beta notes' });
    await inspectTrigger.click();
    await page.getByRole('menuitem', { name: 'Inspect runs' }).click();

    await expect(page).toHaveURL(/conversation\/conversation-beta/);
    await expect.poll(() => fixture.detailRequests.filter((id) => id === 'conversation-beta').length).toBeGreaterThan(0);
    const drawer = page.getByRole('dialog', { name: /recent runs/i });
    await expect(drawer).toBeVisible();
    await expect(drawer.getByText('failed', { exact: true })).toBeVisible();
    await expect(drawer.getByText('completed', { exact: true })).toBeVisible();
    await drawer.getByRole('button', { name: 'Close run details' }).click();
    await expect(drawer).toBeHidden();
    await expect(inspectTrigger).toBeFocused();
  });

  test('reconciles an active run snapshot and keeps cancel/retry eligibility correct', async ({ page }) => {
    const activeRun = run('run-reconcile', 'conversation-reconcile', 'running');
    const fixture = await installFixture(page, { conversations: [conversation('conversation-reconcile', 'Reconciliation', { currentRun: activeRun, recentRuns: [activeRun] })] });
    await page.goto('/conversation/conversation-reconcile');
    await page.getByRole('button', { name: /run details|runs/i }).click();
    const drawer = page.getByRole('dialog', { name: /recent runs/i });
    await expect(drawer.getByRole('button', { name: /cancel/i })).toBeVisible();
    await expect(drawer.getByRole('button', { name: /retry/i })).toHaveCount(0);

    await expect(drawer.getByRole('button', { name: /cancel/i })).toHaveCount(0);
    await expect(drawer.getByRole('button', { name: /retry/i })).toBeVisible();
    await drawer.getByRole('button', { name: /retry/i }).click();
    await expect.poll(() => fixture.runRequests.some((request) => request.method === 'POST' && /retry/.test(request.url))).toBeTruthy();
  });

  test('traps focus and restores it when the mobile navigation closes', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await installFixture(page);
    await page.goto('/conversation');
    const trigger = page.getByRole('button', { name: /open navigation/i });
    await trigger.click();
    const navigation = page.getByRole('navigation', { name: /primary navigation/i });
    await expect(navigation).toBeVisible();
    await page.keyboard.press('Tab');
    await expect.poll(async () => page.evaluate(() => document.activeElement?.closest('#primary-navigation') !== null)).toBeTruthy();
    await page.keyboard.press('Escape');
    await expect(navigation).toBeHidden();
    await expect(trigger).toBeFocused();
  });

  test('keeps the expanded navigation available at tablet width', async ({ page }) => {
    await page.setViewportSize({ width: 768, height: 1024 });
    await installFixture(page);
    await page.goto('/conversation');
    await expect(page.getByRole('navigation', { name: /primary navigation/i })).toBeVisible();
    await expect(page.getByRole('button', { name: /open navigation/i })).toHaveCount(0);
  });
});
