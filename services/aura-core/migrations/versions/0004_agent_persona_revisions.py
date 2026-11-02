"""Add immutable agent/persona configuration and assignment provenance.

The migration is intentionally idempotent against both a database created by
0001's declarative metadata and an installation upgraded from the original
agent tables. Existing profile/revision/run UUIDs are retained.
"""

# ruff: noqa: E501

import uuid
from typing import Any

from alembic import op
from sqlalchemy import Column, DateTime, Integer, String, Text, inspect, text
from sqlalchemy.dialects.postgresql import UUID

revision = "0004_agent_persona_revisions"
down_revision = "0003_idempotency_issuer"
branch_labels = None
depends_on = None

_NAMESPACE = uuid.UUID("a8a6b450-20fb-4c6a-b0af-e7cb0f9c7b8a")
_PROFILE = uuid.uuid5(_NAMESPACE, "general-assistant")
_REVISION = uuid.uuid5(_NAMESPACE, "general-assistant-revision-1")
_POLICY = uuid.uuid5(_NAMESPACE, "ollama-model-policy-1")
_PERSONA = uuid.uuid5(_NAMESPACE, "neutral-persona")
_PERSONA_REVISION = uuid.uuid5(_NAMESPACE, "neutral-persona-revision-1")
_PLATFORM = uuid.uuid5(_NAMESPACE, "prompt-platform-1")
_GOVERNANCE = uuid.uuid5(_NAMESPACE, "prompt-governance-1")
_BUNDLE = uuid.uuid5(_NAMESPACE, "prompt-bundle-1")


def _columns(bind: object, table: str) -> set[str]:
    return {item["name"] for item in inspect(bind).get_columns(table)}  # type: ignore[arg-type]


def _add_column(bind: object, table: str, column: Any) -> None:
    if column.name not in _columns(bind, table):
        op.add_column(table, column)


