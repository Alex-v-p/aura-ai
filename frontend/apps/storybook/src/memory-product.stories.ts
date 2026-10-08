import { applicationConfig, moduleMetadata } from '@storybook/angular';
import type { Decorator, Meta, StoryObj } from '@storybook/angular';
import { signal } from '@angular/core';
import { provideRouter } from '@angular/router';
import type { MemorySummary } from '@aura/aura-api-client';
import { MemoryPageComponent, MemorySettingsPageComponent, MemoryCandidatesPageComponent, MemoryStore } from '@aura/aura/knowledge/memory';
import { AgentStore } from '@aura/aura/interaction/agents';

const now = '2026-10-08T09:00:00.000Z';
const memory: MemorySummary = {
  id: 'memory-family', scope: { type: 'user' }, status: 'active', pinned: false, version: 2,
  currentRevision: { id: 'revision-family', memoryId: 'memory-family', revision: 2, kind: 'preference', content: 'The family prefers quiet Sunday mornings.', correctionReason: null, confidence: .94, importance: .7, halfLifeDays: 180, observedAt: now, validFrom: null, validTo: null, createdAt: now },
  reinforcedAt: now, dormantAt: null, archivedAt: null, createdAt: now, updatedAt: now,
};

function fixtureStore(): MemoryStore {
  return {
    memories: signal({ items: [memory], nextCursor: null }), selected: signal(null), candidates: signal([]), candidate: signal(null),
    modelInventory: signal(null), modelConfiguration: signal(null), modelConfigurationMissing: signal(false), reindex: signal(null), policies: signal([]), attachedPolicyRevisionId: signal(null), agentVersion: signal(1),
    filters: signal({}), loading: signal(false), saving: signal(false), notice: signal(null), hasMore: signal(false),
    load: async () => undefined, loadConfigurationStatus: async () => undefined, loadMore: async () => undefined, loadDetail: async () => undefined, create: async () => true, correct: async () => true, setStatus: async () => true, setPinned: async () => true, purgeSelected: async () => true,
    loadCandidates: async () => undefined, loadCandidate: async () => undefined, approveCandidate: async () => true, rejectCandidate: async () => true, loadSettings: async () => undefined, saveSettings: async () => true, resumeReindex: async () => true, loadPolicies: async () => undefined, createPolicy: async () => true, attachPolicy: async () => true,
  } as unknown as MemoryStore;
}

function firstRunStore(): MemoryStore {
  const store = fixtureStore() as unknown as Record<string, unknown>;
  store['memories'] = signal({ items: [], nextCursor: null });
  store['modelInventory'] = signal({ observedAt: now, models: [
    { id: 'qwen3:8b', displayName: 'qwen3:8b', provider: 'ollama', modelRevision: 'latest', modelDigest: 'a'.repeat(64), capabilities: ['structured_output'], dimension: null, available: true, disabledReason: null },
    { id: 'qwen3-embedding:4b', displayName: 'qwen3-embedding:4b', provider: 'ollama', modelRevision: 'latest', modelDigest: 'b'.repeat(64), capabilities: ['embedding'], dimension: 2560, available: true, disabledReason: null },
  ] });
  store['modelConfiguration'] = signal(null);
  store['modelConfigurationMissing'] = signal(true);
  return store as unknown as MemoryStore;
}

function recordsStore(filters: Record<string, unknown> = {}, missing = false): MemoryStore {
  const store = fixtureStore() as unknown as Record<string, unknown>;
  store['memories'] = signal({ items: filters['q'] ? [] : [memory], nextCursor: null });
  store['filters'] = signal(filters);
  store['modelConfigurationMissing'] = signal(missing);
  return store as unknown as MemoryStore;
}

function settingsStore(configuration: unknown, reindex: unknown, notice: string | null = null): MemoryStore {
  const store = fixtureStore() as unknown as Record<string, unknown>;
  store['modelInventory'] = signal({ observedAt: now, models: [
    { id: 'qwen3:8b', displayName: 'qwen3:8b', provider: 'ollama', modelRevision: 'latest', modelDigest: 'a'.repeat(64), capabilities: ['structured_output'], dimension: null, available: true, disabledReason: null },
    { id: 'qwen3-embedding:4b', displayName: 'qwen3-embedding:4b', provider: 'ollama', modelRevision: 'latest', modelDigest: 'b'.repeat(64), capabilities: ['embedding'], dimension: 2560, available: true, disabledReason: null },
  ] });
  store['modelConfiguration'] = signal(configuration);
  store['modelConfigurationMissing'] = signal(false);
  store['reindex'] = signal(reindex);
  store['notice'] = signal(notice);
  return store as unknown as MemoryStore;
}

