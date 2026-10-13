// Generated from contracts/openapi/aura-v1.yaml. Do not edit by hand.
// Contract SHA-256: 041a22fd63326a1dba5b7307125110fcb469652f098402f06a05a116b8e0c7c6

export type AuraBinaryRequestBody = Blob | ArrayBuffer | ArrayBufferView | ReadableStream<Uint8Array>;

export interface AuraBinaryResponse {
  readonly body: ReadableStream<Uint8Array>;
  readonly contentType: string;
  readonly contentLength: number;
  readonly contentDisposition: string;
}

export type Session = { readonly "principal": Principal; readonly "csrfToken": string; readonly "idleExpiresAt": string; readonly "absoluteExpiresAt": string; };

export type Principal = { readonly "issuer": string; readonly "subject": string; readonly "displayName"?: string | null; };

export type ModelCatalog = { readonly "models": ReadonlyArray<Model>; readonly "defaultModelId": string | null; readonly "observedAt": string; };

export type Model = { readonly "id": string; readonly "displayName": string; readonly "provider": string; readonly "capabilities": ReadonlyArray<"chat" | "completion" | "embedding" | "vision" | "tools" | "structured_output">; readonly "availability": "available" | "unavailable" | "unknown"; readonly "selectable": boolean; readonly "disabledReason": string | null; };

export type ToolCatalog = { readonly "items": ReadonlyArray<ToolDescriptorSummary>; };

export type ToolDescriptorSummary = { readonly "id": string; readonly "version": number; readonly "displayName": string; readonly "description": string; readonly "classification": ToolClassification; readonly "permissionScope": string; readonly "confirmation": ToolConfirmation; readonly "timeoutMs": number; readonly "retryLimit": number; readonly "idempotency": ToolIdempotency; readonly "sensitivity": ToolSensitivity; readonly "observableComponent": string; };

export type ToolClassification = "read" | "mutation";

export type ToolConfirmation = "never" | "policy" | "always";

export type ToolIdempotency = "read_only" | "idempotent" | "non_idempotent";

export type ToolSensitivity = "public" | "internal" | "private" | "restricted";

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

export type MemoryRecallMode = "off" | "automatic";

export type AgentMemoryPolicy = { readonly "id": string; readonly "agentProfileId": string; readonly "revision": number; readonly "recallMode": MemoryRecallMode; readonly "automaticRecallThreshold": number; readonly "sharedUserRead": boolean; readonly "currentAgentRead": boolean; readonly "sharedUserPromotion": boolean; readonly "fallbackRelevanceThreshold": number; readonly "maxMemories": number; readonly "contextBudgetFraction": number; readonly "fallbackAgentProfileIds": ReadonlyArray<string>; readonly "createdAt": string; };

export type CreateAgentMemoryPolicyRequest = { readonly "recallMode"?: MemoryRecallMode; readonly "automaticRecallThreshold"?: number; readonly "sharedUserRead": boolean; readonly "currentAgentRead": boolean; readonly "sharedUserPromotion": boolean; readonly "fallbackRelevanceThreshold": number; readonly "maxMemories": number; readonly "contextBudgetFraction": number; readonly "fallbackAgentProfileIds": ReadonlyArray<string>; readonly "expectedRevision": number; };

export type AttachAgentMemoryPolicyRequest = { readonly "expectedAgentVersion": number; };

export type AgentToolPolicyCollection = { readonly "items": ReadonlyArray<AgentToolPolicy>; readonly "attachedPolicyRevisionId": string; readonly "agentVersion": number; };

export type AgentToolPolicy = { readonly "id": string; readonly "agentProfileId": string; readonly "revision": number; readonly "grants": ReadonlyArray<ToolGrant>; readonly "maxCallsPerRun": number; readonly "timeoutMs": number; readonly "retryLimit": number; readonly "createdAt": string; };

export type ToolGrant = { readonly "toolId": string; readonly "toolVersion": number; };

export type CreateAgentToolPolicyRequest = { readonly "grants": ReadonlyArray<ToolGrant>; readonly "expectedRevision": number; };

export type AttachAgentToolPolicyRequest = { readonly "expectedAgentVersion": number; };

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

export type AgentRevision = { readonly "id": string; readonly "profileId": string; readonly "revision": number; readonly "displayName": string; readonly "purpose": string; readonly "instructions": string; readonly "personaRevisionId": string; readonly "promptBundleRevisionId": string; readonly "modelPolicyRevisionId": string; readonly "memoryPolicyRevisionId"?: string; readonly "toolPolicyRevisionId"?: string; readonly "createdAt": string; };

export type CreateAgentRequest = { readonly "displayName": string; readonly "purpose": string; readonly "instructions": string; readonly "personaRevisionId": string; readonly "initialToolGrants"?: ReadonlyArray<ToolGrant>; readonly "initialMemoryPolicy"?: InitialAgentMemoryPolicy; };

export type InitialAgentMemoryPolicy = { readonly "recallMode"?: MemoryRecallMode; readonly "automaticRecallThreshold"?: number; readonly "sharedUserRead": boolean; readonly "currentAgentRead": boolean; readonly "sharedUserPromotion": boolean; readonly "fallbackRelevanceThreshold": number; readonly "maxMemories": number; readonly "contextBudgetFraction": number; readonly "fallbackAgentProfileIds": ReadonlyArray<string>; };

export type CreateAgentRevisionRequest = { readonly "displayName": string; readonly "purpose": string; readonly "instructions": string; readonly "personaRevisionId": string; readonly "expectedVersion": number; };

export type UpdateAgentStatusRequest = { readonly "status": ProfileStatus; readonly "expectedVersion": number; };

export type PersonaCollection = { readonly "items": ReadonlyArray<PersonaProfile>; };

export type PersonaProfile = { readonly "id": string; readonly "status": ProfileStatus; readonly "version": number; readonly "currentRevision": PersonaRevision; readonly "assignedAgentCount": number; readonly "createdAt": string; readonly "updatedAt": string; };

export type PersonaDetail = { readonly "id": string; readonly "status": ProfileStatus; readonly "version": number; readonly "currentRevision": PersonaRevision; readonly "revisions": ReadonlyArray<PersonaRevision>; readonly "assignedAgentCount": number; readonly "assignedAgents": ReadonlyArray<PersonaAgentAssignment>; readonly "assignedAgentsTruncated": boolean; readonly "createdAt": string; readonly "updatedAt": string; };

