import { Injectable, computed, inject, signal } from '@angular/core';
import { AuraApprovalsApi } from './approvals-api';
import { ApprovalApiError, approvalSummaryFromDetail, collectApprovalHistory, decisionFailureNotice, emptyApprovalCollection, isTerminalApproval, isWorkflowApproval, isWorkflowApprovalDetail, shouldPreserveReviewedApproval, type ApprovalCollection, type ApprovalDetail, type ApprovalStatus, type ApprovalStream, type ApprovalStreamEvent, type ApprovalSummary, type ApprovalView, type ApprovalsApi } from './approval-models';

const retryDelaysMs = [500, 1_000, 2_000, 4_000, 8_000, 15_000] as const;
function statusOf(error: unknown): number | null { const status = (error as { readonly status?: unknown })?.status; return typeof status === 'number' ? status : null; }
function safeMessage(error: unknown, fallback: string): string { const status = statusOf(error); if (status === 401) return 'Your session has expired. Sign in again to view approvals.'; if (status === 403) return 'You do not have permission to view these approvals.'; if (status === 404) return 'That approval is no longer available.'; if (status === 409) return 'This approval changed elsewhere. Refresh before deciding again.'; if (status === 410) return 'The live approval cursor expired. Aura is catching up safely.'; return fallback; }

/** Apply the server decision without allowing a summary to erase detail's discriminant or proposal. */
function mergeDecisionIntoDetail(detail: ApprovalDetail, updated: ApprovalSummary): ApprovalDetail {
  const detailIsWorkflow = isWorkflowApprovalDetail(detail);
  const updateIsWorkflow = isWorkflowApproval(updated);
  if (detailIsWorkflow !== updateIsWorkflow) return detail;
  return {
    ...detail,
    status: updated.status,
    executionStatus: updated.executionStatus,
    version: updated.version,
    expiresAt: updated.expiresAt,
    updatedAt: updated.updatedAt,
    decidedAt: updated.decidedAt,
  };
}

@Injectable({ providedIn: 'root' })
export class ApprovalStore {
  private readonly api: ApprovalsApi = inject(AuraApprovalsApi);
  readonly collection = signal<ApprovalCollection>(emptyApprovalCollection());
  readonly pendingCollection = signal<ApprovalCollection>(emptyApprovalCollection());
  readonly selected = signal<ApprovalDetail | null>(null);
  readonly loading = signal(false);
  readonly loadingMore = signal(false);
  readonly detailLoading = signal(false);
  readonly detailError = signal<string | null>(null);
  readonly saving = signal(false);
  readonly decisionConfirmationApprovalIds = signal<ReadonlySet<string>>(new Set<string>());
  readonly decisionConfirmationRequired = computed(() => {
    const confirmationIds = this.decisionConfirmationApprovalIds();
    const selected = this.selected();
    return selected === null ? confirmationIds.size > 0 : confirmationIds.has(selected.id);
  });
  readonly notice = signal<string | null>(null);
  readonly errorStatus = signal<number | null>(null);
  readonly retryAvailable = computed(() => this.notice() !== null && this.errorStatus() !== 401 && this.errorStatus() !== 403);
  readonly connectionState = signal<'offline' | 'connecting' | 'reconnecting' | 'live' | 'unavailable'>('offline');
  readonly view = signal<ApprovalView>('pending');
  readonly loadedView = signal<ApprovalView | null>(null);
  /** Compatibility projection for conversation consumers. */
  readonly filter = signal<'all' | ApprovalStatus>('pending');
  readonly pendingCount = computed(() => this.pendingCollection().items.length);
  readonly pendingCountReconciled = signal(false);
  readonly pendingCountExact = computed(() => this.pendingCountReconciled() && this.pendingCollection().nextCursor === null);
  readonly resetEpoch = signal(0);
  private stream: ApprovalStream | null = null;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private started = false;
  private stopped = false;
  private retryAttempt = 0;
  private lastEventId: string | undefined;
  private sessionGeneration = 0;
  private listGeneration = 0;
  private pendingGeneration = 0;
  private streamGeneration = 0;
  private detailGeneration = 0;
  private detailKey = '';
  private mutationGeneration = 0;
  private activeMutation: { readonly generation: number; readonly session: number; readonly approvalId: string } | null = null;
  private reviewedId: string | null = null;
  private confirmationReconcile: { readonly id: string; readonly promise: Promise<boolean> } | null = null;
  private sessionResetHandler: ((status?: 401 | 403) => void) | null = null;

