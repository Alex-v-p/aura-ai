import { Injectable } from '@angular/core';
import { AURA_API_OPERATIONS, AuraApiClient, type AuraApiOperations, type AuraOperationDescriptor, type AuraOperationId, type AuraOperationInput, type AuraOperationResponse } from '@aura/aura-api-client';
import type { CreateWorkflowDraftRequest, ResolveWorkflowInstanceRequest, StartWorkflowInstanceRequest, WorkflowDeletionScope, WorkflowStreamEvent, WorkflowStudioApi, WorkflowStreamError, UpdateWorkflowDraftRequest } from './workflow-models';
import { isValidWorkflowStreamEvent, workflowLoginHref as buildWorkflowLoginHref, WorkflowStreamError as StreamError } from './workflow-models';

type EmptyInput = { readonly path: Record<string, never>; readonly query: Record<string, never>; readonly headers: Record<string, never>; readonly body: never };
const emptyInput = (): EmptyInput => ({ path: {}, query: {}, headers: {}, body: undefined as never });
export const workflowApiLoginHref = (returnTo = '/workflows'): string => buildWorkflowLoginHref(returnTo, AURA_API_OPERATIONS.startLogin.pathTemplate);
const key = (): string => {
  if (typeof crypto === 'undefined' || typeof crypto.randomUUID !== 'function') throw new WorkflowApiError(0, 'Secure mutation identity is unavailable.');
  return crypto.randomUUID();
};

@Injectable({ providedIn: 'root' })
export class AuraWorkflowApi implements WorkflowStudioApi {
  private readonly client = new AuraApiClient(new GeneratedWorkflowTransport());
  private csrf = '';
  private sessionEpoch = 0;

