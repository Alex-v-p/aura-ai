import { Injectable, inject, signal } from '@angular/core';
import type { AgentProfile, WorkflowDefinition, WorkflowDeletionReceipt, WorkflowDeletionScope, WorkflowDraft, WorkflowInstance, WorkflowInstanceSummary, WorkflowRevision, WorkflowStep, WorkflowStreamEvent, WorkflowValidationResult } from '@aura/aura-api-client';
import { AuraWorkflowApi, WorkflowApiError } from './workflow-api';
import { WORKFLOW_STUDIO_API, cloneWorkflowStep, emptyWorkflowDraft, isUnknownWorkflowOutcome, mergeWorkflowInvalidation, newWorkflowStep, shouldCommitWorkflowAgentLoad, validateWorkflowDraft, visibleWorkflowInvalidation, WorkflowStreamError, type WorkflowAuthoringDraft, type WorkflowInvalidationProjection, type WorkflowMutationOutcome, type WorkflowStudioApi, type WorkflowStudioStatus, type WorkflowStepKind } from './workflow-models';

type Mutation = { readonly key: string; readonly fingerprint: string; readonly epoch: number };
const emptyPage = () => ({ items: [] as ReadonlyArray<WorkflowDefinition>, nextCursor: null as string | null });
const newKey = (): string => { if (typeof crypto === 'undefined' || typeof crypto.randomUUID !== 'function') throw new WorkflowApiError(0, 'Secure mutation identity is unavailable.'); return crypto.randomUUID(); };
const statusOf = (error: unknown): number | null => { const status = (error as { readonly status?: unknown })?.status; return typeof status === 'number' ? status : null; };

@Injectable({ providedIn: 'root' })
export class WorkflowStore {
  private readonly api: WorkflowStudioApi;
  readonly workflows = signal(emptyPage());
  readonly agents = signal<ReadonlyArray<AgentProfile>>([]);
  readonly selectedWorkflow = signal<WorkflowDefinition | null>(null);
  readonly draft = signal<WorkflowDraft | null>(null);
  readonly authoring = signal<WorkflowAuthoringDraft>(emptyWorkflowDraft());
  readonly revision = signal<WorkflowRevision | null>(null);
  readonly instances = signal<ReadonlyArray<WorkflowInstanceSummary>>([]);
  readonly selectedInstance = signal<WorkflowInstance | null>(null);
  readonly terminalDeletionTarget = signal<{ readonly workflowId: string; readonly instanceId: string; readonly version: number } | null>(null);
  readonly cancellationRequested = signal(false);
  readonly validation = signal<WorkflowValidationResult | null>(null);
  readonly fieldErrors = signal<Readonly<Record<string, string>>>({});
  readonly status = signal<WorkflowStudioStatus>('idle');
  readonly notice = signal<string | null>(null);
  readonly errorStatus = signal<number | null>(null);
  readonly mutationOutcome = signal<WorkflowMutationOutcome>('idle');
  readonly connectionState = signal<'paused' | 'connecting' | 'live' | 'reconnecting' | 'unavailable'>('paused');
  readonly lastEventId = signal<string | null>(null);
  readonly resetEpoch = signal(0);
  private sessionEpoch = 0;
  private listEpoch = 0;
  private detailEpoch = 0;
  private agentLoadEpoch = 0;
  private mutationEpoch = 0;
  private agentsSessionEstablished = false;
  private readonly mutations = new Map<string, Mutation>();
  private stopStream: (() => void) | null = null;
  private streamRetry: ReturnType<typeof setTimeout> | null = null;
  private streamRetryCount = 0;
  private refreshInFlight: Promise<void> | null = null;
  private queuedInvalidation: WorkflowInvalidationProjection | null = null;
  /**
   * Event ids remain pending until every authoritative projection affected by
   * the event has reconciled successfully.  The stream can replay an event
   * while a refresh is being retried, so this is deliberately separate from
   * the cursor: advancing the cursor must never be the only deduplication
   * guard.
   */
  private readonly pendingEventIds = new Set<string>();
  private readonly processedEventIds = new Set<string>();
  private readonly injectedApi = inject(WORKFLOW_STUDIO_API, { optional: true });

