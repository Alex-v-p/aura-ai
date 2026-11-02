import { applicationConfig } from '@storybook/angular';
import type { Meta, StoryObj } from '@storybook/angular';
import {
  ActivatedRoute,
  convertToParamMap,
  provideRouter,
  withDisabledInitialNavigation,
} from '@angular/router';
import { signal } from '@angular/core';
import { of } from 'rxjs';
import type {
  ActiveGeneratedArtifactDetail,
  ActiveResearchDetail,
  DeletedGeneratedArtifactTombstone,
  DurableTaskDetail,
  GeneratedArtifactPage,
  GeneratedArtifactPreview,
  GeneratedArtifactRevision,
  ResearchBudget,
  ResearchMode,
  ResearchPage,
  ResearchSummary,
  ResearchSettings,
} from '@aura/aura-api-client';
import { ResearchPageComponent, ResearchStore } from '@aura/aura/knowledge/research';
import { ArtifactsPageComponent, ArtifactsStore } from '@aura/aura/knowledge/artifacts';
import { TasksPageComponent, TasksStore } from '@aura/aura/activity/tasks';

const now = '2026-10-30T10:00:00.000Z';
const researchId = 'storybook-research-0147';
const taskId = 'storybook-task-0147';
const artifactId = 'storybook-artifact-0147';
const sourceId = 'storybook-source-0147';

const budget = (wallTimeSeconds: number, fetchedSources: number): ResearchBudget => ({
  wallTimeSeconds,
  fetchedSources,
  searchRequests: Math.max(1, Math.ceil(fetchedSources / 3)),
  browserPages: Math.min(4, Math.ceil(fetchedSources / 8)),
  modelCalls: 3,
});

const settings: ResearchSettings = {
  version: 3,
  presets: [
    { mode: 'quick', maximum: budget(90, 6), defaults: budget(90, 6) },
    { mode: 'auto', maximum: budget(600, 15), defaults: budget(300, 8) },
    { mode: 'deep', maximum: budget(1800, 30), defaults: budget(900, 18) },
    { mode: 'agent_led', maximum: budget(3600, 60), defaults: budget(1200, 24) },
  ],
  updatedAt: now,
};

const research: ActiveResearchDetail = {
  id: researchId,
  taskRef: { taskId },
  mode: 'quick',
  stage: 'fetching',
  outcome: null,
  contentState: 'active',
  version: 3,
  capabilities: {
    canCancel: true,
    canExtend: true,
    canDelete: true,
    canRetryCleanup: false,
    canReconcileCleanup: false,
  },
  createdAt: now,
  updatedAt: now,
  finishedAt: null,
  question: 'Compare local-first research tools using public evidence',
  admittedBudget: budget(90, 6),
  consumedBudget: { ...budget(90, 6), fetchedSources: 2, searchRequests: 1 },
  extensionAccepted: false,
  sourceCount: 2,
  failureClass: null,
  artifactRefs: [{ artifactId }],
  citations: [
    {
      kind: 'web',
      sourceId,
      label: 'Fixture source',
      url: 'https://example.test/research',
      retrievedAt: now,
      locator: 'Overview',
    },
  ],
};

const task: DurableTaskDetail = {
  id: taskId,
  sourceKind: 'research',
  sourceId: researchId,
  sourceVersion: 3,
  status: 'running',
  sourceStatus: 'fetching',
  sourceOutcome: null,
  stage: 'fetching',
  progress: 0.35,
  capabilities: { canCancel: true, canRetry: true, hasSourceDetail: true },
  routeKey: 'research_detail',
  createdAt: now,
  updatedAt: now,
  displayLabel: 'Compare local-first research tools using public evidence',
  failureClass: null,
  recoveryAction: null,
  artifactRefs: [{ artifactId }],
};

const artifact: ActiveGeneratedArtifactDetail = {
  id: artifactId,
  kind: 'research_report',
  title: 'Research report.md',
  mediaType: 'text/markdown; charset=utf-8',
  contentState: 'active',
  currentRevision: 2,
  version: 4,
  capabilities: {
    canPreview: true,
    canDownload: true,
    canDelete: true,
    canRetryCleanup: false,
    canReconcileCleanup: false,
  },
  createdAt: now,
  updatedAt: now,
  lineage: { taskId, runId: null, conversationId: null, workspaceId: null },
  revision: {
    revision: 2,
    mediaType: 'text/markdown; charset=utf-8',
    sha256: 'a'.repeat(64),
    byteCount: 128,
    creatorComponent: 'aura.research.report',
    previousRevision: 1,
    createdAt: now,
  },
};

function routeFor(id?: string): ActivatedRoute {
  const paramMap = convertToParamMap(id ? { id } : {});
  return { paramMap: of(paramMap), snapshot: { paramMap } } as unknown as ActivatedRoute;
}

