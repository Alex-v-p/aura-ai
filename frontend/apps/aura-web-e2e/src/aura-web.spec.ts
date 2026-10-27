import { expect, test, type Locator, type Page } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';

const DESTINATION_LABELS = [
  'Today',
  'Conversations',
  'Agents',
  'Personas',
  'Memory',
  'Documents',
  'Reminders',
  'Calendar',
  'Approvals',
  'Notifications',
] as const;
const destinationTitle = (label: (typeof DESTINATION_LABELS)[number]): string =>
  label === 'Calendar' ? 'Calendar integrations' : label;

async function expectConnectDestinationGroup(navigation: Locator): Promise<void> {
  const group = navigation.locator('.nav-group-connect');
  await expect(group).toHaveCount(1);
  await expect(group.locator('h2.nav-group-heading')).toHaveText('Connect');
  await expect(
    group.getByRole('link', { name: 'Calendar integrations', exact: true }),
  ).toHaveAttribute('title', 'Calendar integrations');
}

function mobileNavigationTrigger(page: Page): Locator {
  return page.locator('button[aria-controls="primary-navigation"]');
}

async function settleMobileDrawer(page: Page, desired: 'open' | 'closed'): Promise<void> {
  const navigation = page.locator('#primary-navigation');
  const trigger = mobileNavigationTrigger(page);
  const close = navigation.getByRole('button', { name: 'Close navigation' });
  const state = async (): Promise<'open' | 'closed' | 'transitioning'> => {
    const expanded = await trigger.getAttribute('aria-expanded');
    const drawerOpen = await navigation.evaluate((element) =>
      element.classList.contains('is-mobile-open'),
    );
    if (expanded === 'true' && drawerOpen) return 'open';
    if (expanded === 'false' && !drawerOpen) return 'closed';
    return 'transitioning';
  };

  const current = await state();
  if (current === desired) return;
  if (current === 'transitioning') {
    // Route changes can update the trigger and drawer class in separate
    // change-detection turns. Let that user-visible transition settle before
    // deciding whether a control action is needed.
    await expect.poll(state).toMatch(/open|closed/);
    const settled = await state();
    if (settled === desired) return;
  }
  if (desired === 'open') {
    await trigger.click();
    await expect.poll(state).toBe('open');
  } else {
    await close.click();
    await expect.poll(state).toBe('closed');
  }
}

async function waitForMobileDrawer(page: Page, desired: 'open' | 'closed'): Promise<void> {
  const navigation = page.locator('#primary-navigation');
  const trigger = mobileNavigationTrigger(page);
  await expect
    .poll(async () => {
      const expanded = await trigger.getAttribute('aria-expanded');
      const drawerOpen = await navigation.evaluate((element) =>
        element.classList.contains('is-mobile-open'),
      );
      return expanded === 'true' && drawerOpen
        ? 'open'
        : expanded === 'false' && !drawerOpen
          ? 'closed'
          : 'transitioning';
    })
    .toBe(desired);
}

async function waitForConversationReady(page: Page): Promise<void> {
  await expect(
    page.locator('form.composer-wrap[data-conversation-ready="true"]'),
  ).toBeVisible({ timeout: 15_000 });
}

async function waitForAttachmentComposerReady(page: Page): Promise<void> {
  await waitForConversationReady(page);
}

async function openConversationLibrary(
  page: Page,
  testInfo: { readonly project: { readonly name: string } },
): Promise<void> {
  const navigation = page.locator('#primary-navigation');
  if (testInfo.project.name === 'mobile') await settleMobileDrawer(page, 'open');
  if (testInfo.project.name !== 'tablet') {
    await expect(page.getByRole('button', { name: 'Filter', exact: true })).toBeVisible();
    return;
  }
  const dialog = page.getByRole('dialog', { name: /conversation library/i });
  if (await dialog.isVisible()) return;
  const trigger = navigation.getByRole('button', { name: 'Conversation library', exact: true });
  await expect(trigger).toHaveCount(1);
  await trigger.click();
  await expect(page.getByRole('dialog', { name: /conversation library/i })).toBeVisible();
}

async function navigateToConversationContext(
  page: Page,
  testInfo: { readonly project: { readonly name: string } },
): Promise<void> {
  if (!page.url().match(/\/conversation(?:\/|$)/)) {
    const navigation = page.locator('#primary-navigation');
    if (testInfo.project.name === 'mobile') await settleMobileDrawer(page, 'open');
    await navigation.getByRole('link', { name: /^Conversations$/i }).click();
    await expect(page).toHaveURL(/\/conversation(?:$|\?)/);
    // Product links intentionally close the mobile drawer. Wait for that
    // route-driven close before reopening the conversation library so the
    // trigger and drawer cannot disagree during the transition.
    if (testInfo.project.name === 'mobile') await waitForMobileDrawer(page, 'closed');
  }
  await openConversationLibrary(page, testInfo);
}

