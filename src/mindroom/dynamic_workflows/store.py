"""Disk-backed Dynamic Workflow store."""

from __future__ import annotations

import copy
import fcntl
import hashlib
import html
import json
import re
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

import yaml

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping
    from pathlib import Path

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,127}$")
_REVISION_RE = re.compile(r"^[0-9]{6}$")
_SUPPORTED_SCHEMA_VERSION = 1
_STEP_TYPES = frozenset({"agent_step", "report_step", "transform_step"})
_SCOPES = frozenset({"agent", "room", "tenant"})
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
_REVISION_METADATA_KEYS = frozenset({"revision", "revision_reason", "updated_by", "updated_at"})
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


@dataclass(frozen=True)
class DynamicWorkflowSummary:
    """User-facing summary for one saved Dynamic Workflow."""

    workflow_id: str
    scope: str
    owner_id: str
    active_revision: str
    name: str
    description: str
    created_by: str
    created_at: str
    updated_at: str
    archived: bool = False


@dataclass(frozen=True)
class DynamicWorkflowRun:
    """Persistent record for one Dynamic Workflow run."""

    run_id: str
    workflow_id: str
    scope: str
    owner_id: str
    revision: str
    status: str
    input_data: dict[str, object]
    steps: list[dict[str, object]]
    outputs: dict[str, object]
    artifacts: dict[str, str]
    report_url: str | None
    requested_by: str
    report_access_token: str | None
    started_at: str
    completed_at: str | None
    error: str | None = None


