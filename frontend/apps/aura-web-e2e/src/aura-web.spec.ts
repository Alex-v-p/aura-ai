import { expect, test, type Locator } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';

async function expectExpandedDestinationTiles(productNav: Locator): Promise<void> {
  const metrics = await productNav.locator('a').evaluateAll((links) => {
    const grid = links[0]?.parentElement;
    const gridStyle = grid ? getComputedStyle(grid) : null;
    return {
      gridColumns: gridStyle?.gridTemplateColumns.split(' ').filter(Boolean).length ?? 0,
      tiles: links.map((link) => {
        const label = link.querySelector<HTMLElement>('.label');
        const linkRect = link.getBoundingClientRect();
        const labelRect = label?.getBoundingClientRect();
        return {
          left: linkRect.left,
          top: linkRect.top,
          width: linkRect.width,
          right: linkRect.right,
          labelText: label?.textContent?.trim() ?? '',
          labelWidth: labelRect?.width ?? 0,
          labelRight: labelRect?.right ?? 0,
          labelClientWidth: label?.clientWidth ?? 0,
          labelScrollWidth: label?.scrollWidth ?? 0,
        };
      }),
    };
  });

  expect(metrics.gridColumns).toBe(3);
  expect(metrics.tiles).toHaveLength(3);
  expect(metrics.tiles.map((tile) => tile.labelText)).toEqual(['Conversations', 'Agents', 'Personas']);
  expect(Math.max(...metrics.tiles.map((tile) => tile.top)) - Math.min(...metrics.tiles.map((tile) => tile.top))).toBeLessThanOrEqual(1);
  expect(Math.max(...metrics.tiles.map((tile) => tile.width)) - Math.min(...metrics.tiles.map((tile) => tile.width))).toBeLessThanOrEqual(1);
  expect(metrics.tiles[0]?.left).toBeLessThan(metrics.tiles[1]?.left ?? 0);
  expect(metrics.tiles[1]?.left).toBeLessThan(metrics.tiles[2]?.left ?? 0);
  for (const tile of metrics.tiles) {
    expect(tile.width).toBeGreaterThan(0);
    expect(tile.labelWidth).toBeGreaterThan(0);
    expect(tile.labelClientWidth).toBeGreaterThanOrEqual(tile.labelScrollWidth);
    expect(tile.labelRight).toBeLessThanOrEqual(tile.right + 1);
  }
}

