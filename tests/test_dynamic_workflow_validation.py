"""Direct unit tests for parsing Dynamic Workflow specs and checking run input."""

from __future__ import annotations

import re

import pytest

from mindroom.dynamic_workflows.spec import (
    DynamicWorkflowError,
    InputSchema,
    Permissions,
    RoomAgentParticipant,
    SubagentParticipant,
    WorkflowSpecError,
    parse_workflow_spec,
    stored_spec,
)


def _spec(**overrides: object) -> dict[str, object]:
    spec: dict[str, object] = {
        "schema_version": 1,
        "id": "demo-workflow",
        "name": "Demo Workflow",
        "kind": "workflow",
        "participants": [{"id": "writer", "kind": "subagent", "system_prompt": "You write."}],
        "workflow": [{"id": "write", "type": "agent_step", "participant": "writer", "prompt": "Write."}],
    }
    spec.update(overrides)
    return spec


def _errors(raw: object) -> list[str]:
    with pytest.raises(WorkflowSpecError) as caught:
        parse_workflow_spec(raw)
    return list(caught.value.errors)


def _rejects(raw: object, message: str) -> None:
    with pytest.raises(WorkflowSpecError, match=re.escape(message)):
        parse_workflow_spec(raw)


def test_minimal_spec_parses_with_defaults() -> None:
    """A minimal spec parses and gains its defaults."""
    spec = parse_workflow_spec(_spec())
    assert spec.id == "demo-workflow"
    assert spec.outputs == []
    assert spec.permissions == Permissions()
    assert spec.permissions.max_total_agents == 16
    [participant] = spec.participants
    assert isinstance(participant, SubagentParticipant)
    assert participant.tools is None


def test_parse_does_not_mutate_the_input_spec() -> None:
    """Parsing leaves the input spec untouched."""
    spec = _spec(participants=[{"id": " writer ", "system_prompt": "P", "tools": ["file", " file "]}])
    parse_workflow_spec(spec)
    assert "permissions" not in spec
    assert spec["participants"] == [{"id": " writer ", "system_prompt": "P", "tools": ["file", " file "]}]


def test_spec_error_joins_every_message() -> None:
    """A spec error reads as every problem it found, one per line."""
    with pytest.raises(WorkflowSpecError) as caught:
        parse_workflow_spec(_spec(kind="pipeline", name=""))
    assert isinstance(caught.value, DynamicWorkflowError)
    assert str(caught.value) == "\n".join(caught.value.errors)
    assert len(caught.value.errors) == 2


def test_spec_must_be_a_mapping() -> None:
    """Non-mapping specs are rejected."""
    assert _errors(["not", "a", "mapping"]) == [
        "Workflow spec: Input should be a valid dictionary or instance of WorkflowSpec.",
    ]


def test_spec_rejects_unsupported_top_level_field() -> None:
    """Unknown top-level spec fields are rejected."""
    _rejects(_spec(surprise=True), "Workflow spec contains unsupported field 'surprise'.")


@pytest.mark.parametrize("schema_version", [0, 2, True, 1.0, "1", None])
def test_spec_rejects_unsupported_schema_version(schema_version: object) -> None:
    """Only schema_version 1 as a true integer is accepted."""
    assert _errors(_spec(schema_version=schema_version)) == ["Workflow spec field 'schema_version' must be 1."]


def test_spec_rejects_missing_schema_version() -> None:
    """A spec without schema_version is rejected."""
    spec = _spec()
    del spec["schema_version"]
    assert _errors(spec) == ["Workflow spec field 'schema_version': Field required."]


@pytest.mark.parametrize("workflow_id", ["", "Has Spaces", "UPPER", "-leading-dash"])
def test_spec_rejects_invalid_workflow_id(workflow_id: str) -> None:
    """Workflow IDs must match the lowercase id pattern."""
    _rejects(_spec(id=workflow_id), "Workflow spec field 'id': String should match pattern")


def test_spec_rejects_non_workflow_kind() -> None:
    """Only kind 'workflow' is supported."""
    assert _errors(_spec(kind="pipeline")) == ["Workflow spec field 'kind': Input should be 'workflow'."]


def test_spec_strips_text_fields() -> None:
    """Required text fields are stripped."""
    spec = parse_workflow_spec(_spec(id="  demo-workflow  ", name="  Demo  "))
    assert spec.id == "demo-workflow"
    assert spec.name == "Demo"


@pytest.mark.parametrize("description", [None, "", "Does a thing."])
def test_spec_description_is_text_or_null(description: str | None) -> None:
    """The workflow description is optional text."""
    assert parse_workflow_spec(_spec(description=description)).description == description


def test_spec_rejects_non_text_description() -> None:
    """A new spec cannot describe itself with a non-text value."""
    assert _errors(_spec(description=3)) == ["Workflow spec field 'description': Input should be a valid string."]


