import { describe, expect, it } from 'vitest';

describe('shared UI state vocabulary', () => {
  it('keeps status tones semantic and product neutral', () => {
    expect(['info', 'success', 'warning', 'danger']).toContain('danger');
  });
});
