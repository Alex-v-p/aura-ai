import { describe, expect, it } from 'vitest';
import { CalendarSessionHydrator } from './calendar-session-hydration';

describe('CalendarSessionHydrator', () => {
  it('deduplicates token loads and invalidates an in-flight session when reset', async () => {
    let resolve: ((token: string) => void) | undefined;
    let calls = 0;
    const hydrator = new CalendarSessionHydrator(() => { calls += 1; return new Promise<string>((done) => { resolve = done; }); }, () => new Error('session changed'));
    const first = hydrator.ensure();
    const second = hydrator.ensure();
    expect(calls).toBe(1);
    hydrator.clearSession();
    resolve?.('stale-token');
    await expect(first).rejects.toThrow('session changed');
    await expect(second).rejects.toThrow('session changed');
    expect(hydrator.currentToken()).toBe('');
  });
});
