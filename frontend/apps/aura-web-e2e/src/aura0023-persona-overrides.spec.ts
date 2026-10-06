import { expect, test, type Locator, type Page, type Route } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';

const now = '2026-10-06T00:00:00.000Z';

type FixtureOptions = {
  readonly activeRun?: boolean;
  readonly conflict?: 'optimistic' | 'idempotency';
  readonly delayPatch?: boolean;
  readonly disabledAgent?: boolean;
  readonly multiConversation?: boolean;
  readonly pinnedLatestPersona?: boolean;
  readonly pinnedDisabledPersona?: boolean;
};

type FixtureState = {
  readonly patchBodies: Array<Record<string, unknown>>;
  readonly patchTargets: string[];
  readonly createBodies: Array<Record<string, unknown>>;
  readonly runBodies: Array<Record<string, unknown>>;
  readonly getSummary: () => Record<string, unknown>;
  readonly releasePatch: () => void;
};

const neutral = {
  id: 'persona-neutral-r1',
  profileId: 'persona-neutral',
  revision: 1,
  displayName: 'Neutral',
  description: 'Clear and balanced.',
  instructions: 'Use direct, calm language.',
  status: 'active' as const,
  createdAt: now,
};

const vivid = {
  id: 'persona-vivid-r1',
  profileId: 'persona-vivid',
  revision: 1,
  displayName: 'Vivid',
  description: 'Energetic and expressive.',
  instructions: 'Use vivid examples and energetic language.',
  status: 'active' as const,
  createdAt: now,
};

const vividR2 = {
  ...vivid,
  id: 'persona-vivid-r2',
  revision: 2,
  instructions: 'Use vivid examples with a measured pace.',
};

const disabled = {
  id: 'persona-disabled-r1',
  profileId: 'persona-disabled',
  revision: 1,
  displayName: 'Archived',
  description: 'A previously used style.',
  instructions: 'Keep the historical style.',
  status: 'disabled' as const,
  createdAt: now,
};

const auraR1 = {
  id: 'agent-aura-r1',
  profileId: 'agent-aura',
  revision: 1,
  displayName: 'Aura',
  purpose: 'A steady thinking partner.',
  instructions: 'Be kind and practical.',
  personaRevisionId: neutral.id,
  promptBundleRevisionId: 'bundle-aura-r1',
  modelPolicyRevisionId: 'policy-aura',
  createdAt: now,
};

const auraR2 = {
  ...auraR1,
  id: 'agent-aura-r2',
  revision: 2,
  purpose: 'A focused thinking partner.',
  instructions: 'Be precise and action-oriented.',
  personaRevisionId: vividR2.id,
  promptBundleRevisionId: 'bundle-aura-r2',
};

const researcherR1 = {
  id: 'agent-researcher-r1',
  profileId: 'agent-researcher',
  revision: 1,
  displayName: 'Researcher',
  purpose: 'A careful research assistant.',
  instructions: 'Cite assumptions and distinguish evidence.',
  personaRevisionId: neutral.id,
  promptBundleRevisionId: 'bundle-researcher-r1',
  modelPolicyRevisionId: 'policy-researcher',
  createdAt: now,
};

const disabledAgentR1 = {
  ...auraR1,
  id: 'agent-disabled-r1',
  profileId: 'agent-disabled',
  displayName: 'Disabled Agent',
};

type PersonaRevisionFixture = typeof neutral | typeof vivid | typeof vividR2 | typeof disabled;
type AgentRevisionFixture = typeof auraR1 | typeof auraR2 | typeof researcherR1;

const personaReference = (revision: PersonaRevisionFixture): Record<string, unknown> => ({
  profileId: revision.profileId,
  revisionId: revision.id,
  revision: revision.revision,
  displayName: revision.displayName,
  status: revision.status,
  newerRevisionAvailable: false,
});

const agentReference = (revision: AgentRevisionFixture, status: 'active' | 'disabled' = revision.profileId === 'agent-disabled' ? 'disabled' : 'active'): Record<string, unknown> => ({
  profileId: revision.profileId,
  revisionId: revision.id,
  revision: revision.revision,
  displayName: revision.displayName,
  status,
  newerRevisionAvailable: revision.id === auraR1.id,
});

const personaPayload = (revision: PersonaRevisionFixture): Record<string, unknown> => ({
  id: revision.id,
  profileId: revision.profileId,
  revision: revision.revision,
  displayName: revision.displayName,
  description: revision.description,
  instructions: revision.instructions,
  createdAt: revision.createdAt,
});

const agentPayload = (revision: AgentRevisionFixture): Record<string, unknown> => ({
  ...revision,
  instructions: revision.instructions,
});

