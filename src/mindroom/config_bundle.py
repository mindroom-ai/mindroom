"""Validate and install complete configuration trees independently of transport."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from itertools import chain
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Literal

from mindroom.config.main import load_config
from mindroom.config.yaml_includes import load_yaml_config_source
from mindroom.constants import exported_process_env, resolve_runtime_paths
from mindroom.file_locks import advisory_file_lock

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = ["BundleChange", "BundleInstallResult", "classify_bundle_change", "install_config_bundle"]

_METADATA = ".mindroom-bundle.json"
_MAX_REVISION_LENGTH = 128


class _NestedMountError(ValueError):
    """A tree contains files owned by another mounted filesystem."""


def _reject_managed_mounts(*roots: Path) -> None:
    """Reject mounts at or below managed roots, including same-device bind mounts."""
    if sys.platform == "linux":
        try:
            mountinfo = Path("/proc/self/mountinfo").read_text(encoding="utf-8", errors="surrogateescape")
        except OSError as exc:
            msg = "Cannot verify bundle mount boundaries."
            raise _NestedMountError(msg) from exc
        mount_paths = (
            Path(re.sub(r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)), line.split()[4]))
            for line in mountinfo.splitlines()
        )
    else:
        mount_paths = (
            path for root in roots if root.exists() for path in chain((root,), root.rglob("*")) if path.is_mount()
        )
    for mount_path in mount_paths:
        if any(mount_path.is_relative_to(root) for root in roots):
            msg = f"Bundle tree contains a mount: {mount_path}"
            raise _NestedMountError(msg)


@dataclass(frozen=True)
class _IdleTransaction:
    previous: str | None


@dataclass(frozen=True)
class _ActiveTransaction:
    pending: str | None
    retired: str | None


type _Transaction = _IdleTransaction | _ActiveTransaction


@dataclass(frozen=True)
class BundleInstallResult:
    """Filesystem receipt; a fingerprint does not imply runtime application."""

    status: Literal["installed", "unchanged", "initialized"]
    config_path: Path
    fingerprint: str | None = None
    digest: str | None = None
    recovery_pending: bool = False
    previous_digest: str | None = None


def _tree_entries(root: Path) -> dict[str, tuple[int, bytes | None]]:
    """Map relative names to modes and content hashes in digest order, rejecting links and special files."""
    entries: dict[str, tuple[int, bytes | None]] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        mode = path.lstat().st_mode
        if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
            msg = f"Bundle contains a symlink or special file: {path}"
            raise ValueError(msg)
        if relative == _METADATA:
            continue
        content = None
        if stat.S_ISREG(mode):
            with path.open("rb") as stream:
                content = hashlib.file_digest(stream, "sha256").digest()
        entries[relative] = (mode, content)
    return entries


def _tree_digest(root: Path) -> str:
    """Hash names, modes and bytes, rejecting links and special files."""
    digest = hashlib.sha256()
    for relative, (mode, content) in _tree_entries(root).items():
        digest.update(json.dumps([relative, mode]).encode())
        if content is not None:
            digest.update(content)
    return digest.hexdigest()


def _directory_digest(path: Path) -> str | None:
    return _tree_digest(path) if path.exists() else None


def _read_transaction(journal: Path) -> _Transaction:
    """Read the content digests owned by an interrupted publication."""
    try:
        owned = json.loads(journal.read_text())
    except ValueError as exc:
        msg = "Invalid bundle recovery transaction; no files were changed."
        raise ValueError(msg) from exc
    if (
        not isinstance(owned, dict)
        or set(owned) not in ({"previous"}, {"pending", "retired"})
        or any(value is not None and not isinstance(value, str) for value in owned.values())
    ):
        msg = "Invalid bundle recovery transaction; no files were changed."
        raise ValueError(msg)
    if "previous" in owned:
        return _IdleTransaction(previous=owned["previous"])
    return _ActiveTransaction(pending=owned["pending"], retired=owned["retired"])


def _write_transaction(journal: Path, owned: _Transaction) -> None:
    """Publish complete bookkeeping atomically, retaining old state on write failure."""
    descriptor, name = tempfile.mkstemp(prefix=f"{journal.name}-", dir=journal.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(asdict(owned), stream)
        temporary.replace(journal)
    finally:
        temporary.unlink(missing_ok=True)


def _recovery_transaction(target: Path) -> _ActiveTransaction | None:
    """Require persisted ownership before any previous or recovery tree can move."""
    journal = target.with_name(f".{target.name}.transaction")
    owned = _read_transaction(journal) if journal.exists() else _IdleTransaction(previous=None)
    previous = target.with_name(f"{target.name}.previous")
    previous_digest = _directory_digest(previous)
    if isinstance(owned, _ActiveTransaction):
        if previous_digest is not None and previous_digest not in (owned.pending, owned.retired):
            msg = "Unowned previous bundle directory during recovery; no files were changed."
            raise ValueError(msg)
        return owned
    if previous_digest != owned.previous:
        msg = "Unowned previous bundle directory; no files were changed."
        raise ValueError(msg)
    if any(target.with_name(f".{target.name}.{suffix}").exists() for suffix in ("pending", "retired")):
        msg = "Unowned bundle recovery directory; no files were changed."
        raise ValueError(msg)
    return None


def _finish_rotation(target: Path) -> None:
    """Finish interrupted publication, preserving the most recent complete tree."""
    pending = target.with_name(f".{target.name}.pending")
    previous = target.with_name(f"{target.name}.previous")
    retired = target.with_name(f".{target.name}.retired")
    journal = target.with_name(f".{target.name}.transaction")
    owned = _recovery_transaction(target)
    if owned is None:
        return
    _reject_managed_mounts(target, previous, pending, retired)
    for path, digest in ((pending, owned.pending), (retired, owned.retired)):
        if path.exists() and _directory_digest(path) != digest:
            msg = f"Unowned bundle recovery directory: {path}"
            raise ValueError(msg)
    if pending.exists():
        if not target.exists():
            pending.rename(target)
        else:
            if previous.exists():
                if _directory_digest(previous) != owned.retired:
                    msg = "Previous bundle changed during recovery; no files were changed."
                    raise ValueError(msg)
                previous.rename(retired)
            pending.rename(previous)
    if retired.exists():
        _reject_managed_mounts(retired)
        shutil.rmtree(retired)
    _write_transaction(journal, _IdleTransaction(previous=_directory_digest(previous)))


def _publish_bundle(stage: Path, target: Path) -> bool:
    """Publish a validated tree and return whether recovery still needs a retry."""
    pending = target.with_name(f".{target.name}.pending")
    previous = target.with_name(f"{target.name}.previous")
    journal = target.with_name(f".{target.name}.transaction")
    _reject_managed_mounts(target, previous, pending, target.with_name(f".{target.name}.retired"))
    _write_transaction(
        journal,
        _ActiveTransaction(pending=_directory_digest(target), retired=_directory_digest(previous)),
    )
    if target.exists():
        target.rename(pending)
    try:
        stage.rename(target)
    except OSError:
        if pending.exists():
            pending.rename(target)
        raise
    try:
        _finish_rotation(target)
    except (OSError, _NestedMountError):
        # Publication succeeded; the journal still owns recovery paths.
        return True
    return False


def _replaceable_digest(target: Path, candidate_digest: str, *, force: bool, require_managed: bool) -> str | None:
    """Return the active tree digest once replacing that tree is allowed."""
    if not target.exists():
        return None
    active_digest = _tree_digest(target)
    if active_digest == candidate_digest and not require_managed:
        return active_digest
    try:
        metadata = json.loads((target / _METADATA).read_text())
    except (OSError, ValueError):
        metadata = None
    baseline = metadata.get("digest") if isinstance(metadata, dict) else None
    if not force and active_digest != baseline:
        msg = "Target contains authored edits or is unmanaged; use --force to replace it explicitly."
        raise ValueError(msg)
    return active_digest


def _validate_revision(revision: str | None) -> None:
    if revision is not None and (
        not isinstance(revision, str)
        or not revision.strip()
        or any(0xD800 <= ord(char) <= 0xDFFF for char in revision)
        or len(revision.encode("utf-8")) > _MAX_REVISION_LENGTH
        or any(ord(char) < 32 or ord(char) == 127 for char in revision)
    ):
        msg = "Bundle revision must be a nonempty string of at most 128 UTF-8 bytes without control characters."
        raise ValueError(msg)


def _active_revision(target: Path) -> str | None:
    try:
        metadata = json.loads((target / _METADATA).read_text())
    except (OSError, ValueError):
        return None
    revision = metadata.get("revision") if isinstance(metadata, dict) else None
    try:
        _validate_revision(revision)
    except ValueError:
        return None
    return revision


def _replace_metadata(target: Path, digest: str, revision: str) -> None:
    """Publish a revision change without exposing partial active metadata."""
    descriptor, name = tempfile.mkstemp(prefix=f".{target.name}.metadata-", dir=target.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump({"digest": digest, "revision": revision}, stream)
            stream.write("\n")
        temporary.replace(target / _METADATA)
    finally:
        temporary.unlink(missing_ok=True)


def _validate_config(config: Path) -> None:
    if config == Path(_METADATA):
        msg = f"{_METADATA} is reserved for bundle metadata."
        raise ValueError(msg)
    if config.is_absolute() or ".." in config.parts or not config.name:
        msg = "--config must be a relative file path inside the bundle."
        raise ValueError(msg)


def _validate_target(target: Path, config: Path, *, initialize_only: bool, force: bool) -> Path:
    """Reject unsafe paths and conflicting install modes before filesystem mutation."""
    _validate_config(config)
    if force and initialize_only:
        msg = "--force and --initialize-only cannot be combined."
        raise ValueError(msg)
    target = target.expanduser().absolute()
    target = target.parent.resolve() / target.name
    for path in (
        target,
        target.with_name(f"{target.name}.previous"),
        target.with_name(f".{target.name}.pending"),
        target.with_name(f".{target.name}.retired"),
    ):
        if path.is_symlink() or (path.exists() and not path.is_dir()):
            msg = f"Bundle target and recovery paths must be real directories: {path}"
            raise ValueError(msg)
    for suffix in ("lock", "transaction"):
        path = target.with_name(f".{target.name}.{suffix}")
        if path.is_symlink() or (path.exists() and not path.is_file()):
            msg = f"Bundle {suffix} must be a real file: {path}"
            raise ValueError(msg)
    return target


def _validate_source(source: Path, target: Path) -> Path:
    source = source.expanduser().absolute()
    if source.is_symlink() or not source.is_dir():
        msg = "Bundle source must be a real directory, not a symlink."
        raise ValueError(msg)
    source = source.resolve()
    if source.is_relative_to(target) or target.is_relative_to(source):
        msg = "Bundle source and target must not overlap."
        raise ValueError(msg)
    _tree_digest(source)
    return source


@dataclass(frozen=True)
class BundleChange:
    """Changed tree paths, split by whether native YAML/include loading reads them."""

    sources: tuple[str, ...]
    other: tuple[str, ...]


def _source_files(root: Path, configs: Sequence[Path]) -> set[str]:
    """Return every file the native YAML loader reads from the entrypoints, relative to root."""
    root = root.resolve()
    files: set[str] = set()
    for config in configs:
        try:
            read = load_yaml_config_source(root / config)[1]
        except Exception as exc:
            # Any loader failure makes the tree unclassifiable, including the
            # KeyError/AttributeError/IndexError the safe constructor raises for
            # tags such as `!!bool maybe` or `!!int ""`. Some messages echo YAML
            # values, so name only the file and the error type.
            msg = f"Cannot load the YAML/include sources of {root / config} ({type(exc).__name__})."
            raise ValueError(msg) from exc
        files.update(path.relative_to(root).as_posix() for path in read)
    return files


def classify_bundle_change(old: Path, new: Path, configs: Sequence[Path]) -> BundleChange:
    """Split the differences between two trees into YAML/include sources and everything else.

    Sources are the files that loading each entrypoint reads in either tree,
    plus directories that only appear or disappear around them. Every
    entrypoint must load in both trees. Installer metadata is ignored, and
    links and special files are rejected as in installation.
    """
    for config in configs:
        _validate_config(config)
    old, new = old.expanduser(), new.expanduser()
    for root in (old, new):
        if root.is_symlink() or not root.is_dir():
            msg = f"Bundle tree must be a real directory: {root}"
            raise ValueError(msg)
    before, after = _tree_entries(old), _tree_entries(new)
    sources = _source_files(old, configs) | _source_files(new, configs)
    source_dirs = {parent.as_posix() for name in sources for parent in PurePosixPath(name).parents[:-1]}
    changed_sources: list[str] = []
    other: list[str] = []
    for name in sorted(before.keys() | after.keys()):
        if before.get(name) == after.get(name):
            continue
        present = [tree[name][0] for tree in (before, after) if name in tree]
        if (name in sources and all(stat.S_ISREG(mode) for mode in present)) or (
            name in source_dirs and len(present) == 1 and stat.S_ISDIR(present[0])
        ):
            changed_sources.append(name)
        else:
            other.append(name)
    return BundleChange(tuple(changed_sources), tuple(other))


def _require_source_only(target: Path, stage: Path, config: Path) -> None:
    if not target.exists():
        msg = "--source-only requires an existing target; no replacement was made."
        raise ValueError(msg)
    other = classify_bundle_change(target, stage, (config,)).other
    if other:
        shown = ", ".join(other[:10]) + (", ..." if len(other) > 10 else "")
        msg = (
            f"Candidate changes {len(other)} path(s) outside the YAML/include sources of {config}: "
            f"{shown}; no replacement was made."
        )
        raise ValueError(msg)


def install_config_bundle(
    source: Path,
    target: Path,
    *,
    config: Path = Path("config.yaml"),
    initialize_only: bool = False,
    force: bool = False,
    source_only: bool = False,
    expected_digest: str | None = None,
    revision: str | None = None,
    process_env: dict[str, str] | None = None,
) -> BundleInstallResult:
    """Stage, validate and publish a tree; keep the prior tree at TARGET.previous.

    Directory replacement uses two renames, with a brief missing-target window.
    Installer calls serialize, but readers and external writers do not. Retry
    recovers interrupted renames; this is not a power-loss durability guarantee.
    Initialize-only preserves an existing directory when the revision is absent or matches.
    Force permits authored replacement, never invalid configuration.
    Source-only refuses a change outside the active and candidate YAML/include sources.
    """
    _validate_revision(revision)
    target = _validate_target(target, config, initialize_only=initialize_only, force=force)
    with advisory_file_lock(target.with_name(f".{target.name}.lock")):
        _finish_rotation(target)
        if initialize_only and target.exists() and (revision is None or _active_revision(target) == revision):
            return BundleInstallResult("initialized", target / config)
        source = _validate_source(source, target)
        stage = Path(tempfile.mkdtemp(prefix=f".{target.name}.stage-", dir=target.parent))
        try:
            shutil.copytree(source, stage, dirs_exist_ok=True, symlinks=True)
            # Check again after copying so links cannot enter the validated tree.
            _tree_digest(stage)
            runtime = resolve_runtime_paths(
                config_path=stage / config,
                process_env=exported_process_env() if process_env is None else process_env,
            )
            loaded = load_config(runtime)
            digest = _tree_digest(stage)
            if expected_digest is not None and digest != expected_digest:
                msg = "Candidate tree digest does not match --expected-digest; no replacement was made."
                raise ValueError(msg)
            active_digest = _replaceable_digest(target, digest, force=force, require_managed=revision is not None)
            unchanged = active_digest == digest
            if source_only and not unchanged:
                _require_source_only(target, stage, config)
            if unchanged and revision is None:
                return BundleInstallResult("unchanged", target / config, loaded.source_fingerprint, digest)
            active_revision = _active_revision(target) if target.exists() else None
            if unchanged:
                if revision is not None and active_revision != revision:
                    _replace_metadata(target, digest, revision)
                return BundleInstallResult("unchanged", target / config, loaded.source_fingerprint, digest)
            installed_revision = revision if revision is not None else active_revision
            metadata = {"digest": digest}
            if installed_revision is not None:
                metadata["revision"] = installed_revision
            (stage / _METADATA).write_text(json.dumps(metadata) + "\n")
            # The existing runtime watcher polls mtimes. Reproducible bundles
            # can change bytes while preserving every source timestamp.
            active_config = target / config
            old_mtime = active_config.stat().st_mtime_ns if active_config.exists() else 0
            mtime = max(time.time_ns(), old_mtime + 1)
            os.utime(stage / config, ns=(mtime, mtime))
            recovery_pending = _publish_bundle(stage, target)
            return BundleInstallResult(
                "installed",
                target / config,
                loaded.source_fingerprint,
                digest,
                recovery_pending,
                active_digest,
            )
        finally:
            if stage.exists():
                _reject_managed_mounts(stage)
                shutil.rmtree(stage)
