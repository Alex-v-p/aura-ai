import { afterEach, describe, expect, it, vi } from 'vitest';
import { ResearchTransport } from './research-api';

function event(taskId: string, eventId: string, sourceVersion: number): string {
  return `id: ${eventId}\ndata: ${JSON.stringify({ eventId, taskId, sourceKind: 'research', sourceId: taskId, sourceVersion, occurredAt: new Date().toISOString() })}\n\n`;
}

describe('ResearchTransport stream recovery', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('drops an expired Last-Event-ID before reconnecting after a gap', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(event('task-1', 'event-1', 2), { headers: { 'Content-Type': 'text/event-stream' } }))
      .mockResolvedValueOnce(new Response(null, { status: 410 }))
      .mockResolvedValueOnce(new Response(event('task-1', 'event-2', 3), { headers: { 'Content-Type': 'text/event-stream' } }));
    vi.stubGlobal('fetch', fetchMock);
    const received: string[] = [];
    const transport = new ResearchTransport();
    const stop = transport.stream('task-1', null, (value) => received.push(value.eventId), () => undefined);
    await vi.waitFor(() => expect(received).toContain('event-2'), { timeout: 3_000 });
    expect(fetchMock.mock.calls[1][1].headers['Last-Event-ID']).toBe('event-1');
    expect(fetchMock.mock.calls[2][1].headers['Last-Event-ID']).toBeUndefined();
    expect(fetchMock).toHaveBeenCalledTimes(3);
    stop();
  });
});