async function installPersonaOverrideFixture(page: Page, options: FixtureOptions = {}): Promise<FixtureState> {
  const patchBodies: Array<Record<string, unknown>> = [];
  const patchTargets: string[] = [];
  const createBodies: Array<Record<string, unknown>> = [];
  const runBodies: Array<Record<string, unknown>> = [];
  let releasePatch: () => void = () => undefined;
  const patchReleased = options.delayPatch
    ? new Promise<void>((resolve) => {
        releasePatch = resolve;
      })
    : Promise.resolve();
  let version = 1;
  let selectedAgent: AgentRevisionFixture = options.disabledAgent ? disabledAgentR1 : auraR1;
  let selectedPersona: PersonaRevisionFixture = options.pinnedDisabledPersona ? disabled : options.pinnedLatestPersona ? vividR2 : neutral;
  let personaOverride = Boolean(options.pinnedDisabledPersona || options.pinnedLatestPersona);
  let assignmentNumber = 1;
  let personaAssignmentNumber = 1;
  let currentRun: Record<string, unknown> | null = options.activeRun
    ? { id: 'run-active', conversationId: 'welcome', userMessageId: 'user-1', assistantMessageId: 'assistant-active', status: 'running', agentRevisionId: selectedAgent.id, personaRevisionId: selectedPersona.id, modelPolicyRevisionId: selectedAgent.modelPolicyRevisionId, promptBundleRevisionId: selectedAgent.promptBundleRevisionId, provider: 'fixture', modelId: 'fixture-chat', retryOfRunId: null, createdAt: now, startedAt: now, finishedAt: null, error: null }
    : null;

  const allPersonas: PersonaRevisionFixture[] = [neutral, vivid, vividR2, disabled];
  const allAgents: AgentRevisionFixture[] = [auraR1, auraR2, researcherR1, disabledAgentR1];
  const defaultPersonaFor = (agent: AgentRevisionFixture): PersonaRevisionFixture => allPersonas.find((item) => item.id === agent.personaRevisionId) ?? neutral;
  const activePersona = (): PersonaRevisionFixture => allPersonas.find((item) => item.id === selectedPersona.id) ?? neutral;
  const assignmentAgent = (): Record<string, unknown> => agentReference(selectedAgent);
  const assignmentPersona = (): Record<string, unknown> => personaReference(activePersona());
  const agentAssignments: Array<Record<string, unknown>> = [{ id: 'agent-assignment-1', agent: assignmentAgent(), reason: 'initial', effectiveAfterMessageId: null, createdAt: now }];
  const personaAssignments: Array<Record<string, unknown>> = [{ id: 'persona-assignment-1', persona: assignmentPersona(), source: personaOverride ? 'conversation_override' : 'agent_default', reason: 'initial', effectiveAfterMessageId: null, createdAt: now }];

  const summary = (id = 'welcome'): Record<string, unknown> => ({
    id,
    title: id === 'welcome' ? 'A thoughtful beginning' : 'Second conversation',
    agentProfileId: selectedAgent.profileId,
    agentRevisionId: selectedAgent.id,
    agent: assignmentAgent(),
    agentAssignments,
    persona: personaReference(activePersona()),
    personaOverride,
    personaAssignments,
    modelId: 'fixture-chat',
    version,
    createdAt: now,
    updatedAt: now,
    currentRun,
  });

  const detail = (id = 'welcome'): Record<string, unknown> => ({
    ...summary(id),
    messages: [
      { id: 'user-1', conversationId: id, role: 'user', content: 'Earlier completed question.', state: 'complete', runId: 'run-1', createdAt: now, updatedAt: now },
      { id: 'assistant-1', conversationId: id, role: 'assistant', content: 'Earlier completed answer.', state: 'complete', runId: 'run-1', createdAt: now, updatedAt: now },
      { id: 'assistant-partial', conversationId: id, role: 'assistant', content: 'A partial answer that is not reusable context.', state: 'partial', runId: 'run-partial', createdAt: now, updatedAt: now },
      { id: 'assistant-interrupted', conversationId: id, role: 'assistant', content: 'An interrupted answer that is not reusable context.', state: 'interrupted', runId: 'run-interrupted', createdAt: now, updatedAt: now },
    ],
    recentRuns: currentRun ? [currentRun] : [],
  });

  const updateAssignmentState = (body: Record<string, unknown>): void => {
    const afterMessageId = 'assistant-1';
    if (typeof body.agentRevisionId === 'string') {
      const replacement = allAgents.find((revision) => revision.id === body.agentRevisionId);
      if (replacement) {
        const reason = replacement.profileId === selectedAgent.profileId && replacement.revision > selectedAgent.revision ? 'revision_upgrade' : 'manual_switch';
        selectedAgent = replacement;
        agentAssignments.push({ id: `agent-assignment-${++assignmentNumber}`, agent: assignmentAgent(), reason, effectiveAfterMessageId: afterMessageId, createdAt: now });
        if (!personaOverride) selectedPersona = defaultPersonaFor(replacement);
      }
    }
    if (typeof body.personaRevisionId === 'string') {
      const replacement = allPersonas.find((revision) => revision.id === body.personaRevisionId);
      if (replacement) {
        selectedPersona = replacement;
        personaOverride = true;
        personaAssignments.push({ id: `persona-assignment-${++personaAssignmentNumber}`, persona: personaReference(replacement), source: 'conversation_override', reason: 'manual_override', effectiveAfterMessageId: afterMessageId, createdAt: now });
      }
    }
    if (body.useAgentDefaultPersona === true) {
      selectedPersona = defaultPersonaFor(selectedAgent);
      personaOverride = false;
      personaAssignments.push({ id: `persona-assignment-${++personaAssignmentNumber}`, persona: personaReference(selectedPersona), source: 'agent_default', reason: 'reset_to_agent_default', effectiveAfterMessageId: afterMessageId, createdAt: now });
    } else if (typeof body.agentRevisionId === 'string' && !body.personaRevisionId && personaOverride === false) {
      personaAssignments.push({ id: `persona-assignment-${++personaAssignmentNumber}`, persona: personaReference(selectedPersona), source: 'agent_default', reason: 'agent_revision_upgrade', effectiveAfterMessageId: afterMessageId, createdAt: now });
    }
  };

  await page.route('**/api/**', async (route: Route) => {
    const request = route.request();
    const url = new URL(request.url());
    const method = request.method();
    if (url.pathname === '/api/v1/auth/session') return route.fulfill({ json: { principal: { issuer: 'https://issuer.test', subject: 'owner', displayName: 'Owner' }, csrfToken: 'csrf-test', idleExpiresAt: now, absoluteExpiresAt: now } });
    if (url.pathname === '/api/v1/models') return route.fulfill({ json: { models: [{ id: 'fixture-chat', displayName: 'Fixture Chat', provider: 'fixture', capabilities: ['chat'], availability: 'available', selectable: true, disabledReason: null }], defaultModelId: 'fixture-chat', observedAt: now } });
    if (url.pathname === '/api/v1/agents' && method === 'GET') return route.fulfill({ json: { items: [{ id: 'agent-aura', status: 'active', version: 2, currentRevision: agentPayload(auraR2), createdAt: now, updatedAt: now }, { id: 'agent-researcher', status: 'active', version: 1, currentRevision: agentPayload(researcherR1), createdAt: now, updatedAt: now }, { id: 'agent-disabled', status: 'disabled', version: 2, currentRevision: agentPayload(disabledAgentR1), createdAt: now, updatedAt: now }] } });
    if (url.pathname === '/api/v1/agents/agent-aura' && method === 'GET') return route.fulfill({ json: { id: 'agent-aura', status: 'active', version: 2, currentRevision: agentPayload(auraR2), revisions: [agentPayload(auraR1), agentPayload(auraR2)], createdAt: now, updatedAt: now } });
    if (url.pathname === '/api/v1/agents/agent-researcher' && method === 'GET') return route.fulfill({ json: { id: 'agent-researcher', status: 'active', version: 1, currentRevision: agentPayload(researcherR1), revisions: [agentPayload(researcherR1)], createdAt: now, updatedAt: now } });
    if (url.pathname === '/api/v1/agents/agent-disabled' && method === 'GET') return route.fulfill({ json: { id: 'agent-disabled', status: 'disabled', version: 2, currentRevision: agentPayload(disabledAgentR1), revisions: [agentPayload(disabledAgentR1)], createdAt: now, updatedAt: now } });
    if (url.pathname === '/api/v1/personas' && method === 'GET') return route.fulfill({ json: { items: [{ id: 'persona-neutral', status: 'active', version: 1, currentRevision: personaPayload(neutral), createdAt: now, updatedAt: now }, { id: 'persona-vivid', status: 'active', version: 2, currentRevision: personaPayload(vividR2), createdAt: now, updatedAt: now }, { id: 'persona-disabled', status: 'disabled', version: 2, currentRevision: personaPayload(disabled), createdAt: now, updatedAt: now }] } });
    if (url.pathname === '/api/v1/personas/persona-neutral' && method === 'GET') return route.fulfill({ json: { id: 'persona-neutral', status: 'active', version: 1, currentRevision: personaPayload(neutral), revisions: [personaPayload(neutral)], createdAt: now, updatedAt: now } });
    if (url.pathname === '/api/v1/personas/persona-vivid' && method === 'GET') return route.fulfill({ json: { id: 'persona-vivid', status: 'active', version: 2, currentRevision: personaPayload(vividR2), revisions: [personaPayload(vivid), personaPayload(vividR2)], createdAt: now, updatedAt: now } });
    if (url.pathname === '/api/v1/personas/persona-disabled' && method === 'GET') return route.fulfill({ json: { id: 'persona-disabled', status: 'disabled', version: 2, currentRevision: personaPayload(disabled), revisions: [personaPayload(disabled)], createdAt: now, updatedAt: now } });
    if (url.pathname === '/api/v1/conversations' && method === 'GET') return route.fulfill({ json: { items: options.multiConversation ? [summary(), summary('second')] : [summary()], nextCursor: null } });
    if (url.pathname === '/api/v1/conversations' && method === 'POST') {
      const body = JSON.parse(request.postData() ?? '{}') as Record<string, unknown>;
      createBodies.push(body);
      const created = summary('created-1');
      const userMessage = { id: 'user-created', conversationId: 'created-1', role: 'user', content: String(body.message ?? ''), state: 'complete', runId: 'run-created', createdAt: now, updatedAt: now };
      const run = { id: 'run-created', conversationId: 'created-1', userMessageId: userMessage.id, assistantMessageId: 'assistant-created', status: 'queued', agentRevisionId: String(body.agentRevisionId ?? selectedAgent.id), personaRevisionId: String(body.personaRevisionId ?? selectedPersona.id), modelPolicyRevisionId: selectedAgent.modelPolicyRevisionId, promptBundleRevisionId: selectedAgent.promptBundleRevisionId, provider: 'fixture', modelId: 'fixture-chat', retryOfRunId: null, createdAt: now, startedAt: null, finishedAt: null, error: null };
      return route.fulfill({ status: 202, json: { conversation: { ...created, currentRun: run }, userMessage, run } });
    }
    if (url.pathname === '/api/v1/conversations/welcome' && method === 'GET') return route.fulfill({ json: detail() });
    if (url.pathname === '/api/v1/conversations/welcome' && method === 'PATCH') {
      const body = JSON.parse(request.postData() ?? '{}') as Record<string, unknown>;
      patchBodies.push(body);
      patchTargets.push(url.pathname);
      if (options.activeRun) return route.fulfill({ status: 409, json: { detail: 'Wait for the active run to finish before changing conversation configuration.' } });
      if (options.conflict === 'optimistic') return route.fulfill({ status: 409, json: { detail: 'This conversation changed elsewhere. Refresh before applying this configuration.' } });
      if (options.conflict === 'idempotency') return route.fulfill({ status: 409, json: { detail: 'This idempotency key conflicts with an earlier request.' } });
      if (body.personaRevisionId === disabled.id) return route.fulfill({ status: 409, json: { detail: 'Disabled personas cannot be selected for new configuration.' } });
      if (body.transcriptSharingConfirmed !== true && (body.personaRevisionId || body.agentRevisionId || body.useAgentDefaultPersona)) return route.fulfill({ status: 409, json: { detail: 'The full completed transcript will be shared with the selected configuration.' } });
      if (options.delayPatch) await patchReleased;
      updateAssignmentState(body);
      version += 1;
      return route.fulfill({ json: summary() });
    }
    if (url.pathname === '/api/v1/conversations/welcome/runs' && method === 'POST') {
      const body = JSON.parse(request.postData() ?? '{}') as Record<string, unknown>;
      runBodies.push(body);
      if (options.activeRun) return route.fulfill({ status: 409, json: { detail: 'An active run already exists.' } });
      const run = { id: `run-${runBodies.length + 1}`, conversationId: 'welcome', userMessageId: `user-${runBodies.length + 1}`, assistantMessageId: null, status: 'queued', agentRevisionId: selectedAgent.id, personaRevisionId: selectedPersona.id, modelPolicyRevisionId: selectedAgent.modelPolicyRevisionId, promptBundleRevisionId: selectedAgent.promptBundleRevisionId, provider: 'fixture', modelId: 'fixture-chat', retryOfRunId: null, createdAt: now, startedAt: null, finishedAt: null, error: null };
      currentRun = run;
      return route.fulfill({ status: 202, json: { conversation: summary(), userMessage: { id: run.userMessageId, conversationId: 'welcome', role: 'user', content: String(body.message ?? ''), state: 'complete', runId: run.id, createdAt: now, updatedAt: now }, run } });
    }
    const detailMatch = url.pathname.match(/^\/api\/v1\/conversations\/([^/]+)$/);
    if (detailMatch && method === 'GET') return route.fulfill({ json: detail(decodeURIComponent(detailMatch[1])) });
    if (url.pathname.endsWith('/events')) return route.fulfill({ status: 200, headers: { 'content-type': 'text/event-stream' }, body: '' });
    return route.continue();
  });

  return { patchBodies, patchTargets, createBodies, runBodies, getSummary: () => summary(), releasePatch };
}

