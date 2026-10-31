import { expect, test, type Page, type Route } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';
import type {
  ActiveGeneratedArtifactDetail,
  ActiveResearchDetail,
  DeletedGeneratedArtifactTombstone,
  DeletedResearchTombstone,
  DurableTaskDetail,
  GeneratedArtifactPage,
  GeneratedArtifactPreview,
  GeneratedArtifactRevision,
  ResearchBudget,
  ResearchEvidencePage,
  ResearchModeEnvelope,
  ResearchPage,
  ResearchSettings,
  TaskCommandReceipt,
} from '@aura/aura-api-client';

const OWNER = 'research-fixture-owner';
const CSRF = 'research-fixture-csrf';
const NOW = '2026-10-30T10:00:00.000Z';
const RESEARCH_ID = '11111111-1111-4111-8111-111111111111';
const TASK_ID = '22222222-2222-4222-8222-222222222222';
const ARTIFACT_ID = '33333333-3333-4333-8333-333333333333';
const SOURCE_ID = '44444444-4444-4444-8444-444444444444';
const CONVERSATION_ID = '88888888-8888-4888-8888-888888888888';
const RUN_ID = '99999999-9999-4999-8999-999999999999';
const PRIVATE_MARKER = 'owner-private-research-question-should-not-persist';

function inputFingerprint(value: string): string {
  let hash = 2166136261;
  for (let index = 0; index < value.length; index += 1)
    hash = Math.imul(hash ^ value.charCodeAt(index), 16777619);
  return `f${(hash >>> 0).toString(16)}`;
}

type ResearchMode = 'quick' | 'auto' | 'deep' | 'agent_led';
type FixtureState =
  | 'running'
  | 'completed'
  | 'partial'
  | 'budget_exhausted'
  | 'failed'
  | 'canceled'
  | 'interrupted'
  | 'deletion_pending'
  | 'deletion_failed'
  | 'deleted';

interface FixtureOptions {
  researchState?: FixtureState;
  signedOutAfterFirstSession?: boolean;
  stream?:
    | 'fresh'
    | 'stale-duplicate'
    | 'reconnect-gap'
    | 'malformed'
    | 'crlf-chunk'
    | 'gone'
    | 'gone-then-live'
    | 'clean-eof';
  admissionStatus?: 202 | 409;
  researchListStatus?: 200 | 401 | 403;
  researchDetailStatus?: 200 | 401 | 403;
  downloadVariant?:
    'valid' | 'html' | 'executable' | 'missing-length' | 'oversize' | 'media-unavailable';
  downloadMediaType?: 'markdown' | 'plain' | 'json';
  commandState?: 'accepted' | 'rejected' | 'unknown';
  detailDelayMs?: number;
  taskListDelayMs?: number;
  mutationDelayMs?: number;
  taskPageCursor?: string;
  settingsStatus?: 200 | 401 | 403 | 409 | 503;
  settingsDelayMs?: number;
  artifactListDelayMs?: number;
  artifactPageCursor?: string;
  artifactDetailStatus?: 200 | 401 | 403;
  downloadStatus?: 200 | 401 | 403;
  artifactUpdatedAt?: string;
  evidenceDelayMs?: number;
  conversationActivity?: 'running' | 'detached' | 'terminal';
}

interface ResearchFixture {
  readonly options: FixtureOptions;
  readonly requests: Array<{
    method: string;
    pathname: string;
    query: string;
    body: string | null;
    headers: Record<string, string>;
  }>;
  state: {
    researchState: FixtureState;
    version: number;
    sessionReads: number;
    deleteCalls: number;
  };
  route: (route: Route) => Promise<void>;
}

const budgets: Record<ResearchMode, ResearchBudget> = {
  quick: {
    wallTimeSeconds: 90,
    fetchedSources: 6,
    searchRequests: 2,
    browserPages: 1,
    modelCalls: 2,
  },
  auto: {
    wallTimeSeconds: 600,
    fetchedSources: 15,
    searchRequests: 5,
    browserPages: 2,
    modelCalls: 4,
  },
  deep: {
    wallTimeSeconds: 1_800,
    fetchedSources: 30,
    searchRequests: 10,
    browserPages: 6,
    modelCalls: 8,
  },
  agent_led: {
    wallTimeSeconds: 3_600,
    fetchedSources: 60,
    searchRequests: 20,
    browserPages: 12,
    modelCalls: 12,
  },
};

function researchDetail(fixture: ResearchFixture): ActiveResearchDetail {
  const outcome =
    fixture.state.researchState === 'running'
      ? null
      : fixture.state.researchState === 'completed' ||
          fixture.state.researchState.startsWith('deletion') ||
          fixture.state.researchState === 'deleted'
        ? 'completed'
        : fixture.state.researchState;
  return {
    id: RESEARCH_ID,
    taskRef: { taskId: TASK_ID },
    mode: 'quick',
    stage: fixture.state.researchState === 'running' ? 'fetching' : 'persisting',
    outcome,
    contentState: 'active',
    version: fixture.state.version,
    capabilities: {
      canCancel: fixture.state.researchState === 'running',
      canExtend: fixture.state.researchState === 'running',
      canDelete: true,
      canRetryCleanup: fixture.state.researchState === 'deletion_failed',
      canReconcileCleanup: false,
    },
    createdAt: NOW,
    updatedAt: NOW,
    finishedAt: fixture.state.researchState === 'running' ? null : NOW,
    question: 'Compare owner-private fixture sources',
    admittedBudget: budgets.quick,
    consumedBudget: {
      wallTimeSeconds: 12,
      fetchedSources: 2,
      searchRequests: 1,
      browserPages: 0,
      modelCalls: 1,
    },
    extensionAccepted: false,
    sourceCount: 2,
    failureClass: null,
    artifactRefs: [{ artifactId: ARTIFACT_ID }],
    citations: [
      {
        kind: 'web',
        sourceId: SOURCE_ID,
        label: 'Fixture source',
        url: 'https://example.test/research',
        retrievedAt: NOW,
        locator: 'Overview',
      },
    ],
  };
}

function researchTombstone(fixture: ResearchFixture): DeletedResearchTombstone {
  return {
    id: RESEARCH_ID,
    taskRef: { taskId: TASK_ID },
    mode: 'quick',
    stage: 'terminal',
    outcome: 'completed',
    contentState:
      fixture.state.researchState === 'deleted'
        ? 'deleted'
        : (fixture.state.researchState as
            'deletion_pending' | 'deletion_failed' | 'cleanup_unknown'),
    version: fixture.state.version,
    capabilities: {
      canCancel: false,
      canExtend: false,
      canDelete: false,
      canRetryCleanup: fixture.state.researchState === 'deletion_failed',
      canReconcileCleanup: false,
    },
    createdAt: NOW,
    updatedAt: NOW,
    finishedAt: NOW,
    tombstoneLabel: 'Research source deleted',
  };
}

function researchSummary(fixture: ResearchFixture): ResearchPage['items'][number] {
  const detail = researchDetail(fixture);
  return {
    id: detail.id,
    taskRef: detail.taskRef,
    mode: detail.mode,
    stage: detail.stage,
    outcome: detail.outcome,
    contentState: detail.contentState,
    version: detail.version,
    capabilities: detail.capabilities,
    createdAt: detail.createdAt,
    updatedAt: detail.updatedAt,
    finishedAt: detail.finishedAt,
  };
}