export type PersonaAgentAssignment = { readonly "agentProfileId": string; readonly "displayName": string; readonly "status": ProfileStatus; readonly "currentRevisionNumber": number; };

export type PersonaRevision = { readonly "id": string; readonly "profileId": string; readonly "revision": number; readonly "displayName": string; readonly "description": string; readonly "instructions": string; readonly "createdAt": string; };

export type CreatePersonaRequest = { readonly "displayName": string; readonly "description": string; readonly "instructions": string; };

export type CreatePersonaRevisionRequest = { readonly "displayName": string; readonly "description": string; readonly "instructions": string; readonly "expectedVersion": number; };

export type UpdatePersonaStatusRequest = { readonly "status": ProfileStatus; readonly "expectedVersion": number; };

export type PersonaPreviewDraft = { readonly "displayName": string; readonly "description": string; readonly "instructions": string; };

export type PersonaDraftPreviewRequest = { readonly "modelId": string; readonly "prompt": string; readonly "draft": PersonaPreviewDraft; };

export type PersonaRevisionPreviewRequest = { readonly "modelId": string; readonly "prompt": string; readonly "personaRevisionId": string; };

export type PersonaPreviewRequest = PersonaDraftPreviewRequest | PersonaRevisionPreviewRequest;

export type PersonaPreviewResponse = { readonly "modelId": string; readonly "content": string; };

export type CreateConversationRequest = { readonly "message": string; readonly "modelId": string; readonly "agentRevisionId"?: string; readonly "personaRevisionId"?: string; readonly "clientTimeZone"?: ClientTimeZone; };

export type UpdateConversationRequest = { readonly "modelId"?: string; readonly "agentRevisionId"?: string; readonly "personaRevisionId"?: string; readonly "useAgentDefaultPersona"?: true; readonly "transcriptSharingConfirmed"?: boolean; readonly "version": number; };

export type UpdateConversationMetadataRequest = { readonly "title"?: string; readonly "archived"?: boolean; readonly "version": number; };

export type CreateRunRequest = { readonly "message": string; readonly "conversationVersion": number; readonly "clientTimeZone"?: ClientTimeZone; };

export type ConversationPage = { readonly "items": ReadonlyArray<ConversationSummary>; readonly "nextCursor": string | null; };

export type ConversationSummary = { readonly "id": string; readonly "title": string; readonly "agentProfileId": string; readonly "agentRevisionId": string; readonly "agent"?: AgentReference; readonly "agentAssignments"?: ReadonlyArray<AgentAssignment>; readonly "persona"?: PersonaReference; readonly "personaOverride"?: boolean; readonly "personaAssignments"?: ReadonlyArray<PersonaAssignment>; readonly "modelId": string; readonly "version": number; readonly "createdAt": string; readonly "updatedAt": string; readonly "archivedAt"?: string | null; readonly "currentRun": Run | null; };

export type AgentReference = { readonly "profileId": string; readonly "revisionId": string; readonly "displayName": string; readonly "revision": number; readonly "status": ProfileStatus; readonly "newerRevisionAvailable": boolean; };

export type AgentAssignment = { readonly "id": string; readonly "agent": AgentReference; readonly "reason": "initial" | "manual_switch" | "revision_upgrade"; readonly "effectiveAfterMessageId": string | null; readonly "createdAt": string; };

export type PersonaReference = { readonly "profileId": string; readonly "revisionId": string; readonly "displayName": string; readonly "revision": number; readonly "status": ProfileStatus; readonly "newerRevisionAvailable": boolean; };

export type PersonaAssignment = { readonly "id": string; readonly "persona": PersonaReference; readonly "source": "agent_default" | "conversation_override"; readonly "reason": "initial" | "agent_switch" | "agent_revision_upgrade" | "manual_override" | "reset_to_agent_default"; readonly "effectiveAfterMessageId": string | null; readonly "createdAt": string; };

export type ConversationDetail = ConversationSummary & { readonly "messages": ReadonlyArray<Message>; readonly "recentRuns": ReadonlyArray<Run>; };

export type Message = { readonly "id": string; readonly "conversationId": string; readonly "role": "user" | "assistant"; readonly "content": string; readonly "state": "complete" | "partial" | "interrupted" | "failed"; readonly "runId": string | null; readonly "createdAt": string; readonly "updatedAt": string; };

export type Run = { readonly "id": string; readonly "conversationId": string; readonly "userMessageId": string; readonly "assistantMessageId": string | null; readonly "status": RunStatus; readonly "agentRevisionId": string; readonly "modelPolicyRevisionId": string; readonly "toolPolicyRevisionId"?: string; readonly "toolCallLimit"?: number; readonly "toolCallsUsed"?: number; readonly "clientTimeZone"?: ClientTimeZone | null; readonly "approvalId"?: string | null; readonly "pendingToolCallId"?: string | null; readonly "toolLifecycleStatus"?: RunToolLifecycleStatus | null; readonly "personaRevisionId"?: string; readonly "promptBundleRevisionId"?: string; readonly "promptHash"?: string; readonly "provider": string; readonly "modelId": string; readonly "retryOfRunId": string | null; readonly "createdAt": string; readonly "startedAt": string | null; readonly "finishedAt": string | null; readonly "error": RunError | null; };

export type RunStatus = "queued" | "running" | "waiting_for_confirmation" | "cancel_requested" | "canceled" | "completed" | "failed" | "interrupted";

export type ClientTimeZone = string;

export type RunToolLifecycleStatus = "awaiting_confirmation" | "approved" | "denied" | "executing" | "completed" | "failed";

export type ConversationArchiveState = "active" | "archived" | "all";

export type RunError = { readonly "code": string; readonly "message": string; readonly "retryable": boolean; readonly "traceId": string; };

export type ConversationRunAccepted = { readonly "conversation": ConversationSummary; readonly "userMessage": Message; readonly "run": Run; };

export type RunEvent = RunSnapshotEvent | RunStatusEvent | AssistantDeltaEvent | ModelThinkingDeltaEvent | AssistantSnapshotEvent | RunErrorEvent | MemoryActivityEvent | ToolStatusEvent | ApprovalStatusEvent | HeartbeatEvent;