function conversationOptionsButton(page: Page) {
  return page.getByRole('button', { name: /conversation options/i });
}

function conversationOptionsPopover(page: Page): Locator {
  return page.locator('[aria-label="Conversation options"]').filter({ has: page.locator('select') }).first();
}

async function ensureConversationOptionsMenu(page: Page): Promise<Locator> {
  const menu = conversationOptionsPopover(page);
  if (!(await menu.isVisible().catch(() => false))) await conversationOptionsButton(page).click();
  await expect(menu).toBeVisible();
  await expect(menu).toHaveAttribute('role', /^(?:dialog|group)$/);
  return menu;
}

function personaPicker(scope: Page | Locator) {
  return scope.getByLabel(/persona/i).first();
}

function agentPicker(scope: Page | Locator) {
  return scope.getByLabel(/agent/i).first();
}

async function confirmConfiguration(page: Page): Promise<void> {
  const dialog = page.getByRole('dialog');
  await expect(dialog).toContainText(/full completed transcript/i);
  await dialog.getByRole('button', { name: /confirm|continue|switch|apply/i }).click();
}

async function applyPendingConfiguration(scope: Locator): Promise<void> {
  const apply = scope.getByRole('button', { name: /^apply(?: changes)?$/i });
  if (await apply.count()) await apply.click();
}

