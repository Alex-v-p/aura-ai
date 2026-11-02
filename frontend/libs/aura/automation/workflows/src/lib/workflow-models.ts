import { InjectionToken } from '@angular/core';
import type {
  CreateWorkflowDraftRequest,
  WorkflowDefinition,
  WorkflowDefinitionPage,
  AgentCollection,
  WorkflowDeletionReceipt,
  WorkflowDeletionScope,
  WorkflowDraft,
  WorkflowInstance,
  WorkflowInstancePage,
  WorkflowInputFieldDefinition,
  WorkflowInputType,
  UpdateWorkflowDraftRequest,
  WorkflowRevision,
  WorkflowStep,
  WorkflowValidationResult,
  WorkflowStreamEvent,
  StartWorkflowInstanceRequest,
  ResolveWorkflowInstanceRequest,
} from '@aura/aura-api-client';

export type {
  CreateWorkflowDraftRequest,
  WorkflowDefinition,
  WorkflowDefinitionPage,
  AgentCollection,
  WorkflowDeletionReceipt,
  WorkflowDeletionScope,
  WorkflowDraft,
  WorkflowInstance,
  WorkflowInstanceInput,
  WorkflowInstancePage,
  WorkflowInputFieldDefinition,
  WorkflowInputType,
  WorkflowSelector,
  UpdateWorkflowDraftRequest,
  WorkflowRevision,
  WorkflowStep,
  WorkflowValidationIssue,
  WorkflowValidationResult,
  WorkflowVersionCommand,
  WorkflowStreamEvent,
  StartWorkflowInstanceRequest,
  ResolveWorkflowInstanceRequest,
} from '@aura/aura-api-client';

export class WorkflowStreamError extends Error {
  constructor(
    readonly status: number,
    message: string,
    readonly reason: 'http' | 'eof' | 'protocol' | 'overflow',
  ) {
    super(message);
    this.name = 'WorkflowStreamError';
  }
}

