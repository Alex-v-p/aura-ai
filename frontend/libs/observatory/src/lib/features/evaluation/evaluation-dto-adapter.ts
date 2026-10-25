import type {
  EvaluationArtifact, EvaluationArtifactPage, EvaluationAttempt, EvaluationAttemptPage,
  EvaluationCalibration, EvaluationCalibrationPage, EvaluationCase, EvaluationCasePage,
  EvaluationDataset, EvaluationDatasetPage, EvaluationDeletionAction, EvaluationEvaluator,
  EvaluationEvaluatorPage, EvaluationHistoryEntry, EvaluationHistoryPage, EvaluationJob,
  EvaluationJobPage, EvaluationLiveEvent, EvaluationQualification, EvaluationQualificationPage,
  EvaluationResult, EvaluationResultPage, EvaluationReview, EvaluationReviewPage,
  EvaluationScore, EvaluationScorePage, EvaluationSuite, EvaluationSuitePage,
  EvaluationProvenance, EvaluationResourceReference, EvaluationEvaluatorReference,
  EvaluationComponentBinding, EvaluationExecutionProvenance, EvaluationReproducibility,
  EvaluationRunnerExecutionProvenance, EvaluationExecutionLimits, EvaluationCaptureAuthorization, EvaluationRunnerLimits,
  EvaluationModelBinding, EvaluationInputArtifactReference, EvaluationExperimentVariant,
} from '@aura/observatory-api-client';

type KnownFields = Partial<EvaluationDataset> & Partial<EvaluationCase> & Partial<EvaluationSuite> & Partial<EvaluationEvaluator> & Partial<EvaluationJob> & Partial<EvaluationAttempt> & Partial<EvaluationResult> & Partial<EvaluationScore> & Partial<EvaluationArtifact> & Partial<EvaluationReview> & Partial<EvaluationCalibration> & Partial<EvaluationQualification> & Partial<EvaluationDeletionAction> & Partial<EvaluationHistoryEntry> & Partial<EvaluationLiveEvent> & Partial<EvaluationDatasetPage> & Partial<EvaluationCasePage> & Partial<EvaluationSuitePage> & Partial<EvaluationEvaluatorPage> & Partial<EvaluationJobPage> & Partial<EvaluationAttemptPage> & Partial<EvaluationResultPage> & Partial<EvaluationScorePage> & Partial<EvaluationArtifactPage> & Partial<EvaluationReviewPage> & Partial<EvaluationCalibrationPage> & Partial<EvaluationQualificationPage> & Partial<EvaluationHistoryPage> & Partial<EvaluationProvenance> & Partial<EvaluationResourceReference> & Partial<EvaluationEvaluatorReference> & Partial<EvaluationComponentBinding> & Partial<EvaluationExecutionProvenance> & Partial<EvaluationRunnerExecutionProvenance> & Partial<EvaluationReproducibility> & Partial<EvaluationExecutionLimits> & Partial<EvaluationCaptureAuthorization> & Partial<EvaluationRunnerLimits> & Partial<EvaluationModelBinding> & Partial<EvaluationInputArtifactReference>;
type UnknownRecord = Record<string, unknown> & KnownFields;
const record = (value: unknown): value is UnknownRecord => typeof value === 'object' && value !== null && !Array.isArray(value);
const closed = (value: UnknownRecord, keys: readonly string[]): boolean => {
  const allowed = new Set(keys);
  const present = Reflect.ownKeys(value);
  return present.length === allowed.size && present.every((key) => typeof key === 'string' && allowed.has(key));
};
// Observatory's published UUID contract requires canonical lowercase
// 8-4-4-4-12 hexadecimal syntax, but deliberately does not require RFC
// version or variant marker bits. Deterministic provider IDs are therefore
// valid even when those marker nibbles come directly from a digest.
const uuid = (value: unknown): value is string => typeof value === 'string' && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(value);
const boundedIdentifier = (value: unknown): value is string => typeof value === 'string' && /^[A-Za-z0-9][A-Za-z0-9._:+-]{0,255}$/.test(value);
const utc = (value: unknown): value is string => typeof value === 'string' && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?Z$/.test(value) && Number.isFinite(Date.parse(value));
const version = (value: unknown): value is string => typeof value === 'string' && /^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$/.test(value);
const digest = (value: unknown): value is string => typeof value === 'string' && /^[0-9a-f]{64}$/.test(value);
const finiteNumber = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value);
const integer = (value: unknown): value is number => finiteNumber(value) && Number.isSafeInteger(value);
const oneOf = <T extends string>(values: readonly T[], value: unknown): value is T => typeof value === 'string' && values.some((entry) => entry === value);
const capture = (value: unknown): boolean => oneOf(['metadata_only', 'hashed', 'redacted', 'sampled_content', 'full_content'] as const, value);
const scope = (value: unknown): boolean => oneOf(['public_project', 'private', 'sensitive', 'environment'] as const, value);
const sourceKind = (value: unknown): boolean => oneOf(['synthetic', 'owner_supplied', 'captured'] as const, value);
const state = (value: unknown): boolean => oneOf(['active', 'superseded', 'deleted'] as const, value);