async function expectExpandedDestinationTiles(productNav: Locator): Promise<void> {
  const metrics = await productNav.locator('a').evaluateAll((links) => {
    return {
      tiles: links.map((link) => {
        const label = link.querySelector<HTMLElement>('.label');
        const linkRect = link.getBoundingClientRect();
        const labelRect = label?.getBoundingClientRect();
        return {
          left: linkRect.left,
          top: linkRect.top,
          width: linkRect.width,
          height: linkRect.height,
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

  expect(metrics.tiles).toHaveLength(DESTINATION_LABELS.length);
  expect(metrics.tiles.map((tile) => tile.labelText)).toEqual(DESTINATION_LABELS);
  for (const tile of metrics.tiles) {
    expect(tile.width).toBeGreaterThanOrEqual(220);
    expect(tile.height).toBeGreaterThanOrEqual(44);
    expect(tile.labelWidth).toBeGreaterThan(0);
    expect(tile.labelClientWidth).toBeGreaterThanOrEqual(tile.labelScrollWidth);
    expect(tile.labelRight).toBeLessThanOrEqual(tile.right + 1);
  }
}

async function expectDestinationPresentation(productNav: Locator): Promise<void> {
  const presentation = await productNav.locator('a').evaluateAll((links) =>
    links.map((link) => {
      const label = link.querySelector<HTMLElement>('.label');
      const icon = link.querySelector<SVGElement>('svg');
      const iconShapes =
        icon?.querySelectorAll('path, rect, polygon, polyline, line, ellipse').length ?? 0;
      const linkStyle = getComputedStyle(link);
      const before = getComputedStyle(link, '::before');
      const after = getComputedStyle(link, '::after');
      const rect = link.getBoundingClientRect();
      const marker = [before, after].find(
        (style) =>
          style.content !== 'none' &&
          style.display !== 'none' &&
          (style.content !== '""' ||
            style.width !== 'auto' ||
            style.height !== 'auto' ||
            style.borderTopWidth !== '0px' ||
            style.borderRightWidth !== '0px' ||
            style.borderBottomWidth !== '0px' ||
            style.borderLeftWidth !== '0px'),
      );
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
        iconDecorative: Boolean(
          icon?.matches('[aria-hidden="true"]') || icon?.closest('[aria-hidden="true"]'),
        ),
        selected: link.getAttribute('aria-current') === 'page',
        color: linkStyle.color,
        selectedSurface: linkStyle.backgroundColor,
        selectedBoxShadow: linkStyle.boxShadow,
        markerVisible:
          getComputedStyle(link.querySelector<HTMLElement>('.product-current-marker') ?? link)
            .display !== 'none' || Boolean(marker),
        left: rect.left,
        right: rect.right,
        top: rect.top,
        height: rect.height,
        width: rect.width,
      };
    }),
  );

  expect(presentation).toHaveLength(DESTINATION_LABELS.length);
  expect(presentation.map((item) => item.labelText)).toEqual(DESTINATION_LABELS);
  const trayStyle = await productNav.evaluate((element) => {
    const style = getComputedStyle(element);
    return {
      backgroundColor: style.backgroundColor,
      border: style.border,
      boxShadow: style.boxShadow,
    };
  });
  expect(trayStyle.backgroundColor).toMatch(/rgba?\(0, 0, 0, 0\)|transparent/);
  expect(trayStyle.border).toMatch(/0px/);
  expect(trayStyle.boxShadow).toMatch(/none|0px 0px 0px 0px/);
  for (const [index, item] of presentation.entries()) {
    const expectedLabel = DESTINATION_LABELS[index];
    const expectedTitle = expectedLabel ? destinationTitle(expectedLabel) : '';
    expect(item.accessibleName).toMatch(/.+/);
    expect(item.accessibleName).toBe(expectedTitle);
    expect(item.title).toBe(expectedTitle);
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
  expect(presentation[0]?.left ?? 0).toBeGreaterThanOrEqual(tray.x);
  expect(presentation.at(-1)?.right ?? 0).toBeLessThanOrEqual(tray.x + tray.width);

  const selected = presentation.find((item) => item.selected);
  expect(selected).toBeDefined();
  expect(selected?.selectedSurface).not.toMatch(/rgba?\(0, 0, 0, 0\)|transparent/);
  expect(selected?.markerVisible).toBe(true);
}

async function expectCompactDestinationControls(productNav: Locator): Promise<void> {
  const controls = await productNav.locator('a').evaluateAll((links) =>
    links.map((link) => {
      const label = link.querySelector<HTMLElement>('.label');
      const icon = link.querySelector<SVGElement>('svg');
      return {
        labelVisible: Boolean(
          label &&
          getComputedStyle(label).visibility !== 'hidden' &&
          getComputedStyle(label).display !== 'none',
        ),
        accessibleName: link.getAttribute('aria-label') ?? link.textContent?.trim() ?? '',
        title: link.getAttribute('title') ?? '',
        iconCount: link.querySelectorAll('svg').length,
        iconShapes:
          icon?.querySelectorAll('path, rect, polygon, polyline, line, ellipse').length ?? 0,
        iconViewBox: icon?.getAttribute('viewBox') ?? '',
        iconDecorative: Boolean(
          icon?.matches('[aria-hidden="true"]') || icon?.closest('[aria-hidden="true"]'),
        ),
        current: link.getAttribute('aria-current') === 'page',
      };
    }),
  );

  expect(controls).toHaveLength(DESTINATION_LABELS.length);
  for (const [index, control] of controls.entries()) {
    expect(control.labelVisible).toBe(false);
    const expectedLabel = DESTINATION_LABELS[index];
    const expectedTitle = expectedLabel ? destinationTitle(expectedLabel) : '';
    expect(control.accessibleName).toBe(expectedTitle);
    expect(control.title).toBe(expectedTitle);
    expect(control.iconCount).toBe(1);
    expect(control.iconShapes).toBeGreaterThan(0);
    expect(control.iconViewBox).toMatch(/\S+/);
    expect(control.iconDecorative).toBe(true);
  }
  expect(controls.filter((control) => control.current)).toHaveLength(1);
}

type ApiFakeOptions = { orderingFixture?: boolean; errorFixture?: boolean };

async function installApiFake(
  page: import('@playwright/test').Page,
  options: ApiFakeOptions = {},
): Promise<void> {
  let errorOnce = options.errorFixture ?? false;
  let failNextStream = false;
  let orderingFixture = options.orderingFixture ?? false;
  let rejectCreateFixture = false;
  page.on('framenavigated', (frame) => {
    if (frame !== page.mainFrame()) return;
    const frameUrl = frame.url();
    if (frameUrl.includes('fixture=ordering')) orderingFixture = true;
    if (frameUrl.includes('fixture=error')) errorOnce = true;
    if (frameUrl.includes('fixture=rejected-create')) rejectCreateFixture = true;
  });
  let runNumber = 0;
  let version = 1;
  let selectedModel = 'qwen2.5:7b';
  let messages: Array<Record<string, unknown>> = [];
  let activeRun: Record<string, unknown> | null = null;
  let createdConversationId: string | null = null;
  let orderingIds = ['second', 'first'];
  const deferredConversationResolvers = new Map<string, () => void>();
  const deferredConversationPending = new Set<string>();
  const now = (): string => new Date().toISOString();
  const builtInAuraProfileId = '7e1d98ee-65b2-5bc7-9ca9-edd7ad85b540';
  const builtInAuraRevision = (): Record<string, unknown> => ({
    id: 'agent-rev-1',
    profileId: builtInAuraProfileId,
    revision: 1,
    displayName: 'Aura',
    purpose: 'A steady, practical thinking partner.',
    instructions: 'Be clear, kind, and action-oriented.',
    personaRevisionId: 'persona-rev-1',
    promptBundleRevisionId: 'bundle-1',
    modelPolicyRevisionId: 'policy-1',
    createdAt: now(),
  });
  const builtInAuraDetail = (): Record<string, unknown> => {
    const currentRevision = builtInAuraRevision();
    return {
      id: builtInAuraProfileId,
      status: 'active',
      version: 1,
      displayName: 'Aura',
      createdAt: now(),
      updatedAt: now(),
      currentRevision,
      revisions: [currentRevision],
    };
  };
  const summary = (): Record<string, unknown> => ({
    id: 'welcome',
    title: 'A thoughtful beginning',
    agentProfileId: 'general',
    agentRevisionId: 'agent-rev-1',
    modelId: selectedModel,
    version,
    createdAt: now(),
    updatedAt: now(),
    currentRun: activeRun,
  });
  // A terminal run belongs in recentRuns, not currentRun. This keeps the
  // browser fixture from re-subscribing to a closed SSE stream after refresh.
  const currentRunForDetail = (): Record<string, unknown> | null => {
    const status = activeRun?.['status'];
    return activeRun &&
      (status === 'queued' || status === 'running' || status === 'cancel_requested')
      ? activeRun
      : null;
  };
  const isLongSidebarFixture = (): boolean => page.url().includes('fixture=long-sidebar');
  const isOrderingFixture = (): boolean =>
    orderingFixture || page.url().includes('fixture=ordering');
  const isRouteRaceFixture = (): boolean => page.url().includes('fixture=route-race');
  const orderingSummary = (id: string): Record<string, unknown> => ({
    ...summary(),
    id,
    title: id === 'first' ? 'First topic' : 'Second topic',
    version: 1,
    agent: {
      profileId: 'general',
      revisionId: 'agent-rev-1',
      displayName: 'Aura',
      revision: 1,
      status: 'active',
      newerRevisionAvailable: false,
    },
    currentRun: id === 'first' ? currentRunForDetail() : null,
  });
  const recentConversationSummaries = (): Array<Record<string, unknown>> => {
    if (isOrderingFixture()) return orderingIds.map((id) => orderingSummary(id));
    if (isLongSidebarFixture())
      return Array.from({ length: 48 }, (_, index) => ({
        ...summary(),
        id: index === 0 ? 'welcome' : `recent-${index}`,
        title: index === 0 ? 'A thoughtful beginning' : `Recent conversation ${index}`,
        active: index === 0,
      }));
    return [summary()];
  };
  await page.route('**/api/**', async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.pathname === '/api/v1/auth/session')
      return route.fulfill({
        json: {
          principal: { issuer: 'https://authentik.test', subject: 'owner', displayName: 'Owner' },
          csrfToken: 'csrf',
          idleExpiresAt: now(),
          absoluteExpiresAt: now(),
        },
      });
    if (url.pathname === '/api/v1/test/release' && request.method() === 'GET') {
      const conversationId = url.searchParams.get('conversation');
      deferredConversationResolvers.get(conversationId ?? '')?.();
      deferredConversationResolvers.delete(conversationId ?? '');
      return route.fulfill({ json: { released: conversationId } });
    }
    if (url.pathname === '/api/v1/test/state' && request.method() === 'GET') {
      const conversationId = url.searchParams.get('conversation');
      return route.fulfill({
        json: { pending: deferredConversationPending.has(conversationId ?? '') },
      });
    }
    if (url.pathname === '/api/v1/models')
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
            {
              id: 'llama3.2:3b',
              displayName: 'Llama 3.2',
              provider: 'ollama',
              capabilities: ['chat'],
              availability: 'available',
              selectable: true,
              disabledReason: null,
            },
            {
              id: 'embed-only',
              displayName: 'Embeddings only',
              provider: 'ollama',
              capabilities: ['embedding'],
              availability: 'available',
              selectable: false,
              disabledReason: 'This model does not support chat.',
            },
          ],
          defaultModelId: 'qwen2.5:7b',
          observedAt: now(),
        },
      });
    if (url.pathname === '/api/v1/agents' && request.method() === 'GET') {
      return route.fulfill({
        json: {
          items: [
            {
              id: 'general',
              status: 'active',
              version: 1,
              currentRevision: builtInAuraRevision(),
              revisions: [],
            },
          ],
        },
      });
    }
    if (url.pathname === '/api/v1/agents/general' && request.method() === 'GET') {
      return route.fulfill({ json: builtInAuraDetail() });
    }
    if (url.pathname === '/api/v1/personas' && request.method() === 'GET') {
      return route.fulfill({ json: { items: [] } });
    }
    if (url.pathname === '/api/v1/conversations' && request.method() === 'GET')
      return route.fulfill({ json: { items: recentConversationSummaries(), nextCursor: null } });
    if (url.pathname === '/api/v1/conversations' && request.method() === 'POST') {
      const body = JSON.parse(request.postData() ?? '{}') as { message?: string; modelId?: string };
      if (rejectCreateFixture)
        return route.fulfill({
          status: 503,
          json: { detail: 'Conversation service temporarily unavailable.' },
        });
      if (page.url().includes('fixture=deferred-create')) {
        await new Promise<void>((resolve) => {
          deferredConversationPending.add('create');
          deferredConversationResolvers.set('create', resolve);
        });
        deferredConversationPending.delete('create');
      }
      createdConversationId = 'created-1';
      const runId = `run-${++runNumber}`;
      const userMessage = {
        id: `user-${runId}`,
        conversationId: createdConversationId,
        role: 'user',
        content: body.message ?? '',
        state: 'complete',
        runId,
        createdAt: now(),
        updatedAt: now(),
      };
      activeRun = {
        id: runId,
        conversationId: createdConversationId,
        userMessageId: userMessage.id,
        assistantMessageId: `assistant-${runId}`,
        status: 'running',
        agentRevisionId: 'agent-rev-1',
        modelPolicyRevisionId: 'policy-1',
        provider: 'ollama',
        modelId: body.modelId ?? selectedModel,
        retryOfRunId: null,
        createdAt: now(),
        startedAt: now(),
        finishedAt: null,
        error: null,
      };
      messages = [userMessage];
      return route.fulfill({
        status: 202,
        json: {
          conversation: {
            ...summary(),
            id: createdConversationId,
            title: body.message?.slice(0, 30) ?? 'New conversation',
            version: 1,
          },
          userMessage,
          run: activeRun,
        },
      });
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
      return route.fulfill({
        json: {
          ...summary(),
          currentRun: currentRunForDetail(),
          messages,
          recentRuns: activeRun ? [activeRun] : [],
        },
      });
    }
    if (
      isOrderingFixture() &&
      url.pathname.match(/^\/api\/v1\/conversations\/(first|second)$/) &&
      request.method() === 'GET'
    ) {
      const conversationId = url.pathname.split('/').at(-1) ?? 'first';
      return route.fulfill({
        json: {
          ...orderingSummary(conversationId),
          messages: [],
          recentRuns: conversationId === 'first' && activeRun ? [activeRun] : [],
        },
      });
    }
    if (
      createdConversationId &&
      url.pathname === `/api/v1/conversations/${createdConversationId}` &&
      request.method() === 'GET'
    )
      return route.fulfill({
        json: {
          ...summary(),
          id: createdConversationId,
          title: 'Created conversation',
          currentRun: currentRunForDetail(),
          messages,
          recentRuns: activeRun ? [activeRun] : [],
        },
      });
    const detailMatch = url.pathname.match(/^\/api\/v1\/conversations\/([^/]+)$/);
    if (detailMatch && request.method() === 'GET') {
      const conversationId = decodeURIComponent(detailMatch[1]);
      if (conversationId === 'route-one' && isRouteRaceFixture()) {
        deferredConversationPending.add(conversationId);
        await new Promise<void>((resolve) =>
          deferredConversationResolvers.set(conversationId, resolve),
        );
        deferredConversationPending.delete(conversationId);
        return route.fulfill({ status: 503, json: { detail: 'Stale route lookup failed.' } });
      }
      if (conversationId === 'route-two' && isRouteRaceFixture())
        return route.fulfill({
          json: {
            ...summary(),
            id: conversationId,
            title: 'Route two',
            messages: [
              {
                id: 'route-two-message',
                conversationId,
                role: 'assistant',
                content: 'Route two response.',
                state: 'complete',
                runId: null,
                createdAt: now(),
                updatedAt: now(),
              },
            ],
            recentRuns: [],
          },
        });
      if (conversationId === 'protected' && page.url().includes('fixture=route-401'))
        return route.fulfill({ status: 401, json: { detail: 'Please sign in to continue.' } });
      if (conversationId === 'unavailable' && page.url().includes('fixture=route-503'))
        return route.fulfill({
          status: 503,
          json: { detail: 'Conversation service temporarily unavailable.' },
        });
    }
    if (url.pathname.startsWith('/api/v1/conversations/') && request.method() === 'GET')
      return route.fulfill({ status: 404, json: { detail: 'Conversation not found.' } });
    if (url.pathname.endsWith('/runs') && request.method() === 'POST') {
      // Use the current document URL as a second source so the fixture does
      // not depend on the relative ordering of `framenavigated` and the
      // application's first API request.
      if (errorOnce || page.url().includes('fixture=error')) {
        errorOnce = false;
        failNextStream = true;
      }
      const body = JSON.parse(request.postData() ?? '{}') as { message?: string };
      const runPath = url.pathname.match(/^\/api\/v1\/conversations\/([^/]+)\/runs$/);
      const runConversationId = isOrderingFixture() ? (runPath?.[1] ?? 'first') : 'welcome';
      const runId = `run-${++runNumber}`;
      const userMessage = {
        id: `user-${runId}`,
        conversationId: runConversationId,
        role: 'user',
        content: body.message ?? '',
        state: 'complete',
        runId,
        createdAt: now(),
        updatedAt: now(),
      };
      activeRun = {
        id: runId,
        conversationId: runConversationId,
        userMessageId: userMessage.id,
        assistantMessageId: `assistant-${runId}`,
        status: 'running',
        agentRevisionId: 'agent-rev-1',
        modelPolicyRevisionId: 'policy-1',
        provider: 'ollama',
        modelId: 'qwen2.5:7b',
        retryOfRunId: null,
        createdAt: now(),
        startedAt: now(),
        finishedAt: null,
        error: null,
      };
      if (isOrderingFixture())
        orderingIds = [runConversationId, ...orderingIds.filter((id) => id !== runConversationId)];
      messages = [...messages, userMessage];
      version += 1;
      return route.fulfill({
        status: 202,
        json: {
          conversation: isOrderingFixture() ? orderingSummary(runConversationId) : summary(),
          userMessage,
          run: activeRun,
        },
      });
    }
    if (url.pathname.endsWith('/cancel') && request.method() === 'POST') {
      activeRun = activeRun ? { ...activeRun, status: 'canceled', finishedAt: now() } : null;
      return route.fulfill({ json: activeRun });
    }
    if (url.pathname.endsWith('/retry') && request.method() === 'POST') {
      const runId = `run-${++runNumber}`;
      activeRun = {
        ...(activeRun ?? {}),
        id: runId,
        status: 'running',
        createdAt: now(),
        startedAt: now(),
        finishedAt: null,
        retryOfRunId: activeRun?.['id'] ?? null,
      };
      return route.fulfill({
        status: 202,
        json: { conversation: summary(), userMessage: messages.at(-1), run: activeRun },
      });
    }
    if (url.pathname.includes('/events')) {
      const run = activeRun;
      const lastUser =
        messages.filter((message) => message['role'] === 'user').at(-1)?.['content'] ??
        'your thought';
      const eventConversationId = String(run?.['conversationId'] ?? 'welcome');
      if (
        String(lastUser).includes('small local thought') &&
        activeRun?.['status'] !== 'canceled'
      ) {
        const deadline = Date.now() + 5_000;
        while (activeRun?.['status'] !== 'canceled' && Date.now() < deadline) {
          await new Promise((resolve) => setTimeout(resolve, 50));
        }
      }
      if (activeRun?.['status'] === 'canceled') {
        const partial = {
          id: String(run?.['assistantMessageId'] ?? 'assistant-run'),
          conversationId: eventConversationId,
          role: 'assistant',
          content: 'A partial response from the provider.',
          state: 'interrupted',
          runId: activeRun['id'],
          createdAt: now(),
          updatedAt: now(),
        };
        messages = [...messages.filter((message) => message['id'] !== partial.id), partial];
        const canceledEvent = [
          'retry: 86400000\n\n',
          `id: 1\nevent: assistant.snapshot\ndata: ${JSON.stringify({ schemaVersion: 1, eventId: 'event-partial', sequence: 1, eventType: 'assistant.snapshot', runId: activeRun['id'], conversationId: eventConversationId, occurredAt: now(), data: { message: partial } })}\n\n`,
          `id: 2\nevent: run.status\ndata: ${JSON.stringify({ schemaVersion: 1, eventId: 'event-canceled', sequence: 2, eventType: 'run.status', runId: activeRun['id'], conversationId: eventConversationId, occurredAt: now(), data: { status: 'canceled', startedAt: activeRun['startedAt'], finishedAt: activeRun['finishedAt'] } })}\n\n`,
        ].join('');
        return route.fulfill({
          status: 200,
          headers: { 'content-type': 'text/event-stream' },
          body: canceledEvent,
        });
      }
      const assistant = {
        id: String(run?.['assistantMessageId'] ?? 'assistant-run'),
        conversationId: eventConversationId,
        role: 'assistant',
        content: `A server response with your thought: ${lastUser}`,
        state: 'complete',
        runId: run?.['id'] ?? 'run',
        createdAt: now(),
        updatedAt: now(),
        ...(page.url().includes('fixture=citations')
          ? {
              citations: [
                {
                  documentId: '11111111-1111-4111-8111-111111111111',
                  displayLabel: 'Document source',
                  locator: 'page:2',
                },
              ],
            }
          : {}),
      };
      if (failNextStream) {
        failNextStream = false;
        activeRun = run
          ? {
              ...run,
              status: 'failed',
              finishedAt: now(),
              error: {
                code: 'provider_unavailable',
                message: 'Aura could not reach the selected provider.',
                retryable: true,
                traceId: 'trace-test',
              },
            }
          : null;
        const errorEvent = `retry: 86400000\n\nid: 1\nevent: run.error\ndata: ${JSON.stringify({ schemaVersion: 1, eventId: 'event-error', sequence: 1, eventType: 'run.error', runId: run?.['id'] ?? 'run', conversationId: eventConversationId, occurredAt: now(), data: activeRun?.['error'] })}\n\n`;
        return route.fulfill({
          status: 200,
          headers: { 'content-type': 'text/event-stream' },
          body: errorEvent,
        });
      }
      messages = [...messages.filter((message) => message['id'] !== assistant.id), assistant];
      activeRun = run ? { ...run, status: 'completed', finishedAt: now() } : null;
      const events = [
        'retry: 86400000\n\n',
        `id: 1\nevent: assistant.snapshot\ndata: ${JSON.stringify({ schemaVersion: 1, eventId: 'event-1', sequence: 1, eventType: 'assistant.snapshot', runId: run?.['id'] ?? 'run', conversationId: eventConversationId, occurredAt: now(), data: { message: assistant } })}\n\n`,
        `id: 2\nevent: run.status\ndata: ${JSON.stringify({ schemaVersion: 1, eventId: 'event-2', sequence: 2, eventType: 'run.status', runId: run?.['id'] ?? 'run', conversationId: eventConversationId, occurredAt: now(), data: { status: 'completed', startedAt: run?.['startedAt'] ?? now(), finishedAt: now() } })}\n\n`,
      ].join('');
      return route.fulfill({
        status: 200,
        headers: { 'content-type': 'text/event-stream' },
        body: events,
      });
    }
    return route.continue();
  });
}

test.beforeEach(async ({ page }, testInfo) => {
  await installApiFake(page, {
    orderingFixture: testInfo.title.includes('selection order stable'),
    errorFixture: testInfo.title.includes('provider error'),
  });
});

