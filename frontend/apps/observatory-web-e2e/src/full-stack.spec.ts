import { expect, test, type Page } from '@playwright/test';

type IdentityMode = 'local-identity' | 'external-oidc';

interface IdentityConfig {
  readonly mode: IdentityMode;
  readonly baseUrl: string;
  readonly issuer: string;
  readonly identityUrl: string;
  readonly username: string;
  readonly password: string;
  readonly ownerSubject?: string;
}

interface AcceptedRun {
  readonly conversationId: string;
  readonly runId: string;
}

/**
 * This suite is intentionally opt-in. The Compose runner supplies the
 * disposable Core and Observatory services plus fixture-only OIDC credentials.
 * It does not use synthetic telemetry: the Core UI creates the run that the
 * Observatory later observes.
 *
 * Required normal-phase variables:
 *   AURA_OBSERVATORY_FULL_STACK=1
 *   AURA_OBSERVATORY_CORE_BASE_URL (default: http://aura-web:8080)
 *   AURA_OBSERVATORY_CORE_MODEL (default: fixture-chat-2)
 *   AURA_OBSERVATORY_BASE_URL (default: http://observatory.localhost:4300)
 *   AURA_OBSERVATORY_* or existing AURA_E2E_* owner OIDC variables
 *
 * The outage phase is a separate opt-in test. The platform runner must stop
 * only Observatory services before invoking it with
 * AURA_OBSERVATORY_SERVICES_STOPPED=1; Core remains reachable and is then
 * exercised through the same authenticated browser session.
 */
const enabled = process.env['AURA_OBSERVATORY_FULL_STACK'] === '1';
const coreBaseUrl = process.env['AURA_OBSERVATORY_CORE_BASE_URL'] ?? 'http://aura-web:8080';
const coreModel = process.env['AURA_OBSERVATORY_CORE_MODEL'] ?? 'fixture-chat-2';
const observatoryBaseUrl =
  process.env['AURA_OBSERVATORY_BASE_URL'] ?? 'http://observatory.localhost:4300';
const prompt =
  process.env['AURA_OBSERVATORY_RUN_PROMPT'] ?? `AURA_OBSERVATORY_E2E_METADATA_RUN_${Date.now()}`;
const privacySentinel = process.env['AURA_OBSERVATORY_PRIVACY_SENTINEL'] ?? 'AURA0090_PRIVACY_SENTINEL';
const lifecyclePhase = process.env['AURA_OBSERVATORY_LIFECYCLE_PHASE'] ?? '';
const storageStatePath = process.env['AURA_OBSERVATORY_STORAGE_STATE_PATH'];

function envValue(...names: string[]): string | undefined {
  return names
    .map((name) => process.env[name])
    .find((value) => value !== undefined && value !== '');
}

function identityConfig(
  prefix: 'AURA_E2E_' | 'AURA_OBSERVATORY_',
  baseUrl: string,
): IdentityConfig | undefined {
  const modeValue =
    envValue(`${prefix}IDENTITY_MODE`, 'AURA_E2E_IDENTITY_MODE') ?? 'local-identity';
  const mode: IdentityMode =
    modeValue === 'external' || modeValue === 'external-oidc' ? 'external-oidc' : 'local-identity';
  const modeKey = mode === 'local-identity' ? 'LOCAL' : 'EXTERNAL';
  const value = (suffix: string): string | undefined =>
    envValue(`${prefix}${suffix}`, `AURA_E2E_${modeKey}_${suffix}`, `AURA_E2E_${suffix}`);
  const issuer = value('ISSUER');
  const identityUrl = value('IDENTITY_URL');
  const username = value('OWNER_USERNAME');
  const password = value('OWNER_PASSWORD');
  if (!issuer || !identityUrl || !username || !password) return undefined;
  return {
    mode,
    baseUrl,
    issuer,
    identityUrl,
    username,
    password,
    ownerSubject: value('OWNER_SUBJECT'),
  };
}

async function sessionStatus(
  page: Page,
  config: IdentityConfig,
  path: string,
): Promise<{ status: number; body: Record<string, unknown> }> {
  const response = await page.request.get(new URL(path, config.baseUrl).toString());
  let body: Record<string, unknown> = {};
  try {
    body = (await response.json()) as Record<string, unknown>;
  } catch {
    /* anonymous 401 bodies need not be JSON */
  }
  return { status: response.status(), body };
}

