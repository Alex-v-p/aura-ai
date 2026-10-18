// Generated from contracts/openapi/observatory-v1.yaml. Do not edit by hand.
// Contract SHA-256: b575aee7bfaf6bebe59ac83134a799ed6b686adb5887837813ac7d17ac248bf3

export type Uuid = string;

export type UtcInstant = string;

export type SafeLabel = string;

export type RevisionIdentifier = string;

export type ModelIdentifier = string;

export type ComponentId = string;

export type DefinitionIdentifier = string;

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
  listDeployments: { method: 'GET', pathTemplate: '/api/v1/deployments', requestMode: 'none', responseMode: 'json' },
  streamEvents: { method: 'GET', pathTemplate: '/api/v1/events', requestMode: 'none', responseMode: 'event-stream' },
  createRetentionAction: { method: 'POST', pathTemplate: '/api/v1/retention/actions', requestMode: 'json', responseMode: 'json' },
  getRetentionAction: { method: 'GET', pathTemplate: '/api/v1/retention/actions/{action_id}', requestMode: 'none', responseMode: 'json' },
} as const satisfies Readonly<Record<ObservatoryOperationId, ObservatoryOperationDescriptor>>;