class DynamicWorkflowStore:
    """Persist Dynamic Workflow specs, revisions, runs, and artifacts under one storage root."""

    def __init__(self, storage_root: Path) -> None:
        self._storage_root = storage_root
        self._root = storage_root / "dynamic_workflows"

    def create_workflow(
        self,
        *,
        spec: dict[str, object],
        scope: str,
        owner_id: str,
        created_by: str,
        reason: str | None = None,
        spec_validator: Callable[[dict[str, object]], None] | None = None,
    ) -> DynamicWorkflowSummary:
        """Create a workflow with revision 000001."""
        validated_spec = validate_workflow_spec(spec)
        if spec_validator is not None:
            spec_validator(validated_spec)
        workflow_id = str(validated_spec["id"])
        workflow_dir = self._workflow_dir(scope, owner_id, workflow_id)
        with _workflow_lock(workflow_dir):
            if workflow_dir.exists():
                msg = f"Dynamic Workflow '{workflow_id}' already exists in {scope} scope."
                raise DynamicWorkflowError(msg)

            now = _utc_now()
            revision = "000001"
            revision_spec = _revision_spec(
                validated_spec,
                revision=revision,
                reason=reason,
                updated_by=created_by,
                now=now,
            )
            summary = DynamicWorkflowSummary(
                workflow_id=workflow_id,
                scope=scope,
                owner_id=owner_id,
                active_revision=revision,
                name=str(validated_spec["name"]),
                description=str(validated_spec.get("description", "")),
                created_by=created_by,
                created_at=now,
                updated_at=now,
            )
            _atomic_write_yaml(workflow_dir / "revisions" / f"{revision}.yaml", revision_spec)
            _atomic_write_yaml(workflow_dir / "workflow.yaml", _summary_to_yaml(summary))
        return summary

    def validate_workflow(self, spec: dict[str, object]) -> dict[str, object]:
        """Validate one workflow spec and return normalized data."""
        return validate_workflow_spec(spec)

    def update_workflow(
        self,
        *,
        workflow_id: str,
        scope: str,
        owner_id: str,
        patch: dict[str, object],
        updated_by: str,
        reason: str,
        spec_validator: Callable[[dict[str, object]], None] | None = None,
    ) -> DynamicWorkflowSummary:
        """Create and publish a new revision by applying a recursive patch."""
        workflow_dir = self._workflow_dir(scope, owner_id, workflow_id)
        with _workflow_lock(workflow_dir):
            summary = self.get_workflow(workflow_id=workflow_id, scope=scope, owner_id=owner_id)
            current_spec = _workflow_spec_payload(self._load_revision(workflow_dir, summary.active_revision))
            patched_spec = _recursive_merge(current_spec, patch)
            validated_patched_spec = validate_workflow_spec(patched_spec)
            if spec_validator is not None:
                spec_validator(validated_patched_spec)
            if str(validated_patched_spec["id"]) != workflow_id:
                msg = "Workflow ID is immutable and cannot be changed by update_workflow."
                raise DynamicWorkflowError(msg)

            next_revision = self._next_revision(workflow_dir)
            now = _utc_now()
            revision_spec = _revision_spec(
                validated_patched_spec,
                revision=next_revision,
                reason=reason,
                updated_by=updated_by,
                now=now,
            )
            updated_summary = DynamicWorkflowSummary(
                workflow_id=workflow_id,
                scope=scope,
                owner_id=owner_id,
                active_revision=next_revision,
                name=str(revision_spec["name"]),
                description=str(revision_spec.get("description", "")),
                created_by=summary.created_by,
                created_at=summary.created_at,
                updated_at=now,
                archived=summary.archived,
            )
            _atomic_write_yaml(workflow_dir / "revisions" / f"{next_revision}.yaml", revision_spec)
            _atomic_write_yaml(workflow_dir / "workflow.yaml", _summary_to_yaml(updated_summary))
        return updated_summary

    def get_workflow(self, *, workflow_id: str, scope: str, owner_id: str) -> DynamicWorkflowSummary:
        """Load one workflow summary."""
        workflow_dir = self._workflow_dir(scope, owner_id, workflow_id)
        data = _load_yaml_mapping(workflow_dir / "workflow.yaml")
        return _summary_from_yaml(data)

    def list_workflows(self, *, scope: str, owner_id: str) -> list[DynamicWorkflowSummary]:
        """List workflows for one scope owner."""
        scope_dir = self._scope_dir(scope, owner_id)
        if not scope_dir.exists():
            return []
        summaries = [
            _summary_from_yaml(_load_yaml_mapping(workflow_dir / "workflow.yaml"))
            for workflow_dir in sorted(scope_dir.iterdir())
            if (workflow_dir / "workflow.yaml").is_file()
        ]
        return [summary for summary in summaries if not summary.archived]

    def list_workflow_revisions(self, *, workflow_id: str, scope: str, owner_id: str) -> list[str]:
        """List immutable revision IDs for one workflow."""
        revisions_dir = self._workflow_dir(scope, owner_id, workflow_id) / "revisions"
        if not revisions_dir.exists():
            return []
        return sorted(path.stem for path in revisions_dir.glob("*.yaml"))

    def load_workflow_revision(
        self,
        *,
        workflow_id: str,
        scope: str,
        owner_id: str,
        revision: str,
    ) -> dict[str, object]:
        """Load one immutable workflow revision."""
        _validate_revision(revision)
        workflow_dir = self._workflow_dir(scope, owner_id, workflow_id)
        return _workflow_spec_payload(self._load_revision(workflow_dir, revision))

    def start_workflow_run(
        self,
        *,
        workflow_id: str,
        scope: str,
        owner_id: str,
        input_data: dict[str, object],
        requested_by: str,
        base_url: str | None = None,
    ) -> DynamicWorkflowRun:
        """Persist a running workflow run record before execution starts."""
        summary = self.get_workflow(workflow_id=workflow_id, scope=scope, owner_id=owner_id)
        _validate_revision(summary.active_revision)
        run_id = f"run_{uuid4().hex}"
        run = DynamicWorkflowRun(
            run_id=run_id,
            workflow_id=workflow_id,
            scope=scope,
            owner_id=owner_id,
            revision=summary.active_revision,
            status="running",
            input_data=dict(input_data),
            steps=[],
            outputs={},
            artifacts={},
            report_url=_private_report_url(base_url, scope, owner_id, workflow_id, run_id),
            requested_by=requested_by,
            report_access_token=None,
            started_at=_utc_now(),
            completed_at=None,
            error=None,
        )
        self._write_run(run)
        return run

    def complete_workflow_run(self, run: DynamicWorkflowRun, execution: Any) -> DynamicWorkflowRun:  # noqa: ANN401
        """Persist completed or execution-failed workflow outputs and artifacts."""
        workflow_dir = self._workflow_dir(run.scope, run.owner_id, run.workflow_id)
        title = self._run_report_title(workflow_dir, run)
        artifacts = self._write_run_artifacts(
            run,
            title=title,
            report_markdown=str(execution.report_markdown),
            step_outputs=execution.step_outputs_json(),
        )
        completed = DynamicWorkflowRun(
            run_id=run.run_id,
            workflow_id=run.workflow_id,
            scope=run.scope,
            owner_id=run.owner_id,
            revision=run.revision,
            status=str(execution.status),
            input_data=dict(run.input_data),
            steps=[step.to_json() for step in execution.steps],
            outputs=dict(execution.outputs),
            artifacts=artifacts,
            report_url=run.report_url,
            requested_by=run.requested_by,
            report_access_token=run.report_access_token,
            started_at=run.started_at,
            completed_at=_utc_now(),
            error=str(execution.error) if execution.error is not None else None,
        )
        self._write_run(completed)
        return completed

    def fail_workflow_run(self, run: DynamicWorkflowRun, *, error: str) -> DynamicWorkflowRun:
        """Persist a failed workflow run without executing steps."""
        workflow_dir = self._workflow_dir(run.scope, run.owner_id, run.workflow_id)
        title = self._run_report_title(workflow_dir, run)
        report_markdown = _failed_input_report_markdown(title, run.input_data, error)
        artifacts = self._write_run_artifacts(
            run,
            title=title,
            report_markdown=report_markdown,
            step_outputs={},
        )
        failed = DynamicWorkflowRun(
            run_id=run.run_id,
            workflow_id=run.workflow_id,
            scope=run.scope,
            owner_id=run.owner_id,
            revision=run.revision,
            status="failed",
            input_data=dict(run.input_data),
            steps=[],
            outputs={},
            artifacts=artifacts,
            report_url=run.report_url,
            requested_by=run.requested_by,
            report_access_token=run.report_access_token,
            started_at=run.started_at,
            completed_at=_utc_now(),
            error=error,
        )
        self._write_run(failed)
        return failed

    def get_workflow_run(
        self,
        *,
        workflow_id: str,
        scope: str,
        owner_id: str,
        run_id: str,
    ) -> DynamicWorkflowRun:
        """Load one workflow run."""
        _validate_id(run_id, "run_id")
        run_path = self._workflow_dir(scope, owner_id, workflow_id) / "runs" / f"{run_id}.json"
        return _run_from_json(_load_json_mapping(run_path))

    def private_report_html_path(
        self,
        *,
        scope: str,
        owner_key: str,
        workflow_id: str,
        run_id: str,
    ) -> Path:
        """Return the private HTML report path for one scoped run."""
        _validate_scope(scope)
        _validate_id(owner_key, "owner_key")
        _validate_id(workflow_id, "workflow_id")
        _validate_id(run_id, "run_id")
        report_path = self._root / scope / owner_key / workflow_id / "artifacts" / run_id / "report.html"
        if not report_path.is_file():
            msg = f"Private report for run '{run_id}' was not found."
            raise DynamicWorkflowError(msg)
        return report_path

    def _load_revision(self, workflow_dir: Path, revision: str) -> dict[str, object]:
        _validate_revision(revision)
        return _load_yaml_mapping(workflow_dir / "revisions" / f"{revision}.yaml")

    def _run_report_title(self, workflow_dir: Path, run: DynamicWorkflowRun) -> str:
        try:
            return str(self._load_revision(workflow_dir, run.revision)["name"])
        except DynamicWorkflowError:
            return run.workflow_id

    def _next_revision(self, workflow_dir: Path) -> str:
        revision_numbers = [
            int(path.stem) for path in (workflow_dir / "revisions").glob("*.yaml") if path.stem.isdecimal()
        ]
        return f"{(max(revision_numbers) if revision_numbers else 0) + 1:06d}"

    def _scope_dir(self, scope: str, owner_id: str) -> Path:
        _validate_scope(scope)
        owner_dir = _owner_dir_name(scope, owner_id)
        return self._root / scope / owner_dir

    def _workflow_dir(self, scope: str, owner_id: str, workflow_id: str) -> Path:
        _validate_id(workflow_id, "workflow_id")
        return self._scope_dir(scope, owner_id) / workflow_id

    def _write_run(self, run: DynamicWorkflowRun) -> None:
        run_path = self._workflow_dir(run.scope, run.owner_id, run.workflow_id) / "runs" / f"{run.run_id}.json"
        _atomic_write_json(run_path, _run_to_json(run))

    def _write_run_artifacts(
        self,
        run: DynamicWorkflowRun,
        *,
        title: str,
        report_markdown: str,
        step_outputs: dict[str, object],
    ) -> dict[str, str]:
        artifact_dir = self._workflow_dir(run.scope, run.owner_id, run.workflow_id) / "artifacts" / run.run_id
        report_path = artifact_dir / "report.html"
        report_markdown_path = artifact_dir / "report.md"
        step_outputs_path = artifact_dir / "step_outputs.json"
        _atomic_write_text(report_path, _render_report_html(title=title, markdown=report_markdown))
        _atomic_write_text(report_markdown_path, report_markdown)
        _atomic_write_json(step_outputs_path, step_outputs)
        return {
            "report_markdown": _relative_artifact_path(report_markdown_path, self._storage_root),
            "report_html": _relative_artifact_path(report_path, self._storage_root),
            "step_outputs": _relative_artifact_path(step_outputs_path, self._storage_root),
        }


