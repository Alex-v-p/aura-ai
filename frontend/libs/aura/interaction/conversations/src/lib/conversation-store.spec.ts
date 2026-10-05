import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ConversationStore } from './conversation-store';

describe('ConversationStore', () => {
  let store: ConversationStore;
  beforeEach(() => { vi.useFakeTimers(); store = new ConversationStore(); });
  it('keeps drafts in memory and validates empty sends', () => { store.updateDraft(''); expect(store.send()).toBe(false); expect(store.notice()).toContain('Write a message'); });
  it('adds a deterministic local reply after a prompt', () => { store.updateDraft('A small thought'); expect(store.send()).toBe(true); expect(store.selected().turns).toHaveLength(1); vi.advanceTimersByTime(850); expect(store.selected().turns).toHaveLength(2); expect(store.selected().turns[1].text).toContain('A small thought'); });
  it('preserves and marks the user turn when generation is stopped', () => { store.updateDraft('Keep this visible'); store.send(); store.stop(); expect(store.selected().turns[0].text).toBe('Keep this visible'); expect(store.selected().turns[0].state).toBe('interrupted'); expect(store.runState()).toBe('interrupted'); });
  it('marks a working conversation interrupted when creating another conversation', () => {
    store.updateDraft('Keep this before creating');
    store.send();
    const originalId = store.selectedId();
    store.create();
    store.select(originalId);
    expect(store.selected().turns[0].state).toBe('interrupted');
  });
  it('marks a working conversation interrupted when selecting another conversation', () => {
    store.create();
    const workingId = store.selectedId();
    store.updateDraft('Keep this before switching');
    store.send();
    store.select('welcome');
    store.select(workingId);
    expect(store.selected().turns[0].state).toBe('interrupted');
  });
  it('keeps the user turn and exposes a recoverable local error', () => {
    store.failNextLocalReply();
    store.updateDraft('Keep my work safe');
    store.send();
    vi.advanceTimersByTime(850);
    expect(store.selected().turns).toHaveLength(1);
    expect(store.runState()).toBe('error');
    expect(store.notice()).toContain('Your message is still here');
    store.retry();
    expect(store.runState()).toBe('working');
    vi.advanceTimersByTime(850);
    expect(store.runState()).toBe('idle');
    expect(store.selected().turns).toHaveLength(2);
    expect(store.notice()).toBeNull();
  });
});
