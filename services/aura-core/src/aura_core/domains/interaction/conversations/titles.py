"""Deterministic title-input and model-output handling.

Title generation is presentation metadata, not a second conversation prompt.
This module keeps the neutral instruction and sanitization rules independent of
provider adapters and of the selected agent/persona prompt compilation.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from aura_core.domains.execution.runs.dto import Run, RunStatus
from aura_core.domains.interaction.conversations.dto import (
    Conversation,
    MessageRole,
    MessageState,
    PendingTitle,
    TitleState,
)

TITLE_SYSTEM_PROMPT = (
    "Create a neutral plain-text title for this conversation. "
    "Use 3 to 8 words and at most 72 characters. Return only the title."
)
_MAX_EXCHANGE_CHARS = 2000
_MAX_TITLE_CHARS = 72
_MAX_TITLE_WORDS = 8
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_LEADING_TITLE = re.compile(r"^title\s*:\s*", re.IGNORECASE)
_LEADING_MARKDOWN = re.compile(r"^(?:[#>*_`~]|[-+])\s*")


def validate_manual_title(value: str) -> str:
    """Trim and validate a user-provided title at the domain boundary."""

    if any(
        ord(character) < 0x20
        or 0x7F <= ord(character) <= 0x9F
        or ord(character) in {0x2028, 0x2029}
        for character in value
    ):
        raise ValueError("title contains forbidden control characters")
    title = value.strip()
    if not 1 <= len(title) <= 255:
        raise ValueError("title must be 1 to 255 characters")
    return title


def pending_title_for(conversation: Conversation, run: Run) -> PendingTitle | None:
    """Return the first completed exchange eligible for title settlement.

    A run can be redelivered after the assistant snapshot was committed.  The
    pending state and first-completed-run check make repeated settlement safe,
    while partial/failed/interrupted output is excluded from the input.
    """

    if conversation.title_state is not TitleState.PENDING:
        return None
    return _completed_title_for(conversation, run)


def fallback_title_candidate(conversation: Conversation, run: Run) -> PendingTitle | None:
    """Build bounded fallback input while preserving assistant eligibility."""

    if conversation.title_state is not TitleState.PENDING:
        return None
    return _completed_title_for(conversation, run)


def _completed_title_for(
    conversation: Conversation,
    run: Run,
    *,
    require_assistant: bool = True,
) -> PendingTitle | None:
    if run.status is not RunStatus.COMPLETED:
        return None
    user = next(
        (
            message
            for message in conversation.messages
            if message.id == run.user_message_id
            and message.role is MessageRole.USER
            and message.state is MessageState.COMPLETE
        ),
        None,
    )
    assistant = next(
        (
            message
            for message in conversation.messages
            if message.id == run.assistant_message_id
            and message.role is MessageRole.ASSISTANT
            and message.state is MessageState.COMPLETE
        ),
        None,
    )
    if user is None or require_assistant and assistant is None:
        return None
    completed = [
        candidate for candidate in conversation.runs if candidate.status is RunStatus.COMPLETED
    ]
    if require_assistant:
        completed = [
            candidate
            for candidate in completed
            if candidate.assistant_message_id is not None
            and any(
                message.id == candidate.assistant_message_id
                and message.role is MessageRole.ASSISTANT
                and message.state is MessageState.COMPLETE
                for message in conversation.messages
            )
        ]
    if not completed or completed[0].id != run.id:
        return None
    return PendingTitle(
        conversation.id,
        run.id,
        run.model_id,
        user.content[:_MAX_EXCHANGE_CHARS],
        assistant.content[:_MAX_EXCHANGE_CHARS] if assistant is not None else "",
    )


def title_request_messages(candidate: PendingTitle) -> tuple[tuple[str, str], ...]:
    """Build the provider-neutral title-only request without prompt leakage."""

    return (
        ("system", TITLE_SYSTEM_PROMPT),
        (
            "user",
            f"User message:\n{candidate.user_content}\n\nAssistant response:\n"
            f"{candidate.assistant_content}",
        ),
    )


def normalize_generated_title(value: str) -> str | None:
    """Normalize a model response to a bounded, plain-text conversation title."""

    value = _CONTROL_CHARS.sub(" ", value)
    value = " ".join(value.split()).strip()
    if not value:
        return None
    value = _LEADING_TITLE.sub("", value).strip()
    # Remove common wrapping Markdown/quote syntax without changing ordinary
    # punctuation inside a title. Repeat because models often return
    # ``Title: **\"...\"**``.
    previous = None
    while value and value != previous:
        previous = value
        value = _LEADING_MARKDOWN.sub("", value).strip()
        if len(value) >= 2 and value[0] in "'\"`*_“‘" and value[-1] in "'\"`*_”’":
            value = value[1:-1].strip()
        value = _LEADING_TITLE.sub("", value).strip()
    value = _CONTROL_CHARS.sub(" ", value)
    value = " ".join(value.split()).strip(" \t\r\n`*_~\"'“”‘’")
    if not value or not any(character.isalnum() for character in value):
        return None
    words = value.split()
    if len(words) > _MAX_TITLE_WORDS:
        words = words[:_MAX_TITLE_WORDS]
        value = " ".join(words)
    if len(value) > _MAX_TITLE_CHARS:
        value = value[:_MAX_TITLE_CHARS].rsplit(" ", 1)[0]
    value = value.strip()
    if len(value.split()) < 3 or not value:
        return None
    if _CONTROL_CHARS.search(value):
        return None
    return value


def first_completed_run(conversation: Conversation) -> Iterable[Run]:
    """Expose a tiny deterministic seam for stores and focused tests."""

    return (
        run
        for run in conversation.runs
        if run.status is RunStatus.COMPLETED
        and pending_title_for(conversation, run) is not None
    )
