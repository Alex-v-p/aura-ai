import { Injectable, computed, inject, signal } from '@angular/core';
import type { AgentMemoryPolicy, MemoryCandidateDetail, MemoryCandidateEdit, MemoryCandidateSummary, MemoryDetail, MemoryModelConfiguration, MemoryModelInventory, MemoryPage, MemoryReindexStatus, MemoryScope } from '@aura/aura-api-client';
import { AuraMemoryApi } from './memory-api';
import { isActionableReviewCandidate, isMissingMemoryConfiguration, loadMemorySettings, MEMORY_API, normalizeMemoryFilters, saveMemorySettings, type MemoryApi, type MemoryFilters } from './memory-models';

@Injectable({ providedIn: 'root' })
export class MemoryStore {
  private readonly api: MemoryApi = inject(MEMORY_API, { optional: true }) ?? inject(AuraMemoryApi);
  readonly memories = signal<MemoryPage>({ items: [], nextCursor: null });
  readonly selected = signal<MemoryDetail | null>(null);
  readonly detailLoading = signal(false);
  readonly detailError = signal<string | null>(null);
  readonly candidates = signal<ReadonlyArray<MemoryCandidateSummary>>([]);
  readonly candidateNextCursor = signal<string | null>(null);
  readonly candidateLoadingMore = signal(false);
  readonly candidateListLoadFailed = signal(false);
  readonly candidateQueueGeneration = signal(0);
  readonly candidate = signal<MemoryCandidateDetail | null>(null);
  readonly candidateDetailLoading = signal(false);
  readonly candidateDetailError = signal<string | null>(null);
  readonly modelInventory = signal<MemoryModelInventory | null>(null);
  readonly modelConfiguration = signal<MemoryModelConfiguration | null>(null);
  /** True only when Core explicitly reports that the owner has not configured memory models yet. */
  readonly modelConfigurationMissing = signal(false);
  readonly reindex = signal<MemoryReindexStatus | null>(null);
  readonly policies = signal<ReadonlyArray<AgentMemoryPolicy>>([]);
  readonly attachedPolicyRevisionId = signal<string | null>(null);
  readonly agentVersion = signal(0);
  // The browser's “All scopes” view is explicit: omission is the safer
  // user-only contract default, so the product must send `all` deliberately.
  readonly filters = signal<MemoryFilters>({ scopeType: 'all' });
  readonly loading = signal(false);
  readonly saving = signal(false);
  readonly notice = signal<string | null>(null);
  readonly createOutcome = signal<'idle' | 'pending' | 'confirmed' | 'failed' | 'unconfirmed'>('idle');
  readonly policiesLoaded = signal(false);
  readonly resetEpoch = signal(0);
  readonly hasMore = computed(() => this.memories().nextCursor !== null);
  private loadGeneration = 0;
  private detailGeneration = 0;
  private detailKey = '';
  private candidateListGeneration = 0;
  private candidateDetailGeneration = 0;
  private policyGeneration = 0;
  readonly loadedPolicyAgentId = signal<string | null>(null);
  private requestGeneration = 0;
  private sessionResetHandler: ((status?: 401 | 403) => void) | null = null;

