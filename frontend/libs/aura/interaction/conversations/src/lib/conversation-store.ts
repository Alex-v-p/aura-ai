import { Inject, Injectable, InjectionToken, Optional, computed, signal } from '@angular/core';
import type { ConversationDetail, ConversationSummary, ConversationRunAccepted, MemoryActivity, MemoryCandidateDetail, MemoryDetail, Message, Model, PersonaAssignment as ApiPersonaAssignment, PersonaReference, Run, RunEvent, RunMemoryActivitySnapshot, Session, ToolStatusData } from '@aura/aura-api-client';
import type { AgentReference } from '@aura/aura/interaction/agents';
import { AuraConversationApi, type ConversationApi, type ConversationApiError, type ConversationBranchDetail, type ConversationBranchPage, type ConversationBranchRunAccepted, type ConversationBranchSummary, type ConversationListFilters, type RunEventHandler, type RunEventSubscription } from './conversation-api';
import type { AttachmentSelection, AttachmentSummary } from './conversation-attachments';

// Memory processing can legitimately wait behind a provider run. Keep the
// background reconciliation horizon aligned with the configured 300-second
// run timeout while remaining finite and silent for expected no-activity reads.
const MEMORY_ACTIVITY_RETRY_DELAYS_MS = [250, 1_000, 3_000, 10_000, 30_000, 60_000, 120_000, 120_000] as const;

export type TurnRole = 'user' | 'assistant';
export type RunState = 'idle' | 'working' | 'waiting' | 'interrupted' | 'error';
export type AuthState = 'loading' | 'authenticated' | 'unauthenticated' | 'error';

/** Keep a submitted composer value only until the store observes authoritative acceptance. */
export function shouldClearComposerDraftAfterAcceptance(
  localDraft: string,
  storedDraft: string,
  submittedDraft: string | null,
  runState: RunState,
): boolean {
  return submittedDraft !== null
    && storedDraft.length === 0
    && localDraft === submittedDraft
    && runState !== 'error'
    && runState !== 'interrupted';
}

/** Metadata-only source reference accepted by Core for this run. */
export interface DocumentCitation { readonly documentId: string; readonly displayLabel: string; readonly locator: string; }
const citationIdPattern = /^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/;
const citationText = (value: unknown, maxLength: number): string | null => {
  if (typeof value !== 'string' || value.length === 0 || value.length > maxLength || [...value].some((character) => { const code = character.codePointAt(0) ?? 0; return code < 0x20 || code === 0x7f; })) return null;
  return value;
};
/** Normalize additive API data before it reaches the transcript template. */
export function normalizeDocumentCitations(value: unknown): ReadonlyArray<DocumentCitation> {
  if (!Array.isArray(value)) return [];
  const seen = new Set<string>(); const result: DocumentCitation[] = [];
  for (const candidate of value) {
    if (!candidate || typeof candidate !== 'object') continue;
    const item = candidate as Record<string, unknown>;
    const documentId = citationText(item['documentId'], 128);
    const displayLabel = citationText(item['displayLabel'] ?? item['displayName'], 120);
    const locator = citationText(item['locator'], 255);
    if (!documentId || !citationIdPattern.test(documentId) || !displayLabel || !locator || !/^(?:page|section):[1-9][0-9]{0,6}$/u.test(locator)) continue;
    const key = `${documentId}\u0000${locator}`;
    if (seen.has(key)) continue;
    seen.add(key); result.push({ documentId, displayLabel, locator });
    if (result.length >= 20) break;
  }
  return result;
}

export interface ConversationTurn { readonly id: string; readonly role: TurnRole; readonly text: string; readonly state?: 'partial' | 'interrupted' | 'failed'; readonly runId?: string | null; readonly transitionMarker?: string; readonly memoryActivities?: ReadonlyArray<MemoryActivity>; readonly citations?: ReadonlyArray<DocumentCitation>; readonly attachments?: ReadonlyArray<AttachmentSummary>; }
export function attachmentSelectionForHydration(
  switchedConversation: boolean,
  editingMessageId: string | null,
  draftAttachments: ReadonlyArray<AttachmentSelection>,
  sourceAttachments: ReadonlyArray<AttachmentSelection>,
): ReadonlyArray<AttachmentSelection> {
  return switchedConversation && editingMessageId ? sourceAttachments : draftAttachments;
}
export interface ConversationBranchState {
  readonly summaries: ReadonlyArray<ConversationBranchSummary>;
  readonly activeId: string | null;
  readonly nextCursor: string | null;
  readonly loading: boolean;
  readonly error: string | null;
  readonly errorAction: BranchRetryAction | null;
  readonly editingMessageId: string | null;
  readonly editDraft: string;
}
interface BranchIntent {
  readonly kind: 'edit' | 'regenerate';
  readonly messageId: string;
  readonly key: string;
  readonly version: number;
  readonly clientTimeZone?: string;
  readonly sourceBranchId?: string;
  readonly text?: string;
  readonly runId?: string;
  readonly attachments?: ReadonlyArray<AttachmentSelection>;
}
export type BranchRetryAction = { readonly kind: 'list' } | { readonly kind: 'edit'; readonly messageId: string } | { readonly kind: 'regenerate'; readonly messageId: string } | { readonly kind: 'select'; readonly branchId: string };
export type MemoryPopupRecord = { readonly kind: 'memory'; readonly detail: MemoryDetail } | { readonly kind: 'candidate'; readonly detail: MemoryCandidateDetail };
export function memoryActivityIndicators(items: ReadonlyArray<MemoryActivity>): { readonly recalled: boolean; readonly updated: boolean } {
  return { recalled: items.some((item) => item.action === 'recalled'), updated: items.some((item) => item.action !== 'recalled') };
}
/**
 * Only recalled records and activities with an owner-authorized record are
 * meaningful in the conversation footer. A queued projection without either
 * identifier is an internal processing placeholder; showing it as a creation
 * indicator makes ignored/rejected extraction look like a broken memory.
 */
export function visibleMemoryActivities(items: ReadonlyArray<MemoryActivity>): ReadonlyArray<MemoryActivity> {
  return items.filter((item) => item.action === 'recalled' || Boolean(item.memoryId || item.candidateId));
}
/** Tool lifecycle records are metadata-only and intentionally append in SSE order so replay and live delivery render identically. */
export function appendToolStatusActivity(existing: ReadonlyArray<ToolStatusData>, next: ToolStatusData): ReadonlyArray<ToolStatusData> {
  return [...existing, { ...next }];
}
const MAX_THINKING_DELTA_LENGTH = 4_096;
const MAX_THINKING_LENGTH = 65_536;
const thinkingLength = (text: string): number => Array.from(text).length;

/** Ephemeral, owner-local model reasoning. It is never part of a transcript turn. */
export interface ModelThinkingState {
  readonly runId: string;
  readonly text: string;
  readonly nextOffset: number;
}

/**
 * Reconcile one transient thinking frame without making the browser a
 * best-effort text merger. A frame is accepted only when it starts exactly at
 * the next expected offset and remains within both protocol bounds.
 */
export function reconcileModelThinkingDelta(existing: ModelThinkingState | null, runId: string, offset: unknown, text: unknown): ModelThinkingState | null {
  if (!runId || !Number.isInteger(offset) || (offset as number) < 0 || typeof text !== 'string' || text.length === 0 || thinkingLength(text) > MAX_THINKING_DELTA_LENGTH) return null;
  const base = existing ?? { runId, text: '', nextOffset: 0 };
  if (base.runId !== runId) return null;
  const numericOffset = offset as number;
  const chunkLength = thinkingLength(text);
  if (numericOffset !== base.nextOffset || thinkingLength(base.text) + chunkLength > MAX_THINKING_LENGTH) return null;
  return { runId, text: `${base.text}${text}`, nextOffset: numericOffset + chunkLength };
}

export interface ConversationAssignment { readonly id: string; readonly agent: AgentReference; readonly reason: 'initial' | 'manual_switch' | 'revision_upgrade'; readonly afterMessageId: string | null; readonly changedAt: string; }
export interface ConversationPersonaAssignment { readonly id: string; readonly persona: PersonaReference; readonly source: ApiPersonaAssignment['source']; readonly reason: ApiPersonaAssignment['reason']; readonly afterMessageId: string | null; readonly changedAt: string; }
export interface PendingConversationConfiguration { readonly conversationId: string; readonly expectedVersion: number; readonly agent: AgentReference | null; readonly persona: PersonaReference | null; readonly useAgentDefaultPersona: boolean; readonly agentChanged: boolean; readonly personaChanged: boolean; readonly action: 'switch' | 'upgrade'; }
export interface Conversation { readonly id: string; readonly title: string; readonly turns: ReadonlyArray<ConversationTurn>; readonly updatedAt: number; readonly modelId: string; readonly version: number; readonly archivedAt: string | null; readonly currentRun: Run | null; readonly retryableRun: Run | null; readonly agent: AgentReference | null; readonly assignments: ReadonlyArray<ConversationAssignment>; readonly persona: PersonaReference | null; readonly personaOverride: boolean; readonly personaAssignments: ReadonlyArray<ConversationPersonaAssignment>; readonly runs: ReadonlyArray<Run>; readonly branch?: ConversationBranchState; readonly toolActivities?: Readonly<Record<string, ReadonlyArray<ToolStatusData>>>; readonly toolActivityEventIds?: Readonly<Record<string, ReadonlyArray<string>>>; readonly toolActivityCursors?: Readonly<Record<string, string>>; }
export type ConversationRouteSelection = { readonly status: 'selected' } | { readonly status: 'not_found' } | { readonly status: 'unauthorized' | 'forbidden' | 'error'; readonly error: ConversationApiError } | { readonly status: 'stale' };
export type SessionAuthorizationStatus = 401 | 403;
interface SessionGuard { readonly epoch: number; readonly fingerprint: string | null; }

/**
 * The conversation boundary owns accepted-session and logout lifecycle, while
 * management stores own their private state.  Keeping this narrow port here
 * preserves the one-way Conversations -> management dependency without
 * importing feature implementations into this library (and keeps direct
 * store construction useful in tests and Storybook).
 */
export interface ManagementSessionPort {
  readonly resetSessionState: () => void;
  readonly registerSessionResetHandler: (handler: ((status?: SessionAuthorizationStatus) => void) | null) => void;
}

export const AURA_MANAGEMENT_SESSION = new InjectionToken<ManagementSessionPort>('AURA_MANAGEMENT_SESSION');

const draftAgent: AgentReference = { profileId: '', revisionId: '', revision: 1, displayName: 'Aura', status: 'active' };
const emptyBranchState = (): ConversationBranchState => ({ summaries: [], activeId: null, nextCursor: null, loading: false, error: null, errorAction: null, editingMessageId: null, editDraft: '' });
const emptyDraft: Conversation = { id: 'draft-welcome', title: 'New conversation', turns: [], updatedAt: Date.now(), modelId: '', version: 0, archivedAt: null, currentRun: null, retryableRun: null, agent: null, assignments: [], persona: null, personaOverride: false, personaAssignments: [], runs: [], branch: emptyBranchState() };

type ConversationRun = Run & { readonly conversationId: string };
type ConversationRunEvent = RunEvent & { readonly conversationId: string };
/** Workflow-origin runs are not conversation-owned; never let their null id cross this boundary. */
function isConversationRun(run: Run | null | undefined): run is ConversationRun {
  return typeof run?.conversationId === 'string' && run.conversationId.length > 0;
}
function isConversationRunEvent(event: RunEvent | null | undefined): event is ConversationRunEvent {
  return typeof event?.conversationId === 'string' && event.conversationId.length > 0;
}

@Injectable({ providedIn: 'root' })
export class ConversationStore {
  readonly loading = signal(true);
  readonly authState = signal<AuthState>('loading');
  readonly session = signal<Session | null>(null);
  readonly models = signal<ReadonlyArray<Model>>([]);
  readonly modelCatalogDefaultId = signal<string | null>(null);
  readonly conversations = signal<ReadonlyArray<Conversation>>([emptyDraft]);
  /** The library query is deliberately held in signals only; it is never serialized into a URL or offline store. */
  readonly libraryFilters = signal<ConversationListFilters>({ archiveState: 'active' });
  readonly libraryLoading = signal(false);
  readonly libraryLoadingMore = signal(false);
  readonly libraryError = signal<string | null>(null);
  readonly libraryNextCursor = signal<string | null>(null);
  readonly hasMoreConversations = computed(() => this.libraryNextCursor() !== null);
  readonly selectedId = signal(emptyDraft.id);
  readonly drafts = signal<Readonly<Record<string, string>>>({ [emptyDraft.id]: '' });
  /** Edit text whose source disappeared during branch recovery. Keep it out
   * of the sendable composer draft so it cannot overwrite an ordinary prompt. */
  readonly orphanedEditDrafts = signal<Readonly<Record<string, string>>>({});
  readonly draftAttachments = signal<Readonly<Record<string, ReadonlyArray<AttachmentSelection>>>>({ [emptyDraft.id]: [] });
  readonly notices = signal<Readonly<Record<string, string | null>>>({});
  /** Provider discovery is advisory: it must not replace a conversation error. */
  readonly modelCatalogNotice = signal<string | null>(null);
  readonly runStates = signal<Readonly<Record<string, RunState>>>({ [emptyDraft.id]: 'idle' });
  readonly lastPersistedDraftId = signal<string | null>(null);
  readonly selected = computed(() => this.conversations().find((conversation) => conversation.id === this.selectedId()) ?? emptyDraft);
  readonly draft = computed(() => this.drafts()[this.selectedId()] ?? '');
  readonly notice = computed(() => this.notices()[this.selectedId()] ?? this.modelCatalogNotice());
  readonly runState = computed<RunState>(() => this.runStates()[this.selectedId()] ?? this.statusToState(this.selected().currentRun?.status));
  readonly selectedModelId = computed(() => this.selected().modelId);
  readonly selectedAgent = computed(() => this.selected().agent ?? draftAgent);
  readonly selectedPersona = computed(() => this.selected().persona);
  readonly canSend = computed(() => Boolean(this.draft().trim() && this.authenticated() && !this.selected().archivedAt && !this.selectedBranch().loading && this.modelFor(this.selectedId()) && !['working', 'waiting'].includes(this.runState()) && this.canRunWithSelectedAgent(this.selectedId())));
  /** Every agentless draft starts unresolved. The shell must hydrate the
   * built-in agent's current revision before a first send can be accepted. */
  readonly defaultDraftAgentResolution = signal<'resolved' | 'pending' | 'failed'>('pending');
  readonly defaultDraftAgentError = signal<string | null>(null);
  readonly selectedDefaultDraftAgentError = computed(() => {
    const conversation = this.selected();
    return conversation.id.startsWith('draft-') && !conversation.agent ? this.defaultDraftAgentError() : null;
  });
  readonly selectedAttachments = computed(() => this.draftAttachments()[this.selectedId()] ?? []);
  readonly attachmentAcceptanceRevision = signal(0);
  /** Conversation identity paired with the latest accepted attachment set. */
  readonly attachmentAcceptanceConversationId = signal<string | null>(null);
  private readonly modelVisionRequiredState = signal<Readonly<Record<string, string>>>({});
  /** The admission conflict is scoped to the conversation and model that
   * produced it. Switching either context clears the stale guidance. */
  readonly modelVisionRequired = computed(() => this.modelVisionRequiredFor());
  readonly pendingAgent = signal<AgentReference | null>(null);
  readonly pendingAgentAction = signal<'switch' | 'upgrade'>('switch');
  readonly pendingPersona = signal<PersonaReference | null>(null);
  readonly pendingPersonaReset = signal(false);
  readonly pendingConfiguration = signal<PendingConversationConfiguration | null>(null);
  /** Signals the shell to refresh its active replacement options after a
   * server-side agent status change. */
  readonly agentRefreshRequested = signal(0);
  readonly runInspectorRequested = signal(0);
  readonly runInspectorFocusId = signal<string | null>(null);
  readonly authenticated = computed(() => this.authState() === 'authenticated');
  readonly memoryActivityNotices = signal<Readonly<Record<string, string | null>>>({});
  readonly memoryActivityNotice = computed(() => this.memoryActivityNotices()[this.selectedId()] ?? null);
  readonly memoryPopupRecords = signal<Readonly<Record<string, MemoryPopupRecord | null>>>({});
  readonly memoryPopupLoading = signal<Readonly<Record<string, boolean>>>({});
  readonly memoryPopupErrors = signal<Readonly<Record<string, string | null>>>({});
  /** Thinking is keyed by run and held only in this live browser session. */
  readonly modelThinking = signal<Readonly<Record<string, ModelThinkingState>>>({});
  readonly selectedBranch = computed(() => this.selected().branch ?? emptyBranchState());
  readonly branchEditing = computed(() => this.selectedBranch().editingMessageId !== null);
  readonly branchAnnouncement = computed(() => this.branchAnnouncements()[this.selectedId()] ?? '');

