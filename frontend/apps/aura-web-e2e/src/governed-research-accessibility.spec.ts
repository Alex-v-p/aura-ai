import AxeBuilder from '@axe-core/playwright';
import { expect, test, type Page, type Route } from '@playwright/test';

const NOW = '2026-10-30T10:00:00.000Z';
const RESEARCH_ID = '11111111-1111-4111-8111-111111111111';
const TASK_ID = '22222222-2222-4222-8222-222222222222';
const ARTIFACT_ID = '33333333-3333-4333-8333-333333333333';
const CONVERSATION_ID = '88888888-8888-4888-8888-888888888888';

async function installAccessibilityFixture(page: Page, sessionExpired = false): Promise<void> {
  await page.route('**/api/v1/**', async (route: Route) => {
    const request = route.request();
    const url = new URL(request.url());
    const json = (body: unknown, status = 200): Promise<void> =>
      route.fulfill({ status, headers: { 'cache-control': 'no-store' }, json: body });
    if (url.pathname === '/api/v1/auth/session' && sessionExpired)
      return json({ detail: 'synthetic session expired' }, 401);
    if (url.pathname === '/api/v1/auth/session')
      return json({
        principal: { issuer: 'synthetic', subject: 'fixture-owner', displayName: 'Fixture Owner' },
        csrfToken: 'fixture-csrf',
        idleExpiresAt: '2099-10-30T10:00:00Z',
        absoluteExpiresAt: '2099-10-31T10:00:00Z',
      });
    if (url.pathname === '/api/v1/models')
      return json({ models: [], defaultModelId: null, observedAt: NOW });
    if (url.pathname === '/api/v1/agents') return json({ items: [] });
    if (url.pathname === '/api/v1/conversations') return json({ items: [], nextCursor: null });
    if (url.pathname === '/api/v1/notifications')
      return json({ items: [], nextCursor: null, unacknowledgedCount: 0 });
    if (url.pathname === `/api/v1/conversations/${CONVERSATION_ID}`)
      return json({
        id: CONVERSATION_ID,
        title: 'Synthetic research conversation',
        agentProfileId: null,
        agentRevisionId: null,
        modelId: null,
        version: 1,
        createdAt: NOW,
        updatedAt: NOW,
        archivedAt: null,
        currentRun: null,
        messages: [
          {
            id: 'message-1',
            conversationId: CONVERSATION_ID,
            role: 'assistant',
            content: 'Research activity completed.',
            state: 'complete',
            runId: null,
            taskRefs: [{ taskId: TASK_ID }],
            researchActivity: {
              taskRef: { taskId: TASK_ID },
              mode: 'quick',
              admissionState: 'completed',
            },
            createdAt: NOW,
            updatedAt: NOW,
          },
        ],
        recentRuns: [],
      });
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
    if (url.pathname === '/api/v1/research' && request.method() === 'GET')
      return json({ items: [], nextCursor: null });
    if (url.pathname === '/api/v1/research' && request.method() === 'POST')
      return json(
        {
          researchId: RESEARCH_ID,
          taskRef: { taskId: TASK_ID },
          mode: 'quick',
          admissionState: 'admitted',
          admittedBudget: {
            wallTimeSeconds: 90,
            fetchedSources: 6,
            searchRequests: 2,
            browserPages: 1,
            modelCalls: 2,
          },
          version: 1,
          createdAt: NOW,
        },
        202,
      );
    if (url.pathname === `/api/v1/research/${RESEARCH_ID}`)
      return json({
        id: RESEARCH_ID,
        taskRef: { taskId: TASK_ID },
        mode: 'quick',
        stage: 'persisting',
        outcome: 'completed',
        contentState: 'active',
        version: 2,
        capabilities: {
          canCancel: false,
          canExtend: false,
          canDelete: true,
          canRetryCleanup: false,
          canReconcileCleanup: false,
        },
        createdAt: NOW,
        updatedAt: NOW,
        finishedAt: NOW,
        question: 'Synthetic question',
        admittedBudget: {
          wallTimeSeconds: 90,
          fetchedSources: 6,
          searchRequests: 2,
          browserPages: 1,
          modelCalls: 2,
        },
        consumedBudget: {
          wallTimeSeconds: 12,
          fetchedSources: 2,
          searchRequests: 1,
          browserPages: 0,
          modelCalls: 1,
        },
        extensionAccepted: false,
        sourceCount: 2,
        failureClass: null,
        artifactRefs: [{ artifactId: ARTIFACT_ID }],
        citations: [],
      });
    if (url.pathname === `/api/v1/research/${RESEARCH_ID}/evidence`)
      return json({ items: [], nextCursor: null });
    if (url.pathname === '/api/v1/tasks')
      return json({
        items: [
          {
            id: TASK_ID,
            sourceKind: 'research',
            sourceId: RESEARCH_ID,
            sourceVersion: 2,
            status: 'completed',
            sourceStatus: 'completed',
            sourceOutcome: 'completed',
            stage: 'persisting',
            progress: 1,
            capabilities: { canCancel: false, canRetry: false, hasSourceDetail: true },
            routeKey: 'research_detail',
            failureClass: null,
            recoveryAction: null,
            displayLabel: 'Synthetic research',
            artifactRefs: [],
            createdAt: NOW,
            updatedAt: NOW,
          },
        ],
        nextCursor: null,
      });
    if (url.pathname === `/api/v1/tasks/${TASK_ID}`)
      return json({
        id: TASK_ID,
        sourceKind: 'research',
        sourceId: RESEARCH_ID,
        sourceVersion: 2,
        status: 'completed',
        sourceStatus: 'completed',
        sourceOutcome: 'completed',
        stage: 'persisting',
        progress: 1,
        capabilities: { canCancel: false, canRetry: false, hasSourceDetail: true },
        routeKey: 'research_detail',
        failureClass: null,
        recoveryAction: null,
        displayLabel: 'Synthetic research',
        artifactRefs: [],
        createdAt: NOW,
        updatedAt: NOW,
      });
    if (url.pathname === `/api/v1/tasks/${TASK_ID}/events`)
      return route.fulfill({
        status: 200,
        headers: { 'cache-control': 'no-store', 'content-type': 'text/event-stream' },
        body: '',
      });
    if (url.pathname === '/api/v1/artifacts')
      return json({
        items: [
          {
            id: ARTIFACT_ID,
            kind: 'research_report',
            title: 'Research report.md',
            mediaType: 'text/markdown; charset=utf-8',
            contentState: 'active',
            currentRevision: 1,
            version: 1,
            capabilities: {
              canPreview: true,
              canDownload: true,
              canDelete: true,
              canRetryCleanup: false,
              canReconcileCleanup: false,
            },
            createdAt: NOW,
            updatedAt: NOW,
            lineage: { taskId: TASK_ID, runId: null, conversationId: null, workspaceId: null },
            revision: {
              revision: 1,
              mediaType: 'text/markdown; charset=utf-8',
              sha256: 'a'.repeat(64),
              byteCount: 32,
              creatorComponent: 'aura.research.coordinator@1',
              previousRevision: null,
              createdAt: NOW,
            },
          },
        ],
        nextCursor: null,
      });
    if (url.pathname === `/api/v1/artifacts/${ARTIFACT_ID}`)
      return json({
        id: ARTIFACT_ID,
        kind: 'research_report',
        title: 'Research report.md',
        mediaType: 'text/markdown; charset=utf-8',
        contentState: 'active',
        currentRevision: 1,
        version: 1,
        capabilities: {
          canPreview: true,
          canDownload: true,
          canDelete: true,
          canRetryCleanup: false,
          canReconcileCleanup: false,
        },
        createdAt: NOW,
        updatedAt: NOW,
        lineage: { taskId: TASK_ID, runId: null, conversationId: null, workspaceId: null },
        revision: {
          revision: 1,
          mediaType: 'text/markdown; charset=utf-8',
          sha256: 'a'.repeat(64),
          byteCount: 32,
          creatorComponent: 'aura.research.coordinator@1',
          previousRevision: null,
          createdAt: NOW,
        },
      });
    if (url.pathname === `/api/v1/artifacts/${ARTIFACT_ID}/revisions`)
      return json([
        {
          revision: 1,
          mediaType: 'text/markdown; charset=utf-8',
          sha256: 'a'.repeat(64),
          byteCount: 32,
          creatorComponent: 'aura.research.coordinator@1',
          previousRevision: null,
          createdAt: NOW,
        },
      ]);
    if (url.pathname === `/api/v1/artifacts/${ARTIFACT_ID}/preview`)
      return json({
        artifactId: ARTIFACT_ID,
        revision: 1,
        mediaType: 'text/markdown; charset=utf-8',
        text: 'Synthetic report',
        truncated: false,
      });
    return json({ detail: 'synthetic endpoint not implemented' }, 404);
  });
}