// All nested response objects must use this metadata vocabulary. Unknown/private fields
// therefore fail closed before any feature template or command code sees them.
const metadataKeys = new Set([
  'items', 'nextCursor', 'snapshotAt', 'partial', 'schemaVersion', 'resourceType', 'datasetId', 'datasetVersion', 'sourceKind', 'scopeIds', 'captureClassification', 'caseCount', 'retentionMode', 'provenance', 'state', 'createdAt', 'caseId', 'caseVersion', 'dataset', 'artifactIds', 'suiteId', 'suiteVersion', 'evaluators', 'components', 'componentIds', 'componentId', 'componentVersion', 'codeRevision', 'configurationRevision', 'imageDigest', 'limits', 'variantBindings', 'variantId', 'manualThresholds', 'timeoutMs', 'caseBudget', 'evaluatorId', 'evaluatorVersion', 'kind', 'metric', 'rubricVersion', 'calibrationVersion', 'jobId', 'status', 'attemptCount', 'resultCount', 'artifactCount', 'deadlineAt', 'failureCode', 'updatedAt', 'attemptId', 'attemptNumber', 'workerComponentId', 'startedAt', 'finishedAt', 'nextAttemptAt', 'resultId', 'partialEvidence', 'reproducible', 'completedAt', 'component', 'scoreId', 'value', 'outcome', 'evidenceStatus', 'uncertainty', 'scoreDisposition', 'method', 'lower', 'upper', 'artifactId', 'artifactVersion', 'purpose', 'mediaClassification', 'mediaType', 'digest', 'byteCount', 'authorizationRecordId', 'deletionStatus', 'reviewId', 'reviewVersion', 'calibration', 'reviewScore', 'reviewOutcome', 'reviewLabel', 'disagreement', 'commentArtifactId', 'calibrationId', 'calibrationVersion', 'judgeDefinition', 'sampleCount', 'agreementRate', 'meanAbsoluteError', 'disagreementRate', 'eligibility', 'advisory', 'qualificationId', 'qualificationVersion', 'judgeDefinitionId', 'judgeDefinitionVersion', 'actionId', 'scope', 'resourceId', 'resourceVersion', 'cutoff', 'deletedArtifactCount', 'deletedMetadataCount', 'historyId', 'change', 'occurredAt', 'eventId', 'eventType', 'capturePolicy', 'createdByComponent', 'createdByVersion', 'sourceDigest', 'id', 'version', 'evaluator', 'model', 'prompt', 'rubric', 'action', 'actionMode', 'calibrationResultId', 'calibrationEligibility', 'qualifiedAt', 'executionKind', 'requestVersion', 'targetComponent', 'captureAuthorization', 'caseIds', 'inputArtifactReferences', 'modelId', 'modelVersion', 'modelDigest', 'deploymentRevision', 'reproducibility', 'seed', 'clockMode', 'timeZone', 'locale', 'fixtureRevision', 'promptRevision', 'policyRevision', 'algorithm', 'integrity', 'artifactReferenceId', 'authorizationPurpose', 'authorizationContext', 'authorizedAt', 'maxQueueTimeMs', 'maxModelCalls', 'maxArtifactOperations', 'maxConcurrency', 'maxAttempts', 'maxCases', 'maxInputBytes', 'maxOutputBytes', 'maxTokens',
]);
metadataKeys.add('suite');
metadataKeys.add('scoreCount');
function metadataTree(value: unknown, depth = 0): boolean {
  if (depth > 12) return false;
  if (Array.isArray(value)) return value.length <= 1000 && value.every((entry) => metadataTree(entry, depth + 1));
  if (!record(value)) return true;
  const keys = Reflect.ownKeys(value);
  return keys.every((key) => typeof key === 'string' && metadataKeys.has(key)) && Object.values(value).every((entry) => metadataTree(entry, depth + 1));
}
function provenance(value: unknown): boolean {
  return record(value) && closed(value, ['createdByComponent', 'createdByVersion', 'sourceDigest']) && boundedIdentifier(value.createdByComponent) && version(value.createdByVersion) && digest(value.sourceDigest);
}
function reference(value: unknown): boolean {
  return record(value) && closed(value, ['resourceId', 'resourceVersion']) && uuid(value.resourceId) && version(value.resourceVersion);
}
function evaluatorReference(value: unknown): boolean {
  return record(value) && closed(value, ['evaluatorId', 'evaluatorVersion', 'kind']) && uuid(value.evaluatorId) && version(value.evaluatorVersion) && oneOf(['deterministic', 'local_judge', 'human_review'] as const, value.kind);
}
function componentBinding(value: unknown): boolean {
  return record(value) && closed(value, ['componentId', 'componentVersion', 'codeRevision', 'imageDigest', 'configurationRevision']) && boundedIdentifier(value.componentId) && version(value.componentVersion) && boundedIdentifier(value.codeRevision) && digest(value.imageDigest) && boundedIdentifier(value.configurationRevision);
}
function executionProvenance(value: unknown): boolean {
  return record(value) && closed(value, ['dataset', 'suite', 'evaluators', 'components', 'model', 'deploymentRevision', 'reproducibility']) && reference(value.dataset) && reference(value.suite) && Array.isArray(value.evaluators) && value.evaluators.every(evaluatorReference) && Array.isArray(value.components) && value.components.every(componentBinding) && record(value.model) && closed(value.model, ['modelId', 'modelVersion', 'modelDigest']) && boundedIdentifier(value.model.modelId) && version(value.model.modelVersion) && digest(value.model.modelDigest) && boundedIdentifier(value.deploymentRevision) && record(value.reproducibility) && closed(value.reproducibility, ['seed', 'clockMode', 'timeZone', 'locale', 'fixtureRevision', 'promptRevision', 'policyRevision']) && integer(value.reproducibility.seed) && oneOf(['fixed', 'recorded'] as const, value.reproducibility.clockMode) && value.reproducibility.timeZone === 'UTC' && boundedIdentifier(value.reproducibility.locale) && boundedIdentifier(value.reproducibility.fixtureRevision) && boundedIdentifier(value.reproducibility.promptRevision) && boundedIdentifier(value.reproducibility.policyRevision);
}
function definitionReference(value: unknown): boolean {
  return record(value) && closed(value, ['id', 'version']) && uuid(value['id']) && version(value['version']);
}
function runnerExecutionProvenance(value: unknown): value is EvaluationRunnerExecutionProvenance {
  return record(value) && closed(value, ['dataset', 'suite', 'evaluators', 'components', 'model', 'deploymentRevision', 'reproducibility']) && definitionReference(value.dataset) && definitionReference(value.suite) && Array.isArray(value.evaluators) && value.evaluators.every(evaluatorReference) && Array.isArray(value.components) && value.components.every(componentBinding) && modelBinding(value.model) && boundedIdentifier(value.deploymentRevision) && record(value.reproducibility) && closed(value.reproducibility, ['seed', 'clockMode', 'timeZone', 'locale', 'fixtureRevision', 'promptRevision', 'policyRevision']) && integer(value.reproducibility.seed) && oneOf(['fixed', 'recorded'] as const, value.reproducibility.clockMode) && value.reproducibility.timeZone === 'UTC' && boundedIdentifier(value.reproducibility.locale) && boundedIdentifier(value.reproducibility.fixtureRevision) && boundedIdentifier(value.reproducibility.promptRevision) && boundedIdentifier(value.reproducibility.policyRevision);
}
function resource(value: unknown, resourceType: string, keys: readonly string[], id: 'uuid' | 'identifier' = 'uuid', idKey = `${resourceType}Id`): value is UnknownRecord {
  if (!record(value) || !closed(value, keys) || !metadataTree(value)) return false;
  return value.schemaVersion === 1 && value.resourceType === resourceType && (id === 'uuid' ? uuid(value[idKey]) : boundedIdentifier(value[idKey])) && utc(value.createdAt);
}