def validate_workflow_spec(spec: dict[str, object]) -> dict[str, object]:
    """Validate and normalize a declarative Dynamic Workflow spec."""
    if not isinstance(spec, dict):
        msg = "Workflow spec must be a mapping."
        raise DynamicWorkflowError(msg)
    normalized = copy.deepcopy(spec)
    _reject_unsupported_fields(normalized, _SPEC_KEYS, "Workflow spec")
    _validate_schema_version(normalized)
    workflow_id = _required_text(normalized, "id")
    _validate_id(workflow_id, "id")
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
    if value != _SUPPORTED_SCHEMA_VERSION:
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
    inputs = _object_mapping(cast("Mapping[object, object]", raw_inputs))
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
    return _object_mapping(cast("Mapping[object, object]", raw_properties))


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
        field_schema = _object_mapping(cast("Mapping[object, object]", raw_field_schema))
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
        field_schema = _object_mapping(cast("Mapping[object, object]", raw_field_schema))
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
        _validate_id(participant_id, f"{context} id")
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
        msg = f"{context} room_agent participants cannot declare tools; workflow tool grants are not supported yet."
        raise DynamicWorkflowError(msg)


def _validate_ephemeral_agent_participant(participant: dict[str, object], context: str) -> None:
    _reject_unsupported_fields(participant, _EPHEMERAL_PARTICIPANT_KEYS, context)
    if participant.get("tools") not in (None, []):
        msg = f"{context} ephemeral_agent participants cannot use tools yet."
        raise DynamicWorkflowError(msg)
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
        _validate_id(step_id, f"{context} id")
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
    permissions = _object_mapping(cast("Mapping[object, object]", raw_permissions))
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
    tools = permissions.get("tools", [])
    if not isinstance(tools, list) or not all(isinstance(tool, str) and tool.strip() for tool in tools):
        msg = "Workflow permission 'tools' must be a list of non-empty strings."
        raise DynamicWorkflowError(msg)
    if tools:
        msg = "Workflow permission 'tools' must be empty until Dynamic Workflow tool grants are supported."
        raise DynamicWorkflowError(msg)
    permissions["tools"] = []