async function signIn(
  page: Page,
  config: IdentityConfig,
  sessionPath: string,
  sessionKind: 'observatory' | 'core',
): Promise<void> {
  const before = await sessionStatus(page, config, sessionPath);
  if (before.status === 200) return;
  expect([401, 403], `owner session should be anonymous before ${config.mode} login`).toContain(
    before.status,
  );

  const loginUrl = new URL('/api/v1/auth/login', config.baseUrl);
  loginUrl.searchParams.set('return_to', '/');
  await page.goto(loginUrl.toString(), { waitUntil: 'domcontentloaded' });
  await page.waitForURL((url) => url.origin === new URL(config.identityUrl).origin, {
    timeout: 30_000,
  });

  if (config.mode === 'local-identity') {
    const username = page.locator('input[name="uidField"]');
    await expect(username).toBeVisible({ timeout: 15_000 });
    await username.fill(config.username);
    await page.getByRole('button', { name: /^log in$/i }).click();
    const password = page.locator('#ak-stage-password-input');
    await expect(password).toBeVisible({ timeout: 15_000 });
    await password.fill(config.password);
    await page.getByRole('button', { name: /^continue$/i }).click();
  } else {
    const username = page.locator('input[name="username"]');
    const password = page.locator('input[name="password"]');
    await expect(username).toBeVisible({ timeout: 15_000 });
    await username.fill(config.username);
    if (await password.isVisible()) {
      await password.fill(config.password);
      await password.press('Enter');
    } else {
      await username.press('Enter');
      await expect(password).toBeVisible({ timeout: 15_000 });
      await password.fill(config.password);
      await password.press('Enter');
    }
  }

  const baseOrigin = new URL(config.baseUrl).origin;
  await page.waitForURL((url) => url.origin === baseOrigin, { timeout: 30_000 });
  await expect
    .poll(async () => (await sessionStatus(page, config, sessionPath)).status, { timeout: 15_000 })
    .toBe(200);
  const session = await sessionStatus(page, config, sessionPath);
  if (sessionKind === 'observatory') {
    const csrfToken = session.body['csrfToken'];
    expect(typeof csrfToken).toBe('string');
    expect((csrfToken as string).length).toBeGreaterThanOrEqual(32);
    expect((csrfToken as string).length).toBeLessThanOrEqual(256);
    return;
  }
  const principal = (session.body['principal'] ?? session.body) as {
    issuer?: string;
    subject?: string;
  };
  expect(principal.issuer).toBe(config.issuer);
  expect(principal.subject).toBeTruthy();
  if (config.ownerSubject) expect(principal.subject).toBe(config.ownerSubject);
}

async function createCoreRun(
  corePage: Page,
  config: IdentityConfig,
  runPrompt: string,
): Promise<AcceptedRun> {
  await corePage.goto(config.baseUrl, { waitUntil: 'domcontentloaded' });
  await expect(corePage.getByRole('status', { name: 'Loading your conversation' })).toBeHidden({
    timeout: 30_000,
  });
  const newConversation = corePage.getByRole('button', { name: 'New conversation', exact: true });
  await expect(newConversation).toBeVisible();
  await newConversation.click();
  await corePage.waitForURL((url) => url.pathname === '/conversation', { timeout: 15_000 });
  const composer = corePage.getByRole('textbox', { name: 'Message Aura', exact: true });
  await expect(composer).toBeVisible();
  await expect(composer).toBeEditable();
  const sendButton = corePage.getByRole('button', { name: /^send$/i, exact: true });
  await expect(
    corePage.getByRole('heading', { name: 'New conversation', exact: true }),
  ).toBeVisible();
  await expect(composer).toHaveValue('');
  await expect(sendButton).toBeDisabled();
  const optionsButton = corePage.getByRole('button', { name: 'Conversation options', exact: true });
  await optionsButton.click();
  const optionsMenu = corePage.getByRole('dialog', { name: 'Conversation options', exact: true });
  const modelSelect = optionsMenu.getByLabel('Model');
  await expect(modelSelect).toBeVisible();
  await expect(modelSelect.locator(`option[value="${coreModel}"]`)).toHaveCount(1);
  await modelSelect.selectOption(coreModel);
  await optionsMenu.getByRole('button', { name: 'Close', exact: true }).click();
  await expect(optionsMenu).toBeHidden({ timeout: 15_000 });
  await expect(
    corePage.getByRole('button', { name: `Model ${coreModel}`, exact: true }),
  ).toBeVisible();
  await composer.fill(runPrompt);
  await expect(composer).toHaveValue(runPrompt);
  const sendDiagnostics = async (): Promise<string> =>
    JSON.stringify({
      path: new URL(corePage.url()).pathname,
      sendEnabled: await sendButton.isEnabled().catch(() => false),
      composerEditable: await composer.isEditable().catch(() => false),
      composerLength: await composer
        .inputValue()
        .then((value) => value.length)
        .catch(() => -1),
      selectedModelVisible: await corePage
        .getByRole('button', { name: `Model ${coreModel}`, exact: true })
        .isVisible()
        .catch(() => false),
    });
  await expect(sendButton).toBeVisible();
  try {
    await expect(sendButton).toBeEnabled({ timeout: 30_000 });
  } catch {
    throw new Error(
      `Core send remained disabled after filling the composer (${await sendDiagnostics()})`,
    );
  }
  const createResponse = corePage.waitForResponse(
    (response) =>
      response.request().method() === 'POST' &&
      new URL(response.url()).pathname === '/api/v1/conversations',
    { timeout: 30_000 },
  );
  try {
    await sendButton.click();
  } catch (error) {
    void createResponse.catch(() => undefined);
    const clickError = String(error).replace(/\s+/gu, ' ').slice(0, 240);
    throw new Error(
      `Core send click was not actionable (${clickError}; ${await sendDiagnostics()})`,
    );
  }
  let response: Awaited<typeof createResponse>;
  try {
    response = await createResponse;
  } catch {
    throw new Error(
      `Core conversation POST was not observed after send (${await sendDiagnostics()})`,
    );
  }
  expect(response.status(), await response.text()).toBe(202);
  const accepted = (await response.json()) as {
    conversation?: { id?: string };
    run?: { id?: string };
  };
  const conversationId = accepted.conversation?.id;
  const runId = accepted.run?.id;
  expect(typeof conversationId).toBe('string');
  expect(typeof runId).toBe('string');
  if (typeof conversationId !== 'string' || typeof runId !== 'string')
    throw new Error('Core did not return conversation/run identifiers');
  const latestUserMessage = corePage.getByRole('article', { name: 'Your message' }).last();
  await expect(latestUserMessage.getByText(runPrompt, { exact: true })).toBeVisible();
  await expect(corePage.getByRole('article', { name: 'Aura response' }).last()).toBeVisible({
    timeout: 90_000,
  });
  return { conversationId, runId };
}

