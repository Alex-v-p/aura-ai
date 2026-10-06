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

async function expectDestinationPresentation(productNav: Locator): Promise<void> {
  const presentation = await productNav.locator('a').evaluateAll((links) => links.map((link) => {
    const label = link.querySelector<HTMLElement>('.label');
    const icon = link.querySelector<SVGElement>('svg');
    const iconShapes = icon?.querySelectorAll('path, rect, polygon, polyline, line, ellipse').length ?? 0;
    const linkStyle = getComputedStyle(link);
    const before = getComputedStyle(link, '::before');
    const after = getComputedStyle(link, '::after');
    const rect = link.getBoundingClientRect();
    const marker = [before, after].find((style) => style.content !== 'none'
      && style.display !== 'none'
      && (style.content !== '""' || style.width !== 'auto' || style.height !== 'auto'
        || style.borderTopWidth !== '0px' || style.borderRightWidth !== '0px'
        || style.borderBottomWidth !== '0px' || style.borderLeftWidth !== '0px'));
    return {
      accessibleName: link.getAttribute('aria-label') ?? link.textContent?.trim() ?? '',
      title: link.getAttribute('title') ?? '',
      labelText: label?.textContent?.trim() ?? '',
      labelWhiteSpace: label ? getComputedStyle(label).whiteSpace : '',
      labelClientWidth: label?.clientWidth ?? 0,
      labelScrollWidth: label?.scrollWidth ?? 0,
      iconCount: link.querySelectorAll('svg').length,
      iconShapes,
      iconViewBox: icon?.getAttribute('viewBox') ?? '',
      iconDecorative: Boolean(icon?.matches('[aria-hidden="true"]') || icon?.closest('[aria-hidden="true"]')),
      selected: link.getAttribute('aria-current') === 'page',
      selectedSurface: linkStyle.backgroundColor,
      selectedBoxShadow: linkStyle.boxShadow,
      markerVisible: Boolean(marker),
      left: rect.left,
      right: rect.right,
      top: rect.top,
      height: rect.height,
      width: rect.width,
    };
  }));

  expect(presentation).toHaveLength(3);
  expect(presentation.map((item) => item.labelText)).toEqual(['Conversations', 'Agents', 'Personas']);
  for (const item of presentation) {
    expect(item.accessibleName).toMatch(/.+/);
    expect(item.title).toMatch(/^(Conversations|Agents|Personas)$/);
    expect(item.labelWhiteSpace).toBe('nowrap');
    expect(item.labelClientWidth).toBeGreaterThan(0);
    expect(item.labelClientWidth).toBeGreaterThanOrEqual(item.labelScrollWidth);
    expect(item.iconCount).toBe(1);
    expect(item.iconShapes).toBeGreaterThan(0);
    expect(item.iconViewBox).toMatch(/\S+/);
    expect(item.iconDecorative).toBe(true);
    expect(item.width).toBeGreaterThan(0);
    expect(item.height).toBeGreaterThan(0);
  }

  const tray = await productNav.boundingBox();
  if (!tray) throw new Error('Destination tray is not measurable.');
  expect(presentation[0]?.left ?? 0).toBeGreaterThan(tray.x + 7);
  expect((tray.x + tray.width) - (presentation.at(-1)?.right ?? 0)).toBeGreaterThan(7);

  const selected = presentation.find((item) => item.selected);
  expect(selected).toBeDefined();
  expect(selected?.selectedSurface).not.toBe('rgba(0, 0, 0, 0)');
  expect(selected?.selectedBoxShadow).not.toMatch(/0px 0px 0px 1px/);
  expect(selected?.markerVisible).toBe(true);
}

