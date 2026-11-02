import AxeBuilder from '@axe-core/playwright';
import { expect, test, type Page, type Route } from '@playwright/test';

const NOW = '2026-10-31T10:00:00.000Z';
const CONVERSATION_ID = '01530153-0153-4153-8153-015301530153';
const RESEARCH_ID = '01530153-0153-4153-8153-015301530154';
const TASK_ID = '01530153-0153-4153-8153-015301530155';
const ARTIFACT_ID = '01530153-0153-4153-8153-015301530156';
const SOURCE_ID = '01530153-0153-4153-8153-015301530157';
const DELETED_SOURCE_ID = '01530153-0153-4153-8153-015301530158';
const PRIVATE_QUESTION_CANARY = 'aura-0153-owner-question-private-canary';
const PRIVATE_CITATION_LABEL_CANARY = 'aura-0153-citation-label-private-canary';
const PRIVATE_CITATION_URL_CANARY = 'aura-0153-citation-url-private-canary';
const PRIVATE_ARTIFACT_TITLE_CANARY = 'aura-0153-artifact-title-private-canary';
const PRIVATE_ARTIFACT_CONTENT_CANARY = 'aura-0153-artifact-content-private-canary';
const PRIVATE_SOURCE_LABEL_CANARY = 'aura-0153-source-label-private-canary';
const PRIVATE_CANARIES = [PRIVATE_QUESTION_CANARY, PRIVATE_CITATION_LABEL_CANARY, PRIVATE_CITATION_URL_CANARY, PRIVATE_ARTIFACT_TITLE_CANARY, PRIVATE_ARTIFACT_CONTENT_CANARY, PRIVATE_SOURCE_LABEL_CANARY];

function budget() {
  return { wallTimeSeconds: 90, fetchedSources: 6, searchRequests: 2, browserPages: 1, modelCalls: 2 };
}

function session() {
  return { principal: { issuer: 'synthetic', subject: 'aura-0153-owner', displayName: 'Fixture Owner' }, csrfToken: 'aura-0153-csrf', idleExpiresAt: '2099-10-31T10:00:00Z', absoluteExpiresAt: '2099-11-01T10:00:00Z' };
}

function researchDetail() {
  return { id: RESEARCH_ID, taskRef: { taskId: TASK_ID }, mode: 'quick', stage: 'terminal', outcome: 'completed', contentState: 'active', version: 2, capabilities: { canCancel: false, canExtend: false, canDelete: true, canRetryCleanup: false, canReconcileCleanup: false }, createdAt: NOW, updatedAt: NOW, finishedAt: NOW, question: PRIVATE_QUESTION_CANARY, admittedBudget: budget(), consumedBudget: { ...budget(), fetchedSources: 2 }, extensionAccepted: false, sourceCount: 2, failureClass: null, artifactRefs: [{ artifactId: ARTIFACT_ID }], citations: [{ kind: 'web', sourceId: SOURCE_ID, label: PRIVATE_CITATION_LABEL_CANARY, url: `https://example.com/${PRIVATE_CITATION_URL_CANARY}`, retrievedAt: NOW, locator: 'section:1' }] };
}

type ResearchScenario = { readonly outcome: 'partial' | 'budget_exhausted' | 'interrupted' | 'failed'; readonly failureClass?: 'provider_disabled' | 'provider_unavailable'; readonly canExtend?: boolean; readonly extensionAccepted?: boolean };
function researchScenarioDetail(scenario: ResearchScenario) {
  const detail = researchDetail();
  return { ...detail, outcome: scenario.outcome, failureClass: scenario.failureClass ?? null, extensionAccepted: scenario.extensionAccepted ?? false, capabilities: { ...detail.capabilities, canExtend: scenario.canExtend ?? false } };
}

function taskDetail(hasSourceDetail: boolean, sourceKind: 'research' | 'workflow_instance' = 'research') {
  return { id: TASK_ID, sourceKind, sourceId: RESEARCH_ID, sourceVersion: 2, status: 'completed', sourceStatus: 'completed', sourceOutcome: 'completed', stage: 'persisting', progress: 1, capabilities: { canCancel: false, canRetry: false, hasSourceDetail }, routeKey: sourceKind === 'research' ? 'research_detail' : 'workflow_instance_detail', createdAt: NOW, updatedAt: NOW, displayLabel: PRIVATE_SOURCE_LABEL_CANARY, failureClass: null, recoveryAction: null, artifactRefs: [] };
}