  constructor() { this.api = this.injectedApi ?? inject(AuraWorkflowApi); }

  async load(): Promise<boolean> {
    const session = this.sessionEpoch; const generation = ++this.listEpoch; this.status.set('loading'); this.notice.set(null);
    try { const page = await this.api.list(); if (session !== this.sessionEpoch || generation !== this.listEpoch) return false; this.workflows.set(page); this.status.set('idle'); this.errorStatus.set(null); return true; }
    catch (error: unknown) { if (session === this.sessionEpoch && generation === this.listEpoch) this.fail(error, 'Workflows are temporarily unavailable.'); return false; }
  }
  async loadAgents(): Promise<void> {
    const session = this.sessionEpoch;
    if (session > 0 && !this.agentsSessionEstablished) return;
    const generation = ++this.agentLoadEpoch;
    try {
      if (this.api.establishSession) await this.api.establishSession();
      if (!this.active(session) || generation !== this.agentLoadEpoch) return;
      this.agentsSessionEstablished = true;
      const page = await this.api.listAgents?.();
      if (page && shouldCommitWorkflowAgentLoad(session, this.sessionEpoch, generation, this.agentLoadEpoch, this.agentsSessionEstablished)) this.agents.set(page.items);
    } catch (error: unknown) { if (this.active(session) && generation === this.agentLoadEpoch) this.fail(error, 'Authorized agent revisions are temporarily unavailable.'); }
  }

  async inspect(id: string, preserveDraft = false, preserveInstance = false, preserveTerminalTarget = false): Promise<WorkflowDefinition | null> {
    const session = this.sessionEpoch; const generation = ++this.detailEpoch; const parentChanged = this.selectedWorkflow()?.id !== id; if (parentChanged || !preserveInstance) { this.selectedInstance.set(null); if (parentChanged || !preserveTerminalTarget) this.terminalDeletionTarget.set(null); this.cancellationRequested.set(false); } if (parentChanged) { if (!preserveDraft) { this.draft.set(null); this.authoring.set(emptyWorkflowDraft()); } this.revision.set(null); this.instances.set([]); this.validation.set(null); this.fieldErrors.set({}); } this.notice.set(null);
    try {
      const workflow = await this.api.get(id);
      if (!this.active(session, generation)) return null;
      this.selectedWorkflow.set(workflow);
      const [instances, revision] = await Promise.allSettled([
        this.api.listInstances(id),
        workflow.latestRevisionId ? this.api.getRevision(id, workflow.latestRevisionId) : Promise.resolve(null),
      ]);
      if (!this.active(session, generation)) return null;
      if (instances.status === 'fulfilled') this.instances.set(instances.value.items);
      if (revision.status === 'fulfilled') this.revision.set(revision.value);
      if (instances.status === 'rejected' || revision.status === 'rejected') { const failure = instances.status === 'rejected' ? instances.reason : revision.status === 'rejected' ? revision.reason : new Error('Workflow detail reconciliation failed.'); this.fail(failure, 'The workflow detail could not be fully reconciled.'); return null; }
      return workflow;
    } catch (error: unknown) {
      if (this.active(session, generation)) this.fail(error, 'This workflow is unavailable.');
      return null;
    }
  }