async function expectCompactDestinationControls(productNav: Locator): Promise<void> {
  const controls = await productNav.locator('a').evaluateAll((links) => links.map((link) => {
    const label = link.querySelector<HTMLElement>('.label');
    const icon = link.querySelector<SVGElement>('svg');
    return {
      labelVisible: Boolean(label && getComputedStyle(label).visibility !== 'hidden' && getComputedStyle(label).display !== 'none'),
      accessibleName: link.getAttribute('aria-label') ?? link.textContent?.trim() ?? '',
      title: link.getAttribute('title') ?? '',
      iconCount: link.querySelectorAll('svg').length,
      iconShapes: icon?.querySelectorAll('path, rect, polygon, polyline, line, ellipse').length ?? 0,
      iconViewBox: icon?.getAttribute('viewBox') ?? '',
      iconDecorative: Boolean(icon?.matches('[aria-hidden="true"]') || icon?.closest('[aria-hidden="true"]')),
      current: link.getAttribute('aria-current') === 'page',
    };
  }));

  expect(controls).toHaveLength(3);
  for (const control of controls) {
    expect(control.labelVisible).toBe(false);
    expect(control.accessibleName).toMatch(/^(Conversations|Agents|Personas)$/);
    expect(control.title).toMatch(/^(Conversations|Agents|Personas)$/);
    expect(control.iconCount).toBe(1);
    expect(control.iconShapes).toBeGreaterThan(0);
    expect(control.iconViewBox).toMatch(/\S+/);
    expect(control.iconDecorative).toBe(true);
  }
  expect(controls.filter((control) => control.current)).toHaveLength(1);
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
  let createdConversationId: string | null = null;
  const deferredConversationResolvers = new Map<string, () => void>();
  const deferredConversationPending = new Set<string>();
  const now = (): string => new Date().toISOString();
  const summary = (): Record<string, unknown> => ({ id: 'welcome', title: 'A thoughtful beginning', agentProfileId: 'general', agentRevisionId: 'agent-rev-1', modelId: selectedModel, version, createdAt: now(), updatedAt: now(), currentRun: activeRun });
  const isLongSidebarFixture = (): boolean => page.url().includes('fixture=long-sidebar');
  const isRouteRaceFixture = (): boolean => page.url().includes('fixture=route-race');
  const recentConversationSummaries = (): Array<Record<string, unknown>> => isLongSidebarFixture()
    ? Array.from({ length: 48 }, (_, index) => ({ ...summary(), id: index === 0 ? 'welcome' : `recent-${index}`, title: index === 0 ? 'A thoughtful beginning' : `Recent conversation ${index}`, active: index === 0 }))
    : [summary()];
  await page.route('**/api/**', async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.pathname === '/api/v1/auth/session') return route.fulfill({ json: { principal: { issuer: 'https://authentik.test', subject: 'owner', displayName: 'Owner' }, csrfToken: 'csrf', idleExpiresAt: now(), absoluteExpiresAt: now() } });
    if (url.pathname === '/api/v1/test/release' && request.method() === 'GET') {
      const conversationId = url.searchParams.get('conversation');
      deferredConversationResolvers.get(conversationId ?? '')?.();
      deferredConversationResolvers.delete(conversationId ?? '');
      return route.fulfill({ json: { released: conversationId } });
    }
    if (url.pathname === '/api/v1/test/state' && request.method() === 'GET') {
      const conversationId = url.searchParams.get('conversation');
      return route.fulfill({ json: { pending: deferredConversationPending.has(conversationId ?? '') } });
    }
    if (url.pathname === '/api/v1/models') return route.fulfill({ json: { models: [{ id: 'qwen2.5:7b', displayName: 'Qwen 2.5', provider: 'ollama', capabilities: ['chat'], availability: 'available', selectable: true, disabledReason: null }, { id: 'llama3.2:3b', displayName: 'Llama 3.2', provider: 'ollama', capabilities: ['chat'], availability: 'available', selectable: true, disabledReason: null }, { id: 'embed-only', displayName: 'Embeddings only', provider: 'ollama', capabilities: ['embedding'], availability: 'available', selectable: false, disabledReason: 'This model does not support chat.' }], defaultModelId: 'qwen2.5:7b', observedAt: now() } });
    if (url.pathname === '/api/v1/conversations' && request.method() === 'GET') return route.fulfill({ json: { items: recentConversationSummaries(), nextCursor: null } });
    if (url.pathname === '/api/v1/conversations' && request.method() === 'POST') {
      const body = JSON.parse(request.postData() ?? '{}') as { message?: string; modelId?: string };
      if (page.url().includes('fixture=deferred-create')) {
        await new Promise<void>((resolve) => {
          deferredConversationPending.add('create');
          deferredConversationResolvers.set('create', resolve);
        });
        deferredConversationPending.delete('create');
      }
      createdConversationId = 'created-1';
      const runId = `run-${++runNumber}`;
      const userMessage = { id: `user-${runId}`, conversationId: createdConversationId, role: 'user', content: body.message ?? '', state: 'complete', runId, createdAt: now(), updatedAt: now() };
      activeRun = { id: runId, conversationId: createdConversationId, userMessageId: userMessage.id, assistantMessageId: `assistant-${runId}`, status: 'running', agentRevisionId: 'agent-rev-1', modelPolicyRevisionId: 'policy-1', provider: 'ollama', modelId: body.modelId ?? selectedModel, retryOfRunId: null, createdAt: now(), startedAt: now(), finishedAt: null, error: null };
      messages = [userMessage];
      return route.fulfill({ status: 202, json: { conversation: { ...summary(), id: createdConversationId, title: body.message?.slice(0, 30) ?? 'New conversation', version: 1 }, userMessage, run: activeRun } });
    }
    if (url.pathname === '/api/v1/conversations/welcome' && request.method() === 'PATCH') {
      const body = JSON.parse(request.postData() ?? '{}') as { modelId?: string; version?: number };
      if (body.modelId) selectedModel = body.modelId;
      version = (body.version ?? version) + 1;
      return route.fulfill({ json: summary() });
    }
    if (url.pathname === '/api/v1/conversations/welcome' && request.method() === 'GET') {
      if (isLongSidebarFixture() && messages.length === 0) {
        messages = Array.from({ length: 80 }, (_, index) => ({
          id: `fixture-message-${index}`,
          conversationId: 'welcome',
          role: index % 2 === 0 ? 'user' : 'assistant',
          content: `Long conversation fixture message ${index}. `.repeat(3),
          state: 'complete',
          runId: null,
          createdAt: now(),
          updatedAt: now(),
        }));
      }
      return route.fulfill({ json: { ...summary(), messages, recentRuns: activeRun ? [activeRun] : [] } });
    }
    if (createdConversationId && url.pathname === `/api/v1/conversations/${createdConversationId}` && request.method() === 'GET') return route.fulfill({ json: { ...summary(), id: createdConversationId, title: 'Created conversation', messages, recentRuns: activeRun ? [activeRun] : [] } });
    const detailMatch = url.pathname.match(/^\/api\/v1\/conversations\/([^/]+)$/);
    if (detailMatch && request.method() === 'GET') {
      const conversationId = decodeURIComponent(detailMatch[1]);
      if (conversationId === 'route-one' && isRouteRaceFixture()) {
        deferredConversationPending.add(conversationId);
        await new Promise<void>((resolve) => deferredConversationResolvers.set(conversationId, resolve));
        deferredConversationPending.delete(conversationId);
        return route.fulfill({ status: 503, json: { detail: 'Stale route lookup failed.' } });
      }
      if (conversationId === 'route-two' && isRouteRaceFixture()) return route.fulfill({ json: { ...summary(), id: conversationId, title: 'Route two', messages: [{ id: 'route-two-message', conversationId, role: 'assistant', content: 'Route two response.', state: 'complete', runId: null, createdAt: now(), updatedAt: now() }], recentRuns: [] } });
      if (conversationId === 'protected' && page.url().includes('fixture=route-401')) return route.fulfill({ status: 401, json: { detail: 'Please sign in to continue.' } });
      if (conversationId === 'unavailable' && page.url().includes('fixture=route-503')) return route.fulfill({ status: 503, json: { detail: 'Conversation service temporarily unavailable.' } });
    }
    if (url.pathname.startsWith('/api/v1/conversations/') && request.method() === 'GET') return route.fulfill({ status: 404, json: { detail: 'Conversation not found.' } });
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
    await expectCompactDestinationControls(navigation.locator('.product-nav'));
    await expect(navigation.locator('.conversation-nav')).toHaveCount(0);
    await expect(navigation.locator('aura-theme-select')).toHaveCount(0);
    await expect(navigation.locator('.conversation-link')).toHaveCount(0);
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
    await expect(navigation.locator('.conversation-link')).toBeVisible();
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
  await expectCompactDestinationControls(navigation.locator('.product-nav'));
  await expect(navigation.getByRole('radio', { name: 'Dark' })).toBeVisible();
  await expect(navigation.locator('button.new-conversation')).toBeVisible();
  await expect(navigation.locator('.conversation-link')).toBeVisible();
  await expect(navigation.locator('button.new-conversation')).toHaveAttribute('title', 'New conversation');
  await expect(navigation.locator('.conversation-link')).toHaveAttribute('title', 'A thoughtful beginning');
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
  await expectDestinationPresentation(productNav);
  await expect(recentNav).toBeVisible();
  await expect(recentNav.locator('.conversation-link')).toBeVisible();
  await expect(productNav.locator('a')).not.toHaveClass(/conversation-link/);
  await expect(recentNav.locator('.conversation-link')).toHaveCount(1);

  await navigation.getByRole('button', { name: 'Collapse navigation' }).click();
  await expect(navigation).toHaveClass(/is-collapsed/);
  await page.waitForTimeout(220);
  await expect(productNav).toBeVisible();
  await expect(productNav.getByRole('link', { name: /^Conversations$/i })).toBeVisible();
  await expect(productNav.getByRole('link', { name: /^Agents$/i })).toBeVisible();
  await expect(productNav.getByRole('link', { name: /^Personas$/i })).toBeVisible();
  await expect(productNav.getByRole('link')).toHaveCount(3);
  await expectCompactDestinationControls(productNav);
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
  await expectDestinationPresentation(productNav);
  await expect(recentNav).toBeVisible();
  await expect(recentNav.locator('.conversation-link')).toBeVisible();
});

