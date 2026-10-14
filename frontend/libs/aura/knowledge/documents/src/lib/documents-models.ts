import { InjectionToken } from '@angular/core';
import type {
  CreateDocumentUploadRequest,
  DocumentDetail,
  DocumentMediaType,
  DocumentPage,
  DocumentPreview,
  DocumentSearchResultPage,
  DocumentSummary,
  DocumentTransitionRequest,
  DocumentUploadReceipt,
  DocumentUploadSession,
} from '@aura/aura-api-client';

export const DOCUMENT_MAX_BYTES = 100_000_000;
export const DOCUMENT_SEARCH_LIMIT = 20;
export const DOCUMENT_PREVIEW_MAX_CHARS = 20_000;
export const DOCUMENT_ACCEPT = '.pdf,.docx,.md,.markdown,.txt,text/plain,text/markdown,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document';
export const DOCUMENT_MEDIA_TYPES: Readonly<Record<string, DocumentMediaType>> = {
  pdf: 'application/pdf', docx: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  md: 'text/markdown', markdown: 'text/markdown', txt: 'text/plain',
};

export interface DocumentApi {
  clearSession?(): void;
  startLogin?(): void;
  list(cursor?: string): Promise<DocumentPage>;
  search(query: string, cursor?: string): Promise<DocumentSearchResultPage>;
  get(id: string): Promise<DocumentDetail>;
  preview(id: string): Promise<DocumentPreview>;
  createUpload(body: CreateDocumentUploadRequest, idempotencyKey: string, signal?: AbortSignal): Promise<DocumentUploadSession>;
  uploadContent(session: DocumentUploadSession, file: File, onProgress: (loaded: number, total: number) => void, idempotencyKey: string, signal?: AbortSignal): Promise<DocumentUploadReceipt>;
  download(id: string, signal?: AbortSignal): Promise<{ readonly body: ReadableStream<Uint8Array>; readonly contentType: string; readonly contentLength: number; readonly contentDisposition: string }>;
  retry(id: string, body: DocumentTransitionRequest, idempotencyKey: string): Promise<DocumentDetail>;
  delete(id: string, body: DocumentTransitionRequest, idempotencyKey: string): Promise<{ readonly documentId: string; readonly status: 'deleting'; readonly version: number; readonly acceptedAt: string }>;
}

export const DOCUMENT_API = new InjectionToken<DocumentApi>('AURA_DOCUMENT_API');

/** In-memory idempotency ownership for one authenticated feature session. */
export class EphemeralDocumentOperationKeys {
  private readonly keys = new Map<string, string>();
  get(operation: string): string { const current = this.keys.get(operation); if (current) return current; const key = this.newKey(); this.keys.set(operation, key); return key; }
  forget(operation: string): void { this.keys.delete(operation); }
  clear(): void { this.keys.clear(); }
  private newKey(): string { return typeof crypto !== 'undefined' && 'randomUUID' in crypto ? crypto.randomUUID() : `operation-${Date.now()}-${Math.random().toString(36).slice(2)}`; }
}

export interface DocumentUploadItem {
  readonly key: string;
  readonly filename: string;
  readonly byteCount: number;
  readonly mediaType: DocumentMediaType | null;
  readonly progress: number;
  readonly state: 'validating' | 'creating' | 'uploading' | 'queued' | 'processing' | 'ready' | 'duplicate' | 'failed' | 'canceled';
  readonly message: string | null;
  readonly documentId: string | null;
  readonly documentVersion: number | null;
  readonly retryable: boolean;
  readonly file?: File;
}

export function mediaTypeForFile(file: File): DocumentMediaType | null {
  const extension = file.name.toLowerCase().split('.').pop() ?? '';
  return DOCUMENT_MEDIA_TYPES[extension] ?? null;
}

export function validateDocumentFile(file: File): string | null {
  if (file.size < 1) return 'Choose a file with content.';
  if (file.size > DOCUMENT_MAX_BYTES) return 'Files must be 100 MB or smaller.';
  const mediaType = mediaTypeForFile(file);
  if (!mediaType) return 'Aura supports PDF, DOCX, Markdown, and plain text files.';
  if (file.type && file.type !== mediaType && !(mediaType === 'text/markdown' && file.type === 'text/plain')) return 'The file type does not match its extension.';
  return null;
}

export function formatDocumentSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(bytes < 10 * 1024 ? 1 : 0)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(bytes < 10 * 1024 * 1024 ? 1 : 0)} MB`;
}

export function documentTypeLabel(mediaType: DocumentMediaType): string {
  return mediaType === 'application/pdf' ? 'PDF' : mediaType.includes('wordprocessingml') ? 'DOCX' : mediaType === 'text/markdown' ? 'Markdown' : 'Plain text';
}

export function documentSearchRequest(query: string, cursor?: string): { query: string; limit: number; cursor?: string } {
  return { query: query.trim().slice(0, 200), limit: DOCUMENT_SEARCH_LIMIT, ...(cursor ? { cursor } : {}) };
}

export type { DocumentDetail, DocumentPage, DocumentPreview, DocumentSearchResultPage, DocumentSummary };