  async openDraft(workflowId: string, draftId: string): Promise<WorkflowDraft | null> { const session = this.sessionEpoch; try { const value = await this.api.getDraft(workflowId, draftId); if (!this.active(session)) return null; this.draft.set(value); this.authoring.set({ displayName: value.displayName, inputFields: value.inputFields, steps: value.steps }); return value; } catch (error: unknown) { if (this.active(session)) this.fail(error, 'The workflow draft is unavailable.'); return null; } }
  beginDraft(): void { this.draft.set(null); this.revision.set(null); this.validation.set(null); this.fieldErrors.set({}); this.authoring.set(emptyWorkflowDraft()); this.mutationOutcome.set('idle'); this.notice.set(null); }
  setAuthoring(patch: Partial<WorkflowAuthoringDraft>): void { this.authoring.update((value) => ({ ...value, ...patch })); this.validation.set(null); this.fieldErrors.set(validateWorkflowDraft({ ...this.authoring(), ...patch })); }
  setSteps(steps: ReadonlyArray<WorkflowStep>): void { this.setAuthoring({ steps }); }
  setInputFields(inputFields: WorkflowAuthoringDraft['inputFields']): void { this.setAuthoring({ inputFields }); }
  localValidation(): Readonly<Record<string, string>> { const errors = validateWorkflowDraft(this.authoring()); this.fieldErrors.set(errors); return errors; }

  addStep(kind: WorkflowStepKind): void { if (this.authoring().steps.length >= 8) return; this.setSteps([...this.authoring().steps, newWorkflowStep(kind, this.authoring().inputFields)]); }
  moveStep(index: number, direction: -1 | 1): void { const next = index + direction; const steps = [...this.authoring().steps]; if (index < 0 || index >= steps.length || next < 0 || next >= steps.length) return; [steps[index], steps[next]] = [steps[next], steps[index]]; this.setSteps(steps); }
  cloneStep(index: number): void { if (this.authoring().steps.length >= 8) return; const steps = [...this.authoring().steps]; const step = steps[index]; if (!step) return; steps.splice(index + 1, 0, cloneWorkflowStep(step)); this.setSteps(steps); }
  removeStep(index: number): void { this.setSteps(this.authoring().steps.filter((_, stepIndex) => stepIndex !== index)); }

