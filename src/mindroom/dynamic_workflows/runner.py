"""Executable Dynamic Workflow step runner."""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol, cast

_TEMPLATE_REF_RE = re.compile(r"\{([a-zA-Z0-9_.-]+)\}")


class DynamicWorkflowExecutionError(ValueError):
    """Raised when a Dynamic Workflow step cannot execute."""


class ParticipantExecutor(Protocol):
    """Callable that executes one resolved Dynamic Workflow participant."""

    def __call__(
        self,
        *,
        participant: dict[str, object],
        prompt: str,
        input_data: dict[str, object],
        step_outputs: dict[str, object],
    ) -> object:
        """Run one participant with rendered prompt and prior step outputs."""


class AsyncParticipantExecutor(Protocol):
    """Async callable that executes one resolved Dynamic Workflow participant."""

    def __call__(
        self,
        *,
        participant: dict[str, object],
        prompt: str,
        input_data: dict[str, object],
        step_outputs: dict[str, object],
    ) -> Awaitable[object]:
        """Run one participant with rendered prompt and prior step outputs."""


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
        }


@dataclass(frozen=True)
class _DynamicWorkflowExecution:
    """Execution summary for one Dynamic Workflow run."""

    status: str
    steps: list[_DynamicWorkflowStepResult]
    outputs: dict[str, object]
    report_markdown: str
    error: str | None = None

    def step_outputs_json(self) -> dict[str, object]:
        """Return step outputs keyed by step ID."""
        return {step.step_id: step.to_json() for step in self.steps}


def execute_workflow_spec(
    spec: dict[str, object],
    input_data: dict[str, object],
    *,
    participant_executor: ParticipantExecutor | None = None,
) -> _DynamicWorkflowExecution:
    """Execute a declarative Dynamic Workflow spec sequentially."""
    steps: list[_DynamicWorkflowStepResult] = []
    step_outputs: dict[str, object] = {}
    participants_by_id = _participants_by_id(spec)

    for raw_step in _workflow_steps(spec):
        try:
            result = execute_workflow_step(
                raw_step,
                input_data=input_data,
                step_outputs=step_outputs,
                participant_executor=participant_executor,
                participants_by_id=participants_by_id,
            )
        except DynamicWorkflowExecutionError as exc:
            failed_step = _failed_step(raw_step, str(exc))
            steps.append(failed_step)
            return _DynamicWorkflowExecution(
                status="failed",
                steps=steps,
                outputs={},
                report_markdown=_failed_report_markdown(spec, input_data, steps, str(exc)),
                error=str(exc),
            )
        steps.append(result)
        step_outputs[result.step_id] = result.content

    outputs = _collect_outputs(spec, step_outputs)
    return _DynamicWorkflowExecution(
        status="completed",
        steps=steps,
        outputs=outputs,
        report_markdown=_report_markdown(spec, input_data, steps, outputs),
    )


async def async_execute_workflow_spec(
    spec: dict[str, object],
    input_data: dict[str, object],
    *,
    participant_executor: AsyncParticipantExecutor | None = None,
) -> _DynamicWorkflowExecution:
    """Execute a declarative Dynamic Workflow spec sequentially on the current event loop."""
    steps: list[_DynamicWorkflowStepResult] = []
    step_outputs: dict[str, object] = {}
    participants_by_id = _participants_by_id(spec)

    for raw_step in _workflow_steps(spec):
        try:
            result = await _async_execute_workflow_step(
                raw_step,
                input_data=input_data,
                step_outputs=step_outputs,
                participant_executor=participant_executor,
                participants_by_id=participants_by_id,
            )
        except DynamicWorkflowExecutionError as exc:
            failed_step = _failed_step(raw_step, str(exc))
            steps.append(failed_step)
            return _DynamicWorkflowExecution(
                status="failed",
                steps=steps,
                outputs={},
                report_markdown=_failed_report_markdown(spec, input_data, steps, str(exc)),
                error=str(exc),
            )
        steps.append(result)
        step_outputs[result.step_id] = result.content

    outputs = _collect_outputs(spec, step_outputs)
    return _DynamicWorkflowExecution(
        status="completed",
        steps=steps,
        outputs=outputs,
        report_markdown=_report_markdown(spec, input_data, steps, outputs),
    )


