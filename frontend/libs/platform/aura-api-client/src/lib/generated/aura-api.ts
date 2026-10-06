// Generated from contracts/openapi/aura-v1.yaml. Do not edit by hand.
// Contract SHA-256: 2ca1c9a4207fb8c4424d6e1e2d0d2443830b2fcad4585caad59822f5a6d8bfed

export type Session = { readonly "principal": Principal; readonly "csrfToken": string; readonly "idleExpiresAt": string; readonly "absoluteExpiresAt": string; };

export type Principal = { readonly "issuer": string; readonly "subject": string; readonly "displayName"?: string | null; };

export type ModelCatalog = { readonly "models": ReadonlyArray<Model>; readonly "defaultModelId": string | null; readonly "observedAt": string; };

export type Model = { readonly "id": string; readonly "displayName": string; readonly "provider": string; readonly "capabilities": ReadonlyArray<"chat" | "completion" | "embedding" | "vision" | "tools" | "structured_output">; readonly "availability": "available" | "unavailable" | "unknown"; readonly "selectable": boolean; readonly "disabledReason": string | null; };

export type ProfileStatus = "active" | "disabled";

export type AgentCollection = { readonly "items": ReadonlyArray<AgentProfile>; };

export type AgentProfile = { readonly "id": string; readonly "status": ProfileStatus; readonly "version": number; readonly "currentRevision": AgentRevision; readonly "createdAt": string; readonly "updatedAt": string; };

export type AgentDetail = { readonly "id": string; readonly "status": ProfileStatus; readonly "version": number; readonly "currentRevision": AgentRevision; readonly "revisions": ReadonlyArray<AgentRevision>; readonly "createdAt": string; readonly "updatedAt": string; };

export type AgentRevision = { readonly "id": string; readonly "profileId": string; readonly "revision": number; readonly "displayName": string; readonly "purpose": string; readonly "instructions": string; readonly "personaRevisionId": string; readonly "promptBundleRevisionId": string; readonly "modelPolicyRevisionId": string; readonly "createdAt": string; };

export type CreateAgentRequest = { readonly "displayName": string; readonly "purpose": string; readonly "instructions": string; readonly "personaRevisionId": string; };

export type CreateAgentRevisionRequest = { readonly "displayName": string; readonly "purpose": string; readonly "instructions": string; readonly "personaRevisionId": string; readonly "expectedVersion": number; };

export type UpdateAgentStatusRequest = { readonly "status": ProfileStatus; readonly "expectedVersion": number; };

export type PersonaCollection = { readonly "items": ReadonlyArray<PersonaProfile>; };

export type PersonaProfile = { readonly "id": string; readonly "status": ProfileStatus; readonly "version": number; readonly "currentRevision": PersonaRevision; readonly "createdAt": string; readonly "updatedAt": string; };

export type PersonaDetail = { readonly "id": string; readonly "status": ProfileStatus; readonly "version": number; readonly "currentRevision": PersonaRevision; readonly "revisions": ReadonlyArray<PersonaRevision>; readonly "createdAt": string; readonly "updatedAt": string; };

export type PersonaRevision = { readonly "id": string; readonly "profileId": string; readonly "revision": number; readonly "displayName": string; readonly "description": string; readonly "instructions": string; readonly "createdAt": string; };

export type CreatePersonaRequest = { readonly "displayName": string; readonly "description": string; readonly "instructions": string; };

export type CreatePersonaRevisionRequest = { readonly "displayName": string; readonly "description": string; readonly "instructions": string; readonly "expectedVersion": number; };

export type UpdatePersonaStatusRequest = { readonly "status": ProfileStatus; readonly "expectedVersion": number; };

export type CreateConversationRequest = { readonly "message": string; readonly "modelId": string; readonly "agentRevisionId"?: string; readonly "personaRevisionId"?: string; };

export type UpdateConversationRequest = { readonly "modelId"?: string; readonly "agentRevisionId"?: string; readonly "personaRevisionId"?: string; readonly "useAgentDefaultPersona"?: true; readonly "transcriptSharingConfirmed"?: boolean; readonly "version": number; };

