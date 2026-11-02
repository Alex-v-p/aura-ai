import { Inject, Injectable, InjectionToken, Optional, signal } from '@angular/core';
import { AuraApiClient, AURA_API_OPERATIONS, type AttachmentDeletionReceipt, type AttachmentDetail, type AttachmentReference, type AttachmentUploadReceipt, type AttachmentUploadSession, type AuraOperationDescriptor, type AuraOperationId, type AuraOperationInput, type AuraOperationResponse } from '@aura/aura-api-client';

export const ATTACHMENT_MAX_BYTES = 25_000_000;
export const ATTACHMENT_MAX_PER_MESSAGE = 8;
export const ATTACHMENT_MAX_IMAGES_PER_MESSAGE = 4;
export const ATTACHMENT_MAX_PIXELS = 40_000_000;
export const ATTACHMENT_PREVIEW_LONGEST_SIDE = 2_048;
export const IMAGE_ATTACHMENT_ACCEPT = 'image/png,image/jpeg,image/webp,.png,.jpg,.jpeg,.webp';
export const VISION_RECOMMENDATION = 'qwen2.5vl:7b';
export const VISION_ALTERNATIVE = 'gemma3:12b';

export type AttachmentKind = 'document' | 'image';
export type AttachmentAvailability = 'available' | 'unavailable';
export type AttachmentSummary = {
  readonly linkId?: string;
  readonly kind: AttachmentKind;
  readonly sourceId: string;
  readonly displayFilename: string;
  readonly mediaType: string;
  readonly byteCount: number;
  readonly availability: AttachmentAvailability;
  readonly width?: number | null;
  readonly height?: number | null;
  readonly previewUrl?: string | null;
};
export type AttachmentSelection = AttachmentReference;
export type ImageAttachment = AttachmentSummary & { readonly kind: 'image'; readonly attachmentId: string; readonly version?: number; readonly status?: AttachmentDetail['status']; readonly createdAt?: string; readonly updatedAt?: string };
export type AttachmentLibraryErrorKind = 'auth' | 'permission' | 'storage' | 'unknown';
export type AttachmentUploadState = 'validating' | 'uploading' | 'processing' | 'ready' | 'failed';
export type AttachmentUploadItem = {
  readonly key: string;
  readonly filename: string;
  readonly byteCount: number;
  readonly mediaType: string;
  readonly progress: number;
  readonly state: AttachmentUploadState;
  readonly message: string | null;
  readonly attachmentId: string | null;
  readonly previewUrl: string | null;
  readonly file?: File;
};
type AttachmentDeletionOutcome = AttachmentDeletionReceipt | { readonly attachmentId: string; readonly status: 'failed'; readonly version: number; readonly acceptedAt: string };
export type ImageDeletionResult = 'removed' | 'accepted' | 'failed';

export interface AttachmentApi {
  clearSession?(): void;
  listImages(signal?: AbortSignal): Promise<ReadonlyArray<ImageAttachment>>;
  createImageUpload(body: { readonly filename: string; readonly mediaType: string; readonly byteCount: number }, idempotencyKey: string, signal?: AbortSignal): Promise<{ readonly id: string; readonly attachmentId?: string; readonly mediaType: string; readonly version: number }>;
  uploadImageContent(session: { readonly id: string; readonly attachmentId?: string; readonly mediaType: string; readonly version: number }, file: File, onProgress: (loaded: number, total: number) => void, idempotencyKey: string, signal?: AbortSignal): Promise<ImageAttachment>;
  previewImage(id: string, signal?: AbortSignal): Promise<Blob>;
  deleteImage(id: string, version: number, idempotencyKey: string, signal?: AbortSignal): Promise<void | AttachmentDeletionOutcome>;
}

export const ATTACHMENT_API = new InjectionToken<AttachmentApi>('AURA_ATTACHMENT_API');

export function validateImageFile(file: File): string | null {
  if (file.size < 1) return 'Choose an image with content.';
  if (file.size > ATTACHMENT_MAX_BYTES) return 'Images must be 25 MB or smaller.';
  const type = normalizeImageType(file.type, file.name);
  if (!type) return 'Aura supports PNG, JPEG, and static WebP images.';
  return null;
}

