"""Deterministic, turn-aware conversation context selection."""

from aura_core.domains.interaction.conversations.dto import (
    Conversation,
    Message,
    MessageRole,
    MessageState,
)
from aura_core.runtime.models.capacity import estimate_tokens


def build_context(
    conversation: Conversation,
    system_prompt: str,
    budget: int,
) -> list[tuple[str, str]]:
    """Keep the current prompt and budget prior completed turns atomically."""

    messages = {message.id: message for message in conversation.messages}
    current_run = conversation.current_run
    if current_run is None:
        current_run = next(
            (run for run in reversed(conversation.runs) if run.status.value != "completed"),
            None,
        )
    current_prompt = messages.get(current_run.user_message_id) if current_run is not None else None
    if current_prompt is not None and (
        current_prompt.role != MessageRole.USER or current_prompt.state != MessageState.COMPLETE
    ):
        current_prompt = None

    used = estimate_tokens(system_prompt)
    if current_prompt is not None:
        # The prompt being answered is never silently dropped. Provider/model
        # limits can reject an oversized request explicitly at their boundary.
        used += estimate_tokens(current_prompt.content)

    newest_first: list[tuple[Message, Message]] = []
    for run in reversed(conversation.runs):
        if run.status.value != "completed":
            continue
        user = messages.get(run.user_message_id)
        assistant = (
            messages.get(run.assistant_message_id) if run.assistant_message_id is not None else None
        )
        if (
            user is None
            or assistant is None
            or user.state != MessageState.COMPLETE
            or assistant.state != MessageState.COMPLETE
            or user.role != MessageRole.USER
            or assistant.role != MessageRole.ASSISTANT
        ):
            continue
        cost = estimate_tokens(user.content) + estimate_tokens(assistant.content)
        if used + cost > budget:
            break
        newest_first.append((user, assistant))
        used += cost

    selected: list[tuple[str, str]] = [("system", system_prompt)]
    for user, assistant in reversed(newest_first):
        selected.extend(
            ((user.role.value, user.content), (assistant.role.value, assistant.content))
        )
    if current_prompt is not None:
        selected.append((current_prompt.role.value, current_prompt.content))
    return selected