def _validate_permission_data(permissions: dict[str, object]) -> None:
    data = permissions.get("data", {})
    if data is None:
        permissions["data"] = {}
        return
    if not isinstance(data, dict):
        msg = "Workflow permission 'data' must be a mapping."
        raise DynamicWorkflowError(msg)
    normalized = _object_mapping(cast("Mapping[object, object]", data))
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
        output = _object_mapping(cast("Mapping[object, object]", raw_output))
        output_id = _required_text(output, "id", context=context)
        _validate_id(output_id, f"{context} id")
        if output_id in output_ids:
            msg = f"Duplicate workflow output id '{output_id}'."
            raise DynamicWorkflowError(msg)
        output["id"] = output_id
        output_ids.add(output_id)
        if "type" in output:
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
        items.append(_object_mapping(cast("Mapping[object, object]", raw_item)))
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


def _render_report_html(*, title: str, markdown: str) -> str:
    """Render a small self-contained report HTML page."""
    escaped_title = html.escape(title)
    escaped_body = html.escape(markdown).replace("\n", "<br>\n")
    return (
        "<!doctype html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{escaped_title}</title>\n"
        "<style>\n"
        "body{font-family:system-ui,-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;"
        "max-width:840px;margin:0 auto;padding:32px;line-height:1.55;color:#202124;}\n"
        "main{border-top:1px solid #d8dee4;padding-top:24px;}\n"
        "pre{white-space:pre-wrap;background:#f6f8fa;padding:16px;border-radius:6px;}\n"
        "</style>\n"
        "</head>\n"
        "<body>\n"
        f"<h1>{escaped_title}</h1>\n"
        "<main>\n"
        f"<pre>{escaped_body}</pre>\n"
        "</main>\n"
        "</body>\n"
        "</html>\n"
    )