async function waitForCoreRun(
  corePage: Page,
  config: IdentityConfig,
  accepted: AcceptedRun,
): Promise<void> {
  const terminalStatuses = new Set(['failed', 'canceled', 'cancelled', 'interrupted']);
  await expect
    .poll(
      async () => {
        const response = await corePage.request.get(
          new URL(`/api/v1/conversations/${accepted.conversationId}`, config.baseUrl).toString(),
        );
        if (!response.ok()) return `http:${response.status()}`;
        const detail = (await response.json()) as {
          messages?: Array<{
            id?: string;
            role?: string;
            content?: string;
            state?: string;
            runId?: string | null;
          }>;
          recentRuns?: Array<{
            id?: string;
            status?: string;
            assistantMessageId?: string | null;
            toolLifecycleStatus?: string | null;
            toolCallsUsed?: number;
            toolCallLimit?: number;
            error?: { code?: string; message?: string; retryable?: boolean };
          }>;
          currentRun?: {
            id?: string;
            status?: string;
            assistantMessageId?: string | null;
            toolLifecycleStatus?: string | null;
            toolCallsUsed?: number;
            toolCallLimit?: number;
            error?: { code?: string; message?: string; retryable?: boolean };
          } | null;
        };
        const run =
          detail.recentRuns?.find((item) => item.id === accepted.runId) ??
          (detail.currentRun?.id === accepted.runId ? detail.currentRun : undefined);
        if (!run) return 'missing';
        const status = run.status ?? 'unknown';
        if (terminalStatuses.has(status)) {
          const code = run.error?.code?.slice(0, 80) ?? 'unknown';
          const retryable = run.error?.retryable ?? 'unknown';
          const message = (run.error?.message ?? 'unknown').replace(/\s+/gu, ' ').slice(0, 200);
          const assistant = detail.messages?.find(
            (item) =>
              item.id === run.assistantMessageId ||
              (item.role === 'assistant' && item.runId === accepted.runId),
          );
          const assistantState = ['complete', 'partial', 'interrupted', 'failed'].includes(
            assistant?.state ?? '',
          )
            ? assistant?.state
            : 'unknown';
          const assistantContentLength =
            typeof assistant?.content === 'string'
              ? Math.min(assistant.content.length, 1_000_000)
              : 'unknown';
          const toolLifecycle = [
            'awaiting_confirmation',
            'approved',
            'denied',
            'executing',
            'completed',
            'failed',
          ].includes(run.toolLifecycleStatus ?? '')
            ? run.toolLifecycleStatus
            : 'none';
          const boundedToolCount = (value: number | undefined): number | 'unknown' =>
            Number.isInteger(value) && value !== undefined && value >= 0 && value <= 1_000
              ? value
              : 'unknown';
          throw new Error(
            `Core run ${status} (code=${code}; retryable=${retryable}; message=${message}; ` +
              `assistant_state=${assistantState}; assistant_content_length=${assistantContentLength}; ` +
              `tool_lifecycle=${toolLifecycle}; tool_calls_used=${boundedToolCount(run.toolCallsUsed)}; ` +
              `tool_call_limit=${boundedToolCount(run.toolCallLimit)})`,
          );
        }
        return status;
      },
      { timeout: 120_000, intervals: [1_000, 2_000, 5_000] },
    )
    .toBe('completed');
}

