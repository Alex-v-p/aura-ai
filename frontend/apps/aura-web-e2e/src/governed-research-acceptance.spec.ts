import { expect, test, type Page, type Route } from '@playwright/test';

const NOW = '2026-10-30T10:00:00.000Z';
const RESEARCH_ID = '11111111-1111-4111-8111-111111111111';
const TASK_ID = '22222222-2222-4222-8222-222222222222';
const ARTIFACT_ID = '33333333-3333-4333-8333-333333333333';
const CONVERSATION_ID = '88888888-8888-4888-8888-888888888888';
const RUN_ID = '99999999-9999-4999-8999-999999999999';
const BUILT_IN_AURA_AGENT_ID = '7e1d98ee-65b2-5bc7-9ca9-edd7ad85b540';
const MODES = [
  ['quick', 'Quick'],
  ['auto', 'Auto'],
  ['deep', 'Deep'],
  ['agent_led', 'Agent-led'],
] as const;
type ResearchMode = (typeof MODES)[number][0];
type CleanupState = 'deletion_pending' | 'deletion_failed' | 'cleanup_unknown';

const budgets = {
  quick: {
    wallTimeSeconds: 90,
    fetchedSources: 6,
    searchRequests: 2,
    browserPages: 1,
    modelCalls: 2,
  },
  auto: {
    wallTimeSeconds: 600,
    fetchedSources: 15,
    searchRequests: 5,
    browserPages: 2,
    modelCalls: 4,
  },
  deep: {
    wallTimeSeconds: 1800,
    fetchedSources: 30,
    searchRequests: 10,
    browserPages: 6,
    modelCalls: 8,
  },
  agent_led: {
    wallTimeSeconds: 3600,
    fetchedSources: 60,
    searchRequests: 20,
    browserPages: 12,
    modelCalls: 12,
  },
};

type RequestLog = { method: string; pathname: string; body: string | null };

function detail(mode: ResearchMode = 'quick'): Record<string, unknown> {
  return {
    id: RESEARCH_ID,
    taskRef: { taskId: TASK_ID },
    mode,
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
    question: 'Synthetic governed research question',
    admittedBudget: budgets[mode],
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
    citations: [
      {
        kind: 'web',
        sourceId: 'source-1',
        label: 'Synthetic source',
        url: 'https://example.test/source',
        retrievedAt: NOW,
        locator: 'Overview',
      },
    ],
  };
}

function task(): Record<string, unknown> {
  return {
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
    displayLabel: 'Synthetic governed research',
    artifactRefs: [{ artifactId: ARTIFACT_ID }],
    createdAt: NOW,
    updatedAt: NOW,
  };
}

function artifact(): Record<string, unknown> {
  return {
    id: ARTIFACT_ID,
    kind: 'research_report',
    title: 'Research report.md',
    mediaType: 'text/markdown; charset=utf-8',
    contentState: 'active',
    currentRevision: 2,
    version: 2,
    capabilities: {
      canPreview: true,
      canDownload: true,
      canDelete: true,
      canRetryCleanup: false,
      canReconcileCleanup: false,
    },
    createdAt: NOW,
    updatedAt: NOW,
    lineage: { taskId: TASK_ID, runId: null, conversationId: CONVERSATION_ID, workspaceId: null },
    revision: {
      revision: 2,
      mediaType: 'text/markdown; charset=utf-8',
      sha256: 'a'.repeat(64),
      byteCount: 128,
      creatorComponent: 'aura.research.coordinator@1',
      previousRevision: 1,
      createdAt: NOW,
    },
  };
}

