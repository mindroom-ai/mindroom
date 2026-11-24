"""Executable Dynamic Workflow step runner."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol, cast

from mindroom.dynamic_workflows.spec import TEMPLATE_REF_RE, AgentStep, ReportStep, TransformStep
from mindroom.failure_text import failure_text

if TYPE_CHECKING:
    import re

    from mindroom.dynamic_workflows.spec import Participant, Step, WorkflowSpec


class DynamicWorkflowExecutionError(ValueError):
    """Raised when a Dynamic Workflow step cannot execute.

    ``delegation_id`` names the audit record of a participant turn that started before the step failed.
    """

    def __init__(self, message: str, *, delegation_id: str | None = None) -> None:
        super().__init__(message)
        self.delegation_id = delegation_id


@dataclass(frozen=True)
class ParticipantOutput:
    """One participant answer and the delegation record of the turn that produced it, if any."""

    content: object
    delegation_id: str | None = None


class ParticipantExecutor(Protocol):
    """Async callable that executes one resolved Dynamic Workflow participant."""

    def __call__(self, *, participant: Participant, prompt: str) -> Awaitable[ParticipantOutput]:
        """Run one participant on its rendered prompt."""


@dataclass(frozen=True)
class _DynamicWorkflowStepResult:
    """Execution result for one Dynamic Workflow step."""

    step_id: str
    step_type: str
    status: str
    content: object
    started_at: str
    completed_at: str
    error: str | None = None
    delegation_id: str | None = None

    def to_json(self) -> dict[str, object]:
        """Return a JSON-serializable representation."""
        return {
            "id": self.step_id,
            "type": self.step_type,
            "status": self.status,
            "content": self.content,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "error": self.error,
            "delegation_id": self.delegation_id,
        }


@dataclass(frozen=True)
class WorkflowExecution:
    """Execution summary for one Dynamic Workflow run."""

    status: str
    steps: list[_DynamicWorkflowStepResult]
    outputs: dict[str, object]
    report_markdown: str
    error: str | None = None

    def step_outputs_json(self) -> dict[str, object]:
        """Return step outputs keyed by step ID."""
        return {step.step_id: step.to_json() for step in self.steps}


async def execute_workflow_spec(
    spec: WorkflowSpec,
    input_data: dict[str, object],
    *,
    participant_executor: ParticipantExecutor | None = None,
) -> WorkflowExecution:
    """Execute a declarative Dynamic Workflow spec sequentially on the current event loop."""
    steps: list[_DynamicWorkflowStepResult] = []
    step_outputs: dict[str, object] = {}
    participants = {participant.id: participant for participant in spec.participants}

    for step in spec.workflow:
        try:
            result = await _execute_workflow_step(
                step,
                input_data=input_data,
                step_outputs=step_outputs,
                participant_executor=participant_executor,
                participants=participants,
            )
        except DynamicWorkflowExecutionError as exc:
            failed_step = _failed_step(step, str(exc), delegation_id=exc.delegation_id)
            steps.append(failed_step)
            return WorkflowExecution(
                status="failed",
                steps=steps,
                outputs={},
                report_markdown=_failed_report_markdown(spec, input_data, steps, str(exc)),
                error=str(exc),
            )
        steps.append(result)
        step_outputs[result.step_id] = result.content

    outputs = {output.id: _resolve_step_reference(output.from_step, step_outputs) for output in spec.outputs}
    return WorkflowExecution(
        status="completed",
        steps=steps,
        outputs=outputs,
        report_markdown=_report_markdown(spec, input_data, steps, outputs),
    )


async def _execute_workflow_step(
    step: Step,
    *,
    input_data: dict[str, object],
    step_outputs: Mapping[str, object],
    participant_executor: ParticipantExecutor | None,
    participants: Mapping[str, Participant],
) -> _DynamicWorkflowStepResult:
    """Execute one declarative workflow step on the current event loop."""
    started_at = _utc_now()
    match step:
        case AgentStep():
            output = await _execute_agent_step(
                step,
                input_data=input_data,
                step_outputs=step_outputs,
                participant_executor=participant_executor,
                participants=participants,
            )
        case TransformStep():
            output = ParticipantOutput(_render_template(step.body, input_data=input_data, step_outputs=step_outputs))
        case ReportStep():
            output = ParticipantOutput(_report_content(step, input_data=input_data, step_outputs=step_outputs))
    return _DynamicWorkflowStepResult(
        step_id=step.id,
        step_type=step.type,
        status="completed",
        content=output.content,
        started_at=started_at,
        completed_at=_utc_now(),
        delegation_id=output.delegation_id,
    )


def _report_content(step: ReportStep, *, input_data: dict[str, object], step_outputs: Mapping[str, object]) -> str:
    if step.body_template is not None:
        body = _render_template(step.body_template, input_data=input_data, step_outputs=step_outputs)
    else:
        # A report step names exactly one source, so a step without a body template copies a prior step.
        body = _stringify_template_value(_resolve_step_reference(cast("str", step.from_step), step_outputs))
    if step.title is None:
        return body
    return f"# {_render_template(step.title, input_data=input_data, step_outputs=step_outputs)}\n\n{body}"


async def _execute_agent_step(
    step: AgentStep,
    *,
    input_data: dict[str, object],
    step_outputs: Mapping[str, object],
    participant_executor: ParticipantExecutor | None,
    participants: Mapping[str, Participant],
) -> ParticipantOutput:
    if participant_executor is None:
        msg = f"Agent step '{step.id}' requires a participant executor."
        raise DynamicWorkflowExecutionError(msg)
    prompt = _render_template(step.body, input_data=input_data, step_outputs=step_outputs)
    try:
        return await participant_executor(participant=participants[step.participant], prompt=prompt)
    except DynamicWorkflowExecutionError:
        raise
    except Exception as exc:
        msg = f"Agent step '{step.id}' participant '{step.participant}' failed: {failure_text(exc)}"
        raise DynamicWorkflowExecutionError(msg) from exc


def _report_markdown(
    spec: WorkflowSpec,
    input_data: dict[str, object],
    steps: list[_DynamicWorkflowStepResult],
    outputs: dict[str, object],
) -> str:
    html_report_output = next((output.id for output in spec.outputs if output.type == "html_report"), None)
    if html_report_output is not None and html_report_output in outputs:
        return _with_input_section(_stringify_template_value(outputs[html_report_output]), input_data)

    for step in reversed(steps):
        if step.step_type == "report_step":
            return _with_input_section(_stringify_template_value(step.content), input_data)

    input_json = json.dumps(input_data, indent=2, sort_keys=True)
    step_sections = "\n\n".join(f"## {step.step_id}\n\n{_stringify_template_value(step.content)}" for step in steps)
    return f"# {spec.name}\n\nDynamic Workflow run completed.\n\n## Input\n\n```json\n{input_json}\n```\n\n{step_sections}\n"


def _with_input_section(report_markdown: str, input_data: dict[str, object]) -> str:
    input_json = json.dumps(input_data, indent=2, sort_keys=True)
    return f"{report_markdown.rstrip()}\n\n## Input\n\n```json\n{input_json}\n```\n"


def _failed_report_markdown(
    spec: WorkflowSpec,
    input_data: dict[str, object],
    steps: list[_DynamicWorkflowStepResult],
    error: str,
) -> str:
    input_json = json.dumps(input_data, indent=2, sort_keys=True)
    step_sections = "\n\n".join(f"## {step.step_id}\n\nStatus: {step.status}\n\nError: {step.error}" for step in steps)
    return (
        f"# {spec.name}\n\n"
        "Dynamic Workflow run failed.\n\n"
        f"## Error\n\n{error}\n\n"
        f"## Input\n\n```json\n{input_json}\n```\n\n"
        f"{step_sections}\n"
    )


def _failed_step(
    step: Step,
    error: str,
    *,
    delegation_id: str | None = None,
) -> _DynamicWorkflowStepResult:
    now = _utc_now()
    return _DynamicWorkflowStepResult(
        step_id=step.id,
        step_type=step.type,
        status="failed",
        content="",
        started_at=now,
        completed_at=now,
        error=error,
        delegation_id=delegation_id,
    )


def _render_template(template: str, *, input_data: dict[str, object], step_outputs: Mapping[str, object]) -> str:
    def replace(match: re.Match[str]) -> str:
        reference = match.group(1)
        value = _resolve_template_reference(reference, input_data=input_data, step_outputs=step_outputs)
        return _stringify_template_value(value)

    return TEMPLATE_REF_RE.sub(replace, template)


def _resolve_template_reference(
    reference: str,
    *,
    input_data: dict[str, object],
    step_outputs: Mapping[str, object],
) -> object:
    if reference.startswith("input."):
        return _resolve_path(reference, input_data)
    if reference.startswith("steps."):
        parts = reference.split(".")
        if len(parts) == 2 or (len(parts) == 3 and parts[2] == "content"):
            return _resolve_step_reference(parts[1], step_outputs)
    msg = f"Unknown template reference '{reference}'."
    raise DynamicWorkflowExecutionError(msg)


def _resolve_path(reference: str, data: Mapping[str, object]) -> object:
    value: object = data
    for part in reference.split(".")[1:]:
        if isinstance(value, Mapping) and part in value:
            value = cast("Mapping[str, object]", value)[part]
        else:
            msg = f"Unknown template reference '{reference}'."
            raise DynamicWorkflowExecutionError(msg)
    return value


def _resolve_step_reference(step_id: str, step_outputs: Mapping[str, object]) -> object:
    if step_id not in step_outputs:
        msg = f"Unknown template reference 'steps.{step_id}'."
        raise DynamicWorkflowExecutionError(msg)
    return step_outputs[step_id]


def _stringify_template_value(value: object) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, indent=2, sort_keys=True)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
