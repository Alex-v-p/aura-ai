import { describe, expect, expectTypeOf, it, vi } from 'vitest';

import {
  OBSERVATORY_API_OPERATIONS,
  type EvaluationArtifactPage,
  type EvaluationAttemptPage,
  type EvaluationClaimedAttempt,
  type EvaluationCalibrationPage,
  type EvaluationCasePage,
  type EvaluationDatasetPage,
  type EvaluationEvaluatorPage,
  type EvaluationHistoryPage,
  type EvaluationExperimentPage,
  type EvaluationBaselinePage,
  type EvaluationRegressionPage,
  type EvaluationAttribution,
  type EvaluationJobPage,
  type EvaluationJob,
  type EvaluationQualificationPage,
  type EvaluationResultPage,
  type EvaluationReviewPage,
  type EvaluationRunnerExecutionProvenance,
  type EvaluationScorePage,
  type EvaluationSuitePage,
  type EvaluationSuite,
  type EvaluationExperimentVariant,
  type EvaluationQueuedAttempt,
  type ObservatoryOperationResponse,
  type ObservatoryApiOperations,
  type SavedQueryExecutionResult,
} from './generated/observatory-api';
import { ObservatoryApiClient, type ObservatoryTransport } from './observatory-api-client';

describe('ObservatoryApiClient', () => {
  it('delegates an evaluation operation to the injected transport without domain behavior', async () => {
    const response = {
      items: [],
      nextCursor: null,
      snapshotAt: '2026-10-21T00:00:00Z',
      partial: false,
    };
    const execute = vi.fn().mockResolvedValue(response);
    const client = new ObservatoryApiClient({ execute } as unknown as ObservatoryTransport);
    const input = {
      path: {},
      query: { page_size: 50 },
      headers: {},
      body: undefined as never,
    };

    await expect(client.execute('listEvaluationDatasets', input)).resolves.toEqual(response);
    expect(execute).toHaveBeenCalledOnce();
    expect(execute).toHaveBeenCalledWith(
      OBSERVATORY_API_OPERATIONS.listEvaluationDatasets,
      input,
    );
  });

  it('describes commands and SSE as transport metadata only', () => {
    expect(OBSERVATORY_API_OPERATIONS.listEvaluationDatasets.responseMode).toBe('json');
    for (const operation of [
      'listEvaluationCases',
      'listEvaluationSuites',
      'listEvaluationEvaluators',
      'listEvaluationJobs',
      'listEvaluationAttempts',
      'listEvaluationResults',
      'listEvaluationScores',
      'listEvaluationArtifacts',
      'listEvaluationReviews',
      'listEvaluationCalibrations',
      'listEvaluationQualifications',
      'listEvaluationHistory',
      'listExperiments',
      'listBaselines',
      'listRegressions',
    ] as const) {
      expect(OBSERVATORY_API_OPERATIONS[operation].responseMode).toBe('json');
    }
    expect(OBSERVATORY_API_OPERATIONS.submitEvaluationJob).toEqual({
      method: 'POST',
      pathTemplate: '/api/v1/evaluation/jobs',
      requestMode: 'json',
      responseMode: 'json',
    });
    expect(OBSERVATORY_API_OPERATIONS.streamEvents.responseMode).toBe('event-stream');
    expect(OBSERVATORY_API_OPERATIONS.streamEvaluationEvents).toEqual({
      method: 'GET',
      pathTemplate: '/api/v1/evaluation/events',
      requestMode: 'none',
      responseMode: 'event-stream',
    });
  });

  it('exposes exact-version saved-query execution as a transport-only closed union', () => {
    expect(OBSERVATORY_API_OPERATIONS.executeSavedQuery).toEqual({
      method: 'GET',
      pathTemplate: '/api/v1/saved-queries/{query_id}/versions/{resource_version}/execute',
      requestMode: 'none',
      responseMode: 'json',
    });
    expectTypeOf<ObservatoryOperationResponse<'executeSavedQuery'>>().toEqualTypeOf<SavedQueryExecutionResult>();
    expectTypeOf<ObservatoryApiOperations['executeSavedQuery']['query']>().toEqualTypeOf<{ readonly cursor?: string }>();
  });

  it('preserves JSON response parity for every evaluation page operation', () => {
    const pageOperations = [
      'listEvaluationDatasets',
      'listEvaluationCases',
      'listEvaluationSuites',
      'listEvaluationEvaluators',
      'listEvaluationJobs',
      'listEvaluationAttempts',
      'listEvaluationResults',
      'listEvaluationScores',
      'listEvaluationArtifacts',
      'listEvaluationReviews',
      'listEvaluationCalibrations',
      'listEvaluationQualifications',
      'listEvaluationHistory',
      'listExperiments',
      'listBaselines',
      'listRegressions',
    ] as const;
    for (const operation of pageOperations) {
      expect(OBSERVATORY_API_OPERATIONS[operation].responseMode).toBe('json');
    }

    expectTypeOf<ObservatoryOperationResponse<'listEvaluationDatasets'>>().toEqualTypeOf<EvaluationDatasetPage>();
    expectTypeOf<ObservatoryOperationResponse<'listEvaluationCases'>>().toEqualTypeOf<EvaluationCasePage>();
    expectTypeOf<ObservatoryOperationResponse<'listEvaluationSuites'>>().toEqualTypeOf<EvaluationSuitePage>();
    expectTypeOf<ObservatoryOperationResponse<'listEvaluationEvaluators'>>().toEqualTypeOf<EvaluationEvaluatorPage>();
    expectTypeOf<ObservatoryOperationResponse<'listEvaluationJobs'>>().toEqualTypeOf<EvaluationJobPage>();
    expectTypeOf<ObservatoryOperationResponse<'listEvaluationAttempts'>>().toEqualTypeOf<EvaluationAttemptPage>();
    expectTypeOf<ObservatoryOperationResponse<'listEvaluationResults'>>().toEqualTypeOf<EvaluationResultPage>();
    expectTypeOf<ObservatoryOperationResponse<'listEvaluationScores'>>().toEqualTypeOf<EvaluationScorePage>();
    expectTypeOf<ObservatoryOperationResponse<'listEvaluationArtifacts'>>().toEqualTypeOf<EvaluationArtifactPage>();
    expectTypeOf<ObservatoryOperationResponse<'listEvaluationReviews'>>().toEqualTypeOf<EvaluationReviewPage>();
    expectTypeOf<ObservatoryOperationResponse<'listEvaluationCalibrations'>>().toEqualTypeOf<EvaluationCalibrationPage>();
    expectTypeOf<ObservatoryOperationResponse<'listEvaluationQualifications'>>().toEqualTypeOf<EvaluationQualificationPage>();
    expectTypeOf<ObservatoryOperationResponse<'listEvaluationHistory'>>().toEqualTypeOf<EvaluationHistoryPage>();
    expectTypeOf<ObservatoryOperationResponse<'listExperiments'>>().toEqualTypeOf<EvaluationExperimentPage>();
    expectTypeOf<ObservatoryOperationResponse<'listBaselines'>>().toEqualTypeOf<EvaluationBaselinePage>();
    expectTypeOf<ObservatoryOperationResponse<'listRegressions'>>().toEqualTypeOf<EvaluationRegressionPage>();
    expectTypeOf<ObservatoryOperationResponse<'getRegressionAttribution'>>().toEqualTypeOf<EvaluationAttribution>();
  });

  it('keeps experiment, promotion, and regression behavior outside the generated client', () => {
    expect(OBSERVATORY_API_OPERATIONS.createExperiment).toEqual({
      method: 'POST',
      pathTemplate: '/api/v1/evaluation/experiments',
      requestMode: 'json',
      responseMode: 'json',
    });
    expect(OBSERVATORY_API_OPERATIONS.promoteBaseline).toEqual({
      method: 'POST',
      pathTemplate: '/api/v1/evaluation/baselines/{baseline_id}/promotion',
      requestMode: 'json',
      responseMode: 'json',
    });
    expect(OBSERVATORY_API_OPERATIONS.createRegressionComparison).toEqual({
      method: 'POST',
      pathTemplate: '/api/v1/evaluation/regressions',
      requestMode: 'json',
      responseMode: 'json',
    });
    expectTypeOf<ObservatoryApiOperations['getRegression']['query']>().toEqualTypeOf<{
      readonly version: string;
    }>();
    expectTypeOf<ObservatoryApiOperations['getRegressionAttribution']['query']>().toEqualTypeOf<{
      readonly version: string;
    }>();
  });

  it('types queued worker absence and admitted job provenance without fabrication', () => {
    expectTypeOf<EvaluationQueuedAttempt['workerComponentId']>().toEqualTypeOf<
      string | null | undefined
    >();
    expectTypeOf<EvaluationClaimedAttempt['workerComponentId']>().toEqualTypeOf<string>();
    expectTypeOf<EvaluationJob['provenance']>().toEqualTypeOf<EvaluationRunnerExecutionProvenance>();
  });

  it('keeps final suite variant bindings available in the generated transport type', () => {
    expectTypeOf<EvaluationSuite['variantBindings']>().toEqualTypeOf<
      ReadonlyArray<EvaluationExperimentVariant> | undefined
    >();
  });
});