type RetentionStatus = 'queued' | 'running' | 'succeeded' | 'failed' | 'unknown';

type RetentionGetObservation = {
  readonly httpStatus: number;
  status: RetentionStatus;
  failureCode: string | null;
};

type RetentionDiagnostics = {
  postSeen: boolean;
  postStatus: number | null;
  readonly getResponses: RetentionGetObservation[];
  readonly pendingBodies: Promise<void>[];
  stop: () => void;
  drain: () => Promise<void>;
};

const retentionStatuses = new Set<RetentionStatus>(['queued', 'running', 'succeeded', 'failed']);

function boundedRetentionStatus(value: unknown): RetentionStatus {
  return typeof value === 'string' && retentionStatuses.has(value as RetentionStatus)
    ? (value as RetentionStatus)
    : 'unknown';
}

function boundedRetentionFailureCode(value: unknown): string | null {
  if (value === null || value === undefined) return null;
  if (typeof value !== 'string') return 'present';
  return /^[a-z0-9_-]{1,64}$/iu.test(value) ? value : 'present';
}

function observeRetentionActions(page: Page): RetentionDiagnostics {
  const diagnostics: RetentionDiagnostics = {
    postSeen: false,
    postStatus: null,
    getResponses: [],
    pendingBodies: [],
    stop: () => undefined,
    drain: async () => undefined,
  };
  let stopped = false;
  const handler = (response: Awaited<ReturnType<Page['waitForResponse']>>): void => {
    let pathname: string;
    try {
      pathname = new URL(response.url()).pathname;
    } catch {
      return;
    }
    const request = response.request();
    if (pathname === '/api/v1/retention/actions' && request.method() === 'POST') {
      diagnostics.postSeen = true;
      diagnostics.postStatus = response.status();
      return;
    }
    if (
      !diagnostics.postSeen ||
      request.method() !== 'GET' ||
      !/^\/api\/v1\/retention\/actions\/[^/]+$/u.test(pathname)
    )
      return;
    if (diagnostics.getResponses.length >= 20) return;
    const observation: RetentionGetObservation = {
      httpStatus: response.status(),
      status: 'unknown',
      failureCode: null,
    };
    diagnostics.getResponses.push(observation);
    if (!response.ok()) return;
    const bodyTask = response
      .json()
      .then((payload: unknown) => {
        if (!payload || typeof payload !== 'object') return;
        const value = payload as { status?: unknown; failureCode?: unknown };
        observation.status = boundedRetentionStatus(value.status);
        observation.failureCode = boundedRetentionFailureCode(value.failureCode);
      })
      .catch(() => undefined);
    diagnostics.pendingBodies.push(bodyTask);
  };
  page.on('response', handler);
  diagnostics.stop = () => {
    if (stopped) return;
    stopped = true;
    page.off('response', handler);
  };
  diagnostics.drain = async () => {
    await Promise.allSettled(diagnostics.pendingBodies);
  };
  return diagnostics;
}

function retentionDiagnosticSummary(diagnostics: RetentionDiagnostics): string {
  const latest = diagnostics.getResponses.at(-1);
  const getStatuses =
    diagnostics.getResponses
      .slice(-8)
      .map(
        (item) =>
          `${item.httpStatus}:${item.status}${item.failureCode ? `:${item.failureCode}` : ''}`,
      )
      .join(',') || 'none';
  return (
    `post_seen=${diagnostics.postSeen}; post_status=${diagnostics.postStatus ?? 'none'}; ` +
    `get_count=${diagnostics.getResponses.length}; latest_get=${latest ? `${latest.httpStatus}:${latest.status}` : 'none'}; ` +
    `get_statuses=${getStatuses}`
  );
}

