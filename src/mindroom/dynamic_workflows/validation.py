"""Declarative Dynamic Workflow spec and run-input validation."""

from __future__ import annotations

import copy
import re
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from collections.abc import Mapping

ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,127}$")
_SUPPORTED_SCHEMA_VERSION = 1
_STEP_TYPES = frozenset({"agent_step", "report_step", "transform_step"})
_PARTICIPANT_KINDS = frozenset({"ephemeral_agent", "room_agent"})
_AGENT_STEP_TEMPLATE_FIELDS = ("prompt", "response_template", "output_template", "template")
_TEMPLATE_REF_RE = re.compile(r"\{([a-zA-Z0-9_.-]+)\}")
_MAX_WORKFLOW_PARTICIPANTS = 8
_MAX_WORKFLOW_STEPS = 64
_MAX_WORKFLOW_AGENT_STEPS = 16
_MAX_WORKFLOW_RUNTIME_SECONDS = 3600
_MAX_WORKFLOW_CONCURRENT_AGENTS = 8
_PERMISSION_KEYS = frozenset(
    {
        "max_runtime_seconds",
        "max_concurrent_agents",
        "max_total_agents",
        "models",
        "tools",
        "data",
    },
)
_SPEC_KEYS = frozenset(
    {
        "schema_version",
        "id",
        "name",
        "description",
        "kind",
        "inputs",
        "participants",
        "workflow",
        "outputs",
        "permissions",
    },
)
_ROOM_AGENT_PARTICIPANT_KEYS = frozenset({"id", "kind", "agent", "model", "tools"})
_EPHEMERAL_PARTICIPANT_KEYS = frozenset({"id", "kind", "name", "role", "description", "model", "tools", "instructions"})
_AGENT_STEP_KEYS = frozenset({"id", "type", "participant", *_AGENT_STEP_TEMPLATE_FIELDS})
_TRANSFORM_STEP_KEYS = frozenset({"id", "type", "template", "text"})
_REPORT_STEP_KEYS = frozenset({"id", "type", "body_template", "from_step", "title"})
_OUTPUT_KEYS = frozenset({"id", "type", "from_step"})
_OUTPUT_TYPES = frozenset({"text", "markdown", "json", "html_report"})
_INPUT_SCHEMA_KEYS = frozenset({"type", "required", "properties"})
_INPUT_PROPERTY_SCHEMA_KEYS = frozenset({"type", "description", "enum"})


class DynamicWorkflowError(ValueError):
    """Raised when a Dynamic Workflow operation is invalid."""


def validate_workflow_spec(spec: dict[str, object]) -> dict[str, object]:
    """Validate and normalize a declarative Dynamic Workflow spec."""
    if not isinstance(spec, dict):
        msg = "Workflow spec must be a mapping."
        raise DynamicWorkflowError(msg)
    normalized = copy.deepcopy(spec)
    _reject_unsupported_fields(normalized, _SPEC_KEYS, "Workflow spec")
    _validate_schema_version(normalized)
    workflow_id = _required_text(normalized, "id")
    validate_id(workflow_id, "id")
    normalized["id"] = workflow_id
    normalized["name"] = _required_text(normalized, "name")
    kind = _required_text(normalized, "kind")
    if kind != "workflow":
        msg = "Workflow spec kind must be 'workflow'."
        raise DynamicWorkflowError(msg)
    normalized["kind"] = kind

    _validate_input_schema(normalized)
    participants = _required_mapping_list(normalized, "participants", "Participant")
    participant_ids = _validate_participants(participants)
    workflow_steps = _required_mapping_list(normalized, "workflow", "Workflow step")
    step_ids = _validate_workflow_steps(workflow_steps, participant_ids)
    _validate_workflow_limits(normalized, participants, workflow_steps)
    _validate_participant_tool_grants(normalized, participants)
    _validate_outputs(normalized, step_ids)
    return normalized


def validate_workflow_input(spec: dict[str, object], input_data: dict[str, object]) -> None:
    """Validate run input against the workflow's declared input schema."""
    inputs = _input_schema(spec)
    if inputs is None:
        return
    _validate_required_inputs(_input_required_fields(inputs), input_data)
    _validate_input_property_types(_input_properties(inputs), input_data)


