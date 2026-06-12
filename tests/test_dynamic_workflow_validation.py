"""Direct unit tests for Dynamic Workflow spec and run-input validation."""

from __future__ import annotations

import pytest

from mindroom.dynamic_workflows.validation import (
    DynamicWorkflowError,
    validate_workflow_input,
    validate_workflow_spec,
    workflow_runtime_seconds,
)


def _spec(**overrides: object) -> dict[str, object]:
    spec: dict[str, object] = {
        "schema_version": 1,
        "id": "demo-workflow",
        "name": "Demo Workflow",
        "kind": "workflow",
        "participants": [{"id": "writer", "kind": "ephemeral_agent", "name": "Writer"}],
        "workflow": [{"id": "write", "type": "agent_step", "participant": "writer", "prompt": "Write."}],
    }
    spec.update(overrides)
    return spec


def test_minimal_spec_validates_and_normalizes_defaults() -> None:
    """A minimal spec validates and gains normalized defaults."""
    validated = validate_workflow_spec(_spec())
    assert validated["id"] == "demo-workflow"
    assert validated["outputs"] == []
    permissions = validated["permissions"]
    assert permissions == {"tools": [], "data": {}, "max_total_agents": 16}
    participant = validated["participants"][0]
    assert participant["kind"] == "ephemeral_agent"
    assert participant["tools"] == []


def test_validate_does_not_mutate_the_input_spec() -> None:
    """Validation normalizes a deep copy, leaving the input spec untouched."""
    spec = _spec()
    validate_workflow_spec(spec)
    assert "permissions" not in spec
    assert "tools" not in spec["participants"][0]


def test_spec_must_be_a_mapping() -> None:
    """Non-mapping specs are rejected."""
    with pytest.raises(DynamicWorkflowError, match="must be a mapping"):
        validate_workflow_spec(["not", "a", "mapping"])  # type: ignore[arg-type]


def test_spec_rejects_unsupported_top_level_field() -> None:
    """Unknown top-level spec fields are rejected."""
    with pytest.raises(DynamicWorkflowError, match="unsupported field 'surprise'"):
        validate_workflow_spec(_spec(surprise=True))


@pytest.mark.parametrize("schema_version", [0, 2, True, 1.0, "1", None])
def test_spec_rejects_unsupported_schema_version(schema_version: object) -> None:
    """Only schema_version 1 as a true integer is accepted."""
    with pytest.raises(DynamicWorkflowError, match="'schema_version' must be 1"):
        validate_workflow_spec(_spec(schema_version=schema_version))


def test_spec_rejects_missing_schema_version() -> None:
    """A spec without schema_version is rejected."""
    spec = _spec()
    del spec["schema_version"]
    with pytest.raises(DynamicWorkflowError, match="'schema_version' must be 1"):
        validate_workflow_spec(spec)


@pytest.mark.parametrize("workflow_id", ["", "Has Spaces", "UPPER", "-leading-dash"])
def test_spec_rejects_invalid_workflow_id(workflow_id: str) -> None:
    """Workflow IDs must match the lowercase id pattern."""
    with pytest.raises(DynamicWorkflowError):
        validate_workflow_spec(_spec(id=workflow_id))


def test_spec_rejects_non_workflow_kind() -> None:
    """Only kind 'workflow' is supported."""
    with pytest.raises(DynamicWorkflowError, match="kind must be 'workflow'"):
        validate_workflow_spec(_spec(kind="pipeline"))


def test_spec_strips_text_fields() -> None:
    """Required text fields are stripped during normalization."""
    validated = validate_workflow_spec(_spec(id="  demo-workflow  ", name="  Demo  "))
    assert validated["id"] == "demo-workflow"
    assert validated["name"] == "Demo"


# --- participants ---


def test_participants_cannot_be_empty() -> None:
    """At least one participant is required."""
    with pytest.raises(DynamicWorkflowError, match="'participants' cannot be empty"):
        validate_workflow_spec(_spec(participants=[]))