export type RunEventBase = { readonly "schemaVersion": 1; readonly "eventId": string; readonly "sequence": number; readonly "eventType": string; readonly "runId": string; readonly "conversationId": string; readonly "occurredAt": string; };

export type RunSnapshotEvent = RunEventBase & { readonly "eventType": "run.snapshot"; readonly "data": RunSnapshotData; };

export type RunStatusEvent = RunEventBase & { readonly "eventType": "run.status"; readonly "data": RunStatusData; };

export type AssistantDeltaEvent = RunEventBase & { readonly "eventType": "assistant.delta"; readonly "data": AssistantDeltaData; };

export type ModelThinkingDeltaEvent = RunEventBase & { readonly "eventType": "model.thinking.delta"; readonly "data": ModelThinkingDeltaData; };

export type AssistantSnapshotEvent = RunEventBase & { readonly "eventType": "assistant.snapshot"; readonly "data": AssistantSnapshotData; };

export type RunErrorEvent = RunEventBase & { readonly "eventType": "run.error"; readonly "data": RunError; };

export type MemoryActivityEvent = RunEventBase & { readonly "eventType": "memory.activity"; readonly "data": MemoryActivity; };

export type ToolStatusEvent = RunEventBase & { readonly "eventType": "tool.status"; readonly "data": ToolStatusData; };

export type ApprovalStatusEvent = RunEventBase & { readonly "eventType": "approval.status"; readonly "data": ApprovalStatusData; };

export type HeartbeatEvent = RunEventBase & { readonly "eventType": "heartbeat"; readonly "data": { readonly "serverTime": string; }; };

export type RunSnapshotData = { readonly "run": Run; readonly "assistantMessage": Message | null; };

export type RunStatusData = { readonly "status": RunStatus; readonly "startedAt": string | null; readonly "finishedAt": string | null; };

export type AssistantDeltaData = { readonly "messageId": string; readonly "offset": number; readonly "text": string; };

export type ModelThinkingDeltaData = { readonly "offset": number; readonly "text": string; };

export type AssistantSnapshotData = { readonly "message": Message; };

export type ToolStatus = "pending" | "awaiting_confirmation" | "executing" | "completed" | "denied" | "failed";

export type ToolStatusData = { readonly "callId": string; readonly "toolId": string; readonly "toolVersion": number; readonly "status": ToolStatus; readonly "errorClass"?: "capability" | "validation" | "policy" | "budget" | "timeout" | "provider_protocol" | "execution" | "cancellation"; readonly "errorCode"?: string; readonly "resultCount"?: number; };

export type ApprovalStatusData = { readonly "approvalId": string; readonly "proposalId": string; readonly "runId": string; readonly "conversationId": string; readonly "status": ApprovalStatus; readonly "executionStatus": ApprovalExecutionStatus; readonly "version": number; };

export type ReminderStatus = "scheduled" | "fired" | "completed" | "canceled";

export type ReminderListView = "upcoming" | "history";

export type Reminder = { readonly "id": string; readonly "content": string; readonly "dueAt": string; readonly "status": ReminderStatus; readonly "version": number; readonly "createdAt": string; readonly "updatedAt": string; readonly "firedAt": string | null; readonly "completedAt": string | null; readonly "canceledAt": string | null; };

export type ReminderPage = { readonly "items": ReadonlyArray<Reminder>; readonly "nextCursor": string | null; };

export type CreateReminderRequest = { readonly "content": string; readonly "dueAt": string; };

export type ReminderTransitionRequest = { readonly "expectedVersion": number; };

export type RecurrenceFrequency = "daily" | "selected_weekday" | "weekly" | "monthly";

export type RecurrenceRule = { readonly "frequency": "daily" | "selected_weekday" | "weekly" | "monthly"; readonly "interval": number; readonly "weekdays"?: ReadonlyArray<number>; readonly "monthDay"?: number | null; };

export type ReminderSeriesRecurrence = { readonly "frequency": "daily" | "selected_weekday" | "weekly" | "monthly"; readonly "interval": number; readonly "weekdays": ReadonlyArray<number>; readonly "monthDay": number | null; readonly "localStart": string; readonly "timeZone": ClientTimeZone; readonly "count": number | null; readonly "endDate": string | null; };

export type ReminderSeriesStatus = "active" | "paused" | "completed" | "canceled";

export type ReminderOccurrenceStatus = "scheduled" | "fired" | "completed" | "skipped" | "replaced" | "paused-window" | "series-canceled";

export type ReminderSeries = { readonly "id": string; readonly "content": string; readonly "recurrence": ReminderSeriesRecurrence; readonly "timeZone": ClientTimeZone; readonly "localStart": string; readonly "status": ReminderSeriesStatus; readonly "version": number; readonly "readableRule": string; readonly "nextOccurrence": string | null; readonly "nextReminderId": string | null; readonly "missedCount": number; readonly "occurrenceCount": number; readonly "count": number | null; readonly "endDate": string | null; readonly "createdAt": string; readonly "updatedAt": string; readonly "pausedAt": string | null; readonly "completedAt": string | null; readonly "canceledAt": string | null; };

export type ReminderSeriesPage = { readonly "items": ReadonlyArray<ReminderSeries>; readonly "nextCursor": string | null; };

export type ReminderOccurrence = { readonly "id": string; readonly "seriesId": string; readonly "reminderId": string; readonly "sequence": number; readonly "outcome": "scheduled" | "fired" | "completed" | "skipped" | "replaced" | "paused-window" | "series-canceled"; readonly "localDueAt": string; readonly "dueAt": string; readonly "occurredAt": string | null; readonly "createdAt": string; readonly "updatedAt": string; readonly "skippedAt": string | null; readonly "firedAt": string | null; readonly "completedAt": string | null; };

export type ReminderOccurrencePage = { readonly "items": ReadonlyArray<ReminderOccurrence>; readonly "nextCursor": string | null; };

export type CreateReminderSeriesRequest = { readonly "content": string; readonly "recurrence": ReminderSeriesRecurrence; };

export type UpdateReminderSeriesRequest = { readonly "expectedVersion": number; readonly "content"?: string; readonly "recurrence"?: ReminderSeriesRecurrence; };

export type ReminderSeriesTransitionRequest = { readonly "expectedVersion": number; };

export type ApprovalStatus = "pending" | "approved" | "rejected" | "expired" | "canceled";

export type ApprovalExecutionStatus = "not_started" | "queued" | "executing" | "completed" | "failed" | "denied";