async function waitForRetentionTerminal(
  page: Page,
  diagnostics: RetentionDiagnostics,
): Promise<void> {
  let observedState = 'pending';
  try {
    await expect
      .poll(
        async () => {
          const result = page.locator('observatory-retention .result');
          if (await result.isVisible().catch(() => false)) {
            const resultText = (await result.innerText().catch(() => ''))
              .replace(/\s+/gu, ' ')
              .slice(0, 120)
              .toLowerCase();
            if (resultText.includes('succeeded')) {
              observedState = 'succeeded';
              return observedState;
            }
            if (resultText.includes('failed')) {
              observedState = 'failed';
              return observedState;
            }
          }
          if (
            await page
              .locator('observatory-retention .failure')
              .isVisible()
              .catch(() => false)
          ) {
            observedState = 'failure';
            return observedState;
          }
          if (
            await page
              .locator('main[aria-labelledby="auth-recovery-title"]')
              .isVisible()
              .catch(() => false)
          ) {
            observedState = 'session-recovery';
            return observedState;
          }
          return observedState;
        },
        { timeout: 60_000, intervals: [500, 1_000, 2_000] },
      )
      .toBe('succeeded');
  } catch {
    await diagnostics.drain();
    const retentionText =
      (
        await page
          .locator('observatory-retention .progress, observatory-retention .failure')
          .allTextContents()
      )
        .join(' ')
        .replace(/https?:\/\/\S+/giu, '[url]')
        .replace(/[A-Za-z0-9+/=_-]{40,}/gu, '[redacted]')
        .replace(/\s+/gu, ' ')
        .slice(0, 160) || 'none';
    throw new Error(
      `Retention action did not succeed (observed_state=${observedState}; ${retentionDiagnosticSummary(diagnostics)}; ` +
        `retention_text=${retentionText})`,
    );
  } finally {
    diagnostics.stop();
  }
}

async function deletedCount(page: Page, label: string): Promise<number> {
  const value = page
    .locator('observatory-retention .result div')
    .filter({ hasText: label })
    .getByRole('definition');
  await expect(value).toBeVisible();
  return Number.parseInt((await value.textContent())?.trim() ?? '0', 10);
}

async function acceptDestructiveConfirmation(page: Page): Promise<void> {
  const dialog = page.getByRole('dialog');
  if (await dialog.isVisible().catch(() => false))
    await dialog
      .getByRole('button', { name: /confirm|delete|purge/i })
      .last()
      .click();
}

