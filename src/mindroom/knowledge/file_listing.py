"""File listing and inclusion rules for knowledge bases.

This module decides which files belong to a knowledge base, in three composable layers:
include patterns derive listing targets that bound where traversal looks, traversal
walks directory descriptors pinned from the knowledge root without following links, and
per-file rules run cheap relative-path checks before filesystem safety checks.
Callers pass the canonical root their binding resolved; a root that no longer resolves
to itself lists nothing. Every listed path is a regular file within the read cap, reached
without links, and ``open_knowledge_file`` refuses a swap made after listing.
"""

from __future__ import annotations

import os
import stat
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from mindroom.git_invocation import hardened_git_command, hardened_git_env
from mindroom.knowledge.redaction import redact_credentials_in_text
from mindroom.logging_config import get_logger
from mindroom.path_confinement import (
    MAX_READ_BYTES,
    is_git_metadata_path,
    open_directory_within_root,
    open_regular_file_within_root,
)
from mindroom.path_globs import matches_root_glob

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

    from mindroom.config.main import Config

_GLOB_CHARS = frozenset("*?[")
_TEXT_LIKE_EXTENSIONS = {
    ".md",
    ".markdown",
    ".txt",
    ".text",
    ".rst",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".csv",
    ".tsv",
    ".html",
    ".xml",
    ".py",
    ".pyi",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".mjs",
    ".cjs",
    ".c",
    ".cc",
    ".cpp",
    ".cxx",
    ".h",
    ".hh",
    ".hpp",
    ".java",
    ".kt",
    ".kts",
    ".go",
    ".rs",
    ".rb",
    ".php",
    ".swift",
    ".scala",
    ".sc",
    ".sh",
    ".bash",
    ".zsh",
    ".fish",
    ".ps1",
    ".sql",
    ".css",
    ".scss",
    ".sass",
    ".less",
    ".vue",
    ".svelte",
    ".proto",
}


logger = get_logger(__name__)


@dataclass(frozen=True)
class _ListingTarget:
    path: Path
    mode: Literal["file", "dir", "walk"]


def _split_pattern_parts(pattern: str) -> tuple[str, ...]:
    normalized = pattern.replace("\\", "/").strip().removeprefix("./").strip("/")
    if not normalized:
        return ()
    return tuple(part for part in normalized.split("/") if part and part != ".")


def _part_has_glob(part: str) -> bool:
    return any(char in part for char in _GLOB_CHARS)


def _listing_targets_for_pattern(resolved_root: Path, pattern: str) -> list[_ListingTarget]:
    parts = _split_pattern_parts(pattern)
    if not parts:
        return []
    first_glob_index = next((index for index, part in enumerate(parts) if _part_has_glob(part)), len(parts))
    if first_glob_index == len(parts):
        return [_ListingTarget(resolved_root.joinpath(*parts), "file")]

    base = resolved_root.joinpath(*parts[:first_glob_index]) if first_glob_index else resolved_root
    remaining_parts = parts[first_glob_index:]
    if len(remaining_parts) == 1 and remaining_parts[0] != "**":
        return [_ListingTarget(base, "dir")]
    return [_ListingTarget(base, "walk")]


def _listing_targets(resolved_root: Path, patterns: list[str]) -> list[_ListingTarget]:
    if not patterns:
        return [_ListingTarget(resolved_root, "walk")]

    deduped: list[_ListingTarget] = []
    seen: set[tuple[Path, str]] = set()
    for pattern in patterns:
        for target in _listing_targets_for_pattern(resolved_root, pattern):
            key = (target.path, target.mode)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(target)
    return deduped


def _is_hidden_relative_path(relative_path: Path) -> bool:
    return any(part.startswith(".") for part in relative_path.parts)