function taskDetail(fixture: ResearchFixture): DurableTaskDetail {
  const activity = fixture.options.conversationActivity;
  const activityStatus =
    activity === 'running' ? 'running' : activity === 'detached' ? 'waiting' : 'completed';
  const activitySourceStatus =
    activity === 'running' ? 'fetching' : activity === 'detached' ? 'detached' : 'completed';
  const activityStage =
    activity === 'running' ? 'fetching' : activity === 'detached' ? 'draining' : 'persisting';
  const status = activity
    ? activityStatus
    : fixture.state.researchState === 'running'
      ? 'running'
      : fixture.state.researchState === 'canceled'
        ? 'canceled'
        : fixture.state.researchState === 'failed' ||
            fixture.state.researchState === 'budget_exhausted'
          ? 'failed'
          : 'completed';
  const outcome =
    activity === 'running' ||
    activity === 'detached' ||
    fixture.state.researchState === 'running' ||
    fixture.state.researchState === 'deleted' ||
    fixture.state.researchState.startsWith('deletion')
      ? null
      : activity === 'terminal' || fixture.state.researchState === 'completed'
        ? 'completed'
        : fixture.state.researchState;
  return {
    id: TASK_ID,
    sourceKind: 'research',
    sourceId: RESEARCH_ID,
    sourceVersion: fixture.state.version,
    status,
    sourceStatus: activity
      ? activitySourceStatus
      : fixture.state.researchState === 'running'
        ? 'fetching'
        : 'persisting',
    sourceOutcome: outcome,
    stage: activity
      ? activityStage
      : fixture.state.researchState === 'running'
        ? 'fetching'
        : 'persisting',
    progress:
      activity === 'running'
        ? 0.35
        : activity === 'detached'
          ? 0.7
          : fixture.state.researchState === 'running'
            ? 0.35
            : 1,
    capabilities: {
      canCancel: activity === 'running' || (!activity && fixture.state.researchState === 'running'),
      canRetry: false,
      hasSourceDetail: true,
    },
    routeKey: 'research_detail',
    createdAt: NOW,
    updatedAt: NOW,
    displayLabel: 'Compare owner-private fixture sources',
    failureClass: fixture.state.researchState === 'failed' ? 'provider_unavailable' : null,
    recoveryAction:
      fixture.state.researchState === 'failed' ? 'Retry from the owning service.' : null,
    artifactRefs: [{ artifactId: ARTIFACT_ID }],
  };
}

function artifactMediaType(
  fixture: ResearchFixture,
): 'text/markdown; charset=utf-8' | 'text/plain; charset=utf-8' | 'application/json' {
  return fixture.options.downloadMediaType === 'plain'
    ? 'text/plain; charset=utf-8'
    : fixture.options.downloadMediaType === 'json'
      ? 'application/json'
      : 'text/markdown; charset=utf-8';
}

function artifactDetail(fixture: ResearchFixture): ActiveGeneratedArtifactDetail {
  const mediaType = artifactMediaType(fixture);
  const updatedAt = fixture.options.artifactUpdatedAt ?? NOW;
  return {
    id: ARTIFACT_ID,
    kind: 'research_report',
    title: 'Research report.md',
    mediaType,
    contentState: 'active',
    currentRevision: 2,
    version: fixture.state.version,
    capabilities: {
      canPreview: true,
      canDownload: true,
      canDelete: true,
      canRetryCleanup: false,
      canReconcileCleanup: false,
    },
    createdAt: updatedAt,
    updatedAt,
    lineage: { taskId: TASK_ID, runId: null, conversationId: null, workspaceId: null },
    revision: {
      revision: 2,
      mediaType,
      sha256: 'a'.repeat(64),
      byteCount: 128,
      creatorComponent: 'aura.research.report',
      previousRevision: 1,
      createdAt: updatedAt,
    },
  };
}

function artifactTombstone(fixture: ResearchFixture): DeletedGeneratedArtifactTombstone {
  const updatedAt = fixture.options.artifactUpdatedAt ?? NOW;
  return {
    id: ARTIFACT_ID,
    kind: 'research_report',
    title: null,
    mediaType: null,
    contentState:
      fixture.state.researchState === 'deleted'
        ? 'deleted'
        : (fixture.state.researchState as
            'deletion_pending' | 'deletion_failed' | 'cleanup_unknown'),
    currentRevision: 2,
    version: fixture.state.version,
    capabilities: {
      canPreview: false,
      canDownload: false,
      canDelete: false,
      canRetryCleanup: fixture.state.researchState === 'deletion_failed',
      canReconcileCleanup: false,
    },
    createdAt: updatedAt,
    updatedAt,
    lineage: { taskId: TASK_ID, runId: null, conversationId: null, workspaceId: null },
    integrity: { revision: 2, sha256: 'a'.repeat(64), byteCount: 128, createdAt: updatedAt },
    deletionAuditId: '55555555-5555-4555-8555-555555555555',
  };
}

