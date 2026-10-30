import { Injectable } from '@angular/core';
import { AURA_API_OPERATIONS, AuraApiClient, type AuraOperationDescriptor, type AuraOperationInput, type DurableTaskDetail, type AuraOperationId, type AuraOperationResponse } from '@aura/aura-api-client';

@Injectable({ providedIn: 'root' })
export class ConversationResearchActivityApi {
  private readonly client = new AuraApiClient(new ConversationTaskTransport());
  getTask(taskId: string): Promise<DurableTaskDetail> { return this.client.execute('getDurableTask', { path: { task_id: taskId }, query: {}, headers: {}, body: undefined } as AuraOperationInput<'getDurableTask'>); }
}

class ConversationTaskTransport {
  execute<K extends AuraOperationId>(descriptor: AuraOperationDescriptor, request: AuraOperationInput<K>): Promise<AuraOperationResponse<K>> { const input = request as AuraOperationInput<AuraOperationId>; let path = descriptor.pathTemplate; for (const [key, value] of Object.entries(input.path)) path = path.replace(`{${key}}`, encodeURIComponent(value)); const query = new URLSearchParams(); for (const [key, value] of Object.entries(input.query)) if (value !== undefined) query.set(key, String(value)); return fetch(`${path}${query.size ? `?${query}` : ''}`, { credentials: 'include', cache: 'no-store', headers: { Accept: 'application/json' } }).then(async response => { if (!response.ok) throw new Error(`Task snapshot unavailable (${response.status}).`); return (response.status === 204 ? undefined : await response.json()) as AuraOperationResponse<K>; }); }
}