test('keeps the sidebar viewport-stable while long conversation content scrolls', async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== 'desktop' && testInfo.project.name !== 'tablet', 'Viewport-stable sidebar coverage runs on desktop and tablet projects.');
  await page.goto('/?fixture=long-sidebar');
  const navigation = page.locator('#primary-navigation');
  const recentNav = navigation.locator('.conversation-nav');
  const mainContent = page.locator('#main-content');
  await expect(recentNav.locator('.conversation-link')).toHaveCount(48);
  await expect(page.getByText('Long conversation fixture message 79.')).toBeVisible();

  const beforeScroll = await page.evaluate(() => {
    const sidebar = document.querySelector<HTMLElement>('#primary-navigation');
    const main = document.querySelector<HTMLElement>('#main-content');
    const recent = document.querySelector<HTMLElement>('.conversation-nav');
    const rect = (selector: string): { top: number; bottom: number; height: number } => {
      const element = document.querySelector<HTMLElement>(selector);
      const bounds = element?.getBoundingClientRect();
      return { top: bounds?.top ?? 0, bottom: bounds?.bottom ?? 0, height: bounds?.height ?? 0 };
    };
    const sidebarScrollableDescendants = sidebar
      ? [sidebar, ...Array.from(sidebar.querySelectorAll<HTMLElement>('*'))]
        .filter((element) => ['auto', 'scroll'].includes(getComputedStyle(element).overflowY))
        .map((element) => element.className || element.tagName.toLowerCase())
      : [];
    return {
      viewportHeight: window.innerHeight,
      documentScrollHeight: document.documentElement.scrollHeight,
      bodyScrollHeight: document.body.scrollHeight,
      sidebar: rect('#primary-navigation'),
      sidebarTop: rect('.sidebar-top'),
      productNav: rect('.product-nav'),
      sidebarBottom: rect('.sidebar-bottom'),
      mainClientHeight: main?.clientHeight ?? 0,
      mainScrollHeight: main?.scrollHeight ?? 0,
      recentClientHeight: recent?.clientHeight ?? 0,
      recentScrollHeight: recent?.scrollHeight ?? 0,
      recentOverflowY: recent ? getComputedStyle(recent).overflowY : 'visible',
      sidebarScrollableDescendants,
    };
  });

  expect(beforeScroll.sidebar.top).toBeGreaterThanOrEqual(-1);
  expect(beforeScroll.sidebar.bottom).toBeLessThanOrEqual(beforeScroll.viewportHeight + 1);
  expect(beforeScroll.sidebar.height).toBeLessThanOrEqual(beforeScroll.viewportHeight + 1);
  expect(beforeScroll.documentScrollHeight).toBeLessThanOrEqual(beforeScroll.viewportHeight + 1);
  expect(beforeScroll.bodyScrollHeight).toBeLessThanOrEqual(beforeScroll.viewportHeight + 1);
  expect(beforeScroll.mainScrollHeight).toBeGreaterThan(beforeScroll.mainClientHeight);
  expect(beforeScroll.recentScrollHeight).toBeGreaterThan(beforeScroll.recentClientHeight);
  expect(['auto', 'scroll']).toContain(beforeScroll.recentOverflowY);
  expect(beforeScroll.sidebarScrollableDescendants).toEqual(['conversation-nav']);

  await mainContent.evaluate((element) => { element.scrollTop = element.scrollHeight; });
  await recentNav.evaluate((element) => { element.scrollTop = element.scrollHeight; });
  const afterScroll = await page.evaluate(() => {
    const main = document.querySelector<HTMLElement>('#main-content');
    const recent = document.querySelector<HTMLElement>('.conversation-nav');
    const bounds = (selector: string): { top: number; bottom: number } => {
      const rect = document.querySelector<HTMLElement>(selector)?.getBoundingClientRect();
      return { top: rect?.top ?? 0, bottom: rect?.bottom ?? 0 };
    };
    return {
      mainScrollTop: main?.scrollTop ?? 0,
      recentScrollTop: recent?.scrollTop ?? 0,
      sidebarTop: bounds('.sidebar-top'),
      productNav: bounds('.product-nav'),
      sidebarBottom: bounds('.sidebar-bottom'),
    };
  });

  expect(afterScroll.mainScrollTop).toBeGreaterThan(0);
  expect(afterScroll.recentScrollTop).toBeGreaterThan(0);
  expect(afterScroll.sidebarTop.top).toBeCloseTo(beforeScroll.sidebarTop.top, 0);
  expect(afterScroll.sidebarTop.bottom).toBeCloseTo(beforeScroll.sidebarTop.bottom, 0);
  expect(afterScroll.productNav.top).toBeCloseTo(beforeScroll.productNav.top, 0);
  expect(afterScroll.productNav.bottom).toBeCloseTo(beforeScroll.productNav.bottom, 0);
  expect(afterScroll.sidebarBottom.top).toBeCloseTo(beforeScroll.sidebarBottom.top, 0);
  expect(afterScroll.sidebarBottom.bottom).toBeCloseTo(beforeScroll.sidebarBottom.bottom, 0);
});

