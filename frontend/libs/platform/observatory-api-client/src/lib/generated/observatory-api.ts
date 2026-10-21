// Generated from contracts/openapi/observatory-v1.yaml. Do not edit by hand.
// Contract SHA-256: 3b271a5dd45000b353cca42d91b173e51c8a76bc2cd7682601cdb0ca3475c17a

export type Uuid = string;

export type UtcInstant = string;

export type SafeLabel = string;

export type RevisionIdentifier = string;

export type ModelIdentifier = string;

export type ComponentId = string;

export type DefinitionIdentifier = string;

export type EvaluationIdentifier = string;

export type EvaluationVersion = string;

export type EvaluationDigest = string;

export type EvaluationCaptureClassification = "metadata_only" | "hashed" | "redacted" | "sampled_content" | "full_content";

export type EvaluationScope = "public_project" | "private" | "sensitive" | "environment";

export type EvaluationRetentionMode = "manual_only";

export type EvaluationDefinitionState = "active" | "superseded" | "deleted";

export type EvaluationResourceType = "dataset" | "case" | "suite" | "evaluator" | "job" | "attempt" | "result" | "score" | "artifact" | "review" | "calibration" | "qualification" | "experiment" | "baseline" | "regression" | "attribution" | "deletion";

export type EvaluationResourceReference = { readonly "resourceId": Uuid; readonly "resourceVersion": EvaluationVersion; };

export type EvaluationDefinitionReference = { readonly "id": Uuid; readonly "version": EvaluationVersion; };

export type EvaluationProvenance = { readonly "createdByComponent": ComponentId; readonly "createdByVersion": EvaluationVersion; readonly "sourceDigest": EvaluationDigest; };

export type EvaluationExecutionLimits = { readonly "timeoutMs": number; readonly "maxConcurrency": number; readonly "maxAttempts": number; readonly "maxCases": number; readonly "maxInputBytes": number; readonly "maxOutputBytes": number; readonly "maxTokens": number; };

export type EvaluationAuthorizationPurpose = "synthetic_benchmark" | "owner_supplied_evaluation" | "controlled_replay" | "human_review";

export type EvaluationCaptureAuthorization = { readonly "captureClassification": "metadata_only"; readonly "authorizationPurpose": EvaluationAuthorizationPurpose; readonly "authorizationContext": "controlled_evaluation"; readonly "retentionMode": EvaluationRetentionMode; } | { readonly "captureClassification": "hashed" | "redacted" | "sampled_content" | "full_content"; readonly "authorizationPurpose": EvaluationAuthorizationPurpose; readonly "authorizationContext": "controlled_evaluation"; readonly "retentionMode": EvaluationRetentionMode; readonly "authorizationRecordId": Uuid; readonly "authorizedAt": UtcInstant; };

export type EvaluationRunnerLimits = { readonly "execution": EvaluationExecutionLimits; readonly "maxQueueTimeMs": number; readonly "maxModelCalls": number; readonly "maxArtifactOperations": number; };

export type EvaluationArtifactIntegrity = { readonly "algorithm": "sha256"; readonly "digest": EvaluationDigest; };

export type EvaluationInputArtifactReference = { readonly "artifactReferenceId": Uuid; readonly "mediaClassification": "evaluation_input" | "evaluation_context" | "evaluation_evidence"; readonly "mediaType": "application/json" | "application/octet-stream" | "text/plain" | "text/markdown" | "image/png" | "image/jpeg" | "audio/wav"; readonly "byteCount": number; readonly "integrity": EvaluationArtifactIntegrity; readonly "captureAuthorization": EvaluationCaptureAuthorization; };

export type EvaluationRunnerRequestData = { readonly "executionKind": "module_replay" | "synthetic_end_to_end"; readonly "requestVersion": EvaluationVersion; readonly "targetComponent": EvaluationComponentBinding; readonly "captureAuthorization": EvaluationCaptureAuthorization; readonly "provenance": EvaluationRunnerExecutionProvenance; readonly "limits": EvaluationRunnerLimits; readonly "deadlineAt": UtcInstant; readonly "caseIds": ReadonlyArray<Uuid>; readonly "inputArtifactReferences": ReadonlyArray<EvaluationInputArtifactReference>; };

export type EvaluationModelBinding = { readonly "modelId": EvaluationIdentifier; readonly "modelVersion": EvaluationVersion; readonly "modelDigest": EvaluationDigest; };

export type EvaluationReproducibility = { readonly "seed": number; readonly "clockMode": "fixed" | "recorded"; readonly "timeZone": "UTC"; readonly "locale": string; readonly "fixtureRevision": RevisionIdentifier; readonly "promptRevision": RevisionIdentifier; readonly "policyRevision": RevisionIdentifier; };

export type EvaluationExecutionProvenance = { readonly "dataset": EvaluationResourceReference; readonly "suite": EvaluationResourceReference; readonly "evaluators": ReadonlyArray<EvaluationEvaluatorReference>; readonly "components": ReadonlyArray<EvaluationComponentBinding>; readonly "model": EvaluationModelBinding; readonly "deploymentRevision": RevisionIdentifier; readonly "reproducibility": EvaluationReproducibility; };

export type EvaluationRunnerExecutionProvenance = { readonly "dataset": EvaluationDefinitionReference; readonly "suite": EvaluationDefinitionReference; readonly "evaluators": ReadonlyArray<EvaluationEvaluatorReference>; readonly "components": ReadonlyArray<EvaluationComponentBinding>; readonly "model": EvaluationModelBinding; readonly "deploymentRevision": RevisionIdentifier; readonly "reproducibility": EvaluationReproducibility; };

export type EvaluationPageMetadata = { readonly "nextCursor": Cursor | null; readonly "snapshotAt": UtcInstant; readonly "partial": boolean; };

export type EvaluationCaseInput = { readonly "caseId": Uuid; readonly "caseVersion": EvaluationVersion; readonly "artifactIds": ReadonlyArray<Uuid>; readonly "captureClassification": EvaluationCaptureClassification; };

export type CreateEvaluationDatasetRequest = { readonly "datasetId": Uuid; readonly "datasetVersion": EvaluationVersion; readonly "sourceKind": "synthetic" | "owner_supplied" | "captured"; readonly "scopeIds": ReadonlyArray<EvaluationScope>; readonly "captureClassification": EvaluationCaptureClassification; readonly "cases": ReadonlyArray<EvaluationCaseInput>; readonly "retentionMode": EvaluationRetentionMode; };

export type EvaluationDataset = { readonly "schemaVersion": 1; readonly "resourceType": "dataset"; readonly "datasetId": Uuid; readonly "datasetVersion": EvaluationVersion; readonly "sourceKind": "synthetic" | "owner_supplied" | "captured"; readonly "scopeIds": ReadonlyArray<EvaluationScope>; readonly "captureClassification": EvaluationCaptureClassification; readonly "caseCount": number; readonly "retentionMode": EvaluationRetentionMode; readonly "provenance": EvaluationProvenance; readonly "state": EvaluationDefinitionState; readonly "createdAt": UtcInstant; };

export type EvaluationCase = { readonly "schemaVersion": 1; readonly "resourceType": "case"; readonly "caseId": Uuid; readonly "caseVersion": EvaluationVersion; readonly "dataset": EvaluationResourceReference; readonly "sourceKind": "synthetic" | "owner_supplied" | "captured"; readonly "scopeIds": ReadonlyArray<EvaluationScope>; readonly "captureClassification": EvaluationCaptureClassification; readonly "artifactIds": ReadonlyArray<Uuid>; readonly "retentionMode": EvaluationRetentionMode; readonly "provenance": EvaluationProvenance; readonly "createdAt": UtcInstant; };

export type EvaluationDatasetPage = { readonly "items": ReadonlyArray<EvaluationDataset>; readonly "nextCursor": Cursor | null; readonly "snapshotAt": UtcInstant; readonly "partial": boolean; };

export type EvaluationCasePage = { readonly "items": ReadonlyArray<EvaluationCase>; readonly "nextCursor": Cursor | null; readonly "snapshotAt": UtcInstant; readonly "partial": boolean; };

export type EvaluationEvaluatorReference = { readonly "evaluatorId": Uuid; readonly "evaluatorVersion": EvaluationVersion; readonly "kind": EvaluationEvaluatorKind; };

export type EvaluationSuiteEvaluatorReference = { readonly "evaluatorId": Uuid; readonly "evaluatorVersion": EvaluationVersion; readonly "kind": "deterministic"; };

export type CreateEvaluationSuiteRequest = { readonly "suiteId": Uuid; readonly "suiteVersion": EvaluationVersion; readonly "dataset": EvaluationDefinitionReference; readonly "evaluators": ReadonlyArray<EvaluationSuiteEvaluatorReference>; readonly "components": ReadonlyArray<EvaluationComponentBinding>; readonly "variantBindings"?: ReadonlyArray<EvaluationExperimentVariant>; readonly "limits": EvaluationExecutionLimits; readonly "manualThresholds"?: Readonly<Record<string, number>>; readonly "retentionMode": EvaluationRetentionMode; readonly "provenance": EvaluationProvenance; readonly "createdAt": UtcInstant; };

export type EvaluationSuite = { readonly "schemaVersion": 1; readonly "resourceType": "suite"; readonly "suiteId": Uuid; readonly "suiteVersion": EvaluationVersion; readonly "dataset": EvaluationResourceReference; readonly "evaluators": ReadonlyArray<EvaluationEvaluatorReference>; readonly "componentIds": ReadonlyArray<ComponentId>; readonly "components"?: ReadonlyArray<EvaluationComponentBinding>; readonly "variantBindings"?: ReadonlyArray<EvaluationExperimentVariant>; readonly "caseBudget": number; readonly "timeoutMs": number; readonly "limits"?: EvaluationExecutionLimits; readonly "retentionMode": EvaluationRetentionMode; readonly "provenance": EvaluationProvenance; readonly "state": EvaluationDefinitionState; readonly "createdAt": UtcInstant; };

export type EvaluationSuitePage = { readonly "items": ReadonlyArray<EvaluationSuite>; readonly "nextCursor": Cursor | null; readonly "snapshotAt": UtcInstant; readonly "partial": boolean; };

export type EvaluationEvaluatorKind = "deterministic" | "local_judge" | "human_review";

export type EvaluationEvaluator = { readonly "schemaVersion": 1; readonly "resourceType": "evaluator"; readonly "evaluatorId": Uuid; readonly "evaluatorVersion": EvaluationVersion; readonly "kind": EvaluationEvaluatorKind; readonly "metric": EvaluationIdentifier; readonly "componentIds": ReadonlyArray<ComponentId>; readonly "rubricVersion"?: EvaluationVersion; readonly "calibrationVersion"?: EvaluationVersion; readonly "retentionMode": EvaluationRetentionMode; readonly "provenance": EvaluationProvenance; readonly "createdAt": UtcInstant; };

export type EvaluationEvaluatorPage = { readonly "items": ReadonlyArray<EvaluationEvaluator>; readonly "nextCursor": Cursor | null; readonly "snapshotAt": UtcInstant; readonly "partial": boolean; };

export type EvaluationJobStatus = "queued" | "running" | "succeeded" | "failed" | "cancelled" | "deadline";

export type SubmitEvaluationJobRequest = { readonly "requestId": Uuid; readonly "requestVersion": EvaluationVersion; readonly "dataset": EvaluationDefinitionReference; readonly "suite": EvaluationDefinitionReference; readonly "evaluator": EvaluationDefinitionReference; readonly "captureClassification": EvaluationCaptureClassification; readonly "deadlineAt": UtcInstant; readonly "maxAttempts": number; readonly "requestData": EvaluationRunnerRequestData; readonly "createdAt": UtcInstant; };

export type CancelEvaluationJobRequest = { readonly "action": "cancel"; readonly "reason": "OWNER_REQUESTED" | "SUPERSEDED"; };

export type EvaluationJob = { readonly "schemaVersion": 1; readonly "resourceType": "job"; readonly "jobId": Uuid; readonly "suite": EvaluationResourceReference; readonly "status": EvaluationJobStatus; readonly "captureClassification": EvaluationCaptureClassification; readonly "attemptCount": number; readonly "resultCount": number; readonly "artifactCount": number; readonly "deadlineAt": UtcInstant; readonly "failureCode": EvaluationIdentifier | null; readonly "provenance": EvaluationRunnerExecutionProvenance; readonly "createdAt": UtcInstant; readonly "updatedAt": UtcInstant; };

export type EvaluationJobPage = { readonly "items": ReadonlyArray<EvaluationJob>; readonly "nextCursor": Cursor | null; readonly "snapshotAt": UtcInstant; readonly "partial": boolean; };

export type EvaluationQueuedAttempt = { readonly "attemptId": Uuid; readonly "jobId": Uuid; readonly "attemptNumber": number; readonly "status": "queued"; readonly "workerComponentId"?: ComponentId | null; readonly "startedAt": UtcInstant | null; readonly "finishedAt": UtcInstant | null; readonly "nextAttemptAt": UtcInstant | null; readonly "failureCode": EvaluationIdentifier | null; };

export type EvaluationClaimedAttempt = { readonly "attemptId": Uuid; readonly "jobId": Uuid; readonly "attemptNumber": number; readonly "status": "running" | "succeeded" | "failed" | "cancelled" | "deadline"; readonly "workerComponentId": ComponentId; readonly "startedAt": UtcInstant | null; readonly "finishedAt": UtcInstant | null; readonly "nextAttemptAt": UtcInstant | null; readonly "failureCode": EvaluationIdentifier | null; };

export type EvaluationAttempt = EvaluationQueuedAttempt | EvaluationClaimedAttempt;

export type EvaluationAttemptPage = { readonly "items": ReadonlyArray<EvaluationAttempt>; readonly "nextCursor": Cursor | null; readonly "snapshotAt": UtcInstant; readonly "partial": boolean; };

export type EvaluationResultStatus = "completed" | "cancelled" | "failed";

export type EvaluationResult = { readonly "schemaVersion": 1; readonly "resourceType": "result"; readonly "resultId": Uuid; readonly "jobId": Uuid; readonly "attemptId": Uuid; readonly "status": EvaluationResultStatus; readonly "captureClassification": EvaluationCaptureClassification; readonly "scoreCount": number; readonly "artifactCount": number; readonly "partialEvidence": boolean; readonly "reproducible": boolean; readonly "provenance": EvaluationExecutionProvenance; readonly "createdAt": UtcInstant; readonly "completedAt": UtcInstant | null; };

export type EvaluationResultPage = { readonly "items": ReadonlyArray<EvaluationResult>; readonly "nextCursor": Cursor | null; readonly "snapshotAt": UtcInstant; readonly "partial": boolean; };

export type EvaluationComponentBinding = { readonly "componentId": ComponentId; readonly "componentVersion": EvaluationVersion; readonly "codeRevision": RevisionIdentifier; readonly "imageDigest": EvaluationDigest; readonly "configurationRevision": RevisionIdentifier; };

export type EvaluationScoreUncertainty = { readonly "method": "none" | "calibrated_interval" | "human_disagreement"; readonly "lower": number; readonly "upper": number; };