# --- participants ---


def test_participants_cannot_be_empty() -> None:
    """At least one participant is required."""
    assert _errors(_spec(participants=[])) == [
        "Workflow spec field 'participants': List should have at least 1 item after validation, not 0.",
    ]


def test_participants_must_be_mappings() -> None:
    """Participant entries must be mappings."""
    _rejects(_spec(participants=["writer"]), "Participant at index 0: Input should be a valid dictionary")


def test_participant_requires_id() -> None:
    """Each participant must declare an id."""
    _rejects(
        _spec(participants=[{"kind": "subagent", "system_prompt": "P"}]),
        "Participant at index 0 field 'id': Field required.",
    )


def test_participant_ids_must_be_unique() -> None:
    """Duplicate participant ids are rejected."""
    participants = [{"id": "writer", "system_prompt": "P"}, {"id": "writer", "system_prompt": "P"}]
    assert _errors(_spec(participants=participants)) == ["Duplicate participant id 'writer'."]


def test_participant_kind_defaults_to_subagent() -> None:
    """Participants without a kind are authored subagents."""
    spec = parse_workflow_spec(_spec(participants=[{"id": "writer", "system_prompt": "P"}]))
    assert isinstance(spec.participants[0], SubagentParticipant)
    assert spec.participants[0].kind == "subagent"


def test_participant_rejects_unsupported_kind() -> None:
    """Unknown participant kinds are rejected with the kinds that exist."""
    [error] = _errors(_spec(participants=[{"id": "writer", "kind": "robot"}]))
    assert error == "Participant at index 0 has unsupported kind 'robot'; expected one of 'subagent', 'room_agent'."


def test_participant_rejects_unsupported_field() -> None:
    """Unknown participant fields are rejected before other problems of the same participant."""
    assert _errors(_spec(participants=[{"id": "writer", "voice": "baritone", "mode": "fast"}])) == [
        "Participant at index 0 contains unsupported field 'voice'.",
        "Participant at index 0 field 'mode': Input should be 'standard' or 'minimal'.",
    ]


def test_room_agent_participant_requires_agent_name() -> None:
    """room_agent participants must name a room agent."""
    assert _errors(_spec(participants=[{"id": "writer", "kind": "room_agent"}])) == [
        "Participant at index 0 field 'agent': Field required.",
    ]


def test_room_agent_participant_cannot_override_model() -> None:
    """room_agent participants cannot override the agent model."""
    participants = [{"id": "writer", "kind": "room_agent", "agent": "code", "model": "gpt-6-astra"}]
    assert _errors(_spec(participants=participants)) == [
        "Participant at index 0 room_agent participants cannot override model.",
    ]


def test_room_agent_participant_cannot_declare_tools() -> None:
    """room_agent participants cannot declare tool grants."""
    participants = [{"id": "writer", "kind": "room_agent", "agent": "code", "tools": ["shell"]}]
    assert _errors(_spec(participants=participants)) == [
        "Participant at index 0 room_agent participants cannot declare tools; tool grants are only available to subagent participants.",
    ]


def test_room_agent_participant_keeps_tolerated_empty_overrides() -> None:
    """An empty model or tool list is tolerated on a room agent and kept as written."""
    participants = [{"id": "writer", "kind": "room_agent", "agent": "code", "model": "", "tools": []}]
    spec = parse_workflow_spec(_spec(participants=participants))
    assert isinstance(spec.participants[0], RoomAgentParticipant)
    assert stored_spec(spec)["participants"] == [
        {"id": "writer", "kind": "room_agent", "agent": "code", "model": "", "tools": []},
    ]


def test_subagent_participant_requires_prompt_or_profile() -> None:
    """A subagent participant authors its prompt inline or names a saved profile."""
    for participant in (
        {"id": "writer"},
        {"id": "writer", "system_prompt": "  "},
        {"id": "writer", "system_prompt": None},
    ):
        assert _errors(_spec(participants=[participant])) == [
            "Participant at index 0 needs a non-empty 'system_prompt' or a 'profile'.",
        ]


@pytest.mark.parametrize(
    "participant",
    [
        {"id": "writer", "kind": "subagent", "system_prompt": "P", "tools": ["file"], "model": "haiku"},
        {"id": "writer", "kind": "subagent", "system_prompt": "P", "skills": ["research"]},
        {"id": "writer", "kind": "subagent", "profile": "critic"},
    ],
)
def test_subagent_participant_accepts_inline_fields_or_a_profile(participant: dict[str, object]) -> None:
    """A subagent participant authors its prompt, tools, and model inline, or names a saved profile."""
    parse_workflow_spec(_spec(participants=[participant], permissions={"tools": ["file"]}))