export function normalizeImageType(mediaType: string, filename = ''): 'image/png' | 'image/jpeg' | 'image/webp' | null {
  if (mediaType === 'image/png' || mediaType === 'image/jpeg' || mediaType === 'image/webp') return mediaType;
  const extension = filename.toLowerCase().split('.').pop();
  return extension === 'png' ? 'image/png' : extension === 'jpg' || extension === 'jpeg' ? 'image/jpeg' : extension === 'webp' ? 'image/webp' : null;
}

export async function inspectImageFile(file: File): Promise<string | null> {
  const validation = validateImageFile(file);
  if (validation) return validation;
  const type = normalizeImageType(file.type, file.name);
  const header = new Uint8Array(await file.slice(0, 32).arrayBuffer());
  if (type === 'image/png' && !(header[0] === 0x89 && header[1] === 0x50 && header[2] === 0x4e && header[3] === 0x47)) return 'The PNG signature does not match the file type.';
  if (type === 'image/jpeg' && !(header[0] === 0xff && header[1] === 0xd8 && header[2] === 0xff)) return 'The JPEG signature does not match the file type.';
  if (type === 'image/webp' && !(ascii(header, 0, 4) === 'RIFF' && ascii(header, 8, 4) === 'WEBP')) return 'The WebP signature does not match the file type.';
  if (type === 'image/webp' && /ANIM|ANMF/u.test(ascii(header, 0, header.length))) return 'Animated WebP images are not supported.';
  if (typeof createImageBitmap === 'undefined') return null;
  try {
    const bitmap = await createImageBitmap(file);
    const pixels = bitmap.width * bitmap.height;
    bitmap.close();
    return pixels > ATTACHMENT_MAX_PIXELS ? 'Images must be 40 megapixels or smaller.' : null;
  } catch {
    return 'Aura could not decode this image.';
  }
}

function ascii(bytes: Uint8Array, offset: number, length: number): string { return String.fromCharCode(...bytes.slice(offset, offset + length)); }

@Injectable({ providedIn: 'root' })
export class ConversationAttachmentStore {
  readonly images = signal<ReadonlyArray<ImageAttachment>>([]);
  readonly uploads = signal<ReadonlyArray<AttachmentUploadItem>>([]);
  readonly loading = signal(false);
  readonly error = signal<string | null>(null);
  readonly errorKind = signal<AttachmentLibraryErrorKind | null>(null);
  readonly selected = signal<ReadonlyArray<AttachmentSelection>>([]);
  readonly limitNotice = signal<string | null>(null);
  private readonly controllers = new Map<string, AbortController>();
  private readonly previewControllers = new Set<AbortController>();
  private readonly deletionControllers = new Set<AbortController>();
  private readonly objectUrls = new Set<string>();
  private readonly api: AttachmentApi;
  private epoch = 0;
  private loadController: AbortController | null = null;

  constructor(@Optional() @Inject(ATTACHMENT_API) api?: AttachmentApi) { this.api = api ?? new AuraAttachmentApi(); }

  async load(): Promise<void> {
    const epoch = ++this.epoch;
    this.loadController?.abort();
    const controller = new AbortController();
    this.loadController = controller;
    this.loading.set(true); this.error.set(null);
    this.errorKind.set(null);
    try {
      const images = await this.api.listImages(controller.signal);
      if (epoch !== this.epoch) return;
      this.images.set(images);
    }
    catch (error: unknown) {
      if (epoch !== this.epoch) return;
      this.errorKind.set(errorKind(error));
      this.error.set(errorMessage(error));
    }
    finally { if (epoch === this.epoch) { this.loading.set(false); this.loadController = null; } }
  }