def test_participants_must_be_mappings() -> None:
    """Participant entries must be mappings."""
    with pytest.raises(DynamicWorkflowError, match="Participant at index 0 must be a mapping"):
        validate_workflow_spec(_spec(participants=["writer"]))


def test_participant_requires_id() -> None:
    """Each participant must declare an id."""
    with pytest.raises(DynamicWorkflowError, match="Participant at index 0 field 'id' is missing"):
        validate_workflow_spec(_spec(participants=[{"kind": "ephemeral_agent"}]))


def test_participant_ids_must_be_unique() -> None:
    """Duplicate participant ids are rejected."""
    participants = [{"id": "writer"}, {"id": "writer"}]
    with pytest.raises(DynamicWorkflowError, match="Duplicate participant id 'writer'"):
        validate_workflow_spec(_spec(participants=participants))


def test_participant_kind_defaults_to_ephemeral_agent() -> None:
    """Participants without a kind default to ephemeral_agent."""
    validated = validate_workflow_spec(_spec(participants=[{"id": "writer"}]))
    assert validated["participants"][0]["kind"] == "ephemeral_agent"


def test_participant_rejects_unsupported_kind() -> None:
    """Unknown participant kinds are rejected."""
    with pytest.raises(DynamicWorkflowError, match="unsupported kind 'robot'"):
        validate_workflow_spec(_spec(participants=[{"id": "writer", "kind": "robot"}]))


def test_participant_rejects_unsupported_field() -> None:
    """Unknown participant fields are rejected."""
    with pytest.raises(DynamicWorkflowError, match="unsupported field 'voice'"):
        validate_workflow_spec(_spec(participants=[{"id": "writer", "voice": "baritone"}]))


def test_room_agent_participant_requires_agent_name() -> None:
    """room_agent participants must name a room agent."""
    with pytest.raises(DynamicWorkflowError, match="field 'agent' is missing"):
        validate_workflow_spec(_spec(participants=[{"id": "writer", "kind": "room_agent"}]))


def test_room_agent_participant_cannot_override_model() -> None:
    """room_agent participants cannot override the agent model."""
    participants = [{"id": "writer", "kind": "room_agent", "agent": "code", "model": "gpt-5.5"}]
    with pytest.raises(DynamicWorkflowError, match="cannot override model"):
        validate_workflow_spec(_spec(participants=participants))


def test_room_agent_participant_cannot_declare_tools() -> None:
    """room_agent participants cannot declare tool grants."""
    participants = [{"id": "writer", "kind": "room_agent", "agent": "code", "tools": ["shell"]}]
    with pytest.raises(DynamicWorkflowError, match="cannot declare tools"):
        validate_workflow_spec(_spec(participants=participants))


def test_ephemeral_participant_instructions_must_be_strings() -> None:
    """Participant instructions must be a string or list of strings."""
    participants = [{"id": "writer", "instructions": [1, 2]}]
    with pytest.raises(DynamicWorkflowError, match="must be a string or list of strings"):
        validate_workflow_spec(_spec(participants=participants))


def test_participant_tools_must_be_granted_by_permissions() -> None:
    """Participant tools must appear in permissions.tools."""
    participants = [{"id": "writer", "tools": ["shell"]}]
    with pytest.raises(DynamicWorkflowError, match=r"not granted by permissions\.tools"):
        validate_workflow_spec(_spec(participants=participants))


def test_granted_participant_tools_validate_and_deduplicate() -> None:
    """Granted participant tools are stripped and deduplicated."""
    participants = [{"id": "writer", "tools": ["shell", " shell "]}]
    validated = validate_workflow_spec(_spec(participants=participants, permissions={"tools": ["shell"]}))
    assert validated["participants"][0]["tools"] == ["shell"]


# --- workflow steps ---


def test_workflow_steps_cannot_be_empty() -> None:
    """At least one workflow step is required."""
    with pytest.raises(DynamicWorkflowError, match="'workflow' cannot be empty"):
        validate_workflow_spec(_spec(workflow=[]))