async function installAttachmentFixture(page: Page, options: { readonly rejectVisionRun?: boolean; readonly documentProcessing?: boolean; readonly documentProcessingFailure?: boolean } = {}): Promise<void> {
  let deleted = false;
  let deleteAttempts = 0;
  let visionAttempts = 0;
  let documentAccepted = false;
  let documentPolls = 0;
  let documentRetryAttempts = 0;
  const now = (): string => new Date().toISOString();
  const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=', 'base64');
  const attachment = (id: string, filename: string, status = 'ready'): Record<string, unknown> => ({ id, filename, mediaType: 'image/png', byteCount: png.byteLength, status, failure: null, version: status === 'ready' ? 1 : 2, width: 1, height: 1, createdAt: now(), updatedAt: now() });
  await page.route('**/api/v1/models', async (route) => route.fulfill({ json: { models: [{ id: 'qwen2.5vl:7b', displayName: 'Qwen 2.5 VL', provider: 'ollama', capabilities: ['chat', 'vision'], availability: 'available', selectable: true, disabledReason: null }, { id: 'gemma3:12b', displayName: 'Gemma 3', provider: 'ollama', capabilities: ['chat', 'vision'], availability: 'available', selectable: true, disabledReason: null }, { id: 'fixture-chat', displayName: 'Fixture text model', provider: 'fake', capabilities: ['chat'], availability: 'available', selectable: true, disabledReason: null }], defaultModelId: 'fixture-chat', observedAt: now() } }));
  await page.route('**/api/v1/attachments**', async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.pathname === '/api/v1/attachments' && request.method() === 'GET') return route.fulfill({ json: { items: deleted ? [] : [attachment('fixture-image', 'inspection.png')], nextCursor: null } });
    if (url.pathname === '/api/v1/attachments/uploads' && request.method() === 'POST') return route.fulfill({ status: 201, json: { id: 'fixture-upload', attachmentId: 'fixture-image', filename: 'inspection.png', mediaType: 'image/png', byteCount: png.byteLength, status: 'uploading', version: 1, createdAt: now(), expiresAt: now() } });
    if (url.pathname === '/api/v1/attachments/uploads/fixture-upload/content' && request.method() === 'PUT') return route.fulfill({ status: 202, json: { attachment: attachment('fixture-image', 'inspection.png') } });
    if (url.pathname === '/api/v1/attachments/fixture-image/preview' && request.method() === 'GET') return route.fulfill({ status: 200, contentType: 'image/png', body: png, headers: { 'Cache-Control': 'no-store' } });
    if (url.pathname === '/api/v1/attachments/fixture-image' && request.method() === 'GET') return deleted ? route.fulfill({ status: 404, json: { detail: 'Not found' } }) : route.fulfill({ json: attachment('fixture-image', 'inspection.png') });
    if (url.pathname === '/api/v1/attachments/fixture-image' && request.method() === 'DELETE') { deleteAttempts += 1; if (deleteAttempts === 1) return route.fulfill({ status: 503, json: { detail: 'Storage unavailable' } }); deleted = true; return route.fulfill({ status: 202, json: { attachmentId: 'fixture-image', status: 'deleting', version: 2, acceptedAt: now() } }); }
    return route.continue();
  });
  await page.route('**/api/v1/documents**', async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const documentStatus = (): 'processing' | 'ready' | 'failed' => {
      if (!documentAccepted) return 'ready';
      if (options.documentProcessingFailure && documentRetryAttempts === 0) return documentPolls < 2 ? 'processing' : 'failed';
      return options.documentProcessing && documentPolls < 2 ? 'processing' : 'ready';
    };
    const documentStatusValue = documentStatus();
    const document = { id: 'fixture-document', filename: 'grounding.pdf', mediaType: 'application/pdf', byteCount: 42_000, status: documentStatusValue, failure: documentStatusValue === 'failed' ? { code: 'parse_failed', retryable: true } : null, version: documentRetryAttempts === 0 ? (documentStatusValue === 'failed' ? 3 : 1) : (documentStatusValue === 'ready' ? 5 : 4), extractedTextLength: 64, chunkCount: 1, createdAt: now(), updatedAt: now() };
    if (url.pathname === '/api/v1/documents' && request.method() === 'GET') return route.fulfill({ json: { items: [document], nextCursor: null } });
    if (url.pathname === '/api/v1/documents/fixture-document' && request.method() === 'GET') { if (documentAccepted) documentPolls += 1; const status = documentStatus(); return route.fulfill({ json: { ...document, status, failure: status === 'failed' ? { code: 'parse_failed', retryable: true } : null, version: documentRetryAttempts === 0 ? (status === 'failed' ? 3 : 1) : (status === 'ready' ? 5 : 4) } }); }
    if (url.pathname === '/api/v1/documents/uploads' && request.method() === 'POST') return route.fulfill({ status: 201, json: { id: 'fixture-document-upload', documentId: 'fixture-document', filename: 'grounding.pdf', mediaType: 'application/pdf', byteCount: 42_000, status: 'uploading', version: 1, createdAt: now(), expiresAt: now() } });
    if (url.pathname === '/api/v1/documents/uploads/fixture-document-upload/content' && request.method() === 'PUT') { documentAccepted = true; return route.fulfill({ status: 202, json: { duplicate: false, document: { ...document, status: options.documentProcessing || options.documentProcessingFailure ? 'processing' : 'ready', failure: null, version: 1 } } }); }
    if (url.pathname === '/api/v1/documents/fixture-document/retry' && request.method() === 'POST') { documentRetryAttempts += 1; documentPolls = 0; return route.fulfill({ status: 202, json: { ...document, status: 'processing', failure: null, version: 4 } }); }
    return route.continue();
  });
  if (options.rejectVisionRun) await page.route('**/api/v1/conversations**', async (route) => { const path = new URL(route.request().url()).pathname; const admission = route.request().method() === 'POST' && (path === '/api/v1/conversations' || /\/conversations\/[^/]+\/runs$/u.test(path)); if (!admission) return route.fallback(); visionAttempts += 1; if (visionAttempts === 1) return route.fulfill({ status: 409, contentType: 'application/problem+json', json: { status: 409, code: 'model_vision_required', retryable: false, title: 'Vision verification required', detail: 'The selected model is not verified for image understanding.' } }); return route.fallback(); });
}

test('conversation shell bootstraps to the composer before attachment flows', async ({ page }) => {
  await page.goto('/conversation', { waitUntil: 'domcontentloaded' });
  await expect(page.getByRole('textbox', { name: 'Message Aura' })).toBeVisible({ timeout: 10_000 });
});

test('root and Aura home links resolve to the Today action center', async ({ page }, testInfo) => {
  await page.goto('/', { waitUntil: 'domcontentloaded' });
  await expect(page).toHaveURL(/\/today$/);
  await expect(page.locator('#today-heading')).toBeVisible();
  if (testInfo.project.name === 'mobile') {
    await mobileNavigationTrigger(page).click();
  }
  await expect(
    page.locator('.product-nav').getByRole('link', { name: 'Today', exact: true }),
  ).toHaveAttribute('aria-current', 'page');
  await expect(
    page.locator('.product-nav').getByRole('link', { name: 'Conversations', exact: true }),
  ).toHaveAttribute('href', /\/conversation$/);
  await expect(page.getByRole('link', { name: 'Aura home' }).first()).toHaveAttribute(
    'href',
    /\/today$/,
  );
});

test('attachment picker has keyboard tabs and restores focus across desktop, tablet, and mobile', async ({ page }, testInfo) => {
  await installAttachmentFixture(page);
  if (testInfo.project.name === 'mobile') await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/conversation', { waitUntil: 'domcontentloaded' });
  await waitForAttachmentComposerReady(page);
  const trigger = page.getByRole('button', { name: 'Add attachments' });
  await trigger.click();
  const picker = page.getByRole('dialog', { name: 'Add attachments' });
  await expect(picker).toBeVisible();
  const tabs = picker.getByRole('tab');
  await expect(tabs).toHaveCount(3);
  await expect(tabs.nth(0)).toHaveAttribute('aria-selected', 'true');
  await tabs.nth(0).press('ArrowRight');
  await expect(tabs.nth(1)).toHaveAttribute('aria-selected', 'true');
  await tabs.nth(1).press('End');
  await expect(tabs.nth(2)).toBeFocused();
  await page.keyboard.press('Escape');
  await expect(picker).toBeHidden();
  await expect(trigger).toBeFocused();
});

