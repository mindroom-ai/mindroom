"""Shared path-safety helpers for local file-oriented tools."""

from __future__ import annotations

from glob import has_magic
from pathlib import Path

from mindroom.path_confinement import resolve_path_within_root

_BASE_DIR_ESCAPE_HINT = "Set restrict_to_base_dir=false to allow access outside base_dir."

#: Version-control metadata directories. Their contents configure the commands
#: a VCS runs -- ``.git/config`` alone can name an ``fsmonitor``, a hook path,
#: an SSH command or a content filter -- and MindRoom runs ``git`` against
#: workspace trees (knowledge checkouts above all) from processes that hold
#: every primary secret. A tool that writes agent-authored content therefore
#: never writes here, whatever the model was talked into.
_VCS_METADATA_DIRECTORIES = frozenset({".git", ".hg", ".svn", ".bzr"})


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
        resolve_path_within_root(base_dir, path.resolve(), symlinks="internal")
    except (OSError, ValueError):
        return False
    return True


def is_vcs_metadata_path(path: Path) -> bool:
    """Check whether a path names or descends into version-control metadata.

    Components are compared case-insensitively and with trailing dots and
    spaces removed, because a case-insensitive filesystem opens ``.GIT`` and
    Windows opens ``.git.`` as the very directory this refuses.
    """
    return any(part.rstrip(". ").lower() in _VCS_METADATA_DIRECTORIES for part in path.parts)


def blocked_vcs_metadata_message(action: str, requested_path: str) -> str:
    """Explain why a file-tool write into version-control metadata was blocked."""
    return (
        f"Error {action}: path '{requested_path}' is inside a version control metadata directory "
        f"({', '.join(sorted(_VCS_METADATA_DIRECTORIES))}), which tools may not modify."
    )


def _writes_vcs_metadata(requested: Path, resolved: Path, base_dir: Path) -> bool:
    """Check a write target both as written and as resolved.

    An internal symlink can point a lexically innocent name at
    ``<checkout>/.git/config``, so the resolved path is checked too. Components
    above base_dir are ignored there: they are not something a tool call chose.
    """
    if is_vcs_metadata_path(requested):
        return True
    try:
        relative = resolved.relative_to(base_dir.resolve())
    except (OSError, ValueError):
        return is_vcs_metadata_path(resolved)
    return is_vcs_metadata_path(relative)


def resolve_base_dir_path(
    base_dir: Path,
    path: str,
    restrict_to_base_dir: bool = True,
    *,
    for_write: bool = False,
) -> Path:
    """Resolve a path relative to base_dir, optionally preventing traversal.

    ``for_write`` additionally refuses version-control metadata, which is
    configuration for commands MindRoom itself runs rather than content.
    """
    requested = Path(path)
    candidate = requested if requested.is_absolute() else base_dir / requested
    if not restrict_to_base_dir:
        resolved = candidate.resolve()
    else:
        try:
            resolved = resolve_path_within_root(base_dir, requested, symlinks="internal")
        except ValueError:
            raise ValueError(_blocked_base_dir_message(path, candidate.resolve(), base_dir.resolve())) from None
    if for_write and _writes_vcs_metadata(requested, resolved, base_dir):
        raise ValueError(blocked_vcs_metadata_message("writing", path))
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
