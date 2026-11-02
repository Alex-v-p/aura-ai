import { describe, expect, it } from 'vitest';
import { EXPERIMENT_VARIANT_MAX, EXPERIMENT_VARIANT_MIN, PROMOTION_CONFIRMATION, hasValidExperimentVariants } from './experiment-contract-guards';
import { experimentWorkspaceStateFor, type ExperimentStateData } from './experiment-state.utils';

describe('experiment workspace contract guards', () => {
  it('requires 2–16 unique bounded variant IDs', () => {
    expect(EXPERIMENT_VARIANT_MIN).toBe(2);
    expect(EXPERIMENT_VARIANT_MAX).toBe(16);
    expect(hasValidExperimentVariants([{ variantId: 'control' }])).toBe(false);
    expect(hasValidExperimentVariants([{ variantId: 'control' }, { variantId: 'candidate' }])).toBe(true);
    expect(hasValidExperimentVariants([{ variantId: 'control' }, { variantId: 'control' }])).toBe(false);
    expect(hasValidExperimentVariants(Array.from({ length: 17 }, (_, index) => ({ variantId: `variant-${index}` })))).toBe(false);
  });

  it('keeps the irreversible confirmation exact and closed', () => {
    expect(PROMOTION_CONFIRMATION).toBe('PROMOTE_IMMUTABLE_BASELINE');
    expect(PROMOTION_CONFIRMATION).not.toBe('PROMOTE BASELINE');
  });

  it('reports a partial source failure even when fulfilled sources are valid empty pages', () => {
    const empty = { items: [], nextCursor: null };
    const data = { experiments: empty, baselines: empty, regressions: empty, history: empty } as unknown as ExperimentStateData;
    expect(experimentWorkspaceStateFor(data, true, 7)).toBe('partial');
    expect(experimentWorkspaceStateFor(data, true, 0)).toBe('unavailable');
    expect(experimentWorkspaceStateFor(data, false, 8)).toBe('empty');
  });
});