export type CreateRunRequest = { readonly "message": string; readonly "conversationVersion": number; };

export type ConversationPage = { readonly "items": ReadonlyArray<ConversationSummary>; readonly "nextCursor": string | null; };

export type ConversationSummary = { readonly "id": string; readonly "title": string; readonly "agentProfileId": string; readonly "agentRevisionId": string; readonly "agent"?: AgentReference; readonly "agentAssignments"?: ReadonlyArray<AgentAssignment>; readonly "persona"?: PersonaReference; readonly "personaOverride"?: boolean; readonly "personaAssignments"?: ReadonlyArray<PersonaAssignment>; readonly "modelId": string; readonly "version": number; readonly "createdAt": string; readonly "updatedAt": string; readonly "currentRun": Run | null; };

export type AgentReference = { readonly "profileId": string; readonly "revisionId": string; readonly "displayName": string; readonly "revision": number; readonly "status": ProfileStatus; readonly "newerRevisionAvailable": boolean; };

export type AgentAssignment = { readonly "id": string; readonly "agent": AgentReference; readonly "reason": "initial" | "manual_switch" | "revision_upgrade"; readonly "effectiveAfterMessageId": string | null; readonly "createdAt": string; };

export type PersonaReference = { readonly "profileId": string; readonly "revisionId": string; readonly "displayName": string; readonly "revision": number; readonly "status": ProfileStatus; readonly "newerRevisionAvailable": boolean; };

export type PersonaAssignment = { readonly "id": string; readonly "persona": PersonaReference; readonly "source": "agent_default" | "conversation_override"; readonly "reason": "initial" | "agent_switch" | "agent_revision_upgrade" | "manual_override" | "reset_to_agent_default"; readonly "effectiveAfterMessageId": string | null; readonly "createdAt": string; };

export type ConversationDetail = ConversationSummary & { readonly "messages": ReadonlyArray<Message>; readonly "recentRuns": ReadonlyArray<Run>; };

export type Message = { readonly "id": string; readonly "conversationId": string; readonly "role": "user" | "assistant"; readonly "content": string; readonly "state": "complete" | "partial" | "interrupted" | "failed"; readonly "runId": string | null; readonly "createdAt": string; readonly "updatedAt": string; };

export type Run = { readonly "id": string; readonly "conversationId": string; readonly "userMessageId": string; readonly "assistantMessageId": string | null; readonly "status": RunStatus; readonly "agentRevisionId": string; readonly "modelPolicyRevisionId": string; readonly "personaRevisionId"?: string; readonly "promptBundleRevisionId"?: string; readonly "promptHash"?: string; readonly "provider": string; readonly "modelId": string; readonly "retryOfRunId": string | null; readonly "createdAt": string; readonly "startedAt": string | null; readonly "finishedAt": string | null; readonly "error": RunError | null; };

export type RunStatus = "queued" | "running" | "cancel_requested" | "canceled" | "completed" | "failed" | "interrupted";

export type RunError = { readonly "code": string; readonly "message": string; readonly "retryable": boolean; readonly "traceId": string; };

export type ConversationRunAccepted = { readonly "conversation": ConversationSummary; readonly "userMessage": Message; readonly "run": Run; };

export type RunEvent = RunSnapshotEvent | RunStatusEvent | AssistantDeltaEvent | AssistantSnapshotEvent | RunErrorEvent | HeartbeatEvent;

export type RunEventBase = { readonly "schemaVersion": 1; readonly "eventId": string; readonly "sequence": number; readonly "eventType": string; readonly "runId": string; readonly "conversationId": string; readonly "occurredAt": string; };

export type RunSnapshotEvent = RunEventBase & { readonly "eventType": "run.snapshot"; readonly "data": RunSnapshotData; };

export type RunStatusEvent = RunEventBase & { readonly "eventType": "run.status"; readonly "data": RunStatusData; };

