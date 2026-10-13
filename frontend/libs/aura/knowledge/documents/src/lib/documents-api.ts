import { Injectable } from '@angular/core';
import { AuraApiClient, AURA_API_OPERATIONS, type AuraOperationDescriptor, type AuraOperationId, type AuraOperationInput, type AuraOperationResponse, type CreateDocumentUploadRequest, type DocumentDetail, type DocumentPage, type DocumentSearchResultPage, type DocumentTransitionRequest, type DocumentUploadReceipt, type DocumentUploadSession, type Session } from '@aura/aura-api-client';
import { DOCUMENT_PREVIEW_MAX_CHARS, documentSearchRequest, type DocumentApi, type DocumentPreview } from './documents-models';

type RequestInput = { path: Record<string, string>; query: Record<string, unknown>; headers: Record<string, string>; body: unknown; signal?: AbortSignal };
const emptyInput = (): { path: Record<string, never>; query: Record<string, never>; headers: Record<string, never>; body: never } => ({ path: {}, query: {}, headers: {}, body: undefined as never });

@Injectable({ providedIn: 'root' })
export class AuraDocumentsApi implements DocumentApi {
  private readonly transport = new GeneratedDocumentsTransport();
  private readonly client = new AuraApiClient(this.transport);
  private csrf = '';
  private sessionEpoch = 0;

  clearSession(): void { ++this.sessionEpoch; this.csrf = ''; }
  startLogin(): void {
    if (typeof window === 'undefined') return;
    const returnTo = `${window.location.pathname}${window.location.search}`;
    const query = new URLSearchParams({ return_to: returnTo });
    window.location.assign(`${AURA_API_OPERATIONS.startLogin.pathTemplate}?${query.toString()}`);
  }
  async list(cursor?: string): Promise<DocumentPage> { await this.ensureSession(); return this.client.execute('listDocuments', { ...emptyInput(), query: { limit: 30, ...(cursor ? { cursor } : {}) } }); }
  async search(query: string, cursor?: string): Promise<DocumentSearchResultPage> { await this.ensureSession(); return this.client.execute('searchDocuments', { ...emptyInput(), headers: { 'X-CSRF-Token': this.csrf }, body: documentSearchRequest(query, cursor) }); }
  async get(id: string): Promise<DocumentDetail> { await this.ensureSession(); return this.client.execute('getDocument', { ...emptyInput(), path: { document_id: id } }); }
  async createUpload(body: CreateDocumentUploadRequest, idempotencyKey: string, signal?: AbortSignal): Promise<DocumentUploadSession> {
    if (signal?.aborted) throw canceledError();
    await this.ensureSession(signal);
    if (signal?.aborted) throw canceledError();
    try {
      return await this.transport.executeDescriptor(AURA_API_OPERATIONS.createDocumentUpload, { ...emptyInput(), headers: { 'X-CSRF-Token': this.csrf, 'Idempotency-Key': idempotencyKey }, body, signal }) as DocumentUploadSession;
    } catch (error: unknown) {
      if (signal?.aborted) throw canceledError();
      throw error;
    }
  }
  async uploadContent(session: DocumentUploadSession, file: File, onProgress: (loaded: number, total: number) => void, idempotencyKey: string, signal?: AbortSignal): Promise<DocumentUploadReceipt> {
    if (signal?.aborted) throw canceledError();
    await this.ensureSession(signal);
    if (signal?.aborted) throw canceledError();
    const descriptor = AURA_API_OPERATIONS.uploadDocumentContent;
    return new Promise<DocumentUploadReceipt>((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      let settled = false;
      const abort = (): void => { xhr.abort(); finish(() => reject(new DocumentApiError(0, 'Upload canceled.'))); };
      const finish = (callback: () => void): void => { if (settled) return; settled = true; signal?.removeEventListener('abort', abort); callback(); };
      signal?.addEventListener('abort', abort, { once: true });
      xhr.open(descriptor.method, descriptor.pathTemplate.replace('{upload_id}', encodeURIComponent(session.id)), true);
      xhr.withCredentials = true;
      xhr.setRequestHeader('Accept', 'application/json');
      xhr.setRequestHeader('Content-Type', session.mediaType);
      xhr.setRequestHeader('X-CSRF-Token', this.csrf);
      xhr.setRequestHeader('Idempotency-Key', idempotencyKey);
      xhr.setRequestHeader('X-Expected-Version', String(session.version));
      xhr.upload.addEventListener('progress', (event) => onProgress(event.loaded, event.lengthComputable ? event.total : file.size));
      xhr.addEventListener('load', () => {
        if (xhr.status >= 200 && xhr.status < 300) {
          try { finish(() => resolve(JSON.parse(xhr.responseText) as DocumentUploadReceipt)); } catch { finish(() => reject(new DocumentApiError(xhr.status, 'Aura returned an unreadable upload response.'))); }
        } else finish(() => reject(new DocumentApiError(xhr.status, publicError(xhr.status))));
      });
      xhr.addEventListener('error', () => finish(() => reject(new DocumentApiError(0, 'The upload connection was interrupted.'))));
      xhr.addEventListener('abort', () => { if (!settled) finish(() => reject(new DocumentApiError(0, 'Upload canceled.'))); });
      xhr.send(file);
    });
  }
  async download(id: string, signal?: AbortSignal): Promise<{ readonly body: ReadableStream<Uint8Array>; readonly contentType: string; readonly contentLength: number; readonly contentDisposition: string }> {
    if (signal?.aborted) throw canceledError();
    await this.ensureSession(signal);
    if (signal?.aborted) throw canceledError();
    try {
      return await this.transport.executeDescriptor(AURA_API_OPERATIONS.downloadDocument, { ...emptyInput(), path: { document_id: id }, signal }) as { readonly body: ReadableStream<Uint8Array>; readonly contentType: string; readonly contentLength: number; readonly contentDisposition: string };
    } catch (error: unknown) {
      if (signal?.aborted) throw canceledError();
      throw error;
    }
  }
  async retry(id: string, body: DocumentTransitionRequest, idempotencyKey: string): Promise<DocumentDetail> { await this.ensureSession(); return this.client.execute('retryDocumentProcessing', { ...emptyInput(), path: { document_id: id }, headers: { 'X-CSRF-Token': this.csrf, 'Idempotency-Key': idempotencyKey }, body }); }
  async delete(id: string, body: DocumentTransitionRequest, idempotencyKey: string): Promise<{ readonly documentId: string; readonly status: 'deleting'; readonly version: number; readonly acceptedAt: string }> { await this.ensureSession(); return this.client.execute('deleteDocument', { ...emptyInput(), path: { document_id: id }, headers: { 'X-CSRF-Token': this.csrf, 'Idempotency-Key': idempotencyKey }, body }); }
  async preview(id: string): Promise<DocumentPreview> {
    await this.ensureSession();
    const response = await this.client.execute('getDocumentPreview', { ...emptyInput(), path: { document_id: id } });
    return { ...response, text: response.text.slice(0, DOCUMENT_PREVIEW_MAX_CHARS), truncated: response.truncated || response.text.length > DOCUMENT_PREVIEW_MAX_CHARS };
  }
  private async ensureSession(signal?: AbortSignal): Promise<void> {
    if (signal?.aborted) throw canceledError();
    if (this.csrf) return;
    const epoch = this.sessionEpoch;
    try {
      const session = signal ? await this.transport.executeDescriptor(AURA_API_OPERATIONS.getSession, { ...emptyInput(), signal }) as Session : await this.client.execute('getSession', emptyInput());
      if (signal?.aborted) throw canceledError();
      if (epoch !== this.sessionEpoch) throw new DocumentApiError(401, 'Session changed.');
      this.csrf = session.csrfToken;
    } catch (error: unknown) {
      if (signal?.aborted) throw canceledError();
      throw error;
    }
  }
}