test('scrolls the desktop main canvas when the pointer is over conversation content', async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== 'desktop', 'Desktop wheel ownership runs once in the desktop project.');
  await page.goto('/?fixture=long-sidebar');
  const conversation = page.locator('.conversation');
  const mainContent = page.locator('#main-content');
  const transcript = page.locator('.transcript');
  const composer = page.locator('form.composer-wrap');
  await expect(conversation).toBeVisible();
  await expect(page.getByText('Long conversation fixture message 79.')).toBeVisible();

  const ownersBeforeWheel = await page.evaluate(() => {
    const candidates = [
      document.querySelector<HTMLElement>('#main-content'),
      document.querySelector<HTMLElement>('.conversation'),
      document.querySelector<HTMLElement>('.transcript'),
    ];
    return candidates
      .filter((element): element is HTMLElement => element !== null)
      .filter((element) => ['auto', 'scroll'].includes(getComputedStyle(element).overflowY) && element.scrollHeight > element.clientHeight)
      .map((element) => element.id || element.className);
  });
  expect(ownersBeforeWheel).toEqual(['main-content']);
  await expect(mainContent).toHaveCSS('overflow-y', 'auto');
  await expect(transcript).not.toHaveCSS('overflow-y', 'auto');
  await expect(transcript).not.toHaveCSS('overflow-y', 'scroll');
  await expect(composer).toHaveCSS('position', 'sticky');

  await transcript.hover({ position: { x: 12, y: 120 } });
  await page.mouse.wheel(0, 900);
  await expect.poll(async () => mainContent.evaluate((element) => element.scrollTop)).toBeGreaterThan(0);
  await expect.poll(async () => transcript.evaluate((element) => element.scrollTop)).toBe(0);

  await mainContent.evaluate((element) => { element.scrollTop = 0; });
  await mainContent.focus();
  await expect(mainContent).toBeFocused();
  await page.keyboard.press('PageDown');
  await expect.poll(async () => mainContent.evaluate((element) => element.scrollTop)).toBeGreaterThan(0);
  await expect.poll(async () => transcript.evaluate((element) => element.scrollTop)).toBe(0);

  const afterWheel = await page.evaluate(() => {
    const main = document.querySelector<HTMLElement>('#main-content');
    const composerElement = document.querySelector<HTMLElement>('form.composer-wrap');
    const bounds = composerElement?.getBoundingClientRect();
    return {
      mainScrollTop: main?.scrollTop ?? 0,
      composerTop: bounds?.top ?? 0,
      composerBottom: bounds?.bottom ?? 0,
      viewportHeight: window.innerHeight,
    };
  });
  expect(afterWheel.mainScrollTop).toBeGreaterThan(0);
  expect(afterWheel.composerTop).toBeGreaterThanOrEqual(-1);
  expect(afterWheel.composerBottom).toBeLessThanOrEqual(afterWheel.viewportHeight + 1);
  await expect(page.getByRole('textbox', { name: 'Message Aura' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Send' })).toBeEnabled();
});

test('keeps mobile conversation scrolling contained in the conversation panel', async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== 'mobile', 'Mobile contained scrolling runs once in the mobile project.');
  await page.goto('/?fixture=long-sidebar');
  const conversation = page.locator('.conversation');
  const mainContent = page.locator('#main-content');
  const transcript = page.locator('.transcript');
  await expect(conversation).toBeVisible();
  await expect(page.getByText('Long conversation fixture message 79.')).toBeVisible();

  const metrics = await page.evaluate(() => {
    const conversationElement = document.querySelector<HTMLElement>('.conversation');
    const main = document.querySelector<HTMLElement>('#main-content');
    const transcriptElement = document.querySelector<HTMLElement>('.transcript');
    const candidates = [main, conversationElement, transcriptElement];
    return {
      conversationOverflowY: conversationElement ? getComputedStyle(conversationElement).overflowY : 'visible',
      conversationClientHeight: conversationElement?.clientHeight ?? 0,
      conversationScrollHeight: conversationElement?.scrollHeight ?? 0,
      mainScrollTop: main?.scrollTop ?? 0,
      owners: candidates
        .filter((element): element is HTMLElement => element !== null)
        .filter((element) => ['auto', 'scroll'].includes(getComputedStyle(element).overflowY) && element.scrollHeight > element.clientHeight)
        .map((element) => element.className || element.id),
    };
  });
  expect(['auto', 'scroll']).toContain(metrics.conversationOverflowY);
  expect(metrics.conversationScrollHeight).toBeGreaterThan(metrics.conversationClientHeight);
  expect(metrics.owners).toEqual(['conversation']);
  expect(metrics.mainScrollTop).toBe(0);
  await transcript.hover({ position: { x: 12, y: 120 } });
  await page.mouse.wheel(0, 900);
  await expect.poll(async () => conversation.evaluate((element) => element.scrollTop)).toBeGreaterThan(0);
  await expect.poll(async () => mainContent.evaluate((element) => element.scrollTop)).toBe(0);

  await conversation.evaluate((element) => { element.scrollTop = 0; });
  const conversationBounds = await conversation.boundingBox();
  if (!conversationBounds) throw new Error('Conversation panel is not measurable for touch scrolling.');
  const touchX = conversationBounds.x + Math.min(160, conversationBounds.width / 2);
  const touchStartY = conversationBounds.y + Math.min(280, Math.max(32, conversationBounds.height - 32));
  const touchClient = await page.context().newCDPSession(page);
  try {
    await touchClient.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [{ x: touchX, y: touchStartY }] });
    for (const offset of [100, 200, 300, 400]) {
      await touchClient.send('Input.dispatchTouchEvent', { type: 'touchMove', touchPoints: [{ x: touchX, y: touchStartY - offset }] });
    }
    await touchClient.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
  } finally {
    await touchClient.detach();
  }
  await expect.poll(async () => conversation.evaluate((element) => element.scrollTop)).toBeGreaterThan(0);
  await expect.poll(async () => mainContent.evaluate((element) => element.scrollTop)).toBe(0);
  await expect(page.getByRole('textbox', { name: 'Message Aura' })).toBeVisible();
});