async function resetToAgentDefault(page: Page): Promise<void> {
  const menu = await ensureConversationOptionsMenu(page);
  const reset = menu.getByRole('button', { name: /use agent default|reset.*default/i });
  if (await reset.count()) {
    await reset.first().click();
    return;
  }
  const picker = personaPicker(menu);
  const option = picker.locator('option').filter({ hasText: /agent default|use default/i }).first();
  await expect(option).toHaveCount(1);
  await picker.selectOption({ label: await option.textContent() ?? '' });
}

test('selects a persona on a new draft and sends the selected revision in create payload', async ({ page }) => {
  const fixture = await installPersonaOverrideFixture(page);
  await page.goto('/conversation');
  const menu = await ensureConversationOptionsMenu(page);
  await expect(personaPicker(menu)).toBeVisible();
  await personaPicker(menu).selectOption('persona-vivid-r1');
  await page.getByRole('textbox', { name: /message aura/i }).fill('Start this conversation in a vivid style.');
  await page.getByRole('button', { name: /send/i }).click();
  await expect.poll(() => fixture.createBodies.length).toBe(1);
  expect(fixture.createBodies[0]?.personaRevisionId).toBe('persona-vivid-r1');
});

test('confirms full completed-transcript sharing before changing a persisted persona', async ({ page }) => {
  const fixture = await installPersonaOverrideFixture(page);
  await page.goto('/conversation/welcome');
  const menu = await ensureConversationOptionsMenu(page);
  await personaPicker(menu).selectOption('persona-vivid-r1');
  await applyPendingConfiguration(menu);
  await confirmConfiguration(page);
  await expect.poll(() => fixture.patchBodies.length).toBe(1);
  expect(fixture.patchBodies[0]).toMatchObject({ personaRevisionId: 'persona-vivid-r1', transcriptSharingConfirmed: true, version: 1 });
  await expect(page.getByText(/vivid/i).first()).toBeVisible();
});

