import { Injectable, computed, inject, signal } from '@angular/core';
import { AgentStore } from '@aura/aura/interaction/agents';
import { CalendarStore } from '@aura/aura/integrations/calendar';
import { DocumentsStore } from '@aura/aura/knowledge/documents';
import { AuraBriefingApi, BriefingApiError } from './briefing-api';
import { BRIEFING_API, emptyBriefingDraft, hasPinnedToolPolicy, validateBriefingDraft, type BriefingApi, type BriefingDraft, type BriefingOccurrence, type BriefingResult, type BriefingSchedule } from './briefing-models';

type MutationAttempt = { readonly key: string; readonly fingerprint: string; readonly sessionEpoch: number; readonly mutationEpoch: number };

@Injectable({ providedIn: 'root' })
export class BriefingStore {
  private readonly api: BriefingApi;
  readonly capabilities = signal<Awaited<ReturnType<BriefingApi['capabilities']>> | null>(null);
  readonly schedules = signal<ReadonlyArray<BriefingSchedule>>([]);
  readonly occurrences = signal<ReadonlyArray<BriefingOccurrence>>([]);
  readonly selectedSchedule = signal<BriefingSchedule | null>(null);
  readonly selectedOccurrence = signal<BriefingOccurrence | null>(null);
  readonly selectedResult = signal<BriefingResult | null>(null);
  readonly draft = signal<BriefingDraft>(emptyBriefingDraft());
  readonly errors = signal<Readonly<Record<string, string>>>({});
  readonly loading = signal(false);
  readonly detailLoading = signal(false);
  readonly saving = signal(false);
  readonly notice = signal<string | null>(null);
  readonly errorStatus = signal<number | null>(null);
  readonly resetEpoch = signal(0);
  readonly activeAgents = computed(() => this.agents.activeAgents());
  readonly eligibleAgentRevisions = computed(() => this.activeAgents().flatMap((profile) => profile.revisions.filter((revision) => revision.revisionId === profile.currentRevisionId && hasPinnedToolPolicy(revision))));
  readonly sourceReadiness = computed(() => {
    const calendarReady = this.calendar.connected() && this.calendar.calendarsComplete() && this.calendar.requestState() !== 'error' && !this.calendar.error();
    const documentsReady = !this.documents.loading() && !this.documents.error() && !this.documents.authRequired() && !this.documents.permissionDenied() && this.documents.documents().nextCursor === null;
    return calendarReady && documentsReady;
  });
  readonly documentsLoading = computed(() => this.documents.loading());
  readonly calendarsLoading = computed(() => ['loading', 'refreshing', 'saving'].includes(this.calendar.requestState()));
  readonly sourcesLoading = computed(() => this.calendarsLoading() || this.documents.loading());
  readonly calendarsHaveMore = computed(() => this.calendar.connected() && this.calendar.requestState() === 'idle' && !this.calendar.calendarsComplete() && !this.calendar.error());
  readonly readyDocuments = computed(() => this.sourceReadiness() ? this.documents.documents().items.filter((document) => document.status === 'ready') : []);
  readonly selectableCalendars = computed(() => this.sourceReadiness() ? this.calendar.calendars().filter((item) => Boolean(item.id) && item.selected) : []);
  readonly sourcesComplete = computed(() => this.sourceReadiness());
  readonly documentsHaveMore = computed(() => this.documents.documents().nextCursor !== null);
  readonly sourceNotice = computed(() => {
    if (this.calendarsLoading()) return 'Calendar sources are loading. Creation stays disabled until discovery settles.';
    if (this.calendar.error()) return 'Calendar sources could not be loaded. Retry sources when ready.';
    if (!this.calendar.connected()) return 'Connect an authorized calendar before creating a briefing.';
    if (!this.calendar.calendarsComplete()) return 'Load all calendar pages before creating a briefing.';
    if (this.documents.loading()) return 'Document sources are loading. Creation stays disabled until the current list settles.';
    if (this.documents.authRequired()) return 'Sign in again to review authorized documents.';
    if (this.documents.permissionDenied()) return 'You do not have permission to use these documents.';
    if (this.documents.error()) return 'Documents could not be loaded. Retry sources or load the remaining page.';
    if (this.documentsHaveMore()) return 'Load all document pages before creating a briefing.';
    return 'Calendar and document authorization is still being checked. Creation stays disabled until the current lists are complete.';
  });