def test_step_requires_id() -> None:
    """Each workflow step must declare an id."""
    with pytest.raises(DynamicWorkflowError, match="Workflow step at index 0 field 'id' is missing"):
        validate_workflow_spec(_spec(workflow=[{"type": "agent_step", "participant": "writer", "prompt": "x"}]))


def test_step_ids_must_be_unique() -> None:
    """Duplicate step ids are rejected."""
    workflow = [
        {"id": "write", "type": "agent_step", "participant": "writer", "prompt": "x"},
        {"id": "write", "type": "transform_step", "text": "y"},
    ]
    with pytest.raises(DynamicWorkflowError, match="Duplicate workflow step id 'write'"):
        validate_workflow_spec(_spec(workflow=workflow))


def test_step_rejects_unsupported_type() -> None:
    """Unknown step types are rejected."""
    with pytest.raises(DynamicWorkflowError, match="Unsupported workflow step type 'loop_step'"):
        validate_workflow_spec(_spec(workflow=[{"id": "write", "type": "loop_step"}]))


def test_agent_step_rejects_unknown_participant() -> None:
    """Agent steps must reference a declared participant."""
    workflow = [{"id": "write", "type": "agent_step", "participant": "ghost", "prompt": "x"}]
    with pytest.raises(DynamicWorkflowError, match="references unknown participant 'ghost'"):
        validate_workflow_spec(_spec(workflow=workflow))


def test_agent_step_requires_exactly_one_template_field() -> None:
    """Agent steps must include one template field."""
    workflow = [{"id": "write", "type": "agent_step", "participant": "writer"}]
    with pytest.raises(DynamicWorkflowError, match="must include one of"):
        validate_workflow_spec(_spec(workflow=workflow))


def test_agent_step_rejects_multiple_template_fields() -> None:
    """Agent steps cannot mix multiple template fields."""
    workflow = [{"id": "write", "type": "agent_step", "participant": "writer", "prompt": "x", "template": "y"}]
    with pytest.raises(DynamicWorkflowError, match="only one template field"):
        validate_workflow_spec(_spec(workflow=workflow))


def test_transform_step_rejects_template_and_text_together() -> None:
    """Transform steps cannot declare both template and text."""
    workflow = [
        {"id": "write", "type": "agent_step", "participant": "writer", "prompt": "x"},
        {"id": "shape", "type": "transform_step", "template": "a", "text": "b"},
    ]
    with pytest.raises(DynamicWorkflowError, match="only one template field"):
        validate_workflow_spec(_spec(workflow=workflow))


def test_report_step_rejects_both_body_template_and_from_step() -> None:
    """Report steps must use one source field."""
    workflow = [
        {"id": "write", "type": "agent_step", "participant": "writer", "prompt": "x"},
        {"id": "report", "type": "report_step", "body_template": "a", "from_step": "write"},
    ]
    with pytest.raises(DynamicWorkflowError, match="only one report source field"):
        validate_workflow_spec(_spec(workflow=workflow))


def test_report_step_rejects_unknown_from_step() -> None:
    """Report steps must reference a prior step."""
    workflow = [
        {"id": "write", "type": "agent_step", "participant": "writer", "prompt": "x"},
        {"id": "report", "type": "report_step", "from_step": "missing"},
    ]
    with pytest.raises(DynamicWorkflowError, match="references unknown prior step 'missing'"):
        validate_workflow_spec(_spec(workflow=workflow))


def test_template_rejects_unknown_reference_namespace() -> None:
    """Template references must use input. or steps. namespaces."""
    workflow = [{"id": "write", "type": "agent_step", "participant": "writer", "prompt": "{secrets.key}"}]
    with pytest.raises(DynamicWorkflowError, match=r"unknown template reference 'secrets\.key'"):
        validate_workflow_spec(_spec(workflow=workflow))


def test_template_rejects_reference_to_later_step() -> None:
    """Template references may only target prior steps."""
    workflow = [{"id": "write", "type": "agent_step", "participant": "writer", "prompt": "{steps.write}"}]
    with pytest.raises(DynamicWorkflowError, match="references unknown prior step 'write'"):
        validate_workflow_spec(_spec(workflow=workflow))


