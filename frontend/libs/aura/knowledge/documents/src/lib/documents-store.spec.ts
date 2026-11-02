import type { DocumentDetail, DocumentPage, DocumentUploadReceipt, DocumentUploadSession } from '@aura/aura-api-client';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { DocumentApi } from './documents-models';
import { DocumentsStoreState } from './documents-store-state';

const detail = (id: string, status: DocumentDetail['status'] = 'processing'): DocumentDetail => ({ id, filename: `${id}.txt`, mediaType: 'text/plain', byteCount: 12, status, failure: null, version: 1, extractedTextLength: 0, chunkCount: 0, createdAt: '2026-01-01T00:00:00Z', updatedAt: '2026-01-01T00:00:00Z' });
const page = (...items: DocumentDetail[]): DocumentPage => ({ items, nextCursor: null });
const session = (): DocumentUploadSession => ({ id: 'upload-1', documentId: 'doc-1', filename: 'notes.txt', mediaType: 'text/plain', byteCount: 5, status: 'uploading', version: 1, createdAt: '2026-01-01T00:00:00Z', expiresAt: '2026-01-01T01:00:00Z' });
const receipt = (document = detail('doc-1', 'queued')): DocumentUploadReceipt => ({ document, duplicate: false });
function deferred<T>(): { promise: Promise<T>; resolve(value: T): void; reject(error: unknown): void } { let resolve!: (value: T) => void; let reject!: (error: unknown) => void; const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej; }); return { promise, resolve, reject }; }
function fakeApi(overrides: Partial<DocumentApi> = {}): DocumentApi {
  return { list: vi.fn().mockResolvedValue(page()), search: vi.fn().mockResolvedValue({ items: [], nextCursor: null }), get: vi.fn().mockResolvedValue(detail('doc-1')), preview: vi.fn().mockResolvedValue({ documentId: 'doc-1', text: 'preview', truncated: false, totalCharacters: 7 }), createUpload: vi.fn().mockResolvedValue(session()), uploadContent: vi.fn().mockResolvedValue(receipt()), download: vi.fn(), retry: vi.fn().mockResolvedValue(detail('doc-1', 'queued')), delete: vi.fn().mockResolvedValue({ documentId: 'doc-1', status: 'deleting', version: 2, acceptedAt: '2026-01-01T00:00:00Z' }), ...overrides } as DocumentApi;
}
const authFailure = { status: 401, message: 'expired' };

afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks(); });

