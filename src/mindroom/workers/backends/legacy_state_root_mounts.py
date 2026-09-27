"""Retire sandbox workers that mounted whole agent state roots, and report what they may have left behind."""

# LEGACY_COMPAT: Sandbox workers that mounted whole agent state roots writable.
# Legacy format: Kubernetes worker pods and Docker worker containers started by releases that mounted
#   agents/<agent> and private_instances/<scope> writable instead of only the workspaces below them.
# Last legacy release: v2026.9.324; the next release mounts only workspaces.
# Handling: at primary startup, stop every such running worker through its backend so the next ensure recreates it
#   with workspace mounts, then warn, without following or changing anything, about links above workspaces and hard
#   links under state roots that those workers could have planted. The primary never repairs them automatically.
# Coverage: tests/test_legacy_state_root_mounts.py.

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from mindroom.background_tasks import run_blocking_until_complete
from mindroom.config.yaml_includes import load_yaml_config_source
from mindroom.logging_config import get_logger
from mindroom.tool_system.worker_routing import private_instances_root_path, private_root_name, shared_storage_root
from mindroom.workers.backend import WorkerBackendError
from mindroom.workers.backends._dedicated_worker_common import resolved_agent_policies_from_config_data
from mindroom.workers.runtime import primary_worker_backend_name

if TYPE_CHECKING:
    from collections.abc import Iterator

    from mindroom.agent_policy import ResolvedAgentPolicy
    from mindroom.constants import RuntimePaths

logger = get_logger(__name__)

_MAX_SCANNED_ENTRIES = 200_000
_MAX_REPORTED_PATHS = 20


async def retire_state_root_worker_mounts(runtime_paths: RuntimePaths) -> None:
    """Stop workers that still mount state roots and report links they may have left, before runtime work starts."""
    await run_blocking_until_complete(_retire_state_root_worker_mounts, runtime_paths)


def _retire_state_root_worker_mounts(runtime_paths: RuntimePaths) -> None:
    try:
        backend_name = primary_worker_backend_name(runtime_paths)
    except WorkerBackendError:
        return
    if backend_name not in {"docker", "kubernetes"}:
        return
    try:
        stopped = _stop_state_root_workers(runtime_paths, backend_name)
    except (WorkerBackendError, OSError):
        # Old pods are never reused: ensure recreates any worker whose template predates workspace mounts.
        logger.exception("Could not stop sandbox workers that mount whole state roots", backend=backend_name)
    else:
        if stopped:
            logger.warning(
                "Stopped sandbox workers that mounted whole state roots",
                backend=backend_name,
                count=len(stopped),
            )
    _report_links_left_by_state_root_mounts(runtime_paths)


def _stop_state_root_workers(runtime_paths: RuntimePaths, backend_name: str) -> tuple[str, ...]:
    # Keep Docker and Kubernetes dependencies off primary module import paths.
    if backend_name == "docker":
        from mindroom.workers.backends.docker import remove_docker_workers_mounting_state_roots  # noqa: PLC0415

        return remove_docker_workers_mounting_state_roots(runtime_paths)
    from mindroom.workers.backends.kubernetes import stop_kubernetes_workers_mounting_state_roots  # noqa: PLC0415

    return stop_kubernetes_workers_mounting_state_roots(runtime_paths)


def _agent_policies(runtime_paths: RuntimePaths) -> dict[str, ResolvedAgentPolicy]:
    # Default private roots still locate workspaces when the config cannot be read yet.
    try:
        config_data, _source_files = load_yaml_config_source(runtime_paths.config_path)
    except (OSError, yaml.YAMLError, UnicodeError):
        return {}
    return resolved_agent_policies_from_config_data(config_data)


def _real_directories(parent: Path) -> list[Path]:
    try:
        entries = list(os.scandir(parent))
    except (FileNotFoundError, NotADirectoryError):
        return []
    return sorted(Path(entry.path) for entry in entries if entry.is_dir(follow_symlinks=False))