test('image upload auto-selects and recovers from reusable-image deletion failure', async ({ page }) => {
  await installAttachmentFixture(page);
  await page.goto('/conversation', { waitUntil: 'domcontentloaded' });
  await waitForAttachmentComposerReady(page);
  const composer = page.getByRole('textbox', { name: 'Message Aura' });
  await composer.fill('Inspect the image');
  await page.getByRole('button', { name: 'Add attachments' }).click();
  const imageInput = page.locator('input[type="file"][accept*="image/png"]');
  await imageInput.setInputFiles({ name: 'inspection.png', mimeType: 'image/png', buffer: Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=', 'base64') });
  await expect(page.getByText('inspection.png', { exact: true }).first()).toBeVisible();
  await expect(page.locator('.selected-attachments')).toContainText('inspection.png');
  await page.getByRole('tab', { name: 'Images' }).click();
  await expect(page.getByRole('button', { name: 'Select inspection.png' })).toBeVisible();
  await page.getByRole('button', { name: 'Delete inspection.png' }).click();
  await page.getByRole('button', { name: 'Confirm deletion of inspection.png' }).click();
  await expect(page.getByText('Could not delete inspection.png. Try again.', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Delete inspection.png' }).click();
  await page.getByRole('button', { name: 'Confirm deletion of inspection.png' }).click();
  await expect(page.getByRole('button', { name: 'Delete inspection.png' })).toHaveCount(0);
  await expect(page.locator('.selected-attachments')).toHaveCount(0);
});

test('keeps Send gated during direct document processing and enables it when ready', async ({ page }) => {
  await installAttachmentFixture(page, { documentProcessing: true });
  let documentDetailPolls = 0;
  page.on('request', (request) => { if (request.method() === 'GET' && new URL(request.url()).pathname === '/api/v1/documents/fixture-document') documentDetailPolls += 1; });
  await page.goto('/conversation', { waitUntil: 'domcontentloaded' });
  await waitForAttachmentComposerReady(page);
  await page.getByRole('textbox', { name: 'Message Aura' }).fill('Ground this answer in my document');
  await page.getByRole('button', { name: 'Add attachments' }).click();
  await page.getByRole('button', { name: 'Choose PDF or DOCX documents' }).click();
  await page.locator('input[type="file"][accept=".pdf,.docx,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document"]').setInputFiles({ name: 'grounding.pdf', mimeType: 'application/pdf', buffer: Buffer.from('%PDF-1.4 fixture') });
  await expect(page.locator('.selected-attachments')).toContainText('grounding.pdf');
  await expect(page.getByRole('button', { name: 'Send' })).toBeDisabled();
  await expect.poll(() => documentDetailPolls, { timeout: 10_000 }).toBeGreaterThanOrEqual(2);
  await expect(page.getByRole('button', { name: 'Send' })).toBeEnabled({ timeout: 7_000 });
});

test('reconciles a failed direct document upload row and retries processing with its current version', async ({ page }) => {
  await installAttachmentFixture(page, { documentProcessingFailure: true });
  let documentDetailPolls = 0;
  page.on('request', (request) => { if (request.method() === 'GET' && new URL(request.url()).pathname === '/api/v1/documents/fixture-document') documentDetailPolls += 1; });
  await page.goto('/conversation', { waitUntil: 'domcontentloaded' });
  await waitForAttachmentComposerReady(page);
  await page.getByRole('textbox', { name: 'Message Aura' }).fill('Retry this document after processing failure');
  await page.getByRole('button', { name: 'Add attachments' }).click();
  await page.getByRole('button', { name: 'Choose PDF or DOCX documents' }).click();
  await page.locator('input[type="file"][accept=".pdf,.docx,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document"]').setInputFiles({ name: 'grounding.pdf', mimeType: 'application/pdf', buffer: Buffer.from('%PDF-1.4 fixture') });
  await expect(page.locator('.selected-attachments')).toContainText('grounding.pdf');
  await expect(page.getByRole('button', { name: 'Send' })).toBeDisabled();
  await expect.poll(() => documentDetailPolls, { timeout: 10_000 }).toBeGreaterThanOrEqual(2);
  const retry = page.locator('form.composer-wrap').getByRole('button', { name: 'Retry processing' });
  await expect(retry).toBeVisible({ timeout: 7_000 });
  await retry.click();
  await expect(retry).toBeHidden();
  await expect(page.getByRole('button', { name: 'Send' })).toBeEnabled({ timeout: 7_000 });
});

test('preserves image draft after model_vision_required and exposes verified model recovery', async ({ page }) => {
  await installAttachmentFixture(page, { rejectVisionRun: true });
  await page.goto('/conversation', { waitUntil: 'domcontentloaded' });
  await waitForAttachmentComposerReady(page);
  await page.getByRole('textbox', { name: 'Message Aura' }).fill('Describe this image');
  await page.getByRole('button', { name: 'Add attachments' }).click();
  await page.getByRole('tab', { name: 'Images' }).click();
  await page.getByRole('button', { name: 'Select inspection.png' }).click();
  await page.getByRole('button', { name: 'Close attachment picker' }).click();
  await page.getByRole('button', { name: 'Conversation options' }).click();
  const model = page.getByRole('dialog', { name: 'Conversation options' }).getByLabel('Model');
  await model.selectOption('qwen2.5vl:7b');
  await expect(model).toHaveValue('qwen2.5vl:7b');
  await page.getByRole('button', { name: 'Close' }).click();
  await page.getByRole('button', { name: 'Send' }).click();
  await expect(page.getByRole('alert')).toContainText(/verified vision model/i);
  await expect(page.locator('.selected-attachments')).toContainText('inspection.png');
  await expect(page.getByRole('alert')).not.toContainText(/Qwen/i);
  await page.getByRole('button', { name: 'Conversation options' }).click();
  const retryModel = page.getByRole('dialog', { name: 'Conversation options' }).getByLabel('Model');
  await expect(retryModel).toHaveValue('qwen2.5vl:7b');
  await expect(page.getByRole('button', { name: 'Send' })).toBeDisabled();
  await retryModel.selectOption('gemma3:12b');
  await expect(retryModel).toHaveValue('gemma3:12b');
  await expect(page.getByRole('button', { name: 'Send' })).toBeEnabled();
  await page.getByRole('button', { name: 'Close' }).click();
  await page.getByRole('button', { name: 'Send' }).click();
  await expect(page.locator('.selected-attachments')).toHaveCount(0);
});

test('opens with a new-conversation title surface', async ({ page }) => {
    await page.goto('/conversation');
  await expect(page.getByRole('heading', { name: 'New conversation' })).toBeVisible();
  await expect(page.getByRole('textbox', { name: 'Message Aura' })).toBeVisible();
});

test('supports Enter and Shift+Enter composer behavior', async ({ page }) => {
  await page.goto('/conversation');
  await waitForConversationReady(page);
  const composer = page.getByRole('textbox', { name: 'Message Aura' });
  await composer.fill('A first line');
  await composer.press('Shift+Enter');
  await expect(composer).toHaveValue('A first line\n');
  await composer.fill('Send with Enter');
  await composer.press('Enter');
  await expect(page.getByRole('article', { name: 'Your message' })).toContainText(
    'Send with Enter',
  );
});

test('persists the selected theme preference', async ({ page }, testInfo) => {
  test.skip(
    testInfo.project.name !== 'desktop',
    'Theme persistence is covered once in the desktop project.',
  );
  await page.goto('/conversation');
  await page.getByRole('button', { name: 'Preferences' }).click();
  await page.getByRole('radio', { name: 'Dark' }).check();
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
  await page.reload();
  await page.getByRole('button', { name: 'Preferences' }).click();
  await expect(page.getByRole('radio', { name: 'Dark' })).toBeChecked();
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
});

test('supports desktop collapse and tablet rail navigation', async ({ page }, testInfo) => {
  test.skip(
    testInfo.project.name !== 'desktop' && testInfo.project.name !== 'tablet',
    'Navigation geometry is covered by desktop and tablet projects.',
  );
  const navigation = page.locator('#primary-navigation');
  if (testInfo.project.name === 'desktop') {
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto('/conversation');
    await expectConnectDestinationGroup(navigation);
    await navigation.getByRole('button', { name: 'Collapse navigation' }).click();
    await expect(navigation).toHaveClass(/is-collapsed/);
    await expect(navigation).toHaveCSS('flex-basis', '72px');
    await page.waitForTimeout(220);
    await expect(navigation.getByRole('button', { name: 'Expand navigation' })).toBeVisible();
    await expect(navigation.locator('.wordmark-mark')).toBeVisible();
    await expect(navigation.locator('.product-nav')).toBeVisible();
    await expect(navigation.locator('.product-nav a')).toHaveCount(DESTINATION_LABELS.length);
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
    expect(compactAlignment.width).toBeGreaterThanOrEqual(72);
    expect(compactAlignment.borderRight).toBe(1);
    expect(Math.abs(compactAlignment.mark - compactAlignment.axis)).toBeLessThanOrEqual(5);
    expect(Math.abs(compactAlignment.expand - compactAlignment.axis)).toBeLessThanOrEqual(5);
    expect(Math.abs(compactAlignment.plus - compactAlignment.axis)).toBeLessThanOrEqual(5);
    await navigation.getByRole('button', { name: 'Expand navigation' }).click();
    await page.waitForTimeout(60);
    const expansionWidth = await navigation.evaluate(
      (element) => element.getBoundingClientRect().width,
    );
    expect(expansionWidth).toBeLessThan(280);
    await expect(navigation).not.toHaveClass(/is-collapsed/);
    await expect(navigation).toHaveCSS('flex-basis', '280px');
    await expect(navigation.locator('.conversation-nav')).toBeVisible();
    await expect(navigation.locator('.conversation-link')).toBeVisible();
    await navigation.getByRole('button', { name: 'Preferences' }).click();
    await expect(navigation.getByRole('radio', { name: 'Dark' })).toBeVisible();
    await expect(navigation.locator('.wordmark-text')).toBeVisible();
    await expect(navigation.locator('button.new-conversation').locator('.label')).toBeVisible();
    await expect(navigation.locator('.wordmark')).toHaveCSS('gap', '8.8px');
    await expect(navigation.locator('button.new-conversation')).toHaveCSS('gap', '8px');
    return;
  }
  await page.setViewportSize({ width: 1024, height: 768 });
  await page.goto('/conversation');
  await expectConnectDestinationGroup(navigation);
  await expect(navigation).toHaveCSS('flex-basis', '72px');
  await expect(navigation.locator('.product-nav')).toBeVisible();
  await expect(navigation.locator('.product-nav a')).toHaveCount(DESTINATION_LABELS.length);
  await expect(
    navigation.locator('.product-nav').getByRole('link', { name: /^Conversations$/i }),
  ).toBeVisible();
  await expect(
    navigation.locator('.product-nav').getByRole('link', { name: /^Agents$/i }),
  ).toBeVisible();
  await expect(
    navigation.locator('.product-nav').getByRole('link', { name: /^Personas$/i }),
  ).toBeVisible();
  await expect(
    navigation.locator('.product-nav').getByRole('link', { name: /^Memory$/i }),
  ).toBeVisible();
  await expect(
    navigation.locator('.product-nav').getByRole('link', { name: /^Documents$/i }),
  ).toBeVisible();
  await expectCompactDestinationControls(navigation.locator('.product-nav'));
  await expect(navigation.getByRole('button', { name: 'Preferences' })).toBeVisible();
  await expect(navigation.locator('button.new-conversation')).toBeVisible();
  await expect(navigation.locator('.conversation-nav')).toHaveCount(0);
  await navigation.getByRole('button', { name: 'Conversation library' }).click();
  await expect(page.getByRole('dialog', { name: /conversation library/i })).toBeVisible();
  await expect(navigation.locator('button.new-conversation')).toHaveAttribute(
    'title',
    'New conversation',
  );
  await expect(page.locator('.conversation-link')).toHaveAttribute(
    'title',
    'A thoughtful beginning',
  );
  const geometry = await page.evaluate(() => {
    const conversationNav = document.querySelector<HTMLElement>(
      '.conversation-library-context .conversation-nav',
    );
    const conversationLink = document.querySelector<HTMLElement>(
      '.conversation-library-context .conversation-link',
    );
    return {
      navigationWidth:
        document.querySelector<HTMLElement>('#primary-navigation')?.getBoundingClientRect().width ??
        0,
      conversationNavWidth: conversationNav?.getBoundingClientRect().width ?? 0,
      conversationLinkWidth: conversationLink?.getBoundingClientRect().width ?? 0,
    };
  });
  expect(geometry.conversationNavWidth).toBeGreaterThanOrEqual(geometry.navigationWidth - 25);
  expect(geometry.conversationLinkWidth).toBeGreaterThan(40);
  expect(geometry.conversationLinkWidth).toBeLessThanOrEqual(geometry.conversationNavWidth);
});

test('keeps compact destination controls fixed at breakpoint boundaries during live resize', async ({
  page,
}, testInfo) => {
  test.skip(
    testInfo.project.name !== 'desktop',
    'Breakpoint geometry runs once in the desktop project.',
  );

  const navigation = page.locator('#primary-navigation');
  const productNav = navigation.locator('.product-nav');
  const destinationLinks = productNav.getByRole('link');

  async function waitForViewport(width: number): Promise<void> {
    await page.setViewportSize({ width, height: 900 });
    await expect.poll(() => page.evaluate(() => window.innerWidth)).toBe(width);
  }

  async function measureGeometry(): Promise<{
    newConversation: { top: number; bottom: number; width: number; height: number };
    productNav: { top: number; bottom: number; width: number; height: number };
    links: Array<{
      top: number;
      bottom: number;
      left: number;
      right: number;
      width: number;
      height: number;
    }>;
  }> {
    return page.evaluate(() => {
      const rect = (
        element: Element | null,
      ): {
        top: number;
        bottom: number;
        left: number;
        right: number;
        width: number;
        height: number;
      } => {
        const bounds = element?.getBoundingClientRect();
        return {
          top: bounds?.top ?? 0,
          bottom: bounds?.bottom ?? 0,
          left: bounds?.left ?? 0,
          right: bounds?.right ?? 0,
          width: bounds?.width ?? 0,
          height: bounds?.height ?? 0,
        };
      };
      return {
        newConversation: rect(document.querySelector('button.new-conversation')),
        productNav: rect(document.querySelector('.product-nav')),
        links: Array.from(document.querySelectorAll<HTMLElement>('.product-nav a')).map((link) =>
          rect(link),
        ),
      };
    });
  }

  async function expectDestinationGroup(options: {
    compact: boolean;
    mobile: boolean;
  }): Promise<void> {
    await expect(destinationLinks).toHaveCount(DESTINATION_LABELS.length);
    const geometry = await measureGeometry();
    expect(geometry.productNav.top).toBeGreaterThanOrEqual(geometry.newConversation.bottom - 1);
    expect(geometry.productNav.top - geometry.newConversation.bottom).toBeLessThanOrEqual(24);
    expect(geometry.links.every((link) => link.width > 0 && link.height > 0)).toBe(true);
    expect(
      geometry.links.every((link, index) =>
        geometry.links
          .slice(index + 1)
          .every(
            (other) =>
              link.right <= other.left + 1 ||
              other.right <= link.left + 1 ||
              link.bottom <= other.top + 1 ||
              other.bottom <= link.top + 1,
          ),
      ),
    ).toBe(true);

    if (options.compact)
      expect(geometry.links.every((link) => link.width >= 44 && link.height >= 44)).toBe(true);
    else expect(geometry.links.every((link) => link.width > 200 && link.height >= 44)).toBe(true);
  }

  await page.goto('/conversation');

  await waitForViewport(720);
    await mobileNavigationTrigger(page).click();
  await expect(navigation).toBeVisible();
  await expectDestinationGroup({ compact: false, mobile: true });

  await waitForViewport(721);
  await expect(navigation).toBeVisible();
  await expectDestinationGroup({ compact: true, mobile: false });
  await expect(navigation.getByRole('button', { name: 'Conversation library' })).toBeVisible();

  await waitForViewport(1279);
  await expectDestinationGroup({ compact: true, mobile: false });
  await expect(navigation.getByRole('button', { name: 'Conversation library' })).toBeVisible();

  await waitForViewport(1280);
  await expectDestinationGroup({ compact: false, mobile: false });
  await expect(navigation.locator('.conversation-nav')).toBeVisible();

  await waitForViewport(1440);
  await navigation.getByRole('button', { name: 'Collapse navigation' }).click();
  await expect(navigation).toHaveClass(/is-collapsed/);
  await expect.poll(() => productNav.isVisible()).toBe(true);
  await expectDestinationGroup({ compact: true, mobile: false });
  await expect(navigation.getByRole('button', { name: 'Conversation library' })).toBeVisible();
});

test('closes the tablet conversation library before entering the wide desktop layout', async ({
  page,
}, testInfo) => {
  test.skip(
    testInfo.project.name !== 'desktop',
    'Breakpoint overlay coverage runs once in the desktop project.',
  );
  await page.setViewportSize({ width: 1024, height: 768 });
  await page.goto('/conversation/welcome');
  const navigation = page.locator('#primary-navigation');
  await navigation.getByRole('button', { name: 'Conversation library' }).click();
  await expect(page.getByRole('dialog', { name: /conversation library/i })).toBeVisible();

  await page.setViewportSize({ width: 1280, height: 768 });
  await expect.poll(() => page.evaluate(() => window.innerWidth)).toBe(1280);
  await expect(page.getByRole('dialog', { name: /conversation library/i })).toBeHidden();
  await expect(navigation.locator('.conversation-nav')).toBeVisible();
  await expect(page.locator('.conversation-nav')).toHaveCount(1);
  await expect(page.locator('[aria-controls="conversation-filters"]')).toHaveCount(1);
  await expect(navigation.locator('.product-link[aria-current="page"]')).toBeFocused();
});

test('closes the collapsed desktop conversation library before expanding inline', async ({
  page,
}, testInfo) => {
  test.skip(
    testInfo.project.name !== 'desktop',
    'Collapsed-to-expanded overlay coverage runs once in the desktop project.',
  );
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto('/conversation/welcome');
  const navigation = page.locator('#primary-navigation');
  await navigation.getByRole('button', { name: 'Collapse navigation' }).click();
  await expect(navigation).toHaveClass(/is-collapsed/);
  await navigation.getByRole('button', { name: 'Conversation library' }).click();
  await expect(page.getByRole('dialog', { name: /conversation library/i })).toBeVisible();

  await navigation.getByRole('button', { name: 'Expand navigation' }).click();
  await expect(navigation).not.toHaveClass(/is-collapsed/);
  await expect(page.getByRole('dialog', { name: /conversation library/i })).toBeHidden();
  await expect(navigation.locator('.conversation-nav')).toBeVisible();
  await expect(page.locator('.conversation-nav')).toHaveCount(1);
  await expect(page.locator('[aria-controls="conversation-filters"]')).toHaveCount(1);
  await expect(navigation.locator('.product-link[aria-current="page"]')).toBeFocused();
});

test('keeps the expanded sidebar scroll bounded while resizing', async ({
  page,
}, testInfo) => {
  test.skip(
    testInfo.project.name !== 'desktop',
    'Dynamic sidebar scroll ownership runs once in the desktop project.',
  );

  const navigation = page.locator('#primary-navigation');
  await page.goto('/conversation/welcome?fixture=long-sidebar');

  for (const width of [1280, 1279, 721]) {
    await page.setViewportSize({ width, height: 900 });
    await expect.poll(() => page.evaluate(() => window.innerWidth)).toBe(width);
    if (width === 1280) await expect(navigation.locator('.conversation-nav')).toBeVisible();
    else {
      const libraryTrigger = navigation.getByRole('button', { name: 'Conversation library' });
      await libraryTrigger.click();
      await expect(page.getByRole('dialog', { name: /conversation library/i })).toBeVisible();
    }

    const metrics = await page.evaluate(() => {
      const sidebar = document.querySelector<HTMLElement>('#primary-navigation');
      const recent = document.querySelector<HTMLElement>('.conversation-nav');
      const theme = document.querySelector<HTMLElement>('.sidebar-bottom');
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
      const recentBounds = recent?.getBoundingClientRect();
      const themeBounds = theme?.getBoundingClientRect();
      const contextBounds = document
        .querySelector<HTMLElement>('.conversation-library-context')
        ?.getBoundingClientRect();
      return {
        scrollOwners,
        recentOverflow: recent ? getComputedStyle(recent).overflowY : 'visible',
        recentScrollable: (recent?.scrollHeight ?? 0) > (recent?.clientHeight ?? 0),
        sidebarHeight: sidebarBounds?.height ?? 0,
        viewportHeight: window.innerHeight,
        themeBottom: themeBounds?.bottom ?? 0,
        recentBottom: recentBounds?.bottom ?? 0,
        contextBottom: contextBounds?.bottom ?? 0,
      };
    });

    if (width === 1280) {
      expect(metrics.scrollOwners).toContain('sidebar-scroll-region');
      expect(metrics.scrollOwners).not.toContain('conversation-nav');
      expect(metrics.recentOverflow).toBe('visible');
    } else {
      expect(['auto', 'scroll']).toContain(metrics.recentOverflow);
      expect(metrics.recentScrollable).toBe(true);
    }
    expect(metrics.sidebarHeight).toBeLessThanOrEqual(metrics.viewportHeight + 1);
    if (width === 1280) {
      expect(metrics.themeBottom).toBeLessThanOrEqual(metrics.viewportHeight + 1);
      expect(metrics.themeBottom).toBeGreaterThanOrEqual(metrics.recentBottom - 1);
    } else {
      expect(metrics.contextBottom).toBeGreaterThan(0);
      expect(metrics.contextBottom).toBeLessThanOrEqual(metrics.viewportHeight + 1);
    }
  }
});

test('bounds the expanded 1280x720 navigation above Preferences', async ({ page }, testInfo) => {
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
    const recent = document.querySelector<HTMLElement>('.conversation-nav');
    const preferences = document.querySelector<HTMLElement>('.sidebar-bottom');
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
    const recentBounds = recent?.getBoundingClientRect();
    const preferencesBounds = preferences?.getBoundingClientRect();
    return {
      scrollOwners,
      recentOverflow: recent ? getComputedStyle(recent).overflowY : 'visible',
      recentScrollable: (recent?.scrollHeight ?? 0) > (recent?.clientHeight ?? 0),
      sidebarBottom: sidebarBounds?.bottom ?? 0,
      recentBottom: recentBounds?.bottom ?? 0,
      preferencesTop: preferencesBounds?.top ?? 0,
      viewportHeight: window.innerHeight,
    };
  });

  expect(metrics.scrollOwners).toEqual(['sidebar-scroll-region']);
  expect(metrics.recentOverflow).toBe('visible');
  expect(metrics.recentScrollable).toBe(true);
  expect(metrics.sidebarBottom).toBeLessThanOrEqual(metrics.viewportHeight + 1);
  expect(metrics.recentBottom).toBeLessThanOrEqual(metrics.preferencesTop + 1);
});

test('keeps expanded preferences visible at a short desktop viewport', async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== 'desktop', 'Desktop sidebar height coverage runs once.');
  await page.setViewportSize({ width: 1280, height: 720 });
  await page.goto('/conversation/welcome?fixture=long-sidebar');
  const navigation = page.locator('#primary-navigation');
  await navigation.getByRole('button', { name: 'Preferences' }).click();
  await expect(navigation.getByRole('radio', { name: 'Dark' })).toBeVisible();
  const geometry = await page.evaluate(() => {
    const rect = (selector: string): DOMRect | null => document.querySelector<HTMLElement>(selector)?.getBoundingClientRect() ?? null;
    const sidebar = rect('#primary-navigation');
    const scrollRegion = rect('.sidebar-scroll-region');
    const preferences = rect('.sidebar-bottom');
    return { viewportHeight: window.innerHeight, sidebar, scrollRegion, preferences };
  });
  expect(geometry.sidebar?.bottom ?? 0).toBeLessThanOrEqual(geometry.viewportHeight + 1);
  expect(geometry.preferences?.bottom ?? 0).toBeLessThanOrEqual(geometry.viewportHeight + 1);
  expect(geometry.scrollRegion?.bottom ?? 0).toBeLessThanOrEqual((geometry.preferences?.top ?? 0) + 1);
});

