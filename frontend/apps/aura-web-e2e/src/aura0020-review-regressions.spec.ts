import { expect, test, type Page, type Route } from '@playwright/test';

const now = '2026-10-06T00:00:00.000Z';
const neutral = {
  id: 'persona-neutral-r1',
  profileId: 'persona-neutral',
  revision: 1,
  displayName: 'Neutral',
  description: 'Clear and calm.',
  instructions: 'Be clear.',
  personaRevisionId: null,
  promptBundleRevisionId: 'bundle-1',
  modelPolicyRevisionId: 'policy-1',
  createdAt: now,
};
const activeRevision = {
  id: 'agent-researcher-r1',
  profileId: 'agent-researcher',
  revision: 1,
  displayName: 'Researcher',
  purpose: 'Research carefully.',
  instructions: 'Cite sources.',
  personaRevisionId: neutral.id,
  promptBundleRevisionId: 'bundle-1',
  modelPolicyRevisionId: 'policy-1',
  createdAt: now,
};
const activeAgent = {
  id: 'agent-researcher',
  status: 'active',
  version: 1,
  currentRevision: activeRevision,
  createdAt: now,
  updatedAt: now,
  revisions: [activeRevision],
};
const disabledRevision = { ...activeRevision, id: 'agent-disabled-r1', profileId: 'agent-disabled', displayName: 'Disabled Agent' };
const disabledAgent = { ...activeAgent, id: 'agent-disabled', status: 'disabled', currentRevision: disabledRevision, revisions: [disabledRevision] };

function agentReference(profile: typeof activeAgent, revision = profile.currentRevision): Record<string, unknown> {
  return {
    profileId: profile.id,
    revisionId: revision.id,
    revision: revision.revision,
    displayName: revision.displayName,
    status: profile.status,
    newerRevisionAvailable: false,
  };
}

async function installFake(page: Page, disabled: boolean): Promise<{ readonly runRequests: string[] }> {
  const runRequests: string[] = [];
  const selected = disabled ? disabledAgent : activeAgent;
  const summary = {
    id: 'conversation-1',
    title: 'Existing conversation',
    agentProfileId: selected.id,
    agentRevisionId: selected.currentRevision.id,
    agent: agentReference(selected),
    agentAssignments: [{ id: 'assignment-1', agent: agentReference(selected), reason: 'initial', effectiveAfterMessageId: null, createdAt: now }],
    modelId: 'chat',
    version: 1,
    createdAt: now,
    updatedAt: now,
    currentRun: null,
  };
  await page.route('**/api/**', async (route: Route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.pathname === '/api/v1/auth/session') return route.fulfill({ json: { principal: { issuer: 'https://issuer', subject: 'owner', displayName: 'Owner' }, csrfToken: 'csrf', idleExpiresAt: now, absoluteExpiresAt: now } });
    if (url.pathname === '/api/v1/models') return route.fulfill({ json: { models: [{ id: 'chat', displayName: 'Chat', provider: 'ollama', capabilities: ['chat'], availability: 'available', selectable: true, disabledReason: null }], defaultModelId: 'chat', observedAt: now } });
    if (url.pathname === '/api/v1/agents' && request.method() === 'GET') return route.fulfill({ json: { items: [activeAgent, disabledAgent] } });
    if (url.pathname === '/api/v1/agents/agent-researcher' && request.method() === 'GET') return route.fulfill({ json: activeAgent });
    if (url.pathname === '/api/v1/agents/agent-disabled' && request.method() === 'GET') return route.fulfill({ json: disabledAgent });
    if (url.pathname === '/api/v1/conversations' && request.method() === 'GET') return route.fulfill({ json: { items: [summary], nextCursor: null } });
    if (url.pathname === '/api/v1/conversations/conversation-1' && request.method() === 'GET') return route.fulfill({ json: { ...summary, messages: [{ id: 'assistant-1', conversationId: summary.id, role: 'assistant', content: 'Existing answer.', state: 'complete', runId: null, createdAt: now, updatedAt: now }], recentRuns: [], agentAssignments: summary.agentAssignments } });
    if (url.pathname === '/api/v1/conversations/conversation-1/runs' && request.method() === 'POST') {
      runRequests.push(request.postData() ?? '');
      return route.fulfill({ status: 409, json: { detail: 'The agent was disabled before the request was accepted.' } });
    }
    if (url.pathname.endsWith('/events')) return route.fulfill({ status: 200, headers: { 'content-type': 'text/event-stream' }, body: '' });
    return route.continue();
  });
  return { runRequests };
}

test('Enter on a disabled agent is a no-op and keeps the draft', async ({ page }) => {
  await installFake(page, true);
  await page.goto('/conversation');
  const composer = page.getByRole('textbox', { name: /message aura/i });
  await composer.fill('Keep this draft while disabled.');
  await composer.press('Enter');
  await expect(composer).toHaveValue('Keep this draft while disabled.');
  await expect(page.getByRole('alert')).toContainText(/disabled/i);
});

test('server-side 409 immediately before submit preserves the exact draft', async ({ page }) => {
  const requests = await installFake(page, false);
  await page.goto('/conversation');
  const composer = page.getByRole('textbox', { name: /message aura/i });
  await composer.fill('The server may reject this, but do not lose it.');
  await composer.press('Enter');
  await expect.poll(() => requests.runRequests.length).toBe(1);
  await expect(composer).toHaveValue('The server may reject this, but do not lose it.');
  await expect(page.locator('#composer-notice')).toContainText(/could not complete|disabled|accepted/i);
});
