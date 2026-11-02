"""Deterministic workflow evaluators registered by the Core component plan."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from .public import (
    WorkflowEffectOutcome,
    WorkflowInstanceStatus,
)

# Evaluators are intentionally metadata-only.  They report closed labels and
# never inspect workflow content or provider output.
WORKFLOW_EVALUATORS = (
    "workflow.definition.v1", "workflow.agent-run.v1", "workflow.authorization.v1",
    "workflow.notification-step.v1", "workflow.wait-step.v1", "workflow.checkpoint.v1",
    "workflow.cancellation.v1", "workflow.state-machine.v1",
)

EVALUATOR_COMPONENTS = {
    "workflow.definition.v1": "aura.automation.workflow_definition",
    "workflow.agent-run.v1": "aura.automation.workflow_step.agent_run",
    "workflow.authorization.v1": "aura.automation.workflow_approval",
    "workflow.notification-step.v1": "aura.automation.workflow_step.notification",
    "workflow.wait-step.v1": "aura.automation.workflow_step.wait_until",
    "workflow.checkpoint.v1": "aura.automation.workflow_checkpoint",
    "workflow.cancellation.v1": "aura.automation.workflow_cancellation",
    "workflow.state-machine.v1": "aura.automation.workflow_state",
}


@dataclass(frozen=True, slots=True)
class WorkflowEvaluationResult:
    evaluator: str
    passed: bool
    outcome: str
    component_id: str = ""
    component_version: str = "1.0.0"
    scenario_id: str = "workflow.synthetic.v1"
    case_id: str = "baseline"
    replay_key: str = ""
    cases: tuple[dict[str, str | bool], ...] = ()


def _assert_component_case(evaluator: str, status: str, outcome: str, case_id: str) -> bool:
    """Bounded component assertions used by both production and replay tests."""
    if case_id == "negative":
        return status in {"invalid", "rejected", "failed"} or outcome in {"invalid", "unknown"}
    if case_id == "race":
        expected = {
            "workflow.definition.v1": ({"draft", "published"}, {"conflict", "unknown"}),
            "workflow.agent-run.v1": ({"resolution_required"}, {"unknown"}),
            "workflow.authorization.v1": (
                {"waiting_for_approval", "resolution_required"}, {"unknown"}
            ),
            "workflow.notification-step.v1": ({"failed_replayable"}, {"definitively_failed"}),
            "workflow.wait-step.v1": ({"waiting_for_time"}, {"not_attempted"}),
            "workflow.checkpoint.v1": ({"running"}, {"not_attempted", "completed"}),
            "workflow.cancellation.v1": ({"cancel_requested"}, {"unknown", "definitively_failed"}),
            "workflow.state-machine.v1": ({"waiting_for_resolution"}, {"unknown"}),
        }[evaluator]
        return status in expected[0] and outcome in expected[1]
    if case_id == "replay":
        expected = {
            "workflow.definition.v1": ({"draft", "published"}, {"completed"}),
            "workflow.agent-run.v1": ({"completed"}, {"completed"}),
            "workflow.authorization.v1": ({"waiting_for_approval"}, {"not_attempted"}),
            "workflow.notification-step.v1": ({"completed"}, {"completed"}),
            "workflow.wait-step.v1": ({"completed"}, {"completed"}),
            "workflow.checkpoint.v1": ({"completed"}, {"completed"}),
            "workflow.cancellation.v1": ({"terminal"}, {"completed"}),
            "workflow.state-machine.v1": ({"completed"}, {"completed"}),
        }[evaluator]
        return status in expected[0] and outcome in expected[1]
    if evaluator == "workflow.definition.v1":
        return status in {"draft", "published", "retired", "valid"} and outcome != "invalid"
    if evaluator == "workflow.agent-run.v1":
        return status in {"queued", "running", "completed", "resolution_required"} and outcome in {
            "not_attempted", "completed", "unknown", "definitively_failed"
        }
    if evaluator == "workflow.authorization.v1":
        return outcome in {"not_attempted", "completed"} and status != "resolution_required"
    if evaluator == "workflow.notification-step.v1":
        return status in {"running", "completed", "failed_replayable"} and outcome in {
            "not_attempted", "completed", "definitively_failed"
        }
    if evaluator == "workflow.wait-step.v1":
        return status in {"waiting_for_time", "completed"} and outcome in {
            "not_attempted", "completed"
        }
    if evaluator == "workflow.checkpoint.v1":
        return status in {"queued", "running", "completed"} and outcome != "unknown"
    if evaluator == "workflow.cancellation.v1":
        return status in {"cancel_requested", "terminal", "canceled"} or (
            outcome == "definitively_failed"
        )
    return status in {item.value for item in WorkflowInstanceStatus} and outcome in {
        item.value for item in WorkflowEffectOutcome
    }


def evaluate_workflow_case(
    evaluator: str, *, status: str, outcome: str, case_id: str = "baseline"
) -> WorkflowEvaluationResult:
    if evaluator not in WORKFLOW_EVALUATORS:
        raise ValueError("workflow evaluator is not registered")
    if case_id not in {"baseline", "negative", "race", "replay"}:
        raise ValueError("workflow evaluator case is not registered")
    passed = _assert_component_case(evaluator, status, outcome, case_id)
    component_id = EVALUATOR_COMPONENTS[evaluator]
    canonical = json.dumps(
        {"evaluator": evaluator, "scenario": "workflow.synthetic.v1", "case": case_id},
        sort_keys=True, separators=(",", ":"),
    ).encode()
    replay_key = hashlib.sha256(canonical).hexdigest()
    scenario_inputs = {
        "baseline": (status, outcome),
        "negative": ("invalid", "invalid"),
        "race": {
            "workflow.definition.v1": ("published", "conflict"),
            "workflow.agent-run.v1": ("resolution_required", "unknown"),
            "workflow.authorization.v1": ("waiting_for_approval", "unknown"),
            "workflow.notification-step.v1": ("failed_replayable", "definitively_failed"),
            "workflow.wait-step.v1": ("waiting_for_time", "not_attempted"),
            "workflow.checkpoint.v1": ("running", "completed"),
            "workflow.cancellation.v1": ("cancel_requested", "unknown"),
            "workflow.state-machine.v1": ("waiting_for_resolution", "unknown"),
        }[evaluator],
        "replay": {
            "workflow.definition.v1": ("published", "completed"),
            "workflow.agent-run.v1": ("completed", "completed"),
            "workflow.authorization.v1": ("waiting_for_approval", "not_attempted"),
            "workflow.notification-step.v1": ("completed", "completed"),
            "workflow.wait-step.v1": ("completed", "completed"),
            "workflow.checkpoint.v1": ("completed", "completed"),
            "workflow.cancellation.v1": ("terminal", "completed"),
            "workflow.state-machine.v1": ("completed", "completed"),
        }[evaluator],
    }
    cases = tuple(
        {
            "caseId": case_id,
            "outcome": "pass" if case_ok else "fail",
            "replayKey": hashlib.sha256(
                f"{evaluator}:workflow.synthetic.v1:{case_id}:v1".encode()
            ).hexdigest(),
            "deterministic": True,
        }
        for case_id, (case_status, case_outcome) in scenario_inputs.items()
        for case_ok in (_assert_component_case(evaluator, case_status, case_outcome, case_id),)
    )
    return WorkflowEvaluationResult(
        evaluator, passed, "passed" if passed else "rejected", component_id,
        "1.0.0", "workflow.synthetic.v1", case_id, replay_key, cases,
    )


__all__ = ["WORKFLOW_EVALUATORS", "WorkflowEvaluationResult", "evaluate_workflow_case"]
