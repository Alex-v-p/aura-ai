import { describe, expect, it } from 'vitest';
import { researchLifecycleAnnouncement, researchLifecycleLabel } from './research-activity-lifecycle';

describe('research activity lifecycle copy', () => {
  it.each([
    ['terminal · completed', 'Research completed.'],
    ['terminal · partial', 'Research completed with partial results.'],
    ['terminal · budget_exhausted', 'Research stopped because its budget was exhausted.'],
    ['terminal · canceled', 'Research was canceled.'],
    ['terminal · failed', 'Research failed.'],
    ['terminal · interrupted', 'Research was interrupted.'],
    ['detached', 'Research is continuing in the background.'],
    ['recovery unavailable', 'Research status is temporarily unavailable.'],
  ])('announces %s naturally', (lifecycle, expected) => {
    const announcement = researchLifecycleAnnouncement(lifecycle);
    expect(announcement).toBe(expected);
    expect(announcement).not.toMatch(/[_·]/);
  });

  it('keeps the visible label human-readable', () => {
    expect(researchLifecycleLabel('terminal · budget_exhausted')).toBe('Budget exhausted');
  });
});