  orphanedEditDraftFor(conversationId = this.selectedId()): string | null { return this.orphanedEditDrafts()[conversationId] ?? null; }

  restoreOrphanedEditDraft(replace = false): boolean {
    const id = this.selectedId();
    const recovered = this.orphanedEditDraftFor(id);
    if (!recovered || (!replace && this.draft().trim())) return false;
    this.drafts.update((drafts) => ({ ...drafts, [id]: recovered }));
    this.orphanedEditDrafts.update((drafts) => { const next = { ...drafts }; delete next[id]; return next; });
    return true;
  }

  dismissOrphanedEditDraft(): void {
    const id = this.selectedId();
    if (!this.orphanedEditDraftFor(id)) return;
    this.orphanedEditDrafts.update((drafts) => { const next = { ...drafts }; delete next[id]; return next; });
  }

  toolActivitiesForRun(runId: string): ReadonlyArray<ToolStatusData> { return this.find(this.selectedId())?.toolActivities?.[runId] ?? []; }
  thinkingForRun(runId: string): ModelThinkingState | null { return this.modelThinking()[runId] ?? null; }
  thinkingIsOpen(runId: string): boolean {
    const conversation = this.selected();
    return Boolean(conversation.currentRun?.id === runId && this.runState() === 'working' && this.thinkingForRun(runId)?.text);
  }

  private readonly subscriptions = new Map<string, RunEventSubscription>();
  /** Historical replay is kept separate from live subscriptions so a detail
   * refresh cannot re-apply assistant deltas or create duplicate run streams. */
  private readonly toolReplaySubscriptions = new Map<string, RunEventSubscription>();
  private readonly lastEventIds = new Map<string, string>();
  private readonly expiredCursorRecoveries = new Set<string>();
  private readonly reconnectTimers = new Map<string, ReturnType<typeof setTimeout>>();
  private readonly memoryActivityRetryTimers = new Map<string, ReturnType<typeof setTimeout>>();
  private readonly memoryActivityRetryAttempts = new Map<string, number>();
  private readonly thinkingRunConversations = new Map<string, string>();
  private readonly pendingSubmissions = new Map<string, { readonly draft: string; readonly pendingTurnId: string }>();
  private readonly branchIntents = new Map<string, BranchIntent>();
  private readonly branchAnnouncements = signal<Readonly<Record<string, string>>>({});
  private draftCounter = 0;
  private conversationListWarning = false;
  private filterTimer: ReturnType<typeof setTimeout> | null = null;
  private libraryRequestGeneration = 0;
  private loadGeneration = 0;
  private modelCatalogRequestGeneration = 0;
  private readonly api: ConversationApi;
  private readonly managementSession: ManagementSessionPort | null;
  private acceptedSessionFingerprint: string | null = null;
  private sessionEpoch = 0;
  private resettingManagementState = false;

  constructor(@Inject(AuraConversationApi) api?: ConversationApi, @Optional() @Inject(AURA_MANAGEMENT_SESSION) managementSession?: ManagementSessionPort) {
    this.api = api ?? new AuraConversationApi();
    this.managementSession = managementSession ?? null;
    this.managementSession?.registerSessionResetHandler((status) => {
      if (status === 401 || status === 403) this.handleManagementAuthorizationFailure(status);
      else this.resetManagementState();
    });
    void this.load();
  }

  async load(): Promise<void> {
    const loadGeneration = ++this.loadGeneration;
    const modelCatalogGeneration = ++this.modelCatalogRequestGeneration;
    this.loading.set(true);
    try {
      const session = await this.api.getSession();
      if (loadGeneration !== this.loadGeneration) return;
      const previousSession = this.session();
      const principalChanged = Boolean(previousSession && (previousSession.principal.issuer !== session.principal.issuer || previousSession.principal.subject !== session.principal.subject));
      const fingerprint = this.sessionFingerprint(session);
      if (principalChanged) {
        this.sessionEpoch += 1;
        this.clearOwnerScopedConversationState();
      }
      if (this.acceptedSessionFingerprint !== fingerprint) {
        this.sessionEpoch += 1;
        if (this.acceptedSessionFingerprint !== null) this.resetManagementState();
      }
      this.acceptedSessionFingerprint = fingerprint;
      this.session.set(session); this.authState.set('authenticated');
      // Model discovery depends on the local provider and is deliberately
      // advisory. Start it after authentication, but let transcript hydration
      // finish independently when Ollama is slow or unavailable.
      void this.loadModelCatalog(modelCatalogGeneration, loadGeneration);
      this.conversationListWarning = false;
      const generation = ++this.libraryRequestGeneration;
      const pageItems = await this.loadConversationPage(generation);
      if (loadGeneration !== this.loadGeneration || generation !== this.libraryRequestGeneration) return;
      const loaded = pageItems.map((item) => this.fromSummary(item));
      if (loaded.length === 0) this.ensureDraftModel();
      else {
        const loadedId = loaded[0].id;
        const activeId = this.selectedId();
        const activeDraft = this.find(activeId);
        const preserveActiveDraft = Boolean(activeDraft?.id.startsWith('draft-') && this.hasDraftComposerState(activeId));
        if (!preserveActiveDraft) this.migrateDraftComposerState(activeId, loadedId);
        this.conversations.set(preserveActiveDraft && activeDraft ? [activeDraft, ...loaded] : loaded);
        if (!preserveActiveDraft) this.selectedId.set(loadedId);
        for (const conversation of loaded) if (conversation.currentRun) this.subscribe(conversation.currentRun);
        await this.refresh(loaded[0].id);
        if (this.conversationListWarning) this.setNotice(loaded[0].id, 'Some older conversations could not be loaded. Please refresh and try again.');
      }
    } catch (error: unknown) {
      const apiError = this.toApiError(error);
      if (loadGeneration !== this.loadGeneration) return;
      if (apiError.status === 401 || apiError.status === 403) {
        this.sessionEpoch += 1;
        this.resetManagementState();
        this.authState.set('unauthenticated');
        if (apiError.status === 401) this.acceptedSessionFingerprint = null;
      }
      else { this.authState.set('error'); this.setNotice(this.selectedId(), apiError.message); }
    } finally { if (loadGeneration === this.loadGeneration) this.loading.set(false); }
  }

  private async loadModelCatalog(modelCatalogGeneration: number, loadGeneration: number): Promise<void> {
    try {
      const catalog = await this.api.listModels();
      if (modelCatalogGeneration !== this.modelCatalogRequestGeneration || loadGeneration !== this.loadGeneration || this.authState() !== 'authenticated') return;
      this.models.set(catalog.models);
      this.modelCatalogDefaultId.set(catalog.defaultModelId);
      this.modelCatalogNotice.set(null);
      this.ensureDraftModel();
    } catch (error: unknown) {
      if (modelCatalogGeneration !== this.modelCatalogRequestGeneration || loadGeneration !== this.loadGeneration || this.authState() !== 'authenticated') return;
      const apiError = this.toApiError(error);
      // Keep the existing authentication contract for provider endpoints. A
      // provider outage, however, is recoverable and must not turn the whole
      // conversation surface into an error state.
      if (apiError.status === 401 || apiError.status === 403) {
        this.sessionEpoch += 1;
        this.resetManagementState();
        this.authState.set('unauthenticated');
        if (apiError.status === 401) this.acceptedSessionFingerprint = null;
        return;
      }
      this.modelCatalogNotice.set('Model discovery is temporarily unavailable. Your conversations are still available; choose a model when the provider is ready.');
    }
  }

  setLibraryFilters(filters: ConversationListFilters): void {
    const normalized: ConversationListFilters = {
      ...(filters.q?.trim() ? { q: filters.q.trim().slice(0, 200) } : {}),
      ...(filters.agentProfileId ? { agentProfileId: filters.agentProfileId } : {}),
      ...(filters.modelId ? { modelId: filters.modelId } : {}),
      ...(filters.runStatus ? { runStatus: filters.runStatus } : {}),
      archiveState: filters.archiveState ?? 'active',
      ...(filters.activityFrom ? { activityFrom: filters.activityFrom } : {}),
      ...(filters.activityTo ? { activityTo: filters.activityTo } : {}),
    };
    this.libraryFilters.set(normalized);
    this.libraryRequestGeneration += 1;
    this.libraryLoadingMore.set(false);
    if (this.filterTimer) clearTimeout(this.filterTimer);
    this.filterTimer = setTimeout(() => { this.filterTimer = null; void this.reloadConversationLibrary(); }, normalized.q !== undefined ? 250 : 0);
  }

  async reloadConversationLibrary(): Promise<void> {
    const generation = ++this.libraryRequestGeneration;
    this.libraryLoading.set(true); this.libraryError.set(null); this.libraryNextCursor.set(null);
    try {
      const items = await this.loadConversationPage(generation);
      if (generation !== this.libraryRequestGeneration) return;
      const mapped = items.map((item) => this.fromSummary(item));
      const drafts = this.conversations().filter((conversation) => conversation.id.startsWith('draft-'));
      const selectedId = this.selectedId();
      const selectedConversation = this.conversations().find((conversation) => conversation.id === selectedId && !conversation.id.startsWith('draft-'));
      // Keep an opened conversation visible while its title/filter result is
      // being refreshed. This preserves selection, drafts, and the read-only
      // recovery banner when a filter excludes the current conversation.
      const retainedSelection = selectedConversation ? [selectedConversation] : [];
      this.conversations.set([...drafts, ...retainedSelection, ...mapped.filter((conversation) => conversation.id !== selectedId)]);
      if (selectedId && this.conversations().some((conversation) => conversation.id === selectedId)) this.selectedId.set(selectedId);
      else if (mapped[0]) { this.selectedId.set(mapped[0].id); await this.refresh(mapped[0].id); }
      else if (!drafts.length) this.ensureDraftModel();
      for (const conversation of mapped) if (conversation.currentRun) this.subscribe(conversation.currentRun);
    } catch (error: unknown) {
      if (generation === this.libraryRequestGeneration) this.libraryError.set(this.toApiError(error).message);
    } finally { if (generation === this.libraryRequestGeneration) this.libraryLoading.set(false); }
  }

  async loadMoreConversations(): Promise<void> {
    const cursor = this.libraryNextCursor();
    if (!cursor || this.libraryLoadingMore()) return;
    const generation = this.libraryRequestGeneration;
    const filters = this.libraryFilters();
    this.libraryLoadingMore.set(true); this.libraryError.set(null);
    try {
      const page = await this.api.listConversations(cursor, filters);
      if (generation !== this.libraryRequestGeneration || filters !== this.libraryFilters()) return;
      this.libraryNextCursor.set(page.nextCursor);
      const existing = new Set(this.conversations().map((conversation) => conversation.id));
      const additions = page.items.filter((item) => !existing.has(item.id)).map((item) => this.fromSummary(item));
      this.conversations.update((items) => [...items, ...additions]);
      for (const conversation of additions) if (conversation.currentRun) this.subscribe(conversation.currentRun);
    } catch (error: unknown) { if (generation === this.libraryRequestGeneration) this.libraryError.set(this.toApiError(error).message); }
    finally { if (generation === this.libraryRequestGeneration) this.libraryLoadingMore.set(false); }
  }

  async renameConversation(id: string, title: string): Promise<boolean> { return this.updateMetadata(id, { title }); }
  async archiveConversation(id: string): Promise<boolean> { return this.updateMetadata(id, { archived: true }); }
  async restoreConversation(id: string): Promise<boolean> { return this.updateMetadata(id, { archived: false }); }

  openRunInspector(id: string, focusId: string | null = null): void {
    void this.selectFromRoute(id).then((selection) => {
      if (selection.status === 'selected') {
        this.runInspectorFocusId.set(focusId);
        this.runInspectorRequested.update((value) => value + 1);
      }
    });
  }

