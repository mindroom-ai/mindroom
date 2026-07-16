"""Vendored plugin install and update from GitHub commit archives."""

from __future__ import annotations

import io
import json
import shutil
import tarfile
import tempfile
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

import httpx

from mindroom.plugin_check import check_plugin

_PLUGIN_LOCK_FILENAME = ".mindroom-plugin.lock.json"
_DEFAULT_OWNER = "mindroom-ai"
_DEFAULT_REF = "HEAD"
_HTTP_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True, slots=True)
class _PluginSpec:
    """Parsed plugin install target."""

    repository: str
    ref: str
    directory_name: str


@dataclass(frozen=True, slots=True)
class _PluginLock:
    """Provenance record for one vendored plugin directory."""

    repository: str
    requested_ref: str
    commit: str
    installed_at: str


@dataclass(frozen=True, slots=True)
class InstallResult:
    """Successful install summary for one vendored plugin."""

    name: str
    directory: Path
    lock: _PluginLock
    has_pyproject: bool


@dataclass(frozen=True, slots=True)
class _UpdateResult:
    """Outcome of one plugin update attempt."""

    directory: Path
    previous_commit: str
    installed: InstallResult | None


def parse_plugin_spec(spec: str) -> _PluginSpec:
    """Parse ``NAME``, ``OWNER/REPO``, or ``OWNER/REPO@REF`` into one install target."""
    base, at, ref = spec.strip().partition("@")
    if at and not ref:
        msg = f"Invalid plugin spec (empty ref after '@'): {spec!r}"
        raise ValueError(msg)
    if not base:
        msg = f"Invalid plugin spec (empty repository): {spec!r}"
        raise ValueError(msg)
    if "/" in base:
        owner, _, name = base.partition("/")
        if not owner or not name or "/" in name:
            msg = f"Invalid plugin spec (expected OWNER/REPO): {spec!r}"
            raise ValueError(msg)
    else:
        owner, name = _DEFAULT_OWNER, base
    return _PluginSpec(
        repository=f"{owner}/{name}",
        ref=ref or _DEFAULT_REF,
        directory_name=name,
    )


def install_plugin(spec: _PluginSpec, plugins_dir: Path) -> InstallResult:
    """Download, validate, and vendor one plugin into ``plugins_dir``."""
    destination = plugins_dir / spec.directory_name
    if destination.exists():
        msg = f"Plugin directory already exists: {destination}. Use 'mindroom plugins update' instead."
        raise ValueError(msg)
    commit = _resolve_commit(spec.repository, spec.ref)
    lock = _PluginLock(
        repository=spec.repository,
        requested_ref=spec.ref,
        commit=commit,
        installed_at=datetime.now(UTC).isoformat(timespec="seconds"),
    )
    staged, result = _stage_validated_plugin(lock, plugins_dir)
    staged.rename(destination)
    return InstallResult(
        name=result.name,
        directory=destination,
        lock=lock,
        has_pyproject=result.has_pyproject,
    )


def update_plugin(directory: Path, ref: str | None = None) -> _UpdateResult:
    """Update one vendored plugin to the latest commit of its pinned reference."""
    lock = _read_plugin_lock(directory)
    requested_ref = ref or lock.requested_ref
    commit = _resolve_commit(lock.repository, requested_ref)
    if commit == lock.commit:
        if requested_ref != lock.requested_ref:
            _write_plugin_lock(directory, replace(lock, requested_ref=requested_ref))
        return _UpdateResult(directory=directory, previous_commit=lock.commit, installed=None)

    new_lock = _PluginLock(
        repository=lock.repository,
        requested_ref=requested_ref,
        commit=commit,
        installed_at=datetime.now(UTC).isoformat(timespec="seconds"),
    )
    staged, result = _stage_validated_plugin(new_lock, directory.parent)
    previous = directory.with_name(f".previous-{directory.name}")
    if previous.exists():
        shutil.rmtree(previous)
    directory.rename(previous)
    try:
        staged.rename(directory)
    except BaseException:
        previous.rename(directory)
        raise
    shutil.rmtree(previous)
    return _UpdateResult(
        directory=directory,
        previous_commit=lock.commit,
        installed=InstallResult(
            name=result.name,
            directory=directory,
            lock=new_lock,
            has_pyproject=result.has_pyproject,
        ),
    )