def _validate_schema_version(spec: dict[str, object]) -> None:
    value = spec.get("schema_version")
    if not isinstance(value, int) or isinstance(value, bool) or value != _SUPPORTED_SCHEMA_VERSION:
        msg = f"Workflow spec field 'schema_version' must be {_SUPPORTED_SCHEMA_VERSION}."
        raise DynamicWorkflowError(msg)


def workflow_runtime_seconds(spec: dict[str, object]) -> int:
    """Return the validated runtime cap for one workflow spec."""
    permissions = _permissions_mapping(spec)
    value = permissions.get("max_runtime_seconds")
    if value is None:
        return _MAX_WORKFLOW_RUNTIME_SECONDS
    return _positive_int_permission(value, "max_runtime_seconds", maximum=_MAX_WORKFLOW_RUNTIME_SECONDS)


def _input_schema(spec: dict[str, object]) -> dict[str, object] | None:
    raw_inputs = spec.get("inputs")
    if raw_inputs is None:
        return None
    if not isinstance(raw_inputs, dict):
        msg = "Workflow spec field 'inputs' must be a mapping."
        raise DynamicWorkflowError(msg)
    inputs = object_mapping(cast("Mapping[object, object]", raw_inputs))
    _reject_unsupported_fields(inputs, _INPUT_SCHEMA_KEYS, "Workflow input schema")
    input_type = inputs.get("type", "object")
    if input_type != "object":
        msg = "Workflow input schema type must be 'object'."
        raise DynamicWorkflowError(msg)
    return inputs


def _input_required_fields(inputs: dict[str, object]) -> list[str]:
    raw_required = inputs.get("required", [])
    if raw_required is None:
        return []
    if not isinstance(raw_required, list):
        msg = "Workflow input schema field 'required' must be a list."
        raise DynamicWorkflowError(msg)
    required: list[str] = []
    for field_name in raw_required:
        if not isinstance(field_name, str) or not field_name.strip():
            msg = "Workflow input schema required entries must be strings."
            raise DynamicWorkflowError(msg)
        required.append(field_name)
    return required


def _validate_required_inputs(required_fields: list[str], input_data: dict[str, object]) -> None:
    for field_name in required_fields:
        if field_name not in input_data:
            msg = f"Input field '{field_name}' is required."
            raise DynamicWorkflowError(msg)


def _input_properties(inputs: dict[str, object]) -> dict[str, object]:
    raw_properties = inputs.get("properties", {})
    if raw_properties is None:
        return {}
    if not isinstance(raw_properties, dict):
        msg = "Workflow input schema field 'properties' must be a mapping."
        raise DynamicWorkflowError(msg)
    return object_mapping(cast("Mapping[object, object]", raw_properties))


def _validate_input_schema(spec: dict[str, object]) -> None:
    inputs = _input_schema(spec)
    if inputs is None:
        return
    required_fields = _input_required_fields(inputs)
    if len(required_fields) != len(set(required_fields)):
        msg = "Workflow input schema required entries must be unique."
        raise DynamicWorkflowError(msg)
    for field_name, raw_field_schema in _input_properties(inputs).items():
        if not isinstance(raw_field_schema, dict):
            msg = f"Workflow input schema property '{field_name}' must be a mapping."
            raise DynamicWorkflowError(msg)
        field_schema = object_mapping(cast("Mapping[object, object]", raw_field_schema))
        _reject_unsupported_fields(
            field_schema,
            _INPUT_PROPERTY_SCHEMA_KEYS,
            f"Workflow input schema property '{field_name}'",
        )
        allowed_types = _allowed_input_types(field_schema)
        _validate_input_enum(field_name, field_schema, allowed_types)
        for input_type in allowed_types:
            if input_type not in _INPUT_TYPE_CHECKS:
                msg = f"Unsupported workflow input schema type '{input_type}'."
                raise DynamicWorkflowError(msg)