test('resets an explicit persona override to the selected agent default', async ({ page }) => {
  const fixture = await installPersonaOverrideFixture(page);
  await page.goto('/conversation/welcome');
  const menu = await ensureConversationOptionsMenu(page);
  await personaPicker(menu).selectOption('persona-vivid-r1');
  await applyPendingConfiguration(menu);
  await confirmConfiguration(page);
  await resetToAgentDefault(page);
  await confirmConfiguration(page);
  await expect.poll(() => fixture.patchBodies.length).toBe(2);
  expect(fixture.patchBodies[1]).toMatchObject({ useAgentDefaultPersona: true, transcriptSharingConfirmed: true, version: 2 });
  expect(fixture.getSummary().personaOverride).toBe(false);
  expect((fixture.getSummary().persona as Record<string, unknown>).revisionId).toBe(neutral.id);
});

test('keeps an explicit persona override when switching agents', async ({ page }) => {
  const fixture = await installPersonaOverrideFixture(page);
  await page.goto('/conversation/welcome');
  let menu = await ensureConversationOptionsMenu(page);
  await personaPicker(menu).selectOption('persona-vivid-r1');
  await applyPendingConfiguration(menu);
  await confirmConfiguration(page);
  menu = await ensureConversationOptionsMenu(page);
  await agentPicker(menu).selectOption('agent-researcher-r1');
  await applyPendingConfiguration(menu);
  await confirmConfiguration(page);
  await expect.poll(() => fixture.patchBodies.length).toBe(2);
  expect(fixture.patchBodies[1]).toMatchObject({ agentRevisionId: 'agent-researcher-r1', transcriptSharingConfirmed: true });
  expect(fixture.getSummary().personaOverride).toBe(true);
  expect((fixture.getSummary().persona as Record<string, unknown>).revisionId).toBe(vivid.id);
});

test('adopts a new agent default when no persona override exists', async ({ page }) => {
  const fixture = await installPersonaOverrideFixture(page);
  await page.goto('/conversation/welcome');
  const menu = await ensureConversationOptionsMenu(page);
  await agentPicker(menu).selectOption('agent-aura-r2');
  await applyPendingConfiguration(menu);
  await confirmConfiguration(page);
  await expect.poll(() => fixture.patchBodies.length).toBe(1);
  expect(fixture.patchBodies[0]).toMatchObject({ agentRevisionId: 'agent-aura-r2', transcriptSharingConfirmed: true });
  expect(fixture.getSummary().personaOverride).toBe(false);
  expect((fixture.getSummary().persona as Record<string, unknown>).revisionId).toBe(vividR2.id);
});

test('renders one transition marker for an atomic agent and persona boundary', async ({ page }) => {
  const fixture = await installPersonaOverrideFixture(page);
  await page.goto('/conversation/welcome');
  const menu = await ensureConversationOptionsMenu(page);
  await agentPicker(menu).selectOption('agent-researcher-r1');
  await personaPicker(menu).selectOption('persona-vivid-r1');
  await menu.getByRole('button', { name: /^apply(?: changes)?$/i }).click();
  await confirmConfiguration(page);
  await expect.poll(() => fixture.patchBodies.length).toBe(1);
  expect(fixture.patchBodies[0]).toMatchObject({ agentRevisionId: 'agent-researcher-r1', personaRevisionId: 'persona-vivid-r1', transcriptSharingConfirmed: true });
  const markers = page.getByRole('status').filter({ hasText: /changed/i });
  await expect(markers).toHaveCount(1);
  await expect(markers.first()).toContainText(/agent/i);
  await expect(markers.first()).toContainText(/persona|vivid/i);
});

test('blocks persona changes while a run is active', async ({ page }) => {
  const fixture = await installPersonaOverrideFixture(page, { activeRun: true });
  await page.goto('/conversation/welcome');
  await expect(conversationOptionsButton(page)).toBeDisabled();
  await expect(conversationOptionsPopover(page)).toBeHidden();
  expect(fixture.patchBodies).toHaveLength(0);
});

test('omits disabled personas from new selection while allowing a pinned disabled persona to continue', async ({ page }) => {
  const fixture = await installPersonaOverrideFixture(page, { pinnedDisabledPersona: true });
  await page.goto('/conversation/welcome');
  const menu = await ensureConversationOptionsMenu(page);
  await expect(page.getByText(/archived|disabled/i).first()).toBeVisible();
  const disabledOption = personaPicker(menu).locator('option[value="persona-disabled-r1"]');
  if (await disabledOption.count()) await expect(disabledOption).toBeDisabled();
  const composer = page.getByRole('textbox', { name: /message aura/i });
  await composer.fill('Continue using the pinned historical persona.');
  await page.getByRole('button', { name: /send/i }).click();
  await expect.poll(() => fixture.runBodies.length).toBe(1);
  expect(fixture.runBodies[0]?.message).toBe('Continue using the pinned historical persona.');
  expect(fixture.runBodies[0]?.personaRevisionId ?? fixture.getSummary().persona).toBeTruthy();
});