test('keeps primary destinations structurally separate from recent conversations', async ({
  page,
}, testInfo) => {
  test.skip(
    testInfo.project.name !== 'desktop',
    'Collapsed destination accessibility runs once in the desktop project.',
  );
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto('/conversation');
  const navigation = page.locator('#primary-navigation');
  const productNav = navigation.locator('.product-nav');
  const recentNav = navigation.locator('.conversation-nav');

  await expect(productNav).toBeVisible();
  await expectConnectDestinationGroup(navigation);
  await expect(productNav.getByRole('link')).toHaveCount(DESTINATION_LABELS.length);
  await expect(productNav.getByRole('link', { name: /^Conversations$/i })).toBeVisible();
  await expect(productNav.getByRole('link', { name: /^Agents$/i })).toBeVisible();
  await expect(productNav.getByRole('link', { name: /^Personas$/i })).toBeVisible();
  await expect(productNav.getByRole('link', { name: /^Memory$/i })).toBeVisible();
  await expect(productNav.getByRole('link', { name: /^Documents$/i })).toBeVisible();
  await expectExpandedDestinationTiles(productNav);
  await expectDestinationPresentation(productNav);
  const conversationsDestination = productNav.getByRole('link', { name: /^Conversations$/i });
  await conversationsDestination.focus();
  await page.keyboard.press('Tab');
  await page.keyboard.press('Shift+Tab');
  const focusPresentation = await conversationsDestination.evaluate((link) => {
    const style = getComputedStyle(link);
    return {
      focusVisible: link.matches(':focus-visible'),
      outlineStyle: style.outlineStyle,
      outlineWidth: style.outlineWidth,
    };
  });
  expect(focusPresentation.focusVisible).toBe(true);
  expect(focusPresentation.outlineStyle).not.toBe('none');
  expect(focusPresentation.outlineWidth).not.toBe('0px');
  await expect(recentNav).toBeVisible();
  await expect(recentNav.locator('.conversation-link').first()).toBeVisible();
  await expect(productNav.locator('a.conversation-link')).toHaveCount(0);

  await navigation.getByRole('button', { name: 'Collapse navigation' }).click();
  await expect(navigation).toHaveClass(/is-collapsed/);
  await page.waitForTimeout(220);
  await expect(productNav).toBeVisible();
  await expect(productNav.getByRole('link', { name: /^Conversations$/i })).toBeVisible();
  await expect(productNav.getByRole('link', { name: /^Agents$/i })).toBeVisible();
  await expect(productNav.getByRole('link', { name: /^Personas$/i })).toBeVisible();
  await expect(productNav.getByRole('link', { name: /^Memory$/i })).toBeVisible();
  await expect(productNav.getByRole('link', { name: /^Documents$/i })).toBeVisible();
  await expect(productNav.getByRole('link')).toHaveCount(DESTINATION_LABELS.length);
  await expectCompactDestinationControls(productNav);
});

test('keeps primary destinations available in the mobile drawer', async ({ page }, testInfo) => {
  test.skip(
    testInfo.project.name !== 'mobile',
    'Mobile drawer navigation runs once in the mobile project.',
  );
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/conversation');
  const navigation = page.getByRole('navigation', { name: 'Primary navigation' });
  await mobileNavigationTrigger(page).click();
  await expect(navigation).toHaveClass(/is-mobile-open/);
  await page.waitForTimeout(220);

  const productNav = navigation.locator('.product-nav');
  const recentNav = navigation.locator('.conversation-nav');
  await expect(productNav).toBeVisible();
  await expectConnectDestinationGroup(navigation);
  await expect(productNav.getByRole('link', { name: /^Conversations$/i })).toBeVisible();
  await expect(productNav.getByRole('link', { name: /^Agents$/i })).toBeVisible();
  await expect(productNav.getByRole('link', { name: /^Personas$/i })).toBeVisible();
  await expect(productNav.getByRole('link', { name: /^Memory$/i })).toBeVisible();
  await expect(productNav.getByRole('link', { name: /^Documents$/i })).toBeVisible();
  await expectExpandedDestinationTiles(productNav);
  await expectDestinationPresentation(productNav);
  await expect
    .poll(async () =>
      productNav
        .locator('a .product-current-marker')
        .evaluateAll(
          (markers) =>
            markers.filter((marker) => getComputedStyle(marker).display !== 'none').length,
        ),
    )
    .toBe(1);
  await expect(recentNav).toBeVisible();
  await expect(recentNav.locator('.conversation-link').first()).toBeVisible();
});

test('keeps the sidebar viewport-stable while long conversation content scrolls', async ({
  page,
}, testInfo) => {
  test.skip(
    testInfo.project.name !== 'desktop' && testInfo.project.name !== 'tablet',
    'Viewport-stable sidebar coverage runs on desktop and tablet projects.',
  );
  await page.goto('/conversation/welcome?fixture=long-sidebar');
  const navigation = page.locator('#primary-navigation');
  if (testInfo.project.name === 'tablet') {
    await expect(navigation.getByRole('button', { name: 'Conversation library' })).toHaveCount(1);
    await navigation.getByRole('button', { name: 'Conversation library' }).click();
    await expect(page.getByRole('dialog', { name: /conversation library/i })).toBeVisible();
  }
  const recentNav =
    testInfo.project.name === 'tablet'
      ? page.locator('.conversation-library-context .conversation-nav')
      : navigation.locator('.conversation-nav');
  const mainContent = page.locator('#main-content');
  await expect(recentNav.locator('.conversation-link')).toHaveCount(48);
  await expect(page.getByText('Long conversation fixture message 79.')).toBeVisible();

  const recentSelector =
    testInfo.project.name === 'tablet'
      ? '.conversation-library-context .conversation-nav'
      : '.conversation-nav';
  const beforeScroll = await page.evaluate((selector) => {
    const sidebar = document.querySelector<HTMLElement>('#primary-navigation');
    const main = document.querySelector<HTMLElement>('#main-content');
    const recent = document.querySelector<HTMLElement>(selector);
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
  }, recentSelector);

  expect(beforeScroll.sidebar.top).toBeGreaterThanOrEqual(-1);
  expect(beforeScroll.sidebar.bottom).toBeLessThanOrEqual(beforeScroll.viewportHeight + 1);
  expect(beforeScroll.sidebar.height).toBeLessThanOrEqual(beforeScroll.viewportHeight + 1);
  expect(beforeScroll.documentScrollHeight).toBeLessThanOrEqual(beforeScroll.viewportHeight + 1);
  expect(beforeScroll.bodyScrollHeight).toBeLessThanOrEqual(beforeScroll.viewportHeight + 1);
  expect(beforeScroll.mainScrollHeight).toBeGreaterThan(beforeScroll.mainClientHeight);
  expect(beforeScroll.recentScrollHeight).toBeGreaterThan(beforeScroll.recentClientHeight);
  if (testInfo.project.name === 'desktop') {
    expect(beforeScroll.recentOverflowY).toBe('visible');
    expect(beforeScroll.sidebarScrollableDescendants).toEqual(['sidebar-scroll-region']);
  } else {
    expect(['auto', 'scroll']).toContain(beforeScroll.recentOverflowY);
    expect(beforeScroll.sidebarScrollableDescendants).toContain('sidebar-scroll-region');
  }

  await mainContent.evaluate((element) => {
    element.scrollTop = element.scrollHeight;
  });
  const recentScrollTarget = testInfo.project.name === 'desktop'
    ? navigation.locator('.sidebar-scroll-region')
    : recentNav;
  await recentScrollTarget.evaluate((element) => {
    element.scrollTop = element.scrollHeight;
  });
  const afterScroll = await page.evaluate((selector) => {
    const main = document.querySelector<HTMLElement>('#main-content');
    const recent = document.querySelector<HTMLElement>(selector);
    const bounds = (selector: string): { top: number; bottom: number } => {
      const rect = document.querySelector<HTMLElement>(selector)?.getBoundingClientRect();
      return { top: rect?.top ?? 0, bottom: rect?.bottom ?? 0 };
    };
    return {
      mainScrollTop: main?.scrollTop ?? 0,
      recentScrollTop: recent?.scrollTop ?? 0,
      sidebarScrollTop: document.querySelector<HTMLElement>('.sidebar-scroll-region')?.scrollTop ?? 0,
      sidebarTop: bounds('.sidebar-top'),
      productNav: bounds('.product-nav'),
      sidebarBottom: bounds('.sidebar-bottom'),
    };
  }, recentSelector);

  expect(afterScroll.mainScrollTop).toBeGreaterThan(0);
  expect(
    testInfo.project.name === 'desktop'
      ? afterScroll.sidebarScrollTop
      : afterScroll.recentScrollTop,
  ).toBeGreaterThan(0);
  expect(afterScroll.sidebarTop.top).toBeCloseTo(beforeScroll.sidebarTop.top, 0);
  expect(afterScroll.sidebarTop.bottom).toBeCloseTo(beforeScroll.sidebarTop.bottom, 0);
  if (testInfo.project.name === 'tablet') {
    expect(afterScroll.productNav.top).toBeCloseTo(beforeScroll.productNav.top, 0);
    expect(afterScroll.productNav.bottom).toBeCloseTo(beforeScroll.productNav.bottom, 0);
  }
  expect(afterScroll.sidebarBottom.top).toBeCloseTo(beforeScroll.sidebarBottom.top, 0);
  expect(afterScroll.sidebarBottom.bottom).toBeCloseTo(beforeScroll.sidebarBottom.bottom, 0);
});

test('scrolls the desktop main canvas when the pointer is over conversation content', async ({
  page,
}, testInfo) => {
  test.skip(
    testInfo.project.name !== 'desktop',
    'Desktop wheel ownership runs once in the desktop project.',
  );
  await page.goto('/conversation/welcome?fixture=long-sidebar');
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
      .filter(
        (element) =>
          ['auto', 'scroll'].includes(getComputedStyle(element).overflowY) &&
          element.scrollHeight > element.clientHeight,
      )
      .map((element) => element.id || element.className);
  });
  expect(ownersBeforeWheel).toEqual(['main-content']);
  await expect(mainContent).toHaveCSS('overflow-y', 'auto');
  await expect(transcript).not.toHaveCSS('overflow-y', 'auto');
  await expect(transcript).not.toHaveCSS('overflow-y', 'scroll');
  await expect(composer).toHaveCSS('position', 'sticky');

  await transcript.hover({ position: { x: 12, y: 120 } });
  await page.mouse.wheel(0, 900);
  await expect
    .poll(async () => mainContent.evaluate((element) => element.scrollTop))
    .toBeGreaterThan(0);
  await expect.poll(async () => transcript.evaluate((element) => element.scrollTop)).toBe(0);

  await mainContent.evaluate((element) => {
    element.scrollTop = 0;
  });
  await mainContent.focus();
  await expect(mainContent).toBeFocused();
  await page.keyboard.press('PageDown');
  await expect
    .poll(async () => mainContent.evaluate((element) => element.scrollTop))
    .toBeGreaterThan(0);
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
  await expect(page.getByRole('button', { name: 'Send' })).toBeVisible();
});

