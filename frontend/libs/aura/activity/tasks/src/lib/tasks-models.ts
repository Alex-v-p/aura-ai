import { InjectionToken } from '@angular/core';
import type { AuraOperationInput, DurableTaskDetail, DurableTaskPage, TaskCommandReceipt, TaskSourceKind, TaskStreamEvent } from '@aura/aura-api-client';
export type { DurableTaskDetail, DurableTaskPage, DurableTaskSummary, TaskCommandReceipt, TaskNormalizedStatus, TaskSourceKind, TaskStreamEvent } from '@aura/aura-api-client';
export type TaskListQuery = AuraOperationInput<'listDurableTasks'>['query'];
export interface TasksApi { clearSession(): void; list(query?: TaskListQuery, signal?: AbortSignal): Promise<DurableTaskPage>; get(id: string, signal?: AbortSignal): Promise<DurableTaskDetail>; cancel(id: string, version: number, idempotencyKey: string, signal?: AbortSignal): Promise<TaskCommandReceipt>; retry(id: string, version: number, idempotencyKey: string, signal?: AbortSignal): Promise<TaskCommandReceipt>; subscribe(id: string, lastEventId: string | null, onEvent: (event: TaskStreamEvent) => void, onState?: (state: 'connected' | 'disconnected' | 'unauthorized' | 'malformed' | 'gap') => void): () => void; }
export const TASKS_API = new InjectionToken<TasksApi>('AURA_TASKS_API');
export class TaskListRequestGate<T> { private request: Promise<T> | null = null; peek(): Promise<T> | null { return this.request; } run(start: () => Promise<T>): Promise<T> { if (this.request) return this.request; const request = start(); const tracked = request.finally(() => { if (this.request === tracked) this.request = null; }); this.request = tracked; return tracked; } reset(): void { this.request = null; } }
export const taskSourceLabel = (source: TaskSourceKind): string => source === 'research' ? 'Web research' : source === 'workflow_instance' ? 'Workflow' : source === 'briefing_occurrence' ? 'Briefing' : source === 'document_processing' ? 'Document processing' : 'Memory reindex';

export interface TaskSourceDestination {
  readonly commands: readonly [string, string];
  readonly label: string;
}

const opaqueId = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/iu;

/**
 * Resolve only routes whose owning source has both an advertised capability
 * and an exact public route identity. The Task Center must not guess parent
 * identifiers for workflows, briefings, or memory projections.
 */
export function resolveTaskSource(detail: Pick<DurableTaskDetail, 'sourceKind' | 'sourceId' | 'routeKey' | 'capabilities'>): TaskSourceDestination | null {
  if (!detail.capabilities.hasSourceDetail || !opaqueId.test(detail.sourceId)) return null;
  if (detail.sourceKind === 'research' && detail.routeKey === 'research_detail') return { commands: ['/research', detail.sourceId], label: 'Open research source' };
  if (detail.sourceKind === 'document_processing' && detail.routeKey === 'document_detail') return { commands: ['/documents', detail.sourceId], label: 'Open document source' };
  return null;
}

export function taskSourceDetailExplanation(detail: Pick<DurableTaskDetail, 'sourceKind' | 'routeKey' | 'capabilities'>): string {
  if (!detail.capabilities.hasSourceDetail) return `${taskSourceLabel(detail.sourceKind)} does not expose a direct detail view here.`;
  if (detail.routeKey === 'workflow_instance_detail' || detail.routeKey === 'briefing_occurrence_detail' || detail.routeKey === 'memory_reindex_detail') return `${taskSourceLabel(detail.sourceKind)} recovery remains available through its owning service.`;
  return 'The source detail route is unavailable until the owning service provides a verified route.';
}