  async start(): Promise<void> { if (typeof window === 'undefined' || this.started) return; this.started = true; this.stopped = false; const ok = await this.refresh(); if (ok && !this.stopped) this.connect(); else if (!this.stopped) this.connectionState.set('unavailable'); }
  stop(): void { this.stopped = true; this.started = false; ++this.pendingGeneration; ++this.streamGeneration; this.stream?.close(); this.stream = null; if (this.reconnectTimer) clearTimeout(this.reconnectTimer); this.reconnectTimer = null; this.connectionState.set('offline'); }
  retryConnection(): void { void this.reconcileAndConnect(); }

  async refresh(): Promise<boolean> {
    const session = this.sessionGeneration; const generation = ++this.listGeneration; const view = this.view(); const pendingGeneration = ++this.pendingGeneration;
    this.loading.set(true); this.loadingMore.set(false); this.pendingCountReconciled.set(false); this.notice.set(null); this.errorStatus.set(null);
    try {
      const visiblePromise = view === 'pending' ? this.api.list(undefined, 'pending') : collectApprovalHistory((cursor) => this.api.list(cursor));
      const pendingPromise = view === 'pending' ? visiblePromise : this.api.list(undefined, 'pending');
      const [visibleResult, pendingResult] = await Promise.allSettled([visiblePromise, pendingPromise]);
      // Authorization failures belong to the captured accepted session, not to
      // the view/list generation. Reset the owner even if a newer view has
      // already superseded this refresh; only replaced sessions are ignored.
      if (visibleResult.status === 'rejected' && this.handleAuthorizationFailure(visibleResult.reason, session)) return false;
      if (pendingResult.status === 'rejected' && this.handleAuthorizationFailure(pendingResult.reason, session)) return false;
      if (session !== this.sessionGeneration || generation !== this.listGeneration || pendingGeneration !== this.pendingGeneration || view !== this.view()) return false;
      if (pendingResult.status === 'fulfilled') { this.pendingCollection.set({ ...pendingResult.value, pendingCount: pendingResult.value.items.length }); this.pendingCountReconciled.set(true); }
      else { this.pendingCollection.set(emptyApprovalCollection()); }
      if (visibleResult.status === 'rejected') { if (this.handleAuthorizationFailure(visibleResult.reason, session)) return false; this.collection.set(emptyApprovalCollection()); this.loadedView.set(null); this.setError(visibleResult.reason, 'Approvals are temporarily unavailable.'); return false; }
      const visiblePage = visibleResult.value;
      const visibleItems = view === 'pending' ? visiblePage.items : visiblePage.items.filter((item) => isTerminalApproval(item));
      this.collection.set({ ...visiblePage, items: visibleItems, pendingCount: visibleItems.filter((item) => item.status === 'pending').length }); this.loadedView.set(view);
      if (pendingResult.status === 'rejected') { if (this.handleAuthorizationFailure(pendingResult.reason, session)) return false; this.setError(pendingResult.reason, 'Pending approval count is temporarily unavailable.'); return false; }
      return true;
    } catch (error: unknown) {
      if (this.handleAuthorizationFailure(error, session)) return false;
      if (session === this.sessionGeneration && generation === this.listGeneration && pendingGeneration === this.pendingGeneration && view === this.view()) { this.collection.set(emptyApprovalCollection()); this.loadedView.set(null); this.setError(error, 'Approvals are temporarily unavailable.'); }
      return false;
    } finally { if (session === this.sessionGeneration && generation === this.listGeneration) this.loading.set(false); }
  }

