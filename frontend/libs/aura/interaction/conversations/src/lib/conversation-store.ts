import { Inject, Injectable, computed, signal } from '@angular/core';
import type { ConversationDetail, ConversationSummary, ConversationRunAccepted, Message, Model, Run, RunEvent, Session } from '@aura/aura-api-client';
import type { AgentReference } from '@aura/aura/interaction/agents';
import { AuraConversationApi, type ConversationApi, type ConversationApiError, type RunEventHandler, type RunEventSubscription } from './conversation-api';

export type TurnRole = 'user' | 'assistant';
export type RunState = 'idle' | 'working' | 'interrupted' | 'error';
export type AuthState = 'loading' | 'authenticated' | 'unauthenticated' | 'error';

export interface ConversationTurn { readonly id: string; readonly role: TurnRole; readonly text: string; readonly state?: 'partial' | 'interrupted' | 'failed'; readonly runId?: string | null; readonly transitionMarker?: string; }
export interface ConversationAssignment { readonly id: string; readonly agent: AgentReference; readonly reason: 'initial' | 'manual_switch' | 'revision_upgrade'; readonly afterMessageId: string | null; readonly changedAt: string; }
export interface Conversation { readonly id: string; readonly title: string; readonly turns: ReadonlyArray<ConversationTurn>; readonly updatedAt: number; readonly modelId: string; readonly version: number; readonly currentRun: Run | null; readonly retryableRun: Run | null; readonly agent: AgentReference | null; readonly assignments: ReadonlyArray<ConversationAssignment>; readonly runs: ReadonlyArray<Run>; }

const draftAgent: AgentReference = { profileId: '', revisionId: '', revision: 1, displayName: 'Aura', status: 'active' };
const emptyDraft: Conversation = { id: 'draft-welcome', title: 'New conversation', turns: [], updatedAt: Date.now(), modelId: '', version: 0, currentRun: null, retryableRun: null, agent: null, assignments: [], runs: [] };

@Injectable({ providedIn: 'root' })
export class ConversationStore {
  readonly loading = signal(true);
  readonly authState = signal<AuthState>('loading');
  readonly session = signal<Session | null>(null);
  readonly models = signal<ReadonlyArray<Model>>([]);
  readonly modelCatalogDefaultId = signal<string | null>(null);
  readonly conversations = signal<ReadonlyArray<Conversation>>([emptyDraft]);
  readonly selectedId = signal(emptyDraft.id);
  readonly drafts = signal<Readonly<Record<string, string>>>({ [emptyDraft.id]: '' });
  readonly notices = signal<Readonly<Record<string, string | null>>>({});
  readonly runStates = signal<Readonly<Record<string, RunState>>>({ [emptyDraft.id]: 'idle' });
  readonly selected = computed(() => this.conversations().find((conversation) => conversation.id === this.selectedId()) ?? emptyDraft);
  readonly draft = computed(() => this.drafts()[this.selectedId()] ?? '');
  readonly notice = computed(() => this.notices()[this.selectedId()] ?? null);
  readonly runState = computed<RunState>(() => this.runStates()[this.selectedId()] ?? this.statusToState(this.selected().currentRun?.status));
  readonly selectedModelId = computed(() => this.selected().modelId);
  readonly selectedAgent = computed(() => this.selected().agent ?? draftAgent);
  readonly canSend = computed(() => Boolean(this.draft().trim() && this.authenticated() && this.modelFor(this.selectedId()) && this.runState() !== 'working' && this.canRunWithSelectedAgent(this.selectedId())));
  readonly pendingAgent = signal<AgentReference | null>(null);
  readonly pendingAgentAction = signal<'switch' | 'upgrade'>('switch');
  /** Signals the shell to refresh its active replacement options after a
   * server-side agent status change. */
  readonly agentRefreshRequested = signal(0);
  readonly authenticated = computed(() => this.authState() === 'authenticated');

