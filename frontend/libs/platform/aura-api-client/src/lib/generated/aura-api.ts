// Generated from contracts/openapi/aura-v1.yaml. Do not edit by hand.
// Contract SHA-256: 6cf8b930862dafebe1ccf3d4fa6eb86258153b7295731c112bec52dddb02a4cc

export type Session = { readonly "principal": Principal; readonly "csrfToken": string; readonly "idleExpiresAt": string; readonly "absoluteExpiresAt": string; };

export type Principal = { readonly "issuer": string; readonly "subject": string; readonly "displayName"?: string | null; };

export type ModelCatalog = { readonly "models": ReadonlyArray<Model>; readonly "defaultModelId": string | null; readonly "observedAt": string; };

export type Model = { readonly "id": string; readonly "displayName": string; readonly "provider": string; readonly "capabilities": ReadonlyArray<"chat" | "completion" | "embedding" | "vision" | "tools" | "structured_output">; readonly "availability": "available" | "unavailable" | "unknown"; readonly "selectable": boolean; readonly "disabledReason": string | null; };

export type MemoryKind = "episodic" | "semantic" | "procedural" | "preference" | "system";

export type MemoryScopeType = "user" | "agent";

export type MemoryCollectionScopeType = "user" | "agent" | "all";

export type MemoryLifecycleStatus = "active" | "dormant" | "archived" | "disabled" | "disputed" | "superseded";

export type MemoryProvenanceType = "manual" | "conversation_message" | "run" | "system" | "import";

export type MemoryRelationType = "supersedes" | "superseded_by" | "disputes" | "disputed_by";

export type MemoryEmbeddingGenerationStatus = "building" | "active" | "retired" | "failed";

export type MemoryScope = MemoryUserScope | MemoryAgentScope;

export type MemoryUserScope = { readonly "type": "user"; };

export type MemoryAgentScope = { readonly "type": "agent"; readonly "agentProfileId": string; };

export type MemoryPage = { readonly "items": ReadonlyArray<MemorySummary>; readonly "nextCursor": string | null; };

export type SearchMemoriesRequest = { readonly "query": string; readonly "cursor"?: string; readonly "limit"?: number; readonly "kind"?: MemoryKind; readonly "scopeType"?: MemoryCollectionScopeType; readonly "agentProfileId"?: string; readonly "status"?: MemoryLifecycleStatus; readonly "provenanceType"?: MemoryProvenanceType; readonly "confidenceMin"?: number; readonly "confidenceMax"?: number; readonly "createdFrom"?: string; readonly "createdTo"?: string; readonly "includeHistorical"?: boolean; };

export type MemorySummary = { readonly "id": string; readonly "scope": MemoryScope; readonly "status": MemoryLifecycleStatus; readonly "pinned": boolean; readonly "version": number; readonly "currentRevision": MemoryRevision; readonly "reinforcedAt": string | null; readonly "dormantAt": string | null; readonly "archivedAt": string | null; readonly "createdAt": string; readonly "updatedAt": string; readonly "currentRelevance"?: number; };

export type MemoryDetail = { readonly "id": string; readonly "scope": MemoryScope; readonly "status": MemoryLifecycleStatus; readonly "pinned": boolean; readonly "version": number; readonly "currentRevision": MemoryRevision; readonly "reinforcedAt": string | null; readonly "dormantAt": string | null; readonly "archivedAt": string | null; readonly "createdAt": string; readonly "updatedAt": string; readonly "currentRelevance"?: number; readonly "revisions": ReadonlyArray<MemoryRevision>; readonly "provenance": ReadonlyArray<MemoryProvenance>; readonly "embeddingGenerations": ReadonlyArray<MemoryEmbeddingGeneration>; readonly "embeddings": ReadonlyArray<MemoryEmbedding>; readonly "relations": ReadonlyArray<MemoryRelation>; };

export type MemoryRevision = { readonly "id": string; readonly "memoryId": string; readonly "revision": number; readonly "kind": MemoryKind; readonly "content": string; readonly "correctionReason": string | null; readonly "confidence": number; readonly "importance": number; readonly "halfLifeDays": number; readonly "observedAt": string | null; readonly "validFrom": string | null; readonly "validTo": string | null; readonly "createdAt": string; };

