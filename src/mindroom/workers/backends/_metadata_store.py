"""Shared JSON metadata helpers for local worker backends."""

from __future__ import annotations

import json
import os
import stat
from contextlib import contextmanager, nullcontext, suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, cast

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path
    from threading import Lock


class _WorkerStatePathsLike(Protocol):
    """Filesystem paths required for worker metadata persistence."""

    root: Path
    metadata_dir: Path
    metadata_file: Path


_DIRECTORY_OPEN_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_IDENTITY_FILE_OPEN_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
_MAX_IDENTITY_METADATA_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class _DirectoryBinding:
    parent_fd: int
    name: str
    descriptor: int
    device: int
    inode: int


@dataclass(frozen=True, slots=True)
class _TrustedRootBinding:
    path: Path
    descriptor: int
    device: int
    inode: int


@dataclass(slots=True)
class _BoundWorkerStateRoot:
    """One exact worker-state target pinned beneath an opened trusted root."""

    _trusted_root: _TrustedRootBinding | None
    _bindings: tuple[_DirectoryBinding, ...]
    _worker_parent_fd: int | None
    _worker_name: str
    _worker_fd: int | None
    _absent_parent_fd: int | None = None
    _absent_name: str | None = None
    _absent_trusted_root: Path | None = None

    def remove(self) -> None:
        """Remove the pinned worker tree without recursively resolving its pathname again."""
        _validate_trusted_root(self._trusted_root)
        _validate_bindings(self._bindings)
        if self._worker_fd is None:
            if self._absent_trusted_root is not None:
                try:
                    self._absent_trusted_root.lstat()
                except FileNotFoundError:
                    return
                msg = "Worker state root changed during retirement."
                raise ValueError(msg)
            assert self._absent_parent_fd is not None
            assert self._absent_name is not None
            try:
                os.stat(self._absent_name, dir_fd=self._absent_parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                _validate_trusted_root(self._trusted_root)
                return
            msg = "Worker state root changed during retirement."
            raise ValueError(msg)

        assert self._worker_parent_fd is not None
        _remove_directory_contents(self._worker_fd)
        _validate_trusted_root(self._trusted_root)
        _validate_bindings(self._bindings)
        os.rmdir(self._worker_name, dir_fd=self._worker_parent_fd)
        _validate_trusted_root(self._trusted_root)
        _validate_bindings(self._bindings[:-1])
        try:
            os.stat(self._worker_name, dir_fd=self._worker_parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            _validate_trusted_root(self._trusted_root)
            return
        msg = "Worker state root changed during retirement."
        raise ValueError(msg)


def list_worker_state_paths[PathsT](
    workers_root: Path,
    *,
    state_paths_from_root: Callable[[Path], PathsT],
) -> list[PathsT]:
    """List worker state paths rooted under one workers directory."""
    if not workers_root.exists():
        return []

    return [
        state_paths_from_root(metadata_file.parents[1])
        for metadata_file in sorted(workers_root.glob("*/metadata/worker.json"))
    ]


def load_worker_metadata[MetadataT](
    paths: _WorkerStatePathsLike,
    *,
    metadata_type: type[MetadataT],
) -> MetadataT | None:
    """Load one worker metadata JSON document into the requested dataclass type."""
    if not paths.metadata_file.exists():
        return None

    try:
        with paths.metadata_file.open(encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None

    try:
        return metadata_type(**data)
    except TypeError:
        return None


def save_worker_metadata(
    paths: _WorkerStatePathsLike,
    metadata: object,
    *,
    ensure_root: bool = False,
    lock: Lock | None = None,
) -> None:
    """Persist one worker metadata dataclass to JSON."""
    if ensure_root:
        paths.root.mkdir(parents=True, exist_ok=True)
    paths.metadata_dir.mkdir(parents=True, exist_ok=True)

    lock_context = nullcontext() if lock is None else lock
    with lock_context, paths.metadata_file.open("w", encoding="utf-8") as f:
        json.dump(vars(metadata), f, sort_keys=True)


def _validate_segment(segment: str) -> None:
    if not segment or segment in {".", ".."} or "/" in segment:
        msg = f"Worker state path segment is invalid: {segment!r}"
        raise ValueError(msg)


def _open_directory_at(parent_fd: int, name: str) -> int:
    _validate_segment(name)
    try:
        descriptor = os.open(name, _DIRECTORY_OPEN_FLAGS, dir_fd=parent_fd)
    except OSError as exc:
        metadata = None
        with suppress(OSError):
            metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if metadata is not None and stat.S_ISLNK(metadata.st_mode):
            msg = f"Worker state path cannot contain a symbolic link: {name}"
            raise ValueError(msg) from exc
        raise
    metadata = os.fstat(descriptor)
    if not stat.S_ISDIR(metadata.st_mode):
        os.close(descriptor)
        msg = f"Worker state path must contain only directories: {name}"
        raise ValueError(msg)
    return descriptor


def _binding(parent_fd: int, name: str, descriptor: int) -> _DirectoryBinding:
    metadata = os.fstat(descriptor)
    return _DirectoryBinding(
        parent_fd=parent_fd,
        name=name,
        descriptor=descriptor,
        device=metadata.st_dev,
        inode=metadata.st_ino,
    )


def _trusted_root_binding(path: Path, descriptor: int) -> _TrustedRootBinding:
    metadata = os.fstat(descriptor)
    return _TrustedRootBinding(
        path=path,
        descriptor=descriptor,
        device=metadata.st_dev,
        inode=metadata.st_ino,
    )


def _validate_trusted_root(binding: _TrustedRootBinding | None) -> None:
    if binding is None:
        return
    try:
        metadata = binding.path.stat(follow_symlinks=False)
    except OSError as exc:
        msg = "Worker state root changed during retirement."
        raise ValueError(msg) from exc
    descriptor_metadata = os.fstat(binding.descriptor)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_dev != binding.device
        or metadata.st_ino != binding.inode
        or descriptor_metadata.st_dev != binding.device
        or descriptor_metadata.st_ino != binding.inode
    ):
        msg = "Worker state root changed during retirement."
        raise ValueError(msg)


def _validate_bindings(bindings: tuple[_DirectoryBinding, ...]) -> None:
    for binding in bindings:
        try:
            metadata = os.stat(binding.name, dir_fd=binding.parent_fd, follow_symlinks=False)
        except OSError as exc:
            msg = "Worker state root changed during retirement."
            raise ValueError(msg) from exc
        descriptor_metadata = os.fstat(binding.descriptor)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_dev != binding.device
            or metadata.st_ino != binding.inode
            or descriptor_metadata.st_dev != binding.device
            or descriptor_metadata.st_ino != binding.inode
        ):
            msg = "Worker state root changed during retirement."
            raise ValueError(msg)


@contextmanager
def _open_identity_directory(worker_fd: int, directory_parts: tuple[str, ...]) -> Iterator[int]:
    descriptors: list[int] = []
    current_fd = worker_fd
    try:
        for segment in directory_parts:
            try:
                current_fd = _open_directory_at(current_fd, segment)
            except FileNotFoundError as exc:
                msg = "Worker identity metadata is missing."
                raise ValueError(msg) from exc
            descriptors.append(current_fd)
        yield current_fd
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _read_bounded_identity_file(parent_fd: int, filename: str) -> bytes:
    _validate_segment(filename)
    try:
        identity_fd = os.open(filename, _IDENTITY_FILE_OPEN_FLAGS, dir_fd=parent_fd)
    except FileNotFoundError as exc:
        msg = "Worker identity metadata is missing."
        raise ValueError(msg) from exc
    try:
        metadata = os.fstat(identity_fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > _MAX_IDENTITY_METADATA_BYTES:
            msg = "Worker identity metadata must be a bounded regular file."
            raise ValueError(msg)
        chunks: list[bytes] = []
        remaining = _MAX_IDENTITY_METADATA_BYTES + 1
        while remaining:
            chunk = os.read(identity_fd, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw_payload = b"".join(chunks)
        if len(raw_payload) > _MAX_IDENTITY_METADATA_BYTES:
            msg = "Worker identity metadata must be a bounded regular file."
            raise ValueError(msg)
        return raw_payload
    finally:
        os.close(identity_fd)


def _read_identity_payload(
    worker_fd: int,
    *,
    identity_path: tuple[str, ...],
) -> dict[str, object]:
    if not identity_path:
        msg = "Worker identity metadata path is missing."
        raise ValueError(msg)
    with _open_identity_directory(worker_fd, identity_path[:-1]) as identity_parent_fd:
        raw_payload = _read_bounded_identity_file(identity_parent_fd, identity_path[-1])
    try:
        payload = json.loads(raw_payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        msg = "Worker identity metadata must contain a valid JSON object."
        raise ValueError(msg) from exc
    if not isinstance(payload, dict):
        msg = "Worker identity metadata must contain a valid JSON object."
        raise TypeError(msg)
    return cast("dict[str, object]", payload)


def _identity_value(payload: dict[str, object], field_path: tuple[str, ...]) -> object:
    value: object = payload
    for field in field_path:
        if not isinstance(value, dict) or field not in value:
            msg = "Worker identity metadata is missing its exact worker key."
            raise ValueError(msg)
        value = cast("dict[str, object]", value)[field]
    return value


def _remove_directory_contents(directory_fd: int) -> None:
    for name in os.listdir(directory_fd):
        try:
            metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            continue
        if not stat.S_ISDIR(metadata.st_mode):
            os.unlink(name, dir_fd=directory_fd)
            continue
        child_fd = _open_directory_at(directory_fd, name)
        try:
            child_metadata = os.fstat(child_fd)
            if child_metadata.st_dev != metadata.st_dev or child_metadata.st_ino != metadata.st_ino:
                msg = "Worker state root changed during retirement."
                raise ValueError(msg)
            _remove_directory_contents(child_fd)
            current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if current.st_dev != metadata.st_dev or current.st_ino != metadata.st_ino:
                msg = "Worker state root changed during retirement."
                raise ValueError(msg)
            os.rmdir(name, dir_fd=directory_fd)
        finally:
            os.close(child_fd)


@contextmanager
def open_worker_state_root(
    trusted_root: Path,
    *,
    workers_subpath: tuple[str, ...],
    worker_name: str,
    expected_worker_key: str | None = None,
    identity_path: tuple[str, ...] = (),
    identity_field_path: tuple[str, ...] = (),
) -> Iterator[_BoundWorkerStateRoot]:
    """Pin one worker leaf below a trusted root and optionally validate its persisted identity."""
    _validate_segment(worker_name)
    descriptors: list[int] = []
    bindings: list[_DirectoryBinding] = []
    trusted_root = trusted_root.expanduser()
    try:
        root_fd = os.open(trusted_root, _DIRECTORY_OPEN_FLAGS)
    except FileNotFoundError:
        yield _BoundWorkerStateRoot(
            _trusted_root=None,
            _bindings=(),
            _worker_parent_fd=None,
            _worker_name=worker_name,
            _worker_fd=None,
            _absent_trusted_root=trusted_root,
        )
        return
    descriptors.append(root_fd)
    trusted_root_binding = _trusted_root_binding(trusted_root, root_fd)
    _validate_trusted_root(trusted_root_binding)
    current_fd = root_fd
    try:
        for segment in workers_subpath:
            try:
                child_fd = _open_directory_at(current_fd, segment)
            except FileNotFoundError:
                target = _BoundWorkerStateRoot(
                    _trusted_root=trusted_root_binding,
                    _bindings=tuple(bindings),
                    _worker_parent_fd=None,
                    _worker_name=worker_name,
                    _worker_fd=None,
                    _absent_parent_fd=current_fd,
                    _absent_name=segment,
                )
                yield target
                return
            descriptors.append(child_fd)
            bindings.append(_binding(current_fd, segment, child_fd))
            current_fd = child_fd
        worker_parent_fd = current_fd
        try:
            worker_fd = _open_directory_at(worker_parent_fd, worker_name)
        except FileNotFoundError:
            target = _BoundWorkerStateRoot(
                _trusted_root=trusted_root_binding,
                _bindings=tuple(bindings),
                _worker_parent_fd=None,
                _worker_name=worker_name,
                _worker_fd=None,
                _absent_parent_fd=worker_parent_fd,
                _absent_name=worker_name,
            )
            yield target
            return
        descriptors.append(worker_fd)
        bindings.append(_binding(worker_parent_fd, worker_name, worker_fd))
        identity_payload = None
        if expected_worker_key is not None:
            identity_payload = _read_identity_payload(worker_fd, identity_path=identity_path)
            if _identity_value(identity_payload, identity_field_path) != expected_worker_key:
                msg = f"Worker identity metadata does not match retirement key '{expected_worker_key}'."
                raise ValueError(msg)
        yield _BoundWorkerStateRoot(
            _trusted_root=trusted_root_binding,
            _bindings=tuple(bindings),
            _worker_parent_fd=worker_parent_fd,
            _worker_name=worker_name,
            _worker_fd=worker_fd,
        )
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