  async createDraft(): Promise<WorkflowDraft | null> { const session = this.sessionEpoch; const value = await this.mutate('create', JSON.stringify(this.authoring()), async (key) => { const errors = this.localValidation(); if (Object.keys(errors).length) return null; return this.api.createDraft(this.authoring(), key); }, true); if (value && this.active(session)) { this.selectedWorkflow.set(null); this.revision.set(null); this.instances.set([]); this.selectedInstance.set(null); this.terminalDeletionTarget.set(null); this.draft.set(value); this.authoring.set({ displayName: value.displayName, inputFields: value.inputFields, steps: value.steps }); await this.inspect(value.workflowId, true); } return value; }
  async cloneDraft(): Promise<WorkflowDraft | null> { const workflow = this.selectedWorkflow(); const revision = this.revision(); if (!workflow || !revision) return null; const session = this.sessionEpoch; const value = await this.mutate(`clone:${workflow.id}:${revision.id}`, String(workflow.version), (key) => this.api.cloneDraft(workflow.id, revision.id, workflow.version, key)); if (value && this.active(session)) { this.draft.set(value); this.authoring.set({ displayName: value.displayName, inputFields: value.inputFields, steps: value.steps }); } return value; }
  async saveDraft(): Promise<WorkflowDraft | null> { const draft = this.draft(); const workflow = this.selectedWorkflow(); if (!draft || !workflow) return null; const session = this.sessionEpoch; const value = await this.mutate(`save:${draft.id}`, JSON.stringify({ ...this.authoring(), version: draft.version }), async (key) => this.api.updateDraft(workflow.id, draft.id, { ...this.authoring(), expectedVersion: draft.version }, key)); if (value && this.active(session)) { this.draft.set(value); this.authoring.set({ displayName: value.displayName, inputFields: value.inputFields, steps: value.steps }); } return value; }
  async validate(): Promise<WorkflowValidationResult | null> { const draft = this.draft(); const workflow = this.selectedWorkflow(); if (!draft || !workflow) { this.localValidation(); return null; } const local = this.localValidation(); if (Object.keys(local).length) return null; const session = this.sessionEpoch; const result = await this.mutate(`validate:${draft.id}`, String(draft.version), (key) => this.api.validateDraft(workflow.id, draft.id, draft.version, key)); if (result && this.active(session)) { this.validation.set(result); this.fieldErrors.set(Object.fromEntries(result.issues.map((issue) => [issue.path, issue.code.replaceAll('_', ' ')]))); } return result; }
  async publish(): Promise<WorkflowRevision | null> { const draft = this.draft(); const workflow = this.selectedWorkflow(); if (!draft || !workflow) return null; const session = this.sessionEpoch; const result = await this.mutate(`publish:${draft.id}`, String(draft.version), (key) => this.api.publishDraft(workflow.id, draft.id, draft.version, key)); if (result && this.active(session)) this.revision.set(result); return result; }
  async start(input: Record<string, string>): Promise<WorkflowInstance | null> { const workflow = this.selectedWorkflow(); const revision = this.revision(); if (!workflow || !revision) return null; const session = this.sessionEpoch; const value = await this.mutate(`start:${workflow.id}:${revision.id}`, JSON.stringify(input), async (key) => this.api.startInstance(workflow.id, { revisionId: revision.id, input }, key)); if (value && this.active(session)) { this.selectedInstance.set(value); this.instances.update((page) => [{ ...value, completedEffects: value.completedEffects }, ...page.filter((item) => item.id !== value.id)]); } return value; }
  async inspectInstance(workflowId: string, instanceId: string): Promise<WorkflowInstance | null> { const session = this.sessionEpoch; const generation = ++this.detailEpoch; this.cancellationRequested.set(false); try { const value = await this.api.getInstance(workflowId, instanceId); if (!this.active(session, generation)) return null; this.selectedInstance.set(value); return value; } catch (error: unknown) { if (this.active(session, generation)) this.fail(error, 'This workflow run is unavailable.'); return null; } }
  async cancel(): Promise<WorkflowInstance | null> { const workflow = this.selectedWorkflow(); const instance = this.selectedInstance(); if (!workflow || !instance) return null; const session = this.sessionEpoch; this.cancellationRequested.set(true); const value = await this.mutate(`cancel:${instance.id}`, String(instance.version), (key) => this.api.cancelInstance(workflow.id, instance.id, instance.version, key)); if (value && this.active(session)) { this.selectedInstance.set(value); this.cancellationRequested.set(value.status === 'cancel_requested'); } return value; }
  async recover(action: 'request_replay' | 'settle_failed' | 'reconcile_completed' | 'reconcile_failed', stepExecutionId: string): Promise<WorkflowInstance | null> { const workflow = this.selectedWorkflow(); const instance = this.selectedInstance(); if (!workflow || !instance) return null; const session = this.sessionEpoch; const value = await this.mutate(`recover:${instance.id}:${stepExecutionId}`, `${action}:${instance.version}`, (key) => this.api.resolveInstance(workflow.id, instance.id, { action, stepExecutionId, expectedVersion: instance.version }, key)); if (value && this.active(session)) this.selectedInstance.set(value); return value; }
  async retireDefinition(): Promise<WorkflowDefinition | null> { const workflow = this.selectedWorkflow(); if (!workflow) return null; const session = this.sessionEpoch; const value = await this.mutate(`retire-definition:${workflow.id}`, String(workflow.version), (key) => this.api.retireDefinition(workflow.id, workflow.version, key)); if (value && this.active(session)) { this.selectedWorkflow.set(value); this.replaceWorkflow(value); } return value; }
  async retireRevision(): Promise<WorkflowRevision | null> { const workflow = this.selectedWorkflow(); const revision = this.revision(); if (!workflow || !revision) return null; const session = this.sessionEpoch; const value = await this.mutate(`retire-revision:${revision.id}`, String(revision.version), (key) => this.api.retireRevision(workflow.id, revision.id, revision.version, key)); if (value && this.active(session)) this.revision.set(value); return value; }
  async delete(scope: WorkflowDeletionScope): Promise<WorkflowDeletionReceipt | null> { const workflow = this.selectedWorkflow(); const draft = this.draft(); const revision = this.revision(); const instance = this.selectedInstance(); const target = instance?.status === 'terminal' ? { workflowId: instance.workflowId, instanceId: instance.id, version: instance.version } : this.terminalDeletionTarget(); if (!workflow) return null; const targetId = scope === 'draft' ? draft?.id ?? null : scope === 'published_revision' ? revision?.id ?? null : scope.startsWith('terminal_instance_') ? target?.instanceId ?? null : null; const version = scope === 'draft' ? draft?.version : scope === 'published_revision' ? revision?.version : scope.startsWith('terminal_instance_') ? target?.version : workflow.version; if (scope === 'draft' && !draft || scope === 'published_revision' && (!revision || revision.status !== 'retired') || scope.startsWith('terminal_instance_') && !target) return null; const session = this.sessionEpoch; const value = await this.mutate(`delete:${scope}:${targetId ?? workflow.id}`, `${version}`, (key) => this.api.delete(scope, workflow.id, targetId, version ?? 0, key)); if (value && this.active(session)) { if (scope === 'draft') this.draft.set(null); if (scope.startsWith('terminal_instance_') && target) this.terminalDeletionTarget.set(target); if (scope === 'terminal_instance_state') this.selectedInstance.set(null); await this.reconcile(); } return value; }