  private sessionEpoch = 0;
  private collectionGeneration = 0;
  private detailGeneration = 0;
  private mutationEpoch = 0;
  private readonly mutationKeys = new Map<string, MutationAttempt>();
  private sessionResetHandler: ((status?: 401 | 403) => void) | null = null;
  private readonly agents = inject(AgentStore);
  private readonly calendar = inject(CalendarStore);
  private readonly documents = inject(DocumentsStore);
  private readonly injectedApi = inject(BRIEFING_API, { optional: true });

  constructor() { this.api = this.injectedApi ?? inject(AuraBriefingApi); void this.loadCapabilities(); void this.loadSources(); }

  async loadCapabilities(): Promise<void> {
    const sessionEpoch = this.sessionEpoch;
    try {
      const value = await this.api.capabilities();
      if (sessionEpoch !== this.sessionEpoch) return;
      if (value.cadenceKind !== 'daily' || value.memoryEnabled || value.toolsEnabled) throw new BriefingApiError(422, 'The briefing capability is not available for this workspace.');
      this.capabilities.set(value);
      if (sessionEpoch === this.sessionEpoch) { this.notice.set(null); this.errorStatus.set(null); }
    } catch (error: unknown) {
      if (sessionEpoch === this.sessionEpoch) this.setFailure(error, 'Briefing settings are temporarily unavailable.');
    }
  }

  async reloadCapabilities(): Promise<void> { this.capabilities.set(null); await this.loadCapabilities(); }

  async loadSources(): Promise<void> {
    const sessionEpoch = this.sessionEpoch;
    try { await Promise.all([this.agents.load(), this.calendar.load(), this.documents.load()]); if (sessionEpoch === this.sessionEpoch && this.sourceReadiness()) { this.notice.set(null); this.errorStatus.set(null); } }
    catch (error: unknown) { if (sessionEpoch === this.sessionEpoch) this.setFailure(error, 'Briefing sources could not be loaded. Your draft is still here.'); }
  }

  async retrySources(): Promise<void> { await this.loadSources(); }
  async loadMoreCalendars(): Promise<void> {
    if (!this.calendarsHaveMore()) return;
    const sessionEpoch = this.sessionEpoch;
    try { await this.calendar.discoverCalendars(false, true); if (sessionEpoch === this.sessionEpoch && !this.calendar.error()) { this.notice.set(null); this.errorStatus.set(null); } }
    catch (error: unknown) { this.setFailure(error, 'More calendars could not be loaded. Retry when ready.'); }
  }
  async loadMoreDocuments(): Promise<void> { const sessionEpoch = this.sessionEpoch; try { await this.documents.loadMore(); if (sessionEpoch === this.sessionEpoch && !this.documents.error()) { this.notice.set(null); this.errorStatus.set(null); } } catch (error: unknown) { this.setFailure(error, 'More documents could not be loaded. Retry when ready.'); } }

  async load(): Promise<void> {
    const sessionEpoch = this.sessionEpoch;
    const generation = ++this.collectionGeneration;
    this.loading.set(true); this.notice.set(null);
    try {
      const page = await this.api.list();
      if (this.isCurrentRequest(sessionEpoch, generation, this.collectionGeneration)) this.schedules.set(page.items);
    } catch (error: unknown) {
      if (this.isCurrentRequest(sessionEpoch, generation, this.collectionGeneration)) this.setFailure(error, 'Briefings are temporarily unavailable.');
    } finally { if (this.isCurrentRequest(sessionEpoch, generation, this.collectionGeneration)) this.loading.set(false); }
  }

  async inspect(id: string): Promise<BriefingSchedule | null> {
    const sessionEpoch = this.sessionEpoch;
    const generation = ++this.detailGeneration;
    this.clearPrivateDetail(); this.detailLoading.set(true); this.notice.set(null);
    try {
      const schedule = await this.api.get(id);
      if (!this.isCurrentRequest(sessionEpoch, generation, this.detailGeneration)) return null;
      this.selectedSchedule.set(schedule);
      const page = await this.api.occurrences(id);
      if (this.isCurrentRequest(sessionEpoch, generation, this.detailGeneration)) this.occurrences.set(page.items);
      return schedule;
    } catch (error: unknown) {
      if (this.isCurrentRequest(sessionEpoch, generation, this.detailGeneration)) this.setFailure(error, 'This briefing is unavailable.');
      return null;
    } finally { if (this.isCurrentRequest(sessionEpoch, generation, this.detailGeneration)) this.detailLoading.set(false); }
  }

