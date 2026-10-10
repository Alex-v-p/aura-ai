"""Aura-owned, provider-neutral tool execution primitives.

The runtime deliberately contains no provider or persistence adapters.  A
composition root supplies the small application callbacks used by executable
tools (currently only ``memory.search``).
"""

from aura_core.runtime.tools.public import (
    MEMORY_SEARCH_TOOL_ID,
    MEMORY_SEARCH_TOOL_VERSION,
    REMINDER_CREATE_TOOL_ID,
    REMINDER_CREATE_TOOL_VERSION,
    MemorySearchContext,
    ReminderCreateContext,
    ToolCall,
    ToolDescriptor,
    ToolExecutionError,
    ToolExecutionResult,
    ToolExecutor,
    ToolPolicyDecision,
    ToolPolicyEvaluator,
    ToolRegistry,
    default_tool_registry,
    validate_tool_arguments,
    validate_tool_result,
)

__all__ = [
    "MEMORY_SEARCH_TOOL_ID",
    "MEMORY_SEARCH_TOOL_VERSION",
    "REMINDER_CREATE_TOOL_ID",
    "REMINDER_CREATE_TOOL_VERSION",
    "MemorySearchContext",
    "ReminderCreateContext",
    "ToolCall",
    "ToolDescriptor",
    "ToolExecutionError",
    "ToolExecutionResult",
    "ToolExecutor",
    "ToolPolicyDecision",
    "ToolPolicyEvaluator",
    "ToolRegistry",
    "default_tool_registry",
    "validate_tool_arguments",
    "validate_tool_result",
]
