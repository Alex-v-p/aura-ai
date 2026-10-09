import { expect, test, type Page, type Route } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';

const inventory = {
  observedAt: '2026-10-08T09:00:00Z',
  models: [
    { id: 'qwen3:8b', displayName: 'Qwen 3 · 8B', provider: 'ollama', modelRevision: 'qwen3-8b-r1', modelDigest: 'sha256:qwen-extraction', capabilities: ['structured_output'], dimension: null, available: true, disabledReason: null },
    { id: 'qwen3-embedding:4b', displayName: 'Qwen 3 Embedding · 4B', provider: 'ollama', modelRevision: 'qwen3-embedding-4b-r1', modelDigest: 'sha256:qwen-embedding', capabilities: ['embedding'], dimension: 2560, available: true, disabledReason: null },
  ],
};

const idleReindex = {
  phase: 'idle', activeGeneration: null, replacementGeneration: null,
  processedRevisionCount: 0, totalRevisionCount: 0, startedAt: null, updatedAt: null,
  completedAt: null, retryable: false,
};

const configured = (version = 1) => ({
  version,
  extraction: { modelId: 'qwen3:8b', modelRevision: 'qwen3-8b-r1', modelDigest: 'sha256:qwen-extraction' },
  embedding: { modelId: 'qwen3-embedding:4b', modelRevision: 'qwen3-embedding-4b-r1', modelDigest: 'sha256:qwen-embedding' },
  activeGeneration: null, buildingGeneration: null, updatedAt: '2026-10-08T09:00:00Z',
});

async function mockShellApi(page: Page, feature: (route: Route, url: URL) => Promise<void>): Promise<void> {
  await page.route('**/api/v1/**', async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname.endsWith('/auth/session')) return route.fulfill({ json: {
      principal: { issuer: 'https://authentik.test', subject: 'fixture-owner', displayName: 'Fixture Owner' },
      csrfToken: 'fixture-csrf', idleExpiresAt: '2099-10-08T10:00:00Z', absoluteExpiresAt: '2099-10-09T10:00:00Z',
    } });
    if (url.pathname.endsWith('/models')) return route.fulfill({ json: { models: [{ id: 'fixture-model', displayName: 'Fixture model', provider: 'fixture', capabilities: ['chat'], availability: 'available', selectable: true, disabledReason: null }], defaultModelId: 'fixture-model' } });
    if (url.pathname.endsWith('/conversations')) return route.fulfill({ json: { items: [], nextCursor: null } });
    if (url.pathname.endsWith('/agents')) return route.fulfill({ json: { items: [] } });
    return feature(route, url);
  });
}