  private async updateMetadata(id: string, mutation: { readonly title?: string; readonly archived?: boolean }): Promise<boolean> {
    const conversation = this.find(id);
    if (!conversation || id.startsWith('draft-') || !this.api.updateConversationMetadata) return false;
    if (conversation.archivedAt && (mutation.title !== undefined || mutation.archived === true)) {
      this.setNotice(id, 'Restore this archived conversation before changing its metadata.');
      return false;
    }
    try {
      const summary = await this.api.updateConversationMetadata(id, { ...mutation, version: conversation.version }, this.key());
      this.replaceConversation(this.mergeSummary(summary, conversation));
      if (mutation.archived !== undefined) this.reconcileMetadataVisibility(id, mutation.archived);
      return true;
    } catch (error: unknown) { this.setNotice(id, this.toApiError(error).message); return false; }
  }

  login(): void { this.api.startLogin(typeof window === 'undefined' ? '/' : window.location.pathname + window.location.search); }
  async logout(): Promise<void> {
    const token = this.session()?.csrfToken;
    this.sessionEpoch += 1;
    ++this.loadGeneration;
    ++this.modelCatalogRequestGeneration;
    this.clearOwnerScopedConversationState();
    this.resetManagementState();
    this.acceptedSessionFingerprint = null;
    this.session.set(null);
    this.authState.set('unauthenticated');
    const logoutGuard = this.captureSessionGuard();
    if (!token) return;
    try { await this.api.logout(token); }
    finally {
      if (this.matchesSessionGuard(logoutGuard)) {
        this.resetManagementState();
        this.acceptedSessionFingerprint = null;
        this.session.set(null);
        this.authState.set('unauthenticated');
      }
    }
  }

  select(id: string): void { if (this.conversations().some((conversation) => conversation.id === id)) { this.cancelPendingConfigurationForSelection(id); this.selectedId.set(id); this.setNotice(id, null); if (!id.startsWith('draft-')) void this.refresh(id); } }

  beginEditMessage(messageId: string): void {
    const turn = this.selected().turns.find((item) => item.id === messageId && item.role === 'user');
    if (!turn || this.controlsBlockedForBranch()) return;
    this.clearBranchIntent(this.selectedId());
    this.updateBranchState({ editingMessageId: messageId, editDraft: turn.text, error: null });
  }

  updateEditDraft(value: string): void {
    if (!this.selectedBranch().editingMessageId) return;
    this.clearBranchIntent(this.selectedId());
    this.updateBranchState({ editDraft: value });
  }

  /** Preserve an edit whose source disappeared while restoring the ordinary composer. */
  recoverOrphanedEdit(): void {
    const conversation = this.selected();
    const draft = conversation.branch?.editDraft ?? '';
    if (!conversation.branch?.editingMessageId) return;
    this.preserveEditDraft(conversation.id, draft);
    this.updateBranchState({ editingMessageId: null, editDraft: '' });
  }

  cancelEditMessage(): void { this.updateBranchState({ editingMessageId: null, editDraft: '' }); }

  async editAndBranch(explicitAttachments?: ReadonlyArray<AttachmentSelection>): Promise<boolean> {
    const conversation = this.selected();
    const branch = this.selectedBranch();
    const messageId = branch.editingMessageId;
    const text = branch.editDraft.trim();
    if (!messageId || !text) { this.updateBranchState({ error: 'Write a replacement message before creating a branch.' }); return false; }
    if (!this.api.createConversationBranch || this.controlsBlockedForBranch()) return false;
    const intent = this.branchIntent(conversation.id, {
      kind: 'edit',
      messageId,
      version: conversation.version,
      clientTimeZone: this.clientTimeZone(),
      sourceBranchId: conversation.branch?.activeId ?? undefined,
      text,
      attachments: explicitAttachments ?? turnAttachments(conversation, messageId),
    });
    this.updateBranchState({ loading: true, error: null });
    try {
      const accepted = await this.api.createConversationBranch(conversation.id, messageId, intent.text ?? text, intent.version, intent.key, intent.clientTimeZone, intent.sourceBranchId, intent.attachments);
      const applied = await this.applyAcceptedBranch(conversation.id, accepted, { kind: 'edit', messageId });
      if (applied) {
        this.clearBranchIntent(conversation.id);
        this.updateBranchState({ loading: false, editingMessageId: null, editDraft: '', errorAction: null });
        return true;
      }
      return false;
    } catch (error: unknown) {
      const apiError = this.toApiError(error);
      if (apiError.status === 409) {
        this.clearBranchIntent(conversation.id);
        await this.recoverBranchConflict(conversation.id, { kind: 'edit', messageId });
        return false;
      }
      if (this.isDefinitiveBranchFailure(apiError)) {
        this.clearBranchIntent(conversation.id);
        this.updateBranchState({ loading: false, error: this.branchErrorMessage(apiError, 'edit'), errorAction: null });
        return false;
      }
      const reconciled = await this.refresh(conversation.id);
      this.updateBranchState({ loading: false, error: this.unknownBranchOutcomeMessage(reconciled), errorAction: { kind: 'edit', messageId } });
      return false;
    }
  }

  async regenerateAssistant(messageId: string): Promise<boolean> {
    const conversation = this.selected();
    const turn = conversation.turns.find((item) => item.id === messageId && item.role === 'assistant');
    const runId = turn?.runId;
    if (!runId || !this.api.regenerateRun || this.controlsBlockedForBranch()) return false;
    const intent = this.branchIntent(conversation.id, {
      kind: 'regenerate',
      messageId,
      version: conversation.version,
      clientTimeZone: this.clientTimeZone(),
      runId,
    });
    this.updateBranchState({ loading: true, error: null });
    try {
      const accepted = await this.api.regenerateRun(conversation.id, intent.runId ?? runId, intent.version, intent.key, intent.clientTimeZone);
      const applied = await this.applyAcceptedBranch(conversation.id, accepted, { kind: 'regenerate', messageId });
      if (applied) {
        this.clearBranchIntent(conversation.id);
        this.updateBranchState({ loading: false, errorAction: null });
        return true;
      }
      return false;
    } catch (error: unknown) {
      const apiError = this.toApiError(error);
      if (apiError.status === 409) {
        this.clearBranchIntent(conversation.id);
        await this.recoverBranchConflict(conversation.id, { kind: 'regenerate', messageId });
        return false;
      }
      if (this.isDefinitiveBranchFailure(apiError)) {
        this.clearBranchIntent(conversation.id);
        this.updateBranchState({ loading: false, error: this.branchErrorMessage(apiError, 'regenerate'), errorAction: null });
        return false;
      }
      const reconciled = await this.refresh(conversation.id);
      this.updateBranchState({ loading: false, error: this.unknownBranchOutcomeMessage(reconciled), errorAction: { kind: 'regenerate', messageId } });
      return false;
    }
  }

  async loadBranches(conversationId = this.selectedId(), cursor?: string): Promise<void> {
    const conversation = this.find(conversationId);
    if (!conversation || conversationId.startsWith('draft-') || !this.api.listConversationBranches) return;
    const initialLoad = cursor === undefined;
    this.updateBranchState({ loading: true, error: null, errorAction: null, ...(initialLoad ? { nextCursor: null } : {}) }, conversationId);
    try {
      let pageCursor = cursor;
      let summaries = initialLoad ? [] : [...(conversation.branch?.summaries ?? [])];
      do {
        const page: ConversationBranchPage = await this.api.listConversationBranches(conversationId, pageCursor);
        summaries = [...summaries, ...page.items];
        pageCursor = page.nextCursor ?? undefined;
      } while (pageCursor);
      // A branch detail request can complete while the list request is still
      // in flight (notably for opaque URL deep links). Read the current
      // conversation after pagination so the list cannot restore the root
      // branch over a freshly hydrated alternative transcript.
      const latest = this.find(conversationId);
      const preferredActiveId = latest?.branch?.activeId ?? conversation.branch?.activeId;
      const activeId = preferredActiveId && summaries.some((item) => item.id === preferredActiveId)
        ? preferredActiveId
        : summaries.find((item) => item.parentBranchId === null)?.id ?? summaries[0]?.id ?? null;
      this.updateBranchState({ loading: false, summaries, activeId, nextCursor: null }, conversationId);
    } catch (error: unknown) {
      this.updateBranchState({ loading: false, error: this.branchErrorMessage(error, 'list'), errorAction: this.retryActionFor(error, { kind: 'list' }) }, conversationId);
    }
  }

  // The no-argument retry shape remains valid for list/regenerate actions:
  // async retryBranchAction(): Promise<boolean>
  async retryBranchAction(explicitAttachments?: ReadonlyArray<AttachmentSelection>): Promise<boolean> {
    const action = this.selectedBranch().errorAction;
    if (!action) return false;
    if (action.kind === 'list') { await this.loadBranches(); return false; }
    if (action.kind === 'edit') return this.editAndBranch(explicitAttachments);
    if (action.kind === 'regenerate') return this.regenerateAssistant(action.messageId);
    return this.selectBranch(action.branchId);
  }

  async selectBranch(branchId: string): Promise<boolean> {
    const conversation = this.selected();
    const guard = this.captureSessionGuard();
    const branchSummary = conversation.branch?.summaries.find((item) => item.id === branchId);
    const branchContentLoaded = branchSummary?.headMessageId === null || branchSummary?.headMessageId === undefined
      ? branchId === conversation.branch?.activeId
      : conversation.turns.some((turn) => turn.id === branchSummary.headMessageId);
    if (conversation.branch?.loading || !branchSummary || !this.api.getConversationBranch || (branchId === conversation.branch?.activeId && branchContentLoaded)) return false;
    this.updateBranchState({ loading: true, error: null });
    try {
      const detail = await this.api.getConversationBranch(conversation.id, branchId);
      if (!this.matchesSessionGuard(guard)) return false;
      // A branch request can overlap a user cancellation or another
      // conversation-state transition. Merge the response into the latest
      // owner-scoped state so a stale pre-await snapshot cannot resurrect an
      // edit that the user already cancelled.
      const latest = this.find(conversation.id) ?? conversation;
      const next = this.fromBranchDetail(detail, latest);
      this.replaceConversation(next);
      this.updateBranchState({ loading: false, activeId: branchId }, conversation.id);
      this.setBranchAnnouncement(conversation.id, 'Conversation branch loaded.');
      return true;
    } catch (error: unknown) {
      if (!this.matchesSessionGuard(guard)) return false;
      // The user may navigate to another conversation while this request is
      // pending. Keep the failure and retry action on the conversation that
      // authorized the request; never paint A's error onto the current B.
      this.updateBranchState({ loading: false, error: this.branchErrorMessage(error, 'select'), errorAction: this.retryActionFor(error, { kind: 'select', branchId }) }, conversation.id);
      return false;
    }
  }

  async navigateBranch(direction: -1 | 1): Promise<boolean> {
    if (this.selectedBranch().loading) return false;
    const branches = this.selectedBranch().summaries;
    const index = branches.findIndex((item) => item.id === this.selectedBranch().activeId);
    const next = branches[index + direction];
    return next ? this.selectBranch(next.id) : false;
  }

  async selectFromRoute(id: string, isCurrent: () => boolean = () => true): Promise<ConversationRouteSelection> {
    this.cancelPendingConfigurationForSelection(id);
    if (id.startsWith('draft-')) { if (!isCurrent() || !this.find(id)) return { status: 'stale' }; this.select(id); return { status: 'selected' }; }
    const guard = this.captureSessionGuard();
    try {
      const detail = await this.api.getConversation(id);
      if (!isCurrent() || !this.matchesSessionGuard(guard)) return { status: 'stale' };
      const conversation = this.fromDetail(detail);
      // Opening a conversation is a selection/detail refresh, not new
      // activity. Keep the server-provided list order (and append a direct
      // route lookup that was not present in the list).
      this.upsertConversation(conversation);
      this.hydrateHistoricalToolActivities(conversation);
      this.cancelPendingConfigurationForSelection(id);
      this.selectedId.set(id);
      this.setNotice(id, null);
      if (isConversationRun(detail.currentRun)) { this.setRunState(id, this.statusToState(detail.currentRun.status)); this.subscribe(detail.currentRun); }
      else if (conversation.retryableRun) { this.setRunState(id, this.statusToState(conversation.retryableRun.status)); }
      void this.loadBranches(id);
      return { status: 'selected' };
    } catch (error: unknown) {
      if (!isCurrent() || !this.matchesSessionGuard(guard)) return { status: 'stale' };
      const apiError = this.toApiError(error);
      if (apiError.status === 404) return { status: 'not_found' };
      if (apiError.status === 401) { this.invalidateSession(401); return { status: 'unauthorized', error: apiError }; }
      if (apiError.status === 403) { this.invalidateSession(403); return { status: 'forbidden', error: apiError }; }
      return { status: 'error', error: apiError };
    }
  }

  selectDraftForRoute(): void {
    const draft = this.conversations().find((conversation) => conversation.id.startsWith('draft-'));
    if (draft) this.select(draft.id);
    else this.create();
  }

  showRouteNotice(message: string): void { this.setNotice(this.selectedId(), message); }

  create(): void {
    const existingDraft = this.conversations().find((conversation) => conversation.id.startsWith('draft-'));
    if (existingDraft) {
      this.cancelPendingConfigurationForSelection(existingDraft.id);
      this.selectedId.set(existingDraft.id);
      return;
    }
    const id = `draft-${Date.now()}-${this.draftCounter++}`;
    this.cancelPendingConfigurationForSelection(id);
    const conversation: Conversation = { id, title: 'New conversation', turns: [], updatedAt: Date.now(), modelId: this.defaultModelId(), version: 0, archivedAt: null, currentRun: null, retryableRun: null, agent: null, assignments: [], persona: null, personaOverride: false, personaAssignments: [], runs: [], branch: emptyBranchState() };
    this.conversations.update((items) => [conversation, ...items]); this.drafts.update((drafts) => ({ ...drafts, [id]: '' })); this.runStates.update((states) => ({ ...states, [id]: 'idle' })); this.defaultDraftAgentResolution.set('pending'); this.defaultDraftAgentError.set(null); this.selectedId.set(id); this.setNotice(id, conversation.modelId ? null : 'The configured default model is unavailable. Choose an available model to continue.');
  }