  /** Reconcile the visible collection and, when present, the currently reviewed detail. */
  async retry(): Promise<boolean> {
    const session = this.sessionGeneration;
    const detailId = this.detailKey;
    const detailGeneration = this.detailGeneration;
    const confirmationIds = [...this.decisionConfirmationApprovalIds()];
    const refreshed = await this.refresh();
    if (!detailId) {
      const confirmationResults = await Promise.all(confirmationIds.map((id) => this.reconcileBlockedDecision(id)));
      return refreshed && confirmationResults.every(Boolean);
    }
    if (session !== this.sessionGeneration || detailGeneration !== this.detailGeneration || this.detailKey !== detailId) return false;
    const detail = await this.inspect(detailId);
    if (session !== this.sessionGeneration || this.detailKey !== detailId) return false;
    if (confirmationIds.includes(detailId) && detail !== null) { this.removeDecisionConfirmation(detailId); this.reviewedId = null; }
    return refreshed && detail !== null;
  }

  async loadMore(): Promise<void> {
    const current = this.collection(); if (!current.nextCursor || this.loading()) return;
    const session = this.sessionGeneration; const generation = this.listGeneration; const view = this.view(); this.loading.set(true); this.loadingMore.set(true);
    try {
      const page = view === 'pending' ? await this.api.list(current.nextCursor, 'pending') : await collectApprovalHistory((cursor) => this.api.list(cursor), current.nextCursor);
      if (session !== this.sessionGeneration || generation !== this.listGeneration || view !== this.view()) return;
      const items = view === 'pending' ? page.items : page.items.filter((item) => isTerminalApproval(item));
      const nextCollection = { items: [...current.items, ...items], nextCursor: page.nextCursor, pendingCount: view === 'pending' ? [...current.items, ...items].filter((item) => item.status === 'pending').length : 0 };
      this.collection.set(nextCollection);
      if (view === 'pending') { this.pendingCollection.set(nextCollection); this.pendingCountReconciled.set(true); }
    } catch (error: unknown) { if (this.handleAuthorizationFailure(error, session)) return; if (session === this.sessionGeneration && generation === this.listGeneration && view === this.view()) this.setError(error, 'More approvals could not be loaded.'); }
    finally { if (session === this.sessionGeneration && generation === this.listGeneration) { this.loading.set(false); this.loadingMore.set(false); } }
  }

  async inspect(id: string): Promise<ApprovalDetail | null> {
    const routeChanged = this.detailKey !== id;
    if (routeChanged && this.activeMutation) { this.addDecisionConfirmation(this.activeMutation.approvalId); ++this.mutationGeneration; this.activeMutation = null; this.saving.set(false); }
    // An approval event can reconcile the same detail while its decision
    // buttons are still in the DOM. Keep the last owner-reviewed snapshot
    // until the replacement arrives so a real click cannot be dropped merely
    // because reviewedId was cleared between pointerdown and click.
    const preserveReviewedDetail = shouldPreserveReviewedApproval(routeChanged, this.reviewedId, this.selected()?.id ?? null, id);
    const session = this.sessionGeneration; const generation = ++this.detailGeneration; this.detailKey = id; if (!preserveReviewedDetail) { this.reviewedId = null; this.selected.set(null); } this.detailLoading.set(true); this.detailError.set(null); this.notice.set(null); if (!this.activeMutation) this.saving.set(false);
    try { const detail = await this.api.get(id); if (session !== this.sessionGeneration || generation !== this.detailGeneration || this.detailKey !== id) return null; this.selected.set(detail); this.reviewedId = id; return detail; }
    catch (error: unknown) { if (this.handleAuthorizationFailure(error, session)) return null; if (session === this.sessionGeneration && generation === this.detailGeneration && this.detailKey === id) { const message = safeMessage(error, 'That approval could not be loaded.'); const status = statusOf(error); this.errorStatus.set(status); this.detailError.set(message); } return null; }
    finally { if (session === this.sessionGeneration && generation === this.detailGeneration && this.detailKey === id) this.detailLoading.set(false); }
  }