  private readonly subscriptions = new Map<string, RunEventSubscription>();
  private readonly lastEventIds = new Map<string, string>();
  private readonly expiredCursorRecoveries = new Set<string>();
  private readonly reconnectTimers = new Map<string, ReturnType<typeof setTimeout>>();
  private readonly pendingSubmissions = new Map<string, { readonly draft: string; readonly pendingTurnId: string }>();
  private draftCounter = 0;
  private conversationListWarning = false;
  private readonly api: ConversationApi;

  constructor(@Inject(AuraConversationApi) api?: ConversationApi) { this.api = api ?? new AuraConversationApi(); void this.load(); }

  async load(): Promise<void> {
    this.loading.set(true);
    try {
      const session = await this.api.getSession();
      this.session.set(session); this.authState.set('authenticated');
      const catalog = await this.api.listModels(); this.models.set(catalog.models); this.modelCatalogDefaultId.set(catalog.defaultModelId);
      this.conversationListWarning = false;
      const loaded = (await this.listAllConversationSummaries()).map((item) => this.fromSummary(item));
      if (loaded.length === 0) this.ensureDraftModel();
      else {
        this.conversations.set(loaded); this.selectedId.set(loaded[0].id);
        for (const conversation of loaded) if (conversation.currentRun) this.subscribe(conversation.currentRun);
        await this.refresh(loaded[0].id);
        if (this.conversationListWarning) this.setNotice(loaded[0].id, 'Some older conversations could not be loaded. Please refresh and try again.');
      }
    } catch (error: unknown) {
      const apiError = this.toApiError(error);
      if (apiError.status === 401 || apiError.status === 403) this.authState.set('unauthenticated');
      else { this.authState.set('error'); this.setNotice(this.selectedId(), apiError.message); }
    } finally { this.loading.set(false); }
  }

  login(): void { this.api.startLogin(typeof window === 'undefined' ? '/' : window.location.pathname + window.location.search); }
  async logout(): Promise<void> { const token = this.session()?.csrfToken; if (!token) return; try { await this.api.logout(token); } finally { this.session.set(null); this.authState.set('unauthenticated'); } }

  select(id: string): void { if (this.conversations().some((conversation) => conversation.id === id)) { this.selectedId.set(id); this.setNotice(id, null); if (!id.startsWith('draft-')) void this.refresh(id); } }

  create(): void {
    const id = `draft-${Date.now()}-${this.draftCounter++}`;
    const conversation: Conversation = { id, title: 'New conversation', turns: [], updatedAt: Date.now(), modelId: this.defaultModelId(), version: 0, currentRun: null, retryableRun: null, agent: null, assignments: [], runs: [] };
    this.conversations.update((items) => [conversation, ...items]); this.drafts.update((drafts) => ({ ...drafts, [id]: '' })); this.runStates.update((states) => ({ ...states, [id]: 'idle' })); this.selectedId.set(id); this.setNotice(id, conversation.modelId ? null : 'The configured default model is unavailable. Choose an available model to continue.');
  }

  updateDraft(value: string): void { const id = this.selectedId(); this.drafts.update((drafts) => ({ ...drafts, [id]: value })); if (value.trim()) this.setNotice(id, null); }

  async selectModel(modelId: string): Promise<void> {
    const model = this.models().find((item) => item.id === modelId); if (!model || !model.selectable) return;
    const conversation = this.selected(); const previous = conversation.modelId; this.replaceConversation({ ...conversation, modelId });
    if (conversation.id.startsWith('draft-')) return;
    try { const summary = await this.api.updateConversation(conversation.id, modelId, conversation.version, this.key()); this.replaceConversation({ ...this.fromSummary(summary), turns: this.find(conversation.id)?.turns ?? conversation.turns }); }
    catch (error: unknown) { this.replaceConversation({ ...conversation, modelId: previous }); this.setNotice(conversation.id, this.toApiError(error).message); }
  }