const STREAM_EVENT_TYPES = new Set([
  'workflow.revision.published', 'workflow.revision.retired', 'workflow.definition.retired',
  'workflow.instance.admitted', 'workflow.instance.status_changed', 'workflow.instance.terminal',
  'workflow.step.status_changed', 'workflow.step.reconciled', 'workflow.retention.deleted', 'heartbeat',
]);
const INSTANCE_STATUSES = new Set(['queued', 'running', 'waiting_for_time', 'waiting_for_approval', 'waiting_for_resolution', 'resolution_required', 'cancel_requested', 'terminal']);
const TERMINAL_OUTCOMES = new Set(['completed', 'canceled', 'failed']);
const STEP_KINDS = new Set(['agent_run', 'owner_approval', 'owner_notification', 'wait_until']);
const STEP_STATUSES = new Set(['pending', 'running', 'waiting_for_time', 'waiting_for_approval', 'failed_replayable', 'resolution_required', 'completed', 'rejected', 'expired', 'canceled', 'failed']);
const EFFECT_OUTCOMES = new Set(['not_attempted', 'completed', 'definitively_failed', 'unknown']);
const RECOVERY_CAPABILITIES = new Set(['request_replay', 'settle_failed', 'reconcile', 'cancel']);
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const RFC3339 = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d{1,9})?(Z|[+-]\d{2}:\d{2})$/;
export const workflowReturnPath = (returnTo: string): string => returnTo.startsWith('/') && !returnTo.startsWith('//') && returnTo.length <= 2048 ? returnTo : '/workflows';
export const workflowLoginHref = (returnTo = '/workflows', loginPath = '/api/v1/auth/login'): string => `${loginPath}?${new URLSearchParams({ return_to: workflowReturnPath(returnTo) }).toString()}`;
const uuid = (value: unknown): value is string => typeof value === 'string' && UUID.test(value);
const timestamp = (value: unknown): value is string => {
  if (typeof value !== 'string') return false;
  const match = RFC3339.exec(value);
  if (!match) return false;
  const year = Number(match[1]); const month = Number(match[2]); const day = Number(match[3]);
  const hour = Number(match[4]); const minute = Number(match[5]); const second = Number(match[6]);
  const offset = match[7]; const offsetHour = offset === 'Z' ? 0 : Number(offset.slice(1, 3)); const offsetMinute = offset === 'Z' ? 0 : Number(offset.slice(4, 6));
  const leap = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
  const daysInMonth = [31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1] ?? 0;
  return month >= 1 && month <= 12 && day >= 1 && day <= daysInMonth && hour <= 23 && minute <= 59 && second <= 59 && offsetHour <= 23 && offsetMinute <= 59;
};
const positiveInteger = (value: unknown): value is number => typeof value === 'number' && Number.isSafeInteger(value) && value > 0;
const ordinal = (value: unknown): value is number => positiveInteger(value) && value <= 8;
const nullableOrdinal = (value: unknown): value is number | null => value === null || ordinal(value);
const DELETION_SCOPES = new Set(['draft', 'retired_definition', 'published_revision', 'terminal_instance_state', 'terminal_instance_history', 'terminal_instance_result', 'terminal_instance_checkpoints']);
const onlyKeys = (value: StreamEventRecord, keys: ReadonlyArray<string>): boolean => Object.keys(value).every((key) => keys.includes(key));
type StreamEventRecord = Record<string, unknown> & { readonly schemaVersion?: unknown; readonly eventId?: unknown; readonly workflowId?: unknown; readonly occurredAt?: unknown; readonly eventType?: unknown; readonly revisionId?: unknown; readonly version?: unknown; readonly instanceId?: unknown; readonly instanceStatus?: unknown; readonly terminalOutcome?: unknown; readonly currentStepOrdinal?: unknown; readonly stepExecutionId?: unknown; readonly stepOrdinal?: unknown; readonly stepKind?: unknown; readonly stepStatus?: unknown; readonly effectOutcome?: unknown; readonly recoveryCapabilities?: unknown; readonly instanceVersion?: unknown; readonly deletionScope?: unknown; readonly tombstoneRetained?: unknown; };
const baseEvent = (value: StreamEventRecord): boolean => value.schemaVersion === 1 && uuid(value.eventId) && uuid(value.workflowId) && timestamp(value.occurredAt);

export function isValidWorkflowStreamEvent(value: unknown): value is WorkflowStreamEvent {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false;
  const event = value as StreamEventRecord;
  if (event.eventType === 'heartbeat') return event.schemaVersion === 1 && timestamp(event.occurredAt) && Object.keys(event).every((key) => key === 'schemaVersion' || key === 'eventType' || key === 'occurredAt');
  if (!STREAM_EVENT_TYPES.has(String(event.eventType)) || !baseEvent(event)) return false;
  if (event.eventType === 'workflow.revision.published' || event.eventType === 'workflow.revision.retired' || event.eventType === 'workflow.definition.retired') return onlyKeys(event, ['schemaVersion', 'eventId', 'eventType', 'workflowId', 'revisionId', 'version', 'occurredAt']) && (event.eventType === 'workflow.definition.retired' ? event.revisionId === null : uuid(event.revisionId)) && positiveInteger(event.version);
  if (event.eventType === 'workflow.instance.admitted' || event.eventType === 'workflow.instance.status_changed' || event.eventType === 'workflow.instance.terminal') return onlyKeys(event, ['schemaVersion', 'eventId', 'eventType', 'workflowId', 'revisionId', 'instanceId', 'instanceStatus', 'terminalOutcome', 'currentStepOrdinal', 'version', 'occurredAt']) && uuid(event.revisionId) && uuid(event.instanceId) && INSTANCE_STATUSES.has(String(event.instanceStatus)) && (event.terminalOutcome === null || TERMINAL_OUTCOMES.has(String(event.terminalOutcome))) && nullableOrdinal(event.currentStepOrdinal) && positiveInteger(event.version);
  if (event.eventType === 'workflow.step.status_changed' || event.eventType === 'workflow.step.reconciled') { const capabilities = event.recoveryCapabilities; return onlyKeys(event, ['schemaVersion', 'eventId', 'eventType', 'workflowId', 'revisionId', 'instanceId', 'stepExecutionId', 'stepOrdinal', 'stepKind', 'stepStatus', 'effectOutcome', 'recoveryCapabilities', 'instanceVersion', 'occurredAt']) && uuid(event.revisionId) && uuid(event.instanceId) && uuid(event.stepExecutionId) && ordinal(event.stepOrdinal) && STEP_KINDS.has(String(event.stepKind)) && STEP_STATUSES.has(String(event.stepStatus)) && EFFECT_OUTCOMES.has(String(event.effectOutcome)) && Array.isArray(capabilities) && capabilities.length <= 4 && new Set(capabilities.map(String)).size === capabilities.length && capabilities.every((item) => RECOVERY_CAPABILITIES.has(String(item))) && positiveInteger(event.instanceVersion); }
  if (event.eventType === 'workflow.retention.deleted') return onlyKeys(event, ['schemaVersion', 'eventId', 'eventType', 'workflowId', 'revisionId', 'instanceId', 'deletionScope', 'tombstoneRetained', 'version', 'occurredAt']) && (event.revisionId === null || uuid(event.revisionId)) && (event.instanceId === null || uuid(event.instanceId)) && DELETION_SCOPES.has(String(event.deletionScope)) && typeof event.tombstoneRetained === 'boolean' && positiveInteger(event.version);
  return false;
}