function artifactDetail(contentState: 'active' | 'deletion_failed' | 'cleanup_unknown' | 'deleted' = 'deletion_failed') {
  return { id: ARTIFACT_ID, kind: 'research_report', title: contentState === 'active' ? PRIVATE_ARTIFACT_TITLE_CANARY : 'Deleted artifact', mediaType: 'text/plain; charset=utf-8', contentState, currentRevision: 1, version: 4, capabilities: { canPreview: contentState === 'active', canDownload: contentState === 'active', canDelete: contentState === 'active', canRetryCleanup: contentState === 'deletion_failed', canReconcileCleanup: contentState === 'cleanup_unknown' }, createdAt: NOW, updatedAt: NOW, lineage: { taskId: TASK_ID, runId: null, conversationId: CONVERSATION_ID, workspaceId: null }, integrity: { revision: 1, sha256: 'a'.repeat(64), byteCount: 32, createdAt: NOW }, revision: { revision: 1, sha256: 'a'.repeat(64), byteCount: 32, createdAt: NOW }, deletionAuditId: 'audit-0153' };
}

function researchDeletedDetail(contentState: 'deletion_failed' | 'cleanup_unknown' | 'deleted') {
  return { id: RESEARCH_ID, taskRef: { taskId: TASK_ID }, mode: 'quick', stage: 'terminal', outcome: 'completed', contentState, version: 7, capabilities: { canCancel: false, canExtend: false, canDelete: false, canRetryCleanup: contentState === 'deletion_failed', canReconcileCleanup: contentState === 'cleanup_unknown' }, createdAt: NOW, updatedAt: NOW, finishedAt: NOW, tombstoneLabel: 'Research source deleted' };
}

type RecordedRequest = { readonly method: string; readonly path: string; readonly body: string | null; readonly idempotencyKey: string | null; readonly status: number };
type BrowserPrivacySnapshot = { readonly storage: string; readonly indexedDb: string; readonly caches: string; readonly serviceWorkers: string; readonly history: string; readonly urls: ReadonlyArray<string>; readonly storageKeys: ReadonlyArray<string> };

async function installPrivacyRecorder(page: Page): Promise<void> {
  await page.addInitScript(() => {
    const urls: string[] = [];
    const target = window as unknown as { __aura0153History?: string[] };
    target.__aura0153History = urls;
    for (const method of ['pushState', 'replaceState'] as const) {
      const original = history[method].bind(history);
      history[method] = ((state: unknown, title: string, url?: string | URL | null) => { urls.push(JSON.stringify({ state, url: url ? new URL(url, location.href).href : location.href })); return original(state, title, url); }) as typeof history[typeof method];
    }
  });
}