test('selects a recent conversation in one click from agent and persona routes', async ({ page }, testInfo) => {
  for (const route of ['/agents', '/personas']) {
    await page.goto(route);
    if (testInfo.project.name === 'mobile') await page.getByRole('button', { name: 'Open navigation' }).click();
    const recentConversation = page.locator('.conversation-link').filter({ hasText: 'A thoughtful beginning' });
    await expect(recentConversation).toBeVisible();
    await recentConversation.click();
    await expect(page).toHaveURL(/\/conversation\/welcome$/);
    await expect(page.getByRole('heading', { name: 'A calm space to think.' })).toBeVisible();
  }
});

test('supports direct conversation links, reload, and browser history', async ({ page }) => {
  await page.goto('/conversation/welcome');
  await expect(page).toHaveURL(/\/conversation\/welcome$/);
  await expect(page.getByRole('heading', { name: 'A calm space to think.' })).toBeVisible();
  await expect(page.locator('.product-nav').getByRole('link', { name: /^Conversations$/i })).toHaveAttribute('aria-current', 'page');

  await page.reload();
  await expect(page).toHaveURL(/\/conversation\/welcome$/);
  await expect(page.getByRole('heading', { name: 'A calm space to think.' })).toBeVisible();

  await page.goto('/agents');
  await expect(page.getByRole('heading', { name: /agents/i })).toBeVisible();
  await page.goBack();
  await expect(page).toHaveURL(/\/conversation\/welcome$/);
  await page.goForward();
  await expect(page).toHaveURL(/\/agents$/);
});