  async upload(file: File, key = operationKey(file.name)): Promise<boolean> {
    const epoch = this.epoch;
    const validation = await inspectImageFile(file);
    if (epoch !== this.epoch) return false;
    const mediaType = normalizeImageType(file.type, file.name) ?? 'application/octet-stream';
    this.upsertUpload({ key, filename: file.name, byteCount: file.size, mediaType, progress: 0, state: validation ? 'failed' : 'uploading', message: validation, attachmentId: null, previewUrl: null, ...(validation ? {} : { file }) });
    if (validation) return false;
    const controller = new AbortController(); this.controllers.set(key, controller);
    try {
      const session = await this.api.createImageUpload({ filename: file.name, mediaType, byteCount: file.size }, key, controller.signal);
      if (epoch !== this.epoch) return false;
      this.patchUpload(key, { state: 'uploading', attachmentId: session.attachmentId ?? session.id });
      const image = await this.api.uploadImageContent(session, file, (loaded, total) => this.patchUpload(key, { progress: total ? Math.round(loaded / total * 100) : 0 }), key, controller.signal);
      if (epoch !== this.epoch) return false;
      this.patchUpload(key, { state: 'ready', progress: 100, attachmentId: image.attachmentId, message: null, file: undefined });
      this.images.update((items) => [image, ...items.filter((item) => item.attachmentId !== image.attachmentId)]);
      this.toggle({ kind: 'image', attachmentId: image.attachmentId });
      return true;
    } catch (error: unknown) {
      if (epoch !== this.epoch) return false;
      if (isAuthFailure(error)) this.reset();
      this.errorKind.set(errorKind(error)); this.error.set(errorMessage(error)); this.patchUpload(key, { state: 'failed', message: isAuthFailure(error) ? 'Your session has expired. Sign in again.' : 'Image upload failed. Try again.' }); return false;
    }
    finally { this.controllers.delete(key); }
  }