  clearSession(): void { this.csrf = ''; this.sessionEpoch += 1; }
  async establishSession(): Promise<void> { await this.ensureSession(); }
  async list() { return this.client.execute('listWorkflows', emptyInput()); }
  async listAgents() { return this.client.execute('listAgents', emptyInput()); }
  async get(id: string) { return this.client.execute('getWorkflow', { ...emptyInput(), path: { workflow_id: id } }); }
  async createDraft(request: CreateWorkflowDraftRequest, idempotencyKey: string) { await this.ensureSession(); return this.client.execute('createWorkflowDraft', { ...emptyInput(), headers: this.headers(idempotencyKey), body: request }); }
  async cloneDraft(workflowId: string, revisionId: string, expectedDefinitionVersion: number, idempotencyKey: string) { await this.ensureSession(); return this.client.execute('cloneWorkflowDraft', { ...emptyInput(), path: { workflow_id: workflowId }, headers: this.headers(idempotencyKey), body: { revisionId, expectedDefinitionVersion } }); }
  async getDraft(workflowId: string, draftId: string) { return this.client.execute('getWorkflowDraft', { ...emptyInput(), path: { workflow_id: workflowId, draft_id: draftId } }); }
  async updateDraft(workflowId: string, draftId: string, request: UpdateWorkflowDraftRequest, idempotencyKey: string) { await this.ensureSession(); return this.client.execute('updateWorkflowDraft', { ...emptyInput(), path: { workflow_id: workflowId, draft_id: draftId }, headers: this.headers(idempotencyKey), body: request }); }
  async validateDraft(workflowId: string, draftId: string, expectedVersion: number, idempotencyKey: string) { await this.ensureSession(); return this.client.execute('validateWorkflowDraft', { ...emptyInput(), path: { workflow_id: workflowId, draft_id: draftId }, headers: this.headers(idempotencyKey), body: { expectedVersion } }); }
  async publishDraft(workflowId: string, draftId: string, expectedVersion: number, idempotencyKey: string) { await this.ensureSession(); return this.client.execute('publishWorkflowDraft', { ...emptyInput(), path: { workflow_id: workflowId, draft_id: draftId }, headers: this.headers(idempotencyKey), body: { expectedVersion } }); }
  async getRevision(workflowId: string, revisionId: string) { return this.client.execute('getWorkflowRevision', { ...emptyInput(), path: { workflow_id: workflowId, revision_id: revisionId } }); }
  async retireDefinition(workflowId: string, expectedVersion: number, idempotencyKey: string) { await this.ensureSession(); return this.client.execute('retireWorkflowDefinition', { ...emptyInput(), path: { workflow_id: workflowId }, headers: this.headers(idempotencyKey), body: { expectedVersion } }); }
  async retireRevision(workflowId: string, revisionId: string, expectedVersion: number, idempotencyKey: string) { await this.ensureSession(); return this.client.execute('retireWorkflowRevision', { ...emptyInput(), path: { workflow_id: workflowId, revision_id: revisionId }, headers: this.headers(idempotencyKey), body: { expectedVersion } }); }
  async listInstances(workflowId: string) { return this.client.execute('listWorkflowInstances', { ...emptyInput(), path: { workflow_id: workflowId } }); }
  async startInstance(workflowId: string, request: StartWorkflowInstanceRequest, idempotencyKey: string) { await this.ensureSession(); return this.client.execute('startWorkflowInstance', { ...emptyInput(), path: { workflow_id: workflowId }, headers: this.headers(idempotencyKey), body: request }); }
  async getInstance(workflowId: string, instanceId: string) { return this.client.execute('getWorkflowInstance', { ...emptyInput(), path: { workflow_id: workflowId, instance_id: instanceId } }); }
  async cancelInstance(workflowId: string, instanceId: string, expectedVersion: number, idempotencyKey: string) { await this.ensureSession(); return this.client.execute('cancelWorkflowInstance', { ...emptyInput(), path: { workflow_id: workflowId, instance_id: instanceId }, headers: this.headers(idempotencyKey), body: { expectedVersion } }); }
  async resolveInstance(workflowId: string, instanceId: string, request: ResolveWorkflowInstanceRequest, idempotencyKey: string) { await this.ensureSession(); return this.client.execute('resolveWorkflowInstance', { ...emptyInput(), path: { workflow_id: workflowId, instance_id: instanceId }, headers: this.headers(idempotencyKey), body: request }); }
  async delete(scope: WorkflowDeletionScope, workflowId: string, targetId: string | null, expectedVersion: number, idempotencyKey: string) {
    await this.ensureSession();
    if (scope === 'draft') { if (!targetId) throw new WorkflowApiError(400, 'A draft target is required.'); return this.client.execute('deleteWorkflowDraft', { ...emptyInput(), path: { workflow_id: workflowId, draft_id: targetId }, headers: this.headers(idempotencyKey), body: { scope, expectedVersion } }); }
    if (scope === 'retired_definition') return this.client.execute('deleteWorkflowDefinition', { ...emptyInput(), path: { workflow_id: workflowId }, headers: this.headers(idempotencyKey), body: { scope, expectedVersion } });
    if (scope === 'published_revision') { if (!targetId) throw new WorkflowApiError(400, 'A published revision target is required.'); return this.client.execute('deleteWorkflowRevision', { ...emptyInput(), path: { workflow_id: workflowId, revision_id: targetId }, headers: this.headers(idempotencyKey), body: { scope, expectedVersion } }); }
    if (!targetId) throw new WorkflowApiError(400, 'A terminal instance target is required.');
    if (scope === 'terminal_instance_state') return this.client.execute('deleteWorkflowInstanceState', { ...emptyInput(), path: { workflow_id: workflowId, instance_id: targetId }, headers: this.headers(idempotencyKey), body: { scope, expectedVersion } });
    if (scope === 'terminal_instance_history') return this.client.execute('deleteWorkflowInstanceHistory', { ...emptyInput(), path: { workflow_id: workflowId, instance_id: targetId }, headers: this.headers(idempotencyKey), body: { scope, expectedVersion } });
    if (scope === 'terminal_instance_result') return this.client.execute('deleteWorkflowInstanceResult', { ...emptyInput(), path: { workflow_id: workflowId, instance_id: targetId }, headers: this.headers(idempotencyKey), body: { scope, expectedVersion } });
    return this.client.execute('deleteWorkflowInstanceCheckpoints', { ...emptyInput(), path: { workflow_id: workflowId, instance_id: targetId }, headers: this.headers(idempotencyKey), body: { scope, expectedVersion } });
  }

