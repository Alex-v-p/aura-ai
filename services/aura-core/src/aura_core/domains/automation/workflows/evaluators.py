"""Public deterministic evaluator registry for workflow components."""

from collections.abc import Mapping
from typing import cast

from .evaluation import WORKFLOW_EVALUATORS, WorkflowEvaluationResult, evaluate_workflow_case

# Defaults are deliberately closed and component-specific.  An omitted field
# means "run the declared fixture", while a supplied field is evaluated as-is
# (and therefore cannot be smuggled through by selecting a canonical case).
_CASE_DEFAULTS: dict[str, dict[str, tuple[str, str]]] = {
    "baseline": {
        "workflow.definition.v1": ("published", "completed"),
        "workflow.agent-run.v1": ("queued", "not_attempted"),
        "workflow.authorization.v1": ("waiting_for_approval", "not_attempted"),
        "workflow.notification-step.v1": ("running", "not_attempted"),
        "workflow.wait-step.v1": ("waiting_for_time", "not_attempted"),
        "workflow.checkpoint.v1": ("running", "completed"),
        "workflow.cancellation.v1": ("cancel_requested", "unknown"),
        "workflow.state-machine.v1": ("queued", "not_attempted"),
    },
    "negative": {
        evaluator: ("invalid", "invalid") for evaluator in WORKFLOW_EVALUATORS
    },
    "race": {
        "workflow.definition.v1": ("published", "conflict"),
        "workflow.agent-run.v1": ("resolution_required", "unknown"),
        "workflow.authorization.v1": ("waiting_for_approval", "unknown"),
        "workflow.notification-step.v1": ("failed_replayable", "definitively_failed"),
        "workflow.wait-step.v1": ("waiting_for_time", "not_attempted"),
        "workflow.checkpoint.v1": ("running", "completed"),
        "workflow.cancellation.v1": ("cancel_requested", "unknown"),
        "workflow.state-machine.v1": ("waiting_for_resolution", "unknown"),
    },
    "replay": {
        "workflow.definition.v1": ("published", "completed"),
        "workflow.agent-run.v1": ("completed", "completed"),
        "workflow.authorization.v1": ("waiting_for_approval", "not_attempted"),
        "workflow.notification-step.v1": ("completed", "completed"),
        "workflow.wait-step.v1": ("completed", "completed"),
        "workflow.checkpoint.v1": ("completed", "completed"),
        "workflow.cancellation.v1": ("terminal", "completed"),
        "workflow.state-machine.v1": ("completed", "completed"),
    },
}


def evaluate(evaluator_id: str, case: object) -> dict[str, object]:
    """Execute a bounded synthetic evaluation without reading workflow data."""
    supplied: Mapping[str, object] = (
        cast(Mapping[str, object], case) if isinstance(case, Mapping) else {}
    )
    requested_case = str(supplied.get("caseId", "baseline"))
    if requested_case not in _CASE_DEFAULTS:
        raise ValueError("workflow evaluator case is not registered")
    try:
        default_status, default_outcome = _CASE_DEFAULTS[requested_case][evaluator_id]
    except KeyError as exc:
        # Preserve the evaluator registry's closed failure for unknown IDs.
        if evaluator_id not in WORKFLOW_EVALUATORS:
            raise ValueError("workflow evaluator is not registered") from exc
        raise
    status = str(supplied.get("status", default_status))
    outcome = str(supplied.get("outcome", default_outcome))
    result = evaluate_workflow_case(
        evaluator_id, status=status, outcome=outcome, case_id=requested_case
    )
    return {
        "evaluatorId": result.evaluator,
        "componentId": result.component_id,
        "componentVersion": result.component_version,
        "scenarioId": result.scenario_id,
        "caseId": result.case_id,
        "replayKey": result.replay_key,
        # These are the values actually evaluated above.  In particular, a
        # caller cannot obtain a passing race/replay result by selecting the
        # predeclared canonical case while supplying different state.
        "passed": result.passed,
        # Keep the public evaluator's closed outcome vocabulary while deriving
        # it from the actual evaluated result (never from the fixture row).
        "outcome": "pass" if result.passed else "fail",
        "cases": list(result.cases),
    }

__all__ = ["WORKFLOW_EVALUATORS", "WorkflowEvaluationResult", "evaluate_workflow_case", "evaluate"]