test('starts a new conversation from configuration routes', async ({ page }, testInfo) => {
  for (const route of ['/agents', '/personas']) {
    await page.goto(route);
    if (testInfo.project.name === 'mobile') await page.getByRole('button', { name: 'Open navigation' }).click();
    await page.getByRole('button', { name: /new conversation/i }).click();
    await expect(page).toHaveURL(/\/conversation$/);
    await expect(page.getByRole('textbox', { name: 'Message Aura' })).toBeVisible();
  }
});

test('replaces a new conversation URL with its persisted identifier after acceptance', async ({ page }, testInfo) => {
  await page.goto('/agents');
  if (testInfo.project.name === 'mobile') await page.getByRole('button', { name: 'Open navigation' }).click();
  await page.getByRole('button', { name: /new conversation/i }).click();
  const composer = page.getByRole('textbox', { name: 'Message Aura' });
  await composer.fill('Persist this new conversation.');
  await page.getByRole('button', { name: 'Send' }).click();
  await expect(page).toHaveURL(/\/conversation\/created-1$/);
  await expect(page.getByRole('article', { name: 'Your message' })).toContainText('Persist this new conversation.');
});

test('keeps the conversation destination active on detail routes and usable in collapsed/mobile navigation', async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== 'desktop' && testInfo.project.name !== 'mobile', 'Detail-route navigation coverage runs on desktop and mobile projects.');
  await page.goto('/conversation/welcome');
  const navigation = page.getByRole('navigation', { name: 'Primary navigation' });
  const conversationsLink = navigation.locator('.product-nav').getByRole('link', { name: /^Conversations$/i });
  await expect(conversationsLink).toHaveAttribute('aria-current', 'page');

  if (testInfo.project.name === 'desktop') {
    await navigation.getByRole('button', { name: 'Collapse navigation' }).click();
    await expect(conversationsLink).toBeVisible();
    await expect(conversationsLink).toHaveAttribute('title', 'Conversations');
    await conversationsLink.click();
    await expect(page).toHaveURL(/\/conversation$/);
    return;
  }

  await page.goto('/agents');
  await page.getByRole('button', { name: 'Open navigation' }).click();
  const mobileConversationsLink = page.getByRole('navigation', { name: 'Primary navigation' }).locator('.product-nav').getByRole('link', { name: /^Conversations$/i });
  await expect(mobileConversationsLink).toBeVisible();
  await mobileConversationsLink.click();
  await expect(page).toHaveURL(/\/conversation$/);
});