def _include_knowledge_relative_path(config: Config, base_id: str, relative_path: str) -> bool:
    """Return whether a relative path is managed by the base path filters."""
    path_obj = Path(relative_path)
    if path_obj.is_absolute() or ".." in path_obj.parts or is_git_metadata_path(path_obj):
        return False

    base_config = config.get_knowledge_base_config(base_id)
    if base_config.include_patterns and not any(
        matches_root_glob(relative_path, pattern) for pattern in base_config.include_patterns
    ):
        return False
    if any(matches_root_glob(relative_path, pattern) for pattern in base_config.exclude_patterns):
        return False

    git_config = base_config.git
    skip_hidden = git_config.skip_hidden if git_config is not None else base_config.skip_hidden
    if skip_hidden and _is_hidden_relative_path(path_obj):
        return False

    if git_config is None:
        return True

    git_included = not git_config.include_patterns or any(
        matches_root_glob(relative_path, pattern) for pattern in git_config.include_patterns
    )
    git_excluded = any(matches_root_glob(relative_path, pattern) for pattern in git_config.exclude_patterns)
    return git_included and not git_excluded


def include_semantic_knowledge_relative_path(config: Config, base_id: str, relative_path: str) -> bool:
    """Return whether a relative path is semantically indexable for one base."""
    if not _include_knowledge_relative_path(config, base_id, relative_path):
        return False

    base_config = config.get_knowledge_base_config(base_id)
    allowed_extensions = (
        set(base_config.include_extensions) if base_config.include_extensions is not None else _TEXT_LIKE_EXTENSIONS
    )
    allowed_extensions = allowed_extensions | set(base_config.extra_extensions)

    suffix = Path(relative_path).suffix.lower()
    if suffix not in allowed_extensions:
        return False
    return suffix not in base_config.exclude_extensions


def include_knowledge_relative_path(config: Config, base_id: str, relative_path: str) -> bool:
    """Return whether a relative path belongs to the active source set for one base."""
    if config.get_knowledge_base_config(base_id).mode == "files":
        return _include_knowledge_relative_path(config, base_id, relative_path)
    return include_semantic_knowledge_relative_path(config, base_id, relative_path)


@contextmanager
def _pinned_directory(path: Path) -> Iterator[int]:
    """Pin one canonical absolute directory by a no-follow walk from the filesystem root."""
    with open_directory_within_root(Path(path.anchor), path.relative_to(path.anchor)) as directory_fd:
        yield directory_fd


@contextmanager
def open_knowledge_file(path: Path) -> Iterator[int]:
    """Open one listed knowledge file without following a link swapped onto its path after listing.

    Listed paths are canonical, so a no-follow walk of every component from the
    filesystem root reaches exactly the listed regular file or fails.
    """
    with open_regular_file_within_root(Path(path.anchor), path.relative_to(path.anchor)) as file_fd:
        yield file_fd


def _is_regular_file_at(root_fd: int, relative_path: Path) -> bool:
    try:
        with open_directory_within_root(root_fd, relative_path.parent) as parent_fd:
            status = os.stat(relative_path.name, dir_fd=parent_fd, follow_symlinks=False)
    except (OSError, ValueError):
        return False
    if stat.S_ISREG(status.st_mode) and status.st_size > MAX_READ_BYTES:
        logger.warning("Skipping a knowledge file above the read cap", path=str(relative_path), size=status.st_size)
        return False
    return stat.S_ISREG(status.st_mode)


def _walk_relative_files(root_fd: int, base: Path) -> list[Path]:
    """Return files below ``base`` by a descriptor walk that never enters a linked directory."""
    try:
        with open_directory_within_root(root_fd, base) as base_fd:
            files: list[Path] = []
            for dirpath, dirnames, filenames, _dirfd in os.fwalk(".", dir_fd=base_fd):
                dirnames[:] = [name for name in dirnames if name.casefold() != ".git"]
                files.extend(base / dirpath / name for name in filenames)
            return files
    except (OSError, ValueError):
        return []


def _iter_target_files(root_fd: int, target: _ListingTarget, root: Path) -> Iterator[Path]:
    """Yield candidate paths relative to the root for one listing target."""
    relative_target = target.path.relative_to(root)
    if ".." in relative_target.parts:
        return
    if target.mode == "file":
        yield relative_target
        return
    if target.mode == "dir":
        try:
            with open_directory_within_root(root_fd, relative_target) as directory_fd:
                names = [entry.name for entry in os.scandir(directory_fd) if entry.is_file(follow_symlinks=False)]
        except (OSError, ValueError):
            return
        yield from (relative_target / name for name in names)
        return
    yield from _walk_relative_files(root_fd, relative_target)