export type MemoryProvenance = { readonly "id": string; readonly "memoryRevisionId": string; readonly "type": MemoryProvenanceType; readonly "sourceId": string | null; readonly "sourceContentDigest": string | null; readonly "evidence": string | null; readonly "observedAt": string | null; readonly "createdAt": string; };

export type MemoryEmbeddingGeneration = { readonly "id": string; readonly "generation": number; readonly "modelId": string; readonly "modelRevision": string | null; readonly "modelDigest"?: string | null; readonly "dimension": number; readonly "status": MemoryEmbeddingGenerationStatus; readonly "createdAt": string; readonly "activatedAt": string | null; };

export type MemoryEmbedding = { readonly "id": string; readonly "memoryRevisionId": string; readonly "generationId": string; readonly "generation": number; readonly "modelId": string; readonly "modelRevision": string | null; readonly "modelDigest"?: string | null; readonly "digest": string; readonly "dimension": number; readonly "createdAt": string; };

export type MemoryRelation = { readonly "type": MemoryRelationType; readonly "memoryId": string; readonly "createdAt": string; };

export type CreateMemoryRequest = { readonly "content": string; readonly "kind": MemoryKind; readonly "scope": MemoryScope; readonly "confidence": number; readonly "importance": number; readonly "halfLifeDays": number; readonly "observedAt"?: string | null; readonly "validFrom"?: string | null; readonly "validTo"?: string | null; };

export type CorrectMemoryRequest = { readonly "content": string; readonly "reason": string; readonly "kind"?: MemoryKind; readonly "confidence"?: number; readonly "importance"?: number; readonly "halfLifeDays"?: number; readonly "observedAt"?: string | null; readonly "validFrom"?: string | null; readonly "validTo"?: string | null; readonly "expectedVersion": number; };

export type UpdateMemoryStatusRequest = { readonly "status": MemoryLifecycleStatus; readonly "relatedMemoryId"?: string | null; readonly "expectedVersion": number; };

export type UpdateMemoryPinRequest = { readonly "pinned": boolean; readonly "expectedVersion": number; };

export type PurgeMemoryRequest = { readonly "confirmation": "PURGE MEMORY"; readonly "expectedVersion": number; };

export type MemoryPurgeReceipt = { readonly "memoryId": string; readonly "auditId": string; readonly "purgedAt": string; };

export type MemoryCandidateAction = "ignore" | "create" | "reinforce" | "supersede" | "dispute" | "review";

export type MemoryCandidateState = "proposed" | "accepted" | "rejected" | "review" | "retryable";

export type MemorySensitivity = "ordinary" | "health" | "finance" | "identity" | "intimate" | "precise_location" | "sensitive" | "credential" | "unknown_risk";

export type MemoryCandidatePage = { readonly "items": ReadonlyArray<MemoryCandidateSummary>; readonly "nextCursor": string | null; };

export type MemoryCandidateSummary = { readonly "id": string; readonly "jobId": string; readonly "runId": string; readonly "version": number; readonly "action": MemoryCandidateAction; readonly "state": MemoryCandidateState; readonly "content": string | null; readonly "kind": MemoryKind | null; readonly "scope": MemoryScope | null; readonly "confidence": number; readonly "importance": number | null; readonly "halfLifeDays": number | null; readonly "validTo": string | null; readonly "sensitivity": MemorySensitivity; readonly "relatedMemoryId": string | null; readonly "memoryId": string | null; readonly "decisionReason": string | null; readonly "createdAt": string; readonly "decidedAt": string | null; };

export type MemoryCandidateDetail = MemoryCandidateSummary & { readonly "groundedMessageIds": ReadonlyArray<string>; };

export type MemoryCandidateEdit = { readonly "content": string; readonly "action": MemoryCandidateAction; readonly "kind": MemoryKind; readonly "scope": MemoryScope; readonly "confidence": number; readonly "importance": number; readonly "halfLifeDays": number; readonly "validTo": string | null; readonly "relatedMemoryId": string | null; };