export type AssistantDeltaEvent = RunEventBase & { readonly "eventType": "assistant.delta"; readonly "data": AssistantDeltaData; };

export type AssistantSnapshotEvent = RunEventBase & { readonly "eventType": "assistant.snapshot"; readonly "data": AssistantSnapshotData; };

export type RunErrorEvent = RunEventBase & { readonly "eventType": "run.error"; readonly "data": RunError; };

export type HeartbeatEvent = RunEventBase & { readonly "eventType": "heartbeat"; readonly "data": { readonly "serverTime": string; }; };

export type RunSnapshotData = { readonly "run": Run; readonly "assistantMessage": Message | null; };

export type RunStatusData = { readonly "status": RunStatus; readonly "startedAt": string | null; readonly "finishedAt": string | null; };

export type AssistantDeltaData = { readonly "messageId": string; readonly "offset": number; readonly "text": string; };

export type AssistantSnapshotData = { readonly "message": Message; };

export type ErrorEnvelope = { readonly "type": string; readonly "title": string; readonly "status": number; readonly "code": string; readonly "detail": string; readonly "retryable": boolean; readonly "traceId": string; readonly "resource": string | null; readonly "fieldErrors"?: ReadonlyArray<FieldError>; };

export type FieldError = { readonly "field": string; readonly "code": string; readonly "message": string; };

export type Liveness = { readonly "status": "alive"; };

export type Readiness = { readonly "status": "ready" | "degraded"; readonly "checks": Readonly<Record<string, DependencyReadiness>>; readonly "checkedAt": string; };

export type DependencyReadiness = { readonly "status": "ready" | "degraded" | "unavailable"; readonly "detail": string | null; };