  /**
   * The shell supplies the built-in agent's current revision once its profile
   * inventory is hydrated. Keep this narrowly scoped to an untouched draft so
   * an explicit agent choice or a persisted conversation can never be
   * rewritten by the asynchronous inventory response.
   */
  setDefaultDraftAgent(agent: AgentReference): void {
    const conversation = this.selected();
    if (!conversation.id.startsWith('draft-') || conversation.agent || !agent.revisionId) return;
    this.replaceConversation({ ...conversation, agent });
    this.defaultDraftAgentResolution.set('resolved');
    this.defaultDraftAgentError.set(null);
  }

  requireDefaultDraftAgent(): void {
    const conversation = this.selected();
    if (!conversation.id.startsWith('draft-') || conversation.agent) return;
    this.defaultDraftAgentResolution.set('pending');
    this.defaultDraftAgentError.set(null);
  }

  failDefaultDraftAgent(message: string): void {
    const conversation = this.selected();
    if (!conversation.id.startsWith('draft-') || conversation.agent) return;
    this.defaultDraftAgentResolution.set('failed');
    this.defaultDraftAgentError.set(message);
  }

  updateDraft(value: string): void { this.updateDraftFor(this.selectedId(), value); }
  updateDraftFor(conversationId: string, value: string): void {
    if (!this.conversations().some((conversation) => conversation.id === conversationId)) return;
    this.drafts.update((drafts) => ({ ...drafts, [conversationId]: value }));
    if (conversationId === this.selectedId() && value.trim()) this.setNotice(conversationId, null);
  }
  setDraftAttachments(items: ReadonlyArray<AttachmentSelection>): void { const id = this.selectedId(); const current = this.draftAttachments()[id] ?? []; if (sameAttachmentSelections(current, items)) return; this.draftAttachments.update((attachments) => ({ ...attachments, [id]: [...items] })); }
  setDraftAttachmentsFor(conversationId: string, items: ReadonlyArray<AttachmentSelection>): void { this.draftAttachments.update((attachments) => ({ ...attachments, [conversationId]: [...items] })); }
  attachmentsFor(conversationId: string): ReadonlyArray<AttachmentSelection> { return this.draftAttachments()[conversationId] ?? []; }
  attachmentsForTurn(messageId: string): ReadonlyArray<AttachmentSelection> { return turnAttachments(this.selected(), messageId); }
  modelVisionRequiredFor(conversationId = this.selectedId(), modelId = this.selectedModelId()): boolean {
    return Boolean(modelId && this.modelVisionRequiredState()[conversationId] === modelId);
  }
  private clearModelVisionRequired(conversationId: string, modelId?: string): void {
    this.modelVisionRequiredState.update((states) => {
      if (!states[conversationId] || (modelId && states[conversationId] !== modelId)) return states;
      const next = { ...states };
      delete next[conversationId];
      return next;
    });
  }
  announceAttachmentNotice(message: string): void { this.setNotice(this.selectedId(), message); }

  async selectModel(modelId: string): Promise<void> {
    const model = this.models().find((item) => item.id === modelId); if (!model || !model.selectable) return;
    if (this.authState() !== 'authenticated') { this.setNotice(this.selectedId(), 'Sign in before changing conversation settings.'); return; }
    if (this.selected().archivedAt) { this.setNotice(this.selectedId(), 'Restore this archived conversation before changing its configuration.'); return; }
    if (this.selectedBranch().loading) { this.setNotice(this.selectedId(), 'Wait for the conversation branch to finish loading before changing its configuration.'); return; }
    if (this.runState() === 'working') { this.setNotice(this.selectedId(), 'Wait for the active run to finish before changing conversation settings.'); return; }
    const conversation = this.selected(); const previous = conversation.modelId; const selectedModelIsFreshlyVerifiedVision = model.capabilities.includes('vision'); this.replaceConversation({ ...conversation, modelId });
    if (conversation.id.startsWith('draft-')) { if (selectedModelIsFreshlyVerifiedVision && previous !== modelId) this.clearModelVisionRequired(conversation.id, previous); return; }
    try { const summary = await this.api.updateConversation(conversation.id, modelId, conversation.version, this.key()); this.replaceConversation(this.mergeSummary(summary, conversation)); if (selectedModelIsFreshlyVerifiedVision && previous !== modelId) this.clearModelVisionRequired(conversation.id, previous); }
    catch (error: unknown) { this.replaceConversation({ ...conversation, modelId: previous }); this.setNotice(conversation.id, this.toApiError(error).message); }
  }

  requestAgent(agent: AgentReference): void {
    const current = this.selectedAgent();
    if (!agent.revisionId || agent.revisionId === current.revisionId) return;
    if (this.authState() !== 'authenticated') { this.setNotice(this.selectedId(), 'Sign in before changing conversation settings.'); return; }
    if (this.selected().archivedAt) { this.setNotice(this.selectedId(), 'Restore this archived conversation before changing its configuration.'); return; }
    if (this.selectedBranch().loading) { this.setNotice(this.selectedId(), 'Wait for the conversation branch to finish loading before changing its configuration.'); return; }
    if (this.runState() === 'working') { this.setNotice(this.selectedId(), 'Wait for the active run to finish before changing conversation settings.'); return; }
    if (this.selected().id.startsWith('draft-')) {
      this.replaceConversation({ ...this.selected(), agent });
      this.defaultDraftAgentResolution.set('resolved');
      this.defaultDraftAgentError.set(null);
      return;
    }
    this.pendingAgentAction.set(agent.profileId === current.profileId && agent.revision > current.revision ? 'upgrade' : 'switch');
    this.pendingAgent.set(agent);
  }

  requestPersona(persona: PersonaReference): void {
    const current = this.selected().persona;
    if (current?.revisionId === persona.revisionId && this.selected().personaOverride) return;
    if (this.authState() !== 'authenticated') { this.setNotice(this.selectedId(), 'Sign in before changing conversation settings.'); return; }
    if (this.selected().archivedAt) { this.setNotice(this.selectedId(), 'Restore this archived conversation before changing its configuration.'); return; }
    if (this.selectedBranch().loading) { this.setNotice(this.selectedId(), 'Wait for the conversation branch to finish loading before changing its configuration.'); return; }
    if (this.runState() === 'working') { this.setNotice(this.selectedId(), 'Wait for the active run to finish before changing conversation settings.'); return; }
    if (this.selected().id.startsWith('draft-')) {
      this.replaceConversation({ ...this.selected(), persona, personaOverride: true });
      return;
    }
    this.pendingPersonaReset.set(false);
    this.pendingPersona.set(persona);
  }

  requestUseAgentDefaultPersona(): void {
    if (!this.selected().personaOverride) return;
    if (this.authState() !== 'authenticated') { this.setNotice(this.selectedId(), 'Sign in before changing conversation settings.'); return; }
    if (this.selected().archivedAt) { this.setNotice(this.selectedId(), 'Restore this archived conversation before changing its configuration.'); return; }
    if (this.selectedBranch().loading) { this.setNotice(this.selectedId(), 'Wait for the conversation branch to finish loading before changing its configuration.'); return; }
    if (this.runState() === 'working') { this.setNotice(this.selectedId(), 'Wait for the active run to finish before changing conversation settings.'); return; }
    if (this.selected().id.startsWith('draft-')) {
      this.replaceConversation({ ...this.selected(), persona: null, personaOverride: false });
      return;
    }
    this.pendingPersona.set(null);
    this.pendingPersonaReset.set(true);
  }

  stageConfiguration(agent: AgentReference | null, persona: PersonaReference | null, useAgentDefaultPersona: boolean): void {
    const conversation = this.selected();
    if (this.authState() !== 'authenticated') { this.setNotice(conversation.id, 'Sign in before changing conversation settings.'); return; }
    if (conversation.archivedAt) { this.setNotice(conversation.id, 'Restore this archived conversation before changing its configuration.'); return; }
    if (this.selectedBranch().loading) {
      this.setNotice(conversation.id, 'Wait for the conversation branch to finish loading before changing its configuration.');
      return;
    }
    if (this.runState() === 'working') {
      this.setNotice(conversation.id, 'Wait for the active run to finish before changing conversation settings.');
      return;
    }
    const agentChanged = (agent?.revisionId ?? null) !== (conversation.agent?.revisionId ?? null);
    const personaChanged = useAgentDefaultPersona
      ? conversation.personaOverride
      : (persona?.revisionId ?? null) !== (conversation.persona?.revisionId ?? null) || !conversation.personaOverride;
    if (!agentChanged && !personaChanged) return;
    const action = agent && conversation.agent && agent.profileId === conversation.agent.profileId && agent.revision > conversation.agent.revision ? 'upgrade' : 'switch';
    if (conversation.id.startsWith('draft-')) {
      this.replaceConversation({ ...conversation, agent, persona: useAgentDefaultPersona ? null : persona, personaOverride: !useAgentDefaultPersona && Boolean(persona) });
      if (agent) {
        this.defaultDraftAgentResolution.set('resolved');
        this.defaultDraftAgentError.set(null);
      }
      return;
    }
    this.pendingConfiguration.set({ conversationId: conversation.id, expectedVersion: conversation.version, agent, persona, useAgentDefaultPersona, agentChanged, personaChanged, action });
  }

  cancelPendingConfiguration(): void { this.pendingConfiguration.set(null); }

  async confirmConfigurationChange(): Promise<void> {
    const pending = this.pendingConfiguration();
    if (!pending) return;
    const selectedId = this.selectedId();
    if (selectedId !== pending.conversationId) {
      this.pendingConfiguration.set(null);
      this.setNotice(selectedId, 'The pending configuration change was canceled after navigating to another conversation.');
      return;
    }
    const conversation = this.find(pending.conversationId);
    if (!conversation) {
      this.pendingConfiguration.set(null);
      this.setNotice(selectedId, 'The pending configuration change is no longer available.');
      return;
    }
    if (conversation.version !== pending.expectedVersion) {
      this.pendingConfiguration.set(null);
      this.setNotice(conversation.id, 'This conversation changed while the configuration was waiting. Review and try again.');
      return;
    }
    if (this.authState() !== 'authenticated' || this.runStates()[conversation.id] === 'working' || conversation.branch?.loading) {
      this.setNotice(conversation.id, this.authState() !== 'authenticated' ? 'Sign in before changing conversation settings.' : conversation.branch?.loading ? 'Wait for the conversation branch to finish loading before changing its configuration.' : 'Wait for the active run to finish before changing conversation settings.');
      this.pendingConfiguration.set(null);
      return;
    }
    this.pendingConfiguration.set(null);
    try {
      const summary = await this.api.updateConversation(
        conversation.id,
        conversation.modelId,
        conversation.version,
        this.key(),
        pending.agentChanged ? pending.agent?.revisionId : undefined,
        true,
        pending.personaChanged && !pending.useAgentDefaultPersona ? pending.persona?.revisionId : undefined,
        pending.personaChanged && pending.useAgentDefaultPersona,
      );
      this.replaceConversation(this.mergeSummary(summary, conversation));
    } catch (error: unknown) {
      this.setNotice(conversation.id, this.toApiError(error).message);
    }
  }

  transitionMarkerFor(messageId: string): string | null {
    const assignments = this.selected().assignments;
    const personaAssignments = this.selected().personaAssignments;
    const index = assignments.findIndex((item) => item.afterMessageId === messageId);
    const personaIndex = personaAssignments.findIndex((item) => item.afterMessageId === messageId);
    if (index < 0 && personaIndex < 0) return null;
    const assignment = index >= 0 ? assignments[index] : undefined;
    const personaAssignment = personaIndex >= 0 ? personaAssignments[personaIndex] : undefined;
    if (assignment?.reason === 'initial' && !personaAssignment) return null;
    const previous = index >= 0 ? assignments[index - 1]?.agent ?? this.selected().agent : undefined;
    const agentMarker = assignment && assignment.reason !== 'initial'
      ? previous ? `Agent changed from ${previous.displayName} r${previous.revision} to ${assignment.agent.displayName} r${assignment.agent.revision}` : `Agent changed to ${assignment.agent.displayName} r${assignment.agent.revision}`
      : '';
    const personaMarker = personaAssignment && personaAssignment.reason !== 'initial'
      ? `Persona changed to ${personaAssignment.persona.displayName} r${personaAssignment.persona.revision}`
      : '';
    return [agentMarker, personaMarker].filter(Boolean).join(' · ') || null;
  }

  agentForTurn(turn: ConversationTurn): AgentReference {
    const run = turn.runId ? this.selected().runs.find((item) => item.id === turn.runId) : undefined;
    const byRun = run ? this.selected().assignments.find((item) => item.agent.revisionId === run.agentRevisionId)?.agent : undefined;
    if (byRun) return byRun;
    const turnIndex = this.selected().turns.findIndex((item) => item.id === turn.id);
    const applicable = this.selected().assignments.filter((item) => {
      if (!item.afterMessageId) return turnIndex >= 0;
      const boundaryIndex = this.selected().turns.findIndex((itemInTurn) => itemInTurn.id === item.afterMessageId);
      return boundaryIndex >= 0 && turnIndex > boundaryIndex;
    }).at(-1)?.agent;
    return applicable ?? this.selectedAgent();
  }

  async confirmAgentSwitch(): Promise<void> {
    const replacement = this.pendingAgent(); const conversation = this.selected();
    if (!replacement) return;
    if (this.runState() === 'working') { this.setNotice(conversation.id, 'Wait for the active run to finish before changing agents.'); this.pendingAgent.set(null); return; }
    const previous = conversation.agent; this.pendingAgent.set(null);
    if (!conversation.id.startsWith('draft-')) {
      try { const summary = await this.api.updateConversation(conversation.id, conversation.modelId, conversation.version, this.key(), replacement.revisionId, true); this.replaceConversation(this.mergeSummary(summary, conversation)); }
      catch (error: unknown) { this.replaceConversation({ ...conversation, agent: previous }); this.setNotice(conversation.id, this.toApiError(error).message); }
    } else {
      this.replaceConversation({ ...conversation, agent: replacement });
    }
  }