function storySettings(mode: ResearchMode): ResearchSettings {
  const wallTimes: Record<ResearchMode, number> = {
    quick: 90,
    auto: 360,
    deep: 1_200,
    agent_led: 2_400,
  };
  return {
    ...settings,
    presets: settings.presets.map((item) =>
      item.mode === mode
        ? { ...item, defaults: { ...item.defaults, wallTimeSeconds: wallTimes[mode] } }
        : item,
    ),
  };
}

function researchStore(
  value: ActiveResearchDetail | null = research,
  settingsValue: ResearchSettings = settings,
): ResearchStore {
  const summary: ResearchSummary | null = value
    ? {
        id: value.id,
        taskRef: value.taskRef,
        mode: value.mode,
        stage: value.stage,
        outcome: value.outcome,
        contentState: value.contentState,
        version: value.version,
        capabilities: value.capabilities,
        createdAt: value.createdAt,
        updatedAt: value.updatedAt,
        finishedAt: value.finishedAt,
      }
    : null;
  const page: ResearchPage = { items: summary ? [summary] : [], nextCursor: null };
  return {
    page: signal(page),
    activeItems: signal(page.items),
    selected: signal(value),
    evidence: signal({ items: [], nextCursor: null }),
    settings: signal(settingsValue),
    loading: signal(false),
    saving: signal(false),
    error: signal(null),
    signedOut: signal(false),
    selectedId: signal(value?.id ?? null),
    resetEpoch: signal(0),
    load: async () => undefined,
    loadSettings: async () => settingsValue,
    loadDetail: async () => undefined,
    create: async () => ({
      researchId,
      taskRef: { taskId },
      mode: 'quick',
      admissionState: 'admitted',
      admittedBudget: budget(90, 6),
      version: 1,
      createdAt: now,
    }),
    saveSettings: async () => settings,
    cancel: async () => undefined,
    extend: async () => undefined,
    delete: async () => undefined,
    retryCleanup: async () => undefined,
    reconcileCleanup: async () => undefined,
    clearAdmissionHandoff: () => undefined,
    clearSelection: () => undefined,
  } as unknown as ResearchStore;
}

function tasksStore(value: DurableTaskDetail = task): TasksStore {
  return {
    page: signal({ items: [value], nextCursor: null }),
    selected: signal(value),
    loading: signal(false),
    error: signal(null),
    saving: signal(false),
    statusFilter: signal(null),
    sourceFilter: signal(null),
    resetEpoch: signal(0),
    streamState: signal<'connected' | 'disconnected' | 'unauthorized' | 'malformed' | null>(
      'connected',
    ),
    commandNotice: signal(null),
    commandState: signal<'idle' | 'accepted' | 'rejected' | 'unknown'>('idle'),
    signedOut: signal(false),
    items: signal([value]),
    load: async () => undefined,
    loadDetail: async () => undefined,
    cancel: async () => undefined,
    retry: async () => undefined,
    clearSelection: () => undefined,
  } as unknown as TasksStore;
}

function artifactStore(
  value: ActiveGeneratedArtifactDetail | DeletedGeneratedArtifactTombstone = artifact,
): ArtifactsStore {
  const currentRevision: GeneratedArtifactRevision =
    value.contentState === 'active'
      ? value.revision
      : {
          revision: value.integrity.revision,
          mediaType: 'text/plain; charset=utf-8',
          sha256: value.integrity.sha256,
          byteCount: value.integrity.byteCount,
          creatorComponent: 'aura.research.report',
          previousRevision: null,
          createdAt: value.integrity.createdAt,
        };
  const preview: GeneratedArtifactPreview = {
    artifactId,
    revision: 2,
    mediaType: 'text/markdown; charset=utf-8',
    text: 'A bounded report preview. <script>must remain text</script>',
    truncated: false,
  };
  const revisions: ReadonlyArray<GeneratedArtifactRevision> = [
    {
      ...currentRevision,
      revision: 1,
      byteCount: 88,
      previousRevision: null,
      sha256: 'b'.repeat(64),
    },
    currentRevision,
  ];
  return {
    page: signal({ items: [value], nextCursor: null } as GeneratedArtifactPage),
    selected: signal(value),
    revisions: signal(revisions),
    preview: signal(value.contentState === 'active' ? preview : null),
    loading: signal(false),
    saving: signal(false),
    error: signal(null),
    signedOut: signal(false),
    resetEpoch: signal(0),
    items: signal([value]),
    load: async () => undefined,
    loadDetail: async () => undefined,
    delete: async () => undefined,
    retryCleanup: async () => undefined,
    reconcileCleanup: async () => undefined,
    download: async () => undefined,
    clearSelection: () => undefined,
  } as unknown as ArtifactsStore;
}

