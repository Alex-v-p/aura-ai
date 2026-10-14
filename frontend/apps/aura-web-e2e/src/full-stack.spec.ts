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

interface ReminderResponse extends JsonObject {
  readonly id?: string;
  readonly status?: string;
}

interface NotificationResponse extends JsonObject {
  readonly id?: string;
  readonly reminderId?: string;
  readonly status?: string;
  readonly version?: number;
}

interface NotificationPageResponse extends JsonObject {
  readonly items?: ReadonlyArray<NotificationResponse>;
  readonly unacknowledgedCount?: number;
}

interface ApprovalSummary extends JsonObject {
  readonly id?: string;
  readonly conversationId?: string;
  readonly toolId?: string;
  readonly status?: string;
  readonly executionStatus?: string;
  readonly version?: number;
}

interface ApprovalDetail extends ApprovalSummary {
  readonly proposal?: JsonObject | null;
}

interface ReminderSeriesResponse extends JsonObject {
  readonly id?: string;
  readonly status?: string;
  readonly nextReminderId?: string | null;
  readonly version?: number;
}

interface ReminderSeriesPageResponse extends JsonObject {
  readonly items?: ReadonlyArray<ReminderSeriesResponse>;
}

interface ReminderOccurrenceResponse extends JsonObject {
  readonly reminderId?: string;
  readonly outcome?: string;
}

