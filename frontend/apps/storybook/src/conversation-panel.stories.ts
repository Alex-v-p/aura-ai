import { applicationConfig } from '@storybook/angular';
import type { Meta, StoryObj } from '@storybook/angular';
import { signal } from '@angular/core';
import { provideRouter } from '@angular/router';
import { ConversationAttachmentStore, ConversationPanelComponent, ConversationStore, type AttachmentUploadItem, type ConversationApi, type ConversationTurn } from '@aura/aura/interaction/conversations';
import type { ConversationDetail, ConversationRunAccepted, ConversationSummary, Message, Model, ModelCatalog, Run, Session } from '@aura/aura-api-client';
import { AGENT_API, AgentStore, InMemoryAgentApi } from '@aura/aura/interaction/agents';
import { PersonaStore } from '@aura/aura/interaction/personas';
import { ApprovalStore } from '@aura/aura/governance/approvals';
import { DocumentsStore } from '@aura/aura/knowledge/documents';

type FixtureState = 'empty' | 'loading' | 'working' | 'thinking' | 'tool-activity' | 'interrupted' | 'recoverable-error' | 'completed' | 'archived' | 'run-inspector' | 'attachment-uploading' | 'attachment-processing' | 'attachment-failed' | 'attachment-ready' | 'attachment-tombstone' | 'attachment-model-vision';

const turns: Record<'completed', ReadonlyArray<ConversationTurn>> = {
  completed: [
    { id: 'fixture-user', role: 'user', text: 'Help me find a small next step.' },
    { id: 'fixture-assistant', role: 'assistant', text: '## A small next step\n\nStart with one **kind, concrete action** you can finish today.\n\n```ts\nconst nextStep = "one small action";\n```\n\nSee [the Aura guide](https://example.com/aura) for more.' },
  ],
};

const fixtureModel: Model = { id: 'fixture-model', displayName: 'Fixture model', provider: 'fake', capabilities: ['chat'], availability: 'available', selectable: true, disabledReason: null };
const fixtureSession: Session = { principal: { issuer: 'https://authentik.test', subject: 'storybook' }, csrfToken: 'storybook-csrf', idleExpiresAt: '2026-10-06T20:00:00.000Z', absoluteExpiresAt: '2026-10-07T12:00:00.000Z' };
const fixtureAgent = { profileId: 'fixture-agent', revisionId: 'fixture-agent-r1', revision: 1, displayName: 'Fixture agent', status: 'active' as const, newerRevisionAvailable: false };
const now = '2026-10-06T12:00:00.000Z';

function fixtureRun(state: FixtureState): Run | null {
  if (!['working', 'thinking', 'tool-activity', 'interrupted', 'recoverable-error', 'run-inspector'].includes(state)) return null;
  const status = state === 'working' || state === 'thinking' || state === 'tool-activity' ? 'running' : state === 'interrupted' ? 'interrupted' : 'failed';
  return { id: 'fixture-run', conversationId: 'fixture', userMessageId: 'fixture-user', assistantMessageId: null, status, agentRevisionId: fixtureAgent.revisionId, modelPolicyRevisionId: 'fixture-policy', provider: 'fake', modelId: fixtureModel.id, retryOfRunId: null, createdAt: now, startedAt: now, finishedAt: status === 'running' ? null : '2026-10-06T12:00:01.000Z', error: status === 'failed' ? { code: 'PROVIDER_ERROR', message: 'Private provider payload', retryable: true, traceId: '0123456789abcdef0123456789abcdef' } : null };
}

const attachmentSummary = (state: FixtureState): Message['attachments'] => {
  if (!state.startsWith('attachment-')) return undefined;
  if (state === 'attachment-tombstone') {
    return [
      { linkId: 'fixture-link-1', kind: 'image', sourceId: 'fixture-image', displayFilename: 'Attachment unavailable', mediaType: 'application/octet-stream', byteCount: 0, availability: 'unavailable' },
      { linkId: 'fixture-link-2', kind: 'document', sourceId: 'fixture-document', displayFilename: 'Attachment unavailable', mediaType: 'application/octet-stream', byteCount: 0, availability: 'unavailable' },
    ];
  }
  return [
    { linkId: 'fixture-link-1', kind: 'image', sourceId: 'fixture-image', displayFilename: 'inspection.png', mediaType: 'image/png', byteCount: 12_345, availability: 'available', width: 640, height: 480 },
    { linkId: 'fixture-link-2', kind: 'document', sourceId: 'fixture-document', displayFilename: 'grounding.pdf', mediaType: 'application/pdf', byteCount: 42_000, availability: 'available' },
  ];
};