function reviewStore(): MemoryStore {
  const store = fixtureStore() as unknown as Record<string, unknown>;
  const candidate = { id: 'candidate-story', jobId: 'job-story', runId: 'run-story', version: 1, action: 'create', state: 'review', content: 'The family prefers quiet Sunday mornings.', kind: 'preference', scope: { type: 'user' }, confidence: .74, importance: .7, halfLifeDays: 180, validTo: null, sensitivity: 'ordinary', relatedMemoryId: null, memoryId: null, decisionReason: null, createdAt: now, decidedAt: null, groundedMessageIds: ['message-story'] };
  store['candidates'] = signal([candidate]);
  store['candidateNextCursor'] = signal(null);
  store['candidateLoadingMore'] = signal(false);
  store['candidate'] = signal(candidate);
  return store as unknown as MemoryStore;
}

const configured = { version: 2, extraction: { modelId: 'qwen3:8b', modelRevision: 'latest', modelDigest: 'a'.repeat(64) }, embedding: { modelId: 'qwen3-embedding:4b', modelRevision: 'latest', modelDigest: 'b'.repeat(64) }, activeGeneration: null, buildingGeneration: null, updatedAt: now };
const idleReindex = { phase: 'idle', activeGeneration: null, replacementGeneration: null, processedRevisionCount: 0, totalRevisionCount: 0, startedAt: null, updatedAt: null, completedAt: null, retryable: false };
const runningReindex = { ...idleReindex, phase: 'running' as const, processedRevisionCount: 4, totalRevisionCount: 12, replacementGeneration: { id: 'generation-next', generation: 2, modelId: 'qwen3-embedding:4b', modelRevision: 'latest', modelDigest: 'b'.repeat(64), dimension: 2560, status: 'building' as const, createdAt: now, completedAt: null } };
const darkThemeDecorator: Decorator = (story) => { if (typeof document !== 'undefined') { document.documentElement.dataset['theme'] = 'dark'; document.documentElement.style.colorScheme = 'dark'; } return story(); };

const meta: Meta<MemoryPageComponent> = {
  title: 'Aura/Memory product', component: MemoryPageComponent, parameters: { layout: 'fullscreen' },
  decorators: [applicationConfig({ providers: [{ provide: MemoryStore, useValue: fixtureStore() }, { provide: AgentStore, useValue: { activeAgents: signal([]) } }, provideRouter([])] })],
};
export default meta;
type Story = StoryObj<MemoryPageComponent>;
export const Records: Story = { name: 'Records and detail-ready list' };
export const FirstRunSetup: Story = {
  name: 'Settings · guided first run',
  render: () => ({ template: '<aura-memory-settings-page />' }),
  decorators: [moduleMetadata({ imports: [MemorySettingsPageComponent] }), applicationConfig({ providers: [{ provide: MemoryStore, useValue: firstRunStore() }, provideRouter([])] })],
};
export const ReviewQueue: Story = {
  name: 'Review queue · candidate detail',
  render: () => ({ template: '<aura-memory-candidates-page />' }),
  decorators: [moduleMetadata({ imports: [MemoryCandidatesPageComponent] }), applicationConfig({ providers: [{ provide: MemoryStore, useValue: reviewStore() }, { provide: AgentStore, useValue: { activeAgents: signal([]) } }, provideRouter([])] })],
};
export const EmptyRecords: Story = { name: 'Records · empty first use', decorators: [applicationConfig({ providers: [{ provide: MemoryStore, useValue: recordsStore({}, true) }, provideRouter([])] })] };
export const FilteredEmptyRecords: Story = { name: 'Records · filtered empty', decorators: [applicationConfig({ providers: [{ provide: MemoryStore, useValue: recordsStore({ scopeType: 'all', q: 'does not exist' }) }, provideRouter([])] })] };
export const ConfiguredSettings: Story = { name: 'Settings · configured', render: () => ({ template: '<aura-memory-settings-page />' }), decorators: [moduleMetadata({ imports: [MemorySettingsPageComponent] }), applicationConfig({ providers: [{ provide: MemoryStore, useValue: settingsStore(configured, idleReindex) }, provideRouter([])] })] };
export const ReindexSettings: Story = { name: 'Settings · reindex progress', render: () => ({ template: '<aura-memory-settings-page />' }), decorators: [moduleMetadata({ imports: [MemorySettingsPageComponent] }), applicationConfig({ providers: [{ provide: MemoryStore, useValue: settingsStore(configured, runningReindex) }, provideRouter([])] })] };
export const SettingsError: Story = { name: 'Settings · recoverable error', render: () => ({ template: '<aura-memory-settings-page />' }), decorators: [moduleMetadata({ imports: [MemorySettingsPageComponent] }), applicationConfig({ providers: [{ provide: MemoryStore, useValue: settingsStore(null, null, 'Model inventory is temporarily unavailable.') }, provideRouter([])] })] };
export const MobileDarkRecords: Story = { name: 'Records · mobile dark', parameters: { viewport: { defaultViewport: 'mobile1' }, backgrounds: { default: 'dark' } }, decorators: [darkThemeDecorator, applicationConfig({ providers: [{ provide: MemoryStore, useValue: fixtureStore() }, { provide: AgentStore, useValue: { activeAgents: signal([]) } }, provideRouter([])] })] };