export type WorkflowStudioStatus = 'idle' | 'loading' | 'saving' | 'stale' | 'unavailable' | 'error';
export type WorkflowMutationOutcome = 'idle' | 'pending' | 'confirmed' | 'failed' | 'unknown' | 'reconciled';

export interface WorkflowInvalidationProjection {
  refreshList: boolean;
  refreshWorkflow: boolean;
  refreshInstance: boolean;
  cursor: string | null;
}

export function mergeWorkflowInvalidation(current: WorkflowInvalidationProjection | null, projection: Omit<WorkflowInvalidationProjection, 'cursor'>, cursor: string | null): WorkflowInvalidationProjection {
  const merged = current ?? { refreshList: false, refreshWorkflow: false, refreshInstance: false, cursor: null };
  return { refreshList: merged.refreshList || projection.refreshList, refreshWorkflow: merged.refreshWorkflow || projection.refreshWorkflow, refreshInstance: merged.refreshInstance || projection.refreshInstance, cursor };
}

export function visibleWorkflowInvalidation(hasSelectedWorkflow: boolean, hasSelectedInstance: boolean): Omit<WorkflowInvalidationProjection, 'cursor'> {
  return { refreshList: true, refreshWorkflow: hasSelectedWorkflow, refreshInstance: hasSelectedInstance };
}

export function shouldCommitWorkflowAgentLoad(requestSession: number, currentSession: number, requestGeneration: number, currentGeneration: number, sessionEstablished: boolean): boolean {
  return sessionEstablished && requestSession === currentSession && requestGeneration === currentGeneration;
}