test.describe('memory product', () => {
  test.describe.configure({ mode: 'serial' });

  test('browses owner memory with session-only search and inert content', async ({ page }) => {
    const memory = {
      id: 'memory-family', scope: { type: 'user' }, status: 'active', pinned: false, version: 1,
      currentRevision: { id: 'revision-family', memoryId: 'memory-family', revision: 1, kind: 'preference', content: '<not-an-instruction>', correctionReason: null, confidence: .95, importance: .7, halfLifeDays: 180, observedAt: '2026-10-08T09:00:00Z', validFrom: null, validTo: null, createdAt: '2026-10-08T09:00:00Z' },
      reinforcedAt: null, dormantAt: null, archivedAt: null, createdAt: '2026-10-08T09:00:00Z', updatedAt: '2026-10-08T09:00:00Z',
    };
    await page.route('**/api/v1/**', async (route) => {
      const url = new URL(route.request().url());
      if (url.pathname.endsWith('/auth/session')) return route.fulfill({ json: { principal: { issuer: 'https://authentik.test', subject: 'fixture-owner', displayName: 'Fixture Owner' }, csrfToken: 'fixture-csrf', idleExpiresAt: '2099-10-08T10:00:00Z', absoluteExpiresAt: '2099-10-09T10:00:00Z' } });
      if (url.pathname.endsWith('/models')) return route.fulfill({ json: { models: [{ id: 'fixture-model', displayName: 'Fixture model', provider: 'fixture', capabilities: ['chat'], availability: 'available', selectable: true, disabledReason: null }], defaultModelId: 'fixture-model' } });
      if (url.pathname.endsWith('/conversations')) return route.fulfill({ json: { items: [], nextCursor: null } });
      if (url.pathname.endsWith('/agents')) return route.fulfill({ json: { items: [] } });
      if (url.pathname.endsWith('/memories')) return route.fulfill({ json: { items: [memory], nextCursor: null } });
      if (url.pathname.endsWith('/memories/memory-family')) return route.fulfill({ json: { ...memory, revisions: [memory.currentRevision], provenance: [], embeddingGenerations: [], embeddings: [], relations: [] } });
      return route.fulfill({ status: 404, json: { detail: 'fixture endpoint not implemented' } });
    });

    await page.goto('/memory');
    await expect(page.getByRole('heading', { name: 'Memory', exact: true })).toBeVisible();
    await expect(page.getByText('<not-an-instruction>', { exact: true })).toBeVisible();
    // The application bundles scripts; the user-authored memory must never
    // become executable markup or inline script content.
    expect(await page.locator('script').filter({ hasText: '<not-an-instruction>' }).count()).toBe(0);
    const search = page.locator('input[type="search"][placeholder="Search memory"]');
    await search.fill('family');
    await expect(search).toHaveValue('family');
    expect(page.url()).not.toContain('family');
    // Search results are intentionally session-only. Navigate to the stable
    // record URL for the detail assertion instead of racing the debounced
    // list reload after clearing the query.
    await page.goto('/memory/memory-family');
    await expect(page.getByRole('heading', { name: '<not-an-instruction>', exact: true })).toBeVisible();
    await expect(page.getByRole('link', { name: 'Review queue' })).toBeVisible();
  });

  test('preserves an agent scope when opening a stable detail link directly', async ({ page }) => {
    const memory = {
      id: 'memory-agent', scope: { type: 'agent', agentProfileId: 'agent-research' }, status: 'active', pinned: false, version: 1,
      currentRevision: { id: 'revision-agent', memoryId: 'memory-agent', revision: 1, kind: 'semantic', content: 'Private project context', correctionReason: null, confidence: .91, importance: .8, halfLifeDays: 90, observedAt: '2026-10-08T09:00:00Z', validFrom: null, validTo: null, createdAt: '2026-10-08T09:00:00Z' },
      reinforcedAt: null, dormantAt: null, archivedAt: null, createdAt: '2026-10-08T09:00:00Z', updatedAt: '2026-10-08T09:00:00Z',
    };
    let detailScope = '';
    await page.route('**/api/v1/**', async (route) => {
      const url = new URL(route.request().url());
      if (url.pathname.endsWith('/auth/session')) return route.fulfill({ json: { principal: { issuer: 'https://authentik.test', subject: 'fixture-owner', displayName: 'Fixture Owner' }, csrfToken: 'fixture-csrf', idleExpiresAt: '2099-10-08T10:00:00Z', absoluteExpiresAt: '2099-10-09T10:00:00Z' } });
      if (url.pathname.endsWith('/agents')) return route.fulfill({ json: { items: [{ id: 'agent-research', status: 'active', version: 1, currentRevision: { id: 'agent-revision', profileId: 'agent-research', revision: 1, displayName: 'Researcher', purpose: 'Private research', instructions: 'Fixture', personaRevisionId: 'persona-1', promptBundleRevisionId: 'prompt-1', modelPolicyRevisionId: 'model-1', createdAt: '2026-10-08T09:00:00Z' }, createdAt: '2026-10-08T09:00:00Z', updatedAt: '2026-10-08T09:00:00Z' }] } });
      if (url.pathname.endsWith('/memories')) return route.fulfill({ json: { items: [memory], nextCursor: null } });
      if (url.pathname.endsWith('/memories/memory-agent')) { detailScope = url.search; return route.fulfill({ json: { ...memory, revisions: [memory.currentRevision], provenance: [], embeddingGenerations: [], embeddings: [], relations: [] } }); }
      return route.fulfill({ status: 404, json: { detail: 'fixture endpoint not implemented' } });
    });
    await page.goto('/memory/memory-agent?scopeType=agent&agentProfileId=agent-research');
    await expect(page.getByRole('heading', { name: 'Private project context', exact: true })).toBeVisible();
    expect(detailScope).toContain('scopeType=agent');
    expect(detailScope).toContain('agentProfileId=agent-research');
    await expect(page.getByText('Private to agent agent-research', { exact: true })).toBeVisible();
  });

  test('exposes independent indicators and exact popup actions accessibly', async ({ page }) => {
    const now = '2026-10-08T09:00:00Z';
    const run = { id: 'run-memory-popup', conversationId: 'conversation-memory-popup', userMessageId: 'user-memory-popup', assistantMessageId: 'assistant-memory-popup', status: 'completed', agentRevisionId: 'agent-revision', modelPolicyRevisionId: 'model-policy', provider: 'fixture', modelId: 'fixture-model', retryOfRunId: null, createdAt: now, startedAt: now, finishedAt: now, error: null };
    const summary = { id: 'conversation-memory-popup', title: 'Memory popup', agentProfileId: 'agent-profile', agentRevisionId: 'agent-revision', modelId: 'fixture-model', version: 1, createdAt: now, updatedAt: now, currentRun: null };
    const detail = { ...summary, messages: [{ id: 'user-memory-popup', conversationId: summary.id, role: 'user', content: 'Remember this', state: 'complete', runId: run.id, createdAt: now, updatedAt: now }, { id: 'assistant-memory-popup', conversationId: summary.id, role: 'assistant', content: 'I will use that context.', state: 'complete', runId: run.id, createdAt: now, updatedAt: now }], recentRuns: [run], currentRun: null };
    await page.route('**/api/v1/**', async (route) => {
      const url = new URL(route.request().url());
      if (url.pathname.endsWith('/auth/session')) return route.fulfill({ json: { principal: { issuer: 'https://authentik.test', subject: 'fixture-owner', displayName: 'Fixture Owner' }, csrfToken: 'fixture-csrf', idleExpiresAt: '2099-10-08T10:00:00Z', absoluteExpiresAt: '2099-10-09T10:00:00Z' } });
      if (url.pathname.endsWith('/models')) return route.fulfill({ json: { models: [{ id: 'fixture-model', displayName: 'Fixture model', provider: 'fixture', capabilities: ['chat'], availability: 'available', selectable: true, disabledReason: null }], defaultModelId: 'fixture-model', observedAt: now } });
      if (url.pathname.endsWith('/agents')) return route.fulfill({ json: { items: [] } });
      if (url.pathname.endsWith('/conversations')) return route.fulfill({ json: { items: [summary], nextCursor: null } });
      if (url.pathname.endsWith('/conversations/conversation-memory-popup')) return route.fulfill({ json: detail });
      if (url.pathname.endsWith('/runs/run-memory-popup/memory-activity')) return route.fulfill({ json: { runId: run.id, processingStatus: 'settled', lastEventId: null, reconciledAt: now, items: [{ id: 'activity-recalled', action: 'recalled', status: 'completed', scope: { type: 'user' }, candidateId: null, memoryId: 'memory-popup', memoryRevisionId: 'revision-popup', policyRevisionId: null, embeddingGenerationId: null, reconciliationStatus: 'authoritative', occurredAt: now }, { id: 'activity-candidate', action: 'queued_for_review', status: 'completed', scope: { type: 'user' }, candidateId: 'candidate-popup', memoryId: null, memoryRevisionId: null, policyRevisionId: null, embeddingGenerationId: null, reconciliationStatus: 'authoritative', occurredAt: now }, { id: 'activity-processing', action: 'queued_for_review', status: 'queued', scope: { type: 'user' }, candidateId: null, memoryId: null, memoryRevisionId: null, policyRevisionId: null, embeddingGenerationId: null, reconciliationStatus: 'pending', occurredAt: now }] } });
      if (url.pathname.endsWith('/memories/memory-popup')) return route.fulfill({ json: { id: 'memory-popup', scope: { type: 'user' }, status: 'active', pinned: false, version: 1, currentRevision: { id: 'revision-popup', memoryId: 'memory-popup', revision: 1, kind: 'semantic', content: 'Bounded popup record', correctionReason: null, confidence: .9, importance: .8, halfLifeDays: 30, observedAt: now, validFrom: null, validTo: null, createdAt: now }, reinforcedAt: null, dormantAt: null, archivedAt: null, createdAt: now, updatedAt: now, provenance: [{ id: 'provenance-popup', memoryRevisionId: 'revision-popup', type: 'run', sourceId: run.id, sourceContentDigest: 'digest', evidence: null, observedAt: now, createdAt: now }], revisions: [], embeddingGenerations: [], embeddings: [], relations: [] } });
      if (url.pathname.endsWith('/memory-candidates/candidate-popup')) return route.fulfill({ json: { id: 'candidate-popup', jobId: 'job-popup', runId: run.id, version: 1, action: 'create', state: 'review', content: 'Candidate context', kind: 'semantic', scope: { type: 'user' }, confidence: .7, importance: .5, halfLifeDays: 7, validTo: null, sensitivity: 'ordinary', relatedMemoryId: null, memoryId: null, decisionReason: null, createdAt: now, decidedAt: null, groundedMessageIds: ['user-memory-popup'] } });
      return route.fulfill({ status: 404, json: { detail: 'fixture endpoint not implemented' } });
    });
    await page.goto('/conversation/conversation-memory-popup');
    await expect(page.getByText('I will use that context.', { exact: true })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Memory recalled for this answer' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Memory created or updated from this turn' })).toBeVisible();
    const recall = page.getByRole('button', { name: 'Memory recalled for this answer' });
    await recall.click();
    const popup = page.getByRole('dialog', { name: 'Memory activity' });
    await expect(popup).toBeVisible();
    await expect(popup).toHaveAttribute('aria-modal', 'false');
    await expect(popup).toHaveAttribute('aria-labelledby', /memory-popup-title-/);
    await expect(popup.getByText('run', { exact: true })).toBeVisible();
    await expect(popup.getByText('Queued for review', { exact: true })).toBeVisible();
    await expect(popup.getByText(/Candidate run run-memory-popup · 1 grounded message\(s\)/)).toBeVisible();
    await expect(popup.getByText('Processing memory', { exact: true })).toBeVisible();
    await expect(popup.getByText('Memory processing is still in progress', { exact: true })).toBeVisible();
    await expect(popup.getByRole('link', { name: 'Inspect record' })).toHaveAttribute('href', /\/memory\/memory-popup/);
    await expect(popup.getByRole('link', { name: 'Correct' })).toHaveAttribute('href', /action=correct/);
    await expect(popup.getByRole('link', { name: 'Disable' })).toHaveAttribute('href', /action=disable/);
    await expect(popup.getByRole('link', { name: 'Review candidate' })).toHaveAttribute('href', /candidateId=candidate-popup/);
    await page.setViewportSize({ width: 375, height: 800 });
    const box = await popup.boundingBox();
    expect(box?.width ?? 999).toBeLessThanOrEqual(375);
    await popup.getByRole('button', { name: 'Close memory activity' }).click();
    await expect.poll(async () => page.evaluate(() => document.activeElement?.getAttribute('aria-label'))).toBe('Memory recalled for this answer');
    await recall.press('Enter');
    await expect(page.getByRole('dialog', { name: 'Memory activity' })).toBeVisible();
  });

  test('guides first-run setup and saves only after explicit owner confirmation', async ({ page }) => {
    let configurationGets = 0;
    let savedBody: Record<string, unknown> | null = null;
    await mockShellApi(page, async (route, url) => {
      if (url.pathname.endsWith('/memory-model-inventory')) return route.fulfill({ json: inventory });
      if (url.pathname.endsWith('/memory-model-configuration')) {
        if (route.request().method() === 'GET') {
        configurationGets += 1;
        return route.fulfill({ status: 404, json: { detail: 'Memory model configuration has not been created.' } });
        }
        savedBody = route.request().postDataJSON() as Record<string, unknown>;
        return route.fulfill({ json: configured(1) });
      }
      if (url.pathname.endsWith('/memory-reindex')) return route.fulfill({ json: idleReindex });
      return route.fulfill({ status: 404, json: { detail: 'fixture endpoint not implemented' } });
    });

    await page.goto('/memory/settings');
    await expect(page.getByRole('heading', { name: /memory/i }).first()).toBeVisible();
    await expect(page.getByText(/automatic memory|set up memory|first use/i).first()).toBeVisible();
    await expect(page.getByRole('alert')).toHaveCount(0);
    const firstRunA11y = await new AxeBuilder({ page }).include('.settings-page').analyze();
    expect(firstRunA11y.violations).toEqual([]);

    const extraction = page.getByRole('combobox', { name: /memory extraction|extraction model/i });
    const embedding = page.getByRole('combobox', { name: /memory embeddings|embedding model/i });
    await expect(extraction).toHaveValue('qwen3:8b');
    await expect(embedding).toHaveValue('qwen3-embedding:4b');
    await expect(extraction.locator('option:checked')).toHaveText(/Recommended/);
    await expect(embedding.locator('option:checked')).toHaveText(/Recommended/);
    expect(configurationGets).toBeGreaterThan(0);
    expect(savedBody).toBeNull();

    // A forged submit must remain side-effect free until the owner confirms
    // that completed turns may be processed for durable memories.
    await page.locator('form.setup-form').evaluate((form) => (form as HTMLFormElement).requestSubmit());
    await expect.poll(() => savedBody).toBeNull();

    const confirmation = page.getByRole('checkbox', { name: /I understand Aura will process/i });
    await confirmation.check();
    await page.getByRole('button', { name: /save|enable|configure/i }).last().click();
    await expect.poll(() => savedBody).toMatchObject({ extractionModelId: 'qwen3:8b', embeddingModelId: 'qwen3-embedding:4b', expectedVersion: 1 });
    await expect(page.getByText(/saved|configured|ready/i).first()).toBeVisible();
  });

  test('hydrates configured selections and retries an independent reindex failure', async ({ page }) => {
    let reindexAttempts = 0;
    await mockShellApi(page, async (route, url) => {
      if (url.pathname.endsWith('/memory-model-inventory')) return route.fulfill({ json: inventory });
      if (url.pathname.endsWith('/memory-model-configuration')) return route.fulfill({ json: configured(3) });
      if (!url.pathname.endsWith('/memory-reindex')) return route.fulfill({ status: 404, json: { detail: 'fixture endpoint not implemented' } });
      reindexAttempts += 1;
      return reindexAttempts === 1
        ? route.fulfill({ status: 503, json: { detail: 'Reindex status temporarily unavailable.' } })
        : route.fulfill({ json: idleReindex });
    });

    await page.goto('/memory/settings');
    await expect(page.getByRole('combobox', { name: /memory extraction|extraction model/i })).toHaveValue('qwen3:8b');
    await expect(page.getByRole('combobox', { name: /memory embeddings|embedding model/i })).toHaveValue('qwen3-embedding:4b');
    await expect(page.getByRole('button', { name: /retry/i })).toBeVisible();
    await page.getByRole('button', { name: /retry/i }).click();
    await expect(page.getByText(/no reindex|idle/i).first()).toBeVisible();
    expect(reindexAttempts).toBe(2);
  });

  test('uses disclosed advanced filters and contextual detail on narrow screens', async ({ page }) => {
    const memory = {
      id: 'memory-responsive', scope: { type: 'user' }, status: 'active', pinned: false, version: 1,
      currentRevision: { id: 'revision-responsive', memoryId: 'memory-responsive', revision: 1, kind: 'preference', content: 'AURA-0044 responsive fixture', correctionReason: null, confidence: .95, importance: .7, halfLifeDays: 180, observedAt: '2026-10-08T09:00:00Z', validFrom: null, validTo: null, createdAt: '2026-10-08T09:00:00Z' },
      reinforcedAt: null, dormantAt: null, archivedAt: null, createdAt: '2026-10-08T09:00:00Z', updatedAt: '2026-10-08T09:00:00Z',
    };
    await mockShellApi(page, async (route, url) => {
      if (url.pathname.endsWith('/memories')) return route.fulfill({ json: { items: [memory], nextCursor: null } });
      if (url.pathname.endsWith('/memories/memory-responsive')) return route.fulfill({ json: { ...memory, revisions: [memory.currentRevision], provenance: [], embeddingGenerations: [], embeddings: [], relations: [] } });
      return route.fulfill({ status: 404, json: { detail: 'fixture endpoint not implemented' } });
    });

    await page.goto('/memory');
    const advanced = page.getByRole('button', { name: /advanced filters/i });
    await expect(advanced).toBeVisible();
    await advanced.click();
    await expect(page.getByLabel(/provenance/i)).toBeVisible();
    await page.evaluate(() => { document.documentElement.dataset['theme'] = 'dark'; });
    const darkRecordsA11y = await new AxeBuilder({ page }).include('.records-page').analyze();
    expect(darkRecordsA11y.violations).toEqual([]);
    await advanced.press('Escape');
    await expect(advanced).toBeFocused();

    await page.setViewportSize({ width: 375, height: 800 });
    await page.getByRole('button', { name: /AURA-0044 responsive fixture/ }).click();
    await expect(page.getByRole('heading', { name: 'AURA-0044 responsive fixture', exact: true })).toBeVisible();
    await expect(page.getByText('Select a memory', { exact: true })).not.toBeVisible();
    const mobileRecordsA11y = await new AxeBuilder({ page }).include('.records-page').analyze();
    expect(mobileRecordsA11y.violations).toEqual([]);
  });

  test('keeps review decisions and settings controls keyboard-accessible', async ({ page }) => {
    const candidate = {
      id: 'candidate-keyboard', jobId: 'job-keyboard', runId: 'run-keyboard', version: 1, action: 'create', state: 'review',
      content: 'Keyboard review fixture', kind: 'preference', scope: { type: 'user' }, confidence: .7, importance: .5, halfLifeDays: 30,
      validTo: null, sensitivity: 'ordinary', relatedMemoryId: null, memoryId: null, decisionReason: null, createdAt: '2026-10-08T09:00:00Z', decidedAt: null,
    };
    await mockShellApi(page, async (route, url) => {
      if (url.pathname.endsWith('/memory-candidates/candidate-keyboard')) return route.fulfill({ json: { ...candidate, groundedMessageIds: ['message-keyboard'] } });
      if (url.pathname.endsWith('/memory-candidates')) return route.fulfill({ json: { items: [candidate], nextCursor: null } });
      return route.fulfill({ status: 404, json: { detail: 'fixture endpoint not implemented' } });
    });
    await page.goto('/memory/candidates');
    await expect(page.getByRole('heading', { name: 'Review queue', exact: true })).toBeVisible();
    const candidateButton = page.getByRole('button', { name: /Keyboard review fixture/ });
    await candidateButton.focus();
    await candidateButton.press('Enter');
    await expect(page.getByRole('heading', { name: 'Keyboard review fixture', exact: true })).toBeVisible();
    await expect(page.getByRole('button', { name: /approve memory/i })).toBeEnabled();
    await expect(page.getByRole('button', { name: /edit and approve/i })).toBeEnabled();
    const reviewA11y = await new AxeBuilder({ page }).include('.memory-page').analyze();
    expect(reviewA11y.violations).toEqual([]);
  });

  test('makes an incomplete provider-review candidate editable with safe approval defaults', async ({ page }) => {
    let approvalBody: Record<string, unknown> | null = null;
    const candidate = {
      id: 'candidate-legacy', jobId: 'job-legacy', runId: 'run-legacy', version: 3, action: 'review', state: 'review',
      content: 'The owner prefers quiet mornings.', kind: null, scope: null, confidence: .88, importance: null, halfLifeDays: null,
      validTo: null, sensitivity: 'ordinary', relatedMemoryId: null, memoryId: null, decisionReason: 'provider_requested_review', createdAt: '2026-10-08T09:00:00Z', decidedAt: null,
    };
    await mockShellApi(page, async (route, url) => {
      if (url.pathname.endsWith('/memory-candidates/candidate-legacy/approve')) {
        approvalBody = route.request().postDataJSON() as Record<string, unknown>;
        return route.fulfill({ json: { candidate: { ...candidate, action: 'create', state: 'accepted', version: 4, kind: 'semantic', scope: { type: 'user' }, importance: .5, halfLifeDays: 30, groundedMessageIds: ['message-legacy'] }, activityId: 'activity-legacy' } });
      }
      if (url.pathname.endsWith('/memory-candidates/candidate-legacy')) return route.fulfill({ json: { ...candidate, groundedMessageIds: ['message-legacy'] } });
      if (url.pathname.endsWith('/memory-candidates')) return route.fulfill({ json: { items: [candidate], nextCursor: null } });
      return route.fulfill({ status: 404, json: { detail: 'fixture endpoint not implemented' } });
    });

    await page.setViewportSize({ width: 375, height: 800 });
    await page.goto('/memory/candidates');
    const detailResponse = page.waitForResponse((response) => response.ok() && response.url().includes('/memory-candidates/candidate-legacy') && !response.url().endsWith('/approve'));
    await page.getByRole('button', { name: /owner prefers quiet mornings/i }).click();
    const detail = await detailResponse;
    expect(await detail.json()).toMatchObject({ id: 'candidate-legacy', action: 'review', content: 'The owner prefers quiet mornings.' });
    await expect(page.getByRole('heading', { name: /owner prefers quiet mornings/i })).toBeVisible();
    await expect(page.getByRole('button', { name: /edit and approve/i })).toBeVisible();
    await page.getByRole('button', { name: /edit and approve/i }).click();
    await expect(page.getByRole('combobox', { name: 'Approval action' })).toHaveValue('create');
    await expect(page.getByRole('combobox', { name: 'Memory kind' })).toHaveValue('semantic');
    await expect(page.getByRole('combobox', { name: 'Memory scope' })).toHaveValue('user');
    await expect(page.getByRole('spinbutton', { name: 'Memory importance' })).toHaveValue('0.5');
    await expect(page.getByRole('spinbutton', { name: 'Memory half-life' })).toHaveValue('30');
    await expect(page.getByRole('combobox', { name: 'Approval action' }).locator('option')).toHaveCount(4);
    await expect(page.getByText(/optional; leave blank for no expiry/i)).toBeVisible();
    await page.getByRole('button', { name: /approve edited candidate/i }).click();
    await expect.poll(() => approvalBody).toMatchObject({ expectedVersion: 3, edit: { action: 'create', kind: 'semantic', scope: { type: 'user' }, importance: .5, halfLifeDays: 30, validTo: null } });
  });

  test('hydrates and attaches an explicit off recall policy accessibly', async ({ page }) => {
    let createdBody: Record<string, unknown> | null = null;
    let attachBody: Record<string, unknown> | null = null;
    const initialPolicy = {
      id: 'policy-automatic', agentProfileId: 'agent-task', revision: 1,
      sharedUserRead: true, currentAgentRead: true, sharedUserPromotion: false,
      fallbackRelevanceThreshold: .7, maxMemories: 2, contextBudgetFraction: .05,
      fallbackAgentProfileIds: [], recallMode: 'automatic', automaticRecallThreshold: .7,
      createdAt: '2026-10-08T09:00:00Z',
    };
    const offPolicy = { ...initialPolicy, id: 'policy-off', revision: 2, recallMode: 'off' as const };
    await page.route('**/api/v1/**', async (route) => {
      const url = new URL(route.request().url());
      if (url.pathname.endsWith('/auth/session')) return route.fulfill({ json: { principal: { issuer: 'https://authentik.test', subject: 'fixture-owner', displayName: 'Fixture Owner' }, csrfToken: 'fixture-csrf', idleExpiresAt: '2099-10-08T10:00:00Z', absoluteExpiresAt: '2099-10-09T10:00:00Z' } });
      if (url.pathname.endsWith('/models')) return route.fulfill({ json: { models: [{ id: 'fixture-model', displayName: 'Fixture model', provider: 'fixture', capabilities: ['chat'], availability: 'available', selectable: true, disabledReason: null }], defaultModelId: 'fixture-model' } });
      if (url.pathname.endsWith('/conversations')) return route.fulfill({ json: { items: [], nextCursor: null } });
      if (url.pathname.endsWith('/agents')) return route.fulfill({ json: { items: [{ id: 'agent-task', status: 'active', version: 3, currentRevision: { id: 'agent-revision-task', profileId: 'agent-task', revision: 1, displayName: 'Task agent', purpose: 'Focused work', instructions: 'Stay focused', personaRevisionId: 'persona-task', promptBundleRevisionId: 'prompt-task', modelPolicyRevisionId: 'model-task', createdAt: '2026-10-08T09:00:00Z' }, createdAt: '2026-10-08T09:00:00Z', updatedAt: '2026-10-08T09:00:00Z' }] } });
      if (url.pathname.endsWith('/agents/agent-task/memory-policies')) {
        if (route.request().method() === 'POST') {
          createdBody = route.request().postDataJSON() as Record<string, unknown>;
          return route.fulfill({ json: offPolicy });
        }
        return route.fulfill({ json: { items: [initialPolicy], attachedPolicyRevisionId: initialPolicy.id, agentVersion: 3 } });
      }
      if (url.pathname.endsWith('/agents/agent-task/memory-policies/policy-off/attach')) {
        attachBody = route.request().postDataJSON() as Record<string, unknown>;
        return route.fulfill({ json: { id: 'agent-task', version: 4, currentRevision: { id: 'agent-revision-task-2' } } });
      }
      return route.fulfill({ status: 404, json: { detail: 'fixture endpoint not implemented' } });
    });

    await page.goto('/agents/agent-task/memory');
    await expect(page.getByRole('heading', { name: 'Memory policy', exact: true })).toBeVisible();
    await expect(page.locator('input[name="recallMode"][value="automatic"]')).toBeChecked();
    await expect(page.getByRole('spinbutton', { name: /automatic recall threshold/i })).toHaveValue('0.7');
    await page.locator('input[name="recallMode"][value="off"]').check();
    await expect(page.getByRole('status')).toContainText('Recall is off for this agent');

    await page.getByRole('button', { name: 'Create immutable revision' }).click();
    await expect.poll(() => createdBody).toMatchObject({
      recallMode: 'off', automaticRecallThreshold: .7, maxMemories: 2,
      contextBudgetFraction: .05, expectedRevision: 1,
    });
    await expect(page.getByText('Revision 2', { exact: true })).toBeVisible();
    await page.getByRole('button', { name: 'Attach revision' }).click();
    await expect.poll(() => attachBody).toMatchObject({ expectedAgentVersion: 3 });
    const a11y = await new AxeBuilder({ page }).include('.memory-page').analyze();
    expect(a11y.violations).toEqual([]);
  });
});