@pytest.mark.parametrize(
    ("participant", "field_name"),
    [
        ({"id": "writer", "profile": "critic", "system_prompt": "P"}, "system_prompt"),
        ({"id": "writer", "profile": "critic", "tools": []}, "tools"),
        ({"id": "writer", "profile": "critic", "skills": []}, "skills"),
        ({"id": "writer", "profile": "critic", "model": None, "mode": None}, "mode"),
    ],
)
def test_profile_excludes_inline_fields_even_when_null_or_empty(
    participant: dict[str, object],
    field_name: str,
) -> None:
    """A profile participant cannot name an inline field, even as null or an empty list."""
    assert _errors(_spec(participants=[participant])) == [
        f"Participant at index 0 uses profile, so it cannot also set '{field_name}'.",
    ]


def test_new_specs_cannot_use_the_retired_ephemeral_kind() -> None:
    """The retired ephemeral participant kind is not accepted in new specs."""
    _rejects(
        _spec(participants=[{"id": "writer", "kind": "ephemeral_agent", "name": "Writer", "role": "Writes"}]),
        "Participant at index 0 has unsupported kind 'ephemeral_agent'",
    )


@pytest.mark.parametrize(
    ("participant", "message"),
    [
        (
            {"id": "writer", "system_prompt": "P", "description": None},
            "field 'description': Input should be a valid string.",
        ),
        ({"id": "writer", "profile": None}, "field 'profile': Input should be a valid string."),
    ],
)
def test_subagent_participant_rejects_explicit_null_description_or_profile(
    participant: dict[str, object],
    message: str,
) -> None:
    """A participant description or profile may be absent but never null."""
    assert _errors(_spec(participants=[participant])) == [f"Participant at index 0 {message}"]


@pytest.mark.parametrize("skills", ["research", [""], [1]])
def test_subagent_participant_skills_must_be_skill_names(skills: object) -> None:
    """A participant's skills are a list of skill names."""
    _rejects(
        _spec(participants=[{"id": "writer", "system_prompt": "P", "skills": skills}]),
        "Participant at index 0 field 'skills",
    )


def test_participant_tools_must_be_granted_by_permissions() -> None:
    """Participant tools must appear in permissions.tools when the spec narrows tools that way."""
    participants = [{"id": "writer", "system_prompt": "P", "tools": ["shell"]}]
    parse_workflow_spec(_spec(participants=participants))
    assert _errors(_spec(participants=participants, permissions={"tools": ["file"]})) == [
        "Participant 'writer' tool 'shell' is not granted by permissions.tools.",
    ]


def test_granted_participant_tools_validate_and_deduplicate() -> None:
    """Granted participant tools are stripped and deduplicated."""
    participants = [{"id": "writer", "system_prompt": "P", "tools": ["shell", " shell "]}]
    spec = parse_workflow_spec(_spec(participants=participants, permissions={"tools": ["shell"]}))
    assert isinstance(spec.participants[0], SubagentParticipant)
    assert spec.participants[0].tools == ["shell"]


@pytest.mark.parametrize("mode", [["standard"], {"standard": True}, "fast"])
def test_subagent_participant_mode_must_be_a_known_string(mode: object) -> None:
    """A non-string mode is a validation error, not a crash."""
    assert _errors(_spec(participants=[{"id": "writer", "system_prompt": "P", "mode": mode}])) == [
        "Participant at index 0 field 'mode': Input should be 'standard' or 'minimal'.",
    ]


def test_function_level_permission_grants_cover_matching_participant_tools() -> None:
    """A permissions.tools entry can grant one function, which a participant may then name."""
    participants = [{"id": "writer", "system_prompt": "P", "tools": ["file.read_file"]}]
    parse_workflow_spec(_spec(participants=participants, permissions={"tools": ["file.read_file"]}))
    assert _errors(
        _spec(
            participants=[{"id": "writer", "system_prompt": "P", "tools": ["file"]}],
            permissions={"tools": ["file.read_file"]},
        ),
    ) == ["Participant 'writer' tool 'file' is not granted by permissions.tools."]


# --- workflow steps ---


def test_workflow_steps_cannot_be_empty() -> None:
    """At least one workflow step is required."""
    assert _errors(_spec(workflow=[])) == [
        "Workflow spec field 'workflow': List should have at least 1 item after validation, not 0.",
    ]


def test_step_requires_id() -> None:
    """Each workflow step must declare an id."""
    assert _errors(_spec(workflow=[{"type": "agent_step", "participant": "writer", "prompt": "x"}])) == [
        "Workflow step at index 0 field 'id': Field required.",
    ]


