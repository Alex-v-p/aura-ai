import { getTestBed, TestBed } from '@angular/core/testing';
import { BrowserDynamicTestingModule, platformBrowserDynamicTesting } from '@angular/platform-browser-dynamic/testing';
import type { ActiveGeneratedArtifactDetail } from '@aura/aura-api-client';
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';
import { ARTIFACTS_API, type ArtifactsApi } from './artifacts-models';
import { ArtifactApiError, AuraArtifactsApi } from './artifacts-api';
import { ArtifactsStore } from './artifacts-store';

const revision = { revision: 1, mediaType: 'text/plain; charset=utf-8' as const, sha256: 'a'.repeat(64), byteCount: 5, creatorComponent: 'research', previousRevision: null, createdAt: '2026-10-30T00:00:00Z' };
function artifact(id: string): ActiveGeneratedArtifactDetail { return { id, kind: 'generated_text', title: `Artifact ${id}`, mediaType: 'text/plain; charset=utf-8', contentState: 'active', currentRevision: 1, version: 1, capabilities: { canPreview: true, canDownload: true, canDelete: true, canRetryCleanup: false, canReconcileCleanup: false }, createdAt: '2026-10-30T00:00:00Z', updatedAt: '2026-10-30T00:00:00Z', lineage: { taskId: null, runId: null, conversationId: null, workspaceId: null }, revision }; }

function apiHarness(overrides: Partial<ArtifactsApi> = {}): { readonly store: ArtifactsStore; readonly api: ArtifactsApi } {
  const api: ArtifactsApi = { clearSession: vi.fn(), list: vi.fn(async () => ({ items: [], nextCursor: null })), get: vi.fn(async (id: string) => artifact(id)), revisions: vi.fn(async () => [revision]), preview: vi.fn(async (id: string) => ({ artifactId: id, revision: 1, mediaType: revision.mediaType, text: 'hello', truncated: false })), download: vi.fn(), delete: vi.fn(), retryCleanup: vi.fn(), reconcileCleanup: vi.fn(), ...overrides };
  TestBed.configureTestingModule({ providers: [{ provide: ARTIFACTS_API, useValue: api }, ArtifactsStore] });
  return { store: TestBed.inject(ArtifactsStore), api };
}

describe('ArtifactsStore and authorized artifact transport', () => {
  beforeAll(() => getTestBed().initTestEnvironment(BrowserDynamicTestingModule, platformBrowserDynamicTesting()));
  afterEach(() => { TestBed.resetTestingModule(); vi.unstubAllGlobals(); });

  it('clears private state and pending flags on an authorized detail failure', async () => {
    const api = { get: vi.fn(async () => { throw Object.assign(new Error('expired'), { status: 401 }); }) };
    const harness = apiHarness(api);
    await harness.store.loadDetail('artifact-1');
    expect(harness.store.signedOut()).toBe(true);
    expect(harness.store.selected()).toBeNull();
    expect(harness.store.loading()).toBe(false);
    expect(harness.store.saving()).toBe(false);
    expect(harness.api.clearSession).toHaveBeenCalledOnce();
  });

  it('does not publish a late detail response after the route identity changes', async () => {
    let resolveA: ((value: ActiveGeneratedArtifactDetail) => void) | undefined;
    let resolveB: ((value: ActiveGeneratedArtifactDetail) => void) | undefined;
    const api = { get: vi.fn((id: string) => id === 'a' ? new Promise<ActiveGeneratedArtifactDetail>(resolve => { resolveA = resolve; }) : new Promise<ActiveGeneratedArtifactDetail>(resolve => { resolveB = resolve; })) };
    const harness = apiHarness(api);
    const first = harness.store.loadDetail('a');
    const second = harness.store.loadDetail('b');
    resolveB?.(artifact('b'));
    await second;
    resolveA?.(artifact('a'));
    await first;
    expect(harness.store.selected()?.id).toBe('b');
  });

  it('appends authoritative cursor pages instead of hiding older artifacts', async () => {
    const list = vi.fn(async (_mediaType?: string, cursor?: string) => cursor ? { items: [artifact('older')], nextCursor: null } : { items: [artifact('newer')], nextCursor: 'cursor-1' });
    const harness = apiHarness({ list });
    await harness.store.load();
    await harness.store.load(undefined, harness.store.page().nextCursor ?? undefined);
    expect(harness.store.items().map(item => item.id)).toEqual(['newer', 'older']);
    expect(harness.store.page().nextCursor).toBeNull();
    expect(list.mock.calls[1]?.[1]).toBe('cursor-1');
  });

  it('accepts only an authorized text download and rejects media confusion', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ csrfToken: 'csrf' }), { headers: { 'Content-Type': 'application/json' } }))
      .mockResolvedValueOnce(new Response('hello', { headers: { 'Content-Type': 'text/plain; charset=utf-8', 'Content-Length': '5', 'Content-Disposition': 'attachment; filename="artifact.txt"' } }));
    vi.stubGlobal('fetch', fetchMock);
    const api = new AuraArtifactsApi();
    await expect(api.download('artifact-1', 1, 'text/plain; charset=utf-8', 5)).resolves.toMatchObject({ contentLength: 5 });

    const hostileFetch = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ csrfToken: 'csrf' }), { headers: { 'Content-Type': 'application/json' } }))
      .mockResolvedValueOnce(new Response('<html>not an artifact</html>', { headers: { 'Content-Type': 'text/html', 'Content-Length': '5', 'Content-Disposition': 'attachment; filename="artifact.txt"' } }));
    vi.stubGlobal('fetch', hostileFetch);
    const hostileApi = new AuraArtifactsApi();
    await expect(hostileApi.download('artifact-1', 1, 'text/plain; charset=utf-8', 5)).rejects.toBeInstanceOf(ArtifactApiError);
  });
});