function attachmentFixture(state: FixtureState): ConversationAttachmentStore {
  const store = new ConversationAttachmentStore({
    listImages: async () => [],
    createImageUpload: async () => ({ id: 'fixture-upload', attachmentId: 'fixture-image', mediaType: 'image/png', version: 1 }),
    uploadImageContent: async () => ({ kind: 'image', attachmentId: 'fixture-image', sourceId: 'fixture-image', displayFilename: 'inspection.png', mediaType: 'image/png', byteCount: 12_345, availability: 'available' as const }),
    previewImage: async () => new Blob(['fixture-preview'], { type: 'image/png' }),
    deleteImage: async () => undefined,
  });
  store.load = async () => undefined;
  if (state === 'attachment-uploading' || state === 'attachment-processing' || state === 'attachment-failed') {
    const upload: AttachmentUploadItem = { key: 'fixture-upload', filename: 'inspection.png', byteCount: 12_345, mediaType: 'image/png', progress: state === 'attachment-uploading' ? 46 : 100, state: state === 'attachment-uploading' ? 'uploading' : state === 'attachment-processing' ? 'processing' : 'failed', message: state === 'attachment-failed' ? 'Image inspection failed. Try again.' : null, attachmentId: state === 'attachment-failed' ? null : 'fixture-image', previewUrl: null };
    store.uploads.set([upload]);
  }
  if (state === 'attachment-ready' || state === 'attachment-model-vision') {
    store.images.set([{ kind: 'image', attachmentId: 'fixture-image', sourceId: 'fixture-image', displayFilename: 'inspection.png', mediaType: 'image/png', byteCount: 12_345, availability: 'available', version: 1 }]);
    store.setSelected([{ kind: 'image', attachmentId: 'fixture-image' }, { kind: 'document', documentId: 'fixture-document' }]);
  }
  if (state === 'attachment-tombstone') store.images.set([{ kind: 'image', attachmentId: 'fixture-image', sourceId: 'fixture-image', displayFilename: 'inspection.png', mediaType: 'image/png', byteCount: 12_345, availability: 'unavailable', status: 'failed', version: 2 }]);
  return store;
}

function documentFixture(): DocumentsStore {
  const store = new DocumentsStore({ list: async () => ({ items: [], nextCursor: null }), search: async () => ({ items: [], nextCursor: null }), get: async () => ({ id: 'fixture-document', filename: 'grounding.pdf', mediaType: 'application/pdf', byteCount: 42_000, status: 'ready', failure: null, version: 1, extractedTextLength: 12, chunkCount: 1, createdAt: now, updatedAt: now }), preview: async () => ({ documentId: 'fixture-document', text: 'Fixture excerpt', truncated: false, totalCharacters: 15 }), createUpload: async () => { throw new Error('not used'); }, uploadContent: async () => { throw new Error('not used'); }, download: async () => { throw new Error('not used'); }, retry: async () => { throw new Error('not used'); }, delete: async () => ({ documentId: 'fixture-document', status: 'deleting' as const, version: 2, acceptedAt: now }) });
  store.load = async () => undefined;
  store.documents.set({ items: [{ id: 'fixture-document', filename: 'grounding.pdf', mediaType: 'application/pdf', byteCount: 42_000, status: 'ready', failure: null, version: 1, createdAt: now, updatedAt: now }], nextCursor: null });
  return store;
}

function fixtureSummary(state: FixtureState): ConversationSummary {
  const run = fixtureRun(state);
  return { id: 'fixture', title: state === 'archived' ? 'Archived fixture conversation' : 'Fixture conversation', agentProfileId: fixtureAgent.profileId, agentRevisionId: fixtureAgent.revisionId, agent: fixtureAgent, modelId: fixtureModel.id, version: 1, createdAt: now, updatedAt: now, archivedAt: state === 'archived' ? now : null, currentRun: state === 'working' || state === 'thinking' || state === 'tool-activity' ? run : null };
}

