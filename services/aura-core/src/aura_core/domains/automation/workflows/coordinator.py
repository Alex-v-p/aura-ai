"""Compatibility name for the workflow application execution coordinator."""

from .application import WorkflowApplicationService

WorkflowCoordinator = WorkflowApplicationService

__all__ = ["WorkflowCoordinator", "WorkflowApplicationService"]
