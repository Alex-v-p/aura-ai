import type { EvaluationBaselinePage, EvaluationExperimentPage, EvaluationHistoryPage, EvaluationRegressionPage } from '@aura/observatory-api-client';

export type ExperimentStateData = {
  readonly experiments: EvaluationExperimentPage | null;
  readonly baselines: EvaluationBaselinePage | null;
  readonly regressions: EvaluationRegressionPage | null;
  readonly history: EvaluationHistoryPage | null;
};

export type ExperimentState = 'ready' | 'empty' | 'partial' | 'incompatible' | 'insufficient-evidence' | 'stale' | 'inconclusive' | 'unavailable';

export function experimentWorkspaceStateFor(data: ExperimentStateData, rejected: boolean, fulfilledSources: number): ExperimentState {
  if (rejected) return fulfilledSources > 0 ? 'partial' : 'unavailable';
  if (data.regressions?.items.some((item) => item.evidenceState === 'incompatible')) return 'incompatible';
  if (data.regressions?.items.some((item) => item.evidenceState === 'insufficient' || item.evidenceState === 'missing')) return 'insufficient-evidence';
  if (data.regressions?.items.some((item) => item.evidenceState === 'late')) return 'stale';
  if (data.regressions?.items.some((item) => item.decision === 'inconclusive')) return 'inconclusive';
  return data.experiments?.items.length || data.baselines?.items.length || data.regressions?.items.length || data.history?.items.length ? 'ready' : 'empty';
}
