import { expect, test, type Page } from '@playwright/test';

type IdentityMode = 'local-identity' | 'external-oidc';

interface StackConfig {
  readonly mode: IdentityMode;
  readonly baseUrl: string;
  readonly issuer: string;
  readonly identityUrl: string;
  readonly username: string;
  readonly password: string;
  readonly ownerSubject?: string;
}

const configuredModes = (process.env['AURA_E2E_IDENTITY_MODES'] ?? process.env['AURA_E2E_IDENTITY_MODE'] ?? 'local-identity')
  .split(',')
  .map((mode) => mode.trim())
  .map((mode): IdentityMode | undefined => mode === 'local' ? 'local-identity' : mode === 'external' ? 'external-oidc' : mode === 'local-identity' || mode === 'external-oidc' ? mode : undefined)
  .filter((mode): mode is IdentityMode => mode !== undefined);

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

function valueFor(mode: IdentityMode, suffix: string): string | undefined {
  const modeKey = mode === 'local-identity' ? 'LOCAL' : 'EXTERNAL';
  return process.env[`AURA_E2E_${modeKey}_${suffix}`] ?? process.env[`AURA_E2E_${suffix}`];
}

function stackConfig(mode: IdentityMode): StackConfig | undefined {
  const baseUrl = valueFor(mode, 'BASE_URL') ?? process.env['AURA_E2E_BASE_URL'] ?? process.env['BASE_URL'];
  const issuer = valueFor(mode, 'ISSUER');
  const identityUrl = valueFor(mode, 'IDENTITY_URL');
  const username = valueFor(mode, 'OWNER_USERNAME');
  const password = valueFor(mode, 'OWNER_PASSWORD');
  if (!baseUrl || !issuer || !identityUrl || !username || !password) return undefined;
  return {
    mode,
    baseUrl,
    issuer,
    identityUrl,
    username,
    password,
    ownerSubject: valueFor(mode, 'OWNER_SUBJECT'),
  };
}

async function apiSession(page: Page, config: StackConfig): Promise<{ status: number; body: Record<string, unknown> }> {
  const response = await page.request.get(new URL('/api/v1/auth/session', config.baseUrl).toString());
  return { status: response.status(), body: (await response.json()) as Record<string, unknown> };
}