def test_step_ids_must_be_unique() -> None:
    """Duplicate step ids are rejected."""
    workflow = [
        {"id": "write", "type": "agent_step", "participant": "writer", "prompt": "x"},
        {"id": "write", "type": "transform_step", "text": "y"},
    ]
    assert _errors(_spec(workflow=workflow)) == ["Duplicate workflow step id 'write'."]


def test_step_rejects_unsupported_type() -> None:
    """Unknown step types are rejected with the types that exist."""
    [error] = _errors(_spec(workflow=[{"id": "write", "type": "loop_step"}]))
    assert error == (
        "Workflow step at index 0 has unsupported type 'loop_step'; expected one of 'agent_step', 'transform_step', "
        "'report_step'."
    )


def test_step_type_defaults_to_agent_step() -> None:
    """A step without a type is an agent step."""
    spec = parse_workflow_spec(_spec(workflow=[{"id": "write", "participant": "writer", "prompt": "Write."}]))
    assert spec.workflow[0].type == "agent_step"


def test_agent_step_rejects_unknown_participant() -> None:
    """Agent steps must reference a declared participant."""
    workflow = [{"id": "write", "type": "agent_step", "participant": "ghost", "prompt": "x"}]
    assert _errors(_spec(workflow=workflow)) == ["Workflow step at index 0 references unknown participant 'ghost'."]


def test_agent_step_requires_exactly_one_template_field() -> None:
    """Agent steps must include one template field."""
    workflow = [{"id": "write", "type": "agent_step", "participant": "writer"}]
    assert _errors(_spec(workflow=workflow)) == [
        "Workflow step at index 0 must include one of: prompt, response_template, output_template, template.",
    ]


def test_agent_step_rejects_multiple_template_fields() -> None:
    """Agent steps cannot mix multiple template fields."""
    workflow = [{"id": "write", "type": "agent_step", "participant": "writer", "prompt": "x", "template": "y"}]
    assert _errors(_spec(workflow=workflow)) == [
        "Workflow step at index 0 must include only one template field; found: prompt, template.",
    ]


def test_agent_step_template_is_stripped_and_cannot_be_null() -> None:
    """The chosen template is stripped text, and a template field may be absent but never null."""
    spec = parse_workflow_spec(_spec(workflow=[{"id": "write", "participant": "writer", "prompt": "  Write.  "}]))
    assert spec.workflow[0].templates() == {"prompt": "Write."}
    workflow = [{"id": "write", "participant": "writer", "prompt": "x", "template": None}]
    assert _errors(_spec(workflow=workflow)) == [
        "Workflow step at index 0 field 'template': Input should be a valid string.",
    ]


def test_transform_step_rejects_template_and_text_together() -> None:
    """Transform steps cannot declare both template and text."""
    workflow = [
        {"id": "write", "type": "agent_step", "participant": "writer", "prompt": "x"},
        {"id": "shape", "type": "transform_step", "template": "a", "text": "b"},
    ]
    assert _errors(_spec(workflow=workflow)) == [
        "Workflow step at index 1 must include only one template field; found: template, text.",
    ]


def test_report_step_rejects_both_body_template_and_from_step() -> None:
    """Report steps must use one source field."""
    workflow = [
        {"id": "write", "type": "agent_step", "participant": "writer", "prompt": "x"},
        {"id": "report", "type": "report_step", "body_template": "a", "from_step": "write"},
    ]
    assert _errors(_spec(workflow=workflow)) == [
        "Workflow step at index 1 must include only one report source field; found: body_template, from_step.",
    ]


def test_report_step_requires_a_source() -> None:
    """Report steps must name a body template or a prior step."""
    workflow = [
        {"id": "write", "type": "agent_step", "participant": "writer", "prompt": "x"},
        {"id": "report", "type": "report_step", "title": "Report"},
    ]
    assert _errors(_spec(workflow=workflow)) == [
        "Workflow step at index 1 must include one of: body_template, from_step.",
    ]


def test_report_step_rejects_unknown_from_step() -> None:
    """Report steps must reference a prior step."""
    workflow = [
        {"id": "write", "type": "agent_step", "participant": "writer", "prompt": "x"},
        {"id": "report", "type": "report_step", "from_step": "missing"},
    ]
    assert _errors(_spec(workflow=workflow)) == ["Workflow step at index 1 references unknown prior step 'missing'."]


@pytest.mark.parametrize(
    ("prompt", "message"),
    [
        ("{secrets.key}", "contains unknown template reference 'secrets.key'."),
        ("{input}", "contains unknown template reference 'input'."),
        ("{input.}", "contains invalid template reference 'input.'."),
        ("{input.a..b}", "contains invalid template reference 'input.a..b'."),
        ("{steps.write}", "references unknown prior step 'write'."),
    ],
)
def test_template_rejects_invalid_references(prompt: str, message: str) -> None:
    """Template references use input. or steps. namespaces and may only target prior steps."""
    workflow = [{"id": "write", "type": "agent_step", "participant": "writer", "prompt": prompt}]
    assert _errors(_spec(workflow=workflow)) == [f"Workflow step at index 0 field 'prompt' {message}"]