export type ApproveMemoryCandidateRequest = { readonly "expectedVersion": number; readonly "edit"?: MemoryCandidateEdit; };

export type RejectMemoryCandidateRequest = { readonly "expectedVersion": number; readonly "reason": string; };

export type MemoryCandidateDecisionReceipt = { readonly "candidate": MemoryCandidateDetail; readonly "activityId": string; };

export type MemoryModelCapability = "structured_output" | "embedding";

export type MemoryCompatibleModel = { readonly "id": string; readonly "displayName": string; readonly "provider": string; readonly "modelRevision": string | null; readonly "modelDigest": string; readonly "capabilities": ReadonlyArray<MemoryModelCapability>; readonly "dimension": number | null; readonly "available": boolean; readonly "disabledReason": string | null; };

export type MemoryModelInventory = { readonly "models": ReadonlyArray<MemoryCompatibleModel>; readonly "observedAt": string; };

export type MemoryModelSelection = { readonly "modelId": string; readonly "modelRevision": string | null; readonly "modelDigest": string; };

export type MemoryManagedEmbeddingGeneration = { readonly "id": string; readonly "generation": number; readonly "model": MemoryModelSelection; readonly "dimension": number; readonly "status": MemoryEmbeddingGenerationStatus; readonly "createdAt": string; readonly "activatedAt": string | null; };

export type MemoryModelConfiguration = { readonly "version": number; readonly "extraction": MemoryModelSelection; readonly "embedding": MemoryModelSelection; readonly "activeGeneration": MemoryManagedEmbeddingGeneration | null; readonly "buildingGeneration": MemoryManagedEmbeddingGeneration | null; readonly "updatedAt": string; };

export type UpdateMemoryModelConfigurationRequest = { readonly "extractionModelId": string; readonly "embeddingModelId": string; readonly "expectedVersion": number; };

export type MemoryReindexPhase = "idle" | "queued" | "running" | "failed" | "completed";

export type MemoryReindexStatus = { readonly "phase": MemoryReindexPhase; readonly "activeGeneration": MemoryManagedEmbeddingGeneration | null; readonly "replacementGeneration": MemoryManagedEmbeddingGeneration | null; readonly "processedRevisionCount": number; readonly "totalRevisionCount": number; readonly "startedAt": string | null; readonly "updatedAt": string | null; readonly "completedAt": string | null; readonly "retryable": boolean; };

export type ResumeMemoryReindexRequest = { readonly "generationId": string; };

export type AgentMemoryPolicyCollection = { readonly "items": ReadonlyArray<AgentMemoryPolicy>; readonly "attachedPolicyRevisionId": string; readonly "agentVersion": number; };

export type AgentMemoryPolicy = { readonly "id": string; readonly "agentProfileId": string; readonly "revision": number; readonly "sharedUserRead": boolean; readonly "currentAgentRead": boolean; readonly "sharedUserPromotion": boolean; readonly "fallbackRelevanceThreshold": number; readonly "maxMemories": number; readonly "contextBudgetFraction": number; readonly "fallbackAgentProfileIds": ReadonlyArray<string>; readonly "createdAt": string; };

export type CreateAgentMemoryPolicyRequest = { readonly "sharedUserRead": boolean; readonly "currentAgentRead": boolean; readonly "sharedUserPromotion": boolean; readonly "fallbackRelevanceThreshold": number; readonly "maxMemories": number; readonly "contextBudgetFraction": number; readonly "fallbackAgentProfileIds": ReadonlyArray<string>; readonly "expectedRevision": number; };

export type AttachAgentMemoryPolicyRequest = { readonly "expectedAgentVersion": number; };

export type MemoryActivityAction = "created" | "reinforced" | "disputed" | "recalled" | "queued_for_review";

export type MemoryActivityStatus = "queued" | "completed" | "failed";

export type MemoryActivityReconciliationStatus = "pending" | "authoritative";