  retryUpload(key: string): void { const item = this.uploads().find((candidate) => candidate.key === key); if (item?.file) void this.upload(item.file, key); }
  async removeUpload(key: string): Promise<void> { this.controllers.get(key)?.abort(); this.controllers.delete(key); const item = this.uploads().find((candidate) => candidate.key === key); if (item?.attachmentId) { const image = this.images().find((candidate) => candidate.attachmentId === item.attachmentId); const result = image ? await this.deleteImage(image) : 'removed'; if (result === 'failed') { this.patchUpload(key, { state: 'failed', message: 'The uploaded image could not be removed. Try again.' }); return; } } this.uploads.update((items) => items.filter((candidate) => candidate.key !== key)); }
  toggle(selection: AttachmentSelection): void {
    const current = this.selected(); const exists = current.some((item) => attachmentKey(item) === attachmentKey(selection));
    if (exists) { this.selected.set(current.filter((item) => attachmentKey(item) !== attachmentKey(selection))); return; }
    if (current.length >= ATTACHMENT_MAX_PER_MESSAGE) { this.limitNotice.set(`You can attach up to ${ATTACHMENT_MAX_PER_MESSAGE} files per message.`); return; }
    if (selection.kind === 'image' && current.filter((item) => item.kind === 'image').length >= ATTACHMENT_MAX_IMAGES_PER_MESSAGE) { this.limitNotice.set(`You can attach up to ${ATTACHMENT_MAX_IMAGES_PER_MESSAGE} images per message.`); return; }
    this.limitNotice.set(null);
    this.selected.set([...current, selection]);
  }
  remove(selection: AttachmentSelection): void { this.selected.update((items) => items.filter((item) => attachmentKey(item) !== attachmentKey(selection))); }
  setSelected(items: ReadonlyArray<AttachmentSelection>): void { if (sameSelections(this.selected(), items)) return; this.selected.set([...items]); this.limitNotice.set(null); }
  clear(): void { this.selected.set([]); this.limitNotice.set(null); }
  isSelected(selection: AttachmentSelection): boolean { return this.selected().some((item) => attachmentKey(item) === attachmentKey(selection)); }
  async preview(id: string, signal?: AbortSignal): Promise<string | null> {
    const epoch = this.epoch;
    const controller = new AbortController();
    this.previewControllers.add(controller);
    if (signal) {
      if (signal.aborted) controller.abort();
      else signal.addEventListener('abort', () => controller.abort(), { once: true });
    }
    try {
      const blob = await this.api.previewImage(id, controller.signal);
      if (epoch !== this.epoch || controller.signal.aborted) return null;
      const url = URL.createObjectURL(blob); this.objectUrls.add(url); return url;
    } catch (error: unknown) { if (epoch !== this.epoch) return null; if (isAuthFailure(error)) this.reset(); return null; }
    finally { this.previewControllers.delete(controller); }
  }
  async deleteImage(image: ImageAttachment): Promise<ImageDeletionResult> {
    const epoch = this.epoch;
    const controller = new AbortController();
    this.deletionControllers.add(controller);
    try {
      const receipt = await this.api.deleteImage(image.attachmentId, image.version ?? 1, operationKey(`delete:${image.attachmentId}`), controller.signal);
      if (epoch !== this.epoch) return 'failed';
      if (receipt) {
        let refreshed: ReadonlyArray<ImageAttachment>;
        try {
          refreshed = await this.api.listImages(controller.signal);
        } catch {
          if (epoch !== this.epoch || controller.signal.aborted) return 'failed';
          // The DELETE response is authoritative. A best-effort refresh must
          // not turn an accepted asynchronous deletion into a false failure.
          const fallback = { ...image, availability: 'unavailable' as const, status: receipt.status, version: receipt.version };
          this.images.update((items) => [fallback, ...items.filter((item) => item.attachmentId !== image.attachmentId)]);
          if (receipt.status === 'failed') {
            this.errorKind.set('storage');
            this.error.set(`${image.displayFilename} could not be deleted. Try again.`);
            return 'failed';
          }
          this.errorKind.set(null);
          this.error.set(null);
          return 'accepted';
        }
        if (epoch !== this.epoch) return 'failed';
        const current = refreshed.find((item) => item.attachmentId === image.attachmentId);
        const others = refreshed.filter((item) => item.attachmentId !== image.attachmentId);
        if (receipt.status === 'failed' || current?.status === 'failed') {
          this.images.set([...others, { ...(current ?? image), availability: 'unavailable', status: 'failed', version: receipt.version }]);
          this.errorKind.set('storage');
          this.error.set(`${image.displayFilename} could not be deleted. Try again.`);
          return 'failed';
        }
        if (receipt.status === 'deleting') {
          // The object is now unavailable to new consumers, but physical
          // deletion is asynchronous. Keep a content-free status row for
          // retry/refresh while removing it from the unsent draft in the UI.
          this.images.set([...others, { ...(current ?? image), availability: 'unavailable', status: 'deleting', version: receipt.version }]);
          this.errorKind.set(null);
          this.error.set(null);
          return 'accepted';
        }
      }
      this.images.update((items) => items.filter((item) => item.attachmentId !== image.attachmentId));
      // A confirmed deletion removes the reusable library entry and its
      // completed upload-history row. Failed/deferred deletion paths return
      // above so the row remains available for retry.
      this.uploads.update((items) => items.filter((item) => item.attachmentId !== image.attachmentId));
      this.remove({ kind: 'image', attachmentId: image.attachmentId });
      return 'removed';
    } catch (error: unknown) { if (epoch !== this.epoch || controller.signal.aborted) return 'failed'; if (isAuthFailure(error)) this.reset(); this.errorKind.set(errorKind(error)); this.error.set(errorMessage(error)); return 'failed'; }
    finally { this.deletionControllers.delete(controller); }
  }
  revokePreview(url: string): void { if (!this.objectUrls.has(url)) return; this.objectUrls.delete(url); URL.revokeObjectURL(url); }
  reset(): void { this.epoch += 1; this.loadController?.abort(); this.loadController = null; this.controllers.forEach((controller) => controller.abort()); this.controllers.clear(); this.previewControllers.forEach((controller) => controller.abort()); this.previewControllers.clear(); this.deletionControllers.forEach((controller) => controller.abort()); this.deletionControllers.clear(); this.objectUrls.forEach((url) => URL.revokeObjectURL(url)); this.objectUrls.clear(); this.api.clearSession?.(); this.images.set([]); this.uploads.set([]); this.selected.set([]); this.limitNotice.set(null); this.error.set(null); this.errorKind.set(null); this.loading.set(false); }
  private upsertUpload(item: AttachmentUploadItem): void { this.uploads.update((items) => [...items.filter((candidate) => candidate.key !== item.key), item]); }
  private patchUpload(key: string, patch: Partial<AttachmentUploadItem>): void { this.uploads.update((items) => items.map((item) => item.key === key ? { ...item, ...patch } : item)); }
}