export type ReminderApprovalToolId = "reminder.create" | "reminder.series.create" | "reminder.reschedule" | "reminder.series.update" | "reminder.series.pause" | "reminder.series.resume" | "reminder.cancel";

export type Approval = { readonly "id": string; readonly "proposalId": string; readonly "runId": string; readonly "conversationId": string; readonly "toolCallId": string; readonly "toolId": ReminderApprovalToolId; readonly "toolVersion": 1; readonly "status": ApprovalStatus; readonly "executionStatus": ApprovalExecutionStatus; readonly "version": number; readonly "expiresAt": string; readonly "createdAt": string; readonly "updatedAt": string; readonly "decidedAt": string | null; };

export type ReminderProposalDetail = { readonly "id": string; readonly "content": string; readonly "dueAt": string; };

export type ReminderCommandProposalDetail = { readonly "id": string; readonly "operation": "series.create" | "reschedule" | "series.update" | "series.pause" | "series.resume" | "cancel"; readonly "summary": string; readonly "targetType": "series" | "reminder" | "occurrence" | null; readonly "targetId": string | null; readonly "expectedVersion": number | null; };

export type ApprovalDetail = Approval & { readonly "proposal": ReminderProposalDetail | ReminderCommandProposalDetail; };

export type ApprovalPage = { readonly "items": ReadonlyArray<Approval>; readonly "nextCursor": string | null; };

export type ApprovalDecisionRequest = { readonly "expectedVersion": number; };

export type ApprovalStreamEvent = ApprovalEvent | ApprovalHeartbeatEvent;

export type ApprovalEvent = { readonly "schemaVersion": 1; readonly "eventId": string; readonly "eventType": "approval.pending" | "approval.approved" | "approval.rejected" | "approval.expired" | "approval.canceled"; readonly "approvalId": string; readonly "proposalId": string; readonly "runId": string; readonly "conversationId": string; readonly "toolId": ReminderApprovalToolId; readonly "toolVersion": 1; readonly "status": ApprovalStatus; readonly "executionStatus": ApprovalExecutionStatus; readonly "version": number; readonly "occurredAt": string; };

export type ApprovalHeartbeatEvent = { readonly "schemaVersion": 1; readonly "eventType": "heartbeat"; readonly "occurredAt": string; };

export type NotificationStatus = "delivered" | "acknowledged";

export type Notification = { readonly "id": string; readonly "reminderId": string; readonly "status": NotificationStatus; readonly "version": number; readonly "createdAt": string; readonly "updatedAt": string; readonly "deliveredAt": string; readonly "acknowledgedAt": string | null; };

export type NotificationPage = { readonly "items": ReadonlyArray<Notification>; readonly "nextCursor": string | null; readonly "unacknowledgedCount": number; };

export type NotificationAcknowledgeRequest = { readonly "expectedVersion": number; };

export type NotificationStreamEvent = NotificationLifecycleEvent | NotificationHeartbeatEvent;

export type NotificationLifecycleEvent = { readonly "schemaVersion": 1; readonly "eventId": string; readonly "eventType": "notification.delivered" | "notification.acknowledged"; readonly "notificationId": string; readonly "reminderId": string; readonly "status": NotificationStatus; readonly "notificationVersion": number; readonly "occurredAt": string; };

export type NotificationHeartbeatEvent = { readonly "schemaVersion": 1; readonly "eventType": "heartbeat"; readonly "occurredAt": string; };

export type DocumentStatus = "uploading" | "queued" | "processing" | "ready" | "failed" | "deleting";

export type DocumentMediaType = "application/pdf" | "application/vnd.openxmlformats-officedocument.wordprocessingml.document" | "text/markdown" | "text/plain";

export type DocumentFailure = { readonly "code": string; readonly "retryable": boolean; };

export type CreateDocumentUploadRequest = { readonly "filename": string; readonly "mediaType": DocumentMediaType; readonly "byteCount": number; };

export type DocumentUploadSession = { readonly "id": string; readonly "documentId": string; readonly "filename": string; readonly "mediaType": DocumentMediaType; readonly "byteCount": number; readonly "status": "uploading"; readonly "version": number; readonly "createdAt": string; readonly "expiresAt": string; };

export type DocumentPage = { readonly "items": ReadonlyArray<DocumentSummary>; readonly "nextCursor": string | null; };

export type DocumentSummary = { readonly "id": string; readonly "filename": string; readonly "mediaType": DocumentMediaType; readonly "byteCount": number; readonly "status": DocumentStatus; readonly "failure": DocumentFailure | null; readonly "version": number; readonly "createdAt": string; readonly "updatedAt": string; };

export type DocumentDetail = { readonly "id": string; readonly "filename": string; readonly "mediaType": DocumentMediaType; readonly "byteCount": number; readonly "status": DocumentStatus; readonly "failure": DocumentFailure | null; readonly "version": number; readonly "extractedTextLength": number; readonly "chunkCount": number; readonly "createdAt": string; readonly "updatedAt": string; };

export type DocumentUploadReceipt = { readonly "document": DocumentDetail; readonly "duplicate": boolean; };

export type SearchDocumentsRequest = { readonly "query": string; readonly "cursor"?: string; readonly "limit"?: number; readonly "mediaType"?: DocumentMediaType; };

export type DocumentPageLocator = { readonly "kind": "page"; readonly "pageNumber": number; };

export type DocumentSectionLocator = { readonly "kind": "section"; readonly "sectionOrdinal": number; readonly "heading": string | null; };

export type DocumentLocator = DocumentPageLocator | DocumentSectionLocator;

export type DocumentSearchResult = { readonly "evidenceId": string; readonly "documentId": string; readonly "displayName": string; readonly "excerpt": string; readonly "locator": DocumentLocator; readonly "score": number; };

export type DocumentSearchResultPage = { readonly "items": ReadonlyArray<DocumentSearchResult>; readonly "nextCursor": string | null; };

export type DocumentTransitionRequest = { readonly "expectedVersion": number; };