def execute_workflow_step(
    step: dict[str, object],
    *,
    input_data: dict[str, object],
    step_outputs: Mapping[str, object],
    participant_executor: ParticipantExecutor | None = None,
    participants_by_id: Mapping[str, dict[str, object]] | None = None,
) -> _DynamicWorkflowStepResult:
    """Execute one declarative workflow step."""
    step_id = _required_text(step, "id")
    step_type = str(step.get("type", "agent_step"))
    started_at = _utc_now()
    content = _execute_step_content(
        step,
        step_type=step_type,
        input_data=input_data,
        step_outputs=step_outputs,
        participant_executor=participant_executor,
        participants_by_id=participants_by_id,
    )
    return _DynamicWorkflowStepResult(
        step_id=step_id,
        step_type=step_type,
        status="completed",
        content=content,
        started_at=started_at,
        completed_at=_utc_now(),
    )


async def _async_execute_workflow_step(
    step: dict[str, object],
    *,
    input_data: dict[str, object],
    step_outputs: Mapping[str, object],
    participant_executor: AsyncParticipantExecutor | None = None,
    participants_by_id: Mapping[str, dict[str, object]] | None = None,
) -> _DynamicWorkflowStepResult:
    """Execute one declarative workflow step on the current event loop."""
    step_id = _required_text(step, "id")
    step_type = str(step.get("type", "agent_step"))
    started_at = _utc_now()
    content = await _aexecute_step_content(
        step,
        step_type=step_type,
        input_data=input_data,
        step_outputs=step_outputs,
        participant_executor=participant_executor,
        participants_by_id=participants_by_id,
    )
    return _DynamicWorkflowStepResult(
        step_id=step_id,
        step_type=step_type,
        status="completed",
        content=content,
        started_at=started_at,
        completed_at=_utc_now(),
    )


def _execute_step_content(
    step: dict[str, object],
    *,
    step_type: str,
    input_data: dict[str, object],
    step_outputs: Mapping[str, object],
    participant_executor: ParticipantExecutor | None,
    participants_by_id: Mapping[str, dict[str, object]] | None,
) -> object:
    if step_type == "transform_step":
        template = _step_template(step, ("template", "text"))
        return _render_template(template, input_data=input_data, step_outputs=step_outputs)

    if step_type == "agent_step":
        step_id = _required_text(step, "id")
        if participant_executor is None:
            msg = f"Agent step '{step_id}' requires a participant executor."
            raise DynamicWorkflowExecutionError(msg)
        participant_id = _required_text(step, "participant")
        if participants_by_id is None or participant_id not in participants_by_id:
            msg = f"Agent step '{step_id}' references unknown participant '{participant_id}'."
            raise DynamicWorkflowExecutionError(msg)
        template = _step_template(step, ("prompt", "response_template", "output_template", "template"))
        prompt = _render_template(template, input_data=input_data, step_outputs=step_outputs)
        try:
            return participant_executor(
                participant=participants_by_id[participant_id],
                prompt=prompt,
                input_data=input_data,
                step_outputs=dict(step_outputs),
            )
        except DynamicWorkflowExecutionError:
            raise
        except Exception as exc:
            msg = f"Agent step '{step_id}' participant '{participant_id}' failed: {exc}"
            raise DynamicWorkflowExecutionError(msg) from exc

    if step_type == "report_step":
        body_template = step.get("body_template")
        if isinstance(body_template, str):
            body = _render_template(body_template, input_data=input_data, step_outputs=step_outputs)
        else:
            source_step = _required_text(step, "from_step")
            body = _stringify_template_value(_resolve_step_reference(source_step, step_outputs))
        title = step.get("title")
        if isinstance(title, str) and title.strip():
            rendered_title = _render_template(title, input_data=input_data, step_outputs=step_outputs)
            return f"# {rendered_title}\n\n{body}"
        return body

    msg = f"Unsupported workflow step type '{step_type}'."
    raise DynamicWorkflowExecutionError(msg)