function operationKey(prefix: string): string { return typeof crypto !== 'undefined' && 'randomUUID' in crypto ? crypto.randomUUID() : `attachment-${prefix}-${Date.now()}`; }

class AuraAttachmentApi implements AttachmentApi {
  private readonly client = new AuraApiClient(new AttachmentTransport());
  private csrf = '';
  clearSession(): void { this.csrf = ''; }
  async listImages(signal?: AbortSignal): Promise<ReadonlyArray<ImageAttachment>> { await this.ensureSession(signal); if (signal?.aborted) throw new Error('Attachment list canceled.'); const response = await this.client.execute('listAttachments', { ...emptyInput(), query: { limit: 100 } }); if (signal?.aborted) throw new Error('Attachment list canceled.'); return response.items.map(toImageAttachment); }
  async createImageUpload(body: { readonly filename: string; readonly mediaType: string; readonly byteCount: number }, idempotencyKey: string, signal?: AbortSignal): Promise<AttachmentUploadSession> { await this.ensureSession(signal); if (signal?.aborted) throw new Error('Upload canceled.'); return this.client.execute('createAttachmentUpload', { ...emptyInput(), headers: { 'X-CSRF-Token': this.csrf, 'Idempotency-Key': idempotencyKey }, body } as never);
  }
  async uploadImageContent(session: AttachmentUploadSession, file: File, onProgress: (loaded: number, total: number) => void, idempotencyKey: string, signal?: AbortSignal): Promise<ImageAttachment> { await this.ensureSession(signal); if (signal?.aborted) throw new Error('Upload canceled.'); const descriptor = AURA_API_OPERATIONS.uploadAttachmentContent; return new Promise((resolve, reject) => { const xhr = new XMLHttpRequest(); if (signal?.aborted) { reject(new Error('Upload canceled.')); return; } xhr.open(descriptor.method, descriptor.pathTemplate.replace('{upload_id}', encodeURIComponent(session.id)), true); xhr.withCredentials = true; xhr.setRequestHeader('Accept', 'application/json'); xhr.setRequestHeader('Content-Type', session.mediaType); xhr.setRequestHeader('X-CSRF-Token', this.csrf); xhr.setRequestHeader('Idempotency-Key', idempotencyKey); xhr.setRequestHeader('X-Expected-Version', String(session.version)); xhr.upload.onprogress = (event) => onProgress(event.loaded, event.lengthComputable ? event.total : file.size); xhr.onload = () => { if (xhr.status >= 200 && xhr.status < 300) { const receipt = JSON.parse(xhr.responseText) as AttachmentUploadReceipt; resolve(toImageAttachment(receipt.attachment)); } else reject(new AttachmentHttpError(xhr.status)); }; xhr.onerror = () => reject(new Error('Image upload failed.')); signal?.addEventListener('abort', () => xhr.abort(), { once: true }); xhr.send(file); }); }
  async previewImage(id: string, signal?: AbortSignal): Promise<Blob> { await this.ensureSession(signal); const descriptor = AURA_API_OPERATIONS.getAttachmentPreview; const response = await fetch(descriptor.pathTemplate.replace('{attachment_id}', encodeURIComponent(id)), { credentials: 'include', cache: 'no-store', signal }); if (!response.ok) throw response.status === 401 || response.status === 403 ? new AttachmentHttpError(response.status) : new Error('Image preview is unavailable.'); return response.blob(); }
  async deleteImage(id: string, version: number, idempotencyKey: string, signal?: AbortSignal): Promise<AttachmentDeletionOutcome> { await this.ensureSession(signal); if (signal?.aborted) throw new Error('Deletion canceled.'); try { return await this.client.execute('deleteAttachment', { ...emptyInput(), path: { attachment_id: id }, headers: { 'X-CSRF-Token': this.csrf, 'Idempotency-Key': idempotencyKey }, body: { expectedVersion: version } }); } catch (error: unknown) { if (signal?.aborted) throw new Error('Deletion canceled.'); if (error instanceof AttachmentHttpError && error.status === 503 && error.body && typeof error.body === 'object') { const payload = error.body as Record<string, unknown>; if (typeof payload['attachmentId'] === 'string' && payload['status'] === 'failed' && typeof payload['version'] === 'number' && typeof payload['acceptedAt'] === 'string') return payload as AttachmentDeletionOutcome; } throw error; } }
  private async ensureSession(signal?: AbortSignal): Promise<void> { if (signal?.aborted) throw new Error('Request canceled.'); if (this.csrf) return; const session = await this.client.execute('getSession', emptyInput()); if (signal?.aborted) throw new Error('Request canceled.'); this.csrf = session.csrfToken; }
}