test('preserves the draft after optimistic and idempotency configuration conflicts', async ({ page }) => {
  for (const conflict of ['optimistic', 'idempotency'] as const) {
    await page.unroute('**/api/**');
    const fixture = await installPersonaOverrideFixture(page, { conflict });
    await page.goto('/conversation/welcome');
    const composer = page.getByRole('textbox', { name: /message aura/i });
    await composer.fill(`Keep this draft after the ${conflict} conflict.`);
    const menu = await ensureConversationOptionsMenu(page);
    await personaPicker(menu).selectOption('persona-vivid-r1');
    await applyPendingConfiguration(menu);
    await confirmConfiguration(page);
    await expect.poll(() => fixture.patchBodies.length).toBe(1);
    await expect(composer).toHaveValue(`Keep this draft after the ${conflict} conflict.`);
    await expect(page.getByRole('alert')).toContainText(/changed elsewhere|idempotency|draft/i);
  }
});

test('retains the direct conversation route while changing persona and survives reload', async ({ page }) => {
  const fixture = await installPersonaOverrideFixture(page);
  await page.goto('/conversation/welcome');
  const menu = await ensureConversationOptionsMenu(page);
  await personaPicker(menu).selectOption('persona-vivid-r1');
  await applyPendingConfiguration(menu);
  await confirmConfiguration(page);
  await expect.poll(() => fixture.patchBodies.length).toBe(1);
  expect(new URL(page.url()).pathname).toBe('/conversation/welcome');
  await page.reload();
  const menuAfterReload = await ensureConversationOptionsMenu(page);
  await expect(personaPicker(menuAfterReload)).toHaveValue('persona-vivid-r1');
  expect(new URL(page.url()).pathname).toBe('/conversation/welcome');
});

test('keeps the configuration surface keyboard accessible and responsive', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/conversation/welcome');
  const menu = await ensureConversationOptionsMenu(page);
  await expect(personaPicker(menu)).toBeVisible();
  await expect(agentPicker(menu)).toBeVisible();
  await personaPicker(menu).focus();
  await expect(personaPicker(menu)).toBeFocused();
  const results = await new AxeBuilder({ page }).include('section.conversation').analyze();
  expect(results.violations).toEqual([]);
});

test('replaces the large configuration card with a compact conversation summary and supported options menu', async ({ page }) => {
  await installPersonaOverrideFixture(page);
  await page.goto('/conversation/welcome');
  await expect(page.locator('.agent-context')).toHaveCount(0);
  await expect(page.getByText(/Aura r1/i).first()).toBeVisible();
  await expect(page.getByText(/Neutral r1/i).first()).toBeVisible();

  const menu = await ensureConversationOptionsMenu(page);
  await expect(menu).toContainText(/model/i);
  await expect(menu).toContainText(/agent/i);
  await expect(menu).toContainText(/persona/i);
  await expect(menu).toContainText(/use agent default/i);
  await expect(menu).not.toContainText(/attachment|attach|tool|memory|voice|delegat|upload|avatar/i);
});

test('opens and closes the conversation options menu with keyboard focus return', async ({ page }) => {
  await installPersonaOverrideFixture(page);
  await page.goto('/conversation/welcome');
  const options = conversationOptionsButton(page);
  await options.focus();
  await options.press('ArrowDown');
  const menu = conversationOptionsPopover(page);
  await expect(menu).toBeVisible();
  await page.keyboard.press('Tab');
  expect(await menu.evaluate((element) => element.contains(document.activeElement))).toBe(true);
  await page.keyboard.press('Escape');
  await expect(menu).toBeHidden();
  await expect(options).toBeFocused();
});

test('supports keyboard navigation and visible focus for every supported configuration control', async ({ page }) => {
  await installPersonaOverrideFixture(page);
  await page.goto('/conversation/welcome');
  const menu = await ensureConversationOptionsMenu(page);
  const controls = menu.locator('select, button').filter({ hasNotText: /close/i });
  await expect(controls).not.toHaveCount(0);
  await controls.first().focus();
  await expect(controls.first()).toBeFocused();
  const focusStyle = await controls.first().evaluate((element) => {
    const style = getComputedStyle(element);
    return { outlineStyle: style.outlineStyle, outlineWidth: style.outlineWidth, boxShadow: style.boxShadow };
  });
  expect(focusStyle.outlineStyle !== 'none' || focusStyle.outlineWidth !== '0px' || focusStyle.boxShadow !== 'none').toBe(true);
  await page.keyboard.press('Tab');
  expect(await menu.evaluate((element) => element.contains(document.activeElement))).toBe(true);
});

test('changes both agent and persona from the menu with transcript confirmation', async ({ page }) => {
  const fixture = await installPersonaOverrideFixture(page);
  await page.goto('/conversation/welcome');
  const menu = await ensureConversationOptionsMenu(page);
  await agentPicker(menu).selectOption('agent-researcher-r1');
  await personaPicker(menu).selectOption('persona-vivid-r1');
  await menu.getByRole('button', { name: /^apply(?: changes)?$/i }).click();
  await confirmConfiguration(page);
  await expect.poll(() => fixture.patchBodies.length).toBe(1);
  expect(fixture.patchBodies[0]).toMatchObject({ agentRevisionId: 'agent-researcher-r1', personaRevisionId: 'persona-vivid-r1', transcriptSharingConfirmed: true });
});

