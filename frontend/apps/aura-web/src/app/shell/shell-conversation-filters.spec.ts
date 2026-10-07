import { describe, expect, it } from 'vitest';
import { normalizeActivityBoundary } from './shell-conversation-filters';

describe('conversation activity filter bounds', () => {
  it('uses an inclusive UTC start bound', () => {
    expect(normalizeActivityBoundary('activityFrom', '2026-10-01')).toBe('2026-10-01T00:00:00.000Z');
  });

  it('uses the next UTC day as an exclusive end bound', () => {
    expect(normalizeActivityBoundary('activityTo', '2026-10-01')).toBe('2026-10-02T00:00:00.000Z');
  });

  it('rejects malformed date input', () => {
    expect(normalizeActivityBoundary('activityFrom', 'not-a-date')).toBeUndefined();
  });
});