export type EvaluationScore = { readonly "schemaVersion": 1; readonly "resourceType": "score"; readonly "scoreId": Uuid; readonly "resultId": Uuid; readonly "caseId": Uuid; readonly "evaluator": EvaluationEvaluatorReference; readonly "component": EvaluationComponentBinding; readonly "metric": EvaluationIdentifier; readonly "value": number | null; readonly "outcome": "pass" | "fail" | "inconclusive" | null; readonly "evidenceStatus": "observed" | "not_observed" | "not_applicable"; readonly "uncertainty": EvaluationScoreUncertainty; readonly "scoreDisposition": "deterministic" | "advisory_only" | "calibrated_enabled" | "human_review"; readonly "provenance": EvaluationExecutionProvenance; readonly "createdAt": UtcInstant; };

export type EvaluationScorePage = { readonly "items": ReadonlyArray<EvaluationScore>; readonly "nextCursor": Cursor | null; readonly "snapshotAt": UtcInstant; readonly "partial": boolean; };

export type EvaluationArtifactDeletionStatus = "retained" | "deletion_queued" | "deleting" | "deleted" | "deletion_failed";

export type EvaluationArtifact = { readonly "schemaVersion": 1; readonly "resourceType": "artifact"; readonly "artifactId": Uuid; readonly "artifactVersion": EvaluationVersion; readonly "jobId": Uuid | null; readonly "resultId": Uuid | null; readonly "purpose": "dataset_case" | "input" | "expected_output" | "actual_output" | "evidence" | "judge_prompt" | "judge_rubric" | "review_comment"; readonly "mediaClassification": "evaluation_input" | "evaluation_output" | "evaluation_context" | "evaluation_evidence" | "evaluation_report"; readonly "mediaType": "application/json" | "application/octet-stream" | "text/plain" | "text/markdown" | "image/png" | "image/jpeg" | "audio/wav"; readonly "digest": EvaluationDigest; readonly "byteCount": number; readonly "captureClassification": EvaluationCaptureClassification; readonly "authorizationRecordId": Uuid; readonly "retentionMode": EvaluationRetentionMode; readonly "deletionStatus": EvaluationArtifactDeletionStatus; readonly "provenance": EvaluationExecutionProvenance; readonly "createdAt": UtcInstant; };

export type EvaluationArtifactPage = { readonly "items": ReadonlyArray<EvaluationArtifact>; readonly "nextCursor": Cursor | null; readonly "snapshotAt": UtcInstant; readonly "partial": boolean; };

export type EvaluationReviewOutcome = "pass" | "fail" | "inconclusive";

export type EvaluationReviewLabel = "accepted" | "rejected" | "needs_review";

export type EvaluationDisagreement = "none" | "within_tolerance" | "score_disagreement" | "outcome_disagreement" | "insufficient_evidence";

export type EvaluationCalibrationReference = { readonly "calibrationId": Uuid; readonly "calibrationVersion": EvaluationVersion; };

export type EvaluationReviewCommentArtifactBinding = { readonly "artifactId": Uuid; readonly "artifactVersion": EvaluationVersion; readonly "purpose": "human_review_comment"; readonly "digest": EvaluationDigest; };

export type CreateEvaluationReviewRequest = { readonly "reviewId": Uuid; readonly "reviewVersion": EvaluationVersion; readonly "calibration": EvaluationCalibrationReference; readonly "caseId": Uuid; readonly "resultId": Uuid; readonly "scoreId": Uuid; readonly "reviewScore": number; readonly "reviewOutcome": EvaluationReviewOutcome; readonly "reviewLabel": EvaluationReviewLabel; readonly "disagreement": EvaluationDisagreement; readonly "authorizationRecordId": Uuid; readonly "commentArtifact"?: EvaluationReviewCommentArtifactBinding; readonly "retentionMode": EvaluationRetentionMode; readonly "provenance": EvaluationProvenance; readonly "createdAt": UtcInstant; };

export type EvaluationReview = { readonly "schemaVersion": 1; readonly "resourceType": "human_review"; readonly "reviewId": Uuid; readonly "reviewVersion": EvaluationVersion; readonly "calibration": EvaluationResourceReference; readonly "caseId": Uuid; readonly "resultId": Uuid; readonly "scoreId": Uuid; readonly "reviewScore": number; readonly "reviewOutcome": EvaluationReviewOutcome; readonly "reviewLabel": EvaluationReviewLabel; readonly "disagreement": EvaluationDisagreement; readonly "authorizationRecordId": Uuid; readonly "commentArtifactId": Uuid | null; readonly "provenance": EvaluationProvenance; readonly "createdAt": UtcInstant; };

export type EvaluationReviewPage = { readonly "items": ReadonlyArray<EvaluationReview>; readonly "nextCursor": Cursor | null; readonly "snapshotAt": UtcInstant; readonly "partial": boolean; };

export type EvaluationCalibrationEligibility = "eligible" | "ineligible";

export type EvaluationCalibration = { readonly "calibrationId": EvaluationIdentifier; readonly "calibrationVersion": EvaluationVersion; readonly "dataset": EvaluationResourceReference; readonly "suite": EvaluationResourceReference; readonly "evaluator": EvaluationEvaluatorReference; readonly "judgeDefinition": EvaluationResourceReference; readonly "sampleCount": number; readonly "agreementRate": number; readonly "meanAbsoluteError": number; readonly "disagreementRate": number; readonly "eligibility": EvaluationCalibrationEligibility; readonly "advisory": boolean; readonly "provenance": EvaluationProvenance; readonly "createdAt": UtcInstant; };

export type EvaluationCalibrationPage = { readonly "items": ReadonlyArray<EvaluationCalibration>; readonly "nextCursor": Cursor | null; readonly "snapshotAt": UtcInstant; readonly "partial": boolean; };

export type EvaluationQualificationState = "qualified" | "revoked";

export type EvaluationJudgeDefinitionReference = { readonly "judgeDefinitionId": Uuid; readonly "judgeDefinitionVersion": EvaluationVersion; };

export type EvaluationLocalJudgeModel = { readonly "provider": "local_ollama"; readonly "modelName": ModelIdentifier; readonly "modelVersion": EvaluationVersion; readonly "modelDigest": EvaluationDigest; };

export type EvaluationPromptArtifactBinding = { readonly "artifactId": Uuid; readonly "artifactVersion": EvaluationVersion; readonly "purpose": "judge_prompt"; readonly "digest": EvaluationDigest; };

export type EvaluationRubricArtifactBinding = { readonly "artifactId": Uuid; readonly "artifactVersion": EvaluationVersion; readonly "purpose": "judge_rubric"; readonly "digest": EvaluationDigest; };

export type QualifyEvaluationJudgeRequest = { readonly "qualificationId": Uuid; readonly "qualificationVersion": EvaluationVersion; readonly "action": "qualify"; readonly "actionMode": "manual_owner_action"; readonly "dataset": EvaluationDefinitionReference; readonly "suite": EvaluationDefinitionReference; readonly "evaluator": EvaluationEvaluatorReference; readonly "judgeDefinition": EvaluationJudgeDefinitionReference; readonly "model": EvaluationLocalJudgeModel; readonly "prompt": EvaluationPromptArtifactBinding; readonly "rubric": EvaluationRubricArtifactBinding; readonly "calibration": EvaluationCalibrationReference; readonly "calibrationResultId": Uuid; readonly "calibrationEligibility": "eligible"; readonly "authorizationRecordId": Uuid; readonly "scoreDisposition": "calibrated_enabled"; readonly "qualifiedAt": UtcInstant; readonly "retentionMode": EvaluationRetentionMode; readonly "provenance": EvaluationProvenance; readonly "createdAt": UtcInstant; };

export type RevokeEvaluationQualificationRequest = { readonly "action": "revoke"; readonly "authorizationRecordId": Uuid; readonly "revokedAt": UtcInstant; };

export type EvaluationQualification = { readonly "schemaVersion": 1; readonly "resourceType": "judge_qualification"; readonly "qualificationId": Uuid; readonly "qualificationVersion": EvaluationVersion; readonly "state": EvaluationQualificationState; readonly "dataset": EvaluationResourceReference; readonly "suite": EvaluationResourceReference; readonly "evaluator": EvaluationEvaluatorReference; readonly "judgeDefinition": EvaluationResourceReference; readonly "calibration": EvaluationResourceReference; readonly "calibrationResultId": Uuid; readonly "authorizationRecordId": Uuid; readonly "scoreDisposition": "calibrated_enabled" | "advisory_only"; readonly "provenance": EvaluationProvenance; readonly "createdAt": UtcInstant; };

export type EvaluationQualificationPage = { readonly "items": ReadonlyArray<EvaluationQualification>; readonly "nextCursor": Cursor | null; readonly "snapshotAt": UtcInstant; readonly "partial": boolean; };

export type EvaluationExperimentVariant = { readonly "variantId": EvaluationIdentifier; readonly "components": ReadonlyArray<EvaluationComponentBinding>; readonly "model": EvaluationModelBinding; readonly "configurationRevision": RevisionIdentifier; readonly "promptRevision": RevisionIdentifier; readonly "policyRevision": RevisionIdentifier; readonly "deploymentRevision": RevisionIdentifier; };

export type EvaluationCohort = { readonly "cohortId": EvaluationIdentifier; readonly "caseIds": ReadonlyArray<Uuid>; };

export type CreateEvaluationExperimentRequest = { readonly "experimentId": Uuid; readonly "experimentVersion": EvaluationVersion; readonly "suite": EvaluationDefinitionReference; readonly "variants": ReadonlyArray<EvaluationExperimentVariant>; readonly "cohorts": ReadonlyArray<EvaluationCohort>; readonly "captureAuthorization": EvaluationCaptureAuthorization; readonly "limits": EvaluationExecutionLimits; readonly "retentionMode": EvaluationRetentionMode; readonly "provenance": EvaluationProvenance; readonly "createdAt": UtcInstant; };

export type EvaluationExperiment = { readonly "schemaVersion": 1; readonly "resourceType": "experiment"; readonly "experimentId": Uuid; readonly "experimentVersion": EvaluationVersion; readonly "state": EvaluationDefinitionState; readonly "suite": EvaluationResourceReference; readonly "variants": ReadonlyArray<EvaluationExperimentVariant>; readonly "cohorts": ReadonlyArray<EvaluationCohort>; readonly "captureAuthorization": EvaluationCaptureAuthorization; readonly "limits": EvaluationExecutionLimits; readonly "captureClassification": EvaluationCaptureClassification; readonly "retentionMode": EvaluationRetentionMode; readonly "provenance": EvaluationProvenance; readonly "createdAt": UtcInstant; };

export type EvaluationExperimentPage = { readonly "items": ReadonlyArray<EvaluationExperiment>; readonly "nextCursor": Cursor | null; readonly "snapshotAt": UtcInstant; readonly "partial": boolean; };

export type EvaluationRegressionThreshold = { readonly "metric": EvaluationIdentifier; readonly "metricClass": "operational" | "quality"; readonly "evaluator": EvaluationEvaluatorReference; readonly "unit": "ratio" | "count" | "milliseconds" | "bytes" | "tokens" | "operations" | "per_second" | "score"; readonly "aggregation": "minimum" | "maximum" | "mean" | "p50" | "p95" | "p99" | "total"; readonly "direction": "higher_is_better" | "lower_is_better"; readonly "comparisonMode": "absolute" | "relative"; readonly "thresholdValue": number; readonly "minimumSampleSize": number; readonly "missingEvidencePolicy": "regressed" | "inconclusive" | "not_applicable"; };

export type EvaluationBaselineState = "candidate" | "promoted";

export type CreateEvaluationBaselineCandidateRequest = { readonly "baselineId": Uuid; readonly "baselineVersion": EvaluationVersion; readonly "experiment": EvaluationDefinitionReference; readonly "suite": EvaluationDefinitionReference; readonly "variantId": EvaluationIdentifier; readonly "cohortIds": ReadonlyArray<EvaluationIdentifier>; readonly "sourceResultIds": ReadonlyArray<Uuid>; readonly "sourceResultFrom": UtcInstant; readonly "sourceResultTo": UtcInstant; readonly "thresholds": ReadonlyArray<EvaluationRegressionThreshold>; readonly "captureClassification": EvaluationCaptureClassification; readonly "reproducibility": EvaluationReproducibility; readonly "retentionMode": EvaluationRetentionMode; readonly "provenance": EvaluationProvenance; readonly "createdAt": UtcInstant; };

export type PromoteEvaluationBaselineRequest = { readonly "expectedCandidateVersion": EvaluationVersion; readonly "authorizationRecordId": Uuid; readonly "promotedAt": UtcInstant; };

export type EvaluationBaseline = { readonly "schemaVersion": 1; readonly "resourceType": "baseline"; readonly "baselineId": Uuid; readonly "baselineVersion": EvaluationVersion; readonly "state": EvaluationBaselineState; readonly "experiment": EvaluationResourceReference; readonly "suite": EvaluationResourceReference; readonly "variantId": EvaluationIdentifier; readonly "cohortIds": ReadonlyArray<EvaluationIdentifier>; readonly "sourceResultIds": ReadonlyArray<Uuid>; readonly "sourceResultFrom": UtcInstant; readonly "sourceResultTo": UtcInstant; readonly "thresholds": ReadonlyArray<EvaluationRegressionThreshold>; readonly "captureClassification": EvaluationCaptureClassification; readonly "reproducibility": EvaluationReproducibility; readonly "immutable": true; readonly "promotionMode": "manual_owner_action"; readonly "manualPromotionRecordId": Uuid | null; readonly "promotedAt": UtcInstant | null; readonly "retentionMode": EvaluationRetentionMode; readonly "provenance": EvaluationProvenance; readonly "createdAt": UtcInstant; };

export type EvaluationBaselinePage = { readonly "items": ReadonlyArray<EvaluationBaseline>; readonly "nextCursor": Cursor | null; readonly "snapshotAt": UtcInstant; readonly "partial": boolean; };

export type EvaluationEvidenceReference = { readonly "kind": "run" | "trace" | "result" | "score" | "artifact_reference"; readonly "id": SafeLabel; readonly "digest": EvaluationDigest; readonly "version": EvaluationVersion; };

export type EvaluationRegressionOutcome = "regressed" | "unchanged" | "inconclusive" | "unavailable" | "incompatible";

export type EvaluationRegressionEvidenceState = "complete" | "partial" | "insufficient" | "missing" | "incompatible" | "late";

export type CreateEvaluationRegressionRequest = { readonly "regressionId": Uuid; readonly "regressionVersion": EvaluationVersion; readonly "experiment": EvaluationDefinitionReference; readonly "baseline": EvaluationDefinitionReference; readonly "variantId": EvaluationIdentifier; readonly "cohortIds": ReadonlyArray<EvaluationIdentifier>; readonly "baselineResultIds": ReadonlyArray<Uuid>; readonly "candidateResultIds": ReadonlyArray<Uuid>; readonly "thresholds": ReadonlyArray<EvaluationRegressionThreshold>; readonly "createdAt": UtcInstant; };

export type EvaluationMetricComparison = { readonly "metric": EvaluationIdentifier; readonly "metricClass": "operational" | "quality"; readonly "evaluator": EvaluationEvaluatorReference; readonly "unit": "ratio" | "count" | "milliseconds" | "bytes" | "tokens" | "operations" | "per_second" | "score"; readonly "aggregation": "minimum" | "maximum" | "mean" | "p50" | "p95" | "p99" | "total"; readonly "direction": "higher_is_better" | "lower_is_better"; readonly "comparisonMode": "absolute" | "relative"; readonly "baselineValue": number; readonly "candidateValue": number; readonly "difference": number | null; readonly "relativeChange": number | null; readonly "thresholdValue": number; readonly "minimumSampleSize": number; readonly "baselineSampleSize": number; readonly "candidateSampleSize": number; readonly "missingEvidencePolicy": "regressed" | "inconclusive" | "not_applicable"; readonly "outcome": EvaluationRegressionOutcome; };

