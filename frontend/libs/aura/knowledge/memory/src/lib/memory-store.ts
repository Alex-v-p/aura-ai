import { Injectable, computed, inject, signal } from '@angular/core';
import type { AgentMemoryPolicy, MemoryCandidateDetail, MemoryCandidateEdit, MemoryCandidateSummary, MemoryDetail, MemoryModelConfiguration, MemoryModelInventory, MemoryPage, MemoryReindexStatus, MemoryScope } from '@aura/aura-api-client';
import { AuraMemoryApi } from './memory-api';
import { isActionableReviewCandidate, isMissingMemoryConfiguration, loadMemorySettings, MEMORY_API, normalizeMemoryFilters, saveMemorySettings, type MemoryApi, type MemoryFilters } from './memory-models';

@Injectable({ providedIn: 'root' })
export class MemoryStore {
  private readonly api: MemoryApi = inject(MEMORY_API, { optional: true }) ?? inject(AuraMemoryApi);
  readonly memories = signal<MemoryPage>({ items: [], nextCursor: null });
  readonly selected = signal<MemoryDetail | null>(null);
  readonly candidates = signal<ReadonlyArray<MemoryCandidateSummary>>([]);
  readonly candidateNextCursor = signal<string | null>(null);
  readonly candidateLoadingMore = signal(false);
  readonly candidate = signal<MemoryCandidateDetail | null>(null);
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
  readonly hasMore = computed(() => this.memories().nextCursor !== null);
  private loadGeneration = 0;