async function installFixture(
  page: Page,
  conversationMode: ResearchMode = 'quick',
  cleanupState: CleanupState = 'deletion_pending',
): Promise<RequestLog[]> {
  const requests: RequestLog[] = [];
  let activeMode: ResearchMode = conversationMode;
  await page.route('**/api/v1/**', async (route: Route) => {
    const request = route.request();
    const url = new URL(request.url());
    requests.push({ method: request.method(), pathname: url.pathname, body: request.postData() });
    const json = (body: unknown, status = 200): Promise<void> =>
      route.fulfill({ status, headers: { 'cache-control': 'no-store' }, json: body });
    if (url.pathname === '/api/v1/auth/session')
      return json({
        principal: { issuer: 'synthetic', subject: 'fixture-owner', displayName: 'Fixture Owner' },
        csrfToken: 'fixture-csrf',
        idleExpiresAt: '2099-10-30T10:00:00Z',
        absoluteExpiresAt: '2099-10-31T10:00:00Z',
      });
    if (url.pathname === '/api/v1/models')
      return json({
        models: [
          {
            id: 'fixture-model',
            displayName: 'Fixture model',
            provider: 'fixture',
            capabilities: ['chat'],
            availability: 'available',
            selectable: true,
            disabledReason: null,
          },
        ],
        defaultModelId: 'fixture-model',
        observedAt: NOW,
      });
    if (url.pathname === '/api/v1/agents')
      return json({
        items: [
          {
            id: BUILT_IN_AURA_AGENT_ID,
            status: 'active',
            version: 1,
            currentRevision: {
              id: 'agent-revision',
              profileId: BUILT_IN_AURA_AGENT_ID,
              revision: 1,
              displayName: 'Aura',
              purpose: 'Fixture research assistant',
              instructions: 'Use the bounded fixture policy.',
              personaRevisionId: null,
              promptBundleRevisionId: 'prompt-revision',
              modelPolicyRevisionId: 'model-policy',
              createdAt: NOW,
            },
            revisions: [],
          },
        ],
      });
    if (url.pathname === `/api/v1/agents/${BUILT_IN_AURA_AGENT_ID}`)
      return json({
        id: BUILT_IN_AURA_AGENT_ID,
        status: 'active',
        version: 1,
        displayName: 'Aura',
        createdAt: NOW,
        updatedAt: NOW,
        currentRevision: {
          id: 'agent-revision',
          profileId: BUILT_IN_AURA_AGENT_ID,
          revision: 1,
          displayName: 'Aura',
          purpose: 'Fixture research assistant',
          instructions: 'Use the bounded fixture policy.',
          personaRevisionId: null,
          promptBundleRevisionId: 'prompt-revision',
          modelPolicyRevisionId: 'model-policy',
          createdAt: NOW,
        },
        revisions: [],
      });
    if (url.pathname === '/api/v1/conversations' && request.method() === 'GET')
      return json({ items: [], nextCursor: null });
    if (url.pathname === `/api/v1/conversations/${CONVERSATION_ID}`) {
      const mode = activeMode;
      return json({
        id: CONVERSATION_ID,
        title: 'Synthetic research',
        agentProfileId: BUILT_IN_AURA_AGENT_ID,
        agentRevisionId: 'agent-revision',
        modelId: 'fixture-model',
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
            content:
              mode === 'auto' ? 'Research activity detached.' : 'Research activity admitted.',
            state: 'complete',
            runId: null,
            taskRefs: [{ taskId: TASK_ID }],
            researchActivity: {
              taskRef: { taskId: TASK_ID },
              mode,
              admissionState: mode === 'auto' ? 'detached' : 'admitted',
            },
            createdAt: NOW,
            updatedAt: NOW,
          },
        ],
        recentRuns: [],
      });
    }
    if (
      url.pathname === `/api/v1/conversations/${CONVERSATION_ID}/runs` &&
      request.method() === 'POST'
    ) {
      const run = {
        id: RUN_ID,
        conversationId: CONVERSATION_ID,
        userMessageId: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
        assistantMessageId: null,
        status: 'running',
        agentRevisionId: 'agent-revision',
        modelPolicyRevisionId: 'model-policy',
        provider: 'fixture',
        modelId: 'fixture-model',
        retryOfRunId: null,
        createdAt: NOW,
        startedAt: NOW,
        finishedAt: null,
        error: null,
      };
      return json(
        {
          conversation: {
            id: CONVERSATION_ID,
            title: 'Synthetic research',
            agentProfileId: BUILT_IN_AURA_AGENT_ID,
            agentRevisionId: 'agent-revision',
            modelId: 'fixture-model',
            version: 2,
            createdAt: NOW,
            updatedAt: NOW,
            archivedAt: null,
            currentRun: run,
          },
          userMessage: {
            id: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
            conversationId: CONVERSATION_ID,
            role: 'user',
            content: 'Synthetic governed research request',
            state: 'complete',
            runId: RUN_ID,
            createdAt: NOW,
            updatedAt: NOW,
          },
          run,
        },
        202,
      );
    }
    if (url.pathname === `/api/v1/runs/${RUN_ID}/events`) {
      const states =
        conversationMode === 'auto'
          ? ['running', 'detached', 'completed']
          : ['running', 'completed'];
      const events = states.map((state, index) => {
        const admissionState =
          state === 'detached' ? 'detached' : state === 'completed' ? 'completed' : 'admitted';
        const content =
          state === 'detached'
            ? 'Research activity detached.'
            : state === 'completed'
              ? 'Research activity completed.'
              : 'Research activity running.';
        return {
          schemaVersion: 1,
          eventId: `event-${index + 1}`,
          sequence: index + 1,
          eventType: 'assistant.snapshot',
          runId: RUN_ID,
          conversationId: CONVERSATION_ID,
          occurredAt: NOW,
          data: {
            message: {
              id: `message-${index + 2}`,
              conversationId: CONVERSATION_ID,
              role: 'assistant',
              content,
              state: 'complete',
              runId: RUN_ID,
              taskRefs: [{ taskId: TASK_ID }],
              researchActivity: {
                taskRef: { taskId: TASK_ID },
                mode: activeMode,
                admissionState,
              },
              createdAt: NOW,
              updatedAt: NOW,
            },
          },
        };
      });
      return route.fulfill({
        status: 200,
        headers: { 'cache-control': 'no-store', 'content-type': 'text/event-stream' },
        body: `${events.map((event) => `id: ${event.eventId}\ndata: ${JSON.stringify(event)}\n\n`).join('')}`,
      });
    }
    if (url.pathname === '/api/v1/notifications')
      return json({
        items: [
          {
            id: 'notification-1',
            researchId: RESEARCH_ID,
            status: 'delivered',
            version: 1,
            deliveredAt: NOW,
            createdAt: NOW,
            updatedAt: NOW,
            acknowledgedAt: null,
          },
        ],
        nextCursor: null,
        unacknowledgedCount: 1,
      });
    if (url.pathname === '/api/v1/research/settings')
      return json({
        version: 1,
        presets: Object.entries(budgets).map(([mode, budget]) => ({
          mode,
          maximum: budget,
          defaults: budget,
        })),
        updatedAt: NOW,
      });
    if (url.pathname === '/api/v1/research' && request.method() === 'GET')
      return json({ items: [], nextCursor: null });
    if (url.pathname === '/api/v1/research' && request.method() === 'POST') {
      const body = JSON.parse(request.postData() ?? '{}') as { mode?: keyof typeof budgets };
      activeMode = body.mode ?? activeMode;
      return json(
        {
          researchId: RESEARCH_ID,
          taskRef: { taskId: TASK_ID },
          mode: body.mode ?? 'quick',
          admissionState: 'admitted',
          admittedBudget: budgets[body.mode ?? 'quick'],
          version: 1,
          createdAt: NOW,
        },
        202,
      );
    }
    if (url.pathname === `/api/v1/research/${RESEARCH_ID}`) return json(detail(activeMode));
    if (url.pathname === `/api/v1/research/${RESEARCH_ID}/evidence`)
      return json({
        items: [
          {
            sourceId: 'source-1',
            label: 'Synthetic source',
            url: 'https://example.test/source',
            publisher: 'Example',
            retrievedAt: NOW,
            mediaType: 'text/html',
            extractionMethod: 'static',
            freshnessAt: NOW,
            locators: ['Overview'],
          },
        ],
        nextCursor: null,
      });
    if (url.pathname === '/api/v1/tasks' && request.method() === 'GET')
      return json({ items: [task()], nextCursor: null });
    if (url.pathname === `/api/v1/tasks/${TASK_ID}`) return json(task());
    if (url.pathname === `/api/v1/tasks/${TASK_ID}/events`)
      return route.fulfill({
        status: 200,
        headers: { 'cache-control': 'no-store', 'content-type': 'text/event-stream' },
        body: '',
      });
    if (url.pathname === '/api/v1/artifacts' && request.method() === 'GET')
      return json({ items: [artifact()], nextCursor: null });
    if (url.pathname === `/api/v1/artifacts/${ARTIFACT_ID}`) return json(artifact());
    if (url.pathname === `/api/v1/artifacts/${ARTIFACT_ID}/revisions`)
      return json([
        {
          revision: 1,
          mediaType: 'text/markdown; charset=utf-8',
          sha256: 'b'.repeat(64),
          byteCount: 96,
          creatorComponent: 'aura.research.coordinator@1',
          previousRevision: null,
          createdAt: NOW,
        },
        artifact().revision,
      ]);
    if (url.pathname === `/api/v1/artifacts/${ARTIFACT_ID}/preview`)
      return json({
        artifactId: ARTIFACT_ID,
        revision: 2,
        mediaType: 'text/markdown; charset=utf-8',
        text: 'Synthetic cited report',
        truncated: false,
      });
    if (url.pathname === `/api/v1/artifacts/${ARTIFACT_ID}/content`)
      return route.fulfill({
        status: 200,
        headers: {
          'cache-control': 'no-store',
          'content-type': 'text/markdown; charset=utf-8',
          'content-length': '128',
          'content-disposition': 'attachment; filename="research-report.md"',
        },
        body: 'x'.repeat(128),
      });
    if (url.pathname === `/api/v1/artifacts/${ARTIFACT_ID}/deletions`) {
      const current = artifact();
      return json(
        {
          ...current,
          title: null,
          mediaType: null,
          contentState: cleanupState,
          capabilities: {
            canPreview: false,
            canDownload: false,
            canDelete: false,
            canRetryCleanup: cleanupState === 'deletion_failed',
            canReconcileCleanup: cleanupState === 'cleanup_unknown',
          },
          integrity: {
            revision: current.currentRevision,
            sha256: 'a'.repeat(64),
            byteCount: 128,
            createdAt: NOW,
          },
          deletionAuditId: '44444444-4444-4444-8444-444444444444',
        },
        202,
      );
    }
    return json({ detail: 'synthetic endpoint not implemented' }, 404);
  });
  return requests;
}

