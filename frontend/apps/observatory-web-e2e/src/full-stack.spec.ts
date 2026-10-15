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
const observatoryBaseUrl = process.env['AURA_OBSERVATORY_BASE_URL'] ?? 'http://observatory.localhost:4300';
const prompt = process.env['AURA_OBSERVATORY_RUN_PROMPT'] ?? `AURA_OBSERVATORY_E2E_METADATA_RUN_${Date.now()}`;

function envValue(...names: string[]): string | undefined {
  return names.map((name) => process.env[name]).find((value) => value !== undefined && value !== '');
}

function identityConfig(prefix: 'AURA_E2E_' | 'AURA_OBSERVATORY_', baseUrl: string): IdentityConfig | undefined {
  const modeValue = envValue(`${prefix}IDENTITY_MODE`, 'AURA_E2E_IDENTITY_MODE') ?? 'local-identity';
  const mode: IdentityMode = modeValue === 'external' || modeValue === 'external-oidc' ? 'external-oidc' : 'local-identity';
  const modeKey = mode === 'local-identity' ? 'LOCAL' : 'EXTERNAL';
  const value = (suffix: string): string | undefined => envValue(
    `${prefix}${suffix}`,
    `AURA_E2E_${modeKey}_${suffix}`,
    `AURA_E2E_${suffix}`,
  );
  const issuer = value('ISSUER');
  const identityUrl = value('IDENTITY_URL');
  const username = value('OWNER_USERNAME');
  const password = value('OWNER_PASSWORD');
  if (!issuer || !identityUrl || !username || !password) return undefined;
  return { mode, baseUrl, issuer, identityUrl, username, password, ownerSubject: value('OWNER_SUBJECT') };
}

async function sessionStatus(page: Page, config: IdentityConfig, path: string): Promise<{ status: number; body: Record<string, unknown> }> {
  const response = await page.request.get(new URL(path, config.baseUrl).toString());
  let body: Record<string, unknown> = {};
  try { body = (await response.json()) as Record<string, unknown>; } catch { /* anonymous 401 bodies need not be JSON */ }
  return { status: response.status(), body };
}

async function signIn(page: Page, config: IdentityConfig, sessionPath: string, sessionKind: 'observatory' | 'core'): Promise<void> {
  const before = await sessionStatus(page, config, sessionPath);
  if (before.status === 200) return;
  expect([401, 403], `owner session should be anonymous before ${config.mode} login`).toContain(before.status);

  const loginUrl = new URL('/api/v1/auth/login', config.baseUrl);
  loginUrl.searchParams.set('return_to', '/');
  await page.goto(loginUrl.toString(), { waitUntil: 'domcontentloaded' });
  await page.waitForURL((url) => url.origin === new URL(config.identityUrl).origin, { timeout: 30_000 });

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
  await expect.poll(async () => (await sessionStatus(page, config, sessionPath)).status, { timeout: 15_000 }).toBe(200);
  const session = await sessionStatus(page, config, sessionPath);
  if (sessionKind === 'observatory') {
    const csrfToken = session.body['csrfToken'];
    expect(typeof csrfToken).toBe('string');
    expect((csrfToken as string).length).toBeGreaterThanOrEqual(32);
    expect((csrfToken as string).length).toBeLessThanOrEqual(256);
    return;
  }
  const principal = (session.body['principal'] ?? session.body) as { issuer?: string; subject?: string };
  expect(principal.issuer).toBe(config.issuer);
  expect(principal.subject).toBeTruthy();
  if (config.ownerSubject) expect(principal.subject).toBe(config.ownerSubject);
}

