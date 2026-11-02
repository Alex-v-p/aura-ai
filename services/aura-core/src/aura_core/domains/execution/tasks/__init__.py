"""Owner-private, source-authoritative durable task projection."""

from aura_core.domains.execution.tasks.contracts import *  # noqa: F403
from aura_core.domains.execution.tasks.ports import TaskSourcePort
from aura_core.domains.execution.tasks.repository import (
    MemoryTaskProjectionRepository,
    TaskProjectionRepository,
)
from aura_core.domains.execution.tasks.service import (
    TaskProjectionConflict,
    TaskProjectionNotFound,
    TaskProjectionService,
)
from aura_core.domains.execution.tasks.transport import (
    NatsTaskSourceInvalidationConsumer,
    TaskSourceInvalidationStage,
    make_task_source_invalidation_command,
    parse_task_source_invalidation,
)

__all__ = [
    "TaskSourcePort",
    "MemoryTaskProjectionRepository",
    "TaskProjectionRepository",
    "TaskProjectionConflict",
    "TaskProjectionNotFound",
    "TaskProjectionService",
    "NatsTaskSourceInvalidationConsumer",
    "TaskSourceInvalidationStage",
    "make_task_source_invalidation_command",
    "parse_task_source_invalidation",
]
