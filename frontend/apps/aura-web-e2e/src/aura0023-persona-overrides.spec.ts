import { expect, test, type Page, type Route } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';

const now = '2026-10-06T00:00:00.000Z';

type FixtureOptions = {
  readonly activeRun?: boolean;
  readonly conflict?: 'optimistic' | 'idempotency';
  readonly pinnedDisabledPersona?: boolean;
};

type FixtureState = {
  readonly patchBodies: Array<Record<string, unknown>>;
  readonly createBodies: Array<Record<string, unknown>>;
  readonly runBodies: Array<Record<string, unknown>>;
  readonly getSummary: () => Record<string, unknown>;
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
  personaRevisionId: vivid.id,
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

type PersonaRevisionFixture = typeof neutral | typeof vivid | typeof disabled;
type AgentRevisionFixture = typeof auraR1 | typeof auraR2 | typeof researcherR1;

const personaReference = (revision: PersonaRevisionFixture): Record<string, unknown> => ({
  profileId: revision.profileId,
  revisionId: revision.id,
  revision: revision.revision,
  displayName: revision.displayName,
  status: revision.status,
  newerRevisionAvailable: false,
});

const agentReference = (revision: AgentRevisionFixture, status: 'active' | 'disabled' = 'active'): Record<string, unknown> => ({
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
  behavioralInstructions: revision.instructions,
  createdAt: revision.createdAt,
});

const agentPayload = (revision: AgentRevisionFixture): Record<string, unknown> => ({
  ...revision,
  instructions: revision.instructions,
  behavioralInstructions: revision.instructions,
});

async function installPersonaOverrideFixture(page: Page, options: FixtureOptions = {}): Promise<FixtureState> {
  const patchBodies: Array<Record<string, unknown>> = [];
  const createBodies: Array<Record<string, unknown>> = [];
  const runBodies: Array<Record<string, unknown>> = [];
  let version = 1;
  let selectedAgent = auraR1;
  let selectedPersona = options.pinnedDisabledPersona ? disabled : neutral;
  let personaOverride = options.pinnedDisabledPersona;
  let assignmentNumber = 1;
  let personaAssignmentNumber = 1;
  let currentRun: Record<string, unknown> | null = options.activeRun
    ? { id: 'run-active', conversationId: 'welcome', userMessageId: 'user-1', assistantMessageId: 'assistant-active', status: 'running', agentRevisionId: selectedAgent.id, personaRevisionId: selectedPersona.id, modelPolicyRevisionId: selectedAgent.modelPolicyRevisionId, promptBundleRevisionId: selectedAgent.promptBundleRevisionId, provider: 'fixture', modelId: 'fixture-chat', retryOfRunId: null, createdAt: now, startedAt: now, finishedAt: null, error: null }
    : null;

  const allPersonas = [neutral, vivid, disabled];
  const allAgents = [auraR1, auraR2, researcherR1];
  const defaultPersonaFor = (agent: AgentRevisionFixture): PersonaRevisionFixture => allPersonas.find((item) => item.id === agent.personaRevisionId) ?? neutral;
  const activePersona = (): PersonaRevisionFixture => allPersonas.find((item) => item.id === selectedPersona.id) ?? neutral;
  const assignmentAgent = (): Record<string, unknown> => agentReference(selectedAgent);
  const assignmentPersona = (): Record<string, unknown> => personaReference(activePersona());
  const agentAssignments: Array<Record<string, unknown>> = [{ id: 'agent-assignment-1', agent: assignmentAgent(), reason: 'initial', effectiveAfterMessageId: null, createdAt: now }];
  const personaAssignments: Array<Record<string, unknown>> = [{ id: 'persona-assignment-1', persona: assignmentPersona(), source: personaOverride ? 'conversation_override' : 'agent_default', reason: 'initial', effectiveAfterMessageId: null, createdAt: now }];

  const summary = (id = 'welcome'): Record<string, unknown> => ({
    id,
    title: id === 'welcome' ? 'A thoughtful beginning' : 'A new thought',
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
    if (url.pathname === '/api/v1/agents' && method === 'GET') return route.fulfill({ json: { items: [{ id: 'agent-aura', status: 'active', version: 2, currentRevision: agentPayload(auraR2), createdAt: now, updatedAt: now }, { id: 'agent-researcher', status: 'active', version: 1, currentRevision: agentPayload(researcherR1), createdAt: now, updatedAt: now }] } });
    if (url.pathname === '/api/v1/agents/agent-aura' && method === 'GET') return route.fulfill({ json: { id: 'agent-aura', status: 'active', version: 2, currentRevision: agentPayload(auraR2), revisions: [agentPayload(auraR1), agentPayload(auraR2)], createdAt: now, updatedAt: now } });
    if (url.pathname === '/api/v1/agents/agent-researcher' && method === 'GET') return route.fulfill({ json: { id: 'agent-researcher', status: 'active', version: 1, currentRevision: agentPayload(researcherR1), revisions: [agentPayload(researcherR1)], createdAt: now, updatedAt: now } });
    if (url.pathname === '/api/v1/personas' && method === 'GET') return route.fulfill({ json: { items: [{ id: 'persona-neutral', status: 'active', version: 1, currentRevision: personaPayload(neutral), createdAt: now, updatedAt: now }, { id: 'persona-vivid', status: 'active', version: 1, currentRevision: personaPayload(vivid), createdAt: now, updatedAt: now }, { id: 'persona-disabled', status: 'disabled', version: 2, currentRevision: personaPayload(disabled), createdAt: now, updatedAt: now }] } });
    if (url.pathname === '/api/v1/personas/persona-neutral' && method === 'GET') return route.fulfill({ json: { id: 'persona-neutral', status: 'active', version: 1, currentRevision: personaPayload(neutral), revisions: [personaPayload(neutral)], createdAt: now, updatedAt: now } });
    if (url.pathname === '/api/v1/personas/persona-vivid' && method === 'GET') return route.fulfill({ json: { id: 'persona-vivid', status: 'active', version: 1, currentRevision: personaPayload(vivid), revisions: [personaPayload(vivid)], createdAt: now, updatedAt: now } });
    if (url.pathname === '/api/v1/personas/persona-disabled' && method === 'GET') return route.fulfill({ json: { id: 'persona-disabled', status: 'disabled', version: 2, currentRevision: personaPayload(disabled), revisions: [personaPayload(disabled)], createdAt: now, updatedAt: now } });
    if (url.pathname === '/api/v1/conversations' && method === 'GET') return route.fulfill({ json: { items: [summary()], nextCursor: null } });
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
      if (options.activeRun) return route.fulfill({ status: 409, json: { detail: 'Wait for the active run to finish before changing conversation configuration.' } });
      if (options.conflict === 'optimistic') return route.fulfill({ status: 409, json: { detail: 'This conversation changed elsewhere. Refresh before applying this configuration.' } });
      if (options.conflict === 'idempotency') return route.fulfill({ status: 409, json: { detail: 'This idempotency key conflicts with an earlier request.' } });
      if (body.personaRevisionId === disabled.id) return route.fulfill({ status: 409, json: { detail: 'Disabled personas cannot be selected for new configuration.' } });
      if (body.transcriptSharingConfirmed !== true && (body.personaRevisionId || body.agentRevisionId || body.useAgentDefaultPersona)) return route.fulfill({ status: 409, json: { detail: 'The full completed transcript will be shared with the selected configuration.' } });
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
    if (url.pathname.endsWith('/events')) return route.fulfill({ status: 200, headers: { 'content-type': 'text/event-stream' }, body: '' });
    return route.continue();
  });

  return { patchBodies, createBodies, runBodies, getSummary: () => summary() };
}

function personaPicker(page: Page) {
  return page.getByLabel(/persona/i).first();
}

function agentPicker(page: Page) {
  return page.getByLabel(/agent/i).first();
}

async function confirmConfiguration(page: Page): Promise<void> {
  const dialog = page.getByRole('dialog');
  await expect(dialog).toContainText(/full completed transcript/i);
  await dialog.getByRole('button', { name: /confirm|continue|switch|apply/i }).click();
}

async function resetToAgentDefault(page: Page): Promise<void> {
  const reset = page.getByRole('button', { name: /use agent default|reset.*default/i });
  if (await reset.count()) {
    await reset.first().click();
    return;
  }
  const picker = personaPicker(page);
  const option = picker.locator('option').filter({ hasText: /agent default|use default/i }).first();
  await expect(option).toHaveCount(1);
  await picker.selectOption({ label: await option.textContent() ?? '' });
}

test('selects a persona on a new draft and sends the selected revision in create payload', async ({ page }) => {
  const fixture = await installPersonaOverrideFixture(page);
  await page.goto('/conversation');
  await expect(personaPicker(page)).toBeVisible();
  await personaPicker(page).selectOption('persona-vivid-r1');
  await page.getByRole('textbox', { name: /message aura/i }).fill('Start this conversation in a vivid style.');
  await page.getByRole('button', { name: /send/i }).click();
  await expect.poll(() => fixture.createBodies.length).toBe(1);
  expect(fixture.createBodies[0]?.personaRevisionId).toBe('persona-vivid-r1');
});

test('confirms full completed-transcript sharing before changing a persisted persona', async ({ page }) => {
  const fixture = await installPersonaOverrideFixture(page);
  await page.goto('/conversation/welcome');
  await personaPicker(page).selectOption('persona-vivid-r1');
  await confirmConfiguration(page);
  await expect.poll(() => fixture.patchBodies.length).toBe(1);
  expect(fixture.patchBodies[0]).toMatchObject({ personaRevisionId: 'persona-vivid-r1', transcriptSharingConfirmed: true, version: 1 });
  await expect(page.getByText(/vivid/i).first()).toBeVisible();
});

test('resets an explicit persona override to the selected agent default', async ({ page }) => {
  const fixture = await installPersonaOverrideFixture(page);
  await page.goto('/conversation/welcome');
  await personaPicker(page).selectOption('persona-vivid-r1');
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
  await personaPicker(page).selectOption('persona-vivid-r1');
  await confirmConfiguration(page);
  await agentPicker(page).selectOption('agent-researcher-r1');
  await confirmConfiguration(page);
  await expect.poll(() => fixture.patchBodies.length).toBe(2);
  expect(fixture.patchBodies[1]).toMatchObject({ agentRevisionId: 'agent-researcher-r1', transcriptSharingConfirmed: true });
  expect(fixture.getSummary().personaOverride).toBe(true);
  expect((fixture.getSummary().persona as Record<string, unknown>).revisionId).toBe(vivid.id);
});

test('adopts a new agent default when no persona override exists', async ({ page }) => {
  const fixture = await installPersonaOverrideFixture(page);
  await page.goto('/conversation/welcome');
  await agentPicker(page).selectOption('agent-aura-r2');
  await confirmConfiguration(page);
  await expect.poll(() => fixture.patchBodies.length).toBe(1);
  expect(fixture.patchBodies[0]).toMatchObject({ agentRevisionId: 'agent-aura-r2', transcriptSharingConfirmed: true });
  expect(fixture.getSummary().personaOverride).toBe(false);
  expect((fixture.getSummary().persona as Record<string, unknown>).revisionId).toBe(vivid.id);
});

test('renders one transition marker for an atomic agent and persona boundary', async ({ page }) => {
  const fixture = await installPersonaOverrideFixture(page);
  await page.goto('/conversation/welcome');
  await agentPicker(page).selectOption('agent-researcher-r1');
  await personaPicker(page).selectOption('persona-vivid-r1');
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
  const picker = personaPicker(page);
  if (await picker.isDisabled()) {
    await expect(picker).toBeDisabled();
  } else {
    await picker.selectOption('persona-vivid-r1');
    await expect(page.getByRole('alert')).toContainText(/active run|finish/i);
  }
  expect(fixture.patchBodies).toHaveLength(0);
});

test('omits disabled personas from new selection while allowing a pinned disabled persona to continue', async ({ page }) => {
  const fixture = await installPersonaOverrideFixture(page, { pinnedDisabledPersona: true });
  await page.goto('/conversation/welcome');
  await expect(page.getByText(/archived|disabled/i).first()).toBeVisible();
  const disabledOption = personaPicker(page).locator('option[value="persona-disabled-r1"]');
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
    await personaPicker(page).selectOption('persona-vivid-r1');
    await confirmConfiguration(page);
    await expect.poll(() => fixture.patchBodies.length).toBe(1);
    await expect(composer).toHaveValue(`Keep this draft after the ${conflict} conflict.`);
    await expect(page.getByRole('alert')).toContainText(/changed elsewhere|idempotency|draft/i);
  }
});

test('retains the direct conversation route while changing persona and survives reload', async ({ page }) => {
  const fixture = await installPersonaOverrideFixture(page);
  await page.goto('/conversation/welcome');
  await personaPicker(page).selectOption('persona-vivid-r1');
  await confirmConfiguration(page);
  await expect.poll(() => fixture.patchBodies.length).toBe(1);
  expect(new URL(page.url()).pathname).toBe('/conversation/welcome');
  await page.reload();
  await expect(personaPicker(page)).toHaveValue('persona-vivid-r1');
  expect(new URL(page.url()).pathname).toBe('/conversation/welcome');
});

test('keeps the configuration surface keyboard accessible and responsive', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/conversation/welcome');
  await expect(personaPicker(page)).toBeVisible();
  await expect(agentPicker(page)).toBeVisible();
  await personaPicker(page).focus();
  await expect(personaPicker(page)).toBeFocused();
  const results = await new AxeBuilder({ page }).include('section.conversation').analyze();
  expect(results.violations).toEqual([]);
});
