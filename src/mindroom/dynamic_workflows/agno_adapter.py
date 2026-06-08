"""Adapter from MindRoom Dynamic Workflow specs to Agno factories."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from agno.db.sqlite import SqliteDb
from agno.workflow import Step, Workflow, WorkflowFactory
from agno.workflow.types import StepInput, StepOutput

from mindroom.dynamic_workflows.runner import DynamicWorkflowExecutionError, execute_workflow_step
from mindroom.dynamic_workflows.store import validate_workflow_spec

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from agno.factory import RequestContext


def build_agno_workflow_factory(
    spec: dict[str, object],
    *,
    db_file: Path,
) -> WorkflowFactory:
    """Compile one declarative Dynamic Workflow spec into an Agno WorkflowFactory."""
    validated = validate_workflow_spec(spec)
    workflow_id = str(validated["id"])
    workflow_name = str(validated["name"])
    workflow_description = str(validated.get("description", ""))
    db_file.parent.mkdir(parents=True, exist_ok=True)
    db = SqliteDb(db_file=str(db_file), id=f"dynamic-workflow-{workflow_id}")

    def build_workflow(ctx: RequestContext) -> Workflow:
        del ctx
        return Workflow(
            id=workflow_id,
            name=workflow_name,
            description=workflow_description,
            db=db,
            steps=cast("Any", _build_steps(validated)),
            metadata={
                "mindroom_dynamic_workflow": True,
                "workflow_id": workflow_id,
            },
        )

    return WorkflowFactory(
        id=workflow_id,
        name=workflow_name,
        description=workflow_description,
        db=db,
        factory=build_workflow,
    )


def _build_steps(spec: dict[str, object]) -> list[Step]:
    raw_steps = spec["workflow"]
    if not isinstance(raw_steps, list):
        msg = "Workflow spec field 'workflow' must be a list."
        raise TypeError(msg)
    steps: list[Step] = []
    for index, raw_step in enumerate(raw_steps):
        if not isinstance(raw_step, dict):
            msg = f"Workflow step at index {index} must be a mapping."
            raise TypeError(msg)
        steps.append(_build_step(_object_mapping(cast("Mapping[object, object]", raw_step))))
    return steps


def _build_step(step: dict[str, object]) -> Step:
    step_id = str(step["id"])
    description = str(step.get("prompt") or step.get("description") or "")

    def executor(step_input: StepInput) -> StepOutput:
        input_data = step_input.input if isinstance(step_input.input, dict) else {}
        previous_outputs = _previous_step_outputs(step_input.previous_step_outputs)
        try:
            result = execute_workflow_step(step, input_data=input_data, step_outputs=previous_outputs)
        except DynamicWorkflowExecutionError as exc:
            return StepOutput(
                step_name=step_id,
                step_id=step_id,
                content="",
                success=False,
                error=str(exc),
            )
        return StepOutput(
            step_name=step_id,
            step_id=step_id,
            content=result.content,
            success=result.status == "completed",
            error=result.error,
        )

    return Step(
        name=step_id,
        step_id=step_id,
        description=description,
        executor=executor,
    )


def _object_mapping(data: Mapping[object, object]) -> dict[str, object]:
    return {str(key): value for key, value in data.items()}


def _previous_step_outputs(previous_step_outputs: dict[str, StepOutput] | None) -> dict[str, object]:
    if previous_step_outputs is None:
        return {}
    return {step_id: output.content for step_id, output in previous_step_outputs.items()}