  startLive(): void {
    if (!this.api.stream || this.stopStream || this.refreshInFlight) return;
    const session = this.sessionEpoch;
    if (this.queuedInvalidation) {
      this.connectionState.set('reconnecting');
      this.refreshInFlight = this.flushInvalidations();
      void this.refreshInFlight.then(() => { if (this.active(session) && !this.stopStream && !this.refreshInFlight && !this.streamRetry) { this.lastEventId.set(null); this.startLive(); } });
      return;
    }
    this.connectionState.set('connecting');
    this.stopStream = this.api.stream((event) => { if (this.active(session)) this.onEvent(event); }, (error) => { if (!this.active(session)) return; this.stopStream = null; void this.handleStreamError(error); }, this.lastEventId() ?? undefined);
  }
  stopLive(): void { this.stopStream?.(); this.stopStream = null; if (this.streamRetry) { clearTimeout(this.streamRetry); this.streamRetry = null; } this.streamRetryCount = 0; this.connectionState.set('paused'); }
  async reconcile(): Promise<void> { await this.reconcileVisible({ ...visibleWorkflowInvalidation(Boolean(this.selectedWorkflow()), Boolean(this.selectedInstance())), cursor: null }); }
  resetSessionState(): void { this.sessionEpoch += 1; this.listEpoch += 1; this.detailEpoch += 1; this.agentLoadEpoch += 1; this.agentsSessionEstablished = false; this.mutationEpoch += 1; this.mutations.clear(); this.pendingEventIds.clear(); this.processedEventIds.clear(); this.queuedInvalidation = null; this.refreshInFlight = null; this.api.clearSession?.(); this.stopLive(); this.workflows.set(emptyPage()); this.agents.set([]); this.selectedWorkflow.set(null); this.draft.set(null); this.authoring.set(emptyWorkflowDraft()); this.revision.set(null); this.instances.set([]); this.selectedInstance.set(null); this.terminalDeletionTarget.set(null); this.cancellationRequested.set(false); this.validation.set(null); this.fieldErrors.set({}); this.status.set('idle'); this.notice.set(null); this.errorStatus.set(null); this.mutationOutcome.set('idle'); this.lastEventId.set(null); this.resetEpoch.update((value) => value + 1); }

