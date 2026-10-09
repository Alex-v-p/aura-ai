import { ChangeDetectionStrategy, Component, effect, ElementRef, inject, signal, ViewChild } from '@angular/core';
import { DatePipe, DecimalPipe, PercentPipe } from '@angular/common';
import { A11yModule } from '@angular/cdk/a11y';
import { FormsModule } from '@angular/forms';
import { ActivatedRoute, Router, RouterLink, RouterLinkActive, type ParamMap } from '@angular/router';
import type { AgentMemoryPolicy, MemoryCandidateAction, MemoryCandidateEdit, MemoryCollectionScopeType, MemoryKind, MemoryLifecycleStatus, MemoryRecallMode, MemoryScope } from '@aura/aura-api-client';
import { AgentStore } from '@aura/aura/interaction/agents';
import { canSaveMemoryConfiguration, defaultMemoryPolicySettings, hasMemoryAdvancedFilters, hydratedMemoryModelIds, hydratedMemoryPolicySettings, isSelectableMemoryModel, memoryAgentFilterChange, memoryScopeFilterChange, recommendedMemoryModelId } from './memory-models';
import { MemoryStore } from './memory-store';

const kinds: ReadonlyArray<MemoryKind> = ['episodic', 'semantic', 'procedural', 'preference', 'system'];
const statuses: ReadonlyArray<MemoryLifecycleStatus> = ['active', 'dormant', 'archived', 'disabled', 'disputed', 'superseded'];
const approvalActions: ReadonlyArray<Exclude<MemoryCandidateAction, 'ignore' | 'review'>> = ['create', 'reinforce', 'supersede', 'dispute'];