def _canonical_root(knowledge_root: Path) -> Path | None:
    """Return the knowledge root when it still resolves to itself, else ``None``."""
    root = knowledge_root.expanduser()
    return root if root.is_absolute() and root.resolve() == root else None


def list_knowledge_files(config: Config, base_id: str, knowledge_root: Path) -> list[Path]:
    """List managed files without constructing a knowledge manager."""
    root = _canonical_root(knowledge_root)
    if root is None:
        logger.warning("Knowledge root no longer resolves to itself; listing nothing", base_id=base_id)
        return []
    include_patterns = config.get_knowledge_base_config(base_id).include_patterns
    files: set[Path] = set()
    try:
        with _pinned_directory(root) as root_fd:
            for target in _listing_targets(root, include_patterns):
                for relative_path in _iter_target_files(root_fd, target, root):
                    if not include_knowledge_relative_path(config, base_id, relative_path.as_posix()):
                        continue
                    if _is_regular_file_at(root_fd, relative_path):
                        files.add(root / relative_path)
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as exc:
        logger.warning("Cannot list knowledge files", base_id=base_id, root=str(root), error=str(exc))
        return []
    return sorted(files)


def knowledge_files_from_relative_paths(
    config: Config,
    base_id: str,
    knowledge_root: Path,
    relative_paths: Iterable[str],
) -> list[Path]:
    """Resolve claimed relative paths through the same inclusion rules and safety checks."""
    root = _canonical_root(knowledge_root)
    if root is None:
        return []
    files: list[Path] = []
    try:
        with _pinned_directory(root) as root_fd:
            for relative_path in sorted(set(relative_paths)):
                if not include_knowledge_relative_path(config, base_id, relative_path):
                    continue
                if _is_regular_file_at(root_fd, Path(relative_path)):
                    files.append(root / relative_path)
    except (OSError, ValueError):
        return []
    return files


def git_checkout_present(root: Path, git_dir: Path) -> bool:
    """Return whether root is a checkout of the MindRoom-owned Git directory ``git_dir``.

    Nothing inside root is consulted: a ``.git`` there is writable by whoever
    can write the knowledge files, and Git is never pointed at it.
    """
    return root.is_dir() and (git_dir / "HEAD").is_file()


def git_tracked_relative_paths_from_checkout(
    config: Config,
    base_id: str,
    knowledge_root: Path,
    git_dir: Path,
    *,
    timeout_seconds: float | None = None,
) -> set[str]:
    """Return the Git-tracked relative paths that pass the base inclusion rules."""
    git_config = config.get_knowledge_base_config(base_id).git
    if git_config is None:
        return set()
    effective_timeout_seconds = float(
        git_config.sync_timeout_seconds if timeout_seconds is None else timeout_seconds,
    )
    try:
        result = subprocess.run(
            hardened_git_command(["ls-files", "-z"]),
            cwd=str(knowledge_root),
            env=hardened_git_env(git_dir=git_dir, work_tree=knowledge_root),
            check=False,
            capture_output=True,
            text=True,
            timeout=effective_timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        msg = f"Git command timed out after {effective_timeout_seconds:g}s: git ls-files -z"
        raise RuntimeError(msg) from exc
    except OSError as exc:
        msg = f"Git command failed: git ls-files -z\n{exc}"
        raise RuntimeError(msg) from exc

    if result.returncode != 0:
        details = redact_credentials_in_text((result.stderr or result.stdout).strip())
        msg = f"Git command failed with exit code {result.returncode}: git ls-files -z"
        if details:
            msg = f"{msg}\n{details}"
        raise RuntimeError(msg)

    return {
        path for path in result.stdout.split("\x00") if path and include_knowledge_relative_path(config, base_id, path)
    }


def list_git_tracked_knowledge_files(
    config: Config,
    base_id: str,
    knowledge_root: Path,
    git_dir: Path,
    *,
    timeout_seconds: float | None = None,
) -> list[Path]:
    """List Git-tracked files using the active source set for one base."""
    root = _canonical_root(knowledge_root)
    if root is None or not git_checkout_present(root, git_dir):
        return []
    return knowledge_files_from_relative_paths(
        config,
        base_id,
        root,
        git_tracked_relative_paths_from_checkout(config, base_id, root, git_dir, timeout_seconds=timeout_seconds),
    )
