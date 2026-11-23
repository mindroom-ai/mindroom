"""Saved workflows in `<workspace>/workflows/<name>.py`: confined reads/writes and AST-only metadata."""

from __future__ import annotations

import ast
import os
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from mindroom.atomic_file import atomic_write_file_at
from mindroom.path_confinement import open_directory_within_root, read_regular_file_within_root
from mindroom.script_runs.launch_contract import MAX_SOURCE_BYTES

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from mindroom.workflow_runs.models import WorkflowRun

WORKFLOWS_DIR = "workflows"
SAVED_WORKFLOW_NAME_RE = re.compile(r"\A[a-z0-9][a-z0-9_-]{0,63}\Z")
_ENCODING_COOKIE_RE = re.compile(r"^[ \t\f]*#.*?coding[:=][ \t]*([-_.a-zA-Z0-9]+)")
_EXISTS_MESSAGE = "workflows/{name}.py already exists with different content. Pick another name or edit the file."


class SavedWorkflowExistsError(ValueError):
    """Raised when saving a workflow would overwrite an existing file with different content."""


@dataclass(frozen=True, slots=True)
class SavedWorkflow:
    """Metadata of one saved workflow file, read via AST without executing the file."""

    name: str
    description: str
    args: dict[str, str]
    path: Path


def validate_workflow_name(name: str) -> str:
    """Validate a saved workflow name or raise ValueError."""
    if not isinstance(name, str) or not SAVED_WORKFLOW_NAME_RE.fullmatch(name):
        msg = (
            f"Invalid workflow name {name!r}. "
            "Use 1 to 64 lowercase letters, digits, hyphens, or underscores, starting with a letter or digit."
        )
        raise ValueError(msg)
    return name


def read_saved_source(workspace: Path, name: str) -> bytes:
    """Read `<workspace>/workflows/<name>.py` through no-follow directory descriptors."""
    validate_workflow_name(name)
    filename = f"{name}.py"
    try:
        with open_directory_within_root(workspace, WORKFLOWS_DIR) as directory_fd:
            payload = read_regular_file_within_root(
                directory_fd,
                filename,
                max_bytes=MAX_SOURCE_BYTES + 1,
                truncate=True,
            )
    except FileNotFoundError as exc:
        msg = f"Saved workflow {name!r} not found."
        raise FileNotFoundError(msg) from exc
    if not payload:
        msg = "Background script source must not be empty."
        raise ValueError(msg)
    if len(payload) > MAX_SOURCE_BYTES:
        msg = f"Background script source exceeds the {MAX_SOURCE_BYTES}-byte limit."
        raise ValueError(msg)
    return payload


def list_saved(workspace: Path) -> list[SavedWorkflow]:
    """List valid saved workflow files in `<workspace>/workflows`, parsed with AST only."""
    try:
        with open_directory_within_root(workspace, WORKFLOWS_DIR) as directory_fd:
            workflows_dir = workspace / WORKFLOWS_DIR
            found: list[SavedWorkflow] = []
            with os.scandir(directory_fd) as entries:
                for entry in entries:
                    if not entry.name.endswith(".py"):
                        continue
                    stem = entry.name[:-3]
                    if not SAVED_WORKFLOW_NAME_RE.fullmatch(stem):
                        continue
                    try:
                        raw = read_regular_file_within_root(directory_fd, entry.name, max_bytes=MAX_SOURCE_BYTES)
                    except (OSError, ValueError):
                        continue
                    description, args = _parse_metadata(raw)
                    found.append(
                        SavedWorkflow(name=stem, description=description, args=args, path=workflows_dir / entry.name),
                    )
    except FileNotFoundError:
        return []
    found.sort(key=lambda item: item.name)
    return found