export type EvaluationAttributionCandidate = { readonly "rank": number; readonly "bindingKind": "component" | "model" | "deployment" | "prompt" | "policy" | "configuration"; readonly "componentId": ComponentId | null; readonly "beforeRevision": RevisionIdentifier; readonly "afterRevision": RevisionIdentifier; readonly "affectedCohortIds": ReadonlyArray<EvaluationIdentifier>; readonly "unaffectedCohortIds": ReadonlyArray<EvaluationIdentifier>; readonly "evidence": ReadonlyArray<EvaluationEvidenceReference>; };

export type EvaluationAttribution = { readonly "attributionId": Uuid; readonly "attributionVersion": EvaluationVersion; readonly "regression": EvaluationResourceReference; readonly "availability": "available" | "unavailable" | "insufficient_evidence" | "incompatible"; readonly "candidates": ReadonlyArray<EvaluationAttributionCandidate>; readonly "capturePolicy": "metadata_only"; readonly "createdAt": UtcInstant; };

export type EvaluationRegression = { readonly "schemaVersion": 1; readonly "resourceType": "regression_decision"; readonly "regressionId": Uuid; readonly "regressionVersion": EvaluationVersion; readonly "experiment": EvaluationResourceReference; readonly "baseline": EvaluationResourceReference; readonly "variant": EvaluationExperimentVariant; readonly "cohortIds": ReadonlyArray<EvaluationIdentifier>; readonly "baselineResultIds": ReadonlyArray<Uuid>; readonly "candidateResultIds": ReadonlyArray<Uuid>; readonly "comparisons": ReadonlyArray<EvaluationMetricComparison>; readonly "decision": EvaluationRegressionOutcome; readonly "evidenceState": EvaluationRegressionEvidenceState; readonly "evidence": ReadonlyArray<EvaluationEvidenceReference>; readonly "attribution": EvaluationAttribution; readonly "capturePolicy": "metadata_only"; readonly "createdAt": UtcInstant; };

export type EvaluationRegressionPage = { readonly "items": ReadonlyArray<EvaluationRegression>; readonly "nextCursor": Cursor | null; readonly "snapshotAt": UtcInstant; readonly "partial": boolean; };

export type EvaluationHistoryEntry = { readonly "historyId": Uuid; readonly "resourceType": EvaluationResourceType; readonly "resourceId": string; readonly "resourceVersion": EvaluationVersion | null; readonly "change": "created" | "updated" | "state_changed" | "cancel_requested" | "qualified" | "revoked" | "candidate_created" | "promoted" | "comparison_completed" | "attribution_completed" | "deletion_requested" | "deleted"; readonly "state": EvaluationIdentifier; readonly "occurredAt": UtcInstant; };

export type EvaluationHistoryPage = { readonly "items": ReadonlyArray<EvaluationHistoryEntry>; readonly "nextCursor": Cursor | null; readonly "snapshotAt": UtcInstant; readonly "partial": boolean; };

export type EvaluationDeletionScope = "job" | "dataset" | "suite" | "cutoff" | "purge_all";

export type DeleteEvaluationJobRequest = { readonly "commandId": Uuid; readonly "requestedAt": UtcInstant; readonly "kind": "job"; readonly "jobId": Uuid; };

export type DeleteEvaluationDatasetRequest = { readonly "commandId": Uuid; readonly "requestedAt": UtcInstant; readonly "kind": "dataset"; readonly "datasetId": Uuid; };

export type DeleteEvaluationSuiteRequest = { readonly "commandId": Uuid; readonly "requestedAt": UtcInstant; readonly "kind": "suite"; readonly "suiteId": Uuid; };

export type DeleteEvaluationBeforeRequest = { readonly "commandId": Uuid; readonly "requestedAt": UtcInstant; readonly "kind": "cutoff"; readonly "cutoff": UtcInstant; };

export type PurgeAllEvaluationRequest = { readonly "commandId": Uuid; readonly "requestedAt": UtcInstant; readonly "kind": "purge_all"; };

export type CreateEvaluationDeletionRequest = DeleteEvaluationJobRequest | DeleteEvaluationDatasetRequest | DeleteEvaluationSuiteRequest | DeleteEvaluationBeforeRequest | PurgeAllEvaluationRequest;

export type EvaluationDeletionStatus = "queued" | "running" | "succeeded" | "failed";

export type EvaluationDeletionAction = { readonly "actionId": Uuid; readonly "scope": EvaluationDeletionScope; readonly "resourceId": string | null; readonly "resourceVersion": EvaluationVersion | null; readonly "cutoff": UtcInstant | null; readonly "status": EvaluationDeletionStatus; readonly "deletedArtifactCount": number; readonly "deletedMetadataCount": number; readonly "failureCode": EvaluationIdentifier | null; readonly "createdAt": UtcInstant; readonly "startedAt": UtcInstant | null; readonly "finishedAt": UtcInstant | null; };

export type OperationalVersion = string;

export type OperationalResourceId = string;

export type OperationalResourceType = "dashboard" | "saved_query" | "alert_rule" | "alert_group" | "alert_instance" | "alert_suppression" | "alert_acknowledgement" | "incident" | "annotation";

export type OperationalResourceReference = { readonly "resourceType": OperationalResourceType; readonly "resourceId": OperationalResourceId; readonly "resourceVersion": OperationalVersion; };

export type AlertRuleReference = { readonly "resourceType": "alert_rule"; readonly "resourceId": OperationalResourceId; readonly "resourceVersion": OperationalVersion; };

export type AlertInstanceReference = { readonly "resourceType": "alert_instance"; readonly "resourceId": OperationalResourceId; readonly "resourceVersion": OperationalVersion; };

export type OperationalPageMetadata = { readonly "nextCursor": Cursor | null; readonly "snapshotAt": UtcInstant; readonly "partial": boolean; };

export type OperationalQueryKind = "analytics" | "run_search" | "trace_search" | "run_comparison" | "regression_search" | "component_health";

export type OperationalWindow = "fifteen_minutes" | "one_hour" | "six_hours" | "twenty_four_hours" | "seven_days" | "thirty_one_days";

export type OperationalFacetName = "component" | "component_version" | "operation" | "deployment" | "environment" | "host" | "service_role" | "provider" | "outcome" | "error_class" | "status" | "retrieval_stage" | "model" | "regression_outcome";

export type OperationalFacet = { readonly "name": OperationalFacetName; readonly "value": DimensionValue; };

export type AnalyticsSavedQuery = { readonly "kind": "analytics"; readonly "metric": AnalyticsMetricName; readonly "unit": AnalyticsUnit; readonly "aggregation": AnalyticsAggregation; readonly "window": OperationalWindow; readonly "bucket": AnalyticsBucket; readonly "groupBy": AnalyticsGroupBy; readonly "focus": AnalyticsFocus; readonly "facets": ReadonlyArray<OperationalFacet>; readonly "seriesLimit": number; readonly "pointLimit": number; };

export type RunSearchSavedQuery = { readonly "kind": "run_search"; readonly "window": OperationalWindow; readonly "statuses": ReadonlyArray<RunStatus>; readonly "components": ReadonlyArray<ComponentId>; readonly "deployments": ReadonlyArray<SafeLabel>; readonly "models": ReadonlyArray<ModelIdentifier>; readonly "pageSize": number; };

export type TraceSearchSavedQuery = { readonly "kind": "trace_search"; readonly "window": OperationalWindow; readonly "components": ReadonlyArray<ComponentId>; readonly "outcomes": ReadonlyArray<Outcome>; readonly "pageSize": number; };

export type RunComparisonSavedQuery = { readonly "kind": "run_comparison"; readonly "baselineRunId": Uuid; readonly "candidateRunId": Uuid; };

export type RegressionSearchSavedQuery = { readonly "kind": "regression_search"; readonly "window": OperationalWindow; readonly "outcomes": ReadonlyArray<EvaluationRegressionOutcome>; readonly "experimentReferences": ReadonlyArray<EvaluationResourceReference>; readonly "pageSize": number; };

export type ComponentHealthSavedQuery = { readonly "kind": "component_health"; readonly "componentId": ComponentId; readonly "componentVersion": SafeLabel; readonly "window": OperationalWindow; readonly "pointLimit": number; };

export type SavedQueryDefinition = AnalyticsSavedQuery | RunSearchSavedQuery | TraceSearchSavedQuery | RunComparisonSavedQuery | RegressionSearchSavedQuery | ComponentHealthSavedQuery;

export type CreateSavedQueryRequest = { readonly "name": string; readonly "definition": SavedQueryDefinition; readonly "createdAt": UtcInstant; };

export type SavedQuery = { readonly "queryId": Uuid; readonly "queryVersion": OperationalVersion; readonly "name": string; readonly "definition": SavedQueryDefinition; readonly "createdAt": UtcInstant; };

export type SavedQueryPage = { readonly "items": ReadonlyArray<SavedQuery>; readonly "nextCursor": Cursor | null; readonly "snapshotAt": UtcInstant; readonly "partial": boolean; };

export type SavedQueryReference = { readonly "queryId": Uuid; readonly "queryVersion": OperationalVersion; };

export type SavedQueryExecutionState = "complete" | "partial" | "unavailable" | "incompatible";

export type SavedQueryFreshnessState = "fresh" | "stale" | "unavailable";

export type SavedQueryExecutionFreshness = { readonly "state": SavedQueryFreshnessState; readonly "measuredAt": UtcInstant | null; readonly "lagMs": number | null; };

export type AnalyticsSavedQueryExecution = { readonly "kind": "analytics"; readonly "query": SavedQueryReference; readonly "state": SavedQueryExecutionState; readonly "partial": boolean; readonly "snapshotAt": UtcInstant; readonly "freshness": SavedQueryExecutionFreshness; readonly "result": OperationalAnalyticsSnapshot | null; };

export type RunSearchSavedQueryExecution = { readonly "kind": "run_search"; readonly "query": SavedQueryReference; readonly "state": SavedQueryExecutionState; readonly "partial": boolean; readonly "snapshotAt": UtcInstant; readonly "freshness": SavedQueryExecutionFreshness; readonly "result": RunPage | null; readonly "nextCursor": Cursor | null; };

export type TraceSearchSavedQueryExecution = { readonly "kind": "trace_search"; readonly "query": SavedQueryReference; readonly "state": SavedQueryExecutionState; readonly "partial": boolean; readonly "snapshotAt": UtcInstant; readonly "freshness": SavedQueryExecutionFreshness; readonly "result": RunPage | null; readonly "nextCursor": Cursor | null; };

export type RunComparisonSavedQueryExecution = { readonly "kind": "run_comparison"; readonly "query": SavedQueryReference; readonly "state": SavedQueryExecutionState; readonly "partial": boolean; readonly "snapshotAt": UtcInstant; readonly "freshness": SavedQueryExecutionFreshness; readonly "result": RunComparison | null; };

export type RegressionSearchSavedQueryExecution = { readonly "kind": "regression_search"; readonly "query": SavedQueryReference; readonly "state": SavedQueryExecutionState; readonly "partial": boolean; readonly "snapshotAt": UtcInstant; readonly "freshness": SavedQueryExecutionFreshness; readonly "result": EvaluationRegressionPage | null; readonly "nextCursor": Cursor | null; };

export type ComponentHealthSavedQueryExecution = { readonly "kind": "component_health"; readonly "query": SavedQueryReference; readonly "state": SavedQueryExecutionState; readonly "partial": boolean; readonly "snapshotAt": UtcInstant; readonly "freshness": SavedQueryExecutionFreshness; readonly "result": ComponentHealth | null; };

export type SavedQueryExecutionResult = AnalyticsSavedQueryExecution | RunSearchSavedQueryExecution | TraceSearchSavedQueryExecution | RunComparisonSavedQueryExecution | RegressionSearchSavedQueryExecution | ComponentHealthSavedQueryExecution;

export type DashboardVisualization = "line" | "area" | "bar" | "stat" | "table" | "trace" | "comparison" | "regression";

export type DashboardPanelSize = "small" | "medium" | "large" | "full_width";

export type DashboardPanel = { readonly "panelId": Uuid; readonly "title": string; readonly "query": SavedQueryReference; readonly "visualization": DashboardVisualization; readonly "size": DashboardPanelSize; readonly "position": number; };

export type DashboardPresetKind = "system" | "agent" | "module" | "model" | "regression";

export type DashboardPresetPanel = { readonly "panelId": Uuid; readonly "title": string; readonly "query": SavedQueryDefinition; readonly "visualization": DashboardVisualization; readonly "size": DashboardPanelSize; readonly "position": number; };

export type DashboardPreset = { readonly "presetId": DefinitionIdentifier; readonly "presetVersion": OperationalVersion; readonly "kind": DashboardPresetKind; readonly "name": string; readonly "panels": ReadonlyArray<DashboardPresetPanel>; readonly "immutable": true; };

export type DashboardPresetPage = { readonly "items": ReadonlyArray<DashboardPreset>; readonly "nextCursor": Cursor | null; readonly "snapshotAt": UtcInstant; readonly "partial": boolean; };

export type CreateDashboardRequest = { readonly "name": string; readonly "description": string; readonly "panels": ReadonlyArray<DashboardPanel>; readonly "createdAt": UtcInstant; };

export type Dashboard = { readonly "dashboardId": Uuid; readonly "dashboardVersion": OperationalVersion; readonly "name": string; readonly "description": string; readonly "panels": ReadonlyArray<DashboardPanel>; readonly "createdAt": UtcInstant; };

export type DashboardPage = { readonly "items": ReadonlyArray<Dashboard>; readonly "nextCursor": Cursor | null; readonly "snapshotAt": UtcInstant; readonly "partial": boolean; };

export type DeleteOperationalResourceRequest = { readonly "expectedVersion": OperationalVersion; readonly "requestedAt": UtcInstant; };

export type OperationalSeverity = "info" | "warning" | "error" | "critical";

export type AlertSignal = "latency" | "error" | "saturation" | "quality" | "availability" | "privacy" | "freshness";

export type AlertThresholdOperator = "greater_than" | "greater_than_or_equal" | "less_than" | "less_than_or_equal" | "equal" | "not_equal";

export type AlertMissingEvidencePolicy = "no_data" | "inconclusive" | "firing";

export type AlertCondition = { readonly "signal": AlertSignal; readonly "query": SavedQueryReference; readonly "operator": AlertThresholdOperator; readonly "threshold": number; readonly "unit": AnalyticsUnit; readonly "window": OperationalWindow; readonly "minimumSampleSize": number; readonly "missingEvidencePolicy": AlertMissingEvidencePolicy; readonly "staleAfterSeconds": number; readonly "groupBy": ReadonlyArray<OperationalFacetName>; };

export type AlertRuleState = "enabled" | "disabled";

export type AlertCadence = "one_minute" | "five_minutes" | "fifteen_minutes" | "one_hour";