test('focuses confirmation, cancels on Escape, and restores focus after cancel and completion', async ({ page }) => {
  const fixture = await installPersonaOverrideFixture(page);
  await page.goto('/conversation/welcome');
  let menu = await ensureConversationOptionsMenu(page);
  await personaPicker(menu).selectOption('persona-vivid-r1');
  await applyPendingConfiguration(menu);
  const dialog = page.getByRole('dialog');
  await expect(dialog).toContainText(/full completed transcript/i);
  expect(await dialog.evaluate((element) => element.contains(document.activeElement))).toBe(true);
  await page.keyboard.press('Escape');
  await expect(dialog).toBeHidden();
  expect(fixture.patchBodies).toHaveLength(0);
  await expect(conversationOptionsButton(page)).toBeFocused();

  menu = await ensureConversationOptionsMenu(page);
  await personaPicker(menu).selectOption('persona-vivid-r1');
  await applyPendingConfiguration(menu);
  await expect(page.getByRole('dialog')).toBeVisible();
  await page.getByRole('dialog').getByRole('button', { name: /confirm|apply/i }).click();
  await expect.poll(() => fixture.patchBodies.length).toBe(1);
  await expect(page.getByRole('dialog')).toBeHidden();
  await expect(conversationOptionsButton(page)).toBeFocused();
});

test('disables the conversation options trigger while a run is active', async ({ page }) => {
  await installPersonaOverrideFixture(page, { activeRun: true });
  await page.goto('/conversation/welcome');
  const options = conversationOptionsButton(page);
  await expect(options).toBeDisabled();
  await expect(conversationOptionsPopover(page)).toBeHidden();
});

test('closes an open popover when a run starts and prevents configuration requests during the run', async ({ page }) => {
  const fixture = await installPersonaOverrideFixture(page);
  await page.goto('/conversation/welcome');
  await ensureConversationOptionsMenu(page);
  const composer = page.getByRole('textbox', { name: /message aura/i });
  await composer.fill('Start a run while the options popover is open.');
  await composer.press('Enter');
  await expect.poll(() => fixture.runBodies.length).toBe(1);
  await expect(conversationOptionsPopover(page)).toBeHidden();
  await expect(conversationOptionsButton(page)).toBeDisabled();
  expect(fixture.patchBodies).toHaveLength(0);
});

test('recovers from a disabled agent through the menu without losing the draft', async ({ page }) => {
  await installPersonaOverrideFixture(page, { disabledAgent: true });
  await page.goto('/conversation/welcome');
  const composer = page.getByRole('textbox', { name: /message aura/i });
  await composer.fill('Keep this draft while I replace the disabled agent.');
  await expect(page.getByText(/disabled agent|choose an active replacement/i).first()).toBeVisible();
  await expect(page.getByRole('button', { name: /send/i })).toBeDisabled();
  const menu = await ensureConversationOptionsMenu(page);
  await agentPicker(menu).selectOption('agent-researcher-r1');
  await applyPendingConfiguration(menu);
  await confirmConfiguration(page);
  await expect(composer).toHaveValue('Keep this draft while I replace the disabled agent.');
  await expect(page.getByRole('button', { name: /send/i })).toBeEnabled();
});

test('keeps a disabled pinned persona available for continuation while excluding it from new menu choices', async ({ page }) => {
  const fixture = await installPersonaOverrideFixture(page, { pinnedDisabledPersona: true });
  await page.goto('/conversation/welcome');
  const menu = await ensureConversationOptionsMenu(page);
  const picker = personaPicker(menu);
  const disabledOption = picker.locator('option[value="persona-disabled-r1"]');
  if (await disabledOption.count()) await expect(disabledOption).toBeDisabled();
  const composer = page.getByRole('textbox', { name: /message aura/i });
  await composer.fill('Continue with the pinned persona.');
  await page.getByRole('button', { name: /send/i }).click();
  await expect.poll(() => fixture.runBodies.length).toBe(1);
  expect(fixture.runBodies[0]?.message).toBe('Continue with the pinned persona.');
});

test('shows a newer agent revision without adopting it automatically', async ({ page }) => {
  await installPersonaOverrideFixture(page);
  await page.goto('/conversation/welcome');
  const menu = await ensureConversationOptionsMenu(page);
  await expect(page.getByText(/newer revision available/i).first()).toBeVisible();
  await expect(menu).toContainText(/newer/i);
  await expect(agentPicker(menu)).toHaveValue('agent-aura-r1');
  const summary = page.locator('.configuration-summary');
  await expect(summary).toContainText(/Aura r1/i);
  await expect(summary).not.toContainText(/Aura r2/i);
});

test('labels only a same-profile higher agent revision as newer and preserves production API mapping', async ({ page }) => {
  await installPersonaOverrideFixture(page);
  await page.goto('/conversation/welcome');
  const menu = await ensureConversationOptionsMenu(page);
  await expect(menu.getByRole('option', { name: /Aura r2/i })).toContainText(/newer/i);
  await expect(menu.getByRole('option', { name: /Researcher r1/i })).not.toContainText(/newer/i);
  await expect(menu.getByRole('option', { name: /Vivid r2/i })).toBeVisible();
});