test('keeps mobile conversation scrolling contained in the conversation panel', async ({
  page,
}, testInfo) => {
  test.skip(
    testInfo.project.name !== 'mobile',
    'Mobile contained scrolling runs once in the mobile project.',
  );
  await page.goto('/conversation/welcome?fixture=long-sidebar');
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
      conversationOverflowY: conversationElement
        ? getComputedStyle(conversationElement).overflowY
        : 'visible',
      conversationClientHeight: conversationElement?.clientHeight ?? 0,
      conversationScrollHeight: conversationElement?.scrollHeight ?? 0,
      mainScrollTop: main?.scrollTop ?? 0,
      documentScrollHeight: document.documentElement.scrollHeight,
      documentClientHeight: document.documentElement.clientHeight,
      owners: candidates
        .filter((element): element is HTMLElement => element !== null)
        .filter(
          (element) =>
            ['auto', 'scroll'].includes(getComputedStyle(element).overflowY) &&
            element.scrollHeight > element.clientHeight,
        )
        .map((element) => element.className || element.id),
    };
  });
  expect(['auto', 'scroll']).toContain(metrics.conversationOverflowY);
  if (metrics.conversationScrollHeight === metrics.conversationClientHeight) {
    // Chromium's mobile viewport lays out the conversation as the document's
    // scroll surface; retain the ownership check without assuming WebKit's
    // nested-flex sizing behavior.
    expect(metrics.documentScrollHeight).toBeGreaterThan(metrics.documentClientHeight);
    expect(metrics.owners).toEqual([]);
    await transcript.hover({ position: { x: 12, y: 120 } });
    await page.mouse.wheel(0, 900);
    await expect.poll(async () => page.evaluate(() => window.scrollY)).toBeGreaterThan(0);
    await expect.poll(async () => mainContent.evaluate((element) => element.scrollTop)).toBe(0);
    return;
  }
  expect(metrics.conversationScrollHeight).toBeGreaterThan(metrics.conversationClientHeight);
  expect(metrics.owners).toEqual(['conversation']);
  expect(metrics.mainScrollTop).toBe(0);
  await transcript.hover({ position: { x: 12, y: 120 } });
  await page.mouse.wheel(0, 900);
  await expect
    .poll(async () => conversation.evaluate((element) => element.scrollTop))
    .toBeGreaterThan(0);
  await expect.poll(async () => mainContent.evaluate((element) => element.scrollTop)).toBe(0);

  await conversation.evaluate((element) => {
    element.scrollTop = 0;
  });
  const conversationBounds = await conversation.boundingBox();
  if (!conversationBounds)
    throw new Error('Conversation panel is not measurable for touch scrolling.');
  const touchX = conversationBounds.x + Math.min(160, conversationBounds.width / 2);
  const touchStartY =
    conversationBounds.y + Math.min(280, Math.max(32, conversationBounds.height - 32));
  const touchClient = await page.context().newCDPSession(page);
  try {
    await touchClient.send('Input.dispatchTouchEvent', {
      type: 'touchStart',
      touchPoints: [{ x: touchX, y: touchStartY }],
    });
    for (const offset of [100, 200, 300, 400]) {
      await touchClient.send('Input.dispatchTouchEvent', {
        type: 'touchMove',
        touchPoints: [{ x: touchX, y: touchStartY - offset }],
      });
    }
    await touchClient.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
  } finally {
    await touchClient.detach();
  }
  await expect
    .poll(async () => conversation.evaluate((element) => element.scrollTop))
    .toBeGreaterThan(0);
  await expect.poll(async () => mainContent.evaluate((element) => element.scrollTop)).toBe(0);
  await expect(page.getByRole('textbox', { name: 'Message Aura' })).toBeVisible();
});

test('selects a recent conversation in one click from agent and persona routes', async ({
  page,
}, testInfo) => {
  for (const route of ['/agents', '/personas']) {
    await page.goto(route);
    await navigateToConversationContext(page, testInfo);
    const recentConversation = page
      .locator('.conversation-link:visible')
      .filter({ hasText: 'A thoughtful beginning' });
    await expect(recentConversation).toBeVisible();
    await recentConversation.click();
    await expect(page).toHaveURL(/\/conversation\/welcome$/);
    await expect(page.getByRole('heading', { name: 'A thoughtful beginning' })).toBeVisible();
  }
});

test('keeps selection order stable and promotes only after an accepted message', async ({
  page,
}, testInfo) => {
  await page.goto('/conversation?fixture=ordering');
  await openConversationLibrary(page, testInfo);

  async function recentTitles(): Promise<string[]> {
    await openConversationLibrary(page, testInfo);
    const persistedLinks = page.locator(
      '.conversation-link:visible[href*="/conversation/first"], .conversation-link:visible[href*="/conversation/second"]',
    );
    await expect(persistedLinks).toHaveCount(2);
    return persistedLinks.locator('.label').allTextContents();
  }

  const initialOrder = await recentTitles();
  expect(initialOrder).toEqual(['Second topic', 'First topic']);

  await page.locator('.conversation-link:visible').filter({ hasText: 'Second topic' }).click();
  await expect(page).toHaveURL(/\/conversation\/second$/);
  await expect(page.getByRole('heading', { name: 'Second topic' })).toBeVisible();
  expect(await recentTitles()).toEqual(initialOrder);

  await page.locator('.conversation-link:visible').filter({ hasText: 'First topic' }).click();
  await expect(page).toHaveURL(/\/conversation\/first$/);
  await expect(page.getByRole('heading', { name: 'First topic' })).toBeVisible();
  const composer = page.getByRole('textbox', { name: 'Message Aura' });
  await composer.fill('Accepted activity advances this conversation.');
  await page.getByRole('button', { name: 'Send' }).click();
  await expect.poll(recentTitles).toEqual(['First topic', 'Second topic']);
});

test('supports direct conversation links, reload, and browser history', async ({
  page,
}, testInfo) => {
  await page.goto('/conversation/welcome');
  await expect(page).toHaveURL(/\/conversation\/welcome$/);
  await expect(page.getByRole('heading', { name: 'A thoughtful beginning' })).toBeVisible();
  if (testInfo.project.name === 'mobile')
    await mobileNavigationTrigger(page).click();
  await expect(
    page.locator('.product-nav').getByRole('link', { name: /^Conversations$/i }),
  ).toHaveAttribute('aria-current', 'page');

  await page.reload();
  await expect(page).toHaveURL(/\/conversation\/welcome$/);
  await expect(page.getByRole('heading', { name: 'A thoughtful beginning' })).toBeVisible();
  if (testInfo.project.name === 'mobile')
    await mobileNavigationTrigger(page).click();

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
    if (testInfo.project.name === 'mobile')
    await mobileNavigationTrigger(page).click();
    await page.getByRole('button', { name: /new conversation/i }).click();
    if (testInfo.project.name === 'mobile') await waitForMobileDrawer(page, 'closed');
    await expect(page).toHaveURL(/\/conversation$/);
    await expect(page.getByRole('textbox', { name: 'Message Aura' })).toBeVisible();
  }
});

test('replaces a new conversation URL with its persisted identifier after acceptance', async ({
  page,
}, testInfo) => {
  await page.goto('/agents');
  if (testInfo.project.name === 'mobile')
    await mobileNavigationTrigger(page).click();
  await page.getByRole('button', { name: /new conversation/i }).click();
  if (testInfo.project.name === 'mobile') await waitForMobileDrawer(page, 'closed');
  await waitForConversationReady(page);
  const composer = page.getByRole('textbox', { name: 'Message Aura' });
  await composer.fill('Persist this new conversation.');
  await page.getByRole('button', { name: 'Send' }).click();
  await expect(page).toHaveURL(/\/conversation\/created-1$/);
  await expect(page.getByRole('article', { name: 'Your message' })).toContainText(
    'Persist this new conversation.',
  );
});

test('keeps the conversation destination active on detail routes and usable in collapsed/mobile navigation', async ({
  page,
}, testInfo) => {
  test.skip(
    testInfo.project.name !== 'desktop' && testInfo.project.name !== 'mobile',
    'Detail-route navigation coverage runs on desktop and mobile projects.',
  );
  await page.goto('/conversation/welcome');
  const navigation = page.getByRole('navigation', { name: 'Primary navigation' });
  if (testInfo.project.name === 'mobile')
    await mobileNavigationTrigger(page).click();
  const conversationsLink = navigation
    .locator('.product-nav')
    .getByRole('link', { name: /^Conversations$/i });
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
    await mobileNavigationTrigger(page).click();
  const mobileConversationsLink = page
    .getByRole('navigation', { name: 'Primary navigation' })
    .locator('.product-nav')
    .getByRole('link', { name: /^Conversations$/i });
  await expect(mobileConversationsLink).toBeVisible();
  await mobileConversationsLink.click();
  await expect(page).toHaveURL(/\/conversation$/);
});

test('applies the latest deep-link selection when an earlier route lookup resolves late', async ({
  page,
}) => {
  await page.goto('/conversation/route-one?fixture=route-race');
  await expect
    .poll(async () =>
      page.evaluate(() =>
        fetch('/api/v1/test/state?conversation=route-one')
          .then((response) => response.json())
          .then((state: { pending: boolean }) => state.pending),
      ),
    )
    .toBe(true);
  await page.evaluate(() => {
    window.history.pushState({}, '', '/conversation/route-two?fixture=route-race');
    window.dispatchEvent(new PopStateEvent('popstate'));
  });
  await expect(page).toHaveURL(/\/conversation\/route-two\?fixture=route-race$/);
  await expect(page.getByText('Route two response.')).toBeVisible();

  await page.evaluate(() => {
    void fetch('/api/v1/test/release?conversation=route-one');
  });
  await expect(page).toHaveURL(/\/conversation\/route-two\?fixture=route-race$/);
  await expect(page.getByText('Route two response.')).toBeVisible();
  await expect(page.getByText('Route one response.')).toHaveCount(0);
});

test('keeps the latest routed conversation selected while a prior draft acceptance resolves', async ({
  page,
}, testInfo) => {
  await page.goto('/conversation?fixture=deferred-create');
  const composer = page.getByRole('textbox', { name: 'Message Aura' });
  await expect(page.locator('.composer-wrap')).toHaveAttribute('data-conversation-ready', 'true');
  await composer.fill('Background acceptance should not steal selection.');
  const send = page.getByRole('button', { name: 'Send' });
  await expect(send).toBeEnabled({ timeout: 15_000 });
  await send.click();
  await expect
    .poll(async () =>
      page.evaluate(() =>
        fetch('/api/v1/test/state?conversation=create')
          .then((response) => response.json())
          .then((state: { pending: boolean }) => state.pending),
      ),
    )
    .toBe(true);

  await page.evaluate(() => {
    window.history.pushState({}, '', '/conversation/welcome?fixture=deferred-create');
    window.dispatchEvent(new PopStateEvent('popstate'));
  });
  await expect(page).toHaveURL(/\/conversation\/welcome\?fixture=deferred-create$/);
  await openConversationLibrary(page, testInfo);
  const welcomeLink = page
    .locator('.conversation-link')
    .filter({ hasText: 'A thoughtful beginning' });
  await page.evaluate(() => fetch('/api/v1/test/release?conversation=create'));
  await expect
    .poll(async () =>
      page.evaluate(() =>
        fetch('/api/v1/test/state?conversation=create')
          .then((response) => response.json())
          .then((state: { pending: boolean }) => state.pending),
      ),
    )
    .toBe(false);
  if (testInfo.project.name === 'mobile') {
    await settleMobileDrawer(page, 'open');
  }
  await expect(welcomeLink).toHaveAttribute('title', 'A thoughtful beginning');
  await expect(page).toHaveURL(/\/conversation\/welcome\?fixture=deferred-create$/);
  await expect(page.locator('.conversation-link[href="/conversation/created-1"]')).toBeVisible();
});

test('keeps a new conversation out of the library until its first accepted message', async ({
  page,
}, testInfo) => {
  await page.goto('/conversation');
  await openConversationLibrary(page, testInfo);
  await page.getByRole('button', { name: /new conversation/i }).click();
  if (testInfo.project.name === 'mobile') await waitForMobileDrawer(page, 'closed');
  const composer = page.getByRole('textbox', { name: 'Message Aura' });
  await composer.fill('Persist this routed draft.');

  // New conversation is idempotent while the local draft is unsent. Reopening
  // it must preserve the text and must not create a second hidden library row.
  if (testInfo.project.name === 'mobile')
    await mobileNavigationTrigger(page).click();
  await page.getByRole('button', { name: /new conversation/i }).click();
  if (testInfo.project.name === 'mobile') await waitForMobileDrawer(page, 'closed');
  await expect(composer).toHaveValue('Persist this routed draft.');
  await expect(
    page.locator('.conversation-link').filter({ hasText: 'New conversation' }),
  ).toHaveCount(0);

  await page.getByRole('button', { name: 'Send' }).click();
  await expect(page).toHaveURL(/\/conversation\/created-1$/);
  await openConversationLibrary(page, testInfo);
  const durableRows = page.locator('.conversation-link[href="/conversation/created-1"]');
  await expect(durableRows).toHaveCount(1);
  await page.reload();
  await expect(page).toHaveURL(/\/conversation\/created-1$/);
  await expect(
    page
      .getByRole('article', { name: 'Your message' })
      .getByText('Persist this routed draft.', { exact: true }),
  ).toBeVisible();
});

test('keeps a rejected first send in the local draft and outside the library', async ({
  page,
}, testInfo) => {
  await page.goto('/conversation?fixture=rejected-create');
  if (testInfo.project.name === 'mobile')
    await mobileNavigationTrigger(page).click();
  await page.getByRole('button', { name: /new conversation/i }).click();
  if (testInfo.project.name === 'mobile') await waitForMobileDrawer(page, 'closed');
  const composer = page.getByRole('textbox', { name: 'Message Aura' });
  await composer.fill('Keep this after the rejected first send.');
  const rejectedCreate = page.waitForResponse(
    (response) =>
      response.url().endsWith('/api/v1/conversations') && response.request().method() === 'POST',
  );
  await page.getByRole('button', { name: 'Send' }).click();
  expect((await rejectedCreate).status()).toBe(503);

  await expect(page.locator('#composer-notice')).toContainText(
    /temporarily unavailable|could not complete|try again/i,
  );
  await expect(composer).toHaveValue('Keep this after the rejected first send.');
  await expect(
    page.locator('.conversation-link').filter({ hasText: 'New conversation' }),
  ).toHaveCount(0);
  await expect(page.locator('.conversation-link[href^="/conversation/draft-"]')).toHaveCount(0);
});

test('preserves an unauthorized deep-link URL and surfaces the authentication state', async ({
  page,
}) => {
  await page.goto('/conversation/protected?fixture=route-401');
  await expect(page).toHaveURL(/\/conversation\/protected\?fixture=route-401$/);
  await expect(
    page.getByRole('status').getByRole('heading', { name: 'Sign in to continue' }),
  ).toBeVisible();
  await expect(page.getByRole('button', { name: /sign in with authentik/i })).toBeVisible();
});

test('preserves a failed deep-link URL and surfaces the service failure notice', async ({
  page,
}) => {
  await page.goto('/conversation/unavailable?fixture=route-503');
  await expect(page).toHaveURL(/\/conversation\/unavailable\?fixture=route-503$/);
  await expect(page.getByText(/temporarily unavailable|could not load|try again/i)).toBeVisible();
});

