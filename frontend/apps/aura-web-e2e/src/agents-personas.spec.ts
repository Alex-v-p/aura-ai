import { expect, test, type Page, type Route } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';

type Revision = {
  id: string;
  profileId?: string;
  revision: number;
  displayName?: string;
  purpose?: string;
  instructions: string;
  personaRevisionId?: string;
};

type Profile = {
  id: string;
  displayName: string;
  status: 'active' | 'disabled';
  version: number;
  currentRevisionId: string;
  revisions: Revision[];
};

async function installConfigurationFake(page: Page): Promise<void> {
  const now = (): string => new Date().toISOString();
  const neutralRevision: Revision = { id: 'persona-neutral-r1', profileId: 'persona-neutral', revision: 1, displayName: 'Neutral', instructions: 'Use a clear, calm style.' };
  const persona: Profile = { id: 'persona-neutral', displayName: 'Neutral', status: 'active', version: 1, currentRevisionId: neutralRevision.id, revisions: [neutralRevision] };
  const auraRevision: Revision = { id: 'agent-aura-r1', revision: 1, displayName: 'Aura', purpose: 'A helpful assistant.', instructions: 'Be helpful.', personaRevisionId: neutralRevision.id };
  const auraRevision2: Revision = { id: 'agent-aura-r2', revision: 2, displayName: 'Aura', purpose: 'A more focused Aura.', instructions: 'Be precise.', personaRevisionId: neutralRevision.id };
  const agent: Profile = { id: 'agent-aura', displayName: 'Aura', status: 'active', version: 2, currentRevisionId: auraRevision2.id, revisions: [auraRevision, auraRevision2] };
  const researcherRevision: Revision = { id: 'agent-researcher-r1', profileId: 'agent-researcher', revision: 1, displayName: 'Researcher', purpose: 'Research carefully.', instructions: 'Cite sources.', personaRevisionId: neutralRevision.id };
  const researcher: Profile = { id: 'agent-researcher', displayName: 'Researcher', status: 'active', version: 1, currentRevisionId: researcherRevision.id, revisions: [researcherRevision] };
  let version = 1;
  let draft = '';
  let assignmentRevision = auraRevision.id;
  let assignments: Array<Record<string, unknown>> = [{ id: 'assignment-0', agent: { profileId: agent.id, revisionId: auraRevision.id, revision: 1, displayName: agent.displayName, status: agent.status, newerRevisionAvailable: true }, reason: 'initial', effectiveAfterMessageId: null, createdAt: now() }];
  let disabledFixture = false;
  page.on('framenavigated', (frame) => { if (frame === page.mainFrame() && frame.url().includes('fixture=disabled')) disabledFixture = true; });
  const revisionPayload = (profile: Profile, revision: Revision): Record<string, unknown> => ({ id: revision.id, profileId: profile.id, revision: revision.revision, displayName: revision.displayName ?? profile.displayName, purpose: revision.purpose ?? '', description: profile.id.startsWith('persona-') ? 'A reusable interaction style.' : '', instructions: revision.instructions, personaRevisionId: revision.personaRevisionId ?? null, promptBundleRevisionId: 'prompt-bundle-1', modelPolicyRevisionId: 'policy-1', createdAt: now() });
  const profileSummary = (profile: Profile): Record<string, unknown> => {
    const current = profile.revisions.at(-1);
    if (!current) throw new Error(`Profile ${profile.id} has no revision`);
    return { id: profile.id, status: profile.status, version: profile.version, currentRevision: revisionPayload(profile, current), createdAt: now(), updatedAt: now() };
  };
  const profileDetail = (profile: Profile): Record<string, unknown> => ({ ...profileSummary(profile), revisions: profile.revisions.map((revision) => revisionPayload(profile, revision)) });
  const agentReference = (revisionId: string, status: 'active' | 'disabled' = agent.status): Record<string, unknown> => {
    const revision = agent.revisions.find((item) => item.id === revisionId) ?? auraRevision;
    return { profileId: agent.id, revisionId: revision.id, revision: revision.revision, displayName: agent.displayName, status, newerRevisionAvailable: revision.revision < (agent.revisions.at(-1)?.revision ?? revision.revision) };
  };
  const summary = (): Record<string, unknown> => ({
    id: 'welcome',
    title: 'A thoughtful beginning',
    agentProfileId: agent.id,
    agentRevisionId: assignmentRevision,
    agent: agentReference(assignmentRevision, disabledFixture ? 'disabled' : agent.status),
    agentAssignments: assignments,
    modelId: 'qwen2.5:7b',
    version,
    createdAt: now(),
    updatedAt: now(),
    currentRun: null,
  });

  await page.route('**/api/**', async (route: Route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.pathname === '/api/v1/auth/session') return route.fulfill({ json: { principal: { issuer: 'https://authentik.test', subject: 'owner', displayName: 'Owner' }, csrfToken: 'csrf', idleExpiresAt: now(), absoluteExpiresAt: now() } });
    if (url.pathname === '/api/v1/models') return route.fulfill({ json: { models: [{ id: 'qwen2.5:7b', displayName: 'Qwen 2.5', provider: 'ollama', capabilities: ['chat'], availability: 'available', selectable: true, disabledReason: null }], defaultModelId: 'qwen2.5:7b', observedAt: now() } });
    if (url.pathname === '/api/v1/agents' && request.method() === 'GET') return route.fulfill({ json: { items: [profileSummary(agent), profileSummary(researcher)] } });
    if (url.pathname === '/api/v1/personas' && request.method() === 'GET') return route.fulfill({ json: { items: [profileSummary(persona)] } });
    if (url.pathname === '/api/v1/agents/agent-aura' && request.method() === 'GET') return route.fulfill({ json: profileDetail(agent) });
    if (url.pathname === '/api/v1/personas/persona-neutral' && request.method() === 'GET') return route.fulfill({ json: profileDetail(persona) });
    if (url.pathname === '/api/v1/conversations' && request.method() === 'GET') return route.fulfill({ json: { items: [summary()], nextCursor: null } });
    if (url.pathname === '/api/v1/conversations/welcome' && request.method() === 'GET') return route.fulfill({ json: { ...summary(), messages: [{ id: 'assistant-0', conversationId: 'welcome', role: 'assistant', content: 'Welcome.', state: 'complete', runId: null, createdAt: now(), updatedAt: now() }], recentRuns: [], agentAssignments: assignments } });
    if (url.pathname === '/api/v1/conversations/welcome' && request.method() === 'PATCH') {
      const body = JSON.parse(request.postData() ?? '{}') as { agentRevisionId?: string; transcriptSharingConfirmed?: boolean; version?: number };
      if (body.agentRevisionId && !body.transcriptSharingConfirmed) return route.fulfill({ status: 409, json: { detail: 'Confirm sharing the full completed transcript.' } });
      if (body.agentRevisionId) {
        assignmentRevision = body.agentRevisionId;
        const replacement = body.agentRevisionId === researcherRevision.id ? researcherRevision : auraRevision2;
        const replacementAgent = replacement.profileId === agent.id ? agentReference(replacement.id) : { profileId: researcher.id, revisionId: replacement.id, revision: replacement.revision, displayName: researcher.displayName, status: 'active', newerRevisionAvailable: false };
        assignments = [...assignments, { id: `assignment-${assignments.length}`, agent: replacementAgent, reason: replacement.profileId === agent.id ? 'revision_upgrade' : 'manual_switch', effectiveAfterMessageId: 'assistant-0', createdAt: now() }];
      }
      version = (body.version ?? version) + 1;
      return route.fulfill({ json: summary() });
    }
    if (url.pathname === '/api/v1/agents' && request.method() === 'POST') {
      const body = JSON.parse(request.postData() ?? '{}') as { displayName: string; purpose: string; instructions: string };
      agent.displayName = body.displayName;
      agent.revisions = [{ id: 'agent-new-r1', revision: 1, displayName: body.displayName, purpose: body.purpose, instructions: body.instructions, personaRevisionId: neutralRevision.id }];
      agent.currentRevisionId = 'agent-new-r1';
      return route.fulfill({ status: 201, json: profileDetail(agent) });
    }
    if (url.pathname === '/api/v1/agents/agent-aura' && request.method() === 'PATCH') {
      const body = JSON.parse(request.postData() ?? '{}') as { status?: 'active' | 'disabled' };
      if (body.status) agent.status = body.status;
      agent.version += 1;
      return route.fulfill({ json: profileDetail(agent) });
    }
    if (url.pathname === '/api/v1/conversations/welcome/runs' && request.method() === 'POST') {
      const body = JSON.parse(request.postData() ?? '{}') as { message?: string };
      draft = body.message ?? '';
      return route.fulfill({ status: 202, json: { conversation: summary(), userMessage: { id: 'user-1', conversationId: 'welcome', role: 'user', content: draft, state: 'complete', runId: 'run-1', createdAt: now(), updatedAt: now() }, run: { id: 'run-1', conversationId: 'welcome', userMessageId: 'user-1', assistantMessageId: null, status: 'queued', agentRevisionId: assignmentRevision, modelPolicyRevisionId: 'policy-1', provider: 'ollama', modelId: 'qwen2.5:7b', retryOfRunId: null, createdAt: now(), startedAt: null, finishedAt: null, error: null } } });
    }
    if (url.pathname.endsWith('/events')) return route.fulfill({ status: 200, headers: { 'content-type': 'text/event-stream' }, body: '' });
    return route.continue();
  });
}

