"""Bounded metadata-only telemetry and structured container-log export.

The synchronous recording path performs no I/O. Records are retained in a
bounded ring and exported by a periodic lifecycle task. Capacity pressure drops
the oldest record and counts it. Export failures retain the batch for a bounded
number of attempts; an exhausted batch is dropped and counted so telemetry can
never block or exhaust the application.

Only declared dimensions and trace fields can be represented. Prompts,
responses, provider payloads, endpoints, credentials, and token values have no
accepted field and therefore cannot reach the exporter.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
import secrets
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from time import monotonic
from typing import Protocol, cast
from uuid import UUID, uuid4, uuid5

COMPONENT_VERSIONS: Mapping[str, str] = {
    "aura.platform.telemetry_export": "1.0.0",
    "aura.platform.run_evidence_export": "1.0.0",
    "aura.artifacts.attachment_upload": "1.0.0",
    "aura.artifacts.attachment_processing": "1.0.0",
    "aura.artifacts.attachment_deletion": "1.0.0",
    "aura.models.inference": "1.0.0",
    "aura.runtime.tool_selection": "1.4.0",
    "aura.runtime.policy_evaluation": "1.1.0",
    "aura.runtime.tool_execution": "1.3.0",
    "aura.interaction.agent_configuration": "1.2.0",
    "aura.interaction.persona_preview": "1.0.0",
    "aura.execution.run_coordinator": "1.9.0",
    "aura.interaction.conversation_persistence": "1.7.0",
    "aura.runtime.model_inference": "1.5.0",
    "aura.runtime.model_routing": "1.2.0",
    "aura.runtime.prompt_compilation": "1.2.0",
    "aura.runtime.stream_delivery": "1.5.0",
    "aura.knowledge.memory_persistence": "1.0.0",
    "aura.knowledge.memory_extraction": "1.2.0",
    "aura.knowledge.memory_maintenance": "1.0.0",
    "aura.runtime.structured_inference": "1.1.0",
    "aura.runtime.embedding_gateway": "1.0.0",
    "aura.knowledge.memory_retrieval": "1.2.0",
    "aura.knowledge.document_upload": "1.0.0",
    "aura.knowledge.document_processing": "1.0.0",
    "aura.knowledge.document_retrieval": "1.2.0",
    "aura.knowledge.document_deletion": "1.0.0",
    "aura.automation.reminder_management": "1.2.0",
    "aura.automation.reminder_scheduler": "1.1.0",
    "aura.automation.notification_delivery": "1.2.0",
    "aura.automation.briefing_scheduler": "1.0.0",
    "aura.execution.briefing_admission": "1.0.0",
    "aura.execution.briefing_context": "1.0.0",
    "aura.automation.briefing_persistence": "1.0.0",
    "aura.automation.briefing_cancellation": "1.0.0",
    "aura.automation.briefing_delivery": "1.0.0",
    "aura.governance.approval_workflow": "1.2.0",
    "aura.automation.workflow_state": "1.0.0",
    "aura.automation.workflow_definition": "1.0.0",
    "aura.automation.workflow_step.agent_run": "1.0.0",
    "aura.automation.workflow_approval": "1.0.0",
    "aura.automation.workflow_step.notification": "1.0.0",
    "aura.automation.workflow_step.wait_until": "1.0.0",
    "aura.automation.workflow_checkpoint": "1.0.0",
    "aura.automation.workflow_cancellation": "1.0.0",
    "aura.automation.workflow_settlement": "1.0.0",
    "aura.evaluation.runner": "1.0.0",
    "aura.integration.calendar_connection": "1.0.0",
    "aura.provider.google_calendar": "1.0.0",
    "aura.integration.calendar_retrieval": "1.0.0",
    "aura.automation.calendar_conflict": "1.0.0",
}

_METRICS: Mapping[str, frozenset[str]] = {
    "aura.platform.run_evidence_export": frozenset(
        {
            "run_evidence_export_outcome_count",
            "run_evidence_export_dropped_count",
            "run_evidence_serialize_latency_ms",
            "run_evidence_queue_offer_latency_ms",
            "run_evidence_transport_latency_ms",
            "run_evidence_queue_depth",
            "run_evidence_queue_capacity",
        }
    ),
    "aura.runtime.tool_selection": frozenset(
        {
            "tool_selection_duration_ms",
            "tool_selection_outcome",
            "reminder_create_tool_outcome",
            "reminder_tool_selection_outcome",
            "document_search_tool_selection",
        }
    ),
    "aura.runtime.policy_evaluation": frozenset(
        {
            "tool_argument_validation_outcome",
            "tool_policy_decision",
            "tool_policy_evaluation_duration_ms",
            "tool_budget_exhaustion_count",
        }
    ),
    "aura.runtime.tool_execution": frozenset(
        {
            "tool_execution_duration_ms",
            "tool_execution_outcome",
            "tool_timeout_count",
            "reminder_create_tool_outcome",
            "reminder_tool_resolution_outcome",
            "reminder_command_execution_outcome",
            "document_search_tool_outcome",
            "document_citation_validation_outcome",
        }
    ),
    "aura.interaction.agent_configuration": frozenset(
        {
            "configuration_outcome",
            "operation_duration_ms",
            "memory_policy_configuration_duration_ms",
            "memory_policy_configuration_outcome",
        }
    ),
    "aura.interaction.persona_preview": frozenset(
        {"persona_preview_duration_ms", "persona_preview_outcome", "errors", "provider_errors"}
    ),
    "aura.execution.run_coordinator": frozenset(
        {
            "cancellations",
            "conversation_regeneration_outcome",
            "consumer_acknowledged",
            "consumer_received",
            "consumer_nacked",
            "consumer_rejected",
            "consumer_redelivered",
            "errors",
            "lease_expired",
            "outbox_dispatched",
            "outbox_duplicates",
            "operation_duration_ms",
            "queue_wait_ms",
            "retries",
            "run_duration_ms",
            "run.regenerate",
            "run_started",
            "run_status",
        }
    ),
    "aura.interaction.conversation_persistence": frozenset(
        {
            "checkpoint_persisted",
            "conversation_list_duration_ms",
            "conversation_metadata_mutation_duration_ms",
            "conversation_filter_outcome",
            "conversation_archive_outcome",
            "conversation_branch_creation_outcome",
            "conversation_branch_load_outcome",
            "conversation_regeneration_outcome",
            "errors",
            "model_selection_persisted",
            "persistence_duration_ms",
            "title_persistence_duration_ms",
            "conversation_attachment_admission_outcome",
        }
    ),
    "aura.runtime.model_inference": frozenset(
        {
            "errors",
            "inference_duration_ms",
            "output_tokens",
            "provider_errors",
            "title_generation_outcome",
            "title_inference_duration_ms",
            "title_input_size",
            "title_output_size",
            "time_to_first_token_ms",
            "model_vision_verification_outcome",
            "vision_inference_outcome",
        }
    ),
    "aura.artifacts.attachment_upload": frozenset({"attachment_upload_outcome"}),
    "aura.artifacts.attachment_processing": frozenset({"attachment_processing_outcome"}),
    "aura.artifacts.attachment_deletion": frozenset({"attachment_deletion_outcome"}),
    "aura.models.inference": frozenset(
        {"model_vision_verification_outcome", "vision_inference_outcome", "inference_duration_ms"}
    ),
    "aura.runtime.model_routing": frozenset(
        {"errors", "model_routed", "model_routing_duration_ms", "model_selection_persisted"}
    ),
    "aura.runtime.prompt_compilation": frozenset(
        {
            "compiled_prompt_size",
            "operation_duration_ms",
            "prompt_compile_duration_ms",
            "prompt_component_count",
            "client_timezone_validation_outcome",
        }
    ),
    "aura.runtime.stream_delivery": frozenset(
        {
            "errors",
            "events_delivered",
            "memory_activity_publication_duration_ms",
            "memory_activity_publication_outcome",
            "memory_activity_delivery_duration_ms",
            "memory_activity_delivery_outcome",
            "sse_connections",
            "sse_reconnects",
            "stream_delivery_duration_ms",
            "thinking_stream_outcome",
        }
    ),
    "aura.knowledge.memory_persistence": frozenset(
        {
            "memory_product_operation_duration_ms",
            "memory_product_operation_outcome",
            "memory_model_configuration_duration_ms",
            "memory_model_configuration_outcome",
            "memory_operation_duration_ms",
            "memory_operation_outcome",
            "memory_lifecycle_status",
            "memory_scope_type",
        }
    ),
    "aura.automation.reminder_management": frozenset(
        {
            "reminder_command_duration_ms",
            "reminder_command_outcome",
            "reminder_outbox_dispatched",
            "reminder_outbox_duplicates",
            "reminder_version_conflict_count",
            "reminder_idempotency_conflict_count",
            "reminder_create_idempotency_outcome",
            "reminder_series_command_duration_ms",
            "reminder_series_command_outcome",
            "reminder_recurrence_calculation_outcome",
            "reminder_occurrence_generated_count",
            "reminder_series_version_conflict_count",
            "reminder_series_idempotency_conflict_count",
        }
    ),
    "aura.automation.reminder_scheduler": frozenset(
        {
            "reminder_scheduler_scan_duration_ms",
            "reminder_due_lag_ms",
            "reminder_fired_count",
            "reminder_duplicate_prevention_count",
            "reminder_fire_persistence_outcome",
            "reminder_failure_count",
            "reminder_fire_duration_ms",
            "reminder_occurrence_generated_count",
            "reminder_occurrence_skipped_count",
            "reminder_occurrence_missed_count",
            "reminder_series_due_lag_ms",
        }
    ),
    "aura.automation.notification_delivery": frozenset(
        {
            "notification_sse_connection_count",
            "notification_sse_replay_count",
            "notification_acknowledgement_outcome",
            "notification_failure_count",
            "notification_sse_duration_ms",
            "notification_sse_replay_duration_ms",
            "notification_acknowledgement_duration_ms",
        }
    ),
    "aura.automation.workflow_state": frozenset(
        {
            "workflow_transition_outcome",
            "workflow_command_outcome",
            "workflow_repository_outcome",
            "workflow_transition_duration_ms",
            "workflow_command_duration_ms",
            "workflow_repository_duration_ms",
        }
    ),
    "aura.automation.workflow_definition": frozenset(
        {"workflow_definition_validation_duration_ms"}
    ),
    "aura.automation.workflow_step.agent_run": frozenset(
        {"workflow_agent_run_step_duration_ms"}
    ),
    "aura.automation.workflow_approval": frozenset(
        {"workflow_approval_command_duration_ms"}
    ),
    "aura.automation.workflow_step.notification": frozenset(
        {"workflow_notification_step_duration_ms"}
    ),
    "aura.automation.workflow_step.wait_until": frozenset(
        {
            "workflow_wait_command_duration_ms",
            "workflow_wait_lag_ms",
        }
    ),
    "aura.automation.workflow_checkpoint": frozenset(
        {"workflow_checkpoint_persistence_duration_ms"}
    ),
    "aura.automation.workflow_cancellation": frozenset(
        {"workflow_cancellation_duration_ms"}
    ),
    "aura.automation.workflow_settlement": frozenset(
        {"workflow_settlement_duration_ms"}
    ),
    "aura.automation.briefing_scheduler": frozenset(
        {
            "briefing_schedule_lag_ms",
            "briefing_schedule_claim_duration_ms",
            "briefing_outcome_count",
            "consumer_received",
            "consumer_acknowledged",
            "consumer_nacked",
            "consumer_rejected",
            "consumer_redelivered",
            "outbox_duplicates",
            "errors",
        }
    ),
    "aura.execution.briefing_admission": frozenset(
        {
            "briefing_admission_duration_ms",
            "briefing_outcome_count",
            "briefing_budget_exhaustion_count",
        }
    ),
    "aura.execution.briefing_context": frozenset(
        {"briefing_context_assembly_duration_ms", "briefing_outcome_count"}
    ),
    "aura.automation.briefing_persistence": frozenset(
        {"briefing_result_persistence_duration_ms", "briefing_outcome_count"}
    ),
    "aura.automation.briefing_cancellation": frozenset(
        {"briefing_cancellation_duration_ms", "briefing_outcome_count"}
    ),
    "aura.automation.briefing_delivery": frozenset(
        {"briefing_delivery_duration_ms", "briefing_outcome_count"}
    ),
    "aura.governance.approval_workflow": frozenset(
        {
            "approval_creation_outcome",
            "approval_decision_outcome",
            "approval_expiry_outcome",
            "approval_expiry_count",
            "approval_wait_duration_ms",
            "approval_resume_outcome",
            "approval_sse_connection_count",
            "approval_sse_replay_count",
            "approval_sse_duration_ms",
            "approval_sse_replay_duration_ms",
            "approval_decision_duration_ms",
            "approval_resume_duration_ms",
            "approval_outbox_publish_outcome",
            "approval_outbox_persistence_outcome",
            "approval_outbox_dispatch_duration_ms",
            "approval_outbox_persist_duration_ms",
            "reminder_command_approval_outcome",
            "errors",
        }
    ),
    "aura.knowledge.memory_extraction": frozenset(
        {
            "memory_job_outcome",
            "memory_candidate_outcome",
            "memory_action_outcome",
            "memory_extraction_outcome",
            "memory_candidate_decision",
            "memory_personal_retention_gate_outcome",
            "memory_reviewable_candidate_count",
            "memory_fallback_outcome",
            "structured_inference_duration_ms",
            "queue_wait_ms",
            "retry_count",
            "backlog",
            "errors",
            "provider_errors",
            "duration_ms",
            "backlog_depth",
            "memory_extraction_duration_ms",
            "memory_job_duration_ms",
            "memory_job_queue_wait_ms",
            "memory_policy_duration_ms",
            "memory_retention_gate_duration_ms",
            "memory_processing_errors",
            "memory_retry_count",
            "missing_embedding_backlog",
            "embedding_duration_ms",
            "consumer_received",
            "consumer_acknowledged",
            "consumer_redelivered",
            "outbox_duplicates",
            "consumer_nacked",
            "consumer_rejected",
            "operation_duration_ms",
            "memory_candidate_review_duration_ms",
            "memory_candidate_review_outcome",
        }
    ),
    "aura.knowledge.memory_maintenance": frozenset(
        {
            "memory_maintenance_transition",
            "maintenance_duration_ms",
            "retry_count",
            "backlog",
            "errors",
            "duration_ms",
            "backlog_depth",
            "memory_maintenance_duration_ms",
            "memory_reindex_backlog",
            "memory_reindex_chunk_duration_ms",
            "memory_reindex_progress",
            "memory_reindex_switch_duration_ms",
            "missing_embedding_backlog",
            "embedding_duration_ms",
            "memory_retry_count",
            "memory_processing_errors",
            "operation_duration_ms",
        }
    ),
    "aura.runtime.structured_inference": frozenset(
        {
            "structured_inference_duration_ms",
            "provider_errors",
            "errors",
            "duration_ms",
            "backlog_depth",
            "retry_count",
            "model_inventory_duration_ms",
            "model_inventory_outcome",
            "model_inventory_cache_outcome",
            "model_capability_verification_duration_ms",
            "model_capability_verification_outcome",
            "model_capability_verification_cache_outcome",
        }
    ),
    "aura.runtime.embedding_gateway": frozenset(
        {
            "embedding_duration_ms",
            "provider_errors",
            "errors",
            "retry_count",
            "backlog",
            "duration_ms",
            "backlog_depth",
        }
    ),
    # Retrieval metrics intentionally contain no memory text, vectors, owner
    # subjects, or provider payloads.  Stage and scope are bounded dimensions;
    # identifiers are retained only as trace attributes where a caller opts in.
    "aura.knowledge.memory_retrieval": frozenset(
        {
            "memory_retrieval_duration_ms",
            "memory_lexical_search_duration_ms",
            "memory_vector_search_duration_ms",
            "memory_rerank_duration_ms",
            "memory_query_embedding_duration_ms",
            "memory_retrieval_candidate_count",
            "memory_recall_count",
            "memory_recall_gate_outcome",
            "memory_fallback_outcome",
            "memory_context_tokens",
            "memory_retrieval_errors",
        }
    ),
    "aura.knowledge.document_upload": frozenset(
        {"document_upload_duration_ms", "document_upload_outcome"}
    ),
    "aura.knowledge.document_processing": frozenset(
        {
            "document_processing_duration_ms",
            "document_processing_outcome",
            "document_chunk_count",
            "document_retry_count",
            "consumer_received",
            "consumer_acknowledged",
            "consumer_nacked",
            "consumer_rejected",
            "consumer_redelivered",
            "outbox_duplicates",
        }
    ),
    "aura.knowledge.document_retrieval": frozenset(
        {
            "document_retrieval_duration_ms",
            "document_retrieval_outcome",
            "document_retrieval_candidate_count",
            "attached_document_retrieval_outcome",
        }
    ),
    "aura.knowledge.document_deletion": frozenset(
        {
            "document_deletion_duration_ms",
            "document_deletion_outcome",
            "consumer_received",
            "consumer_acknowledged",
            "consumer_nacked",
            "consumer_rejected",
            "consumer_redelivered",
            "outbox_duplicates",
        }
    ),
    "aura.evaluation.runner": frozenset(
        {
            "evaluation_runner_queue_time_ms",
            "evaluation_runner_fixture_load_duration_ms",
            "evaluation_runner_execution_duration_ms",
            "evaluation_runner_outcome_count",
            "evaluation_runner_cancellation_count",
            "evaluation_runner_resource_limit_count",
            "evaluation_runner_reproducibility_completeness",
            "evaluation_runner_queue_accepted",
            "evaluation_runner_nats_fetch_failure_count",
            "evaluation_runner_nats_publish_failure_count",
            "evaluation_runner_nats_drain_failure_count",
            "evaluation_runner_nats_recovery_count",
            "evaluation_runner_nats_poison_count",
            "evaluation_runner_terminal_settlement_failure_count",
            "evaluation_runner_lease_renewal_failure_count",
            "evaluation_runner_lease_lost_count",
            "evaluation_runner_artifact_cleanup_failure_count",
            "evaluation_runner_recovery_scan_failure_count",
            "evaluation_runner_recovery_count",
            "evaluation_runner_publication_recovery_failure_count",
            "evaluation_runner_terminal_accounting_failure_count",
            "evaluation_runner_nats_identity_conflict_count",
        }
    ),
    "aura.integration.calendar_connection": frozenset(
        {
            "calendar_connection_command_duration_ms",
            "calendar_connection_command_outcome",
            "calendar_connection_health",
        }
    ),
    "aura.provider.google_calendar": frozenset(
        {
            "google_calendar_provider_request_duration_ms",
            "google_calendar_provider_request_outcome",
            "google_calendar_provider_retry_count",
            "calendar_rate_limit_state",
        }
    ),
    "aura.integration.calendar_retrieval": frozenset(
        {
            "calendar_retrieval_duration_ms",
            "calendar_retrieval_outcome",
            "calendar_returned_event_count_bucket",
        }
    ),
    "aura.automation.calendar_conflict": frozenset(
        {
            "calendar_conflict_evaluation_duration_ms",
            "calendar_conflict_evaluation_outcome",
        }
    ),
}

_SPAN_OPERATIONS: Mapping[str, frozenset[str]] = {
    "aura.platform.run_evidence_export": frozenset(
        {"run_evidence.serialize", "run_evidence.queue_offer", "run_evidence.transport_publish"}
    ),
    "aura.artifacts.attachment_upload": frozenset({"attachment.upload"}),
    "aura.artifacts.attachment_processing": frozenset({"attachment.process"}),
    "aura.artifacts.attachment_deletion": frozenset({"attachment.delete"}),
    "aura.models.inference": frozenset({"model.infer"}),
    "aura.runtime.tool_selection": frozenset({"tool.select"}),
    "aura.runtime.policy_evaluation": frozenset({"tool.policy.evaluate"}),
    "aura.runtime.tool_execution": frozenset({"tool.execute"}),
    "aura.interaction.agent_configuration": frozenset({"agent.configure"}),
    "aura.interaction.persona_preview": frozenset({"persona.preview"}),
    "aura.execution.run_coordinator": frozenset(
        {
            "consumer.delivery",
            "conversation.command",
            "lease.expire",
            "outbox.dispatch",
            "run.cancel",
            "run.execute",
            "run.regenerate",
            "run.retry",
        }
    ),
    "aura.interaction.conversation_persistence": frozenset(
        {
            "checkpoint.persist",
            "conversation.list",
            "conversation.list.request",
            "conversation.metadata.request",
            "conversation.persist",
            "conversation.branch.request",
            "conversation.branch.create",
            "conversation.branch.get",
            "conversation.branch.list",
            "run.claim",
            "run.finish",
        }
    ),
    "aura.runtime.model_inference": frozenset({"model.infer"}),
    "aura.runtime.model_routing": frozenset({"model.route", "model.selection.persist"}),
    "aura.runtime.prompt_compilation": frozenset({"prompt.compile"}),
    "aura.runtime.stream_delivery": frozenset(
        {
            "event.publish",
            "memory.activity.publish",
            "memory.activity.reconcile",
            "memory.activity.replay",
            "memory.activity.cursor_expired",
            "sse.connect",
            "sse.deliver",
            "sse.reconnect",
            "thinking.publish",
        }
    ),
    "aura.knowledge.memory_persistence": frozenset(
        {
            "memory.persist",
            "memory.list",
            "memory.get",
            "memory.create",
            "memory.reinforce",
            "memory.revise",
            "memory.status",
            "memory.pin",
            "memory.purge",
            "memory.embedding.register",
            "memory.embedding.activate",
            "memory.embedding.attach",
            "memory.model.configure",
            "memory.model.get",
            "memory.job.enqueue",
            "memory.job.get",
            "memory.job.claim",
            "memory.job.claim_next",
            "memory.job.settle",
            "memory.candidate.persist",
            "memory.candidate.get",
            "memory.candidate.list",
            "memory.candidate.detail",
            "memory.candidate.approve",
            "memory.candidate.reject",
            "memory.retrieval.generation",
            "memory.retrieval.lexical",
            "memory.retrieval.vector",
            "memory.outcome.record",
            "memory.embedding.queue",
            "memory.embedding.settle",
        }
    ),
    "aura.automation.reminder_management": frozenset(
        {
            "outbox.dispatch",
            "outbox.persist",
            "reminder.create",
            "reminder.fire",
            "reminder.get",
            "reminder.list",
            "reminder.complete",
            "reminder.cancel",
            "reminder.series.create",
            "reminder.series.update",
            "reminder.series.pause",
            "reminder.series.resume",
            "reminder.series.skip",
            "reminder.series.cancel",
        }
    ),
    "aura.automation.reminder_scheduler": frozenset(
        {"reminder.scan", "reminder.fire", "reminder.persistence", "reminder.series.advance"}
    ),
    "aura.automation.notification_delivery": frozenset(
        {"notification.sse.connect", "notification.sse.replay", "notification.acknowledge"}
    ),
    "aura.automation.workflow_state": frozenset(
        {"workflow.transition", "workflow.command", "workflow.repository"}
    ),
    # Workflow application instrumentation uses one closed operation set per
    # component.  These are intentionally finite: a workflow definition or
    # step can never turn user content into a span name.
    "aura.automation.workflow_definition": frozenset({"validate"}),
    "aura.automation.workflow_step.agent_run": frozenset({"execute"}),
    "aura.automation.workflow_approval": frozenset({"execute"}),
    "aura.automation.workflow_step.notification": frozenset({"execute"}),
    "aura.automation.workflow_step.wait_until": frozenset({"execute", "resume"}),
    "aura.automation.workflow_checkpoint": frozenset({"persist"}),
    "aura.automation.workflow_cancellation": frozenset({"cancel"}),
    "aura.automation.workflow_settlement": frozenset({"settle"}),
    "aura.automation.briefing_scheduler": frozenset(
        {"briefing.schedule.scan", "briefing.schedule.claim"}
    ),
    "aura.execution.briefing_admission": frozenset({"briefing.admission"}),
    "aura.execution.briefing_context": frozenset({"briefing.context.assemble"}),
    "aura.automation.briefing_persistence": frozenset({"briefing.result.persist"}),
    "aura.automation.briefing_cancellation": frozenset({"briefing.cancel"}),
    "aura.automation.briefing_delivery": frozenset({"briefing.delivery"}),
    "aura.governance.approval_workflow": frozenset(
        {
            "approval.create",
            "approval.decide",
            "approval.expire",
            "approval.resume",
            "approval.sse.connect",
            "approval.sse.replay",
            "approval.sse",
            "outbox.dispatch",
            "outbox.persist",
        }
    ),
    "aura.knowledge.memory_extraction": frozenset(
        {
            "memory.job",
            "memory.job.queue",
            "memory.extraction",
            "memory.extract",
            "memory.candidate",
            "memory.candidate.approve",
            "memory.candidate.reject",
            "memory.action",
            "memory.policy",
            "memory.retention_gate",
            "memory.embedding",
            "memory.fallback",
            "memory.retry",
            "memory.error",
        }
    ),
    "aura.knowledge.memory_maintenance": frozenset(
        {
            "memory.maintenance",
            "memory.decay",
            "memory.archive",
            "memory.reindex.chunk",
            "memory.reindex.switch",
            "memory.embedding",
            "memory.retry",
            "memory.error",
        }
    ),
    "aura.runtime.structured_inference": frozenset(
        {"structured.inference", "model.inventory", "model.capability.verify"}
    ),
    "aura.runtime.embedding_gateway": frozenset({"embedding.generate", "embedding.retry"}),
    "aura.knowledge.memory_retrieval": frozenset(
        {
            "memory.retrieval",
            "memory.query_embedding",
            "memory.lexical",
            "memory.vector",
            "memory.fusion",
            "memory.rerank",
            "memory.selection",
            "memory.context_budget",
            "memory.fallback",
            "memory.degradation",
        }
    ),
    "aura.knowledge.document_upload": frozenset({"document.upload"}),
    "aura.knowledge.document_processing": frozenset({"document.process", "document.retry"}),
    "aura.knowledge.document_retrieval": frozenset(
        {"document.search", "document.preview", "document.detail"}
    ),
    "aura.knowledge.document_deletion": frozenset({"document.delete"}),
    "aura.evaluation.runner": frozenset({"evaluation.execute", "evaluation.fixture.load"}),
    "aura.integration.calendar_connection": frozenset(
        {
            "calendar.connection.get",
            "calendar.connection.start",
            "calendar.connection.complete",
            "calendar.connection.reauthorize",
            "calendar.connection.disconnect",
            "calendar.connection.selection",
            "calendar.connection.authorize",
            "calendar.connection.callback",
        }
    ),
    "aura.provider.google_calendar": frozenset({"calendar.provider.request"}),
    "aura.integration.calendar_retrieval": frozenset(
        {"calendar.discovery.list", "calendar.events.query"}
    ),
    "aura.automation.calendar_conflict": frozenset(
        {"calendar.conflicts.query", "calendar.conflicts.evaluate"}
    ),
}

_SPAN_METRICS: Mapping[str, str] = {
    "aura.platform.run_evidence_export": "run_evidence_transport_latency_ms",
    "aura.runtime.tool_selection": "tool_selection_duration_ms",
    "aura.runtime.policy_evaluation": "tool_policy_evaluation_duration_ms",
    "aura.runtime.tool_execution": "tool_execution_duration_ms",
    "aura.interaction.agent_configuration": "operation_duration_ms",
    "aura.interaction.persona_preview": "persona_preview_duration_ms",
    "aura.execution.run_coordinator": "operation_duration_ms",
    "aura.interaction.conversation_persistence": "persistence_duration_ms",
    "aura.runtime.model_inference": "inference_duration_ms",
    "aura.runtime.model_routing": "model_routing_duration_ms",
    "aura.runtime.prompt_compilation": "prompt_compile_duration_ms",
    "aura.runtime.stream_delivery": "stream_delivery_duration_ms",
    "aura.knowledge.memory_persistence": "memory_operation_duration_ms",
    "aura.automation.reminder_management": "reminder_command_duration_ms",
    "aura.automation.reminder_scheduler": "reminder_scheduler_scan_duration_ms",
    "aura.automation.notification_delivery": "notification_sse_duration_ms",
    "aura.automation.workflow_state": "workflow_transition_duration_ms",
    "aura.automation.briefing_scheduler": "briefing_schedule_claim_duration_ms",
    "aura.execution.briefing_admission": "briefing_admission_duration_ms",
    "aura.execution.briefing_context": "briefing_context_assembly_duration_ms",
    "aura.automation.briefing_persistence": "briefing_result_persistence_duration_ms",
    "aura.automation.briefing_cancellation": "briefing_cancellation_duration_ms",
    "aura.automation.briefing_delivery": "briefing_delivery_duration_ms",
    "aura.governance.approval_workflow": "approval_decision_duration_ms",
    "aura.knowledge.memory_extraction": "operation_duration_ms",
    "aura.knowledge.memory_maintenance": "operation_duration_ms",
    "aura.runtime.structured_inference": "structured_inference_duration_ms",
    "aura.runtime.embedding_gateway": "embedding_duration_ms",
    "aura.knowledge.document_upload": "document_upload_duration_ms",
    "aura.knowledge.document_processing": "document_processing_duration_ms",
    "aura.knowledge.document_retrieval": "document_retrieval_duration_ms",
    "aura.knowledge.document_deletion": "document_deletion_duration_ms",
    "aura.artifacts.attachment_upload": "attachment_upload_duration_ms",
    "aura.artifacts.attachment_processing": "attachment_processing_duration_ms",
    "aura.artifacts.attachment_deletion": "attachment_deletion_duration_ms",
    "aura.models.inference": "inference_duration_ms",
    "aura.evaluation.runner": "evaluation_runner_execution_duration_ms",
    "aura.integration.calendar_connection": "calendar_connection_command_duration_ms",
    "aura.provider.google_calendar": "google_calendar_provider_request_duration_ms",
    "aura.integration.calendar_retrieval": "calendar_retrieval_duration_ms",
    "aura.automation.calendar_conflict": "calendar_conflict_evaluation_duration_ms",
}

_OPERATION_SPAN_METRICS: Mapping[tuple[str, str], str] = {
    ("aura.execution.run_coordinator", "run.regenerate"): "run.regenerate",
    (
        "aura.runtime.stream_delivery",
        "memory.activity.publish",
    ): "memory_activity_publication_duration_ms",
    ("aura.knowledge.memory_extraction", "memory.job.queue"): "memory_job_queue_wait_ms",
    ("aura.knowledge.memory_extraction", "memory.job"): "memory_job_duration_ms",
    ("aura.knowledge.memory_extraction", "memory.extraction"): "memory_extraction_duration_ms",
    ("aura.knowledge.memory_extraction", "memory.extract"): "memory_extraction_duration_ms",
    ("aura.knowledge.memory_extraction", "memory.policy"): "memory_policy_duration_ms",
    (
        "aura.knowledge.memory_extraction",
        "memory.retention_gate",
    ): "memory_retention_gate_duration_ms",
    ("aura.knowledge.memory_extraction", "memory.fallback"): "operation_duration_ms",
    ("aura.knowledge.memory_maintenance", "memory.maintenance"): "memory_maintenance_duration_ms",
    (
        "aura.knowledge.memory_maintenance",
        "memory.reindex.chunk",
    ): "memory_reindex_chunk_duration_ms",
    (
        "aura.knowledge.memory_maintenance",
        "memory.reindex.switch",
    ): "memory_reindex_switch_duration_ms",
    ("aura.knowledge.memory_maintenance", "memory.embedding"): "embedding_duration_ms",
    ("aura.knowledge.memory_retrieval", "memory.retrieval"): "memory_retrieval_duration_ms",
    ("aura.knowledge.memory_retrieval", "memory.lexical"): "memory_lexical_search_duration_ms",
    ("aura.knowledge.memory_retrieval", "memory.vector"): "memory_vector_search_duration_ms",
    ("aura.knowledge.memory_retrieval", "memory.rerank"): "memory_rerank_duration_ms",
    (
        "aura.knowledge.memory_retrieval",
        "memory.query_embedding",
    ): "memory_query_embedding_duration_ms",
    ("aura.runtime.structured_inference", "model.inventory"): "model_inventory_duration_ms",
    (
        "aura.runtime.structured_inference",
        "model.capability.verify",
    ): "model_capability_verification_duration_ms",
    ("aura.automation.reminder_scheduler", "reminder.fire"): "reminder_fire_duration_ms",
    (
        "aura.automation.reminder_management",
        "reminder.series.create",
    ): "reminder_series_command_duration_ms",
    (
        "aura.automation.reminder_management",
        "reminder.series.update",
    ): "reminder_series_command_duration_ms",
    (
        "aura.automation.reminder_management",
        "reminder.series.pause",
    ): "reminder_series_command_duration_ms",
    (
        "aura.automation.reminder_management",
        "reminder.series.resume",
    ): "reminder_series_command_duration_ms",
    (
        "aura.automation.reminder_management",
        "reminder.series.skip",
    ): "reminder_series_command_duration_ms",
    (
        "aura.automation.reminder_management",
        "reminder.series.cancel",
    ): "reminder_series_command_duration_ms",
    ("aura.automation.reminder_scheduler", "reminder.series.advance"): "reminder_fire_duration_ms",
    (
        "aura.automation.notification_delivery",
        "notification.sse.replay",
    ): "notification_sse_replay_duration_ms",
    (
        "aura.automation.notification_delivery",
        "notification.acknowledge",
    ): "notification_acknowledgement_duration_ms",
    ("aura.automation.workflow_state", "workflow.command"): "workflow_command_duration_ms",
    ("aura.automation.workflow_state", "workflow.repository"): "workflow_repository_duration_ms",
    (
        "aura.automation.workflow_definition", "validate"
    ): "workflow_definition_validation_duration_ms",
    (
        "aura.automation.workflow_step.agent_run", "execute"
    ): "workflow_agent_run_step_duration_ms",
    ("aura.automation.workflow_approval", "execute"): "workflow_approval_command_duration_ms",
    (
        "aura.automation.workflow_step.notification", "execute"
    ): "workflow_notification_step_duration_ms",
    ("aura.automation.workflow_step.wait_until", "execute"): "workflow_wait_command_duration_ms",
    ("aura.automation.workflow_step.wait_until", "resume"): "workflow_wait_lag_ms",
    (
        "aura.automation.workflow_checkpoint", "persist"
    ): "workflow_checkpoint_persistence_duration_ms",
    ("aura.automation.workflow_cancellation", "cancel"): "workflow_cancellation_duration_ms",
    ("aura.automation.workflow_settlement", "settle"): "workflow_settlement_duration_ms",
    (
        "aura.automation.briefing_scheduler",
        "briefing.schedule.scan",
    ): "briefing_schedule_claim_duration_ms",
    (
        "aura.automation.briefing_scheduler",
        "briefing.schedule.claim",
    ): "briefing_schedule_claim_duration_ms",
    ("aura.execution.briefing_admission", "briefing.admission"): "briefing_admission_duration_ms",
    (
        "aura.execution.briefing_context",
        "briefing.context.assemble",
    ): "briefing_context_assembly_duration_ms",
    (
        "aura.automation.briefing_persistence",
        "briefing.result.persist",
    ): "briefing_result_persistence_duration_ms",
    (
        "aura.automation.briefing_cancellation",
        "briefing.cancel",
    ): "briefing_cancellation_duration_ms",
    (
        "aura.automation.briefing_delivery",
        "briefing.delivery",
    ): "briefing_delivery_duration_ms",
    ("aura.governance.approval_workflow", "approval.sse.connect"): "approval_sse_duration_ms",
    ("aura.governance.approval_workflow", "approval.sse.replay"): "approval_sse_replay_duration_ms",
    ("aura.governance.approval_workflow", "approval.resume"): "approval_resume_duration_ms",
    (
        "aura.governance.approval_workflow",
        "outbox.dispatch",
    ): "approval_outbox_dispatch_duration_ms",
    ("aura.governance.approval_workflow", "outbox.persist"): "approval_outbox_persist_duration_ms",
}

# Dimensions are suitable for metric aggregation and intentionally exclude all
# identifiers. Trace attributes may be high-cardinality but are never labels.
_DIMENSION_KEYS = frozenset(
    {
        "archive_state",
        "dependency",
        "error_class",
        "outcome",
        "provider",
        "status",
        "token_estimator",
        "scope_type",
        "retrieval_stage",
        "degradation",
        "operation",
        "connection_health",
        "rate_limit_state",
        "event_count_bucket",
        "relationship_count_bucket",
        "retry_class",
    }
)

_WORKFLOW_OPERATIONS = frozenset(
    {"workflow.transition", "workflow.command", "workflow.repository"}
)
_WORKFLOW_DEPENDENCIES = frozenset(
    {"application_validation", "postgresql", "transactional_outbox"}
)
_WORKFLOW_ERROR_CLASSES = frozenset(
    {
        "authorization",
        "conflict",
        "database",
        "database_programming",
        "idempotency",
        "internal",
        "not_found",
        "persistence",
        "serialization",
        "timeout",
        "validation",
        "unknown",
    }
)
_WORKFLOW_OUTCOMES = frozenset(
    {
        "accepted",
        "canceled",
        "completed",
        "conflict",
        "duplicate",
        "error",
        "failed",
        "idempotent",
        "invalid",
        "ok",
        "replayed",
        "unknown",
    }
)

# Workflow application instrumentation must never fall back to the all-zero
# trace used by pure transition tests.  Production composition may omit an
# explicit parent for a read or a legacy adapter, so the telemetry boundary
# supplies an opaque request trace while retaining the same finite dimensions.
_WORKFLOW_TELEMETRY_COMPONENTS = frozenset(
    {
        "aura.automation.workflow_state",
        "aura.automation.workflow_definition",
        "aura.automation.workflow_step.agent_run",
        "aura.automation.workflow_approval",
        "aura.automation.workflow_step.notification",
        "aura.automation.workflow_step.wait_until",
        "aura.automation.workflow_checkpoint",
        "aura.automation.workflow_cancellation",
        "aura.automation.workflow_settlement",
    }
)

CALENDAR_TERMINAL_OUTCOMES = frozenset(
    {"ok", "partial", "canceled", "rejected", "timeout", "error"}
)
CALENDAR_ERROR_CLASSES = frozenset(
    {
        "authorization",
        "validation",
        "conflict",
        "provider_auth",
        "provider_rate_limit",
        "provider_unavailable",
        "dependency_unavailable",
        "deadline",
        "budget",
        "internal",
    }
)


def classify_calendar_terminal(
    outcome: str,
    *,
    error_class: str | None = None,
) -> tuple[str, str | None]:
    """Normalize one calendar terminal outcome to the closed metadata vocabulary."""

    normalized_error = error_class if error_class in CALENDAR_ERROR_CLASSES else None
    if outcome == "ok":
        return "ok", normalized_error
    if outcome == "partial":
        return "partial", normalized_error
    if outcome in {"canceled", "cancelled"}:
        return "canceled", normalized_error or "budget"
    if outcome in {"provider_auth", "provider_rate_limit", "provider_unavailable"}:
        return "rejected", outcome
    if outcome in {"deadline", "deadline_exceeded", "timeout"}:
        return "timeout", normalized_error or "deadline"
    if outcome in {"authorization", "validation", "conflict"}:
        return "rejected", normalized_error or outcome
    if outcome == "rejected":
        return "rejected", normalized_error or "internal"
    if outcome == "error":
        return "error", normalized_error or "internal"
    return "error", normalized_error or "internal"
_TRACE_ATTRIBUTE_KEYS = frozenset(
    {
        "aggregate_id",
        "aggregate_type",
        "agent_revision_id",
        "agent_revision_number",
        "profile_id",
        "persona_revision_number",
        "persona_revision_id",
        "prompt_bundle_revision_id",
        "prompt_hash",
        "prompt_component_count",
        "compiled_prompt_size",
        "configuration_outcome",
        "configuration_source",
        "configuration_reason",
        "result_count",
        "memory_id",
        "memory_revision_id",
        "generation_id",
        "job_id",
        "operation_duration_ms",
        "attempt_id",
        "attempt_count",
        "causation_id",
        "command_id",
        "conversation_id",
        "branch_id",
        "regenerated_from_run_id",
        "correlation_id",
        "delivery_count",
        "assistant_message_id",
        "model_id",
        "model_policy_revision_id",
        "retry_of_run_id",
        "run_id",
        "user_message_id",
        "title_input_size",
        "title_output_size",
        "memory_policy_revision_id",
        "tool_policy_revision_id",
        "policy_revision_id",
        "embedding_generation_id",
        "retrieval_version",
        "selection_order",
        "candidate_count",
        "recalled_count",
        "context_tokens",
        "event_status",
        "event_type",
        "event_version",
        "fallback_grant_id",
        "recall_mode",
        "gate_outcome",
        "reminder_id",
        "notification_id",
        "event_id",
        "approval_id",
        "proposal_id",
        "tool_call_id",
        "approval_version",
        "idempotency",
        "document_id",
        "attachment_id",
        "evidence_id",
        "briefing_schedule_id",
        "briefing_occurrence_id",
        "briefing_result_id",
        "workflow_id",
        "revision_id",
        "instance_id",
        "step_execution_id",
        "expected_version",
        "result_version",
    }
)
_UUID_TRACE_ATTRIBUTES = frozenset(
    {
        "aggregate_id",
        "agent_revision_id",
        "profile_id",
        "persona_revision_id",
        "prompt_bundle_revision_id",
        "attempt_id",
        "causation_id",
        "command_id",
        "conversation_id",
        "branch_id",
        "regenerated_from_run_id",
        "correlation_id",
        "assistant_message_id",
        "memory_id",
        "memory_revision_id",
        "generation_id",
        "job_id",
        "model_policy_revision_id",
        "retry_of_run_id",
        "run_id",
        "user_message_id",
        "memory_policy_revision_id",
        "tool_policy_revision_id",
        "policy_revision_id",
        "embedding_generation_id",
        "fallback_grant_id",
        "reminder_id",
        "notification_id",
        "event_id",
        "approval_id",
        "proposal_id",
        "tool_call_id",
        "document_id",
        "attachment_id",
        "evidence_id",
        "briefing_schedule_id",
        "briefing_occurrence_id",
        "briefing_result_id",
        "workflow_id",
        "revision_id",
        "instance_id",
        "step_execution_id",
    }
)
_COUNT_TRACE_ATTRIBUTES = frozenset(
    {
        "attempt_count",
        "delivery_count",
        "prompt_component_count",
        "compiled_prompt_size",
        "operation_duration_ms",
        "agent_revision_number",
        "persona_revision_number",
        "title_input_size",
        "title_output_size",
        "result_count",
        "selection_order",
        "candidate_count",
        "recalled_count",
        "context_tokens",
        "event_version",
        "approval_version",
        "expected_version",
        "result_version",
    }
)
_ENUM_DIMENSIONS: Mapping[str, frozenset[str]] = {
    "dependency": frozenset(
        {
            "conversation_store",
            "event_store",
            "model_policy",
            "nats_jetstream",
            "transactional_outbox",
            "postgresql",
            "sse_client",
            "configuration_store",
            "prompt_compiler",
            "memory_store",
            "structured_inference",
            "embedding_provider",
            "parser",
            "object_storage",
            "attachment_store",
            "document_store",
            "query_embedding",
            "nats_transient",
            "memory_worker",
            "reminder_store",
            "approval_store",
            "approval-replay",
            "identity_authorization",
            "transport_validation",
            "application_validation",
            "application_unknown",
            "model_provider",
        }
    ),
    "archive_state": frozenset({"active", "archived", "all", "unknown"}),
    "error_class": frozenset(
        {
            "authorization",
            "cancel",
            "conflict",
            "provider_auth",
            "provider_rate_limit",
            "provider_unavailable",
            "dependency_unavailable",
            "internal",
            "identity",
            "delivery",
            "disabled",
            "idempotency",
            "not_found",
            "persistence",
            "provider",
            "generation",
            "capability",
            "policy",
            "provider_protocol",
            "parse_failed",
            "processing_failed",
            "object_storage_failed",
            "embedding_unavailable",
            "upload_cleanup_retryable",
            "delete_retryable",
            "execution",
            "cancellation",
            "purged",
            "queue",
            "timeout",
            "deadline",
            "validation",
            "query_embedding",
            "database",
            "database_programming",
            "telemetry",
            "budget",
            "scope",
            "lifecycle",
            "dimension",
            "identity_mismatch",
            "cursor_expired",
            "run_not_found",
            "serialization",
            "serialization_rejected",
            "queue_pressure",
            "transport_unavailable",
            "unknown",
            "stream_error",
            "invalid_cursor",
        }
    ),
    "outcome": frozenset(
        {
            "accepted",
            "canceled",
            "conflict",
            "duplicate",
            "error",
            "partial",
            "rejected",
            "timeout",
            "fallback",
            "generated",
            "ignored",
            "idempotent",
            "replayed",
            "ok",
            "retryable",
            "review",
            "skipped",
            "used",
            "not_needed",
            "hit",
            "miss",
            "denied",
            "degraded",
            "truncated",
            "empty",
            "not_granted",
            "not_found",
            "expired",
            "unknown",
            "policy_off",
            "selected",
            "allowed",
            "completed",
            "failed",
            "personal",
            "explicit_request",
            "none",
            "text",
            "tool_call",
            "allow",
            "deny",
            "invalid",
            "valid",
            "capability",
            "budget_exceeded",
            "unavailable",
            "publisher_error",
            "published",
            "delivered",
            "out_of_order",
            "over_budget",
            "no_subscriber",
            "queue_full",
            "malformed_envelope",
            "delivery",
            "transport_error",
            "transport_published",
            "offset_unavailable",
            "created",
            "pending",
            "approved",
            "resumed",
            "opened",
            "closed",
            "omitted",
            "awaiting_confirmation",
            "waiting_for_confirmation",
            "executing",
            "resolved",
            "clarification",
            "failure",
            "validation",
            "verified",
            "ready",
            "storage_unavailable",
            "fenced",
            "recovered",
        }
    ),
    "operation": frozenset(
        {
            "create",
            "update",
            "pause",
            "resume",
            "skip",
            "cancel",
            "fire",
            "complete",
            "acknowledge",
            "scan",
            "serialize",
            "queue_offer",
            "transport_publish",
            "workflow.transition",
            "workflow.command",
            "workflow.repository",
            "validate",
            "execute",
            "persist",
            "settle",
        }
    ),
    "status": frozenset(
        {
            "canceled",
            "cancel_requested",
            "completed",
            "failed",
            "interrupted",
            "queued",
            "running",
            "active",
            "dormant",
            "archived",
            "disabled",
            "disputed",
            "superseded",
            "unknown",
            "retryable",
            "pending",
            "approved",
            "rejected",
            "expired",
            "awaiting_confirmation",
            "waiting_for_confirmation",
            "executing",
            "waiting_for_time",
            "terminal",
        }
    ),
    "token_estimator": frozenset({"chars_div_4_ceil"}),
    "scope_type": frozenset(
        {
            "user",
            "agent",
            "owner",
            "shared_user",
            "current_agent",
            "foreign_agent",
            "unknown",
        }
    ),
    "retrieval_stage": frozenset(
        {
            "primary",
            "query_embedding",
            "lexical",
            "vector",
            "fusion",
            "rerank",
            "selection",
            "context_budget",
            "fallback",
            "degradation",
            "unknown",
        }
    ),
    "degradation": frozenset(
        {
            "none",
            "query_embedding",
            "database",
            "telemetry",
            "budget",
            "embedding_unavailable",
            "identity_mismatch",
        }
    ),
    "connection_health": frozenset({"connected", "disconnected", "degraded", "unavailable"}),
    "rate_limit_state": frozenset({"not_limited", "limited", "unknown"}),
    "event_count_bucket": frozenset({"0", "1_10", "11_50", "51_100"}),
    "retry_class": frozenset({"none", "rate_limit", "transient", "unknown"}),
}
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_OTLP_NO_TRACE_NAMESPACE = UUID("9e8e7f18-92ed-4c10-82ec-a0d1cdb0b35c")
_OTLP_NO_TRACE_ID = "0" * 32
_METADATA_OBSERVATION_V2_SCHEMA_URL = (
    "https://aura.local/contracts/telemetry/v2/metadata-observation.schema.json"
)
_V2_REVISION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
_V2_MODEL_IDENTIFIER = re.compile(
    r"^(?!(?:https?|ftp|file|mailto|ssh|ws|wss):)"
    r"(?!(?:owner|identity|credential|bearer|token|secret|password|private|content|hash|"
    r"digest|endpoint)(?:$|[._:+-]))"
    r"(?!.*[._:+-](?:owner|identity|credential|bearer|token|secret|password|private|content|"
    r"hash|digest|endpoint)(?:$|[._:+-]))"
    r"[a-z0-9][a-z0-9._+-]{0,127}"
    r"(?::(?=[a-z0-9._+-]{1,64}$)(?=[a-z0-9._+-]*[a-z])[a-z0-9._+-]+)?$"
)
_V2_PRIVATE_MARKER_SEGMENT = re.compile(
    r"(?:^|[._:+-])(?:owner|identity|credential|bearer|token|secret|password|private|content|"
    r"hash|digest|endpoint)(?=$|[._:+-])",
    re.IGNORECASE,
)


def _valid_v2_revision(value: str) -> bool:
    """Validate a closed v2 revision without allowing private/content shapes."""

    return bool(
        _V2_REVISION.fullmatch(value)
        and not _V2_PRIVATE_MARKER_SEGMENT.search(value)
        and not any(character.isspace() for character in value)
        and "://" not in value
        and "/" not in value
        and "\\" not in value
        and "@" not in value
    )


def _valid_v2_model_identifier(value: str) -> bool:
    """Validate repository-supported local model IDs, including provider tags."""

    return bool(_V2_MODEL_IDENTIFIER.fullmatch(value)) and not _V2_PRIVATE_MARKER_SEGMENT.search(
        value
    )


@dataclass(frozen=True, slots=True)
class Measurement:
    kind: str
    component_id: str
    component_version: str
    metric: str
    value: float
    observed_at: datetime
    dimensions: tuple[tuple[str, str], ...] = ()
    trace_id: str | None = None
    span_id: str | None = None
    parent_span_id: str | None = None
    link_span_ids: tuple[str, ...] = ()
    trace_attributes: tuple[tuple[str, str], ...] = ()
    # The identifier is assigned at record time so retries through the
    # Collector preserve deduplication semantics.
    observation_id: UUID = field(default_factory=uuid4)


@dataclass(frozen=True, slots=True)
class MemoryTraceContext:
    """Metadata-only context shared by one asynchronous memory command."""

    trace_id: str
    span_id: str
    command_id: str | None = None
    job_id: str | None = None
    run_id: str | None = None
    conversation_id: str | None = None
    correlation_id: str | None = None
    causation_id: str | None = None
    agent_revision_id: str | None = None
    user_message_id: str | None = None
    assistant_message_id: str | None = None
    memory_id: str | None = None
    memory_revision_id: str | None = None
    generation_id: str | None = None
    link_span_ids: tuple[str, ...] = ()
    # Housekeeping still performs its owner-scoped reads, but successful
    # discovery reads are internal polling details. Failures remain visible.
    suppress_idle_success: bool = False

    def attributes(self) -> dict[str, str]:
        return {
            key: value
            for key, value in {
                "command_id": self.command_id,
                "job_id": self.job_id,
                "run_id": self.run_id,
                "conversation_id": self.conversation_id,
                "correlation_id": self.correlation_id,
                "causation_id": self.causation_id,
                "agent_revision_id": self.agent_revision_id,
                "user_message_id": self.user_message_id,
                "assistant_message_id": self.assistant_message_id,
                "memory_id": self.memory_id,
                "memory_revision_id": self.memory_revision_id,
                "generation_id": self.generation_id,
            }.items()
            if value is not None
        }


@dataclass(frozen=True, slots=True)
class CalendarTraceContext:
    """Metadata-only context shared by one calendar API request."""

    trace_id: str
    span_id: str
    deadline: float | None = None
    cancelled: Callable[[], bool] | None = None


_CALENDAR_TRACE_CONTEXT: ContextVar[CalendarTraceContext | None] = ContextVar(
    "aura_calendar_trace_context", default=None
)


@contextmanager
def calendar_trace_context(context: CalendarTraceContext):
    """Propagate one request trace and deadline across calendar adapters."""

    token = _CALENDAR_TRACE_CONTEXT.set(context)
    try:
        yield context
    finally:
        _CALENDAR_TRACE_CONTEXT.reset(token)


def current_calendar_trace_context() -> CalendarTraceContext | None:
    """Return the active calendar request context without private values."""

    return _CALENDAR_TRACE_CONTEXT.get()


_MEMORY_TRACE_CONTEXT: ContextVar[MemoryTraceContext | None] = ContextVar(
    "aura_memory_trace_context", default=None
)


@contextmanager
def memory_trace_context(context: MemoryTraceContext):
    """Propagate one command trace across async Core/provider call sites."""

    token = _MEMORY_TRACE_CONTEXT.set(context)
    try:
        yield context
    finally:
        _MEMORY_TRACE_CONTEXT.reset(token)


def current_memory_trace_context() -> MemoryTraceContext | None:
    """Return the active memory context without exposing content."""

    return _MEMORY_TRACE_CONTEXT.get()


@dataclass(frozen=True, slots=True)
class TelemetryStats:
    retained: int
    capacity: int
    dropped: int
    rejected: int
    export_failures: int
    abandoned_batches: int
    exporter_configured: bool


@dataclass(frozen=True, slots=True)
class TelemetryBatch:
    measurements: tuple[Measurement, ...]
    stats: TelemetryStats


class TelemetryExporter(Protocol):
    async def export(self, batch: TelemetryBatch) -> None: ...


@dataclass(frozen=True, slots=True)
class DeploymentMetadata:
    """Bounded deployment identity attached to every exported observation."""

    deployment_id: str = "local"
    git_commit: str = "0000000"
    environment: str = "development"
    host_id: str = "local"
    service_role: str = "api"
    image_version: str | None = None
    configuration_version: str | None = None

    def __post_init__(self) -> None:
        validate_deployment_metadata(self)


_DEPLOYMENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
_ENVIRONMENT = re.compile(r"^[a-z][a-z0-9_.-]{0,31}$")
_GIT_COMMIT = re.compile(r"^[0-9a-f]{7,64}$")


def _valid_bounded_metadata(value: str, pattern: re.Pattern[str], label: str) -> None:
    if (
        not pattern.fullmatch(value)
        or any(character.isspace() for character in value)
        or "://" in value
        or "/" in value
        or "\\" in value
        or "@" in value
        or _V2_PRIVATE_MARKER_SEGMENT.search(value)
    ):
        raise ValueError(
            f"{label} must be bounded metadata, not a URL, path, identity, or credential"
        )


def validate_deployment_metadata(metadata: DeploymentMetadata) -> None:
    """Fail closed before metadata reaches either local logs or OTLP."""

    _valid_bounded_metadata(metadata.deployment_id, _DEPLOYMENT_ID, "deployment id")
    _valid_bounded_metadata(metadata.host_id, _DEPLOYMENT_ID, "host id")
    if not _ENVIRONMENT.fullmatch(metadata.environment):
        raise ValueError("environment must be a bounded lower-case identifier")
    if not _GIT_COMMIT.fullmatch(metadata.git_commit):
        raise ValueError("git commit must be 7-64 lower-case hexadecimal characters")
    if metadata.service_role not in {"api", "worker", "scheduler", "cli"}:
        raise ValueError("service role must be api, worker, scheduler, or cli")
    for label, value in (
        ("image version", metadata.image_version),
        ("configuration version", metadata.configuration_version),
    ):
        if value is not None:
            if not _valid_v2_revision(value):
                raise ValueError(f"{label} must be bounded non-sensitive metadata")


_WIRE_DIMENSIONS = {
    "archive_state": "archiveState",
    "error_class": "errorClass",
    "token_estimator": "tokenEstimator",
    "scope_type": "scopeType",
    "retrieval_stage": "retrievalStage",
}


def _wire_observation(item: Measurement, deployment: DeploymentMetadata) -> dict[str, object]:
    """Serialize one already-redacted Core measurement to telemetry v1.

    The serializer is deliberately closed over ``Measurement`` fields. It
    never forwards arbitrary trace attributes and therefore remains safe if a
    caller passes an untrusted mapping to one of the public recording helpers.
    """

    attributes = dict(item.trace_attributes)
    operation = attributes.get("operation", item.metric)
    trace: dict[str, object] | None = None
    if item.trace_id is not None:
        trace = {"traceId": item.trace_id}
        if item.span_id is not None:
            trace["spanId"] = item.span_id
        if item.parent_span_id is not None:
            trace["parentSpanId"] = item.parent_span_id
        if item.link_span_ids:
            trace["linkedSpanIds"] = list(item.link_span_ids)
    correlation: dict[str, str] | None = None
    if attributes.get("run_id") is not None or attributes.get("conversation_id") is not None:
        correlation = {
            key.removesuffix("_id") + "Id": value
            for key, value in attributes.items()
            if key in {"run_id", "conversation_id"}
        }
    dimensions = {_WIRE_DIMENSIONS.get(key, key): value for key, value in item.dimensions}
    observation: dict[str, object] = {
        "schemaVersion": 1,
        "observationId": str(item.observation_id),
        "observedAt": item.observed_at.isoformat(),
        "kind": item.kind,
        "component": {"id": item.component_id, "version": item.component_version},
        "operation": operation,
        "measurement": {"name": item.metric, "value": item.value},
        "dimensions": dimensions,
        "deployment": {
            "deploymentId": deployment.deployment_id,
            "gitCommit": deployment.git_commit,
            "environment": deployment.environment,
            "hostId": deployment.host_id,
        },
        "serviceRole": deployment.service_role,
        "capturePolicy": "metadata_only",
    }
    if trace is not None:
        observation["trace"] = trace
    if correlation is not None:
        observation["correlation"] = correlation
    return observation


class StructuredContainerLogExporter:
    """Write metadata-only JSON lines to stdout for the container log driver."""

    def __init__(
        self,
        writer: Callable[[str], None] | None = None,
        *,
        deployment: DeploymentMetadata | None = None,
        health_provider: Callable[[], Mapping[str, int | float]] | None = None,
    ) -> None:
        self._writer = writer or _write_stdout
        self.deployment = deployment
        self._health_provider = health_provider

    async def export(self, batch: TelemetryBatch) -> None:
        lines = [self._measurement_line(item, self.deployment) for item in batch.measurements]
        lines.append(self._health_line(batch.stats, self._health_provider))
        # Export runs only on the background lifecycle task; the application
        # recording path never performs this container-log I/O.
        self._write_lines(lines)
        await asyncio.sleep(0)

    def _write_lines(self, lines: Sequence[str]) -> None:
        for line in lines:
            self._writer(line)

    @staticmethod
    def _measurement_line(item: Measurement, deployment: DeploymentMetadata | None = None) -> str:
        if deployment is not None:
            wire = _wire_observation(item, deployment)
            # Keep the established structured-log keys for local operators
            # while adding the versioned wire envelope for Collector parity.
            legacy = {
                "capture_policy": "metadata_only",
                "component_id": item.component_id,
                "component_version": item.component_version,
                "dimensions": dict(item.dimensions),
                "kind": item.kind,
                "metric": item.metric,
                "observed_at": item.observed_at.isoformat(),
                "value": item.value,
            }
            return json.dumps(
                {"event": "aura.telemetry.measurement", **legacy, **wire},
                separators=(",", ":"),
                sort_keys=True,
            )
        trace: dict[str, object] = {}
        if item.trace_id is not None:
            trace["trace_id"] = item.trace_id
        if item.span_id is not None:
            trace["span_id"] = item.span_id
        if item.parent_span_id is not None:
            trace["parent_span_id"] = item.parent_span_id
        if item.link_span_ids:
            trace["link_span_ids"] = item.link_span_ids
        if item.trace_attributes:
            trace["attributes"] = dict(item.trace_attributes)
        record: dict[str, object] = {
            "capture_policy": "metadata_only",
            "component_id": item.component_id,
            "component_version": item.component_version,
            "dimensions": dict(item.dimensions),
            "event": "aura.telemetry.measurement",
            "kind": item.kind,
            "metric": item.metric,
            "observed_at": item.observed_at.isoformat(),
            "value": item.value,
        }
        if trace:
            record["trace"] = trace
        return json.dumps(record, separators=(",", ":"), sort_keys=True)

    @staticmethod
    def _health_line(
        stats: TelemetryStats,
        health_provider: Callable[[], Mapping[str, int | float]] | None = None,
    ) -> str:
        record: dict[str, object] = {
            "abandoned_batches": stats.abandoned_batches,
            "capture_policy": "metadata_only",
            "dropped": stats.dropped,
            "event": "aura.telemetry.health",
            "export_failures": stats.export_failures,
            "rejected": stats.rejected,
            "retained": stats.retained,
        }
        if health_provider is not None:
            record.update(health_provider())
        return json.dumps(
            record,
            separators=(",", ":"),
            sort_keys=True,
        )


class _OtlpHttpTransport(Protocol):
    async def send(
        self, endpoint: str, payload: bytes, credential: str | None, timeout: float
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class _OtlpSignal:
    signal: str
    payload: bytes
    observation_count: int


class OtlpHttpTelemetryExporter:
    """Independent bounded background sink for the Observatory Collector.

    ``export`` only enqueues an already-redacted batch. Network I/O happens
    on a separate task and is retried/dropped within bounded limits, so a
    Collector outage cannot block Core's structured log exporter or request
    processing.
    """

    def __init__(
        self,
        endpoint: str,
        *,
        credential: str | None = None,
        queue_capacity: int = 32,
        max_attempts: int = 3,
        timeout_seconds: float = 2.0,
        transport: _OtlpHttpTransport | None = None,
        deployment: DeploymentMetadata | None = None,
    ) -> None:
        if not endpoint or queue_capacity < 1 or max_attempts < 1 or timeout_seconds <= 0:
            raise ValueError("invalid OTLP telemetry exporter configuration")
        self.endpoint = endpoint
        self.credential = credential
        self.queue_capacity = queue_capacity
        self.max_attempts = max_attempts
        self.timeout_seconds = timeout_seconds
        self.deployment = deployment or DeploymentMetadata()
        self._transport = transport or _DefaultOtlpHttpTransport()
        self._queue: asyncio.Queue[tuple[_OtlpSignal, int]] = asyncio.Queue(queue_capacity)
        self._task: asyncio.Task[None] | None = None
        self._inflight: _OtlpSignal | None = None
        self._accepted = 0
        self._failures = 0
        self._dropped = 0
        self._retries = 0
        self._duration_ms = 0.0

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def stats(self) -> Mapping[str, int | float]:
        return {
            "telemetry_export_accepted_count": self._accepted,
            "telemetry_export_failure_count": self._failures,
            "telemetry_export_dropped_count": self._dropped,
            "telemetry_export_retry_count": self._retries,
            "telemetry_export_duration_ms": self._duration_ms,
        }

    async def start(self) -> None:
        if not self.running:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is None:
            return
        try:
            await asyncio.wait_for(self._queue.join(), timeout=self.timeout_seconds)
        except TimeoutError:
            while True:
                try:
                    signal, _ = self._queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                self._dropped += signal.observation_count
                self._queue.task_done()
        self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)
        self._task = None

    async def export(self, batch: TelemetryBatch) -> None:
        for signal in self._signal_payloads(batch):
            try:
                self._queue.put_nowait((signal, 0))
            except asyncio.QueueFull:
                self._dropped += signal.observation_count
                continue
            self._accepted += signal.observation_count

    async def _run(self) -> None:
        while True:
            signal, attempt = await self._queue.get()
            self._inflight = signal
            started = monotonic()
            try:
                await self._transport.send(
                    self._signal_endpoint(signal.signal),
                    signal.payload,
                    self.credential,
                    self.timeout_seconds,
                )
            except asyncio.CancelledError:
                self._dropped += signal.observation_count
                raise
            except Exception:
                self._failures += signal.observation_count
                if attempt + 1 < self.max_attempts:
                    self._retries += signal.observation_count
                    try:
                        self._queue.put_nowait((signal, attempt + 1))
                    except asyncio.QueueFull:
                        self._dropped += signal.observation_count
                else:
                    self._dropped += signal.observation_count
            finally:
                self._duration_ms += (monotonic() - started) * 1000
                self._inflight = None
                self._queue.task_done()

    def _signal_endpoint(self, signal: str) -> str:
        parsed = urllib.parse.urlsplit(self.endpoint)
        path = parsed.path.rstrip("/")
        for suffix in ("/v1/observations", "/v1/metrics", "/v1/traces"):
            if path.endswith(suffix):
                path = path[: -len(suffix)]
                break
        return urllib.parse.urlunsplit(
            (parsed.scheme, parsed.netloc, f"{path}/v1/{signal}", "", "")
        )

    def _signal_payloads(self, batch: TelemetryBatch) -> tuple[_OtlpSignal, ...]:
        metrics = tuple(item for item in batch.measurements if item.kind == "metric")
        spans = tuple(item for item in batch.measurements if item.kind == "span")
        payloads: list[_OtlpSignal] = []
        if metrics:
            payloads.append(_OtlpSignal("metrics", self._metrics_payload(metrics), len(metrics)))
        if spans:
            payloads.append(_OtlpSignal("traces", self._traces_payload(spans), len(spans)))
        return tuple(payloads)

    def _resource_attributes(self) -> list[dict[str, object]]:
        attributes: list[dict[str, object]] = [
            {"key": "service.name", "value": {"stringValue": "aura-core"}},
            {"key": "service.role", "value": {"stringValue": self.deployment.service_role}},
            {"key": "deployment.id", "value": {"stringValue": self.deployment.deployment_id}},
            {
                "key": "deployment.environment",
                "value": {"stringValue": self.deployment.environment},
            },
            {"key": "deployment.git_commit", "value": {"stringValue": self.deployment.git_commit}},
            {"key": "host.id", "value": {"stringValue": self.deployment.host_id}},
            {"key": "capture.policy", "value": {"stringValue": "metadata_only"}},
        ]
        # These are resource-level markers because they describe the deployed
        # Core process, not a metric dimension. Optional values are omitted so
        # an unset marker remains unavailable to Observatory rather than being
        # represented by a fabricated default.
        if self.deployment.image_version is not None:
            attributes.append(
                self._string_attribute("deployment.image_version", self.deployment.image_version)
            )
        if self.deployment.configuration_version is not None:
            attributes.append(
                self._string_attribute(
                    "deployment.configuration_version", self.deployment.configuration_version
                )
            )
        return attributes

    @staticmethod
    def _string_attribute(key: str, value: str) -> dict[str, object]:
        return {"key": key, "value": {"stringValue": value}}

    def _observation_attributes(self, item: Measurement) -> list[dict[str, object]]:
        attributes = dict(item.trace_attributes)
        values = [
            self._string_attribute("aura.observation_id", str(item.observation_id)),
            self._string_attribute("aura.component_id", item.component_id),
            self._string_attribute("aura.component_version", item.component_version),
            self._string_attribute("aura.operation", attributes.get("operation", item.metric)),
            self._string_attribute("aura.metric_name", item.metric),
            self._string_attribute("aura.capture_policy", "metadata_only"),
        ]
        for key, value in item.dimensions:
            values.append(self._string_attribute(f"aura.dimension.{key}", value))
        for key in ("run_id", "conversation_id"):
            if key in attributes:
                values.append(self._string_attribute(f"aura.{key}", attributes[key]))
        # Only the closed v2 execution markers are projected. In particular,
        # prompt_hash and every unlisted trace attribute stay out of OTLP even
        # though they may exist in Core's internal trace context.
        execution: list[tuple[str, str]] = []
        model_id = attributes.get("model_id")
        if model_id is not None and _valid_v2_model_identifier(model_id):
            execution.append(("model_id", model_id))
        for source, target in (
            ("model_version", "model_version"),
            ("agent_revision_id", "agent_revision"),
            ("prompt_bundle_revision_id", "prompt_bundle_revision"),
        ):
            value = attributes.get(source)
            if value is not None and _valid_v2_revision(value):
                execution.append((target, value))
        # Core can carry several policy provenance fields internally. The v2
        # contract deliberately exposes one explicit marker, with a stable
        # precedence that never infers a value from operation order.
        for source in (
            "policy_revision_id",
            "tool_policy_revision_id",
            "model_policy_revision_id",
            "memory_policy_revision_id",
        ):
            value = attributes.get(source)
            if value is not None and _valid_v2_revision(value):
                execution.append(("policy_revision", value))
                break
        values.extend(
            self._string_attribute(f"aura.execution.{key}", value) for key, value in execution
        )
        return values

    @staticmethod
    def _resource_schema_url() -> str:
        return _METADATA_OBSERVATION_V2_SCHEMA_URL

    def _metrics_payload(self, measurements: Sequence[Measurement]) -> bytes:
        data_points = [
            {
                "attributes": self._observation_attributes(item),
                "timeUnixNano": str(int(item.observed_at.timestamp() * 1_000_000_000)),
                "asDouble": item.value,
            }
            for item in measurements
        ]
        payload = {
            "resourceMetrics": [
                {
                    "resource": {"attributes": self._resource_attributes()},
                    "schemaUrl": self._resource_schema_url(),
                    "scopeMetrics": [
                        {
                            "scope": {"name": "aura.core.telemetry", "version": "1.0.0"},
                            "metrics": [
                                {
                                    "name": "aura.metadata.measurement",
                                    "gauge": {"dataPoints": data_points},
                                }
                            ],
                        }
                    ],
                }
            ]
        }
        return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")

    def _traces_payload(self, measurements: Sequence[Measurement]) -> bytes:
        spans: list[dict[str, object]] = []
        for item in measurements:
            attributes = self._observation_attributes(item)
            # Some background Core paths use UUID(int=0) as their internal
            # "no trace" sentinel.  OTLP treats an all-zero trace ID as
            # unset and drops it from JSON, which makes the Collector reject
            # the whole batch.  Give that one sentinel a stable, content-free
            # ID derived from the durable observation ID while preserving all
            # supplied span parent/link fields.
            trace_id = (
                uuid5(_OTLP_NO_TRACE_NAMESPACE, f"observation:{item.observation_id}").hex
                if item.trace_id == _OTLP_NO_TRACE_ID
                else item.trace_id
            )
            end_ns = int(item.observed_at.timestamp() * 1_000_000_000)
            start_ns = max(0, end_ns - int(item.value * 1_000_000))
            span: dict[str, object] = {
                "traceId": trace_id,
                "spanId": item.span_id,
                "name": dict(item.trace_attributes).get("operation", item.metric),
                "startTimeUnixNano": str(start_ns),
                "endTimeUnixNano": str(end_ns),
                "attributes": attributes,
                "status": {
                    "code": "STATUS_CODE_ERROR"
                    if dict(item.dimensions).get("outcome") in {"error", "failed"}
                    else "STATUS_CODE_OK"
                },
            }
            if item.parent_span_id is not None:
                span["parentSpanId"] = item.parent_span_id
            if item.link_span_ids:
                span["links"] = [
                    {"traceId": trace_id, "spanId": link} for link in item.link_span_ids
                ]
            spans.append(span)
        payload = {
            "resourceSpans": [
                {
                    "resource": {"attributes": self._resource_attributes()},
                    "schemaUrl": self._resource_schema_url(),
                    "scopeSpans": [
                        {
                            "scope": {"name": "aura.core.telemetry", "version": "1.0.0"},
                            "spans": spans,
                        }
                    ],
                }
            ]
        }
        return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")


class _DefaultOtlpHttpTransport:
    async def send(
        self, endpoint: str, payload: bytes, credential: str | None, timeout: float
    ) -> None:
        headers = {"Content-Type": "application/json"}
        if credential:
            headers["Authorization"] = f"Bearer {credential}"

        def request() -> None:
            request = urllib.request.Request(endpoint, data=payload, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    if response.status >= 300:
                        raise urllib.error.HTTPError(
                            endpoint,
                            response.status,
                            "Collector rejected telemetry",
                            response.headers,
                            None,
                        )
            except urllib.error.URLError as exc:
                raise ConnectionError("Collector telemetry export failed") from exc

        await asyncio.to_thread(request)


# Short public name used by composition and test callers. Keep the transport
# detail in the longer class name for readability in tracebacks.
OtlpTelemetryExporter = OtlpHttpTelemetryExporter


class CompositeTelemetryExporter:
    """Fan out without coupling the structured and OTLP failure domains."""

    def __init__(self, *exporters: TelemetryExporter) -> None:
        self.exporters = exporters

    async def start(self) -> None:
        for exporter in self.exporters:
            start = getattr(exporter, "start", None)
            if callable(start):
                await cast(Callable[[], Awaitable[None]], start)()

    async def export(self, batch: TelemetryBatch) -> None:
        first_error: BaseException | None = None
        for exporter in self.exporters:
            try:
                await exporter.export(batch)
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error

    async def stop(self) -> None:
        for exporter in reversed(self.exporters):
            stop = getattr(exporter, "stop", None)
            if callable(stop):
                await cast(Callable[[], Awaitable[None]], stop)()


def make_telemetry_exporter(
    *,
    enabled: bool = False,
    endpoint: str = "http://127.0.0.1:4318",
    credential: str | None = None,
    queue_capacity: int = 32,
    max_attempts: int = 3,
    timeout_seconds: float = 2.0,
    deployment_id: str = "local",
    git_commit: str = "0000000",
    environment: str = "development",
    host_id: str = "local",
    service_role: str = "api",
    image_version: str | None = None,
    configuration_version: str | None = None,
) -> TelemetryExporter:
    """Compose Core's structured exporter and optional independent OTLP sink."""

    role_map = {
        "api": "api",
        "worker": "worker",
        "scheduler": "scheduler",
        "cli": "cli",
        "migrate": "cli",
    }
    if service_role not in role_map:
        raise ValueError("service role must be api, worker, scheduler, cli, or migrate")
    deployment = DeploymentMetadata(
        deployment_id=deployment_id,
        git_commit=git_commit,
        environment=environment,
        host_id=host_id,
        service_role=role_map[service_role],
        image_version=image_version,
        configuration_version=configuration_version,
    )
    if not enabled:
        return StructuredContainerLogExporter(deployment=deployment)
    otlp = OtlpHttpTelemetryExporter(
        endpoint,
        credential=credential,
        queue_capacity=queue_capacity,
        max_attempts=max_attempts,
        timeout_seconds=timeout_seconds,
        deployment=deployment,
    )
    structured = StructuredContainerLogExporter(
        deployment=deployment,
        health_provider=lambda: otlp.stats,
    )
    # Queue first so the structured health line reports the latest accepted
    # count; the OTLP sink never raises transport failures into this fanout.
    return CompositeTelemetryExporter(otlp, structured)