export type MemoryActivity = { readonly "id": string; readonly "action": MemoryActivityAction; readonly "status": MemoryActivityStatus; readonly "scope": MemoryScope | null; readonly "candidateId": string | null; readonly "memoryId": string | null; readonly "memoryRevisionId": string | null; readonly "policyRevisionId": string | null; readonly "embeddingGenerationId": string | null; readonly "reconciliationStatus": MemoryActivityReconciliationStatus; readonly "occurredAt": string; };

export type MemoryProcessingStatus = "queued" | "running" | "settled";

export type RunMemoryActivitySnapshot = { readonly "runId": string; readonly "processingStatus": MemoryProcessingStatus; readonly "items": ReadonlyArray<MemoryActivity>; readonly "lastEventId": string | null; readonly "reconciledAt": string; };

export type ProfileStatus = "active" | "disabled";

export type AgentCollection = { readonly "items": ReadonlyArray<AgentProfile>; };

export type AgentProfile = { readonly "id": string; readonly "status": ProfileStatus; readonly "version": number; readonly "currentRevision": AgentRevision; readonly "createdAt": string; readonly "updatedAt": string; };

export type AgentDetail = { readonly "id": string; readonly "status": ProfileStatus; readonly "version": number; readonly "currentRevision": AgentRevision; readonly "revisions": ReadonlyArray<AgentRevision>; readonly "createdAt": string; readonly "updatedAt": string; };

export type AgentRevision = { readonly "id": string; readonly "profileId": string; readonly "revision": number; readonly "displayName": string; readonly "purpose": string; readonly "instructions": string; readonly "personaRevisionId": string; readonly "promptBundleRevisionId": string; readonly "modelPolicyRevisionId": string; readonly "memoryPolicyRevisionId"?: string; readonly "createdAt": string; };

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

export type UpdateConversationMetadataRequest = { readonly "title"?: string; readonly "archived"?: boolean; readonly "version": number; };

export type CreateRunRequest = { readonly "message": string; readonly "conversationVersion": number; };

export type ConversationPage = { readonly "items": ReadonlyArray<ConversationSummary>; readonly "nextCursor": string | null; };

export type ConversationSummary = { readonly "id": string; readonly "title": string; readonly "agentProfileId": string; readonly "agentRevisionId": string; readonly "agent"?: AgentReference; readonly "agentAssignments"?: ReadonlyArray<AgentAssignment>; readonly "persona"?: PersonaReference; readonly "personaOverride"?: boolean; readonly "personaAssignments"?: ReadonlyArray<PersonaAssignment>; readonly "modelId": string; readonly "version": number; readonly "createdAt": string; readonly "updatedAt": string; readonly "archivedAt"?: string | null; readonly "currentRun": Run | null; };

export type AgentReference = { readonly "profileId": string; readonly "revisionId": string; readonly "displayName": string; readonly "revision": number; readonly "status": ProfileStatus; readonly "newerRevisionAvailable": boolean; };

export type AgentAssignment = { readonly "id": string; readonly "agent": AgentReference; readonly "reason": "initial" | "manual_switch" | "revision_upgrade"; readonly "effectiveAfterMessageId": string | null; readonly "createdAt": string; };

export type PersonaReference = { readonly "profileId": string; readonly "revisionId": string; readonly "displayName": string; readonly "revision": number; readonly "status": ProfileStatus; readonly "newerRevisionAvailable": boolean; };

export type PersonaAssignment = { readonly "id": string; readonly "persona": PersonaReference; readonly "source": "agent_default" | "conversation_override"; readonly "reason": "initial" | "agent_switch" | "agent_revision_upgrade" | "manual_override" | "reset_to_agent_default"; readonly "effectiveAfterMessageId": string | null; readonly "createdAt": string; };

export type ConversationDetail = ConversationSummary & { readonly "messages": ReadonlyArray<Message>; readonly "recentRuns": ReadonlyArray<Run>; };

export type Message = { readonly "id": string; readonly "conversationId": string; readonly "role": "user" | "assistant"; readonly "content": string; readonly "state": "complete" | "partial" | "interrupted" | "failed"; readonly "runId": string | null; readonly "createdAt": string; readonly "updatedAt": string; };