async function assertTaskCenterRecoveryAndCitation(page: Page): Promise<void> {
  await page.goto('/tasks');
  const taskCard = page.locator('.task-card').first();
  await expect(taskCard).toBeVisible();
  await taskCard.click();
  await expect(page).toHaveURL(new RegExp(`/tasks/${TASK_ID}$`));
  await expect(page.getByText(/completed/i).first()).toBeVisible();
  await expect(page.getByRole('link', { name: /open (?:research )?source/i })).toBeVisible();
  await page.getByRole('link', { name: /open (?:research )?source/i }).click();
  await expect(page).toHaveURL(new RegExp(`/research/${RESEARCH_ID}$`));
  await expect(page.getByText('Cited evidence', { exact: true })).toBeVisible();
  await expect(page.getByText('Synthetic source', { exact: true })).toBeVisible();
  await expect(page.getByRole('link', { name: /open report artifact/i })).toBeVisible();
}

async function assertNotificationAndArtifactLifecycle(page: Page): Promise<void> {
  await page.goto('/notifications');
  await expect(page.locator('.notification-card').first()).toBeVisible();
  await expect(page.locator('.notification-card').first()).toContainText(/unread/i);

  await page.goto('/artifacts');
  await page.locator('.artifact-card').first().click();
  await expect(page.getByText('Synthetic cited report', { exact: true })).toBeVisible();
  await expect(page.getByText('Revision 1', { exact: true })).toBeVisible();
  await expect(page.getByText('Revision 2', { exact: true })).toBeVisible();
  const download = page.waitForEvent('download');
  await page.getByRole('button', { name: /download/i }).click();
  await expect((await download).suggestedFilename()).toMatch(/research-report/i);
  await page.getByRole('button', { name: /delete artifact/i }).click();
  await page
    .getByRole('alertdialog')
    .getByRole('button', { name: /^delete$/i })
    .click();
  await expect(
    page.getByText(/cleanup status|deletion pending|no longer readable/i).first(),
  ).toBeVisible();
  await expect(page.locator('.preview')).toHaveCount(0);
  await expect(page.locator('.revisions li')).toHaveCount(0);
}