  async load(filters: MemoryFilters = this.filters()): Promise<void> {
    const sessionGeneration = this.requestGeneration;
    const generation = ++this.loadGeneration;
    this.filters.set(normalizeMemoryFilters(filters)); this.loading.set(true); this.notice.set(null);
    try { const page = await this.api.listMemories(undefined, this.filters()); if (sessionGeneration === this.requestGeneration && generation === this.loadGeneration) this.memories.set(page); } catch (error: unknown) { if (sessionGeneration === this.requestGeneration && generation === this.loadGeneration) { this.notice.set(message(error, 'We could not load your memories.')); this.handleAuthFailure(error); } }
    finally { if (sessionGeneration === this.requestGeneration && generation === this.loadGeneration) this.loading.set(false); }
  }
  /** Resolve only the setup status for Records without turning provider errors into a page failure. */
  async loadConfigurationStatus(): Promise<void> {
    const sessionGeneration = this.requestGeneration;
    try {
      const configuration = await this.api.getModelConfiguration();
      if (sessionGeneration !== this.requestGeneration) return;
      this.modelConfiguration.set(configuration);
      this.modelConfigurationMissing.set(false);
    } catch (error: unknown) {
      if (sessionGeneration !== this.requestGeneration) return;
      if (isMissingMemoryConfiguration(error)) this.modelConfigurationMissing.set(true);
      else this.handleAuthFailure(error);
    }
  }
  async loadMore(): Promise<void> { const cursor = this.memories().nextCursor; if (!cursor || this.loading()) return; const sessionGeneration = this.requestGeneration; const generation = this.loadGeneration; const filters = this.filters(); this.loading.set(true); try { const page = await this.api.listMemories(cursor, filters); if (sessionGeneration === this.requestGeneration && generation === this.loadGeneration && filters === this.filters()) this.memories.update((current) => ({ items: [...current.items, ...page.items], nextCursor: page.nextCursor })); } catch (error: unknown) { if (sessionGeneration === this.requestGeneration && generation === this.loadGeneration) { this.notice.set(message(error, 'We could not load more memories.')); this.handleAuthFailure(error); } } finally { if (sessionGeneration === this.requestGeneration && generation === this.loadGeneration) this.loading.set(false); } }
  async loadDetail(id: string, scope?: MemoryScope): Promise<MemoryDetail | null> {
    const sessionGeneration = this.requestGeneration;
    const generation = ++this.detailGeneration;
    const key = `${id}:${scope?.type ?? ''}:${scope?.type === 'agent' ? scope.agentProfileId : ''}`;
    this.detailKey = key;
    this.detailLoading.set(true);
    this.detailError.set(null);
    // Do not leave the previous principal's or record's detail visible while
    // a route-backed request is in flight. This is both clearer on a direct
    // deep link and prevents a stale record from appearing under a new scope.
    this.selected.set(null);
    this.notice.set(null);
    // A direct /memory/:id visit has no list-row context yet. Resolve the
    // owner-authorized scope from the loaded summary when possible, while
    // preserving an explicit agent scope supplied by a scoped link.
    const resolvedScope = scope ?? this.memories().items.find((item) => item.id === id)?.scope;
    try {
      const detail = await this.api.getMemory(id, resolvedScope);
      if (sessionGeneration !== this.requestGeneration || generation !== this.detailGeneration || key !== this.detailKey) return null;
      this.selected.set(detail);
      this.detailLoading.set(false);
      return detail;
    } catch (error: unknown) {
      if (sessionGeneration !== this.requestGeneration || generation !== this.detailGeneration || key !== this.detailKey) return null;
      this.detailLoading.set(false);
      this.detailError.set(message(error, 'We could not load that memory.'));
      this.notice.set(message(error, 'We could not load that memory.'));
      this.handleAuthFailure(error);
      return null;
    }
  }
  /** Invalidate route-owned detail when the collection is shown again. */
  clearDetail(): void { ++this.detailGeneration; this.detailKey = ''; this.selected.set(null); this.detailLoading.set(false); this.detailError.set(null); this.notice.set(null); }
  async create(input: Parameters<MemoryApi['createMemory']>[0]): Promise<boolean> {
    const sessionGeneration = this.requestGeneration;
    this.createOutcome.set('pending');
    this.saving.set(true);
    this.notice.set(null);
    try {
      const detail = await this.api.createMemory(input);
      if (sessionGeneration !== this.requestGeneration) return false;
      this.createOutcome.set('confirmed');
      this.selected.set(detail);
      void this.load(this.filters());
      return true;
    } catch (error: unknown) {
      if (sessionGeneration !== this.requestGeneration) return false;
      this.createOutcome.set(hasKnownStatus(error) ? 'failed' : 'unconfirmed');
      this.notice.set(hasKnownStatus(error) ? message(error, 'We could not save that memory. Your draft is still here.') : 'We could not confirm whether the memory was created. Your draft is still here; retrying uses the same safe request.');
      this.handleAuthFailure(error);
      return false;
    } finally {
      if (sessionGeneration === this.requestGeneration) this.saving.set(false);
    }
  }
  cancelCreateDraft(): void { this.api.cancelCreateDraft?.(); this.createOutcome.set('idle'); }
  async correct(id: string, input: Parameters<MemoryApi['correctMemory']>[2]): Promise<boolean> { const current = this.selected(); if (!current) return false; const sessionGeneration = this.requestGeneration; const detailGeneration = this.detailGeneration; const detailKey = this.detailKey; const isCurrent = () => this.isCurrentDetail(detailGeneration, detailKey, id); return this.mutate(() => this.api.correctMemory(id, current.scope, input).then((detail) => { if (!isCurrent()) return; this.selected.set(detail); this.replaceSummary(detail); }), sessionGeneration, isCurrent); }
  async setStatus(status: Parameters<MemoryApi['updateStatus']>[1], relatedMemoryId?: string | null): Promise<boolean> { const current = this.selected(); if (!current) return false; const sessionGeneration = this.requestGeneration; const detailGeneration = this.detailGeneration; const detailKey = this.detailKey; const isCurrent = () => this.isCurrentDetail(detailGeneration, detailKey, current.id); return this.mutate(() => this.api.updateStatus(current.id, status, current.version, relatedMemoryId, current.scope).then((detail) => { if (!isCurrent()) return; this.selected.set(detail); this.replaceSummary(detail); }), sessionGeneration, isCurrent); }
  async setPinned(pinned: boolean): Promise<boolean> { const current = this.selected(); if (!current) return false; const sessionGeneration = this.requestGeneration; const detailGeneration = this.detailGeneration; const detailKey = this.detailKey; const isCurrent = () => this.isCurrentDetail(detailGeneration, detailKey, current.id); return this.mutate(() => this.api.updatePin(current.id, pinned, current.version, current.scope).then((detail) => { if (!isCurrent()) return; this.selected.set(detail); this.replaceSummary(detail); }), sessionGeneration, isCurrent); }
  async purgeSelected(confirmation: string): Promise<boolean> { const current = this.selected(); if (!current || confirmation !== 'PURGE MEMORY') return false; const sessionGeneration = this.requestGeneration; const detailGeneration = this.detailGeneration; const detailKey = this.detailKey; const isCurrent = () => this.isCurrentDetail(detailGeneration, detailKey, current.id); return this.mutate(() => this.api.purge(current.id, current.scope, confirmation, current.version).then(() => { if (!isCurrent()) return; this.selected.set(null); this.memories.update((page) => ({ ...page, items: page.items.filter((item) => item.id !== current.id) })); }), sessionGeneration, isCurrent); }
  async loadCandidates(state: Parameters<MemoryApi['listCandidates']>[1] = 'review'): Promise<void> { const sessionGeneration = this.requestGeneration; const generation = ++this.candidateListGeneration; this.candidateQueueGeneration.update((value) => value + 1); this.candidates.set([]); this.candidateNextCursor.set(null); this.clearCandidateDetail(); this.candidateListLoadFailed.set(false); this.loading.set(true); this.notice.set(null); try { const page = await this.api.listCandidates(undefined, state); if (sessionGeneration !== this.requestGeneration || generation !== this.candidateListGeneration) return; this.candidates.set(actionableCandidates(page.items)); this.candidateNextCursor.set(page.nextCursor); } catch (error: unknown) { if (sessionGeneration === this.requestGeneration && generation === this.candidateListGeneration) { this.notice.set(message(error, 'We could not load review candidates.')); this.handleAuthFailure(error); this.candidateListLoadFailed.set(true); } } finally { if (sessionGeneration === this.requestGeneration && generation === this.candidateListGeneration) this.loading.set(false); } }
  async loadMoreCandidates(state: Parameters<MemoryApi['listCandidates']>[1] = 'review'): Promise<void> { const cursor = this.candidateNextCursor(); if (!cursor || this.candidateLoadingMore()) return; const sessionGeneration = this.requestGeneration; const generation = this.candidateListGeneration; this.candidateLoadingMore.set(true); try { const page = await this.api.listCandidates(cursor, state); if (sessionGeneration !== this.requestGeneration || generation !== this.candidateListGeneration) return; this.candidates.update((items) => [...items, ...actionableCandidates(page.items)]); this.candidateNextCursor.set(page.nextCursor); } catch (error: unknown) { if (sessionGeneration === this.requestGeneration && generation === this.candidateListGeneration) { this.notice.set(message(error, 'We could not load more review candidates.')); this.handleAuthFailure(error); } } finally { if (sessionGeneration === this.requestGeneration && generation === this.candidateListGeneration) this.candidateLoadingMore.set(false); } }
  async loadCandidate(id: string): Promise<void> { const sessionGeneration = this.requestGeneration; const generation = ++this.candidateDetailGeneration; this.candidate.set(null); this.candidateDetailLoading.set(true); this.candidateDetailError.set(null); this.notice.set(null); try { const candidate = await this.api.getCandidate(id); if (sessionGeneration !== this.requestGeneration || generation !== this.candidateDetailGeneration) return; this.candidate.set(candidate); this.candidateDetailLoading.set(false); } catch (error: unknown) { if (sessionGeneration === this.requestGeneration && generation === this.candidateDetailGeneration) { this.candidateDetailLoading.set(false); this.candidateDetailError.set(message(error, 'We could not load that candidate.')); this.notice.set(message(error, 'We could not load that candidate.')); this.handleAuthFailure(error); } } }
  clearCandidateDetail(): void { ++this.candidateDetailGeneration; this.candidate.set(null); this.candidateDetailLoading.set(false); this.candidateDetailError.set(null); this.notice.set(null); }
  async approveCandidate(edit?: MemoryCandidateEdit): Promise<boolean> { const current = this.candidate(); if (!current || current.state !== 'review' || !isActionableReviewCandidate(current)) return false; const sessionGeneration = this.requestGeneration; const candidateGeneration = this.candidateDetailGeneration; const candidateId = current.id; return this.mutate(() => this.api.approveCandidate(candidateId, current.version, edit).then((receipt) => { if (sessionGeneration !== this.requestGeneration || candidateGeneration !== this.candidateDetailGeneration || this.candidate()?.id !== candidateId) return; this.candidate.set(receipt.candidate); this.candidates.update((items) => items.filter((item) => item.id !== receipt.candidate.id)); }), sessionGeneration); }
  async rejectCandidate(reason: string): Promise<boolean> { const current = this.candidate(); if (!current || current.state !== 'review' || !isActionableReviewCandidate(current) || !reason.trim()) return false; const sessionGeneration = this.requestGeneration; const candidateGeneration = this.candidateDetailGeneration; const candidateId = current.id; return this.mutate(() => this.api.rejectCandidate(candidateId, current.version, reason.trim()).then((receipt) => { if (sessionGeneration !== this.requestGeneration || candidateGeneration !== this.candidateDetailGeneration || this.candidate()?.id !== candidateId) return; this.candidate.set(receipt.candidate); this.candidates.update((items) => items.filter((item) => item.id !== receipt.candidate.id)); }), sessionGeneration); }
  async loadSettings(): Promise<void> {
    const sessionGeneration = this.requestGeneration;
    this.loading.set(true);
    this.notice.set(null);
    this.modelConfigurationMissing.set(false);
    const result = await loadMemorySettings(this.api);
    if (sessionGeneration !== this.requestGeneration) return;
    const failures = result.errors.map(({ key, reason }) => message(reason, key === 'inventory' ? 'Model inventory is unavailable.' : key === 'configuration' ? 'Model configuration is unavailable.' : 'Reindex status is unavailable.'));
    if (result.inventory) this.modelInventory.set(result.inventory);
    if (result.configuration) this.modelConfiguration.set(result.configuration);
    else if (result.configurationMissing) this.modelConfigurationMissing.set(true);
    if (result.reindex) this.reindex.set(result.reindex);
    const authFailure = result.errors.find(({ reason }) => isAuthFailure(reason));
    if (authFailure) { this.handleAuthFailure(authFailure.reason); return; }
    if (failures.length > 0) this.notice.set(Array.from(new Set(failures)).join(' '));
    this.loading.set(false);
  }
  async saveSettings(extractionModelId: string, embeddingModelId: string): Promise<boolean> {
    const current = this.modelConfiguration();
    if (!extractionModelId || !embeddingModelId) return false;
    const sessionGeneration = this.requestGeneration;
    return this.mutate(() => saveMemorySettings(this.api, extractionModelId, embeddingModelId, current).then((configuration) => {
      if (sessionGeneration !== this.requestGeneration) return;
      this.modelConfiguration.set(configuration);
      this.modelConfigurationMissing.set(false);
      return this.api.getReindexStatus();
    }).then((status) => { if (sessionGeneration === this.requestGeneration && status) this.reindex.set(status); }), sessionGeneration);
  }
  async resumeReindex(): Promise<boolean> { const generationId = this.reindex()?.replacementGeneration?.id; if (!generationId) return false; const sessionGeneration = this.requestGeneration; return this.mutate(() => this.api.resumeReindex(generationId).then((status) => { if (sessionGeneration === this.requestGeneration) this.reindex.set(status); }), sessionGeneration); }
  async loadPolicies(agentProfileId: string): Promise<void> { const sessionGeneration = this.requestGeneration; const generation = ++this.policyGeneration; this.policies.set([]); this.attachedPolicyRevisionId.set(null); this.agentVersion.set(0); this.loadedPolicyAgentId.set(null); this.policiesLoaded.set(false); this.loading.set(true); this.notice.set(null); try { const result = await this.api.listAgentPolicies(agentProfileId); if (sessionGeneration !== this.requestGeneration || generation !== this.policyGeneration) return; this.policies.set(result.items); this.attachedPolicyRevisionId.set(result.attachedPolicyRevisionId); this.agentVersion.set(result.agentVersion); this.loadedPolicyAgentId.set(agentProfileId); this.policiesLoaded.set(true); } catch (error: unknown) { if (sessionGeneration === this.requestGeneration && generation === this.policyGeneration) { this.notice.set(message(error, 'We could not load this agent memory policy.')); this.handleAuthFailure(error); } } finally { if (sessionGeneration === this.requestGeneration && generation === this.policyGeneration) this.loading.set(false); } }
  async createPolicy(agentProfileId: string, body: Parameters<MemoryApi['createAgentPolicy']>[1]): Promise<boolean> { if (!this.policiesLoaded() || this.loadedPolicyAgentId() !== agentProfileId) return false; const sessionGeneration = this.requestGeneration; const generation = this.policyGeneration; return this.mutate(() => this.api.createAgentPolicy(agentProfileId, body).then((policy) => { if (sessionGeneration === this.requestGeneration && generation === this.policyGeneration && this.loadedPolicyAgentId() === agentProfileId) this.policies.update((items) => [...items, policy]); }), sessionGeneration); }
  async attachPolicy(agentProfileId: string, policyRevisionId: string): Promise<boolean> { if (!this.policiesLoaded() || this.loadedPolicyAgentId() !== agentProfileId) return false; const sessionGeneration = this.requestGeneration; const generation = this.policyGeneration; return this.mutate(() => this.api.attachAgentPolicy(agentProfileId, policyRevisionId, this.agentVersion()).then(() => { if (sessionGeneration === this.requestGeneration && generation === this.policyGeneration && this.loadedPolicyAgentId() === agentProfileId) this.attachedPolicyRevisionId.set(policyRevisionId); }), sessionGeneration); }