async function createCoreRun(corePage: Page, config: IdentityConfig, runPrompt: string): Promise<AcceptedRun> {
  await corePage.goto(config.baseUrl, { waitUntil: 'domcontentloaded' });
  await expect(corePage.getByRole('heading', { name: /aura/i }).first()).toBeVisible();
  const composer = corePage.getByRole('textbox', { name: /message aura|message|prompt/i }).first();
  await expect(composer).toBeVisible();
  const createResponse = corePage.waitForResponse((response) => response.request().method() === 'POST' && new URL(response.url()).pathname === '/api/v1/conversations');
  await composer.fill(runPrompt);
  await corePage.getByRole('button', { name: /^send$/i }).click();
  const response = await createResponse;
  expect(response.status(), await response.text()).toBe(202);
  const accepted = (await response.json()) as { conversation?: { id?: string }; run?: { id?: string } };
  const conversationId = accepted.conversation?.id;
  const runId = accepted.run?.id;
  expect(typeof conversationId).toBe('string');
  expect(typeof runId).toBe('string');
  if (typeof conversationId !== 'string' || typeof runId !== 'string') throw new Error('Core did not return conversation/run identifiers');
  await expect(corePage.getByText(runPrompt)).toBeVisible();
  await expect(corePage.getByRole('article', { name: 'Aura response' }).last()).toBeVisible({ timeout: 90_000 });
  return { conversationId, runId };
}

async function waitForCoreRun(corePage: Page, config: IdentityConfig, accepted: AcceptedRun): Promise<void> {
  await expect.poll(async () => {
    const response = await corePage.request.get(new URL(`/api/v1/conversations/${accepted.conversationId}`, config.baseUrl).toString());
    if (!response.ok()) return false;
    const detail = await response.json() as { recentRuns?: Array<{ id?: string; status?: string }>; currentRun?: { id?: string; status?: string } | null };
    const run = detail.recentRuns?.find((item) => item.id === accepted.runId) ?? (detail.currentRun?.id === accepted.runId ? detail.currentRun : undefined);
    return run?.status === 'completed';
  }, { timeout: 120_000, intervals: [1_000, 2_000, 5_000] }).toBe(true);
}

async function waitForRetentionTerminal(page: Page): Promise<void> {
  await expect(page.locator('observatory-retention .result').getByText(/succeeded|failed/i)).toBeVisible({ timeout: 60_000 });
}

async function deletedCount(page: Page, label: string): Promise<number> {
  const value = page.locator('observatory-retention .result div').filter({ hasText: label }).getByRole('definition');
  await expect(value).toBeVisible();
  return Number.parseInt((await value.textContent())?.trim() ?? '0', 10);
}

async function acceptDestructiveConfirmation(page: Page): Promise<void> {
  const dialog = page.getByRole('dialog');
  if (await dialog.isVisible().catch(() => false)) await dialog.getByRole('button', { name: /confirm|delete|purge/i }).last().click();
}

