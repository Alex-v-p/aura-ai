import { randomUUID } from 'node:crypto';
import { expect, test, type Locator, type Page } from '@playwright/test';

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

type JsonObject = Record<string, unknown>;

interface RunDetail {
  readonly status?: string;
  readonly error?: JsonObject | null;
}

interface ConversationDetailResponse extends JsonObject {
  readonly recentRuns?: ReadonlyArray<RunDetail & JsonObject>;
  readonly currentRun?: RunDetail & JsonObject | null;
}

interface MemoryActivityResponse extends JsonObject {
  readonly processingStatus?: string;
  readonly items?: ReadonlyArray<JsonObject>;
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

function csrfHeaders(session: { body: Record<string, unknown> }, origin: string): Record<string, string> {
  const csrfToken = session.body['csrfToken'];
  if (typeof csrfToken !== 'string' || !csrfToken) throw new Error('Aura did not return a CSRF token');
  return { 'X-CSRF-Token': csrfToken, Origin: origin, 'Idempotency-Key': randomUUID() };
}

async function waitForCompletedRun(
  page: Page,
  config: StackConfig,
  conversationId: string,
  runId: string,
): Promise<ConversationDetailResponse> {
  let detail: ConversationDetailResponse = {};
  await expect.poll(async () => {
    const response = await page.request.get(new URL(`/api/v1/conversations/${conversationId}`, config.baseUrl).toString());
    if (!response.ok()) return false;
    detail = (await response.json()) as ConversationDetailResponse;
    const run = detail.recentRuns?.find((item) => item['id'] === runId)
      ?? (detail.currentRun?.['id'] === runId ? detail.currentRun : undefined);
    return run?.status === 'completed';
  }, { timeout: 120_000, intervals: [1_000, 2_000, 5_000] }).toBe(true);
  const completed = detail.recentRuns?.find((item) => item['id'] === runId)
    ?? (detail.currentRun?.['id'] === runId ? detail.currentRun : undefined);
  expect(completed).toMatchObject({ status: 'completed', error: null });
  return detail;
}

async function waitForSettledMemoryActivity(
  page: Page,
  config: StackConfig,
  runId: string,
): Promise<MemoryActivityResponse> {
  let activity: MemoryActivityResponse = {};
  await expect.poll(async () => {
    const response = await page.request.get(new URL(`/api/v1/runs/${runId}/memory-activity`, config.baseUrl).toString());
    if (!response.ok()) return false;
    activity = (await response.json()) as MemoryActivityResponse;
    return activity.processingStatus === 'settled';
  }, { timeout: 120_000, intervals: [1_000, 2_000, 5_000] }).toBe(true);
  expect(activity.items?.every((item) => ['completed', 'failed'].includes(String(item['status'] ?? '')))).toBe(true);
  return activity;
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

async function openConversationOptions(page: Page): Promise<Locator> {
  const dialog = page.getByRole('dialog', { name: 'Conversation options' });
  if (!(await dialog.isVisible())) {
    await page.getByRole('button', { name: 'Conversation options' }).click();
    await expect(dialog).toBeVisible();
  }
  return dialog;
}

async function closeConversationOptions(page: Page): Promise<void> {
  const dialog = page.getByRole('dialog', { name: 'Conversation options' });
  if (await dialog.isVisible()) {
    await dialog.getByRole('button', { name: 'Close' }).click();
    await expect(dialog).toBeHidden();
  }
}

async function waitForModelCatalog(page: Page): Promise<Locator> {
  await expect(page.getByRole('status', { name: 'Loading your conversation' })).toBeHidden();
  const dialog = await openConversationOptions(page);
  const picker = dialog.getByLabel('Model');
  await expect(picker).toBeVisible();
  const selectableModels = picker.locator('option:not([disabled])[value]:not([value=""])');
  // The live provider may expose more than the two-model deterministic
  // fixture. The seam only requires a second selectable chat model.
  await expect.poll(async () => selectableModels.count(), { timeout: 15_000 }).toBeGreaterThanOrEqual(2);
  return picker;
}

async function selectSecondChatModel(page: Page): Promise<string> {
  const picker = await waitForModelCatalog(page);
  const catalogResponse = await page.request.get(new URL('/api/v1/models', page.url()).toString());
  expect(catalogResponse.ok()).toBeTruthy();
  const catalog = await catalogResponse.json() as { models?: Array<{ id?: string; capabilities?: string[]; selectable?: boolean }> };
  const chatModelIds = (catalog.models ?? [])
    .filter((model) => model.selectable === true && model.capabilities?.some((capability) => capability === 'chat' || capability === 'completion'))
    .map((model) => model.id)
    .filter((modelId): modelId is string => Boolean(modelId));
  const modelId = chatModelIds[1];
  if (!modelId) throw new Error('full-stack fixture did not provide a second selectable chat model');
  const selectedRoute = new URL(page.url()).pathname;
  const selectedPersistedConversation = /^\/conversation\/[^/]+$/.test(selectedRoute);
  const currentModelId = await picker.inputValue();
  const modelUpdate = selectedPersistedConversation && currentModelId !== modelId
    ? page.waitForResponse((response) => {
      const request = response.request();
      return request.method() === 'PATCH'
        && new URL(request.url()).pathname.startsWith('/api/v1/conversations/');
    })
    : undefined;
  await picker.selectOption(modelId);
  await expect(picker).toHaveValue(modelId);
  if (modelUpdate) expect((await modelUpdate).ok()).toBeTruthy();
  await closeConversationOptions(page);
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

      await waitForModelCatalog(page);
      await closeConversationOptions(page);
      const selectedModel = await selectSecondChatModel(page);

      const prompt = `real-stack-${mode}-${Date.now()}`;
      await sendAndWaitForAssistant(page, prompt);
      await expect.poll(async () => (await apiSession(page, config)).status).toBe(200);
      await page.reload();
      await expect(page.getByRole('article', { name: 'Your message' }).last()).toContainText(prompt);
      await expect(page.getByRole('button', { name: `Model ${selectedModel}` })).toBeVisible();
      const reloadedPicker = await waitForModelCatalog(page);
      await expect(reloadedPicker).toHaveValue(selectedModel);
      await closeConversationOptions(page);

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
      await closeConversationOptions(page);
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
      const firstConversation = page.getByRole('link', { name: new RegExp(`^${escapeRegExp(firstPrompt)}(?:,|$)`) });
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

    test('exposes the conservative recall policy seam on the authenticated stack', async ({ page }) => {
      const config = stackConfig(mode);
      const enabled = process.env['AURA_E2E_MEMORY_RECALL'] === '1';
      test.skip(!config || !enabled, `set the ${mode} AURA_E2E_* identity variables and AURA_E2E_MEMORY_RECALL=1 to run the memory recall seam`);
      if (!config) return;
      test.setTimeout(300_000);

      await signIn(page, config);
      const session = await apiSession(page, config);
      const modelId = await selectSecondChatModel(page);
      const agentsResponse = await page.request.get(new URL('/api/v1/agents', config.baseUrl).toString());
      expect(agentsResponse.ok()).toBeTruthy();
      const agents = (await agentsResponse.json()) as { items?: Array<JsonObject> };
      const agent = agents.items?.[0];
      const agentId = typeof agent?.['id'] === 'string' ? agent['id'] : undefined;
      expect(agentId).toBeTruthy();
      if (!agentId) return;

      let policyResponse = await page.request.get(new URL(`/api/v1/agents/${agentId}/memory-policies`, config.baseUrl).toString());
      expect(policyResponse.ok()).toBeTruthy();
      let policyPayload = (await policyResponse.json()) as { items?: Array<JsonObject>; attachedPolicyRevisionId?: string; agentVersion?: number };
      let attached = policyPayload.items?.find((item) => item['id'] === policyPayload.attachedPolicyRevisionId);
      const conservativeAutomatic = policyPayload.items?.find((item) =>
        item['recallMode'] === 'automatic'
        && Number(item['automaticRecallThreshold']) === .7
        && Number(item['maxMemories']) === 2
        && Number(item['contextBudgetFraction']) === .05,
      );
      expect(conservativeAutomatic).toBeTruthy();
      expect(policyPayload.agentVersion).toEqual(expect.any(Number));
      // A failed prior run can leave its deliberately-created off policy
      // attached. Reattach the existing conservative automatic revision so
      // retries remain isolated without creating another policy revision.
      if (policyPayload.attachedPolicyRevisionId !== conservativeAutomatic?.['id']) {
        const attachAutomaticResponse = await page.request.post(new URL(`/api/v1/agents/${agentId}/memory-policies/${conservativeAutomatic?.['id']}/attach`, config.baseUrl).toString(), {
          headers: csrfHeaders(session, config.baseUrl),
          data: { expectedAgentVersion: policyPayload.agentVersion },
        });
        expect(attachAutomaticResponse.status(), await attachAutomaticResponse.text()).toBe(201);
        const attachedAutomaticProfile = (await attachAutomaticResponse.json()) as JsonObject;
        const attachedAutomaticRevision = attachedAutomaticProfile['currentRevision'] as JsonObject | undefined;
        expect(typeof attachedAutomaticRevision?.['id']).toBe('string');
        policyResponse = await page.request.get(new URL(`/api/v1/agents/${agentId}/memory-policies`, config.baseUrl).toString());
        expect(policyResponse.ok()).toBeTruthy();
        policyPayload = (await policyResponse.json()) as typeof policyPayload;
        expect(policyPayload.attachedPolicyRevisionId).toBe(conservativeAutomatic?.['id']);
        attached = policyPayload.items?.find((item) => item['id'] === policyPayload.attachedPolicyRevisionId);
      }
      expect(attached).toMatchObject({ recallMode: 'automatic', automaticRecallThreshold: .7, maxMemories: 2, contextBudgetFraction: .05 });
      expect(policyPayload.agentVersion).toEqual(expect.any(Number));
      const currentAgentResponse = await page.request.get(new URL(`/api/v1/agents/${agentId}`, config.baseUrl).toString());
      expect(currentAgentResponse.ok()).toBeTruthy();
      const currentAgent = (await currentAgentResponse.json()) as JsonObject;
      const currentAgentRevision = typeof currentAgent['currentRevision'] === 'object' && currentAgent['currentRevision'] !== null
        ? currentAgent['currentRevision'] as JsonObject
        : undefined;
      const automaticRevisionId = currentAgentRevision?.['id'];
      expect(typeof automaticRevisionId).toBe('string');

      const factLabel = `AURA0047${mode === 'local-identity' ? 'LOCAL' : 'EXTERNAL'}${Date.now()}`;
      const factContent = `The answer to what is the glacier spectroscopy signal ${factLabel} is cobalt-orbit.`;
      const createdMemory = await page.request.post(new URL('/api/v1/memories', config.baseUrl).toString(), {
        headers: csrfHeaders(session, config.baseUrl),
        data: {
          content: factContent,
          kind: 'semantic',
          // Keep the live seam isolated from durable user memories created by
          // earlier runs while exercising the current-agent scope gate.
          scope: { type: 'agent', agentProfileId: agentId },
          confidence: .99,
          importance: .95,
          halfLifeDays: 3650,
        },
      });
      expect(createdMemory.ok(), await createdMemory.text()).toBeTruthy();
      const memory = (await createdMemory.json()) as JsonObject;
      const memoryId = memory['id'];
      const revision = memory['currentRevision'] as JsonObject | undefined;
      expect(typeof memoryId).toBe('string');
      expect(revision?.['id']).toBeTruthy();

      let memoryDetail: JsonObject = memory;
      await expect.poll(async () => {
        const response = await page.request.get(new URL(`/api/v1/memories/${memoryId}?scopeType=agent&agentProfileId=${agentId}`, config.baseUrl).toString());
        if (!response.ok()) return false;
        memoryDetail = (await response.json()) as JsonObject;
        const embeddings = memoryDetail['embeddings'];
        return Array.isArray(embeddings) && embeddings.length > 0;
      }, { timeout: 120_000, intervals: [1_000, 2_000, 5_000] }).toBe(true);
      expect(memoryDetail['embeddings']).toEqual(expect.arrayContaining([expect.objectContaining({ dimension: expect.any(Number) })]));

      const automaticPrompt = `What is the glacier spectroscopy signal ${factLabel}?`;
      const automaticRunResponse = await page.request.post(new URL('/api/v1/conversations', config.baseUrl).toString(), {
        headers: csrfHeaders(session, config.baseUrl),
        data: { message: automaticPrompt, modelId, agentRevisionId: automaticRevisionId },
      });
      expect(automaticRunResponse.status(), await automaticRunResponse.text()).toBe(202);
      const automaticAccepted = (await automaticRunResponse.json()) as JsonObject;
      const automaticConversation = automaticAccepted['conversation'] as JsonObject;
      const automaticRun = automaticAccepted['run'] as JsonObject;
      const automaticConversationId = automaticConversation['id'];
      const automaticRunId = automaticRun['id'];
      expect(typeof automaticConversationId).toBe('string');
      expect(typeof automaticRunId).toBe('string');
      await waitForCompletedRun(page, config, String(automaticConversationId), String(automaticRunId));
      const recalledActivity = await waitForSettledMemoryActivity(page, config, String(automaticRunId));
      const recalled = recalledActivity.items?.find((item) => item['action'] === 'recalled' && item['memoryId'] === memoryId);
      expect(recalled).toMatchObject({
        action: 'recalled',
        status: 'completed',
        memoryId,
        memoryRevisionId: revision?.['id'],
      });
      expect(recalled?.['policyRevisionId']).toBeTruthy();
      expect(recalled?.['embeddingGenerationId']).toBeTruthy();
      expect(recalled).not.toHaveProperty('content');

      const current = attached as JsonObject;
      const sameScopeAndLimits = (candidate: JsonObject): boolean => (
        candidate['sharedUserRead'] === current['sharedUserRead']
        && candidate['currentAgentRead'] === current['currentAgentRead']
        && candidate['sharedUserPromotion'] === current['sharedUserPromotion']
        && Number(candidate['fallbackRelevanceThreshold']) === Number(current['fallbackRelevanceThreshold'])
        && Number(candidate['maxMemories']) === Number(current['maxMemories'])
        && Number(candidate['contextBudgetFraction']) === Number(current['contextBudgetFraction'])
        && JSON.stringify(candidate['fallbackAgentProfileIds'] ?? []) === JSON.stringify(current['fallbackAgentProfileIds'] ?? [])
      );
      let offPolicy = policyPayload.items?.find((candidate) => candidate['recallMode'] === 'off' && sameScopeAndLimits(candidate));
      if (!offPolicy) {
        const latestPolicyRevision = Math.max(
          ...(policyPayload.items ?? []).map((candidate) => Number(candidate['revision'])).filter(Number.isFinite),
          0,
        );
        const offPolicyResponse = await page.request.post(new URL(`/api/v1/agents/${agentId}/memory-policies`, config.baseUrl).toString(), {
          headers: csrfHeaders(session, config.baseUrl),
          data: {
            recallMode: 'off',
            automaticRecallThreshold: Number(current['automaticRecallThreshold'] ?? .7),
            sharedUserRead: Boolean(current['sharedUserRead']),
            currentAgentRead: Boolean(current['currentAgentRead']),
            sharedUserPromotion: Boolean(current['sharedUserPromotion']),
            fallbackRelevanceThreshold: Number(current['fallbackRelevanceThreshold'] ?? .7),
            maxMemories: Math.max(1, Number(current['maxMemories'] ?? 2)),
            contextBudgetFraction: Number(current['contextBudgetFraction'] ?? .05),
            fallbackAgentProfileIds: current['fallbackAgentProfileIds'] ?? [],
            expectedRevision: latestPolicyRevision,
          },
        });
        expect(offPolicyResponse.status(), await offPolicyResponse.text()).toBe(201);
        offPolicy = (await offPolicyResponse.json()) as JsonObject;
      }
      if (!offPolicy) throw new Error('No compatible Off memory policy revision is available');
      expect(offPolicy['recallMode']).toBe('off');
      const attachOffResponse = await page.request.post(new URL(`/api/v1/agents/${agentId}/memory-policies/${offPolicy['id']}/attach`, config.baseUrl).toString(), {
        headers: csrfHeaders(session, config.baseUrl),
        data: { expectedAgentVersion: policyPayload.agentVersion },
      });
      expect(attachOffResponse.status(), await attachOffResponse.text()).toBe(201);
      const attachedOffProfile = (await attachOffResponse.json()) as JsonObject;
      const offRevision = attachedOffProfile['currentRevision'] as JsonObject | undefined;
      const offRevisionId = offRevision?.['id'];
      expect(typeof offRevisionId).toBe('string');
      const offPolicyRefresh = await page.request.get(new URL(`/api/v1/agents/${agentId}/memory-policies`, config.baseUrl).toString());
      expect(offPolicyRefresh.ok()).toBeTruthy();
      const offPolicyPayload = (await offPolicyRefresh.json()) as { attachedPolicyRevisionId?: string; agentVersion?: number };
      expect(offPolicyPayload.attachedPolicyRevisionId).toBe(offPolicy['id']);
      expect(offPolicyPayload.agentVersion).toEqual(expect.any(Number));

      const offPrompt = `Without recalling prior context, what durable value belongs to integration fixture label ${factLabel}?`;
      const offRunResponse = await page.request.post(new URL('/api/v1/conversations', config.baseUrl).toString(), {
        headers: csrfHeaders(session, config.baseUrl),
        data: { message: offPrompt, modelId, agentRevisionId: offRevisionId },
      });
      expect(offRunResponse.status(), await offRunResponse.text()).toBe(202);
      const offAccepted = (await offRunResponse.json()) as JsonObject;
      const offConversation = offAccepted['conversation'] as JsonObject;
      const offRun = offAccepted['run'] as JsonObject;
      const offConversationId = offConversation['id'];
      const offRunId = offRun['id'];
      expect(typeof offConversationId).toBe('string');
      expect(typeof offRunId).toBe('string');
      await waitForCompletedRun(page, config, String(offConversationId), String(offRunId));
      const offActivity = await waitForSettledMemoryActivity(page, config, String(offRunId));
      expect(offActivity.items?.length ?? 0).toBeGreaterThan(0);
      expect(offActivity.items?.every((item) => item['reconciliationStatus'] === 'authoritative')).toBe(true);
      expect(offActivity.items?.some((item) => item['action'] === 'recalled')).toBe(false);
      expect(offActivity.processingStatus).toBe('settled');

      await page.goto(new URL('/memory/settings', config.baseUrl).toString());
      await expect(page.getByRole('heading', { level: 1, name: 'Model settings', exact: true })).toBeVisible({ timeout: 15_000 });
    });
  });
}