class MetadataMetrics:
    """Nonblocking bounded sink with metric/trace cardinality separation."""

    def __init__(
        self,
        *,
        capacity: int = 2048,
        exporter: TelemetryExporter | None = None,
        max_export_attempts: int = 3,
    ) -> None:
        if capacity < 1:
            raise ValueError("telemetry capacity must be positive")
        if max_export_attempts < 1:
            raise ValueError("telemetry export attempts must be positive")
        self._capacity = capacity
        self._measurements: deque[Measurement] = deque()
        self._exporter = exporter
        self._max_export_attempts = max_export_attempts
        self._current_export_attempts = 0
        self._dropped = 0
        self._rejected = 0
        self._export_failures = 0
        self._abandoned_batches = 0

    @property
    def exporter(self) -> TelemetryExporter | None:
        return self._exporter

    def increment(
        self,
        component: str,
        name: str,
        *,
        trace_id: str | None = None,
        run_id: str | None = None,
        conversation_id: str | None = None,
        **attributes: str,
    ) -> None:
        self._record_metric(
            component,
            name,
            1.0,
            trace_id=trace_id,
            run_id=run_id,
            conversation_id=conversation_id,
            attributes=attributes,
        )

    def observe(
        self,
        component: str,
        name: str,
        value: float,
        *,
        trace_id: str | None = None,
        run_id: str | None = None,
        conversation_id: str | None = None,
        **attributes: str,
    ) -> None:
        self._record_metric(
            component,
            name,
            value,
            trace_id=trace_id,
            run_id=run_id,
            conversation_id=conversation_id,
            attributes=attributes,
        )

    def record_span(
        self,
        component: str,
        operation: str,
        duration_ms: float,
        *,
        trace_id: str,
        span_id: str,
        parent_span_id: str | None,
        dependency: str,
        outcome: str,
        error_class: str | None = None,
        provider: str | None = None,
        retrieval_stage: str | None = None,
        scope_type: str | None = None,
        degradation: str | None = None,
        connection_health: str | None = None,
        rate_limit_state: str | None = None,
        event_count_bucket: str | None = None,
        retry_class: str | None = None,
        run_id: str | None = None,
        conversation_id: str | None = None,
        metric_name: str | None = None,
        link_span_ids: Sequence[str] = (),
        **trace_attributes: str,
    ) -> None:
        trace_id = cast(str, _ensure_workflow_trace_id(component, trace_id))
        if component in _WORKFLOW_TELEMETRY_COMPONENTS and dependency == "workflow":
            # Older public workflow ports use the generic dependency label;
            # keep the closed telemetry vocabulary and historical lineage
            # contract stable at this boundary.
            dependency = "application_validation"
        memory_context = current_memory_trace_context()
        if memory_context is not None and component in {
            "aura.knowledge.memory_extraction",
            "aura.knowledge.memory_maintenance",
            "aura.runtime.structured_inference",
            "aura.runtime.embedding_gateway",
            "aura.knowledge.memory_persistence",
            "aura.knowledge.memory_retrieval",
        }:
            trace_id = memory_context.trace_id
            parent_span_id = parent_span_id or memory_context.span_id
            trace_attributes = {**trace_attributes, **memory_context.attributes()}
            link_span_ids = (*memory_context.link_span_ids, *link_span_ids)
        if operation not in _SPAN_OPERATIONS.get(component, frozenset()):
            self._rejected += 1
            return
        if component == "aura.runtime.prompt_compilation":
            dependency = "prompt_compiler"
        dimensions = {"dependency": dependency, "outcome": outcome}
        if component == "aura.automation.reminder_management" and operation.startswith(
            "reminder.series."
        ):
            dimensions["operation"] = operation.removeprefix("reminder.series.")
        elif (
            component == "aura.automation.reminder_scheduler"
            and operation == "reminder.series.advance"
        ):
            dimensions["operation"] = "fire"
        elif component == "aura.automation.workflow_state":
            dimensions["operation"] = operation
        if error_class is not None:
            dimensions["error_class"] = error_class
        if provider is not None:
            dimensions["provider"] = provider
        if retrieval_stage is not None:
            dimensions["retrieval_stage"] = retrieval_stage
        if scope_type is not None:
            dimensions["scope_type"] = scope_type
        if degradation is not None:
            dimensions["degradation"] = degradation
        if connection_health is not None:
            dimensions["connection_health"] = connection_health
        if rate_limit_state is not None:
            dimensions["rate_limit_state"] = rate_limit_state
        if event_count_bucket is not None:
            dimensions["event_count_bucket"] = event_count_bucket
        if retry_class is not None:
            dimensions["retry_class"] = retry_class
        normalized = self._normalize(
            component,
            trace_id,
            run_id,
            conversation_id,
            dimensions,
            trace_attributes,
        )
        links = tuple(dict.fromkeys(link_span_ids))
        if (
            normalized is None
            or not _valid_span_id(span_id)
            or parent_span_id is not None
            and not _valid_span_id(parent_span_id)
            or any(not _valid_span_id(link) for link in links)
        ):
            self._rejected += 1
            return
        self._append(
            Measurement(
                kind="span",
                component_id=component,
                component_version=COMPONENT_VERSIONS[component],
                metric=metric_name
                or _OPERATION_SPAN_METRICS.get((component, operation), _SPAN_METRICS[component]),
                value=float(duration_ms),
                observed_at=datetime.now(UTC),
                dimensions=normalized[0],
                trace_id=trace_id,
                span_id=span_id,
                parent_span_id=parent_span_id,
                link_span_ids=links,
                trace_attributes=(("operation", operation), *normalized[1]),
            )
        )
        if component == "aura.runtime.prompt_compilation":
            trace_values = dict(normalized[1])
            for metric, attribute in (
                ("prompt_component_count", "prompt_component_count"),
                ("compiled_prompt_size", "compiled_prompt_size"),
            ):
                value = trace_values.get(attribute)
                if value is not None:
                    self._record_metric(
                        component,
                        metric,
                        float(value),
                        trace_id=trace_id,
                        run_id=run_id,
                        conversation_id=conversation_id,
                        attributes={},
                    )

    def _record_metric(
        self,
        component: str,
        metric: str,
        value: float,
        *,
        trace_id: str | None,
        run_id: str | None,
        conversation_id: str | None,
        attributes: Mapping[str, str],
    ) -> None:
        trace_id = _ensure_workflow_trace_id(component, trace_id)
        if (
            component in _WORKFLOW_TELEMETRY_COMPONENTS
            and attributes.get("dependency") == "workflow"
        ):
            attributes = {**attributes, "dependency": "application_validation"}
        memory_context = current_memory_trace_context()
        if memory_context is not None and component in {
            "aura.knowledge.memory_extraction",
            "aura.knowledge.memory_maintenance",
            "aura.runtime.structured_inference",
            "aura.runtime.embedding_gateway",
            "aura.knowledge.memory_persistence",
            "aura.knowledge.memory_retrieval",
        }:
            trace_id = memory_context.trace_id
            run_id = memory_context.run_id or run_id
            conversation_id = memory_context.conversation_id or conversation_id
            attributes = {**attributes, **memory_context.attributes()}
        if metric not in _METRICS.get(component, frozenset()) or not math.isfinite(value):
            self._rejected += 1
            return
        dimensions = {key: value for key, value in attributes.items() if key in _DIMENSION_KEYS}
        trace_attributes = {
            key: value for key, value in attributes.items() if key in _TRACE_ATTRIBUTE_KEYS
        }
        if len(dimensions) + len(trace_attributes) != len(attributes):
            self._rejected += 1
            return
        normalized = self._normalize(
            component,
            trace_id,
            run_id,
            conversation_id,
            dimensions,
            trace_attributes,
        )
        if normalized is None:
            self._rejected += 1
            return
        self._append(
            Measurement(
                kind="metric",
                component_id=component,
                component_version=COMPONENT_VERSIONS[component],
                metric=metric,
                value=float(value),
                observed_at=datetime.now(UTC),
                dimensions=normalized[0],
                trace_id=trace_id,
                trace_attributes=normalized[1],
            )
        )

    def _normalize(
        self,
        component: str,
        trace_id: str | None,
        run_id: str | None,
        conversation_id: str | None,
        dimensions: Mapping[str, str],
        trace_attributes: Mapping[str, str],
    ) -> tuple[tuple[tuple[str, str], ...], tuple[tuple[str, str], ...]] | None:
        if component not in COMPONENT_VERSIONS:
            return None
        if trace_id is not None and not _valid_trace_id(trace_id):
            return None
        complete_trace_attributes = dict(trace_attributes)
        if run_id is not None:
            complete_trace_attributes["run_id"] = run_id
        if conversation_id is not None:
            complete_trace_attributes["conversation_id"] = conversation_id
        normalized_dimensions: list[tuple[str, str]] = []
        for key, value in dimensions.items():
            if key not in _DIMENSION_KEYS or _looks_like_uuid(value):
                return None
            if component == "aura.automation.workflow_state":
                if key == "operation" and value not in _WORKFLOW_OPERATIONS:
                    return None
                if key == "dependency" and value not in _WORKFLOW_DEPENDENCIES:
                    return None
                if key == "outcome" and value not in _WORKFLOW_OUTCOMES:
                    return None
                if key == "error_class" and value not in _WORKFLOW_ERROR_CLASSES:
                    return None
            allowed = _ENUM_DIMENSIONS.get(key)
            if allowed is not None and value not in allowed:
                return None
            if key == "provider" and (
                _SAFE_NAME.fullmatch(value) is None or "://" in value or "@" in value
            ):
                return None
            normalized_dimensions.append((key, value))
        normalized_trace: list[tuple[str, str]] = []
        for key, value in complete_trace_attributes.items():
            if key not in _TRACE_ATTRIBUTE_KEYS:
                return None
            if key in _UUID_TRACE_ATTRIBUTES and not _valid_uuid(value):
                return None
            if (
                key in _COUNT_TRACE_ATTRIBUTES
                and key != "operation_duration_ms"
                and (not value.isascii() or not value.isdecimal())
            ):
                return None
            if key == "operation_duration_ms":
                try:
                    if not math.isfinite(float(value)) or float(value) < 0:
                        return None
                except ValueError:
                    return None
            if key == "configuration_outcome" and value not in {"ok", "error"}:
                return None
            if key == "archive_state" and value not in {"active", "archived", "all"}:
                return None
            if key == "model_id" and (
                _SAFE_NAME.fullmatch(value) is None or "://" in value or "@" in value
            ):
                return None
            if key == "idempotency" and value not in {
                "read_only",
                "idempotent",
                "non_idempotent",
                "approval-derived",
            }:
                return None
            normalized_trace.append((key, value))
        return tuple(sorted(normalized_dimensions)), tuple(sorted(normalized_trace))

    def _append(self, measurement: Measurement) -> None:
        if len(self._measurements) == self._capacity:
            self._measurements.popleft()
            self._dropped += 1
        self._measurements.append(measurement)

    def snapshot(self) -> tuple[Measurement, ...]:
        return tuple(self._measurements)

    def stats(self) -> TelemetryStats:
        return TelemetryStats(
            retained=len(self._measurements),
            capacity=self._capacity,
            dropped=self._dropped,
            rejected=self._rejected,
            export_failures=self._export_failures,
            abandoned_batches=self._abandoned_batches,
            exporter_configured=self._exporter is not None,
        )

    async def flush(self) -> bool:
        """Export current records and aggregate health with bounded retries."""

        if self._exporter is None:
            return False
        batch_measurements = tuple(self._measurements)
        batch = TelemetryBatch(batch_measurements, self.stats())
        try:
            await self._exporter.export(batch)
        except Exception:
            self._export_failures += 1
            self._current_export_attempts += 1
            if self._current_export_attempts >= self._max_export_attempts:
                for expected in batch_measurements:
                    if self._measurements and self._measurements[0] is expected:
                        self._measurements.popleft()
                        self._dropped += 1
                self._abandoned_batches += 1
                self._current_export_attempts = 0
            return False
        for expected in batch_measurements:
            if self._measurements and self._measurements[0] is expected:
                self._measurements.popleft()
        self._current_export_attempts = 0
        return True