function fixtureMessages(state: FixtureState): ReadonlyArray<Message> {
  const user: Message = { id: 'fixture-user', conversationId: 'fixture', role: 'user', content: state.startsWith('attachment-') ? 'Please inspect these attachments and ground the answer.' : 'Help me find a small next step.', state: 'complete', runId: fixtureRun(state)?.id ?? null, attachments: attachmentSummary(state), createdAt: now, updatedAt: now };
  if (state === 'completed' || state === 'archived') return [user, { id: 'fixture-assistant', conversationId: 'fixture', role: 'assistant', content: turns.completed[1]?.text ?? '', state: 'complete', runId: null, createdAt: now, updatedAt: now }];
  if (state === 'interrupted') return [user, { id: 'fixture-assistant', conversationId: 'fixture', role: 'assistant', content: 'I started gathering a thought, but the preview was interrupted.', state: 'interrupted', runId: 'fixture-run', createdAt: now, updatedAt: now }];
  return state === 'empty' || state === 'loading' ? [] : [user];
}

function fixtureDetail(state: FixtureState): ConversationDetail {
  const summary = fixtureSummary(state);
  const run = fixtureRun(state);
  return { ...summary, messages: fixtureMessages(state), recentRuns: run && run.status !== 'running' ? [run] : [] };
}

function fixtureApi(state: FixtureState): ConversationApi {
  const detail = fixtureDetail(state);
  const page: ReadonlyArray<ConversationSummary> = state === 'empty' || state === 'loading' ? [] : [detail];
  const catalog: ModelCatalog = { models: [fixtureModel], defaultModelId: fixtureModel.id, observedAt: now };
  const api: ConversationApi = {
    getSession: async () => fixtureSession,
    startLogin: () => undefined,
    logout: async () => undefined,
    listModels: async () => catalog,
    listConversations: async () => state === 'loading' ? new Promise<{ items: ReadonlyArray<ConversationSummary>; nextCursor: string | null }>(() => undefined) : ({ items: page, nextCursor: null }),
    getConversation: async () => detail,
    updateConversationMetadata: async (_id, metadata) => ({ ...detail, title: metadata.title ?? detail.title, archivedAt: metadata.archived === true ? now : metadata.archived === false ? null : detail.archivedAt }),
    createConversation: async (message, modelId) => acceptedRun('fixture-created', message, modelId),
    updateConversation: async (_id, modelId) => ({ ...detail, modelId }),
    createRun: async (_id, message) => acceptedRun('fixture', message, fixtureModel.id),
    cancelRun: async () => {
      const run = fixtureRun('working');
      if (!run) throw new Error('Working fixture run is unavailable.');
      return { ...run, status: 'canceled' as const, finishedAt: now };
    },
    retryRun: async () => acceptedRun('fixture', 'Retry fixture', fixtureModel.id),
    streamRunEvents: (_runId, _lastEventId, onEvent) => {
      if (state === 'thinking') queueMicrotask(() => {
        onEvent({ schemaVersion: 1, eventId: 'thinking-delta', sequence: 1, eventType: 'model.thinking.delta', runId: 'fixture-run', conversationId: 'fixture', occurredAt: now, data: { offset: 0, text: '<private model notes>\nReasoning remains inert text.' } });
      });
      if (state === 'tool-activity') queueMicrotask(() => {
        onEvent({ schemaVersion: 1, eventId: 'tool-completed', sequence: 1, eventType: 'tool.status', runId: 'fixture-run', conversationId: 'fixture', occurredAt: now, data: { callId: 'fixture-call', toolId: 'memory.search', toolVersion: 1, status: 'completed', resultCount: 2 } });
      });
      return { close: () => undefined };
    },
  };
  return api;

  function acceptedRun(conversationId: string, message: string, modelId: string): ConversationRunAccepted {
    const run: Run = { id: `run-${conversationId}`, conversationId, userMessageId: `user-${conversationId}`, assistantMessageId: null, status: 'running', agentRevisionId: fixtureAgent.revisionId, modelPolicyRevisionId: 'fixture-policy', provider: 'fake', modelId, retryOfRunId: null, createdAt: now, startedAt: now, finishedAt: null, error: null };
    const summary: ConversationSummary = { ...fixtureSummary('working'), id: conversationId, currentRun: run, modelId };
    const userMessage: Message = { id: run.userMessageId, conversationId, role: 'user', content: message, state: 'complete', runId: run.id, createdAt: now, updatedAt: now };
    return { conversation: summary, userMessage, run };
  }
}