def _validate_input_property_types(properties: dict[str, object], input_data: dict[str, object]) -> None:
    for field_name, raw_field_schema in properties.items():
        if field_name not in input_data or not isinstance(raw_field_schema, dict):
            continue
        field_schema = object_mapping(cast("Mapping[object, object]", raw_field_schema))
        allowed_types = _allowed_input_types(field_schema)
        if not allowed_types:
            _validate_input_enum_value(field_name, field_schema, input_data[field_name])
            continue
        if not any(_input_value_matches_type(input_data[field_name], allowed_type) for allowed_type in allowed_types):
            msg = f"Input field '{field_name}' must be {_input_type_label(allowed_types)}."
            raise DynamicWorkflowError(msg)
        _validate_input_enum_value(field_name, field_schema, input_data[field_name])


def _allowed_input_types(field_schema: dict[str, object]) -> list[str]:
    expected_type = field_schema.get("type")
    if expected_type is None:
        return []
    if isinstance(expected_type, list):
        if not expected_type:
            msg = "Workflow input schema type list must be non-empty."
            raise DynamicWorkflowError(msg)
        allowed_types = []
        for item in expected_type:
            if not isinstance(item, str) or not item.strip():
                msg = "Workflow input schema type list entries must be non-empty strings."
                raise DynamicWorkflowError(msg)
            allowed_types.append(item.strip())
        return allowed_types
    if not isinstance(expected_type, str) or not expected_type.strip():
        msg = "Workflow input schema type must be a non-empty string or list of strings."
        raise DynamicWorkflowError(msg)
    return [str(expected_type)]


def _validate_input_enum(field_name: str, field_schema: dict[str, object], allowed_types: list[str]) -> None:
    enum_values = field_schema.get("enum")
    if enum_values is None:
        return
    if not isinstance(enum_values, list) or not enum_values:
        msg = f"Workflow input schema property '{field_name}' enum must be a non-empty list."
        raise DynamicWorkflowError(msg)
    if not allowed_types:
        return
    for enum_value in enum_values:
        if not any(_input_value_matches_type(enum_value, allowed_type) for allowed_type in allowed_types):
            msg = f"Workflow input schema property '{field_name}' enum values must match its declared type."
            raise DynamicWorkflowError(msg)


def _validate_input_enum_value(field_name: str, field_schema: dict[str, object], value: object) -> None:
    enum_values = field_schema.get("enum")
    if enum_values is None:
        return
    if not isinstance(enum_values, list):
        msg = f"Workflow input schema property '{field_name}' enum must be a list."
        raise DynamicWorkflowError(msg)
    if not any(_enum_value_matches(value, enum_value) for enum_value in enum_values):
        msg = f"Input field '{field_name}' must be one of the declared enum values."
        raise DynamicWorkflowError(msg)


def _enum_value_matches(value: object, enum_value: object) -> bool:
    if type(value) is not type(enum_value):
        return False
    return value == enum_value


def _validate_participants(participants: list[dict[str, object]]) -> set[str]:
    participant_ids: set[str] = set()
    for index, participant in enumerate(participants):
        context = f"Participant at index {index}"
        participant_id = _required_text(participant, "id", context=context)
        validate_id(participant_id, f"{context} id")
        if participant_id in participant_ids:
            msg = f"Duplicate participant id '{participant_id}'."
            raise DynamicWorkflowError(msg)
        participant["id"] = participant_id
        participant_ids.add(participant_id)
        participant_kind = (
            _required_text(participant, "kind", context=context) if "kind" in participant else "ephemeral_agent"
        )
        if participant_kind not in _PARTICIPANT_KINDS:
            msg = f"{context} has unsupported kind '{participant_kind}'."
            raise DynamicWorkflowError(msg)
        participant["kind"] = participant_kind
        if participant_kind == "room_agent":
            _validate_room_agent_participant(participant, context)
        else:
            _validate_ephemeral_agent_participant(participant, context)
    return participant_ids


def _validate_room_agent_participant(participant: dict[str, object], context: str) -> None:
    _reject_unsupported_fields(participant, _ROOM_AGENT_PARTICIPANT_KEYS, context)
    agent_name = _required_text(participant, "agent", context=context)
    participant["agent"] = agent_name
    if "model" in participant and participant.get("model") not in (None, ""):
        msg = f"{context} room_agent participants cannot override model."
        raise DynamicWorkflowError(msg)
    if participant.get("tools") not in (None, []):
        msg = f"{context} room_agent participants cannot declare tools; tool grants are only available to ephemeral participants."
        raise DynamicWorkflowError(msg)