class TelemetryLifecycle:
    """Periodic exporter lifecycle shared by API and worker composition roots."""

    def __init__(self, metrics: MetadataMetrics, *, interval_seconds: float = 5.0) -> None:
        if interval_seconds <= 0:
            raise ValueError("telemetry flush interval must be positive")
        self.metrics = metrics
        self.interval_seconds = interval_seconds
        self._task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if not self.running:
            start_exporter = getattr(self.metrics.exporter, "start", None)
            if callable(start_exporter):
                await cast(Callable[[], Awaitable[None]], start_exporter)()
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> bool:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        result = await self.metrics.flush()
        stop_exporter = getattr(self.metrics.exporter, "stop", None)
        if callable(stop_exporter):
            await cast(Callable[[], Awaitable[None]], stop_exporter)()
        return result

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self.interval_seconds)
            await self.metrics.flush()


def new_span_id() -> str:
    return secrets.token_hex(8)


def root_span_id(trace_id: str) -> str:
    if not _valid_trace_id(trace_id):
        raise ValueError("trace id must be 32 lowercase hex characters")
    return trace_id[:16]


def _write_stdout(line: str) -> None:
    sys.stdout.write(f"{line}\n")
    sys.stdout.flush()


def _valid_uuid(value: str) -> bool:
    try:
        UUID(value)
    except ValueError, AttributeError:
        return False
    return True


