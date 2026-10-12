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
    await expect(page.getByRole('heading', { name: 'What Aura knows', exact: true })).toBeVisible();
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
    let statusCalls = 0;
    await page.route('**/api/v1/**', async (route) => {
      const url = new URL(route.request().url());
      if (url.pathname.endsWith('/auth/session')) return route.fulfill({ json: { principal: { issuer: 'https://authentik.test', subject: 'fixture-owner', displayName: 'Fixture Owner' }, csrfToken: 'fixture-csrf', idleExpiresAt: '2099-10-08T10:00:00Z', absoluteExpiresAt: '2099-10-09T10:00:00Z' } });
      if (url.pathname.endsWith('/agents')) return route.fulfill({ json: { items: [{ id: 'agent-research', status: 'active', version: 1, currentRevision: { id: 'agent-revision', profileId: 'agent-research', revision: 1, displayName: 'Researcher', purpose: 'Private research', instructions: 'Fixture', personaRevisionId: 'persona-1', promptBundleRevisionId: 'prompt-1', modelPolicyRevisionId: 'model-1', createdAt: '2026-10-08T09:00:00Z' }, createdAt: '2026-10-08T09:00:00Z', updatedAt: '2026-10-08T09:00:00Z' }] } });
      if (url.pathname.endsWith('/memories')) return route.fulfill({ json: { items: [memory], nextCursor: null } });
      if (url.pathname.endsWith('/memories/memory-agent')) { if (route.request().method() !== 'GET') statusCalls += 1; detailScope = url.search; return route.fulfill({ json: { ...memory, revisions: [memory.currentRevision], provenance: [], embeddingGenerations: [], embeddings: [], relations: [] } }); }
      return route.fulfill({ status: 404, json: { detail: 'fixture endpoint not implemented' } });
    });
    await page.goto('/memory/memory-agent?scopeType=agent&agentProfileId=agent-research');
    await expect(page.getByRole('heading', { name: 'Private project context', exact: true })).toBeVisible();
    expect(detailScope).toContain('scopeType=agent');
    expect(detailScope).toContain('agentProfileId=agent-research');
    await expect(page.locator('.detail-header').getByText('Private to agent agent-research', { exact: true })).toBeVisible();
    await page.goto('/memory/memory-agent?scopeType=agent&agentProfileId=agent-research&action=disable');
    await expect(page.getByRole('alertdialog', { name: 'Disable this memory?' })).toBeVisible();
    expect(statusCalls).toBe(0);
    await page.keyboard.press('Escape');
    await expect(page.getByRole('alertdialog', { name: 'Disable this memory?' })).toHaveCount(0);
    expect(statusCalls).toBe(0);
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
      if (url.pathname.endsWith('/runs/run-memory-popup/memory-activity')) return route.fulfill({ json: { runId: run.id, processingStatus: 'settled', lastEventId: null, reconciledAt: now, items: [{ id: 'activity-recalled', action: 'recalled', status: 'completed', scope: { type: 'user' }, candidateId: null, memoryId: 'memory-popup', memoryRevisionId: 'revision-popup', policyRevisionId: null, embeddingGenerationId: null, reconciliationStatus: 'authoritative', occurredAt: now }, { id: 'activity-candidate', action: 'queued_for_review', status: 'completed', scope: { type: 'user' }, candidateId: 'candidate-popup', memoryId: null, memoryRevisionId: null, policyRevisionId: null, embeddingGenerationId: null, reconciliationStatus: 'authoritative', occurredAt: now }, { id: 'activity-processing', action: 'queued_for_review', status: 'queued', scope: { type: 'user' }, candidateId: 'candidate-processing', memoryId: null, memoryRevisionId: null, policyRevisionId: null, embeddingGenerationId: null, reconciliationStatus: 'pending', occurredAt: now }] } });
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
    await expect(popup.locator('a[href*="candidateId=candidate-popup"]', { hasText: 'Review candidate' })).toHaveCount(1);
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

  test('clears first-use model selections and confirmation after an authorized session reset during settings recovery', async ({ page }) => {
    let reindexReads = 0;
    await mockShellApi(page, async (route, url) => {
      if (url.pathname.endsWith('/memory-model-inventory')) return route.fulfill({ json: inventory });
      if (url.pathname.endsWith('/memory-model-configuration')) {
        if (route.request().method() === 'GET') return route.fulfill({ status: 404, json: { detail: 'Memory model configuration has not been created.' } });
        return route.fulfill({ json: configured(1) });
      }
      if (url.pathname.endsWith('/memory-reindex')) {
        reindexReads += 1;
        return reindexReads === 1 ? route.fulfill({ json: idleReindex }) : route.fulfill({ status: 401, json: { detail: '<private reindex body>' } });
      }
      return route.fulfill({ status: 404, json: { detail: 'fixture endpoint not implemented' } });
    });

    await page.goto('/memory/settings');
    await expect(page.getByRole('heading', { name: 'Turn on automatic memory', exact: true })).toBeVisible();
    const extraction = page.getByRole('combobox', { name: /memory extraction/i });
    const embedding = page.getByRole('combobox', { name: /memory embeddings/i });
    await extraction.selectOption('qwen3:8b');
    await embedding.selectOption('qwen3-embedding:4b');
    const confirmation = page.getByRole('checkbox', { name: /I understand Aura will process/i });
    await confirmation.check();
    await expect(extraction).toHaveValue('qwen3:8b');
    await expect(embedding).toHaveValue('qwen3-embedding:4b');
    await expect(confirmation).toBeChecked();

    await page.getByRole('button', { name: 'Save and enable memory' }).click();
    await expect(page.getByRole('alert')).toContainText(/session has expired/i);
    await expect(page.getByRole('heading', { name: 'Turn on automatic memory', exact: true })).toHaveCount(0);
    await expect(page.getByRole('combobox')).toHaveCount(0);
    await expect(page.getByRole('checkbox', { name: /I understand Aura will process/i })).toHaveCount(0);
    await expect(page.getByRole('alert').getByRole('button', { name: 'Retry settings' })).toBeVisible();
    await expect(page.getByText(/private reindex body/i)).toHaveCount(0);
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

  test('resets private memory state and loading controls on record, queue, and settings auth failures', async ({ page }) => {
    const memory = {
      id: 'memory-auth', scope: { type: 'user' }, status: 'active', pinned: false, version: 1,
      currentRevision: { id: 'memory-auth-revision', memoryId: 'memory-auth', revision: 1, kind: 'semantic', content: 'Private auth fixture', correctionReason: null, confidence: .9, importance: .5, halfLifeDays: 30, observedAt: '2026-10-08T09:00:00Z', validFrom: null, validTo: null, createdAt: '2026-10-08T09:00:00Z' },
      reinforcedAt: null, dormantAt: null, archivedAt: null, createdAt: '2026-10-08T09:00:00Z', updatedAt: '2026-10-08T09:00:00Z',
    };
    const candidate = { id: 'candidate-auth', jobId: 'job-auth', runId: 'run-auth', version: 1, action: 'create', state: 'review', content: 'Private candidate fixture', kind: 'semantic', scope: { type: 'user' }, confidence: .8, importance: .5, halfLifeDays: 30, validTo: null, sensitivity: 'ordinary', relatedMemoryId: null, memoryId: null, decisionReason: null, createdAt: '2026-10-08T09:00:00Z', decidedAt: null };
    await mockShellApi(page, async (route, url) => {
      if (url.pathname.endsWith('/memories')) return url.searchParams.has('cursor') ? route.fulfill({ status: 401, json: { detail: 'secret record error' } }) : route.fulfill({ json: { items: [memory], nextCursor: 'record-next' } });
      if (url.pathname.endsWith('/memory-candidates')) return url.searchParams.has('cursor') ? route.fulfill({ status: 403, json: { detail: '<secret queue error>' } }) : route.fulfill({ json: { items: [candidate], nextCursor: 'candidate-next' } });
      if (url.pathname.endsWith('/memory-model-inventory')) return route.fulfill({ status: 401, json: { detail: 'secret settings error' } });
      if (url.pathname.endsWith('/memory-model-configuration') || url.pathname.endsWith('/memory-reindex')) return route.fulfill({ status: 404, json: { detail: 'not configured' } });
      return route.fulfill({ status: 404, json: { detail: 'fixture endpoint not implemented' } });
    });

    await page.goto('/memory');
    await page.getByRole('button', { name: 'Create memory' }).click();
    await page.locator('textarea[name="createContent"]').fill('Draft must not cross a session reset.');
    await expect(page.locator('textarea[name="createContent"]')).toHaveValue('Draft must not cross a session reset.');
    await page.getByRole('button', { name: 'Load more' }).click();
    await expect(page.getByRole('alert')).toContainText(/session has expired/i);
    await expect(page.locator('textarea[name="createContent"]')).toHaveCount(0);
    await expect(page.getByRole('button', { name: 'Create and embed' })).toHaveCount(0);
    await expect(page.getByText('Private auth fixture', { exact: true })).toHaveCount(0);
    await expect(page.getByRole('button', { name: 'Load more' })).toHaveCount(0);

    await page.goto('/memory/candidates');
    await page.getByRole('button', { name: 'Load more candidates' }).click();
    await expect(page.getByRole('alert')).toContainText(/permission/i);
    await expect(page.getByText('Private candidate fixture', { exact: true })).toHaveCount(0);
    await expect(page.getByRole('button', { name: 'Load more candidates' })).toHaveCount(0);

    await page.goto('/memory/settings');
    await expect(page.getByRole('alert')).toContainText(/session has expired/i);
    await expect(page.locator('[aria-busy="true"]')).toHaveCount(0);
  });

  test('clears an in-flight candidate pagination request after a delayed permission failure', async ({ page }) => {
    const candidate = {
      id: 'candidate-delayed-auth', jobId: 'job-delayed-auth', runId: 'run-delayed-auth', version: 1, action: 'create', state: 'review',
      content: 'Delayed auth candidate', kind: 'preference', scope: { type: 'user' }, confidence: .8, importance: .5, halfLifeDays: 30,
      validTo: null, sensitivity: 'ordinary', relatedMemoryId: null, memoryId: null, decisionReason: null,
      createdAt: '2026-10-08T09:00:00Z', decidedAt: null,
    };
    let release!: () => void;
    await mockShellApi(page, async (route, url) => {
      if (url.pathname.endsWith('/memory-candidates')) {
        if (!url.searchParams.has('cursor')) return route.fulfill({ json: { items: [candidate], nextCursor: 'candidate-delayed-next' } });
        return new Promise<void>((resolve) => {
          release = () => { void route.fulfill({ status: 403, json: { detail: '<private queue body>' } }); resolve(); };
        });
      }
      return route.fulfill({ status: 404, json: { detail: 'fixture endpoint not implemented' } });
    });

    await page.goto('/memory/candidates');
    await page.getByRole('button', { name: 'Load more candidates' }).click();
    await expect.poll(() => typeof release).toBe('function');
    await expect(page.getByRole('button', { name: 'Loading…' })).toBeDisabled();
    release();
    await expect(page.getByRole('alert')).toContainText(/permission/i);
    await expect(page.getByText('Delayed auth candidate', { exact: true })).toHaveCount(0);
    await expect(page.getByRole('button', { name: 'Load more candidates' })).toHaveCount(0);
    await expect(page.locator('[aria-busy="true"]')).toHaveCount(0);
    await expect(page.getByText(/private queue body/i)).toHaveCount(0);
  });

  test('resets settings controls when configuration status loses authorization', async ({ page }) => {
    await mockShellApi(page, async (route, url) => {
      if (url.pathname.endsWith('/memory-model-inventory')) return route.fulfill({ json: inventory });
      if (url.pathname.endsWith('/memory-model-configuration')) return route.fulfill({ status: 403, json: { detail: '<private configuration body>' } });
      if (url.pathname.endsWith('/memory-reindex')) return route.fulfill({ json: idleReindex });
      return route.fulfill({ status: 404, json: { detail: 'fixture endpoint not implemented' } });
    });
    await page.goto('/memory/settings');
    await expect(page.getByRole('alert')).toContainText(/permission/i);
    await expect(page.locator('[aria-busy="true"]')).toHaveCount(0);
    await expect(page.getByRole('combobox')).toHaveCount(0);
    await expect(page.getByText(/private configuration body/i)).toHaveCount(0);
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

    await page.setViewportSize({ width: 768, height: 1024 });
    await expect(page.locator('.memory-detail-panel')).toHaveCSS('display', 'none');
    const tabletCollection = await page.locator('.memory-list-panel').boundingBox();
    expect(tabletCollection?.width ?? 0).toBeGreaterThan(400);
    await page.getByRole('button', { name: /AURA-0044 responsive fixture/ }).click();
    await expect(page.getByRole('heading', { name: 'AURA-0044 responsive fixture', exact: true })).toBeVisible();
    await expect(page.getByRole('link', { name: /Back to What Aura knows/ })).toBeVisible();
    expect(await page.locator('.memory-list-panel').evaluate((node) => getComputedStyle(node).display)).toBe('none');
    const tabletDetail = await page.locator('.memory-detail-panel.has-selection').boundingBox();
    expect(tabletDetail?.width ?? 0).toBeGreaterThan(500);

    await page.setViewportSize({ width: 375, height: 800 });
    await expect(page.getByRole('heading', { name: 'AURA-0044 responsive fixture', exact: true })).toBeVisible();
    await expect(page.getByRole('link', { name: /Back to What Aura knows/ })).toBeVisible();
    expect(await page.locator('.memory-list-panel').evaluate((node) => getComputedStyle(node).display)).toBe('none');
    await expect(page.getByText('Select a memory', { exact: true })).not.toBeVisible();
    const mobileRecordsA11y = await new AxeBuilder({ page }).include('.records-page').analyze();
    expect(mobileRecordsA11y.violations).toEqual([]);
  });

  test('does not publish an out-of-order detail response under a newer route', async ({ page }) => {
    const base = (id: string, content: string) => ({
      id, scope: { type: 'user' }, status: 'active', pinned: false, version: 1,
      currentRevision: { id: `${id}-revision`, memoryId: id, revision: 1, kind: 'semantic', content, correctionReason: null, confidence: .9, importance: .5, halfLifeDays: 30, observedAt: '2026-10-08T09:00:00Z', validFrom: null, validTo: null, createdAt: '2026-10-08T09:00:00Z' },
      reinforcedAt: null, dormantAt: null, archivedAt: null, createdAt: '2026-10-08T09:00:00Z', updatedAt: '2026-10-08T09:00:00Z',
    });
    const memoryA = base('memory-a', 'Older route response');
    const memoryB = base('memory-b', 'Current route response');
    let releaseA = (): void => undefined;
    let releaseB = (): void => undefined;
    let aStartedResolve = (): void => undefined;
    let bStartedResolve = (): void => undefined;
    const aStarted = new Promise<void>((resolve) => { aStartedResolve = resolve; });
    const bStarted = new Promise<void>((resolve) => { bStartedResolve = resolve; });
    await mockShellApi(page, async (route, url) => {
      if (url.pathname.endsWith('/memories')) return route.fulfill({ json: { items: [memoryA, memoryB], nextCursor: null } });
      if (url.pathname.endsWith('/memories/memory-a')) { aStartedResolve(); await new Promise<void>((resolve) => { releaseA = resolve; }); return route.fulfill({ json: { ...memoryA, revisions: [memoryA.currentRevision], provenance: [], embeddingGenerations: [], embeddings: [], relations: [] } }); }
      if (url.pathname.endsWith('/memories/memory-b')) { bStartedResolve(); await new Promise<void>((resolve) => { releaseB = resolve; }); return route.fulfill({ json: { ...memoryB, revisions: [memoryB.currentRevision], provenance: [], embeddingGenerations: [], embeddings: [], relations: [] } }); }
      return route.fulfill({ status: 404, json: { detail: 'fixture endpoint not implemented' } });
    });
    await page.goto('/memory');
    await page.getByRole('button', { name: /Older route response/ }).click();
    await aStarted;
    await page.getByRole('button', { name: /Current route response/ }).click();
    await bStarted;
    releaseB();
    await expect(page.getByRole('heading', { name: 'Current route response', exact: true })).toBeVisible();
    releaseA();
    await expect(page.getByRole('heading', { name: 'Current route response', exact: true })).toBeVisible();
    await expect(page.getByRole('heading', { name: 'Older route response', exact: true })).toHaveCount(0);
  });

  test('shows delayed direct memory detail failure and retries the same route', async ({ page }) => {
    const memory = {
      id: 'memory-direct-retry', scope: { type: 'user' }, status: 'active', pinned: false, version: 1,
      currentRevision: { id: 'memory-direct-retry-revision', memoryId: 'memory-direct-retry', revision: 1, kind: 'semantic', content: 'Recovered direct memory', correctionReason: null, confidence: .9, importance: .5, halfLifeDays: 30, observedAt: '2026-10-08T09:00:00Z', validFrom: null, validTo: null, createdAt: '2026-10-08T09:00:00Z' },
      reinforcedAt: null, dormantAt: null, archivedAt: null, createdAt: '2026-10-08T09:00:00Z', updatedAt: '2026-10-08T09:00:00Z',
    };
    let detailAttempts = 0;
    let releaseFirst!: () => void;
    await mockShellApi(page, async (route, url) => {
      if (url.pathname.endsWith('/memories')) return route.fulfill({ json: { items: [], nextCursor: null } });
      if (url.pathname.endsWith('/memories/memory-direct-retry')) {
        detailAttempts += 1;
        if (detailAttempts === 1) {
          await new Promise<void>((resolve) => { releaseFirst = resolve; });
          return route.fulfill({ status: 503, json: { detail: '<private direct detail body>' } });
        }
        return route.fulfill({ json: { ...memory, revisions: [memory.currentRevision], provenance: [], embeddingGenerations: [], embeddings: [], relations: [] } });
      }
      return route.fulfill({ status: 404, json: { detail: 'fixture endpoint not implemented' } });
    });

    await page.goto('/memory/memory-direct-retry');
    await expect.poll(() => detailAttempts).toBe(1);
    await expect(page.getByRole('heading', { name: 'Recovered direct memory', exact: true })).toHaveCount(0);
    releaseFirst();
    const detailAlert = page.getByRole('alert').filter({ hasText: 'Memory details are unavailable' });
    await expect(detailAlert).toBeVisible();
    await detailAlert.getByRole('button', { name: 'Retry details' }).click();
    await expect(page.getByRole('heading', { name: 'Recovered direct memory', exact: true })).toBeVisible();
    expect(detailAttempts).toBe(2);
    await expect(page.getByText(/private direct detail body/i)).toHaveCount(0);
  });

  test('shows delayed direct candidate failure and retries the same route', async ({ page }) => {
    const candidate = {
      id: 'candidate-direct-retry', jobId: 'job-direct-retry', runId: 'run-direct-retry', version: 1, action: 'create', state: 'review',
      content: 'Recovered direct candidate', kind: 'preference', scope: { type: 'user' }, confidence: .8, importance: .5, halfLifeDays: 30,
      validTo: null, sensitivity: 'ordinary', relatedMemoryId: null, memoryId: null, decisionReason: null,
      createdAt: '2026-10-08T09:00:00Z', decidedAt: null, groundedMessageIds: [],
    };
    let detailAttempts = 0;
    let releaseFirst!: () => void;
    await mockShellApi(page, async (route, url) => {
      if (url.pathname.endsWith('/memory-candidates') && route.request().method() === 'GET') return route.fulfill({ json: { items: [], nextCursor: null } });
      if (url.pathname.endsWith('/memory-candidates/candidate-direct-retry')) {
        detailAttempts += 1;
        if (detailAttempts === 1) {
          await new Promise<void>((resolve) => { releaseFirst = resolve; });
          return route.fulfill({ status: 503, json: { detail: '<private direct candidate body>' } });
        }
        return route.fulfill({ json: candidate });
      }
      return route.fulfill({ status: 404, json: { detail: 'fixture endpoint not implemented' } });
    });

    await page.goto('/memory/candidates?candidateId=candidate-direct-retry');
    await expect.poll(() => detailAttempts).toBe(1);
    await expect(page.getByRole('heading', { name: 'Recovered direct candidate', exact: true })).toHaveCount(0);
    releaseFirst();
    const candidateDetailAlert = page.getByRole('alert').filter({ hasText: 'Candidate details are unavailable' });
    await expect(candidateDetailAlert).toBeVisible();
    await candidateDetailAlert.getByRole('button', { name: 'Retry details' }).click();
    await expect(page.getByRole('heading', { name: 'Recovered direct candidate', exact: true })).toBeVisible();
    expect(detailAttempts).toBe(2);
    await expect(page.getByText(/private direct candidate body/i)).toHaveCount(0);
  });

  test('clears route detail on Back and ignores a late response after returning to the collection', async ({ page }, testInfo) => {
    test.skip(testInfo.project.name === 'desktop', 'The focused Back link is intentionally rendered only at narrow widths.');
    const memory = {
      id: 'memory-back', scope: { type: 'user' }, status: 'active', pinned: false, version: 1,
      currentRevision: { id: 'memory-back-revision', memoryId: 'memory-back', revision: 1, kind: 'semantic', content: 'Back navigation fixture', correctionReason: null, confidence: .9, importance: .5, halfLifeDays: 30, observedAt: '2026-10-08T09:00:00Z', validFrom: null, validTo: null, createdAt: '2026-10-08T09:00:00Z' },
      reinforcedAt: null, dormantAt: null, archivedAt: null, createdAt: '2026-10-08T09:00:00Z', updatedAt: '2026-10-08T09:00:00Z',
    };
    let detailRequests = 0;
    let releaseLate!: () => void;
    await mockShellApi(page, async (route, url) => {
      if (url.pathname.endsWith('/memories')) return route.fulfill({ json: { items: [memory], nextCursor: null } });
      if (url.pathname.endsWith('/memories/memory-back')) {
        detailRequests += 1;
        if (detailRequests === 2) {
          await new Promise<void>((resolve) => { releaseLate = resolve; });
        }
        return route.fulfill({ json: { ...memory, revisions: [memory.currentRevision], provenance: [], embeddingGenerations: [], embeddings: [], relations: [] } });
      }
      return route.fulfill({ status: 404, json: { detail: 'fixture endpoint not implemented' } });
    });

    await page.goto('/memory/memory-back');
    await expect(page.getByRole('heading', { name: 'Back navigation fixture', exact: true })).toBeVisible();
    await page.getByRole('link', { name: /Back to What Aura knows/ }).click();
    await expect(page.getByRole('heading', { name: 'What Aura knows', exact: true })).toBeVisible();
    await expect(page.getByRole('heading', { name: 'Back navigation fixture', exact: true })).toHaveCount(0);

    await page.goto('/memory/memory-back');
    await expect.poll(() => detailRequests).toBe(2);
    await page.goto('/memory');
    releaseLate();
    await expect(page.getByRole('heading', { name: 'What Aura knows', exact: true })).toBeVisible();
    await expect(page.getByRole('heading', { name: 'Back navigation fixture', exact: true })).toHaveCount(0);
  });

  test('does not publish a late lifecycle mutation under a newer route', async ({ page }, testInfo) => {
    test.skip(testInfo.project.name === 'desktop', 'The route transition uses the narrow-only Back link.');
    const base = (id: string, content: string) => ({
      id, scope: { type: 'user' }, status: 'active', pinned: false, version: 1,
      currentRevision: { id: `${id}-revision`, memoryId: id, revision: 1, kind: 'semantic', content, correctionReason: null, confidence: .9, importance: .5, halfLifeDays: 30, observedAt: '2026-10-08T09:00:00Z', validFrom: null, validTo: null, createdAt: '2026-10-08T09:00:00Z' },
      reinforcedAt: null, dormantAt: null, archivedAt: null, createdAt: '2026-10-08T09:00:00Z', updatedAt: '2026-10-08T09:00:00Z',
    });
    const memoryA = base('memory-mutation-a', 'Mutation started on A');
    const memoryB = base('memory-mutation-b', 'Current route B');
    let mutationStarted = false;
    let mutationMethod = '';
    let releaseMutation!: () => void;
    await mockShellApi(page, async (route, url) => {
      if (url.pathname.endsWith('/memories')) return route.fulfill({ json: { items: [memoryA, memoryB], nextCursor: null } });
      if (url.pathname === '/api/v1/memories/memory-mutation-a/status') {
        mutationStarted = true;
        mutationMethod = route.request().method();
        await new Promise<void>((resolve) => { releaseMutation = resolve; });
        return route.fulfill({ json: { ...memoryA, status: 'disabled', version: 2, revisions: [memoryA.currentRevision], provenance: [], embeddingGenerations: [], embeddings: [], relations: [] } });
      }
      if (url.pathname === '/api/v1/memories/memory-mutation-a') {
        return route.fulfill({ json: { ...memoryA, revisions: [memoryA.currentRevision], provenance: [], embeddingGenerations: [], embeddings: [], relations: [] } });
      }
      if (url.pathname.endsWith('/memories/memory-mutation-b')) return route.fulfill({ json: { ...memoryB, revisions: [memoryB.currentRevision], provenance: [], embeddingGenerations: [], embeddings: [], relations: [] } });
      return route.fulfill({ status: 404, json: { detail: 'fixture endpoint not implemented' } });
    });

    await page.goto('/memory/memory-mutation-a');
    await expect(page.getByRole('heading', { name: 'Mutation started on A', exact: true })).toBeVisible();
    await page.locator('summary').filter({ hasText: 'More actions' }).click();
    await page.getByRole('button', { name: 'Disable', exact: true }).click();
    await page.getByRole('button', { name: /Confirm disable/i }).click();
    await expect.poll(() => mutationStarted).toBe(true);
    expect(mutationMethod).toBe('PATCH');
    await page.keyboard.press('Escape');
    await expect(page.getByRole('alertdialog', { name: 'Disable this memory?' })).toHaveCount(0);
    await page.getByRole('link', { name: /Back to What Aura knows/ }).click();
    await page.getByRole('button', { name: /Current route B/ }).click();
    await expect(page.getByRole('heading', { name: 'Current route B', exact: true })).toBeVisible();
    releaseMutation();
    await expect(page.getByRole('heading', { name: 'Current route B', exact: true })).toBeVisible();
    await expect(page.getByRole('heading', { name: 'Mutation started on A', exact: true })).toHaveCount(0);
  });

  test('closes hard purge with Escape and restores focus to its opener', async ({ page }) => {
    const memory = {
      id: 'memory-purge-focus', scope: { type: 'user' }, status: 'active', pinned: false, version: 1,
      currentRevision: { id: 'memory-purge-focus-revision', memoryId: 'memory-purge-focus', revision: 1, kind: 'semantic', content: 'Purge focus fixture', correctionReason: null, confidence: .9, importance: .5, halfLifeDays: 30, observedAt: '2026-10-08T09:00:00Z', validFrom: null, validTo: null, createdAt: '2026-10-08T09:00:00Z' },
      reinforcedAt: null, dormantAt: null, archivedAt: null, createdAt: '2026-10-08T09:00:00Z', updatedAt: '2026-10-08T09:00:00Z',
    };
    await mockShellApi(page, async (route, url) => {
      if (url.pathname.endsWith('/memories')) return route.fulfill({ json: { items: [memory], nextCursor: null } });
      if (url.pathname.endsWith('/memories/memory-purge-focus')) return route.fulfill({ json: { ...memory, revisions: [memory.currentRevision], provenance: [], embeddingGenerations: [], embeddings: [], relations: [] } });
      return route.fulfill({ status: 404, json: { detail: 'fixture endpoint not implemented' } });
    });

    await page.goto('/memory/memory-purge-focus');
    await expect(page.getByRole('heading', { name: 'Purge focus fixture', exact: true })).toBeVisible();
    const opener = page.locator('#open-memory-purge');
    await opener.click();
    await expect(page.getByRole('alertdialog', { name: 'Purge this memory permanently?' })).toBeVisible();
    await page.keyboard.press('Escape');
    await expect(page.getByRole('alertdialog', { name: 'Purge this memory permanently?' })).toHaveCount(0);
    await expect(opener).toBeFocused();
  });

  test('keeps candidate, related-memory, and grounded message IDs under advanced technical disclosure', async ({ page }) => {
    const candidate = {
      id: 'candidate-technical-private', jobId: 'job-technical-private', runId: 'run-technical-private', version: 1, action: 'create', state: 'review',
      content: 'Technical disclosure candidate', kind: 'preference', scope: { type: 'user' }, confidence: .8, importance: .5, halfLifeDays: 30,
      validTo: null, sensitivity: 'ordinary', relatedMemoryId: 'memory-related-private', memoryId: null, decisionReason: null,
      createdAt: '2026-10-08T09:00:00Z', decidedAt: null, groundedMessageIds: ['message-grounded-private'],
    };
    await mockShellApi(page, async (route, url) => {
      if (url.pathname.endsWith('/memory-candidates') && route.request().method() === 'GET') return route.fulfill({ json: { items: [], nextCursor: null } });
      if (url.pathname.endsWith('/memory-candidates/candidate-technical-private')) return route.fulfill({ json: candidate });
      return route.fulfill({ status: 404, json: { detail: 'fixture endpoint not implemented' } });
    });

    await page.goto('/memory/candidates?candidateId=candidate-technical-private');
    await expect(page.getByRole('heading', { name: 'Technical disclosure candidate', exact: true })).toBeVisible();
    await expect(page.getByText('candidate-technical-private', { exact: true })).not.toBeVisible();
    await expect(page.getByText('memory-related-private', { exact: true })).not.toBeVisible();
    await expect(page.getByText('message-grounded-private', { exact: true })).not.toBeVisible();
    await page.getByText('Advanced technical evidence', { exact: true }).click();
    await expect(page.getByText('candidate-technical-private', { exact: true })).toBeVisible();
    await expect(page.getByText('memory-related-private', { exact: true })).toBeVisible();
    await expect(page.getByText('message-grounded-private', { exact: true })).toBeVisible();
  });

  test('keeps candidate list and detail generations independent while rejecting stale detail', async ({ page }) => {
    let resolveList!: (response: unknown) => void;
    let resolveA!: (response: unknown) => void;
    let resolveB!: (response: unknown) => void;
    const candidate = (id: string, content: string) => ({
      id, jobId: `job-${id}`, runId: `run-${id}`, version: 1, action: 'create', state: 'review', content,
      kind: 'preference', scope: { type: 'user' }, confidence: .8, importance: .6, halfLifeDays: 30,
      validTo: null, sensitivity: 'ordinary', relatedMemoryId: null, memoryId: null, decisionReason: null,
      createdAt: '2026-10-08T09:00:00Z', decidedAt: null, groundedMessageIds: [`message-${id}`],
    });
    const first = candidate('candidate-a', 'Older candidate response');
    const second = candidate('candidate-b', 'Current candidate response');
    await page.route('**/api/v1/**', async (route) => {
      const url = new URL(route.request().url());
      if (url.pathname.endsWith('/auth/session')) return route.fulfill({ json: { principal: { issuer: 'https://authentik.test', subject: 'fixture-owner', displayName: 'Fixture Owner' }, csrfToken: 'fixture-csrf', idleExpiresAt: '2099-10-08T10:00:00Z', absoluteExpiresAt: '2099-10-09T10:00:00Z' } });
      if (url.pathname.endsWith('/memory-candidates') && route.request().method() === 'GET') return new Promise<void>((resolve) => { resolveList = (response) => { void route.fulfill({ json: response }); resolve(); }; });
      if (url.pathname.endsWith('/memory-candidates/candidate-a')) return new Promise<void>((resolve) => { resolveA = (response) => { void route.fulfill({ json: response }); resolve(); }; });
      if (url.pathname.endsWith('/memory-candidates/candidate-b')) return new Promise<void>((resolve) => { resolveB = (response) => { void route.fulfill({ json: response }); resolve(); }; });
      return route.fulfill({ status: 404, json: { detail: 'fixture endpoint not implemented' } });
    });
    await page.goto('/memory/candidates?candidateId=candidate-a');
    await expect.poll(() => typeof resolveA).toBe('function');
    await page.goto('/memory/candidates?candidateId=candidate-b');
    await expect.poll(() => typeof resolveB).toBe('function');
    resolveA(first);
    await expect(page.getByRole('heading', { name: 'Older candidate response', exact: true })).toHaveCount(0);
    resolveB(second);
    await expect(page.getByRole('heading', { name: 'Current candidate response', exact: true })).toBeVisible();
    resolveList({ items: [first, second], nextCursor: null });
    await expect(page.getByText('2 pending candidates loaded', { exact: true })).toBeVisible();
  });

  test('clears previously loaded queue rows and actions after a failed refresh', async ({ page }) => {
    const candidate = {
      id: 'candidate-refresh-stale', jobId: 'job-refresh-stale', runId: 'run-refresh-stale', version: 1, action: 'create', state: 'review',
      content: 'Stale queue row', kind: 'preference', scope: { type: 'user' }, confidence: .8, importance: .5, halfLifeDays: 30,
      validTo: null, sensitivity: 'ordinary', relatedMemoryId: null, memoryId: null, decisionReason: null,
      createdAt: '2026-10-08T09:00:00Z', decidedAt: null,
    };
    let loads = 0;
    await mockShellApi(page, async (route, url) => {
      if (url.pathname.endsWith('/memory-candidates') && route.request().method() === 'GET') {
        loads += 1;
        return loads === 1
          ? route.fulfill({ json: { items: [candidate], nextCursor: null } })
          : route.fulfill({ status: 503, json: { detail: '<private refresh body>' } });
      }
      return route.fulfill({ status: 404, json: { detail: 'fixture endpoint not implemented' } });
    });

    await page.goto('/memory/candidates');
    await expect(page.getByText('Stale queue row', { exact: true })).toBeVisible();
    await page.getByRole('link', { name: 'Records', exact: true }).click();
    await page.getByRole('link', { name: 'Review queue', exact: true }).click();
    await expect.poll(() => loads).toBe(2);
    await expect(page.getByRole('alert')).toBeVisible();
    await expect(page.getByText('Stale queue row', { exact: true })).toHaveCount(0);
    await expect(page.getByRole('button', { name: /approve memory/i })).toHaveCount(0);
    await expect(page.getByRole('button', { name: /reject candidate/i })).toHaveCount(0);
    await expect(page.getByText('Pending count unavailable', { exact: true })).toBeVisible();
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
    // Route reuse without a candidateId must invalidate the prior detail.
    await page.getByRole('link', { name: 'Records', exact: true }).click();
    await page.getByRole('link', { name: 'Review queue', exact: true }).click();
    await expect(page.getByRole('heading', { name: 'Review queue', exact: true })).toBeVisible();
    await expect(page.getByRole('heading', { name: 'Keyboard review fixture', exact: true })).toHaveCount(0);
  });

  test('keeps an incomplete provider diagnostic out of the actionable review queue', async ({ page }) => {
    const candidate = {
      id: 'candidate-legacy', jobId: 'job-legacy', runId: 'run-legacy', version: 3, action: 'review', state: 'review',
      content: null, kind: null, scope: null, confidence: .88, importance: null, halfLifeDays: null,
      validTo: null, sensitivity: 'ordinary', relatedMemoryId: null, memoryId: null, decisionReason: 'incomplete_provider_output', createdAt: '2026-10-08T09:00:00Z', decidedAt: null,
    };
    await mockShellApi(page, async (route, url) => {
      if (url.pathname.endsWith('/memory-candidates/candidate-legacy')) return route.fulfill({ json: { ...candidate, groundedMessageIds: ['message-legacy'] } });
      if (url.pathname.endsWith('/memory-candidates')) return route.fulfill({ json: { items: [candidate], nextCursor: null } });
      return route.fulfill({ status: 404, json: { detail: 'fixture endpoint not implemented' } });
    });

    await page.setViewportSize({ width: 375, height: 800 });
    await page.goto('/memory/candidates?candidateId=candidate-legacy');
    await expect(page.getByRole('heading', { name: /complete memory proposal/i })).toBeVisible();
    await expect(page.getByText('Content withheld pending review', { exact: true })).toHaveCount(0);
    await expect(page.getByRole('button', { name: /approve memory/i })).toBeDisabled();
    await expect(page.getByRole('button', { name: /edit and approve/i })).toBeDisabled();
    await expect(page.getByRole('button', { name: /reject candidate/i })).toBeDisabled();
  });

  test('does not present zero as the pending count when the review queue fails', async ({ page }) => {
    await mockShellApi(page, async (route, url) => {
      if (url.pathname.endsWith('/memory-candidates')) return route.fulfill({ status: 503, json: { detail: '<script>private-provider-detail</script>' } });
      return route.fulfill({ status: 404, json: { detail: 'fixture endpoint not implemented' } });
    });
    await page.goto('/memory/candidates');
    await expect(page.getByText('Pending count unavailable', { exact: true })).toBeVisible();
    await expect(page.getByText(/0 pending/)).toHaveCount(0);
    await expect(page.getByText(/private-provider-detail|<script>/)).toHaveCount(0);
  });

  test('labels a lost manual-create response as unconfirmed and retains the draft', async ({ page }) => {
    await mockShellApi(page, async (route, url) => {
      if (url.pathname.endsWith('/memories')) {
        if (route.request().method() === 'POST') return route.abort('failed');
        return route.fulfill({ json: { items: [], nextCursor: null } });
      }
      return route.fulfill({ status: 404, json: { detail: 'fixture endpoint not implemented' } });
    });
    await page.goto('/memory');
    await page.getByRole('button', { name: 'Create memory' }).click();
    await page.locator('textarea[name="createContent"]').fill('A retained draft after an uncertain save.');
    await page.getByRole('button', { name: 'Create and embed' }).click();
    await expect(page.getByText(/save outcome is unconfirmed/i)).toBeVisible();
    await expect(page.locator('textarea[name="createContent"]')).toHaveValue('A retained draft after an uncertain save.');
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
    await page.getByText('Advanced recall threshold', { exact: true }).click();
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

  test('does not publish an older agent policy after route reuse', async ({ page }, testInfo) => {
    test.skip(testInfo.project.name === 'mobile', 'The global Agents link is intentionally inside the closed mobile drawer.');
    const now = '2026-10-08T09:00:00Z';
    const agent = (id: string, name: string) => ({ id, status: 'active', version: 1, currentRevision: { id: `${id}-revision`, profileId: id, revision: 1, displayName: name, purpose: 'Fixture purpose', instructions: 'Fixture instructions', personaRevisionId: 'persona', promptBundleRevisionId: 'prompt', modelPolicyRevisionId: 'model', createdAt: now }, revisions: [{ id: `${id}-revision`, profileId: id, revision: 1, displayName: name, purpose: 'Fixture purpose', instructions: 'Fixture instructions', personaRevisionId: 'persona', promptBundleRevisionId: 'prompt', modelPolicyRevisionId: 'model', createdAt: now }], createdAt: now, updatedAt: now });
    const policy = (agentProfileId: string, id: string, maxMemories: number) => ({ id, agentProfileId, revision: 1, sharedUserRead: true, currentAgentRead: true, sharedUserPromotion: false, fallbackRelevanceThreshold: .7, maxMemories, contextBudgetFraction: .05, fallbackAgentProfileIds: [], recallMode: 'automatic', automaticRecallThreshold: .7, createdAt: now });
    const agentA = agent('agent-policy-a', 'Policy A');
    const agentB = agent('agent-policy-b', 'Policy B');
    const policyA = policy(agentA.id, 'policy-a', 1);
    const policyB = policy(agentB.id, 'policy-b', 7);
    let resolveA!: (response: unknown) => void;
    let resolveB!: (response: unknown) => void;
    await page.route('**/api/v1/**', async (route) => {
      const url = new URL(route.request().url());
      if (url.pathname.endsWith('/auth/session')) return route.fulfill({ json: { principal: { issuer: 'https://authentik.test', subject: 'fixture-owner', displayName: 'Fixture Owner' }, csrfToken: 'fixture-csrf', idleExpiresAt: '2099-10-08T10:00:00Z', absoluteExpiresAt: '2099-10-09T10:00:00Z' } });
      if (url.pathname.endsWith('/models')) return route.fulfill({ json: { models: [{ id: 'fixture-model', displayName: 'Fixture model', provider: 'fixture', capabilities: ['chat'], availability: 'available', selectable: true, disabledReason: null }], defaultModelId: 'fixture-model' } });
      if (url.pathname.endsWith('/conversations')) return route.fulfill({ json: { items: [], nextCursor: null } });
      if (url.pathname.endsWith('/agents')) return route.fulfill({ json: { items: [agentA, agentB] } });
      if (url.pathname.endsWith('/agents/agent-policy-a')) return route.fulfill({ json: agentA });
      if (url.pathname.endsWith('/agents/agent-policy-b')) return route.fulfill({ json: agentB });
      if (url.pathname.endsWith('/agents/agent-policy-a/memory-policies')) return new Promise<void>((resolve) => { resolveA = (response) => { void route.fulfill({ json: response }); resolve(); }; });
      if (url.pathname.endsWith('/agents/agent-policy-b/memory-policies')) return new Promise<void>((resolve) => { resolveB = (response) => { void route.fulfill({ json: response }); resolve(); }; });
      return route.fulfill({ status: 404, json: { detail: 'fixture endpoint not implemented' } });
    });
    await page.goto('/agents/agent-policy-a/memory');
    await expect.poll(() => typeof resolveA).toBe('function');
    await page.getByRole('link', { name: 'Agents', exact: true }).click();
    await page.getByRole('link', { name: /Policy B/ }).click();
    await page.getByRole('link', { name: 'Open memory policy', exact: true }).click();
    await expect.poll(() => typeof resolveB).toBe('function');
    resolveA({ items: [policyA], attachedPolicyRevisionId: policyA.id, agentVersion: 1 });
    await expect(page.locator('input[name="maxMemories"]')).toHaveValue('2');
    resolveB({ items: [policyB], attachedPolicyRevisionId: policyB.id, agentVersion: 1 });
    await expect(page.locator('input[name="maxMemories"]')).toHaveValue('7');
    await expect(page.getByText('Revision 1', { exact: true })).toBeVisible();
    await expect(page.getByText('1 memories', { exact: true })).toHaveCount(0);
  });
});