  async confirmPersonaChange(): Promise<void> {
    const persona = this.pendingPersona();
    const reset = this.pendingPersonaReset();
    const conversation = this.selected();
    if (!persona && !reset) return;
    if (this.runState() === 'working') {
      this.setNotice(conversation.id, 'Wait for the active run to finish before changing personas.');
      this.pendingPersona.set(null);
      this.pendingPersonaReset.set(false);
      return;
    }
    this.pendingPersona.set(null);
    this.pendingPersonaReset.set(false);
    if (conversation.id.startsWith('draft-')) {
      this.replaceConversation({ ...conversation, persona: reset ? null : persona, personaOverride: !reset });
      return;
    }
    try {
      const summary = await this.api.updateConversation(
        conversation.id,
        conversation.modelId,
        conversation.version,
        this.key(),
        undefined,
        true,
        persona?.revisionId,
        reset,
      );
      this.replaceConversation(this.mergeSummary(summary, conversation));
    } catch (error: unknown) {
      this.setNotice(conversation.id, this.toApiError(error).message);
    }
  }

  send(): boolean {
    const draft = this.draft(); const text = draft.trim(); const id = this.selectedId();
    if (!text) { this.setNotice(id, 'Write a message before sending.'); return false; }
    if (this.selected().archivedAt) { this.setNotice(id, 'Restore this archived conversation before sending a message.'); return false; }
    if (this.runState() === 'working') return false;
    if (!this.canUseDefaultDraftAgent()) {
      this.setNotice(id, this.defaultDraftAgentError() ?? 'Aura is still preparing its current agent revision. Try again in a moment.');
      return false;
    }
    if (!this.modelFor(id)) { this.setNotice(id, 'Choose an available model before sending.'); return false; }
    if (!this.canRunWithSelectedAgent(id)) { this.setNotice(id, this.selected().agent ? 'This agent is disabled. Select an active replacement before sending.' : 'Select an active agent before sending.'); return false; }
    this.clearThinkingForConversation(id);
    const pendingTurnId = `pending-user-${Date.now()}`;
    this.appendTurn(id, { id: pendingTurnId, role: 'user', text });
    this.pendingSubmissions.set(id, { draft, pendingTurnId });
    // Keep the exact composer value until Core accepts the command. This
    // preserves whitespace and user edits across conflicts and network
    // failures; acceptance clears it only if it has not changed meanwhile.
    this.setRunState(id, 'working'); this.setNotice(id, null);
    void (id.startsWith('draft-') ? this.createPersisted(id, text) : this.createRun(id, text)); return true;
  }

  private canUseDefaultDraftAgent(): boolean {
    const conversation = this.selected();
    return !conversation.id.startsWith('draft-') || Boolean(conversation.agent) || this.defaultDraftAgentResolution() === 'resolved';
  }

  stop(): void {
    const conversation = this.selected(); const run = conversation.currentRun; if (!run || this.runState() !== 'working') return;
    const guard = this.captureSessionGuard();
    void this.api.cancelRun(run.id, this.session()?.csrfToken ?? '', this.key()).then((updated) => { if (!this.matchesSessionGuard(guard)) return; this.replaceRun(conversation.id, updated); this.setRunState(conversation.id, this.statusToState(updated.status)); this.setNotice(conversation.id, 'Generation stopped. Your partial response is still here.'); }).catch((error: unknown) => this.handleError(conversation.id, error, guard));
  }

  cancelRunById(conversationId: string, runId: string): void {
    const conversation = this.find(conversationId); const run = conversation?.runs.find((item) => item.id === runId);
    if (!conversation || !run || conversation.archivedAt || !['queued', 'running', 'cancel_requested'].includes(run.status)) return;
    const guard = this.captureSessionGuard();
    void this.api.cancelRun(run.id, this.session()?.csrfToken ?? '', this.key()).then((updated) => { if (!this.matchesSessionGuard(guard)) return; this.replaceRun(conversation.id, updated); this.setRunState(conversation.id, this.statusToState(updated.status)); }).catch((error: unknown) => this.handleError(conversation.id, error, guard));
  }

  retry(): void {
    const conversation = this.selected(); const run = conversation.currentRun ?? conversation.retryableRun;
    if (conversation.archivedAt) { this.setNotice(conversation.id, 'Restore this archived conversation before retrying a run.'); return; }
    if (!run || !['error', 'interrupted', 'canceled', 'failed'].includes(run.status)) { this.setNotice(conversation.id, null); return; }
    if (!this.canRunWithSelectedAgent(conversation.id)) { this.setNotice(conversation.id, conversation.agent ? 'This agent is disabled. Select an active replacement before retrying.' : 'Select an active agent before retrying.'); return; }
    this.clearThinkingForConversation(conversation.id);
    this.setRunState(conversation.id, 'working'); this.setNotice(conversation.id, null);
    const guard = this.captureSessionGuard();
    void this.api.retryRun(run.id, this.session()?.csrfToken ?? '', this.key()).then((accepted) => { if (this.matchesSessionGuard(guard)) this.acceptRun(conversation.id, accepted); }).catch((error: unknown) => this.handleError(conversation.id, error, guard));
  }

  retryRunById(conversationId: string, runId: string): void {
    const conversation = this.find(conversationId); const run = conversation?.runs.find((item) => item.id === runId);
    if (!conversation || !run || conversation.archivedAt || !this.isRetryable(run.status) || !this.canRunWithSelectedAgent(conversationId)) return;
    this.clearThinkingForConversation(conversationId);
    this.setRunState(conversationId, 'working');
    const guard = this.captureSessionGuard();
    void this.api.retryRun(run.id, this.session()?.csrfToken ?? '', this.key()).then((accepted) => { if (this.matchesSessionGuard(guard)) this.acceptRun(conversationId, accepted); }).catch((error: unknown) => this.handleError(conversationId, error, guard));
  }

  /** Compatibility hook for older local-preview fixtures; provider responses are never synthesized in production. */
  failNextLocalReply(): void { /* no-op */ }

  private async createPersisted(draftId: string, text: string): Promise<void> {
    const guard = this.captureSessionGuard();
    try {
      const draftConversation = this.find(draftId); const selectedRevisionId = draftConversation?.agent?.revisionId || undefined; const selectedPersonaRevisionId = draftConversation?.personaOverride ? draftConversation.persona?.revisionId : undefined; const clientTimeZone = typeof Intl !== 'undefined' ? Intl.DateTimeFormat().resolvedOptions().timeZone : undefined; const accepted = await this.api.createConversation(text, this.modelFor(draftId), this.key(), selectedRevisionId, selectedPersonaRevisionId, clientTimeZone, this.attachmentsFor(draftId)); const draft = draftConversation; const persisted = this.fromSummary(accepted.conversation);
      if (!this.matchesSessionGuard(guard)) return;
      const submission = this.pendingSubmissions.get(draftId);
      this.conversations.update((items) => [persisted, ...items.filter((item) => item.id !== draftId)]);
      this.drafts.update((drafts) => { const next = { ...drafts }; delete next[draftId]; next[persisted.id] = this.draftValueAfterAcceptance(draftId, submission?.draft); return next; }); this.draftAttachments.update((attachments) => { const next = { ...attachments }; delete next[draftId]; next[persisted.id] = []; return next; }); this.runStates.update((states) => { const next = { ...states }; delete next[draftId]; next[persisted.id] = this.statusToState(accepted.run.status); return next; }); this.lastPersistedDraftId.set(draftId); if (this.selectedId() === draftId) this.selectedId.set(persisted.id);
      if (draft) {
        const fallbackTitle = draft.turns.find((turn) => turn.role === 'user')?.text;
        this.replaceConversation({
          ...persisted,
          turns: draft.turns,
          title: persisted.title === 'New conversation' && fallbackTitle ? this.makeTitle(fallbackTitle) : persisted.title,
        });
      }
      this.pendingSubmissions.delete(draftId); this.publishAttachmentAcceptance(persisted.id); this.acceptRun(persisted.id, accepted, submission?.pendingTurnId);
    } catch (error: unknown) { this.rejectSubmission(draftId, error, guard); }
  }

  private async createRun(id: string, text: string): Promise<void> {
    const conversation = this.find(id); if (!conversation) return;
    const guard = this.captureSessionGuard();
    try {
      const submission = this.pendingSubmissions.get(id); const clientTimeZone = this.clientTimeZone();
      const accepted = await this.api.createRun(id, text, conversation.version, this.key(), clientTimeZone, conversation.branch?.activeId ?? undefined, this.attachmentsFor(id));
      if (!this.matchesSessionGuard(guard)) return;
      this.acceptRun(id, accepted, submission?.pendingTurnId); this.promoteConversation(id); this.clearDraftAfterAcceptance(id, submission?.draft); this.setDraftAttachmentsFor(id, []); this.publishAttachmentAcceptance(id); this.pendingSubmissions.delete(id);
    } catch (error: unknown) { this.rejectSubmission(id, error, guard); }
  }

  private acceptRun(id: string, accepted: ConversationRunAccepted, pendingTurnId?: string): void {
    const existing = this.find(id); if (!existing) return;
    if (!isConversationRun(accepted.run)) return;
    this.clearThinkingForConversation(id);
    const turns = existing.turns.map((turn) => !pendingTurnId || turn.id === pendingTurnId ? (turn.id.startsWith('pending-user-') ? this.fromMessage(accepted.userMessage) : turn) : turn);
    const acceptedConversation = this.fromSummary(accepted.conversation);
    // Core may still report the pending placeholder while title settlement is
    // running. Preserve the local first-message fallback until a terminal
    // refresh supplies the generated or deterministic fallback title.
    const title = acceptedConversation.title === 'New conversation' ? existing.title : acceptedConversation.title;
    this.replaceConversation({ ...acceptedConversation, title, turns, runs: [...existing.runs, accepted.run] }); this.setRunState(id, this.statusToState(accepted.run.status)); this.subscribe(accepted.run);
  }

  private async applyAcceptedBranch(id: string, accepted: ConversationBranchRunAccepted, action: BranchRetryAction): Promise<boolean> {
    const existing = this.find(id);
    if (!existing) return false;
    if (!isConversationRun(accepted.run)) return false;
    const acceptedConversation = this.fromSummary(accepted.conversation);
    const branch = accepted.branch;
    if (!branch) {
      this.updateBranchState({ loading: false, error: 'Aura created the run, but the new branch could not be identified.', errorAction: action }, id);
      this.subscribe(accepted.run);
      return false;
    }
    const branchState: ConversationBranchState = {
      ...(existing.branch ?? emptyBranchState()),
      activeId: branch.id,
      error: null,
      errorAction: null,
      loading: false,
      editingMessageId: null,
      editDraft: '',
      summaries: (existing.branch?.summaries ?? []).some((item) => item.id === branch.id) ? (existing.branch?.summaries ?? []) : [...(existing.branch?.summaries ?? []), branch],
    };
    const acceptedUser = accepted.userMessage ? this.fromMessage(accepted.userMessage) : null;
    this.replaceConversation({
      ...acceptedConversation,
      branch: branchState,
      turns: acceptedUser ? [...existing.turns.slice(0, Math.max(0, existing.turns.findIndex((turn) => turn.id === branch.forkedFromMessageId) + 1)), acceptedUser] : existing.turns,
      runs: [...existing.runs, accepted.run],
    });
    this.setRunState(id, this.statusToState(accepted.run.status));
    this.subscribe(accepted.run);
    if (this.api.getConversationBranch) {
      try {
        const detail = await this.api.getConversationBranch(id, branch.id);
        this.replaceConversation(this.fromBranchDetail(detail, this.find(id) ?? existing));
        this.setRunState(id, this.statusToState(accepted.run.status));
        this.setBranchAnnouncement(id, 'New conversation branch loaded.');
      } catch {
        this.updateBranchState({ loading: false, error: 'The branch was created, but its transcript is not available yet.', errorAction: { kind: 'select', branchId: branch.id } }, id);
        return false;
      }
    }
    return true;
  }

  private async recoverBranchConflict(id: string, action: Extract<BranchRetryAction, { kind: 'edit' | 'regenerate' }>): Promise<void> {
    const refreshed = await this.refresh(id);
    const message = refreshed
      ? 'This conversation changed. The latest version is loaded; review your draft and retry the branch action.'
      : 'This conversation changed, but the latest version could not be loaded. Retry when the conversation is available.';
    this.updateBranchState({ loading: false, error: message, errorAction: action }, id);
    this.setNotice(id, message);
  }

  private fromBranchDetail(detail: ConversationBranchDetail, previous: Conversation): Conversation {
    const turns = detail.messages.map((message) => {
      const next = this.fromMessage(message);
      const old = previous.turns.find((turn) => turn.id === message.id);
      return old?.memoryActivities ? { ...next, memoryActivities: old.memoryActivities } : next;
    });
    const runs = this.uniqueRuns(detail.recentRuns);
    const currentRun = runs.find((run) => !this.isTerminal(run.status)) ?? null;
    const retryableRun = runs.filter((run) => this.isRetryable(run.status)).sort((left, right) => Date.parse(right.finishedAt ?? right.createdAt) - Date.parse(left.finishedAt ?? left.createdAt))[0] ?? null;
    const previousBranch = previous.branch ?? emptyBranchState();
    const editStillExists = !previousBranch.editingMessageId || turns.some((turn) => turn.id === previousBranch.editingMessageId && turn.role === 'user');
    if (!editStillExists && previousBranch.editDraft) this.preserveEditDraft(previous.id, previousBranch.editDraft);
    return {
      ...previous,
      turns,
      currentRun,
      retryableRun,
      runs,
      branch: {
        ...previousBranch,
        activeId: detail.branch.id,
        error: null,
        errorAction: null,
        loading: false,
        editingMessageId: editStillExists ? previousBranch.editingMessageId : null,
        editDraft: editStillExists ? previousBranch.editDraft : '',
      },
    };
  }

  private controlsBlockedForBranch(): boolean {
    const conversation = this.selected();
    if (conversation.id.startsWith('draft-')) { this.setNotice(conversation.id, 'Branching is available after the conversation is saved.'); return true; }
    if (conversation.archivedAt) { this.setNotice(conversation.id, 'Restore this conversation before creating a branch.'); return true; }
    if (this.selectedBranch().loading) { this.setNotice(conversation.id, 'Wait for the conversation branch to finish loading.'); return true; }
    if (this.runState() === 'working' || this.runState() === 'waiting') { this.setNotice(conversation.id, 'Wait for the active response to finish before creating a branch.'); return true; }
    if (this.authState() !== 'authenticated') { this.setNotice(conversation.id, 'Sign in before creating a branch.'); return true; }
    return false;
  }

