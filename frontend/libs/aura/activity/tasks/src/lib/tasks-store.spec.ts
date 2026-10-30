import { getTestBed, TestBed } from '@angular/core/testing';
import { BrowserDynamicTestingModule, platformBrowserDynamicTesting } from '@angular/platform-browser-dynamic/testing';
import type { DurableTaskDetail, TaskCommandReceipt, TaskStreamEvent } from '@aura/aura-api-client';
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';
import { TaskListRequestGate } from './tasks-models';
import { TASKS_API, type TasksApi } from './tasks-models';
import { TasksStore } from './tasks-store';

function task(version: number): DurableTaskDetail { return { id: 'task-1', sourceKind: 'research', sourceId: 'research-1', sourceVersion: version, status: 'running', sourceStatus: 'running', sourceOutcome: null, stage: 'searching', progress: .5, capabilities: { canCancel: true, canRetry: true, hasSourceDetail: true }, routeKey: 'research_detail', createdAt: '2026-10-30T00:00:00Z', updatedAt: '2026-10-30T00:00:00Z', displayLabel: 'Research', failureClass: null, recoveryAction: null, artifactRefs: [] }; }

function apiHarness(gets: ReadonlyArray<DurableTaskDetail>, receipt: TaskCommandReceipt) {
  let eventHandler: ((event: TaskStreamEvent) => void) | undefined;
  let getIndex = 0;
  const api: TasksApi = { clearSession: vi.fn(), list: vi.fn(async () => ({ items: [], nextCursor: null })), get: vi.fn(async () => gets[Math.min(getIndex++, gets.length - 1)]), cancel: vi.fn(async () => receipt), retry: vi.fn(async () => receipt), subscribe: vi.fn((_id, _lastEventId, onEvent) => { eventHandler = onEvent; return () => undefined; }) };
  TestBed.configureTestingModule({ providers: [{ provide: TASKS_API, useValue: api }, TasksStore] });
  return { store: TestBed.inject(TasksStore), api, emit: (event: TaskStreamEvent) => eventHandler?.(event) };
}

describe('Task Center list initialization', () => {
  beforeAll(() => getTestBed().initTestEnvironment(BrowserDynamicTestingModule, platformBrowserDynamicTesting()));
  afterEach(() => TestBed.resetTestingModule());

  it('coalesces duplicate same-generation list initialization and preserves the first request', async () => {
    let resolve: ((value: string) => void) | undefined;
    let calls = 0;
    const gate = new TaskListRequestGate<string>();
    const start = () => { calls += 1; return new Promise<string>((done) => { resolve = done; }); };
    const first = gate.run(start);
    const second = gate.run(start);
    expect(calls).toBe(1);
    expect(second).toBe(first);
    resolve?.('first list response');
    await expect(first).resolves.toBe('first list response');
    expect(gate.peek()).toBeNull();
  });

  it('keeps rejected command truth at the same authoritative version', async () => {
    const harness = apiHarness([task(1)], { taskId: 'task-1', command: 'cancel', state: 'rejected', sourceVersion: 1 });
    await harness.store.loadDetail('task-1');
    await harness.store.cancel();
    expect(harness.store.commandState()).toBe('rejected');
    expect(harness.store.saving()).toBe(false);
  });

  it('settles an accepted command only after matching REST version advancement', async () => {
    const harness = apiHarness([task(1), task(1), task(2)], { taskId: 'task-1', command: 'cancel', state: 'accepted', sourceVersion: 1 });
    await harness.store.loadDetail('task-1');
    await harness.store.cancel();
    expect(harness.store.commandState()).toBe('accepted');
    harness.emit({ eventId: 'event-2', taskId: 'task-1', sourceKind: 'research', sourceId: 'research-1', sourceVersion: 2, occurredAt: new Date().toISOString() });
    await vi.waitFor(() => expect(harness.store.commandState()).toBe('idle'));
    expect(harness.store.saving()).toBe(false);
    const firstKey = vi.mocked(harness.api.cancel).mock.calls[0]?.[2];
    await harness.store.cancel();
    const secondKey = vi.mocked(harness.api.cancel).mock.calls[1]?.[2];
    expect(firstKey).toMatch(/^[0-9a-f]{32}$/);
    expect(secondKey).toMatch(/^[0-9a-f]{32}$/);
    expect(secondKey).not.toBe(firstKey);
  });

  it('fences a late detail response after session reset', async () => {
    let resolve: ((value: DurableTaskDetail) => void) | undefined;
    const harness = apiHarness([task(1)], { taskId: 'task-1', command: 'cancel', state: 'accepted', sourceVersion: 1 });
    vi.mocked(harness.api.get).mockImplementationOnce(() => new Promise<DurableTaskDetail>(done => { resolve = done; }));
    const pending = harness.store.loadDetail('task-1');
    harness.store.resetSessionState();
    resolve?.(task(1));
    await pending;
    expect(harness.store.selected()).toBeNull();
    expect(harness.store.loading()).toBe(false);
    expect(harness.store.saving()).toBe(false);
  });
});