const datasetKeys = ['schemaVersion', 'resourceType', 'datasetId', 'datasetVersion', 'sourceKind', 'scopeIds', 'captureClassification', 'caseCount', 'retentionMode', 'provenance', 'state', 'createdAt'] as const;
export function isEvaluationDataset(value: unknown): value is EvaluationDataset { return resource(value, 'dataset', datasetKeys) && provenance(value.provenance) && version(value.datasetVersion) && sourceKind(value.sourceKind) && Array.isArray(value.scopeIds) && value.scopeIds.length <= 4 && value.scopeIds.every(scope) && capture(value.captureClassification) && integer(value.caseCount) && value.caseCount >= 0 && value.retentionMode === 'manual_only' && state(value.state); }
const caseKeys = ['schemaVersion', 'resourceType', 'caseId', 'caseVersion', 'dataset', 'sourceKind', 'scopeIds', 'captureClassification', 'artifactIds', 'retentionMode', 'provenance', 'createdAt'] as const;
export function isEvaluationCase(value: unknown): value is EvaluationCase { return resource(value, 'case', caseKeys) && provenance(value.provenance) && uuid(value.caseId) && version(value.caseVersion) && reference(value.dataset) && sourceKind(value.sourceKind) && Array.isArray(value.scopeIds) && value.scopeIds.every(scope) && capture(value.captureClassification) && Array.isArray(value.artifactIds) && value.artifactIds.length <= 32 && value.artifactIds.every(uuid) && value.retentionMode === 'manual_only'; }
function modelBinding(value: unknown): value is EvaluationModelBinding {
  return record(value) && closed(value, ['modelId', 'modelVersion', 'modelDigest']) && boundedIdentifier(value.modelId) && version(value.modelVersion) && digest(value.modelDigest);
}
function executionLimits(value: unknown): value is EvaluationExecutionLimits {
  return record(value) && closed(value, ['timeoutMs', 'maxConcurrency', 'maxAttempts', 'maxCases', 'maxInputBytes', 'maxOutputBytes', 'maxTokens']) && integer(value.timeoutMs) && value.timeoutMs >= 100 && value.timeoutMs <= 86_400_000 && integer(value.maxConcurrency) && value.maxConcurrency >= 1 && value.maxConcurrency <= 64 && integer(value.maxAttempts) && value.maxAttempts >= 1 && value.maxAttempts <= 10 && integer(value.maxCases) && value.maxCases >= 1 && value.maxCases <= 100_000 && integer(value.maxInputBytes) && value.maxInputBytes >= 0 && value.maxInputBytes <= 1_073_741_824 && integer(value.maxOutputBytes) && value.maxOutputBytes >= 0 && value.maxOutputBytes <= 1_073_741_824 && integer(value.maxTokens) && value.maxTokens >= 0 && value.maxTokens <= 100_000_000;
}
function experimentVariant(value: unknown): value is EvaluationExperimentVariant {
  return record(value) && closed(value, ['variantId', 'components', 'model', 'configurationRevision', 'promptRevision', 'policyRevision', 'deploymentRevision']) && boundedIdentifier(value['variantId']) && Array.isArray(value['components']) && value['components'].length <= 32 && value['components'].every(componentBinding) && modelBinding(value['model']) && boundedIdentifier(value['configurationRevision']) && boundedIdentifier(value['promptRevision']) && boundedIdentifier(value['policyRevision']) && boundedIdentifier(value['deploymentRevision']);
}
const suiteRequiredKeys = ['schemaVersion', 'resourceType', 'suiteId', 'suiteVersion', 'dataset', 'evaluators', 'componentIds', 'caseBudget', 'timeoutMs', 'retentionMode', 'provenance', 'state', 'createdAt'] as const;
const suiteOptionalKeys = ['components', 'variantBindings', 'limits'] as const;
const closedWithOptional = (value: UnknownRecord, required: readonly string[], optional: readonly string[]): boolean => {
  const allowed = new Set([...required, ...optional]);
  const present = Reflect.ownKeys(value);
  return required.every((key) => Object.prototype.hasOwnProperty.call(value, key)) && present.every((key) => typeof key === 'string' && allowed.has(key));
};
function suiteResource(value: unknown): value is UnknownRecord {
  return record(value) && closedWithOptional(value, suiteRequiredKeys, suiteOptionalKeys) && metadataTree(value) && value.schemaVersion === 1 && value.resourceType === 'suite' && uuid(value.suiteId) && utc(value.createdAt);
}
export function isEvaluationSuite(value: unknown): value is EvaluationSuite { return suiteResource(value) && provenance(value.provenance) && uuid(value.suiteId) && version(value.suiteVersion) && reference(value.dataset) && Array.isArray(value.evaluators) && value.evaluators.length <= 32 && value.evaluators.every(evaluatorReference) && Array.isArray(value.componentIds) && value.componentIds.length <= 32 && value.componentIds.every(boundedIdentifier) && (!('components' in value) || (Array.isArray(value.components) && value.components.length <= 32 && value.components.every(componentBinding))) && (!('variantBindings' in value) || (Array.isArray(value.variantBindings) && value.variantBindings.length <= 16 && value.variantBindings.every(experimentVariant))) && (!('limits' in value) || executionLimits(value.limits)) && integer(value.caseBudget) && value.caseBudget >= 1 && value.caseBudget <= 100_000 && integer(value.timeoutMs) && value.timeoutMs >= 100 && value.timeoutMs <= 86_400_000 && value.retentionMode === 'manual_only' && state(value.state); }
const evaluatorRequiredKeys = ['schemaVersion', 'resourceType', 'evaluatorId', 'evaluatorVersion', 'kind', 'metric', 'componentIds', 'retentionMode', 'provenance', 'createdAt'] as const;
const evaluatorOptionalKeys = ['rubricVersion', 'calibrationVersion'] as const;
function evaluatorResource(value: unknown): value is UnknownRecord {
  return record(value) && closedWithOptional(value, evaluatorRequiredKeys, evaluatorOptionalKeys) && metadataTree(value) && value.schemaVersion === 1 && value.resourceType === 'evaluator' && uuid(value.evaluatorId) && utc(value.createdAt);
}
export function isEvaluationEvaluator(value: unknown): value is EvaluationEvaluator { return evaluatorResource(value) && provenance(value.provenance) && uuid(value.evaluatorId) && version(value.evaluatorVersion) && oneOf(['deterministic', 'local_judge', 'human_review'] as const, value.kind) && boundedIdentifier(value.metric) && Array.isArray(value.componentIds) && value.componentIds.length <= 32 && value.componentIds.every(boundedIdentifier) && (!Object.prototype.hasOwnProperty.call(value, 'rubricVersion') || version(value.rubricVersion)) && (!Object.prototype.hasOwnProperty.call(value, 'calibrationVersion') || version(value.calibrationVersion)) && value.retentionMode === 'manual_only'; }
const jobKeys = ['schemaVersion', 'resourceType', 'jobId', 'suite', 'status', 'captureClassification', 'attemptCount', 'resultCount', 'artifactCount', 'deadlineAt', 'failureCode', 'provenance', 'createdAt', 'updatedAt'] as const;
export function isEvaluationJob(value: unknown): value is EvaluationJob { return resource(value, 'job', jobKeys) && runnerExecutionProvenance(value.provenance) && uuid(value.jobId) && reference(value.suite) && oneOf(['queued', 'running', 'succeeded', 'failed', 'cancelled', 'deadline'] as const, value.status) && capture(value.captureClassification) && integer(value.attemptCount) && integer(value.resultCount) && integer(value.artifactCount) && utc(value.deadlineAt) && (value.failureCode === null || boundedIdentifier(value.failureCode)) && utc(value.updatedAt); }
const attemptBaseKeys = ['attemptId', 'jobId', 'attemptNumber', 'status', 'startedAt', 'finishedAt', 'nextAttemptAt', 'failureCode'] as const;
const attemptWorkerKeys = [...attemptBaseKeys, 'workerComponentId'] as const;
export function isEvaluationAttempt(value: unknown): value is EvaluationAttempt {
  if (!record(value) || !metadataTree(value) || (!closed(value, attemptBaseKeys) && !closed(value, attemptWorkerKeys)) || !uuid(value.attemptId) || !uuid(value.jobId) || !integer(value.attemptNumber) || value.attemptNumber < 1 || !oneOf(['queued', 'running', 'succeeded', 'failed', 'cancelled', 'deadline'] as const, value.status) || (value.startedAt !== null && !utc(value.startedAt)) || (value.finishedAt !== null && !utc(value.finishedAt)) || (value.nextAttemptAt !== null && !utc(value.nextAttemptAt)) || (value.failureCode !== null && !boundedIdentifier(value.failureCode))) return false;
  const status = value.status as unknown;
  const workerComponentId = value.workerComponentId as unknown;
  if (status === 'queued') return workerComponentId === undefined || workerComponentId === null;
  return boundedIdentifier(workerComponentId);
}
const resultKeys = ['schemaVersion', 'resourceType', 'resultId', 'jobId', 'attemptId', 'status', 'captureClassification', 'scoreCount', 'artifactCount', 'partialEvidence', 'reproducible', 'provenance', 'createdAt', 'completedAt'] as const;
export function isEvaluationResult(value: unknown): value is EvaluationResult { return resource(value, 'result', resultKeys) && uuid(value.resultId) && uuid(value.jobId) && uuid(value.attemptId) && oneOf(['completed', 'cancelled', 'failed'] as const, value.status) && capture(value.captureClassification) && integer(value.scoreCount) && integer(value.artifactCount) && typeof value.partialEvidence === 'boolean' && typeof value.reproducible === 'boolean' && executionProvenance(value.provenance) && (value.completedAt === null || utc(value.completedAt)); }
const scoreKeys = ['schemaVersion', 'resourceType', 'scoreId', 'resultId', 'caseId', 'evaluator', 'component', 'metric', 'value', 'outcome', 'evidenceStatus', 'uncertainty', 'scoreDisposition', 'provenance', 'createdAt'] as const;
export function isEvaluationScore(value: unknown): value is EvaluationScore { return resource(value, 'score', scoreKeys) && uuid(value.scoreId) && uuid(value.resultId) && uuid(value.caseId) && evaluatorReference(value.evaluator) && componentBinding(value.component) && boundedIdentifier(value.metric) && (value.value === null || finiteNumber(value.value)) && (value.outcome === null || oneOf(['pass', 'fail', 'inconclusive'] as const, value.outcome)) && oneOf(['observed', 'not_observed', 'not_applicable'] as const, value.evidenceStatus) && record(value.uncertainty) && closed(value.uncertainty, ['method', 'lower', 'upper']) && oneOf(['none', 'calibrated_interval', 'human_disagreement'] as const, value.uncertainty.method) && finiteNumber(value.uncertainty.lower) && finiteNumber(value.uncertainty.upper) && oneOf(['deterministic', 'advisory_only', 'calibrated_enabled', 'human_review'] as const, value.scoreDisposition) && executionProvenance(value.provenance); }
const artifactKeys = ['schemaVersion', 'resourceType', 'artifactId', 'artifactVersion', 'jobId', 'resultId', 'purpose', 'mediaClassification', 'mediaType', 'digest', 'byteCount', 'captureClassification', 'authorizationRecordId', 'retentionMode', 'deletionStatus', 'provenance', 'createdAt'] as const;
export function isEvaluationArtifact(value: unknown): value is EvaluationArtifact { return resource(value, 'artifact', artifactKeys) && uuid(value.artifactId) && version(value.artifactVersion) && (value.jobId === null || uuid(value.jobId)) && (value.resultId === null || uuid(value.resultId)) && oneOf(['dataset_case', 'input', 'expected_output', 'actual_output', 'evidence', 'judge_prompt', 'judge_rubric', 'review_comment'] as const, value.purpose) && oneOf(['evaluation_input', 'evaluation_output', 'evaluation_context', 'evaluation_evidence', 'evaluation_report'] as const, value.mediaClassification) && typeof value.mediaType === 'string' && digest(value.digest) && integer(value.byteCount) && value.byteCount >= 0 && capture(value.captureClassification) && uuid(value.authorizationRecordId) && value.retentionMode === 'manual_only' && oneOf(['retained', 'deletion_queued', 'deleting', 'deleted', 'deletion_failed'] as const, value.deletionStatus) && executionProvenance(value.provenance); }
const reviewKeys = ['schemaVersion', 'resourceType', 'reviewId', 'reviewVersion', 'calibration', 'caseId', 'resultId', 'scoreId', 'reviewScore', 'reviewOutcome', 'reviewLabel', 'disagreement', 'authorizationRecordId', 'commentArtifactId', 'provenance', 'createdAt'] as const;
export function isEvaluationReview(value: unknown): value is EvaluationReview { return resource(value, 'human_review', reviewKeys, 'uuid', 'reviewId') && provenance(value.provenance) && uuid(value.reviewId) && version(value.reviewVersion) && reference(value.calibration) && uuid(value.caseId) && uuid(value.resultId) && uuid(value.scoreId) && finiteNumber(value.reviewScore) && value.reviewScore >= 0 && value.reviewScore <= 1 && oneOf(['pass', 'fail', 'inconclusive'] as const, value.reviewOutcome) && oneOf(['accepted', 'rejected', 'needs_review'] as const, value.reviewLabel) && oneOf(['none', 'within_tolerance', 'score_disagreement', 'outcome_disagreement', 'insufficient_evidence'] as const, value.disagreement) && uuid(value.authorizationRecordId) && (value.commentArtifactId === null || uuid(value.commentArtifactId)); }
const calibrationKeys = ['calibrationId', 'calibrationVersion', 'dataset', 'suite', 'evaluator', 'judgeDefinition', 'sampleCount', 'agreementRate', 'meanAbsoluteError', 'disagreementRate', 'eligibility', 'advisory', 'provenance', 'createdAt'] as const;
export function isEvaluationCalibration(value: unknown): value is EvaluationCalibration { return record(value) && closed(value, calibrationKeys) && metadataTree(value) && boundedIdentifier(value.calibrationId) && version(value.calibrationVersion) && reference(value.dataset) && reference(value.suite) && evaluatorReference(value.evaluator) && reference(value.judgeDefinition) && integer(value.sampleCount) && finiteNumber(value.agreementRate) && finiteNumber(value.meanAbsoluteError) && finiteNumber(value.disagreementRate) && oneOf(['eligible', 'ineligible'] as const, value.eligibility) && typeof value.advisory === 'boolean' && provenance(value.provenance) && utc(value.createdAt); }
const qualificationKeys = ['schemaVersion', 'resourceType', 'qualificationId', 'qualificationVersion', 'state', 'dataset', 'suite', 'evaluator', 'judgeDefinition', 'calibration', 'calibrationResultId', 'authorizationRecordId', 'scoreDisposition', 'provenance', 'createdAt'] as const;
export function isEvaluationQualification(value: unknown): value is EvaluationQualification { return resource(value, 'judge_qualification', qualificationKeys, 'uuid', 'qualificationId') && provenance(value.provenance) && uuid(value.qualificationId) && version(value.qualificationVersion) && oneOf(['qualified', 'revoked'] as const, value.state) && reference(value.dataset) && reference(value.suite) && evaluatorReference(value.evaluator) && reference(value.judgeDefinition) && reference(value.calibration) && uuid(value.calibrationResultId) && uuid(value.authorizationRecordId) && oneOf(['calibrated_enabled', 'advisory_only'] as const, value.scoreDisposition); }
const historyKeys = ['historyId', 'resourceType', 'resourceId', 'resourceVersion', 'change', 'state', 'occurredAt'] as const;
export function isEvaluationHistoryEntry(value: unknown): value is EvaluationHistoryEntry { return record(value) && closed(value, historyKeys) && metadataTree(value) && uuid(value.historyId) && boundedIdentifier(value.resourceType) && boundedIdentifier(value.resourceId) && (value.resourceVersion === null || version(value.resourceVersion)) && oneOf(['created', 'updated', 'state_changed', 'cancel_requested', 'qualified', 'revoked', 'deletion_requested', 'deleted'] as const, value.change) && boundedIdentifier(value.state) && utc(value.occurredAt); }
const deletionKeys = ['actionId', 'scope', 'resourceId', 'resourceVersion', 'cutoff', 'status', 'deletedArtifactCount', 'deletedMetadataCount', 'failureCode', 'createdAt', 'startedAt', 'finishedAt'] as const;
export function isEvaluationDeletionAction(value: unknown): value is EvaluationDeletionAction { return record(value) && closed(value, deletionKeys) && metadataTree(value) && uuid(value.actionId) && oneOf(['job', 'dataset', 'suite', 'cutoff', 'purge_all'] as const, value.scope) && (value.resourceId === null || boundedIdentifier(value.resourceId)) && (value.resourceVersion === null || version(value.resourceVersion)) && (value.cutoff === null || utc(value.cutoff)) && oneOf(['queued', 'running', 'succeeded', 'failed'] as const, value.status) && integer(value.deletedArtifactCount) && integer(value.deletedMetadataCount) && (value.failureCode === null || boundedIdentifier(value.failureCode)) && utc(value.createdAt) && (value.startedAt === null || utc(value.startedAt)) && (value.finishedAt === null || utc(value.finishedAt)); }