export type DocumentDeletionReceipt = { readonly "documentId": string; readonly "status": "deleting"; readonly "version": number; readonly "acceptedAt": string; };

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
  readonly listTools: { readonly response: ToolCatalog; readonly body: never; readonly path: Readonly<Record<string, never>>; readonly query: Readonly<Record<string, never>>; readonly headers: Readonly<Record<string, never>>; };
  readonly listAgents: { readonly response: AgentCollection; readonly body: never; readonly path: Readonly<Record<string, never>>; readonly query: Readonly<Record<string, never>>; readonly headers: Readonly<Record<string, never>>; };
  readonly createAgent: { readonly response: AgentDetail; readonly body: CreateAgentRequest; readonly path: Readonly<Record<string, never>>; readonly query: Readonly<Record<string, never>>; readonly headers: { readonly "X-CSRF-Token": string; readonly "Idempotency-Key": string; }; };
  readonly getAgent: { readonly response: AgentDetail; readonly body: never; readonly path: { readonly "agent_profile_id": string; }; readonly query: Readonly<Record<string, never>>; readonly headers: Readonly<Record<string, never>>; };
  readonly updateAgentStatus: { readonly response: AgentDetail; readonly body: UpdateAgentStatusRequest; readonly path: { readonly "agent_profile_id": string; }; readonly query: Readonly<Record<string, never>>; readonly headers: { readonly "X-CSRF-Token": string; readonly "Idempotency-Key": string; }; };
  readonly createAgentRevision: { readonly response: AgentDetail; readonly body: CreateAgentRevisionRequest; readonly path: { readonly "agent_profile_id": string; }; readonly query: Readonly<Record<string, never>>; readonly headers: { readonly "X-CSRF-Token": string; readonly "Idempotency-Key": string; }; };
  readonly listPersonas: { readonly response: PersonaCollection; readonly body: never; readonly path: Readonly<Record<string, never>>; readonly query: Readonly<Record<string, never>>; readonly headers: Readonly<Record<string, never>>; };
  readonly createPersona: { readonly response: PersonaDetail; readonly body: CreatePersonaRequest; readonly path: Readonly<Record<string, never>>; readonly query: Readonly<Record<string, never>>; readonly headers: { readonly "X-CSRF-Token": string; readonly "Idempotency-Key": string; }; };
  readonly previewPersona: { readonly response: PersonaPreviewResponse; readonly body: PersonaPreviewRequest; readonly path: Readonly<Record<string, never>>; readonly query: Readonly<Record<string, never>>; readonly headers: { readonly "X-CSRF-Token": string; }; };
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
  readonly listAgentToolPolicies: { readonly response: AgentToolPolicyCollection; readonly body: never; readonly path: { readonly "agent_profile_id": string; }; readonly query: Readonly<Record<string, never>>; readonly headers: Readonly<Record<string, never>>; };
  readonly createAgentToolPolicy: { readonly response: AgentToolPolicy; readonly body: CreateAgentToolPolicyRequest; readonly path: { readonly "agent_profile_id": string; }; readonly query: Readonly<Record<string, never>>; readonly headers: { readonly "X-CSRF-Token": string; readonly "Idempotency-Key": string; }; };
  readonly attachAgentToolPolicy: { readonly response: AgentDetail; readonly body: AttachAgentToolPolicyRequest; readonly path: { readonly "agent_profile_id": string; readonly "tool_policy_revision_id": string; }; readonly query: Readonly<Record<string, never>>; readonly headers: { readonly "X-CSRF-Token": string; readonly "Idempotency-Key": string; }; };
  readonly listReminders: { readonly response: ReminderPage; readonly body: never; readonly path: Readonly<Record<string, never>>; readonly query: { readonly "view"?: ReminderListView; readonly "cursor"?: string; readonly "limit"?: number; }; readonly headers: Readonly<Record<string, never>>; };
  readonly createReminder: { readonly response: Reminder; readonly body: CreateReminderRequest; readonly path: Readonly<Record<string, never>>; readonly query: Readonly<Record<string, never>>; readonly headers: { readonly "X-CSRF-Token": string; readonly "Idempotency-Key": string; }; };
  readonly getReminder: { readonly response: Reminder; readonly body: never; readonly path: { readonly "reminder_id": string; }; readonly query: Readonly<Record<string, never>>; readonly headers: Readonly<Record<string, never>>; };
  readonly completeReminder: { readonly response: Reminder; readonly body: ReminderTransitionRequest; readonly path: { readonly "reminder_id": string; }; readonly query: Readonly<Record<string, never>>; readonly headers: { readonly "X-CSRF-Token": string; readonly "Idempotency-Key": string; }; };
  readonly cancelReminder: { readonly response: Reminder; readonly body: ReminderTransitionRequest; readonly path: { readonly "reminder_id": string; }; readonly query: Readonly<Record<string, never>>; readonly headers: { readonly "X-CSRF-Token": string; readonly "Idempotency-Key": string; }; };
  readonly listReminderSeries: { readonly response: ReminderSeriesPage; readonly body: never; readonly path: Readonly<Record<string, never>>; readonly query: { readonly "cursor"?: string; readonly "limit"?: number; }; readonly headers: Readonly<Record<string, never>>; };
  readonly createReminderSeries: { readonly response: ReminderSeries; readonly body: CreateReminderSeriesRequest; readonly path: Readonly<Record<string, never>>; readonly query: Readonly<Record<string, never>>; readonly headers: { readonly "X-CSRF-Token": string; readonly "Idempotency-Key": string; }; };
  readonly getReminderSeries: { readonly response: ReminderSeries; readonly body: never; readonly path: { readonly "series_id": string; }; readonly query: Readonly<Record<string, never>>; readonly headers: Readonly<Record<string, never>>; };
  readonly listReminderSeriesOccurrences: { readonly response: ReminderOccurrencePage; readonly body: never; readonly path: { readonly "series_id": string; }; readonly query: { readonly "cursor"?: string; readonly "limit"?: number; }; readonly headers: Readonly<Record<string, never>>; };
  readonly updateReminderSeries: { readonly response: ReminderSeries; readonly body: UpdateReminderSeriesRequest; readonly path: { readonly "series_id": string; }; readonly query: Readonly<Record<string, never>>; readonly headers: { readonly "X-CSRF-Token": string; readonly "Idempotency-Key": string; }; };
  readonly pauseReminderSeries: { readonly response: ReminderSeries; readonly body: ReminderSeriesTransitionRequest; readonly path: { readonly "series_id": string; }; readonly query: Readonly<Record<string, never>>; readonly headers: { readonly "X-CSRF-Token": string; readonly "Idempotency-Key": string; }; };
  readonly resumeReminderSeries: { readonly response: ReminderSeries; readonly body: ReminderSeriesTransitionRequest; readonly path: { readonly "series_id": string; }; readonly query: Readonly<Record<string, never>>; readonly headers: { readonly "X-CSRF-Token": string; readonly "Idempotency-Key": string; }; };
  readonly skipNextReminderSeriesOccurrence: { readonly response: ReminderSeries; readonly body: ReminderSeriesTransitionRequest; readonly path: { readonly "series_id": string; }; readonly query: Readonly<Record<string, never>>; readonly headers: { readonly "X-CSRF-Token": string; readonly "Idempotency-Key": string; }; };
  readonly cancelReminderSeries: { readonly response: ReminderSeries; readonly body: ReminderSeriesTransitionRequest; readonly path: { readonly "series_id": string; }; readonly query: Readonly<Record<string, never>>; readonly headers: { readonly "X-CSRF-Token": string; readonly "Idempotency-Key": string; }; };
  readonly listApprovals: { readonly response: ApprovalPage; readonly body: never; readonly path: Readonly<Record<string, never>>; readonly query: { readonly "status"?: ApprovalStatus; readonly "cursor"?: string; readonly "limit"?: number; }; readonly headers: Readonly<Record<string, never>>; };
  readonly getApproval: { readonly response: ApprovalDetail; readonly body: never; readonly path: { readonly "approval_id": string; }; readonly query: Readonly<Record<string, never>>; readonly headers: Readonly<Record<string, never>>; };
  readonly approveApproval: { readonly response: Approval; readonly body: ApprovalDecisionRequest; readonly path: { readonly "approval_id": string; }; readonly query: Readonly<Record<string, never>>; readonly headers: { readonly "X-CSRF-Token": string; readonly "Idempotency-Key": string; }; };
  readonly rejectApproval: { readonly response: Approval; readonly body: ApprovalDecisionRequest; readonly path: { readonly "approval_id": string; }; readonly query: Readonly<Record<string, never>>; readonly headers: { readonly "X-CSRF-Token": string; readonly "Idempotency-Key": string; }; };
  readonly streamApprovals: { readonly response: ApprovalStreamEvent; readonly body: never; readonly path: Readonly<Record<string, never>>; readonly query: Readonly<Record<string, never>>; readonly headers: { readonly "Last-Event-ID"?: string; }; };
  readonly listNotifications: { readonly response: NotificationPage; readonly body: never; readonly path: Readonly<Record<string, never>>; readonly query: { readonly "status"?: NotificationStatus; readonly "cursor"?: string; readonly "limit"?: number; }; readonly headers: Readonly<Record<string, never>>; };
  readonly getNotification: { readonly response: Notification; readonly body: never; readonly path: { readonly "notification_id": string; }; readonly query: Readonly<Record<string, never>>; readonly headers: Readonly<Record<string, never>>; };
  readonly acknowledgeNotification: { readonly response: Notification; readonly body: NotificationAcknowledgeRequest; readonly path: { readonly "notification_id": string; }; readonly query: Readonly<Record<string, never>>; readonly headers: { readonly "X-CSRF-Token": string; readonly "Idempotency-Key": string; }; };
  readonly streamNotificationEvents: { readonly response: NotificationStreamEvent; readonly body: never; readonly path: Readonly<Record<string, never>>; readonly query: Readonly<Record<string, never>>; readonly headers: { readonly "Last-Event-ID"?: string; }; };
  readonly createDocumentUpload: { readonly response: DocumentUploadSession; readonly body: CreateDocumentUploadRequest; readonly path: Readonly<Record<string, never>>; readonly query: Readonly<Record<string, never>>; readonly headers: { readonly "X-CSRF-Token": string; readonly "Idempotency-Key": string; }; };
  readonly uploadDocumentContent: { readonly response: DocumentUploadReceipt; readonly body: AuraBinaryRequestBody; readonly path: { readonly "upload_id": string; }; readonly query: Readonly<Record<string, never>>; readonly headers: { readonly "X-CSRF-Token": string; readonly "Idempotency-Key": string; readonly "X-Expected-Version": string; readonly "Content-Length": string; readonly "Content-Type": string; }; };
  readonly listDocuments: { readonly response: DocumentPage; readonly body: never; readonly path: Readonly<Record<string, never>>; readonly query: { readonly "cursor"?: string; readonly "limit"?: number; readonly "status"?: DocumentStatus; readonly "mediaType"?: DocumentMediaType; }; readonly headers: Readonly<Record<string, never>>; };
  readonly searchDocuments: { readonly response: DocumentSearchResultPage; readonly body: SearchDocumentsRequest; readonly path: Readonly<Record<string, never>>; readonly query: Readonly<Record<string, never>>; readonly headers: { readonly "X-CSRF-Token": string; }; };
  readonly getDocument: { readonly response: DocumentDetail; readonly body: never; readonly path: { readonly "document_id": string; }; readonly query: Readonly<Record<string, never>>; readonly headers: Readonly<Record<string, never>>; };
  readonly deleteDocument: { readonly response: DocumentDeletionReceipt; readonly body: DocumentTransitionRequest; readonly path: { readonly "document_id": string; }; readonly query: Readonly<Record<string, never>>; readonly headers: { readonly "X-CSRF-Token": string; readonly "Idempotency-Key": string; }; };
  readonly downloadDocument: { readonly response: AuraBinaryResponse; readonly body: never; readonly path: { readonly "document_id": string; }; readonly query: Readonly<Record<string, never>>; readonly headers: Readonly<Record<string, never>>; };
  readonly retryDocumentProcessing: { readonly response: DocumentDetail; readonly body: DocumentTransitionRequest; readonly path: { readonly "document_id": string; }; readonly query: Readonly<Record<string, never>>; readonly headers: { readonly "X-CSRF-Token": string; readonly "Idempotency-Key": string; }; };
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
  readonly requestMode: 'none' | 'json' | 'binary';
  readonly responseMode: 'json' | 'event-stream' | 'binary';
}