async function privacySnapshot(page: Page): Promise<BrowserPrivacySnapshot> {
  return page.evaluate(async () => {
    const storageEntries = (storage: Storage): Array<[string, string]> => Array.from({ length: storage.length }, (_, index) => { const key = storage.key(index) ?? ''; return [key, storage.getItem(key) ?? '']; });
    const serialize = (value: unknown): string => { try { return JSON.stringify(value); } catch { return String(value); } };
    const indexedDbValues: unknown[] = [];
    const databases = typeof indexedDB.databases === 'function' ? await indexedDB.databases() : [];
    for (const database of databases) {
      if (!database.name) continue;
      indexedDbValues.push(database.name);
      await new Promise<void>((resolve) => {
        const request = indexedDB.open(database.name!);
        request.onerror = () => resolve();
        request.onsuccess = () => {
          const db = request.result;
          const names = Array.from(db.objectStoreNames);
          if (!names.length) { db.close(); resolve(); return; }
          const transaction = db.transaction(names, 'readonly');
          for (const name of names) transaction.objectStore(name).getAll().onsuccess = (event) => indexedDbValues.push((event.target as IDBRequest).result);
          transaction.oncomplete = () => { db.close(); resolve(); };
          transaction.onerror = () => { db.close(); resolve(); };
        };
      });
    }
    const cacheValues: unknown[] = [];
    if ('caches' in window) for (const cacheName of await caches.keys()) {
      cacheValues.push(cacheName);
      const cache = await caches.open(cacheName);
      for (const request of await cache.keys()) { cacheValues.push(request.url); const response = await cache.match(request); if (response) cacheValues.push(await response.clone().text().catch(() => '')); }
    }
    const registrations = 'serviceWorker' in navigator ? await navigator.serviceWorker.getRegistrations() : [];
    const serviceWorkers = registrations.map((registration) => ({ scope: registration.scope, scriptURL: registration.active?.scriptURL ?? registration.waiting?.scriptURL ?? registration.installing?.scriptURL ?? '' }));
    const historyValues = (window as unknown as { __aura0153History?: string[] }).__aura0153History ?? [];
    const local = storageEntries(localStorage); const session = storageEntries(sessionStorage);
    return { storage: serialize({ local, session }), indexedDb: serialize(indexedDbValues), caches: serialize(cacheValues), serviceWorkers: serialize(serviceWorkers), history: serialize(historyValues), urls: [location.href, ...historyValues], storageKeys: [...local, ...session].map(([key]) => key) };
  });
}

async function assertPrivacy(page: Page): Promise<void> {
  const snapshot = await privacySnapshot(page);
  const serialized = JSON.stringify(snapshot);
  for (const canary of PRIVATE_CANARIES) expect(serialized).not.toContain(canary);
  expect(snapshot.urls.every((value) => !PRIVATE_CANARIES.some((canary) => value.includes(canary)))).toBeTruthy();
  expect(snapshot.storageKeys.every((key) => key === 'aura-theme')).toBeTruthy();
  expect(new URL(page.url()).pathname + new URL(page.url()).search + new URL(page.url()).hash).not.toContain(PRIVATE_QUESTION_CANARY);
}