export interface WorkflowStudioApi {
  clearSession?(): void;
  establishSession?(): Promise<void>;
  listAgents?(): Promise<AgentCollection>;
  list(): Promise<WorkflowDefinitionPage>;
  get(id: string): Promise<WorkflowDefinition>;
  createDraft(request: CreateWorkflowDraftRequest, idempotencyKey: string): Promise<WorkflowDraft>;
  cloneDraft(workflowId: string, revisionId: string, expectedDefinitionVersion: number, idempotencyKey: string): Promise<WorkflowDraft>;
  getDraft(workflowId: string, draftId: string): Promise<WorkflowDraft>;
  updateDraft(workflowId: string, draftId: string, request: UpdateWorkflowDraftRequest, idempotencyKey: string): Promise<WorkflowDraft>;
  validateDraft(workflowId: string, draftId: string, expectedVersion: number, idempotencyKey: string): Promise<WorkflowValidationResult>;
  publishDraft(workflowId: string, draftId: string, expectedVersion: number, idempotencyKey: string): Promise<WorkflowRevision>;
  getRevision(workflowId: string, revisionId: string): Promise<WorkflowRevision>;
  retireDefinition(workflowId: string, expectedVersion: number, idempotencyKey: string): Promise<WorkflowDefinition>;
  retireRevision(workflowId: string, revisionId: string, expectedVersion: number, idempotencyKey: string): Promise<WorkflowRevision>;
  listInstances(workflowId: string): Promise<WorkflowInstancePage>;
  startInstance(workflowId: string, request: StartWorkflowInstanceRequest, idempotencyKey: string): Promise<WorkflowInstance>;
  getInstance(workflowId: string, instanceId: string): Promise<WorkflowInstance>;
  cancelInstance(workflowId: string, instanceId: string, expectedVersion: number, idempotencyKey: string): Promise<WorkflowInstance>;
  resolveInstance(workflowId: string, instanceId: string, request: ResolveWorkflowInstanceRequest, idempotencyKey: string): Promise<WorkflowInstance>;
  delete(scope: WorkflowDeletionScope, workflowId: string, targetId: string | null, expectedVersion: number, idempotencyKey: string): Promise<WorkflowDeletionReceipt>;
  stream?(onEvent: (event: WorkflowStreamEvent) => void, onError: (error: WorkflowStreamError) => void, lastEventId?: string): () => void;
}

export const WORKFLOW_STUDIO_API = new InjectionToken<WorkflowStudioApi>('AURA_WORKFLOW_STUDIO_API');

export interface WorkflowAuthoringDraft {
  readonly displayName: string;
  readonly inputFields: ReadonlyArray<WorkflowInputFieldDefinition>;
  readonly steps: ReadonlyArray<WorkflowStep>;
}

export const emptyWorkflowDraft = (): WorkflowAuthoringDraft => ({ displayName: '', inputFields: [], steps: [] });

export const WORKFLOW_STEP_KINDS = ['agent_run', 'owner_approval', 'owner_notification', 'wait_until'] as const;
export type WorkflowStepKind = (typeof WORKFLOW_STEP_KINDS)[number];

const boundedName = /^[A-Za-z][A-Za-z0-9_]{0,31}$/;
const MAX_STEPS = 8;

export function newWorkflowStep(kind: WorkflowStepKind, inputFields: ReadonlyArray<WorkflowInputFieldDefinition> = []): WorkflowStep {
  const field = inputFields[0]?.name ?? '';
  if (kind === 'agent_run') return { kind, agentRevisionId: '', instructions: '', input: { source: 'instance_input', field } };
  if (kind === 'owner_notification') return { kind, content: { source: 'instance_input', field } };
  if (kind === 'wait_until') return { kind, dueAt: { source: 'instance_input', field } };
  return { kind };
}

export function cloneWorkflowStep(step: WorkflowStep): WorkflowStep { return JSON.parse(JSON.stringify(step)) as WorkflowStep; }

