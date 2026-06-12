"""File listing and inclusion rules for knowledge bases.

This module decides which files belong to a knowledge base, in three composable layers:
include patterns derive listing targets that bound where traversal looks, traversal
yields only candidates whose directory chain is vetted, and per-file rules run cheap
relative-path checks before filesystem safety checks.
Every path returned by the listing functions is a regular file, not a symlink, with no
symlinked ancestors, whose strictly resolved location stays inside the knowledge root.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from mindroom.knowledge.redaction import redact_credentials_in_text
from mindroom.path_globs import matches_root_glob

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

    from mindroom.config.main import Config

_GIT_CHECKOUT_DETECTION_TIMEOUT_SECONDS = 5.0
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
    if path_obj.is_absolute() or ".." in path_obj.parts:
        return False

    base_config = config.get_knowledge_base_config(base_id)
    if base_config.include_patterns and not any(
        matches_root_glob(relative_path, pattern) for pattern in base_config.include_patterns
    ):
        return False
    if any(matches_root_glob(relative_path, pattern) for pattern in base_config.exclude_patterns):
        return False

    git_config = base_config.git
    if git_config is not None and git_config.skip_hidden and _is_hidden_relative_path(path_obj):
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


@dataclass
class _DirectoryGuard:
    """Cached directory-chain vetting for one listing pass."""

    root: Path
    _symlink_cache: dict[Path, bool] = field(default_factory=dict)

    def is_safe(self, directory: Path) -> bool:
        """Return whether a directory is inside root and reached without symlinks."""
        try:
            relative_path = directory.relative_to(self.root)
        except ValueError:
            return False

        current = self.root
        for part in relative_path.parts:
            current = current / part
            cached = self._symlink_cache.get(current)
            if cached is None:
                cached = current.is_symlink()
                self._symlink_cache[current] = cached
            if cached:
                return False
        return True


def _iter_target_files(target: _ListingTarget, guard: _DirectoryGuard) -> Iterator[Path]:
    """Yield candidate files for one target, vetting their directory chain via the guard."""
    if target.mode == "file":
        if guard.is_safe(target.path.parent):
            yield target.path
        return
    if not target.path.is_dir() or not guard.is_safe(target.path):
        return
    if target.mode == "dir":
        yield from (path for path in target.path.iterdir() if path.is_file())
        return
    for dirpath, dirnames, filenames in os.walk(target.path, followlinks=False):
        current_dir = Path(dirpath)
        dirnames[:] = [dirname for dirname in dirnames if not (current_dir / dirname).is_symlink()]
        for filename in filenames:
            yield current_dir / filename


def _resolve_safe_file(root: Path, candidate: Path) -> Path | None:
    """Return the resolved path when a chain-vetted candidate is a regular file inside root."""
    if candidate.is_symlink():
        return None
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError):
        return None
    if not candidate.is_file():
        return None
    return resolved


def list_knowledge_files(config: Config, base_id: str, knowledge_root: Path) -> list[Path]:
    """List managed files without constructing a knowledge manager."""
    root = knowledge_root.resolve()
    if not root.is_dir():
        return []

    guard = _DirectoryGuard(root=root)
    include_patterns = config.get_knowledge_base_config(base_id).include_patterns
    files: set[Path] = set()
    for target in _listing_targets(root, include_patterns):
        for candidate in _iter_target_files(target, guard):
            if not include_knowledge_relative_path(config, base_id, candidate.relative_to(root).as_posix()):
                continue
            resolved = _resolve_safe_file(root, candidate)
            if resolved is not None:
                files.add(resolved)
    return sorted(files)


def knowledge_files_from_relative_paths(
    config: Config,
    base_id: str,
    knowledge_root: Path,
    relative_paths: Iterable[str],
) -> list[Path]:
    """Resolve claimed relative paths through the same inclusion rules and safety checks."""
    root = knowledge_root.resolve()
    guard = _DirectoryGuard(root=root)
    files: list[Path] = []
    for relative_path in sorted(set(relative_paths)):
        if not include_knowledge_relative_path(config, base_id, relative_path):
            continue
        candidate = root / relative_path
        if not guard.is_safe(candidate.parent):
            continue
        resolved = _resolve_safe_file(root, candidate)
        if resolved is not None:
            files.append(resolved)
    return files


def git_checkout_present(root: Path, *, timeout_seconds: float | None = None) -> bool:
    """Return whether root itself is a Git worktree checkout."""
    if not root.is_dir():
        return False
    effective_timeout_seconds = _GIT_CHECKOUT_DETECTION_TIMEOUT_SECONDS if timeout_seconds is None else timeout_seconds
    if effective_timeout_seconds <= 0:
        effective_timeout: float | None = None
    else:
        effective_timeout = effective_timeout_seconds
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--is-inside-work-tree", "--show-toplevel"],
            check=False,
            capture_output=True,
            text=True,
            timeout=effective_timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if result.returncode != 0:
        return False
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if len(lines) < 2 or lines[0] != "true":
        return False
    try:
        return Path(lines[1]).resolve() == root.resolve()
    except OSError:
        return False


def git_tracked_relative_paths_from_checkout(
    config: Config,
    base_id: str,
    knowledge_root: Path,
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
            ["git", "ls-files", "-z"],
            cwd=str(knowledge_root),
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
    *,
    timeout_seconds: float | None = None,
) -> list[Path]:
    """List Git-tracked files using the active source set for one base."""
    root = knowledge_root.resolve()
    if not git_checkout_present(root, timeout_seconds=timeout_seconds):
        return []
    return knowledge_files_from_relative_paths(
        config,
        base_id,
        root,
        git_tracked_relative_paths_from_checkout(config, base_id, root, timeout_seconds=timeout_seconds),
    )