def test_template_rejects_unsupported_step_attribute() -> None:
    """Only step content references are supported."""
    workflow = [
        {"id": "write", "type": "agent_step", "participant": "writer", "prompt": "x"},
        {"id": "shape", "type": "transform_step", "template": "{steps.write.tokens}"},
    ]
    assert _errors(_spec(workflow=workflow)) == [
        "Workflow step at index 1 field 'template' contains unsupported template reference 'steps.write.tokens'.",
    ]


def test_report_title_references_are_checked() -> None:
    """A report title may only reference prior steps."""
    workflow = [
        {"id": "write", "type": "agent_step", "participant": "writer", "prompt": "x"},
        {"id": "report", "type": "report_step", "from_step": "write", "title": "{steps.report}"},
    ]
    assert _errors(_spec(workflow=workflow)) == [
        "Workflow step at index 1 field 'title' references unknown prior step 'report'.",
    ]


def test_template_accepts_input_and_prior_step_references() -> None:
    """Valid input and prior-step references parse."""
    workflow = [
        {"id": "write", "type": "agent_step", "participant": "writer", "prompt": "Topic: {input.topic}"},
        {"id": "shape", "type": "transform_step", "template": '{steps.write.content} {"not": "a reference"}'},
    ]
    spec = parse_workflow_spec(_spec(workflow=workflow))
    assert [step.id for step in spec.workflow] == ["write", "shape"]


# --- limits and permissions ---


def test_rejects_too_many_participants() -> None:
    """Participant count is capped at 8."""
    participants = [{"id": f"agent-{index}", "system_prompt": "P"} for index in range(9)]
    assert _errors(_spec(participants=participants)) == [
        "Workflow spec field 'participants': List should have at most 8 items after validation, not 9.",
    ]


def test_rejects_too_many_steps() -> None:
    """Step count is capped at 64."""
    workflow = [{"id": f"step-{index}", "type": "transform_step", "text": "x"} for index in range(65)]
    assert _errors(_spec(workflow=workflow)) == [
        "Workflow spec field 'workflow': List should have at most 64 items after validation, not 65.",
    ]


def test_rejects_agent_steps_above_max_total_agents() -> None:
    """Agent step count cannot exceed permissions.max_total_agents."""
    workflow = [
        {"id": f"step-{index}", "type": "agent_step", "participant": "writer", "prompt": "x"} for index in range(3)
    ]
    assert _errors(_spec(workflow=workflow, permissions={"max_total_agents": 2})) == [
        "Workflow agent steps cannot exceed permissions.max_total_agents (2).",
    ]


def test_rejects_unknown_permission_keys() -> None:
    """Unknown permission keys are rejected."""
    assert _errors(_spec(permissions={"budget": 10})) == ["Workflow permission contains unsupported field 'budget'."]


@pytest.mark.parametrize("runtime_seconds", [0, 3601, True, "60"])
def test_rejects_invalid_max_runtime_seconds(runtime_seconds: object) -> None:
    """max_runtime_seconds must be an int between 1 and 3600."""
    _rejects(
        _spec(permissions={"max_runtime_seconds": runtime_seconds}),
        "Workflow permission field 'max_runtime_seconds': ",
    )


def test_rejects_max_concurrent_agents_above_cap() -> None:
    """max_concurrent_agents is capped at 8."""
    assert _errors(_spec(permissions={"max_concurrent_agents": 9})) == [
        "Workflow permission field 'max_concurrent_agents': Input should be less than or equal to 8.",
    ]


def test_rejects_non_string_permission_models() -> None:
    """permissions.models must be non-empty strings."""
    assert _errors(_spec(permissions={"models": ["claude-sonnet-5", 7]})) == [
        "Workflow permission field 'models.1': Input should be a valid string.",
    ]


def test_rejects_unknown_data_permission() -> None:
    """Unknown data permission fields are rejected."""
    assert _errors(_spec(permissions={"data": {"secrets": "all"}})) == [
        "Workflow permission contains unsupported field 'data.secrets'.",
    ]


@pytest.mark.parametrize("field_name", ["matrix_history", "attachments"])
def test_rejects_non_none_data_grants(field_name: str) -> None:
    """Data grants other than 'none' are not yet supported."""
    assert _errors(_spec(permissions={"data": {field_name: "thread"}})) == [
        f"Workflow permission data.{field_name} must be 'none' until workflow data grants are supported.",
    ]