export function validateWorkflowDraft(draft: WorkflowAuthoringDraft): Readonly<Record<string, string>> {
  const errors: Record<string, string> = {};
  const names = new Set<string>();
  const inputFields = new Map<string, WorkflowInputType>();
  if (!draft.displayName.trim()) errors['displayName'] = 'Give this workflow a name.';
  else if (draft.displayName.trim().length > 80) errors['displayName'] = 'Use 80 characters or fewer.';
  if (draft.steps.length < 1) errors['steps'] = 'Add at least one step.';
  if (draft.steps.length > MAX_STEPS) errors['steps'] = 'Workflows can contain at most eight ordered steps.';
  draft.inputFields.forEach((field, index) => {
    const path = `inputFields.${index}`;
    if (!boundedName.test(field.name)) errors[`${path}.name`] = 'Use a letter followed by letters, numbers, or underscores.';
    else if (names.has(field.name)) errors[`${path}.name`] = 'Input names must be unique.';
    names.add(field.name); inputFields.set(field.name, field.valueType);
    if (field.valueType !== 'bounded_text' && field.valueType !== 'instant') errors[`${path}.valueType`] = 'Choose a supported input type.';
  });
  let agentRuns = 0;
  draft.steps.forEach((step, index) => {
    const path = `steps.${index}`;
    if (!WORKFLOW_STEP_KINDS.includes(step.kind as WorkflowStepKind)) { errors[path] = 'Choose one of the four supported step types.'; return; }
    if (step.kind === 'agent_run') {
      agentRuns += 1;
      if (!step.agentRevisionId.trim()) errors[`${path}.agentRevisionId`] = 'Choose an agent revision.';
      if (!step.instructions.trim()) errors[`${path}.instructions`] = 'Add instructions for the agent run.';
      selectorError(step.input, path, inputFields, draft.steps, index, errors);
    } else if (step.kind === 'owner_notification') selectorError(step.content, path, inputFields, draft.steps, index, errors, 'bounded_text');
    else if (step.kind === 'wait_until') selectorError(step.dueAt, path, inputFields, draft.steps, index, errors, 'instant');
  });
  if (agentRuns > 1) errors['steps'] = 'A workflow can contain at most one agent run.';
  return errors;
}

function selectorError(selector: unknown, path: string, inputFields: ReadonlyMap<string, WorkflowInputType>, steps: ReadonlyArray<WorkflowStep>, stepIndex: number, errors: Record<string, string>, requiredType?: WorkflowInputType): void {
  if (!selector || typeof selector !== 'object') { errors[`${path}.mapping`] = 'Choose a mapping.'; return; }
  const value = selector as { readonly source?: unknown; readonly field?: unknown; readonly stepOrdinal?: unknown };
  if (value.source === 'instance_input') {
    if (typeof value.field !== 'string' || !inputFields.has(value.field)) errors[`${path}.mapping`] = 'Choose an input field that exists.';
    else if (requiredType && inputFields.get(value.field) !== requiredType) errors[`${path}.mapping`] = requiredType === 'instant' ? 'Choose an instant input.' : 'Choose a bounded text input.';
  } else if (value.source === 'step_output') {
    if (!Number.isInteger(value.stepOrdinal) || Number(value.stepOrdinal) < 1 || Number(value.stepOrdinal) > stepIndex) errors[`${path}.mapping`] = 'Mappings may reference only an earlier step.';
    else { const prior = steps[Number(value.stepOrdinal) - 1]; const allowed = prior?.kind === 'agent_run' ? ['output'] : prior?.kind === 'owner_approval' ? ['decision', 'decided_at', 'status'] : prior?.kind === 'owner_notification' ? ['notification_id', 'status'] : prior?.kind === 'wait_until' ? ['resumed_at'] : []; if (!allowed.includes(String(value.field))) errors[`${path}.mapping`] = 'Choose an output from the referenced earlier step.'; else if (requiredType === 'instant' && value.field !== 'resumed_at') errors[`${path}.mapping`] = 'Choose an instant output from an earlier wait step.'; else if (requiredType === 'bounded_text' && value.field === 'resumed_at') errors[`${path}.mapping`] = 'Choose a bounded text output.'; }
  } else errors[`${path}.mapping`] = 'Choose an input or earlier step output.';
}

export function workflowStatusLabel(status: string): string { return status.replaceAll('_', ' ').replace(/\b\w/g, (letter) => letter.toUpperCase()); }
export function workflowStepKindLabel(kind: WorkflowStepKind): string { return kind === 'agent_run' ? 'Agent run' : kind === 'owner_approval' ? 'Owner approval' : kind === 'owner_notification' ? 'Owner notification' : 'Wait until'; }
export function isUnknownWorkflowOutcome(error: unknown): boolean { const status = (error as { readonly status?: unknown })?.status; return typeof status !== 'number' || status === 0 || status >= 500; }