async function installApiFake(page: import('@playwright/test').Page): Promise<void> {
  let errorOnce = false;
  let failNextStream = false;
  page.on('framenavigated', (frame) => { if (frame === page.mainFrame() && frame.url().includes('fixture=error')) errorOnce = true; });
  let runNumber = 0;
  let version = 1;
  let selectedModel = 'qwen2.5:7b';
  let messages: Array<Record<string, unknown>> = [];
  let activeRun: Record<string, unknown> | null = null;
  const now = (): string => new Date().toISOString();
  const summary = (): Record<string, unknown> => ({ id: 'welcome', title: 'A thoughtful beginning', agentProfileId: 'general', agentRevisionId: 'agent-rev-1', modelId: selectedModel, version, createdAt: now(), updatedAt: now(), currentRun: activeRun });
  await page.route('**/api/**', async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.pathname === '/api/v1/auth/session') return route.fulfill({ json: { principal: { issuer: 'https://authentik.test', subject: 'owner', displayName: 'Owner' }, csrfToken: 'csrf', idleExpiresAt: now(), absoluteExpiresAt: now() } });
    if (url.pathname === '/api/v1/models') return route.fulfill({ json: { models: [{ id: 'qwen2.5:7b', displayName: 'Qwen 2.5', provider: 'ollama', capabilities: ['chat'], availability: 'available', selectable: true, disabledReason: null }, { id: 'llama3.2:3b', displayName: 'Llama 3.2', provider: 'ollama', capabilities: ['chat'], availability: 'available', selectable: true, disabledReason: null }, { id: 'embed-only', displayName: 'Embeddings only', provider: 'ollama', capabilities: ['embedding'], availability: 'available', selectable: false, disabledReason: 'This model does not support chat.' }], defaultModelId: 'qwen2.5:7b', observedAt: now() } });
    if (url.pathname === '/api/v1/conversations' && request.method() === 'GET') return route.fulfill({ json: { items: [summary()], nextCursor: null } });
    if (url.pathname === '/api/v1/conversations/welcome' && request.method() === 'PATCH') {
      const body = JSON.parse(request.postData() ?? '{}') as { modelId?: string; version?: number };
      if (body.modelId) selectedModel = body.modelId;
      version = (body.version ?? version) + 1;
      return route.fulfill({ json: summary() });
    }
    if (url.pathname === '/api/v1/conversations/welcome' && request.method() === 'GET') return route.fulfill({ json: { ...summary(), messages, recentRuns: activeRun ? [activeRun] : [] } });
    if (url.pathname.endsWith('/runs') && request.method() === 'POST') {
      // Use the current document URL as a second source so the fixture does
      // not depend on the relative ordering of `framenavigated` and the
      // application's first API request.
      if (errorOnce || page.url().includes('fixture=error')) { errorOnce = false; failNextStream = true; }
      const body = JSON.parse(request.postData() ?? '{}') as { message?: string };
      const runId = `run-${++runNumber}`;
      const userMessage = { id: `user-${runId}`, conversationId: 'welcome', role: 'user', content: body.message ?? '', state: 'complete', runId, createdAt: now(), updatedAt: now() };
      activeRun = { id: runId, conversationId: 'welcome', userMessageId: userMessage.id, assistantMessageId: `assistant-${runId}`, status: 'running', agentRevisionId: 'agent-rev-1', modelPolicyRevisionId: 'policy-1', provider: 'ollama', modelId: 'qwen2.5:7b', retryOfRunId: null, createdAt: now(), startedAt: now(), finishedAt: null, error: null };
      messages = [...messages, userMessage]; version += 1;
      return route.fulfill({ status: 202, json: { conversation: summary(), userMessage, run: activeRun } });
    }
    if (url.pathname.endsWith('/cancel') && request.method() === 'POST') { activeRun = activeRun ? { ...activeRun, status: 'canceled', finishedAt: now() } : null; return route.fulfill({ json: activeRun }); }
    if (url.pathname.endsWith('/retry') && request.method() === 'POST') {
      const runId = `run-${++runNumber}`; activeRun = { ...(activeRun ?? {}), id: runId, status: 'running', createdAt: now(), startedAt: now(), finishedAt: null, retryOfRunId: activeRun?.['id'] ?? null }; return route.fulfill({ status: 202, json: { conversation: summary(), userMessage: messages.at(-1), run: activeRun } });
    }
    if (url.pathname.includes('/events')) {
      const run = activeRun; const lastUser = messages.filter((message) => message['role'] === 'user').at(-1)?.['content'] ?? 'your thought';
      if (String(lastUser).includes('small local thought') && activeRun?.['status'] !== 'canceled') {
        const deadline = Date.now() + 5_000;
        while (activeRun?.['status'] !== 'canceled' && Date.now() < deadline) {
          await new Promise((resolve) => setTimeout(resolve, 50));
        }
      }
      if (activeRun?.['status'] === 'canceled') {
        const partial = { id: String(run?.['assistantMessageId'] ?? 'assistant-run'), conversationId: 'welcome', role: 'assistant', content: 'A partial response from the provider.', state: 'interrupted', runId: activeRun['id'], createdAt: now(), updatedAt: now() };
        const canceledEvent = [
          `id: 1\nevent: assistant.snapshot\ndata: ${JSON.stringify({ schemaVersion: 1, eventId: 'event-partial', sequence: 1, eventType: 'assistant.snapshot', runId: activeRun['id'], conversationId: 'welcome', occurredAt: now(), data: { message: partial } })}\n\n`,
          `id: 2\nevent: run.status\ndata: ${JSON.stringify({ schemaVersion: 1, eventId: 'event-canceled', sequence: 2, eventType: 'run.status', runId: activeRun['id'], conversationId: 'welcome', occurredAt: now(), data: { status: 'canceled', startedAt: activeRun['startedAt'], finishedAt: activeRun['finishedAt'] } })}\n\n`,
        ].join('');
        return route.fulfill({ status: 200, headers: { 'content-type': 'text/event-stream' }, body: canceledEvent });
      }
      const assistant = { id: String(run?.['assistantMessageId'] ?? 'assistant-run'), conversationId: 'welcome', role: 'assistant', content: `A server response with your thought: ${lastUser}`, state: 'complete', runId: run?.['id'] ?? 'run', createdAt: now(), updatedAt: now() };
      if (failNextStream) {
        failNextStream = false; activeRun = run ? { ...run, status: 'failed', finishedAt: now(), error: { code: 'provider_unavailable', message: 'Aura could not reach the selected provider.', retryable: true, traceId: 'trace-test' } } : null;
        const errorEvent = `id: 1\nevent: run.error\ndata: ${JSON.stringify({ schemaVersion: 1, eventId: 'event-error', sequence: 1, eventType: 'run.error', runId: run?.['id'] ?? 'run', conversationId: 'welcome', occurredAt: now(), data: activeRun?.['error'] })}\n\n`;
        return route.fulfill({ status: 200, headers: { 'content-type': 'text/event-stream' }, body: errorEvent });
      }
      messages = [...messages, assistant]; activeRun = run ? { ...run, status: 'completed', finishedAt: now() } : null;
      const events = [
        `id: 1\nevent: assistant.snapshot\ndata: ${JSON.stringify({ schemaVersion: 1, eventId: 'event-1', sequence: 1, eventType: 'assistant.snapshot', runId: run?.['id'] ?? 'run', conversationId: 'welcome', occurredAt: now(), data: { message: assistant } })}\n\n`,
        `id: 2\nevent: run.status\ndata: ${JSON.stringify({ schemaVersion: 1, eventId: 'event-2', sequence: 2, eventType: 'run.status', runId: run?.['id'] ?? 'run', conversationId: 'welcome', occurredAt: now(), data: { status: 'completed', startedAt: run?.['startedAt'] ?? now(), finishedAt: now() } })}\n\n`,
      ].join('');
      return route.fulfill({ status: 200, headers: { 'content-type': 'text/event-stream' }, body: events });
    }
    return route.continue();
  });
}