type PageShape<T> = { readonly items: ReadonlyArray<T>; readonly nextCursor: string | null; readonly snapshotAt: string; readonly partial: boolean };
function page<T>(value: unknown, keys: readonly string[], guard: (item: unknown) => item is T): PageShape<T> | null {
  if (!record(value) || !closed(value, keys) || !metadataTree(value) || !Array.isArray(value.items) || value.items.length > 200 || !value.items.every(guard) || (value.nextCursor !== null && !boundedIdentifier(value.nextCursor)) || !utc(value.snapshotAt) || typeof value.partial !== 'boolean') return null;
  return { items: value.items, nextCursor: value.nextCursor, snapshotAt: value.snapshotAt, partial: value.partial };
}
const pageKeys = ['items', 'nextCursor', 'snapshotAt', 'partial'] as const;
export const parseEvaluationDatasetPage = (value: unknown): EvaluationDatasetPage | null => page(value, pageKeys, isEvaluationDataset);
export const parseEvaluationCasePage = (value: unknown): EvaluationCasePage | null => page(value, pageKeys, isEvaluationCase);
export const parseEvaluationSuitePage = (value: unknown): EvaluationSuitePage | null => page(value, pageKeys, isEvaluationSuite);
export const parseEvaluationEvaluatorPage = (value: unknown): EvaluationEvaluatorPage | null => page(value, pageKeys, isEvaluationEvaluator);
export const parseEvaluationJobPage = (value: unknown): EvaluationJobPage | null => page(value, pageKeys, isEvaluationJob);
export const parseEvaluationAttemptPage = (value: unknown): EvaluationAttemptPage | null => page(value, pageKeys, isEvaluationAttempt);
export const parseEvaluationResultPage = (value: unknown): EvaluationResultPage | null => page(value, pageKeys, isEvaluationResult);
export const parseEvaluationScorePage = (value: unknown): EvaluationScorePage | null => page(value, pageKeys, isEvaluationScore);
export const parseEvaluationArtifactPage = (value: unknown): EvaluationArtifactPage | null => page(value, pageKeys, isEvaluationArtifact);
export const parseEvaluationReviewPage = (value: unknown): EvaluationReviewPage | null => page(value, pageKeys, isEvaluationReview);
export const parseEvaluationCalibrationPage = (value: unknown): EvaluationCalibrationPage | null => page(value, pageKeys, isEvaluationCalibration);
export const parseEvaluationQualificationPage = (value: unknown): EvaluationQualificationPage | null => page(value, pageKeys, isEvaluationQualification);
export const parseEvaluationHistoryPage = (value: unknown): EvaluationHistoryPage | null => page(value, pageKeys, isEvaluationHistoryEntry);
export const parseEvaluationDataset = (value: unknown): EvaluationDataset | null => isEvaluationDataset(value) ? value : null;
export const parseEvaluationCase = (value: unknown): EvaluationCase | null => isEvaluationCase(value) ? value : null;
export const parseEvaluationSuite = (value: unknown): EvaluationSuite | null => isEvaluationSuite(value) ? value : null;
export const parseEvaluationJob = (value: unknown): EvaluationJob | null => isEvaluationJob(value) ? value : null;
export const parseEvaluationResult = (value: unknown): EvaluationResult | null => isEvaluationResult(value) ? value : null;
export const parseEvaluationReview = (value: unknown): EvaluationReview | null => isEvaluationReview(value) ? value : null;
export const parseEvaluationQualification = (value: unknown): EvaluationQualification | null => isEvaluationQualification(value) ? value : null;
export const parseEvaluationDeletionAction = (value: unknown): EvaluationDeletionAction | null => isEvaluationDeletionAction(value) ? value : null;