def test_template_rejects_unsupported_step_attribute() -> None:
    """Only step content references are supported."""
    workflow = [
        {"id": "write", "type": "agent_step", "participant": "writer", "prompt": "x"},
        {"id": "shape", "type": "transform_step", "template": "{steps.write.tokens}"},
    ]
    with pytest.raises(DynamicWorkflowError, match="unsupported template reference"):
        validate_workflow_spec(_spec(workflow=workflow))


def test_template_accepts_input_and_prior_step_references() -> None:
    """Valid input and prior-step references pass validation."""
    workflow = [
        {"id": "write", "type": "agent_step", "participant": "writer", "prompt": "Topic: {input.topic}"},
        {"id": "shape", "type": "transform_step", "template": "{steps.write.content}"},
    ]
    validated = validate_workflow_spec(_spec(workflow=workflow))
    assert [step["id"] for step in validated["workflow"]] == ["write", "shape"]


# --- limits and permissions ---


def test_rejects_too_many_participants() -> None:
    """Participant count is capped at 8."""
    participants = [{"id": "writer"}, *({"id": f"agent-{index}"} for index in range(8))]
    with pytest.raises(DynamicWorkflowError, match="participants cannot exceed 8"):
        validate_workflow_spec(_spec(participants=participants))


def test_rejects_too_many_steps() -> None:
    """Step count is capped at 64."""
    workflow = [{"id": f"step-{index}", "type": "transform_step", "text": "x"} for index in range(65)]
    with pytest.raises(DynamicWorkflowError, match="steps cannot exceed 64"):
        validate_workflow_spec(_spec(workflow=workflow))


def test_rejects_agent_steps_above_max_total_agents() -> None:
    """Agent step count cannot exceed permissions.max_total_agents."""
    workflow = [
        {"id": f"step-{index}", "type": "agent_step", "participant": "writer", "prompt": "x"} for index in range(3)
    ]
    with pytest.raises(DynamicWorkflowError, match=r"cannot exceed permissions.max_total_agents \(2\)"):
        validate_workflow_spec(_spec(workflow=workflow, permissions={"max_total_agents": 2}))


def test_rejects_unknown_permission_keys() -> None:
    """Unknown permission keys are rejected."""
    with pytest.raises(DynamicWorkflowError, match="unsupported keys: budget"):
        validate_workflow_spec(_spec(permissions={"budget": 10}))


@pytest.mark.parametrize("runtime_seconds", [0, 3601, True, "60"])
def test_rejects_invalid_max_runtime_seconds(runtime_seconds: object) -> None:
    """max_runtime_seconds must be an int between 1 and 3600."""
    with pytest.raises(DynamicWorkflowError, match="max_runtime_seconds"):
        validate_workflow_spec(_spec(permissions={"max_runtime_seconds": runtime_seconds}))


def test_rejects_max_concurrent_agents_above_cap() -> None:
    """max_concurrent_agents is capped at 8."""
    with pytest.raises(DynamicWorkflowError, match="'max_concurrent_agents' must be between 1 and 8"):
        validate_workflow_spec(_spec(permissions={"max_concurrent_agents": 9}))


def test_rejects_non_string_permission_models() -> None:
    """permissions.models must be non-empty strings."""
    with pytest.raises(DynamicWorkflowError, match="'models' must be a list of non-empty strings"):
        validate_workflow_spec(_spec(permissions={"models": ["claude-sonnet-4-6", 7]}))


def test_rejects_unknown_data_permission() -> None:
    """Unknown data permission fields are rejected."""
    with pytest.raises(DynamicWorkflowError, match=r"data\.secrets is not supported"):
        validate_workflow_spec(_spec(permissions={"data": {"secrets": "all"}}))


