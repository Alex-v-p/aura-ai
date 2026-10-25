import { describe, expect, expectTypeOf, it, vi } from 'vitest';

import {
  OBSERVATORY_API_OPERATIONS,
  type ArtifactUploadForm,
  type EvaluationArtifactPage,
  type EvaluationCaptureAuthorizationRecord,
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
  type RunInspection,
  type RunEvidenceDeletion,
  type RunEvidenceManifest,
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
    expect(OBSERVATORY_API_OPERATIONS.createEvaluationLocalJudgeEvaluator).toEqual({
      method: 'POST',
      pathTemplate: '/api/v1/evaluation/evaluators',
      requestMode: 'json',
      responseMode: 'json',
    });
    expect(OBSERVATORY_API_OPERATIONS.createEvaluationArtifact).toEqual({
      method: 'POST',
      pathTemplate: '/api/v1/evaluation/artifacts',
      requestMode: 'multipart',
      responseMode: 'json',
    });
    expect(OBSERVATORY_API_OPERATIONS.createEvaluationCaptureAuthorization).toEqual({
      method: 'POST',
      pathTemplate: '/api/v1/evaluation/capture-authorizations',
      requestMode: 'json',
      responseMode: 'json',
    });
    expectTypeOf<ObservatoryApiOperations['createEvaluationArtifact']['body']>().toEqualTypeOf<ArtifactUploadForm>();
    expectTypeOf<
      ObservatoryOperationResponse<'createEvaluationCaptureAuthorization'>
    >().toEqualTypeOf<EvaluationCaptureAuthorizationRecord>();
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

  it('exposes bounded run inspection and streamed evidence as transport-only operations', () => {
    expect(OBSERVATORY_API_OPERATIONS.getRunInspection).toEqual({
      method: 'GET',
      pathTemplate: '/api/v1/runs/{run_id}/inspection',
      requestMode: 'none',
      responseMode: 'json',
    });
    expect(OBSERVATORY_API_OPERATIONS.getRunEvidence).toEqual({
      method: 'GET',
      pathTemplate: '/api/v1/runs/{run_id}/evidence/{evidence_id}',
      requestMode: 'none',
      responseMode: 'blob',
    });
    expect(OBSERVATORY_API_OPERATIONS.deleteRunEvidence).toEqual({
      method: 'DELETE',
      pathTemplate: '/api/v1/runs/{run_id}/evidence',
      requestMode: 'none',
      responseMode: 'json',
    });
    expectTypeOf<ObservatoryOperationResponse<'getRunInspection'>>().toEqualTypeOf<RunInspection>();
    expectTypeOf<ObservatoryOperationResponse<'getRunEvidence'>>().toEqualTypeOf<Blob>();
    expectTypeOf<RunInspection['steps'][number]['evidence'][number]>().toEqualTypeOf<RunEvidenceManifest>();
    expectTypeOf<RunEvidenceManifest['captureState']>().toEqualTypeOf<
      'captured' | 'excluded' | 'partial' | 'dropped' | 'deleted'
    >();
    expectTypeOf<RunEvidenceManifest['digest']>().toEqualTypeOf<string | null>();
    expectTypeOf<ObservatoryOperationResponse<'deleteRunEvidence'>>().toEqualTypeOf<RunEvidenceDeletion>();
    expectTypeOf<RunEvidenceDeletion['failureCode']>().toEqualTypeOf<
      | 'manifest_store_unavailable'
      | 'object_store_unavailable'
      | 'object_delete_failed'
      | 'audit_persist_failed'
      | 'unknown_deletion_failure'
      | null
    >();
  });

  it('passes an approved evidence Blob through the generated transport unchanged', async () => {
    const evidence = new Blob(['fixture evidence'], { type: 'text/plain' });
    const execute = vi.fn().mockResolvedValue(evidence);
    const client = new ObservatoryApiClient({ execute } as unknown as ObservatoryTransport);
    const input = {
      path: {
        run_id: '8f4f1d14-2adf-4e6d-9cc8-67a9ad4e9a03',
        evidence_id: '7f4f1d14-2adf-4e6d-9cc8-67a9ad4e9a02',
      },
      query: {},
      headers: {},
      body: undefined as never,
    };

    await expect(client.execute('getRunEvidence', input)).resolves.toBe(evidence);
    expect(execute).toHaveBeenCalledOnce();
    expect(execute).toHaveBeenCalledWith(OBSERVATORY_API_OPERATIONS.getRunEvidence, input);
    expect(evidence).toBeInstanceOf(Blob);
    expect(evidence.type).toBe('text/plain');
    expect(evidence.size).toBeGreaterThan(0);
    expectTypeOf<ObservatoryApiOperations['deleteRunEvidence']['headers']>().toEqualTypeOf<{
      readonly 'X-Observatory-CSRF': string;
      readonly 'Idempotency-Key': string;
      readonly 'X-Observatory-Confirmation': string;
    }>();
  });
});
