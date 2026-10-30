"""Typed Dynamic Workflow spec, parsed once from the declarative mapping an agent authors."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Annotated, Literal, Self, cast

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Discriminator,
    Field,
    JsonValue,
    StringConstraints,
    Tag,
    ValidationError,
    ValidationInfo,
    model_validator,
)
from pydantic_core import ErrorDetails, PydanticCustomError, PydanticKnownError

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence

ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,127}$")
TEMPLATE_REF_RE = re.compile(r"\{([a-zA-Z0-9_.-]+)\}")
_MAX_WORKFLOW_RUNTIME_SECONDS = 3600
_AGENT_STEP_TEMPLATE_FIELDS = ("prompt", "response_template", "output_template", "template")
_SUBAGENT_INLINE_FIELDS = frozenset({"system_prompt", "tools", "skills", "model", "mode"})
_UNION_TAGS = frozenset({"subagent", "room_agent", "agent_step", "transform_step", "report_step"})


class DynamicWorkflowError(ValueError):
    """Raised when a Dynamic Workflow operation is invalid."""


class WorkflowSpecError(DynamicWorkflowError):
    """Raised when a workflow spec is invalid; ``errors`` holds one sentence per problem found."""

    def __init__(self, errors: Sequence[str]) -> None:
        super().__init__("\n".join(errors))
        self.errors = tuple(errors)


def _rule(message: str) -> PydanticCustomError:
    """Return a spec rule error whose message is shown as written."""
    return PydanticCustomError("workflow_rule", "{message}", {"message": message})


def _item_rule(message: str) -> PydanticCustomError:
    """Return a rule error for one participant, step, output, or input property, shown after that item's label."""
    return PydanticCustomError("workflow_item_rule", "{message}", {"message": message})


def _not_null(value: object) -> object:
    """Refuse an explicit null for a field that may be absent but is never null."""
    if value is None:
        error_type = "string_type"
        raise PydanticKnownError(error_type)
    return value


def _non_blank(value: str) -> str:
    if not value.strip():
        error_type, msg = "string_blank", "String should not be blank"
        raise PydanticCustomError(error_type, msg)
    return value


def _unique[T](values: list[T]) -> list[T]:
    return list(dict.fromkeys(values))


def _null_as(default: Callable[[], object]) -> BeforeValidator:
    """Read an explicit null as the field's default, as an absent field."""
    return BeforeValidator(lambda value: default() if value is None else value)


_Id = Annotated[str, StringConstraints(strip_whitespace=True, pattern=ID_RE.pattern)]
_Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
# A field that may be absent, but that holds text when present.
_TextIfPresent = Annotated[_Text | None, BeforeValidator(_not_null)]
_Names = Annotated[list[_Text], AfterValidator(_unique)]


class _SpecModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


def _present(**fields: str | None) -> dict[str, str]:
    """Return the named fields that are set, in order."""
    return {name: value for name, value in fields.items() if value is not None}


def _require_one(present: Mapping[str, str], names: Sequence[str], what: str) -> None:
    if len(present) > 1:
        msg = f"must include only one {what}; found: {', '.join(present)}."
        raise _item_rule(msg)
    if not present:
        msg = f"must include one of: {', '.join(names)}."
        raise _item_rule(msg)


# --- inputs ---


def _is_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


_INPUT_TYPE_CHECKS: dict[str, Callable[[object], bool]] = {
    "string": lambda value: isinstance(value, str),
    "integer": _is_integer,
    "number": _is_number,
    "boolean": lambda value: isinstance(value, bool),
    "object": lambda value: isinstance(value, dict),
    "array": lambda value: isinstance(value, list),
    "null": lambda value: value is None,
}
_INPUT_TYPE_LABELS = {
    "string": "a string",
    "integer": "an integer",
    "number": "a number",
    "boolean": "a boolean",
    "object": "an object",
    "array": "an array",
    "null": "null",
}