test('does not label an older persona revision newer when its latest pinned revision is active', async ({ page }) => {
  await installPersonaOverrideFixture(page, { pinnedLatestPersona: true });
  await page.goto('/conversation/welcome');
  const menu = await ensureConversationOptionsMenu(page);
  await expect(personaPicker(menu)).toHaveValue('persona-vivid-r2');
  await expect(menu.getByRole('option', { name: /Vivid r1/i })).not.toContainText(/newer/i);
  await expect(menu.getByRole('option', { name: /Vivid r2/i })).not.toContainText(/newer/i);
});

test('preserves a draft when the options menu closes outside the composer', async ({ page }) => {
  await installPersonaOverrideFixture(page);
  await page.goto('/conversation/welcome');
  const composer = page.getByRole('textbox', { name: /message aura/i });
  await composer.fill('Do not discard this draft when I close the menu.');
  await ensureConversationOptionsMenu(page);
  await page.locator('.transcript').click();
  await expect(conversationOptionsPopover(page)).toBeHidden();
  await ensureConversationOptionsMenu(page);
  await composer.click();
  await expect(conversationOptionsPopover(page)).toBeHidden();
  await expect(composer).toHaveValue('Do not discard this draft when I close the menu.');
});

test('keeps the compact menu inside the viewport and accessible on mobile and tablet layouts', async ({ page }) => {
  await installPersonaOverrideFixture(page);
  await page.goto('/conversation/welcome');
  const menu = await ensureConversationOptionsMenu(page);
  const box = await menu.boundingBox();
  const viewport = page.viewportSize();
  expect(box).not.toBeNull();
  expect(viewport).not.toBeNull();
  if (box && viewport) {
    expect(box.x).toBeGreaterThanOrEqual(0);
    expect(box.y).toBeGreaterThanOrEqual(0);
    expect(box.x + box.width).toBeLessThanOrEqual(viewport.width);
    expect(box.y + box.height).toBeLessThanOrEqual(viewport.height);
  }
  const results = await new AxeBuilder({ page }).include('section.conversation').analyze();
  expect(results.violations).toEqual([]);
});

test('cancels staged configuration when navigating from conversation A to B and preserves browser history', async ({ page }) => {
  const fixture = await installPersonaOverrideFixture(page, { multiConversation: true });
  await page.goto('/conversation/welcome');

  const menu = await ensureConversationOptionsMenu(page);
  await agentPicker(menu).selectOption('agent-researcher-r1');
  await personaPicker(menu).selectOption('persona-vivid-r1');
  await menu.getByRole('button', { name: /^apply(?: changes)?$/i }).click();

  const confirmation = page.getByRole('dialog');
  await expect(confirmation).toContainText(/full completed transcript/i);
  await expect.poll(() => fixture.patchBodies.length).toBe(0);

  // Persistent navigation must abandon the pending confirmation instead of applying
  // it to B or leaving a stale request for A.
  const secondConversationLink = page.getByRole('link', { name: /second conversation/i });
  await secondConversationLink.click();
  await expect(page).toHaveURL(/\/conversation\/second$/);
  await expect(page.getByRole('dialog')).toBeHidden();
  await expect(secondConversationLink).toBeFocused();
  await expect(page.getByText('Earlier completed question.')).toBeVisible();
  await expect.poll(() => fixture.patchBodies.length).toBe(0);
  expect(fixture.patchTargets).toEqual([]);

  await page.goBack();
  await expect(page).toHaveURL(/\/conversation\/welcome$/);
  await expect(page.getByRole('dialog')).toBeHidden();
  await page.goForward();
  await expect(page).toHaveURL(/\/conversation\/second$/);
  await expect(page.getByRole('dialog')).toBeHidden();
  await expect.poll(() => fixture.patchBodies.length).toBe(0);
  expect(fixture.patchTargets).toEqual([]);
});

test('does not steal destination focus when a delayed configuration response completes after navigation', async ({ page }) => {
  const fixture = await installPersonaOverrideFixture(page, { delayPatch: true, multiConversation: true });
  await page.goto('/conversation/welcome');

  const menu = await ensureConversationOptionsMenu(page);
  await agentPicker(menu).selectOption('agent-researcher-r1');
  await personaPicker(menu).selectOption('persona-vivid-r1');
  await menu.getByRole('button', { name: /^apply(?: changes)?$/i }).click();
  const confirmation = page.getByRole('dialog');
  await expect(confirmation).toContainText(/full completed transcript/i);

  const patchResponse = page.waitForResponse((response) => response.request().method() === 'PATCH' && response.url().endsWith('/api/v1/conversations/welcome'));
  await confirmation.getByRole('button', { name: /confirm|continue|switch|apply/i }).click();
  await expect.poll(() => fixture.patchBodies.length).toBe(1);

  const secondConversationLink = page.getByRole('link', { name: /second conversation/i });
  await secondConversationLink.click();
  await expect(page).toHaveURL(/\/conversation\/second$/);
  await expect(page.getByText('Earlier completed question.')).toBeVisible();
  await expect(secondConversationLink).toBeFocused();
  await expect(conversationOptionsButton(page)).not.toBeFocused();

  fixture.releasePatch();
  await patchResponse;
  await expect(page.getByText('Earlier completed question.')).toBeVisible();
  await expect(conversationOptionsButton(page)).not.toBeFocused();
  await expect(secondConversationLink).toBeFocused();
});