async def _aexecute_step_content(
    step: dict[str, object],
    *,
    step_type: str,
    input_data: dict[str, object],
    step_outputs: Mapping[str, object],
    participant_executor: AsyncParticipantExecutor | None,
    participants_by_id: Mapping[str, dict[str, object]] | None,
) -> object:
    if step_type != "agent_step":
        return _execute_step_content(
            step,
            step_type=step_type,
            input_data=input_data,
            step_outputs=step_outputs,
            participant_executor=None,
            participants_by_id=participants_by_id,
        )

    step_id = _required_text(step, "id")
    if participant_executor is None:
        msg = f"Agent step '{step_id}' requires a participant executor."
        raise DynamicWorkflowExecutionError(msg)
    participant_id = _required_text(step, "participant")
    if participants_by_id is None or participant_id not in participants_by_id:
        msg = f"Agent step '{step_id}' references unknown participant '{participant_id}'."
        raise DynamicWorkflowExecutionError(msg)
    template = _step_template(step, ("prompt", "response_template", "output_template", "template"))
    prompt = _render_template(template, input_data=input_data, step_outputs=step_outputs)
    try:
        return await participant_executor(
            participant=participants_by_id[participant_id],
            prompt=prompt,
            input_data=input_data,
            step_outputs=dict(step_outputs),
        )
    except DynamicWorkflowExecutionError:
        raise
    except Exception as exc:
        msg = f"Agent step '{step_id}' participant '{participant_id}' failed: {exc}"
        raise DynamicWorkflowExecutionError(msg) from exc


def _workflow_steps(spec: dict[str, object]) -> list[dict[str, object]]:
    raw_steps = spec.get("workflow", [])
    if not isinstance(raw_steps, list):
        msg = "Workflow spec field 'workflow' must be a list."
        raise DynamicWorkflowExecutionError(msg)
    steps: list[dict[str, object]] = []
    seen: set[str] = set()
    for raw_step in raw_steps:
        if not isinstance(raw_step, dict):
            msg = "Workflow steps must be mappings."
            raise DynamicWorkflowExecutionError(msg)
        step = _object_mapping(cast("Mapping[object, object]", raw_step))
        step_id = _required_text(step, "id")
        if step_id in seen:
            msg = f"Duplicate workflow step id '{step_id}'."
            raise DynamicWorkflowExecutionError(msg)
        seen.add(step_id)
        steps.append(step)
    return steps


def _participants_by_id(spec: dict[str, object]) -> dict[str, dict[str, object]]:
    raw_participants = spec.get("participants", [])
    if not isinstance(raw_participants, list):
        msg = "Workflow spec field 'participants' must be a list."
        raise DynamicWorkflowExecutionError(msg)
    participants: dict[str, dict[str, object]] = {}
    for raw_participant in raw_participants:
        if not isinstance(raw_participant, dict):
            msg = "Workflow participants must be mappings."
            raise DynamicWorkflowExecutionError(msg)
        participant = _object_mapping(cast("Mapping[object, object]", raw_participant))
        participant_id = _required_text(participant, "id")
        participants[participant_id] = participant
    return participants


def _collect_outputs(spec: dict[str, object], step_outputs: Mapping[str, object]) -> dict[str, object]:
    raw_outputs = spec.get("outputs", [])
    if not isinstance(raw_outputs, list):
        return {}
    outputs: dict[str, object] = {}
    for raw_output in raw_outputs:
        if not isinstance(raw_output, dict):
            continue
        output = _object_mapping(cast("Mapping[object, object]", raw_output))
        output_id = _required_text(output, "id")
        from_step = output.get("from_step")
        if isinstance(from_step, str) and from_step.strip():
            outputs[output_id] = _resolve_step_reference(from_step.strip(), step_outputs)
    return outputs


