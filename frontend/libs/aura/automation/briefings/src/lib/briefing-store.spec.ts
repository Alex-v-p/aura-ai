import { getTestBed, TestBed } from '@angular/core/testing';
import { BrowserDynamicTestingModule, platformBrowserDynamicTesting } from '@angular/platform-browser-dynamic/testing';
import { signal } from '@angular/core';
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';
import { AgentStore } from '@aura/aura/interaction/agents';
import { CalendarStore } from '@aura/aura/integrations/calendar';
import { DocumentsStore } from '@aura/aura/knowledge/documents';
import { BRIEFING_API, type BriefingApi, type BriefingSchedule } from './briefing-models';
import { BriefingStore } from './briefing-store';

vi.mock('@aura/aura/interaction/agents', () => ({ AgentStore: class AgentStore {} }));
vi.mock('@aura/aura/integrations/calendar', () => ({ CalendarStore: class CalendarStore {} }));
vi.mock('@aura/aura/knowledge/documents', () => ({ DocumentsStore: class DocumentsStore {} }));
vi.mock('./briefing-api', () => ({ AuraBriefingApi: class AuraBriefingApi {}, BriefingApiError: class BriefingApiError extends Error { readonly status: number; constructor(status: number, message: string) { super(message); this.status = status; } } }));

const capability = { executionPolicyRevisionId: 'policy', executionPolicyRevision: 'briefing-execution-v1', deadlineSeconds: 300, maxOutputTokens: 16384, maxOutputChars: 32768, cadenceKind: 'daily' as const, memoryEnabled: false, toolsEnabled: false };
const profile = { status: 'active', currentRevisionId: 'agent-revision', revisions: [{ revisionId: 'agent-revision', revision: 2, displayName: 'Owner agent', promptBundleRevisionId: 'prompt', modelPolicyRevisionId: 'model', toolPolicyRevisionId: 'tool' }] };
const schedule = { id: 'schedule-new', version: 1, displayName: 'New schedule', status: 'active', cadence: { kind: 'daily', localTime: '08:00', timeZone: 'Europe/Brussels' }, sources: { includeReminders: true, calendarIds: ['calendar'], documentIds: ['document'] }, nextDueAt: '2026-10-27T08:00:00Z', createdAt: '2026-10-27T07:00:00Z', updatedAt: '2026-10-27T07:00:00Z' } as unknown as BriefingSchedule;

type Deferred<T> = { promise: Promise<T>; resolve: (value: T) => void; reject: (error: unknown) => void };
function deferred<T>(): Deferred<T> { let resolve!: (value: T) => void; let reject!: (error: unknown) => void; const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej; }); return { promise, resolve, reject }; }

function harness(apiOverrides: Partial<BriefingApi> = {}, options: { readonly nextCursor?: string | null; readonly calendarSelected?: boolean; readonly calendarComplete?: boolean } = {}) {
  const calendarItems = signal([{ id: 'calendar', displayName: 'Personal', timeZone: 'Europe/Brussels', selected: options.calendarSelected ?? true }]);
  const documentPage = signal({ items: [{ id: 'document', filename: 'Notes.md', mediaType: 'text/markdown', status: 'ready' as const, failure: null, byteCount: 12, version: 1, createdAt: '', updatedAt: '' }], nextCursor: options.nextCursor ?? null });
  const calendar = { connected: signal(true), calendarsComplete: signal(options.calendarComplete ?? true), requestState: signal<'idle' | 'loading'>('idle'), error: signal(null), calendars: calendarItems, load: vi.fn(async () => undefined), discoverCalendars: vi.fn(async () => { calendar.calendarsComplete.set(true); }) };
  const documents = { loading: signal(false), error: signal<string | null>(null), authRequired: signal(false), permissionDenied: signal(false), documents: documentPage, load: vi.fn(async () => undefined), loadMore: vi.fn(async () => { documentPage.set({ ...documentPage(), nextCursor: null }); }) };
  const agents = { activeAgents: () => [profile], load: vi.fn(async () => undefined) };
  const api: BriefingApi = { capabilities: vi.fn(async () => capability), list: vi.fn(async () => ({ items: [], nextCursor: null })), get: vi.fn(async () => schedule), create: vi.fn(async () => schedule), pause: vi.fn(async () => schedule), resume: vi.fn(async () => schedule), deleteSchedule: vi.fn(async () => ({ scope: 'schedule' } as never)), occurrences: vi.fn(async () => ({ items: [], nextCursor: null })), occurrence: vi.fn(async () => ({}) as never), deleteHistory: vi.fn(async () => ({ scope: 'occurrence_history' } as never)), result: vi.fn(async () => ({}) as never), deleteResult: vi.fn(async () => ({ scope: 'result_content' } as never)), ...apiOverrides };
  TestBed.configureTestingModule({ providers: [{ provide: BRIEFING_API, useValue: api }, { provide: AgentStore, useValue: agents }, { provide: CalendarStore, useValue: calendar }, { provide: DocumentsStore, useValue: documents }, BriefingStore] });
  return { store: TestBed.inject(BriefingStore), api, calendar, documents, agents };
}

