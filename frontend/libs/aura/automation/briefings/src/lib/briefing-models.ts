import { InjectionToken } from '@angular/core';
import type { BriefingCapabilities, BriefingOccurrence, BriefingResult, BriefingSchedule, ClientTimeZone, CreateBriefingScheduleRequest, BriefingDeletionReceipt } from '@aura/aura-api-client';

export type { BriefingCapabilities, BriefingDailyCadence, BriefingExecutionPolicyReferences, BriefingOccurrence, BriefingResult, BriefingSchedule, BriefingSourceGrants, ClientTimeZone, CreateBriefingScheduleRequest, BriefingDeletionReceipt } from '@aura/aura-api-client';

export interface BriefingApi {
  clearSession?(): void;
  capabilities(): Promise<BriefingCapabilities>;
  list(status?: 'active' | 'paused' | 'deleted'): Promise<{ readonly items: ReadonlyArray<BriefingSchedule>; readonly nextCursor: string | null }>;
  get(id: string): Promise<BriefingSchedule>;
  create(request: CreateBriefingScheduleRequest, idempotencyKey?: string): Promise<BriefingSchedule>;
  pause(id: string, expectedVersion: number, idempotencyKey?: string): Promise<BriefingSchedule>;
  resume(id: string, expectedVersion: number, idempotencyKey?: string): Promise<BriefingSchedule>;
  deleteSchedule(id: string, expectedVersion: number, idempotencyKey?: string): Promise<BriefingDeletionReceipt>;
  occurrences(id: string): Promise<{ readonly items: ReadonlyArray<BriefingOccurrence>; readonly nextCursor: string | null }>;
  occurrence(scheduleId: string, occurrenceId: string): Promise<BriefingOccurrence>;
  deleteHistory(scheduleId: string, expectedScheduleVersion: number, idempotencyKey?: string): Promise<BriefingDeletionReceipt>;
  result(id: string): Promise<BriefingResult>;
  deleteResult(id: string, expectedVersion: number, idempotencyKey?: string): Promise<BriefingDeletionReceipt>;
}

export const BRIEFING_API = new InjectionToken<BriefingApi>('AURA_BRIEFING_API');

export interface BriefingDraft {
  readonly displayName: string;
  readonly localTime: string;
  readonly timeZone: ClientTimeZone | '';
  readonly agentRevisionId: string;
  readonly calendarIds: ReadonlyArray<string>;
  readonly documentIds: ReadonlyArray<string>;
}

export const emptyBriefingDraft = (): BriefingDraft => ({ displayName: '', localTime: '08:00', timeZone: '', agentRevisionId: '', calendarIds: [], documentIds: [] });

export function validateBriefingDraft(draft: BriefingDraft, capabilities: BriefingCapabilities | null, eligibleAgentRevisionIds: ReadonlySet<string> | null = null): Readonly<Record<string, string>> {
  const errors: Record<string, string> = {};
  if (!draft.displayName.trim()) errors['displayName'] = 'Give this briefing a name.';
  else if (draft.displayName.trim().length > 80) errors['displayName'] = 'Use 80 characters or fewer.';
  if (!/^([01]\d|2[0-3]):[0-5]\d$/.test(draft.localTime)) errors['localTime'] = 'Use a local time such as 08:00.';
  if (!draft.timeZone || !isIanaTimeZone(draft.timeZone)) errors['timeZone'] = 'Choose a valid IANA timezone.';
  if (!draft.agentRevisionId) errors['agentRevisionId'] = 'Choose an active agent revision.';
  else if (eligibleAgentRevisionIds && !eligibleAgentRevisionIds.has(draft.agentRevisionId)) errors['agentRevisionId'] = 'Choose an active revision with a valid pinned tool policy.';
  if (!draft.calendarIds.length) errors['calendarIds'] = 'Choose at least one calendar.';
  if (!draft.documentIds.length) errors['documentIds'] = 'Choose at least one ready document.';
  if (!capabilities) errors['capability'] = 'Briefing settings are still loading.';
  else if (capabilities.cadenceKind !== 'daily' || capabilities.memoryEnabled || capabilities.toolsEnabled) errors['capability'] = 'The briefing capability is not available for this workspace.';
  return errors;
}

export function hasPinnedToolPolicy(revision: { readonly toolPolicyRevisionId?: string }): boolean {
  const value = revision.toolPolicyRevisionId?.trim();
  return Boolean(value && !value.includes(' '));
}

export function isIanaTimeZone(value: string): value is ClientTimeZone {
  try { new Intl.DateTimeFormat('en-US', { timeZone: value }).format(); return value === 'UTC' || value.includes('/'); } catch { return false; }
}

export function briefingStatusLabel(status: BriefingSchedule['status']): string { return status === 'active' ? 'Active' : status === 'paused' ? 'Paused' : 'Deleted'; }
export function occurrenceStatusLabel(status: BriefingOccurrence['status']): string { return status.replaceAll('_', ' ').replace(/\b\w/g, (letter) => letter.toUpperCase()); }
export function sourceCompletenessLabel(value: BriefingOccurrence['sourceCompleteness'] | BriefingResult['sourceCompleteness']): string { return value === 'complete' ? 'All selected sources were available' : value === 'partial' ? 'Some selected sources were unavailable' : value === 'unavailable' ? 'Selected sources were unavailable' : 'Source completeness is unknown'; }
