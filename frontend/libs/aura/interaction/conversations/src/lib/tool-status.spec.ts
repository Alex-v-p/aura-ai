import { describe, expect, it } from 'vitest';
import type { ToolStatusData } from '@aura/aura-api-client';
import { appendToolStatusActivity } from './conversation-store';

const status = (value: ToolStatusData['status']): ToolStatusData => ({ callId: 'call-1', toolId: 'memory.search', toolVersion: 1, status: value });

describe('tool status activity', () => {
  it('preserves the ordered lifecycle and keeps only metadata', () => {
    const events = appendToolStatusActivity(appendToolStatusActivity([], status('pending')), status('executing'));
    const completed = appendToolStatusActivity(events, { ...status('completed'), resultCount: 2 });
    expect(completed.map((event) => event.status)).toEqual(['pending', 'executing', 'completed']);
    expect(completed[2]).toEqual({ callId: 'call-1', toolId: 'memory.search', toolVersion: 1, status: 'completed', resultCount: 2 });
  });
});