function fixtureStore(state: FixtureState): ConversationStore {
  const store = new ConversationStore(fixtureApi(state));
  if (state === 'loading') store.loading.set(true);
  if (state === 'run-inspector' || state === 'tool-activity') store.runInspectorRequested.set(1);
  if (state.startsWith('attachment-')) {
    // The store begins its fixture load asynchronously. Seed the persisted
    // draft under the eventual selected conversation id so the panel's
    // conversation-scoped hydration cannot clear the story's explicit image
    // selection while the initial detail settles.
    store.setDraftAttachmentsFor('fixture', [{ kind: 'image', attachmentId: 'fixture-image' }, { kind: 'document', documentId: 'fixture-document' }]);
    store.selectedId.set('fixture');
  }
  return store;
}

function attachmentProviders(state: FixtureState): ReadonlyArray<unknown> {
  return [{ provide: ConversationAttachmentStore, useValue: attachmentFixture(state) }, { provide: DocumentsStore, useValue: documentFixture() }];
}

function fixtureApprovals(): ApprovalStore {
  return { start: async () => undefined, stop: () => undefined, forConversation: () => [], saving: signal(false), pendingCount: signal(0), connectionState: signal('live') } as unknown as ApprovalStore;
}

const meta: Meta<ConversationPanelComponent> = {
  title: 'Aura/Conversation panel',
  component: ConversationPanelComponent,
  // The browser matrix owns deterministic automated axe coverage for these
  // fixture states. Keep the addon available for manual inspection without
  // racing its scan against the component-scoped Playwright scan.
  parameters: { layout: 'fullscreen', a11y: { manual: true } },
};
export default meta;

type Story = StoryObj<ConversationPanelComponent>;

const story = (state: FixtureState): Story => ({ decorators: [applicationConfig({ providers: [{ provide: ConversationStore, useValue: fixtureStore(state) }, { provide: ApprovalStore, useValue: fixtureApprovals() }, { provide: AGENT_API, useFactory: () => new InMemoryAgentApi() }, { provide: AgentStore, useClass: AgentStore }, { provide: PersonaStore, useValue: { activePersonas: signal([]) } }, ...attachmentProviders(state), provideRouter([])] })] });

export const Empty: Story = { ...story('empty'), name: 'Empty' };
export const Loading: Story = { ...story('loading'), name: 'Loading' };
export const Working: Story = { ...story('working'), name: 'Working' };
export const Thinking: Story = { ...story('thinking'), name: 'Thinking model' };
export const ToolActivity: Story = { ...story('tool-activity'), name: 'Tool activity' };
export const Interrupted: Story = { ...story('interrupted'), name: 'Interrupted' };
export const RecoverableError: Story = { ...story('recoverable-error'), name: 'Recoverable error' };
export const Completed: Story = { ...story('completed'), name: 'Completed' };
export const ArchivedRecovery: Story = { ...story('archived'), name: 'Archived recovery' };
export const RunInspector: Story = { ...story('run-inspector'), name: 'Run inspector' };
export const AttachmentUploading: Story = { ...story('attachment-uploading'), name: 'Attachment uploading' };
export const AttachmentProcessing: Story = { ...story('attachment-processing'), name: 'Attachment processing' };
export const AttachmentFailed: Story = { ...story('attachment-failed'), name: 'Attachment failed' };
export const AttachmentReady: Story = { ...story('attachment-ready'), name: 'Attachment ready' };
export const AttachmentTombstone: Story = { ...story('attachment-tombstone'), name: 'Attachment unavailable tombstone' };
export const AttachmentModelVision: Story = { ...story('attachment-model-vision'), name: 'Attachment model vision guidance' };