test('applies the latest deep-link selection when an earlier route lookup resolves late', async ({ page }) => {
  await page.goto('/conversation/route-one?fixture=route-race');
  await expect.poll(async () => page.evaluate(() => fetch('/api/v1/test/state?conversation=route-one').then((response) => response.json()).then((state: { pending: boolean }) => state.pending))).toBe(true);
  await page.evaluate(() => {
    window.history.pushState({}, '', '/conversation/route-two?fixture=route-race');
    window.dispatchEvent(new PopStateEvent('popstate'));
  });
  await expect(page).toHaveURL(/\/conversation\/route-two\?fixture=route-race$/);
  await expect(page.getByText('Route two response.')).toBeVisible();

  await page.evaluate(() => { void fetch('/api/v1/test/release?conversation=route-one'); });
  await expect(page).toHaveURL(/\/conversation\/route-two\?fixture=route-race$/);
  await expect(page.getByText('Route two response.')).toBeVisible();
  await expect(page.getByText('Route one response.')).toHaveCount(0);
});

test('keeps the latest routed conversation selected while a prior draft acceptance resolves', async ({ page }, testInfo) => {
  await page.goto('/conversation?fixture=deferred-create');
  const composer = page.getByRole('textbox', { name: 'Message Aura' });
  await composer.fill('Background acceptance should not steal selection.');
  await page.getByRole('button', { name: 'Send' }).click();
  await expect.poll(async () => page.evaluate(() => fetch('/api/v1/test/state?conversation=create').then((response) => response.json()).then((state: { pending: boolean }) => state.pending))).toBe(true);

  await page.evaluate(() => {
    window.history.pushState({}, '', '/conversation/welcome?fixture=deferred-create');
    window.dispatchEvent(new PopStateEvent('popstate'));
  });
  await expect(page).toHaveURL(/\/conversation\/welcome\?fixture=deferred-create$/);
  if (testInfo.project.name === 'mobile') await page.getByRole('button', { name: 'Open navigation' }).click();
  const welcomeLink = page.locator('.conversation-link').filter({ hasText: 'A thoughtful beginning' });
  await expect(welcomeLink).toHaveClass(/is-route-active/);
  await expect(welcomeLink).toHaveAttribute('aria-current', 'page');
  await page.evaluate(() => { void fetch('/api/v1/test/release?conversation=create'); });
  await expect(welcomeLink).toHaveClass(/is-route-active/);
  await expect(welcomeLink).toHaveAttribute('aria-current', 'page');
  await expect(welcomeLink).toHaveAttribute('title', 'A thoughtful beginning');
  await expect(page).toHaveURL(/\/conversation\/welcome\?fixture=deferred-create$/);
  await expect(page.locator('.conversation-link').filter({ hasText: 'Background acceptance' })).toBeVisible();
});