test('marks only the exact routed destination and conversation link as current', async ({
  page,
}, testInfo) => {
  const navigation = page.getByRole('navigation', { name: 'Primary navigation' });
  const product = (name: RegExp) => navigation.locator('.product-nav').getByRole('link', { name });
  const recent = navigation.locator('.conversation-link');
  const openMobileNavigation = async (): Promise<void> => {
    if (testInfo.project.name !== 'mobile') return;
    await mobileNavigationTrigger(page).click();
    await expect(navigation).toHaveClass(/is-mobile-open/);
    await page.waitForTimeout(220);
  };

  await page.goto('/agents');
  await openMobileNavigation();
  await expect(product(/^Agents$/i)).toHaveAttribute('aria-current', 'page');
  await expect(product(/^Conversations$/i)).not.toHaveAttribute('aria-current', 'page');
  await expect(product(/^Personas$/i)).not.toHaveAttribute('aria-current', 'page');
  await expect(product(/^Memory$/i)).not.toHaveAttribute('aria-current', 'page');
  await expect(recent).toHaveCount(0);

  await page.goto('/personas');
  await openMobileNavigation();
  await expect(product(/^Personas$/i)).toHaveAttribute('aria-current', 'page');
  await expect(product(/^Conversations$/i)).not.toHaveAttribute('aria-current', 'page');
  await expect(product(/^Agents$/i)).not.toHaveAttribute('aria-current', 'page');
  await expect(product(/^Memory$/i)).not.toHaveAttribute('aria-current', 'page');
  await expect(recent).toHaveCount(0);

  await page.goto('/documents');
  await openMobileNavigation();
  await expect(product(/^Documents$/i)).toHaveAttribute('aria-current', 'page');
  await expect(product(/^Conversations$/i)).not.toHaveAttribute('aria-current', 'page');
  await expect(product(/^Memory$/i)).not.toHaveAttribute('aria-current', 'page');
  await expect(recent).toHaveCount(0);

  await page.goto('/conversation');
  await openMobileNavigation();
  await openConversationLibrary(page, testInfo);
  await expect(product(/^Conversations$/i)).toHaveAttribute('aria-current', 'page');
  await expect(product(/^Agents$/i)).not.toHaveAttribute('aria-current', 'page');
  await expect(product(/^Personas$/i)).not.toHaveAttribute('aria-current', 'page');
  await expect(product(/^Memory$/i)).not.toHaveAttribute('aria-current', 'page');
  await expect(page.locator('.conversation-link[aria-current="page"]:visible')).toHaveCount(0);

  await page.goto('/memory');
  await openMobileNavigation();
  await expect(product(/^Memory$/i)).toHaveAttribute('aria-current', 'page');
  await expect(product(/^Conversations$/i)).not.toHaveAttribute('aria-current', 'page');
  await expect(product(/^Agents$/i)).not.toHaveAttribute('aria-current', 'page');
  await expect(product(/^Personas$/i)).not.toHaveAttribute('aria-current', 'page');
  await expect(recent).toHaveCount(0);

  await page.goto('/conversation/welcome');
  await openMobileNavigation();
  await expect(product(/^Conversations$/i)).toHaveAttribute('aria-current', 'page');
  await expect(product(/^Memory$/i)).not.toHaveAttribute('aria-current', 'page');
  await openConversationLibrary(page, testInfo);
  await expect(page.locator('.conversation-link[aria-current="page"]:visible')).toHaveCount(1);
});

test('recovers an invalid conversation route without discarding an unrelated draft', async ({
  page,
}, testInfo) => {
  await page.goto('/agents');
  if (testInfo.project.name === 'mobile')
    await mobileNavigationTrigger(page).click();
  await page.getByRole('button', { name: /new conversation/i }).click();
  if (testInfo.project.name === 'mobile') await waitForMobileDrawer(page, 'closed');
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
  await page.goto('/conversation');
  const composer = page.getByRole('textbox', { name: 'Message Aura' });
  await expect(page.locator('.composer-wrap')).toHaveAttribute('data-conversation-ready', 'true');
  await composer.fill('A small local thought');
  const send = page.getByRole('button', { name: 'Send' });
  await expect(send).toBeEnabled({ timeout: 15_000 });
  await send.click();
  await expect(
    page.getByRole('article', { name: 'Your message' }).getByText('A small local thought'),
  ).toBeVisible();
  const workingStatus = page.getByRole('status');
  await expect(workingStatus.getByRole('button', { name: 'Stop' })).toBeVisible();
  await workingStatus.getByRole('button', { name: 'Stop' }).click();
  await expect(page.getByText(/Generation stopped/)).toBeVisible();
  await expect(page.getByText('Generation interrupted')).toBeVisible();
});

test('renders a completed local reply', async ({ page }) => {
  await page.goto('/conversation');
  const composer = page.getByRole('textbox', { name: 'Message Aura' });
  await expect(page.locator('.composer-wrap')).toHaveAttribute('data-conversation-ready', 'true');
  await composer.fill('A completed local thought');
  const send = page.getByRole('button', { name: 'Send' });
  await expect(send).toBeEnabled({ timeout: 15_000 });
  await send.click();
  await expect(page.getByRole('article', { name: 'Aura response' }).last()).toContainText(
    'A completed local thought',
  );
});

test('navigates immutable conversation branches with keyboard controls and opaque URL state', async ({
  page,
}) => {
  const rootBranch = {
    id: 'root-branch',
    conversationId: 'welcome',
    parentBranchId: null,
    forkedFromMessageId: null,
    headMessageId: 'assistant-root',
    createdAt: '2026-01-01T00:00:00Z',
  };
  const alternativeBranch = {
    id: 'branch-alt',
    conversationId: 'welcome',
    parentBranchId: 'root-branch',
    forkedFromMessageId: 'user-root',
    headMessageId: 'assistant-alt',
    createdAt: '2026-01-01T00:01:00Z',
  };
  const rootMessages = [
    {
      id: 'user-root',
      conversationId: 'welcome',
      role: 'user',
      content: 'Original question',
      state: 'complete',
      runId: 'run-root',
      createdAt: '2026-01-01T00:00:00Z',
      updatedAt: '2026-01-01T00:00:00Z',
    },
    {
      id: 'assistant-root',
      conversationId: 'welcome',
      role: 'assistant',
      content: 'Original answer',
      state: 'complete',
      runId: 'run-root',
      createdAt: '2026-01-01T00:00:01Z',
      updatedAt: '2026-01-01T00:00:01Z',
    },
  ];
  const alternativeMessages = [
    { ...rootMessages[0], id: 'user-alt', content: 'Corrected question', runId: 'run-alt' },
    { ...rootMessages[1], id: 'assistant-alt', content: 'Alternative answer', runId: 'run-alt' },
  ];
  await page.route('**/api/v1/conversations/welcome', async (route) => {
    if (route.request().method() !== 'GET') return route.continue();
    return route.fulfill({
      json: {
        id: 'welcome',
        title: 'A thoughtful beginning',
        agentProfileId: 'general',
        agentRevisionId: 'agent-rev-1',
        modelId: 'qwen2.5:7b',
        version: 2,
        createdAt: '2026-01-01T00:00:00Z',
        updatedAt: '2026-01-01T00:01:00Z',
        messages: rootMessages,
        recentRuns: [],
        currentRun: null,
      },
    });
  });
  const branchDetail = {
    branch: alternativeBranch,
    messages: alternativeMessages,
    recentRuns: [
      {
        id: 'run-alt',
        conversationId: 'welcome',
        userMessageId: 'user-alt',
        assistantMessageId: 'assistant-alt',
        status: 'completed',
        agentRevisionId: 'agent-rev-1',
        modelPolicyRevisionId: 'policy-1',
        provider: 'ollama',
        modelId: 'qwen2.5:7b',
        retryOfRunId: null,
        branchId: 'branch-alt',
        regeneratedFromRunId: null,
        createdAt: '2026-01-01T00:02:00Z',
        startedAt: '2026-01-01T00:02:00Z',
        finishedAt: '2026-01-01T00:02:01Z',
        error: null,
      },
    ],
    currentRun: null,
  };
  let branchCreateAttempts = 0;
  await page.route(
    (url) => url.pathname === '/api/v1/conversations/welcome/branches',
    async (route) => {
      if (route.request().method() === 'POST') {
        branchCreateAttempts += 1;
        if (branchCreateAttempts === 1) {
          return route.fulfill({
            status: 409,
            contentType: 'application/json',
            body: JSON.stringify({ detail: 'Conversation version conflict' }),
          });
        }
        return route.fulfill({
          status: 202,
          json: {
            conversation: {
              id: 'welcome',
              title: 'A thoughtful beginning',
              agentProfileId: 'general',
              agentRevisionId: 'agent-rev-1',
              modelId: 'qwen2.5:7b',
              version: 3,
              createdAt: '2026-01-01T00:00:00Z',
              updatedAt: '2026-01-01T00:02:00Z',
            },
            branch: alternativeBranch,
            userMessage: alternativeMessages[0],
            run: {
              id: 'run-alt',
              conversationId: 'welcome',
              userMessageId: 'user-alt',
              assistantMessageId: 'assistant-alt',
              status: 'completed',
              agentRevisionId: 'agent-rev-1',
              modelPolicyRevisionId: 'policy-1',
              provider: 'ollama',
              modelId: 'qwen2.5:7b',
              retryOfRunId: null,
              branchId: 'branch-alt',
              regeneratedFromRunId: null,
              createdAt: '2026-01-01T00:02:00Z',
              startedAt: '2026-01-01T00:02:00Z',
              finishedAt: '2026-01-01T00:02:01Z',
              error: null,
            },
          },
        });
      }
      if (route.request().method() !== 'GET') return route.continue();
      return route.fulfill({ json: { items: [rootBranch, alternativeBranch], nextCursor: null } });
    },
  );
  await page.route('**/api/v1/conversations/welcome/branches/**', async (route) => {
    if (route.request().method() !== 'GET') return route.continue();
    const url = new URL(route.request().url());
    if (url.pathname.endsWith('/root-branch')) {
      return route.fulfill({
        json: { branch: rootBranch, messages: rootMessages, recentRuns: [], currentRun: null },
      });
    }
    return route.fulfill({ json: branchDetail });
  });
  // The successful branch detail is already terminal. Keep the generic fake
  // provider stream from emitting a completion event that would reconcile the
  // selected alternative against the legacy root projection.
  await page.route('**/api/v1/runs/run-alt/events**', async (route) =>
    route.fulfill({ status: 200, headers: { 'content-type': 'text/event-stream' }, body: 'retry: 86400000\n\n' }),
  );

  await page.goto('/conversation/welcome?fixture=branches&branch=branch-alt');
  await expect(page.getByText('Corrected question', { exact: true })).toBeVisible();
  await expect(page.getByText('Branch 2 of 2', { exact: true })).toBeVisible();
  await expect.poll(() => new URL(page.url()).searchParams.get('branch')).toBe('branch-alt');
  expect(new URL(page.url()).searchParams.get('fixture')).toBe('branches');

  const previous = page.getByRole('button', { name: 'Previous conversation branch' });
  const next = page.getByRole('button', { name: 'Next conversation branch' });
  await expect(previous).toBeEnabled();
  await expect(next).toBeDisabled();
  await previous.focus();
  await expect(previous).toBeFocused();
  await previous.press('Enter');
  await expect(page.getByText('Original question', { exact: true })).toBeVisible();
  await expect(page.getByText('Corrected question', { exact: true })).not.toBeVisible();
  await expect.poll(() => new URL(page.url()).searchParams.get('branch')).toBe('root-branch');
  await expect(previous).toBeDisabled();
  await expect(next).toBeEnabled();
  const edit = page.getByRole('button', { name: 'Edit and branch' }).first();
  await edit.focus();
  await expect(edit).toBeFocused();
  await edit.press('Enter');
  const editor = page.getByRole('textbox', { name: 'Edit your message' });
  await expect(editor).toBeVisible();
  await editor.fill('A canceled correction');
  await page.getByRole('button', { name: 'Cancel', exact: true }).click();
  await expect(editor).not.toBeVisible();
  await expect(edit).toBeFocused();
  await expect(page.getByText('Original question', { exact: true })).toBeVisible();

  await edit.press('Enter');
  await editor.fill('A corrected question');
  await page.getByRole('button', { name: 'Create branch' }).click();
  await expect(page.getByRole('alert')).toContainText(/latest version|changed/i);
  await page.getByRole('button', { name: 'Retry branch action' }).click();
  await expect(page.getByText('Alternative answer', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Edit and branch' }).first()).toBeFocused();
  expect(branchCreateAttempts).toBe(2);
});

test('shows a branch-list error and retries the branch action', async ({ page }) => {
  const rootBranch = {
    id: 'root-branch',
    conversationId: 'branch-error',
    parentBranchId: null,
    forkedFromMessageId: null,
    headMessageId: 'assistant-root',
    createdAt: '2026-01-01T00:00:00Z',
  };
  let attempts = 0;
  await page.route('**/api/v1/conversations/branch-error', async (route) => {
    if (route.request().method() !== 'GET') return route.continue();
    return route.fulfill({
      json: {
        id: 'branch-error',
        title: 'Branch recovery',
        agentProfileId: 'general',
        agentRevisionId: 'agent-rev-1',
        modelId: 'qwen2.5:7b',
        version: 1,
        createdAt: '2026-01-01T00:00:00Z',
        updatedAt: '2026-01-01T00:00:00Z',
        messages: [
          {
            id: 'user-root',
            conversationId: 'branch-error',
            role: 'user',
            content: 'Root question',
            state: 'complete',
            runId: 'run-root',
            createdAt: '2026-01-01T00:00:00Z',
            updatedAt: '2026-01-01T00:00:00Z',
          },
          {
            id: 'assistant-root',
            conversationId: 'branch-error',
            role: 'assistant',
            content: 'Root answer',
            state: 'complete',
            runId: 'run-root',
            createdAt: '2026-01-01T00:00:01Z',
            updatedAt: '2026-01-01T00:00:01Z',
          },
        ],
        recentRuns: [],
        currentRun: null,
      },
    });
  });
  await page.route(
    (url) => url.pathname === '/api/v1/conversations/branch-error/branches',
    async (route) => {
      if (route.request().method() !== 'GET') return route.continue();
      attempts += 1;
      if (attempts === 1) {
        return route.fulfill({
          status: 503,
          contentType: 'application/json',
          body: JSON.stringify({ detail: 'Branch list temporarily unavailable' }),
        });
      }
      return route.fulfill({ json: { items: [rootBranch], nextCursor: null } });
    },
  );

  await page.goto('/conversation/branch-error');
  await expect(page.getByRole('alert')).toContainText(/branch/i);
  await page.getByRole('button', { name: 'Retry branch action' }).click();
  // A recovered root-only branch has no actionable navigation, so the
  // toolbar is intentionally removed once recovery is complete.
  await expect(page.getByText('Branch 1 of 1', { exact: true })).not.toBeVisible();
  expect(attempts).toBe(2);
});

test('renders validated document citations as accessible authorized links', async ({ page }) => {
  const documentId = '11111111-1111-4111-8111-111111111111';
  const fixtureDocument = {
    id: documentId,
    filename: 'Document source',
    mediaType: 'text/plain',
    byteCount: 42,
    status: 'ready',
    version: 1,
    failure: null,
    createdAt: '2026-01-01T00:00:00Z',
    updatedAt: '2026-01-01T00:00:00Z',
    extractedTextLength: 42,
    chunkCount: 1,
  };
  await page.route('**/api/v1/documents**', async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (request.method() !== 'GET') return route.continue();
    if (url.pathname === `/api/v1/documents/${documentId}`)
      return route.fulfill({ json: fixtureDocument });
    if (url.pathname === `/api/v1/documents/${documentId}/preview`)
      return route.fulfill({
        json: {
          documentId,
          text: 'Authorized source preview.',
          truncated: false,
          totalCharacters: 26,
        },
      });
    if (url.pathname === '/api/v1/documents')
      return route.fulfill({ json: { items: [fixtureDocument], nextCursor: null } });
    return route.continue();
  });

  const expectDocumentDestination = async (): Promise<void> => {
    await expect(page).toHaveURL(new RegExp(`/documents/${documentId}$`));
    await expect(page.getByRole('heading', { level: 2, name: 'Document source', exact: true })).toBeVisible();
    await expect
      .poll(() => page.evaluate(() => document.activeElement?.id ?? ''))
      .toMatch(/^(detail-heading|main-content)$/);
  };

  const openCitationFixture = async (url: string): Promise<void> => {
    await page.goto(url);
    const composerForm = page.locator('form.composer-wrap');
    await expect(composerForm).toHaveAttribute('data-conversation-ready', 'true');
    await expect(composerForm).toHaveAttribute('aria-busy', 'false');
    await expect(page.getByRole('textbox', { name: 'Message Aura' })).toBeEditable();
  };

  await openCitationFixture('/conversation?fixture=citations');
  const composer = page.getByRole('textbox', { name: 'Message Aura' });
  await composer.fill('A grounded document question');
  await expect(composer).toHaveValue('A grounded document question');
  const firstSend = page.getByRole('button', { name: 'Send' });
  await expect(firstSend).toBeEnabled();
  await firstSend.click();

  const citation = page.getByRole('link', {
    name: 'Open source Document source, Page 2',
  });
  await expect(citation).toBeVisible();
  await expect(citation).toHaveAttribute(
    'href',
    `/documents/${documentId}`,
  );
  await citation.focus();
  await expect(citation).toBeFocused();
  await expect(citation).toHaveText(/Document source/);

  await citation.press('Enter');
  await expectDocumentDestination();

  // Use a distinct fixture URL for the click pass so the browser performs a
  // fresh conversation bootstrap after returning from the document detail.
  await openCitationFixture('/conversation?fixture=citations&round=click');
  const secondComposer = page.getByRole('textbox', { name: 'Message Aura' });
  await secondComposer.fill('A grounded document question');
  await expect(secondComposer).toHaveValue('A grounded document question');
  const secondSend = page.getByRole('button', { name: 'Send' });
  // Returning from the document route recreates the draft conversation. Wait
  // for agent/model hydration before submitting the second interaction; a
  // click alone can otherwise race the disabled draft send state on narrow
  // viewports.
  await expect(secondSend).toBeEnabled();
  await secondSend.click();
  const clickableCitation = page.getByRole('link', {
    name: 'Open source Document source, Page 2',
  });
  await expect(clickableCitation).toBeVisible();
  await clickableCitation.click();
  await expectDocumentDestination();
});

test('shows incompatible models as disabled and persists a conversation model change', async ({
  page,
}) => {
  await page.goto('/conversation/welcome');
  const optionsButton = page.getByRole('button', { name: 'Conversation options' });
  await optionsButton.click();
  const optionsDialog = page.getByRole('dialog', { name: 'Conversation options' });
  await expect(optionsDialog).toBeVisible();
  const picker = optionsDialog.getByLabel('Model');
  await expect(picker).toHaveValue('qwen2.5:7b');
  await expect(picker.locator('option[value="embed-only"]')).toHaveAttribute('disabled', '');

  await picker.selectOption('llama3.2:3b');
  await expect(picker).toHaveValue('llama3.2:3b');
  await optionsDialog.getByRole('button', { name: 'Close' }).click();
  await expect(optionsDialog).not.toBeVisible();
  await page.reload();
  await page.getByRole('button', { name: 'Conversation options' }).click();
  const reopenedOptionsDialog = page.getByRole('dialog', { name: 'Conversation options' });
  await expect(reopenedOptionsDialog).toBeVisible();
  await expect(reopenedOptionsDialog.getByLabel('Model')).toHaveValue('llama3.2:3b');
});

test('recovers from a provider error', async ({ page }) => {
  await page.goto('/conversation/welcome?fixture=error');
  const composer = page.getByRole('textbox', { name: 'Message Aura' });
  await composer.fill('A recoverable local thought');
  await page.getByRole('button', { name: 'Send' }).click();
  await expect(page.getByText(/could not reach the selected provider/)).toBeVisible();
  await page.getByRole('button', { name: 'Try again' }).click();
  await expect(page.getByRole('article', { name: 'Aura response' }).last()).toContainText(
    'A recoverable local thought',
  );
});

test('mobile composer remains sticky and passes axe checks', async ({ page }, testInfo) => {
  test.skip(
    testInfo.project.name !== 'mobile',
    'Mobile sticky and axe coverage uses the mobile project.',
  );
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/conversation');
  const composer = page.locator('form.composer-wrap');
  await expect(composer).toHaveCSS('position', 'sticky');
  await expect(composer).toHaveCSS('bottom', '0px');
  const results = await new AxeBuilder({ page }).analyze();
  expect(results.violations).toEqual([]);
});

test('dark theme conversation passes axe checks', async ({ page }, testInfo) => {
  test.skip(
    testInfo.project.name !== 'desktop',
    'Dark contrast coverage runs once in the desktop project.',
  );
  await page.addInitScript(() => localStorage.setItem('aura-theme', 'dark'));
  await page.goto('/conversation');
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
  const results = await new AxeBuilder({ page }).analyze();
  expect(results.violations).toEqual([]);
});

test('mobile navigation returns focus to its trigger', async ({ page }, testInfo) => {
  test.skip(
    testInfo.project.name !== 'mobile',
    'Mobile overlay focus coverage uses the mobile project.',
  );
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/conversation');
  const trigger = mobileNavigationTrigger(page);
  await trigger.click();
  await expect(page.getByRole('navigation', { name: 'Primary navigation' })).toBeVisible();
  await page
    .getByRole('navigation', { name: 'Primary navigation' })
    .getByRole('button', { name: 'Close navigation' })
    .click();
  await expect(trigger).toBeFocused();
});

test('desktop collapse cannot create a collapsed mobile drawer', async ({ page }, testInfo) => {
  test.skip(
    testInfo.project.name !== 'desktop',
    'Desktop-to-mobile collapse coverage runs once in the desktop project.',
  );
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto('/conversation');
  const navigation = page.locator('#primary-navigation');
  await navigation.getByRole('button', { name: 'Collapse navigation' }).click();
  await expect(navigation).toHaveClass(/is-collapsed/);

  await page.setViewportSize({ width: 390, height: 844 });
  const trigger = mobileNavigationTrigger(page);
  await trigger.click();
  await expect(navigation).toHaveClass(/is-mobile-open/);
  await expect(navigation).not.toHaveClass(/is-collapsed/);
  await expect(navigation).toHaveCSS('flex-basis', '280px');
  await expect(navigation.getByRole('button', { name: 'Close navigation' })).toBeVisible();
  await expect(navigation.getByRole('button', { name: 'Expand navigation' })).toHaveCount(0);
  await navigation.getByRole('button', { name: 'Close navigation' }).click();
  await expect(navigation).not.toHaveClass(/is-mobile-open/);
  await expect(trigger).toBeFocused();
});

test('sidebar motion respects reduced-motion preferences', async ({ page }, testInfo) => {
  test.skip(
    testInfo.project.name !== 'desktop',
    'Motion preference coverage runs once in the desktop project.',
  );
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await page.goto('/conversation');
  const transitionDurations = await page.locator('#primary-navigation').evaluate((element) =>
    getComputedStyle(element)
      .transitionDuration.split(',')
      .map((duration) => Number.parseFloat(duration)),
  );
  expect(transitionDurations.every((duration) => duration <= 0.001)).toBe(true);
  const navigation = page.locator('#primary-navigation');
  await navigation.getByRole('button', { name: 'Collapse navigation' }).click();
  await expect(navigation.locator('.conversation-nav')).toHaveCount(0);
  await navigation.getByRole('button', { name: 'Expand navigation' }).click();
  await expect(navigation.locator('.conversation-nav')).toBeVisible();
  await navigation.getByRole('button', { name: 'Preferences' }).click();
  await expect(navigation.locator('aura-theme-select')).toBeVisible();
});

test('mobile navigation closes on Escape and restores focus', async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== 'mobile', 'Mobile Escape coverage uses the mobile project.');
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/conversation');
  const trigger = mobileNavigationTrigger(page);
  const navigation = page.locator('#primary-navigation');
  await trigger.click();
  await expect(navigation).toHaveClass(/is-mobile-open/);
  await page.keyboard.press('Escape');
  await expect(navigation).not.toHaveClass(/is-mobile-open/);
  await expect(trigger).toBeFocused();
});