def upgrade() -> None:
    bind = op.get_bind()
    _add_column(
        bind,
        "agent_profiles",
        Column("status", String(16), nullable=False, server_default="active"),
    )
    _add_column(
        bind, "agent_profiles", Column("version", Integer(), nullable=False, server_default="1")
    )
    _add_column(
        bind, "agent_profiles", Column("current_revision_id", UUID(as_uuid=True), nullable=True)
    )
    _add_column(
        bind, "agent_profiles", Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP"))
    )
    _add_column(
        bind, "agent_profiles", Column("updated_at", DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP"))
    )
    _add_column(
        bind, "agent_revisions", Column("purpose", Text(), nullable=False, server_default="")
    )
    _add_column(
        bind, "agent_revisions", Column("instructions", Text(), nullable=False, server_default="")
    )
    _add_column(
        bind,
        "agent_revisions",
        Column("display_name", String(255), nullable=False, server_default=""),
    )
    _add_column(
        bind, "agent_revisions", Column("persona_revision_id", UUID(as_uuid=True), nullable=True)
    )
    _add_column(
        bind,
        "agent_revisions",
        Column("prompt_bundle_revision_id", UUID(as_uuid=True), nullable=True),
    )
    _add_column(bind, "runs", Column("persona_revision_id", UUID(as_uuid=True), nullable=True))
    _add_column(
        bind, "runs", Column("prompt_bundle_revision_id", UUID(as_uuid=True), nullable=True)
    )
    _add_column(bind, "runs", Column("prompt_hash", String(64), nullable=True))

    tables = set(inspect(bind).get_table_names())  # type: ignore[arg-type]
    if "persona_profiles" not in tables:
        op.create_table(
            "persona_profiles",
            Column("id", UUID(as_uuid=True), primary_key=True),
            Column("slug", String(128), nullable=False, unique=True),
            Column("display_name", String(255), nullable=False),
            Column("status", String(16), nullable=False, server_default="active"),
            Column("version", Integer(), nullable=False, server_default="1"),
            Column("current_revision_id", UUID(as_uuid=True)),
            Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")),
            Column("updated_at", DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")),
        )
    tables = set(inspect(bind).get_table_names())  # type: ignore[arg-type]
    if "persona_revisions" not in tables:
        op.create_table(
            "persona_revisions",
            Column("id", UUID(as_uuid=True), primary_key=True),
            Column("persona_profile_id", UUID(as_uuid=True), nullable=False),
            Column("revision", Integer(), nullable=False),
            Column("display_name", String(255), nullable=False, server_default=""),
            Column("description", Text(), nullable=False),
            Column("instructions", Text(), nullable=False),
            Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")),
        )
    _add_column(
        bind,
        "persona_revisions",
        Column("display_name", String(255), nullable=False, server_default=""),
    )
    tables = set(inspect(bind).get_table_names())  # type: ignore[arg-type]
    if "prompt_component_revisions" not in tables:
        op.create_table(
            "prompt_component_revisions",
            Column("id", UUID(as_uuid=True), primary_key=True),
            Column("component", String(64), nullable=False),
            Column("revision", Integer(), nullable=False),
            Column("content", Text(), nullable=False),
            Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")),
        )
    tables = set(inspect(bind).get_table_names())  # type: ignore[arg-type]
    if "prompt_bundle_revisions" not in tables:
        op.create_table(
            "prompt_bundle_revisions",
            Column("id", UUID(as_uuid=True), primary_key=True),
            Column("revision", Integer(), nullable=False),
            Column("platform_component_revision_id", UUID(as_uuid=True), nullable=False),
            Column("governance_component_revision_id", UUID(as_uuid=True), nullable=False),
            Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")),
        )
    tables = set(inspect(bind).get_table_names())  # type: ignore[arg-type]
    if "conversation_agent_assignments" not in tables:
        op.create_table(
            "conversation_agent_assignments",
            Column("id", UUID(as_uuid=True), primary_key=True),
            Column("conversation_id", UUID(as_uuid=True), nullable=False),
            Column("agent_profile_id", UUID(as_uuid=True), nullable=False),
            Column("agent_revision_id", UUID(as_uuid=True), nullable=False),
            Column("reason", String(32), nullable=False),
            Column("effective_after_message_id", UUID(as_uuid=True)),
            Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")),
        )

    # Keep upgraded schemas equivalent to the declarative metadata.  These
    # indexes are idempotent and protect revision sequencing across workers.
    op.execute(
        text("CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_revision_number "
             "ON agent_revisions (agent_profile_id, revision)")
    )
    op.execute(
        text("CREATE UNIQUE INDEX IF NOT EXISTS uq_persona_revision_number "
             "ON persona_revisions (persona_profile_id, revision)")
    )
    op.execute(
        text("CREATE UNIQUE INDEX IF NOT EXISTS uq_prompt_component_revision "
             "ON prompt_component_revisions (component, revision)")
    )

    # Seed immutable platform/governance components and Neutral persona. The
    # INSERT guards also make a rerun safe after a partially applied deployment.
    op.execute(
        text("""
        INSERT INTO prompt_component_revisions (id, component, revision, content)
        VALUES (:platform, 'platform', 1, ''), (:governance, 'governance', 1, '')
        ON CONFLICT (id) DO NOTHING
        """).bindparams(platform=_PLATFORM, governance=_GOVERNANCE)
    )
    op.execute(
        text("""
        INSERT INTO prompt_bundle_revisions
          (id, revision, platform_component_revision_id, governance_component_revision_id)
        VALUES (:bundle, 1, :platform, :governance)
        ON CONFLICT (id) DO NOTHING
        """).bindparams(bundle=_BUNDLE, platform=_PLATFORM, governance=_GOVERNANCE)
    )
    op.execute(
        text("""
        INSERT INTO persona_profiles
          (id, slug, display_name, status, version, current_revision_id)
        VALUES (:profile, 'neutral', 'Neutral', 'active', 1, :revision)
        ON CONFLICT (id) DO NOTHING
        """).bindparams(profile=_PERSONA, revision=_PERSONA_REVISION)
    )
    op.execute(
        text("""
        INSERT INTO persona_revisions
          (id, persona_profile_id, revision, display_name, description, instructions)
        VALUES (:revision, :profile, 1, 'Neutral', :description, :instructions)
        ON CONFLICT (id) DO NOTHING
        """).bindparams(
            revision=_PERSONA_REVISION,
            profile=_PERSONA,
            description="A neutral, clear conversational style.",
            instructions="Use a clear, calm and direct conversational style.",
        )
    )
    op.execute(
        text("""
        UPDATE agent_revisions
        SET purpose = CASE WHEN purpose = '' THEN system_prompt ELSE purpose END,
            instructions = CASE WHEN instructions = '' THEN system_prompt ELSE instructions END,
            display_name = CASE WHEN display_name = '' THEN 'Aura' ELSE display_name END,
            persona_revision_id = COALESCE(persona_revision_id, :persona),
            prompt_bundle_revision_id = COALESCE(prompt_bundle_revision_id, :bundle)
        """).bindparams(persona=_PERSONA_REVISION, bundle=_BUNDLE)
    )
    op.execute(
        text("""
        UPDATE agent_profiles
        SET current_revision_id = COALESCE(current_revision_id, :revision),
            status = COALESCE(status, 'active'), version = COALESCE(version, 1)
        WHERE id = :profile
        """).bindparams(profile=_PROFILE, revision=_REVISION)
    )
    op.execute(
        text("""
        UPDATE agent_profiles p
        SET current_revision_id = latest.id,
            updated_at = COALESCE(p.updated_at, CURRENT_TIMESTAMP),
            created_at = COALESCE(p.created_at, CURRENT_TIMESTAMP)
        FROM (
          SELECT DISTINCT ON (agent_profile_id) id, agent_profile_id
          FROM agent_revisions
          ORDER BY agent_profile_id, revision DESC, id
        ) latest
        WHERE latest.agent_profile_id = p.id
          AND p.current_revision_id IS NULL
        """)
    )
    op.execute(
        text("""
        UPDATE persona_profiles
        SET created_at = COALESCE(created_at, CURRENT_TIMESTAMP),
            updated_at = COALESCE(updated_at, CURRENT_TIMESTAMP)
        """)
    )
    op.execute(
        text("""
        UPDATE persona_revisions
        SET display_name = CASE
          WHEN persona_revisions.display_name = '' THEN p.display_name
          ELSE persona_revisions.display_name
        END
        FROM persona_profiles p
        WHERE p.id = persona_revisions.persona_profile_id
        """)
    )
    op.execute(
        text("""
        UPDATE persona_profiles p
        SET current_revision_id = latest.id
        FROM (
          SELECT DISTINCT ON (persona_profile_id) id, persona_profile_id
          FROM persona_revisions
          ORDER BY persona_profile_id, revision DESC, id
        ) latest
        WHERE latest.persona_profile_id = p.id
          AND p.current_revision_id IS NULL
        """)
    )
    # New installations get the deterministic Aura seed; existing installations
    # keep their 0001 UUIDs and only gain missing provenance.
    op.execute(
        text("""
        INSERT INTO agent_profiles
          (id, slug, display_name, status, version, current_revision_id)
        VALUES (:profile, 'general-assistant', 'Aura', 'active', 1, :revision)
        ON CONFLICT (id) DO NOTHING
        """).bindparams(profile=_PROFILE, revision=_REVISION)
    )
    op.execute(
        text("""
        INSERT INTO model_policy_revisions (id, revision, provider, policy)
        VALUES (:policy, 1, 'ollama', CAST(:policy_json AS JSONB))
        ON CONFLICT (id) DO NOTHING
        """).bindparams(policy=_POLICY, policy_json='{"fallback": false}')
    )
    op.execute(
        text("""
        INSERT INTO agent_revisions
          (id, agent_profile_id, revision, display_name, system_prompt, purpose, instructions,
           persona_revision_id, prompt_bundle_revision_id, model_policy_revision_id)
        VALUES (:revision, :profile, 1, 'Aura', :prompt, :prompt, :prompt,
                :persona, :bundle, :policy)
        ON CONFLICT (id) DO NOTHING
        """).bindparams(
            revision=_REVISION,
            profile=_PROFILE,
            prompt="You are Aura, a helpful local-first household assistant.",
            persona=_PERSONA_REVISION,
            bundle=_BUNDLE,
            policy=_POLICY,
        )
    )
    op.execute(
        text("""
        INSERT INTO conversation_agent_assignments
          (id, conversation_id, agent_profile_id, agent_revision_id, reason, created_at)
        SELECT md5(c.id::text || chr(58) || 'initial')::uuid,
               c.id, c.agent_profile_id, c.agent_revision_id, 'initial',
               COALESCE(c.created_at, CURRENT_TIMESTAMP)
        FROM conversations c
        WHERE NOT EXISTS (
          SELECT 1 FROM conversation_agent_assignments a WHERE a.conversation_id = c.id
        )
        """)
    )


def downgrade() -> None:
    raise RuntimeError("Agent/persona revision downgrade is disabled; use a forward migration")