test('persists a UI-created routed draft and survives reload at its persisted URL', async ({ page }, testInfo) => {
  await page.goto('/');
  if (testInfo.project.name === 'mobile') await page.getByRole('button', { name: 'Open navigation' }).click();
  await page.getByRole('button', { name: /new conversation/i }).click();
  if (testInfo.project.name === 'mobile') await page.getByRole('button', { name: 'Open navigation' }).click();
  const draftLink = page.locator('.conversation-link').filter({ hasText: 'New conversation' }).first();
  await expect(draftLink).toBeVisible();
  const draftHref = await draftLink.getAttribute('href');
  expect(draftHref).toMatch(/^\/conversation\/draft-/);
  await draftLink.click();
  await expect(page).toHaveURL(/\/conversation\/draft-[^/]+$/);
  const composer = page.getByRole('textbox', { name: 'Message Aura' });
  await composer.fill('Persist this routed draft.');
  await page.getByRole('button', { name: 'Send' }).click();
  await expect(page).toHaveURL(/\/conversation\/created-1$/);
  await page.reload();
  await expect(page).toHaveURL(/\/conversation\/created-1$/);
  await expect(page.getByText('Persist this routed draft.')).toBeVisible();
});

test('preserves an unauthorized deep-link URL and surfaces the authentication state', async ({ page }) => {
  await page.goto('/conversation/protected?fixture=route-401');
  await expect(page).toHaveURL(/\/conversation\/protected\?fixture=route-401$/);
  await expect(page.getByText(/sign in to continue/i)).toBeVisible();
  await expect(page.getByRole('button', { name: /sign in with authentik/i })).toBeVisible();
});

test('preserves a failed deep-link URL and surfaces the service failure notice', async ({ page }) => {
  await page.goto('/conversation/unavailable?fixture=route-503');
  await expect(page).toHaveURL(/\/conversation\/unavailable\?fixture=route-503$/);
  await expect(page.getByText(/temporarily unavailable|could not load|try again/i)).toBeVisible();
});

test('marks only the exact routed destination and conversation link as current', async ({ page }) => {
  const navigation = page.getByRole('navigation', { name: 'Primary navigation' });
  const product = (name: RegExp) => navigation.locator('.product-nav').getByRole('link', { name });
  const recent = navigation.locator('.conversation-link');

  await page.goto('/agents');
  await expect(product(/^Agents$/i)).toHaveAttribute('aria-current', 'page');
  await expect(product(/^Conversations$/i)).not.toHaveAttribute('aria-current', 'page');
  await expect(product(/^Personas$/i)).not.toHaveAttribute('aria-current', 'page');
  await expect(recent).not.toHaveAttribute('aria-current', 'page');

  await page.goto('/personas');
  await expect(product(/^Personas$/i)).toHaveAttribute('aria-current', 'page');
  await expect(product(/^Conversations$/i)).not.toHaveAttribute('aria-current', 'page');
  await expect(product(/^Agents$/i)).not.toHaveAttribute('aria-current', 'page');
  await expect(recent).not.toHaveAttribute('aria-current', 'page');

  await page.goto('/conversation');
  await expect(product(/^Conversations$/i)).toHaveAttribute('aria-current', 'page');
  await expect(product(/^Agents$/i)).not.toHaveAttribute('aria-current', 'page');
  await expect(product(/^Personas$/i)).not.toHaveAttribute('aria-current', 'page');
  await expect(recent).not.toHaveAttribute('aria-current', 'page');

  await page.goto('/conversation/welcome');
  await expect(product(/^Conversations$/i)).toHaveAttribute('aria-current', 'page');
  await expect(recent).toHaveAttribute('aria-current', 'page');
});

test('recovers an invalid conversation route without discarding an unrelated draft', async ({ page }, testInfo) => {
  await page.goto('/agents');
  if (testInfo.project.name === 'mobile') await page.getByRole('button', { name: 'Open navigation' }).click();
  await page.getByRole('button', { name: /new conversation/i }).click();
  const composer = page.getByRole('textbox', { name: 'Message Aura' });
  await composer.fill('Keep this unrelated draft.');

  await page.evaluate(() => {
    window.history.pushState({}, '', '/conversation/does-not-exist');
    window.dispatchEvent(new PopStateEvent('popstate'));
  });
  await expect(page).toHaveURL(/\/conversation$/);
  await expect(composer).toHaveValue('Keep this unrelated draft.');
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
  await navigation.locator('.conversation-link').filter({ hasText: 'A thoughtful beginning' }).click();
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