def _failed_input_report_markdown(title: str, input_data: dict[str, object], error: str) -> str:
    input_json = json.dumps(input_data, indent=2, sort_keys=True)
    return (
        f"# {title}\n\n"
        "Dynamic Workflow run failed before step execution.\n\n"
        f"## Error\n\n{error}\n\n"
        f"## Input\n\n```json\n{input_json}\n```\n"
    )


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


def _revision_spec(
    spec: dict[str, object],
    *,
    revision: str,
    reason: str | None,
    updated_by: str,
    now: str,
) -> dict[str, object]:
    revision_spec = copy.deepcopy(spec)
    revision_spec["revision"] = revision
    revision_spec["revision_reason"] = reason
    revision_spec["updated_by"] = updated_by
    revision_spec["updated_at"] = now
    return revision_spec


def _workflow_spec_payload(revision_spec: dict[str, object]) -> dict[str, object]:
    return {key: value for key, value in revision_spec.items() if key not in _REVISION_METADATA_KEYS}


def _recursive_merge(base: dict[str, object], patch: dict[str, object]) -> dict[str, object]:
    merged = copy.deepcopy(base)
    for key, value in patch.items():
        base_value = merged.get(key)
        if isinstance(value, dict) and isinstance(base_value, dict):
            merged[key] = _recursive_merge(
                _object_mapping(cast("Mapping[object, object]", base_value)),
                _object_mapping(cast("Mapping[object, object]", value)),
            )
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _summary_to_yaml(summary: DynamicWorkflowSummary) -> dict[str, object]:
    return {
        "id": summary.workflow_id,
        "scope": summary.scope,
        "owner_id": summary.owner_id,
        "active_revision": summary.active_revision,
        "name": summary.name,
        "description": summary.description,
        "created_by": summary.created_by,
        "created_at": summary.created_at,
        "updated_at": summary.updated_at,
        "archived": summary.archived,
    }


def _summary_from_yaml(data: dict[str, object]) -> DynamicWorkflowSummary:
    return DynamicWorkflowSummary(
        workflow_id=str(data["id"]),
        scope=str(data["scope"]),
        owner_id=str(data["owner_id"]),
        active_revision=str(data["active_revision"]),
        name=str(data["name"]),
        description=str(data.get("description", "")),
        created_by=str(data["created_by"]),
        created_at=str(data["created_at"]),
        updated_at=str(data["updated_at"]),
        archived=bool(data.get("archived", False)),
    )


def _run_to_json(run: DynamicWorkflowRun) -> dict[str, object]:
    return {
        "run_id": run.run_id,
        "workflow_id": run.workflow_id,
        "scope": run.scope,
        "owner_id": run.owner_id,
        "revision": run.revision,
        "status": run.status,
        "input_data": run.input_data,
        "steps": run.steps,
        "outputs": run.outputs,
        "artifacts": run.artifacts,
        "report_url": run.report_url,
        "requested_by": run.requested_by,
        "report_access_token": run.report_access_token,
        "started_at": run.started_at,
        "completed_at": run.completed_at,
        "error": run.error,
    }