def _looks_like_uuid(value: str) -> bool:
    return _valid_uuid(value)


def _valid_trace_id(value: str) -> bool:
    return len(value) == 32 and all(character in "0123456789abcdef" for character in value)


def _ensure_workflow_trace_id(component: str, trace_id: str | None) -> str | None:
    """Return a non-zero trace id for workflow records.

    Transition helpers intentionally use an all-zero sentinel when called in
    isolation.  That sentinel is useful for deterministic unit tests but is
    not a valid production lineage.  Normalize it only for workflow records;
    all other telemetry retains its existing optional trace semantics.
    """

    if component not in _WORKFLOW_TELEMETRY_COMPONENTS:
        return trace_id
    if trace_id is None or not _valid_trace_id(trace_id) or set(trace_id) == {"0"}:
        return secrets.token_hex(16)
    return trace_id


def _valid_span_id(value: str) -> bool:
    return len(value) == 16 and all(character in "0123456789abcdef" for character in value)


class Stopwatch:
    def __init__(self) -> None:
        self.started = monotonic()

    def elapsed_ms(self) -> float:
        return (monotonic() - self.started) * 1000


def record_memory_operation(
    metrics: MetadataMetrics,
    timer: Stopwatch,
    *,
    outcome: str,
    scope_type: str,
    lifecycle_status: str,
    dependency: str,
    trace_id: str,
    memory_id: str | None = None,
    memory_revision_id: str | None = None,
    generation_id: str | None = None,
    error_class: str | None = None,
    operation: str = "memory.persist",
    span_id: str | None = None,
    parent_span_id: str | None = None,
) -> None:
    """Emit the complete memory measurement set under one trace correlation."""

    bounded_scope = scope_type if scope_type in {"user", "agent"} else "unknown"
    bounded_status = (
        lifecycle_status
        if lifecycle_status
        in {"active", "dormant", "archived", "disabled", "disputed", "superseded"}
        else "unknown"
    )
    identifiers: dict[str, str] = {}
    if memory_id is not None:
        identifiers["memory_id"] = memory_id
    if memory_revision_id is not None:
        identifiers["memory_revision_id"] = memory_revision_id
    if generation_id is not None:
        identifiers["generation_id"] = generation_id
    metrics.record_span(
        "aura.knowledge.memory_persistence",
        operation,
        timer.elapsed_ms(),
        trace_id=trace_id,
        span_id=span_id or new_span_id(),
        parent_span_id=parent_span_id,
        dependency=dependency,
        outcome=outcome,
        error_class=error_class,
        **identifiers,
    )
    # ``trace_id`` is a first-class call argument.  Keeping it out of the
    # attribute map avoids an unregistered duplicate trace dimension and
    # prevents valid memory operations from incrementing telemetry rejection.
    common = dict(identifiers)
    metrics.observe(
        "aura.knowledge.memory_persistence",
        "memory_operation_duration_ms",
        timer.elapsed_ms(),
        trace_id=trace_id,
        outcome=outcome,
        dependency=dependency,
        **common,
    )
    metrics.increment(
        "aura.knowledge.memory_persistence",
        "memory_operation_outcome",
        trace_id=trace_id,
        outcome=outcome,
        dependency=dependency,
        **common,
    )
    metrics.increment(
        "aura.knowledge.memory_persistence",
        "memory_lifecycle_status",
        trace_id=trace_id,
        status=bounded_status,
        **common,
    )
    metrics.increment(
        "aura.knowledge.memory_persistence",
        "memory_scope_type",
        trace_id=trace_id,
        scope_type=bounded_scope,
        **common,
    )