def _input_type(value: object) -> object:
    """Check a property's declared type: one type name, or a non-empty list of them whose entries are stripped."""
    if value is None:
        return None
    if isinstance(value, list):
        if not value:
            msg = "Workflow input schema type list must be non-empty."
            raise _rule(msg)
        if not all(isinstance(item, str) and item.strip() for item in value):
            msg = "Workflow input schema type list entries must be non-empty strings."
            raise _rule(msg)
        names = [str(item).strip() for item in value]
    elif isinstance(value, str) and value.strip():
        names = [value]
    else:
        msg = "Workflow input schema type must be a non-empty string or list of strings."
        raise _rule(msg)
    for name in names:
        if name not in _INPUT_TYPE_CHECKS:
            msg = f"Unsupported workflow input schema type '{name}'."
            raise _rule(msg)
    return names if isinstance(value, list) else value


def _matches_any(value: object, types: Iterable[str]) -> bool:
    return any(_INPUT_TYPE_CHECKS[name](value) for name in types)


class InputProperty(_SpecModel):
    """One declared run-input field."""

    type: Annotated[str | list[str] | None, BeforeValidator(_input_type)] = None
    description: str | None = None
    enum: Annotated[list[JsonValue], Field(min_length=1)] | None = None

    @property
    def types(self) -> tuple[str, ...]:
        """Return the declared type names; no names accept any value."""
        if self.type is None:
            return ()
        return (self.type,) if isinstance(self.type, str) else tuple(self.type)

    @model_validator(mode="after")
    def _enum_matches_types(self) -> Self:
        if self.enum is not None and self.types and not all(_matches_any(value, self.types) for value in self.enum):
            msg = "enum values must match its declared type."
            raise _item_rule(msg)
        return self

    def check(self, field_name: str, value: object) -> None:
        """Refuse a run-input value outside this field's declared types or enum."""
        if self.types and not _matches_any(value, self.types):
            label = " or ".join(_INPUT_TYPE_LABELS[name] for name in self.types)
            msg = f"Input field '{field_name}' must be {label}."
            raise DynamicWorkflowError(msg)
        # An enum value matches only a value of the same JSON type, so true does not match 1.
        if self.enum is not None and not any(type(value) is type(item) and value == item for item in self.enum):
            msg = f"Input field '{field_name}' must be one of the declared enum values."
            raise DynamicWorkflowError(msg)


class InputSchema(_SpecModel):
    """The run-input schema, a small subset of JSON Schema."""

    type: Literal["object"] = "object"
    # Required names are kept as written, because run input is looked up by them.
    required: Annotated[list[Annotated[str, AfterValidator(_non_blank)]], _null_as(list)] = Field(
        default_factory=list,
    )
    properties: Annotated[dict[str, InputProperty], _null_as(dict)] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _unique_required(self) -> Self:
        if len(self.required) != len(set(self.required)):
            msg = "Workflow input schema required entries must be unique."
            raise _rule(msg)
        return self

    def check(self, input_data: Mapping[str, object]) -> None:
        """Refuse run input that misses a required field or has a declared field of the wrong type."""
        for field_name in self.required:
            if field_name not in input_data:
                msg = f"Input field '{field_name}' is required."
                raise DynamicWorkflowError(msg)
        for field_name, field_schema in self.properties.items():
            if field_name in input_data:
                field_schema.check(field_name, input_data[field_name])


# --- participants ---


class SubagentParticipant(_SpecModel):
    """An authored copy of the caller: a saved profile, or an inline prompt with optional tools, skills, model, and mode."""

    id: _Id
    kind: Literal["subagent"] = "subagent"
    description: Annotated[str | None, BeforeValidator(_not_null)] = None
    profile: _TextIfPresent = None
    system_prompt: str | None = None
    tools: _Names | None = None
    skills: _Names | None = None
    model: _Text | None = None
    mode: Literal["standard", "minimal"] | None = None

    @model_validator(mode="after")
    def _profile_or_prompt(self) -> Self:
        if self.profile is not None:
            # A field the author names conflicts with the profile even when it is null or empty.
            inline = sorted(_SUBAGENT_INLINE_FIELDS & self.model_fields_set)
            if inline:
                msg = f"uses profile, so it cannot also set '{inline[0]}'."
                raise _item_rule(msg)
        elif self.system_prompt is None or not self.system_prompt.strip():
            msg = "needs a non-empty 'system_prompt' or a 'profile'."
            raise _item_rule(msg)
        return self