export interface AuraApiOperations {
  readonly getSession: { readonly response: Session; readonly body: never; readonly path: Readonly<Record<string, never>>; readonly query: Readonly<Record<string, never>>; readonly headers: Readonly<Record<string, never>>; };
  readonly startLogin: { readonly response: void; readonly body: never; readonly path: Readonly<Record<string, never>>; readonly query: { readonly "return_to"?: string; }; readonly headers: Readonly<Record<string, never>>; };
  readonly completeLogin: { readonly response: void; readonly body: never; readonly path: Readonly<Record<string, never>>; readonly query: { readonly "code": string; readonly "state": string; }; readonly headers: Readonly<Record<string, never>>; };
  readonly logout: { readonly response: void; readonly body: never; readonly path: Readonly<Record<string, never>>; readonly query: Readonly<Record<string, never>>; readonly headers: { readonly "X-CSRF-Token": string; }; };
  readonly listModels: { readonly response: ModelCatalog; readonly body: never; readonly path: Readonly<Record<string, never>>; readonly query: Readonly<Record<string, never>>; readonly headers: Readonly<Record<string, never>>; };
  readonly listAgents: { readonly response: AgentCollection; readonly body: never; readonly path: Readonly<Record<string, never>>; readonly query: Readonly<Record<string, never>>; readonly headers: Readonly<Record<string, never>>; };
  readonly createAgent: { readonly response: AgentDetail; readonly body: CreateAgentRequest; readonly path: Readonly<Record<string, never>>; readonly query: Readonly<Record<string, never>>; readonly headers: { readonly "X-CSRF-Token": string; readonly "Idempotency-Key": string; }; };
  readonly getAgent: { readonly response: AgentDetail; readonly body: never; readonly path: { readonly "agent_profile_id": string; }; readonly query: Readonly<Record<string, never>>; readonly headers: Readonly<Record<string, never>>; };
  readonly updateAgentStatus: { readonly response: AgentDetail; readonly body: UpdateAgentStatusRequest; readonly path: { readonly "agent_profile_id": string; }; readonly query: Readonly<Record<string, never>>; readonly headers: { readonly "X-CSRF-Token": string; readonly "Idempotency-Key": string; }; };
  readonly createAgentRevision: { readonly response: AgentDetail; readonly body: CreateAgentRevisionRequest; readonly path: { readonly "agent_profile_id": string; }; readonly query: Readonly<Record<string, never>>; readonly headers: { readonly "X-CSRF-Token": string; readonly "Idempotency-Key": string; }; };
  readonly listPersonas: { readonly response: PersonaCollection; readonly body: never; readonly path: Readonly<Record<string, never>>; readonly query: Readonly<Record<string, never>>; readonly headers: Readonly<Record<string, never>>; };
  readonly createPersona: { readonly response: PersonaDetail; readonly body: CreatePersonaRequest; readonly path: Readonly<Record<string, never>>; readonly query: Readonly<Record<string, never>>; readonly headers: { readonly "X-CSRF-Token": string; readonly "Idempotency-Key": string; }; };
  readonly getPersona: { readonly response: PersonaDetail; readonly body: never; readonly path: { readonly "persona_profile_id": string; }; readonly query: Readonly<Record<string, never>>; readonly headers: Readonly<Record<string, never>>; };
  readonly updatePersonaStatus: { readonly response: PersonaDetail; readonly body: UpdatePersonaStatusRequest; readonly path: { readonly "persona_profile_id": string; }; readonly query: Readonly<Record<string, never>>; readonly headers: { readonly "X-CSRF-Token": string; readonly "Idempotency-Key": string; }; };
  readonly createPersonaRevision: { readonly response: PersonaDetail; readonly body: CreatePersonaRevisionRequest; readonly path: { readonly "persona_profile_id": string; }; readonly query: Readonly<Record<string, never>>; readonly headers: { readonly "X-CSRF-Token": string; readonly "Idempotency-Key": string; }; };
  readonly listConversations: { readonly response: ConversationPage; readonly body: never; readonly path: Readonly<Record<string, never>>; readonly query: { readonly "cursor"?: string; readonly "limit"?: number; }; readonly headers: Readonly<Record<string, never>>; };
  readonly createConversation: { readonly response: ConversationRunAccepted; readonly body: CreateConversationRequest; readonly path: Readonly<Record<string, never>>; readonly query: Readonly<Record<string, never>>; readonly headers: { readonly "X-CSRF-Token": string; readonly "Idempotency-Key": string; }; };
  readonly getConversation: { readonly response: ConversationDetail; readonly body: never; readonly path: { readonly "conversation_id": string; }; readonly query: Readonly<Record<string, never>>; readonly headers: Readonly<Record<string, never>>; };
  readonly updateConversation: { readonly response: ConversationSummary; readonly body: UpdateConversationRequest; readonly path: { readonly "conversation_id": string; }; readonly query: Readonly<Record<string, never>>; readonly headers: { readonly "X-CSRF-Token": string; readonly "Idempotency-Key": string; }; };
  readonly createRun: { readonly response: ConversationRunAccepted; readonly body: CreateRunRequest; readonly path: { readonly "conversation_id": string; }; readonly query: Readonly<Record<string, never>>; readonly headers: { readonly "X-CSRF-Token": string; readonly "Idempotency-Key": string; }; };
  readonly cancelRun: { readonly response: Run; readonly body: never; readonly path: { readonly "run_id": string; }; readonly query: Readonly<Record<string, never>>; readonly headers: { readonly "X-CSRF-Token": string; readonly "Idempotency-Key": string; }; };
  readonly retryRun: { readonly response: ConversationRunAccepted; readonly body: never; readonly path: { readonly "run_id": string; }; readonly query: Readonly<Record<string, never>>; readonly headers: { readonly "X-CSRF-Token": string; readonly "Idempotency-Key": string; }; };
  readonly streamRunEvents: { readonly response: RunEvent; readonly body: never; readonly path: { readonly "run_id": string; }; readonly query: Readonly<Record<string, never>>; readonly headers: { readonly "Last-Event-ID"?: string; }; };
  readonly getLiveness: { readonly response: Liveness; readonly body: never; readonly path: Readonly<Record<string, never>>; readonly query: Readonly<Record<string, never>>; readonly headers: Readonly<Record<string, never>>; };
  readonly getReadiness: { readonly response: Readiness; readonly body: never; readonly path: Readonly<Record<string, never>>; readonly query: Readonly<Record<string, never>>; readonly headers: Readonly<Record<string, never>>; };
}