async function signIn(page: Page, config: StackConfig): Promise<void> {
  await page.goto(config.baseUrl);
  const before = await apiSession(page, config);
  if (before.status === 200) return;
  expect([401, 403], `Aura session should be anonymous before ${config.mode} login`).toContain(before.status);

  await page.getByRole('button', { name: /sign in with authentik/i }).click();
  await page.waitForURL((url) => url.origin === new URL(config.identityUrl).origin, { timeout: 30_000 });

  const authentikUsername = page.locator('input[name="uidField"]');
  const authentikPassword = page.locator('#ak-stage-password-input');
  if (config.mode === 'local-identity') {
    await expect(authentikUsername).toBeVisible({ timeout: 15_000 });
    await authentikUsername.fill(config.username);
    await page.getByRole('button', { name: /^log in$/i }).click();
    await expect(authentikPassword).toBeVisible({ timeout: 15_000 });
    await authentikPassword.fill(config.password);
    await page.getByRole('button', { name: /^continue$/i }).click();
  } else {
    // The deterministic external fixture uses one combined form.  Keep these
    // selectors exact so auxiliary password-manager fields cannot be chosen.
    const username = page.locator('input[name="username"]');
    const password = page.locator('input[name="password"]');
    await expect(username).toBeVisible();
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
  const identityOrigin = new URL(config.identityUrl).origin;
  // Authentik may render explicit consent after a redirect, while the
  // deterministic external provider redirects directly to Aura. Wait for
  // either terminal transition rather than probing the consent DOM too early.
  if (new URL(page.url()).origin !== baseOrigin) {
    await page.waitForURL(
      (url) =>
        url.origin === baseOrigin ||
        (url.origin === identityOrigin && url.pathname.includes('explicit-consent')),
      { timeout: 30_000 },
    );
  }
  if (new URL(page.url()).origin === identityOrigin) {
    const consent = page.getByRole('button', { name: /^continue$/i }).first();
    const auraTransition = page
      .waitForURL((url) => url.origin === baseOrigin, { timeout: 30_000 })
      .then(() => 'aura' as const)
      .catch(() => 'timeout' as const);
    const consentTransition = expect(consent)
      .toBeVisible({ timeout: 30_000 })
      .then(() => 'consent' as const)
      .catch(() => 'timeout' as const);
    const transition = await Promise.race([auraTransition, consentTransition]);
    const afterTransition = new URL(page.url());
    if (transition === 'consent' && afterTransition.origin === identityOrigin && await consent.isVisible()) {
      await consent.click();
    } else if (afterTransition.origin !== baseOrigin) {
      throw new Error('OIDC consent did not transition to Aura');
    }
  }
  await page.waitForURL(
    (url) => url.origin === baseOrigin && !url.pathname.includes('/api/v1/auth/callback'),
    { timeout: 30_000 },
  );

  await expect.poll(async () => (await apiSession(page, config)).status, { timeout: 15_000 }).toBe(200);
  const session = await apiSession(page, config);
  expect(session.body['principal']).toBeTruthy();
  const principal = session.body['principal'] as { issuer?: string; subject?: string };
  expect(principal.issuer).toBe(config.issuer);
  expect(principal.subject).toBeTruthy();
  if (config.ownerSubject) expect(principal.subject).toBe(config.ownerSubject);
}

async function waitForModelCatalog(page: Page): Promise<void> {
  const picker = page.getByLabel('Model');
  await expect(picker).toBeVisible();
  const selectableModels = picker.locator('option:not([disabled])[value]:not([value=""])');
  await expect(selectableModels).toHaveCount(2);
  await expect(picker.locator('option[value="embed"]')).toHaveAttribute('disabled', '');
}

async function selectSecondChatModel(page: Page): Promise<string> {
  await waitForModelCatalog(page);
  const picker = page.getByLabel('Model');
  const selectableModels = picker.locator('option:not([disabled])[value]:not([value=""])');
  const modelId = await selectableModels.nth(1).getAttribute('value');
  if (!modelId) throw new Error('full-stack fixture did not provide a second selectable chat model');
  await picker.selectOption(modelId);
  await expect(picker).toHaveValue(modelId);
  return modelId;
}

async function sendAndWaitForAssistant(page: Page, text: string): Promise<void> {
  const composer = page.getByRole('textbox', { name: 'Message Aura' });
  await composer.fill(text);
  await page.getByRole('button', { name: 'Send' }).click();
  await expect(page.getByRole('article', { name: 'Your message' }).last()).toContainText(text);
  await expect(page.getByRole('article', { name: 'Aura response' }).last()).toBeVisible({ timeout: 90_000 });
  await expect(page.getByRole('article', { name: 'Aura response' }).last().locator('p')).not.toBeEmpty();
}

for (const mode of configuredModes) {
  test.describe(`real Aura stack (${mode})`, () => {
    test.describe.configure({ mode: 'serial' });

    test('authenticates the configured owner, discovers models, streams, and reloads persistence', async ({ page }) => {
      const config = stackConfig(mode);
      test.skip(!config, `set the ${mode} AURA_E2E_* identity variables to run the real-stack check`);
      if (!config) return;

      const eventUrls: string[] = [];
      page.on('request', (request) => {
        if (request.url().includes('/api/v1/runs/') && request.url().endsWith('/events')) eventUrls.push(request.url());
      });
      await signIn(page, config);

      const picker = page.getByLabel('Model');
      await expect(picker.locator('option[value="embed"]')).toHaveAttribute('disabled', '');
      const selectedModel = await selectSecondChatModel(page);

      const prompt = `real-stack-${mode}-${Date.now()}`;
      await sendAndWaitForAssistant(page, prompt);
      await expect.poll(async () => (await apiSession(page, config)).status).toBe(200);
      await page.reload();
      await expect(page.getByRole('article', { name: 'Your message' }).last()).toContainText(prompt);
      await expect(page.getByLabel('Model')).toHaveValue(selectedModel);

      expect(eventUrls.length).toBeGreaterThan(0);
      for (const url of eventUrls) {
        const parsed = new URL(url);
        expect(parsed.search).not.toMatch(/token|authorization|access_token/i);
      }
    });

    test('keeps an independent conversation running in the background', async ({ page }) => {
      const config = stackConfig(mode);
      test.skip(!config, `set the ${mode} AURA_E2E_* identity variables to run the real-stack check`);
      if (!config) return;

      await signIn(page, config);
      await waitForModelCatalog(page);
      await page.locator('button.new-conversation').click();
      await expect(page.getByRole('textbox', { name: 'Message Aura' })).toBeVisible();
      await selectSecondChatModel(page);
      const firstPrompt = `background-run-${mode}-${Date.now()}`;
      await page.getByRole('textbox', { name: 'Message Aura' }).fill(firstPrompt);
      await page.getByRole('button', { name: 'Send' }).click();
      // The deterministic fake Ollama fixture must hold prompts beginning
      // with `background-run-` long enough to prove navigation does not stop
      // the server-side run.
      await expect(page.getByRole('status').getByText(/gathering a thought/i)).toBeVisible({ timeout: 5_000 });
      const firstConversation = page.getByRole('button', { name: new RegExp(`^${escapeRegExp(firstPrompt)}(?:,|$)`) });
      await expect(firstConversation).toBeVisible({ timeout: 15_000 });
      await page.locator('button.new-conversation').click();
      await expect(page.getByRole('textbox', { name: 'Message Aura' })).toBeVisible();
      await selectSecondChatModel(page);

      const secondPrompt = `foreground-run-${mode}-${Date.now()}`;
      await sendAndWaitForAssistant(page, secondPrompt);
      await firstConversation.click();
      await expect(firstConversation).toHaveAttribute('aria-current', 'page');
      await expect(page.getByRole('article', { name: 'Your message' }).last()).toContainText(firstPrompt);
      await expect(page.getByRole('article', { name: 'Aura response' }).last()).toBeVisible({ timeout: 90_000 });
    });
  });
}
