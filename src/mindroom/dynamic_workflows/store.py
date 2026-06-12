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
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

import yaml

from mindroom.dynamic_workflows.validation import (
    ID_RE,
    DynamicWorkflowError,
    object_mapping,
    validate_id,
    validate_workflow_spec,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping

_REVISION_RE = re.compile(r"^[0-9]{6}$")
_SCOPES = frozenset({"agent", "room", "tenant"})
_REVISION_METADATA_KEYS = frozenset({"revision", "revision_reason", "updated_by", "updated_at"})


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
        validate_id(run_id, "run_id")
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
        validate_id(owner_key, "owner_key")
        validate_id(workflow_id, "workflow_id")
        validate_id(run_id, "run_id")
        report_path = self._root / scope / owner_key / workflow_id / "artifacts" / run_id / "report.html"
        if not report_path.is_file():
            msg = f"Private report for run '{run_id}' was not found."
            raise DynamicWorkflowError(msg)
        return report_path

    def run_report_html_artifact_path(self, run: DynamicWorkflowRun) -> Path:
        """Return the HTML report artifact path for one persisted run."""
        report_artifact = run.artifacts.get("report_html")
        if report_artifact is None:
            msg = f"Run '{run.run_id}' does not have an HTML report artifact."
            raise DynamicWorkflowError(msg)
        report_path = self._artifact_path_from_relative(report_artifact)
        if not report_path.is_file():
            msg = f"HTML report artifact for run '{run.run_id}' was not found."
            raise DynamicWorkflowError(msg)
        return report_path

    def run_report_title(self, run: DynamicWorkflowRun) -> str:
        """Return the report title for one persisted run."""
        return self._run_report_title(self._workflow_dir(run.scope, run.owner_id, run.workflow_id), run)

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
        validate_id(workflow_id, "workflow_id")
        return self._scope_dir(scope, owner_id) / workflow_id

    def _artifact_path_from_relative(self, artifact_path: str) -> Path:
        relative_path = Path(artifact_path)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            msg = "Dynamic Workflow artifact path is invalid."
            raise DynamicWorkflowError(msg)
        return self._storage_root / relative_path

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
                object_mapping(cast("Mapping[object, object]", base_value)),
                object_mapping(cast("Mapping[object, object]", value)),
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
        input_data=object_mapping(cast("Mapping[object, object]", input_data)) if isinstance(input_data, dict) else {},
        steps=[object_mapping(cast("Mapping[object, object]", step)) for step in steps if isinstance(step, dict)]
        if isinstance(steps, list)
        else [],
        outputs=object_mapping(cast("Mapping[object, object]", outputs)) if isinstance(outputs, dict) else {},
        artifacts={str(key): str(value) for key, value in artifacts.items()} if isinstance(artifacts, dict) else {},
        report_url=str(data["report_url"]) if data.get("report_url") is not None else None,
        requested_by=str(data["requested_by"]),
        started_at=str(data["started_at"]),
        completed_at=str(data["completed_at"]) if data.get("completed_at") is not None else None,
        error=str(data["error"]) if data.get("error") is not None else None,
    )


def _validate_scope(scope: str) -> None:
    if scope not in _SCOPES:
        msg = f"Unsupported Dynamic Workflow scope '{scope}'."
        raise DynamicWorkflowError(msg)


def _validate_revision(value: str) -> None:
    if not _REVISION_RE.fullmatch(value):
        msg = f"revision must match {_REVISION_RE.pattern}."
        raise DynamicWorkflowError(msg)


def _owner_dir_name(scope: str, owner_id: str) -> str:
    if scope == "tenant":
        return "tenant"
    if ID_RE.fullmatch(owner_id):
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