test.describe('Observatory real Core-to-browser acceptance', () => {
  test.skip(!enabled, 'set AURA_OBSERVATORY_FULL_STACK=1 in the disposable Compose runner');
  test.describe.configure({ mode: 'serial' });

  test('authenticates owner, creates a real Core run, observes it through SSE, inspects measurements, and executes retention', async ({
    page,
    context,
  }) => {
    const observatory = identityConfig('AURA_OBSERVATORY_', observatoryBaseUrl);
    const core = identityConfig('AURA_E2E_', coreBaseUrl);
    test.skip(!observatory || !core, 'provide both Observatory and Core owner OIDC variables');
    if (!observatory || !core) return;

    await signIn(page, observatory, '/api/v1/auth/me', 'observatory');
    let sseOpened = false;
    const observedRunResponses: Array<{ readonly count: number; readonly runIds: string[] }> = [];
    const runResponseTasks: Promise<void>[] = [];
    const recordRunResponse = (response: Awaited<ReturnType<Page['waitForResponse']>>): void => {
      const request = response.request();
      let pathname: string;
      try {
        pathname = new URL(response.url()).pathname;
      } catch {
        return;
      }
      if (request.method() !== 'GET' || pathname !== '/api/v1/runs' || !response.ok()) return;
      const task = response
        .json()
        .then((payload: unknown) => {
          const value =
            payload && typeof payload === 'object' ? (payload as { items?: unknown }) : {};
          const items = Array.isArray(value.items) ? value.items : [];
          const runIds = items.slice(0, 50).flatMap((item: unknown) => {
            if (!item || typeof item !== 'object') return [];
            const runId = (item as { runId?: unknown }).runId;
            return typeof runId === 'string' && runId.length <= 128 ? [runId] : [];
          });
          if (observedRunResponses.length < 100)
            observedRunResponses.push({ count: items.length, runIds });
        })
        .catch(() => undefined);
      runResponseTasks.push(task);
    };
    page.on('response', recordRunResponse);
    const liveEventsResponse = page.waitForResponse(
      (response) =>
        response.request().method() === 'GET' &&
        new URL(response.url()).pathname === '/api/v1/events',
      { timeout: 30_000 },
    );
    await page.goto('/', { waitUntil: 'domcontentloaded' });
    const eventsResponse = await liveEventsResponse;
    expect(eventsResponse.status()).toBe(200);
    sseOpened = true;
    await expect(
      page.getByRole('heading', { level: 1, name: 'Operational metadata' }),
    ).toBeVisible();

    const corePage = await context.newPage();
    await signIn(corePage, core, '/api/v1/auth/session', 'core');
    const accepted = await createCoreRun(corePage, core, prompt);
    await waitForCoreRun(corePage, core, accepted);

    await page.getByRole('button', { name: 'Runs', exact: true }).click();
    const acceptedRunButton = page.getByRole('button', { name: accepted.runId, exact: true });
    try {
      await expect(acceptedRunButton).toBeVisible({ timeout: 60_000 });
    } catch {
      await Promise.allSettled(runResponseTasks);
      let restDiagnostic = { status: 0, containsTarget: false };
      try {
        restDiagnostic = await page.evaluate(async (targetRunId) => {
          const response = await fetch('/api/v1/runs?limit=50', {
            cache: 'no-store',
            credentials: 'same-origin',
            headers: { Accept: 'application/json' },
          });
          let containsTarget = false;
          if (response.ok) {
            const payload = (await response.json()) as unknown;
            const value =
              payload && typeof payload === 'object' ? (payload as { items?: unknown }) : {};
            containsTarget =
              Array.isArray(value.items) &&
              value.items.some(
                (item: unknown) =>
                  Boolean(item) &&
                  typeof item === 'object' &&
                  (item as { runId?: unknown }).runId === targetRunId,
              );
          }
          return { status: response.status, containsTarget };
        }, accepted.runId);
      } catch {
        // Keep the diagnostic content-free when the same-origin probe cannot start.
      }
      const observedTarget = observedRunResponses.some((response) =>
        response.runIds.includes(accepted.runId),
      );
      const connectionStatus = await page
        .locator('.connection')
        .innerText()
        .catch(() => 'unavailable');
      const visibleRunButtonCount = await acceptedRunButton
        .evaluateAll(
          (buttons) =>
            buttons.filter((button) => {
              const style = window.getComputedStyle(button);
              const bounds = button.getBoundingClientRect();
              return (
                style.display !== 'none' &&
                style.visibility !== 'hidden' &&
                bounds.width > 0 &&
                bounds.height > 0
              );
            }).length,
        )
        .catch(() => 0);
      throw new Error(
        `Observatory run did not render (sse_opened=${sseOpened}; observed_target=${observedTarget}; ` +
          `rest_status=${restDiagnostic.status}; rest_contains_target=${restDiagnostic.containsTarget}; ` +
          `connection_status=${connectionStatus.replace(/\s+/gu, ' ').slice(0, 80)}; ` +
          `visible_run_buttons=${visibleRunButtonCount})`,
      );
    }
    await expect(
      page
        .getByRole('status')
        .filter({ hasText: /live updates|disconnected|reconnecting/i })
        .first(),
    ).toBeVisible();
    await page.getByRole('button', { name: accepted.runId, exact: true }).click();
    await page
      .getByRole('navigation', { name: 'Observatory sections' })
      .getByRole('button', { name: 'Trace', exact: true })
      .click();
    await expect(page.getByText(/trace waterfall/i)).toBeVisible();
    const traceList = page.getByRole('list', { name: 'Ordered trace spans' });
    await expect(traceList).toHaveCount(1);
    const selectedSpan = traceList.getByRole('listitem').first();
    const componentId = (await selectedSpan.locator('small').textContent())?.trim() ?? '';
    expect(componentId).toMatch(/^aura\./);
    await expect(selectedSpan.getByText(/\d+ ms/)).toBeVisible();
    await expect(selectedSpan.getByText(/succeeded|failed|cancelled|unknown/i)).toBeVisible();

    await page.getByRole('button', { name: 'Components', exact: true }).click();
    const componentCard = page
      .locator('observatory-components article')
      .filter({ hasText: componentId });
    await expect(componentCard).toHaveCount(1, { timeout: 30_000 });
    await expect(componentCard.getByText(/latency|errors?/i).first()).toBeVisible();

    // The run above must be visible through the real bounded analytics query,
    // not only through the run/trace projections.  Keep this assertion at the
    // browser boundary so the acceptance path covers the generated client,
    // ClickHouse aggregation, and the semantic table paired with the chart.
    await page.getByRole('button', { name: 'Analytics', exact: true }).click();
    await expect(page.getByRole('heading', { name: 'Metric explorer', exact: true })).toBeVisible();
    await expect(page.getByText(/rows scanned/i)).toBeVisible({ timeout: 30_000 });
    await expect(page.getByRole('table', { name: /data in milliseconds/i }).first()).toBeVisible({
      timeout: 30_000,
    });
    await expect(page.locator('observatory-analytics')).not.toContainText(
      /prompt|response|credential|bearer token/i,
    );

    // Create a second real Core run before retention so the comparison path
    // exercises the same production telemetry, ClickHouse query, generated
    // client, and browser workspace as the first run.  Run identifiers must
    // remain in the authenticated POST body, never in a URL or browser store.
    const comparisonRun = await createCoreRun(corePage, core, `${prompt}_COMPARE`);
    await waitForCoreRun(corePage, core, comparisonRun);
    const comparisonRequests: string[] = [];
    const comparisonRequestFingerprints = new Set<string>();
    const recordComparisonRequest = (
      request: Awaited<ReturnType<Page['waitForRequest']>>,
    ): void => {
      try {
        if (
          request.method() === 'POST' &&
          new URL(request.url()).pathname === '/api/v1/run-comparisons/query'
        ) {
          const url = request.url();
          const fingerprint = `${url}\n${request.postData() ?? ''}`;
          if (comparisonRequestFingerprints.has(fingerprint)) return;
          comparisonRequestFingerprints.add(fingerprint);
          comparisonRequests.push(url);
        }
      } catch {
        // Ignore malformed diagnostic URLs without exposing request bodies.
      }
    };
    page.on('request', recordComparisonRequest);
    await page.getByRole('button', { name: 'Runs', exact: true }).click();
    await expect(page.getByRole('button', { name: comparisonRun.runId, exact: true })).toBeVisible({
      timeout: 60_000,
    });
    await page.getByRole('button', { name: 'Compare', exact: true }).click();
    const comparison = page.locator('observatory-comparison');
    await comparison.getByRole('combobox', { name: 'Left run' }).selectOption(accepted.runId);
    await comparison.getByRole('combobox', { name: 'Right run' }).selectOption(comparisonRun.runId);
    await comparison.getByRole('button', { name: 'Compare selected runs', exact: true }).click();
    await expect(
      comparison.getByRole('heading', {
        name: new RegExp(`${accepted.runId} versus ${comparisonRun.runId}`),
      }),
    ).toBeVisible({ timeout: 30_000 });
    await expect(
      comparison.getByRole('region', { name: 'Run comparison summary table' }).getByRole('table'),
    ).toBeVisible();
    await expect(
      comparison
        .getByRole('region', { name: 'Component measurement differences table' })
        .getByRole('table'),
    ).toBeVisible();
    await expect(
      comparison.getByRole('region', { name: 'Aligned trace comparison table' }).getByRole('table'),
    ).toBeVisible();
    // A persisted live event may invalidate the comparison immediately after
    // the explicit submit and cause one bounded refresh.  Wait for that short
    // settle window, then prove the request stream is finite and stable rather
    // than making the acceptance depend on the exact SSE scheduling order.
    await expect
      .poll(() => comparisonRequests.length, {
        timeout: 5_000,
        intervals: [250, 500, 1_000],
      })
      .toBeGreaterThan(0);
    const settledComparisonRequestCount = comparisonRequests.length;
    await page.waitForTimeout(1_000);
    expect(comparisonRequests.length).toBe(settledComparisonRequestCount);
    expect(settledComparisonRequestCount).toBeLessThanOrEqual(2);
    for (const requestUrl of comparisonRequests) {
      expect(requestUrl).not.toContain(accepted.runId);
      expect(requestUrl).not.toContain(comparisonRun.runId);
    }
    const comparisonStorage = await page.evaluate(() => ({
      local: Object.keys(localStorage),
      session: Object.keys(sessionStorage),
      url: location.href,
    }));
    expect(comparisonStorage.local).toEqual([]);
    expect(comparisonStorage.session).toEqual([]);
    expect(comparisonStorage.url).not.toMatch(/run-comparisons|leftRunId|rightRunId/);
    page.off('request', recordComparisonRequest);

    // Send one non-secret synthetic reference through the real Core run and
    // prove it does not cross the Observatory metadata surfaces.  The page
    // evaluates only booleans so a failing diagnostic can never print it.
    const privacyScan = await page.evaluate(async (sentinel) => {
      const response = await fetch('/api/v1/events', {
        credentials: 'same-origin',
        headers: { Accept: 'text/event-stream' },
      });
      const reader = response.body?.getReader();
      const firstChunk = reader ? await reader.read() : { value: undefined };
      await reader?.cancel();
      const eventText = firstChunk.value ? new TextDecoder().decode(firstChunk.value) : '';
      const resourceUrls = performance.getEntriesByType('resource').map((entry) => entry.name);
      const storedValues = [
        ...Object.keys(localStorage).map((key) => localStorage.getItem(key) ?? ''),
        ...Object.keys(sessionStorage).map((key) => sessionStorage.getItem(key) ?? ''),
      ];
      return {
        eventSafe: !eventText.includes(sentinel),
        urlSafe: !resourceUrls.some((url) => url.includes(sentinel)),
        storageSafe: !storedValues.some((value) => value.includes(sentinel)),
      };
    }, privacySentinel);
    expect(privacyScan).toEqual({ eventSafe: true, urlSafe: true, storageSafe: true });

    await page.getByRole('button', { name: 'Retention', exact: true }).click();
    await expect(
      page.getByRole('heading', { name: 'Delete stored observations', exact: true }),
    ).toBeVisible();
    await page
      .getByRole('textbox', { name: 'Delete observations before (UTC)' })
      .fill('2099-01-01T00:00');
    const cutoffDiagnostics = observeRetentionActions(page);
    await page.getByRole('button', { name: 'Delete before cutoff', exact: true }).click();
    await acceptDestructiveConfirmation(page);
    await waitForRetentionTerminal(page, cutoffDiagnostics);
    expect(await deletedCount(page, 'Deleted observations')).toBeGreaterThan(0);
    await page.getByRole('button', { name: 'Runs', exact: true }).click();
    await expect(
      page.locator('observatory-runs').getByRole('button', { name: accepted.runId, exact: true }),
    ).toHaveCount(0);

    const purgeRun = await createCoreRun(corePage, core, `${prompt}_PURGE`);
    await waitForCoreRun(corePage, core, purgeRun);
    await page.getByRole('button', { name: 'Runs', exact: true }).click();
    await expect(
      page.locator('observatory-runs').getByRole('button', { name: purgeRun.runId, exact: true }),
    ).toBeVisible({ timeout: 120_000 });
    await page.getByRole('button', { name: 'Components', exact: true }).click();
    await expect(
      page.locator('observatory-components article').filter({ hasText: componentId }),
    ).toHaveCount(1);

    await page.getByRole('button', { name: 'Retention', exact: true }).click();
    const purgeDiagnostics = observeRetentionActions(page);
    await page.getByRole('button', { name: 'Purge all observations', exact: true }).click();
    await acceptDestructiveConfirmation(page);
    await waitForRetentionTerminal(page, purgeDiagnostics);
    expect(await deletedCount(page, 'Deleted observations')).toBeGreaterThan(0);
    await page.getByRole('button', { name: 'Runs', exact: true }).click();
    await expect(
      page.locator('observatory-runs').getByRole('button', { name: purgeRun.runId, exact: true }),
    ).toHaveCount(0);
    await page.getByRole('button', { name: 'Components', exact: true }).click();
    await expect(
      page.locator('observatory-components article').filter({ hasText: componentId }),
    ).toHaveCount(1);
    if (storageStatePath) await context.storageState({ path: storageStatePath });
  });

  test('preserves the owner session across a disposable Observatory restart', async ({ page }) => {
    test.skip(
      lifecyclePhase !== 'preserved',
      'runner must restart the disposable Observatory services with session volume retained',
    );
    const observatory = identityConfig('AURA_OBSERVATORY_', observatoryBaseUrl);
    test.skip(!observatory, 'provide Observatory owner OIDC variables');
    if (!observatory) return;
    await expect
      .poll(async () => (await sessionStatus(page, observatory, '/api/v1/auth/me')).status, {
        timeout: 30_000,
      })
      .toBe(200);
    const eventsResponse = page.waitForResponse(
      (response) =>
        response.request().method() === 'GET' &&
        new URL(response.url()).pathname === '/api/v1/events',
      { timeout: 30_000 },
    );
    await page.goto('/', { waitUntil: 'domcontentloaded' });
    expect((await eventsResponse).status()).toBe(200);
    await expect(
      page.getByRole('heading', { level: 1, name: 'Operational metadata' }),
    ).toBeVisible();
  });

  test('rejects the old session after reset and permits owner reauthentication', async ({
    page,
  }) => {
    test.skip(
      lifecyclePhase !== 'reset',
      'runner must remove only the disposable Observatory session volume',
    );
    const observatory = identityConfig('AURA_OBSERVATORY_', observatoryBaseUrl);
    test.skip(!observatory, 'provide Observatory owner OIDC variables');
    if (!observatory) return;
    await expect
      .poll(async () => (await sessionStatus(page, observatory, '/api/v1/auth/me')).status, {
        timeout: 30_000,
      })
      .toBe(401);
    await signIn(page, observatory, '/api/v1/auth/me', 'observatory');
    await page.goto('/', { waitUntil: 'domcontentloaded' });
    await expect(
      page.getByRole('heading', { level: 1, name: 'Operational metadata' }),
    ).toBeVisible();
  });

  test('keeps Core usable after the platform stops Observatory services', async ({ page }) => {
    test.skip(
      process.env['AURA_OBSERVATORY_SERVICES_STOPPED'] !== '1',
      'platform runner must stop Observatory services and set AURA_OBSERVATORY_SERVICES_STOPPED=1',
    );
    const core = identityConfig('AURA_E2E_', coreBaseUrl);
    test.skip(!core, 'provide Core owner OIDC variables');
    if (!core) return;
    await signIn(page, core, '/api/v1/auth/session', 'core');
    const outagePrompt = `${prompt}_CORE_STILL_USABLE`;
    // Reuse the normal Core path so this outage assertion waits for
    // asynchronous conversation/model/agent readiness, verifies that the
    // configured selectable model is present, and still proves a completed
    // response after Observatory services have stopped.
    const accepted = await createCoreRun(page, core, outagePrompt);
    await waitForCoreRun(page, core, accepted);
  });
});
