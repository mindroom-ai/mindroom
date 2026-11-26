"""Saved workflows in `<workspace>/workflows/<name>.py`: confined reads/writes and AST-only metadata."""

from __future__ import annotations

import ast
import os
import re
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING

from mindroom.atomic_file import atomic_write_file_at
from mindroom.path_confinement import open_directory_within_root, read_regular_file_within_root
from mindroom.script_runs.launch_contract import MAX_SOURCE_BYTES

if TYPE_CHECKING:
    from pathlib import Path

WORKFLOWS_DIR = "workflows"
SAVED_WORKFLOW_NAME_RE = re.compile(r"\A[a-z0-9][a-z0-9_-]{0,63}\Z")
_ENCODING_COOKIE_RE = re.compile(r"^[ \t\f]*#.*?coding[:=][ \t]*([-_.a-zA-Z0-9]+)")
_EXISTS_MESSAGE = "workflows/{name}.py already exists with different content. Pick another name or edit the file."


@dataclass(frozen=True, slots=True)
class SavedWorkflow:
    """Metadata of one saved workflow file, read via AST without executing the file."""

    name: str
    description: str
    args: dict[str, str]


def validate_workflow_name(name: str) -> str:
    """Validate a saved workflow name or raise ValueError."""
    if not isinstance(name, str) or not SAVED_WORKFLOW_NAME_RE.fullmatch(name):
        msg = (
            f"Invalid workflow name {name!r}. "
            "Use 1 to 64 lowercase letters, digits, hyphens, or underscores, starting with a letter or digit."
        )
        raise ValueError(msg)
    return name


def list_saved(workspace: Path) -> list[SavedWorkflow]:
    """List valid saved workflow files in `<workspace>/workflows`, parsed with AST only."""
    try:
        with open_directory_within_root(workspace, WORKFLOWS_DIR) as directory_fd:
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
                    found.append(SavedWorkflow(name=stem, description=description, args=args))
    except FileNotFoundError:
        return []
    found.sort(key=lambda item: item.name)
    return found


def save_source(
    workspace: Path,
    name: str,
    source: str | bytes,
    description: str | None = None,
) -> None:
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
                raise ValueError(_EXISTS_MESSAGE.format(name=name)) from None


def _parse_metadata(raw: bytes) -> tuple[str, dict[str, str]]:
    try:
        tree = ast.parse(raw.decode("utf-8-sig"))
    except (SyntaxError, UnicodeDecodeError):
        return "", {}
    raw_doc = ast.get_docstring(tree)
    description = next((line.strip() for line in raw_doc.splitlines() if line.strip()), "") if raw_doc else ""
    values: list[ast.expr] = [
        value
        for stmt in tree.body
        if (
            (isinstance(stmt, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "ARGS" for t in stmt.targets))
            or (isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name) and stmt.target.id == "ARGS")
        )
        and (value := stmt.value) is not None
    ]
    args: dict[str, str] = {}
    if values:
        with suppress(ValueError, TypeError):
            evaluated = ast.literal_eval(values[-1])
            if isinstance(evaluated, dict) and all(
                isinstance(k, str) and isinstance(v, str) for k, v in evaluated.items()
            ):
                args = dict(evaluated)
    return description, args


def _with_docstring(source: str, description: str) -> str:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return source
    if ast.get_docstring(tree) is not None:
        return source
    lines = source.splitlines(keepends=True)
    header = int(bool(lines and lines[0].startswith("#!")))
    if header < len(lines) and _ENCODING_COOKIE_RE.match(lines[header]):
        header += 1
    prefix, rest = "".join(lines[:header]), "".join(lines[header:]).lstrip("\n")
    literal = repr(description.strip())
    return f"{prefix}{literal}\n\n{rest}" if rest else f"{prefix}{literal}\n"
