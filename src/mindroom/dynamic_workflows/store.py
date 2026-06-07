"""Disk-backed Dynamic Workflow store."""

from __future__ import annotations

import copy
import hashlib
import html
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast
from uuid import uuid4

import yaml

from mindroom.dynamic_workflows.runner import execute_workflow_spec

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,127}$")
_SCOPES = frozenset({"agent", "room", "tenant"})


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
    ) -> DynamicWorkflowSummary:
        """Create a workflow with revision 000001."""
        validated_spec = validate_workflow_spec(spec)
        workflow_id = str(validated_spec["id"])
        workflow_dir = self._workflow_dir(scope, owner_id, workflow_id)
        if workflow_dir.exists():
            msg = f"Dynamic Workflow '{workflow_id}' already exists in {scope} scope."
            raise DynamicWorkflowError(msg)

        now = _utc_now()
        revision = "000001"
        revision_spec = _revision_spec(validated_spec, revision=revision, reason=reason, updated_by=created_by, now=now)
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
    ) -> DynamicWorkflowSummary:
        """Create and publish a new revision by applying a recursive patch."""
        summary = self.get_workflow(workflow_id=workflow_id, scope=scope, owner_id=owner_id)
        workflow_dir = self._workflow_dir(scope, owner_id, workflow_id)
        current_spec = self._load_revision(workflow_dir, summary.active_revision)
        next_revision = self._next_revision(workflow_dir)
        patched_spec = _recursive_merge(current_spec, patch)
        revision_spec = _revision_spec(
            validate_workflow_spec(patched_spec),
            revision=next_revision,
            reason=reason,
            updated_by=updated_by,
            now=_utc_now(),
        )
        now = _utc_now()
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

    def run_workflow(
        self,
        *,
        workflow_id: str,
        scope: str,
        owner_id: str,
        input_data: dict[str, object],
        requested_by: str,
        base_url: str | None = None,
    ) -> DynamicWorkflowRun:
        """Execute the active revision and persist run artifacts."""
        summary = self.get_workflow(workflow_id=workflow_id, scope=scope, owner_id=owner_id)
        workflow_dir = self._workflow_dir(scope, owner_id, workflow_id)
        spec = self._load_revision(workflow_dir, summary.active_revision)
        run_id = f"run_{uuid4().hex}"
        started_at = _utc_now()
        artifact_dir = workflow_dir / "artifacts" / run_id
        execution = execute_workflow_spec(spec, input_data)
        report_markdown = execution.report_markdown
        report_html = _render_report_html(title=summary.name, markdown=report_markdown)
        report_path = artifact_dir / "report.html"
        report_markdown_path = artifact_dir / "report.md"
        step_outputs_path = artifact_dir / "step_outputs.json"
        _atomic_write_text(report_path, report_html)
        artifacts = {
            "report_markdown": _relative_artifact_path(report_markdown_path, self._storage_root),
            "report_html": _relative_artifact_path(report_path, self._storage_root),
            "step_outputs": _relative_artifact_path(step_outputs_path, self._storage_root),
        }
        _atomic_write_text(report_markdown_path, report_markdown)
        _atomic_write_json(step_outputs_path, execution.step_outputs_json())
        report_url = _private_report_url(base_url, run_id)
        run = DynamicWorkflowRun(
            run_id=run_id,
            workflow_id=workflow_id,
            scope=scope,
            owner_id=owner_id,
            revision=summary.active_revision,
            status=execution.status,
            input_data=dict(input_data),
            steps=[step.to_json() for step in execution.steps],
            outputs=execution.outputs,
            artifacts=artifacts,
            report_url=report_url,
            requested_by=requested_by,
            started_at=started_at,
            completed_at=_utc_now(),
            error=execution.error,
        )
        _atomic_write_json(workflow_dir / "runs" / f"{run_id}.json", _run_to_json(run))
        return run

    def get_workflow_run(
        self,
        *,
        workflow_id: str,
        scope: str,
        owner_id: str,
        run_id: str,
    ) -> DynamicWorkflowRun:
        """Load one workflow run."""
        run_path = self._workflow_dir(scope, owner_id, workflow_id) / "runs" / f"{run_id}.json"
        return _run_from_json(_load_json_mapping(run_path))

    def find_private_report_html(self, run_id: str) -> Path:
        """Find the private HTML report for one run ID."""
        _validate_id(run_id, "run_id")
        matches = list(self._root.glob(f"*/*/*/artifacts/{run_id}/report.html"))
        if len(matches) != 1:
            msg = f"Private report for run '{run_id}' was not found."
            raise DynamicWorkflowError(msg)
        return matches[0]

    def _load_revision(self, workflow_dir: Path, revision: str) -> dict[str, object]:
        return _load_yaml_mapping(workflow_dir / "revisions" / f"{revision}.yaml")

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


def validate_workflow_spec(spec: dict[str, object]) -> dict[str, object]:
    """Validate and normalize a declarative Dynamic Workflow spec."""
    if not isinstance(spec, dict):
        msg = "Workflow spec must be a mapping."
        raise DynamicWorkflowError(msg)
    normalized = copy.deepcopy(spec)
    workflow_id = _required_text(normalized, "id")
    _validate_id(workflow_id, "id")
    _required_text(normalized, "name")
    kind = _required_text(normalized, "kind")
    if kind != "workflow":
        msg = "Workflow spec kind must be 'workflow'."
        raise DynamicWorkflowError(msg)
    if not isinstance(normalized.get("workflow"), list) or not normalized["workflow"]:
        msg = "Workflow spec must include at least one workflow step."
        raise DynamicWorkflowError(msg)
    if not isinstance(normalized.get("participants"), list) or not normalized["participants"]:
        msg = "Workflow spec must include at least one participant."
        raise DynamicWorkflowError(msg)
    return normalized


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
        started_at=str(data["started_at"]),
        completed_at=str(data["completed_at"]) if data.get("completed_at") is not None else None,
        error=str(data["error"]) if data.get("error") is not None else None,
    )


def _required_text(data: dict[str, object], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        msg = f"Workflow spec field '{key}' must be a non-empty string."
        raise DynamicWorkflowError(msg)
    return value.strip()


def _validate_scope(scope: str) -> None:
    if scope not in _SCOPES:
        msg = f"Unsupported Dynamic Workflow scope '{scope}'."
        raise DynamicWorkflowError(msg)


def _validate_id(value: str, field_name: str) -> None:
    if not _ID_RE.fullmatch(value):
        msg = f"{field_name} must match {_ID_RE.pattern}."
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


def _private_report_url(base_url: str | None, run_id: str) -> str | None:
    if base_url is None or not base_url.strip():
        return None
    return f"{base_url.rstrip('/')}/reports/private/{run_id}"


def _object_mapping(data: Mapping[object, object]) -> dict[str, object]:
    return {str(key): value for key, value in data.items()}


def _relative_artifact_path(artifact_path: Path, storage_root: Path) -> str:
    return artifact_path.relative_to(storage_root).as_posix()


def _load_yaml_mapping(path: Path) -> dict[str, object]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        msg = f"YAML mapping was not found at {path}."
        raise DynamicWorkflowError(msg) from exc
    if not isinstance(data, dict):
        msg = f"Expected YAML mapping at {path}."
        raise DynamicWorkflowError(msg)
    return data


def _load_json_mapping(path: Path) -> dict[str, object]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        msg = f"JSON mapping was not found at {path}."
        raise DynamicWorkflowError(msg) from exc
    if not isinstance(data, dict):
        msg = f"Expected JSON mapping at {path}."
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