@Component({ selector: 'aura-memory-page', standalone: true, imports: [A11yModule, DatePipe, PercentPipe, FormsModule, RouterLink, RouterLinkActive], changeDetection: ChangeDetectionStrategy.OnPush, templateUrl: './memory-pages.component.html', styleUrl: './memory-pages.component.css' })
export class MemoryPageComponent {
  readonly store = inject(MemoryStore);
  private readonly route = inject(ActivatedRoute);
  private readonly router = inject(Router);
  readonly agents = inject(AgentStore);
  readonly kinds = kinds;
  readonly statuses = statuses;
  readonly selectedId = signal<string | null>(null);
  readonly editing = signal(false);
  readonly creating = signal(false);
  readonly purgeOpen = signal(false);
  readonly purgePhrase = signal('');
  readonly relationTargetId = signal('');
  readonly relationAction = signal<'disputed' | 'superseded'>('disputed');
  readonly scopeType = signal<'user' | 'agent'>('user');
  readonly agentProfileId = signal('');
  readonly purgeOpenerId = signal<string | null>(null);
  readonly advancedFiltersOpen = signal(false);
  @ViewChild('advancedFilterToggle') private readonly advancedFilterToggle?: ElementRef<HTMLButtonElement>;
  @ViewChild('purgeCloseButton') private readonly purgeCloseButton?: ElementRef<HTMLButtonElement>;
  draftContent = '';
  draftReason = '';
  draftKind: MemoryKind = 'semantic';
  draftConfidence = 0.9;
  draftImportance = 0.5;
  draftHalfLife = 365;
  createContent = '';
  createKind: MemoryKind = 'semantic';
  createConfidence = 0.9;
  createImportance = 0.5;
  createHalfLife = 365;
  private readonly initialLoad: Promise<void>;
  private routeActionKey: string | null = null;
  private routeLoadKey: string | null = null;
  constructor() {
    this.initialLoad = this.store.load();
    void this.store.loadConfigurationStatus();
    this.route.paramMap.subscribe((params) => { const id = params.get('id'); this.selectedId.set(id); if (id) void this.openRoute(id, this.route.snapshot.queryParamMap); });
    this.route.queryParamMap.subscribe((params) => { const id = this.selectedId(); if (id) void this.openRoute(id, params); });
  }
  select(id: string): void { this.selectedId.set(id); this.editing.set(false); this.purgePhrase.set(''); const summary = this.store.memories().items.find((item) => item.id === id); void this.router.navigate(['/memory', id], { queryParams: this.scopeQuery(summary?.scope) }); void this.store.loadDetail(id, summary?.scope); }
  setScopeFilter(value: string): void {
    const scopeType: MemoryCollectionScopeType | undefined = value === 'user' || value === 'agent' || value === 'all' ? value : undefined;
    void this.store.load(memoryScopeFilterChange(this.store.filters(), scopeType));
  }
  setAgentFilter(agentProfileId: string): void {
    void this.store.load(memoryAgentFilterChange(this.store.filters(), agentProfileId));
  }
  beginCreate(): void { this.creating.set(true); this.createContent = ''; this.scopeType.set('user'); this.agentProfileId.set(''); }
  cancelCreate(): void { this.creating.set(false); }
  async saveCreate(): Promise<void> { if (!this.createContent.trim() || (this.scopeType() === 'agent' && !this.agentProfileId().trim())) return; const scope: MemoryScope = this.scopeType() === 'agent' ? { type: 'agent', agentProfileId: this.agentProfileId().trim() } : { type: 'user' }; if (await this.store.create({ content: this.createContent.trim(), kind: this.createKind, scope, confidence: this.createConfidence, importance: this.createImportance, halfLifeDays: this.createHalfLife })) { this.creating.set(false); } }
  beginCorrection(): void { const current = this.store.selected(); if (!current) return; this.draftContent = current.currentRevision.content; this.draftReason = ''; this.draftKind = current.currentRevision.kind; this.draftConfidence = current.currentRevision.confidence; this.draftImportance = current.currentRevision.importance; this.draftHalfLife = current.currentRevision.halfLifeDays; this.editing.set(true); }
  async saveCorrection(): Promise<void> { const current = this.store.selected(); if (!current || !this.draftContent.trim() || !this.draftReason.trim()) return; if (await this.store.correct(current.id, { content: this.draftContent.trim(), reason: this.draftReason.trim(), kind: this.draftKind, confidence: this.draftConfidence, importance: this.draftImportance, halfLifeDays: this.draftHalfLife, expectedVersion: current.version })) this.editing.set(false); }
  async changeStatus(status: MemoryLifecycleStatus): Promise<void> { await this.store.setStatus(status); }
  async relate(): Promise<void> { if (!this.relationTargetId().trim()) return; await this.store.setStatus(this.relationAction(), this.relationTargetId().trim()); }
  openPurge(event: Event): void { this.purgeOpenerId.set((event.currentTarget as HTMLElement | null)?.id ?? null); this.purgePhrase.set(''); this.purgeOpen.set(true); queueMicrotask(() => this.purgeCloseButton?.nativeElement.focus()); }
  closePurge(): void { this.purgeOpen.set(false); this.purgePhrase.set(''); queueMicrotask(() => { const id = this.purgeOpenerId(); const opener = id ? document.getElementById(id) : null; opener?.focus(); }); }
  async purge(): Promise<void> { if (await this.store.purgeSelected(this.purgePhrase())) { this.selectedId.set(null); await this.router.navigate(['/memory']); } this.closePurge(); }
  scopeLabel(scope: MemoryScope): string { return scope.type === 'user' ? 'Shared with you' : `Private to agent ${scope.agentProfileId}`; }
  relevance(detail: NonNullable<ReturnType<MemoryStore['selected']>>): string { return detail?.currentRelevance === undefined ? 'Not reported' : `${Math.round(detail.currentRelevance * 100)}%`; }
  scopeQuery(scope?: MemoryScope): Record<string, string> { return scope?.type === 'agent' ? { scopeType: 'agent', agentProfileId: scope.agentProfileId } : scope ? { scopeType: 'user' } : {}; }
  hasAdvancedFilters(): boolean { return hasMemoryAdvancedFilters(this.store.filters()); }
  isFirstUse(): boolean { const filters = this.store.filters(); return this.store.modelConfigurationMissing() && !this.store.loading() && !this.store.notice() && this.store.memories().items.length === 0 && !filters.q && !filters.agentProfileId && !filters.kind && !filters.status && !filters.provenanceType && filters.confidenceMin === undefined && !filters.createdFrom && !filters.createdTo && !filters.includeHistorical; }
  isFilteredEmpty(): boolean { const filters = this.store.filters(); return Boolean(filters.q || filters.agentProfileId || filters.kind || filters.status || filters.provenanceType || filters.confidenceMin !== undefined || filters.createdFrom || filters.createdTo || filters.includeHistorical); }
  toggleAdvancedFilters(event?: Event): void { if (event) { if ((event as KeyboardEvent).key !== 'Escape') return; event.preventDefault(); this.advancedFiltersOpen.set(false); queueMicrotask(() => this.advancedFilterToggle?.nativeElement.focus()); return; } this.advancedFiltersOpen.update((open) => !open); }
  private async openRoute(id: string, params: ParamMap): Promise<void> {
    const routeLoadKey = `${id}:${params.get('scopeType') ?? ''}:${params.get('agentProfileId') ?? ''}:${params.get('action') ?? ''}`;
    if (this.routeLoadKey === routeLoadKey) return;
    this.routeLoadKey = routeLoadKey;
    await this.initialLoad;
    if (this.selectedId() !== id) return;
    const queryScope = this.scopeFromQuery(params);
    const summary = this.store.memories().items.find((item) => item.id === id);
    const detail = await this.store.loadDetail(id, queryScope ?? summary?.scope);
    const action = params.get('action');
    const actionKey = `${id}:${action ?? ''}`;
    if (!detail || !action || this.routeActionKey === actionKey) return;
    this.routeActionKey = actionKey;
    if (action === 'correct') this.beginCorrection();
    if (action === 'disable' && detail.status !== 'disabled') void this.changeStatus('disabled');
  }
  private scopeFromQuery(params: ParamMap): MemoryScope | undefined {
    const type = params.get('scopeType');
    const agentProfileId = params.get('agentProfileId');
    if (type === 'agent' && agentProfileId) return { type: 'agent', agentProfileId };
    if (type === 'user') return { type: 'user' };
    return undefined;
  }
}