def _formerly_mounted_roots(
    storage_root: Path,
    policies: dict[str, ResolvedAgentPolicy],
) -> list[tuple[Path, frozenset[Path]]]:
    """Return each root old workers could write, with the workspaces below it that workers still own."""
    roots = [
        (agent_root, frozenset({agent_root / "workspace"})) for agent_root in _real_directories(storage_root / "agents")
    ]
    for scope_root in _real_directories(private_instances_root_path(storage_root)):
        workspaces = set()
        for agent_root in _real_directories(scope_root):
            policy = policies.get(agent_root.name)
            workspaces.add(agent_root / private_root_name(agent_root.name, policy.private_root if policy else None))
        roots.append((scope_root, frozenset(workspaces)))
    return roots


@dataclass
class _ScanBudget:
    remaining: int


def _entries_below(root: Path, *, skip: frozenset[Path], budget: _ScanBudget) -> Iterator[tuple[Path, os.stat_result]]:
    """Yield every entry below ``root`` without following links, leaving ``skip`` directories unvisited."""
    pending = [root]
    while pending and budget.remaining > 0:
        try:
            entries = list(os.scandir(pending.pop()))
        except OSError:
            continue
        for entry in entries:
            budget.remaining -= 1
            try:
                entry_stat = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            path = Path(entry.path)
            if stat.S_ISDIR(entry_stat.st_mode) and path not in skip:
                pending.append(path)
            yield path, entry_stat


def _hard_links_leaving(workspace: Path, *, budget: _ScanBudget) -> list[Path]:
    """Return workspace files with a hard link outside the workspace; links among its own files are normal."""
    links_by_inode: dict[tuple[int, int], tuple[int, int, Path]] = {}
    for path, entry_stat in _entries_below(workspace, skip=frozenset(), budget=budget):
        if stat.S_ISREG(entry_stat.st_mode) and entry_stat.st_nlink > 1:
            inode = (entry_stat.st_dev, entry_stat.st_ino)
            seen, nlink, first = links_by_inode.get(inode, (0, entry_stat.st_nlink, path))
            links_by_inode[inode] = (seen + 1, nlink, first)
    return [first for seen, nlink, first in links_by_inode.values() if seen < nlink]


def _report_links_left_by_state_root_mounts(runtime_paths: RuntimePaths) -> None:
    """Warn about links above workspaces and hard links under state roots, without following or changing them.

    Workers that mounted whole state roots could replace primary-owned entries
    with links, or hard-link files they should not keep into their workspace.
    Links inside a workspace are the worker's own and stay unreported, because
    the primary refuses to follow them. Primary-owned state is scanned before
    the larger workspaces, and the walk stops after a fixed number of entries.
    """
    storage_root = shared_storage_root(runtime_paths.storage_root)
    roots = _formerly_mounted_roots(storage_root, _agent_policies(runtime_paths))
    budget = _ScanBudget(_MAX_SCANNED_ENTRIES)
    links: list[Path] = []
    hard_links: list[Path] = []
    for root, workspaces in roots:
        for path, entry_stat in _entries_below(root, skip=workspaces, budget=budget):
            if stat.S_ISLNK(entry_stat.st_mode):
                links.append(path)
            elif not stat.S_ISDIR(entry_stat.st_mode) and entry_stat.st_nlink > 1:
                hard_links.append(path)
    for workspace in sorted(workspace for _root, workspaces in roots for workspace in workspaces):
        if workspace.is_dir() and not workspace.is_symlink():
            hard_links.extend(_hard_links_leaving(workspace, budget=budget))
    if budget.remaining <= 0:
        logger.warning("Stopped scanning state roots for worker-planted links", scanned_entries=_MAX_SCANNED_ENTRIES)
    for paths, message in (
        (links, "Links above agent workspaces may have been planted by sandbox workers; inspect and remove them"),
        (hard_links, "Hard links under agent state roots may have been planted by sandbox workers; inspect them"),
    ):
        if paths:
            logger.warning(
                message,
                count=len(paths),
                paths=[path.relative_to(storage_root).as_posix() for path in paths[:_MAX_REPORTED_PATHS]],
            )