export type CreateAlertRuleRequest = { readonly "name": string; readonly "severity": OperationalSeverity; readonly "condition": AlertCondition; readonly "cadence": AlertCadence; readonly "state": AlertRuleState; readonly "createdAt": UtcInstant; };

export type AlertRule = { readonly "ruleId": Uuid; readonly "ruleVersion": OperationalVersion; readonly "name": string; readonly "severity": OperationalSeverity; readonly "condition": AlertCondition; readonly "cadence": AlertCadence; readonly "state": AlertRuleState; readonly "createdAt": UtcInstant; readonly "updatedAt": UtcInstant; };

export type AlertRulePage = { readonly "items": ReadonlyArray<AlertRule>; readonly "nextCursor": Cursor | null; readonly "snapshotAt": UtcInstant; readonly "partial": boolean; };

export type ChangeAlertRuleStateRequest = { readonly "expectedVersion": OperationalVersion; readonly "state": AlertRuleState; readonly "requestedAt": UtcInstant; };

export type AlertInstanceState = "firing" | "recovered" | "stale" | "partial" | "no_data" | "inconclusive";

export type AlertEvidenceState = "complete" | "partial" | "missing" | "stale" | "incompatible" | "unavailable";

export type AlertEvidenceReference = { readonly "resourceType": "run" | "deployment" | "experiment" | "regression" | "analytics_snapshot" | "component_health"; readonly "resourceId": OperationalResourceId; readonly "resourceVersion": OperationalVersion; readonly "observedAt": UtcInstant; };

export type AlertInstance = { readonly "instanceId": Uuid; readonly "instanceVersion": OperationalVersion; readonly "rule": AlertRuleReference; readonly "groupingId": Uuid; readonly "groupingFacets": ReadonlyArray<OperationalFacet>; readonly "severity": OperationalSeverity; readonly "state": AlertInstanceState; readonly "evidenceState": AlertEvidenceState; readonly "observedValue": number | null; readonly "unit": AnalyticsUnit; readonly "sampleSize": number; readonly "evidence": ReadonlyArray<AlertEvidenceReference>; readonly "firstObservedAt": UtcInstant; readonly "lastObservedAt": UtcInstant; readonly "occurrenceCount": number; readonly "suppressed": boolean; readonly "acknowledged": boolean; };

export type AlertInstancePage = { readonly "items": ReadonlyArray<AlertInstance>; readonly "nextCursor": Cursor | null; readonly "snapshotAt": UtcInstant; readonly "partial": boolean; };

export type AlertAcknowledgementReason = "investigating" | "known_issue" | "accepted_risk" | "false_positive";

export type CreateAlertAcknowledgementRequest = { readonly "instanceVersion": OperationalVersion; readonly "reason": AlertAcknowledgementReason; readonly "acknowledgedAt": UtcInstant; };

export type AlertAcknowledgement = { readonly "acknowledgementId": Uuid; readonly "acknowledgementVersion": OperationalVersion; readonly "instance": AlertInstanceReference; readonly "reason": AlertAcknowledgementReason; readonly "acknowledgedAt": UtcInstant; };

export type AlertSuppressionScope = "rule" | "group" | "instance";

export type AlertSuppressionRuleTarget = { readonly "resourceType": "alert_rule"; readonly "resourceId": OperationalResourceId; readonly "resourceVersion": OperationalVersion; };

export type AlertSuppressionGroupTarget = { readonly "resourceType": "alert_group"; readonly "resourceId": OperationalResourceId; readonly "resourceVersion": OperationalVersion; };

export type AlertSuppressionInstanceTarget = { readonly "resourceType": "alert_instance"; readonly "resourceId": OperationalResourceId; readonly "resourceVersion": OperationalVersion; };

export type AlertSuppressionTarget = AlertSuppressionRuleTarget | AlertSuppressionGroupTarget | AlertSuppressionInstanceTarget;

export type AlertSuppressionState = "scheduled" | "active" | "expired" | "cleared";

export type CreateAlertSuppressionRequest = { readonly "scope": AlertSuppressionScope; readonly "target": AlertSuppressionTarget; readonly "startsAt": UtcInstant; readonly "endsAt": UtcInstant | null; readonly "reason": "maintenance" | "known_issue" | "testing" | "noise_reduction"; };

export type AlertSuppression = { readonly "suppressionId": Uuid; readonly "suppressionVersion": OperationalVersion; readonly "scope": AlertSuppressionScope; readonly "target": AlertSuppressionTarget; readonly "startsAt": UtcInstant; readonly "endsAt": UtcInstant | null; readonly "state": AlertSuppressionState; readonly "reason": "maintenance" | "known_issue" | "testing" | "noise_reduction"; readonly "createdAt": UtcInstant; readonly "clearedAt": UtcInstant | null; };

export type AlertSuppressionPage = { readonly "items": ReadonlyArray<AlertSuppression>; readonly "nextCursor": Cursor | null; readonly "snapshotAt": UtcInstant; readonly "partial": boolean; };

export type ClearAlertSuppressionRequest = { readonly "expectedVersion": OperationalVersion; readonly "clearedAt": UtcInstant; };

export type IncidentState = "open" | "investigating" | "mitigated" | "resolved" | "closed";

export type IncidentImpact = "none" | "limited" | "degraded" | "major" | "critical";

export type CreateIncidentRequest = { readonly "title": string; readonly "severity": OperationalSeverity; readonly "impact": IncidentImpact; readonly "relatedAlerts": ReadonlyArray<AlertInstanceReference>; readonly "evidence": ReadonlyArray<AlertEvidenceReference>; readonly "createdAt": UtcInstant; };

export type Incident = { readonly "incidentId": Uuid; readonly "incidentVersion": OperationalVersion; readonly "title": string; readonly "state": IncidentState; readonly "severity": OperationalSeverity; readonly "impact": IncidentImpact; readonly "relatedAlerts": ReadonlyArray<AlertInstanceReference>; readonly "evidence": ReadonlyArray<AlertEvidenceReference>; readonly "resolutionNote": string | null; readonly "createdAt": UtcInstant; readonly "updatedAt": UtcInstant; };

export type IncidentPage = { readonly "items": ReadonlyArray<Incident>; readonly "nextCursor": Cursor | null; readonly "snapshotAt": UtcInstant; readonly "partial": boolean; };

export type TransitionIncidentRequest = { readonly "expectedVersion": OperationalVersion; readonly "targetState": IncidentState; readonly "impact": IncidentImpact; readonly "resolutionNote": string | null; readonly "transitionedAt": UtcInstant; };

export type AnnotationTargetType = "run" | "deployment" | "experiment" | "regression" | "incident";

export type AnnotationTargetReference = { readonly "targetType": AnnotationTargetType; readonly "targetId": OperationalResourceId; readonly "targetVersion": OperationalVersion; };

export type AnnotationText = string;

export type CreateAnnotationRequest = { readonly "target": AnnotationTargetReference; readonly "text": AnnotationText; readonly "createdAt": UtcInstant; };

export type Annotation = { readonly "annotationId": Uuid; readonly "annotationVersion": OperationalVersion; readonly "target": AnnotationTargetReference; readonly "text": AnnotationText; readonly "createdAt": UtcInstant; readonly "updatedAt": UtcInstant; };

export type AnnotationPage = { readonly "items": ReadonlyArray<Annotation>; readonly "nextCursor": Cursor | null; readonly "snapshotAt": UtcInstant; readonly "partial": boolean; };

export type OperationsHistoryChange = "created" | "updated" | "state_changed" | "evaluated" | "firing" | "recovered" | "suppressed" | "suppression_cleared" | "acknowledged" | "transitioned" | "deleted";

export type OperationsHistoryEntry = { readonly "historyId": Uuid; readonly "resourceType": OperationalResourceType; readonly "resourceId": OperationalResourceId; readonly "resourceVersion": OperationalVersion; readonly "change": OperationsHistoryChange; readonly "state": DefinitionIdentifier; readonly "occurredAt": UtcInstant; };

export type OperationsHistoryPage = { readonly "items": ReadonlyArray<OperationsHistoryEntry>; readonly "nextCursor": Cursor | null; readonly "snapshotAt": UtcInstant; readonly "partial": boolean; };

export type OperationalLiveEvent = { readonly "schemaVersion": 1; readonly "eventId": Uuid; readonly "eventType": "operations.changed"; readonly "occurredAt": UtcInstant; readonly "resourceType": OperationalResourceType; readonly "resourceId": OperationalResourceId; readonly "resourceVersion": OperationalVersion; readonly "change": OperationsHistoryChange; readonly "state": DefinitionIdentifier; readonly "capturePolicy": "metadata_only"; };

export type TraceId = string;

export type SpanId = string;

export type Cursor = string;

export type RunStatus = "queued" | "running" | "succeeded" | "failed" | "cancelled" | "unknown";

export type Outcome = "succeeded" | "failed" | "cancelled" | "unknown";

export type ServiceState = "healthy" | "degraded" | "unavailable";

export type HealthState = "healthy" | "delayed" | "partial" | "degraded" | "unavailable";

export type RejectionReason = "encoding" | "json" | "unsupported_fields" | "dropped_nonzero" | "status_code" | "status_message" | "shape_object" | "shape_array" | "shape_trace_id" | "shape_span_id" | "shape_parent_span_id" | "shape_link_trace_id" | "shape_link_span_id" | "shape_attribute_key" | "shape_attribute_value" | "scope" | "link" | "event" | "attribute" | "shape" | "contract_value" | "payload_too_large" | "schema_validation" | "malformed";

export type NonNegativeCount = number;

export type BoundedLatencyMs = number;

export type BoundedLagMs = number;

export type OperationName = string;

export type EnvironmentName = string;

export type DimensionValue = string;

export type ServiceRole = "api" | "worker" | "scheduler" | "cli";

export type AnalyticsFocus = "all" | "model" | "queue" | "retrieval" | "tool" | "memory" | "automation";

export type AnalyticsUnit = "count" | "milliseconds" | "bytes" | "ratio" | "score";

export type AnalyticsMetricName = "operation_duration_ms" | "run_duration_ms" | "queue_wait_ms" | "errors" | "retries" | "backlog" | "backlog_depth" | "inference_duration_ms" | "time_to_first_token_ms" | "output_tokens" | "provider_errors" | "model_routing_duration_ms" | "structured_inference_duration_ms" | "tool_selection_duration_ms" | "tool_selection_outcome" | "tool_policy_evaluation_duration_ms" | "tool_policy_decision" | "tool_argument_validation_outcome" | "tool_execution_duration_ms" | "tool_execution_outcome" | "tool_timeout_count" | "memory_extraction_duration_ms" | "memory_candidate_decision" | "memory_reviewable_candidate_count" | "memory_operation_duration_ms" | "memory_operation_outcome" | "memory_retrieval_duration_ms" | "memory_lexical_search_duration_ms" | "memory_vector_search_duration_ms" | "memory_rerank_duration_ms" | "memory_processing_errors" | "memory_retry_count" | "memory_reindex_backlog" | "document_retrieval_duration_ms" | "document_retrieval_outcome" | "document_retrieval_candidate_count" | "reminder_scheduler_scan_duration_ms" | "reminder_due_lag_ms" | "reminder_series_due_lag_ms" | "reminder_fired_count" | "reminder_failure_count" | "notification_failure_count" | "notification_acknowledgement_outcome";

export type AnalyticsAggregation = "average" | "sum" | "minimum" | "maximum" | "p50" | "p90" | "p95" | "p99" | "rate" | "count";

export type AnalyticsBucket = "auto" | "one_minute" | "five_minutes" | "fifteen_minutes" | "one_hour" | "six_hours" | "one_day";

export type AnalyticsGroupBy = "none" | "component" | "component_version" | "operation" | "deployment" | "environment" | "host" | "service_role" | "provider" | "outcome" | "error_class" | "status" | "retrieval_stage";

export type AnalyticsAvailability = "available" | "no_data" | "unavailable";

export type AnalyticsPartialReason = "scan_limit" | "execution_limit" | "series_limit" | "point_limit" | "late_data" | "retention_boundary";

export type AnalyticsUnavailableSignal = "model_identity" | "retryability" | "input_cohort" | "gpu" | "selected_evidence" | "side_effect_detail" | "quality" | "saturation" | "measurement" | "unit" | "dimension";

export type AnalyticsPercentileMethod = "clickhouse_quantile_exact";

export type AnalyticsMetricDefinition = { readonly "metric": AnalyticsMetricName; readonly "unit": AnalyticsUnit; readonly "focusAreas": ReadonlyArray<AnalyticsFocus>; readonly "supportedAggregations": ReadonlyArray<AnalyticsAggregation>; readonly "supportedGroupings": ReadonlyArray<AnalyticsGroupBy>; readonly "availability": AnalyticsAvailability; readonly "unavailableSignals": ReadonlyArray<AnalyticsUnavailableSignal>; };

export type AnalyticsLimits = { readonly "defaultWindowHours": 24; readonly "maximumWindowDays": 31; readonly "maximumPointsPerSeries": 200; readonly "maximumSeries": 12; readonly "maximumBreakdowns": 50; readonly "maximumScannedRows": 1000000; readonly "executionTimeoutMs": 5000; };

export type AnalyticsMetricCatalogue = { readonly "snapshotAt": UtcInstant; readonly "items": ReadonlyArray<AnalyticsMetricDefinition>; readonly "limits": AnalyticsLimits; readonly "capturePolicy": "metadata_only"; };

export type AnalyticsSeriesFacets = { readonly "component"?: ComponentId; readonly "componentVersion"?: SafeLabel; readonly "operation"?: OperationName; readonly "deployment"?: SafeLabel; readonly "environment"?: EnvironmentName; readonly "host"?: SafeLabel; readonly "serviceRole"?: ServiceRole; readonly "provider"?: DimensionValue; readonly "outcome"?: Outcome; readonly "errorClass"?: DimensionValue; readonly "status"?: DimensionValue; readonly "retrievalStage"?: DimensionValue; };

export type AnalyticsPoint = { readonly "startAt": UtcInstant; readonly "endAt": UtcInstant; readonly "value": number | null; readonly "sampleCount": NonNegativeCount; readonly "availability": AnalyticsAvailability; readonly "partial": boolean; };

export type AnalyticsSeriesSummary = { readonly "sampleCount": NonNegativeCount; readonly "minimum": number | null; readonly "maximum": number | null; readonly "average": number | null; readonly "p50": number | null; readonly "p90": number | null; readonly "p95": number | null; readonly "p99": number | null; readonly "ratePerInterval": number | null; readonly "rateIntervalSeconds": number | null; };

export type AnalyticsSeries = { readonly "seriesKey": string; readonly "facets": AnalyticsSeriesFacets; readonly "summary": AnalyticsSeriesSummary; readonly "points": ReadonlyArray<AnalyticsPoint>; readonly "availability": AnalyticsAvailability; readonly "partial": boolean; };

export type AnalyticsBreakdown = { readonly "groupBy": AnalyticsGroupBy; readonly "value": DimensionValue; readonly "count": NonNegativeCount; readonly "partial": boolean; };

export type AnalyticsFreshness = { readonly "latestObservedAt": UtcInstant | null; readonly "lagMs": BoundedLagMs | null; readonly "state": "fresh" | "stale" | "unavailable"; };