export class DocumentApiError extends Error {
  constructor(readonly status: number, message: string) { super(message); this.name = 'DocumentApiError'; }
}

class GeneratedDocumentsTransport {
  async execute<K extends AuraOperationId>(descriptor: AuraOperationDescriptor, input: AuraOperationInput<K>): Promise<AuraOperationResponse<K>> { return this.executeDescriptor(descriptor, input) as Promise<AuraOperationResponse<K>>; }
  async executeDescriptor(descriptor: AuraOperationDescriptor, input: unknown): Promise<unknown> {
    const request = input as RequestInput;
    let path = descriptor.pathTemplate;
    for (const [name, value] of Object.entries(request.path)) path = path.replace(`{${name}}`, encodeURIComponent(value));
    const query = new URLSearchParams();
    for (const [name, value] of Object.entries(request.query)) if (value !== undefined && value !== null && value !== '') query.set(name, String(value));
    const response = await fetch(`${path}${query.size ? `?${query.toString()}` : ''}`, { method: descriptor.method, credentials: 'include', cache: 'no-store', headers: { Accept: descriptor.responseMode === 'binary' ? '*/*' : 'application/json', ...(request.body !== undefined ? { 'Content-Type': 'application/json' } : {}), ...request.headers }, body: request.body === undefined ? undefined : JSON.stringify(request.body), signal: request.signal });
    if (!response.ok) throw new DocumentApiError(response.status, publicError(response.status));
    if (descriptor.responseMode === 'binary') return { body: response.body ?? new ReadableStream<Uint8Array>(), contentType: response.headers.get('Content-Type') ?? 'application/octet-stream', contentLength: Number(response.headers.get('Content-Length') ?? 0), contentDisposition: response.headers.get('Content-Disposition') ?? '' };
    return response.status === 204 ? undefined : await response.json();
  }
}

function publicError(status: number): string { if (status === 401) return 'Your session has expired. Sign in again.'; if (status === 403) return 'You do not have permission to view this document.'; if (status === 404) return 'That document is no longer available.'; if (status === 409) return 'This document changed elsewhere. Refresh and try again.'; if (status === 413) return 'This file is larger than 100 MB.'; if (status === 415) return 'Aura could not validate this file type or its contents.'; if (status === 503) return 'The document service is temporarily unavailable.'; return 'Aura could not complete that document action.'; }
function canceledError(): DocumentApiError { return new DocumentApiError(0, 'Upload canceled.'); }