  async inspectOccurrence(scheduleId: string, occurrenceId: string): Promise<BriefingOccurrence | null> {
    const sessionEpoch = this.sessionEpoch;
    const generation = ++this.detailGeneration;
    this.clearPrivateDetail(); this.detailLoading.set(true);
    try {
      const occurrence = await this.api.occurrence(scheduleId, occurrenceId);
      if (!this.isCurrentRequest(sessionEpoch, generation, this.detailGeneration)) return null;
      this.selectedOccurrence.set(occurrence);
      if (occurrence.resultId) {
        const result = await this.api.result(occurrence.resultId);
        if (this.isCurrentRequest(sessionEpoch, generation, this.detailGeneration)) this.selectedResult.set(result);
      }
      return occurrence;
    } catch (error: unknown) {
      if (this.isCurrentRequest(sessionEpoch, generation, this.detailGeneration)) this.setFailure(error, 'This briefing occurrence is unavailable.');
      return null;
    } finally { if (this.isCurrentRequest(sessionEpoch, generation, this.detailGeneration)) this.detailLoading.set(false); }
  }

  setDraft(patch: Partial<BriefingDraft>): void { this.mutationKeys.delete('create'); this.draft.update((current) => ({ ...current, ...patch })); this.errors.set({}); }
  toggleCalendar(id: string): void { this.mutationKeys.delete('create'); this.draft.update((draft) => ({ ...draft, calendarIds: draft.calendarIds.includes(id) ? draft.calendarIds.filter((item) => item !== id) : [...draft.calendarIds, id] })); }
  toggleDocument(id: string): void { this.mutationKeys.delete('create'); this.draft.update((draft) => ({ ...draft, documentIds: draft.documentIds.includes(id) ? draft.documentIds.filter((item) => item !== id) : [...draft.documentIds, id] })); }

  async create(): Promise<BriefingSchedule | null> {
    const sessionEpoch = this.sessionEpoch;
    let draft = this.draft();
    const sourceErrors: Record<string, string> = {};
    if (!this.sourcesComplete()) sourceErrors['sources'] = this.documentsHaveMore() ? 'Load all documents before creating a briefing.' : 'Wait for calendars and documents to finish loading before creating a briefing.';
    else {
      const calendars = new Set(this.selectableCalendars().map((calendar) => calendar.id));
      const documents = new Set(this.readyDocuments().map((document) => document.id));
      const calendarIds = draft.calendarIds.filter((id) => calendars.has(id));
      const documentIds = draft.documentIds.filter((id) => documents.has(id));
      if (calendarIds.length !== draft.calendarIds.length || documentIds.length !== draft.documentIds.length) {
        draft = { ...draft, calendarIds, documentIds }; this.mutationKeys.delete('create'); this.draft.set(draft);
        sourceErrors['sources'] = 'One or more selected sources is no longer authorized or ready. Review your selections.';
      }
    }
    const eligible = new Set(this.eligibleAgentRevisions().map((revision) => revision.revisionId));
    const errors = { ...validateBriefingDraft(draft, this.capabilities(), eligible), ...sourceErrors };
    this.errors.set(errors);
    if (Object.keys(errors).length) return null;
    const agent = this.eligibleAgentRevisions().find((revision) => revision.revisionId === draft.agentRevisionId);
    const capability = this.capabilities();
    const toolPolicyRevisionId = agent?.toolPolicyRevisionId;
    if (!agent || !capability || !hasPinnedToolPolicy(agent) || !toolPolicyRevisionId) return null;
    const request = { displayName: draft.displayName.trim(), cadence: { kind: 'daily' as const, localTime: draft.localTime, timeZone: draft.timeZone as never }, sources: { includeReminders: true as const, calendarIds: draft.calendarIds, documentIds: draft.documentIds }, agentRevisionId: agent.revisionId, executionPolicies: { executionPolicyRevisionId: capability.executionPolicyRevisionId, promptBundleRevisionId: agent.promptBundleRevisionId, modelPolicyRevisionId: agent.modelPolicyRevisionId, toolPolicyRevisionId } };
    let attempt: MutationAttempt;
    try { attempt = this.mutationKey('create', JSON.stringify(request)); }
    catch (error: unknown) { this.setFailure(error, 'Secure mutation identity is unavailable. Retry when secure randomness is available.'); return null; }
    this.saving.set(true); this.notice.set(null);
    try {
      const schedule = await this.api.create(request, attempt.key);
      if (sessionEpoch !== this.sessionEpoch) return null;
      this.schedules.update((items) => [schedule, ...items]); this.mutationKeys.delete('create'); this.clearDraft(); return schedule;
    } catch (error: unknown) {
      if (sessionEpoch === this.sessionEpoch) {
        this.setFailure(error, 'The briefing could not be created. Your selections are still here.');
        if (!isAmbiguous(error) && this.ownsAttempt('create', attempt)) this.mutationKeys.delete('create');
      }
      return null;
    } finally { if (sessionEpoch === this.sessionEpoch) this.saving.set(false); }
  }