@Component({ selector: 'aura-memory-candidates-page', standalone: true, imports: [DatePipe, PercentPipe, FormsModule, RouterLink], changeDetection: ChangeDetectionStrategy.OnPush, templateUrl: './memory-candidates.component.html', styleUrl: './memory-pages.component.css' })
export class MemoryCandidatesPageComponent {
  readonly store = inject(MemoryStore);
  readonly agents = inject(AgentStore);
  private readonly route = inject(ActivatedRoute);
  readonly kinds = kinds;
  readonly approvalActions = approvalActions;
  readonly editing = signal(false);
  readonly rejectReason = signal('');
  readonly edit = signal<MemoryCandidateEdit | null>(null);
  draftAction: MemoryCandidateAction = 'create';
  draftKind: MemoryKind = 'semantic';
  draftScopeType: 'user' | 'agent' = 'user';
  draftAgentProfileId = '';
  draftValidTo: string | null = null;
  draftRelatedMemoryId: string | null = null;
  constructor() { void this.store.loadCandidates(); this.route.queryParamMap.subscribe((params) => { const candidateId = params.get('candidateId'); if (candidateId && this.store.candidate()?.id !== candidateId) void this.store.loadCandidate(candidateId); }); }
  select(id: string): void { this.editing.set(false); this.rejectReason.set(''); void this.store.loadCandidate(id); }
  beginEdit(): void {
    const candidate = this.store.candidate();
    if (!candidate?.content?.trim()) return;
    const action = this.approvalAction(candidate.action);
    const kind = candidate.kind ?? 'semantic';
    const scope = candidate.scope ?? { type: 'user' as const };
    const importance = candidate.importance ?? 0.5;
    const halfLifeDays = candidate.halfLifeDays ?? 30;
    this.edit.set({ content: candidate.content, action, kind, scope, confidence: candidate.confidence, importance, halfLifeDays, validTo: candidate.validTo, relatedMemoryId: candidate.relatedMemoryId });
    this.draftAction = action;
    this.draftKind = kind;
    this.draftScopeType = scope.type;
    this.draftAgentProfileId = scope.type === 'agent' ? scope.agentProfileId : '';
    this.draftValidTo = candidate.validTo;
    this.draftRelatedMemoryId = candidate.relatedMemoryId;
    this.editing.set(true);
  }
  async approve(): Promise<void> {
    const candidate = this.store.candidate();
    if (!candidate?.content?.trim()) return;
    const edit = this.edit();
    if (this.editing() && (!edit || !edit.content.trim() || edit.confidence < 0 || edit.confidence > 1 || edit.importance < 0 || edit.importance > 1 || edit.halfLifeDays < 0.25 || edit.halfLifeDays > 3650 || (this.draftScopeType === 'agent' && !this.draftAgentProfileId.trim()))) return;
    const approved: MemoryCandidateEdit | undefined = this.editing() && edit
      ? { ...edit, action: this.draftAction, kind: this.draftKind, scope: this.draftScopeType === 'agent' ? { type: 'agent' as const, agentProfileId: this.draftAgentProfileId.trim() } : { type: 'user' as const }, validTo: this.draftValidTo, relatedMemoryId: this.draftRelatedMemoryId }
      : undefined;
    if (await this.store.approveCandidate(approved)) this.editing.set(false);
  }
  async reject(): Promise<void> { if (this.rejectReason().trim()) await this.store.rejectCandidate(this.rejectReason()); }
  candidateScope(candidate: NonNullable<ReturnType<MemoryStore['candidate']>>): string { const scope = candidate.scope; return !scope ? 'Unclassified' : scope.type === 'user' ? 'Shared user' : `Agent ${scope.agentProfileId}`; }
  candidateAction(candidate: { readonly action: MemoryCandidateAction }): string { return this.approvalAction(candidate.action); }
  private approvalAction(action: MemoryCandidateAction): Exclude<MemoryCandidateAction, 'ignore' | 'review'> { return action === 'ignore' || action === 'review' ? 'create' : action; }
}

