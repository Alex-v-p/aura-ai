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
  toolPolicyRevisionId?: string;
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
  let toolPolicyRevision = 1;
  let toolPolicyId = 'tool-policy-aura-r1';
  let toolGrants: Array<{ toolId: string; toolVersion: number }> = [{ toolId: 'memory.search', toolVersion: 1 }];
  page.on('framenavigated', (frame) => { if (frame === page.mainFrame() && frame.url().includes('fixture=disabled')) disabledFixture = true; });
  const revisionPayload = (profile: Profile, revision: Revision): Record<string, unknown> => ({ id: revision.id, profileId: profile.id, revision: revision.revision, displayName: revision.displayName ?? profile.displayName, purpose: revision.purpose ?? '', description: profile.id.startsWith('persona-') ? 'A reusable interaction style.' : '', instructions: revision.instructions, personaRevisionId: revision.personaRevisionId ?? null, promptBundleRevisionId: 'prompt-bundle-1', modelPolicyRevisionId: 'policy-1', memoryPolicyRevisionId: 'memory-policy-1', toolPolicyRevisionId: revision.toolPolicyRevisionId ?? (profile.id === agent.id ? toolPolicyId : 'tool-policy-deny-r1'), createdAt: now() });
  const profileSummary = (profile: Profile): Record<string, unknown> => {
    const current = profile.revisions.at(-1);
    if (!current) throw new Error(`Profile ${profile.id} has no revision`);
    return { id: profile.id, status: profile.status, version: profile.version, currentRevision: revisionPayload(profile, current), ...(profile.id.startsWith('persona-') ? { assignedAgentCount: 0 } : {}), createdAt: now(), updatedAt: now() };
  };
  const profileDetail = (profile: Profile): Record<string, unknown> => ({ ...profileSummary(profile), revisions: profile.revisions.map((revision) => revisionPayload(profile, revision)), ...(profile.id.startsWith('persona-') ? { assignedAgents: [], assignedAgentsTruncated: false } : {}) });
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
    if (url.pathname === '/api/v1/tools' && request.method() === 'GET') return route.fulfill({ json: { items: [{ id: 'memory.search', version: 1, displayName: 'Search memory', description: 'Search the agent\'s authorized private memory.', classification: 'read', permissionScope: 'memory:read', confirmation: 'never', timeoutMs: 30000, retryLimit: 0, idempotency: 'read_only', sensitivity: 'private', observableComponent: 'aura.runtime.tool_execution' }] } });
    if (url.pathname === '/api/v1/agents' && request.method() === 'GET') return route.fulfill({ json: { items: [profileSummary(agent), profileSummary(researcher)] } });
    if (url.pathname === '/api/v1/personas' && request.method() === 'GET') return route.fulfill({ json: { items: [profileSummary(persona)] } });
    if (url.pathname === '/api/v1/personas/preview' && request.method() === 'POST') return route.fulfill({ json: { modelId: 'qwen2.5:7b', content: 'A calm fixture preview.' } });
    if (url.pathname === '/api/v1/agents/agent-aura' && request.method() === 'GET') return route.fulfill({ json: profileDetail(agent) });
    if (url.pathname === '/api/v1/agents/agent-researcher' && request.method() === 'GET') return route.fulfill({ json: profileDetail(researcher) });
    if (url.pathname === '/api/v1/agents/agent-aura/tool-policies' && request.method() === 'GET') return route.fulfill({ json: { items: [{ id: toolPolicyId, agentProfileId: agent.id, revision: toolPolicyRevision, grants: toolGrants, maxCallsPerRun: toolGrants.length ? 1 : 0, timeoutMs: 30000, retryLimit: 0, createdAt: now() }], attachedPolicyRevisionId: toolPolicyId, agentVersion: agent.version } });
    if (url.pathname === '/api/v1/agents/agent-aura/tool-policies' && request.method() === 'POST') {
      const body = JSON.parse(request.postData() ?? '{}') as { grants?: Array<{ toolId: string; toolVersion: number }> };
      toolPolicyRevision += 1;
      toolPolicyId = `tool-policy-aura-r${toolPolicyRevision}`;
      toolGrants = body.grants ?? [];
      return route.fulfill({ status: 201, json: { id: toolPolicyId, agentProfileId: agent.id, revision: toolPolicyRevision, grants: toolGrants, maxCallsPerRun: toolGrants.length ? 1 : 0, timeoutMs: 30000, retryLimit: 0, createdAt: now() } });
    }
    if (url.pathname.startsWith('/api/v1/agents/agent-aura/tool-policies/') && request.method() === 'POST') return route.fulfill({ status: 201, json: profileDetail(agent) });
    if (url.pathname === '/api/v1/personas/persona-neutral' && request.method() === 'GET') return route.fulfill({ json: profileDetail(persona) });
    if (url.pathname === '/api/v1/conversations' && request.method() === 'GET') return route.fulfill({ json: { items: [summary()], nextCursor: null } });
    if (url.pathname === '/api/v1/conversations/welcome' && request.method() === 'GET') return route.fulfill({ json: { ...summary(), messages: [{ id: 'assistant-0', conversationId: 'welcome', role: 'assistant', content: 'Welcome.', state: 'complete', runId: null, createdAt: now(), updatedAt: now() }], recentRuns: [], agentAssignments: assignments } });
    if (url.pathname === '/api/v1/conversations/welcome' && request.method() === 'PATCH') {
      const body = JSON.parse(request.postData() ?? '{}') as { agentRevisionId?: string; transcriptSharingConfirmed?: boolean; version?: number };
      if (body.agentRevisionId && !body.transcriptSharingConfirmed) return route.fulfill({ status: 409, json: { detail: 'Confirm sharing the full completed transcript.' } });
      if (body.agentRevisionId) {
        assignmentRevision = body.agentRevisionId;
        disabledFixture = false;
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

async function expectStepControlsToFit(page: Page, expectedCount: number): Promise<void> {
  const steps = page.locator('.stepper button');
  await expect(steps).toHaveCount(expectedCount);
  const viewport = page.viewportSize();
  if (!viewport) throw new Error('Expected a configured viewport for the responsive stepper check.');
  for (let index = 0; index < expectedCount; index += 1) {
    const box = await steps.nth(index).boundingBox();
    if (!box) throw new Error(`Step ${index + 1} is not visible in the configured viewport.`);
    expect(box.x).toBeGreaterThanOrEqual(0);
    expect(box.x + box.width).toBeLessThanOrEqual(viewport.width);
  }
}

test.beforeEach(async ({ page }) => { await installConfigurationFake(page); });

test('exposes keyboard-accessible agent and persona management routes', async ({ page }, testInfo) => {
  await page.goto('/agents');
  await expect(page.getByRole('heading', { name: /agents/i })).toBeVisible();
  await expect(page.locator('.feature-card').filter({ hasText: 'Aura' }).getByText('Neutral')).toBeVisible();
  if (testInfo.project.name === 'mobile') {
    const navigation = page.getByRole('navigation', { name: 'Primary navigation' });
    await page.getByRole('button', { name: 'Open navigation' }).click();
    await expect(navigation).toHaveClass(/is-mobile-open/);
    await expect(page.getByRole('link', { name: /personas/i })).toBeVisible();
    await navigation.getByRole('button', { name: 'Close navigation' }).click();
    await expect(navigation).toBeHidden();
  } else {
    await expect(page.getByRole('link', { name: /personas/i })).toBeVisible();
  }
  await page.getByRole('link', { name: 'Create agent' }).click();
  if (testInfo.project.name === 'mobile') await expectStepControlsToFit(page, 6);
  const displayName = page.getByLabel(/display name/i);
  await displayName.fill('Researcher');
  await page.getByLabel(/purpose/i).fill('Research questions carefully.');
  await page.getByRole('button', { name: 'Continue' }).click();
  await page.getByLabel(/persona revision/i).selectOption('persona-neutral-r1');
  await page.getByRole('button', { name: 'Continue' }).click();
  await page.getByLabel(/behavioral guidance/i).fill('Cite sources.');
  await page.getByRole('button', { name: 'Continue' }).click();
  await page.getByRole('button', { name: 'Continue' }).click();
  await page.getByRole('button', { name: 'Continue' }).click();
  await expect(page.getByText('Researcher')).toBeVisible();
  await page.getByRole('button', { name: 'Publish agent' }).click();
  await page.goto('/agents');
  await expect(page.getByRole('link', { name: /Researcher/ }).first()).toBeVisible();
  await page.goto('/personas');
  await expect(page.getByRole('heading', { name: /personas/i })).toBeVisible();
  await expect(page.getByText('Neutral')).toBeVisible();
  const results = await new AxeBuilder({ page }).analyze();
  expect(results.violations).toEqual([]);
  if (testInfo.project.name === 'mobile') {
    await page.getByRole('link', { name: 'Create persona' }).click();
    await expectStepControlsToFit(page, 4);
  }
});

test('keeps persona preview ephemeral and exposes assignment history without technical noise', async ({ page }) => {
  await page.goto('/personas/persona-neutral');
  await expect(page.getByRole('heading', { name: 'Neutral', level: 1 })).toBeVisible();
  if (test.info().project.name === 'mobile') {
    const sectionChooser = page.getByLabel('Persona section', { exact: true });
    await sectionChooser.selectOption('used-by');
    await expect(page.getByRole('heading', { name: 'Assigned agents' })).toBeVisible();
    await sectionChooser.selectOption('preview');
    await expect(page.getByRole('heading', { name: 'Try this persona' })).toBeVisible();
  } else {
    await expect(page.getByRole('heading', { name: 'Assigned agents' })).toBeVisible();
  }
  await page.getByLabel(/test prompt/i).fill('Give me one calm next step.');
  await page.getByLabel('Model').selectOption('qwen2.5:7b');
  await page.getByRole('button', { name: /generate ephemeral preview/i }).click();
  await expect(page.getByRole('status')).toContainText(/calm fixture preview/i);
  await expect(page.getByText(/not saved/i)).toBeVisible();
  const results = await new AxeBuilder({ page }).analyze();
  expect(results.violations).toEqual([]);
});

test('keeps agent and persona library failures exclusive from empty states', async ({ page }) => {
  await page.route('**/api/v1/agents', async (route) => route.request().method() === 'GET'
    ? route.fulfill({ status: 503, json: { detail: 'Agents are temporarily unavailable.' } })
    : route.continue());
  await page.goto('/agents');
  await expect(page.getByText('Agents are unavailable')).toBeVisible();
  await expect(page.getByText('No agents yet')).toHaveCount(0);
  await expect(page.getByText(/0 agents/)).toHaveCount(0);

  await page.unroute('**/api/v1/agents');
  await page.route('**/api/v1/personas', async (route) => route.request().method() === 'GET'
    ? route.fulfill({ status: 503, json: { detail: 'Personas are temporarily unavailable.' } })
    : route.continue());
  await page.goto('/personas');
  await expect(page.getByText('Personas are unavailable')).toBeVisible();
  await expect(page.getByText('No personas yet')).toHaveCount(0);
  await expect(page.getByText(/0 personas/)).toHaveCount(0);
});

test('keeps the Agent route in explicit authentication recovery for 401', async ({ page }) => {
  await page.route('**/api/v1/agents', async (route) => route.request().method() === 'GET'
    ? route.fulfill({ status: 401, json: { detail: 'Sign in to manage agents.' } })
    : route.continue());
  await page.goto('/agents');

  await expect(page.getByText('Agents are unavailable')).toBeVisible();
  await expect(page.getByText(/session has expired/i)).toBeVisible();
  await expect(page.getByText('No agents yet')).toHaveCount(0);
  await expect(page.getByRole('link', { name: 'Create agent' })).toHaveCount(0);
});

test('keeps the Persona route in explicit permission recovery for 403', async ({ page }) => {
  await page.route('**/api/v1/personas', async (route) => route.request().method() === 'GET'
    ? route.fulfill({ status: 403, json: { detail: 'Permission denied.' } })
    : route.continue());
  await page.goto('/personas');

  await expect(page.getByText('Personas are unavailable')).toBeVisible();
  await expect(page.getByRole('alert').filter({ hasText: 'You do not have permission to view these personas.' })).toBeVisible();
  await expect(page.getByText('No personas yet')).toHaveCount(0);
  await expect(page.getByRole('link', { name: 'Create persona' })).toHaveCount(0);
});

test('shows a recoverable detail failure instead of an indefinite loading state', async ({ page }) => {
  await page.route('**/api/v1/agents/agent-aura', async (route) => route.request().method() === 'GET'
    ? route.fulfill({ status: 503, json: { detail: 'Agent details are temporarily unavailable.' } })
    : route.continue());
  await page.route('**/api/v1/personas/persona-neutral', async (route) => route.request().method() === 'GET'
    ? route.fulfill({ status: 503, json: { detail: 'Persona details are temporarily unavailable.' } })
    : route.continue());

  await page.goto('/agents/agent-aura');
  await expect(page.getByText('Agent details are unavailable')).toBeVisible();
  await expect(page.getByText('Loading agent…')).toHaveCount(0);

  await page.goto('/personas/persona-neutral');
  await expect(page.getByText('Persona details are unavailable')).toBeVisible();
  await expect(page.getByText('Loading persona…')).toHaveCount(0);
});

test('shows an unavailable Persona fallback when assignment data cannot load', async ({ page }) => {
  await page.route('**/api/v1/personas', async (route) => route.request().method() === 'GET'
    ? route.fulfill({ status: 503, json: { detail: 'Personas are temporarily unavailable.' } })
    : route.continue());
  await page.goto('/agents');
  await expect(page.locator('.feature-card').filter({ hasText: 'Aura' }).getByText('Persona unavailable')).toBeVisible();
});

test('keeps Agent and Persona revision editors open after a conflict', async ({ page }) => {
  await page.route('**/api/v1/agents/agent-aura/revisions', async (route) => route.request().method() === 'POST'
    ? route.fulfill({ status: 409, json: { detail: 'This agent changed elsewhere.' } })
    : route.continue());
  await page.route('**/api/v1/personas/persona-neutral/revisions', async (route) => route.request().method() === 'POST'
    ? route.fulfill({ status: 409, json: { detail: 'This persona changed elsewhere.' } })
    : route.continue());

  await page.goto('/agents/agent-aura');
  await page.getByRole('button', { name: 'New revision' }).click();
  await page.getByLabel(/display name/i).fill('Aura conflict draft');
  await page.getByLabel(/purpose/i).fill('Keep the current purpose while resolving the conflict.');
  await page.getByLabel(/behavioral guidance/i).fill('Keep this draft available.');
  await expect(page.getByRole('option', { name: 'Neutral · revision 1' })).toBeAttached();
  await page.getByRole('combobox', { name: 'Persona', exact: true }).selectOption({ label: 'Neutral · revision 1' });
  await page.getByRole('button', { name: 'Save immutable revision' }).click();
  await expect(page.getByRole('heading', { name: 'Shape the next version' })).toBeVisible();
  await expect(page.getByLabel(/display name/i)).toHaveValue('Aura conflict draft');

  await page.goto('/personas/persona-neutral');
  await page.getByRole('button', { name: 'New revision' }).click();
  await page.getByLabel(/display name/i).fill('Neutral conflict draft');
  await page.getByLabel(/description/i).fill('Keep this description while resolving the conflict.');
  await page.getByLabel(/behavioral guidance/i).fill('Keep the persona draft available.');
  await page.getByRole('button', { name: 'Save immutable revision' }).click();
  await expect(page.getByRole('heading', { name: 'Shape the next persona' })).toBeVisible();
  await expect(page.getByLabel(/display name/i)).toHaveValue('Neutral conflict draft');
});

test('keeps Agent and Persona builder drafts after a failed publish', async ({ page }) => {
  await page.route('**/api/v1/agents', async (route) => route.request().method() === 'POST'
    ? route.fulfill({ status: 503, json: { detail: 'Agent publication is temporarily unavailable.' } })
    : route.continue());
  await page.route('**/api/v1/personas', async (route) => route.request().method() === 'POST'
    ? route.fulfill({ status: 503, json: { detail: 'Persona publication is temporarily unavailable.' } })
    : route.fulfill({ json: { items: [{ id: 'persona-neutral', status: 'active', version: 1, currentRevision: { id: 'persona-neutral-r1', profileId: 'persona-neutral', revision: 1, displayName: 'Neutral', description: 'A reusable interaction style.', instructions: 'Use a clear, calm style.', createdAt: new Date().toISOString() }, assignedAgentCount: 0 }] } }));

  await page.goto('/agents/new');
  await page.getByLabel(/display name/i).fill('Draft agent');
  await page.getByLabel(/purpose/i).fill('A draft that should remain visible.');
  await page.getByRole('button', { name: 'Continue' }).click();
  await expect(page.getByRole('option', { name: 'Neutral · revision 1' })).toBeAttached();
  await page.getByRole('combobox', { name: 'Persona revision', exact: true }).selectOption({ label: 'Neutral · revision 1' });
  await page.getByRole('button', { name: 'Continue' }).click();
  await page.getByLabel(/behavioral guidance/i).fill('Do not discard this draft.');
  await page.getByRole('button', { name: 'Continue' }).click();
  await page.getByRole('button', { name: 'Continue' }).click();
  await page.getByRole('button', { name: 'Continue' }).click();
  await page.getByRole('button', { name: 'Publish agent' }).click();
  await expect(page).toHaveURL(/\/agents\/new$/);
  await expect(page.getByRole('heading', { name: 'Review and publish' })).toBeVisible();
  await expect(page.getByText('Draft agent')).toBeVisible();

  await page.goto('/personas/new');
  await page.getByLabel(/display name/i).fill('Draft persona');
  await page.getByLabel(/description/i).fill('A draft that should remain visible.');
  await page.getByRole('button', { name: 'Continue' }).click();
  await page.getByLabel(/behavioral guidance/i).fill('Do not discard this draft.');
  await page.getByRole('button', { name: 'Continue' }).click();
  await page.getByRole('button', { name: 'Continue' }).click();
  await page.getByRole('button', { name: 'Publish persona' }).click();
  await expect(page).toHaveURL(/\/personas\/new$/);
  await expect(page.getByRole('heading', { name: 'Review and publish' })).toBeVisible();
  await expect(page.getByText('Draft persona')).toBeVisible();
});

test('confirms full-transcript sharing and renders an assignment transition marker', async ({ page }) => {
  await page.goto('/conversation/welcome');
  await page.getByRole('button', { name: 'Conversation options' }).click();
  const options = page.getByRole('dialog', { name: 'Conversation options' });
  const picker = options.getByLabel('Agent');
  await expect(picker).toBeVisible();
  await picker.selectOption('agent-aura-r2');
  await options.getByRole('button', { name: 'Apply changes' }).click();
  const confirmation = page.locator('.agent-confirm[role="dialog"]');
  await expect(confirmation).toContainText(/full completed transcript/i);
  await confirmation.getByRole('button', { name: 'Confirm change' }).click();
  await expect(page.getByText(/agent changed/i)).toBeVisible();
  await page.getByRole('button', { name: 'Conversation options' }).click();
  const reopenedOptions = page.getByRole('dialog', { name: 'Conversation options' });
  const reopenedPicker = reopenedOptions.getByLabel('Agent');
  await reopenedPicker.selectOption('agent-researcher-r1');
  await reopenedOptions.getByRole('button', { name: 'Apply changes' }).click();
  await expect(confirmation).toContainText(/full completed transcript/i);
  await confirmation.getByRole('button', { name: 'Confirm change' }).click();
  await expect(page.getByText(/agent changed/i)).toBeVisible();
});

test('configures an immutable read-only memory tool policy with accessible controls', async ({ page }) => {
  await page.goto('/agents/agent-aura/tools');
  await expect(page.getByRole('heading', { name: 'Tool policy' })).toBeVisible();
  const search = page.getByRole('checkbox', { name: /search memory/i });
  await expect(search).toBeChecked();
  await search.uncheck();
  await page.getByRole('button', { name: /save and attach revision/i }).click();
  await expect(page.getByText(/revision 2/i)).toBeVisible();
  await expect(page.getByText(/no tools granted/i)).toBeVisible();
  const results = await new AxeBuilder({ page }).analyze();
  expect(results.violations).toEqual([]);
});

test('preserves a draft and offers an active replacement when the assigned agent is disabled', async ({ page }) => {
  await page.goto('/conversation/welcome?fixture=disabled');
  const composer = page.getByRole('textbox', { name: /message/i });
  await composer.fill('Keep this draft while I choose a replacement.');
  await expect(page.getByRole('heading', { name: 'This agent is disabled' })).toBeVisible();
  await expect(composer).toHaveValue('Keep this draft while I choose a replacement.');
  await expect(page.getByRole('button', { name: /send/i })).toBeDisabled();
  await page.getByRole('button', { name: 'Conversation options' }).click();
  const options = page.getByRole('dialog', { name: 'Conversation options' });
  await options.getByLabel('Agent').selectOption('agent-researcher-r1');
  await options.getByRole('button', { name: 'Apply changes' }).click();
  await page.locator('.agent-confirm[role="dialog"]').getByRole('button', { name: 'Confirm change' }).click();
  await expect(composer).toHaveValue('Keep this draft while I choose a replacement.');
  await expect(page.getByRole('button', { name: /send/i })).toBeEnabled();
});