function toImageAttachment(item: AttachmentDetail): ImageAttachment { return { kind: 'image', attachmentId: item.id, sourceId: item.id, displayFilename: item.filename, mediaType: item.mediaType, byteCount: item.byteCount, availability: item.status === 'ready' ? 'available' : 'unavailable', width: item.width, height: item.height, version: item.version, status: item.status, createdAt: item.createdAt, updatedAt: item.updatedAt }; }

class AttachmentTransport {
  execute<K extends AuraOperationId>(descriptor: AuraOperationDescriptor, input: AuraOperationInput<K>): Promise<AuraOperationResponse<K>> { const request = input as unknown as { path: Record<string, string>; query: Record<string, unknown>; headers: Record<string, string>; body: unknown }; let path = descriptor.pathTemplate; for (const [name, value] of Object.entries(request.path)) path = path.replace(`{${name}}`, encodeURIComponent(value)); const query = new URLSearchParams(); for (const [name, value] of Object.entries(request.query)) if (value !== undefined) query.set(name, String(value)); return fetch(`${path}${query.size ? `?${query.toString()}` : ''}`, { method: descriptor.method, credentials: 'include', cache: 'no-store', headers: { Accept: 'application/json', ...(request.body !== undefined ? { 'Content-Type': 'application/json' } : {}), ...request.headers }, body: request.body === undefined ? undefined : JSON.stringify(request.body) }).then(async (response) => { if (!response.ok) { let body: unknown; try { body = await response.json(); } catch { body = undefined; } throw new AttachmentHttpError(response.status, body); } return response.status === 204 ? undefined : response.json(); }) as Promise<AuraOperationResponse<K>>; }
}

function emptyInput(): { path: Record<string, never>; query: Record<string, never>; headers: Record<string, never>; body: never } { return { path: {}, query: {}, headers: {}, body: undefined as never }; }
function attachmentKey(item: AttachmentSelection): string { return item.kind === 'document' ? `document:${item.documentId}` : `image:${item.attachmentId}`; }
function sameSelections(current: ReadonlyArray<AttachmentSelection>, next: ReadonlyArray<AttachmentSelection>): boolean { return current.length === next.length && current.every((item, index) => { const candidate = next[index]; return candidate !== undefined && attachmentKey(item) === attachmentKey(candidate); }); }
class AttachmentHttpError extends Error { constructor(readonly status: number, readonly body?: unknown) { super(`Attachment request failed (${status}).`); } }
function isAuthFailure(error: unknown): boolean { return error instanceof AttachmentHttpError && (error.status === 401 || error.status === 403); }
function errorKind(error: unknown): AttachmentLibraryErrorKind { const status = error instanceof AttachmentHttpError ? error.status : typeof (error as { readonly status?: unknown })?.status === 'number' ? (error as { readonly status: number }).status : undefined; if (status === 401) return 'auth'; if (status === 403) return 'permission'; if (typeof status === 'number' && status >= 500) return 'storage'; return 'unknown'; }
function errorMessage(error: unknown): string { switch (errorKind(error)) { case 'auth': return 'Sign in again to access your image library.'; case 'permission': return 'You do not have permission to access your image library.'; case 'storage': return 'Your image library is temporarily unavailable. Try again.'; default: return 'Your image library could not be loaded. Try again.'; } }