@Component({ selector: 'aura-memory-settings-page', standalone: true, imports: [DatePipe, FormsModule, RouterLink], changeDetection: ChangeDetectionStrategy.OnPush, templateUrl: './memory-settings.component.html', styleUrl: './memory-pages.component.css' })
export class MemorySettingsPageComponent {
  readonly store = inject(MemoryStore);
  extractionModelId = '';
  embeddingModelId = '';
  confirmSetup = false;
  constructor() { void this.initialize(); }
  async initialize(): Promise<void> { await this.store.loadSettings(); this.hydrateSelections(); }
  inventory(capability: 'structured_output' | 'embedding') { return this.store.modelInventory()?.models.filter((model) => isSelectableMemoryModel(model) && model.capabilities.includes(capability)) ?? []; }
  recommended(capability: 'structured_output' | 'embedding'): string | null { return recommendedMemoryModelId(capability, this.store.modelInventory()?.models ?? []); }
  isRecommended(modelId: string, capability: 'structured_output' | 'embedding'): boolean { return modelId === this.recommended(capability) && (capability === 'structured_output' ? modelId === 'qwen3:8b' : modelId === 'qwen3-embedding:4b'); }
  async retry(): Promise<void> { await this.initialize(); }
  private hydrateSelections(): void {
    const selections = hydratedMemoryModelIds(this.store.modelConfiguration(), this.store.modelInventory()?.models ?? []);
    this.extractionModelId = selections.extractionModelId;
    this.embeddingModelId = selections.embeddingModelId;
  }
  async save(): Promise<void> { if (!canSaveMemoryConfiguration(this.store.modelConfigurationMissing(), this.confirmSetup)) return; if (this.extractionModelId && this.embeddingModelId) await this.store.saveSettings(this.extractionModelId, this.embeddingModelId); }
  async resume(): Promise<void> { await this.store.resumeReindex(); }
}