@pytest.mark.parametrize("field_name", ["matrix_history", "attachments"])
def test_rejects_non_none_data_grants(field_name: str) -> None:
    """Data grants other than 'none' are not yet supported."""
    with pytest.raises(DynamicWorkflowError, match=f"data.{field_name} must be 'none'"):
        validate_workflow_spec(_spec(permissions={"data": {field_name: "thread"}}))


def test_rejects_non_empty_knowledge_bases_grant() -> None:
    """Knowledge base grants are not yet supported."""
    with pytest.raises(DynamicWorkflowError, match="knowledge_bases must be empty"):
        validate_workflow_spec(_spec(permissions={"data": {"knowledge_bases": ["docs"]}}))


def test_workflow_runtime_seconds_defaults_to_cap() -> None:
    """The runtime cap defaults to 3600 seconds."""
    assert workflow_runtime_seconds(validate_workflow_spec(_spec())) == 3600


def test_workflow_runtime_seconds_returns_declared_value() -> None:
    """A declared max_runtime_seconds is returned as-is."""
    validated = validate_workflow_spec(_spec(permissions={"max_runtime_seconds": 120}))
    assert workflow_runtime_seconds(validated) == 120


# --- outputs ---


def test_outputs_must_be_a_list() -> None:
    """Outputs must be declared as a list."""
    with pytest.raises(DynamicWorkflowError, match="'outputs' must be a list"):
        validate_workflow_spec(_spec(outputs={"id": "report"}))


def test_output_requires_supported_type() -> None:
    """Output types are restricted to the supported set."""
    outputs = [{"id": "report", "type": "pdf", "from_step": "write"}]
    with pytest.raises(DynamicWorkflowError, match="unsupported type 'pdf'"):
        validate_workflow_spec(_spec(outputs=outputs))


def test_output_requires_known_from_step() -> None:
    """Outputs must reference a declared step."""
    outputs = [{"id": "report", "type": "markdown", "from_step": "missing"}]
    with pytest.raises(DynamicWorkflowError, match="references unknown step 'missing'"):
        validate_workflow_spec(_spec(outputs=outputs))


def test_output_requires_type_field() -> None:
    """Outputs must declare a type."""
    outputs = [{"id": "report", "from_step": "write"}]
    with pytest.raises(DynamicWorkflowError, match="field 'type' is missing"):
        validate_workflow_spec(_spec(outputs=outputs))


def test_output_ids_must_be_unique() -> None:
    """Duplicate output ids are rejected."""
    outputs = [
        {"id": "report", "type": "markdown", "from_step": "write"},
        {"id": "report", "type": "text", "from_step": "write"},
    ]
    with pytest.raises(DynamicWorkflowError, match="Duplicate workflow output id 'report'"):
        validate_workflow_spec(_spec(outputs=outputs))


def test_output_rejects_unsupported_field() -> None:
    """Unknown output fields are rejected."""
    outputs = [{"id": "report", "type": "markdown", "from_step": "write", "filename": "report.md"}]
    with pytest.raises(DynamicWorkflowError, match="unsupported field 'filename'"):
        validate_workflow_spec(_spec(outputs=outputs))


# --- input schema declaration ---


def test_input_schema_type_must_be_object() -> None:
    """The input schema root type must be 'object'."""
    with pytest.raises(DynamicWorkflowError, match="input schema type must be 'object'"):
        validate_workflow_spec(_spec(inputs={"type": "array"}))


def test_input_schema_rejects_unsupported_keywords() -> None:
    """Unsupported JSON Schema keywords are rejected."""
    inputs = {"type": "object", "additionalProperties": False}
    with pytest.raises(DynamicWorkflowError, match="unsupported field 'additionalProperties'"):
        validate_workflow_spec(_spec(inputs=inputs))


def test_input_schema_rejects_duplicate_required_entries() -> None:
    """Required entries must be unique."""
    inputs = {"type": "object", "required": ["topic", "topic"]}
    with pytest.raises(DynamicWorkflowError, match="required entries must be unique"):
        validate_workflow_spec(_spec(inputs=inputs))