test.beforeEach(async ({ page }) => { await installApiFake(page); });

test('opens with a welcome-first conversation surface', async ({ page }) => {
  await page.goto('/');
  await expect(page.getByRole('heading', { name: 'A calm space to think.' })).toBeVisible();
  await expect(page.getByRole('textbox', { name: 'Message Aura' })).toBeVisible();
});

test('supports Enter and Shift+Enter composer behavior', async ({ page }) => {
  await page.goto('/');
  const composer = page.getByRole('textbox', { name: 'Message Aura' });
  await composer.fill('A first line');
  await composer.press('Shift+Enter');
  await expect(composer).toHaveValue('A first line\n');
  await composer.fill('Send with Enter');
  await composer.press('Enter');
  await expect(page.getByRole('article', { name: 'Your message' })).toContainText('Send with Enter');
});

test('persists the selected theme preference', async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== 'desktop', 'Theme persistence is covered once in the desktop project.');
  await page.goto('/');
  await page.getByRole('radio', { name: 'Dark' }).check();
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
  await page.reload();
  await expect(page.getByRole('radio', { name: 'Dark' })).toBeChecked();
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
});

test('supports desktop collapse and tablet rail navigation', async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== 'desktop' && testInfo.project.name !== 'tablet', 'Navigation geometry is covered by desktop and tablet projects.');
  const navigation = page.locator('#primary-navigation');
  if (testInfo.project.name === 'desktop') {
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto('/');
    await navigation.getByRole('button', { name: 'Collapse navigation' }).click();
    await expect(navigation).toHaveClass(/is-collapsed/);
    await expect(navigation).toHaveCSS('flex-basis', '78px');
    await page.waitForTimeout(220);
    await expect(navigation.getByRole('button', { name: 'Expand navigation' })).toBeVisible();
    await expect(navigation.locator('.wordmark-mark')).toBeVisible();
    await expect(navigation.locator('.product-nav')).toBeVisible();
    await expect(navigation.locator('.product-nav a')).toHaveCount(3);
    await expect(navigation.locator('.conversation-nav')).toHaveCount(0);
    await expect(navigation.locator('aura-theme-select')).toHaveCount(0);
    await expect(navigation.getByRole('button', { name: 'A thoughtful beginning' })).toHaveCount(0);
    await expect(navigation.locator('button.new-conversation')).toHaveCSS('width', '44px');
    const compactAlignment = await navigation.evaluate((element) => {
      const rail = element.getBoundingClientRect();
      const borderRight = Number.parseFloat(getComputedStyle(element).borderRightWidth);
      const axis = rail.left + (rail.width - borderRight) / 2;
      const center = (selector: string): number => {
        const rect = element.querySelector<HTMLElement>(selector)?.getBoundingClientRect();
        return rect ? rect.left + rect.width / 2 : Number.NaN;
      };
      return {
        axis,
        width: rail.width,
        borderRight,
        mark: center('.wordmark-mark'),
        expand: center('.collapse-button span'),
        plus: center('.new-conversation > span[aria-hidden="true"]'),
      };
    });
    expect(compactAlignment.width).toBeGreaterThanOrEqual(78);
    expect(compactAlignment.borderRight).toBe(1);
    expect(Math.abs(compactAlignment.mark - compactAlignment.axis)).toBeLessThanOrEqual(1);
    expect(Math.abs(compactAlignment.expand - compactAlignment.axis)).toBeLessThanOrEqual(1);
    expect(Math.abs(compactAlignment.plus - compactAlignment.axis)).toBeLessThanOrEqual(1);
    await navigation.getByRole('button', { name: 'Expand navigation' }).click();
    await page.waitForTimeout(60);
    const expansionWidth = await navigation.evaluate((element) => element.getBoundingClientRect().width);
    expect(expansionWidth).toBeLessThan(272);
    await expect(navigation.locator('.conversation-nav')).toHaveCount(0);
    await expect(navigation.locator('aura-theme-select')).toHaveCount(0);
    await expect(navigation).not.toHaveClass(/is-collapsed/);
    await expect(navigation).toHaveCSS('flex-basis', '272px');
    await expect(navigation.locator('.conversation-nav')).toBeVisible();
    await expect(navigation.getByRole('button', { name: 'A thoughtful beginning' })).toBeVisible();
    await expect(navigation.getByRole('radio', { name: 'Dark' })).toBeVisible();
    await expect(navigation.locator('.wordmark-text')).toBeVisible();
    await expect(navigation.locator('button.new-conversation').locator('.label')).toBeVisible();
    await expect(navigation.locator('.wordmark')).toHaveCSS('gap', '8.8px');
    await expect(navigation.locator('button.new-conversation')).toHaveCSS('gap', '8px');
    return;
  }
  await page.setViewportSize({ width: 1024, height: 768 });
  await page.goto('/');
  await expect(navigation).toHaveCSS('flex-basis', '78px');
  await expect(navigation.locator('.product-nav')).toBeVisible();
  await expect(navigation.locator('.product-nav a')).toHaveCount(3);
  await expect(navigation.locator('.product-nav').getByRole('link', { name: /^Conversations$/i })).toBeVisible();
  await expect(navigation.locator('.product-nav').getByRole('link', { name: /^Agents$/i })).toBeVisible();
  await expect(navigation.locator('.product-nav').getByRole('link', { name: /^Personas$/i })).toBeVisible();
  await expect(navigation.getByRole('radio', { name: 'Dark' })).toBeVisible();
  await expect(navigation.locator('button.new-conversation')).toBeVisible();
  await expect(navigation.getByRole('button', { name: 'A thoughtful beginning' })).toBeVisible();
  await expect(navigation.locator('button.new-conversation')).toHaveAttribute('title', 'New conversation');
  await expect(navigation.getByRole('button', { name: 'A thoughtful beginning' })).toHaveAttribute('title', 'A thoughtful beginning');
  const geometry = await navigation.evaluate((element) => {
    const conversationNav = element.querySelector<HTMLElement>('.conversation-nav');
    const conversationLink = element.querySelector<HTMLElement>('.conversation-link');
    return {
      navigationWidth: element.getBoundingClientRect().width,
      conversationNavWidth: conversationNav?.getBoundingClientRect().width ?? 0,
      conversationLinkWidth: conversationLink?.getBoundingClientRect().width ?? 0,
    };
  });
  expect(geometry.conversationNavWidth).toBeGreaterThanOrEqual(geometry.navigationWidth - 25);
  expect(geometry.conversationLinkWidth).toBeGreaterThanOrEqual(geometry.navigationWidth - 25);
  expect(geometry.conversationLinkWidth).toBeGreaterThan(40);
});