  private branchIntent(conversationId: string, next: Omit<BranchIntent, 'key'>): BranchIntent {
    const existing = this.branchIntents.get(conversationId);
    // Replay the exact original request after an unknown outcome. Reconciled
    // state may have a newer version, but Core's idempotency fingerprint is
    // keyed by the original body and expected version.
    if (existing?.kind === next.kind && existing.messageId === next.messageId) return existing;
    const intent = { ...next, key: this.key() };
    this.branchIntents.set(conversationId, intent);
    return intent;
  }

  private clearBranchIntent(conversationId: string): void { this.branchIntents.delete(conversationId); }

  private unknownBranchOutcomeMessage(reconciled: boolean): string {
    return reconciled
      ? 'We could not confirm that branch action. Your latest conversation is loaded; review it and retry safely.'
      : 'We could not confirm that branch action. Your conversation is preserved; retry when it is available.';
  }

  private isDefinitiveBranchFailure(error: unknown): boolean {
    const status = this.toApiError(error).status;
    return status === 401 || status === 403 || status === 404;
  }

  private retryActionFor(error: unknown, action: BranchRetryAction): BranchRetryAction | null {
    return this.isDefinitiveBranchFailure(error) ? null : action;
  }

  private branchErrorMessage(error: unknown, action: 'list' | 'select' | 'edit' | 'regenerate' = 'list'): string {
    const apiError = this.toApiError(error);
    if (apiError.status === 401) return action === 'edit'
      ? 'Sign in again before editing this conversation.'
      : action === 'regenerate' ? 'Sign in again before regenerating this response.' : 'Sign in again before changing conversation branches.';
    if (apiError.status === 403) return action === 'edit'
      ? 'You do not have permission to edit this conversation.'
      : action === 'regenerate' ? 'You do not have permission to regenerate this response.' : 'You do not have permission to view conversation branches.';
    if (apiError.status === 404) return action === 'edit'
      ? 'That message is no longer available for editing.'
      : action === 'regenerate' ? 'That response is no longer available for regeneration.' : 'That conversation branch is no longer available.';
    if (apiError.status === 409) return 'This conversation changed. Reload it before trying that branch action again.';
    if (apiError.status === 429) return 'Aura is busy. Try the branch action again in a moment.';
    return 'Aura could not complete that branch action. Your conversation is preserved; try again.';
  }

  private updateBranchState(patch: Partial<ConversationBranchState>, conversationId = this.selectedId()): void {
    const conversation = this.find(conversationId);
    if (!conversation) return;
    this.replaceConversation({ ...conversation, branch: { ...(conversation.branch ?? emptyBranchState()), ...patch } });
  }

  private preserveEditDraft(conversationId: string, draft: string): void {
    if (!draft.trim()) return;
    this.orphanedEditDrafts.update((drafts) => ({ ...drafts, [conversationId]: draft }));
  }

  private clientTimeZone(): string | undefined { return typeof Intl !== 'undefined' ? Intl.DateTimeFormat().resolvedOptions().timeZone : undefined; }

  private subscribe(run: Run, attempt = 0): void {
    if (!isConversationRun(run)) return;
    const guard = this.captureSessionGuard();
    const pendingReconnect = this.reconnectTimers.get(run.id);
    if (pendingReconnect) { clearTimeout(pendingReconnect); this.reconnectTimers.delete(run.id); }
    this.subscriptions.get(run.id)?.close();
    const handleEvent: RunEventHandler = (event, cursor) => { if (this.matchesSessionGuard(guard)) this.applyEvent(event, cursor); };
    this.subscriptions.set(run.id, this.api.streamRunEvents(run.id, this.lastEventIds.get(run.id), handleEvent, (error) => {
      if (!this.matchesSessionGuard(guard)) return;
      const current = this.find(run.conversationId)?.currentRun;
      if (!current || this.isTerminal(current.status)) { if (error.status === 401 || error.status === 403) this.handleError(run.conversationId, error, guard); return; }
      if (error.status === 410 && !this.expiredCursorRecoveries.has(run.id)) { this.expiredCursorRecoveries.add(run.id); this.lastEventIds.delete(run.id); this.scheduleReconciliation(run, attempt, undefined, guard); return; }
      if (error.status === 401 || error.status === 403 || error.status === 404 || error.status === 410) { this.handleError(run.conversationId, error, guard); return; }
      this.scheduleReconciliation(run, attempt, error, guard);
    }));
  }

  private scheduleReconciliation(run: Run, attempt: number, error?: ConversationApiError, guard = this.captureSessionGuard()): void {
    if (!isConversationRun(run)) return;
    if (!this.matchesSessionGuard(guard)) return;
    const current = this.find(run.conversationId)?.currentRun;
    if (!current || this.isTerminal(current.status)) return;
    this.setRunState(run.conversationId, 'working'); this.setNotice(run.conversationId, error?.message ?? 'Connection lost; trying again. Your partial response is still here.');
    const delay = Math.min(30_000, 500 * 2 ** Math.min(attempt, 6));
    const timer = setTimeout(() => { this.reconnectTimers.delete(run.id); if (this.matchesSessionGuard(guard)) void this.reconcileRun(run, attempt + 1, guard); }, delay);
    this.reconnectTimers.set(run.id, timer);
  }

  private clearReconnect(runId: string): void { const timer = this.reconnectTimers.get(runId); if (timer) clearTimeout(timer); this.reconnectTimers.delete(runId); }

  private async reconcileRun(run: Run, attempt: number, guard = this.captureSessionGuard()): Promise<void> {
    if (!isConversationRun(run)) return;
    try {
      const detail = await this.api.getConversation(run.conversationId);
      if (!this.matchesSessionGuard(guard)) return;
      const conversation = this.fromDetail(detail);
      this.replaceConversation(conversation);
      this.hydrateHistoricalToolActivities(conversation);
      if (this.api.getRunMemoryActivity) {
        await this.readMemoryActivity(run.conversationId, run.id, guard);
        if (!this.matchesSessionGuard(guard)) return;
      }
      if (isConversationRun(detail.currentRun) && !this.isTerminal(detail.currentRun.status)) {
        this.expiredCursorRecoveries.delete(run.id); this.setRunState(run.conversationId, 'working'); this.setNotice(run.conversationId, null); this.subscribe(detail.currentRun, attempt); return;
      }
      if (conversation.retryableRun) {
        this.setRunState(run.conversationId, this.statusToState(conversation.retryableRun.status));
        this.setNotice(run.conversationId, 'Generation stopped. Your partial response is still here.');
        return;
      }
      this.scheduleReconciliation(run, attempt, undefined, guard);
    } catch (error: unknown) {
      const apiError = this.toApiError(error);
      if (!this.matchesSessionGuard(guard)) return;
      if (apiError.status === 401 || apiError.status === 403 || apiError.status === 404 || apiError.status === 410) this.handleError(run.conversationId, apiError, guard);
      else this.scheduleReconciliation(run, attempt, apiError, guard);
    }
  }

  private applyEvent(event: RunEvent, cursor?: string): void {
    if (!isConversationRunEvent(event)) return;
    // Only the SSE `id` field identifies a persisted event.  Core heartbeats
    // are intentionally not persisted and do not carry a cursor.
    if (cursor && event.eventType !== 'heartbeat' && event.eventType !== 'model.thinking.delta') this.lastEventIds.set(event.runId, cursor);
    if (event.eventType === 'run.snapshot') { if (isConversationRun(event.data.run)) this.replaceRun(event.conversationId, event.data.run); if (event.data.assistantMessage) this.upsertTurn(event.conversationId, this.fromMessage(event.data.assistantMessage)); }
    else if (event.eventType === 'run.status') { this.setRunState(event.conversationId, this.statusToState(event.data.status)); this.replaceConversationRun(event.conversationId, (run) => run ? { ...run, status: event.data.status, startedAt: event.data.startedAt, finishedAt: event.data.finishedAt } : run); if (this.isTerminal(event.data.status)) { this.clearReconnect(event.runId); void this.refresh(event.conversationId); } }
    else if (event.eventType === 'assistant.delta') this.appendAssistantDelta(event.conversationId, event.data.messageId, event.data.offset, event.data.text, event.runId);
    else if (event.eventType === 'model.thinking.delta') this.applyThinkingDelta(event.conversationId, event.runId, event.data.offset, event.data.text);
    else if (event.eventType === 'assistant.snapshot') this.upsertTurn(event.conversationId, this.fromMessage(event.data.message));
    else if (event.eventType === 'memory.activity') this.applyMemoryActivity(event.conversationId, event.runId, event.data);
    else if (event.eventType === 'tool.status') this.applyToolStatus(event.conversationId, event.runId, event.data, event.eventId, cursor);
    else if (event.eventType === 'run.error') { this.setRunState(event.conversationId, 'error'); this.replaceConversationRun(event.conversationId, (run) => run ? { ...run, status: 'failed', error: event.data } : run); this.setNotice(event.conversationId, event.data.message || 'Aura could not complete this response. Your message is still here.'); }
  }

  private async refresh(id: string): Promise<boolean> {
    const guard = this.captureSessionGuard();
    try {
      const detail = await this.api.getConversation(id);
      if (!this.matchesSessionGuard(guard)) return false;
      // The legacy conversation projection intentionally excludes sibling
      // branch content. Preserve the selected opaque branch and rehydrate its
      // authorized path after refreshing shared metadata; otherwise a terminal
      // SSE event can replace an alternative transcript with root messages
      // while leaving its active branch id unchanged.
      const previous = this.find(id);
      const selectedBranchId = previous?.branch?.activeId;
      let conversation = this.fromDetail(detail);
      if (selectedBranchId && this.api.getConversationBranch) {
        try {
          const branchDetail = await this.api.getConversationBranch(id, selectedBranchId);
          conversation = this.fromBranchDetail(branchDetail, conversation);
        } catch {
          // Keep the last authorized branch path with the refreshed metadata
          // during a transient branch-detail outage; never show root content
          // under the alternative branch id while waiting for reconciliation.
          if (previous) conversation = { ...conversation, turns: previous.turns, currentRun: previous.currentRun, retryableRun: previous.retryableRun, runs: previous.runs, branch: previous.branch };
        }
      }
      // A user can enter edit mode while the branch detail request is in
      // flight. Re-read the interaction state at commit time so a late
      // terminal refresh does not erase the textarea or its draft. Only keep
      // the edit target when the refreshed authorized path still contains the
      // original user message.
      const latest = this.find(id);
      if (latest?.branch && conversation.branch && latest.branch.activeId === conversation.branch.activeId) {
        const latestEditId = latest.branch.editingMessageId;
        const editStillExists = Boolean(latestEditId && conversation.turns.some((turn) => turn.id === latestEditId && turn.role === 'user'));
        conversation = {
          ...conversation,
          branch: {
            ...conversation.branch,
            editingMessageId: editStillExists ? latestEditId : null,
            editDraft: editStillExists ? latest.branch.editDraft : '',
            error: latest.branch.error,
            errorAction: latest.branch.errorAction,
            loading: latest.branch.loading,
          },
        };
      }
      this.replaceConversation(conversation); this.hydrateHistoricalToolActivities(conversation);
      const latestRun = conversation.currentRun ?? conversation.runs.at(-1);
      if (latestRun && this.api.getRunMemoryActivity) await this.readMemoryActivity(id, latestRun.id, guard);
      if (!this.matchesSessionGuard(guard)) return false;
      if (conversation.currentRun) { this.setRunState(id, this.statusToState(conversation.currentRun.status)); this.subscribe(conversation.currentRun); }
      else if (conversation.retryableRun) { this.setRunState(id, this.statusToState(conversation.retryableRun.status)); this.setNotice(id, conversation.retryableRun.status === 'failed' ? 'Aura could not complete this response. Your message is still here.' : 'Generation stopped. Your partial response is still here.'); }
      else this.setRunState(id, 'idle');
      await this.loadBranches(id);
      return true;
    } catch (error: unknown) { this.handleError(id, error, guard); return false; }
  }
  private async loadConversationPage(generation: number): Promise<ReadonlyArray<ConversationSummary>> {
    const page = await this.api.listConversations(undefined, this.libraryFilters());
    if (generation === this.libraryRequestGeneration) this.libraryNextCursor.set(page.nextCursor);
    return page.items;
  }
  private reconcileMetadataVisibility(id: string, archived: boolean): void {
    const archiveState = this.libraryFilters().archiveState ?? 'active';
    const shouldRemove = (archiveState === 'active' && archived) || (archiveState === 'archived' && !archived);
    // Keep the selected conversation visible after a row mutation so a direct
    // open remains readable/recoverable and its draft is never displaced.
    if (!shouldRemove || this.selectedId() === id) return;
    this.conversations.update((items) => items.filter((conversation) => conversation.id !== id));
  }
  private rejectSubmission(id: string, error: unknown, guard?: SessionGuard): void {
    if (guard && !this.matchesSessionGuard(guard)) return;
    const submission = this.pendingSubmissions.get(id);
    this.pendingSubmissions.delete(id);
    if (submission) this.removeTurn(id, submission.pendingTurnId);
    const apiError = this.toApiError(error);
    this.setRunState(id, 'error');
    if (apiError.status === 409 && /agent[\s_-]*disabled|disabled[\s_-]*agent/i.test(`${apiError.code ?? ''} ${apiError.message}`)) {
      this.agentRefreshRequested.update((value) => value + 1);
      this.setNotice(id, 'This agent was disabled before the request was accepted. Select an active replacement to continue.');
      void this.refresh(id);
      return;
    }
    if (apiError.code === 'model_vision_required') {
      this.modelVisionRequiredState.update((states) => ({ ...states, [id]: this.find(id)?.modelId ?? this.selectedModelId() }));
      this.setNotice(id, 'The selected model is not verified for image understanding. Your draft and attachments are preserved; choose a verified vision model and try again.');
      return;
    }
    this.setNotice(id, apiError.message || 'We could not complete this request. Your message is still here.');
  }

  private handleError(id: string, error: unknown, guard?: SessionGuard): void {
    if (guard && !this.matchesSessionGuard(guard)) return;
    const apiError = this.toApiError(error);
    if (apiError.status === 401 || apiError.status === 403) this.invalidateSession(apiError.status);
    this.setRunState(id, 'error');
    this.setNotice(id, apiError.message || 'We could not complete this request. Your message is still here.');
  }