test.describe('Aura governed research synthetic acceptance', () => {
  test('admits every bounded mode with server-owned budgets from /research', async ({ page }) => {
    const requests = await installFixture(page);
    for (const [mode, label] of MODES) {
      await page.goto('/research');
      await page.getByLabel(/research question/i).fill(`Synthetic ${mode} research`);
      await page.getByRole('radio', { name: new RegExp(label, 'i') }).check();
      await expect(
        page
          .getByText(
            new RegExp(
              `${budgets[mode].wallTimeSeconds.toLocaleString()} seconds|${budgets[mode].fetchedSources} sources`,
              'i',
            ),
          )
          .first(),
      ).toBeVisible();
      await page.getByRole('button', { name: /start research/i }).click();
      await expect(page).toHaveURL(new RegExp(`/research/${RESEARCH_ID}$`));
    }
    const admissions = requests.filter(
      (item) => item.pathname === '/api/v1/research' && item.method === 'POST',
    );
    expect(admissions).toHaveLength(MODES.length);
    for (const [index, [mode]] of MODES.entries())
      expect(admissions[index].body).toContain(`"mode":"${mode}"`);
  });

  test('delivers chat activity, Task Center recovery, and artifact inspection', async ({
    page,
  }) => {
    await installFixture(page);
    await page.goto(`/conversation/${CONVERSATION_ID}`);
    await expect(page.getByText('Research activity admitted.', { exact: true })).toBeVisible();
    await page.goto('/tasks');
    await expect(page.locator('.task-card').first()).toBeVisible();
    await page.locator('.task-card').first().click();
    await expect(page).toHaveURL(new RegExp(`/tasks/${TASK_ID}$`));
    await expect(page.getByText(/completed/i).first()).toBeVisible();
    await page.goto(`/artifacts/${ARTIFACT_ID}`);
    await expect(page.getByText('Synthetic cited report', { exact: true })).toBeVisible();
    await expect(page.getByText(/revision 2/i).first()).toBeVisible();
    const download = page.waitForEvent('download');
    await page.getByRole('button', { name: /download/i }).click();
    await expect((await download).suggestedFilename()).toMatch(/research-report/i);
  });

  test('executes all four chat-origin journeys through cited completion and delivery', async ({
    page,
  }) => {
    for (const [mode] of MODES) {
      // Each mode gets a fresh page-owned store so a settled run cannot leave
      // the next journey's composer in its working state.
      const journeyPage = await page.context().newPage();
      const requests = await installFixture(journeyPage, mode);
      await journeyPage.addInitScript(() => {
        localStorage.clear();
        sessionStorage.clear();
      });
      await journeyPage.goto(`/conversation/${CONVERSATION_ID}`);
      await expect(journeyPage.locator('form.composer-wrap')).toHaveAttribute(
        'data-conversation-ready',
        'true',
        { timeout: 15_000 },
      );
      const composer = journeyPage.getByLabel('Message Aura');
      const send = journeyPage.getByRole('button', { name: /^send$/i });
      await composer.fill(`Synthetic ${mode} research request`);
      await expect(send).toBeEnabled({ timeout: 15_000 });
      await send.click();
      await expect(
        journeyPage.getByText('Research activity running.', { exact: true }),
      ).toBeVisible();
      if (mode === 'auto')
        await expect(
          journeyPage.getByText('Research activity detached.', { exact: true }).first(),
        ).toBeVisible();
      await expect(
        journeyPage.getByText('Research activity completed.', { exact: true }),
      ).toBeVisible();
      expect(
        requests.filter(
          (item) =>
            item.pathname === `/api/v1/conversations/${CONVERSATION_ID}/runs` &&
            item.method === 'POST',
        ),
      ).toHaveLength(1);
      await expect(journeyPage.getByRole('link', { name: /open task/i }).first()).toBeVisible();
      await assertTaskCenterRecoveryAndCitation(journeyPage);
      await assertNotificationAndArtifactLifecycle(journeyPage);
      await journeyPage.close();
    }
  });

  test('executes all four dedicated research journeys through cited completion and delivery', async ({
    page,
  }) => {
    for (const [mode, label] of MODES) {
      const journeyPage = await page.context().newPage();
      await installFixture(journeyPage, mode);
      await journeyPage.addInitScript(() => {
        localStorage.clear();
        sessionStorage.clear();
      });
      await journeyPage.goto('/research');
      await journeyPage.getByLabel(/research question/i).fill(`Synthetic ${mode} research`);
      await journeyPage.getByRole('radio', { name: new RegExp(label, 'i') }).check();
      await journeyPage.getByRole('button', { name: /start research/i }).click();
      await expect(journeyPage).toHaveURL(new RegExp(`/research/${RESEARCH_ID}$`));
      await expect(
        journeyPage.getByText(new RegExp(`${label} research`, 'i')).first(),
      ).toBeVisible();
      await expect(journeyPage.getByText('Cited evidence', { exact: true })).toBeVisible();
      await expect(journeyPage.getByText('Synthetic source', { exact: true })).toBeVisible();
      await assertTaskCenterRecoveryAndCitation(journeyPage);
      await assertNotificationAndArtifactLifecycle(journeyPage);
      await journeyPage.close();
    }
  });

  test('renders deletion_failed and cleanup_unknown recovery states without content resurrection', async ({
    page,
  }) => {
    for (const cleanupState of ['deletion_failed', 'cleanup_unknown'] as const) {
      const recoveryPage = await page.context().newPage();
      await installFixture(recoveryPage, 'quick', cleanupState);
      await recoveryPage.goto(`/artifacts/${ARTIFACT_ID}`);
      await expect(recoveryPage.getByText('Synthetic cited report', { exact: true })).toBeVisible();
      await recoveryPage.getByRole('button', { name: /delete artifact/i }).click();
      await recoveryPage
        .getByRole('alertdialog')
        .getByRole('button', { name: /^delete$/i })
        .click();
      await expect(
        recoveryPage.getByText(/cleanup status|deletion pending|no longer readable/i).first(),
      ).toBeVisible();
      await expect(recoveryPage.locator('.preview')).toHaveCount(0);
      await expect(recoveryPage.locator('.revisions li')).toHaveCount(0);
      if (cleanupState === 'deletion_failed')
        await expect(recoveryPage.getByRole('button', { name: /retry cleanup/i })).toBeVisible();
      else
        await expect(
          recoveryPage.getByRole('button', { name: /reconcile cleanup/i }),
        ).toBeVisible();
      await recoveryPage.close();
    }
  });

  test('recovers completion, notification, artifact deletion, and task detail state', async ({
    page,
  }) => {
    const requests = await installFixture(page);
    await page.goto(`/research/${RESEARCH_ID}`);
    await expect(page.getByText(/completed/i).first()).toBeVisible();
    await page.goto('/artifacts');
    await page.locator('.artifact-card').first().click();
    await page.getByRole('button', { name: /delete artifact/i }).click();
    await expect(page.getByRole('alertdialog')).toBeVisible();
    await page.getByRole('button', { name: /^delete$/i }).click();
    await expect(
      page.getByText(/deletion pending|cleanup|no longer readable/i).first(),
    ).toBeVisible();
    await page.goto('/tasks');
    await page.locator('.task-card').first().click();
    await expect(page.getByText(/completed/i).first()).toBeVisible();
    expect(requests.some((item) => item.pathname === '/api/v1/notifications')).toBe(true);
  });
});
