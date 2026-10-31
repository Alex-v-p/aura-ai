import { getTestBed, TestBed } from '@angular/core/testing';
import { BrowserDynamicTestingModule, platformBrowserDynamicTesting } from '@angular/platform-browser-dynamic/testing';
import type { CreateResearchRequest, DeletedResearchTombstone, ResearchAdmission, ResearchBudget, ResearchDetail } from '@aura/aura-api-client';
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';
import { RESEARCH_API, type ResearchApi } from './research-models';
import { ResearchStore } from './research-store';

const budget: ResearchBudget = { wallTimeSeconds: 90, fetchedSources: 6, searchRequests: 6, browserPages: 0, modelCalls: 1 };
const request: CreateResearchRequest = { question: 'Compare tools', mode: 'quick', budget };
const admission: ResearchAdmission = { researchId: 'research-1', taskRef: { taskId: 'task-1' }, mode: 'quick', admissionState: 'admitted', admittedBudget: budget, version: 1, createdAt: '2026-10-30T00:00:00Z' };

function apiHarness(create: ResearchApi['create'], overrides: Partial<ResearchApi> = {}): { readonly store: ResearchStore; readonly api: ResearchApi } {
  const api = { clearSession: vi.fn(), getSettings: vi.fn(async () => { throw new Error('not used'); }), replaceSettings: vi.fn(), list: vi.fn(async () => ({ items: [], nextCursor: null })), create, get: vi.fn(), evidence: vi.fn(), cancel: vi.fn(), extend: vi.fn(), delete: vi.fn(), retryCleanup: vi.fn(), reconcileCleanup: vi.fn(), subscribe: vi.fn(() => () => undefined), ...overrides } as unknown as ResearchApi;
  TestBed.configureTestingModule({ providers: [{ provide: RESEARCH_API, useValue: api }, ResearchStore] });
  return { store: TestBed.inject(ResearchStore), api };
}

describe('ResearchStore admission settlement', () => {
  beforeAll(() => getTestBed().initTestEnvironment(BrowserDynamicTestingModule, platformBrowserDynamicTesting()));
  afterEach(() => TestBed.resetTestingModule());

  it('retires saving before handing the admitted ID to the destination route', async () => {
    const { store, api } = apiHarness(vi.fn(async () => admission));
    await expect(store.create(request)).resolves.toBe('research-1');
    expect(api.get).not.toHaveBeenCalled();
    expect(store.saving()).toBe(false);
    expect(store.admissionHandoff()).toBe('research-1');
    expect(store.consumeAdmissionHandoff('other')).toBe(false);
    expect(store.consumeAdmissionHandoff('research-1')).toBe(true);
    expect(store.consumeAdmissionHandoff('research-1')).toBe(false);
  });

  it('retains saving settlement and does not create an admission handoff after failure', async () => {
    const failure = Object.assign(new Error('admission unavailable'), { status: 503 });
    const { store } = apiHarness(vi.fn(async () => { throw failure; }));
    await expect(store.create(request)).resolves.toBeNull();
    expect(store.saving()).toBe(false);
    expect(store.admissionHandoff()).toBeNull();
  });

  it('does not publish a late detail response after the resource generation changes', async () => {
    let resolveA: ((value: ResearchDetail) => void) | undefined;
    let resolveB: ((value: ResearchDetail) => void) | undefined;
    const detail = (id: string): ResearchDetail => ({ id, taskRef: { taskId: `task-${id}` }, mode: 'quick', stage: 'queued', outcome: null, contentState: 'active', version: 1, capabilities: { canCancel: true, canExtend: true, canDelete: true, canRetryCleanup: false, canReconcileCleanup: false }, createdAt: '2026-10-30T00:00:00Z', updatedAt: '2026-10-30T00:00:00Z', finishedAt: null, question: id, admittedBudget: budget, consumedBudget: budget, extensionAccepted: false, sourceCount: 0, failureClass: null, artifactRefs: [], citations: [] });
    const { store } = apiHarness(vi.fn(async () => admission), { get: vi.fn((id: string) => id === 'a' ? new Promise<ResearchDetail>(resolve => { resolveA = resolve; }) : new Promise<ResearchDetail>(resolve => { resolveB = resolve; })), evidence: vi.fn(async () => ({ items: [], nextCursor: null })), getSettings: vi.fn(async () => ({ version: 1, presets: [], updatedAt: '2026-10-30T00:00:00Z' })) });
    const first = store.loadDetail('a');
    const second = store.loadDetail('b');
    resolveB?.(detail('b'));
    await second;
    resolveA?.(detail('a'));
    await first;
    expect(store.selected()?.id).toBe('b');
  });

  it('sends cleanup recovery with the current version and one stable idempotency key', async () => {
    const tombstone: DeletedResearchTombstone = { id: 'research-1', taskRef: { taskId: 'task-1' }, mode: 'quick', stage: 'terminal', outcome: 'completed', contentState: 'deletion_failed', version: 7, capabilities: { canCancel: false, canExtend: false, canDelete: false, canRetryCleanup: true, canReconcileCleanup: false }, createdAt: '2026-10-30T00:00:00Z', updatedAt: '2026-10-30T00:00:00Z', finishedAt: '2026-10-30T00:01:00Z', tombstoneLabel: 'Research source deleted' };
    const api = { get: vi.fn(async () => tombstone), list: vi.fn(async () => ({ items: [], nextCursor: null })), retryCleanup: vi.fn(async (_id: string, _version: number, _key: string) => tombstone), getSettings: vi.fn(async () => ({ version: 1, presets: [], updatedAt: '2026-10-30T00:00:00Z' })), evidence: vi.fn(async () => ({ items: [], nextCursor: null })), subscribe: vi.fn(() => () => undefined) };
    const harness = apiHarness(vi.fn(async () => admission), api);
    await harness.store.loadDetail('research-1');
    await harness.store.retryCleanup();
    await harness.store.retryCleanup();
    expect(api.retryCleanup).toHaveBeenCalledTimes(2);
    expect(api.retryCleanup.mock.calls[0]?.[1]).toBe(7);
    expect(api.retryCleanup.mock.calls[0]?.[2]).toMatch(/^[0-9a-f]{32}$/);
    expect(api.retryCleanup.mock.calls[1]?.[2]).not.toBe(api.retryCleanup.mock.calls[0]?.[2]);
  });
});
