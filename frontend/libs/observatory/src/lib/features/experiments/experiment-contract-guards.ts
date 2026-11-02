export const EXPERIMENT_VARIANT_MIN = 2;
export const EXPERIMENT_VARIANT_MAX = 16;
export const PROMOTION_CONFIRMATION = 'PROMOTE_IMMUTABLE_BASELINE';

export function hasValidExperimentVariants(variants: ReadonlyArray<{ readonly variantId: string }>): boolean {
  const ids = variants.map((variant) => variant.variantId);
  return ids.length >= EXPERIMENT_VARIANT_MIN && ids.length <= EXPERIMENT_VARIANT_MAX && ids.every((value) => /^[A-Za-z0-9._-]{1,64}$/.test(value)) && new Set(ids).size === ids.length;
}