export type AnalyticsEffectiveQuery = { readonly "metric": AnalyticsMetricName; readonly "unit": AnalyticsUnit; readonly "aggregation": AnalyticsAggregation; readonly "focus": AnalyticsFocus; readonly "from": UtcInstant; readonly "to": UtcInstant; readonly "bucket": AnalyticsBucket; readonly "bucketSeconds": number; readonly "groupBy": AnalyticsGroupBy; readonly "filters": AnalyticsSeriesFacets; readonly "seriesLimit": number; readonly "pointLimit": number; };

export type AnalyticsScan = { readonly "scannedRows": number; readonly "scanLimit": 1000000; readonly "executionMs": number; readonly "executionLimitMs": 5000; };

export type OperationalAnalyticsSnapshot = { readonly "snapshotAt": UtcInstant; readonly "query": AnalyticsEffectiveQuery; readonly "percentileMethod": AnalyticsPercentileMethod; readonly "series": ReadonlyArray<AnalyticsSeries>; readonly "breakdowns": ReadonlyArray<AnalyticsBreakdown>; readonly "freshness": AnalyticsFreshness; readonly "partial": boolean; readonly "partialReasons": ReadonlyArray<AnalyticsPartialReason>; readonly "unavailableSignals": ReadonlyArray<AnalyticsUnavailableSignal>; readonly "scan": AnalyticsScan; readonly "capturePolicy": "metadata_only"; };

export type RunComparisonRequest = { readonly "leftRunId": Uuid; readonly "rightRunId": Uuid; };

export type ComparisonAvailability = "available" | "missing" | "unavailable";

export type ComparisonState = "equal" | "changed" | "missing" | "unavailable";

export type ComparisonAlignment = "matched" | "left_only" | "right_only" | "incompatible";

export type ComparisonMarkerName = "deployment_id" | "git_commit" | "image_version" | "configuration_version" | "model_id" | "model_version" | "agent_revision" | "prompt_bundle_revision" | "policy_revision";

export type ComparisonMarker = { readonly "availability": ComparisonAvailability; readonly "value": RevisionIdentifier | null; };

export type ModelComparisonMarker = { readonly "availability": ComparisonAvailability; readonly "value": ModelIdentifier | null; };

export type MetadataComparisonMarkerName = "deployment_id" | "git_commit" | "image_version" | "configuration_version" | "model_version" | "agent_revision" | "prompt_bundle_revision" | "policy_revision";

export type MetadataMarkerComparison = { readonly "marker": MetadataComparisonMarkerName; readonly "state": ComparisonState; readonly "left": ComparisonMarker; readonly "right": ComparisonMarker; };

export type ModelMarkerComparison = { readonly "marker": "model_id"; readonly "state": ComparisonState; readonly "left": ModelComparisonMarker; readonly "right": ModelComparisonMarker; };

export type MarkerComparison = MetadataMarkerComparison | ModelMarkerComparison;

export type RunComparisonComponentVersion = { readonly "componentId": ComponentId; readonly "version": ComparisonMarker; };

export type RunComparisonSide = { readonly "role": "left" | "right"; readonly "runId": Uuid; readonly "evidence": ComparisonAvailability; readonly "status": RunStatus; readonly "outcome": Outcome; readonly "startedAt": UtcInstant | null; readonly "finishedAt": UtcInstant | null; readonly "durationMs": number | null; readonly "traceId": TraceId | null; readonly "modelId": ModelComparisonMarker; readonly "modelVersion": ComparisonMarker; readonly "deploymentId": ComparisonMarker; readonly "gitCommit": ComparisonMarker; readonly "imageVersion": ComparisonMarker; readonly "configurationVersion": ComparisonMarker; readonly "agentRevision": ComparisonMarker; readonly "promptBundleRevision": ComparisonMarker; readonly "policyRevision": ComparisonMarker; readonly "componentVersions": ReadonlyArray<RunComparisonComponentVersion>; readonly "partial": boolean; };

export type ComparisonNumber = { readonly "availability": ComparisonAvailability; readonly "value": number | null; };

export type ComponentMeasurementComparison = { readonly "name": OperationName; readonly "unit": AnalyticsUnit; readonly "state": ComparisonState; readonly "left": ComparisonNumber; readonly "right": ComparisonNumber; readonly "absoluteDelta": number | null; };

export type ComponentComparisonSide = { readonly "componentVersion": ComparisonMarker; readonly "durationMs": number | null; readonly "outcome": Outcome; readonly "spanCount": number; readonly "partial": boolean; };

export type RunComponentComparison = { readonly "componentId": ComponentId; readonly "operation": OperationName; readonly "alignment": ComparisonAlignment; readonly "left": ComponentComparisonSide | null; readonly "right": ComponentComparisonSide | null; readonly "measurements": ReadonlyArray<ComponentMeasurementComparison>; };

export type TraceIntegrityState = "complete" | "retained_partial" | "truncated" | "missing_parent" | "cyclic" | "duplicate";

export type ComparableSpan = { readonly "spanId": SpanId; readonly "parentSpanId": SpanId | null; readonly "componentId": ComponentId; readonly "componentVersion": SafeLabel; readonly "operation": OperationName; readonly "startedAt": UtcInstant; readonly "durationMs": number; readonly "outcome": Outcome; readonly "partial": boolean; };

export type AlignedSpan = { readonly "alignmentKey": string; readonly "alignment": ComparisonAlignment; readonly "left": ComparableSpan | null; readonly "right": ComparableSpan | null; readonly "leftIntegrity": ReadonlyArray<TraceIntegrityState>; readonly "rightIntegrity": ReadonlyArray<TraceIntegrityState>; };

export type TraceComparison = { readonly "leftTraceId": TraceId | null; readonly "rightTraceId": TraceId | null; readonly "leftEvidence": ComparisonAvailability; readonly "rightEvidence": ComparisonAvailability; readonly "leftIntegrity": ReadonlyArray<TraceIntegrityState>; readonly "rightIntegrity": ReadonlyArray<TraceIntegrityState>; readonly "alignedSpans": ReadonlyArray<AlignedSpan>; readonly "partial": boolean; };

export type RunComparisonPartialReason = "observation_limit" | "span_limit" | "measurement_limit" | "scan_limit" | "execution_limit" | "late_data" | "retention_boundary" | "incomplete_run" | "missing_parent" | "cyclic" | "duplicate_span";

export type RunComparisonLimits = { readonly "runCount": 2; readonly "maximumObservationsPerRun": 10000; readonly "maximumSpansPerRun": 5000; readonly "maximumMeasurements": 2000; readonly "maximumScannedRows": 1000000; readonly "executionTimeoutMs": 5000; };

export type RunComparison = { readonly "snapshotAt": UtcInstant; readonly "left": RunComparisonSide; readonly "right": RunComparisonSide; readonly "markerCorrelations": ReadonlyArray<MarkerComparison>; readonly "components": ReadonlyArray<RunComponentComparison>; readonly "trace": TraceComparison; readonly "freshness": AnalyticsFreshness; readonly "partial": boolean; readonly "partialReasons": ReadonlyArray<RunComparisonPartialReason>; readonly "unavailableMarkerCount": number; readonly "scan": AnalyticsScan; readonly "limits": RunComparisonLimits; readonly "capturePolicy": "metadata_only"; };

export type OwnerSession = { readonly "csrfToken": string; };

export type Problem = { readonly "code": string; readonly "message": string; readonly "retryable": boolean; readonly "traceId": TraceId; };

export type IngestCounts = { readonly "accepted": number; readonly "rejected": number; readonly "duplicate": number; readonly "malformed": number; readonly "storageFailure": number; };

export type IngestOverview = { readonly "state": ServiceState; readonly "counts": IngestCounts; readonly "lastAcceptedAt": string | null; readonly "lastFailureAt": string | null; };

export type RejectionReasonCount = { readonly "reason": RejectionReason; readonly "count": NonNegativeCount; };

export type LatencySummary = { readonly "sampleCount": NonNegativeCount; readonly "latestMs": BoundedLatencyMs | null; readonly "p50Ms": BoundedLatencyMs | null; readonly "p95Ms": BoundedLatencyMs | null; };

export type SelfMonitoringSnapshot = { readonly "snapshotId": Uuid; readonly "observedAt": UtcInstant; readonly "state": HealthState; readonly "intakeLagMs": BoundedLagMs | null; readonly "visibilityWatermark": NonNegativeCount; readonly "visibilityLagCount": NonNegativeCount; readonly "rejectionReasons": ReadonlyArray<RejectionReasonCount>; readonly "duplicateCount": NonNegativeCount; readonly "storageLatency": LatencySummary; readonly "queryLatency": LatencySummary; readonly "queryFreshnessLagMs": BoundedLagMs | null; readonly "workerHeartbeatAt": UtcInstant | null; readonly "workerHeartbeatAgeMs": BoundedLagMs | null; readonly "backlogCount": NonNegativeCount; readonly "oldestBacklogAgeMs": BoundedLagMs | null; };

export type HealthHistoryPage = { readonly "items": ReadonlyArray<SelfMonitoringSnapshot>; readonly "nextCursor": Cursor | null; readonly "snapshotAt": UtcInstant; };

export type Overview = { readonly "observedAt": UtcInstant; readonly "systemState": ServiceState; readonly "ingest": IngestOverview; readonly "observationCount": number; readonly "runCount": number; readonly "latestObservationId": Uuid | null; readonly "health"?: SelfMonitoringSnapshot; };

export type RunSummary = { readonly "runId": Uuid; readonly "status": RunStatus; readonly "startedAt": UtcInstant; readonly "finishedAt": UtcInstant | null; readonly "durationMs": number | null; readonly "model": SafeLabel | null; readonly "deploymentId": SafeLabel; readonly "componentIds": ReadonlyArray<ComponentId>; readonly "traceId": TraceId | null; };

export type RunPage = { readonly "items": ReadonlyArray<RunSummary>; readonly "nextCursor": Cursor | null; readonly "snapshotAt": UtcInstant; readonly "latestObservationId": Uuid | null; };

export type Measurement = { readonly "name": string; readonly "value": number; readonly "unit": "count" | "milliseconds" | "bytes" | "ratio" | "score" | null; };

export type RunDetail = { readonly "runId": Uuid; readonly "status": RunStatus; readonly "startedAt": UtcInstant; readonly "finishedAt": UtcInstant | null; readonly "durationMs": number | null; readonly "model": SafeLabel | null; readonly "deploymentId": SafeLabel; readonly "componentIds": ReadonlyArray<ComponentId>; readonly "traceId": TraceId | null; readonly "observations": ReadonlyArray<ObservationSummary>; readonly "partial": boolean; readonly "nextCursor": Cursor | null; };

export type ObservationSummary = { readonly "observationId": Uuid; readonly "observedAt": UtcInstant; readonly "componentId": ComponentId; readonly "componentVersion": SafeLabel; readonly "operation": string; readonly "measurement": Measurement; readonly "outcome": Outcome; readonly "deploymentId": SafeLabel; readonly "serviceRole": "api" | "worker" | "scheduler" | "cli"; readonly "capturePolicy": "metadata_only"; };

export type Span = { readonly "spanId": SpanId; readonly "parentSpanId": SpanId | null; readonly "componentId": ComponentId; readonly "operation": string; readonly "startedAt": UtcInstant; readonly "durationMs": number; readonly "outcome": Outcome; readonly "depth": number; readonly "partial": boolean; };

export type TraceDetail = { readonly "traceId": TraceId; readonly "runId": Uuid | null; readonly "partial": boolean; readonly "spans": ReadonlyArray<Span>; readonly "nextCursor": Cursor | null; };

export type HealthSummary = { readonly "sampleCount": number; readonly "latencyP50Ms": number | null; readonly "latencyP95Ms": number | null; readonly "errorRate": number | null; readonly "outcomes": { readonly "succeeded": number; readonly "failed": number; readonly "cancelled": number; readonly "unknown": number; }; };

export type ComponentDefinitionState = "declared" | "observed_only" | "missing";

export type DependencyKind = "component" | "provider";

export type DependencyAvailability = "observed" | "unobserved" | "unavailable";

export type DependencyResolution = "resolved" | "missing_definition" | "version_unavailable";

export type ComponentDependencyDeclaration = { readonly "dependencyId": DefinitionIdentifier; readonly "kind": DependencyKind; readonly "requiredVersion": SafeLabel | null; };

export type ComponentDefinition = { readonly "componentId": ComponentId; readonly "componentVersion": SafeLabel; readonly "owner": DefinitionIdentifier; readonly "category": DefinitionIdentifier; readonly "inputSchemaId": DefinitionIdentifier; readonly "outputSchemaId": DefinitionIdentifier; readonly "measurements": ReadonlyArray<DefinitionIdentifier>; readonly "evaluators": ReadonlyArray<DefinitionIdentifier>; readonly "dependencies": ReadonlyArray<ComponentDependencyDeclaration>; };

export type ComponentSummary = { readonly "componentId": ComponentId; readonly "versions": ReadonlyArray<SafeLabel>; readonly "firstSeenAt": UtcInstant; readonly "lastSeenAt": UtcInstant; readonly "health": HealthSummary; readonly "definitionState"?: ComponentDefinitionState; readonly "definitionVersions"?: ReadonlyArray<SafeLabel>; };

export type ComponentCollection = { readonly "items": ReadonlyArray<ComponentSummary>; readonly "snapshotAt": UtcInstant; readonly "nextCursor"?: Cursor | null; };

export type DependencyNode = { readonly "dependencyId": DefinitionIdentifier; readonly "kind": DependencyKind; readonly "componentVersion": SafeLabel | null; readonly "definitionState": ComponentDefinitionState; readonly "deployedVersions": ReadonlyArray<SafeLabel>; readonly "availability": DependencyAvailability; readonly "health": HealthSummary | null; readonly "partialHealth": boolean; };

export type DependencyEdge = { readonly "sourceId": ComponentId; readonly "sourceVersion": SafeLabel; readonly "targetId": DefinitionIdentifier; readonly "targetVersion": SafeLabel | null; readonly "targetKind": DependencyKind; readonly "resolution": DependencyResolution; };

export type ComponentDependencyTopology = { readonly "rootComponentId": ComponentId; readonly "rootComponentVersion": SafeLabel; readonly "nodes": ReadonlyArray<DependencyNode>; readonly "edges": ReadonlyArray<DependencyEdge>; readonly "directDependencyIds": ReadonlyArray<DefinitionIdentifier>; readonly "reverseDependencyIds": ReadonlyArray<ComponentId>; readonly "partial": boolean; readonly "snapshotAt": UtcInstant; };

export type HealthPoint = { readonly "startAt": UtcInstant; readonly "endAt": UtcInstant; readonly "summary": HealthSummary; };

export type ComponentHealth = { readonly "componentId": ComponentId; readonly "summary": HealthSummary; readonly "points": ReadonlyArray<HealthPoint>; };

export type Deployment = { readonly "deploymentId": SafeLabel; readonly "gitCommit": string; readonly "environment": string; readonly "hostId": SafeLabel; readonly "serviceRoles": ReadonlyArray<"api" | "worker" | "scheduler" | "cli">; readonly "firstSeenAt": UtcInstant; readonly "lastSeenAt": UtcInstant; };

export type DeploymentCollection = { readonly "items": ReadonlyArray<Deployment>; readonly "snapshotAt": UtcInstant; };

export type LiveEventType = "observation.changed" | "retention.changed" | "health.changed" | "heartbeat";

