"""Composition-time registration of Core-owned SQLAlchemy mappings."""

from sqlalchemy import MetaData

from aura_core.domains.automation.notifications import persistence as notification_persistence
from aura_core.domains.automation.reminders import persistence as reminder_persistence
from aura_core.domains.automation.reminders import proposals as reminder_proposals
from aura_core.domains.execution.runs import persistence as run_persistence
from aura_core.domains.governance.approvals import persistence as approval_persistence
from aura_core.domains.governance.audit import persistence as audit_persistence
from aura_core.domains.governance.identity import persistence as identity_persistence
from aura_core.domains.interaction.agents import persistence as agent_persistence
from aura_core.domains.interaction.conversations import persistence as conversation_persistence
from aura_core.domains.interaction.personas import persistence as persona_persistence
from aura_core.platform.database.base import Base
from aura_core.platform.outbox import persistence as outbox_persistence

_MAPPING_MODULES = (
    run_persistence,
    reminder_persistence,
    reminder_proposals,
    notification_persistence,
    audit_persistence,
    approval_persistence,
    identity_persistence,
    agent_persistence,
    persona_persistence,
    conversation_persistence,
    outbox_persistence,
)


def metadata() -> MetaData:
    """Return metadata after importing every domain-owned mapping module."""

    _ = _MAPPING_MODULES
    return Base.metadata