  clearDetail(): void { if (this.activeMutation) { this.addDecisionConfirmation(this.activeMutation.approvalId); ++this.mutationGeneration; } ++this.detailGeneration; this.detailKey = ''; this.reviewedId = null; this.activeMutation = null; this.selected.set(null); this.detailLoading.set(false); this.detailError.set(null); this.saving.set(false); }
  decisionConfirmationRequiredFor(id: string): boolean { return this.decisionConfirmationApprovalIds().has(id); }
  canDecide(approval: ApprovalSummary): boolean { return !this.decisionConfirmationRequiredFor(approval.id) && approval.status === 'pending' && this.reviewedId === approval.id && this.selected()?.id === approval.id && this.selected()?.status === 'pending'; }
  async approve(approval: ApprovalSummary): Promise<boolean> { return this.decide(approval, 'approve'); }
  async reject(approval: ApprovalSummary): Promise<boolean> { return this.decide(approval, 'reject'); }
  async setView(view: ApprovalView): Promise<void> { if (this.view() === view) return; this.view.set(view); this.filter.set(view === 'pending' ? 'pending' : 'all'); this.loadedView.set(null); this.collection.set(emptyApprovalCollection()); await this.refresh(); }
  async setFilter(filter: 'all' | ApprovalStatus): Promise<void> { this.filter.set(filter); await this.setView(filter === 'pending' ? 'pending' : 'history'); }
  clearNotice(): void { this.notice.set(null); this.errorStatus.set(null); }
  registerSessionResetHandler(handler: ((status?: 401 | 403) => void) | null): void { this.sessionResetHandler = handler; }
  resumeAfterAcceptedSession(): void { void this.start(); }
  forConversation(conversationId: string): ReadonlyArray<ApprovalSummary> { return this.pendingCollection().items.filter((item) => item.conversationId === conversationId); }
  statusLabel(status: ApprovalStatus): string { return status[0].toUpperCase() + status.slice(1); }
  executionLabel(status: ApprovalSummary['executionStatus']): string { return status === 'not_started' ? 'Waiting to run' : status === 'queued' ? 'Queued' : status === 'executing' ? 'Executing' : status === 'completed' ? 'Completed' : status === 'denied' ? 'Denied' : 'Failed'; }

  resetSessionState(): void { ++this.sessionGeneration; ++this.listGeneration; ++this.pendingGeneration; ++this.detailGeneration; ++this.mutationGeneration; ++this.streamGeneration; this.confirmationReconcile = null; this.decisionConfirmationApprovalIds.set(new Set()); this.activeMutation = null; this.detailKey = ''; this.reviewedId = null; this.api.clearSession?.(); this.stop(); this.collection.set(emptyApprovalCollection()); this.pendingCollection.set(emptyApprovalCollection()); this.pendingCountReconciled.set(false); this.loadedView.set(null); this.clearDetail(); this.loading.set(false); this.loadingMore.set(false); this.saving.set(false); this.notice.set(null); this.errorStatus.set(null); this.lastEventId = undefined; this.retryAttempt = 0; this.resetEpoch.update((value) => value + 1); }

