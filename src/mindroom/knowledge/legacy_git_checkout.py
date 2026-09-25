"""Adoption of knowledge checkouts whose Git directory sits inside the knowledge folder.

Earlier releases cloned Git-backed knowledge bases with an ordinary ``.git``
inside the folder, where agent tools and worker containers could write the
config, hooks and attributes that Git then executed in the primary runtime.
The adoption below moves that directory aside and hard-links only its
repository files into a Git directory created fresh, so a large object store is
neither copied nor fetched again, and no directory an agent may still hold open
ever becomes part of the MindRoom-owned repository.
"""

from __future__ import annotations

import errno
import os
import shutil
import stat
import subprocess
from pathlib import Path

from mindroom.git_invocation import hardened_git_command, hardened_git_env

__all__ = ["adopt_in_tree_git_dir"]

# LEGACY_COMPAT: Git-backed knowledge checkouts with an in-tree `.git` directory.
# Legacy format: a `.git` directory directly inside a Git-backed knowledge folder, selected when the MindRoom-owned Git directory from `knowledge_git_dir` has no `HEAD` yet.
# Last legacy release: v2026.9.290 cloned knowledge bases with an in-tree `.git`; replacement: the next release keeps the Git directory at `<storage>/knowledge_git/<folder>_<path digest>` and the folder holds worktree files only.
# Handling: The in-tree `.git` is renamed to a staging directory beside its new location; the regular files of its HEAD, index, packed-refs, shallow, split-index, objects, refs, reftable and LFS storage are hard-linked into directories created fresh, alternates excepted, beside a new config keeping only the repository format (the sync then writes the configured remote), and the fresh directory is renamed into place before staging is deleted. No directory inode from the old `.git` is reused, so a handle an agent kept into it reaches nothing Git reads afterwards, and an interruption restarts the linking from staging. A `.git` that is a link or a gitdir file, or that sits on another filesystem, is refused with instructions and never followed or copied.
# Coverage: tests/test_knowledge_git_source.py::test_legacy_in_tree_git_dir_is_renamed_not_refetched, tests/test_knowledge_git_source.py::test_legacy_adoption_drops_executable_git_metadata, tests/test_knowledge_git_source.py::test_legacy_adoption_ignores_writes_through_handles_into_the_old_git_dir, tests/test_knowledge_git_source.py::test_legacy_adoption_resumes_from_staging, tests/test_knowledge_git_source.py::test_legacy_git_pointer_is_refused_not_followed, tests/test_knowledge_git_source.py::test_legacy_git_dir_on_another_filesystem_is_refused_not_copied.

#: Repository data Git reads but never executes, which a large checkout cannot
#: afford to fetch again. ``sharedindex.*`` files belong to a split index.
_KEPT_FILES = frozenset({"HEAD", "index", "packed-refs", "shallow"})
_KEPT_TREES = frozenset({"lfs", "objects", "refs", "reftable"})
_SHARED_INDEX_PREFIX = "sharedindex."
#: Alternates would keep the repository reading an object store agents can write.
_DROPPED_FILES = frozenset({("objects", "info", "alternates"), ("objects", "info", "http-alternates")})
#: Format settings the kept data depends on, with the values Git defines for them.
_FORMAT_SETTINGS = {
    "core.repositoryformatversion": frozenset({"0", "1"}),
    "extensions.objectformat": frozenset({"sha1", "sha256"}),
    "extensions.refstorage": frozenset({"files", "reftable"}),
}
_FORMAT_SETTINGS_PATTERN = r"^(core\.repositoryformatversion|extensions\.(objectformat|refstorage))$"
_GIT_CONFIG_TIMEOUT_SECONDS = 30.0


def adopt_in_tree_git_dir(base_id: str, source_path: Path, git_dir: Path) -> bool:
    """Move ``source_path/.git`` to ``git_dir`` and return whether a legacy directory was adopted.

    Agents may still hold a working directory or descriptor inside the old
    ``.git``, and a directory inode moved into place would keep that access,
    ``..`` included. So only regular files are carried over, each hard-linked
    into directories created here; the staging directory keeps its originals
    until the new directory is in place, so an interruption simply restarts.
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
                f"Refusing to sync knowledge base '{base_id}': {in_tree} is a link or a file, not a Git directory, "
                f"and MindRoom never follows a pointer inside a knowledge folder. Delete {source_path} and the next "
                "sync clones it afresh."
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
                f"filesystems. Stop MindRoom, move {in_tree} to {staging}, and start MindRoom again, "
                f"or delete {source_path} and the next sync clones it afresh."
            )
            raise RuntimeError(msg) from None
    building = git_dir.with_name(f"{git_dir.name}.building")
    shutil.rmtree(building, ignore_errors=True)
    building.mkdir()
    for entry in staging.iterdir():
        if entry.name in _KEPT_FILES or entry.name.startswith(_SHARED_INDEX_PREFIX):
            _link_file(entry, building / entry.name)
        elif entry.name in _KEPT_TREES:
            _link_tree(staging, building, entry.name)
    _write_config(staging / "config", building / "config")
    building.rename(git_dir)
    shutil.rmtree(staging, ignore_errors=True)
    return True


def _link_file(source: Path, target: Path) -> None:
    """Hard-link one regular file; a directory cannot be hard-linked, and a swapped-in link is removed."""
    try:
        if not stat.S_ISREG(source.lstat().st_mode):
            return
        os.link(source, target, follow_symlinks=False)
    except FileNotFoundError:
        return
    if not stat.S_ISREG(target.lstat().st_mode):
        target.unlink()


def _link_tree(staging: Path, building: Path, name: str) -> None:
    """Recreate one kept tree from fresh directories holding hard links to its regular files."""
    try:
        if not stat.S_ISDIR((staging / name).lstat().st_mode):
            return
    except FileNotFoundError:
        return
    for directory, _dirnames, filenames in os.walk(staging / name):
        relative = Path(directory).relative_to(staging)
        (building / relative).mkdir(parents=True, exist_ok=True)
        for filename in filenames:
            if (*relative.parts, filename) not in _DROPPED_FILES:
                _link_file(Path(directory) / filename, building / relative / filename)


def _write_config(old_config: Path, new_config: Path) -> None:
    """Write a config holding only the repository format read from the old one.

    ``git config --file`` reads one file, follows no includes and runs nothing,
    and discovery is bounded to the MindRoom-owned directory around staging.
    """
    control_dir = old_config.parent.parent
    result = subprocess.run(
        hardened_git_command(["config", "--file", str(old_config), "--get-regexp", _FORMAT_SETTINGS_PATTERN]),
        cwd=str(control_dir),
        env=hardened_git_env({"GIT_CEILING_DIRECTORIES": str(control_dir.parent)}),
        check=False,
        capture_output=True,
        text=True,
        timeout=_GIT_CONFIG_TIMEOUT_SECONDS,
    )
    lines = []
    for line in result.stdout.splitlines():
        key, _, value = line.partition(" ")
        if value in _FORMAT_SETTINGS.get(key, ()):
            section, _, name = key.partition(".")
            lines.append(f"[{section}]\n\t{name} = {value}\n")
    new_config.write_text("".join(lines), encoding="utf-8")