class RoomAgentParticipant(_SpecModel):
    """Another agent available in the room, run on the step prompt without tools."""

    id: _Id
    kind: Literal["room_agent"]
    agent: _Text
    model: str | None = None
    tools: list[str] | None = None

    @model_validator(mode="after")
    def _no_overrides(self) -> Self:
        if self.model:
            msg = "room_agent participants cannot override model."
            raise _item_rule(msg)
        if self.tools:
            msg = (
                "room_agent participants cannot declare tools; tool grants are only available to subagent participants."
            )
            raise _item_rule(msg)
        return self


def _participant_kind(value: object) -> object:
    if isinstance(value, dict):
        return cast("dict[str, object]", value).get("kind", "subagent")
    return value.kind if isinstance(value, RoomAgentParticipant) else "subagent"


Participant = Annotated[
    Annotated[SubagentParticipant, Tag("subagent")] | Annotated[RoomAgentParticipant, Tag("room_agent")],
    Discriminator(_participant_kind),
]


# --- steps ---


class AgentStep(_SpecModel):
    """A turn of one participant on a rendered prompt."""

    id: _Id
    type: Literal["agent_step"] = "agent_step"
    participant: _Text
    prompt: _TextIfPresent = None
    response_template: _TextIfPresent = None
    output_template: _TextIfPresent = None
    template: _TextIfPresent = None

    @model_validator(mode="after")
    def _one_template(self) -> Self:
        _require_one(self.templates(), _AGENT_STEP_TEMPLATE_FIELDS, "template field")
        return self

    def templates(self) -> dict[str, str]:
        """Return the step's templates by field name."""
        return _present(
            prompt=self.prompt,
            response_template=self.response_template,
            output_template=self.output_template,
            template=self.template,
        )

    @property
    def body(self) -> str:
        """Return the template rendered into the participant prompt."""
        return next(iter(self.templates().values()))


class TransformStep(_SpecModel):
    """A step whose content is one rendered template."""

    id: _Id
    type: Literal["transform_step"]
    template: _TextIfPresent = None
    text: _TextIfPresent = None

    @model_validator(mode="after")
    def _one_template(self) -> Self:
        _require_one(self.templates(), ("template", "text"), "template field")
        return self

    def templates(self) -> dict[str, str]:
        """Return the step's templates by field name."""
        return _present(template=self.template, text=self.text)

    @property
    def body(self) -> str:
        """Return the template the step renders."""
        return next(iter(self.templates().values()))


class ReportStep(_SpecModel):
    """A report built from a rendered template or a prior step's content, with an optional title."""

    id: _Id
    type: Literal["report_step"]
    body_template: _TextIfPresent = None
    from_step: _TextIfPresent = None
    title: _TextIfPresent = None

    @model_validator(mode="after")
    def _one_source(self) -> Self:
        sources = _present(body_template=self.body_template, from_step=self.from_step)
        _require_one(sources, ("body_template", "from_step"), "report source field")
        return self

    def templates(self) -> dict[str, str]:
        """Return the step's templates by field name."""
        return _present(body_template=self.body_template, title=self.title)


def _step_type(value: object) -> object:
    if isinstance(value, dict):
        return cast("dict[str, object]", value).get("type", "agent_step")
    return value.type if isinstance(value, (TransformStep, ReportStep)) else "agent_step"


Step = Annotated[
    Annotated[AgentStep, Tag("agent_step")]
    | Annotated[TransformStep, Tag("transform_step")]
    | Annotated[ReportStep, Tag("report_step")],
    Discriminator(_step_type),
]


class Output(_SpecModel):
    """A named run output taken from one step's content."""

    id: _Id
    type: Literal["text", "markdown", "json", "html_report"]
    from_step: _Text


# --- permissions ---


def _no_data_grant(value: object, info: ValidationInfo) -> object:
    if value is not None and value != "none":
        msg = f"Workflow permission data.{info.field_name} must be 'none' until workflow data grants are supported."
        raise _rule(msg)
    return value


def _no_knowledge_bases(value: list[str]) -> list[str]:
    if value:
        msg = "Workflow permission data.knowledge_bases must be empty until workflow data grants are supported."
        raise _rule(msg)
    return value


class DataPermissions(_SpecModel):
    """Data grants; none are supported yet."""

    matrix_history: Annotated[Literal["none"] | None, BeforeValidator(_no_data_grant)] = None
    attachments: Annotated[Literal["none"] | None, BeforeValidator(_no_data_grant)] = None
    knowledge_bases: Annotated[list[str], AfterValidator(_no_knowledge_bases)] = Field(default_factory=list)