test('keeps primary destinations structurally separate from recent conversations', async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== 'desktop', 'Collapsed destination accessibility runs once in the desktop project.');
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto('/');
  const navigation = page.locator('#primary-navigation');
  const productNav = navigation.locator('.product-nav');
  const recentNav = navigation.locator('.conversation-nav');

  await expect(productNav).toBeVisible();
  await expect(productNav.getByRole('link')).toHaveCount(3);
  await expect(productNav.getByRole('link', { name: /^Conversations$/i })).toBeVisible();
  await expect(productNav.getByRole('link', { name: /^Agents$/i })).toBeVisible();
  await expect(productNav.getByRole('link', { name: /^Personas$/i })).toBeVisible();
  await expectExpandedDestinationTiles(productNav);
  await expect(recentNav).toBeVisible();
  await expect(recentNav.getByRole('button', { name: /A thoughtful beginning/i })).toBeVisible();
  await expect(productNav.locator('a')).not.toHaveClass(/conversation-link/);
  await expect(recentNav.locator('button')).toHaveClass(/conversation-link/);

  await navigation.getByRole('button', { name: 'Collapse navigation' }).click();
  await expect(navigation).toHaveClass(/is-collapsed/);
  await page.waitForTimeout(220);
  await expect(productNav).toBeVisible();
  await expect(productNav.getByRole('link', { name: /^Conversations$/i })).toBeVisible();
  await expect(productNav.getByRole('link', { name: /^Agents$/i })).toBeVisible();
  await expect(productNav.getByRole('link', { name: /^Personas$/i })).toBeVisible();
  await expect(productNav.getByRole('link')).toHaveCount(3);
});