  private onEvent(event: WorkflowStreamEvent): void {
    if (event.eventType === 'heartbeat') { this.connectionState.set('live'); return; }
    const eventId = 'eventId' in event ? event.eventId : null;
    if (eventId && this.processedEventIds.has(eventId)) {
      // A replayed event has already completed every required REST read. It
      // is safe to advance the persisted replay cursor without launching a
      // second refresh, which keeps reconnects bounded while retaining the
      // normal "REST first, cursor second" ordering.
      this.lastEventId.set(eventId);
      return;
    }
    if (eventId && this.pendingEventIds.has(eventId)) return;
    if (eventId) this.pendingEventIds.add(eventId);
    this.connectionState.set('live');
    this.queueProjection({ refreshList: true, refreshWorkflow: this.selectedWorkflow()?.id === event.workflowId, refreshInstance: 'instanceId' in event && this.selectedInstance()?.id === event.instanceId && this.selectedInstance()?.workflowId === event.workflowId }, eventId);
    if (!this.refreshInFlight) this.refreshInFlight = this.flushInvalidations();
  }
  private queueProjection(projection: Omit<WorkflowInvalidationProjection, 'cursor'>, cursor: string | null): void { this.queuedInvalidation = mergeWorkflowInvalidation(this.queuedInvalidation, projection, cursor); }
  private async flushInvalidations(): Promise<void> {
    const session = this.sessionEpoch;
    while (this.queuedInvalidation) {
      const projection = this.queuedInvalidation;
      this.queuedInvalidation = null;
      const batchEventIds = new Set(this.pendingEventIds);
      const reconciled = await this.reconcileVisible(projection);
      if (!this.active(session)) { this.refreshInFlight = null; return; }
      if (!reconciled) {
        const queuedCursor = (this.queuedInvalidation as WorkflowInvalidationProjection | null)?.cursor ?? projection.cursor;
        this.queueProjection(projection, queuedCursor);
        this.refreshInFlight = null;
        this.stopStream?.();
        this.stopStream = null;
        this.connectionState.set('reconnecting');
        this.scheduleReconnect();
        return;
      }
      this.markProcessed(batchEventIds);
      if (projection.cursor) this.lastEventId.set(projection.cursor);
    }
    this.refreshInFlight = null;
    // An EOF/error retry can fire while the authoritative refresh is still
    // pending. In that case startLive() correctly refused to open a second
    // stream, but the consumed timer would otherwise leave the store stuck in
    // reconnecting forever. Resume only after all required reads succeed and
    // only when no replacement stream or retry timer exists.
    if (this.active(session) && this.connectionState() === 'reconnecting' && !this.stopStream && !this.streamRetry) this.startLive();
  }
  private markProcessed(eventIds: ReadonlySet<string>): void {
    for (const eventId of eventIds) {
      this.pendingEventIds.delete(eventId);
      this.processedEventIds.add(eventId);
    }
    while (this.processedEventIds.size > 64) {
      const oldest = this.processedEventIds.values().next().value;
      if (typeof oldest !== 'string') break;
      this.processedEventIds.delete(oldest);
    }
  }
  private async reconcileVisible(projection: WorkflowInvalidationProjection): Promise<boolean> { const session = this.sessionEpoch; const workflowId = this.selectedWorkflow()?.id; const instanceId = this.selectedInstance()?.id; const preserveTerminalTarget = Boolean(this.terminalDeletionTarget()); const preserveSelectedInstance = Boolean(instanceId) || projection.refreshInstance; const listOk = projection.refreshList ? await this.load() : true; if (!listOk || !this.active(session)) return false; const workflowOk = projection.refreshWorkflow && workflowId ? Boolean(await this.inspect(workflowId, false, preserveSelectedInstance, preserveTerminalTarget)) : true; if (!workflowOk || !this.active(session)) return false; const instanceOk = projection.refreshInstance && workflowId && instanceId ? Boolean(await this.inspectInstance(workflowId, instanceId)) : true; if (!instanceOk || !this.active(session)) return false; this.mutationOutcome.set('reconciled'); return true; }
  private async handleStreamError(error: WorkflowStreamError): Promise<void> { if (error.status === 401 || error.status === 403) { this.resetSessionState(); this.errorStatus.set(error.status); this.status.set('error'); this.notice.set(error.status === 401 ? 'Your session has expired. Sign in again.' : 'You do not have permission to view this workflow.'); return; } if (this.streamRetryCount >= 5) { this.connectionState.set('unavailable'); this.notice.set('Live workflow updates stopped after several attempts.'); return; } this.connectionState.set('reconnecting'); const session = this.sessionEpoch; if (error.status === 404 || error.status === 410) { this.pendingEventIds.clear(); this.processedEventIds.clear(); this.lastEventId.set(null); this.queueProjection(visibleWorkflowInvalidation(Boolean(this.selectedWorkflow()), Boolean(this.selectedInstance())), null); if (!this.refreshInFlight) this.refreshInFlight = this.flushInvalidations(); const refresh = this.refreshInFlight; if (refresh) await refresh; if (!this.active(session)) return; } if (!this.active(session)) return; this.scheduleReconnect(); }
  private scheduleReconnect(): void { if (this.streamRetry || this.stopStream || this.streamRetryCount >= 5) return; const delay = [500, 1000, 2000, 4000, 8000][this.streamRetryCount] ?? 8000; this.streamRetryCount += 1; this.streamRetry = setTimeout(() => { this.streamRetry = null; this.startLive(); }, delay); }
  private replaceWorkflow(value: WorkflowDefinition): void { this.workflows.update((page) => ({ ...page, items: page.items.map((item) => item.id === value.id ? value : item) })); }
  private async mutate<T>(operation: string, fingerprint: string, action: (key: string) => Promise<T | null>, allowUnknownRetry = false): Promise<T | null> { const existing = this.mutations.get(operation); if (existing && existing.fingerprint === fingerprint && existing.epoch === this.sessionEpoch && this.mutationOutcome() === 'unknown' && !allowUnknownRetry) { this.notice.set('The previous action has an unknown outcome. Reconcile the authoritative state before retrying this same action.'); return null; } const attempt = this.mutation(operation, fingerprint); const session = this.sessionEpoch; this.status.set('saving'); this.mutationOutcome.set('pending'); this.notice.set(null); try { const value = await action(attempt.key); if (!this.active(session)) return null; this.mutations.delete(operation); this.status.set('idle'); this.mutationOutcome.set('confirmed'); return value; } catch (error: unknown) { if (this.active(session)) { const status = statusOf(error); if (status === 401 || status === 403) { this.resetSessionState(); this.errorStatus.set(status); this.status.set('error'); this.notice.set(this.message(error, 'Your session is no longer authorized.')); } else { const unknown = isUnknownWorkflowOutcome(error); this.mutationOutcome.set(unknown ? 'unknown' : 'failed'); this.status.set(unknown ? 'stale' : status === 503 ? 'unavailable' : 'error'); this.notice.set(unknown ? 'The mutation has an unknown outcome. Reconcile the latest workflow state before retrying; the same request key is retained.' : this.message(error, 'The workflow action could not be completed.')); this.errorStatus.set(status); } } return null; } }
  private mutation(operation: string, fingerprint: string): Mutation { const existing = this.mutations.get(operation); if (existing && existing.fingerprint === fingerprint && existing.epoch === this.sessionEpoch) return existing; const next = { key: newKey(), fingerprint, epoch: this.sessionEpoch }; this.mutations.set(operation, next); return next; }
  private fail(error: unknown, fallback: string): void { const status = statusOf(error); if (status === 401 || status === 403) { this.resetSessionState(); this.status.set('error'); this.errorStatus.set(status); this.notice.set(this.message(error, fallback)); return; } this.status.set(status === 503 ? 'unavailable' : 'error'); this.errorStatus.set(status); this.notice.set(this.message(error, fallback)); }
  private active(session: number, generation?: number): boolean { return session === this.sessionEpoch && (generation === undefined || generation === this.detailEpoch); }
  private message(error: unknown, fallback: string): string { const status = statusOf(error); if (status === 401) return 'Your session has expired. Sign in again.'; if (status === 403) return 'You do not have permission to view this workflow.'; if (status === 404) return 'This workflow is no longer available.'; if (status === 409) return 'This workflow changed elsewhere. Refresh before trying again.'; if (status === 503) return 'Workflow publishing or starting is temporarily unavailable.'; return fallback; }
}