test.beforeEach(async ({ page }) => { await installConfigurationFake(page); });

test('exposes keyboard-accessible agent and persona management routes', async ({ page }) => {
  await page.goto('/agents');
  await expect(page.getByRole('heading', { name: /agents/i })).toBeVisible();
  await expect(page.getByRole('link', { name: /personas/i })).toBeVisible();
  await page.getByRole('button', { name: /new agent|create agent/i }).click();
  await page.getByLabel(/display name/i).fill('Researcher');
  await page.getByLabel(/purpose/i).fill('Research questions carefully.');
  await page.getByLabel(/behavioral instructions|instructions/i).fill('Cite sources.');
  await page.getByRole('button', { name: /save|create/i }).click();
  await expect(page.getByText('Researcher')).toBeVisible();
  await page.goto('/personas');
  await expect(page.getByRole('heading', { name: /personas/i })).toBeVisible();
  await expect(page.getByText('Neutral')).toBeVisible();
  const results = await new AxeBuilder({ page }).analyze();
  expect(results.violations).toEqual([]);
});

test('confirms full-transcript sharing and renders an assignment transition marker', async ({ page }) => {
  await page.goto('/');
  const picker = page.getByLabel(/agent/i);
  await expect(picker).toBeVisible();
  await picker.selectOption('agent-aura-r2');
  await expect(page.getByRole('dialog')).toContainText(/full completed transcript/i);
  await page.getByRole('dialog').getByRole('button', { name: /confirm|switch|continue/i }).click();
  await expect(page.getByText(/agent changed/i)).toBeVisible();
  await picker.selectOption('agent-researcher-r1');
  await page.getByRole('dialog').getByRole('button', { name: /confirm|switch|continue/i }).click();
  await expect(page.getByText(/agent changed/i)).toBeVisible();
});

test('preserves a draft and offers an active replacement when the assigned agent is disabled', async ({ page }) => {
  await page.goto('/?fixture=disabled');
  const composer = page.getByRole('textbox', { name: /message/i });
  await composer.fill('Keep this draft while I choose a replacement.');
  await expect(page.getByText(/disabled|choose an active replacement/i)).toBeVisible();
  await expect(composer).toHaveValue('Keep this draft while I choose a replacement.');
  await expect(page.getByRole('button', { name: /send/i })).toBeDisabled();
  await page.getByLabel(/agent/i).selectOption('agent-researcher-r1');
  await page.getByRole('dialog').getByRole('button', { name: /confirm|switch|continue/i }).click();
  await expect(composer).toHaveValue('Keep this draft while I choose a replacement.');
  await expect(page.getByRole('button', { name: /send/i })).toBeEnabled();
});