const evaluationResourceTypes = ['evaluation_dataset', 'evaluation_case', 'evaluation_suite', 'evaluation_evaluator', 'evaluation_job', 'evaluation_attempt', 'evaluation_result', 'evaluation_artifact', 'evaluation_review', 'evaluation_calibration', 'evaluation_qualification', 'evaluation_deletion'] as const;
const changes = ['created', 'updated', 'state_changed', 'cancel_requested', 'qualified', 'revoked', 'deletion_requested', 'deleted'] as const;
export function parseEvaluationLiveEvent(value: unknown): EvaluationLiveEvent | null {
  let candidate: unknown = value;
  if (typeof value === 'string') { try { candidate = JSON.parse(value); } catch { return null; } }
  if (!record(candidate) || !closed(candidate, ['schemaVersion', 'eventId', 'eventType', 'occurredAt', 'resourceType', 'resourceId', 'change', 'state', 'capturePolicy']) || !metadataTree(candidate)) return null;
  if (candidate.schemaVersion !== 1 || candidate.eventType !== 'evaluation.changed' || !uuid(candidate.eventId) || !utc(candidate.occurredAt) || !oneOf(evaluationResourceTypes, candidate.resourceType) || !boundedIdentifier(candidate.resourceId) || !oneOf(changes, candidate.change) || !boundedIdentifier(candidate.state) || candidate.capturePolicy !== 'metadata_only') return null;
  return { schemaVersion: 1, eventId: candidate.eventId, eventType: 'evaluation.changed', occurredAt: candidate.occurredAt, resourceType: candidate.resourceType, resourceId: candidate.resourceId, change: candidate.change, state: candidate.state, capturePolicy: 'metadata_only' };
}
