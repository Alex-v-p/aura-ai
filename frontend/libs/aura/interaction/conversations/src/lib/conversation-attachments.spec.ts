import { describe, expect, it, vi } from 'vitest';
import { ATTACHMENT_MAX_IMAGES_PER_MESSAGE, ATTACHMENT_MAX_PER_MESSAGE, ATTACHMENT_MAX_BYTES, ConversationAttachmentStore, VISION_ALTERNATIVE, VISION_RECOMMENDATION, normalizeImageType, validateImageFile } from './conversation-attachments';

describe('conversation attachments', () => {
  it('accepts only bounded PNG, JPEG, and WebP uploads', () => {
    expect(normalizeImageType('image/png', 'photo.bin')).toBe('image/png');
    expect(normalizeImageType('', 'photo.jpeg')).toBe('image/jpeg');
    expect(normalizeImageType('', 'photo.webp')).toBe('image/webp');
    expect(validateImageFile(new File([new Uint8Array([1])], 'photo.gif', { type: 'image/gif' }))).toContain('PNG');
    expect(validateImageFile(new File([new Uint8Array(ATTACHMENT_MAX_BYTES + 1)], 'photo.png', { type: 'image/png' }))).toContain('25 MB');
  });

  it('enforces aggregate and image-specific selection limits', () => {
    const store = new ConversationAttachmentStore({
      listImages: async () => [],
      createImageUpload: async () => ({ id: 'upload', attachmentId: 'image-1', mediaType: 'image/png', version: 1 }),
      uploadImageContent: async () => ({ kind: 'image', attachmentId: 'image-1', sourceId: 'image-1', displayFilename: 'x.png', mediaType: 'image/png', byteCount: 1, availability: 'available' }),
      previewImage: async () => new Blob(),
      deleteImage: async () => undefined,
    });
    for (let index = 0; index < ATTACHMENT_MAX_IMAGES_PER_MESSAGE + 2; index += 1) store.toggle({ kind: 'image', attachmentId: `image-${index}` });
    expect(store.selected()).toHaveLength(ATTACHMENT_MAX_IMAGES_PER_MESSAGE);
    for (let index = 0; index < ATTACHMENT_MAX_PER_MESSAGE; index += 1) store.toggle({ kind: 'document', documentId: `document-${index}` });
    expect(store.selected()).toHaveLength(ATTACHMENT_MAX_PER_MESSAGE);
  });

  it('does not publish a redundant selection during conversation bootstrap hydration', () => {
    const store = new ConversationAttachmentStore({
      listImages: async () => [],
      createImageUpload: async () => ({ id: 'upload', attachmentId: 'image-1', mediaType: 'image/png', version: 1 }),
      uploadImageContent: async () => ({ kind: 'image', attachmentId: 'image-1', sourceId: 'image-1', displayFilename: 'x.png', mediaType: 'image/png', byteCount: 1, availability: 'available' as const }),
      previewImage: async () => new Blob(),
      deleteImage: async () => undefined,
    });
    const selection = [{ kind: 'image', attachmentId: 'image-1' }] as const;
    store.setSelected(selection);
    const current = store.selected();
    store.setSelected([{ ...selection[0] }]);
    expect(store.selected()).toBe(current);
  });

  it('revokes authorized preview object URLs on explicit cleanup and reset', async () => {
    const revoke = vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => undefined);
    const create = vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:preview');
    const store = new ConversationAttachmentStore({
      listImages: async () => [],
      createImageUpload: async () => ({ id: 'upload', attachmentId: 'image-1', mediaType: 'image/png', version: 1 }),
      uploadImageContent: async () => ({ kind: 'image', attachmentId: 'image-1', sourceId: 'image-1', displayFilename: 'x.png', mediaType: 'image/png', byteCount: 1, availability: 'available' }),
      previewImage: async () => new Blob(['pixels'], { type: 'image/png' }),
      deleteImage: async () => undefined,
    });

    const url = await store.preview('image-1');
    expect(url).toBe('blob:preview');
    store.revokePreview(url ?? '');
    expect(revoke).toHaveBeenCalledWith('blob:preview');
    const second = await store.preview('image-1');
    expect(second).toBe('blob:preview');
    store.reset();
    expect(revoke).toHaveBeenCalledWith('blob:preview');
    expect(create).toHaveBeenCalledTimes(2);
    revoke.mockRestore();
    create.mockRestore();
  });

  it('keeps the model guidance capability-gated and never stores authenticated bytes', async () => {
    expect(VISION_RECOMMENDATION).toBe('qwen2.5vl:7b');
    expect(VISION_ALTERNATIVE).toBe('gemma3:12b');
  });

  it('keeps deleting images available for retry and uses the server version', async () => {
    const deleteImage = vi.fn().mockResolvedValue({ attachmentId: 'image-1', status: 'deleting', version: 8, acceptedAt: '2026-01-01T00:00:00Z' });
    const image = { kind: 'image' as const, attachmentId: 'image-1', sourceId: 'image-1', displayFilename: 'photo.png', mediaType: 'image/png', byteCount: 10, availability: 'available' as const, version: 7 };
    const store = new ConversationAttachmentStore({ listImages: async () => [image], createImageUpload: async () => ({ id: 'upload', attachmentId: 'image-1', mediaType: 'image/png', version: 1 }), uploadImageContent: async () => image, previewImage: async () => new Blob(), deleteImage });
    await store.load();
    store.uploads.set([{ key: 'upload-1', filename: 'photo.png', byteCount: 10, mediaType: 'image/png', progress: 100, state: 'ready', message: null, attachmentId: 'image-1', previewUrl: null }]);
    expect(await store.deleteImage(image)).toBe('accepted');
    expect(deleteImage).toHaveBeenCalledWith('image-1', 7, expect.any(String), expect.any(AbortSignal));
    expect(store.images()).toHaveLength(1);
    expect(store.images()[0]?.availability).toBe('unavailable');
    expect(store.images()[0]?.status).toBe('deleting');
    deleteImage.mockResolvedValue(undefined);
    expect(await store.deleteImage(image)).toBe('removed');
    expect(store.images()).toHaveLength(0);
    expect(store.uploads()).toHaveLength(0);
  });

  it('preserves a failed deletion row and retries with a fresh idempotency key', async () => {
    const deleteImage = vi.fn().mockRejectedValueOnce({ status: 503 }).mockResolvedValueOnce(undefined);
    const image = { kind: 'image' as const, attachmentId: 'image-1', sourceId: 'image-1', displayFilename: 'photo.png', mediaType: 'image/png', byteCount: 10, availability: 'available' as const, version: 9 };
    const store = new ConversationAttachmentStore({ listImages: async () => [image], createImageUpload: async () => ({ id: 'upload', attachmentId: 'image-1', mediaType: 'image/png', version: 1 }), uploadImageContent: async () => image, previewImage: async () => new Blob(), deleteImage });
    await store.load();
    expect(await store.deleteImage(image)).toBe('failed');
    expect(store.images()).toEqual([image]);
    expect(await store.deleteImage(image)).toBe('removed');
    expect(deleteImage.mock.calls[0]?.[1]).toBe(9);
    expect(deleteImage.mock.calls[0]?.[2]).not.toBe(deleteImage.mock.calls[1]?.[2]);
  });

  it('treats an accepted deleting receipt as authoritative when refresh fails', async () => {
    const image = { kind: 'image' as const, attachmentId: 'image-accepted', sourceId: 'image-accepted', displayFilename: 'accepted.png', mediaType: 'image/png', byteCount: 10, availability: 'available' as const, version: 4 };
    const store = new ConversationAttachmentStore({
      listImages: async () => { throw new Error('refresh unavailable'); },
      createImageUpload: async () => ({ id: 'upload', attachmentId: image.attachmentId, mediaType: 'image/png', version: 1 }),
      uploadImageContent: async () => image,
      previewImage: async () => new Blob(),
      deleteImage: async () => ({ attachmentId: image.attachmentId, status: 'deleting' as const, version: 5, acceptedAt: '2026-01-01T00:00:00Z' }),
    });
    store.images.set([image]);

    expect(await store.deleteImage(image)).toBe('accepted');
    expect(store.images()[0]).toMatchObject({ attachmentId: image.attachmentId, availability: 'unavailable', status: 'deleting', version: 5 });
  });

  it('does not repopulate images or thumbnails after a principal reset invalidates a delayed request', async () => {
    let resolveImages: ((images: ReadonlyArray<never>) => void) | undefined;
    const listImages = vi.fn(() => new Promise<ReadonlyArray<never>>((resolve) => { resolveImages = resolve; }));
    const store = new ConversationAttachmentStore({ listImages, createImageUpload: async () => ({ id: 'upload', attachmentId: 'image-1', mediaType: 'image/png', version: 1 }), uploadImageContent: async () => { throw new Error('unused'); }, previewImage: async () => new Blob(), deleteImage: async () => undefined });
    const loading = store.load();
    store.reset();
    resolveImages?.([]);
    await loading;
    expect(store.images()).toEqual([]);
    expect(store.loading()).toBe(false);
  });

  it('refreshes the backend version from a failed deletion receipt before retrying', async () => {
    const original = { kind: 'image' as const, attachmentId: 'image-1', sourceId: 'image-1', displayFilename: 'photo.png', mediaType: 'image/png', byteCount: 10, availability: 'available' as const, version: 7 };
    let current = original;
    const deleteImage = vi.fn(async (_id: string, version: number) => {
      if (deleteImage.mock.calls.length === 1) {
        current = { ...original, availability: 'unavailable', status: 'failed', version: version + 1 };
        return { attachmentId: 'image-1', status: 'failed' as const, version: version + 1, acceptedAt: '2026-01-01T00:00:00Z' };
      }
      return undefined;
    });
    const store = new ConversationAttachmentStore({ listImages: async () => [current], createImageUpload: async () => ({ id: 'upload', attachmentId: 'image-1', mediaType: 'image/png', version: 1 }), uploadImageContent: async () => original, previewImage: async () => new Blob(), deleteImage });
    await store.load();
    expect(await store.deleteImage(original)).toBe('failed');
    const failed = store.images()[0];
    expect(failed?.version).toBe(8);
    expect(failed?.status).toBe('failed');
    expect(failed).toBeTruthy();
    if (!failed) return;
    expect(await store.deleteImage(failed)).toBe('removed');
    expect(deleteImage.mock.calls[1]?.[1]).toBe(8);
    expect(store.images()).toHaveLength(0);
  });

  it('does not apply delayed upload, deletion, or preview results after reset', async () => {
    let resolveUpload: ((session: { readonly id: string; readonly attachmentId: string; readonly mediaType: string; readonly version: number }) => void) | undefined;
    let resolveDelete: (() => void) | undefined;
    let resolvePreview: ((blob: Blob) => void) | undefined;
    const image = { kind: 'image' as const, attachmentId: 'image-1', sourceId: 'image-1', displayFilename: 'photo.png', mediaType: 'image/png', byteCount: 68, availability: 'available' as const, version: 1 };
    const store = new ConversationAttachmentStore({ listImages: async () => [image], createImageUpload: async () => new Promise((resolve) => { resolveUpload = resolve; }), uploadImageContent: async () => image, previewImage: async () => new Promise((resolve) => { resolvePreview = resolve; }), deleteImage: async () => new Promise((resolve) => { resolveDelete = resolve; }) });
    await store.load();
    const file = { name: 'photo.png', size: 68, type: 'image/png', slice: () => ({ arrayBuffer: async () => new Uint8Array([0x89, 0x50, 0x4e, 0x47]) }) } as unknown as File;
    const upload = store.upload(file);
    const deletion = store.deleteImage(image);
    const preview = store.preview('image-1');
    store.reset();
    resolveUpload?.({ id: 'upload', attachmentId: 'image-1', mediaType: 'image/png', version: 1 });
    resolveDelete?.();
    resolvePreview?.(new Blob(['pixels'], { type: 'image/png' }));
    expect(await upload).toBe(false);
    expect(await deletion).toBe('failed');
    expect(await preview).toBeNull();
    expect(store.images()).toEqual([]);
    expect(store.uploads()).toEqual([]);
  });
});