describe('DocumentsStoreState async ownership guards', () => {
  it('cancels a delayed create session before content upload starts', async () => {
    const create = deferred<DocumentUploadSession>();
    const api = fakeApi({ createUpload: vi.fn((_body, _key, signal) => { signal?.addEventListener('abort', () => create.reject({ status: 0, message: 'Upload canceled.' }), { once: true }); return create.promise; }), uploadContent: vi.fn() });
    const state = new DocumentsStoreState(api); const file = new File(['hello'], 'notes.txt', { type: 'text/plain' });
    const pending = state.upload(file, 'upload-operation');
    await Promise.resolve();
    state.cancelUpload('upload-operation');
    expect(await pending).toBe(false);
    expect(api.uploadContent).not.toHaveBeenCalled();
    expect(state.uploads()[0]?.state).toBe('canceled');
  });

  it('reconciles an accepted upload through processing and failure, then retries with the current version', async () => {
    const processing = { ...detail('doc-1', 'processing'), version: 2 };
    const failed = { ...detail('doc-1', 'failed'), version: 3, failure: { code: 'parse_failed', retryable: true } };
    const ready = { ...detail('doc-1', 'ready'), version: 4 };
    const get = vi.fn().mockResolvedValueOnce(processing).mockResolvedValueOnce(failed);
    const retry = vi.fn().mockResolvedValue(ready);
    const state = new DocumentsStoreState(fakeApi({ get, retry }));
    expect(await state.upload(new File(['hello'], 'notes.txt', { type: 'text/plain' }), 'upload-operation')).toBe(true);
    expect(state.uploads()[0]?.state).toBe('queued');
    await (state as unknown as { poll(id: string): Promise<void> }).poll('doc-1');
    expect(state.uploads()[0]?.state).toBe('processing');
    await (state as unknown as { poll(id: string): Promise<void> }).poll('doc-1');
    expect(state.uploads()[0]).toMatchObject({ state: 'failed', documentId: 'doc-1', documentVersion: 3, retryable: true });
    state.retryUpload('upload-operation');
    await Promise.resolve();
    await Promise.resolve();
    expect(retry).toHaveBeenCalledWith('doc-1', { expectedVersion: 3 }, expect.any(String));
    expect(state.uploads()[0]).toMatchObject({ state: 'ready', documentVersion: 4, retryable: false });
  });

  it.each([401, 403] as const)('resets private state and flags auth when preview loses the session (%s)', async (status) => {
    const api = fakeApi({ get: vi.fn().mockResolvedValue(detail('doc-1', 'ready')), preview: vi.fn().mockRejectedValue({ ...authFailure, status }) });
    const state = new DocumentsStoreState(api);
    await state.loadDetail('doc-1');
    await Promise.resolve(); await Promise.resolve();
    expect(state.selected()).toBeNull();
    expect(state.authRequired()).toBe(status === 401);
    expect(state.permissionDenied()).toBe(status === 403);
    expect(state.documents().items).toHaveLength(0);
  });

  it.each([401, 403] as const)('does not reschedule polling after a %s reset', async (status) => {
    vi.useFakeTimers();
    const api = fakeApi({ get: vi.fn().mockRejectedValue({ ...authFailure, status }) }); const state = new DocumentsStoreState(api);
    state.documents.set(page(detail('doc-1', 'processing')));
    await (state as unknown as { poll(id: string): Promise<void> }).poll('doc-1');
    expect(state.authRequired()).toBe(status === 401);
    expect(state.permissionDenied()).toBe(status === 403);
    expect(vi.getTimerCount()).toBe(0);
  });

  it('keeps a newer collection result when an older request rejects', async () => {
    const old = deferred<DocumentPage>(); const current = deferred<DocumentPage>();
    const api = fakeApi({ list: vi.fn().mockReturnValueOnce(old.promise).mockReturnValueOnce(current.promise) }); const state = new DocumentsStoreState(api);
    const first = state.load(); const second = state.load(); current.resolve(page(detail('current'))); await second; old.reject(new Error('stale failure')); await first;
    expect(state.documents().items[0]?.id).toBe('current');
    expect(state.error()).toBeNull();
  });

  it('keeps a newer detail result when an older request rejects', async () => {
    const old = deferred<DocumentDetail>(); const current = deferred<DocumentDetail>();
    const api = fakeApi({ get: vi.fn().mockReturnValueOnce(old.promise).mockReturnValueOnce(current.promise) }); const state = new DocumentsStoreState(api);
    const first = state.loadDetail('old'); const second = state.loadDetail('current'); current.resolve(detail('current', 'processing')); await second; old.reject(new Error('stale failure')); await first;
    expect(state.selected()?.id).toBe('current');
    expect(state.detailError()).toBeNull();
  });

  it('ignores an old collection 401 after a session reset', async () => {
    const pending = deferred<DocumentPage>(); const api = fakeApi({ list: vi.fn().mockReturnValue(pending.promise) }); const state = new DocumentsStoreState(api);
    const request = state.load(); await Promise.resolve(); state.resetSessionState(); state.documents.set(page(detail('replacement'))); pending.reject({ status: 401, message: 'old session' }); await request;
    expect(state.documents().items[0]?.id).toBe('replacement');
    expect(state.authRequired()).toBe(false);
  });

  it('ignores an old detail 403 after a session reset', async () => {
    const pending = deferred<DocumentDetail>(); const api = fakeApi({ get: vi.fn().mockReturnValue(pending.promise) }); const state = new DocumentsStoreState(api);
    const request = state.loadDetail('old'); await Promise.resolve(); state.resetSessionState(); state.selected.set(detail('replacement')); pending.reject({ status: 403, message: 'old principal' }); await request;
    expect(state.selected()?.id).toBe('replacement');
    expect(state.permissionDenied()).toBe(false);
  });

  it('ignores an old preview 401 after a session reset', async () => {
    const pending = deferred<{ documentId: string; text: string; truncated: boolean; totalCharacters: number }>(); const api = fakeApi({ preview: vi.fn().mockReturnValue(pending.promise) }); const state = new DocumentsStoreState(api);
    state.selected.set(detail('old', 'ready')); const request = state.loadPreview('old'); await Promise.resolve(); state.resetSessionState(); state.selected.set(detail('replacement')); pending.reject({ status: 401, message: 'old session' }); await request;
    expect(state.selected()?.id).toBe('replacement');
    expect(state.authRequired()).toBe(false);
  });

  it('ignores an old poll 403 after a session reset', async () => {
    const pending = deferred<DocumentDetail>(); const api = fakeApi({ get: vi.fn().mockReturnValue(pending.promise) }); const state = new DocumentsStoreState(api);
    state.documents.set(page(detail('old', 'processing'))); const request = (state as unknown as { poll(id: string): Promise<void> }).poll('old'); await Promise.resolve(); state.resetSessionState(); state.documents.set(page(detail('replacement'))); pending.reject({ status: 403, message: 'old principal' }); await request;
    expect(state.documents().items[0]?.id).toBe('replacement');
    expect(state.permissionDenied()).toBe(false);
  });

  it('ignores a stale load-more rejection after the session generation changes', async () => {
    const pending = deferred<DocumentPage>(); const api = fakeApi({ list: vi.fn().mockReturnValue(pending.promise) }); const state = new DocumentsStoreState(api);
    state.documents.set({ items: [detail('old')], nextCursor: 'next' });
    const request = state.loadMore(); await Promise.resolve(); state.resetSessionState(); state.documents.set(page(detail('new'))); pending.reject(new Error('stale failure')); await request;
    expect(state.documents().items[0]?.id).toBe('new');
    expect(state.error()).toBeNull();
  });

  it('does not apply a retry response from a pre-reset principal', async () => {
    const pending = deferred<DocumentDetail>(); const api = fakeApi({ retry: vi.fn().mockReturnValue(pending.promise) }); const state = new DocumentsStoreState(api);
    const request = state.retry('old', 1); await Promise.resolve(); state.resetSessionState(); state.selected.set(detail('new')); pending.resolve(detail('old', 'queued'));
    expect(await request).toBe(false);
    expect(state.selected()?.id).toBe('new');
  });

  it('does not apply a delete response from a pre-reset principal', async () => {
    const pending = deferred<{ readonly documentId: string; readonly status: 'deleting'; readonly version: number; readonly acceptedAt: string }>(); const api = fakeApi({ delete: vi.fn().mockReturnValue(pending.promise) }); const state = new DocumentsStoreState(api);
    state.documents.set(page(detail('old'))); const request = state.remove('old', 1); await Promise.resolve(); state.resetSessionState(); state.documents.set(page(detail('new'))); pending.resolve({ documentId: 'old', status: 'deleting', version: 2, acceptedAt: '2026-01-01T00:00:00Z' });
    expect(await request).toBe(false);
    expect(state.documents().items[0]?.id).toBe('new');
  });

  it('does not create or click a download after session reset', async () => {
    const body = new ReadableStream<Uint8Array>({ start() { /* reset cancels this reader */ } }); const api = fakeApi({ download: vi.fn().mockResolvedValue({ body, contentType: 'text/plain', contentLength: 5, contentDisposition: '' }) }); const state = new DocumentsStoreState(api);
    const createObjectUrl = vi.fn().mockReturnValue('blob:stale'); const revokeObjectUrl = vi.fn(); vi.stubGlobal('URL', { createObjectURL: createObjectUrl, revokeObjectURL: revokeObjectUrl });
    const request = state.download('old', 'old.txt'); await Promise.resolve(); state.resetSessionState();
    expect(await request).toBe(false);
    expect(createObjectUrl).not.toHaveBeenCalled();
  });
});