function makeFixture(options: FixtureOptions = {}): ResearchFixture {
  const fixture = {
    options,
    requests: [],
    state: {
      researchState: options.researchState ?? 'running',
      version: 3,
      sessionReads: 0,
      deleteCalls: 0,
    },
    route: async () => {
      throw new Error('fixture route is not initialized');
    },
  } as ResearchFixture;
  const json = (route: Route, body: unknown, status = 200): Promise<void> =>
    route.fulfill({ status, headers: { 'cache-control': 'no-store' }, json: body });
  fixture.route = async (route: Route): Promise<void> => {
    const request = route.request();
    const url = new URL(request.url());
    fixture.requests.push({
      method: request.method(),
      pathname: url.pathname,
      query: url.search,
      body: request.method() === 'GET' ? null : request.postData(),
      headers: request.headers(),
    });
    if (url.pathname === '/api/v1/auth/session') {
      fixture.state.sessionReads += 1;
      // Allow the shell's duplicate bootstrap session reads before expiring
      // the next document session generation.
      if (options.signedOutAfterFirstSession && fixture.state.sessionReads > 10)
        return json(route, { detail: 'session expired' }, 401);
      return json(route, {
        principal: {
          issuer: 'https://authentik.test',
          subject: OWNER,
          displayName: 'Fixture Owner',
        },
        csrfToken: CSRF,
        idleExpiresAt: '2099-10-30T10:00:00Z',
        absoluteExpiresAt: '2099-10-31T10:00:00Z',
      });
    }
    if (url.pathname === '/api/v1/models')
      return json(route, {
        models: [
          {
            id: 'fixture-model',
            displayName: 'Fixture model',
            provider: 'fixture',
            capabilities: ['chat'],
            availability: 'available',
            selectable: true,
            disabledReason: null,
          },
        ],
        defaultModelId: 'fixture-model',
        observedAt: NOW,
      });
    if (url.pathname === '/api/v1/agents')
      return json(route, {
        items: [
          {
            id: '7e1d98ee-65b2-5bc7-9ca9-edd7ad85b540',
            status: 'active',
            version: 1,
            currentRevision: {
              id: 'agent-revision',
              profileId: '7e1d98ee-65b2-5bc7-9ca9-edd7ad85b540',
              revision: 1,
              displayName: 'Aura',
              purpose: 'Fixture research assistant',
              instructions: 'Use the bounded fixture policy.',
              personaRevisionId: 'persona-revision',
              promptBundleRevisionId: 'prompt-revision',
              modelPolicyRevisionId: 'model-policy',
              createdAt: NOW,
            },
            revisions: [],
          },
        ],
      });
    if (url.pathname === '/api/v1/agents/7e1d98ee-65b2-5bc7-9ca9-edd7ad85b540') {
      const revision = {
        id: 'agent-revision',
        profileId: '7e1d98ee-65b2-5bc7-9ca9-edd7ad85b540',
        revision: 1,
        displayName: 'Aura',
        purpose: 'Fixture research assistant',
        instructions: 'Use the bounded fixture policy.',
        personaRevisionId: 'persona-revision',
        promptBundleRevisionId: 'prompt-revision',
        modelPolicyRevisionId: 'model-policy',
        createdAt: NOW,
      };
      return json(route, {
        id: '7e1d98ee-65b2-5bc7-9ca9-edd7ad85b540',
        status: 'active',
        version: 1,
        displayName: 'Aura',
        createdAt: NOW,
        updatedAt: NOW,
        currentRevision: revision,
        revisions: [revision],
      });
    }
    if (url.pathname === '/api/v1/conversations' && request.method() === 'POST') {
      const run = {
        id: RUN_ID,
        conversationId: CONVERSATION_ID,
        userMessageId: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
        assistantMessageId: null,
        status: 'running',
        agentRevisionId: 'agent-revision',
        modelPolicyRevisionId: 'model-policy',
        provider: 'fixture',
        modelId: 'fixture-model',
        retryOfRunId: null,
        createdAt: NOW,
        startedAt: NOW,
        finishedAt: null,
        error: null,
      };
      const conversation = {
        id: CONVERSATION_ID,
        title: 'Research fixture',
        agentProfileId: 'fixture-agent',
        agentRevisionId: 'agent-revision',
        modelId: 'fixture-model',
        version: 1,
        createdAt: NOW,
        updatedAt: NOW,
        archivedAt: null,
        currentRun: run,
      };
      return json(
        route,
        {
          conversation,
          userMessage: {
            id: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
            conversationId: CONVERSATION_ID,
            role: 'user',
            content: 'Research fixture',
            state: 'complete',
            runId: RUN_ID,
            createdAt: NOW,
            updatedAt: NOW,
          },
          run,
        },
        202,
      );
    }
    if (url.pathname === `/api/v1/conversations/${CONVERSATION_ID}`) {
      const message = {
        id: 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb',
        conversationId: CONVERSATION_ID,
        role: 'assistant',
        content: 'Research activity admitted.',
        state: 'complete',
        runId: RUN_ID,
        taskRefs: [{ taskId: TASK_ID }],
        researchActivity: {
          taskRef: { taskId: TASK_ID },
          mode: 'quick',
          admissionState: 'admitted',
        },
        createdAt: NOW,
        updatedAt: NOW,
      };
      return json(route, {
        id: CONVERSATION_ID,
        title: 'Research fixture',
        agentProfileId: 'fixture-agent',
        agentRevisionId: 'agent-revision',
        modelId: 'fixture-model',
        version: 2,
        createdAt: NOW,
        updatedAt: NOW,
        archivedAt: null,
        currentRun: null,
        messages: [message],
        recentRuns: [
          {
            id: RUN_ID,
            conversationId: CONVERSATION_ID,
            userMessageId: null,
            assistantMessageId: message.id,
            status: 'completed',
            agentRevisionId: 'agent-revision',
            modelPolicyRevisionId: 'model-policy',
            provider: 'fixture',
            modelId: 'fixture-model',
            retryOfRunId: null,
            createdAt: NOW,
            startedAt: NOW,
            finishedAt: NOW,
            error: null,
          },
        ],
      });
    }
    if (url.pathname === '/api/v1/conversations')
      return json(route, { items: [], nextCursor: null });
    if (url.pathname === `/api/v1/runs/${RUN_ID}/events`) {
      const event = {
        schemaVersion: 1,
        eventId: 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb',
        sequence: 1,
        eventType: 'assistant.snapshot',
        runId: RUN_ID,
        conversationId: CONVERSATION_ID,
        occurredAt: NOW,
        data: {
          message: {
            id: 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb',
            conversationId: CONVERSATION_ID,
            role: 'assistant',
            content: 'Research activity admitted.',
            state: 'complete',
            runId: RUN_ID,
            taskRefs: [{ taskId: TASK_ID }],
            researchActivity: {
              taskRef: { taskId: TASK_ID },
              mode: 'quick',
              admissionState: 'admitted',
            },
            createdAt: NOW,
            updatedAt: NOW,
          },
        },
      };
      return route.fulfill({
        status: 200,
        headers: { 'cache-control': 'no-store', 'content-type': 'text/event-stream' },
        body: `id: ${event.eventId}\ndata: ${JSON.stringify(event)}\n\n`,
      });
    }
    if (url.pathname === '/api/v1/notifications')
      return json(route, { items: [], nextCursor: null, unacknowledgedCount: 0 });
    if (url.pathname === '/api/v1/research/settings') {
      if (options.settingsDelayMs)
        await new Promise((resolve) => setTimeout(resolve, options.settingsDelayMs));
      if (options.settingsStatus && options.settingsStatus !== 200)
        return json(
          route,
          { detail: 'authoritative settings unavailable' },
          options.settingsStatus,
        );
      const presets: ReadonlyArray<ResearchModeEnvelope> = (
        Object.keys(budgets) as ResearchMode[]
      ).map((mode) => ({ mode, maximum: budgets[mode], defaults: budgets[mode] }));
      const value: ResearchSettings = { version: 3, presets, updatedAt: NOW };
      return json(route, value);
    }
    if (url.pathname === '/api/v1/research' && request.method() === 'GET') {
      if (options.researchListStatus && options.researchListStatus !== 200)
        return json(route, { detail: 'research session rejected' }, options.researchListStatus);
      return json(route, {
        items: [researchSummary(fixture)],
        nextCursor: null,
      } satisfies ResearchPage);
    }
    if (url.pathname === '/api/v1/research' && request.method() === 'POST') {
      const body = JSON.parse(request.postData() ?? '{}') as {
        mode?: ResearchMode;
        question?: string;
      };
      const mode = body.mode ?? 'quick';
      fixture.state.researchState = 'running';
      fixture.state.version = 1;
      return json(
        route,
        {
          researchId: RESEARCH_ID,
          taskRef: { taskId: TASK_ID },
          mode,
          admissionState: 'admitted',
          admittedBudget: budgets[mode],
          version: 1,
          createdAt: NOW,
        },
        options.admissionStatus ?? 202,
      );
    }
    if (url.pathname === `/api/v1/research/${RESEARCH_ID}`) {
      if (options.researchDetailStatus && options.researchDetailStatus !== 200)
        return json(route, { detail: 'research session rejected' }, options.researchDetailStatus);
      return json(
        route,
        fixture.state.researchState.startsWith('deletion') ||
          fixture.state.researchState === 'deleted'
          ? researchTombstone(fixture)
          : researchDetail(fixture),
      );
    }
    if (url.pathname === `/api/v1/research/${RESEARCH_ID}/evidence`) {
      if (options.evidenceDelayMs)
        await new Promise((resolve) => setTimeout(resolve, options.evidenceDelayMs));
      return json(route, {
        items: [
          {
            sourceId: SOURCE_ID,
            label: 'Fixture source',
            url: 'https://example.test/research',
            publisher: 'Example',
            retrievedAt: NOW,
            mediaType: 'text/html',
            extractionMethod: 'static',
            freshnessAt: NOW,
            locators: ['Overview'],
          },
        ],
        nextCursor: null,
      } satisfies ResearchEvidencePage);
    }
    if (url.pathname === `/api/v1/research/${RESEARCH_ID}/cancel`) {
      fixture.state.researchState = 'completed';
      fixture.state.version += 1;
      return json(route, researchDetail(fixture), 202);
    }
    if (url.pathname === `/api/v1/research/${RESEARCH_ID}/extensions`) {
      fixture.state.version += 1;
      return json(route, { ...researchDetail(fixture), extensionAccepted: true });
    }
    if (url.pathname === `/api/v1/research/${RESEARCH_ID}/deletions`) {
      fixture.state.deleteCalls += 1;
      fixture.state.researchState = 'deletion_pending';
      fixture.state.version += 1;
      return json(route, researchTombstone(fixture), 202);
    }
    if (url.pathname === `/api/v1/research/${RESEARCH_ID}/deletions/retry`) {
      fixture.state.researchState = 'deleted';
      fixture.state.version += 1;
      return json(route, researchTombstone(fixture), 202);
    }
    if (url.pathname === '/api/v1/tasks' && request.method() === 'GET') {
      if (options.taskListDelayMs)
        await new Promise((resolve) => setTimeout(resolve, options.taskListDelayMs));
      return json(route, {
        items: [taskDetail(fixture)],
        nextCursor: options.taskPageCursor ?? null,
      });
    }
    if (url.pathname === `/api/v1/tasks/${TASK_ID}`) {
      if (options.detailDelayMs)
        await new Promise((resolve) => setTimeout(resolve, options.detailDelayMs));
      return json(route, taskDetail(fixture));
    }
    if (url.pathname === `/api/v1/tasks/${TASK_ID}/events`) {
      const eventRequests = fixture.requests.filter(
        (item) => item.pathname === `/api/v1/tasks/${TASK_ID}/events`,
      ).length;
      if (options.stream === 'gone-then-live' && eventRequests > 1) fixture.state.version = 4;
      const event = {
        eventId: '66666666-6666-4666-8666-666666666666',
        taskId: TASK_ID,
        sourceKind: 'research',
        sourceId: RESEARCH_ID,
        sourceVersion: fixture.state.version,
        occurredAt: NOW,
      };
      const stale = { ...event, eventId: '77777777-7777-4777-8777-777777777777', sourceVersion: 1 };
      if (options.stream === 'gone' || (options.stream === 'gone-then-live' && eventRequests === 1))
        return json(route, { detail: 'stream cursor expired' }, 410);
      if (options.stream === 'malformed') {
        return route.fulfill({
          status: 200,
          headers: { 'cache-control': 'no-store', 'content-type': 'text/event-stream' },
          body: 'data: {not-json}\n\n',
        });
      }
      if (options.stream === 'crlf-chunk') {
        return route.fulfill({
          status: 200,
          headers: { 'cache-control': 'no-store', 'content-type': 'text/event-stream' },
          body: `id: ${event.eventId}\r\ndata: ${JSON.stringify(event)}\r\n\r\n`,
        });
      }
      const body =
        options.stream === 'stale-duplicate' || options.stream === 'reconnect-gap'
          ? `id: ${event.eventId}\ndata: ${JSON.stringify(event)}\n\nid: ${stale.eventId}\ndata: ${JSON.stringify(stale)}\n\nid: ${event.eventId}\ndata: ${JSON.stringify(event)}\n\n`
          : `id: ${event.eventId}\ndata: ${JSON.stringify(event)}\n\n`;
      return route.fulfill({
        status: 200,
        headers: { 'cache-control': 'no-store', 'content-type': 'text/event-stream' },
        body,
      });
    }
    if (/^\/api\/v1\/tasks\/[^/]+\/(cancel|retry)$/.test(url.pathname)) {
      if (options.mutationDelayMs)
        await new Promise((resolve) => setTimeout(resolve, options.mutationDelayMs));
      return json(
        route,
        {
          taskId: TASK_ID,
          command: url.pathname.endsWith('/cancel') ? 'cancel' : 'retry',
          state: options.commandState ?? 'accepted',
          sourceVersion: fixture.state.version,
        } satisfies TaskCommandReceipt,
        202,
      );
    }
    if (url.pathname === '/api/v1/artifacts' && request.method() === 'GET') {
      if (options.artifactListDelayMs)
        await new Promise((resolve) => setTimeout(resolve, options.artifactListDelayMs));
      return json(route, {
        items: [artifactDetail(fixture)],
        nextCursor: options.artifactPageCursor ?? null,
      } satisfies GeneratedArtifactPage);
    }
    if (url.pathname === `/api/v1/artifacts/${ARTIFACT_ID}`) {
      if (options.artifactDetailStatus && options.artifactDetailStatus !== 200)
        return json(route, { detail: 'artifact session rejected' }, options.artifactDetailStatus);
      return json(
        route,
        fixture.state.researchState.startsWith('deletion') ||
          fixture.state.researchState === 'deleted'
          ? artifactTombstone(fixture)
          : artifactDetail(fixture),
      );
    }
    if (url.pathname === `/api/v1/artifacts/${ARTIFACT_ID}/revisions`)
      return json(route, [
        {
          revision: 1,
          mediaType: 'text/markdown; charset=utf-8',
          sha256: 'b'.repeat(64),
          byteCount: 88,
          creatorComponent: 'aura.research.report',
          previousRevision: null,
          createdAt: NOW,
        },
        artifactDetail(fixture).revision,
      ] satisfies ReadonlyArray<GeneratedArtifactRevision>);
    if (url.pathname === `/api/v1/artifacts/${ARTIFACT_ID}/preview`) {
      if (
        fixture.state.researchState.startsWith('deletion') ||
        fixture.state.researchState === 'deleted'
      )
        return json(route, { detail: 'artifact unavailable' }, 409);
      return json(route, {
        artifactId: ARTIFACT_ID,
        revision: 2,
        mediaType: 'text/markdown; charset=utf-8',
        text: 'Safe report text. <script>private marker must remain inert</script>',
        truncated: false,
      } satisfies GeneratedArtifactPreview);
    }
    if (url.pathname === `/api/v1/artifacts/${ARTIFACT_ID}/content`) {
      if (options.downloadStatus && options.downloadStatus !== 200)
        return json(
          route,
          { detail: 'artifact download session rejected' },
          options.downloadStatus,
        );
      const mediaType =
        options.downloadMediaType === 'plain'
          ? 'text/plain; charset=utf-8'
          : options.downloadMediaType === 'json'
            ? 'application/json'
            : 'text/markdown; charset=utf-8';
      const validBody =
        options.downloadMediaType === 'json'
          ? JSON.stringify({ report: 'fixture' }).padEnd(128, ' ')
          : options.downloadMediaType === 'plain'
            ? 'Fixture report text. '.repeat(8).slice(0, 128)
            : 'x'.repeat(128);
      const variant = options.downloadVariant ?? 'valid';
      const headers: Record<string, string> = {
        'cache-control': 'no-store',
        'content-type':
          variant === 'html'
            ? 'text/html; charset=utf-8'
            : variant === 'executable' || variant === 'media-unavailable'
              ? 'application/octet-stream'
              : mediaType,
        'x-content-type-options': 'nosniff',
        'content-disposition': 'attachment; filename="research-report.md"',
      };
      if (variant !== 'missing-length')
        headers['content-length'] = String(variant === 'oversize' ? 128 + 1 : 128);
      return route.fulfill({
        status: 200,
        headers,
        // Playwright may synthesize Content-Length for fulfilled responses;
        // make the missing-length fixture fail the authorized byte-count
        // check even when the browser adds that transport header.
        body:
          variant === 'oversize'
            ? `${validBody}x`
            : variant === 'missing-length'
              ? validBody.slice(0, 127)
              : validBody,
      });
    }
    if (url.pathname === `/api/v1/artifacts/${ARTIFACT_ID}/deletions`) {
      fixture.state.deleteCalls += 1;
      fixture.state.researchState = 'deletion_pending';
      fixture.state.version += 1;
      return json(route, artifactTombstone(fixture), 202);
    }
    if (url.pathname === `/api/v1/artifacts/${ARTIFACT_ID}/deletions/retry`) {
      fixture.state.researchState = 'deleted';
      fixture.state.version += 1;
      return json(route, artifactTombstone(fixture), 202);
    }
    return json(route, { detail: 'fixture endpoint not implemented' }, 404);
  };
  return fixture;
}