def record_memory_processing(
    metrics: MetadataMetrics,
    *,
    component: str,
    operation: str = "retrieval",
    duration_ms: float,
    trace_id: str,
    outcome: str,
    dependency: str,
    error_class: str | None = None,
    memory_id: str | None = None,
    memory_revision_id: str | None = None,
    generation_id: str | None = None,
    attempt_count: int | None = None,
    backlog: int | None = None,
    progress: float | None = None,
) -> None:
    """Record worker/model lifecycle metadata with one correlated trace."""

    # Processing also emits maintenance/reindex callbacks through the
    # extraction recorder. Classify by operation so lifecycle and backlog
    # signals remain owned by the maintenance component.
    effective_component = component
    if operation in {
        "memory.maintenance",
        "memory.decay",
        "memory.archive",
        "memory.reindex.chunk",
        "memory.reindex.switch",
    }:
        effective_component = "aura.knowledge.memory_maintenance"

    attrs: dict[str, str] = {}
    if memory_id:
        attrs["memory_id"] = memory_id
    if memory_revision_id:
        attrs["memory_revision_id"] = memory_revision_id
    if generation_id:
        attrs["generation_id"] = generation_id
    if attempt_count is not None:
        attrs["attempt_count"] = str(max(0, attempt_count))
    metrics.record_span(
        effective_component,
        operation,
        duration_ms,
        trace_id=trace_id,
        span_id=new_span_id(),
        parent_span_id=None,
        dependency=dependency,
        outcome=outcome,
        error_class=error_class,
        metric_name=_OPERATION_SPAN_METRICS.get((effective_component, operation)),
        **attrs,
    )
    # Core services report semantic boundary operations; keep the mapping in
    # the telemetry platform so domains do not depend on metric names.
    semantic_metrics: Mapping[str, tuple[str, ...]] = {
        "memory.job.queue": (),
        "memory.job": ("memory_job_outcome",),
        "memory.extraction": ("memory_extraction_outcome",),
        "memory.retention_gate": ("memory_personal_retention_gate_outcome",),
        "memory.policy": ("memory_candidate_decision",),
        "memory.fallback": ("memory_fallback_outcome",),
        "memory.candidate": ("memory_candidate_outcome",),
        "memory.action": ("memory_action_outcome",),
        "memory.embedding": ("embedding_duration_ms", "missing_embedding_backlog"),
        # A sweep measures maintenance duration. Lifecycle transitions are
        # emitted only by the concrete decay/archive operations.
        "memory.maintenance": (),
        "memory.decay": ("memory_maintenance_transition",),
        "memory.archive": ("memory_maintenance_transition",),
        "memory.reindex.chunk": (
            "memory_reindex_progress",
            "memory_reindex_backlog",
        ),
        "memory.reindex.switch": (),
        "memory.retry": ("memory_retry_count",),
        "memory.error": ("memory_processing_errors",),
    }
    for semantic_metric in semantic_metrics.get(operation, ()):
        if semantic_metric.endswith("_duration_ms") or semantic_metric.endswith("_wait_ms"):
            metrics.observe(
                effective_component,
                semantic_metric,
                duration_ms,
                trace_id=trace_id,
                outcome=outcome,
                dependency=dependency,
            )
        elif semantic_metric.endswith("_progress"):
            metrics.observe(
                effective_component,
                semantic_metric,
                float(progress if progress is not None else 0),
                trace_id=trace_id,
                outcome=outcome,
                dependency=dependency,
            )
        elif semantic_metric.endswith("_backlog"):
            metrics.observe(
                effective_component,
                semantic_metric,
                float(max(0, backlog or 0)),
                trace_id=trace_id,
                outcome=outcome,
                dependency=dependency,
            )
        else:
            metrics.increment(
                effective_component,
                semantic_metric,
                outcome=outcome,
                dependency=dependency,
                trace_id=trace_id,
            )
    if operation == "memory.candidate" and outcome == "review":
        metrics.increment(
            effective_component,
            "memory_reviewable_candidate_count",
            outcome="review",
            dependency=dependency,
            trace_id=trace_id,
        )
    if backlog is not None and "backlog" in _METRICS.get(effective_component, frozenset()):
        metrics.observe(
            effective_component,
            "backlog",
            float(max(0, backlog)),
            trace_id=trace_id,
            dependency=dependency,
        )