  registerSessionResetHandler(handler: ((status?: 401 | 403) => void) | null): void { this.sessionResetHandler = handler; }
  resetSessionState(): void { ++this.requestGeneration; ++this.loadGeneration; ++this.detailGeneration; ++this.candidateListGeneration; ++this.candidateDetailGeneration; ++this.policyGeneration; this.api.clearSession?.(); this.memories.set({ items: [], nextCursor: null }); this.selected.set(null); this.detailKey = ''; this.detailLoading.set(false); this.detailError.set(null); this.candidates.set([]); this.candidateNextCursor.set(null); this.candidate.set(null); this.candidateDetailLoading.set(false); this.candidateDetailError.set(null); this.candidateListLoadFailed.set(false); this.candidateLoadingMore.set(false); this.modelInventory.set(null); this.modelConfiguration.set(null); this.modelConfigurationMissing.set(false); this.reindex.set(null); this.policies.set([]); this.attachedPolicyRevisionId.set(null); this.agentVersion.set(0); this.loadedPolicyAgentId.set(null); this.policiesLoaded.set(false); this.filters.set({ scopeType: 'all' }); this.loading.set(false); this.saving.set(false); this.createOutcome.set('idle'); this.notice.set(null); this.resetEpoch.update((value) => value + 1); }
  private async mutate(action: () => Promise<void>, sessionGeneration = this.requestGeneration, isCurrent: () => boolean = () => true): Promise<boolean> { this.saving.set(true); this.notice.set(null); try { await action(); if (sessionGeneration !== this.requestGeneration) return false; return isCurrent(); } catch (error: unknown) { if (sessionGeneration === this.requestGeneration) { if (isCurrent()) this.notice.set(message(error, 'We could not save that change. Your edit is still here.')); this.handleAuthFailure(error); } return false; } finally { if (sessionGeneration === this.requestGeneration) this.saving.set(false); } }
  private handleAuthFailure(error: unknown): void { const status = (error as { readonly status?: unknown })?.status; if (status !== 401 && status !== 403) return; this.resetSessionState(); this.sessionResetHandler?.(status); this.notice.set(status === 401 ? 'Your session has expired. Sign in again to view memory.' : 'You do not have permission to view this memory.'); }
  private isCurrentDetail(generation: number, key: string, id: string): boolean { return generation === this.detailGeneration && key === this.detailKey && this.selected()?.id === id; }
  private replaceSummary(detail: MemoryDetail): void { this.memories.update((page) => ({ ...page, items: page.items.map((item) => item.id === detail.id ? { ...item, ...detail } : item) })); }
}

function hasKnownStatus(error: unknown): boolean { const status = (error as { readonly status?: unknown })?.status; return typeof status === 'number' && Number.isFinite(status); }
function isAuthFailure(error: unknown): boolean { const status = (error as { readonly status?: unknown })?.status; return status === 401 || status === 403; }
function message(error: unknown, fallback: string): string { const status = (error as { readonly status?: unknown })?.status; if (status === 401) return 'Your session has expired. Sign in again to continue.'; if (status === 403) return 'You do not have permission to complete that action.'; if (status === 404) return 'That memory is no longer available.'; if (status === 409) return 'This memory changed elsewhere. Refresh and try again.'; if (status === 503) return 'Memory service is temporarily unavailable. Your draft is still here.'; return fallback; }

/**
 * Review is an owner decision surface. Defensive provider diagnostics with no
 * content or decision metadata must not appear as actionable candidates even
 * if an older Core projection still returns them in the review page.
 */
function actionableCandidates(items: ReadonlyArray<MemoryCandidateSummary>): ReadonlyArray<MemoryCandidateSummary> {
  return items.filter((item) => item.state === 'review' && isActionableReviewCandidate(item));
}