  private async decide(approval: ApprovalSummary, action: 'approve' | 'reject'): Promise<boolean> {
    if (approval.status !== 'pending') return false;
    if (this.decisionConfirmationRequiredFor(approval.id)) { void this.reconcileBlockedDecision(approval.id); return false; }
    if (!this.canDecide(approval)) return false;
    const session = this.sessionGeneration; const listGeneration = this.listGeneration; const detailGeneration = this.detailGeneration; const detailKey = this.detailKey; const mutation = ++this.mutationGeneration;
    this.activeMutation = { generation: mutation, session, approvalId: approval.id }; this.saving.set(true); this.notice.set(null); this.errorStatus.set(null);
    const sameContext = (): boolean => session === this.sessionGeneration && mutation === this.mutationGeneration && detailGeneration === this.detailGeneration && detailKey === this.detailKey;
    try {
      const updated = action === 'approve' ? await this.api.approve(approval.id, approval.version) : await this.api.reject(approval.id, approval.version);
      if (session !== this.sessionGeneration || mutation !== this.mutationGeneration) return false;
      if (listGeneration === this.listGeneration) this.collection.update((current) => ({ ...current, items: current.items.map((item) => item.id === updated.id ? updated : item), pendingCount: current.items.filter((item) => item.id !== updated.id && item.status === 'pending').length }));
      this.pendingCollection.update((current) => ({ ...current, items: current.items.filter((item) => item.id !== updated.id), pendingCount: Math.max(0, current.pendingCount - 1) }));
      if (sameContext() && this.selected()?.id === updated.id) { this.selected.update((detail) => detail ? mergeDecisionIntoDetail(detail, updated) : detail); this.reviewedId = updated.id; }
      const reconciled = await this.refresh(); return reconciled && session === this.sessionGeneration && mutation === this.mutationGeneration;
    } catch (error: unknown) {
      const status = statusOf(error);
      if (this.handleAuthorizationFailure(error, session)) return false;
      if (status === 409 && session === this.sessionGeneration && mutation === this.mutationGeneration && detailGeneration === this.detailGeneration && detailKey === this.detailKey) {
        const reconciled = await this.refresh();
        let detailReconciled = !detailKey;
        const routeStillCurrent = session === this.sessionGeneration && mutation === this.mutationGeneration && detailGeneration === this.detailGeneration && detailKey === this.detailKey;
        if (reconciled && routeStillCurrent && detailKey) detailReconciled = (await this.inspect(approval.id)) !== null;
        // A list-origin mutation stays publishable only on the list surface. A
        // detail-origin mutation may advance detailGeneration while reloading
        // that same approval, but must never publish into a different route.
        const surfaceStillCurrent = session === this.sessionGeneration && mutation === this.mutationGeneration && (detailKey ? this.detailKey === approval.id : detailGeneration === this.detailGeneration && this.detailKey === '');
        if (surfaceStillCurrent) {
          if (reconciled && detailReconciled) this.removeDecisionConfirmation(approval.id);
          else this.addDecisionConfirmation(approval.id);
          this.errorStatus.set(reconciled && detailReconciled ? 409 : status);
          this.notice.set(decisionFailureNotice(reconciled && detailReconciled ? 409 : status));
        }
      } else if (sameContext()) {
        if (status === 401 || status === 403) this.setError(error, 'Your session could not be confirmed.');
        else {
          this.addDecisionConfirmation(approval.id);
          this.errorStatus.set(status);
          this.notice.set(decisionFailureNotice(status));
        }
      }
      return false;
    }
    finally { if (this.activeMutation?.generation === mutation && this.activeMutation.session === session) { this.activeMutation = null; this.saving.set(false); } }
  }

  private connect(): void {
    if (this.stopped || this.stream) return;
    const session = this.sessionGeneration; const streamGeneration = ++this.streamGeneration;
    this.connectionState.set(this.retryAttempt > 0 ? 'reconnecting' : 'connecting');
    let streamActive = true;
    let eventQueue = Promise.resolve();
    const reconcileEvent = async (event: ApprovalStreamEvent): Promise<void> => {
      if (!streamActive || session !== this.sessionGeneration || streamGeneration !== this.streamGeneration || this.stopped || event.eventType === 'heartbeat') return;
      const refreshed = await this.refresh();
      if (!refreshed || session !== this.sessionGeneration || streamGeneration !== this.streamGeneration || this.stopped) { streamActive = false; this.stream?.close(); this.stream = null; await this.handleStreamError(new ApprovalApiError(null, 'Approval reconciliation failed.', true), session, streamGeneration); return; }
      if (this.selected()?.id === event.approvalId && await this.inspect(event.approvalId) === null) { streamActive = false; this.stream?.close(); this.stream = null; await this.handleStreamError(new ApprovalApiError(null, 'Approval detail reconciliation failed.', true), session, streamGeneration); return; }
      if (!streamActive || session !== this.sessionGeneration || streamGeneration !== this.streamGeneration || this.stopped) return;
      this.lastEventId = event.eventId;
      this.retryAttempt = 0;
      this.connectionState.set('live');
    };
    this.stream = this.api.stream(this.lastEventId, (event) => { if (!streamActive) return; eventQueue = eventQueue.then(() => reconcileEvent(event)).catch(() => undefined); }, (error) => { if (!streamActive) return; streamActive = false; if (session === this.sessionGeneration && streamGeneration === this.streamGeneration) void this.handleStreamError(error, session, streamGeneration); });
  }