export const AURA_API_OPERATIONS = {
  getSession: { method: 'GET', pathTemplate: '/api/v1/auth/session', requestMode: 'none', responseMode: 'json' },
  startLogin: { method: 'GET', pathTemplate: '/api/v1/auth/login', requestMode: 'none', responseMode: 'json' },
  completeLogin: { method: 'GET', pathTemplate: '/api/v1/auth/callback', requestMode: 'none', responseMode: 'json' },
  logout: { method: 'POST', pathTemplate: '/api/v1/auth/logout', requestMode: 'none', responseMode: 'json' },
  listModels: { method: 'GET', pathTemplate: '/api/v1/models', requestMode: 'none', responseMode: 'json' },
  listTools: { method: 'GET', pathTemplate: '/api/v1/tools', requestMode: 'none', responseMode: 'json' },
  listAgents: { method: 'GET', pathTemplate: '/api/v1/agents', requestMode: 'none', responseMode: 'json' },
  createAgent: { method: 'POST', pathTemplate: '/api/v1/agents', requestMode: 'json', responseMode: 'json' },
  getAgent: { method: 'GET', pathTemplate: '/api/v1/agents/{agent_profile_id}', requestMode: 'none', responseMode: 'json' },
  updateAgentStatus: { method: 'PATCH', pathTemplate: '/api/v1/agents/{agent_profile_id}', requestMode: 'json', responseMode: 'json' },
  createAgentRevision: { method: 'POST', pathTemplate: '/api/v1/agents/{agent_profile_id}/revisions', requestMode: 'json', responseMode: 'json' },
  listPersonas: { method: 'GET', pathTemplate: '/api/v1/personas', requestMode: 'none', responseMode: 'json' },
  createPersona: { method: 'POST', pathTemplate: '/api/v1/personas', requestMode: 'json', responseMode: 'json' },
  previewPersona: { method: 'POST', pathTemplate: '/api/v1/personas/preview', requestMode: 'json', responseMode: 'json' },
  getPersona: { method: 'GET', pathTemplate: '/api/v1/personas/{persona_profile_id}', requestMode: 'none', responseMode: 'json' },
  updatePersonaStatus: { method: 'PATCH', pathTemplate: '/api/v1/personas/{persona_profile_id}', requestMode: 'json', responseMode: 'json' },
  createPersonaRevision: { method: 'POST', pathTemplate: '/api/v1/personas/{persona_profile_id}/revisions', requestMode: 'json', responseMode: 'json' },
  listConversations: { method: 'GET', pathTemplate: '/api/v1/conversations', requestMode: 'none', responseMode: 'json' },
  createConversation: { method: 'POST', pathTemplate: '/api/v1/conversations', requestMode: 'json', responseMode: 'json' },
  getConversation: { method: 'GET', pathTemplate: '/api/v1/conversations/{conversation_id}', requestMode: 'none', responseMode: 'json' },
  updateConversation: { method: 'PATCH', pathTemplate: '/api/v1/conversations/{conversation_id}', requestMode: 'json', responseMode: 'json' },
  createRun: { method: 'POST', pathTemplate: '/api/v1/conversations/{conversation_id}/runs', requestMode: 'json', responseMode: 'json' },
  updateConversationMetadata: { method: 'PATCH', pathTemplate: '/api/v1/conversations/{conversation_id}/metadata', requestMode: 'json', responseMode: 'json' },
  cancelRun: { method: 'POST', pathTemplate: '/api/v1/runs/{run_id}/cancel', requestMode: 'none', responseMode: 'json' },
  retryRun: { method: 'POST', pathTemplate: '/api/v1/runs/{run_id}/retry', requestMode: 'none', responseMode: 'json' },
  streamRunEvents: { method: 'GET', pathTemplate: '/api/v1/runs/{run_id}/events', requestMode: 'none', responseMode: 'event-stream' },
  getRunMemoryActivity: { method: 'GET', pathTemplate: '/api/v1/runs/{run_id}/memory-activity', requestMode: 'none', responseMode: 'json' },
  listMemories: { method: 'GET', pathTemplate: '/api/v1/memories', requestMode: 'none', responseMode: 'json' },
  createMemory: { method: 'POST', pathTemplate: '/api/v1/memories', requestMode: 'json', responseMode: 'json' },
  searchMemories: { method: 'POST', pathTemplate: '/api/v1/memories/search', requestMode: 'json', responseMode: 'json' },
  getMemory: { method: 'GET', pathTemplate: '/api/v1/memories/{memory_id}', requestMode: 'none', responseMode: 'json' },
  correctMemory: { method: 'POST', pathTemplate: '/api/v1/memories/{memory_id}/revisions', requestMode: 'json', responseMode: 'json' },
  updateMemoryStatus: { method: 'PATCH', pathTemplate: '/api/v1/memories/{memory_id}/status', requestMode: 'json', responseMode: 'json' },
  updateMemoryPin: { method: 'PATCH', pathTemplate: '/api/v1/memories/{memory_id}/pin', requestMode: 'json', responseMode: 'json' },
  purgeMemory: { method: 'POST', pathTemplate: '/api/v1/memories/{memory_id}/purge', requestMode: 'json', responseMode: 'json' },
  listMemoryCandidates: { method: 'GET', pathTemplate: '/api/v1/memory-candidates', requestMode: 'none', responseMode: 'json' },
  getMemoryCandidate: { method: 'GET', pathTemplate: '/api/v1/memory-candidates/{candidate_id}', requestMode: 'none', responseMode: 'json' },
  approveMemoryCandidate: { method: 'POST', pathTemplate: '/api/v1/memory-candidates/{candidate_id}/approve', requestMode: 'json', responseMode: 'json' },
  rejectMemoryCandidate: { method: 'POST', pathTemplate: '/api/v1/memory-candidates/{candidate_id}/reject', requestMode: 'json', responseMode: 'json' },
  getMemoryModelInventory: { method: 'GET', pathTemplate: '/api/v1/memory-model-inventory', requestMode: 'none', responseMode: 'json' },
  getMemoryModelConfiguration: { method: 'GET', pathTemplate: '/api/v1/memory-model-configuration', requestMode: 'none', responseMode: 'json' },
  updateMemoryModelConfiguration: { method: 'PUT', pathTemplate: '/api/v1/memory-model-configuration', requestMode: 'json', responseMode: 'json' },
  getMemoryReindexStatus: { method: 'GET', pathTemplate: '/api/v1/memory-reindex', requestMode: 'none', responseMode: 'json' },
  resumeMemoryReindex: { method: 'POST', pathTemplate: '/api/v1/memory-reindex/resume', requestMode: 'json', responseMode: 'json' },
  listAgentMemoryPolicies: { method: 'GET', pathTemplate: '/api/v1/agents/{agent_profile_id}/memory-policies', requestMode: 'none', responseMode: 'json' },
  createAgentMemoryPolicy: { method: 'POST', pathTemplate: '/api/v1/agents/{agent_profile_id}/memory-policies', requestMode: 'json', responseMode: 'json' },
  attachAgentMemoryPolicy: { method: 'POST', pathTemplate: '/api/v1/agents/{agent_profile_id}/memory-policies/{policy_revision_id}/attach', requestMode: 'json', responseMode: 'json' },
  listAgentToolPolicies: { method: 'GET', pathTemplate: '/api/v1/agents/{agent_profile_id}/tool-policies', requestMode: 'none', responseMode: 'json' },
  createAgentToolPolicy: { method: 'POST', pathTemplate: '/api/v1/agents/{agent_profile_id}/tool-policies', requestMode: 'json', responseMode: 'json' },
  attachAgentToolPolicy: { method: 'POST', pathTemplate: '/api/v1/agents/{agent_profile_id}/tool-policies/{tool_policy_revision_id}/attach', requestMode: 'json', responseMode: 'json' },
  listReminders: { method: 'GET', pathTemplate: '/api/v1/reminders', requestMode: 'none', responseMode: 'json' },
  createReminder: { method: 'POST', pathTemplate: '/api/v1/reminders', requestMode: 'json', responseMode: 'json' },
  getReminder: { method: 'GET', pathTemplate: '/api/v1/reminders/{reminder_id}', requestMode: 'none', responseMode: 'json' },
  completeReminder: { method: 'POST', pathTemplate: '/api/v1/reminders/{reminder_id}/complete', requestMode: 'json', responseMode: 'json' },
  cancelReminder: { method: 'POST', pathTemplate: '/api/v1/reminders/{reminder_id}/cancel', requestMode: 'json', responseMode: 'json' },
  listReminderSeries: { method: 'GET', pathTemplate: '/api/v1/reminder-series', requestMode: 'none', responseMode: 'json' },
  createReminderSeries: { method: 'POST', pathTemplate: '/api/v1/reminder-series', requestMode: 'json', responseMode: 'json' },
  getReminderSeries: { method: 'GET', pathTemplate: '/api/v1/reminder-series/{series_id}', requestMode: 'none', responseMode: 'json' },
  listReminderSeriesOccurrences: { method: 'GET', pathTemplate: '/api/v1/reminder-series/{series_id}/occurrences', requestMode: 'none', responseMode: 'json' },
  updateReminderSeries: { method: 'POST', pathTemplate: '/api/v1/reminder-series/{series_id}/update', requestMode: 'json', responseMode: 'json' },
  pauseReminderSeries: { method: 'POST', pathTemplate: '/api/v1/reminder-series/{series_id}/pause', requestMode: 'json', responseMode: 'json' },
  resumeReminderSeries: { method: 'POST', pathTemplate: '/api/v1/reminder-series/{series_id}/resume', requestMode: 'json', responseMode: 'json' },
  skipNextReminderSeriesOccurrence: { method: 'POST', pathTemplate: '/api/v1/reminder-series/{series_id}/skip-next', requestMode: 'json', responseMode: 'json' },
  cancelReminderSeries: { method: 'POST', pathTemplate: '/api/v1/reminder-series/{series_id}/cancel', requestMode: 'json', responseMode: 'json' },
  listApprovals: { method: 'GET', pathTemplate: '/api/v1/approvals', requestMode: 'none', responseMode: 'json' },
  getApproval: { method: 'GET', pathTemplate: '/api/v1/approvals/{approval_id}', requestMode: 'none', responseMode: 'json' },
  approveApproval: { method: 'POST', pathTemplate: '/api/v1/approvals/{approval_id}/approve', requestMode: 'json', responseMode: 'json' },
  rejectApproval: { method: 'POST', pathTemplate: '/api/v1/approvals/{approval_id}/reject', requestMode: 'json', responseMode: 'json' },
  streamApprovals: { method: 'GET', pathTemplate: '/api/v1/approvals/stream', requestMode: 'none', responseMode: 'event-stream' },
  listNotifications: { method: 'GET', pathTemplate: '/api/v1/notifications', requestMode: 'none', responseMode: 'json' },
  getNotification: { method: 'GET', pathTemplate: '/api/v1/notifications/{notification_id}', requestMode: 'none', responseMode: 'json' },
  acknowledgeNotification: { method: 'POST', pathTemplate: '/api/v1/notifications/{notification_id}/acknowledge', requestMode: 'json', responseMode: 'json' },
  streamNotificationEvents: { method: 'GET', pathTemplate: '/api/v1/notifications/stream', requestMode: 'none', responseMode: 'event-stream' },
  createDocumentUpload: { method: 'POST', pathTemplate: '/api/v1/documents/uploads', requestMode: 'json', responseMode: 'json' },
  uploadDocumentContent: { method: 'PUT', pathTemplate: '/api/v1/documents/uploads/{upload_id}/content', requestMode: 'binary', responseMode: 'json' },
  listDocuments: { method: 'GET', pathTemplate: '/api/v1/documents', requestMode: 'none', responseMode: 'json' },
  searchDocuments: { method: 'POST', pathTemplate: '/api/v1/documents/search', requestMode: 'json', responseMode: 'json' },
  getDocument: { method: 'GET', pathTemplate: '/api/v1/documents/{document_id}', requestMode: 'none', responseMode: 'json' },
  deleteDocument: { method: 'DELETE', pathTemplate: '/api/v1/documents/{document_id}', requestMode: 'json', responseMode: 'json' },
  downloadDocument: { method: 'GET', pathTemplate: '/api/v1/documents/{document_id}/content', requestMode: 'none', responseMode: 'binary' },
  retryDocumentProcessing: { method: 'POST', pathTemplate: '/api/v1/documents/{document_id}/retry', requestMode: 'json', responseMode: 'json' },
  getLiveness: { method: 'GET', pathTemplate: '/health/live', requestMode: 'none', responseMode: 'json' },
  getReadiness: { method: 'GET', pathTemplate: '/health/ready', requestMode: 'none', responseMode: 'json' },
} as const satisfies Readonly<Record<AuraOperationId, AuraOperationDescriptor>>;