_RETRIEVAL_OPERATION_ALIASES: Mapping[str, str] = {
    "retrieval": "memory.retrieval",
    "memory.retrieval": "memory.retrieval",
    "memory.retrieval.generation": "memory.retrieval",
    "primary": "memory.retrieval",
    "memory.retrieval.primary": "memory.retrieval",
    "query_embedding": "memory.query_embedding",
    "memory.retrieval.query_embedding": "memory.query_embedding",
    "memory.query_embedding": "memory.query_embedding",
    "lexical": "memory.lexical",
    "memory.retrieval.lexical": "memory.lexical",
    "memory.lexical": "memory.lexical",
    "vector": "memory.vector",
    "memory.retrieval.vector": "memory.vector",
    "memory.vector": "memory.vector",
    "fusion": "memory.fusion",
    "memory.retrieval.fusion": "memory.fusion",
    "memory.fusion": "memory.fusion",
    "rerank": "memory.rerank",
    "memory.retrieval.rerank": "memory.rerank",
    "memory.rerank": "memory.rerank",
    "selection": "memory.selection",
    "memory.retrieval.selection": "memory.selection",
    "memory.selection": "memory.selection",
    "context_budget": "memory.context_budget",
    "memory.retrieval.context_budget": "memory.context_budget",
    "memory.context_budget": "memory.context_budget",
    "memory.context_budget.post_render": "memory.context_budget",
    "memory.retrieval.context_budget.post_render": "memory.context_budget",
    "fallback": "memory.fallback",
    "memory.retrieval.fallback": "memory.fallback",
    "memory.fallback": "memory.fallback",
    "degradation": "memory.degradation",
    "memory.retrieval.degradation": "memory.degradation",
    "memory.degradation": "memory.degradation",
}
_RETRIEVAL_SPAN_METRICS: Mapping[str, str] = {
    "memory.retrieval": "memory_retrieval_duration_ms",
    "memory.query_embedding": "memory_query_embedding_duration_ms",
    "memory.lexical": "memory_lexical_search_duration_ms",
    "memory.vector": "memory_vector_search_duration_ms",
    "memory.rerank": "memory_rerank_duration_ms",
    # Fusion, selection, budget, fallback, and degradation are represented by
    # bounded stage dimensions and use the enclosing retrieval duration name.
    "memory.fusion": "memory_retrieval_duration_ms",
    "memory.selection": "memory_retrieval_duration_ms",
    "memory.context_budget": "memory_retrieval_duration_ms",
    "memory.fallback": "memory_retrieval_duration_ms",
    "memory.degradation": "memory_retrieval_duration_ms",
}


