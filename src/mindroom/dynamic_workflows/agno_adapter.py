"""Adapter from MindRoom Dynamic Workflow specs to Agno factories."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from agno.db.sqlite import SqliteDb
from agno.workflow import Step, Workflow, WorkflowFactory
from agno.workflow.types import StepInput, StepOutput

from mindroom.dynamic_workflows.runner import (
    DynamicWorkflowExecutionError,
    execute_workflow_step,
)
from mindroom.dynamic_workflows.store import validate_workflow_spec

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from agno.factory import RequestContext

    from mindroom.dynamic_workflows.runner import ParticipantExecutor


def build_agno_workflow_factory(
    spec: dict[str, object],
    *,
    db_file: Path,
    participant_executor: ParticipantExecutor | None = None,
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
            steps=cast("Any", _build_steps(validated, participant_executor=participant_executor)),
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


def _build_steps(
    spec: dict[str, object],
    *,
    participant_executor: ParticipantExecutor | None,
) -> list[Step]:
    raw_steps = spec["workflow"]
    if not isinstance(raw_steps, list):
        msg = "Workflow spec field 'workflow' must be a list."
        raise TypeError(msg)
    steps: list[Step] = []
    participants_by_id = _participants_by_id(spec)
    for index, raw_step in enumerate(raw_steps):
        if not isinstance(raw_step, dict):
            msg = f"Workflow step at index {index} must be a mapping."
            raise TypeError(msg)
        steps.append(
            _build_step(
                _object_mapping(cast("Mapping[object, object]", raw_step)),
                participant_executor=participant_executor,
                participants_by_id=participants_by_id,
            ),
        )
    return steps


def _build_step(
    step: dict[str, object],
    *,
    participant_executor: ParticipantExecutor | None,
    participants_by_id: Mapping[str, dict[str, object]],
) -> Step:
    step_id = str(step["id"])
    description = str(step.get("prompt") or step.get("description") or "")

    def executor(step_input: StepInput) -> StepOutput:
        input_data = step_input.input if isinstance(step_input.input, dict) else {}
        previous_outputs = _previous_step_outputs(step_input.previous_step_outputs)
        try:
            result = execute_workflow_step(
                step,
                input_data=input_data,
                step_outputs=previous_outputs,
                participant_executor=participant_executor,
                participants_by_id=participants_by_id,
            )
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


def _participants_by_id(spec: dict[str, object]) -> dict[str, dict[str, object]]:
    raw_participants = spec["participants"]
    if not isinstance(raw_participants, list):
        msg = "Workflow spec field 'participants' must be a list."
        raise TypeError(msg)
    participants: dict[str, dict[str, object]] = {}
    for raw_participant in raw_participants:
        if not isinstance(raw_participant, dict):
            msg = "Workflow participants must be mappings."
            raise TypeError(msg)
        participant = _object_mapping(cast("Mapping[object, object]", raw_participant))
        participants[str(participant["id"])] = participant
    return participants


def _previous_step_outputs(previous_step_outputs: dict[str, StepOutput] | None) -> dict[str, object]:
    if previous_step_outputs is None:
        return {}
    return {step_id: output.content for step_id, output in previous_step_outputs.items()}