async function installFixture(page: Page, options: { readonly taskHasSourceDetail?: boolean; readonly workflowTask?: boolean; readonly retryDelayMs?: number; readonly researchCleanupState?: 'deletion_failed' | 'cleanup_unknown'; readonly artifactCleanupState?: 'deletion_failed' | 'cleanup_unknown'; readonly researchScenario?: ResearchScenario } = {}): Promise<{ readonly requests: RecordedRequest[]; readonly loseSession: () => void; readonly setResearchScenario: (scenario: ResearchScenario | undefined) => void; readonly setArtifactState: (state: 'active' | 'deletion_failed' | 'cleanup_unknown' | 'deleted') => void }> {
  const requests: RecordedRequest[] = [];
  let loseNextSession = false;
  let currentResearchScenario = options.researchScenario;
  let currentArtifactState: 'active' | 'deletion_failed' | 'cleanup_unknown' | 'deleted' = options.artifactCleanupState ?? 'deletion_failed';
  await page.route('**/api/v1/**', async (route: Route) => {
    const request = route.request();
    const url = new URL(request.url());
    const requestRecord = { method: request.method(), path: url.pathname, body: request.postData(), idempotencyKey: request.headers()['idempotency-key'] ?? request.headers()['x-idempotency-key'] ?? null, status: 200 };
    requests.push(requestRecord);
    const json = (body: unknown, status = 200): Promise<void> => { requestRecord.status = status; return route.fulfill({ status, headers: { 'cache-control': 'no-store' }, json: body }); };
    if (url.pathname === '/api/v1/auth/session') { if (loseNextSession) { loseNextSession = false; return json({ title: 'Session unavailable' }, 401); } return json(session()); }
    if (url.pathname === '/api/v1/models') return json({ models: [{ id: 'fixture-model', displayName: 'Fixture model', provider: 'ollama', capabilities: ['chat'], availability: 'available', selectable: true, disabledReason: null }], defaultModelId: 'fixture-model', observedAt: NOW });
    if (url.pathname === '/api/v1/agents') return json({ items: [] });
    if (url.pathname === '/api/v1/personas') return json({ items: [] });
    if (url.pathname === '/api/v1/notifications') return json({ items: [], nextCursor: null, unacknowledgedCount: 0 });
    if (url.pathname === '/api/v1/conversations' && request.method() === 'GET') return json({ items: [{ id: CONVERSATION_ID, title: 'Research conversation', agentProfileId: 'aura', agentRevisionId: 'aura-1', modelId: 'fixture-model', version: 1, createdAt: NOW, updatedAt: NOW, archivedAt: null, currentRun: null }], nextCursor: null });
    if (url.pathname === `/api/v1/conversations/${CONVERSATION_ID}`) return json({ id: CONVERSATION_ID, title: 'Research conversation', agentProfileId: 'aura', agentRevisionId: 'aura-1', modelId: 'fixture-model', version: 1, createdAt: NOW, updatedAt: NOW, archivedAt: null, currentRun: null, messages: [{ id: '01530153-0153-4153-8153-015301530159', conversationId: CONVERSATION_ID, role: 'assistant', content: `Cited answer for ${PRIVATE_QUESTION_CANARY}`, state: 'complete', runId: null, webCitations: [{ kind: 'web', sourceId: SOURCE_ID, label: PRIVATE_CITATION_LABEL_CANARY, url: `https://example.com/${PRIVATE_CITATION_URL_CANARY}`, retrievedAt: NOW, locator: 'section:1' }, { kind: 'deleted_research', sourceId: DELETED_SOURCE_ID, label: 'Research source deleted' }, { kind: 'web', sourceId: 'not-a-uuid', label: PRIVATE_CITATION_LABEL_CANARY, url: `https://example.com/${PRIVATE_CITATION_URL_CANARY}?owner=private`, retrievedAt: NOW, locator: null }], taskRefs: [{ taskId: TASK_ID }], artifactRefs: [{ artifactId: ARTIFACT_ID }], createdAt: NOW, updatedAt: NOW }], recentRuns: [] });
    if (url.pathname === '/api/v1/research/settings') return json({ version: 1, presets: ['quick', 'auto', 'deep', 'agent_led'].map((mode) => ({ mode, maximum: budget(), defaults: budget() })), updatedAt: NOW });
    if (url.pathname === '/api/v1/research' && request.method() === 'GET') return json({ items: [], nextCursor: null });
    if (url.pathname === `/api/v1/research/${RESEARCH_ID}/deletions/retry` || url.pathname === `/api/v1/research/${RESEARCH_ID}/deletions/reconcile`) { if (options.retryDelayMs) await new Promise((resolve) => setTimeout(resolve, options.retryDelayMs)); return json(researchDeletedDetail('deleted')); }
    if (url.pathname === `/api/v1/research/${RESEARCH_ID}`) return json(options.researchCleanupState ? researchDeletedDetail(options.researchCleanupState) : currentResearchScenario ? researchScenarioDetail(currentResearchScenario) : researchDetail());
    if (url.pathname === `/api/v1/research/${RESEARCH_ID}/evidence`) return json({ items: [], nextCursor: null });
    if (url.pathname === `/api/v1/tasks/${TASK_ID}`) return json(options.workflowTask ? taskDetail(false, 'workflow_instance') : taskDetail(options.taskHasSourceDetail ?? true));
    if (url.pathname === '/api/v1/tasks' && request.method() === 'GET') return json({ items: [options.workflowTask ? taskDetail(false, 'workflow_instance') : taskDetail(options.taskHasSourceDetail ?? true)], nextCursor: null });
    if (url.pathname === `/api/v1/tasks/${TASK_ID}/events`) return route.fulfill({ status: 200, headers: { 'cache-control': 'no-store', 'content-type': 'text/event-stream' }, body: '' });
    if (url.pathname === `/api/v1/artifacts/${ARTIFACT_ID}/revisions`) return json([]);
    if (url.pathname === `/api/v1/artifacts/${ARTIFACT_ID}/deletions/retry` || url.pathname === `/api/v1/artifacts/${ARTIFACT_ID}/deletions/reconcile`) { if (options.retryDelayMs) await new Promise((resolve) => setTimeout(resolve, options.retryDelayMs)); return json(artifactDetail('deleted')); }
    if (url.pathname === `/api/v1/artifacts/${ARTIFACT_ID}`) return json(artifactDetail(currentArtifactState));
    if (url.pathname === `/api/v1/artifacts/${ARTIFACT_ID}/preview`) return json({ artifactId: ARTIFACT_ID, revision: 1, mediaType: 'text/plain', text: PRIVATE_ARTIFACT_CONTENT_CANARY, truncated: false });
    if (url.pathname === '/api/v1/artifacts' && request.method() === 'GET') return json({ items: [{ id: ARTIFACT_ID, kind: 'generated_text', title: PRIVATE_ARTIFACT_TITLE_CANARY, mediaType: 'text/plain', contentState: 'active', currentRevision: 1, version: 1, createdAt: NOW, updatedAt: NOW }], nextCursor: null });
    return json({ items: [], nextCursor: null });
  });
  return { requests, loseSession: () => { loseNextSession = true; }, setResearchScenario: (scenario) => { currentResearchScenario = scenario; }, setArtifactState: (state) => { currentArtifactState = state; } };
}