  private resetManagementState(): void {
    if (this.resettingManagementState) return;
    this.resettingManagementState = true;
    try { this.managementSession?.resetSessionState(); }
    finally { this.resettingManagementState = false; }
  }

  private clearOwnerScopedConversationState(): void {
    this.subscriptions.forEach((subscription) => subscription.close());
    this.subscriptions.clear();
    this.toolReplaySubscriptions.forEach((subscription) => subscription.close());
    this.toolReplaySubscriptions.clear();
    this.reconnectTimers.forEach((timer) => clearTimeout(timer));
    this.reconnectTimers.clear();
    this.memoryActivityRetryTimers.forEach((timer) => clearTimeout(timer));
    this.memoryActivityRetryTimers.clear();
    this.memoryActivityRetryAttempts.clear();
    this.lastEventIds.clear();
    this.expiredCursorRecoveries.clear();
    if (this.filterTimer) clearTimeout(this.filterTimer);
    this.filterTimer = null;
    this.pendingSubmissions.clear();
    this.branchIntents.clear();
    this.conversations.set([emptyDraft]);
    this.selectedId.set(emptyDraft.id);
    this.drafts.set({ [emptyDraft.id]: '' });
    this.orphanedEditDrafts.set({});
    this.draftAttachments.set({ [emptyDraft.id]: [] });
    this.attachmentAcceptanceConversationId.set(null);
    this.notices.set({});
    this.runStates.set({ [emptyDraft.id]: 'idle' });
    this.modelVisionRequiredState.set({});
    this.modelThinking.set({});
    this.branchAnnouncements.set({});
    this.memoryActivityNotices.set({});
    this.memoryPopupRecords.set({});
    this.memoryPopupLoading.set({});
    this.memoryPopupErrors.set({});
    this.lastPersistedDraftId.set(null);
    this.pendingConfiguration.set(null);
    this.pendingAgent.set(null);
    this.pendingPersona.set(null);
    this.pendingPersonaReset.set(false);
    this.defaultDraftAgentResolution.set('pending');
    this.defaultDraftAgentError.set(null);
  }

  private publishAttachmentAcceptance(conversationId: string): void {
    this.attachmentAcceptanceConversationId.set(conversationId);
    this.attachmentAcceptanceRevision.update((value) => value + 1);
  }

  private handleManagementAuthorizationFailure(status: SessionAuthorizationStatus): void {
    this.invalidateSession(status);
    if (status === 401) {
      this.session.set(null);
      this.authState.set('unauthenticated');
    } else {
      this.authState.set('error');
    }
  }

  private sessionFingerprint(session: Session): string {
    return [session.principal.issuer, session.principal.subject, session.csrfToken].join('\u0000');
  }

  private captureSessionGuard(): SessionGuard { return { epoch: this.sessionEpoch, fingerprint: this.acceptedSessionFingerprint }; }
  private matchesSessionGuard(guard: SessionGuard): boolean { return guard.epoch === this.sessionEpoch && guard.fingerprint === this.acceptedSessionFingerprint; }
  private invalidateSession(status: SessionAuthorizationStatus): void {
    this.sessionEpoch += 1;
    ++this.modelCatalogRequestGeneration;
    if (status === 401) this.clearOwnerScopedConversationState();
    this.resetManagementState();
    if (status === 401) {
      this.acceptedSessionFingerprint = null;
      this.session.set(null);
      this.authState.set('unauthenticated');
    } else {
      this.authState.set('error');
    }
  }

  private cancelPendingConfigurationForSelection(id: string): void { if (this.pendingConfiguration()?.conversationId !== id) this.pendingConfiguration.set(null); }