def _read_plugin_lock(directory: Path) -> _PluginLock:
    """Read the provenance lock for one vendored plugin directory."""
    lock_path = directory / _PLUGIN_LOCK_FILENAME
    if not lock_path.is_file():
        msg = f"Not a vendored plugin (missing {_PLUGIN_LOCK_FILENAME}): {directory}"
        raise ValueError(msg)
    payload = json.loads(lock_path.read_text(encoding="utf-8"))
    return _PluginLock(**payload)


def find_locked_plugin_dirs(plugins_dir: Path) -> tuple[Path, ...]:
    """Return every vendored plugin directory under ``plugins_dir``."""
    if not plugins_dir.is_dir():
        return ()
    return tuple(
        sorted(
            path
            for path in plugins_dir.iterdir()
            if path.is_dir() and not path.name.startswith(".") and (path / _PLUGIN_LOCK_FILENAME).is_file()
        ),
    )


@dataclass(frozen=True, slots=True)
class _StagedPlugin:
    """Validated staging-directory facts before the final rename."""

    name: str
    has_pyproject: bool


def _stage_validated_plugin(lock: _PluginLock, parent_dir: Path) -> tuple[Path, _StagedPlugin]:
    """Extract and strictly validate one plugin archive in a staging directory."""
    parent_dir.mkdir(parents=True, exist_ok=True)
    staged = Path(tempfile.mkdtemp(prefix=".staging-", dir=parent_dir))
    try:
        _extract_archive(_download_archive(lock.repository, lock.commit), staged)
        check_result = check_plugin(staged)
        _write_plugin_lock(staged, lock)
    except BaseException:
        shutil.rmtree(staged, ignore_errors=True)
        raise
    return staged, _StagedPlugin(
        name=check_result.name,
        has_pyproject=(staged / "pyproject.toml").is_file(),
    )


def _resolve_commit(repository: str, ref: str) -> str:
    """Resolve one git reference to an exact commit SHA via the GitHub API."""
    response = httpx.get(
        f"https://api.github.com/repos/{repository}/commits/{ref}",
        headers={"Accept": "application/vnd.github.sha"},
        timeout=_HTTP_TIMEOUT_SECONDS,
        follow_redirects=True,
    )
    if response.status_code != httpx.codes.OK:
        msg = f"Failed to resolve {repository}@{ref}: HTTP {response.status_code}"
        raise ValueError(msg)
    commit = response.text.strip()
    if not commit:
        msg = f"GitHub returned an empty commit for {repository}@{ref}"
        raise ValueError(msg)
    return commit


def _download_archive(repository: str, commit: str) -> bytes:
    """Download the tar.gz archive for one exact commit."""
    response = httpx.get(
        f"https://codeload.github.com/{repository}/tar.gz/{commit}",
        timeout=_HTTP_TIMEOUT_SECONDS,
        follow_redirects=True,
    )
    if response.status_code != httpx.codes.OK:
        msg = f"Failed to download archive for {repository}@{commit}: HTTP {response.status_code}"
        raise ValueError(msg)
    return response.content


def _extract_archive(data: bytes, destination: Path) -> None:
    """Extract one GitHub archive, stripping its single top-level directory."""
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        members = archive.getmembers()
        roots = {member.name.split("/", 1)[0] for member in members}
        if len(roots) != 1:
            msg = f"Unexpected archive layout: expected one top-level directory, found {sorted(roots)}"
            raise ValueError(msg)
        [root] = roots
        stripped = []
        for member in members:
            if member.name == root:
                continue
            member.name = member.name.removeprefix(f"{root}/")
            stripped.append(member)
        archive.extractall(destination, members=stripped, filter="data")


def _write_plugin_lock(directory: Path, lock: _PluginLock) -> None:
    payload = json.dumps(asdict(lock), indent=2) + "\n"
    (directory / _PLUGIN_LOCK_FILENAME).write_text(payload, encoding="utf-8")