def record_memory_retrieval(
    metrics: MetadataMetrics,
    *,
    operation: str = "retrieval",
    duration_ms: float,
    trace_id: str,
    outcome: str,
    dependency: str = "memory_store",
    parent_span_id: str | None = None,
    error_class: str | None = None,
    retrieval_stage: str | None = None,
    scope_type: str | None = None,
    degradation: str | None = None,
    candidate_count: int | None = None,
    recall_count: int | None = None,
    fallback_outcome: str | None = None,
    context_tokens: int | None = None,
    memory_id: str | None = None,
    memory_revision_id: str | None = None,
    memory_policy_revision_id: str | None = None,
    tool_policy_revision_id: str | None = None,
    generation_id: str | None = None,
    embedding_generation_id: str | None = None,
    run_id: str | None = None,
    conversation_id: str | None = None,
    retrieval_version: str | None = None,
    selection_order: int | None = None,
    fallback_grant_id: str | None = None,
    recall_mode: str | None = None,
    gate_outcome: str | None = None,
) -> None:
    """Record one retrieval boundary and its bounded recall metadata.

    This is the only telemetry seam retrieval code needs.  It deliberately
    accepts identifiers and counts, never memory text, evidence, vectors,
    prompts, responses, credentials, or owner subjects.  A caller can emit
    stage-specific observations without coupling Core retrieval code to the
    platform metric registry.
    """

    span_operation = _RETRIEVAL_OPERATION_ALIASES.get(operation, operation)
    if span_operation not in _RETRIEVAL_OPERATION_ALIASES.values():
        # Let MetadataMetrics apply its normal rejection accounting while
        # retaining the no-I/O property of the recording path.
        metrics.record_span(
            "aura.knowledge.memory_retrieval",
            span_operation,
            duration_ms,
            trace_id=trace_id,
            span_id=new_span_id(),
            parent_span_id=None,
            dependency=dependency,
            outcome=outcome,
            error_class=error_class,
        )
        return

    # Provider and driver exceptions may arrive as arbitrary class names.
    # Keep those names out of bounded labels and map unknown values to stable
    # privacy-safe buckets before recording.
    bounded_dependency = (
        dependency if dependency in _ENUM_DIMENSIONS["dependency"] else "memory_store"
    )
    bounded_outcome = outcome if outcome in _ENUM_DIMENSIONS["outcome"] else "unknown"
    bounded_error = (
        error_class
        if error_class in _ENUM_DIMENSIONS["error_class"]
        else ("unknown" if error_class is not None else None)
    )
    bounded_fallback = (
        fallback_outcome
        if fallback_outcome in _ENUM_DIMENSIONS["outcome"]
        else ("unknown" if fallback_outcome is not None else None)
    )
    bounded_parent = (
        parent_span_id if parent_span_id is not None and _valid_span_id(parent_span_id) else None
    )
    # The in-memory recall seam may not have a run trace.  Keep the callback
    # valid without serializing the caller's fallback label as a trace id.
    trace_id = trace_id if _valid_trace_id(trace_id) else secrets.token_hex(16)

    bounded_stage = (
        retrieval_stage
        or {
            "memory.retrieval": "primary",
            "memory.query_embedding": "query_embedding",
            "memory.lexical": "lexical",
            "memory.vector": "vector",
            "memory.fusion": "fusion",
            "memory.rerank": "rerank",
            "memory.selection": "selection",
            "memory.context_budget": "context_budget",
            "memory.fallback": "fallback",
            "memory.degradation": "degradation",
        }[span_operation]
    )
    trace_attributes: dict[str, str] = {}
    for key, value in {
        "memory_id": memory_id,
        "memory_revision_id": memory_revision_id,
        "memory_policy_revision_id": memory_policy_revision_id,
        "tool_policy_revision_id": tool_policy_revision_id,
        "generation_id": generation_id,
        "embedding_generation_id": embedding_generation_id,
        "retrieval_version": retrieval_version,
        "fallback_grant_id": fallback_grant_id,
        "recall_mode": recall_mode,
        "gate_outcome": gate_outcome,
    }.items():
        if value is not None:
            trace_attributes[key] = value
    for key, value in {
        "selection_order": selection_order,
        "candidate_count": candidate_count,
        "recalled_count": recall_count,
        "context_tokens": context_tokens,
    }.items():
        if value is not None:
            trace_attributes[key] = str(max(0, value))

    metrics.record_span(
        "aura.knowledge.memory_retrieval",
        span_operation,
        duration_ms,
        trace_id=trace_id,
        span_id=new_span_id(),
        parent_span_id=bounded_parent,
        dependency=bounded_dependency,
        outcome=bounded_outcome,
        error_class=bounded_error,
        retrieval_stage=bounded_stage,
        scope_type=scope_type,
        degradation=degradation,
        run_id=run_id,
        conversation_id=conversation_id,
        metric_name=_RETRIEVAL_SPAN_METRICS[span_operation],
        **trace_attributes,
    )
    common: dict[str, str] = {
        "outcome": bounded_outcome,
        "dependency": bounded_dependency,
        "retrieval_stage": bounded_stage,
    }
    if scope_type is not None:
        common["scope_type"] = scope_type
    if degradation is not None:
        common["degradation"] = degradation
    if bounded_error is not None:
        common["error_class"] = bounded_error
    common.update(trace_attributes)

    if candidate_count is not None:
        metrics.observe(
            "aura.knowledge.memory_retrieval",
            "memory_retrieval_candidate_count",
            float(max(0, candidate_count)),
            trace_id=trace_id,
            run_id=run_id,
            conversation_id=conversation_id,
            **common,
        )
    if recall_count is not None:
        metrics.observe(
            "aura.knowledge.memory_retrieval",
            "memory_recall_count",
            float(max(0, recall_count)),
            trace_id=trace_id,
            run_id=run_id,
            conversation_id=conversation_id,
            **common,
        )
    if fallback_outcome is not None:
        metrics.increment(
            "aura.knowledge.memory_retrieval",
            "memory_fallback_outcome",
            outcome=bounded_fallback or "unknown",
            retrieval_stage="fallback",
            dependency=bounded_dependency,
            trace_id=trace_id,
            run_id=run_id,
            conversation_id=conversation_id,
            **{key: value for key, value in trace_attributes.items()},
        )
    if gate_outcome is not None:
        metrics.increment(
            "aura.knowledge.memory_retrieval",
            "memory_recall_gate_outcome",
            outcome=(
                gate_outcome if gate_outcome in {"policy_off", "selected", "empty"} else "unknown"
            ),
            retrieval_stage="primary",
            dependency=bounded_dependency,
            trace_id=trace_id,
            run_id=run_id,
            conversation_id=conversation_id,
        )
    if context_tokens is not None:
        metrics.observe(
            "aura.knowledge.memory_retrieval",
            "memory_context_tokens",
            float(max(0, context_tokens)),
            trace_id=trace_id,
            run_id=run_id,
            conversation_id=conversation_id,
            **common,
        )
    if bounded_error is not None or bounded_outcome in {"error", "degraded"}:
        metrics.increment(
            "aura.knowledge.memory_retrieval",
            "memory_retrieval_errors",
            outcome=bounded_outcome,
            dependency=bounded_dependency,
            error_class=bounded_error or "persistence",
            retrieval_stage=bounded_stage,
            trace_id=trace_id,
            run_id=run_id,
            conversation_id=conversation_id,
            **{key: value for key, value in trace_attributes.items()},
        )
