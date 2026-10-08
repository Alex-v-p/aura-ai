import { expect, test } from '@playwright/test';

test.describe('memory product', () => {
  test('browses owner memory with session-only search and inert content', async ({ page }) => {
    const memory = {
      id: 'memory-family', scope: { type: 'user' }, status: 'active', pinned: false, version: 1,
      currentRevision: { id: 'revision-family', memoryId: 'memory-family', revision: 1, kind: 'preference', content: '<not-an-instruction>', correctionReason: null, confidence: .95, importance: .7, halfLifeDays: 180, observedAt: '2026-10-08T09:00:00Z', validFrom: null, validTo: null, createdAt: '2026-10-08T09:00:00Z' },
      reinforcedAt: null, dormantAt: null, archivedAt: null, createdAt: '2026-10-08T09:00:00Z', updatedAt: '2026-10-08T09:00:00Z',
    };
    await page.route('**/api/v1/**', async (route) => {
      const url = new URL(route.request().url());
      if (url.pathname.endsWith('/auth/session')) return route.fulfill({ json: { authenticated: true, csrfToken: 'fixture-csrf', subject: 'fixture-owner' } });
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
    expect(await page.locator('script').count()).toBe(0);
    const search = page.getByRole('searchbox', { name: 'Search' });
    await search.fill('family');
    await expect(search).toHaveValue('family');
    expect(page.url()).not.toContain('family');
    await page.getByRole('button', { name: '<not-an-instruction>' }).click();
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
      if (url.pathname.endsWith('/auth/session')) return route.fulfill({ json: { authenticated: true, csrfToken: 'fixture-csrf', subject: 'fixture-owner' } });
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
      if (url.pathname.endsWith('/auth/session')) return route.fulfill({ json: { principal: { issuer: 'https://authentik.test', subject: 'fixture-owner' }, csrfToken: 'fixture-csrf', idleExpiresAt: now, absoluteExpiresAt: now } });
      if (url.pathname.endsWith('/models')) return route.fulfill({ json: { models: [{ id: 'fixture-model', displayName: 'Fixture model', provider: 'fixture', capabilities: ['chat'], availability: 'available', selectable: true, disabledReason: null }], defaultModelId: 'fixture-model', observedAt: now } });
      if (url.pathname.endsWith('/agents')) return route.fulfill({ json: { items: [] } });
      if (url.pathname.endsWith('/conversations')) return route.fulfill({ json: { items: [summary], nextCursor: null } });
      if (url.pathname.endsWith('/conversations/conversation-memory-popup')) return route.fulfill({ json: detail });
      if (url.pathname.endsWith('/runs/run-memory-popup/memory-activity')) return route.fulfill({ json: { runId: run.id, processingStatus: 'settled', lastEventId: null, reconciledAt: now, items: [{ id: 'activity-recalled', action: 'recalled', status: 'completed', scope: { type: 'user' }, candidateId: null, memoryId: 'memory-popup', memoryRevisionId: 'revision-popup', policyRevisionId: null, embeddingGenerationId: null, reconciliationStatus: 'authoritative', occurredAt: now }, { id: 'activity-candidate', action: 'queued_for_review', status: 'queued', scope: { type: 'user' }, candidateId: 'candidate-popup', memoryId: null, memoryRevisionId: null, policyRevisionId: null, embeddingGenerationId: null, reconciliationStatus: 'authoritative', occurredAt: now }] } });
      if (url.pathname.endsWith('/memories/memory-popup')) return route.fulfill({ json: { id: 'memory-popup', scope: { type: 'user' }, status: 'active', pinned: false, version: 1, currentRevision: { id: 'revision-popup', memoryId: 'memory-popup', revision: 1, kind: 'semantic', content: 'Bounded popup record', correctionReason: null, confidence: .9, importance: .8, halfLifeDays: 30, observedAt: now, validFrom: null, validTo: null, createdAt: now }, reinforcedAt: null, dormantAt: null, archivedAt: null, createdAt: now, updatedAt: now, provenance: [{ id: 'provenance-popup', memoryRevisionId: 'revision-popup', type: 'run', sourceId: run.id, sourceContentDigest: 'digest', evidence: null, observedAt: now, createdAt: now }], revisions: [], embeddingGenerations: [], embeddings: [], relations: [] } });
      if (url.pathname.endsWith('/memory-candidates/candidate-popup')) return route.fulfill({ json: { id: 'candidate-popup', jobId: 'job-popup', runId: run.id, version: 1, action: 'create', state: 'review', content: 'Candidate context', kind: 'semantic', scope: { type: 'user' }, confidence: .7, importance: .5, halfLifeDays: 7, validTo: null, sensitivity: 'ordinary', relatedMemoryId: null, memoryId: null, decisionReason: null, createdAt: now, decidedAt: null, groundedMessageIds: ['user-memory-popup'] } });
      return route.fulfill({ status: 404, json: { detail: 'fixture endpoint not implemented' } });
    });
    await page.goto('/conversation');
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
});