test('keeps primary destinations available in the mobile drawer', async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== 'mobile', 'Mobile drawer navigation runs once in the mobile project.');
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/');
  const navigation = page.getByRole('navigation', { name: 'Primary navigation' });
  await page.getByRole('button', { name: 'Open navigation' }).click();
  await expect(navigation).toBeVisible();

  const productNav = navigation.locator('.product-nav');
  const recentNav = navigation.locator('.conversation-nav');
  await expect(productNav).toBeVisible();
  await expect(productNav.getByRole('link', { name: /^Conversations$/i })).toBeVisible();
  await expect(productNav.getByRole('link', { name: /^Agents$/i })).toBeVisible();
  await expect(productNav.getByRole('link', { name: /^Personas$/i })).toBeVisible();
  await expectExpandedDestinationTiles(productNav);
  await expect(recentNav).toBeVisible();
  await expect(recentNav.getByRole('button', { name: /A thoughtful beginning/i })).toBeVisible();
});

test('sends a local message and allows interruption', async ({ page }) => {
  await page.goto('/');
  const composer = page.getByRole('textbox', { name: 'Message Aura' });
  await composer.fill('A small local thought');
  await page.getByRole('button', { name: 'Send' }).click();
  await expect(page.getByRole('article', { name: 'Your message' }).getByText('A small local thought')).toBeVisible();
  const workingStatus = page.getByRole('status');
  await expect(workingStatus.getByRole('button', { name: 'Stop' })).toBeVisible();
  await workingStatus.getByRole('button', { name: 'Stop' }).click();
  await expect(page.getByText(/Generation stopped/)).toBeVisible();
  await expect(page.getByText('Generation interrupted')).toBeVisible();
});