export type Run = { readonly "id": string; readonly "conversationId": string; readonly "userMessageId": string; readonly "assistantMessageId": string | null; readonly "status": RunStatus; readonly "agentRevisionId": string; readonly "modelPolicyRevisionId": string; readonly "personaRevisionId"?: string; readonly "promptBundleRevisionId"?: string; readonly "promptHash"?: string; readonly "provider": string; readonly "modelId": string; readonly "retryOfRunId": string | null; readonly "createdAt": string; readonly "startedAt": string | null; readonly "finishedAt": string | null; readonly "error": RunError | null; };

export type RunStatus = "queued" | "running" | "cancel_requested" | "canceled" | "completed" | "failed" | "interrupted";

export type ConversationArchiveState = "active" | "archived" | "all";

export type RunError = { readonly "code": string; readonly "message": string; readonly "retryable": boolean; readonly "traceId": string; };

export type ConversationRunAccepted = { readonly "conversation": ConversationSummary; readonly "userMessage": Message; readonly "run": Run; };

export type RunEvent = RunSnapshotEvent | RunStatusEvent | AssistantDeltaEvent | AssistantSnapshotEvent | RunErrorEvent | MemoryActivityEvent | HeartbeatEvent;

export type RunEventBase = { readonly "schemaVersion": 1; readonly "eventId": string; readonly "sequence": number; readonly "eventType": string; readonly "runId": string; readonly "conversationId": string; readonly "occurredAt": string; };

export type RunSnapshotEvent = RunEventBase & { readonly "eventType": "run.snapshot"; readonly "data": RunSnapshotData; };

export type RunStatusEvent = RunEventBase & { readonly "eventType": "run.status"; readonly "data": RunStatusData; };

export type AssistantDeltaEvent = RunEventBase & { readonly "eventType": "assistant.delta"; readonly "data": AssistantDeltaData; };

export type AssistantSnapshotEvent = RunEventBase & { readonly "eventType": "assistant.snapshot"; readonly "data": AssistantSnapshotData; };

export type RunErrorEvent = RunEventBase & { readonly "eventType": "run.error"; readonly "data": RunError; };