  async pauseOrResume(schedule: BriefingSchedule): Promise<boolean> {
    const sessionEpoch = this.sessionEpoch; const operation = `${schedule.status === 'active' ? 'pause' : 'resume'}:${schedule.id}`; const attempt = this.mutationKey(operation, `${schedule.id}:${schedule.version}:${schedule.status}`); this.saving.set(true);
    try { const updated = schedule.status === 'active' ? await this.api.pause(schedule.id, schedule.version, attempt.key) : await this.api.resume(schedule.id, schedule.version, attempt.key); if (sessionEpoch !== this.sessionEpoch) return false; this.replace(updated); this.mutationKeys.delete(operation); return true; }
    catch (error: unknown) { if (sessionEpoch === this.sessionEpoch) { this.setFailure(error, 'This briefing changed elsewhere. Refresh before trying again.'); if (!isAmbiguous(error) && this.ownsAttempt(operation, attempt)) this.mutationKeys.delete(operation); } return false; }
    finally { if (sessionEpoch === this.sessionEpoch) this.saving.set(false); }
  }

  async deleteSchedule(schedule: BriefingSchedule): Promise<boolean> {
    const sessionEpoch = this.sessionEpoch; const operation = `delete-schedule:${schedule.id}`; const attempt = this.mutationKey(operation, `${schedule.id}:${schedule.version}`); this.saving.set(true);
    try { await this.api.deleteSchedule(schedule.id, schedule.version, attempt.key); if (sessionEpoch !== this.sessionEpoch) return false; this.schedules.update((items) => items.filter((item) => item.id !== schedule.id)); this.selectedSchedule.set(null); this.notice.set('Schedule deleted. Retained history and results remain available.'); this.mutationKeys.delete(operation); return true; }
    catch (error: unknown) { if (sessionEpoch === this.sessionEpoch) { this.setFailure(error, 'The schedule could not be deleted. No history was removed.'); if (!isAmbiguous(error) && this.ownsAttempt(operation, attempt)) this.mutationKeys.delete(operation); } return false; }
    finally { if (sessionEpoch === this.sessionEpoch) this.saving.set(false); }
  }

  async deleteHistory(schedule: BriefingSchedule): Promise<boolean> {
    const sessionEpoch = this.sessionEpoch; const operation = `delete-history:${schedule.id}`; const attempt = this.mutationKey(operation, `${schedule.id}:${schedule.version}`); this.saving.set(true);
    try { await this.api.deleteHistory(schedule.id, schedule.version, attempt.key); if (sessionEpoch !== this.sessionEpoch) return false; this.occurrences.set([]); this.notice.set('Briefing history deleted. Future schedule work remains unchanged.'); this.mutationKeys.delete(operation); return true; }
    catch (error: unknown) { if (sessionEpoch === this.sessionEpoch) { this.setFailure(error, 'Briefing history could not be deleted.'); if (!isAmbiguous(error) && this.ownsAttempt(operation, attempt)) this.mutationKeys.delete(operation); } return false; }
    finally { if (sessionEpoch === this.sessionEpoch) this.saving.set(false); }
  }