function providers(store: unknown, route?: ActivatedRoute): ReturnType<typeof applicationConfig> {
  return applicationConfig({
    providers: [
      provideRouter([], withDisabledInitialNavigation()),
      ...(route ? [{ provide: ActivatedRoute, useValue: route }] : []),
      store,
    ],
  });
}

const meta: Meta<ResearchPageComponent> = {
  title: 'Aura/Knowledge and Activity/Research Tasks Artifacts',
  component: ResearchPageComponent,
  parameters: { layout: 'fullscreen' },
  decorators: [providers({ provide: ResearchStore, useValue: researchStore() })],
};
export default meta;

type ResearchStory = StoryObj<ResearchPageComponent>;
const renderResearch = () => ({
  template: '<aura-research-page></aura-research-page>',
  moduleMetadata: { imports: [ResearchPageComponent] },
});
export const Quick: ResearchStory = {
  name: 'Quick interactive research',
  render: renderResearch,
  decorators: [
    providers({ provide: ResearchStore, useValue: researchStore(null, storySettings('quick')) }),
  ],
};
export const AutoDetached: ResearchStory = {
  name: 'Auto detached after conversational cutoff',
  render: renderResearch,
  decorators: [
    providers({ provide: ResearchStore, useValue: researchStore(null, storySettings('auto')) }),
  ],
};
export const Deep: ResearchStory = {
  name: 'Deep background research',
  render: renderResearch,
  decorators: [
    providers({ provide: ResearchStore, useValue: researchStore(null, storySettings('deep')) }),
  ],
};
export const AgentLed: ResearchStory = {
  name: 'Agent-led bounded research',
  render: renderResearch,
  decorators: [
    providers({
      provide: ResearchStore,
      useValue: researchStore(null, storySettings('agent_led')),
    }),
  ],
};
export const TaskRecovery: StoryObj<TasksPageComponent> = {
  name: 'Task recovery and unknown outcome',
  render: () => ({
    template: '<aura-tasks-page></aura-tasks-page>',
    moduleMetadata: { imports: [TasksPageComponent] },
  }),
  decorators: [
    providers(
      {
        provide: TasksStore,
        useValue: tasksStore({
          ...task,
          status: 'unknown',
          sourceStatus: 'unknown',
          sourceOutcome: null,
          progress: null,
          failureClass: 'provider_unavailable',
          recoveryAction: 'Reconcile before retrying.',
        }),
      },
      routeFor(taskId),
    ),
  ],
};
export const ArtifactPreview: StoryObj<ArtifactsPageComponent> = {
  name: 'Inert Markdown artifact preview',
  render: () => ({
    template: '<aura-artifacts-page></aura-artifacts-page>',
    moduleMetadata: { imports: [ArtifactsPageComponent] },
  }),
  decorators: [
    providers({ provide: ArtifactsStore, useValue: artifactStore() }, routeFor(artifactId)),
  ],
};
export const DeletionPending: StoryObj<ArtifactsPageComponent> = {
  name: 'Artifact deletion pending',
  render: () => ({
    template: '<aura-artifacts-page></aura-artifacts-page>',
    moduleMetadata: { imports: [ArtifactsPageComponent] },
  }),
  decorators: [
    providers(
      {
        provide: ArtifactsStore,
        useValue: artifactStore({
          ...artifact,
          title: null,
          mediaType: null,
          contentState: 'deletion_pending',
          capabilities: {
            canPreview: false,
            canDownload: false,
            canDelete: false,
            canRetryCleanup: false,
            canReconcileCleanup: false,
          },
          integrity: { revision: 2, sha256: 'a'.repeat(64), byteCount: 128, createdAt: now },
          deletionAuditId: 'storybook-audit-0147',
        } satisfies DeletedGeneratedArtifactTombstone),
      },
      routeFor(artifactId),
    ),
  ],
};
export const DeletionFailed: StoryObj<ArtifactsPageComponent> = {
  name: 'Artifact deletion failed',
  render: () => ({
    template: '<aura-artifacts-page></aura-artifacts-page>',
    moduleMetadata: { imports: [ArtifactsPageComponent] },
  }),
  decorators: [
    providers(
      {
        provide: ArtifactsStore,
        useValue: artifactStore({
          ...artifact,
          title: null,
          mediaType: null,
          contentState: 'deletion_failed',
          capabilities: {
            canPreview: false,
            canDownload: false,
            canDelete: false,
            canRetryCleanup: true,
            canReconcileCleanup: false,
          },
          integrity: { revision: 2, sha256: 'a'.repeat(64), byteCount: 128, createdAt: now },
          deletionAuditId: 'storybook-audit-0147-failed',
        } satisfies DeletedGeneratedArtifactTombstone),
      },
      routeFor(artifactId),
    ),
  ],
};
export const MemoryDraft: ResearchStory = {
  name: 'Memory-only research draft',
  decorators: [providers({ provide: ResearchStore, useValue: researchStore(null) })],
};