export type MemoryActivityEvent = RunEventBase & { readonly "eventType": "memory.activity"; readonly "data": MemoryActivity; };

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
  readonly listConversations: { readonly response: ConversationPage; readonly body: never; readonly path: Readonly<Record<string, never>>; readonly query: { readonly "cursor"?: string; readonly "limit"?: number; readonly "q"?: string; readonly "agentProfileId"?: string; readonly "modelId"?: string; readonly "runStatus"?: RunStatus; readonly "archiveState"?: ConversationArchiveState; readonly "activityFrom"?: string; readonly "activityTo"?: string; }; readonly headers: Readonly<Record<string, never>>; };
  readonly createConversation: { readonly response: ConversationRunAccepted; readonly body: CreateConversationRequest; readonly path: Readonly<Record<string, never>>; readonly query: Readonly<Record<string, never>>; readonly headers: { readonly "X-CSRF-Token": string; readonly "Idempotency-Key": string; }; };
  readonly getConversation: { readonly response: ConversationDetail; readonly body: never; readonly path: { readonly "conversation_id": string; }; readonly query: Readonly<Record<string, never>>; readonly headers: Readonly<Record<string, never>>; };
  readonly updateConversation: { readonly response: ConversationSummary; readonly body: UpdateConversationRequest; readonly path: { readonly "conversation_id": string; }; readonly query: Readonly<Record<string, never>>; readonly headers: { readonly "X-CSRF-Token": string; readonly "Idempotency-Key": string; }; };
  readonly createRun: { readonly response: ConversationRunAccepted; readonly body: CreateRunRequest; readonly path: { readonly "conversation_id": string; }; readonly query: Readonly<Record<string, never>>; readonly headers: { readonly "X-CSRF-Token": string; readonly "Idempotency-Key": string; }; };
  readonly updateConversationMetadata: { readonly response: ConversationSummary; readonly body: UpdateConversationMetadataRequest; readonly path: { readonly "conversation_id": string; }; readonly query: Readonly<Record<string, never>>; readonly headers: { readonly "X-CSRF-Token": string; readonly "Idempotency-Key": string; }; };
  readonly cancelRun: { readonly response: Run; readonly body: never; readonly path: { readonly "run_id": string; }; readonly query: Readonly<Record<string, never>>; readonly headers: { readonly "X-CSRF-Token": string; readonly "Idempotency-Key": string; }; };
  readonly retryRun: { readonly response: ConversationRunAccepted; readonly body: never; readonly path: { readonly "run_id": string; }; readonly query: Readonly<Record<string, never>>; readonly headers: { readonly "X-CSRF-Token": string; readonly "Idempotency-Key": string; }; };
  readonly streamRunEvents: { readonly response: RunEvent; readonly body: never; readonly path: { readonly "run_id": string; }; readonly query: Readonly<Record<string, never>>; readonly headers: { readonly "Last-Event-ID"?: string; }; };
  readonly getRunMemoryActivity: { readonly response: RunMemoryActivitySnapshot; readonly body: never; readonly path: { readonly "run_id": string; }; readonly query: Readonly<Record<string, never>>; readonly headers: Readonly<Record<string, never>>; };
  readonly listMemories: { readonly response: MemoryPage; readonly body: never; readonly path: Readonly<Record<string, never>>; readonly query: { readonly "cursor"?: string; readonly "limit"?: number; readonly "kind"?: MemoryKind; readonly "scopeType"?: MemoryCollectionScopeType; readonly "agentProfileId"?: string; readonly "status"?: MemoryLifecycleStatus; readonly "provenanceType"?: MemoryProvenanceType; readonly "confidenceMin"?: number; readonly "confidenceMax"?: number; readonly "createdFrom"?: string; readonly "createdTo"?: string; readonly "includeHistorical"?: boolean; }; readonly headers: Readonly<Record<string, never>>; };
  readonly createMemory: { readonly response: MemoryDetail; readonly body: CreateMemoryRequest; readonly path: Readonly<Record<string, never>>; readonly query: Readonly<Record<string, never>>; readonly headers: { readonly "X-CSRF-Token": string; readonly "Idempotency-Key": string; }; };
  readonly searchMemories: { readonly response: MemoryPage; readonly body: SearchMemoriesRequest; readonly path: Readonly<Record<string, never>>; readonly query: Readonly<Record<string, never>>; readonly headers: { readonly "X-CSRF-Token": string; }; };
  readonly getMemory: { readonly response: MemoryDetail; readonly body: never; readonly path: { readonly "memory_id": string; }; readonly query: { readonly "scopeType"?: MemoryScopeType; readonly "agentProfileId"?: string; }; readonly headers: Readonly<Record<string, never>>; };
  readonly correctMemory: { readonly response: MemoryDetail; readonly body: CorrectMemoryRequest; readonly path: { readonly "memory_id": string; }; readonly query: { readonly "scopeType"?: MemoryScopeType; readonly "agentProfileId"?: string; }; readonly headers: { readonly "X-CSRF-Token": string; readonly "Idempotency-Key": string; }; };
  readonly updateMemoryStatus: { readonly response: MemoryDetail; readonly body: UpdateMemoryStatusRequest; readonly path: { readonly "memory_id": string; }; readonly query: { readonly "scopeType"?: MemoryScopeType; readonly "agentProfileId"?: string; }; readonly headers: { readonly "X-CSRF-Token": string; readonly "Idempotency-Key": string; }; };
  readonly updateMemoryPin: { readonly response: MemoryDetail; readonly body: UpdateMemoryPinRequest; readonly path: { readonly "memory_id": string; }; readonly query: { readonly "scopeType"?: MemoryScopeType; readonly "agentProfileId"?: string; }; readonly headers: { readonly "X-CSRF-Token": string; readonly "Idempotency-Key": string; }; };
  readonly purgeMemory: { readonly response: MemoryPurgeReceipt; readonly body: PurgeMemoryRequest; readonly path: { readonly "memory_id": string; }; readonly query: { readonly "scopeType"?: MemoryScopeType; readonly "agentProfileId"?: string; }; readonly headers: { readonly "X-CSRF-Token": string; readonly "Idempotency-Key": string; }; };
  readonly listMemoryCandidates: { readonly response: MemoryCandidatePage; readonly body: never; readonly path: Readonly<Record<string, never>>; readonly query: { readonly "cursor"?: string; readonly "limit"?: number; readonly "state"?: MemoryCandidateState; readonly "action"?: MemoryCandidateAction; readonly "sensitivity"?: MemorySensitivity; readonly "runId"?: string; }; readonly headers: Readonly<Record<string, never>>; };
  readonly getMemoryCandidate: { readonly response: MemoryCandidateDetail; readonly body: never; readonly path: { readonly "candidate_id": string; }; readonly query: Readonly<Record<string, never>>; readonly headers: Readonly<Record<string, never>>; };
  readonly approveMemoryCandidate: { readonly response: MemoryCandidateDecisionReceipt; readonly body: ApproveMemoryCandidateRequest; readonly path: { readonly "candidate_id": string; }; readonly query: Readonly<Record<string, never>>; readonly headers: { readonly "X-CSRF-Token": string; readonly "Idempotency-Key": string; }; };
  readonly rejectMemoryCandidate: { readonly response: MemoryCandidateDecisionReceipt; readonly body: RejectMemoryCandidateRequest; readonly path: { readonly "candidate_id": string; }; readonly query: Readonly<Record<string, never>>; readonly headers: { readonly "X-CSRF-Token": string; readonly "Idempotency-Key": string; }; };
  readonly getMemoryModelInventory: { readonly response: MemoryModelInventory; readonly body: never; readonly path: Readonly<Record<string, never>>; readonly query: Readonly<Record<string, never>>; readonly headers: Readonly<Record<string, never>>; };
  readonly getMemoryModelConfiguration: { readonly response: MemoryModelConfiguration; readonly body: never; readonly path: Readonly<Record<string, never>>; readonly query: Readonly<Record<string, never>>; readonly headers: Readonly<Record<string, never>>; };
  readonly updateMemoryModelConfiguration: { readonly response: MemoryModelConfiguration; readonly body: UpdateMemoryModelConfigurationRequest; readonly path: Readonly<Record<string, never>>; readonly query: Readonly<Record<string, never>>; readonly headers: { readonly "X-CSRF-Token": string; readonly "Idempotency-Key": string; }; };
  readonly getMemoryReindexStatus: { readonly response: MemoryReindexStatus; readonly body: never; readonly path: Readonly<Record<string, never>>; readonly query: Readonly<Record<string, never>>; readonly headers: Readonly<Record<string, never>>; };
  readonly resumeMemoryReindex: { readonly response: MemoryReindexStatus; readonly body: ResumeMemoryReindexRequest; readonly path: Readonly<Record<string, never>>; readonly query: Readonly<Record<string, never>>; readonly headers: { readonly "X-CSRF-Token": string; readonly "Idempotency-Key": string; }; };
  readonly listAgentMemoryPolicies: { readonly response: AgentMemoryPolicyCollection; readonly body: never; readonly path: { readonly "agent_profile_id": string; }; readonly query: Readonly<Record<string, never>>; readonly headers: Readonly<Record<string, never>>; };
  readonly createAgentMemoryPolicy: { readonly response: AgentMemoryPolicy; readonly body: CreateAgentMemoryPolicyRequest; readonly path: { readonly "agent_profile_id": string; }; readonly query: Readonly<Record<string, never>>; readonly headers: { readonly "X-CSRF-Token": string; readonly "Idempotency-Key": string; }; };
  readonly attachAgentMemoryPolicy: { readonly response: AgentDetail; readonly body: AttachAgentMemoryPolicyRequest; readonly path: { readonly "agent_profile_id": string; readonly "policy_revision_id": string; }; readonly query: Readonly<Record<string, never>>; readonly headers: { readonly "X-CSRF-Token": string; readonly "Idempotency-Key": string; }; };
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
  updateConversationMetadata: { method: 'PATCH', pathTemplate: '/api/v1/conversations/{conversation_id}/metadata', responseMode: 'json' },
  cancelRun: { method: 'POST', pathTemplate: '/api/v1/runs/{run_id}/cancel', responseMode: 'json' },
  retryRun: { method: 'POST', pathTemplate: '/api/v1/runs/{run_id}/retry', responseMode: 'json' },
  streamRunEvents: { method: 'GET', pathTemplate: '/api/v1/runs/{run_id}/events', responseMode: 'event-stream' },
  getRunMemoryActivity: { method: 'GET', pathTemplate: '/api/v1/runs/{run_id}/memory-activity', responseMode: 'json' },
  listMemories: { method: 'GET', pathTemplate: '/api/v1/memories', responseMode: 'json' },
  createMemory: { method: 'POST', pathTemplate: '/api/v1/memories', responseMode: 'json' },
  searchMemories: { method: 'POST', pathTemplate: '/api/v1/memories/search', responseMode: 'json' },
  getMemory: { method: 'GET', pathTemplate: '/api/v1/memories/{memory_id}', responseMode: 'json' },
  correctMemory: { method: 'POST', pathTemplate: '/api/v1/memories/{memory_id}/revisions', responseMode: 'json' },
  updateMemoryStatus: { method: 'PATCH', pathTemplate: '/api/v1/memories/{memory_id}/status', responseMode: 'json' },
  updateMemoryPin: { method: 'PATCH', pathTemplate: '/api/v1/memories/{memory_id}/pin', responseMode: 'json' },
  purgeMemory: { method: 'POST', pathTemplate: '/api/v1/memories/{memory_id}/purge', responseMode: 'json' },
  listMemoryCandidates: { method: 'GET', pathTemplate: '/api/v1/memory-candidates', responseMode: 'json' },
  getMemoryCandidate: { method: 'GET', pathTemplate: '/api/v1/memory-candidates/{candidate_id}', responseMode: 'json' },
  approveMemoryCandidate: { method: 'POST', pathTemplate: '/api/v1/memory-candidates/{candidate_id}/approve', responseMode: 'json' },
  rejectMemoryCandidate: { method: 'POST', pathTemplate: '/api/v1/memory-candidates/{candidate_id}/reject', responseMode: 'json' },
  getMemoryModelInventory: { method: 'GET', pathTemplate: '/api/v1/memory-model-inventory', responseMode: 'json' },
  getMemoryModelConfiguration: { method: 'GET', pathTemplate: '/api/v1/memory-model-configuration', responseMode: 'json' },
  updateMemoryModelConfiguration: { method: 'PUT', pathTemplate: '/api/v1/memory-model-configuration', responseMode: 'json' },
  getMemoryReindexStatus: { method: 'GET', pathTemplate: '/api/v1/memory-reindex', responseMode: 'json' },
  resumeMemoryReindex: { method: 'POST', pathTemplate: '/api/v1/memory-reindex/resume', responseMode: 'json' },
  listAgentMemoryPolicies: { method: 'GET', pathTemplate: '/api/v1/agents/{agent_profile_id}/memory-policies', responseMode: 'json' },
  createAgentMemoryPolicy: { method: 'POST', pathTemplate: '/api/v1/agents/{agent_profile_id}/memory-policies', responseMode: 'json' },
  attachAgentMemoryPolicy: { method: 'POST', pathTemplate: '/api/v1/agents/{agent_profile_id}/memory-policies/{policy_revision_id}/attach', responseMode: 'json' },
  getLiveness: { method: 'GET', pathTemplate: '/health/live', responseMode: 'json' },
  getReadiness: { method: 'GET', pathTemplate: '/health/ready', responseMode: 'json' },
} as const satisfies Readonly<Record<AuraOperationId, AuraOperationDescriptor>>;
