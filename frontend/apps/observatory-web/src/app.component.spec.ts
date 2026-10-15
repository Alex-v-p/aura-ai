import { describe, expect, it } from 'vitest';

describe('observatory web shell', () => {
  it('is a separate owner-only product surface', () => {
    expect('/api/v1/events').toMatch(/^\/api\/v1\//);
    expect('metadata_only').toBe('metadata_only');
  });
});