def _validate_ephemeral_agent_participant(participant: dict[str, object], context: str) -> None:
    _reject_unsupported_fields(participant, _EPHEMERAL_PARTICIPANT_KEYS, context)
    participant["tools"] = _normalized_tool_names(
        participant.get("tools"),
        f"{context} field 'tools'",
    )
    if "model" in participant and participant.get("model") is not None:
        model = _required_text(participant, "model", context=context)
        participant["model"] = model
    if "instructions" in participant:
        _validate_participant_instructions(participant["instructions"], context)


def _validate_participant_instructions(value: object, context: str) -> None:
    if value is None or isinstance(value, str):
        return
    if isinstance(value, list) and all(isinstance(instruction, str) for instruction in value):
        return
    msg = f"{context} field 'instructions' must be a string or list of strings."
    raise DynamicWorkflowError(msg)


def _validate_workflow_steps(workflow_steps: list[dict[str, object]], participant_ids: set[str]) -> set[str]:
    step_ids: set[str] = set()
    for index, step in enumerate(workflow_steps):
        context = f"Workflow step at index {index}"
        step_id = _required_text(step, "id", context=context)
        validate_id(step_id, f"{context} id")
        if step_id in step_ids:
            msg = f"Duplicate workflow step id '{step_id}'."
            raise DynamicWorkflowError(msg)
        step["id"] = step_id

        step_type = _step_type(step, context)
        step["type"] = step_type
        if step_type == "agent_step":
            _reject_unsupported_fields(step, _AGENT_STEP_KEYS, context)
            _validate_agent_step(step, context, participant_ids, step_ids)
        elif step_type == "transform_step":
            _reject_unsupported_fields(step, _TRANSFORM_STEP_KEYS, context)
            _validate_template_choice(step, context, ("template", "text"), step_ids)
        elif step_type == "report_step":
            _reject_unsupported_fields(step, _REPORT_STEP_KEYS, context)
            _validate_report_step(step, context, step_ids)
        step_ids.add(step_id)
    return step_ids


def _validate_workflow_limits(
    spec: dict[str, object],
    participants: list[dict[str, object]],
    workflow_steps: list[dict[str, object]],
) -> None:
    if len(participants) > _MAX_WORKFLOW_PARTICIPANTS:
        msg = f"Workflow participants cannot exceed {_MAX_WORKFLOW_PARTICIPANTS}."
        raise DynamicWorkflowError(msg)
    if len(workflow_steps) > _MAX_WORKFLOW_STEPS:
        msg = f"Workflow steps cannot exceed {_MAX_WORKFLOW_STEPS}."
        raise DynamicWorkflowError(msg)

    permissions = _permissions_mapping(spec)
    unknown_permissions = sorted(set(permissions) - _PERMISSION_KEYS)
    if unknown_permissions:
        msg = f"Workflow permissions contain unsupported keys: {', '.join(unknown_permissions)}."
        raise DynamicWorkflowError(msg)

    runtime_seconds = permissions.get("max_runtime_seconds")
    if runtime_seconds is not None:
        permissions["max_runtime_seconds"] = _positive_int_permission(
            runtime_seconds,
            "max_runtime_seconds",
            maximum=_MAX_WORKFLOW_RUNTIME_SECONDS,
        )

    max_concurrent_agents = permissions.get("max_concurrent_agents")
    if max_concurrent_agents is not None:
        permissions["max_concurrent_agents"] = _positive_int_permission(
            max_concurrent_agents,
            "max_concurrent_agents",
            maximum=_MAX_WORKFLOW_CONCURRENT_AGENTS,
        )

    agent_step_count = sum(1 for step in workflow_steps if step.get("type") == "agent_step")
    max_total_agents = permissions.get("max_total_agents")
    if max_total_agents is None:
        max_total_agents = _MAX_WORKFLOW_AGENT_STEPS
    max_total_agents = _positive_int_permission(
        max_total_agents,
        "max_total_agents",
        maximum=_MAX_WORKFLOW_AGENT_STEPS,
    )
    permissions["max_total_agents"] = max_total_agents
    if agent_step_count > max_total_agents:
        msg = f"Workflow agent steps cannot exceed permissions.max_total_agents ({max_total_agents})."
        raise DynamicWorkflowError(msg)

    _validate_permission_models(permissions)
    _validate_permission_tools(permissions)
    _validate_permission_data(permissions)
    spec["permissions"] = permissions