test.describe('Observatory real Core-to-browser acceptance', () => {
  test.skip(!enabled, 'set AURA_OBSERVATORY_FULL_STACK=1 in the disposable Compose runner');
  test.describe.configure({ mode: 'serial' });

  test('authenticates owner, creates a real Core run, observes it through SSE, inspects measurements, and executes retention', async ({ page, context }) => {
    const observatory = identityConfig('AURA_OBSERVATORY_', observatoryBaseUrl);
    const core = identityConfig('AURA_E2E_', coreBaseUrl);
    test.skip(!observatory || !core, 'provide both Observatory and Core owner OIDC variables');
    if (!observatory || !core) return;

    await signIn(page, observatory, '/api/v1/auth/me', 'observatory');
    await page.goto('/', { waitUntil: 'domcontentloaded' });
    await expect(page.getByRole('heading', { level: 1, name: 'Operational metadata' })).toBeVisible();

    const corePage = await context.newPage();
    await signIn(corePage, core, '/api/v1/auth/session', 'core');
    const accepted = await createCoreRun(corePage, core, prompt);
    await waitForCoreRun(corePage, core, accepted);

    await page.getByRole('button', { name: 'Runs', exact: true }).click();
    await expect(page.getByRole('button', { name: accepted.runId, exact: true })).toBeVisible({ timeout: 120_000 });
    await expect(page.getByRole('status').filter({ hasText: /live updates|disconnected|reconnecting/i }).first()).toBeVisible();
    await page.getByRole('button', { name: accepted.runId, exact: true }).click();
    await page.getByRole('navigation', { name: 'Observatory sections' }).getByRole('button', { name: 'Trace', exact: true }).click();
    await expect(page.getByText(/trace waterfall/i)).toBeVisible();
    const traceList = page.getByRole('list', { name: 'Ordered trace spans' });
    await expect(traceList).toHaveCount(1);
    const selectedSpan = traceList.getByRole('listitem').first();
    const componentId = (await selectedSpan.locator('small').textContent())?.trim() ?? '';
    expect(componentId).toMatch(/^aura\./);
    await expect(selectedSpan.getByText(/\d+ ms/)).toBeVisible();
    await expect(selectedSpan.getByText(/succeeded|failed|cancelled|unknown/i)).toBeVisible();

    await page.getByRole('button', { name: 'Components', exact: true }).click();
    const componentCard = page.locator('observatory-components article').filter({ hasText: componentId });
    await expect(componentCard).toHaveCount(1, { timeout: 30_000 });
    await expect(componentCard.getByText(/latency|errors?/i).first()).toBeVisible();

    await page.getByRole('button', { name: 'Retention', exact: true }).click();
    await expect(page.getByRole('heading', { name: 'Delete stored observations', exact: true })).toBeVisible();
    await page.getByRole('textbox', { name: 'Delete observations before (UTC)' }).fill('2099-01-01T00:00');
    await page.getByRole('button', { name: 'Delete before cutoff', exact: true }).click();
    await acceptDestructiveConfirmation(page);
    await waitForRetentionTerminal(page);
    expect(await deletedCount(page, 'Deleted observations')).toBeGreaterThan(0);
    await page.getByRole('button', { name: 'Runs', exact: true }).click();
    await expect(page.locator('observatory-runs').getByRole('button', { name: accepted.runId, exact: true })).toHaveCount(0);

    const purgeRun = await createCoreRun(corePage, core, `${prompt}_PURGE`);
    await waitForCoreRun(corePage, core, purgeRun);
    await page.getByRole('button', { name: 'Runs', exact: true }).click();
    await expect(page.locator('observatory-runs').getByRole('button', { name: purgeRun.runId, exact: true })).toBeVisible({ timeout: 120_000 });
    await page.getByRole('button', { name: 'Components', exact: true }).click();
    await expect(page.locator('observatory-components article').filter({ hasText: componentId })).toHaveCount(1);

    await page.getByRole('button', { name: 'Retention', exact: true }).click();
    await page.getByRole('button', { name: 'Purge all observations', exact: true }).click();
    await acceptDestructiveConfirmation(page);
    await waitForRetentionTerminal(page);
    expect(await deletedCount(page, 'Deleted observations')).toBeGreaterThan(0);
    await page.getByRole('button', { name: 'Runs', exact: true }).click();
    await expect(page.locator('observatory-runs').getByRole('button', { name: purgeRun.runId, exact: true })).toHaveCount(0);
    await page.getByRole('button', { name: 'Components', exact: true }).click();
    await expect(page.locator('observatory-components article').filter({ hasText: componentId })).toHaveCount(1);
  });

  test('keeps Core usable after the platform stops Observatory services', async ({ page }) => {
    test.skip(process.env['AURA_OBSERVATORY_SERVICES_STOPPED'] !== '1', 'platform runner must stop Observatory services and set AURA_OBSERVATORY_SERVICES_STOPPED=1');
    const core = identityConfig('AURA_E2E_', coreBaseUrl);
    test.skip(!core, 'provide Core owner OIDC variables');
    if (!core) return;
    await signIn(page, core, '/api/v1/auth/session', 'core');
    const outagePrompt = `${prompt}_CORE_STILL_USABLE`;
    const composer = page.getByRole('textbox', { name: /message aura|message|prompt/i }).first();
    await expect(composer).toBeVisible();
    await composer.fill(outagePrompt);
    await page.getByRole('button', { name: /^send$/i }).click();
    await expect(page.getByText(outagePrompt)).toBeVisible();
    await expect(page.getByRole('article', { name: 'Aura response' }).last()).toBeVisible({ timeout: 90_000 });
  });
});