test('closed mobile navigation is hidden from assistive technology and open navigation makes the page inert', async ({ page }, testInfo) => {
  test.skip(
    testInfo.project.name !== 'mobile',
    'Mobile navigation accessibility-tree semantics run once in the mobile project.',
  );
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/conversation');
  const trigger = mobileNavigationTrigger(page);
  const navigation = page.locator('#primary-navigation');
  const main = page.locator('#main-content');

  await expect(navigation).toHaveAttribute('aria-hidden', 'true');
  await expect(navigation).toHaveAttribute('inert', '');
  await expect(main).not.toHaveAttribute('inert', '');
  await trigger.click();
  await expect(navigation).toHaveClass(/is-mobile-open/);
  await expect(navigation).not.toHaveAttribute('aria-hidden', 'true');
  await expect(main).toHaveAttribute('inert', '');

  await page.keyboard.press('Escape');
  await expect(navigation).not.toHaveClass(/is-mobile-open/);
  await expect(navigation).toHaveAttribute('aria-hidden', 'true');
  await expect(main).not.toHaveAttribute('inert', '');
  await expect(trigger).toBeFocused();
});

test('open mobile navigation isolates header and main controls while the scrim remains operable', async ({ page }, testInfo) => {
  test.skip(
    testInfo.project.name !== 'mobile',
    'Mobile modal-context coverage runs once in the mobile project.',
  );
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/conversation');
  const trigger = mobileNavigationTrigger(page);
  const header = page.locator('.mobile-header');
  const navigation = page.locator('#primary-navigation');
  const main = page.locator('#main-content');
  const scrim = page.locator('.scrim');

  await trigger.click();
  await waitForMobileDrawer(page, 'open');
  await expect(scrim).toBeVisible();
  await expect(header).toHaveAttribute('inert', '');
  await expect(header).toHaveAttribute('aria-hidden', 'true');
  await expect(main).toHaveAttribute('inert', '');
  await expect(navigation).not.toHaveAttribute('aria-hidden', 'true');

  await scrim.click();
  await waitForMobileDrawer(page, 'closed');
  await expect(header).not.toHaveAttribute('inert', '');
  await expect(header).not.toHaveAttribute('aria-hidden', 'true');
  await expect(main).not.toHaveAttribute('inert', '');
  await expect(trigger).toBeFocused();
});

test('closed mobile navigation is hidden and unreachable by Tab', async ({ page }, testInfo) => {
  test.skip(
    testInfo.project.name !== 'mobile',
    'Closed drawer keyboard coverage uses the mobile project.',
  );
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/conversation');
  const trigger = mobileNavigationTrigger(page);
  const navigation = page.locator('#primary-navigation');
  await expect(navigation).not.toHaveClass(/is-mobile-open/);
  await expect(navigation).toHaveCSS('visibility', 'hidden');
  await expect(navigation).toHaveCSS('pointer-events', 'none');
  await trigger.focus();
  const focusableCount = await page
    .locator(
      'a:visible, button:visible, textarea:visible, input:visible, select:visible, [tabindex]:visible',
    )
    .count();
  for (let index = 0; index < focusableCount + 1; index += 1) {
    await page.keyboard.press('Tab');
    await expect
      .poll(() => navigation.evaluate((element) => element.contains(document.activeElement)))
      .toBe(false);
  }
});

test('mobile conversation selection closes navigation and restores focus', async ({
  page,
}, testInfo) => {
  test.skip(
    testInfo.project.name !== 'mobile',
    'Mobile selection coverage uses the mobile project.',
  );
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/conversation');
  const trigger = mobileNavigationTrigger(page);
  const navigation = page.locator('#primary-navigation');
  await trigger.click();
  await navigation
    .locator('.conversation-link')
    .filter({ hasText: 'A thoughtful beginning' })
    .click();
  await expect(navigation).not.toHaveClass(/is-mobile-open/);
  await expect(trigger).toBeFocused();
});

test('mobile new conversation closes navigation and restores focus', async ({ page }, testInfo) => {
  test.skip(
    testInfo.project.name !== 'mobile',
    'Mobile new-conversation coverage uses the mobile project.',
  );
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/conversation');
  const trigger = mobileNavigationTrigger(page);
  const navigation = page.locator('#primary-navigation');
  await trigger.click();
  await navigation.locator('button.new-conversation').click();
  await expect(navigation).not.toHaveClass(/is-mobile-open/);
  await expect(trigger).toBeFocused();
});

test('leaving mobile clears the modal drawer without a reload', async ({ page }, testInfo) => {
  test.skip(
    testInfo.project.name !== 'mobile',
    'Responsive drawer transition coverage uses the mobile project.',
  );
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/conversation');
  const trigger = mobileNavigationTrigger(page);
  const navigation = page.locator('#primary-navigation');
  await trigger.click();
  await expect(navigation).toHaveClass(/is-mobile-open/);

  await page.setViewportSize({ width: 1024, height: 768 });
  await expect(navigation).not.toHaveClass(/is-mobile-open/);
  await expect(page.getByRole('button', { name: 'Close navigation' })).toHaveCount(0);
  await page.locator('#main-content').focus();
  await expect(page.locator('#main-content')).toBeFocused();
});