def test_rejects_non_empty_knowledge_bases_grant() -> None:
    """Knowledge base grants are not yet supported."""
    assert _errors(_spec(permissions={"data": {"knowledge_bases": ["docs"]}})) == [
        "Workflow permission data.knowledge_bases must be empty until workflow data grants are supported.",
    ]


def test_runtime_seconds_defaults_to_cap() -> None:
    """The runtime cap defaults to 3600 seconds."""
    assert parse_workflow_spec(_spec()).permissions.runtime_seconds == 3600


def test_runtime_seconds_returns_declared_value() -> None:
    """A declared max_runtime_seconds is the runtime cap."""
    spec = parse_workflow_spec(_spec(permissions={"max_runtime_seconds": 120}))
    assert spec.permissions.runtime_seconds == 120


# --- outputs ---


def test_outputs_must_be_a_list() -> None:
    """Outputs must be declared as a list."""
    assert _errors(_spec(outputs={"id": "report"})) == ["Workflow spec field 'outputs': Input should be a valid list."]


def test_output_requires_supported_type() -> None:
    """Output types are restricted to the supported set."""
    outputs = [{"id": "report", "type": "pdf", "from_step": "write"}]
    assert _errors(_spec(outputs=outputs)) == [
        "Workflow output at index 0 field 'type': Input should be 'text', 'markdown', 'json' or 'html_report'.",
    ]


def test_output_requires_known_from_step() -> None:
    """Outputs must reference a declared step."""
    outputs = [{"id": "report", "type": "markdown", "from_step": "missing"}]
    assert _errors(_spec(outputs=outputs)) == ["Workflow output at index 0 references unknown step 'missing'."]


def test_output_requires_type_field() -> None:
    """Outputs must declare a type."""
    outputs = [{"id": "report", "from_step": "write"}]
    assert _errors(_spec(outputs=outputs)) == ["Workflow output at index 0 field 'type': Field required."]


def test_output_ids_must_be_unique() -> None:
    """Duplicate output ids are rejected."""
    outputs = [
        {"id": "report", "type": "markdown", "from_step": "write"},
        {"id": "report", "type": "text", "from_step": "write"},
    ]
    assert _errors(_spec(outputs=outputs)) == ["Duplicate workflow output id 'report'."]


def test_output_rejects_unsupported_field() -> None:
    """Unknown output fields are rejected."""
    outputs = [{"id": "report", "type": "markdown", "from_step": "write", "filename": "report.md"}]
    assert _errors(_spec(outputs=outputs)) == ["Workflow output at index 0 contains unsupported field 'filename'."]


# --- input schema declaration ---


def test_input_schema_type_must_be_object() -> None:
    """The input schema root type must be 'object'."""
    assert _errors(_spec(inputs={"type": "array"})) == ["Workflow input schema field 'type': Input should be 'object'."]


def test_input_schema_rejects_unsupported_keywords() -> None:
    """Unsupported JSON Schema keywords are rejected."""
    inputs = {"type": "object", "additionalProperties": False, "properties": {"topic": {"minLength": 1}}}
    assert _errors(_spec(inputs=inputs)) == [
        "Workflow input schema property 'topic' contains unsupported field 'minLength'.",
        "Workflow input schema contains unsupported field 'additionalProperties'.",
    ]


def test_input_schema_rejects_duplicate_required_entries() -> None:
    """Required entries must be unique."""
    inputs = {"type": "object", "required": ["topic", "topic"]}
    assert _errors(_spec(inputs=inputs)) == ["Workflow input schema required entries must be unique."]


def test_input_schema_keeps_required_entries_as_written() -> None:
    """Required entries must not be blank, and are kept unstripped because run input is looked up by them."""
    assert parse_workflow_spec(_spec(inputs={"required": [" topic "]})).inputs == InputSchema(required=[" topic "])
    assert _errors(_spec(inputs={"required": [" "]})) == [
        "Workflow input schema field 'required.0': String should not be blank.",
    ]


@pytest.mark.parametrize(
    ("field_type", "message"),
    [
        ("uuid", "Unsupported workflow input schema type 'uuid'."),
        (" string", "Unsupported workflow input schema type ' string'."),
        ([], "Workflow input schema type list must be non-empty."),
        (["string", ""], "Workflow input schema type list entries must be non-empty strings."),
        (7, "Workflow input schema type must be a non-empty string or list of strings."),
    ],
)
def test_input_schema_rejects_invalid_property_types(field_type: object, message: str) -> None:
    """Property types are one supported type name or a non-empty list of them."""
    inputs = {"type": "object", "properties": {"topic": {"type": field_type}}}
    assert _errors(_spec(inputs=inputs)) == [message]


def test_input_schema_strips_listed_property_types() -> None:
    """Listed property type names are stripped."""
    spec = parse_workflow_spec(_spec(inputs={"properties": {"limit": {"type": [" integer ", "null"]}}}))
    assert spec.inputs is not None
    assert spec.inputs.properties["limit"].types == ("integer", "null")


