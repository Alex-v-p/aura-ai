// Generated from contracts/openapi/observatory-v1.yaml. Do not edit by hand.
// Contract SHA-256: fcf696447c974204516ee92e651766f84db5e6f4b64b96da9d10bd6d6f160084

export type Uuid = string;

export type UtcInstant = string;

export type SafeLabel = string;

export type ComponentId = string;

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

export type ComponentSummary = { readonly "componentId": ComponentId; readonly "versions": ReadonlyArray<SafeLabel>; readonly "firstSeenAt": UtcInstant; readonly "lastSeenAt": UtcInstant; readonly "health": HealthSummary; };

export type ComponentCollection = { readonly "items": ReadonlyArray<ComponentSummary>; readonly "snapshotAt": UtcInstant; };

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
  readonly listComponents: { readonly response: ComponentCollection; readonly body: never; readonly path: Readonly<Record<string, never>>; readonly query: Readonly<Record<string, never>>; readonly headers: Readonly<Record<string, never>>; };
  readonly getComponentHealth: { readonly response: ComponentHealth; readonly body: never; readonly path: { readonly "component_id": ComponentId; }; readonly query: { readonly "from"?: UtcInstant; readonly "to"?: UtcInstant; }; readonly headers: Readonly<Record<string, never>>; };
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
  getComponentHealth: { method: 'GET', pathTemplate: '/api/v1/components/{component_id}/health', requestMode: 'none', responseMode: 'json' },
  listDeployments: { method: 'GET', pathTemplate: '/api/v1/deployments', requestMode: 'none', responseMode: 'json' },
  streamEvents: { method: 'GET', pathTemplate: '/api/v1/events', requestMode: 'none', responseMode: 'event-stream' },
  createRetentionAction: { method: 'POST', pathTemplate: '/api/v1/retention/actions', requestMode: 'json', responseMode: 'json' },
  getRetentionAction: { method: 'GET', pathTemplate: '/api/v1/retention/actions/{action_id}', requestMode: 'none', responseMode: 'json' },
} as const satisfies Readonly<Record<ObservatoryOperationId, ObservatoryOperationDescriptor>>;