@Component({ selector: 'aura-agent-memory-policy-page', standalone: true, imports: [DatePipe, DecimalPipe, FormsModule, RouterLink], changeDetection: ChangeDetectionStrategy.OnPush, templateUrl: './memory-policy.component.html', styleUrl: './memory-pages.component.css' })
export class AgentMemoryPolicyPageComponent {
  private readonly route = inject(ActivatedRoute);
  readonly store = inject(MemoryStore);
  readonly agentId = this.route.snapshot.paramMap.get('id') ?? '';
  private readonly defaults = defaultMemoryPolicySettings();
  recallMode: MemoryRecallMode = this.defaults.recallMode;
  automaticRecallThreshold = this.defaults.automaticRecallThreshold;
  sharedUserRead = this.defaults.sharedUserRead;
  currentAgentRead = this.defaults.currentAgentRead;
  sharedUserPromotion = this.defaults.sharedUserPromotion;
  fallbackThreshold = this.defaults.fallbackRelevanceThreshold;
  maxMemories = this.defaults.maxMemories;
  budgetFraction = this.defaults.contextBudgetFraction;
  fallbackAgents = this.defaults.fallbackAgentProfileIds.join(', ');
  private hydratedPolicyId: string | null = null;
  constructor() {
    effect(() => {
      const attachedId = this.store.attachedPolicyRevisionId();
      const attached = this.store.policies().find((policy) => policy.id === attachedId);
      if (attached && attached.id !== this.hydratedPolicyId) this.hydrate(attached);
    });
    if (this.agentId) void this.store.loadPolicies(this.agentId);
  }
  async create(): Promise<void> {
    if (!this.valid()) return;
    await this.store.createPolicy(this.agentId, { recallMode: this.recallMode, automaticRecallThreshold: this.automaticRecallThreshold, sharedUserRead: this.sharedUserRead, currentAgentRead: this.currentAgentRead, sharedUserPromotion: this.sharedUserPromotion, fallbackRelevanceThreshold: this.fallbackThreshold, maxMemories: this.maxMemories, contextBudgetFraction: this.budgetFraction, fallbackAgentProfileIds: this.fallbackAgents.split(',').map((id) => id.trim()).filter(Boolean), expectedRevision: this.store.policies().at(-1)?.revision ?? 0 });
  }
  async attach(policy: AgentMemoryPolicy): Promise<void> { await this.store.attachPolicy(this.agentId, policy.id); }
  valid(): boolean { return this.automaticRecallThreshold >= 0 && this.automaticRecallThreshold <= 1 && this.fallbackThreshold >= 0 && this.fallbackThreshold <= 1 && this.maxMemories >= 1 && this.maxMemories <= 8 && this.budgetFraction > 0 && this.budgetFraction <= 0.2; }
  private hydrate(policy: AgentMemoryPolicy): void {
    const settings = hydratedMemoryPolicySettings(policy);
    this.hydratedPolicyId = policy.id;
    this.recallMode = settings.recallMode;
    this.automaticRecallThreshold = settings.automaticRecallThreshold;
    this.sharedUserRead = settings.sharedUserRead;
    this.currentAgentRead = settings.currentAgentRead;
    this.sharedUserPromotion = settings.sharedUserPromotion;
    this.fallbackThreshold = settings.fallbackRelevanceThreshold;
    this.maxMemories = settings.maxMemories;
    this.budgetFraction = settings.contextBudgetFraction;
    this.fallbackAgents = settings.fallbackAgentProfileIds.join(', ');
  }
}