export type LiveEvent = { readonly "schemaVersion": 1; readonly "eventId": Uuid; readonly "eventType": LiveEventType; readonly "occurredAt": UtcInstant; readonly "resourceType": "observation" | "run" | "trace" | "component" | "deployment" | "retention" | "system"; readonly "resourceId": string | null; readonly "capturePolicy": "metadata_only"; };

export type EvaluationLiveEvent = { readonly "schemaVersion": 1; readonly "eventId": Uuid; readonly "eventType": "evaluation.changed"; readonly "occurredAt": UtcInstant; readonly "resourceType": "evaluation_dataset" | "evaluation_case" | "evaluation_suite" | "evaluation_evaluator" | "evaluation_job" | "evaluation_attempt" | "evaluation_result" | "evaluation_artifact" | "evaluation_review" | "evaluation_calibration" | "evaluation_qualification" | "evaluation_experiment" | "evaluation_baseline" | "evaluation_regression" | "evaluation_attribution" | "evaluation_deletion"; readonly "resourceId": string; readonly "change": "created" | "updated" | "state_changed" | "cancel_requested" | "qualified" | "revoked" | "candidate_created" | "promoted" | "comparison_completed" | "attribution_completed" | "deletion_requested" | "deleted"; readonly "state": EvaluationIdentifier; readonly "capturePolicy": "metadata_only"; };

export type SsePayload = Overview | LiveEvent;

export type DeleteBeforeRequest = { readonly "action": "delete_before"; readonly "cutoff": UtcInstant; };

export type PurgeAllRequest = { readonly "action": "purge_all"; };

export type CreateRetentionActionRequest = DeleteBeforeRequest | PurgeAllRequest;

export type RetentionActionStatus = "queued" | "running" | "succeeded" | "failed";

export type RetentionAction = { readonly "actionId": Uuid; readonly "action": "delete_before" | "purge_all"; readonly "cutoff": UtcInstant | null; readonly "status": RetentionActionStatus; readonly "createdAt": UtcInstant; readonly "startedAt": UtcInstant | null; readonly "finishedAt": UtcInstant | null; readonly "deletedObservationCount": number; readonly "deletedReceiptCount": number; readonly "deletedRunCount": number; readonly "failureCode": string | null; };