  stream(onEvent: (event: WorkflowStreamEvent) => void, onError: (error: WorkflowStreamError) => void, lastEventId?: string): () => void {
    const controller = new AbortController();
    const headers: Record<string, string> = { Accept: 'text/event-stream' };
    if (lastEventId) headers['Last-Event-ID'] = lastEventId;
    void fetch(AURA_API_OPERATIONS.streamWorkflowEvents.pathTemplate, { credentials: 'include', cache: 'no-store', headers, signal: controller.signal }).then(async (response) => {
      if (!response.ok) throw new StreamError(response.status, 'Workflow live updates are unavailable.', 'http');
      if (!response.body) throw new StreamError(0, 'The workflow stream returned no body.', 'protocol');
      const reader = response.body.getReader(); const decoder = new TextDecoder(); let buffer = '';
      while (!controller.signal.aborted) {
        const chunk = await reader.read(); if (chunk.done) break; const decoded = decoder.decode(chunk.value, { stream: true });
        if (buffer.length + decoded.length > 131072) throw new StreamError(0, 'The workflow stream exceeded its safety limit.', 'overflow');
        buffer += decoded;
        const records = buffer.split(/\r?\n\r?\n/); buffer = records.pop() ?? '';
        for (const record of records) {
          if (record.length > 65536) throw new StreamError(0, 'The workflow event exceeded its safety limit.', 'overflow');
          const fields = new Map<string, string>(); const data: string[] = [];
          for (const line of record.split(/\r?\n/)) { const separator = line.indexOf(':'); const name = separator < 0 ? line : line.slice(0, separator); const value = separator < 0 ? '' : line.slice(separator + 1).replace(/^ /, ''); if (name === 'data') data.push(value); else if (name === 'id' || name === 'event') fields.set(name, value); }
          if (!data.length) continue;
          let payload: unknown; try { payload = JSON.parse(data.join('\n')); } catch { continue; }
          if (!isValidWorkflowStreamEvent(payload)) continue;
          if (fields.get('id') && 'eventId' in payload && fields.get('id') !== payload.eventId) continue;
          onEvent(payload);
        }
      }
      if (!controller.signal.aborted) onError(new StreamError(0, 'The workflow stream disconnected.', 'eof'));
    }).catch((error: unknown) => { if (!controller.signal.aborted) onError(error instanceof StreamError ? error : new StreamError(0, 'Aura could not keep the workflow stream open.', 'protocol')); });
    return () => controller.abort();
  }

  private async ensureSession(): Promise<void> { if (this.csrf) return; const epoch = this.sessionEpoch; const session = await this.client.execute('getSession', emptyInput()); if (epoch !== this.sessionEpoch) throw new WorkflowApiError(401, 'Your session changed.'); this.csrf = session.csrfToken; }
  private headers(idempotencyKey: string): { readonly 'X-CSRF-Token': string; readonly 'Idempotency-Key': string } { return { 'X-CSRF-Token': this.csrf, 'Idempotency-Key': idempotencyKey || key() }; }
}

export class WorkflowApiError extends Error { constructor(readonly status: number, message: string) { super(message); this.name = 'WorkflowApiError'; } }

class GeneratedWorkflowTransport {
  async execute<K extends AuraOperationId>(descriptor: AuraOperationDescriptor, input: AuraOperationInput<K>): Promise<AuraOperationResponse<K>> {
    let path = descriptor.pathTemplate;
    for (const [name, value] of Object.entries(input.path)) path = path.replace(`{${name}}`, encodeURIComponent(String(value)));
    const query = new URLSearchParams(); for (const [name, value] of Object.entries(input.query)) if (value !== undefined && value !== null && value !== '') query.set(name, String(value));
    let response: Response;
    try { response = await fetch(`${path}${query.size ? `?${query.toString()}` : ''}`, { method: descriptor.method, credentials: 'include', cache: 'no-store', headers: { Accept: 'application/json', ...(input.body !== undefined ? { 'Content-Type': 'application/json' } : {}), ...input.headers }, body: input.body === undefined ? undefined : JSON.stringify(input.body) }); }
    catch { throw new WorkflowApiError(0, 'Workflow service is unavailable while you are offline.'); }
    if (!response.ok) { let message = 'Aura could not complete that workflow action.'; try { const body = await response.json() as { readonly detail?: unknown; readonly message?: unknown }; if (typeof body.message === 'string') message = body.message; else if (typeof body.detail === 'string') message = body.detail; } catch { /* bounded fallback */ } throw new WorkflowApiError(response.status, message); }
    return response.status === 204 ? undefined as AuraApiResponse<K> : await response.json() as AuraApiResponse<K>;
  }
}
type AuraApiResponse<K extends AuraOperationId> = AuraApiOperations[K]['response'];