  requestAgent(agent: AgentReference): void { const current = this.selectedAgent(); if (!agent.revisionId || agent.revisionId === current.revisionId) return; this.pendingAgentAction.set(agent.profileId === current.profileId && agent.revision > current.revision ? 'upgrade' : 'switch'); this.pendingAgent.set(agent); }

  transitionMarkerFor(messageId: string): string | null {
    const assignments = this.selected().assignments;
    const index = assignments.findIndex((item) => item.afterMessageId === messageId);
    if (index < 0) return null;
    const assignment = assignments[index];
    if (assignment.reason === 'initial') return null;
    const previous = assignments[index - 1]?.agent ?? this.selected().agent;
    return previous ? `Agent changed from ${previous.displayName} r${previous.revision} to ${assignment.agent.displayName} r${assignment.agent.revision}` : `Agent changed to ${assignment.agent.displayName} r${assignment.agent.revision}`;
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
      try { const summary = await this.api.updateConversation(conversation.id, conversation.modelId, conversation.version, this.key(), replacement.revisionId, true); this.replaceConversation({ ...this.fromSummary(summary), turns: this.find(conversation.id)?.turns ?? conversation.turns }); }
      catch (error: unknown) { this.replaceConversation({ ...conversation, agent: previous }); this.setNotice(conversation.id, this.toApiError(error).message); }
    } else {
      this.replaceConversation({ ...conversation, agent: replacement });
    }
  }

  send(): boolean {
    const draft = this.draft(); const text = draft.trim(); const id = this.selectedId();
    if (!text) { this.setNotice(id, 'Write a message before sending.'); return false; }
    if (this.runState() === 'working') return false;
    if (!this.modelFor(id)) { this.setNotice(id, 'Choose an available model before sending.'); return false; }
    if (!this.canRunWithSelectedAgent(id)) { this.setNotice(id, this.selected().agent ? 'This agent is disabled. Select an active replacement before sending.' : 'Select an active agent before sending.'); return false; }
    const pendingTurnId = `pending-user-${Date.now()}`;
    this.appendTurn(id, { id: pendingTurnId, role: 'user', text });
    this.pendingSubmissions.set(id, { draft, pendingTurnId });
    // Keep the exact composer value until Core accepts the command. This
    // preserves whitespace and user edits across conflicts and network
    // failures; acceptance clears it only if it has not changed meanwhile.
    this.setRunState(id, 'working'); this.setNotice(id, null);
    void (id.startsWith('draft-') ? this.createPersisted(id, text) : this.createRun(id, text)); return true;
  }

  stop(): void {
    const conversation = this.selected(); const run = conversation.currentRun; if (!run || this.runState() !== 'working') return;
    void this.api.cancelRun(run.id, this.session()?.csrfToken ?? '', this.key()).then((updated) => { this.replaceRun(conversation.id, updated); this.setRunState(conversation.id, this.statusToState(updated.status)); this.setNotice(conversation.id, 'Generation stopped. Your partial response is still here.'); }).catch((error: unknown) => this.handleError(conversation.id, error));
  }

  retry(): void {
    const conversation = this.selected(); const run = conversation.currentRun ?? conversation.retryableRun;
    if (!run || !['error', 'interrupted', 'canceled', 'failed'].includes(run.status)) { this.setNotice(conversation.id, null); return; }
    if (!this.canRunWithSelectedAgent(conversation.id)) { this.setNotice(conversation.id, conversation.agent ? 'This agent is disabled. Select an active replacement before retrying.' : 'Select an active agent before retrying.'); return; }
    this.setRunState(conversation.id, 'working'); this.setNotice(conversation.id, null);
    void this.api.retryRun(run.id, this.session()?.csrfToken ?? '', this.key()).then((accepted) => this.acceptRun(conversation.id, accepted)).catch((error: unknown) => this.handleError(conversation.id, error));
  }

  /** Compatibility hook for older local-preview fixtures; provider responses are never synthesized in production. */
  failNextLocalReply(): void { /* no-op */ }

  private async createPersisted(draftId: string, text: string): Promise<void> {
    try {
      const draftConversation = this.find(draftId); const selectedRevisionId = draftConversation?.agent?.revisionId || undefined; const accepted = await this.api.createConversation(text, this.modelFor(draftId), this.key(), selectedRevisionId); const draft = draftConversation; const persisted = this.fromSummary(accepted.conversation);
      const submission = this.pendingSubmissions.get(draftId);
      this.conversations.update((items) => [persisted, ...items.filter((item) => item.id !== draftId)]);
      this.drafts.update((drafts) => { const next = { ...drafts }; delete next[draftId]; next[persisted.id] = this.draftValueAfterAcceptance(draftId, submission?.draft); return next; }); this.runStates.update((states) => { const next = { ...states }; delete next[draftId]; next[persisted.id] = this.statusToState(accepted.run.status); return next; }); this.selectedId.set(persisted.id);
      if (draft) this.replaceConversation({ ...persisted, turns: draft.turns }); this.pendingSubmissions.delete(draftId); this.acceptRun(persisted.id, accepted, submission?.pendingTurnId);
    } catch (error: unknown) { this.rejectSubmission(draftId, error); }
  }

  private async createRun(id: string, text: string): Promise<void> { const conversation = this.find(id); if (!conversation) return; try { const submission = this.pendingSubmissions.get(id); this.acceptRun(id, await this.api.createRun(id, text, conversation.version, this.key()), submission?.pendingTurnId); this.clearDraftAfterAcceptance(id, submission?.draft); this.pendingSubmissions.delete(id); } catch (error: unknown) { this.rejectSubmission(id, error); } }

  private acceptRun(id: string, accepted: ConversationRunAccepted, pendingTurnId?: string): void {
    const existing = this.find(id); if (!existing) return;
    const turns = existing.turns.map((turn) => !pendingTurnId || turn.id === pendingTurnId ? (turn.id.startsWith('pending-user-') ? this.fromMessage(accepted.userMessage) : turn) : turn);
    this.replaceConversation({ ...this.fromSummary(accepted.conversation), turns, runs: [...this.find(id)?.runs ?? [], accepted.run] }); this.setRunState(id, this.statusToState(accepted.run.status)); this.subscribe(accepted.run);
  }

  private subscribe(run: Run, attempt = 0): void {
    const pendingReconnect = this.reconnectTimers.get(run.id);
    if (pendingReconnect) { clearTimeout(pendingReconnect); this.reconnectTimers.delete(run.id); }
    this.subscriptions.get(run.id)?.close();
    const handleEvent: RunEventHandler = (event, cursor) => this.applyEvent(event, cursor);
    this.subscriptions.set(run.id, this.api.streamRunEvents(run.id, this.lastEventIds.get(run.id), handleEvent, (error) => {
      const current = this.find(run.conversationId)?.currentRun;
      if (!current || this.isTerminal(current.status)) { if (error.status === 401 || error.status === 403) this.handleError(run.conversationId, error); return; }
      if (error.status === 410 && !this.expiredCursorRecoveries.has(run.id)) { this.expiredCursorRecoveries.add(run.id); this.lastEventIds.delete(run.id); this.scheduleReconciliation(run, attempt); return; }
      if (error.status === 401 || error.status === 403 || error.status === 404 || error.status === 410) { this.handleError(run.conversationId, error); return; }
      this.scheduleReconciliation(run, attempt, error);
    }));
  }

  private scheduleReconciliation(run: Run, attempt: number, error?: ConversationApiError): void {
    const current = this.find(run.conversationId)?.currentRun;
    if (!current || this.isTerminal(current.status)) return;
    this.setRunState(run.conversationId, 'working'); this.setNotice(run.conversationId, error?.message ?? 'Connection lost; trying again. Your partial response is still here.');
    const delay = Math.min(30_000, 500 * 2 ** Math.min(attempt, 6));
    const timer = setTimeout(() => { this.reconnectTimers.delete(run.id); void this.reconcileRun(run, attempt + 1); }, delay);
    this.reconnectTimers.set(run.id, timer);
  }

  private clearReconnect(runId: string): void { const timer = this.reconnectTimers.get(runId); if (timer) clearTimeout(timer); this.reconnectTimers.delete(runId); }

  private async reconcileRun(run: Run, attempt: number): Promise<void> {
    try {
      const detail = await this.api.getConversation(run.conversationId);
      const conversation = this.fromDetail(detail);
      this.replaceConversation(conversation);
      if (detail.currentRun && !this.isTerminal(detail.currentRun.status)) {
        this.expiredCursorRecoveries.delete(run.id); this.setRunState(run.conversationId, 'working'); this.setNotice(run.conversationId, null); this.subscribe(detail.currentRun, attempt); return;
      }
      if (conversation.retryableRun) {
        this.setRunState(run.conversationId, this.statusToState(conversation.retryableRun.status));
        this.setNotice(run.conversationId, 'Generation stopped. Your partial response is still here.');
        return;
      }
      this.scheduleReconciliation(run, attempt);
    } catch (error: unknown) {
      const apiError = this.toApiError(error);
      if (apiError.status === 401 || apiError.status === 403 || apiError.status === 404 || apiError.status === 410) this.handleError(run.conversationId, apiError);
      else this.scheduleReconciliation(run, attempt, apiError);
    }
  }

  private applyEvent(event: RunEvent, cursor?: string): void {
    // Only the SSE `id` field identifies a persisted event.  Core heartbeats
    // are intentionally not persisted and do not carry a cursor.
    if (cursor && event.eventType !== 'heartbeat') this.lastEventIds.set(event.runId, cursor);
    if (event.eventType === 'run.snapshot') { this.replaceRun(event.conversationId, event.data.run); if (event.data.assistantMessage) this.upsertTurn(event.conversationId, this.fromMessage(event.data.assistantMessage)); }
    else if (event.eventType === 'run.status') { this.setRunState(event.conversationId, this.statusToState(event.data.status)); this.replaceConversationRun(event.conversationId, (run) => run ? { ...run, status: event.data.status, startedAt: event.data.startedAt, finishedAt: event.data.finishedAt } : run); if (this.isTerminal(event.data.status)) { this.clearReconnect(event.runId); void this.refresh(event.conversationId); } }
    else if (event.eventType === 'assistant.delta') this.appendAssistantDelta(event.conversationId, event.data.messageId, event.data.offset, event.data.text, event.runId);
    else if (event.eventType === 'assistant.snapshot') this.upsertTurn(event.conversationId, this.fromMessage(event.data.message));
    else if (event.eventType === 'run.error') { this.setRunState(event.conversationId, 'error'); this.replaceConversationRun(event.conversationId, (run) => run ? { ...run, status: 'failed', error: event.data } : run); this.setNotice(event.conversationId, event.data.message || 'Aura could not complete this response. Your message is still here.'); }
  }

  private async refresh(id: string): Promise<void> { try { const detail = await this.api.getConversation(id); const conversation = this.fromDetail(detail); this.replaceConversation(conversation); if (detail.currentRun) { this.setRunState(id, this.statusToState(detail.currentRun.status)); this.subscribe(detail.currentRun); } else if (conversation.retryableRun) { this.setRunState(id, this.statusToState(conversation.retryableRun.status)); this.setNotice(id, conversation.retryableRun.status === 'failed' ? 'Aura could not complete this response. Your message is still here.' : 'Generation stopped. Your partial response is still here.'); } } catch (error: unknown) { this.handleError(id, error); } }
  private async listAllConversationSummaries(): Promise<ReadonlyArray<ConversationSummary>> {
    const summaries: ConversationSummary[] = [];
    const seenConversations = new Set<string>();
    const seenCursors = new Set<string>();
    let cursor: string | undefined;

    while (true) {
      const page = await this.api.listConversations(cursor);
      for (const summary of page.items) {
        if (!seenConversations.has(summary.id)) {
          seenConversations.add(summary.id);
          summaries.push(summary);
        }
      }
      if (!page.nextCursor) break;
      if (seenCursors.has(page.nextCursor)) {
        this.conversationListWarning = true;
        break;
      }
      seenCursors.add(page.nextCursor);
      cursor = page.nextCursor;
    }
    return summaries;
  }
  private rejectSubmission(id: string, error: unknown): void {
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
    this.setNotice(id, apiError.message || 'We could not complete this request. Your message is still here.');
  }

  private handleError(id: string, error: unknown): void { this.setRunState(id, 'error'); this.setNotice(id, this.toApiError(error).message || 'We could not complete this request. Your message is still here.'); }

  private fromSummary(summary: ConversationSummary): Conversation {
    const raw = summary as ConversationSummary & { agent?: { profileId: string; revisionId: string; revision: number; displayName: string; status: 'active' | 'disabled'; newerRevisionAvailable?: boolean }; agentAssignments?: ReadonlyArray<{ id: string; agent: { profileId: string; revisionId: string; revision: number; displayName: string; status: 'active' | 'disabled'; newerRevisionAvailable?: boolean }; reason: ConversationAssignment['reason']; effectiveAfterMessageId: string | null; createdAt: string }> };
    const agent = raw.agent ? { profileId: raw.agent.profileId, revisionId: raw.agent.revisionId, revision: raw.agent.revision, displayName: raw.agent.displayName, status: raw.agent.status, newerRevisionAvailable: raw.agent.newerRevisionAvailable } : summary.agentRevisionId ? { profileId: summary.agentProfileId, revisionId: summary.agentRevisionId, revision: 1, displayName: 'Aura', status: 'active' as const } : null;
    const assignments = (raw.agentAssignments ?? []).map((item) => ({ id: item.id, agent: { profileId: item.agent.profileId, revisionId: item.agent.revisionId, revision: item.agent.revision, displayName: item.agent.displayName, status: item.agent.status, newerRevisionAvailable: item.agent.newerRevisionAvailable }, reason: item.reason, afterMessageId: item.effectiveAfterMessageId, changedAt: item.createdAt }));
    return { id: summary.id, title: summary.title, turns: [], updatedAt: Date.parse(summary.updatedAt), modelId: summary.modelId, version: summary.version, currentRun: summary.currentRun, retryableRun: null, agent, assignments, runs: summary.currentRun ? [summary.currentRun] : [] };
  }
  private fromDetail(detail: ConversationDetail): Conversation { const retryableRun = this.latestRetryableRun(detail); const base = this.fromSummary(detail); return { ...base, turns: detail.messages.map((message) => this.fromMessage(message)), currentRun: detail.currentRun, retryableRun, runs: [...detail.recentRuns, ...(detail.currentRun ? [detail.currentRun] : [])] }; }
  private latestRetryableRun(detail: ConversationDetail): Run | null { return [...detail.recentRuns, ...(detail.currentRun ? [detail.currentRun] : [])].filter((run) => this.isRetryable(run.status)).sort((left, right) => Date.parse(right.finishedAt ?? right.createdAt) - Date.parse(left.finishedAt ?? left.createdAt))[0] ?? null; }
  private fromMessage(message: Message): ConversationTurn { return { id: message.id, role: message.role, text: message.content, state: message.state === 'complete' ? undefined : message.state === 'failed' ? 'failed' : message.state === 'interrupted' ? 'interrupted' : 'partial', runId: message.runId }; }
  private find(id: string): Conversation | undefined { return this.conversations().find((conversation) => conversation.id === id); }
  private replaceConversation(conversation: Conversation): void { this.conversations.update((items) => items.map((item) => item.id === conversation.id ? { ...conversation, updatedAt: conversation.updatedAt || Date.now() } : item)); }
  private replaceRun(id: string, run: Run): void { this.replaceConversationRun(id, () => run); }
  private replaceConversationRun(id: string, update: (run: Run | null) => Run | null): void { const conversation = this.find(id); if (conversation) { const run = update(conversation.currentRun); const runs = run && !conversation.runs.some((item) => item.id === run.id) ? [...conversation.runs, run] : conversation.runs.map((item) => item.id === run?.id ? run : item).filter((item): item is Run => Boolean(item)); this.replaceConversation({ ...conversation, currentRun: run, runs, retryableRun: run && this.isRetryable(run.status) ? run : conversation.retryableRun }); } }
  private appendTurn(id: string, turn: ConversationTurn): void { const conversation = this.find(id); if (conversation) this.replaceConversation({ ...conversation, turns: [...conversation.turns, turn], updatedAt: Date.now(), title: conversation.turns.length === 0 ? this.makeTitle(turn.text) : conversation.title }); }
  private removeTurn(id: string, turnId: string): void { const conversation = this.find(id); if (conversation) this.replaceConversation({ ...conversation, turns: conversation.turns.filter((turn) => turn.id !== turnId) }); }
  private upsertTurn(id: string, turn: ConversationTurn): void { const conversation = this.find(id); if (!conversation) return; const exists = conversation.turns.some((item) => item.id === turn.id); this.replaceConversation({ ...conversation, turns: exists ? conversation.turns.map((item) => item.id === turn.id ? turn : item) : [...conversation.turns, turn], updatedAt: Date.now() }); }
  private appendAssistantDelta(id: string, messageId: string, offset: number, text: string, runId: string): void { const conversation = this.find(id); if (!conversation) return; const existing = conversation.turns.find((turn) => turn.id === messageId); const content = existing?.text ?? ''; const next = offset <= content.length ? `${content.slice(0, offset)}${text}` : `${content}${text}`; this.upsertTurn(id, { id: messageId, role: 'assistant', text: next, state: 'partial', runId }); }
  private setRunState(id: string, state: RunState): void { this.runStates.update((states) => ({ ...states, [id]: state })); }
  private setNotice(id: string, notice: string | null): void { this.notices.update((notices) => ({ ...notices, [id]: notice })); }
  private ensureDraftModel(): void { const modelId = this.defaultModelId(); const current = this.find(emptyDraft.id); if (current) { this.replaceConversation({ ...current, modelId }); if (!modelId) this.setNotice(emptyDraft.id, 'The configured default model is unavailable. Choose an available model to continue.'); } }
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
  private statusToState(status: Run['status'] | undefined): RunState { return status === 'queued' || status === 'running' || status === 'cancel_requested' ? 'working' : status === 'failed' ? 'error' : status === 'canceled' || status === 'interrupted' ? 'interrupted' : 'idle'; }
  private isRetryable(status: Run['status']): boolean { return status === 'canceled' || status === 'failed' || status === 'interrupted'; }
  private isTerminal(status: Run['status']): boolean { return status === 'completed' || this.isRetryable(status); }
  private key(): string { return typeof crypto !== 'undefined' && 'randomUUID' in crypto ? crypto.randomUUID() : `aura-${Date.now()}-${Math.random().toString(36).slice(2)}`; }
  private toApiError(error: unknown): ConversationApiError { return error && typeof error === 'object' && 'message' in error ? error as ConversationApiError : { message: 'We could not connect to Aura. Your draft is still here.', retryable: true }; }
  private makeTitle(text: string): string { return text.length > 30 ? `${text.slice(0, 30).trimEnd()}…` : text; }
}