test('renders a completed local reply', async ({ page }) => {
  await page.goto('/');
  const composer = page.getByRole('textbox', { name: 'Message Aura' });
  await composer.fill('A completed local thought');
  await page.getByRole('button', { name: 'Send' }).click();
  await expect(page.getByRole('article', { name: 'Aura response' }).last()).toContainText('A completed local thought');
});

test('shows incompatible models as disabled and persists a conversation model change', async ({ page }) => {
  await page.goto('/');
  const picker = page.getByLabel('Model');
  await expect(picker).toHaveValue('qwen2.5:7b');
  await expect(picker.locator('option[value="embed-only"]')).toHaveAttribute('disabled', '');

  await picker.selectOption('llama3.2:3b');
  await expect(picker).toHaveValue('llama3.2:3b');
  await page.reload();
  await expect(page.getByLabel('Model')).toHaveValue('llama3.2:3b');
});

test('recovers from a provider error', async ({ page }) => {
  await page.goto('/?fixture=error');
  const composer = page.getByRole('textbox', { name: 'Message Aura' });
  await composer.fill('A recoverable local thought');
  await page.getByRole('button', { name: 'Send' }).click();
  await expect(page.getByText(/could not reach the selected provider/)).toBeVisible();
  await page.getByRole('button', { name: 'Try again' }).click();
  await expect(page.getByRole('article', { name: 'Aura response' }).last()).toContainText('A recoverable local thought');
});

test('mobile composer remains sticky and passes axe checks', async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== 'mobile', 'Mobile sticky and axe coverage uses the mobile project.');
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/');
  const composer = page.locator('form.composer-wrap');
  await expect(composer).toHaveCSS('position', 'sticky');
  await expect(composer).toHaveCSS('bottom', '0px');
  const results = await new AxeBuilder({ page }).analyze();
  expect(results.violations).toEqual([]);
});

test('dark theme conversation passes axe checks', async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== 'desktop', 'Dark contrast coverage runs once in the desktop project.');
  await page.addInitScript(() => localStorage.setItem('aura-theme', 'dark'));
  await page.goto('/');
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
  const results = await new AxeBuilder({ page }).analyze();
  expect(results.violations).toEqual([]);
});

test('mobile navigation returns focus to its trigger', async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== 'mobile', 'Mobile overlay focus coverage uses the mobile project.');
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/');
  const trigger = page.getByRole('button', { name: 'Open navigation' });
  await trigger.click();
  await expect(page.getByRole('navigation', { name: 'Primary navigation' })).toBeVisible();
  await page.getByRole('navigation', { name: 'Primary navigation' }).getByRole('button', { name: 'Close navigation' }).click();
  await expect(trigger).toBeFocused();
});

test('desktop collapse cannot create a collapsed mobile drawer', async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== 'desktop', 'Desktop-to-mobile collapse coverage runs once in the desktop project.');
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto('/');
  const navigation = page.locator('#primary-navigation');
  await navigation.getByRole('button', { name: 'Collapse navigation' }).click();
  await expect(navigation).toHaveClass(/is-collapsed/);

  await page.setViewportSize({ width: 390, height: 844 });
  const trigger = page.getByRole('button', { name: 'Open navigation' });
  await trigger.click();
  await expect(navigation).toHaveClass(/is-mobile-open/);
  await expect(navigation).not.toHaveClass(/is-collapsed/);
  await expect(navigation).toHaveCSS('flex-basis', '272px');
  await expect(navigation.getByRole('button', { name: 'Close navigation' })).toBeVisible();
  await expect(navigation.getByRole('button', { name: 'Expand navigation' })).toHaveCount(0);
  await navigation.getByRole('button', { name: 'Close navigation' }).click();
  await expect(navigation).not.toHaveClass(/is-mobile-open/);
  await expect(trigger).toBeFocused();
});