def test_input_schema_rejects_unknown_property_type() -> None:
    """Property types are restricted to the supported set."""
    inputs = {"type": "object", "properties": {"topic": {"type": "uuid"}}}
    with pytest.raises(DynamicWorkflowError, match="Unsupported workflow input schema type 'uuid'"):
        validate_workflow_spec(_spec(inputs=inputs))


def test_input_schema_rejects_empty_type_list() -> None:
    """An empty property type list is rejected."""
    inputs = {"type": "object", "properties": {"topic": {"type": []}}}
    with pytest.raises(DynamicWorkflowError, match="type list must be non-empty"):
        validate_workflow_spec(_spec(inputs=inputs))


def test_input_schema_rejects_enum_values_outside_declared_type() -> None:
    """Enum values must match the declared property type."""
    inputs = {"type": "object", "properties": {"depth": {"type": "string", "enum": ["low", 2]}}}
    with pytest.raises(DynamicWorkflowError, match="enum values must match its declared type"):
        validate_workflow_spec(_spec(inputs=inputs))


# --- validate_workflow_input ---


def _inputs_spec(properties: dict[str, object], required: list[str] | None = None) -> dict[str, object]:
    return {"inputs": {"type": "object", "required": required or [], "properties": properties}}


def test_input_accepts_when_spec_declares_no_schema() -> None:
    """Specs without an input schema accept any input."""
    validate_workflow_input({}, {"anything": "goes"})


def test_input_requires_declared_required_fields() -> None:
    """Missing required input fields are rejected."""
    spec = _inputs_spec({"topic": {"type": "string"}}, required=["topic"])
    with pytest.raises(DynamicWorkflowError, match="Input field 'topic' is required"):
        validate_workflow_input(spec, {})


def test_input_accepts_valid_required_value() -> None:
    """Valid required input passes validation."""
    spec = _inputs_spec({"topic": {"type": "string"}}, required=["topic"])
    validate_workflow_input(spec, {"topic": "competitors"})


def test_input_rejects_wrong_type() -> None:
    """Input values must match the declared property type."""
    spec = _inputs_spec({"topic": {"type": "string"}})
    with pytest.raises(DynamicWorkflowError, match="Input field 'topic' must be a string"):
        validate_workflow_input(spec, {"topic": 7})


def test_input_rejects_boolean_for_integer_field() -> None:
    """Booleans are not accepted for integer fields."""
    spec = _inputs_spec({"count": {"type": "integer"}})
    with pytest.raises(DynamicWorkflowError, match="Input field 'count' must be an integer"):
        validate_workflow_input(spec, {"count": True})


def test_input_accepts_any_type_from_type_list() -> None:
    """Any type from a declared type list is accepted."""
    spec = _inputs_spec({"limit": {"type": ["integer", "null"]}})
    validate_workflow_input(spec, {"limit": 3})
    validate_workflow_input(spec, {"limit": None})


def test_input_rejects_value_outside_enum() -> None:
    """Input values outside the declared enum are rejected."""
    spec = _inputs_spec({"depth": {"type": "string", "enum": ["low", "high"]}})
    with pytest.raises(DynamicWorkflowError, match="must be one of the declared enum values"):
        validate_workflow_input(spec, {"depth": "medium"})


def test_input_accepts_enum_value() -> None:
    """Declared enum values are accepted."""
    spec = _inputs_spec({"depth": {"type": "string", "enum": ["low", "high"]}})
    validate_workflow_input(spec, {"depth": "high"})


def test_input_enum_without_type_still_enforced() -> None:
    """Enums are enforced even without a declared type."""
    spec = _inputs_spec({"depth": {"enum": [1, 2]}})
    with pytest.raises(DynamicWorkflowError, match="must be one of the declared enum values"):
        validate_workflow_input(spec, {"depth": True})


def test_input_ignores_undeclared_fields() -> None:
    """Undeclared input fields are ignored."""
    spec = _inputs_spec({"topic": {"type": "string"}})
    validate_workflow_input(spec, {"topic": "ok", "extra": object()})
