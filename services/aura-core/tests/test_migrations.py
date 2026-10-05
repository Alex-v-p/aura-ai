import pytest
from alembic import command
from aura_core.domains.execution.runs.persistence import RunEventRow, RunRow
from aura_core.domains.interaction.agents.persistence import AgentRevisionRow
from aura_core.domains.interaction.conversations.persistence import (
    ConversationRow,
    MessageRow,
)
from aura_core.entrypoints.cli import alembic_config
from aura_core.platform.auth import Settings


def test_real_alembic_config_runs_offline_without_logging_sections(
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = alembic_config(
        Settings(database_url="postgresql+psycopg://aura:aura@database.invalid/aura")
    )

    # The first real revision is sufficient to execute migrations/env.py and
    # exercise the same Config produced by the CLI without requiring a live
    # PostgreSQL connection in this focused regression.
    command.upgrade(config, "0001_core_foundation", sql=True)

    output = capsys.readouterr().out
    assert "fk_conversations_agent_profile_id_agent_profiles" in output
    assert "fk_runs_conversation_id_conversations" in output


def test_runtime_mappings_only_link_tables_within_their_own_domain() -> None:
    assert not ConversationRow.principal_id.foreign_keys
    assert not ConversationRow.agent_profile_id.foreign_keys
    assert not ConversationRow.agent_revision_id.foreign_keys
    assert not RunRow.conversation_id.foreign_keys
    assert not RunRow.user_message_id.foreign_keys
    assert not RunRow.agent_revision_id.foreign_keys
    assert not RunRow.model_policy_revision_id.foreign_keys
    assert not RunEventRow.conversation_id.foreign_keys

    assert MessageRow.conversation_id.foreign_keys
    assert RunEventRow.run_id.foreign_keys
    assert AgentRevisionRow.agent_profile_id.foreign_keys
