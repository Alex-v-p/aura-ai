import { applicationConfig } from '@storybook/angular';
import type { Meta, StoryObj } from '@storybook/angular';
import { signal } from '@angular/core';
import { ActivatedRoute, convertToParamMap, provideRouter, withDisabledInitialNavigation } from '@angular/router';
import { of } from 'rxjs';
import type { DocumentSummary } from '@aura/aura-api-client';
import { DocumentsPageComponent, DocumentsStore } from '@aura/aura/knowledge/documents';

const now = '2026-10-13T09:00:00.000Z';
const ready: DocumentSummary = { id: 'doc-story', filename: 'A quiet guide.md', mediaType: 'text/markdown', byteCount: 12_400, status: 'ready', failure: null, version: 2, createdAt: now, updatedAt: now };
const processing: DocumentSummary = { ...ready, id: 'doc-processing', filename: 'Processing notes.pdf', mediaType: 'application/pdf', status: 'processing', version: 1 };
const failed: DocumentSummary = { ...ready, id: 'doc-failed', filename: 'Needs retry.docx', mediaType: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document', status: 'failed', failure: { code: 'parse_failed', retryable: true }, version: 3 };

function detailRoute(): ActivatedRoute { return { paramMap: of(convertToParamMap({ id: ready.id })) } as unknown as ActivatedRoute; }

function fixture(items: ReadonlyArray<DocumentSummary>, overrides: Record<string, unknown> = {}): DocumentsStore {
  const store = {
    documents: signal({ items, nextCursor: null }), selected: signal(null), preview: signal(null), searchResults: signal(null), query: signal(''), loading: signal(false), detailLoading: signal(false), previewLoading: signal(false), actionLoading: signal(false), error: signal(null), authRequired: signal(false), permissionDenied: signal(false), detailError: signal(null), notice: signal(null), uploads: signal([]), resetEpoch: signal(0), hasMore: signal(false), load: async () => undefined, loadMore: async () => undefined, loadDetail: async () => undefined, upload: async () => true, cancelUpload: () => undefined, retryUpload: () => undefined, removeUpload: () => undefined, retry: async () => true, remove: async () => true, clearSelection: () => undefined, download: async () => true, stopPolling: () => undefined, signIn: () => undefined,
  };
  return { ...store, ...overrides } as unknown as DocumentsStore;
}

function detailFixture(): DocumentsStore {
  const store = fixture([ready]);
  const value = store as unknown as Record<string, unknown>;
  value['selected'] = signal({ ...ready, extractedTextLength: 520, chunkCount: 3 });
  value['preview'] = signal({ documentId: ready.id, text: 'A bounded plain-text preview for the owner.', truncated: false, totalCharacters: 44 });
  return store;
}

function deletingDetailFixture(): DocumentsStore {
  const selected = signal({ ...ready, extractedTextLength: 520, chunkCount: 3 });
  const store = fixture([ready], { selected, remove: async () => { selected.set({ ...selected(), status: 'deleting' }); return true; } });
  return store;
}

const meta: Meta<DocumentsPageComponent> = { title: 'Aura/Knowledge Documents', component: DocumentsPageComponent, parameters: { layout: 'fullscreen' }, decorators: [applicationConfig({ providers: [{ provide: DocumentsStore, useValue: fixture([]) }, provideRouter([], withDisabledInitialNavigation())] })] };
export default meta;
type Story = StoryObj<DocumentsPageComponent>;
export const Empty: Story = { name: 'Empty library' };
export const Ready: Story = { name: 'Ready and processing documents', decorators: [applicationConfig({ providers: [{ provide: DocumentsStore, useValue: fixture([ready, processing]) }, provideRouter([], withDisabledInitialNavigation())] })] };
export const Failed: Story = { name: 'Retryable parse failure', decorators: [applicationConfig({ providers: [{ provide: DocumentsStore, useValue: fixture([failed]) }, provideRouter([], withDisabledInitialNavigation())] })] };
export const DarkDetail: Story = { name: 'Detail and deletion confirmation', decorators: [applicationConfig({ providers: [provideRouter([], withDisabledInitialNavigation()), { provide: ActivatedRoute, useValue: detailRoute() }, { provide: DocumentsStore, useValue: detailFixture() }] })] };
export const Loading: Story = { name: 'Loading library', decorators: [applicationConfig({ providers: [{ provide: DocumentsStore, useValue: fixture([], { loading: signal(true), load: () => new Promise<void>(() => undefined) }) }, provideRouter([], withDisabledInitialNavigation())] })] };
export const DuplicateUpload: Story = { name: 'Duplicate upload feedback', decorators: [applicationConfig({ providers: [{ provide: DocumentsStore, useValue: fixture([ready], { uploads: signal([{ key: 'duplicate-story', filename: ready.filename, byteCount: ready.byteCount, mediaType: ready.mediaType, progress: 100, state: 'duplicate', message: 'Aura already has this content; no duplicate was created.', documentId: ready.id }]) }) }, provideRouter([], withDisabledInitialNavigation())] })] };
export const Unavailable: Story = { name: 'Object store unavailable', decorators: [applicationConfig({ providers: [{ provide: DocumentsStore, useValue: fixture([], { error: signal('The document service is temporarily unavailable. Try again shortly.') }) }, provideRouter([], withDisabledInitialNavigation())] })] };
export const ReadyDetailPreview: Story = { name: 'Ready detail with bounded preview', decorators: [applicationConfig({ providers: [provideRouter([], withDisabledInitialNavigation()), { provide: ActivatedRoute, useValue: detailRoute() }, { provide: DocumentsStore, useValue: detailFixture() }] })] };
export const DeletionConfirmation: Story = { name: 'Permanent deletion confirmation', decorators: [applicationConfig({ providers: [provideRouter([], withDisabledInitialNavigation()), { provide: ActivatedRoute, useValue: detailRoute() }, { provide: DocumentsStore, useValue: deletingDetailFixture() }] })], play: async ({ canvasElement }) => { canvasElement.querySelector<HTMLButtonElement>('#document-delete-action')?.click(); await new Promise((resolve) => setTimeout(resolve, 0)); canvasElement.querySelector<HTMLButtonElement>('.confirm-dialog .danger-button')?.click(); await new Promise((resolve) => setTimeout(resolve, 20)); const target = canvasElement.querySelector<HTMLElement>('#document-deletion-status'); if (!target || target.ownerDocument.activeElement !== target) throw new Error('Accepted deletion must focus the deletion-in-progress status.'); } };