test.describe('AURA-0153 governed product UX', () => {
  test('hydrates generated references and keeps private values out of browser persistence and URLs', async ({ page }) => {
    const fixture = await installFixture(page);
    await installPrivacyRecorder(page);
    await page.addInitScript(() => localStorage.setItem('aura-theme', 'dark'));
    await page.goto(`/conversation/${CONVERSATION_ID}`);
    await expect(page.getByText('Web research sources')).toBeVisible();
    await expect(page.getByRole('link', { name: `Open web source ${PRIVATE_CITATION_LABEL_CANARY}` })).toHaveAttribute('href', `https://example.com/${PRIVATE_CITATION_URL_CANARY}`);
    await expect(page.getByText('Research source deleted')).toBeVisible();
    await assertPrivacy(page);
    const conversationAxe = await new AxeBuilder({ page }).analyze();
    expect(conversationAxe.violations, 'Conversation citations have accessibility violations').toEqual([]);
    const webCitation = page.getByRole('link', { name: `Open web source ${PRIVATE_CITATION_LABEL_CANARY}` });
    const popup = page.waitForEvent('popup');
    await webCitation.focus();
    await webCitation.press('Enter');
    await expect(await popup).toHaveURL(`https://example.com/${PRIVATE_CITATION_URL_CANARY}`);
    await (await popup).close();
    await expect(page.getByRole('link', { name: `Open research task ${TASK_ID}` })).toHaveAttribute('href', `/tasks/${TASK_ID}`);
    await expect(page.getByRole('link', { name: `Open generated artifact ${ARTIFACT_ID}` })).toHaveAttribute('href', `/artifacts/${ARTIFACT_ID}`);
    const taskLink = page.getByRole('link', { name: `Open research task ${TASK_ID}` });
    await taskLink.focus();
    await taskLink.press('Enter');
    await expect(page).toHaveURL(new RegExp(`/tasks/${TASK_ID}$`));
    await expect(page.getByRole('status').filter({ hasText: /Live updates are reconnecting/ })).toBeVisible();
    await expect.poll(() => fixture.requests.filter((request) => request.path === `/api/v1/tasks/${TASK_ID}`).length).toBeGreaterThan(1);
    await assertPrivacy(page);
    await page.getByRole('button', { name: /Back to tasks/ }).press('Enter');
    await expect(page).toHaveURL(/\/tasks$/);
    await page.goto(`/conversation/${CONVERSATION_ID}`);
    const artifactLink = page.getByRole('link', { name: `Open generated artifact ${ARTIFACT_ID}` });
    await artifactLink.focus();
    await artifactLink.press('Enter');
    await expect(page).toHaveURL(new RegExp(`/artifacts/${ARTIFACT_ID}$`));
    const artifactBack = page.getByRole('link', { name: /Back to artifacts/ });
    await expect(artifactBack).toHaveAttribute('href', '/artifacts');
    await artifactBack.focus();
    await expect(artifactBack).toBeFocused();
    await artifactBack.press('Enter');
    await expect(page).toHaveURL(/\/artifacts$/);
    await page.reload();
    await page.goto(`/conversation/${CONVERSATION_ID}`);
    await assertPrivacy(page);
    expect(new URL(page.url()).search).toBe('');
    expect(new URL(page.url()).hash).toBe('');
  });

  test('fails closed when Task Center cannot construct a source route', async ({ page }) => {
    await installFixture(page, { taskHasSourceDetail: false, workflowTask: true });
    await page.goto(`/tasks/${TASK_ID}`);
    await expect(page.getByRole('heading', { name: PRIVATE_SOURCE_LABEL_CANARY })).toBeVisible();
    await expect(page.getByText(/does not expose a direct detail view/)).toBeVisible();
    await expect(page.getByRole('link', { name: /Open source/ })).toHaveCount(0);
    const result = await new AxeBuilder({ page }).analyze();
    expect(result.violations, 'Task detail has accessibility violations').toEqual([]);
  });

  test('renders table-driven research recovery explanations and truthful actions', async ({ page }) => {
    const fixture = await installFixture(page);
    const scenarios: ReadonlyArray<{ readonly name: string; readonly scenario: ResearchScenario; readonly heading: string; readonly copy: string; readonly action: 'new' | 'extend' | 'none' }> = [
      { name: 'partial', scenario: { outcome: 'partial' }, heading: 'Research is partial', copy: 'coverage remains incomplete', action: 'new' },
      { name: 'budget extension available', scenario: { outcome: 'budget_exhausted', canExtend: true }, heading: 'Research reached its budget', copy: 'You may extend the admitted budget once', action: 'extend' },
      { name: 'budget extension already used', scenario: { outcome: 'budget_exhausted', canExtend: true, extensionAccepted: true }, heading: 'Research reached its budget', copy: 'already been used', action: 'new' },
      { name: 'budget extension unavailable', scenario: { outcome: 'budget_exhausted' }, heading: 'Research reached its budget', copy: 'extension is unavailable', action: 'new' },
      { name: 'interrupted', scenario: { outcome: 'interrupted' }, heading: 'Research was interrupted', copy: 'Start new research', action: 'new' },
      { name: 'provider disabled', scenario: { outcome: 'failed', failureClass: 'provider_disabled' }, heading: 'Web research is disabled', copy: 'capability is currently disabled', action: 'none' },
      { name: 'provider unavailable', scenario: { outcome: 'failed', failureClass: 'provider_unavailable' }, heading: 'Research provider unavailable', copy: 'Start new research', action: 'new' },
    ];
    for (const item of scenarios) {
      fixture.setResearchScenario(item.scenario);
      await page.goto(`/research/${RESEARCH_ID}?scenario=${item.name.replaceAll(' ', '-')}`);
      await expect(page.getByRole('heading', { name: item.heading })).toBeVisible();
      await expect(page.locator('.recovery')).toContainText(item.copy);
      await expect(page.getByRole('button', { name: 'Extend admitted budget once' })).toHaveCount(item.action === 'extend' ? 1 : 0);
      await expect(page.getByRole('link', { name: 'Start new research' })).toHaveCount(item.action === 'new' ? 1 : 0);
    }
  });

  test('keeps deletion cleanup recovery actionable, versioned, and keyboard reachable', async ({ page }) => {
    const fixture = await installFixture(page, { retryDelayMs: 500 });
    await page.goto(`/artifacts/${ARTIFACT_ID}`);
    await expect(page.getByRole('heading', { name: 'Artifact cleanup needs a retry' })).toBeVisible();
    await expect(page.locator('[aria-live="assertive"]')).toContainText('cleanup needs a retry');
    const retry = page.getByRole('button', { name: 'Retry cleanup' });
    await retry.focus();
    await expect(retry).toBeEnabled();
    await retry.press('Enter');
    await expect(page.getByText(/Retrying artifact cleanup/)).toBeVisible();
    await expect(retry).toBeDisabled();
    await expect(page.getByRole('heading', { name: 'Artifact deleted' })).toBeVisible();
    await expect(fixture.requests.some((request) => request.path.endsWith(`/artifacts/${ARTIFACT_ID}/deletions/retry`) && request.method === 'POST')).toBeTruthy();
    const command = fixture.requests.find((request) => request.path.endsWith(`/artifacts/${ARTIFACT_ID}/deletions/retry`));
    expect(fixture.requests.filter((request) => request.path.endsWith(`/artifacts/${ARTIFACT_ID}/deletions/retry`))).toHaveLength(1);
    expect(command?.body).toContain('expectedVersion');
    expect(command?.body).toContain('4');
    expect(command?.idempotencyKey).toBeTruthy();
    await expect(page.locator('[role="status"][tabindex="-1"]')).toBeFocused();
    await assertPrivacy(page);
    const result = await new AxeBuilder({ page }).analyze();
    expect(result.violations, 'Artifact detail has accessibility violations').toEqual([]);
  });

  test('executes research retry cleanup exactly once and settles on authoritative state', async ({ page }) => {
    const fixture = await installFixture(page, { researchCleanupState: 'deletion_failed', retryDelayMs: 500 });
    await installPrivacyRecorder(page);
    await page.goto(`/research/${RESEARCH_ID}`);
    const retry = page.getByRole('button', { name: 'Retry cleanup' });
    await expect(page.locator('[aria-live="assertive"]')).toContainText('cleanup needs a retry');
    await retry.focus();
    await retry.press('Enter');
    await expect(page.getByText(/Retrying research cleanup/)).toBeVisible();
    await expect(retry).toBeDisabled();
    await expect(page.getByRole('heading', { name: 'Research source deleted' })).toBeVisible();
    await expect(page.locator('[role="status"][tabindex="-1"]')).toBeFocused();
    const recoveryAxe = await new AxeBuilder({ page }).analyze();
    expect(recoveryAxe.violations, 'Research cleanup recovery has accessibility violations').toEqual([]);
    const commands = fixture.requests.filter((request) => request.path.endsWith(`/research/${RESEARCH_ID}/deletions/retry`));
    expect(commands).toHaveLength(1);
    expect(commands[0].method).toBe('POST');
    expect(commands[0].body).toContain('expectedVersion');
    expect(commands[0].body).toContain('7');
    expect(commands[0].idempotencyKey).toBeTruthy();
    await assertPrivacy(page);
  });

  test('executes research reconciliation exactly once and exposes only the settled state', async ({ page }) => {
    const fixture = await installFixture(page, { researchCleanupState: 'cleanup_unknown', retryDelayMs: 500 });
    await installPrivacyRecorder(page);
    await page.goto(`/research/${RESEARCH_ID}`);
    const reconcile = page.getByRole('button', { name: 'Reconcile cleanup' });
    await expect(page.locator('[aria-live="assertive"]')).toContainText('cleanup is being reconciled');
    await reconcile.focus();
    await reconcile.press('Enter');
    await expect(page.getByText(/Reconciling research cleanup/)).toBeVisible();
    await expect(reconcile).toBeDisabled();
    await expect(page.getByRole('heading', { name: 'Research source deleted' })).toBeVisible();
    await expect(page.locator('[role="status"][tabindex="-1"]')).toBeFocused();
    const commands = fixture.requests.filter((request) => request.path.endsWith(`/research/${RESEARCH_ID}/deletions/reconcile`));
    expect(commands).toHaveLength(1);
    expect(commands[0].method).toBe('POST');
    expect(commands[0].body).toContain('expectedVersion');
    expect(commands[0].body).toContain('7');
    expect(commands[0].idempotencyKey).toBeTruthy();
    await assertPrivacy(page);
  });

  test('executes artifact reconciliation exactly once and handles session loss without persistence', async ({ page }) => {
    const fixture = await installFixture(page, { artifactCleanupState: 'cleanup_unknown', retryDelayMs: 500 });
    await installPrivacyRecorder(page);
    await page.goto(`/artifacts/${ARTIFACT_ID}`);
    const reconcile = page.getByRole('button', { name: 'Reconcile cleanup' });
    await expect(page.locator('[aria-live="assertive"]')).toContainText('cleanup is being reconciled');
    await reconcile.focus();
    await reconcile.press('Enter');
    await expect(page.getByText(/Reconciling artifact cleanup/)).toBeVisible();
    await expect(reconcile).toBeDisabled();
    await expect(page.getByRole('heading', { name: 'Artifact deleted' })).toBeVisible();
    await expect(page.locator('[role="status"][tabindex="-1"]')).toBeFocused();
    const commands = fixture.requests.filter((request) => request.path.endsWith(`/artifacts/${ARTIFACT_ID}/deletions/reconcile`));
    expect(commands).toHaveLength(1);
    expect(commands[0].method).toBe('POST');
    expect(commands[0].body).toContain('expectedVersion');
    expect(commands[0].body).toContain('4');
    expect(commands[0].idempotencyKey).toBeTruthy();
    await assertPrivacy(page);
    fixture.setArtifactState('active');
    await page.goto(`/artifacts/${ARTIFACT_ID}?preview=1`);
    await expect(page.locator('pre')).toContainText(PRIVATE_ARTIFACT_CONTENT_CANARY);
    await assertPrivacy(page);
    await page.evaluate(() => fetch('/api/v1/auth/logout', { method: 'POST' }));
    await assertPrivacy(page);
    fixture.loseSession();
    await page.reload();
    expect(fixture.requests.some((request) => request.path === '/api/v1/auth/session' && request.status === 401)).toBeTruthy();
    await assertPrivacy(page);
  });

  test('preserves focus and announces dialogs, forced colors, reduced motion, and zoom-safe recovery actions', async ({ page }) => {
    const fixture = await installFixture(page, { retryDelayMs: 300 });
    await installPrivacyRecorder(page);
    await page.emulateMedia({ reducedMotion: 'reduce' });
    await page.goto(`/research/${RESEARCH_ID}`);
    const deleteButton = page.getByRole('button', { name: 'Delete retained research' });
    await deleteButton.focus();
    await deleteButton.press('Enter');
    await expect(page.getByRole('alertdialog')).toBeVisible();
    const dialogAxe = await new AxeBuilder({ page }).include('[role="alertdialog"]').analyze();
    expect(dialogAxe.violations, 'Confirmation dialog has accessibility violations').toEqual([]);
    await page.keyboard.press('Escape');
    await expect(page.getByRole('alertdialog')).toHaveCount(0);
    await expect(deleteButton).toBeFocused();
    const motionAndZoom = await page.evaluate(() => {
      const skeleton = document.createElement('div');
      skeleton.className = 'loading-skeleton';
      const skeletonBar = document.createElement('span');
      skeleton.append(skeletonBar);
      document.body.append(skeleton);
      document.documentElement.style.zoom = '200%';
      const computedSkeleton = getComputedStyle(skeletonBar);
      const body = document.body;
      const result = {
        reduced: computedSkeleton.animationName === 'none' && Number.parseFloat(computedSkeleton.animationDuration) <= 0.01,
        scrollBehavior: getComputedStyle(body).getPropertyValue('scroll-behavior'),
        overflow: document.documentElement.scrollWidth <= document.documentElement.clientWidth + 2,
      };
      skeleton.remove();
      return result;
    });
    expect(motionAndZoom.reduced).toBeTruthy();
    expect(motionAndZoom.scrollBehavior).not.toBe('smooth');
    expect(motionAndZoom.overflow).toBeTruthy();
    await page.emulateMedia({ forcedColors: 'active' });
    expect(await page.evaluate(() => matchMedia('(forced-colors: active)').matches)).toBeTruthy();
    await expect(deleteButton).toBeVisible();
    await deleteButton.focus();
    await expect(deleteButton).toBeFocused();
    await page.goto(`/artifacts/${ARTIFACT_ID}`);
    await page.evaluate(() => { document.documentElement.style.zoom = '200%'; });
    const retryAtZoom = page.getByRole('button', { name: 'Retry cleanup' });
    await retryAtZoom.scrollIntoViewIfNeeded();
    await expect(retryAtZoom).toBeVisible();
    const recoveryBounds = await retryAtZoom.boundingBox();
    expect(recoveryBounds?.width ?? 0).toBeGreaterThan(0);
    expect(recoveryBounds?.height ?? 0).toBeGreaterThan(0);
    expect(recoveryBounds?.x ?? -1).toBeGreaterThanOrEqual(0);
    expect((recoveryBounds?.x ?? 0) + (recoveryBounds?.width ?? 0)).toBeLessThanOrEqual(await page.evaluate(() => window.innerWidth) + 2);
    expect(fixture.requests.length).toBeGreaterThan(0);
  });
});