def test_input_schema_rejects_enum_values_outside_declared_type() -> None:
    """Enum values must match the declared property type."""
    inputs = {"type": "object", "properties": {"depth": {"type": "string", "enum": ["low", 2]}}}
    assert _errors(_spec(inputs=inputs)) == [
        "Workflow input schema property 'depth' enum values must match its declared type.",
    ]


def test_input_schema_property_description_is_text_or_null() -> None:
    """A new spec describes an input property with text or null."""
    parse_workflow_spec(_spec(inputs={"properties": {"topic": {"description": None}}}))
    assert _errors(_spec(inputs={"properties": {"topic": {"description": ["x"]}}})) == [
        "Workflow input schema property 'topic' field 'description': Input should be a valid string.",
    ]


# --- run input ---


def _input_schema(properties: dict[str, object], required: list[str] | None = None) -> InputSchema:
    return InputSchema.model_validate({"type": "object", "required": required or [], "properties": properties})


def test_input_requires_declared_required_fields() -> None:
    """Missing required input fields are rejected."""
    schema = _input_schema({"topic": {"type": "string"}}, required=["topic"])
    with pytest.raises(DynamicWorkflowError, match="Input field 'topic' is required"):
        schema.check({})


def test_input_accepts_valid_required_value() -> None:
    """Valid required input passes."""
    _input_schema({"topic": {"type": "string"}}, required=["topic"]).check({"topic": "competitors"})


def test_input_rejects_wrong_type() -> None:
    """Input values must match the declared property type."""
    with pytest.raises(DynamicWorkflowError, match="Input field 'topic' must be a string"):
        _input_schema({"topic": {"type": "string"}}).check({"topic": 7})


def test_input_rejects_boolean_for_integer_field() -> None:
    """Booleans are not accepted for integer fields."""
    with pytest.raises(DynamicWorkflowError, match="Input field 'count' must be an integer"):
        _input_schema({"count": {"type": "integer"}}).check({"count": True})


def test_input_accepts_any_type_from_type_list() -> None:
    """Any type from a declared type list is accepted."""
    schema = _input_schema({"limit": {"type": ["integer", "null"]}})
    schema.check({"limit": 3})
    schema.check({"limit": None})
    with pytest.raises(DynamicWorkflowError, match="Input field 'limit' must be an integer or null"):
        schema.check({"limit": "3"})


def test_input_rejects_value_outside_enum() -> None:
    """Input values outside the declared enum are rejected."""
    with pytest.raises(DynamicWorkflowError, match="must be one of the declared enum values"):
        _input_schema({"depth": {"type": "string", "enum": ["low", "high"]}}).check({"depth": "medium"})


def test_input_accepts_enum_value() -> None:
    """Declared enum values are accepted."""
    _input_schema({"depth": {"type": "string", "enum": ["low", "high"]}}).check({"depth": "high"})


def test_input_enum_without_type_still_enforced() -> None:
    """Enums are enforced even without a declared type, and true does not match 1."""
    with pytest.raises(DynamicWorkflowError, match="must be one of the declared enum values"):
        _input_schema({"depth": {"enum": [1, 2]}}).check({"depth": True})


def test_input_ignores_undeclared_fields() -> None:
    """Undeclared input fields are ignored."""
    _input_schema({"topic": {"type": "string"}}).check({"topic": "ok", "extra": object()})


# --- error collection ---


def test_errors_report_all_top_level_problems_at_once() -> None:
    """Every top-level problem is reported in one pass, unsupported fields first."""
    assert _errors({"steps": [], "extra": 1, "schema_version": 2, "id": "x", "name": "X"}) == [
        "Workflow spec contains unsupported field 'steps'.",
        "Workflow spec contains unsupported field 'extra'.",
        "Workflow spec field 'schema_version' must be 1.",
        "Workflow spec field 'kind': Field required.",
        "Workflow spec field 'participants': Field required.",
        "Workflow spec field 'workflow': Field required.",
    ]


def test_errors_report_participant_and_step_problems_together() -> None:
    """Problems in different participants and steps surface in the same pass."""
    assert _errors(
        _spec(
            participants=[{"id": "p", "agent": "someone"}, {"id": "q"}],
            workflow=[{"id": "s1", "participant": "p"}, {"id": "s2", "type": "report_step"}],
        ),
    ) == [
        "Participant at index 0 contains unsupported field 'agent'.",
        "Participant at index 1 needs a non-empty 'system_prompt' or a 'profile'.",
        "Workflow step at index 0 must include one of: prompt, response_template, output_template, template.",
        "Workflow step at index 1 must include one of: body_template, from_step.",
    ]