test.describe('Aura governed research accessibility', () => {
  test('new routes have no axe violations with reduced motion enabled', async ({ page }) => {
    await installAccessibilityFixture(page);
    await page.emulateMedia({ reducedMotion: 'reduce', colorScheme: 'light' });
    for (const [path, heading] of [
      ['/research', /research/i],
      [`/research/${RESEARCH_ID}`, /research/i],
      ['/tasks', /task center/i],
      [`/tasks/${TASK_ID}`, /task/i],
      ['/artifacts', /artifact/i],
      [`/artifacts/${ARTIFACT_ID}`, /artifact/i],
      [`/conversation/${CONVERSATION_ID}`, /conversation/i],
    ] as const) {
      await page.goto(path);
      await expect(page.getByRole('heading', { name: heading }).first()).toBeVisible();
      const result = await new AxeBuilder({ page }).analyze();
      expect(result.violations).toEqual([]);
    }
  });

  test('research controls remain usable at 200 percent zoom and in dark high contrast', async ({
    page,
  }) => {
    await installAccessibilityFixture(page);
    await page.emulateMedia({
      reducedMotion: 'reduce',
      colorScheme: 'dark',
      forcedColors: 'active',
    });
    for (const path of [
      '/research',
      `/research/${RESEARCH_ID}`,
      '/tasks',
      `/tasks/${TASK_ID}`,
      '/artifacts',
      `/artifacts/${ARTIFACT_ID}`,
      `/conversation/${CONVERSATION_ID}`,
    ]) {
      await page.goto(path);
      await page.evaluate(() => {
        document.documentElement.style.zoom = '200%';
      });
      await expect(page.getByRole('heading').first()).toBeVisible();
      const layout = await page.evaluate(() => ({
        width: document.documentElement.scrollWidth,
        viewport: window.innerWidth,
      }));
      expect(layout.width).toBeLessThanOrEqual(layout.viewport + 2);
    }
  });

  test('keyboard focus, live regions, and dialogs recover on artifact detail', async ({ page }) => {
    await installAccessibilityFixture(page);
    await page.goto(`/artifacts/${ARTIFACT_ID}`);
    const deleteButton = page.getByRole('button', { name: /delete artifact/i });
    await deleteButton.focus();
    await expect(deleteButton).toBeFocused();
    await deleteButton.press('Enter');
    const dialog = page.getByRole('alertdialog');
    await expect(dialog).toBeVisible();
    await expect(dialog.getByRole('button', { name: /keep artifact/i })).toBeFocused();
    for (let index = 0; index < 4; index += 1) {
      await page.keyboard.press('Tab');
      await expect.poll(() => dialog.locator(':focus').count()).toBe(1);
    }
    await page.keyboard.press('Escape');
    await expect(dialog).toBeHidden();
    await expect(deleteButton).toBeFocused();
  });

  test('session loss presents a content-free recovery state', async ({ page }) => {
    await installAccessibilityFixture(page, true);
    await page.goto('/research');
    await expect(
      page.getByText(/session|sign in|unavailable|could not load/i).first(),
    ).toBeVisible();
    await expect(page.getByText('Synthetic question', { exact: true })).toBeHidden();
  });

  test('records the live admission mutation without stealing focus and inspects browser persistence', async ({
    page,
  }) => {
    await installAccessibilityFixture(page);
    await page.goto('/research');
    const question = page.getByLabel(/research question/i);
    await question.fill('Synthetic accessibility admission');
    await page.getByRole('button', { name: /start research/i }).click();
    await expect(
      page.locator('[role="status"]').filter({ hasText: 'Research started.' }),
    ).toBeVisible();
    expect(await page.evaluate(() => document.activeElement?.getAttribute('role'))).not.toBe(
      'status',
    );
    const persistence = await page.evaluate(async () => {
      const indexedDb =
        typeof indexedDB.databases === 'function' ? await indexedDB.databases() : [];
      const indexedDbValues: Array<{ name: string; store: string; values: unknown[] }> = [];
      for (const database of indexedDb) {
        const databaseName = database.name;
        if (!databaseName) continue;
        const opened = await new Promise<IDBDatabase | null>((resolve) => {
          const request = indexedDB.open(databaseName, database.version);
          request.onsuccess = () => resolve(request.result);
          request.onerror = () => resolve(null);
        });
        if (!opened) continue;
        const stores = Array.from(opened.objectStoreNames);
        for (const store of stores) {
          const values = await new Promise<unknown[]>((resolve) => {
            const transaction = opened.transaction(store, 'readonly');
            const request = transaction.objectStore(store).getAll();
            request.onsuccess = () => resolve(request.result as unknown[]);
            request.onerror = () => resolve([]);
          });
          indexedDbValues.push({ name: databaseName, store, values });
        }
        opened.close();
      }
      const cacheNames = await caches.keys();
      const cacheResponses: Array<{ cache: string; url: string; body: string; headers: string[] }> =
        [];
      for (const cacheName of cacheNames) {
        const cache = await caches.open(cacheName);
        for (const request of await cache.keys()) {
          const response = await cache.match(request);
          cacheResponses.push({
            cache: cacheName,
            url: request.url,
            body: response ? await response.clone().text() : '',
            headers: response ? Array.from(response.headers.entries()) : [],
          });
        }
      }
      return {
        indexedDb,
        indexedDbValues,
        caches: cacheNames,
        cacheResponses,
        serviceWorkers: (await navigator.serviceWorker.getRegistrations()).map(
          (item) => item.scope,
        ),
      };
    });
    expect(
      persistence.indexedDb.every(
        (item) => !item.name || !/content|artifact|research/i.test(item.name),
      ),
    ).toBe(true);
    expect(persistence.caches.every((item) => !/content|artifact|research/i.test(item))).toBe(true);
    expect(JSON.stringify(persistence.indexedDbValues)).not.toMatch(
      /content|artifact|research|synthetic accessibility admission/i,
    );
    expect(JSON.stringify(persistence.cacheResponses)).not.toMatch(
      /content|artifact|research|synthetic accessibility admission/i,
    );
    expect(persistence.serviceWorkers).toEqual([]);
  });
});