export interface ObservatoryApiOperations {
  readonly startOwnerLogin: { readonly response: void; readonly body: never; readonly path: Readonly<Record<string, never>>; readonly query: { readonly "return_to"?: string; }; readonly headers: Readonly<Record<string, never>>; };
  readonly getOwnerSession: { readonly response: OwnerSession; readonly body: never; readonly path: Readonly<Record<string, never>>; readonly query: Readonly<Record<string, never>>; readonly headers: Readonly<Record<string, never>>; };
  readonly logoutOwnerSession: { readonly response: void; readonly body: never; readonly path: Readonly<Record<string, never>>; readonly query: Readonly<Record<string, never>>; readonly headers: { readonly "X-Observatory-CSRF": string; }; };
  readonly getOverview: { readonly response: Overview; readonly body: never; readonly path: Readonly<Record<string, never>>; readonly query: Readonly<Record<string, never>>; readonly headers: Readonly<Record<string, never>>; };
  readonly listHealthHistory: { readonly response: HealthHistoryPage; readonly body: never; readonly path: Readonly<Record<string, never>>; readonly query: { readonly "cursor"?: Cursor; readonly "page_size"?: number; readonly "from"?: UtcInstant; readonly "to"?: UtcInstant; }; readonly headers: Readonly<Record<string, never>>; };
  readonly listRuns: { readonly response: RunPage; readonly body: never; readonly path: Readonly<Record<string, never>>; readonly query: { readonly "cursor"?: Cursor; readonly "page_size"?: number; readonly "status"?: RunStatus; readonly "component"?: ComponentId; readonly "model"?: SafeLabel; readonly "deployment"?: SafeLabel; readonly "from"?: UtcInstant; readonly "to"?: UtcInstant; }; readonly headers: Readonly<Record<string, never>>; };
  readonly getRun: { readonly response: RunDetail; readonly body: never; readonly path: { readonly "run_id": Uuid; }; readonly query: { readonly "cursor"?: Cursor; readonly "page_size"?: number; }; readonly headers: Readonly<Record<string, never>>; };
  readonly getTrace: { readonly response: TraceDetail; readonly body: never; readonly path: { readonly "trace_id": TraceId; }; readonly query: { readonly "cursor"?: Cursor; readonly "page_size"?: number; }; readonly headers: Readonly<Record<string, never>>; };
  readonly listComponents: { readonly response: ComponentCollection; readonly body: never; readonly path: Readonly<Record<string, never>>; readonly query: { readonly "cursor"?: Cursor; readonly "page_size"?: number; readonly "definition_state"?: ComponentDefinitionState; }; readonly headers: Readonly<Record<string, never>>; };
  readonly getComponentDefinition: { readonly response: ComponentDefinition; readonly body: never; readonly path: { readonly "component_id": ComponentId; readonly "component_version": SafeLabel; }; readonly query: Readonly<Record<string, never>>; readonly headers: Readonly<Record<string, never>>; };
  readonly getComponentDependencies: { readonly response: ComponentDependencyTopology; readonly body: never; readonly path: { readonly "component_id": ComponentId; readonly "component_version": SafeLabel; }; readonly query: { readonly "depth"?: number; readonly "max_nodes"?: number; }; readonly headers: Readonly<Record<string, never>>; };
  readonly getComponentHealth: { readonly response: ComponentHealth; readonly body: never; readonly path: { readonly "component_id": ComponentId; }; readonly query: { readonly "component_version"?: SafeLabel; readonly "from"?: UtcInstant; readonly "to"?: UtcInstant; readonly "point_limit"?: number; }; readonly headers: Readonly<Record<string, never>>; };
  readonly listAnalyticsMetrics: { readonly response: AnalyticsMetricCatalogue; readonly body: never; readonly path: Readonly<Record<string, never>>; readonly query: { readonly "focus"?: AnalyticsFocus; }; readonly headers: Readonly<Record<string, never>>; };
  readonly queryOperationalAnalytics: { readonly response: OperationalAnalyticsSnapshot; readonly body: never; readonly path: Readonly<Record<string, never>>; readonly query: { readonly "metric": AnalyticsMetricName; readonly "unit": AnalyticsUnit; readonly "aggregation"?: AnalyticsAggregation; readonly "focus"?: AnalyticsFocus; readonly "from"?: UtcInstant; readonly "to"?: UtcInstant; readonly "bucket"?: AnalyticsBucket; readonly "group_by"?: AnalyticsGroupBy; readonly "component"?: ComponentId; readonly "component_version"?: SafeLabel; readonly "operation"?: OperationName; readonly "deployment"?: SafeLabel; readonly "environment"?: EnvironmentName; readonly "host"?: SafeLabel; readonly "service_role"?: ServiceRole; readonly "provider"?: DimensionValue; readonly "outcome"?: Outcome; readonly "error_class"?: DimensionValue; readonly "status"?: DimensionValue; readonly "retrieval_stage"?: DimensionValue; readonly "series_limit"?: number; readonly "point_limit"?: number; }; readonly headers: Readonly<Record<string, never>>; };
  readonly compareRuns: { readonly response: RunComparison; readonly body: RunComparisonRequest; readonly path: Readonly<Record<string, never>>; readonly query: Readonly<Record<string, never>>; readonly headers: Readonly<Record<string, never>>; };
  readonly listEvaluationDatasets: { readonly response: EvaluationDatasetPage; readonly body: never; readonly path: Readonly<Record<string, never>>; readonly query: { readonly "cursor"?: Cursor; readonly "page_size"?: number; readonly "state"?: EvaluationDefinitionState; }; readonly headers: Readonly<Record<string, never>>; };
  readonly createEvaluationDataset: { readonly response: EvaluationDataset; readonly body: CreateEvaluationDatasetRequest; readonly path: Readonly<Record<string, never>>; readonly query: Readonly<Record<string, never>>; readonly headers: { readonly "X-Observatory-CSRF": string; readonly "Idempotency-Key": string; }; };
  readonly getEvaluationDataset: { readonly response: EvaluationDataset; readonly body: never; readonly path: { readonly "dataset_id": Uuid; readonly "resource_version": EvaluationVersion; }; readonly query: Readonly<Record<string, never>>; readonly headers: Readonly<Record<string, never>>; };
  readonly listEvaluationCases: { readonly response: EvaluationCasePage; readonly body: never; readonly path: { readonly "dataset_id": Uuid; readonly "resource_version": EvaluationVersion; }; readonly query: { readonly "cursor"?: Cursor; readonly "page_size"?: number; }; readonly headers: Readonly<Record<string, never>>; };
  readonly listEvaluationSuites: { readonly response: EvaluationSuitePage; readonly body: never; readonly path: Readonly<Record<string, never>>; readonly query: { readonly "cursor"?: Cursor; readonly "page_size"?: number; readonly "dataset_id"?: Uuid; }; readonly headers: Readonly<Record<string, never>>; };
  readonly createEvaluationSuite: { readonly response: EvaluationSuite; readonly body: CreateEvaluationSuiteRequest; readonly path: Readonly<Record<string, never>>; readonly query: Readonly<Record<string, never>>; readonly headers: { readonly "X-Observatory-CSRF": string; readonly "Idempotency-Key": string; }; };
  readonly getEvaluationSuite: { readonly response: EvaluationSuite; readonly body: never; readonly path: { readonly "suite_id": Uuid; readonly "resource_version": EvaluationVersion; }; readonly query: Readonly<Record<string, never>>; readonly headers: Readonly<Record<string, never>>; };
  readonly listEvaluationEvaluators: { readonly response: EvaluationEvaluatorPage; readonly body: never; readonly path: Readonly<Record<string, never>>; readonly query: { readonly "cursor"?: Cursor; readonly "page_size"?: number; readonly "kind"?: EvaluationEvaluatorKind; }; readonly headers: Readonly<Record<string, never>>; };
  readonly getEvaluationEvaluator: { readonly response: EvaluationEvaluator; readonly body: never; readonly path: { readonly "evaluator_id": Uuid; readonly "resource_version": EvaluationVersion; }; readonly query: Readonly<Record<string, never>>; readonly headers: Readonly<Record<string, never>>; };
  readonly listEvaluationJobs: { readonly response: EvaluationJobPage; readonly body: never; readonly path: Readonly<Record<string, never>>; readonly query: { readonly "cursor"?: Cursor; readonly "page_size"?: number; readonly "status"?: EvaluationJobStatus; readonly "suite_id"?: Uuid; readonly "from"?: UtcInstant; readonly "to"?: UtcInstant; }; readonly headers: Readonly<Record<string, never>>; };
  readonly submitEvaluationJob: { readonly response: EvaluationJob; readonly body: SubmitEvaluationJobRequest; readonly path: Readonly<Record<string, never>>; readonly query: Readonly<Record<string, never>>; readonly headers: { readonly "X-Observatory-CSRF": string; readonly "Idempotency-Key": string; }; };
  readonly getEvaluationJob: { readonly response: EvaluationJob; readonly body: never; readonly path: { readonly "job_id": Uuid; }; readonly query: Readonly<Record<string, never>>; readonly headers: Readonly<Record<string, never>>; };
  readonly listEvaluationAttempts: { readonly response: EvaluationAttemptPage; readonly body: never; readonly path: { readonly "job_id": Uuid; }; readonly query: { readonly "cursor"?: Cursor; readonly "page_size"?: number; }; readonly headers: Readonly<Record<string, never>>; };
  readonly cancelEvaluationJob: { readonly response: EvaluationJob; readonly body: CancelEvaluationJobRequest; readonly path: { readonly "job_id": Uuid; }; readonly query: Readonly<Record<string, never>>; readonly headers: { readonly "X-Observatory-CSRF": string; readonly "Idempotency-Key": string; readonly "X-Observatory-Confirmation": string; }; };
  readonly listEvaluationResults: { readonly response: EvaluationResultPage; readonly body: never; readonly path: Readonly<Record<string, never>>; readonly query: { readonly "cursor"?: Cursor; readonly "page_size"?: number; readonly "job_id"?: Uuid; readonly "status"?: EvaluationResultStatus; }; readonly headers: Readonly<Record<string, never>>; };
  readonly getEvaluationResult: { readonly response: EvaluationResult; readonly body: never; readonly path: { readonly "result_id": Uuid; }; readonly query: Readonly<Record<string, never>>; readonly headers: Readonly<Record<string, never>>; };
  readonly listEvaluationScores: { readonly response: EvaluationScorePage; readonly body: never; readonly path: { readonly "result_id": Uuid; }; readonly query: { readonly "cursor"?: Cursor; readonly "page_size"?: number; readonly "evaluator_id"?: EvaluationIdentifier; }; readonly headers: Readonly<Record<string, never>>; };
  readonly listEvaluationArtifacts: { readonly response: EvaluationArtifactPage; readonly body: never; readonly path: Readonly<Record<string, never>>; readonly query: { readonly "cursor"?: Cursor; readonly "page_size"?: number; readonly "job_id"?: Uuid; readonly "result_id"?: Uuid; readonly "deletion_status"?: EvaluationArtifactDeletionStatus; }; readonly headers: Readonly<Record<string, never>>; };
  readonly getEvaluationArtifact: { readonly response: EvaluationArtifact; readonly body: never; readonly path: { readonly "artifact_id": Uuid; }; readonly query: Readonly<Record<string, never>>; readonly headers: Readonly<Record<string, never>>; };
  readonly listEvaluationReviews: { readonly response: EvaluationReviewPage; readonly body: never; readonly path: Readonly<Record<string, never>>; readonly query: { readonly "cursor"?: Cursor; readonly "page_size"?: number; readonly "result_id"?: Uuid; readonly "calibration_id"?: EvaluationIdentifier; }; readonly headers: Readonly<Record<string, never>>; };
  readonly createEvaluationReview: { readonly response: EvaluationReview; readonly body: CreateEvaluationReviewRequest; readonly path: Readonly<Record<string, never>>; readonly query: Readonly<Record<string, never>>; readonly headers: { readonly "X-Observatory-CSRF": string; readonly "Idempotency-Key": string; }; };
  readonly listEvaluationCalibrations: { readonly response: EvaluationCalibrationPage; readonly body: never; readonly path: Readonly<Record<string, never>>; readonly query: { readonly "cursor"?: Cursor; readonly "page_size"?: number; readonly "eligibility"?: EvaluationCalibrationEligibility; }; readonly headers: Readonly<Record<string, never>>; };
  readonly listEvaluationQualifications: { readonly response: EvaluationQualificationPage; readonly body: never; readonly path: Readonly<Record<string, never>>; readonly query: { readonly "cursor"?: Cursor; readonly "page_size"?: number; readonly "state"?: EvaluationQualificationState; }; readonly headers: Readonly<Record<string, never>>; };
  readonly qualifyEvaluationJudge: { readonly response: EvaluationQualification; readonly body: QualifyEvaluationJudgeRequest; readonly path: Readonly<Record<string, never>>; readonly query: Readonly<Record<string, never>>; readonly headers: { readonly "X-Observatory-CSRF": string; readonly "Idempotency-Key": string; readonly "X-Observatory-Confirmation": string; }; };
  readonly revokeEvaluationQualification: { readonly response: EvaluationQualification; readonly body: RevokeEvaluationQualificationRequest; readonly path: { readonly "qualification_id": Uuid; }; readonly query: Readonly<Record<string, never>>; readonly headers: { readonly "X-Observatory-CSRF": string; readonly "Idempotency-Key": string; readonly "X-Observatory-Confirmation": string; }; };
  readonly listExperiments: { readonly response: EvaluationExperimentPage; readonly body: never; readonly path: Readonly<Record<string, never>>; readonly query: { readonly "cursor"?: Cursor; readonly "page_size"?: number; readonly "suite_id"?: Uuid; readonly "state"?: EvaluationDefinitionState; }; readonly headers: Readonly<Record<string, never>>; };
  readonly createExperiment: { readonly response: EvaluationExperiment; readonly body: CreateEvaluationExperimentRequest; readonly path: Readonly<Record<string, never>>; readonly query: Readonly<Record<string, never>>; readonly headers: { readonly "X-Observatory-CSRF": string; readonly "Idempotency-Key": string; }; };
  readonly getExperiment: { readonly response: EvaluationExperiment; readonly body: never; readonly path: { readonly "experiment_id": Uuid; }; readonly query: { readonly "version": EvaluationVersion; }; readonly headers: Readonly<Record<string, never>>; };
  readonly listBaselines: { readonly response: EvaluationBaselinePage; readonly body: never; readonly path: Readonly<Record<string, never>>; readonly query: { readonly "cursor"?: Cursor; readonly "page_size"?: number; readonly "suite_id"?: Uuid; readonly "experiment_id"?: Uuid; readonly "state"?: EvaluationBaselineState; }; readonly headers: Readonly<Record<string, never>>; };
  readonly createBaselineCandidate: { readonly response: EvaluationBaseline; readonly body: CreateEvaluationBaselineCandidateRequest; readonly path: Readonly<Record<string, never>>; readonly query: Readonly<Record<string, never>>; readonly headers: { readonly "X-Observatory-CSRF": string; readonly "Idempotency-Key": string; }; };
  readonly getBaseline: { readonly response: EvaluationBaseline; readonly body: never; readonly path: { readonly "baseline_id": Uuid; }; readonly query: { readonly "version": EvaluationVersion; }; readonly headers: Readonly<Record<string, never>>; };
  readonly promoteBaseline: { readonly response: EvaluationBaseline; readonly body: PromoteEvaluationBaselineRequest; readonly path: { readonly "baseline_id": Uuid; }; readonly query: Readonly<Record<string, never>>; readonly headers: { readonly "X-Observatory-CSRF": string; readonly "Idempotency-Key": string; readonly "X-Observatory-Confirmation": string; }; };
  readonly listRegressions: { readonly response: EvaluationRegressionPage; readonly body: never; readonly path: Readonly<Record<string, never>>; readonly query: { readonly "cursor"?: Cursor; readonly "page_size"?: number; readonly "experiment_id"?: Uuid; readonly "baseline_id"?: Uuid; readonly "decision"?: EvaluationRegressionOutcome; }; readonly headers: Readonly<Record<string, never>>; };
  readonly createRegressionComparison: { readonly response: EvaluationRegression; readonly body: CreateEvaluationRegressionRequest; readonly path: Readonly<Record<string, never>>; readonly query: Readonly<Record<string, never>>; readonly headers: { readonly "X-Observatory-CSRF": string; readonly "Idempotency-Key": string; }; };
  readonly getRegression: { readonly response: EvaluationRegression; readonly body: never; readonly path: { readonly "regression_id": Uuid; }; readonly query: { readonly "version": EvaluationVersion; }; readonly headers: Readonly<Record<string, never>>; };
  readonly getRegressionAttribution: { readonly response: EvaluationAttribution; readonly body: never; readonly path: { readonly "regression_id": Uuid; }; readonly query: { readonly "version": EvaluationVersion; }; readonly headers: Readonly<Record<string, never>>; };
  readonly listEvaluationHistory: { readonly response: EvaluationHistoryPage; readonly body: never; readonly path: Readonly<Record<string, never>>; readonly query: { readonly "cursor"?: Cursor; readonly "page_size"?: number; readonly "resource_type"?: EvaluationResourceType; readonly "resource_id"?: EvaluationIdentifier; }; readonly headers: Readonly<Record<string, never>>; };
  readonly createEvaluationDeletionAction: { readonly response: EvaluationDeletionAction; readonly body: CreateEvaluationDeletionRequest; readonly path: Readonly<Record<string, never>>; readonly query: Readonly<Record<string, never>>; readonly headers: { readonly "X-Observatory-CSRF": string; readonly "Idempotency-Key": string; readonly "X-Observatory-Confirmation": string; }; };
  readonly getEvaluationDeletionAction: { readonly response: EvaluationDeletionAction; readonly body: never; readonly path: { readonly "action_id": Uuid; }; readonly query: Readonly<Record<string, never>>; readonly headers: Readonly<Record<string, never>>; };
  readonly streamEvaluationEvents: { readonly response: EvaluationLiveEvent; readonly body: never; readonly path: Readonly<Record<string, never>>; readonly query: Readonly<Record<string, never>>; readonly headers: { readonly "Last-Event-ID"?: string; }; };
  readonly listDashboardPresets: { readonly response: DashboardPresetPage; readonly body: never; readonly path: Readonly<Record<string, never>>; readonly query: { readonly "cursor"?: Cursor; readonly "page_size"?: number; }; readonly headers: Readonly<Record<string, never>>; };
  readonly listDashboards: { readonly response: DashboardPage; readonly body: never; readonly path: Readonly<Record<string, never>>; readonly query: { readonly "cursor"?: Cursor; readonly "page_size"?: number; }; readonly headers: Readonly<Record<string, never>>; };
  readonly createDashboard: { readonly response: Dashboard; readonly body: CreateDashboardRequest; readonly path: Readonly<Record<string, never>>; readonly query: Readonly<Record<string, never>>; readonly headers: { readonly "X-Observatory-CSRF": string; readonly "Idempotency-Key": string; }; };
  readonly getDashboard: { readonly response: Dashboard; readonly body: never; readonly path: { readonly "dashboard_id": Uuid; readonly "resource_version": OperationalVersion; }; readonly query: Readonly<Record<string, never>>; readonly headers: Readonly<Record<string, never>>; };
  readonly deleteDashboard: { readonly response: void; readonly body: DeleteOperationalResourceRequest; readonly path: { readonly "dashboard_id": Uuid; readonly "resource_version": OperationalVersion; }; readonly query: Readonly<Record<string, never>>; readonly headers: { readonly "X-Observatory-CSRF": string; readonly "Idempotency-Key": string; readonly "X-Observatory-Confirmation": string; }; };
  readonly listSavedQueries: { readonly response: SavedQueryPage; readonly body: never; readonly path: Readonly<Record<string, never>>; readonly query: { readonly "cursor"?: Cursor; readonly "page_size"?: number; readonly "kind"?: OperationalQueryKind; }; readonly headers: Readonly<Record<string, never>>; };
  readonly createSavedQuery: { readonly response: SavedQuery; readonly body: CreateSavedQueryRequest; readonly path: Readonly<Record<string, never>>; readonly query: Readonly<Record<string, never>>; readonly headers: { readonly "X-Observatory-CSRF": string; readonly "Idempotency-Key": string; }; };
  readonly getSavedQuery: { readonly response: SavedQuery; readonly body: never; readonly path: { readonly "query_id": Uuid; readonly "resource_version": OperationalVersion; }; readonly query: Readonly<Record<string, never>>; readonly headers: Readonly<Record<string, never>>; };
  readonly executeSavedQuery: { readonly response: SavedQueryExecutionResult; readonly body: never; readonly path: { readonly "query_id": Uuid; readonly "resource_version": OperationalVersion; }; readonly query: { readonly "cursor"?: Cursor; }; readonly headers: Readonly<Record<string, never>>; };
  readonly deleteSavedQuery: { readonly response: void; readonly body: DeleteOperationalResourceRequest; readonly path: { readonly "query_id": Uuid; readonly "resource_version": OperationalVersion; }; readonly query: Readonly<Record<string, never>>; readonly headers: { readonly "X-Observatory-CSRF": string; readonly "Idempotency-Key": string; readonly "X-Observatory-Confirmation": string; }; };
  readonly listAlertRules: { readonly response: AlertRulePage; readonly body: never; readonly path: Readonly<Record<string, never>>; readonly query: { readonly "cursor"?: Cursor; readonly "page_size"?: number; readonly "state"?: AlertRuleState; }; readonly headers: Readonly<Record<string, never>>; };
  readonly createAlertRule: { readonly response: AlertRule; readonly body: CreateAlertRuleRequest; readonly path: Readonly<Record<string, never>>; readonly query: Readonly<Record<string, never>>; readonly headers: { readonly "X-Observatory-CSRF": string; readonly "Idempotency-Key": string; }; };
  readonly getAlertRule: { readonly response: AlertRule; readonly body: never; readonly path: { readonly "rule_id": Uuid; readonly "resource_version": OperationalVersion; }; readonly query: Readonly<Record<string, never>>; readonly headers: Readonly<Record<string, never>>; };
  readonly changeAlertRuleState: { readonly response: AlertRule; readonly body: ChangeAlertRuleStateRequest; readonly path: { readonly "rule_id": Uuid; readonly "resource_version": OperationalVersion; }; readonly query: Readonly<Record<string, never>>; readonly headers: { readonly "X-Observatory-CSRF": string; readonly "Idempotency-Key": string; readonly "X-Observatory-Confirmation": string; }; };
  readonly listAlertInstances: { readonly response: AlertInstancePage; readonly body: never; readonly path: Readonly<Record<string, never>>; readonly query: { readonly "cursor"?: Cursor; readonly "page_size"?: number; readonly "state"?: AlertInstanceState; readonly "severity"?: OperationalSeverity; readonly "rule_id"?: Uuid; }; readonly headers: Readonly<Record<string, never>>; };
  readonly getAlertInstance: { readonly response: AlertInstance; readonly body: never; readonly path: { readonly "instance_id": Uuid; }; readonly query: Readonly<Record<string, never>>; readonly headers: Readonly<Record<string, never>>; };
  readonly acknowledgeAlertInstance: { readonly response: AlertAcknowledgement; readonly body: CreateAlertAcknowledgementRequest; readonly path: { readonly "instance_id": Uuid; }; readonly query: Readonly<Record<string, never>>; readonly headers: { readonly "X-Observatory-CSRF": string; readonly "Idempotency-Key": string; }; };
  readonly listAlertSuppressions: { readonly response: AlertSuppressionPage; readonly body: never; readonly path: Readonly<Record<string, never>>; readonly query: { readonly "cursor"?: Cursor; readonly "page_size"?: number; readonly "state"?: AlertSuppressionState; }; readonly headers: Readonly<Record<string, never>>; };
  readonly createAlertSuppression: { readonly response: AlertSuppression; readonly body: CreateAlertSuppressionRequest; readonly path: Readonly<Record<string, never>>; readonly query: Readonly<Record<string, never>>; readonly headers: { readonly "X-Observatory-CSRF": string; readonly "Idempotency-Key": string; }; };
  readonly clearAlertSuppression: { readonly response: AlertSuppression; readonly body: ClearAlertSuppressionRequest; readonly path: { readonly "suppression_id": Uuid; }; readonly query: Readonly<Record<string, never>>; readonly headers: { readonly "X-Observatory-CSRF": string; readonly "Idempotency-Key": string; readonly "X-Observatory-Confirmation": string; }; };
  readonly listIncidents: { readonly response: IncidentPage; readonly body: never; readonly path: Readonly<Record<string, never>>; readonly query: { readonly "cursor"?: Cursor; readonly "page_size"?: number; readonly "state"?: IncidentState; readonly "severity"?: OperationalSeverity; }; readonly headers: Readonly<Record<string, never>>; };
  readonly createIncident: { readonly response: Incident; readonly body: CreateIncidentRequest; readonly path: Readonly<Record<string, never>>; readonly query: Readonly<Record<string, never>>; readonly headers: { readonly "X-Observatory-CSRF": string; readonly "Idempotency-Key": string; }; };
  readonly getIncident: { readonly response: Incident; readonly body: never; readonly path: { readonly "incident_id": Uuid; }; readonly query: Readonly<Record<string, never>>; readonly headers: Readonly<Record<string, never>>; };
  readonly transitionIncident: { readonly response: Incident; readonly body: TransitionIncidentRequest; readonly path: { readonly "incident_id": Uuid; }; readonly query: Readonly<Record<string, never>>; readonly headers: { readonly "X-Observatory-CSRF": string; readonly "Idempotency-Key": string; readonly "X-Observatory-Confirmation": string; }; };
  readonly listAnnotations: { readonly response: AnnotationPage; readonly body: never; readonly path: Readonly<Record<string, never>>; readonly query: { readonly "cursor"?: Cursor; readonly "page_size"?: number; readonly "target_type"?: AnnotationTargetType; readonly "target_id"?: OperationalResourceId; readonly "target_version"?: OperationalVersion; }; readonly headers: Readonly<Record<string, never>>; };
  readonly createAnnotation: { readonly response: Annotation; readonly body: CreateAnnotationRequest; readonly path: Readonly<Record<string, never>>; readonly query: Readonly<Record<string, never>>; readonly headers: { readonly "X-Observatory-CSRF": string; readonly "Idempotency-Key": string; }; };
  readonly getAnnotation: { readonly response: Annotation; readonly body: never; readonly path: { readonly "annotation_id": Uuid; readonly "resource_version": OperationalVersion; }; readonly query: Readonly<Record<string, never>>; readonly headers: Readonly<Record<string, never>>; };
  readonly deleteAnnotation: { readonly response: void; readonly body: DeleteOperationalResourceRequest; readonly path: { readonly "annotation_id": Uuid; readonly "resource_version": OperationalVersion; }; readonly query: Readonly<Record<string, never>>; readonly headers: { readonly "X-Observatory-CSRF": string; readonly "Idempotency-Key": string; readonly "X-Observatory-Confirmation": string; }; };
  readonly listOperationsHistory: { readonly response: OperationsHistoryPage; readonly body: never; readonly path: Readonly<Record<string, never>>; readonly query: { readonly "cursor"?: Cursor; readonly "page_size"?: number; readonly "resource_type"?: OperationalResourceType; readonly "resource_id"?: OperationalResourceId; readonly "resource_version"?: OperationalVersion; }; readonly headers: Readonly<Record<string, never>>; };
  readonly streamOperationsEvents: { readonly response: OperationalLiveEvent; readonly body: never; readonly path: Readonly<Record<string, never>>; readonly query: Readonly<Record<string, never>>; readonly headers: { readonly "Last-Event-ID"?: string; }; };
  readonly listDeployments: { readonly response: DeploymentCollection; readonly body: never; readonly path: Readonly<Record<string, never>>; readonly query: Readonly<Record<string, never>>; readonly headers: Readonly<Record<string, never>>; };
  readonly streamEvents: { readonly response: SsePayload; readonly body: never; readonly path: Readonly<Record<string, never>>; readonly query: Readonly<Record<string, never>>; readonly headers: { readonly "Last-Event-ID"?: string; }; };
  readonly createRetentionAction: { readonly response: RetentionAction; readonly body: CreateRetentionActionRequest; readonly path: Readonly<Record<string, never>>; readonly query: Readonly<Record<string, never>>; readonly headers: { readonly "X-Observatory-CSRF": string; readonly "Idempotency-Key": string; readonly "X-Observatory-Confirmation": string; }; };
  readonly getRetentionAction: { readonly response: RetentionAction; readonly body: never; readonly path: { readonly "action_id": Uuid; }; readonly query: Readonly<Record<string, never>>; readonly headers: Readonly<Record<string, never>>; };
}