export type AuraOperationId = keyof AuraApiOperations;
export type AuraOperationInput<K extends AuraOperationId> = Readonly<{
  path: AuraApiOperations[K]['path'];
  query: AuraApiOperations[K]['query'];
  headers: AuraApiOperations[K]['headers'];
  body: AuraApiOperations[K]['body'];
}>;
export type AuraOperationResponse<K extends AuraOperationId> = AuraApiOperations[K]['response'];

export interface AuraOperationDescriptor {
  readonly method: string;
  readonly pathTemplate: string;
  readonly responseMode: 'json' | 'event-stream';
}

export const AURA_API_OPERATIONS = {
  getSession: { method: 'GET', pathTemplate: '/api/v1/auth/session', responseMode: 'json' },
  startLogin: { method: 'GET', pathTemplate: '/api/v1/auth/login', responseMode: 'json' },
  completeLogin: { method: 'GET', pathTemplate: '/api/v1/auth/callback', responseMode: 'json' },
  logout: { method: 'POST', pathTemplate: '/api/v1/auth/logout', responseMode: 'json' },
  listModels: { method: 'GET', pathTemplate: '/api/v1/models', responseMode: 'json' },
  listAgents: { method: 'GET', pathTemplate: '/api/v1/agents', responseMode: 'json' },
  createAgent: { method: 'POST', pathTemplate: '/api/v1/agents', responseMode: 'json' },
  getAgent: { method: 'GET', pathTemplate: '/api/v1/agents/{agent_profile_id}', responseMode: 'json' },
  updateAgentStatus: { method: 'PATCH', pathTemplate: '/api/v1/agents/{agent_profile_id}', responseMode: 'json' },
  createAgentRevision: { method: 'POST', pathTemplate: '/api/v1/agents/{agent_profile_id}/revisions', responseMode: 'json' },
  listPersonas: { method: 'GET', pathTemplate: '/api/v1/personas', responseMode: 'json' },
  createPersona: { method: 'POST', pathTemplate: '/api/v1/personas', responseMode: 'json' },
  getPersona: { method: 'GET', pathTemplate: '/api/v1/personas/{persona_profile_id}', responseMode: 'json' },
  updatePersonaStatus: { method: 'PATCH', pathTemplate: '/api/v1/personas/{persona_profile_id}', responseMode: 'json' },
  createPersonaRevision: { method: 'POST', pathTemplate: '/api/v1/personas/{persona_profile_id}/revisions', responseMode: 'json' },
  listConversations: { method: 'GET', pathTemplate: '/api/v1/conversations', responseMode: 'json' },
  createConversation: { method: 'POST', pathTemplate: '/api/v1/conversations', responseMode: 'json' },
  getConversation: { method: 'GET', pathTemplate: '/api/v1/conversations/{conversation_id}', responseMode: 'json' },
  updateConversation: { method: 'PATCH', pathTemplate: '/api/v1/conversations/{conversation_id}', responseMode: 'json' },
  createRun: { method: 'POST', pathTemplate: '/api/v1/conversations/{conversation_id}/runs', responseMode: 'json' },
  cancelRun: { method: 'POST', pathTemplate: '/api/v1/runs/{run_id}/cancel', responseMode: 'json' },
  retryRun: { method: 'POST', pathTemplate: '/api/v1/runs/{run_id}/retry', responseMode: 'json' },
  streamRunEvents: { method: 'GET', pathTemplate: '/api/v1/runs/{run_id}/events', responseMode: 'event-stream' },
  getLiveness: { method: 'GET', pathTemplate: '/health/live', responseMode: 'json' },
  getReadiness: { method: 'GET', pathTemplate: '/health/ready', responseMode: 'json' },
} as const satisfies Readonly<Record<AuraOperationId, AuraOperationDescriptor>>;