def _run_from_json(data: dict[str, object]) -> DynamicWorkflowRun:
    artifacts = data.get("artifacts", {})
    input_data = data.get("input_data", {})
    steps = data.get("steps", [])
    outputs = data.get("outputs", {})
    return DynamicWorkflowRun(
        run_id=str(data["run_id"]),
        workflow_id=str(data["workflow_id"]),
        scope=str(data["scope"]),
        owner_id=str(data["owner_id"]),
        revision=str(data["revision"]),
        status=str(data["status"]),
        input_data=_object_mapping(cast("Mapping[object, object]", input_data)) if isinstance(input_data, dict) else {},
        steps=[_object_mapping(cast("Mapping[object, object]", step)) for step in steps if isinstance(step, dict)]
        if isinstance(steps, list)
        else [],
        outputs=_object_mapping(cast("Mapping[object, object]", outputs)) if isinstance(outputs, dict) else {},
        artifacts={str(key): str(value) for key, value in artifacts.items()} if isinstance(artifacts, dict) else {},
        report_url=str(data["report_url"]) if data.get("report_url") is not None else None,
        requested_by=str(data["requested_by"]),
        report_access_token=str(data["report_access_token"]) if data.get("report_access_token") is not None else None,
        started_at=str(data["started_at"]),
        completed_at=str(data["completed_at"]) if data.get("completed_at") is not None else None,
        error=str(data["error"]) if data.get("error") is not None else None,
    )


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


def _validate_scope(scope: str) -> None:
    if scope not in _SCOPES:
        msg = f"Unsupported Dynamic Workflow scope '{scope}'."
        raise DynamicWorkflowError(msg)


def _validate_id(value: str, field_name: str) -> None:
    if not _ID_RE.fullmatch(value):
        msg = f"{field_name} must match {_ID_RE.pattern}."
        raise DynamicWorkflowError(msg)


def _validate_revision(value: str) -> None:
    if not _REVISION_RE.fullmatch(value):
        msg = f"revision must match {_REVISION_RE.pattern}."
        raise DynamicWorkflowError(msg)


def _owner_dir_name(scope: str, owner_id: str) -> str:
    if scope == "tenant":
        return "tenant"
    if _ID_RE.fullmatch(owner_id):
        return owner_id
    digest = hashlib.sha256(owner_id.encode("utf-8")).hexdigest()[:24]
    return f"hash_{digest}"


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _private_report_url(
    base_url: str | None,
    scope: str,
    owner_id: str,
    workflow_id: str,
    run_id: str,
) -> str | None:
    if base_url is None or not base_url.strip():
        return None
    owner_key = _owner_dir_name(scope, owner_id)
    return f"{base_url.rstrip('/')}/reports/private/{scope}/{owner_key}/{workflow_id}/{run_id}"


def _object_mapping(data: Mapping[object, object]) -> dict[str, object]:
    return {str(key): value for key, value in data.items()}


def _relative_artifact_path(artifact_path: Path, storage_root: Path) -> str:
    return artifact_path.relative_to(storage_root).as_posix()


def _load_yaml_mapping(path: Path) -> dict[str, object]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        msg = "YAML mapping was not found."
        raise DynamicWorkflowError(msg) from exc
    except yaml.YAMLError as exc:
        msg = f"Failed to parse YAML mapping: {exc}"
        raise DynamicWorkflowError(msg) from exc
    if not isinstance(data, dict):
        msg = "Expected YAML mapping."
        raise DynamicWorkflowError(msg)
    return data


def _load_json_mapping(path: Path) -> dict[str, object]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        msg = "JSON mapping was not found."
        raise DynamicWorkflowError(msg) from exc
    except json.JSONDecodeError as exc:
        msg = f"Failed to parse JSON mapping: {exc}"
        raise DynamicWorkflowError(msg) from exc
    if not isinstance(data, dict):
        msg = "Expected JSON mapping."
        raise DynamicWorkflowError(msg)
    return data


def _atomic_write_yaml(path: Path, data: dict[str, object]) -> None:
    _atomic_write_text(path, yaml.safe_dump(data, sort_keys=False, allow_unicode=True))


def _atomic_write_json(path: Path, data: dict[str, object]) -> None:
    _atomic_write_text(path, json.dumps(data, indent=2, sort_keys=True) + "\n")


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    tmp_path.write_text(text, encoding="utf-8")
    tmp_path.replace(path)


@contextmanager
def _workflow_lock(workflow_dir: Path) -> Iterator[None]:
    lock_path = workflow_dir.with_name(f".{workflow_dir.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