function validDraft(store: BriefingStore, name = 'Morning briefing'): void { store.setDraft({ displayName: name, timeZone: 'Europe/Brussels', agentRevisionId: 'agent-revision', calendarIds: ['calendar'], documentIds: ['document'] }); }
function statusError(status: number): { readonly status: number } { return { status }; }

describe('BriefingStore behavior', () => {
  beforeAll(() => getTestBed().initTestEnvironment(BrowserDynamicTestingModule, platformBrowserDynamicTesting()));
  afterEach(() => TestBed.resetTestingModule());

  it('reuses an ambiguous create UUID, then rekeys after a material draft change', async () => {
    const calls: string[] = []; let attempts = 0;
    const { store } = harness({ create: vi.fn(async (_request, key) => { calls.push(key ?? ''); attempts += 1; throw statusError(503); }) });
    await store.loadCapabilities(); validDraft(store); await store.create(); await store.create();
    expect(attempts).toBe(2); expect(calls[0]).toMatch(/^[0-9a-f-]{36}$/); expect(calls[1]).toBe(calls[0]);
    store.setDraft({ displayName: 'Changed briefing' }); await store.create(); expect(calls[2]).not.toBe(calls[0]);
  });

  it('invalidates create identity on each source toggle and clears revoked ids before dispatch', async () => {
    const calls: string[] = []; const { store, calendar } = harness({ create: vi.fn(async (_request, key) => { calls.push(key ?? ''); throw statusError(503); }) });
    await store.loadCapabilities(); validDraft(store); await store.create(); store.toggleCalendar('calendar'); await store.create(); store.toggleCalendar('calendar'); await store.create();
    store.toggleDocument('document'); await store.create(); store.toggleDocument('document'); await store.create(); expect(new Set(calls).size).toBe(3); calendar.calendars.set([{ id: 'calendar', displayName: 'Personal', timeZone: 'Europe/Brussels', selected: false }]); const result = await store.create(); expect(result).toBeNull(); expect(store.draft().calendarIds).toEqual([]);
  });

  it('does not let a late old-session failure remove the replacement attempt key or state', async () => {
    const old = deferred<BriefingSchedule>(); const replacement = deferred<BriefingSchedule>(); const calls: string[] = [];
    const { store, api } = harness({ create: vi.fn(async (_request, key) => { calls.push(key ?? ''); return calls.length === 1 ? old.promise : replacement.promise; }) });
    await store.loadCapabilities(); validDraft(store); const first = store.create(); store.resetSessionState(); await store.loadCapabilities(); validDraft(store, 'Replacement'); const second = store.create(); old.reject(statusError(409)); await first; replacement.reject(statusError(503)); await second; await store.create();
    expect(calls[2]).toBe(calls[1]); expect(store.schedules()).toEqual([]); expect(api.create).toHaveBeenCalledTimes(3);
  });

  it('fails closed for pagination and exposes an actionable load-more path', async () => {
    const { store, documents } = harness({}, { nextCursor: 'next-page' }); await store.loadCapabilities(); validDraft(store); expect(store.sourcesComplete()).toBe(false); expect(await store.create()).toBeNull(); expect(documents.loadMore).not.toHaveBeenCalled(); await store.loadMoreDocuments(); expect(documents.loadMore).toHaveBeenCalledOnce(); expect(store.sourcesComplete()).toBe(true);
  });

  it('continues incomplete calendar discovery through the public paginated seam', async () => {
    const { store, calendar } = harness({}, { calendarComplete: false });
    await store.loadCapabilities();
    expect(store.sourcesComplete()).toBe(false);
    expect(store.calendarsHaveMore()).toBe(true);
    calendar.connected.set(false);
    calendar.requestState.set('loading');
    expect(store.sourceNotice()).toContain('Calendar sources are loading');
    expect(store.calendarsHaveMore()).toBe(false);
    await store.loadMoreCalendars();
    expect(calendar.discoverCalendars).not.toHaveBeenCalled();
    calendar.connected.set(true);
    calendar.requestState.set('idle');
    await store.loadMoreCalendars();
    expect(calendar.discoverCalendars).toHaveBeenCalledWith(false, true);
    expect(store.calendarsHaveMore()).toBe(false);
  });
});
