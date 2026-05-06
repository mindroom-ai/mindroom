"""Shared path-safety helpers for local file-oriented tools."""

from __future__ import annotations

from glob import has_magic
from pathlib import Path

_BASE_DIR_ESCAPE_HINT = "Set restrict_to_base_dir=false to allow access outside base_dir."


def _blocked_base_dir_message(path: str, resolved: Path, base_dir: Path) -> str:
    """Explain why a resolved path escaped the configured base directory."""
    return f"Path '{path}' resolves to '{resolved}', which is outside base_dir '{base_dir}'. {_BASE_DIR_ESCAPE_HINT}"


def blocked_file_action_message(action: str, requested_path: str, base_dir: Path) -> str:
    """Explain why a file-tool action was blocked."""
    return f"Error {action}: path '{requested_path}' is outside base_dir '{base_dir}'. {_BASE_DIR_ESCAPE_HINT}"


def format_path_for_output(path: str | Path, base_dir: Path) -> str:
    """Prefer base-dir-relative output, falling back to absolute paths outside the base dir."""
    try:
        return str(Path(path).relative_to(base_dir))
    except ValueError:
        return str(path)


def is_within_base_dir(path: Path, base_dir: Path) -> bool:
    """Check whether a resolved path stays within base_dir."""
    try:
        path.resolve().relative_to(base_dir.resolve())
    except (OSError, ValueError):
        return False
    return True


def resolve_base_dir_path(base_dir: Path, path: str, restrict_to_base_dir: bool = True) -> Path:
    """Resolve a path relative to base_dir, optionally preventing traversal."""
    requested = Path(path)
    candidate = requested if requested.is_absolute() else base_dir / requested
    resolved = candidate.resolve()
    if not restrict_to_base_dir:
        return resolved

    base_resolved = base_dir.resolve()
    try:
        resolved.relative_to(base_resolved)
    except ValueError:
        raise ValueError(_blocked_base_dir_message(path, resolved, base_resolved)) from None
    return resolved


def split_search_pattern(base_dir: Path, pattern: str) -> tuple[Path, str]:
    """Resolve the concrete search root ahead of the first glob component."""
    pattern_path = Path(pattern)
    if pattern_path.is_absolute():
        search_root = Path(pattern_path.anchor)
        parts = list(pattern_path.relative_to(search_root).parts)
    else:
        search_root = base_dir
        parts = list(pattern_path.parts)

    first_glob_index = next((index for index, part in enumerate(parts) if has_magic(part)), len(parts))
    static_parts = parts[:first_glob_index]
    glob_parts = parts[first_glob_index:]
    if not glob_parts and static_parts:
        glob_parts = [static_parts.pop()]

    resolved_root = search_root.joinpath(*static_parts).resolve()
    resolved_pattern = str(Path(*glob_parts)) if glob_parts else "."
    return resolved_root, resolved_pattern
