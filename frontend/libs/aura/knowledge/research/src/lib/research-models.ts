import { InjectionToken } from '@angular/core';
import type {
  ActiveResearchDetail, AuraOperationInput, CreateResearchRequest, ResearchAdmission, ResearchBudget, ResearchDetail,
  ResearchEvidencePage, ResearchMode, ResearchPage, ResearchSettings, ResearchSummary, ReplaceResearchSettingsRequest, TaskStreamEvent,
} from '@aura/aura-api-client';

export type { ActiveResearchDetail, CreateResearchRequest, ResearchAdmission, ResearchBudget, ResearchDetail, ResearchEvidencePage, ResearchMode, ResearchPage, ResearchSettings, ResearchSummary, ReplaceResearchSettingsRequest, TaskStreamEvent as ResearchTaskStreamEvent } from '@aura/aura-api-client';

export type ResearchListQuery = AuraOperationInput<'listResearchTasks'>['query'];
export type ResearchStreamEvent = TaskStreamEvent;

export interface ResearchApi {
  clearSession(): void;
  getSettings(signal?: AbortSignal): Promise<ResearchSettings>;
  replaceSettings(body: ReplaceResearchSettingsRequest, signal?: AbortSignal, idempotencyKey?: string): Promise<ResearchSettings>;
  list(query?: ResearchListQuery, signal?: AbortSignal): Promise<ResearchPage>;
  create(body: CreateResearchRequest, signal?: AbortSignal, idempotencyKey?: string): Promise<ResearchAdmission>;
  get(id: string, signal?: AbortSignal): Promise<ResearchDetail>;
  evidence(id: string, signal?: AbortSignal): Promise<ResearchEvidencePage>;
  cancel(id: string, expectedVersion: number, idempotencyKey: string, signal?: AbortSignal): Promise<ResearchDetail>;
  extend(id: string, expectedVersion: number, cumulativeBudget: ResearchBudget, idempotencyKey: string, signal?: AbortSignal): Promise<ResearchDetail>;
  delete(id: string, expectedVersion: number, deleteLinkedArtifacts: boolean, idempotencyKey: string, signal?: AbortSignal): Promise<ResearchDetail>;
  retryCleanup(id: string, expectedVersion: number, idempotencyKey: string, signal?: AbortSignal): Promise<ResearchDetail>;
  reconcileCleanup(id: string, expectedVersion: number, idempotencyKey: string, signal?: AbortSignal): Promise<ResearchDetail>;
  subscribe(taskId: string, lastEventId: string | null, onEvent: (event: ResearchStreamEvent) => void, onState: (state: 'connected' | 'disconnected' | 'unauthorized' | 'malformed' | 'gap') => void): () => void;
}

export const RESEARCH_API = new InjectionToken<ResearchApi>('AURA_RESEARCH_API');

export const RESEARCH_MODES: ReadonlyArray<ResearchMode> = ['quick', 'auto', 'deep', 'agent_led'];
export const researchModeLabel = (mode: ResearchMode): string => mode === 'agent_led' ? 'Agent-led' : `${mode[0].toUpperCase()}${mode.slice(1)}`;
export function researchBudgetFieldError(field: keyof ResearchBudget, value: number, maximum: number, admitted?: number): string | undefined { const minimum = field === 'browserPages' ? 0 : 1; if (!Number.isFinite(value) || value < minimum) return `${field} must be at least ${minimum}.`; if (admitted !== undefined && value < admitted) return `${field} must be at least the admitted ${admitted}.`; if (value > maximum) return `${field} cannot exceed ${maximum}.`; return undefined; }
