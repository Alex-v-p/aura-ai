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
  type EvaluationJobPage,
  type EvaluationJob,
  type EvaluationQualificationPage,
  type EvaluationResultPage,
  type EvaluationReviewPage,
  type EvaluationRunnerExecutionProvenance,
  type EvaluationScorePage,
  type EvaluationSuitePage,
  type EvaluationQueuedAttempt,
  type ObservatoryOperationResponse,
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
  });

  it('types queued worker absence and admitted job provenance without fabrication', () => {
    expectTypeOf<EvaluationQueuedAttempt['workerComponentId']>().toEqualTypeOf<
      string | null | undefined
    >();
    expectTypeOf<EvaluationClaimedAttempt['workerComponentId']>().toEqualTypeOf<string>();
    expectTypeOf<EvaluationJob['provenance']>().toEqualTypeOf<EvaluationRunnerExecutionProvenance>();
  });
});