async function installFixture(page: Page, options: FixtureOptions = {}): Promise<ResearchFixture> {
  const fixture = makeFixture(options);
  await page.route('**/api/v1/**', fixture.route);
  return fixture;
}

async function assertNoPrivateBrowserPersistence(page: Page, marker: string): Promise<void> {
  const state = await page.evaluate(async (privateMarker) => {
    const values = [
      ...Object.keys(localStorage),
      ...Object.values(localStorage),
      ...Object.keys(sessionStorage),
      ...Object.values(sessionStorage),
      window.location.href,
    ];
    const cacheNames = 'caches' in window ? await window.caches.keys() : [];
    return {
      marker: values.some((value) => value.includes(privateMarker)),
      apiCache: cacheNames.some((name) => /api|aura/i.test(name)),
    };
  }, marker);
  expect(state.marker).toBe(false);
  expect(state.apiCache).toBe(false);
}

test.describe('Aura Web governed research, Task Center, and artifacts', () => {
  test('admits Quick, Auto, Deep, and Agent-led research with inline budgets', async ({ page }) => {
    const fixture = await installFixture(page);
    await page.goto('/research');
    await expect(page.getByRole('heading', { name: /research/i }).first()).toBeVisible();
    await expect(page.getByLabel(/research question/i)).toBeVisible();
    await expect(page.getByText(/advanced settings/i)).toBeVisible();
    for (const label of ['Quick', 'Auto', 'Deep', 'Agent-led'])
      await expect(page.getByRole('radio', { name: new RegExp(label, 'i') })).toBeVisible();
    const question = page.getByLabel(/research question/i);
    await question.fill('Compare owner-private fixture sources');
    await page.getByRole('radio', { name: /Quick/i }).check();
    await expect(page.getByText(/90 seconds|6 sources/i).first()).toBeVisible();
    await page.getByRole('button', { name: /start research/i }).click();
    await expect
      .poll(
        () =>
          fixture.requests.filter(
            (request) => request.pathname === '/api/v1/research' && request.method === 'POST',
          ).length,
      )
      .toBe(1);
    const admission = fixture.requests.find(
      (request) => request.pathname === '/api/v1/research' && request.method === 'POST',
    );
    expect(admission?.body).toContain('quick');
    await expect(page.getByText(/admitted|searching|fetching|queued/i).first()).toBeVisible();
  });

  test('keeps a stable conversation activity and does not duplicate task admission', async ({
    page,
  }) => {
    const fixture = await installFixture(page, { stream: 'stale-duplicate' });
    await page.goto(`/conversation/${CONVERSATION_ID}`);
    await expect(page.getByText('Research activity admitted.', { exact: true })).toBeVisible();
    const researchPosts = fixture.requests.filter(
      (request) => request.pathname === '/api/v1/research' && request.method === 'POST',
    );
    expect(researchPosts.length).toBeLessThanOrEqual(1);
  });

  test('reconciles duplicate, stale, and reconnecting Task Center invalidations through REST', async ({
    page,
  }) => {
    const fixture = await installFixture(page, { stream: 'stale-duplicate' });
    await page.goto('/tasks');
    await expect(page.locator('.task-card').first()).toBeVisible();
    await page.locator('.task-card').first().click();
    await expect(page).toHaveURL(new RegExp(`/tasks/${TASK_ID}$`));
    await expect(page.getByText(/source status|fetching|completed/i).first()).toBeVisible();
    await expect
      .poll(
        () =>
          fixture.requests.filter(
            (request) =>
              request.pathname === `/api/v1/tasks/${TASK_ID}` && request.method === 'GET',
          ).length,
      )
      .toBeGreaterThan(0);
    const eventRequests = fixture.requests.filter(
      (request) => request.pathname === `/api/v1/tasks/${TASK_ID}/events`,
    );
    expect(eventRequests.length).toBeGreaterThan(0);
    expect(eventRequests.every((request) => !request.body?.includes(PRIVATE_MARKER))).toBe(true);
  });

  test('recovers a 410 task stream through REST and resumes live events', async ({ page }) => {
    const fixture = await installFixture(page, { stream: 'gone-then-live' });
    await page.goto(`/tasks/${TASK_ID}`);
    await expect(page.getByText(/source status|fetching|completed/i).first()).toBeVisible();
    await expect
      .poll(
        () =>
          fixture.requests.filter(
            (request) => request.pathname === `/api/v1/tasks/${TASK_ID}/events`,
          ).length,
      )
      .toBeGreaterThanOrEqual(2);
    await expect.poll(() => fixture.state.version).toBe(4);
    await expect
      .poll(
        () =>
          fixture.requests.filter(
            (request) =>
              request.pathname === `/api/v1/tasks/${TASK_ID}` && request.method === 'GET',
          ).length,
      )
      .toBeGreaterThan(1);
  });

  test('shows source status and routes capability-gated cancel without inventing recovery', async ({
    page,
  }) => {
    const fixture = await installFixture(page);
    await page.goto(`/tasks/${TASK_ID}`);
    await expect(page.getByText(/source status|fetching|completed/i).first()).toBeVisible();
    const cancel = page.getByRole('button', { name: /cancel/i }).first();
    if (await cancel.isVisible()) {
      await cancel.click();
      const confirm = page.getByRole('alertdialog').getByRole('button', { name: /cancel/i });
      await expect(confirm).toBeVisible();
      await confirm.click();
      await expect
        .poll(
          () =>
            fixture.requests.filter(
              (request) =>
                request.pathname === `/api/v1/tasks/${TASK_ID}/cancel` && request.method === 'POST',
            ).length,
        )
        .toBe(1);
    }
    await expect(page.getByText(/source status|fetching|completed/i).first()).toBeVisible();
  });

  test('renders an inert artifact preview, revision history, download, and truthful deletion', async ({
    page,
  }) => {
    const fixture = await installFixture(page);
    await page.goto(`/artifacts/${ARTIFACT_ID}`);
    await expect(page.getByRole('heading', { name: /research report/i }).first()).toBeVisible();
    await expect(page.getByText(/safe report text/i)).toBeVisible();
    await expect(page.locator('.preview script')).toHaveCount(0);
    await expect(page.getByText(/revision 2|sha-256/i).first()).toBeVisible();
    await expect(page.getByRole('button', { name: /download/i })).toBeVisible();
    const deleteButton = page.getByRole('button', { name: /delete artifact/i });
    if (await deleteButton.isVisible()) {
      await deleteButton.click();
      const confirm = page.getByRole('button', { name: /^delete$/i });
      await expect(confirm).toBeVisible();
      await confirm.click();
      await expect.poll(() => fixture.state.deleteCalls).toBeGreaterThan(0);
      await expect(
        page.getByText(/deletion pending|artifact deleted|cleanup|no longer readable/i).first(),
      ).toBeVisible();
    }
  });

  test('keeps deletion from resurrecting deferred evidence', async ({ page }) => {
    const fixture = await installFixture(page, { evidenceDelayMs: 250 });
    await page.goto(`/research/${RESEARCH_ID}`);
    await expect(
      page.getByRole('heading', { name: /compare owner-private fixture sources/i }),
    ).toBeVisible();
    const deleteButton = page.getByRole('button', { name: /delete retained research/i });
    await expect(deleteButton).toBeVisible();
    await deleteButton.click();
    await page
      .getByRole('alertdialog')
      .getByRole('button', { name: /^delete$/i })
      .click();
    await expect.poll(() => fixture.state.deleteCalls).toBe(1);
    await page.waitForTimeout(300);
    await expect(page.locator('.evidence')).toHaveCount(0);
    await expect(page.getByText('Fixture source', { exact: true })).toHaveCount(0);
  });

  test('clears artifact preview and session state after detail or download authorization loss', async ({
    page,
  }) => {
    for (const status of [401, 403] as const) {
      await page.unrouteAll({ behavior: 'ignoreErrors' });
      const detailFixture = await installFixture(page, { artifactDetailStatus: status });
      await page.goto(`/artifacts/${ARTIFACT_ID}`);
      await expect(page.getByRole('status')).toContainText(/Signed out/i);
      await expect(page.locator('.preview')).toHaveCount(0);
      await expect(page.locator('.detail-card')).toHaveCount(0);
      const sessionReadsAfterDetail = detailFixture.state.sessionReads;
      await page.goto('/artifacts');
      await expect
        .poll(() => detailFixture.state.sessionReads)
        .toBeGreaterThan(sessionReadsAfterDetail);
    }
    for (const status of [401, 403] as const) {
      await page.unrouteAll({ behavior: 'ignoreErrors' });
      const downloadFixture = await installFixture(page, { downloadStatus: status });
      await page.goto(`/artifacts/${ARTIFACT_ID}`);
      await expect(page.getByText(/safe report text/i)).toBeVisible();
      await page.getByRole('button', { name: /download/i }).click();
      await expect(page.getByRole('status')).toContainText(/Signed out/i);
      await expect(page.locator('.preview')).toHaveCount(0);
      const sessionReadsAfterDownload = downloadFixture.state.sessionReads;
      await page.goto('/artifacts');
      await expect
        .poll(() => downloadFixture.state.sessionReads)
        .toBeGreaterThan(sessionReadsAfterDownload);
    }
  });

  test('renders structural loading skeletons while research and artifact lists settle', async ({
    page,
  }) => {
    await installFixture(page, { settingsDelayMs: 250 });
    await page.goto('/research');
    await expect(page.locator('.loading-skeleton')).toBeVisible();
    await expect(
      page.getByRole('heading', { name: /what should aura investigate/i }),
    ).toBeVisible();
    await page.unrouteAll({ behavior: 'ignoreErrors' });
    await installFixture(page, { artifactListDelayMs: 250 });
    await page.goto('/artifacts');
    await expect(page.locator('.loading-skeleton')).toBeVisible();
    await expect(page.getByRole('heading', { name: /your artifacts/i })).toBeVisible();
  });

  test('applies artifact type and time filters without losing the current artifact', async ({
    page,
  }) => {
    const fixture = await installFixture(page, { artifactUpdatedAt: NOW });
    await page.goto('/artifacts');
    await expect(page.locator('.artifact-card')).toHaveCount(1);
    const filters = page.locator('.filters select');
    await filters.nth(0).selectOption('text/plain');
    await expect
      .poll(
        () =>
          fixture.requests.filter((request) => request.pathname === '/api/v1/artifacts').at(-1)
            ?.query ?? '',
      )
      .toContain('mediaType=text%2Fplain');
    await filters.nth(1).selectOption('7d');
    await expect(page.locator('.artifact-card')).toHaveCount(1);
    await expect(page.getByText(/research report/i).first()).toBeVisible();
  });

  test('clears memory-only research drafts after authorization generation changes', async ({
    page,
  }) => {
    await installFixture(page, { signedOutAfterFirstSession: true });
    await page.goto('/research');
    const question = page.getByLabel(/research question/i);
    await question.fill(PRIVATE_MARKER);
    await assertNoPrivateBrowserPersistence(page, PRIVATE_MARKER);
    await page.reload();
    await expect(page.getByText(PRIVATE_MARKER, { exact: true })).toHaveCount(0);
    if (await question.count()) await expect(question).toHaveValue('');
  });

  test('resets loading and saving state to content-free signed-out UI on auth loss', async ({
    page,
  }) => {
    for (const status of [401, 403] as const) {
      await page.unrouteAll({ behavior: 'ignoreErrors' });
      await installFixture(page, { researchDetailStatus: status });
      await page.goto(`/research/${RESEARCH_ID}`);
      await expect(page.getByRole('status')).toContainText(/Signed out/i);
      await expect(page.locator('.detail-card')).toHaveCount(0);
      await expect(page.getByText(/Loading research details/i)).toHaveCount(0);
      await expect(page.getByText(PRIVATE_MARKER, { exact: true })).toHaveCount(0);
    }
  });

  test('keeps core controls usable at 200 percent zoom with reduced motion and no horizontal loss', async ({
    page,
  }, testInfo) => {
    await installFixture(page);
    await page.emulateMedia({ reducedMotion: 'reduce' });
    await page.goto('/research');
    await page.evaluate(() => {
      document.documentElement.style.zoom = '2';
    });
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= document.documentElement.clientWidth,
      ),
    ).toBe(true);
    const motion = await page.locator('body').evaluate((body) => ({
      reduced: matchMedia('(prefers-reduced-motion: reduce)').matches,
      transition: getComputedStyle(body).transitionDuration,
    }));
    expect(motion.reduced).toBe(true);
    expect(Number.parseFloat(motion.transition.replace('s', ''))).toBeLessThan(0.1);
    await expect(page.getByRole('button', { name: /start research/i })).toBeVisible();
    const axe = await new AxeBuilder({ page }).include('.research-composer').analyze();
    expect(axe.violations).toEqual([]);
    expect(testInfo.project.name).toMatch(/desktop|tablet|mobile/);
  });

  test('preserves every memory-only field on rejected admission and clears them only on explicit discard', async ({
    page,
  }) => {
    await installFixture(page, { admissionStatus: 409 });
    await page.goto('/research');
    const question = page.getByLabel(/research question/i);
    const domains = page.getByPlaceholder(/example\.com, docs/i);
    const seeds = page.getByPlaceholder(/https:\/\/example\.com/i);
    await page.getByText(/advanced settings/i).click();
    await question.fill(PRIVATE_MARKER);
    await domains.fill('docs.example.test, standards.example.test');
    await seeds.fill('https://example.test/seed');
    await page.getByRole('button', { name: /start research/i }).click();
    await expect(question).toHaveValue(PRIVATE_MARKER);
    await expect(domains).toHaveValue('docs.example.test, standards.example.test');
    await expect(seeds).toHaveValue('https://example.test/seed');
    await page.getByRole('button', { name: /discard draft/i }).click();
    await expect(question).toHaveValue('');
    await expect(domains).toHaveValue('');
    await expect(seeds).toHaveValue('');
  });

  test('uses opaque fresh idempotency keys after a settled rejected admission', async ({
    page,
  }) => {
    const fixture = await installFixture(page, { admissionStatus: 409 });
    await page.goto('/research');
    const question = page.getByLabel(/research question/i);
    await question.fill(PRIVATE_MARKER);
    const start = page.getByRole('button', { name: /start research/i });
    await start.click();
    await expect
      .poll(
        () =>
          fixture.requests.filter(
            (request) => request.pathname === '/api/v1/research' && request.method === 'POST',
          ).length,
      )
      .toBe(1);
    await start.click();
    await expect
      .poll(
        () =>
          fixture.requests.filter(
            (request) => request.pathname === '/api/v1/research' && request.method === 'POST',
          ).length,
      )
      .toBe(2);
    const keys = fixture.requests
      .filter((request) => request.pathname === '/api/v1/research' && request.method === 'POST')
      .map((request) => request.headers['idempotency-key']);
    expect(keys).toHaveLength(2);
    expect(new Set(keys).size).toBe(2);
    expect(keys.every((key) => /^[0-9a-f]{32}$/i.test(key))).toBe(true);
    expect(keys.every((key) => !key.includes(PRIVATE_MARKER))).toBe(true);
    expect(keys).not.toContain(inputFingerprint(`quick:${PRIVATE_MARKER}:[]:[]`));
  });

  test('clears all draft fields after successful admitted research', async ({ page }) => {
    const fixture = await installFixture(page);
    await page.goto('/research');
    const question = page.getByLabel(/research question/i);
    const domains = page.getByPlaceholder(/example\.com, docs/i);
    const seeds = page.getByPlaceholder(/https:\/\/example\.com/i);
    await page.getByText(/advanced settings/i).click();
    await question.fill(PRIVATE_MARKER);
    await domains.fill('docs.example.test');
    await seeds.fill('https://example.test/seed');
    await page.getByRole('button', { name: /start research/i }).click();
    await expect
      .poll(
        () =>
          fixture.requests.filter(
            (request) => request.pathname === '/api/v1/research' && request.method === 'POST',
          ).length,
      )
      .toBeGreaterThan(0);
    await expect(page).toHaveURL(new RegExp(`/research/${RESEARCH_ID}$`));
    await expect(
      page.getByRole('heading', { name: /compare owner-private fixture sources/i }),
    ).toBeVisible();
    await page.getByRole('button', { name: /back to research/i }).click();
    await expect(page).toHaveURL(/\/research$/);
    await expect(page.getByLabel(/research question/i)).toHaveValue('');
    await page.getByText(/advanced settings/i).click();
    const returnedDomains = page.getByPlaceholder(/example\.com, docs/i);
    const returnedSeeds = page.getByPlaceholder(/https:\/\/example\.com/i);
    await expect(returnedDomains).toHaveValue('');
    await expect(returnedSeeds).toHaveValue('');
    await expect(page.locator('body')).not.toContainText(PRIVATE_MARKER);
    const persisted = await page.evaluate(() =>
      [...Object.entries(localStorage), ...Object.entries(sessionStorage)].flat().join('\n'),
    );
    expect(persisted).not.toContain(PRIVATE_MARKER);
  });

  test('renders terminal partial, budget exhausted, interrupted, and failed outcomes without inventing success', async ({
    page,
  }) => {
    for (const state of ['partial', 'budget_exhausted', 'interrupted', 'failed'] as const) {
      await page.unrouteAll({ behavior: 'ignoreErrors' });
      const fixture = await installFixture(page, { researchState: state });
      await page.goto(`/research/${RESEARCH_ID}`);
      await expect(
        page.getByRole('heading', { name: /compare owner-private fixture sources/i }),
      ).toBeVisible();
      await expect(page.getByText(new RegExp(state.replace('_', ' '), 'i')).first()).toBeVisible();
      expect(
        fixture.requests.some((request) => request.pathname === `/api/v1/research/${RESEARCH_ID}`),
      ).toBe(true);
    }
  });

  test('preserves rejected and unknown command outcomes when REST stays at the same version', async ({
    page,
  }) => {
    for (const commandState of ['rejected', 'unknown'] as const) {
      await page.unrouteAll({ behavior: 'ignoreErrors' });
      const fixture = await installFixture(page, { commandState });
      await page.goto(`/tasks/${TASK_ID}`);
      const cancel = page.getByRole('button', { name: /cancel task/i }).first();
      await cancel.click();
      await page
        .getByRole('alertdialog')
        .getByRole('button', { name: /cancel task/i })
        .click();
      await expect(
        page.getByRole('status').filter({ hasText: /Command|Outcome unknown/i }),
      ).toContainText(commandState === 'rejected' ? /Command rejected/i : /Outcome unknown/i);
      await page.waitForTimeout(250);
      await expect(page.getByText(/fetching/i).first()).toBeVisible();
      expect(fixture.state.version).toBe(3);
      expect(
        fixture.requests.filter((request) => request.pathname.endsWith('/cancel')).length,
      ).toBe(1);
    }
  });

  test('reconciles SSE gaps with Last-Event-ID and keeps malformed streams visible as disconnected', async ({
    page,
  }) => {
    const fixture = await installFixture(page, { stream: 'reconnect-gap' });
    await page.goto(`/tasks/${TASK_ID}`);
    await expect(page.getByText(/source status|fetching|completed/i).first()).toBeVisible();
    await expect
      .poll(
        () =>
          fixture.requests.filter(
            (request) => request.pathname === `/api/v1/tasks/${TASK_ID}/events`,
          ).length,
      )
      .toBeGreaterThan(1);
    const eventRequests = fixture.requests.filter(
      (request) => request.pathname === `/api/v1/tasks/${TASK_ID}/events`,
    );
    expect(eventRequests[0]?.headers.accept).toContain('text/event-stream');
    expect(eventRequests[1]?.headers['last-event-id']).toBeTruthy();
  });

  test('keeps unknown command receipts non-repeatable until reconciliation', async ({ page }) => {
    const fixture = await installFixture(page, { commandState: 'unknown' });
    await page.goto(`/tasks/${TASK_ID}`);
    const cancel = page.getByRole('button', { name: /cancel task/i });
    await expect(cancel).toBeVisible();
    await cancel.click();
    const confirm = page.getByRole('alertdialog').getByRole('button', { name: /cancel task/i });
    await expect(confirm).toBeVisible();
    await confirm.click();
    await expect(page.getByText(/outcome unknown|reconcil/i).first()).toBeVisible();
    await cancel.click();
    expect(
      fixture.requests.filter((request) => request.pathname === `/api/v1/tasks/${TASK_ID}/cancel`)
        .length,
    ).toBe(1);
  });

  test('rejects unsafe, missing-length, and oversized artifact downloads', async ({ page }) => {
    for (const downloadVariant of [
      'html',
      'executable',
      'media-unavailable',
      'missing-length',
      'oversize',
    ] as const) {
      await page.unrouteAll({ behavior: 'ignoreErrors' });
      await installFixture(page, { downloadVariant });
      await page.goto(`/artifacts/${ARTIFACT_ID}`);
      await page.getByRole('button', { name: /download/i }).click();
      await expect(
        page.getByRole('alert').filter({ hasText: /authorized revision|download/i }),
      ).toBeVisible();
    }
  });

  test('preserves the current artifact opener when deletion confirmation is escaped', async ({
    page,
  }) => {
    await installFixture(page);
    await page.goto(`/artifacts/${ARTIFACT_ID}`);
    const deleteButton = page.getByRole('button', { name: /delete artifact/i });
    await deleteButton.focus();
    await deleteButton.click();
    await expect(
      page.getByRole('alertdialog', { name: /delete artifact and retained content/i }),
    ).toBeVisible();
    await page.keyboard.press('Escape');
    await expect(
      page.getByRole('alertdialog', { name: /delete artifact and retained content/i }),
    ).toHaveCount(0);
    await expect(deleteButton).toBeFocused();
  });

  test('keeps task confirmation safe-first and restores the opener on Escape', async ({ page }) => {
    await installFixture(page);
    await page.goto(`/tasks/${TASK_ID}`);
    const cancel = page.getByRole('button', { name: /cancel task/i }).first();
    await cancel.focus();
    await cancel.click();
    const dialog = page.getByRole('alertdialog');
    await expect(dialog).toBeVisible();
    await expect(dialog.getByRole('button', { name: /keep task/i })).toBeFocused();
    await page.keyboard.press('Escape');
    await expect(dialog).toHaveCount(0);
    await expect(cancel).toBeFocused();
  });

  test('returns focus to the deletion status after research deletion', async ({ page }) => {
    await installFixture(page);
    await page.goto(`/research/${RESEARCH_ID}`);
    const deleteButton = page.getByRole('button', { name: /delete retained research/i });
    await deleteButton.click();
    await page
      .getByRole('alertdialog')
      .getByRole('button', { name: /^delete$/i })
      .click();
    await expect(page.getByRole('status').filter({ hasText: /deletion accepted/i })).toBeFocused();
  });

  test('gates the research composer on authoritative settings and reports budget errors', async ({
    page,
  }) => {
    const unavailable = await installFixture(page, { settingsStatus: 503 });
    await page.goto('/research');
    await expect(page.getByRole('status', { name: 'Loading research settings' })).toHaveAttribute(
      'aria-label',
      'Loading research settings',
    );
    await expect(page.getByRole('button', { name: /start research/i })).toHaveCount(0);
    expect(
      unavailable.requests.some(
        (request) => request.pathname === '/api/v1/research' && request.method === 'POST',
      ),
    ).toBe(false);

    await page.unrouteAll({ behavior: 'ignoreErrors' });
    const fixture = await installFixture(page);
    await page.goto('/research');
    await page.getByText(/advanced settings/i).click();
    const wallTime = page.locator('input[type="number"]').first();
    await wallTime.fill('91');
    await expect(
      page.getByText('Wall time seconds cannot exceed 90.', { exact: true }),
    ).toBeVisible();
    await expect(page.getByRole('button', { name: /start research/i })).toBeDisabled();
    expect(
      fixture.requests.some(
        (request) => request.pathname === '/api/v1/research' && request.method === 'POST',
      ),
    ).toBe(false);
  });

  test('rejects budget dimensions below one while allowing zero browser pages', async ({
    page,
  }) => {
    await installFixture(page);
    await page.goto('/research');
    await page.getByText(/advanced settings/i).click();
    const fields = [
      ['Wall time seconds', 'wallTimeSeconds'],
      ['Fetched sources', 'fetchedSources'],
      ['Search requests', 'searchRequests'],
      ['Browser pages', 'browserPages'],
      ['Model calls', 'modelCalls'],
    ] as const;
    for (const [label, field] of fields) {
      const input = page
        .locator('.advanced-grid input[type="number"]')
        .nth(fields.findIndex(([candidate]) => candidate === label));
      await input.evaluate((element) => {
        const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
        setter?.call(element, '0');
        element.dispatchEvent(new Event('input', { bubbles: true }));
        element.dispatchEvent(new Event('change', { bubbles: true }));
      });
      if (field === 'browserPages') {
        await expect(page.getByText(`${label} must be at least 1.`, { exact: true })).toHaveCount(
          0,
        );
      } else {
        await expect(page.getByText(`${label} must be at least 1.`, { exact: true })).toBeVisible();
      }
    }
    await expect(page.getByRole('button', { name: /start research/i })).toBeDisabled();
  });

  test('validates one-time extensions against admitted and operator budgets', async ({ page }) => {
    const fixture = await installFixture(page);
    await page.goto(`/research/${RESEARCH_ID}`);
    await page.getByRole('button', { name: /extend once/i }).click();
    const extensionFields = page.locator('[aria-label="Extend research"] input[type="number"]');
    await extensionFields.first().fill('11');
    await expect(
      page.getByText('Wall time seconds must be at least the admitted 90.', { exact: true }),
    ).toBeVisible();
    await page.getByRole('button', { name: /confirm extension/i }).click();
    expect(fixture.requests.some((request) => request.pathname.endsWith('/extensions'))).toBe(
      false,
    );
  });

  test('announces admitted research and focuses the current activity heading', async ({ page }) => {
    await installFixture(page);
    await page.goto('/research');
    await page.getByLabel(/research question/i).fill('A focus-safe fixture question');
    await page.getByRole('button', { name: /start research/i }).click();
    await expect(page).toHaveURL(new RegExp(`/research/${RESEARCH_ID}$`));
    await expect(
      page.getByRole('heading', { name: /compare owner-private fixture sources/i }),
    ).toBeFocused();
    await expect(page.getByText('Research started.', { exact: true })).toBeVisible();
  });

  test('sends authoritative Task Center status, source, time, and cursor filters without stale races', async ({
    page,
  }) => {
    const fixture = await installFixture(page, { taskListDelayMs: 40, taskPageCursor: 'cursor-2' });
    await page.goto('/tasks');
    await expect(page.locator('.task-card')).toHaveCount(1);
    const filters = page.locator('.filters select');
    await filters.nth(0).selectOption('running');
    await filters.nth(1).selectOption('research');
    await page.locator('.filters input[type="datetime-local"]').first().fill('2026-10-29T00:00');
    await expect
      .poll(() => {
        const query = fixture.requests
          .filter((request) => request.pathname === '/api/v1/tasks')
          .map((request) => request.query)
          .find(
            (value) =>
              value.includes('status=running') &&
              value.includes('sourceKind=research') &&
              value.includes('updatedFrom=2026-10-29T00%3A00'),
          );
        return query ?? '';
      })
      .not.toBe('');
    const latest = fixture.requests
      .filter(
        (request) =>
          request.pathname === '/api/v1/tasks' &&
          request.query.includes('status=running') &&
          request.query.includes('sourceKind=research') &&
          request.query.includes('updatedFrom=2026-10-29T00%3A00'),
      )
      .at(-1);
    expect(latest?.query).toContain('status=running');
    expect(latest?.query).toContain('updatedFrom=2026-10-29T00%3A00');
    await page.getByRole('button', { name: /next page/i }).click();
    await expect
      .poll(
        () =>
          fixture.requests.filter((request) => request.pathname === '/api/v1/tasks').at(-1)
            ?.query ?? '',
      )
      .toContain('cursor=cursor-2');
    await expect(page.locator('.task-card')).toHaveCount(1);
  });

  test('bounds CRLF, malformed, expired, and clean-EOF task streams', async ({ page }) => {
    for (const stream of ['crlf-chunk', 'malformed', 'gone', 'clean-eof'] as const) {
      await page.unrouteAll({ behavior: 'ignoreErrors' });
      const fixture = await installFixture(page, { stream });
      await page.goto(`/tasks/${TASK_ID}`);
      await expect(page.getByText(/source status|fetching|completed/i).first()).toBeVisible();
      await expect
        .poll(
          () => fixture.requests.filter((request) => request.pathname.endsWith('/events')).length,
        )
        .toBeGreaterThan(0);
      await page.waitForTimeout(250);
      expect(
        fixture.requests.filter((request) => request.pathname.endsWith('/events')).length,
      ).toBeLessThanOrEqual(6);
    }
  });

  test('ignores a late detail response after the same-document route changes', async ({ page }) => {
    const fixture = await installFixture(page, { detailDelayMs: 150 });
    await page.goto(`/tasks/${TASK_ID}`);
    await expect
      .poll(() =>
        fixture.requests.some((request) => request.pathname === `/api/v1/tasks/${TASK_ID}`),
      )
      .toBe(true);
    await page.goto('/tasks');
    await expect(page).toHaveURL(/\/tasks$/);
    await expect(page.locator('.task-card')).toHaveCount(1);
    await page.waitForTimeout(220);
    await expect(page.locator('.detail-card')).toHaveCount(0);
    expect(
      fixture.requests.some((request) => request.pathname === `/api/v1/tasks/${TASK_ID}`),
    ).toBe(true);
  });

  test('does not apply a non-abortable task mutation after leaving its detail route', async ({
    page,
  }) => {
    const fixture = await installFixture(page, { mutationDelayMs: 150 });
    await page.goto(`/tasks/${TASK_ID}`);
    const cancel = page.getByRole('button', { name: /cancel task/i });
    await cancel.click();
    const confirm = page.getByRole('alertdialog').getByRole('button', { name: /cancel task/i });
    await expect(confirm).toBeVisible();
    await confirm.click();
    await page.goto('/tasks');
    await expect(page).toHaveURL(/\/tasks$/);
    await page.waitForTimeout(220);
    await expect(page.locator('.detail-card')).toHaveCount(0);
    expect(fixture.requests.some((request) => request.pathname.endsWith('/cancel'))).toBe(true);
  });

  test('renders running, detached, and terminal conversation research activity with links', async ({
    page,
  }) => {
    const lifecycleLabels = {
      running: 'Running',
      detached: 'Continuing in the background',
      terminal: 'Complete',
    } as const;
    const lifecycleAnnouncements = {
      running: 'Research is running.',
      detached: 'Research is continuing in the background.',
      terminal: 'Research completed.',
    } as const;
    for (const activity of ['running', 'detached', 'terminal'] as const) {
      await page.unrouteAll({ behavior: 'ignoreErrors' });
      await installFixture(page, { conversationActivity: activity });
      await page.goto(`/conversation/${CONVERSATION_ID}`);
      const card = page.locator('aura-research-activity');
      await expect(card).toBeVisible();
      await expect(card).toContainText(lifecycleLabels[activity]);
      if (activity === 'detached')
        await expect(
          card.getByText('Continuing in the background.', { exact: true }),
        ).toBeVisible();
      await expect(card.getByText('Open result', { exact: true })).toBeVisible();
      await expect(card.locator('.research-activity-live')).toContainText(
        lifecycleAnnouncements[activity],
      );
      await expect(card.getByRole('link', { name: /open result/i })).toHaveAttribute(
        'href',
        `/artifacts/${ARTIFACT_ID}`,
      );
      await expect(card.getByRole('link', { name: /open task/i })).toHaveAttribute(
        'href',
        `/tasks/${TASK_ID}`,
      );
    }
    await expect(page.locator('[role="status"][aria-live="polite"]').first()).toBeVisible();
  });

  test('supports successful Markdown, plain text, and JSON artifact downloads', async ({
    page,
  }) => {
    for (const downloadMediaType of ['markdown', 'plain', 'json'] as const) {
      await page.unrouteAll({ behavior: 'ignoreErrors' });
      await installFixture(page, { downloadMediaType });
      await page.goto(`/artifacts/${ARTIFACT_ID}`);
      const downloadPromise = page.waitForEvent('download');
      await page.getByRole('button', { name: /download/i }).click();
      const download = await downloadPromise;
      expect(await download.failure()).toBeNull();
    }
  });

  test('shows and follows a visible artifact Load more cursor', async ({ page }) => {
    const fixture = await installFixture(page, { artifactPageCursor: 'artifact-next-page' });
    await page.goto('/artifacts');
    await expect(page.locator('.artifact-card').first()).toBeVisible();
    const loadMore = page.getByRole('button', { name: /load more artifacts/i });
    await expect(loadMore).toBeVisible();
    await loadMore.click();
    await expect
      .poll(
        () =>
          fixture.requests.filter(
            (request) =>
              request.pathname === '/api/v1/artifacts' &&
              request.method === 'GET' &&
              request.query.includes('cursor=artifact-next-page'),
          ).length,
      )
      .toBeGreaterThan(0);
  });

  test('removes artifact preview and revisions after deletion and preserves inert content', async ({
    page,
  }) => {
    await installFixture(page);
    await page.goto(`/artifacts/${ARTIFACT_ID}`);
    await expect(page.locator('.preview script')).toHaveCount(0);
    await page.getByRole('button', { name: /delete artifact/i }).click();
    await page.getByRole('button', { name: /^delete$/i }).click();
    await expect(
      page.getByText(/deletion pending|artifact deleted|cleanup/i).first(),
    ).toBeVisible();
    await expect(page.locator('.preview')).toHaveCount(0);
    await expect(page.locator('.revisions li')).toHaveCount(0);
  });

  test('exposes task progress, outcome, capabilities, confirmation focus, and forced-colors detail', async ({
    page,
  }) => {
    const fixture = await installFixture(page, { researchState: 'failed' });
    await page.goto('/tasks');
    await expect(page.locator('.task-card')).toContainText(/100%|provider unavailable|failed/i);
    await page.locator('.task-card').click();
    await expect(
      page.getByText(/provider unavailable|retry from the owning service/i),
    ).toBeVisible();
    await page.emulateMedia({ forcedColors: 'active' });
    await page.getByRole('link', { name: 'Open research source', exact: true }).click();
    await expect(page).toHaveURL(new RegExp(`/research/${RESEARCH_ID}$`));
    expect(await page.evaluate(() => matchMedia('(forced-colors: active)').matches)).toBe(true);
    expect(
      fixture.requests.some((request) => request.pathname === `/api/v1/tasks/${TASK_ID}`),
    ).toBe(true);
  });

  test('keeps Task Center and Artifacts usable at 200 percent zoom', async ({ page }, testInfo) => {
    for (const [path, heading] of [
      ['/tasks', 'Task Center'],
      ['/artifacts', 'Artifacts'],
    ] as const) {
      await page.unrouteAll({ behavior: 'ignoreErrors' });
      await installFixture(page);
      await page.goto(path);
      await expect(page.getByRole('heading', { name: heading, exact: true })).toBeVisible();
      await page.evaluate(() => {
        document.documentElement.style.zoom = '2';
      });
      expect(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= document.documentElement.clientWidth,
        ),
      ).toBe(true);
    }
    expect(testInfo.project.name).toMatch(/desktop|tablet|mobile/);
  });

  test('keeps ordinary dark-mode task text at computed readable contrast', async ({ page }) => {
    await installFixture(page);
    await page.goto('/tasks');
    await page.evaluate(() => {
      document.documentElement.dataset['theme'] = 'dark';
      document.documentElement.style.colorScheme = 'dark';
    });
    await expect(page.locator('.task-card')).toBeVisible();
    const contrast = await page.locator('.task-card').evaluate((element) => {
      const parse = (value: string): [number, number, number] => {
        const match = value.match(/rgba?\((\d+),\s*(\d+),\s*(\d+)/);
        if (!match) throw new Error(`Unexpected color ${value}`);
        return [Number(match[1]), Number(match[2]), Number(match[3])];
      };
      const luminance = ([red, green, blue]: [number, number, number]): number =>
        [red, green, blue]
          .map((channel) => channel / 255)
          .map((channel) =>
            channel <= 0.03928 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4,
          )
          .reduce((total, channel, index) => total + channel * [0.2126, 0.7152, 0.0722][index], 0);
      const foreground = luminance(parse(getComputedStyle(element).color));
      const background = luminance(parse(getComputedStyle(element).backgroundColor));
      return (Math.max(foreground, background) + 0.05) / (Math.min(foreground, background) + 0.05);
    });
    expect(contrast).toBeGreaterThanOrEqual(4.5);
  });
});