  private async reconcileAndConnect(): Promise<void> { this.stopped = false; this.started = true; this.retryAttempt = 0; this.connectionState.set('connecting'); const session = this.sessionGeneration; const ok = await this.refresh(); if (session !== this.sessionGeneration || this.stopped) return; if (!ok) { this.connectionState.set('unavailable'); return; } this.connect(); }
  private async handleStreamError(error: ApprovalApiError, session: number, streamGeneration: number): Promise<void> {
    if (session !== this.sessionGeneration || streamGeneration !== this.streamGeneration) return;
    this.stream = null; if (this.stopped) return;
    if (!error.retryable) { this.connectionState.set('unavailable'); this.setError(error, 'Live approvals are unavailable.'); return; }
    this.connectionState.set('reconnecting');
    if (error.status === 410) { this.lastEventId = undefined; const ok = await this.refresh(); if (session !== this.sessionGeneration || streamGeneration !== this.streamGeneration || this.stopped) return; if (!ok) { this.connectionState.set('unavailable'); return; } }
    if (this.retryAttempt >= retryDelaysMs.length) { this.connectionState.set('unavailable'); this.notice.set('Live approvals stopped after several attempts. Retry when you are ready.'); this.errorStatus.set(null); return; }
    const delay = retryDelaysMs[this.retryAttempt]; this.retryAttempt += 1; this.reconnectTimer = setTimeout(() => { this.reconnectTimer = null; if (session === this.sessionGeneration && streamGeneration === this.streamGeneration && !this.stopped) this.connect(); }, delay);
  }
  private selectedStatus(): ApprovalStatus | undefined { return this.view() === 'pending' ? 'pending' : undefined; }
  private addDecisionConfirmation(id: string): void { this.decisionConfirmationApprovalIds.update((ids) => ids.has(id) ? ids : new Set([...ids, id])); }
  private removeDecisionConfirmation(id: string): void { this.decisionConfirmationApprovalIds.update((ids) => { if (!ids.has(id)) return ids; const next = new Set(ids); next.delete(id); return next; }); }
  private reconcileBlockedDecision(id: string): Promise<boolean> {
    if (this.confirmationReconcile?.id === id) return this.confirmationReconcile.promise;
    const session = this.sessionGeneration;
    const promise = (async (): Promise<boolean> => {
      const [listResult, detailResult] = await Promise.allSettled([this.refresh(), this.api.get(id)]);
      if (session !== this.sessionGeneration || !this.decisionConfirmationRequiredFor(id)) return false;
      if (detailResult.status === 'rejected') {
        if (this.handleAuthorizationFailure(detailResult.reason, session)) return false;
        this.errorStatus.set(statusOf(detailResult.reason));
        this.notice.set('The approval status could not be confirmed. Refresh to try again.');
        return false;
      }
      const detail = detailResult.value;
      if (this.selected()?.id === id) this.selected.set(detail);
      this.reviewedId = null;
      const summary = approvalSummaryFromDetail(detail);
      this.pendingCollection.update((current) => detail.status === 'pending' ? { ...current, items: current.items.some((item) => item.id === id) ? current.items.map((item) => item.id === id ? summary : item) : [...current.items, summary], pendingCount: current.items.some((item) => item.id === id) ? current.pendingCount : current.pendingCount + 1 } : { ...current, items: current.items.filter((item) => item.id !== id), pendingCount: Math.max(0, current.pendingCount - (current.items.some((item) => item.id === id) ? 1 : 0)) });
      this.removeDecisionConfirmation(id);
      return listResult.status === 'fulfilled' && detailResult.value.status !== 'pending';
    })();
    this.confirmationReconcile = { id, promise };
    void promise.finally(() => { if (this.confirmationReconcile?.promise === promise) this.confirmationReconcile = null; });
    return promise;
  }
  private handleAuthorizationFailure(error: unknown, session: number): boolean { const status = statusOf(error); if (session !== this.sessionGeneration || (status !== 401 && status !== 403)) return false; this.setError(error, 'Your session could not be confirmed.'); return true; }
  private setError(error: unknown, fallback: string): void { const status = statusOf(error); if (status === 401 || status === 403) { const message = status === 401 ? 'Your session has expired. Sign in again to view approvals.' : 'You do not have permission to view approvals.'; this.resetSessionState(); this.sessionResetHandler?.(status); this.errorStatus.set(status); this.notice.set(message); return; } this.errorStatus.set(status); this.notice.set(safeMessage(error, fallback)); }
}