class Permissions(_SpecModel):
    """Run limits and the models and tools a workflow's participants may use."""

    max_runtime_seconds: Annotated[int, Field(ge=1, le=_MAX_WORKFLOW_RUNTIME_SECONDS)] | None = None
    # Steps run one at a time, so this cap is accepted but never reached.
    max_concurrent_agents: Annotated[int, Field(ge=1, le=8)] | None = None
    max_total_agents: Annotated[int, Field(ge=1, le=16), _null_as(lambda: 16)] = 16
    models: list[_Text] | None = None
    tools: Annotated[_Names, _null_as(list)] = Field(default_factory=list)
    data: Annotated[DataPermissions, _null_as(DataPermissions)] = Field(default_factory=DataPermissions)

    @property
    def runtime_seconds(self) -> int:
        """Return the run's runtime cap."""
        return _MAX_WORKFLOW_RUNTIME_SECONDS if self.max_runtime_seconds is None else self.max_runtime_seconds


# --- spec ---


def _schema_version(value: object) -> object:
    # Literal[1] alone accepts true and 1.0, which equal 1.
    if type(value) is not int or value != 1:
        msg = "Workflow spec field 'schema_version' must be 1."
        raise _rule(msg)
    return value


class WorkflowSpec(_SpecModel):
    """A declarative Dynamic Workflow: participants, ordered steps, outputs, and permissions."""

    schema_version: Annotated[Literal[1], BeforeValidator(_schema_version)]
    id: _Id
    name: _Text
    description: str | None = None
    kind: Literal["workflow"]
    inputs: InputSchema | None = None
    participants: Annotated[list[Participant], Field(min_length=1, max_length=8)]
    workflow: Annotated[list[Step], Field(min_length=1, max_length=64)]
    outputs: Annotated[list[Output], _null_as(list)] = Field(default_factory=list)
    permissions: Annotated[Permissions, _null_as(Permissions)] = Field(default_factory=Permissions)


def parse_workflow_spec(raw: object) -> WorkflowSpec:
    """Parse one declarative workflow spec; ``WorkflowSpecError`` lists every problem found.

    Rules that relate items to each other run once every item parses, and report together.
    """
    try:
        spec = WorkflowSpec.model_validate(raw)
    except ValidationError as exc:
        # Unsupported fields come first, so a misplaced key is the first thing an author reads.
        errors = sorted(exc.errors(include_url=False), key=lambda error: error["type"] != "extra_forbidden")
        raise WorkflowSpecError([_error_message(error) for error in errors]) from exc
    if reference_errors := list(_reference_errors(spec)):
        raise WorkflowSpecError(reference_errors)
    return spec


def stored_spec(spec: WorkflowSpec) -> dict[str, object]:
    """Return the spec as saved in a revision; a null field means the same as an absent one, so nulls are left out."""
    return spec.model_dump(mode="json", exclude_none=True)


def granted_tool_error(participant_id: str, tools: Iterable[str], granted: Collection[str]) -> str | None:
    """Name a participant tool that a non-empty permissions.tools grants neither by toolkit nor exactly."""
    if not granted:
        return None
    for tool_name in tools:
        if tool_name.partition(".")[0] not in granted and tool_name not in granted:
            return f"Participant '{participant_id}' tool '{tool_name}' is not granted by permissions.tools."
    return None


def validate_id(value: str, field_name: str) -> None:
    """Validate one Dynamic Workflow identifier against the shared ID pattern."""
    if not ID_RE.fullmatch(value):
        msg = f"{field_name} must match {ID_RE.pattern}."
        raise DynamicWorkflowError(msg)