def test_errors_handle_invalid_participant_tools_without_crashing() -> None:
    """A participant whose tools are not a list fails that field alone."""
    assert _errors(
        _spec(
            participants=[{"id": "writer", "system_prompt": "P", "tools": 123}],
            workflow=[{"id": "write", "participant": "writer", "prompt": "Write."}],
        ),
    ) == ["Participant at index 0 field 'tools': Input should be a valid list."]


def test_errors_report_permissions_when_structure_is_missing() -> None:
    """Permission problems do not depend on participant or workflow presence."""
    assert _errors(
        {"schema_version": 1, "id": "demo", "name": "Demo", "kind": "workflow", "permissions": "invalid"},
    ) == [
        "Workflow spec field 'participants': Field required.",
        "Workflow spec field 'workflow': Field required.",
        "Workflow spec field 'permissions': Input should be a valid dictionary or instance of Permissions.",
    ]


def test_reference_rules_wait_until_every_field_parses() -> None:
    """Rules relating items, such as grants and output steps, report only once every item parses."""
    spec = _spec(
        participants=[{"id": "writer", "system_prompt": "P", "tools": ["shell"]}],
        outputs=[{"id": "result", "type": "text", "from_step": "missing"}],
        permissions={"tools": ["file"]},
    )
    del spec["workflow"]
    assert _errors(spec) == ["Workflow spec field 'workflow': Field required."]


def test_errors_keep_invalid_step_id_available_to_later_steps() -> None:
    """A step's own problem does not create false unknown-reference errors later."""
    assert _errors(
        _spec(
            workflow=[
                {"id": "draft", "participant": "writer"},
                {"id": "publish", "type": "transform_step", "template": "{steps.draft}"},
            ],
            outputs=[{"id": "result", "type": "text", "from_step": "draft"}],
        ),
    ) == ["Workflow step at index 0 must include one of: prompt, response_template, output_template, template."]


# --- stored form ---


def test_nulls_in_optional_fields_parse_as_absent() -> None:
    """A null in an optional field means the field is absent, and the stored spec leaves it out."""
    spec = parse_workflow_spec(
        _spec(
            description=None,
            inputs={"type": "object", "required": None, "properties": {"t": {"type": None, "enum": None}}},
            participants=[
                {"id": "writer", "system_prompt": "P", "tools": None, "skills": None, "model": None, "mode": None},
                {"id": "critic", "kind": "room_agent", "agent": "code", "model": None, "tools": None},
            ],
            outputs=None,
            permissions={
                "max_runtime_seconds": None,
                "max_concurrent_agents": None,
                "max_total_agents": None,
                "models": None,
                "tools": None,
                "data": {"matrix_history": None, "attachments": "none"},
            },
        ),
    )
    assert spec.permissions.max_total_agents == 16
    assert spec.permissions.runtime_seconds == 3600
    assert stored_spec(spec) == {
        "schema_version": 1,
        "id": "demo-workflow",
        "name": "Demo Workflow",
        "kind": "workflow",
        "inputs": {"type": "object", "required": [], "properties": {"t": {}}},
        "participants": [
            {"id": "writer", "kind": "subagent", "system_prompt": "P"},
            {"id": "critic", "kind": "room_agent", "agent": "code"},
        ],
        "workflow": [{"id": "write", "type": "agent_step", "participant": "writer", "prompt": "Write."}],
        "outputs": [],
        "permissions": {"max_total_agents": 16, "tools": [], "data": {"attachments": "none", "knowledge_bases": []}},
    }
    assert parse_workflow_spec(_spec(permissions=None)).permissions == Permissions()


def test_stored_spec_parses_back_to_the_same_spec() -> None:
    """A stored spec parses back unchanged, so a profile participant stays free of inline fields."""
    spec = parse_workflow_spec(
        _spec(
            description="Does a thing.",
            participants=[
                {"id": "writer", "profile": "critic", "description": "Reviews."},
                {"id": "drafter", "system_prompt": " Keep spaces. ", "tools": [], "mode": "minimal"},
            ],
            workflow=[
                {"id": "write", "participant": "writer", "prompt": "Write {input.topic}."},
                {"id": "report", "type": "report_step", "from_step": "write", "title": "Report"},
            ],
            outputs=[{"id": "out", "type": "html_report", "from_step": "report"}],
            inputs={"required": ["topic"], "properties": {"topic": {"type": ["string", "number"], "enum": ["a", 1.5]}}},
        ),
    )
    stored = stored_spec(spec)
    assert stored["participants"] == [
        {"id": "writer", "kind": "subagent", "description": "Reviews.", "profile": "critic"},
        {"id": "drafter", "kind": "subagent", "system_prompt": " Keep spaces. ", "tools": [], "mode": "minimal"},
    ]
    assert parse_workflow_spec(stored) == spec