def _permissions_mapping(spec: dict[str, object]) -> dict[str, object]:
    raw_permissions = spec.get("permissions", {})
    if raw_permissions is None:
        return {}
    if not isinstance(raw_permissions, dict):
        msg = "Workflow spec field 'permissions' must be a mapping."
        raise DynamicWorkflowError(msg)
    permissions = object_mapping(cast("Mapping[object, object]", raw_permissions))
    spec["permissions"] = permissions
    return permissions


def _positive_int_permission(value: object, field_name: str, *, maximum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        msg = f"Workflow permission '{field_name}' must be an integer."
        raise DynamicWorkflowError(msg)
    if value < 1 or value > maximum:
        msg = f"Workflow permission '{field_name}' must be between 1 and {maximum}."
        raise DynamicWorkflowError(msg)
    return value


def _validate_permission_models(permissions: dict[str, object]) -> None:
    models = permissions.get("models")
    if models is None:
        return
    if not isinstance(models, list) or not all(isinstance(model, str) and model.strip() for model in models):
        msg = "Workflow permission 'models' must be a list of non-empty strings."
        raise DynamicWorkflowError(msg)
    permissions["models"] = [cast("str", model).strip() for model in models]


def _validate_permission_tools(permissions: dict[str, object]) -> None:
    permissions["tools"] = _normalized_tool_names(permissions.get("tools", []), "Workflow permission 'tools'")


def _normalized_tool_names(raw_tools: object, context: str) -> list[str]:
    if raw_tools is None:
        return []
    if not isinstance(raw_tools, list) or not all(isinstance(tool, str) and tool.strip() for tool in raw_tools):
        msg = f"{context} must be a list of non-empty strings."
        raise DynamicWorkflowError(msg)
    tool_names: list[str] = []
    for raw_tool in raw_tools:
        tool_name = cast("str", raw_tool).strip()
        if tool_name not in tool_names:
            tool_names.append(tool_name)
    return tool_names


def _validate_participant_tool_grants(spec: dict[str, object], participants: list[dict[str, object]]) -> None:
    granted_tools = _permissions_mapping(spec).get("tools", [])
    granted = set(cast("list[str]", granted_tools))
    for participant in participants:
        for tool_name in cast("list[str]", participant.get("tools") or []):
            if tool_name not in granted:
                participant_id = participant["id"]
                msg = f"Participant '{participant_id}' tool '{tool_name}' is not granted by permissions.tools."
                raise DynamicWorkflowError(msg)


def _validate_permission_data(permissions: dict[str, object]) -> None:
    data = permissions.get("data", {})
    if data is None:
        permissions["data"] = {}
        return
    if not isinstance(data, dict):
        msg = "Workflow permission 'data' must be a mapping."
        raise DynamicWorkflowError(msg)
    normalized = object_mapping(cast("Mapping[object, object]", data))
    supported_fields = {"matrix_history", "attachments", "knowledge_bases"}
    unsupported_fields = sorted(set(normalized) - supported_fields)
    if unsupported_fields:
        msg = f"Workflow permission data.{unsupported_fields[0]} is not supported."
        raise DynamicWorkflowError(msg)
    for field_name in ("matrix_history", "attachments"):
        value = normalized.get(field_name)
        if value is not None and value != "none":
            msg = f"Workflow permission data.{field_name} must be 'none' until workflow data grants are supported."
            raise DynamicWorkflowError(msg)
    knowledge_bases = normalized.get("knowledge_bases", [])
    if not isinstance(knowledge_bases, list) or not all(isinstance(base, str) for base in knowledge_bases):
        msg = "Workflow permission data.knowledge_bases must be a list of strings."
        raise DynamicWorkflowError(msg)
    if knowledge_bases:
        msg = "Workflow permission data.knowledge_bases must be empty until workflow data grants are supported."
        raise DynamicWorkflowError(msg)
    permissions["data"] = normalized


def _step_type(step: dict[str, object], context: str) -> str:
    raw_step_type = step.get("type", "agent_step")
    if not isinstance(raw_step_type, str) or not raw_step_type.strip():
        msg = f"{context} field 'type' must be a non-empty string."
        raise DynamicWorkflowError(msg)
    step_type = raw_step_type.strip()
    if step_type not in _STEP_TYPES:
        msg = f"Unsupported workflow step type '{step_type}'."
        raise DynamicWorkflowError(msg)
    return step_type


def _validate_agent_step(
    step: dict[str, object],
    context: str,
    participant_ids: set[str],
    available_step_ids: set[str],
) -> None:
    participant = _required_text(step, "participant", context=context)
    if participant not in participant_ids:
        msg = f"{context} references unknown participant '{participant}'."
        raise DynamicWorkflowError(msg)
    step["participant"] = participant
    _validate_template_choice(
        step,
        context,
        _AGENT_STEP_TEMPLATE_FIELDS,
        available_step_ids,
    )


def _validate_report_step(step: dict[str, object], context: str, available_step_ids: set[str]) -> None:
    if "body_template" in step and "from_step" in step:
        msg = f"{context} must include only one report source field; found: body_template, from_step."
        raise DynamicWorkflowError(msg)
    if "body_template" in step:
        body_template = _required_text(step, "body_template", context=context)
        step["body_template"] = body_template
        _validate_template_references(body_template, available_step_ids, f"{context} field 'body_template'")
    else:
        from_step = _required_text(step, "from_step", context=context)
        if from_step not in available_step_ids:
            msg = f"{context} references unknown prior step '{from_step}'."
            raise DynamicWorkflowError(msg)
        step["from_step"] = from_step

    if "title" in step:
        title = _required_text(step, "title", context=context)
        step["title"] = title
        _validate_template_references(title, available_step_ids, f"{context} field 'title'")


def _validate_outputs(spec: dict[str, object], step_ids: set[str]) -> None:
    raw_outputs = spec.get("outputs", [])
    if raw_outputs is None:
        spec["outputs"] = []
        return
    if not isinstance(raw_outputs, list):
        msg = "Workflow spec field 'outputs' must be a list."
        raise DynamicWorkflowError(msg)
    outputs: list[dict[str, object]] = []
    output_ids: set[str] = set()
    for index, raw_output in enumerate(raw_outputs):
        context = f"Workflow output at index {index}"
        if not isinstance(raw_output, dict):
            msg = f"{context} must be a mapping."
            raise DynamicWorkflowError(msg)
        output = object_mapping(cast("Mapping[object, object]", raw_output))
        output_id = _required_text(output, "id", context=context)
        validate_id(output_id, f"{context} id")
        if output_id in output_ids:
            msg = f"Duplicate workflow output id '{output_id}'."
            raise DynamicWorkflowError(msg)
        output["id"] = output_id
        output_ids.add(output_id)
        output_type = _required_text(output, "type", context=context)
        if output_type not in _OUTPUT_TYPES:
            msg = f"{context} has unsupported type '{output_type}'."
            raise DynamicWorkflowError(msg)
        output["type"] = output_type
        from_step = _required_text(output, "from_step", context=context)
        if from_step not in step_ids:
            msg = f"{context} references unknown step '{from_step}'."
            raise DynamicWorkflowError(msg)
        output["from_step"] = from_step
        _reject_unsupported_fields(output, _OUTPUT_KEYS, context)
        outputs.append(output)
    spec["outputs"] = outputs


def _required_mapping_list(data: dict[str, object], key: str, item_label: str) -> list[dict[str, object]]:
    value = data.get(key)
    if value is None:
        msg = f"Workflow spec field '{key}' is missing."
        raise DynamicWorkflowError(msg)
    if not isinstance(value, list):
        msg = f"Workflow spec field '{key}' must be a list."
        raise DynamicWorkflowError(msg)
    if not value:
        msg = f"Workflow spec field '{key}' cannot be empty."
        raise DynamicWorkflowError(msg)
    items: list[dict[str, object]] = []
    for index, raw_item in enumerate(value):
        if not isinstance(raw_item, dict):
            msg = f"{item_label} at index {index} must be a mapping."
            raise DynamicWorkflowError(msg)
        items.append(object_mapping(cast("Mapping[object, object]", raw_item)))
    data[key] = items
    return items


def _reject_unsupported_fields(data: dict[str, object], allowed_fields: frozenset[str], context: str) -> None:
    unsupported_fields = sorted(set(data) - allowed_fields)
    if unsupported_fields:
        msg = f"{context} contains unsupported field '{unsupported_fields[0]}'."
        raise DynamicWorkflowError(msg)


def _validate_template_choice(
    step: dict[str, object],
    context: str,
    field_names: tuple[str, ...],
    available_step_ids: set[str],
) -> None:
    present_fields = [field_name for field_name in field_names if field_name in step]
    if len(present_fields) > 1:
        fields = ", ".join(present_fields)
        msg = f"{context} must include only one template field; found: {fields}."
        raise DynamicWorkflowError(msg)
    for field_name in present_fields:
        template = _required_text(step, field_name, context=context)
        step[field_name] = template
        _validate_template_references(template, available_step_ids, f"{context} field '{field_name}'")
        return
    fields = ", ".join(field_names)
    msg = f"{context} must include one of: {fields}."
    raise DynamicWorkflowError(msg)


def _validate_template_references(template: str, available_step_ids: set[str], context: str) -> None:
    for match in _TEMPLATE_REF_RE.finditer(template):
        reference = match.group(1)
        if reference.startswith("input."):
            parts = reference.split(".")
            if len(parts) < 2 or any(not part for part in parts[1:]):
                msg = f"{context} contains invalid template reference '{reference}'."
                raise DynamicWorkflowError(msg)
            continue
        if reference.startswith("steps."):
            parts = reference.split(".")
            if len(parts) == 2 or (len(parts) == 3 and parts[2] == "content"):
                step_id = parts[1]
            else:
                msg = f"{context} contains unsupported template reference '{reference}'."
                raise DynamicWorkflowError(msg)
            if step_id not in available_step_ids:
                msg = f"{context} references unknown prior step '{step_id}'."
                raise DynamicWorkflowError(msg)
            continue
        msg = f"{context} contains unknown template reference '{reference}'."
        raise DynamicWorkflowError(msg)


def _is_integer_input(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number_input(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


_INPUT_TYPE_CHECKS = {
    "string": lambda value: isinstance(value, str),
    "integer": _is_integer_input,
    "number": _is_number_input,
    "boolean": lambda value: isinstance(value, bool),
    "object": lambda value: isinstance(value, dict),
    "array": lambda value: isinstance(value, list),
    "null": lambda value: value is None,
}


def _input_value_matches_type(value: object, expected_type: str) -> bool:
    checker = _INPUT_TYPE_CHECKS.get(expected_type)
    if checker is not None:
        return checker(value)
    msg = f"Unsupported workflow input schema type '{expected_type}'."
    raise DynamicWorkflowError(msg)


def _input_type_label(allowed_types: list[str]) -> str:
    labels = {
        "string": "a string",
        "integer": "an integer",
        "number": "a number",
        "boolean": "a boolean",
        "object": "an object",
        "array": "an array",
        "null": "null",
    }
    return " or ".join(labels.get(input_type, input_type) for input_type in allowed_types)


def _required_text(data: dict[str, object], key: str, *, context: str = "Workflow spec") -> str:
    if key not in data:
        msg = f"{context} field '{key}' is missing."
        raise DynamicWorkflowError(msg)
    value = data[key]
    if not isinstance(value, str):
        msg = f"{context} field '{key}' must be a string."
        raise DynamicWorkflowError(msg)
    stripped = value.strip()
    if not stripped:
        msg = f"{context} field '{key}' cannot be empty."
        raise DynamicWorkflowError(msg)
    return stripped


def validate_id(value: str, field_name: str) -> None:
    """Validate one Dynamic Workflow identifier against the shared ID pattern."""
    if not ID_RE.fullmatch(value):
        msg = f"{field_name} must match {ID_RE.pattern}."
        raise DynamicWorkflowError(msg)


def object_mapping(data: Mapping[object, object]) -> dict[str, object]:
    """Normalize one mapping to string keys."""
    return {str(key): value for key, value in data.items()}
