import { afterEach, describe, expect, it, vi } from 'vitest';
import type { RunEvent } from '@aura/aura-api-client';
import { AuraConversationApi } from './conversation-api';

const runId = '00000000-0000-0000-0000-000000000001';
const conversationId = '00000000-0000-0000-0000-000000000002';

function event(eventType: RunEvent['eventType'], eventId: string): RunEvent {
  const base = { schemaVersion: 1 as const, eventId, sequence: 1, eventType, runId, conversationId, occurredAt: new Date().toISOString() };
  if (eventType === 'heartbeat') return { ...base, eventType, data: { serverTime: new Date().toISOString() } };
  return { ...base, eventType: 'assistant.delta', data: { messageId: '00000000-0000-0000-0000-000000000003', offset: 0, text: 'hello' } };
}

describe('AuraConversationApi SSE transport', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('uses SSE id fields as cursors and does not advance on heartbeats', async () => {
    const response = (payload: string): Response => {
      const stream = new ReadableStream<Uint8Array>({
        start(controller) {
          controller.enqueue(new TextEncoder().encode(payload));
          controller.close();
        },
      });
      return new Response(stream, { status: 200, headers: { 'Content-Type': 'text/event-stream' } });
    };
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(response([
        `id: persisted-1\nevent: assistant.delta\ndata: ${JSON.stringify(event('assistant.delta', 'json-1'))}\n\n`,
        `event: heartbeat\ndata: ${JSON.stringify(event('heartbeat', 'heartbeat-json-id'))}\n\n`,
      ].join('')))
      .mockResolvedValueOnce(response(`id: persisted-2\nevent: assistant.delta\ndata: ${JSON.stringify(event('assistant.delta', 'json-2'))}\n\n`));
    vi.stubGlobal('fetch', fetchMock);

    const received: Array<{ event: RunEvent; cursor?: string }> = [];
    const errors: unknown[] = [];
    new AuraConversationApi().streamRunEvents(runId, undefined, (receivedEvent, cursor) => received.push({ event: receivedEvent, cursor }), (error) => errors.push(error));
    await new Promise<void>((resolve) => setTimeout(resolve, 0));

    const reconnectCursor = received.find((item) => item.event.eventType === 'assistant.delta')?.cursor;
    new AuraConversationApi().streamRunEvents(runId, reconnectCursor, (receivedEvent, cursor) => received.push({ event: receivedEvent, cursor }), (error) => errors.push(error));
    await new Promise<void>((resolve) => setTimeout(resolve, 0));

    expect(errors).toEqual([]);
    expect(fetchMock.mock.calls[1]?.[1]).toMatchObject({ headers: { Accept: 'text/event-stream', 'Last-Event-ID': 'persisted-1' } });
    expect(received.map((item) => [item.event.eventType, item.cursor])).toEqual([
      ['assistant.delta', 'persisted-1'],
      ['heartbeat', undefined],
      ['assistant.delta', 'persisted-2'],
    ]);
  });
});