export type ObservatoryOperationId = keyof ObservatoryApiOperations;
export type ObservatoryOperationInput<K extends ObservatoryOperationId> = Readonly<{
  path: ObservatoryApiOperations[K]['path'];
  query: ObservatoryApiOperations[K]['query'];
  headers: ObservatoryApiOperations[K]['headers'];
  body: ObservatoryApiOperations[K]['body'];
}>;
export type ObservatoryOperationResponse<K extends ObservatoryOperationId> = ObservatoryApiOperations[K]['response'];

export interface ObservatoryOperationDescriptor {
  readonly method: string;
  readonly pathTemplate: string;
  readonly requestMode: 'none' | 'json';
  readonly responseMode: 'none' | 'json' | 'event-stream';
}

export const OBSERVATORY_API_OPERATIONS = {
  startOwnerLogin: { method: 'GET', pathTemplate: '/api/v1/auth/login', requestMode: 'none', responseMode: 'none' },
  getOwnerSession: { method: 'GET', pathTemplate: '/api/v1/auth/me', requestMode: 'none', responseMode: 'json' },
  logoutOwnerSession: { method: 'POST', pathTemplate: '/api/v1/auth/logout', requestMode: 'none', responseMode: 'none' },
  getOverview: { method: 'GET', pathTemplate: '/api/v1/overview', requestMode: 'none', responseMode: 'json' },
  listHealthHistory: { method: 'GET', pathTemplate: '/api/v1/health/history', requestMode: 'none', responseMode: 'json' },
  listRuns: { method: 'GET', pathTemplate: '/api/v1/runs', requestMode: 'none', responseMode: 'json' },
  getRun: { method: 'GET', pathTemplate: '/api/v1/runs/{run_id}', requestMode: 'none', responseMode: 'json' },
  getTrace: { method: 'GET', pathTemplate: '/api/v1/traces/{trace_id}', requestMode: 'none', responseMode: 'json' },
  listComponents: { method: 'GET', pathTemplate: '/api/v1/components', requestMode: 'none', responseMode: 'json' },
  getComponentDefinition: { method: 'GET', pathTemplate: '/api/v1/components/{component_id}/definitions/{component_version}', requestMode: 'none', responseMode: 'json' },
  getComponentDependencies: { method: 'GET', pathTemplate: '/api/v1/components/{component_id}/definitions/{component_version}/dependencies', requestMode: 'none', responseMode: 'json' },
  getComponentHealth: { method: 'GET', pathTemplate: '/api/v1/components/{component_id}/health', requestMode: 'none', responseMode: 'json' },
  listAnalyticsMetrics: { method: 'GET', pathTemplate: '/api/v1/analytics/metrics', requestMode: 'none', responseMode: 'json' },
  queryOperationalAnalytics: { method: 'GET', pathTemplate: '/api/v1/analytics/query', requestMode: 'none', responseMode: 'json' },
  compareRuns: { method: 'POST', pathTemplate: '/api/v1/run-comparisons/query', requestMode: 'json', responseMode: 'json' },
  listEvaluationDatasets: { method: 'GET', pathTemplate: '/api/v1/evaluation/datasets', requestMode: 'none', responseMode: 'json' },
  createEvaluationDataset: { method: 'POST', pathTemplate: '/api/v1/evaluation/datasets', requestMode: 'json', responseMode: 'json' },
  getEvaluationDataset: { method: 'GET', pathTemplate: '/api/v1/evaluation/datasets/{dataset_id}/versions/{resource_version}', requestMode: 'none', responseMode: 'json' },
  listEvaluationCases: { method: 'GET', pathTemplate: '/api/v1/evaluation/datasets/{dataset_id}/versions/{resource_version}/cases', requestMode: 'none', responseMode: 'json' },
  listEvaluationSuites: { method: 'GET', pathTemplate: '/api/v1/evaluation/suites', requestMode: 'none', responseMode: 'json' },
  createEvaluationSuite: { method: 'POST', pathTemplate: '/api/v1/evaluation/suites', requestMode: 'json', responseMode: 'json' },
  getEvaluationSuite: { method: 'GET', pathTemplate: '/api/v1/evaluation/suites/{suite_id}/versions/{resource_version}', requestMode: 'none', responseMode: 'json' },
  listEvaluationEvaluators: { method: 'GET', pathTemplate: '/api/v1/evaluation/evaluators', requestMode: 'none', responseMode: 'json' },
  getEvaluationEvaluator: { method: 'GET', pathTemplate: '/api/v1/evaluation/evaluators/{evaluator_id}/versions/{resource_version}', requestMode: 'none', responseMode: 'json' },
  listEvaluationJobs: { method: 'GET', pathTemplate: '/api/v1/evaluation/jobs', requestMode: 'none', responseMode: 'json' },
  submitEvaluationJob: { method: 'POST', pathTemplate: '/api/v1/evaluation/jobs', requestMode: 'json', responseMode: 'json' },
  getEvaluationJob: { method: 'GET', pathTemplate: '/api/v1/evaluation/jobs/{job_id}', requestMode: 'none', responseMode: 'json' },
  listEvaluationAttempts: { method: 'GET', pathTemplate: '/api/v1/evaluation/jobs/{job_id}/attempts', requestMode: 'none', responseMode: 'json' },
  cancelEvaluationJob: { method: 'POST', pathTemplate: '/api/v1/evaluation/jobs/{job_id}/cancel', requestMode: 'json', responseMode: 'json' },
  listEvaluationResults: { method: 'GET', pathTemplate: '/api/v1/evaluation/results', requestMode: 'none', responseMode: 'json' },
  getEvaluationResult: { method: 'GET', pathTemplate: '/api/v1/evaluation/results/{result_id}', requestMode: 'none', responseMode: 'json' },
  listEvaluationScores: { method: 'GET', pathTemplate: '/api/v1/evaluation/results/{result_id}/scores', requestMode: 'none', responseMode: 'json' },
  listEvaluationArtifacts: { method: 'GET', pathTemplate: '/api/v1/evaluation/artifacts', requestMode: 'none', responseMode: 'json' },
  getEvaluationArtifact: { method: 'GET', pathTemplate: '/api/v1/evaluation/artifacts/{artifact_id}', requestMode: 'none', responseMode: 'json' },
  listEvaluationReviews: { method: 'GET', pathTemplate: '/api/v1/evaluation/reviews', requestMode: 'none', responseMode: 'json' },
  createEvaluationReview: { method: 'POST', pathTemplate: '/api/v1/evaluation/reviews', requestMode: 'json', responseMode: 'json' },
  listEvaluationCalibrations: { method: 'GET', pathTemplate: '/api/v1/evaluation/calibrations', requestMode: 'none', responseMode: 'json' },
  listEvaluationQualifications: { method: 'GET', pathTemplate: '/api/v1/evaluation/qualifications', requestMode: 'none', responseMode: 'json' },
  qualifyEvaluationJudge: { method: 'POST', pathTemplate: '/api/v1/evaluation/qualifications', requestMode: 'json', responseMode: 'json' },
  revokeEvaluationQualification: { method: 'POST', pathTemplate: '/api/v1/evaluation/qualifications/{qualification_id}/revoke', requestMode: 'json', responseMode: 'json' },
  listExperiments: { method: 'GET', pathTemplate: '/api/v1/evaluation/experiments', requestMode: 'none', responseMode: 'json' },
  createExperiment: { method: 'POST', pathTemplate: '/api/v1/evaluation/experiments', requestMode: 'json', responseMode: 'json' },
  getExperiment: { method: 'GET', pathTemplate: '/api/v1/evaluation/experiments/{experiment_id}', requestMode: 'none', responseMode: 'json' },
  listBaselines: { method: 'GET', pathTemplate: '/api/v1/evaluation/baselines', requestMode: 'none', responseMode: 'json' },
  createBaselineCandidate: { method: 'POST', pathTemplate: '/api/v1/evaluation/baselines', requestMode: 'json', responseMode: 'json' },
  getBaseline: { method: 'GET', pathTemplate: '/api/v1/evaluation/baselines/{baseline_id}', requestMode: 'none', responseMode: 'json' },
  promoteBaseline: { method: 'POST', pathTemplate: '/api/v1/evaluation/baselines/{baseline_id}/promotion', requestMode: 'json', responseMode: 'json' },
  listRegressions: { method: 'GET', pathTemplate: '/api/v1/evaluation/regressions', requestMode: 'none', responseMode: 'json' },
  createRegressionComparison: { method: 'POST', pathTemplate: '/api/v1/evaluation/regressions', requestMode: 'json', responseMode: 'json' },
  getRegression: { method: 'GET', pathTemplate: '/api/v1/evaluation/regressions/{regression_id}', requestMode: 'none', responseMode: 'json' },
  getRegressionAttribution: { method: 'GET', pathTemplate: '/api/v1/evaluation/regressions/{regression_id}/attribution', requestMode: 'none', responseMode: 'json' },
  listEvaluationHistory: { method: 'GET', pathTemplate: '/api/v1/evaluation/history', requestMode: 'none', responseMode: 'json' },
  createEvaluationDeletionAction: { method: 'POST', pathTemplate: '/api/v1/evaluation/deletion-actions', requestMode: 'json', responseMode: 'json' },
  getEvaluationDeletionAction: { method: 'GET', pathTemplate: '/api/v1/evaluation/deletion-actions/{action_id}', requestMode: 'none', responseMode: 'json' },
  streamEvaluationEvents: { method: 'GET', pathTemplate: '/api/v1/evaluation/events', requestMode: 'none', responseMode: 'event-stream' },
  listDashboardPresets: { method: 'GET', pathTemplate: '/api/v1/dashboard-presets', requestMode: 'none', responseMode: 'json' },
  listDashboards: { method: 'GET', pathTemplate: '/api/v1/dashboards', requestMode: 'none', responseMode: 'json' },
  createDashboard: { method: 'POST', pathTemplate: '/api/v1/dashboards', requestMode: 'json', responseMode: 'json' },
  getDashboard: { method: 'GET', pathTemplate: '/api/v1/dashboards/{dashboard_id}/versions/{resource_version}', requestMode: 'none', responseMode: 'json' },
  deleteDashboard: { method: 'POST', pathTemplate: '/api/v1/dashboards/{dashboard_id}/versions/{resource_version}/delete', requestMode: 'json', responseMode: 'none' },
  listSavedQueries: { method: 'GET', pathTemplate: '/api/v1/saved-queries', requestMode: 'none', responseMode: 'json' },
  createSavedQuery: { method: 'POST', pathTemplate: '/api/v1/saved-queries', requestMode: 'json', responseMode: 'json' },
  getSavedQuery: { method: 'GET', pathTemplate: '/api/v1/saved-queries/{query_id}/versions/{resource_version}', requestMode: 'none', responseMode: 'json' },
  executeSavedQuery: { method: 'GET', pathTemplate: '/api/v1/saved-queries/{query_id}/versions/{resource_version}/execute', requestMode: 'none', responseMode: 'json' },
  deleteSavedQuery: { method: 'POST', pathTemplate: '/api/v1/saved-queries/{query_id}/versions/{resource_version}/delete', requestMode: 'json', responseMode: 'none' },
  listAlertRules: { method: 'GET', pathTemplate: '/api/v1/alert-rules', requestMode: 'none', responseMode: 'json' },
  createAlertRule: { method: 'POST', pathTemplate: '/api/v1/alert-rules', requestMode: 'json', responseMode: 'json' },
  getAlertRule: { method: 'GET', pathTemplate: '/api/v1/alert-rules/{rule_id}/versions/{resource_version}', requestMode: 'none', responseMode: 'json' },
  changeAlertRuleState: { method: 'POST', pathTemplate: '/api/v1/alert-rules/{rule_id}/versions/{resource_version}/state', requestMode: 'json', responseMode: 'json' },
  listAlertInstances: { method: 'GET', pathTemplate: '/api/v1/alert-instances', requestMode: 'none', responseMode: 'json' },
  getAlertInstance: { method: 'GET', pathTemplate: '/api/v1/alert-instances/{instance_id}', requestMode: 'none', responseMode: 'json' },
  acknowledgeAlertInstance: { method: 'POST', pathTemplate: '/api/v1/alert-instances/{instance_id}/acknowledgements', requestMode: 'json', responseMode: 'json' },
  listAlertSuppressions: { method: 'GET', pathTemplate: '/api/v1/alert-suppressions', requestMode: 'none', responseMode: 'json' },
  createAlertSuppression: { method: 'POST', pathTemplate: '/api/v1/alert-suppressions', requestMode: 'json', responseMode: 'json' },
  clearAlertSuppression: { method: 'POST', pathTemplate: '/api/v1/alert-suppressions/{suppression_id}/clear', requestMode: 'json', responseMode: 'json' },
  listIncidents: { method: 'GET', pathTemplate: '/api/v1/incidents', requestMode: 'none', responseMode: 'json' },
  createIncident: { method: 'POST', pathTemplate: '/api/v1/incidents', requestMode: 'json', responseMode: 'json' },
  getIncident: { method: 'GET', pathTemplate: '/api/v1/incidents/{incident_id}', requestMode: 'none', responseMode: 'json' },
  transitionIncident: { method: 'POST', pathTemplate: '/api/v1/incidents/{incident_id}/transitions', requestMode: 'json', responseMode: 'json' },
  listAnnotations: { method: 'GET', pathTemplate: '/api/v1/annotations', requestMode: 'none', responseMode: 'json' },
  createAnnotation: { method: 'POST', pathTemplate: '/api/v1/annotations', requestMode: 'json', responseMode: 'json' },
  getAnnotation: { method: 'GET', pathTemplate: '/api/v1/annotations/{annotation_id}/versions/{resource_version}', requestMode: 'none', responseMode: 'json' },
  deleteAnnotation: { method: 'POST', pathTemplate: '/api/v1/annotations/{annotation_id}/versions/{resource_version}/delete', requestMode: 'json', responseMode: 'none' },
  listOperationsHistory: { method: 'GET', pathTemplate: '/api/v1/operations/history', requestMode: 'none', responseMode: 'json' },
  streamOperationsEvents: { method: 'GET', pathTemplate: '/api/v1/operations/events', requestMode: 'none', responseMode: 'event-stream' },
  listDeployments: { method: 'GET', pathTemplate: '/api/v1/deployments', requestMode: 'none', responseMode: 'json' },
  streamEvents: { method: 'GET', pathTemplate: '/api/v1/events', requestMode: 'none', responseMode: 'event-stream' },
  createRetentionAction: { method: 'POST', pathTemplate: '/api/v1/retention/actions', requestMode: 'json', responseMode: 'json' },
  getRetentionAction: { method: 'GET', pathTemplate: '/api/v1/retention/actions/{action_id}', requestMode: 'none', responseMode: 'json' },
} as const satisfies Readonly<Record<ObservatoryOperationId, ObservatoryOperationDescriptor>>;
