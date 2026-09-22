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
from pathlib import Path
from typing import Literal

from mindroom.config.main import load_config
from mindroom.constants import exported_process_env, resolve_runtime_paths
from mindroom.file_locks import advisory_file_lock

__all__ = ["BundleInstallResult", "install_config_bundle"]

_METADATA = ".mindroom-bundle.json"


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


def _tree_digest(root: Path) -> str:
    """Hash names, modes and bytes, rejecting links and special files."""
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        mode = path.lstat().st_mode
        if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
            msg = f"Bundle contains a symlink or special file: {path}"
            raise ValueError(msg)
        if relative == _METADATA:
            continue
        digest.update(json.dumps([relative, mode]).encode())
        if stat.S_ISREG(mode):
            with path.open("rb") as stream:
                digest.update(hashlib.file_digest(stream, "sha256").digest())
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


def _unchanged_or_replaceable(target: Path, candidate_digest: str, *, force: bool) -> bool:
    if not target.exists():
        return False
    active_digest = _tree_digest(target)
    if active_digest == candidate_digest:
        return True
    try:
        metadata = json.loads((target / _METADATA).read_text())
    except (OSError, ValueError):
        metadata = None
    baseline = metadata.get("digest") if isinstance(metadata, dict) else None
    if not force and active_digest != baseline:
        msg = "Target contains authored edits or is unmanaged; use --force to replace it explicitly."
        raise ValueError(msg)
    return False


def _validate_target(target: Path, config: Path, *, initialize_only: bool, force: bool) -> Path:
    """Reject unsafe paths and conflicting install modes before filesystem mutation."""
    if config == Path(_METADATA):
        msg = f"{_METADATA} is reserved for bundle metadata."
        raise ValueError(msg)
    if config.is_absolute() or ".." in config.parts or not config.name:
        msg = "--config must be a relative file path inside the bundle."
        raise ValueError(msg)
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


def install_config_bundle(
    source: Path,
    target: Path,
    *,
    config: Path = Path("config.yaml"),
    initialize_only: bool = False,
    force: bool = False,
    expected_digest: str | None = None,
    process_env: dict[str, str] | None = None,
) -> BundleInstallResult:
    """Stage, validate and publish a tree; keep the prior tree at TARGET.previous.

    Directory replacement uses two renames, with a brief missing-target window.
    Installer calls serialize, but readers and external writers do not. Retry
    recovers interrupted renames; this is not a power-loss durability guarantee.
    Initialize-only preserves any existing directory without validating it.
    Force permits authored replacement, never invalid configuration.
    """
    target = _validate_target(target, config, initialize_only=initialize_only, force=force)
    with advisory_file_lock(target.with_name(f".{target.name}.lock")):
        _finish_rotation(target)
        if initialize_only and target.exists():
            return BundleInstallResult("initialized", target / config)
        source = source.expanduser().absolute()
        if source.is_symlink() or not source.is_dir():
            msg = "Bundle source must be a real directory, not a symlink."
            raise ValueError(msg)
        source = source.resolve()
        if source.is_relative_to(target) or target.is_relative_to(source):
            msg = "Bundle source and target must not overlap."
            raise ValueError(msg)
        _tree_digest(source)
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
            if _unchanged_or_replaceable(target, digest, force=force):
                return BundleInstallResult("unchanged", target / config, loaded.source_fingerprint, digest)
            (stage / _METADATA).write_text(json.dumps({"digest": digest}) + "\n")
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
            )
        finally:
            if stage.exists():
                _reject_managed_mounts(stage)
                shutil.rmtree(stage)
