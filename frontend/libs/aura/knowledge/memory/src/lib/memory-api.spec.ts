import { describe, expect, it } from 'vitest';
import { CreateRequestIdempotency, publicMemoryError } from './memory-api-support';

const draft = {
  content: 'The owner prefers quiet mornings.',
  kind: 'preference',
  scope: { type: 'user' },
  confidence: .9,
  importance: .6,
  halfLifeDays: 180,
};

describe('Memory API safety helpers', () => {
  it('maps status errors to bounded copy and never echoes secret or HTML bodies', () => {
    const copy = publicMemoryError(500);
    expect(copy).not.toContain('secret-provider-token');
    expect(copy).not.toContain('<script>');
    expect(copy).toContain('draft is still here');
    expect(publicMemoryError(401)).toContain('session has expired');
    expect(publicMemoryError(403)).toContain('permission');
  });

  it('reuses uncertain create idempotency and resets after success, cancel, or session reset', () => {
    const idempotency = new CreateRequestIdempotency();
    const first = idempotency.next(draft);
    expect(idempotency.next(draft)).toBe(first);
    expect(idempotency.next({ ...draft, content: 'A changed draft.' })).not.toBe(first);

    idempotency.clear();
    const afterSuccess = idempotency.next(draft);
    expect(afterSuccess).not.toBe(first);
    idempotency.clear();
    expect(idempotency.next(draft)).not.toBe(afterSuccess);
  });
});