test('sidebar motion respects reduced-motion preferences', async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== 'desktop', 'Motion preference coverage runs once in the desktop project.');
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await page.goto('/');
  const transitionDurations = await page.locator('#primary-navigation').evaluate((element) => getComputedStyle(element).transitionDuration.split(',').map((duration) => Number.parseFloat(duration)));
  expect(transitionDurations.every((duration) => duration <= 0.001)).toBe(true);
  const navigation = page.locator('#primary-navigation');
  await navigation.getByRole('button', { name: 'Collapse navigation' }).click();
  await expect(navigation.locator('.conversation-nav')).toHaveCount(0);
  await navigation.getByRole('button', { name: 'Expand navigation' }).click();
  await expect(navigation.locator('.conversation-nav')).toBeVisible();
  await expect(navigation.locator('aura-theme-select')).toBeVisible();
});

test('mobile navigation closes on Escape and restores focus', async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== 'mobile', 'Mobile Escape coverage uses the mobile project.');
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/');
  const trigger = page.getByRole('button', { name: 'Open navigation' });
  const navigation = page.locator('#primary-navigation');
  await trigger.click();
  await expect(navigation).toHaveClass(/is-mobile-open/);
  await page.keyboard.press('Escape');
  await expect(navigation).not.toHaveClass(/is-mobile-open/);
  await expect(trigger).toBeFocused();
});

test('closed mobile navigation is hidden and unreachable by Tab', async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== 'mobile', 'Closed drawer keyboard coverage uses the mobile project.');
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/');
  const trigger = page.getByRole('button', { name: 'Open navigation' });
  const navigation = page.locator('#primary-navigation');
  await expect(navigation).not.toHaveClass(/is-mobile-open/);
  await expect(navigation).toHaveCSS('visibility', 'hidden');
  await expect(navigation).toHaveCSS('pointer-events', 'none');
  await trigger.focus();
  const focusableCount = await page.locator('a:visible, button:visible, textarea:visible, input:visible, select:visible, [tabindex]:visible').count();
  for (let index = 0; index < focusableCount + 1; index += 1) {
    await page.keyboard.press('Tab');
    await expect.poll(() => navigation.evaluate((element) => element.contains(document.activeElement))).toBe(false);
  }
});

test('mobile conversation selection closes navigation and restores focus', async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== 'mobile', 'Mobile selection coverage uses the mobile project.');
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/');
  const trigger = page.getByRole('button', { name: 'Open navigation' });
  const navigation = page.locator('#primary-navigation');
  await trigger.click();
  await navigation.getByRole('button', { name: /A thoughtful beginning/ }).click();
  await expect(navigation).not.toHaveClass(/is-mobile-open/);
  await expect(trigger).toBeFocused();
});

test('mobile new conversation closes navigation and restores focus', async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== 'mobile', 'Mobile new-conversation coverage uses the mobile project.');
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/');
  const trigger = page.getByRole('button', { name: 'Open navigation' });
  const navigation = page.locator('#primary-navigation');
  await trigger.click();
  await navigation.locator('button.new-conversation').click();
  await expect(navigation).not.toHaveClass(/is-mobile-open/);
  await expect(trigger).toBeFocused();
});

test('leaving mobile clears the modal drawer without a reload', async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== 'mobile', 'Responsive drawer transition coverage uses the mobile project.');
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/');
  const trigger = page.getByRole('button', { name: 'Open navigation' });
  const navigation = page.locator('#primary-navigation');
  await trigger.click();
  await expect(navigation).toHaveClass(/is-mobile-open/);

  await page.setViewportSize({ width: 1024, height: 768 });
  await expect(navigation).not.toHaveClass(/is-mobile-open/);
  await expect(page.getByRole('button', { name: 'Close navigation' })).toHaveCount(0);
  await page.locator('#main-content').focus();
  await expect(page.locator('#main-content')).toBeFocused();
});