  private fromSummary(summary: ConversationSummary): Conversation {
    const previous = this.find(summary.id);
    const raw = summary as ConversationSummary & { agent?: { profileId: string; revisionId: string; revision: number; displayName: string; status: 'active' | 'disabled'; newerRevisionAvailable?: boolean }; agentAssignments?: ReadonlyArray<{ id: string; agent: { profileId: string; revisionId: string; revision: number; displayName: string; status: 'active' | 'disabled'; newerRevisionAvailable?: boolean }; reason: ConversationAssignment['reason']; effectiveAfterMessageId: string | null; createdAt: string }> };
    const agent = raw.agent ? { profileId: raw.agent.profileId, revisionId: raw.agent.revisionId, revision: raw.agent.revision, displayName: raw.agent.displayName, status: raw.agent.status, newerRevisionAvailable: raw.agent.newerRevisionAvailable } : summary.agentRevisionId ? { profileId: summary.agentProfileId, revisionId: summary.agentRevisionId, revision: 1, displayName: 'Aura', status: 'active' as const } : null;
    const assignments = (raw.agentAssignments ?? []).map((item) => ({ id: item.id, agent: { profileId: item.agent.profileId, revisionId: item.agent.revisionId, revision: item.agent.revision, displayName: item.agent.displayName, status: item.agent.status, newerRevisionAvailable: item.agent.newerRevisionAvailable }, reason: item.reason, afterMessageId: item.effectiveAfterMessageId, changedAt: item.createdAt }));
    const persona = summary.persona ?? null;
    const personaAssignments = (summary.personaAssignments ?? []).map((item) => ({ id: item.id, persona: item.persona, source: item.source, reason: item.reason, afterMessageId: item.effectiveAfterMessageId, changedAt: item.createdAt }));
    const currentRun = isConversationRun(summary.currentRun) ? summary.currentRun : null;
    return { id: summary.id, title: summary.title, turns: [], updatedAt: Date.parse(summary.updatedAt), modelId: summary.modelId, version: summary.version, archivedAt: summary.archivedAt ?? null, currentRun, retryableRun: null, agent, assignments, persona, personaOverride: summary.personaOverride ?? false, personaAssignments, runs: currentRun ? [currentRun] : [], ...(previous?.branch ? { branch: previous.branch } : { branch: emptyBranchState() }) };
  }
  private mergeSummary(summary: ConversationSummary, existing: Conversation): Conversation {
    const mapped = this.fromSummary(summary);
    const runsById = new Map(existing.runs.map((run) => [run.id, run]));
    for (const run of mapped.runs) runsById.set(run.id, run);
    const currentRun = mapped.currentRun;
    const retryableRun = currentRun && this.isRetryable(currentRun.status) ? currentRun : existing.retryableRun;
    return { ...mapped, turns: existing.turns, currentRun, retryableRun, runs: [...runsById.values()], ...(existing.toolActivities ? { toolActivities: existing.toolActivities } : {}), ...(existing.toolActivityEventIds ? { toolActivityEventIds: existing.toolActivityEventIds } : {}), ...(existing.toolActivityCursors ? { toolActivityCursors: existing.toolActivityCursors } : {}) };
  }
  private fromDetail(detail: ConversationDetail): Conversation { const retryableRun = this.latestRetryableRun(detail); const base = this.fromSummary(detail); const previous = this.find(detail.id); const turns = detail.messages.map((message) => { const next = this.fromMessage(message); const old = previous?.turns.find((turn) => turn.id === message.id); return old?.memoryActivities ? { ...next, memoryActivities: old.memoryActivities } : next; }); const currentRun = isConversationRun(detail.currentRun) ? detail.currentRun : null; return { ...base, turns, currentRun, retryableRun, runs: this.uniqueRuns([...detail.recentRuns, ...(currentRun ? [currentRun] : [])]), ...(previous?.branch ? { branch: previous.branch } : {}), ...(previous?.toolActivities ? { toolActivities: previous.toolActivities } : {}), ...(previous?.toolActivityEventIds ? { toolActivityEventIds: previous.toolActivityEventIds } : {}), ...(previous?.toolActivityCursors ? { toolActivityCursors: previous.toolActivityCursors } : {}) }; }
  private uniqueRuns(runs: ReadonlyArray<Run>): ReadonlyArray<Run> { const seen = new Set<string>(); return runs.filter(isConversationRun).filter((run) => { if (seen.has(run.id)) return false; seen.add(run.id); return true; }); }
  private latestRetryableRun(detail: ConversationDetail): Run | null { return [...detail.recentRuns, ...(isConversationRun(detail.currentRun) ? [detail.currentRun] : [])].filter(isConversationRun).filter((run) => this.isRetryable(run.status)).sort((left, right) => Date.parse(right.finishedAt ?? right.createdAt) - Date.parse(left.finishedAt ?? left.createdAt))[0] ?? null; }
  private fromMessage(message: Message): ConversationTurn { const raw = message as Message & { readonly citations?: unknown; readonly attachments?: unknown }; const citations = normalizeDocumentCitations(raw.citations); const attachments = normalizeAttachments(raw.attachments); return { id: message.id, role: message.role, text: message.content, state: message.state === 'complete' ? undefined : message.state === 'failed' ? 'failed' : message.state === 'interrupted' ? 'interrupted' : 'partial', runId: message.runId, ...(citations.length ? { citations } : {}), ...(attachments.length ? { attachments } : {}) }; }
  private hydrateHistoricalToolActivities(conversation: Conversation): void {
    for (const run of conversation.runs) {
      if (conversation.currentRun?.id === run.id || !this.isTerminal(run.status) || this.toolReplaySubscriptions.has(run.id)) continue;
      let finished = false;
      let subscription: RunEventSubscription | null = null;
      const finish = (): void => {
        finished = true;
        subscription?.close();
        this.toolReplaySubscriptions.delete(run.id);
      };
      subscription = this.api.streamRunEvents(run.id, conversation.toolActivityCursors?.[run.id], (event, cursor) => {
        if (!isConversationRunEvent(event)) return;
        if (event.eventType === 'tool.status') this.applyToolStatus(event.conversationId, event.runId, event.data, event.eventId, cursor);
        else if (event.eventType === 'run.status' && this.isTerminal(event.data.status)) finish();
      }, finish);
      if (finished) subscription.close();
      else this.toolReplaySubscriptions.set(run.id, subscription);
    }
  }
  private applyMemoryActivity(conversationId: string, runId: string, activity: MemoryActivity): void {
    // Recall is published synchronously with the answer while extraction may
    // still be settling. Keep reconciliation alive until a non-recall
    // extraction activity becomes authoritative.
    if (this.isAuthoritativeMemoryExtraction(activity)) this.clearMemoryActivityRetry(runId);
    const conversation = this.find(conversationId); if (!conversation) return;
    this.setMemoryActivityNotice(conversationId, null);
    this.clearMemoryPopupError(activity.id);
    const assistant = [...conversation.turns].reverse().find((turn) => turn.role === 'assistant' && turn.runId === runId);
    if (!assistant) return;
    const existing = assistant.memoryActivities ?? [];
    const index = existing.findIndex((item) => item.id === activity.id);
    const activities = index < 0 ? [...existing, ...visibleMemoryActivities([activity])] : visibleMemoryActivities(existing.map((item, itemIndex) => itemIndex === index ? activity : item));
    this.upsertTurn(conversationId, { ...assistant, memoryActivities: activities });
  }
  private applyToolStatus(conversationId: string, runId: string, activity: ToolStatusData, eventId?: string, cursor?: string): void {
    const conversation = this.find(conversationId);
    if (!conversation) return;
    const existing = conversation.toolActivities?.[runId] ?? [];
    const eventIds = conversation.toolActivityEventIds?.[runId] ?? [];
    const lifecycleKey = `${activity.callId}:${activity.status}:${activity.errorClass ?? ''}:${activity.errorCode ?? ''}:${activity.resultCount ?? ''}`;
    const duplicate = (eventId && eventIds.includes(eventId)) || existing.some((item) => `${item.callId}:${item.status}:${item.errorClass ?? ''}:${item.errorCode ?? ''}:${item.resultCount ?? ''}` === lifecycleKey);
    if (duplicate) {
      if (cursor) this.replaceConversation({ ...conversation, toolActivityCursors: { ...(conversation.toolActivityCursors ?? {}), [runId]: cursor } });
      return;
    }
    const activities = appendToolStatusActivity(existing, activity);
    this.replaceConversation({ ...conversation, toolActivities: { ...(conversation.toolActivities ?? {}), [runId]: activities }, toolActivityEventIds: { ...(conversation.toolActivityEventIds ?? {}), [runId]: eventId ? [...eventIds, eventId] : eventIds }, ...(cursor ? { toolActivityCursors: { ...(conversation.toolActivityCursors ?? {}), [runId]: cursor } } : {}) });
  }
  private applyThinkingDelta(conversationId: string, runId: string, offset: unknown, text: unknown): void {
    const conversation = this.find(conversationId);
    // A thinking frame is accepted only for the currently active run. This
    // rejects stale reconnects and cross-conversation/run delivery before it
    // can reach the UI.
    if (!conversation?.currentRun || conversation.currentRun.id !== runId || this.isTerminal(conversation.currentRun.status)) return;
    const existing = this.modelThinking()[runId] ?? { runId, text: '', nextOffset: 0 };
    const next = reconcileModelThinkingDelta(existing, runId, offset, text);
    if (!next) return;
    this.thinkingRunConversations.set(runId, conversationId);
    this.modelThinking.update((items) => ({ ...items, [runId]: next }));
  }
  private applyMemorySnapshot(snapshot: RunMemoryActivitySnapshot): void {
    const conversation = this.conversations().find((item) => item.runs.some((run) => run.id === snapshot.runId) || item.currentRun?.id === snapshot.runId);
    if (!conversation) return;
    const assistant = [...conversation.turns].reverse().find((turn) => turn.role === 'assistant' && turn.runId === snapshot.runId);
    if (assistant) this.upsertTurn(conversation.id, { ...assistant, memoryActivities: visibleMemoryActivities(snapshot.items) });
  }
  async retryMemoryActivity(runId: string): Promise<void> {
    if (!this.api.getRunMemoryActivity) return;
    const conversation = this.conversations().find((item) => item.runs.some((run) => run.id === runId) || item.currentRun?.id === runId);
    if (!conversation) return;
    this.clearMemoryActivityRetry(runId);
    await this.readMemoryActivity(conversation.id, runId);
  }
  async loadMemoryPopup(turnId: string): Promise<void> {
    const activities = visibleMemoryActivities(this.find(this.selectedId())?.turns.find((turn) => turn.id === turnId)?.memoryActivities ?? []);
    if (!activities.length || (!this.api.getMemoryDetail && !this.api.getMemoryCandidateDetail)) return;
    const pending = Object.fromEntries(activities.map((activity) => [activity.id, true]));
    this.memoryPopupLoading.update((items) => ({ ...items, ...pending }));
    await Promise.all(activities.map(async (activity) => {
      try {
        if (this.isPendingMemoryActivity(activity)) {
          this.clearMemoryPopupError(activity.id);
        } else if (activity.memoryId && this.api.getMemoryDetail) {
          const detail = await this.api.getMemoryDetail(activity.memoryId, activity.scope ?? undefined);
          this.memoryPopupRecords.update((items) => ({ ...items, [activity.id]: { kind: 'memory', detail } }));
          this.clearMemoryPopupError(activity.id);
        } else if (activity.candidateId && this.api.getMemoryCandidateDetail) {
          const detail = await this.api.getMemoryCandidateDetail(activity.candidateId);
          this.memoryPopupRecords.update((items) => ({ ...items, [activity.id]: { kind: 'candidate', detail } }));
          this.clearMemoryPopupError(activity.id);
        } else {
          this.memoryPopupErrors.update((items) => ({ ...items, [activity.id]: 'No owner-authorized record is attached to this activity.' }));
        }
      } catch {
        // Provider/API errors must not leak private content or server detail
        // into the conversation surface.
        this.memoryPopupErrors.update((items) => ({ ...items, [activity.id]: 'Record details are temporarily unavailable.' }));
      } finally {
        this.memoryPopupLoading.update((items) => ({ ...items, [activity.id]: false }));
      }
    }));
  }
  memoryPopupRecord(activityId: string): MemoryPopupRecord | null { return this.memoryPopupRecords()[activityId] ?? null; }
  memoryPopupIsLoading(activityId: string): boolean { return this.memoryPopupLoading()[activityId] ?? false; }
  memoryPopupError(activityId: string): string | null { return this.memoryPopupErrors()[activityId] ?? null; }
  private find(id: string): Conversation | undefined { return this.conversations().find((conversation) => conversation.id === id); }
  private replaceConversation(conversation: Conversation): void { this.conversations.update((items) => items.map((item) => item.id === conversation.id ? { ...conversation, updatedAt: conversation.updatedAt || Date.now() } : item)); }
  private migrateDraftComposerState(fromId: string, toId: string): void {
    if (fromId === toId || !fromId.startsWith('draft-')) return;
    const draftText = this.drafts()[fromId];
    const draftAttachments = this.draftAttachments()[fromId];
    if (draftText !== undefined && !(this.drafts()[toId]?.trim())) this.drafts.update((drafts) => ({ ...drafts, [toId]: draftText }));
    if (draftAttachments?.length && !(this.draftAttachments()[toId]?.length)) this.draftAttachments.update((attachments) => ({ ...attachments, [toId]: [...draftAttachments] }));
  }
  private hasDraftComposerState(id: string): boolean { return Boolean(this.drafts()[id]?.trim() || this.draftAttachments()[id]?.length); }
  private upsertConversation(conversation: Conversation): void {
    this.conversations.update((items) => {
      const index = items.findIndex((item) => item.id === conversation.id);
      if (index < 0) return [...items, conversation];
      return items.map((item, itemIndex) => itemIndex === index ? { ...conversation, updatedAt: conversation.updatedAt || Date.now() } : item);
    });
  }
  private promoteConversation(id: string): void {
    this.conversations.update((items) => {
      const index = items.findIndex((item) => item.id === id);
      if (index <= 0) return items;
      const conversation = items[index];
      if (!conversation) return items;
      return [conversation, ...items.slice(0, index), ...items.slice(index + 1)];
    });
  }
  private replaceRun(id: string, run: Run): void { this.replaceConversationRun(id, () => run); }
  private replaceConversationRun(id: string, update: (run: Run | null) => Run | null): void { const conversation = this.find(id); if (conversation) { const run = update(conversation.currentRun); const runs = run && !conversation.runs.some((item) => item.id === run.id) ? [...conversation.runs, run] : conversation.runs.map((item) => item.id === run?.id ? run : item).filter((item): item is Run => Boolean(item)); this.replaceConversation({ ...conversation, currentRun: run, runs, retryableRun: run && this.isRetryable(run.status) ? run : conversation.retryableRun }); } }
  private appendTurn(id: string, turn: ConversationTurn): void { const conversation = this.find(id); if (conversation) this.replaceConversation({ ...conversation, turns: [...conversation.turns, turn], updatedAt: Date.now(), title: conversation.turns.length === 0 ? this.makeTitle(turn.text) : conversation.title }); }
  private removeTurn(id: string, turnId: string): void { const conversation = this.find(id); if (conversation) this.replaceConversation({ ...conversation, turns: conversation.turns.filter((turn) => turn.id !== turnId) }); }
  private upsertTurn(id: string, turn: ConversationTurn): void { const conversation = this.find(id); if (!conversation) return; const exists = conversation.turns.some((item) => item.id === turn.id); this.replaceConversation({ ...conversation, turns: exists ? conversation.turns.map((item) => item.id === turn.id ? turn : item) : [...conversation.turns, turn], updatedAt: Date.now() }); }
  private appendAssistantDelta(id: string, messageId: string, offset: number, text: string, runId: string): void { const conversation = this.find(id); if (!conversation) return; const existing = conversation.turns.find((turn) => turn.id === messageId); const content = existing?.text ?? ''; const next = offset <= content.length ? `${content.slice(0, offset)}${text}` : `${content}${text}`; this.upsertTurn(id, { id: messageId, role: 'assistant', text: next, state: 'partial', runId }); }
  private setRunState(id: string, state: RunState): void { this.runStates.update((states) => ({ ...states, [id]: state })); }
  private setNotice(id: string, notice: string | null): void { this.notices.update((notices) => ({ ...notices, [id]: notice })); }
  private setBranchAnnouncement(id: string, announcement: string): void { this.branchAnnouncements.update((items) => ({ ...items, [id]: announcement })); }
  private setMemoryActivityNotice(id: string, notice: string | null): void { this.memoryActivityNotices.update((notices) => ({ ...notices, [id]: notice })); }
  private clearThinkingForConversation(conversationId: string): void {
    const current = this.modelThinking();
    const next = Object.fromEntries(Object.entries(current).filter(([runId]) => this.thinkingRunConversations.get(runId) !== conversationId));
    for (const runId of this.thinkingRunConversations.keys()) if (this.thinkingRunConversations.get(runId) === conversationId) this.thinkingRunConversations.delete(runId);
    if (Object.keys(next).length !== Object.keys(current).length) this.modelThinking.set(next);
  }
  private isPendingMemoryActivity(activity: MemoryActivity): boolean { return activity.reconciliationStatus === 'pending' || (activity.status === 'queued' && !activity.candidateId && !activity.memoryId); }
  private isAuthoritativeMemoryExtraction(activity: MemoryActivity): boolean { return activity.action !== 'recalled' && activity.reconciliationStatus === 'authoritative'; }
  private clearMemoryPopupError(activityId: string): void {
    this.memoryPopupErrors.update((items) => {
      if (!(activityId in items)) return items;
      const next = { ...items };
      delete next[activityId];
      return next;
    });
  }
  private async readMemoryActivity(conversationId: string, runId: string, guard = this.captureSessionGuard()): Promise<void> {
    if (!this.api.getRunMemoryActivity) return;
    try {
      const snapshot = await this.api.getRunMemoryActivity(runId);
      if (!this.matchesSessionGuard(guard)) return;
      this.applyMemorySnapshot(snapshot);
      this.setMemoryActivityNotice(conversationId, null);
      // The run stream may close on run.status before the asynchronous memory
      // projection publishes its activity. Keep a bounded, silent retry for
      // an empty settled snapshot so a late authoritative create/reinforce is
      // visible without a reload. If it remains empty, no indicator or banner
      // is produced for ignored/rejected extraction.
      const terminal = snapshot.processingStatus === 'settled'
        && snapshot.items.some((item) => this.isAuthoritativeMemoryExtraction(item));
      if (terminal) this.clearMemoryActivityRetry(runId);
      else this.scheduleMemoryActivityRetry(conversationId, runId);
    } catch (error: unknown) {
      if (this.matchesSessionGuard(guard)) this.handleMemoryActivityReadFailure(conversationId, runId, error, guard);
    }
  }
  private handleMemoryActivityReadFailure(conversationId: string, runId: string, error: unknown, guard = this.captureSessionGuard()): void {
    const apiError = this.toApiError(error);
    // Core uses 404 when the run has no memory job or recall metadata yet.
    // That is an expected transient/no-activity result, not a transport
    // outage. The conversation detail was already authorized and loaded by
    // this point, so preserve the transcript, clear the banner, and perform a
    // bounded background retry for an outbox row that is still being created.
    if (apiError.status === 404) { this.setMemoryActivityNotice(conversationId, null); this.scheduleMemoryActivityRetry(conversationId, runId, guard); return; }
    this.clearMemoryActivityRetry(runId);
    this.setMemoryActivityNotice(conversationId, 'Memory activity is temporarily unavailable. Your transcript is preserved. Try reconciliation again.');
  }
  private scheduleMemoryActivityRetry(conversationId: string, runId: string, guard = this.captureSessionGuard()): void {
    if (!this.matchesSessionGuard(guard)) return;
    if (this.memoryActivityRetryTimers.has(runId)) return;
    const attempts = this.memoryActivityRetryAttempts.get(runId) ?? 0;
    if (attempts >= MEMORY_ACTIVITY_RETRY_DELAYS_MS.length) return;
    const delay = MEMORY_ACTIVITY_RETRY_DELAYS_MS[attempts] ?? 120_000;
    this.memoryActivityRetryAttempts.set(runId, attempts + 1);
    const timer = setTimeout(() => {
      this.memoryActivityRetryTimers.delete(runId);
      if (this.matchesSessionGuard(guard) && this.find(conversationId)) void this.readMemoryActivity(conversationId, runId, guard);
    }, delay);
    this.memoryActivityRetryTimers.set(runId, timer);
  }
  private clearMemoryActivityRetry(runId: string): void {
    const timer = this.memoryActivityRetryTimers.get(runId);
    if (timer) clearTimeout(timer);
    this.memoryActivityRetryTimers.delete(runId);
    this.memoryActivityRetryAttempts.delete(runId);
  }
  private ensureDraftModel(): void {
    const modelId = this.defaultModelId();
    const drafts = this.conversations().filter((conversation) => conversation.id.startsWith('draft-') && !conversation.modelId);
    for (const current of drafts) this.replaceConversation({ ...current, modelId });
    if (!modelId && !this.modelCatalogNotice()) this.modelCatalogNotice.set('The configured default model is unavailable. Choose an available model to continue.');
    else if (this.modelCatalogNotice() === 'The configured default model is unavailable. Choose an available model to continue.') this.modelCatalogNotice.set(null);
  }
  private canRunWithSelectedAgent(id: string): boolean {
    const conversation = this.find(id);
    // A brand-new draft intentionally omits an agent reference for backward
    // compatibility; Core resolves that omission to seeded Aura revision 1.
    if (conversation?.id.startsWith('draft-') && !conversation.agent) return true;
    return Boolean(conversation?.agent?.revisionId && conversation.agent.status === 'active');
  }
  private clearDraftAfterAcceptance(id: string, submittedDraft: string | undefined): void {
    if (submittedDraft === undefined) return;
    this.drafts.update((drafts) => drafts[id] === submittedDraft ? { ...drafts, [id]: '' } : drafts);
  }
  private draftValueAfterAcceptance(id: string, submittedDraft: string | undefined): string {
    const current = this.drafts()[id] ?? '';
    return submittedDraft !== undefined && current === submittedDraft ? '' : current;
  }
  private modelFor(id: string): string { return this.find(id)?.modelId || this.defaultModelId(); }
  private defaultModelId(): string { const configured = this.modelCatalogDefaultId(); const configuredModel = this.models().find((model) => model.id === configured); return configuredModel?.selectable ? configuredModel.id : ''; }
  private statusToState(status: Run['status'] | undefined): RunState { return status === 'queued' || status === 'running' || status === 'cancel_requested' ? 'working' : status === 'waiting_for_confirmation' ? 'waiting' : status === 'failed' ? 'error' : status === 'canceled' || status === 'interrupted' ? 'interrupted' : 'idle'; }
  private isRetryable(status: Run['status']): boolean { return status === 'canceled' || status === 'failed' || status === 'interrupted'; }
  private isTerminal(status: Run['status']): boolean { return status === 'completed' || this.isRetryable(status); }
  private key(): string { return typeof crypto !== 'undefined' && 'randomUUID' in crypto ? crypto.randomUUID() : `aura-${Date.now()}-${Math.random().toString(36).slice(2)}`; }
  private toApiError(error: unknown): ConversationApiError { return error && typeof error === 'object' && 'message' in error ? error as ConversationApiError : { message: 'We could not connect to Aura. Your draft is still here.', retryable: true }; }
  private makeTitle(text: string): string { return text.length > 32 ? `${text.slice(0, 32).trimEnd()}…` : text; }
}

function normalizeAttachments(value: unknown): ReadonlyArray<AttachmentSummary> {
  if (!Array.isArray(value)) return [];
  return value.filter((item): item is AttachmentSummary => Boolean(item && typeof item === 'object' && (item as AttachmentSummary).kind && (item as AttachmentSummary).sourceId && (item as AttachmentSummary).displayFilename)).slice(0, 8);
}

function turnAttachments(conversation: Conversation, messageId: string): ReadonlyArray<AttachmentSelection> {
  const turn = conversation.turns.find((item) => item.id === messageId);
  return (turn?.attachments ?? []).map((item): AttachmentSelection => item.kind === 'document' ? { kind: 'document', documentId: item.sourceId } : { kind: 'image', attachmentId: item.sourceId });
}

function sameAttachmentSelections(current: ReadonlyArray<AttachmentSelection>, next: ReadonlyArray<AttachmentSelection>): boolean {
  return current.length === next.length && current.every((item, index) => {
    const candidate = next[index];
    if (!candidate || item.kind !== candidate.kind) return false;
    if (item.kind === 'document' && candidate.kind === 'document') return item.documentId === candidate.documentId;
    if (item.kind === 'image' && candidate.kind === 'image') return item.attachmentId === candidate.attachmentId;
    return false;
  });
}