def _reference_errors(spec: WorkflowSpec) -> Iterator[str]:
    """Yield every rule that relates items to each other, such as ids, step references, and tool grants."""
    participant_ids: set[str] = set()
    for participant in spec.participants:
        if participant.id in participant_ids:
            yield f"Duplicate participant id '{participant.id}'."
        participant_ids.add(participant.id)
    step_ids: set[str] = set()
    for index, step in enumerate(spec.workflow):
        if error := _step_reference_error(step, f"Workflow step at index {index}", participant_ids, step_ids):
            yield error
        step_ids.add(step.id)
    agent_steps = sum(isinstance(step, AgentStep) for step in spec.workflow)
    if agent_steps > spec.permissions.max_total_agents:
        yield f"Workflow agent steps cannot exceed permissions.max_total_agents ({spec.permissions.max_total_agents})."
    for participant in spec.participants:
        if isinstance(participant, SubagentParticipant) and (
            error := granted_tool_error(participant.id, participant.tools or (), spec.permissions.tools)
        ):
            yield error
    yield from _output_reference_errors(spec.outputs, step_ids)


def _output_reference_errors(outputs: Iterable[Output], step_ids: Collection[str]) -> Iterator[str]:
    output_ids: set[str] = set()
    for index, output in enumerate(outputs):
        if output.id in output_ids:
            yield f"Duplicate workflow output id '{output.id}'."
        output_ids.add(output.id)
        if output.from_step not in step_ids:
            yield f"Workflow output at index {index} references unknown step '{output.from_step}'."


def _step_reference_error(
    step: AgentStep | TransformStep | ReportStep,
    context: str,
    participant_ids: Collection[str],
    prior_step_ids: Collection[str],
) -> str | None:
    if step.id in prior_step_ids:
        return f"Duplicate workflow step id '{step.id}'."
    if isinstance(step, AgentStep) and step.participant not in participant_ids:
        return f"{context} references unknown participant '{step.participant}'."
    if isinstance(step, ReportStep) and step.from_step is not None and step.from_step not in prior_step_ids:
        return f"{context} references unknown prior step '{step.from_step}'."
    for field_name, template in step.templates().items():
        if error := _template_reference_error(template, prior_step_ids, f"{context} field '{field_name}'"):
            return error
    return None


def _template_reference_error(template: str, prior_step_ids: Collection[str], context: str) -> str | None:
    for match in TEMPLATE_REF_RE.finditer(template):
        reference = match.group(1)
        parts = reference.split(".")
        if reference.startswith("input."):
            if not all(parts[1:]):
                return f"{context} contains invalid template reference '{reference}'."
        elif reference.startswith("steps."):
            if not (len(parts) == 2 or (len(parts) == 3 and parts[2] == "content")):
                return f"{context} contains unsupported template reference '{reference}'."
            if parts[1] not in prior_step_ids:
                return f"{context} references unknown prior step '{parts[1]}'."
        else:
            return f"{context} contains unknown template reference '{reference}'."
    return None


def _error_message(error: ErrorDetails) -> str:
    """Render one validation error as a sentence naming the item and field it concerns."""
    if error["type"] == "workflow_rule":
        return error["msg"]
    label, path = _error_location(error["loc"])
    if error["type"] == "workflow_item_rule":
        return f"{label} {error['msg']}"
    if error["type"] == "extra_forbidden":
        return f"{label} contains unsupported field '{path}'."
    if error["type"] == "union_tag_invalid":
        ctx = error.get("ctx", {})
        field = "kind" if error["loc"][:1] == ("participants",) else "type"
        return f"{label} has unsupported {field} '{ctx.get('tag')}'; expected one of {ctx.get('expected_tags')}."
    if not path:
        return f"{label}: {error['msg']}."
    return f"{label} field '{path}': {error['msg']}."


def _error_location(loc: tuple[int | str, ...]) -> tuple[str, str]:
    """Return the label of the item an error location points into, and the field path inside that item."""
    match loc:
        case ("participants", int() as index, *rest):
            label = f"Participant at index {index}"
        case ("workflow", int() as index, *rest):
            label = f"Workflow step at index {index}"
        case ("outputs", int() as index, *rest):
            label = f"Workflow output at index {index}"
        case ("inputs", "properties", str() as name, *rest):
            label = f"Workflow input schema property '{name}'"
        case ("inputs", _, *_):
            label, rest = "Workflow input schema", list(loc[1:])
        case ("permissions", _, *_):
            label, rest = "Workflow permission", list(loc[1:])
        case _:
            label, rest = "Workflow spec", list(loc)
    # Errors inside a participant or step name its variant tag first.
    if rest and rest[0] in _UNION_TAGS:
        rest = rest[1:]
    return label, ".".join(str(part) for part in rest)
