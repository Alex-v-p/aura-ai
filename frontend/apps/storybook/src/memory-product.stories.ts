import { applicationConfig } from '@storybook/angular';
import type { Meta, StoryObj } from '@storybook/angular';
import { signal } from '@angular/core';
import { provideRouter } from '@angular/router';
import type { MemorySummary } from '@aura/aura-api-client';
import { MemoryPageComponent, MemoryStore } from '@aura/aura/knowledge/memory';
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
    modelInventory: signal(null), modelConfiguration: signal(null), reindex: signal(null), policies: signal([]), attachedPolicyRevisionId: signal(null), agentVersion: signal(1),
    filters: signal({}), loading: signal(false), saving: signal(false), notice: signal(null), hasMore: signal(false),
    load: async () => undefined, loadMore: async () => undefined, loadDetail: async () => undefined, create: async () => true, correct: async () => true, setStatus: async () => true, setPinned: async () => true, purgeSelected: async () => true,
    loadCandidates: async () => undefined, loadCandidate: async () => undefined, approveCandidate: async () => true, rejectCandidate: async () => true, loadSettings: async () => undefined, saveSettings: async () => true, resumeReindex: async () => true, loadPolicies: async () => undefined, createPolicy: async () => true, attachPolicy: async () => true,
  } as unknown as MemoryStore;
}

const meta: Meta<MemoryPageComponent> = {
  title: 'Aura/Memory product', component: MemoryPageComponent, parameters: { layout: 'fullscreen' },
  decorators: [applicationConfig({ providers: [{ provide: MemoryStore, useValue: fixtureStore() }, { provide: AgentStore, useValue: { activeAgents: signal([]) } }, provideRouter([])] })],
};
export default meta;
type Story = StoryObj<MemoryPageComponent>;
export const Records: Story = { name: 'Records and detail-ready list' };