def _report_markdown(
    spec: dict[str, object],
    input_data: dict[str, object],
    steps: list[_DynamicWorkflowStepResult],
    outputs: dict[str, object],
) -> str:
    html_report_output = _first_html_report_output(spec)
    if html_report_output is not None and html_report_output in outputs:
        return _with_input_section(_stringify_template_value(outputs[html_report_output]), input_data)

    for step in reversed(steps):
        if step.step_type == "report_step":
            return _with_input_section(_stringify_template_value(step.content), input_data)

    title = str(spec["name"])
    input_json = json.dumps(input_data, indent=2, sort_keys=True)
    step_sections = "\n\n".join(f"## {step.step_id}\n\n{_stringify_template_value(step.content)}" for step in steps)
    return (
        f"# {title}\n\nDynamic Workflow run completed.\n\n## Input\n\n```json\n{input_json}\n```\n\n{step_sections}\n"
    )


def _with_input_section(report_markdown: str, input_data: dict[str, object]) -> str:
    input_json = json.dumps(input_data, indent=2, sort_keys=True)
    return f"{report_markdown.rstrip()}\n\n## Input\n\n```json\n{input_json}\n```\n"


def _failed_report_markdown(
    spec: dict[str, object],
    input_data: dict[str, object],
    steps: list[_DynamicWorkflowStepResult],
    error: str,
) -> str:
    title = str(spec["name"])
    input_json = json.dumps(input_data, indent=2, sort_keys=True)
    step_sections = "\n\n".join(f"## {step.step_id}\n\nStatus: {step.status}\n\nError: {step.error}" for step in steps)
    return (
        f"# {title}\n\n"
        "Dynamic Workflow run failed.\n\n"
        f"## Error\n\n{error}\n\n"
        f"## Input\n\n```json\n{input_json}\n```\n\n"
        f"{step_sections}\n"
    )


def _first_html_report_output(spec: dict[str, object]) -> str | None:
    raw_outputs = spec.get("outputs", [])
    if not isinstance(raw_outputs, list):
        return None
    for raw_output in raw_outputs:
        if not isinstance(raw_output, dict):
            continue
        output = _object_mapping(cast("Mapping[object, object]", raw_output))
        if output.get("type") == "html_report":
            return _required_text(output, "id")
    return None


def _failed_step(step: dict[str, object], error: str) -> _DynamicWorkflowStepResult:
    now = _utc_now()
    return _DynamicWorkflowStepResult(
        step_id=str(step.get("id", "unknown")),
        step_type=str(step.get("type", "agent_step")),
        status="failed",
        content="",
        started_at=now,
        completed_at=now,
        error=error,
    )


def _step_template(step: dict[str, object], field_names: tuple[str, ...]) -> str:
    for field_name in field_names:
        value = step.get(field_name)
        if isinstance(value, str):
            return value
    fields = ", ".join(field_names)
    msg = f"Workflow step '{step.get('id', 'unknown')}' must include one of: {fields}."
    raise DynamicWorkflowExecutionError(msg)


def _render_template(template: str, *, input_data: dict[str, object], step_outputs: Mapping[str, object]) -> str:
    def replace(match: re.Match[str]) -> str:
        reference = match.group(1)
        value = _resolve_template_reference(reference, input_data=input_data, step_outputs=step_outputs)
        return _stringify_template_value(value)

    return _TEMPLATE_REF_RE.sub(replace, template)


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
        if len(parts) == 2:
            return _resolve_step_reference(parts[1], step_outputs)
        if len(parts) == 3 and parts[2] == "content":
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


def _required_text(data: dict[str, object], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        msg = f"Workflow step field '{key}' must be a non-empty string."
        raise DynamicWorkflowExecutionError(msg)
    return value.strip()


def _stringify_template_value(value: object) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, indent=2, sort_keys=True)


def _object_mapping(data: Mapping[object, object]) -> dict[str, object]:
    return {str(key): value for key, value in data.items()}


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