def save_source(
    workspace: Path,
    name: str,
    source: str | bytes,
    description: str | None = None,
) -> Path:
    """Save *source* to `<workspace>/workflows/<name>.py` atomically without clobbering different content."""
    validate_workflow_name(name)
    text = source.decode("utf-8") if isinstance(source, bytes) else source
    if description is not None and description.strip():
        text = _with_docstring(text, description)
    payload = text.encode("utf-8")
    if not payload:
        msg = "Background script source must not be empty."
        raise ValueError(msg)
    if len(payload) > MAX_SOURCE_BYTES:
        msg = f"Background script source exceeds the {MAX_SOURCE_BYTES}-byte limit."
        raise ValueError(msg)

    filename = f"{name}.py"
    if not workspace.exists() and not workspace.is_symlink():
        workspace.mkdir(parents=True, exist_ok=True)
    with open_directory_within_root(workspace, WORKFLOWS_DIR, create=True) as directory_fd:
        try:
            with atomic_write_file_at(directory_fd, filename, file_mode=0o644, replace=False) as output:
                output.write(payload)
        except FileExistsError:
            existing = read_regular_file_within_root(
                directory_fd,
                filename,
                max_bytes=MAX_SOURCE_BYTES + 1,
                truncate=True,
            )
            if existing != payload:
                raise SavedWorkflowExistsError(_EXISTS_MESSAGE.format(name=name)) from None
    return workspace / WORKFLOWS_DIR / filename


def _parse_metadata(raw: bytes) -> tuple[str, dict[str, str]]:
    try:
        tree = ast.parse(raw.decode("utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return "", {}
    raw_doc = ast.get_docstring(tree)
    description = next((line.strip() for line in raw_doc.splitlines() if line.strip()), "") if raw_doc else ""
    args: dict[str, str] = {}
    for stmt in tree.body:
        value = _args_value(stmt)
        if value is None:
            continue
        try:
            evaluated = ast.literal_eval(value)
        except (ValueError, TypeError):
            args = {}
            continue
        if isinstance(evaluated, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in evaluated.items()):
            args = dict(evaluated)
        else:
            args = {}
    return description, args


def _args_value(stmt: ast.stmt) -> ast.expr | None:
    if isinstance(stmt, ast.Assign):
        if any(isinstance(target, ast.Name) and target.id == "ARGS" for target in stmt.targets):
            return stmt.value
    elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name) and stmt.target.id == "ARGS":
        return stmt.value
    return None


def _with_docstring(source: str, description: str) -> str:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return source
    if ast.get_docstring(tree) is not None:
        return source
    literal = _docstring_literal(description)
    lines = source.splitlines(keepends=True)
    insert_at = 0
    if lines and lines[0].startswith("#!"):
        insert_at = 1
    if insert_at < len(lines) and _ENCODING_COOKIE_RE.match(lines[insert_at]):
        insert_at += 1
    elif insert_at == 0 and len(lines) > 1 and _ENCODING_COOKIE_RE.match(lines[1]):
        insert_at = 2
    prefix = "".join(lines[:insert_at])
    rest = "".join(lines[insert_at:]).lstrip("\n")
    return f"{prefix}{literal}\n\n{rest}" if rest else f"{prefix}{literal}\n"


def format_workflows_listing(
    runs: Sequence[WorkflowRun],
    saved: Sequence[SavedWorkflow],
) -> str:
    """Format recent workflow runs and saved workflows for chat output (`!workflows`)."""
    if not runs and not saved:
        return "No workflow runs or saved workflows here."
    sections: list[str] = []
    if runs:
        run_lines = [
            f"- `{run.run_id}` ({run.state.value}, attempt {run.attempt})" + (f": {run.title}" if run.title else "")
            for run in runs
        ]
        sections.append("Workflow runs:\n" + "\n".join(run_lines))
    if saved:
        saved_lines: list[str] = []
        for item in saved:
            line = f"- `{item.name}`"
            if item.description:
                line += f": {item.description}"
            if item.args:
                arg_names = ", ".join(f"`{arg}`" for arg in item.args)
                line += f" (args: {arg_names})"
            saved_lines.append(line)
        sections.append("Saved workflows:\n" + "\n".join(saved_lines))
    return "\n\n".join(sections)


def _docstring_literal(description: str) -> str:
    cleaned = description.strip()
    if """\"\"\"""" not in cleaned and not cleaned.endswith('"') and "\\" not in cleaned:
        return f"""\"\"\"{cleaned}\"\"\""""
    return repr(cleaned)