  async deleteResult(result: BriefingResult): Promise<boolean> {
    const sessionEpoch = this.sessionEpoch; const operation = `delete-result:${result.id}`; const attempt = this.mutationKey(operation, `${result.id}:${result.version}`); this.saving.set(true);
    try { await this.api.deleteResult(result.id, result.version, attempt.key); if (sessionEpoch !== this.sessionEpoch) return false; this.selectedResult.set({ ...result, content: null, status: 'deleted', deletedAt: new Date().toISOString() }); this.notice.set('Result content deleted. It will not be reconstructed.'); this.mutationKeys.delete(operation); return true; }
    catch (error: unknown) { if (sessionEpoch === this.sessionEpoch) { this.setFailure(error, 'Result content could not be deleted.'); if (!isAmbiguous(error) && this.ownsAttempt(operation, attempt)) this.mutationKeys.delete(operation); } return false; }
    finally { if (sessionEpoch === this.sessionEpoch) this.saving.set(false); }
  }

  clearDraft(): void { this.mutationKeys.clear(); this.draft.set(emptyBriefingDraft()); this.errors.set({}); }
  clearDetail(): void { ++this.detailGeneration; this.clearPrivateDetail(); this.detailLoading.set(false); }
  resetSessionState(): void { ++this.sessionEpoch; ++this.collectionGeneration; ++this.detailGeneration; ++this.mutationEpoch; this.mutationKeys.clear(); this.api.clearSession?.(); this.capabilities.set(null); this.schedules.set([]); this.clearDraft(); this.clearPrivateDetail(); this.loading.set(false); this.saving.set(false); this.notice.set(null); this.errorStatus.set(null); this.resetEpoch.update((value) => value + 1); }
  registerSessionResetHandler(handler: ((status?: 401 | 403) => void) | null): void { this.sessionResetHandler = handler; }

  private clearPrivateDetail(): void { this.selectedSchedule.set(null); this.selectedOccurrence.set(null); this.selectedResult.set(null); this.occurrences.set([]); }
  private replace(updated: BriefingSchedule): void { this.schedules.update((items) => items.map((item) => item.id === updated.id ? updated : item)); this.selectedSchedule.set(updated); }
  private mutationKey(operation: string, fingerprint: string): MutationAttempt {
    const existing = this.mutationKeys.get(operation);
    if (existing && existing.sessionEpoch === this.sessionEpoch && existing.fingerprint === fingerprint) return existing;
    if (typeof crypto === 'undefined' || typeof crypto.randomUUID !== 'function') throw new BriefingApiError(0, 'Secure mutation identity is unavailable. Retry when secure randomness is available.');
    const attempt = { key: crypto.randomUUID(), fingerprint, sessionEpoch: this.sessionEpoch, mutationEpoch: ++this.mutationEpoch };
    this.mutationKeys.set(operation, attempt); return attempt;
  }
  private ownsAttempt(operation: string, attempt: MutationAttempt): boolean { const current = this.mutationKeys.get(operation); return current?.key === attempt.key && current.sessionEpoch === this.sessionEpoch && current.mutationEpoch === attempt.mutationEpoch; }
  private isCurrentRequest(sessionEpoch: number, generation: number, currentGeneration: number): boolean { return sessionEpoch === this.sessionEpoch && generation === currentGeneration; }
  private setFailure(error: unknown, fallback: string): void { const status = statusOf(error); if (status === 401 || status === 403) { this.resetSessionState(); this.sessionResetHandler?.(status); } this.errorStatus.set(status); this.notice.set(messageOf(error, fallback)); }
}

function statusOf(error: unknown): number | null { const value = (error as { readonly status?: unknown })?.status; return typeof value === 'number' ? value : null; }
function messageOf(error: unknown, fallback: string): string { const status = statusOf(error); if (status === 0) return 'Aura is offline or the request could not be completed. Retry when ready.'; if (status === 401) return 'Your session has expired. Sign in again.'; if (status === 403) return 'You do not have permission to manage briefings.'; if (status === 404) return 'That briefing is no longer available.'; if (status === 409) return 'This briefing changed elsewhere. Refresh and review it before trying again.'; if (status !== null && status >= 500) return 'Briefings are temporarily unavailable. Retry shortly.'; return fallback; }
function isAmbiguous(error: unknown): boolean { const status = statusOf(error); return status === 0 || status === null || status >= 500; }
