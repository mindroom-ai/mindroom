"""Adoption of knowledge checkouts whose Git directory sits inside the knowledge folder.

Earlier releases cloned Git-backed knowledge bases with an ordinary ``.git``
inside the folder, where agent tools and worker containers could write the
config, hooks and attributes that Git then executed in the primary runtime.
The adoption below moves that directory to its MindRoom-owned location with a
rename, so a large object store is neither copied nor fetched again, and drops
everything in it that could name a program before Git reads it from there.
"""

from __future__ import annotations

import errno
import os
import shutil
import stat
import subprocess
from typing import TYPE_CHECKING

from mindroom.git_invocation import hardened_git_command, hardened_git_env

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["adopt_in_tree_git_dir"]

# LEGACY_COMPAT: Git-backed knowledge checkouts with an in-tree `.git` directory.
# Legacy format: a `.git` directory directly inside a Git-backed knowledge folder, selected when the MindRoom-owned Git directory from `knowledge_git_dir` has no `HEAD` yet.
# Last legacy release: v2026.9.290 cloned knowledge bases with an in-tree `.git`; replacement: the next release keeps the Git directory at `<storage>/knowledge_git/<folder>_<path digest>` and the folder holds worktree files only.
# Handling: The in-tree `.git` is renamed to a staging directory beside its new location, so its objects, refs, index and LFS objects move without copying or fetching; a fresh config keeping only the repository format replaces the old one (the sync then writes the configured remote), every other entry (hooks, `info/`, logs, `FETCH_HEAD`, worktree and submodule metadata, alternates) is deleted, and only then is the staging directory renamed into place, so an interruption resumes from staging. A `.git` that is a link or a gitdir file, or that sits on another filesystem, is refused with instructions and never followed or copied.
# Coverage: tests/test_knowledge_git_source.py::test_legacy_in_tree_git_dir_is_renamed_not_refetched, tests/test_knowledge_git_source.py::test_legacy_adoption_drops_executable_git_metadata, tests/test_knowledge_git_source.py::test_legacy_adoption_resumes_from_staging, tests/test_knowledge_git_source.py::test_legacy_git_pointer_is_refused_not_followed, tests/test_knowledge_git_source.py::test_legacy_git_dir_on_another_filesystem_is_refused_not_copied.

#: Repository data Git reads but never executes, which a large checkout cannot
#: afford to fetch again. ``sharedindex.*`` files belong to a split index.
_KEPT_ENTRIES = frozenset({"HEAD", "config", "index", "lfs", "objects", "packed-refs", "refs", "reftable", "shallow"})
_SHARED_INDEX_PREFIX = "sharedindex."
#: Format settings the kept data depends on, with the values Git defines for them.
_FORMAT_SETTINGS = {
    "core.repositoryformatversion": frozenset({"0", "1"}),
    "extensions.objectformat": frozenset({"sha1", "sha256"}),
    "extensions.refstorage": frozenset({"files", "reftable"}),
}
_FORMAT_SETTINGS_PATTERN = r"^(core\.repositoryformatversion|extensions\.(objectformat|refstorage))$"
_MAX_CONFIG_BYTES = 1 << 20
_GIT_CONFIG_TIMEOUT_SECONDS = 30.0


def adopt_in_tree_git_dir(base_id: str, source_path: Path, git_dir: Path) -> bool:
    """Move ``source_path/.git`` to ``git_dir`` and return whether a legacy directory was adopted.

    ``git_dir`` must not hold a repository yet. The rename goes to a staging
    directory first and ``git_dir`` appears only once nothing executable is
    left in it, so an interrupted adoption resumes from staging.
    """
    staging = git_dir.with_name(f"{git_dir.name}.adopting")
    if not os.path.lexists(staging):
        in_tree = source_path / ".git"
        try:
            mode = in_tree.lstat().st_mode
        except FileNotFoundError:
            return False
        if not stat.S_ISDIR(mode):
            msg = (
                f"Refusing to sync knowledge base '{base_id}': {in_tree} is a link or a file, not a Git directory. "
                f"MindRoom keeps knowledge Git directories under {git_dir.parent} and never follows a pointer "
                "inside the knowledge folder, because anyone who can write the folder could redirect it. "
                f"Delete {source_path} and the next sync clones it afresh."
            )
            raise RuntimeError(msg)
        staging.parent.mkdir(parents=True, exist_ok=True)
        try:
            in_tree.rename(staging)
        except OSError as exc:
            if exc.errno != errno.EXDEV:
                raise
            msg = (
                f"Cannot move {in_tree} to {staging} for knowledge base '{base_id}': they are on different "
                "filesystems, and MindRoom renames a knowledge repository rather than copying it. "
                f"Stop MindRoom, move {in_tree} to {staging}, and start MindRoom again to finish the move, "
                f"or delete {source_path} and the next sync clones it afresh."
            )
            raise RuntimeError(msg) from None
    _replace_config(staging)
    _delete_unkept_entries(staging)
    staging.rename(git_dir)
    return True


def _run_git_config(config_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run ``git config`` on one file, which follows no includes and runs nothing.

    Discovery is bounded to the MindRoom-owned directory holding the staging
    directory, so no enclosing repository is consulted either.
    """
    control_dir = config_path.parent.parent
    return subprocess.run(
        hardened_git_command(["config", "--file", str(config_path), *args]),
        cwd=str(control_dir),
        env=hardened_git_env({"GIT_CEILING_DIRECTORIES": str(control_dir.parent)}),
        check=False,
        capture_output=True,
        text=True,
        timeout=_GIT_CONFIG_TIMEOUT_SECONDS,
    )


def _format_settings(config_path: Path) -> dict[str, str]:
    try:
        status = config_path.lstat()
    except FileNotFoundError:
        return {}
    if not stat.S_ISREG(status.st_mode) or status.st_size > _MAX_CONFIG_BYTES:
        return {}
    result = _run_git_config(config_path, "--get-regexp", _FORMAT_SETTINGS_PATTERN)
    settings: dict[str, str] = {}
    for line in result.stdout.splitlines():
        key, _, value = line.partition(" ")
        if value in _FORMAT_SETTINGS.get(key, ()):
            settings[key] = value
    return settings


def _replace_config(staging: Path) -> None:
    """Replace the old config with one holding only the repository format."""
    config_path = staging / "config"
    settings = _format_settings(config_path)
    has_extensions = any(key.startswith("extensions.") for key in settings)
    settings.setdefault("core.repositoryformatversion", "1" if has_extensions else "0")
    settings["core.bare"] = "false"
    new_config = staging / "config.adopting"
    _delete(new_config)
    for key, value in settings.items():
        result = _run_git_config(new_config, key, value)
        if result.returncode != 0:
            msg = f"Could not write {new_config}: {result.stderr.strip()}"
            raise RuntimeError(msg)
    if config_path.is_dir() and not config_path.is_symlink():
        shutil.rmtree(config_path)
    new_config.replace(config_path)


def _delete_unkept_entries(staging: Path) -> None:
    """Delete every entry Git could execute or be redirected by; a kept name that is a link is deleted too."""
    with os.scandir(staging) as entries:
        unkept = [
            entry.name
            for entry in entries
            if entry.is_symlink() or not (entry.name in _KEPT_ENTRIES or entry.name.startswith(_SHARED_INDEX_PREFIX))
        ]
    for name in unkept:
        _delete(staging / name)
    objects_info = staging / "objects" / "info"
    if objects_info.is_symlink():
        objects_info.unlink()
        return
    for name in ("alternates", "http-alternates"):
        _delete(objects_info / name)


def _delete(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)