interface ReminderOccurrencePageResponse extends JsonObject {
  readonly items?: ReadonlyArray<ReminderOccurrenceResponse>;
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

async function waitForPendingReminderApproval(
  page: Page,
  config: StackConfig,
  conversationId: string,
): Promise<ApprovalSummary> {
  let matching: ApprovalSummary[] = [];
  await expect.poll(async () => {
    const response = await page.request.get(new URL('/api/v1/approvals?status=pending&limit=100', config.baseUrl).toString());
    if (!response.ok()) return false;
    const payload = (await response.json()) as { items?: ReadonlyArray<ApprovalSummary> };
    matching = (payload.items ?? []).filter((item) => item.conversationId === conversationId && item.toolId === 'reminder.series.create');
    return matching.length === 1;
  }, { timeout: 60_000, intervals: [500, 1_000, 2_000] }).toBe(true);
  const approval = matching[0];
  if (!approval?.id) throw new Error('The marked conversation did not create one recurring-reminder approval');
  return approval;
}

async function waitForApprovedReminderApproval(
  page: Page,
  config: StackConfig,
  approvalId: string,
): Promise<ApprovalDetail> {
  let detail: ApprovalDetail = {};
  await expect.poll(async () => {
    const response = await page.request.get(new URL(`/api/v1/approvals/${approvalId}`, config.baseUrl).toString());
    if (!response.ok()) return false;
    detail = (await response.json()) as ApprovalDetail;
    return detail.status === 'approved' && detail.executionStatus === 'completed';
  }, { timeout: 120_000, intervals: [500, 1_000, 2_000, 5_000] }).toBe(true);
  expect(detail).toMatchObject({ id: approvalId, status: 'approved', executionStatus: 'completed' });
  return detail;
}

async function waitForOneActiveReminderSeries(
  page: Page,
  config: StackConfig,
): Promise<ReminderSeriesResponse> {
  let active: ReminderSeriesResponse[] = [];
  await expect.poll(async () => {
    const response = await page.request.get(new URL('/api/v1/reminder-series?limit=100', config.baseUrl).toString());
    if (!response.ok()) return false;
    const payload = (await response.json()) as ReminderSeriesPageResponse;
    active = (payload.items ?? []).filter((item) => item.status === 'active');
    return active.length === 1 && typeof active[0]?.nextReminderId === 'string';
  }, { timeout: 120_000, intervals: [500, 1_000, 2_000, 5_000] }).toBe(true);
  const series = active[0];
  if (!series?.id || !series.nextReminderId) throw new Error('The approved command did not create one active recurring series');
  return series;
}

async function waitForGeneratedReminder(
  page: Page,
  config: StackConfig,
  seriesId: string,
  reminderId: string,
): Promise<void> {
  await expect.poll(async () => {
    const occurrenceResponse = await page.request.get(new URL(`/api/v1/reminder-series/${seriesId}/occurrences?limit=100`, config.baseUrl).toString());
    if (!occurrenceResponse.ok()) return false;
    const occurrences = (await occurrenceResponse.json()) as ReminderOccurrencePageResponse;
    const occurrence = (occurrences.items ?? []).filter((item) => item.reminderId === reminderId);
    if (occurrence.length !== 1 || occurrence[0]?.outcome !== 'scheduled') return false;
    const reminderResponse = await page.request.get(new URL(`/api/v1/reminders/${reminderId}`, config.baseUrl).toString());
    return reminderResponse.ok();
  }, { timeout: 60_000, intervals: [500, 1_000, 2_000] }).toBe(true);
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

async function waitForReminderStatus(
  page: Page,
  config: StackConfig,
  reminderId: string,
  status: string,
): Promise<ReminderResponse> {
  let reminder: ReminderResponse = {};
  await expect.poll(async () => {
    const response = await page.request.get(new URL(`/api/v1/reminders/${reminderId}`, config.baseUrl).toString());
    if (!response.ok()) return false;
    reminder = (await response.json()) as ReminderResponse;
    return reminder.status === status;
  }, { timeout: 60_000, intervals: [500, 1_000, 2_000] }).toBe(true);
  return reminder;
}

async function waitForReminderNotification(
  page: Page,
  config: StackConfig,
  reminderId: string,
): Promise<NotificationResponse> {
  let matching: NotificationResponse[] = [];
  await expect.poll(async () => {
    const response = await page.request.get(new URL('/api/v1/notifications?limit=100', config.baseUrl).toString());
    if (!response.ok()) return false;
    const payload = (await response.json()) as NotificationPageResponse;
    matching = (payload.items ?? []).filter((item) => item.reminderId === reminderId);
    return matching.length === 1;
  }, { timeout: 60_000, intervals: [500, 1_000, 2_000] }).toBe(true);
  const notification = matching[0];
  if (!notification) throw new Error(`No notification was created for reminder ${reminderId}`);
  return notification;
}

async function readNotificationStream(
  page: Page,
  targetNotificationId: string | undefined,
  lastEventId: string | undefined,
): Promise<JsonObject[]> {
  return page.evaluate(async ({ targetNotificationId: targetId, lastEventId: cursor }) => {
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), cursor ? 3_000 : 20_000);
    const events: JsonObject[] = [];
    try {
      const response = await fetch('/api/v1/notifications/stream', {
        credentials: 'include',
        headers: { Accept: 'text/event-stream', ...(cursor ? { 'Last-Event-ID': cursor } : {}) },
        signal: controller.signal,
      });
      if (!response.ok) throw new Error(`Notification stream failed with ${response.status}`);
      const reader = response.body?.getReader();
      if (!reader) throw new Error('Notification stream did not return a body');
      const decoder = new TextDecoder();
      let buffer = '';
      while (true) {
        const chunk = await reader.read();
        if (chunk.done) break;
        buffer += decoder.decode(chunk.value, { stream: true });
        const blocks = buffer.split(/\r?\n\r?\n/);
        buffer = blocks.pop() ?? '';
        for (const block of blocks) {
          const data = block.split(/\r?\n/).filter((line) => line.startsWith('data:')).map((line) => line.slice(5).trim()).join('\n');
          if (!data) continue;
          const payload = JSON.parse(data) as JsonObject;
          events.push(payload);
          if (targetId && payload['notificationId'] === targetId) {
            controller.abort();
            return events;
          }
        }
      }
    } catch (error: unknown) {
      if (error instanceof DOMException && error.name === 'AbortError') return events;
      throw error;
    } finally {
      window.clearTimeout(timeout);
    }
    return events;
  }, { targetNotificationId, lastEventId });
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

function pdfFixture(text: string): Buffer {
  const placeholder = 'Owner-private retrieval fixture. The grounded answer must cite this marker: AURA_E2E_DOCUMENT_SEARCH_CITATION_00000000-0000-4000-8000-000000000000.';
  const template = Buffer.from(
    'JVBERi0xLjMKJeLjz9MKMSAwIG9iago8PAovUHJvZHVjZXIgKHB5cGRmKQo+PgplbmRvYmoKMiAwIG9iago8PAovVHlwZSAvUGFnZXMKL0NvdW50IDEKL0tpZHMgWyA0IDAgUiBdCj4+CmVuZG9iagozIDAgb2JqCjw8Ci9UeXBlIC9DYXRhbG9nCi9QYWdlcyAyIDAgUgo+PgplbmRvYmoKNCAwIG9iago8PAovVHlwZSAvUGFnZQovUmVzb3VyY2VzIDw8Ci9Gb250IDw8Ci9GMSA1IDAgUgo+Pgo+PgovTWVkaWFCb3ggWyAwLjAgMC4wIDYxMiA3OTIgXQovUGFyZW50IDIgMCBSCi9Db250ZW50cyA2IDAgUgo+PgplbmRvYmoKNSAwIG9iago8PAovVHlwZSAvRm9udAovU3VidHlwZSAvVHlwZTEKL0Jhc2VGb250IC9IZWx2ZXRpY2EKPj4KZW5kb2JqCjYgMCBvYmoKPDwKL0xlbmd0aCAxNzgKPj4Kc3RyZWFtCkJUIC9GMSAxMiBUZiA3MiA3MjAgVGQgKE93bmVyLXByaXZhdGUgcmV0cmlldmFsIGZpeHR1cmUuIFRoZSBncm91bmRlZCBhbnN3ZXIgbXVzdCBjaXRlIHRoaXMgbWFya2VyOiBBVVJBX0UyRV9ET0NVTUVOVF9TRUFSQ0hfQ0lUQVRJT05fMDAwMDAwMDAtMDAwMC00MDAwLTgwMDAtMDAwMDAwMDAwMDAwLikgVGogRVQKZW5kc3RyZWFtCmVuZG9iagp4cmVmCjAgNwowMDAwMDAwMDAwIDY1NTM1IGYgCjAwMDAwMDAwMTUgMDAwMDAgbiAKMDAwMDAwMDA1NCAwMDAwMCBuIAowMDAwMDAwMTEzIDAwMDAwIG4gCjAwMDAwMDAxNjIgMDAwMDAgbiAKMDAwMDAwMDI5NCAwMDAwMCBuIAowMDAwMDAwMzY0IDAwMDAwIG4gCnRyYWlsZXIKPDwKL1NpemUgNwovUm9vdCAzIDAgUgovSW5mbyAxIDAgUgo+PgpzdGFydHhyZWYKNTkzCiUlRU9GCg==',
    'base64',
  );
  const source = Buffer.from(text, 'ascii');
  const marker = Buffer.from(placeholder, 'ascii');
  if (source.byteLength !== marker.byteLength) throw new Error('PDF fixture text length must remain stable');
  const offset = template.indexOf(marker);
  if (offset < 0) throw new Error('pypdf PDF fixture placeholder was not found');
  source.copy(template, offset);
  return template;
}

function crc32(data: Buffer): number {
  let value = 0xffffffff;
  for (const byte of data) {
    value ^= byte;
    for (let bit = 0; bit < 8; bit += 1) value = (value >>> 1) ^ (0xedb88320 & -(value & 1));
  }
  return (value ^ 0xffffffff) >>> 0;
}

function docxFixture(text: string): Buffer {
  const escapeXml = (value: string): string => value.replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;');
  const entries = [
    ['[Content_Types].xml', '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>'],
    ['_rels/.rels', '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>'],
    ['word/document.xml', `<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>${escapeXml(text)}</w:t></w:r></w:p></w:body></w:document>`],
  ].map(([name, value]) => ({ name, data: Buffer.from(value, 'utf8') }));
  const localChunks: Buffer[] = [];
  const centralChunks: Buffer[] = [];
  let offset = 0;
  for (const entry of entries) {
    const name = Buffer.from(entry.name, 'utf8');
    const checksum = crc32(entry.data);
    const local = Buffer.alloc(30 + name.byteLength + entry.data.byteLength);
    local.writeUInt32LE(0x04034b50, 0);
    local.writeUInt16LE(20, 4);
    local.writeUInt32LE(checksum, 14);
    local.writeUInt32LE(entry.data.byteLength, 18);
    local.writeUInt32LE(entry.data.byteLength, 22);
    local.writeUInt16LE(name.byteLength, 26);
    name.copy(local, 30);
    entry.data.copy(local, 30 + name.byteLength);
    localChunks.push(local);

    const central = Buffer.alloc(46 + name.byteLength);
    central.writeUInt32LE(0x02014b50, 0);
    central.writeUInt16LE(20, 4);
    central.writeUInt16LE(20, 6);
    central.writeUInt32LE(checksum, 16);
    central.writeUInt32LE(entry.data.byteLength, 20);
    central.writeUInt32LE(entry.data.byteLength, 24);
    central.writeUInt16LE(name.byteLength, 28);
    central.writeUInt32LE(offset, 42);
    name.copy(central, 46);
    centralChunks.push(central);
    offset += local.byteLength;
  }
  const localData = Buffer.concat(localChunks);
  const centralData = Buffer.concat(centralChunks);
  const end = Buffer.alloc(22);
  end.writeUInt32LE(0x06054b50, 0);
  end.writeUInt16LE(entries.length, 8);
  end.writeUInt16LE(entries.length, 10);
  end.writeUInt32LE(centralData.byteLength, 12);
  end.writeUInt32LE(localData.byteLength, 16);
  return Buffer.concat([localData, centralData, end]);
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

    test('AURA_E2E_DOCUMENT_SEARCH_CITATION uploads, retrieves, cites, and deletes all supported formats', async ({ page }) => {
      await runDocumentSearchCitationAcceptance(page, mode);
    });

    test('AURA_E2E_CONVERSATION_BRANCHING edits, regenerates, switches, and preserves the original', async ({ page }) => {
      const config = stackConfig(mode);
      test.skip(!config, `set the ${mode} AURA_E2E_* identity variables to run conversation branching acceptance`);
      if (!config) return;
      test.setTimeout(180_000);

      await signIn(page, config);
      await waitForModelCatalog(page);
      await closeConversationOptions(page);
      const originalPrompt = `branch-original-${mode}-${Date.now()}`;
      await sendAndWaitForAssistant(page, originalPrompt);
      const conversationId = page.url().match(/\/conversation\/([^/?]+)/)?.[1];
      expect(conversationId).toBeTruthy();
      if (!conversationId) return;

      await expect(page.getByRole('button', { name: 'Stop', exact: true })).toHaveCount(0, { timeout: 90_000 });
      const initialResponse = await page.request.get(new URL(`/api/v1/conversations/${conversationId}`, config.baseUrl).toString());
      expect(initialResponse.ok(), await initialResponse.text()).toBeTruthy();
      const initialDetail = await initialResponse.json() as ConversationDetailResponse;
      const messages = (initialDetail['messages'] as Array<JsonObject> | undefined) ?? [];
      const originalMessage = messages.find((item) => item['role'] === 'user');
      const runs = (initialDetail['recentRuns'] as Array<JsonObject> | undefined) ?? [];
      const originalRun = runs.find((item) => item['userMessageId'] === originalMessage?.['id']);
      expect(typeof originalMessage?.['id']).toBe('string');
      expect(typeof originalRun?.['id']).toBe('string');
      if (typeof originalMessage?.['id'] !== 'string' || typeof originalRun?.['id'] !== 'string') return;

      const correctedPrompt = `${originalPrompt}-corrected`;
      const transcriptUserMessage = (content: string) =>
        page
          .getByRole('main')
          .getByRole('article', { name: 'Your message' })
          .getByText(content, { exact: true });
      const editAndBranch = page.getByRole('button', { name: 'Edit and branch' }).first();
      await expect(editAndBranch).toBeEnabled();
      await editAndBranch.click();
      const editor = page.getByRole('textbox', { name: 'Edit your message' });
      await expect(editor).toBeVisible();
      await editor.fill(correctedPrompt);
      await page.getByRole('button', { name: 'Create branch' }).click();
      await expect(transcriptUserMessage(correctedPrompt)).toBeVisible({ timeout: 30_000 });
      await expect(page.getByText(/Branch 2 of 2/, { exact: true })).toBeVisible();

      const previous = page.getByRole('button', { name: 'Previous conversation branch' });
      const next = page.getByRole('button', { name: 'Next conversation branch' });
      await expect(previous).toBeEnabled();
      await expect(next).toBeDisabled();

      const branchListUrl = new URL(
        `/api/v1/conversations/${conversationId}/branches?limit=100`,
        config.baseUrl,
      ).toString();
      let editedBranchId: string | undefined;
      let editedBranchRun: (RunDetail & JsonObject) | undefined;
      let editedBranchDetail: ConversationDetailResponse = {};
      await expect.poll(async () => {
        const response = await page.request.get(branchListUrl);
        if (!response.ok()) return false;
        const payload = await response.json() as JsonObject;
        const branches = (payload['items'] as Array<JsonObject> | undefined) ?? [];
        const editedBranch = branches.find(
          (item) => item['forkedFromMessageId'] === originalMessage['id'] && typeof item['id'] === 'string',
        );
        editedBranchId = typeof editedBranch?.['id'] === 'string' ? editedBranch['id'] : undefined;
        return editedBranchId ?? false;
      }, { timeout: 30_000, intervals: [500, 1_000, 2_000] }).toBeTruthy();
      expect(editedBranchId).toBeTruthy();
      if (!editedBranchId) return;

      const editedBranchUrl = new URL(
        `/api/v1/conversations/${conversationId}/branches/${editedBranchId}`,
        config.baseUrl,
      ).toString();
      await expect.poll(async () => {
        const response = await page.request.get(editedBranchUrl);
        if (!response.ok()) return false;
        editedBranchDetail = await response.json() as ConversationDetailResponse;
        const editedMessages = (editedBranchDetail['messages'] as Array<JsonObject> | undefined) ?? [];
        const editedAssistant = editedMessages.find((item) => item['role'] === 'assistant');
        editedBranchRun = editedBranchDetail.recentRuns?.find(
          (item) => item['branchId'] === editedBranchId && item['status'] === 'completed',
        );
        return Boolean(editedAssistant && editedBranchRun && typeof editedBranchRun['id'] === 'string');
      }, { timeout: 120_000, intervals: [1_000, 2_000, 5_000] }).toBe(true);
      const editedMessages = (editedBranchDetail['messages'] as Array<JsonObject> | undefined) ?? [];
      expect(editedMessages.some((item) => item['role'] === 'assistant')).toBe(true);
      expect(editedBranchRun).toMatchObject({ branchId: editedBranchId, status: 'completed', error: null });
      const editedBranchRunId = editedBranchRun?.['id'];
      expect(typeof editedBranchRunId).toBe('string');
      if (typeof editedBranchRunId !== 'string') return;
      await expect(page.getByRole('article', { name: 'Aura response' }).last()).toBeVisible({ timeout: 30_000 });
      await expect(page.getByText(/Branch 2 of 2/, { exact: true })).toBeVisible();
      await expect(transcriptUserMessage(correctedPrompt)).toBeVisible();
      const selectedRegenerate = page.getByRole('button', { name: 'Regenerate' }).last();
      await expect(selectedRegenerate).toBeEnabled();

      const initialBranchesResponse = await page.request.get(branchListUrl);
      expect(initialBranchesResponse.ok(), await initialBranchesResponse.text()).toBeTruthy();
      const initialBranchesPayload = await initialBranchesResponse.json() as JsonObject;
      const initialBranchIds = new Set<string>();
      for (const item of (initialBranchesPayload['items'] as Array<JsonObject> | undefined) ?? []) {
        if (typeof item['id'] === 'string') initialBranchIds.add(item['id']);
      }

      await selectedRegenerate.click();
      await expect(page.getByRole('article', { name: 'Aura response' }).last()).toBeVisible({ timeout: 30_000 });
      let regeneratedBranchId: string | undefined;
      await expect.poll(async () => {
        const response = await page.request.get(branchListUrl);
        if (!response.ok()) return false;
        const payload = await response.json() as JsonObject;
        const branches = (payload['items'] as Array<JsonObject> | undefined) ?? [];
        const regeneratedBranch = branches.find(
          (item) => typeof item['id'] === 'string' && !initialBranchIds.has(item['id']),
        );
        regeneratedBranchId = typeof regeneratedBranch?.['id'] === 'string'
          ? regeneratedBranch['id']
          : undefined;
        return regeneratedBranchId ?? false;
      }, { timeout: 30_000, intervals: [500, 1_000, 2_000] }).toBeTruthy();
      expect(regeneratedBranchId).toBeTruthy();
      if (!regeneratedBranchId) return;

      const regeneratedBranchUrl = new URL(
        `/api/v1/conversations/${conversationId}/branches/${regeneratedBranchId}`,
        config.baseUrl,
      ).toString();
      let regeneratedBranchDetail: ConversationDetailResponse = {};
      let regeneratedRun: (RunDetail & JsonObject) | undefined;
      await expect.poll(async () => {
        const response = await page.request.get(regeneratedBranchUrl);
        if (!response.ok()) return false;
        regeneratedBranchDetail = await response.json() as ConversationDetailResponse;
        regeneratedRun = regeneratedBranchDetail.recentRuns?.find(
          (item) => item['regeneratedFromRunId'] === editedBranchRunId,
        );
        return Boolean(regeneratedRun && typeof regeneratedRun['id'] === 'string');
      }, { timeout: 30_000, intervals: [500, 1_000, 2_000] }).toBe(true);
      expect(regeneratedRun).toMatchObject({ regeneratedFromRunId: editedBranchRunId });
      const regeneratedRunId = regeneratedRun?.['id'];
      expect(typeof regeneratedRunId).toBe('string');
      if (typeof regeneratedRunId !== 'string') return;

      await expect.poll(async () => {
        const response = await page.request.get(regeneratedBranchUrl);
        if (!response.ok()) return false;
        regeneratedBranchDetail = await response.json() as ConversationDetailResponse;
        const completedRun = regeneratedBranchDetail.recentRuns?.find(
          (item) => item['id'] === regeneratedRunId,
        );
        return completedRun?.status === 'completed';
      }, { timeout: 120_000, intervals: [1_000, 2_000, 5_000] }).toBe(true);
      const completedRegeneratedRun = regeneratedBranchDetail.recentRuns?.find(
        (item) => item['id'] === regeneratedRunId,
      );
      expect(completedRegeneratedRun).toMatchObject({ status: 'completed', error: null });

      await expect(page.getByText(/Branch 3 of 3/, { exact: true })).toBeVisible();
      await expect(previous).toBeEnabled();
      await expect(next).toBeDisabled();
      await previous.click();
      await expect(transcriptUserMessage(correctedPrompt)).toBeVisible();
      await expect(page.getByText(/Branch 2 of 3/, { exact: true })).toBeVisible();
      await previous.click();
      await expect(transcriptUserMessage(originalPrompt)).toBeVisible();
      await expect(page.getByText(/Branch 1 of 3/, { exact: true })).toBeVisible();
      await expect(next).toBeEnabled();

      const originalAfter = await page.request.get(new URL(`/api/v1/conversations/${conversationId}`, config.baseUrl).toString());
      expect(originalAfter.ok(), await originalAfter.text()).toBeTruthy();
      const originalPayload = await originalAfter.json() as ConversationDetailResponse;
      const originalMessages = (originalPayload['messages'] as Array<JsonObject> | undefined) ?? [];
      expect(originalMessages.some((item) => item['content'] === originalPrompt)).toBe(true);
      expect(originalMessages.some((item) => item['content'] === correctedPrompt)).toBe(false);
      expect(originalMessages.filter((item) => item['content'] === originalPrompt)).toHaveLength(1);
    });

    test('fires one durable reminder notification and catches up after SSE reconnect', async ({ page }) => {
      const config = stackConfig(mode);
      test.skip(!config, `set the ${mode} AURA_E2E_* identity variables to run the reminder notification check`);
      if (!config) return;
      test.setTimeout(120_000);

      await signIn(page, config);
      const session = await apiSession(page, config);
      const reminderContent = `real-stack-notification-${mode}-${Date.now()}`;
      const dueAt = new Date(Date.now() + 8_000).toISOString();
      const createResponse = await page.request.post(new URL('/api/v1/reminders', config.baseUrl).toString(), {
        headers: csrfHeaders(session, config.baseUrl),
        data: { content: reminderContent, dueAt },
      });
      expect(createResponse.status(), await createResponse.text()).toBe(201);
      const createdReminder = (await createResponse.json()) as ReminderResponse;
      const reminderId = createdReminder.id;
      expect(typeof reminderId).toBe('string');
      if (typeof reminderId !== 'string') return;

      const firedReminder = await waitForReminderStatus(page, config, reminderId, 'fired');
      expect(firedReminder.status).toBe('fired');
      const notification = await waitForReminderNotification(page, config, reminderId);
      expect(notification).toMatchObject({ reminderId, status: 'delivered', version: 1 });
      const notificationId = notification.id;
      expect(typeof notificationId).toBe('string');
      if (typeof notificationId !== 'string') return;

      const streamRequests: Array<Record<string, string>> = [];
      const onRequest = (request: import('@playwright/test').Request): void => {
        const url = new URL(request.url());
        if (url.pathname.endsWith('/api/v1/notifications/stream')) streamRequests.push(request.headers());
      };
      page.on('request', onRequest);
      try {
        const replayedEvents = await readNotificationStream(page, notificationId, undefined);
        const deliveredEvent = replayedEvents.find((event) => event['notificationId'] === notificationId);
        expect(deliveredEvent).toMatchObject({
          eventType: 'notification.delivered',
          notificationId,
          reminderId,
          status: 'delivered',
        });
        const eventId = deliveredEvent?.['eventId'];
        expect(typeof eventId).toBe('string');
        if (typeof eventId !== 'string') return;

        const reconnectEvents = await readNotificationStream(page, undefined, eventId);
        expect(reconnectEvents.some((event) => event['notificationId'] === notificationId)).toBe(false);
        expect(streamRequests.some((headers) => headers['last-event-id'] === eventId)).toBe(true);
      } finally {
        page.off('request', onRequest);
      }

      const acknowledgeResponse = await page.request.post(new URL(`/api/v1/notifications/${notificationId}/acknowledge`, config.baseUrl).toString(), {
        headers: csrfHeaders(session, config.baseUrl),
        data: { expectedVersion: notification.version },
      });
      expect(acknowledgeResponse.status(), await acknowledgeResponse.text()).toBe(200);
      const acknowledged = (await acknowledgeResponse.json()) as NotificationResponse;
      expect(acknowledged).toMatchObject({ id: notificationId, reminderId, status: 'acknowledged', version: 2 });

      // The stream is deliberately closed above. The persisted inbox remains
      // authoritative while the browser is offline and must catch up on the
      // next list request without creating a second notification.
      const offlineCatchUp = await page.request.get(new URL('/api/v1/notifications?limit=100', config.baseUrl).toString());
      expect(offlineCatchUp.ok(), await offlineCatchUp.text()).toBeTruthy();
      const catchUpPage = (await offlineCatchUp.json()) as NotificationPageResponse;
      const catchUpItems = (catchUpPage.items ?? []).filter((item) => item.reminderId === reminderId);
      expect(catchUpItems).toHaveLength(1);
      expect(catchUpItems[0]).toMatchObject({ id: notificationId, status: 'acknowledged' });

      await page.goto(new URL('/notifications', config.baseUrl).toString());
      await expect(page.getByRole('heading', { name: 'Notifications', exact: true })).toBeVisible();
      await expect(page.getByText('A reminder is due.', { exact: true })).toBeVisible();
      await expect(page.locator('li.notification-card .notification-status').filter({ hasText: 'Acknowledged' }).first()).toBeVisible();
      await expect(page.getByRole('button', { name: 'Acknowledge' })).toHaveCount(0);
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

    test('drives one recurring reminder through native tool approval and durable resume', async ({ page }) => {
      const config = stackConfig(mode);
      test.skip(!config, `set the ${mode} AURA_E2E_* identity variables to run the conversational reminder approval check`);
      if (!config) return;
      test.setTimeout(240_000);

      await signIn(page, config);
      const session = await apiSession(page, config);
      const modelsResponse = await page.request.get(new URL('/api/v1/models', config.baseUrl).toString());
      expect(modelsResponse.ok(), await modelsResponse.text()).toBeTruthy();
      const models = (await modelsResponse.json()) as { models?: Array<JsonObject> };
      const model = models.models?.find((candidate) => candidate['id'] === 'fixture-chat' && candidate['selectable'] === true);
      expect(model).toBeTruthy();
      if (!model) return;

      // This request runs in the authenticated browser context, preserving the
      // exact pinned timezone that the conversational command must use.
      const prompt = `AURA_E2E_REMINDER_SERIES_CREATE ${mode} ${Date.now()}`;
      const createResponse = await page.request.post(new URL('/api/v1/conversations', config.baseUrl).toString(), {
        headers: csrfHeaders(session, config.baseUrl),
        data: { message: prompt, modelId: 'fixture-chat', clientTimeZone: 'Europe/Brussels' },
      });
      expect(createResponse.status(), await createResponse.text()).toBe(202);
      const accepted = (await createResponse.json()) as JsonObject;
      const conversation = accepted['conversation'] as JsonObject | undefined;
      const run = accepted['run'] as JsonObject | undefined;
      const conversationId = conversation?.['id'];
      const runId = run?.['id'];
      expect(typeof conversationId).toBe('string');
      expect(typeof runId).toBe('string');
      expect(run?.['clientTimeZone']).toBe('Europe/Brussels');
      if (typeof conversationId !== 'string' || typeof runId !== 'string') return;

      const pending = await waitForPendingReminderApproval(page, config, conversationId);
      expect(pending).toMatchObject({ conversationId, toolId: 'reminder.series.create', status: 'pending', version: 1 });
      expect(pending.id).toEqual(expect.any(String));
      if (typeof pending.id !== 'string') return;

      // Review and approve through the real Aura Web approval surface.  The
      // protected detail is intentionally not asserted for private content.
      await page.goto(new URL('/approvals', config.baseUrl).toString());
      await expect(page.getByRole('heading', { name: 'Approvals', exact: true })).toBeVisible();
      const approvalRow = page.locator(`#approval-${pending.id}`);
      await expect(approvalRow).toHaveCount(1);
      await approvalRow.click();
      const detail = page.getByRole('dialog');
      await expect(detail).toBeVisible();
      const approve = detail.getByRole('button', { name: 'Approve', exact: true });
      await expect(approve).toBeVisible();
      let approvalDecisionPosts = 0;
      const approvalDecisionResponses: number[] = [];
      const approvalDecisionResponseBodies: string[] = [];
      const approvalDecisionFailures: string[] = [];
      const approvalDecisionPath = `/api/v1/approvals/${pending.id}/approve`;
      const observeApprovalDecision = (request: { method(): string; url(): string }): void => {
        const requestUrl = new URL(request.url());
        if (request.method() === 'POST' && requestUrl.pathname === approvalDecisionPath) approvalDecisionPosts += 1;
      };
      const observeApprovalDecisionResponse = async (response: { request(): { method(): string }; url(): string; status(): number; text(): Promise<string> }): Promise<void> => {
        const responseUrl = new URL(response.url());
        if (response.request().method() === 'POST' && responseUrl.pathname === approvalDecisionPath) {
          approvalDecisionResponses.push(response.status());
          approvalDecisionResponseBodies.push(await response.text());
        }
      };
      const observeApprovalDecisionFailure = (request: { method(): string; url(): string; failure(): { errorText?: string } | null }): void => {
        const requestUrl = new URL(request.url());
        if (request.method() === 'POST' && requestUrl.pathname === approvalDecisionPath) approvalDecisionFailures.push(request.failure()?.errorText ?? 'unknown request failure');
      };
      page.on('request', observeApprovalDecision);
      page.on('response', observeApprovalDecisionResponse);
      page.on('requestfailed', observeApprovalDecisionFailure);
      await approve.click();
      await expect.poll(() => approvalDecisionPosts, { timeout: 10_000 }).toBe(1);
      await expect.poll(() => approvalDecisionResponses.length + approvalDecisionFailures.length, { timeout: 10_000 }).toBe(1);
      expect(approvalDecisionFailures, approvalDecisionFailures.join('; ')).toHaveLength(0);
      expect(approvalDecisionResponses, approvalDecisionResponseBodies.join('; ')).toEqual([200]);
      page.off('request', observeApprovalDecision);
      page.off('response', observeApprovalDecisionResponse);
      page.off('requestfailed', observeApprovalDecisionFailure);

      await waitForApprovedReminderApproval(page, config, pending.id);
      await waitForCompletedRun(page, config, conversationId, runId);

      const series = await waitForOneActiveReminderSeries(page, config);
      await waitForGeneratedReminder(page, config, series.id as string, series.nextReminderId as string);

      // The approval decision is idempotent and the resumed tools-disabled
      // synthesis cannot create a second proposal or series.
      const approvalHistoryResponse = await page.request.get(new URL(`/api/v1/approvals?limit=100`, config.baseUrl).toString());
      expect(approvalHistoryResponse.ok(), await approvalHistoryResponse.text()).toBeTruthy();
      const approvalHistory = (await approvalHistoryResponse.json()) as { items?: ReadonlyArray<ApprovalSummary> };
      const conversationApprovals = (approvalHistory.items ?? []).filter((item) => item.conversationId === conversationId);
      expect(conversationApprovals).toHaveLength(1);
      expect(conversationApprovals[0]).toMatchObject({ id: pending.id, toolId: 'reminder.series.create', status: 'approved' });
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

    test('exposes the governed built-in document-search tool policy revision seam', async ({ page }) => {
      const config = stackConfig(mode);
      test.skip(!config, `set the ${mode} AURA_E2E_* identity variables to run the tool policy seam`);
      if (!config) return;

      await signIn(page, config);
      const toolsResponse = await page.request.get(new URL('/api/v1/tools', config.baseUrl).toString());
      expect(toolsResponse.ok(), await toolsResponse.text()).toBeTruthy();
      const tools = (await toolsResponse.json()) as { items?: Array<JsonObject> };
      expect(tools.items).toEqual([
        expect.objectContaining({ id: 'memory.search', version: 1, classification: 'read', permissionScope: 'memory:read' }),
        expect.objectContaining({ id: 'document.search', version: 1, classification: 'read', permissionScope: 'documents:read', sensitivity: 'restricted', observableComponent: 'aura.runtime.tool_execution' }),
        expect.objectContaining({
          id: 'reminder.create',
          version: 1,
          classification: 'mutation',
          permissionScope: 'reminders:write',
          confirmation: 'always',
          timeoutMs: 10000,
          retryLimit: 0,
          idempotency: 'idempotent',
          sensitivity: 'private',
          observableComponent: 'aura.runtime.tool_execution',
        }),
        expect.objectContaining({ id: 'reminder.list', version: 1, classification: 'read', permissionScope: 'reminders:read' }),
        expect.objectContaining({ id: 'reminder.inspect', version: 1, classification: 'read', permissionScope: 'reminders:read' }),
        expect.objectContaining({ id: 'reminder.series.create', version: 1, classification: 'mutation', permissionScope: 'reminders:write', confirmation: 'always' }),
        expect.objectContaining({ id: 'reminder.reschedule', version: 1, classification: 'mutation', permissionScope: 'reminders:write', confirmation: 'always' }),
        expect.objectContaining({ id: 'reminder.series.update', version: 1, classification: 'mutation', permissionScope: 'reminders:write', confirmation: 'always' }),
        expect.objectContaining({ id: 'reminder.series.pause', version: 1, classification: 'mutation', permissionScope: 'reminders:write', confirmation: 'always' }),
        expect.objectContaining({ id: 'reminder.series.resume', version: 1, classification: 'mutation', permissionScope: 'reminders:write', confirmation: 'always' }),
        expect.objectContaining({ id: 'reminder.cancel', version: 1, classification: 'mutation', permissionScope: 'reminders:write', confirmation: 'always' }),
      ]);
      expect(tools.items).toHaveLength(11);
      expect(JSON.stringify(tools.items)).not.toMatch(/handler|schema|credential|query|content/i);

      const agentsResponse = await page.request.get(new URL('/api/v1/agents', config.baseUrl).toString());
      expect(agentsResponse.ok()).toBeTruthy();
      const agents = (await agentsResponse.json()) as { items?: Array<JsonObject> };
      const agentId = agents.items?.[0]?.['id'];
      expect(typeof agentId).toBe('string');
      if (typeof agentId !== 'string') return;
      const policyResponse = await page.request.get(new URL(`/api/v1/agents/${agentId}/tool-policies`, config.baseUrl).toString());
      expect(policyResponse.ok(), await policyResponse.text()).toBeTruthy();
      const policy = (await policyResponse.json()) as JsonObject;
      const attached = (policy['items'] as Array<JsonObject> | undefined)?.find((item) => item['id'] === policy['attachedPolicyRevisionId']);
      expect(attached).toMatchObject({ revision: 4, maxCallsPerRun: 1, timeoutMs: 30000, retryLimit: 0 });
      expect(attached?.['grants']).toEqual([
        { toolId: 'memory.search', toolVersion: 1 },
        { toolId: 'document.search', toolVersion: 1 },
        { toolId: 'reminder.create', toolVersion: 1 },
        { toolId: 'reminder.list', toolVersion: 1 },
        { toolId: 'reminder.inspect', toolVersion: 1 },
        { toolId: 'reminder.series.create', toolVersion: 1 },
        { toolId: 'reminder.reschedule', toolVersion: 1 },
        { toolId: 'reminder.series.update', toolVersion: 1 },
        { toolId: 'reminder.series.pause', toolVersion: 1 },
        { toolId: 'reminder.series.resume', toolVersion: 1 },
        { toolId: 'reminder.cancel', toolVersion: 1 },
      ]);
    });

    async function runDocumentSearchCitationAcceptance(page: Page, identityMode: IdentityMode): Promise<void> {
      const config = stackConfig(identityMode);
      test.skip(!config, `set the ${identityMode} AURA_E2E_* identity variables to run document retrieval acceptance`);
      if (!config) return;
      test.setTimeout(300_000);

      await signIn(page, config);
      const session = await apiSession(page, config);
      const marker = `AURA_E2E_DOCUMENT_SEARCH_CITATION_${randomUUID()}`;
      const fixtureText = `Owner-private retrieval fixture. The grounded answer must cite this marker: ${marker}.`;
      const fixtures = [
        { filename: `aura-e2e-${identityMode}.pdf`, mediaType: 'application/pdf', content: pdfFixture(fixtureText) },
        { filename: `aura-e2e-${identityMode}.docx`, mediaType: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document', content: docxFixture(fixtureText) },
        { filename: `aura-e2e-${identityMode}.md`, mediaType: 'text/markdown', content: Buffer.from(`# Grounded fixture\n\n${fixtureText}\n`, 'utf8') },
        { filename: `aura-e2e-${identityMode}.txt`, mediaType: 'text/plain', content: Buffer.from(fixtureText, 'utf8') },
      ] as const;
      const uploadedDocuments: Array<{ id: string; filename: string; version: number; content: Buffer }> = [];
      for (const fixture of fixtures) {
        const uploadResponse = await page.request.post(
          new URL('/api/v1/documents/uploads', config.baseUrl).toString(),
          {
            headers: csrfHeaders(session, config.baseUrl),
            data: { filename: fixture.filename, mediaType: fixture.mediaType, byteCount: fixture.content.byteLength },
          },
        );
        expect(uploadResponse.status(), await uploadResponse.text()).toBe(201);
        const uploadSession = (await uploadResponse.json()) as JsonObject;
        const uploadId = uploadSession['id'];
        expect(typeof uploadId).toBe('string');
        if (typeof uploadId !== 'string') return;
        const contentResponse = await page.request.put(
          new URL(`/api/v1/documents/uploads/${uploadId}/content`, config.baseUrl).toString(),
          {
            headers: {
              ...csrfHeaders(session, config.baseUrl),
              'Content-Type': fixture.mediaType,
              'Content-Length': String(fixture.content.byteLength),
              'X-Expected-Version': '1',
            },
            data: fixture.content,
          },
        );
        expect(contentResponse.status(), await contentResponse.text()).toBe(202);
        const receipt = (await contentResponse.json()) as JsonObject;
        expect(receipt['duplicate']).toBe(false);
        const document = receipt['document'] as JsonObject | undefined;
        const documentId = document?.['id'];
        expect(typeof documentId).toBe('string');
        if (typeof documentId !== 'string') return;
        uploadedDocuments.push({ id: documentId, filename: fixture.filename, version: 1, content: fixture.content });
      }

      let detail: JsonObject = {};
      const readinessFailures = await Promise.all(uploadedDocuments.map(async (uploaded) => {
        let lastReadout = 'no response';
        let delayMs = 1_000;
        const deadline = Date.now() + 120_000;
        while (Date.now() < deadline) {
          try {
            const response = await page.request.get(new URL(`/api/v1/documents/${uploaded.id}`, config.baseUrl).toString());
            if (!response.ok()) {
              const bodyLength = Number(response.headers()['content-length'] ?? 0);
              lastReadout = `HTTP ${response.status()} bodyLength=${Number.isFinite(bodyLength) ? bodyLength : 0}`;
              if (response.status() >= 500) {
                return `Document ${uploaded.filename} detail endpoint returned ${lastReadout}`;
              }
            } else {
              const current = (await response.json()) as JsonObject;
              const failure = current['failure'];
              const failureCode = failure && typeof failure === 'object' ? (failure as JsonObject)['code'] : undefined;
              lastReadout = `status=${String(current['status'])} failure=${String(failureCode ?? 'none')}`;
              if (typeof current['version'] === 'number') uploaded.version = current['version'];
              if (uploaded.id === uploadedDocuments[3]?.id) detail = current;
              if (current['id'] === uploaded.id && current['status'] === 'ready') return null;
              if (current['status'] === 'failed' || current['status'] === 'deleting') {
                return `Document ${uploaded.filename} reached terminal state (${lastReadout})`;
              }
            }
          } catch (error: unknown) {
            const kind = error instanceof Error ? error.name : 'unknown';
            lastReadout = `request-error=${kind}`;
          }
          await new Promise<void>((resolve) => setTimeout(resolve, delayMs));
          delayMs = Math.min(delayMs * 2, 5_000);
        }
        return `Document ${uploaded.filename} did not become ready (${lastReadout})`;
      }));
      const failedReadiness = readinessFailures.filter((failure): failure is string => failure !== null);
      expect(failedReadiness, failedReadiness.join('; ')).toEqual([]);
      const selectedDocument = uploadedDocuments[3];
      expect(selectedDocument).toBeDefined();
      if (!selectedDocument) return;
      const documentId = selectedDocument.id;
      const selectedFilename = selectedDocument.filename;
      expect(selectedFilename).toBe(`aura-e2e-${identityMode}.txt`);
      expect(detail['filename']).toBe(selectedFilename);
      expect(detail['version']).toEqual(expect.any(Number));

      const libraryResponse = await page.request.get(
        new URL('/api/v1/documents?limit=100', config.baseUrl).toString(),
      );
      expect(libraryResponse.ok(), await libraryResponse.text()).toBeTruthy();
      const library = (await libraryResponse.json()) as { items?: Array<JsonObject> };
      for (const uploaded of uploadedDocuments) {
        expect(library.items?.some((item) => item['id'] === uploaded.id && item['filename'] === uploaded.filename && item['status'] === 'ready')).toBe(true);
      }
      if (!documentId) return;

      const searchResponse = await page.request.post(
        new URL('/api/v1/documents/search', config.baseUrl).toString(),
        {
          headers: csrfHeaders(session, config.baseUrl),
          data: { query: marker, limit: 20 },
        },
      );
      expect(searchResponse.ok(), await searchResponse.text()).toBeTruthy();
      const searchPayload = (await searchResponse.json()) as { items?: Array<JsonObject> };
      const hit = searchPayload.items?.find((item) => item['documentId'] === documentId);
      expect(hit).toMatchObject({
        documentId,
        displayName: selectedFilename,
        evidenceId: expect.stringMatching(/^[0-9a-f-]{36}$/i),
      });
      const evidenceId = hit?.['evidenceId'];
      expect(typeof evidenceId).toBe('string');
      if (typeof evidenceId !== 'string') return;

      const downloadResponse = await page.request.get(
        new URL(`/api/v1/documents/${documentId}/content`, config.baseUrl).toString(),
      );
      expect(downloadResponse.status()).toBe(200);
      expect(await downloadResponse.body()).toEqual(selectedDocument.content);

      // This proves the citation target is an authorized document-detail route,
      // rather than a model-provided URL or a raw object-store path.
      await page.goto(new URL(`/documents/${documentId}`, config.baseUrl).toString());
      await expect(page).toHaveURL(new RegExp(`/documents/${escapeRegExp(documentId)}$`));
      await expect(page.getByRole('heading', { level: 2, name: selectedFilename, exact: true })).toBeVisible({ timeout: 15_000 });
      await expect(page.getByRole('heading', { level: 3, name: 'Extracted text preview', exact: true })).toBeVisible({ timeout: 15_000 });

      const conversationResponse = await page.request.post(
        new URL('/api/v1/conversations', config.baseUrl).toString(),
        {
          headers: csrfHeaders(session, config.baseUrl),
          data: {
            message: `${marker}: answer with the matching owner-private source.`,
            modelId: 'fixture-chat',
            clientTimeZone: 'Europe/Brussels',
          },
        },
      );
      expect(conversationResponse.status(), await conversationResponse.text()).toBe(202);
      const accepted = (await conversationResponse.json()) as JsonObject;
      const conversation = accepted['conversation'] as JsonObject | undefined;
      const run = accepted['run'] as JsonObject | undefined;
      const conversationId = conversation?.['id'];
      const runId = run?.['id'];
      expect(typeof conversationId).toBe('string');
      expect(typeof runId).toBe('string');
      if (typeof conversationId !== 'string' || typeof runId !== 'string') return;
      const completed = await waitForCompletedRun(page, config, conversationId, runId);
      const completedRun = completed.recentRuns?.find((item) => item['id'] === runId);
      expect(completedRun).toMatchObject({ status: 'completed', toolCallLimit: 1, toolCallsUsed: 1 });
      const messages = completed['messages'];
      expect(Array.isArray(messages)).toBe(true);
      const assistant = (messages as Array<JsonObject> | undefined)?.find(
        (item) => item['role'] === 'assistant' && item['runId'] === runId,
      );
      expect(assistant).toBeDefined();
      expect(assistant?.['content']).toContain('Fixture grounded answer');
      expect(assistant?.['content']).not.toContain('[[');
      const citations = assistant?.['citations'];
      expect(citations).toEqual([
        expect.objectContaining({
          documentId: expect.stringMatching(/^[0-9a-f-]{36}$/i),
          locator: expect.any(String),
        }),
      ]);
      expect(citations).toHaveLength(1);
      // The same-run evidence ID is accepted by Core but is never persisted as
      // document content, a query, or a provider URL.
      expect(JSON.stringify(citations)).not.toContain(evidenceId);

      const citedDocumentId = (citations as Array<JsonObject>)[0]?.['documentId'];
      expect(typeof citedDocumentId).toBe('string');
      const citedDocument = uploadedDocuments.find((uploaded) => uploaded.id === citedDocumentId);
      expect(citedDocument).toBeDefined();
      if (!citedDocument || typeof citedDocumentId !== 'string') return;
      await page.goto(new URL(`/documents/${citedDocumentId}`, config.baseUrl).toString());
      await expect(page.getByRole('heading', { level: 2, name: citedDocument.filename, exact: true })).toBeVisible({ timeout: 15_000 });

      for (const uploaded of uploadedDocuments) {
        const deleteResponse = await page.request.delete(
          new URL(`/api/v1/documents/${uploaded.id}`, config.baseUrl).toString(),
          {
            headers: csrfHeaders(session, config.baseUrl),
            data: { expectedVersion: uploaded.id === documentId ? detail['version'] : uploaded.version },
          },
        );
        expect(deleteResponse.status(), await deleteResponse.text()).toBe(202);
      }

      for (const uploaded of uploadedDocuments) {
        await expect.poll(async () => {
          const response = await page.request.post(new URL('/api/v1/documents/search', config.baseUrl).toString(), {
            headers: csrfHeaders(session, config.baseUrl),
            data: { query: marker, limit: 20 },
          });
          if (!response.ok()) return false;
          const payload = (await response.json()) as { items?: Array<JsonObject> };
          return !(payload.items ?? []).some((item) => item['documentId'] === uploaded.id);
        }, { timeout: 120_000, intervals: [1_000, 2_000, 5_000] }).toBe(true);
        await expect.poll(async () => {
          const response = await page.request.get(new URL(`/api/v1/documents/${uploaded.id}/content`, config.baseUrl).toString());
          return response.status() === 404;
        }, { timeout: 120_000, intervals: [1_000, 2_000, 5_000] }).toBe(true);
        await expect.poll(async () => {
          const response = await page.request.get(new URL(`/api/v1/documents/${uploaded.id}`, config.baseUrl).toString());
          return response.status() === 404;
        }, { timeout: 120_000, intervals: [1_000, 2_000, 5_000] }).toBe(true);
      }

      // A fresh run still exercises the native tool path after deletion. The
      // empty result causes the fake provider's invalid marker to be discarded,
      // proving deleted evidence cannot become a new citation.
      const postDeleteResponse = await page.request.post(
        new URL('/api/v1/conversations', config.baseUrl).toString(),
        {
          headers: csrfHeaders(session, config.baseUrl),
          data: {
            message: `${marker}: confirm whether a deleted source is still available.`,
            modelId: 'fixture-chat',
            clientTimeZone: 'Europe/Brussels',
          },
        },
      );
      expect(postDeleteResponse.status(), await postDeleteResponse.text()).toBe(202);
      const postDeleteAccepted = (await postDeleteResponse.json()) as JsonObject;
      const postDeleteConversation = postDeleteAccepted['conversation'] as JsonObject | undefined;
      const postDeleteRun = postDeleteAccepted['run'] as JsonObject | undefined;
      const postDeleteConversationId = postDeleteConversation?.['id'];
      const postDeleteRunId = postDeleteRun?.['id'];
      expect(typeof postDeleteConversationId).toBe('string');
      expect(typeof postDeleteRunId).toBe('string');
      if (typeof postDeleteConversationId !== 'string' || typeof postDeleteRunId !== 'string') return;
      const postDeleteDetail = await waitForCompletedRun(page, config, postDeleteConversationId, postDeleteRunId);
      const postDeleteCompletedRun = postDeleteDetail.recentRuns?.find((item) => item['id'] === postDeleteRunId);
      expect(postDeleteCompletedRun).toMatchObject({ status: 'completed', toolCallLimit: 1, toolCallsUsed: 1 });
      const postDeleteAssistant = (postDeleteDetail['messages'] as Array<JsonObject> | undefined)?.find(
        (item) => item['role'] === 'assistant' && item['runId'] === postDeleteRunId,
      );
      expect(postDeleteAssistant).toBeDefined();
      expect(postDeleteAssistant?.['citations'] ?? []).toEqual([]);
      expect(postDeleteAssistant?.['content']).not.toContain('[[');
    }
  });
}