  async load(filters: MemoryFilters = this.filters()): Promise<void> {
    const generation = ++this.loadGeneration;
    this.filters.set(normalizeMemoryFilters(filters)); this.loading.set(true); this.notice.set(null);
    try { const page = await this.api.listMemories(undefined, this.filters()); if (generation === this.loadGeneration) this.memories.set(page); } catch (error: unknown) { if (generation === this.loadGeneration) this.notice.set(message(error, 'We could not load your memories.')); }
    finally { if (generation === this.loadGeneration) this.loading.set(false); }
  }
  /** Resolve only the setup status for Records without turning provider errors into a page failure. */
  async loadConfigurationStatus(): Promise<void> {
    try {
      this.modelConfiguration.set(await this.api.getModelConfiguration());
      this.modelConfigurationMissing.set(false);
    } catch (error: unknown) {
      if (isMissingMemoryConfiguration(error)) this.modelConfigurationMissing.set(true);
    }
  }
  async loadMore(): Promise<void> { const cursor = this.memories().nextCursor; if (!cursor || this.loading()) return; const generation = this.loadGeneration; const filters = this.filters(); this.loading.set(true); try { const page = await this.api.listMemories(cursor, filters); if (generation === this.loadGeneration && filters === this.filters()) this.memories.update((current) => ({ items: [...current.items, ...page.items], nextCursor: page.nextCursor })); } catch (error: unknown) { if (generation === this.loadGeneration) this.notice.set(message(error, 'We could not load more memories.')); } finally { if (generation === this.loadGeneration) this.loading.set(false); } }
  async loadDetail(id: string, scope?: MemoryScope): Promise<MemoryDetail | null> {
    this.notice.set(null);
    // A direct /memory/:id visit has no list-row context yet. Resolve the
    // owner-authorized scope from the loaded summary when possible, while
    // preserving an explicit agent scope supplied by a scoped link.
    const resolvedScope = scope ?? this.memories().items.find((item) => item.id === id)?.scope;
    try {
      const detail = await this.api.getMemory(id, resolvedScope);
      this.selected.set(detail);
      return detail;
    } catch (error: unknown) { this.notice.set(message(error, 'We could not load that memory.')); return null; }
  }
  async create(input: Parameters<MemoryApi['createMemory']>[0]): Promise<boolean> { return this.mutate(() => this.api.createMemory(input).then((detail) => { this.selected.set(detail); void this.load(this.filters()); })); }
  async correct(id: string, input: Parameters<MemoryApi['correctMemory']>[2]): Promise<boolean> { const current = this.selected(); if (!current) return false; return this.mutate(() => this.api.correctMemory(id, current.scope, input).then((detail) => { this.selected.set(detail); this.replaceSummary(detail); })); }
  async setStatus(status: Parameters<MemoryApi['updateStatus']>[1], relatedMemoryId?: string | null): Promise<boolean> { const current = this.selected(); if (!current) return false; return this.mutate(() => this.api.updateStatus(current.id, status, current.version, relatedMemoryId, current.scope).then((detail) => { this.selected.set(detail); this.replaceSummary(detail); })); }
  async setPinned(pinned: boolean): Promise<boolean> { const current = this.selected(); if (!current) return false; return this.mutate(() => this.api.updatePin(current.id, pinned, current.version, current.scope).then((detail) => { this.selected.set(detail); this.replaceSummary(detail); })); }
  async purgeSelected(confirmation: string): Promise<boolean> { const current = this.selected(); if (!current || confirmation !== 'PURGE MEMORY') return false; return this.mutate(() => this.api.purge(current.id, current.scope, confirmation, current.version).then(() => { this.selected.set(null); this.memories.update((page) => ({ ...page, items: page.items.filter((item) => item.id !== current.id) })); })); }
  async loadCandidates(state: Parameters<MemoryApi['listCandidates']>[1] = 'review'): Promise<void> { this.loading.set(true); this.notice.set(null); try { const page = await this.api.listCandidates(undefined, state); this.candidates.set(actionableCandidates(page.items)); this.candidateNextCursor.set(page.nextCursor); } catch (error: unknown) { this.notice.set(message(error, 'We could not load review candidates.')); } finally { this.loading.set(false); } }
  async loadMoreCandidates(state: Parameters<MemoryApi['listCandidates']>[1] = 'review'): Promise<void> { const cursor = this.candidateNextCursor(); if (!cursor || this.candidateLoadingMore()) return; this.candidateLoadingMore.set(true); try { const page = await this.api.listCandidates(cursor, state); this.candidates.update((items) => [...items, ...actionableCandidates(page.items)]); this.candidateNextCursor.set(page.nextCursor); } catch (error: unknown) { this.notice.set(message(error, 'We could not load more review candidates.')); } finally { this.candidateLoadingMore.set(false); } }
  async loadCandidate(id: string): Promise<void> { this.notice.set(null); try { this.candidate.set(await this.api.getCandidate(id)); } catch (error: unknown) { this.notice.set(message(error, 'We could not load that candidate.')); } }
  async approveCandidate(edit?: MemoryCandidateEdit): Promise<boolean> { const current = this.candidate(); if (!current) return false; return this.mutate(() => this.api.approveCandidate(current.id, current.version, edit).then((receipt) => { this.candidate.set(receipt.candidate); this.candidates.update((items) => items.map((item) => item.id === receipt.candidate.id ? receipt.candidate : item)); })); }
  async rejectCandidate(reason: string): Promise<boolean> { const current = this.candidate(); if (!current || !reason.trim()) return false; return this.mutate(() => this.api.rejectCandidate(current.id, current.version, reason.trim()).then((receipt) => { this.candidate.set(receipt.candidate); this.candidates.update((items) => items.map((item) => item.id === receipt.candidate.id ? receipt.candidate : item)); })); }
  async loadSettings(): Promise<void> {
    this.loading.set(true);
    this.notice.set(null);
    this.modelConfigurationMissing.set(false);
    const result = await loadMemorySettings(this.api);
    const failures = result.errors.map(({ key, reason }) => message(reason, key === 'inventory' ? 'Model inventory is unavailable.' : key === 'configuration' ? 'Model configuration is unavailable.' : 'Reindex status is unavailable.'));
    if (result.inventory) this.modelInventory.set(result.inventory);
    if (result.configuration) this.modelConfiguration.set(result.configuration);
    else if (result.configurationMissing) this.modelConfigurationMissing.set(true);
    if (result.reindex) this.reindex.set(result.reindex);
    if (failures.length > 0) this.notice.set(failures.join(' '));
    this.loading.set(false);
  }
  async saveSettings(extractionModelId: string, embeddingModelId: string): Promise<boolean> {
    const current = this.modelConfiguration();
    if (!extractionModelId || !embeddingModelId) return false;
    return this.mutate(() => saveMemorySettings(this.api, extractionModelId, embeddingModelId, current).then((configuration) => {
      this.modelConfiguration.set(configuration);
      this.modelConfigurationMissing.set(false);
      return this.api.getReindexStatus();
    }).then((status) => { this.reindex.set(status); }));
  }
  async resumeReindex(): Promise<boolean> { const generationId = this.reindex()?.replacementGeneration?.id; if (!generationId) return false; return this.mutate(() => this.api.resumeReindex(generationId).then((status) => this.reindex.set(status))); }
  async loadPolicies(agentProfileId: string): Promise<void> { this.loading.set(true); this.notice.set(null); try { const result = await this.api.listAgentPolicies(agentProfileId); this.policies.set(result.items); this.attachedPolicyRevisionId.set(result.attachedPolicyRevisionId); this.agentVersion.set(result.agentVersion); } catch (error: unknown) { this.notice.set(message(error, 'We could not load this agent memory policy.')); } finally { this.loading.set(false); } }
  async createPolicy(agentProfileId: string, body: Parameters<MemoryApi['createAgentPolicy']>[1]): Promise<boolean> { return this.mutate(() => this.api.createAgentPolicy(agentProfileId, body).then((policy) => { this.policies.update((items) => [...items, policy]); })); }
  async attachPolicy(agentProfileId: string, policyRevisionId: string): Promise<boolean> { return this.mutate(() => this.api.attachAgentPolicy(agentProfileId, policyRevisionId, this.agentVersion()).then(() => { this.attachedPolicyRevisionId.set(policyRevisionId); })); }

  private async mutate(action: () => Promise<void>): Promise<boolean> { this.saving.set(true); this.notice.set(null); try { await action(); return true; } catch (error: unknown) { this.notice.set(message(error, 'We could not save that change. Your edit is still here.')); return false; } finally { this.saving.set(false); } }
  private replaceSummary(detail: MemoryDetail): void { this.memories.update((page) => ({ ...page, items: page.items.map((item) => item.id === detail.id ? { ...item, ...detail } : item) })); }
}

function message(error: unknown, fallback: string): string { return error instanceof Error ? error.message : fallback; }

/**
 * Review is an owner decision surface. Defensive provider diagnostics with no
 * content or decision metadata must not appear as actionable candidates even
 * if an older Core projection still returns them in the review page.
 */
function actionableCandidates(items: ReadonlyArray<MemoryCandidateSummary>): ReadonlyArray<MemoryCandidateSummary> {
  return items.filter(isActionableReviewCandidate);
}
