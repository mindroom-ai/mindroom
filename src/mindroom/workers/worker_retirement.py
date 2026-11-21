"""Descriptor-safe deletion of exact worker-owned filesystem trees."""

from __future__ import annotations

import errno
import json
import logging
import os
import shutil
import stat
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast
from uuid import uuid4

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

__all__ = [
    "open_worker_state_root",
    "open_workers_root",
    "read_worker_identity",
    "read_worker_marker",
    "remove_directory_tree_at",
    "require_worker_identity",
    "worker_state_directories",
    "write_worker_marker",
]


_DIRECTORY_OPEN_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_IDENTITY_FILE_OPEN_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
_MAX_IDENTITY_METADATA_BYTES = 4 * 1024 * 1024
_RETIREMENT_MARKER_SUFFIX = ".retiring"

logger = logging.getLogger(__name__)


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
    if stat.S_ISDIR(metadata.st_mode):
        return descriptor
    os.close(descriptor)
    msg = f"Worker state path must contain only directories: {name}"
    raise ValueError(msg)


def _read_bounded_file(parent_fd: int, filename: str) -> bytes:
    _validate_segment(filename)
    identity_fd = os.open(filename, _IDENTITY_FILE_OPEN_FLAGS, dir_fd=parent_fd)
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


def _decode_identity(raw_payload: bytes) -> dict[str, object]:
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


class _MissingWorkerIdentityError(ValueError):
    """The identity file or one of its parent directories is absent."""


def read_worker_identity(
    worker_fd: int,
    *,
    identity_path: tuple[str, ...],
    identity_field_path: tuple[str, ...],
) -> object:
    """Read one bounded identity field below an open worker tree without following symlinks."""
    if not identity_path or not identity_field_path:
        msg = "Worker identity metadata path is missing."
        raise ValueError(msg)
    descriptors: list[int] = []
    try:
        current_fd = worker_fd
        for segment in identity_path[:-1]:
            try:
                current_fd = _open_directory_at(current_fd, segment)
            except FileNotFoundError as exc:
                msg = "Worker identity metadata is missing."
                raise _MissingWorkerIdentityError(msg) from exc
            descriptors.append(current_fd)
        try:
            payload = _decode_identity(_read_bounded_file(current_fd, identity_path[-1]))
        except FileNotFoundError as exc:
            msg = "Worker identity metadata is missing."
            raise _MissingWorkerIdentityError(msg) from exc
        return _identity_value(payload, identity_field_path)
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def require_worker_identity(
    worker_fd: int,
    worker_key: str,
    *,
    identity_path: tuple[str, ...],
    identity_field_path: tuple[str, ...],
    allow_missing: bool = False,
) -> None:
    """Check the identity, optionally allowing a manifest that has not been written yet."""
    try:
        identity = read_worker_identity(worker_fd, identity_path=identity_path, identity_field_path=identity_field_path)
    except _MissingWorkerIdentityError:
        if allow_missing:
            return
        raise
    if identity != worker_key:
        msg = "Worker identity metadata does not match the retirement key."
        raise ValueError(msg)


def read_worker_marker(workers_fd: int, filename: str) -> str | None:
    """Read a bounded regular file beside a worker's state without following links."""
    try:
        return _read_bounded_file(workers_fd, filename).decode("utf-8")
    except FileNotFoundError:
        return None


def write_worker_marker(workers_fd: int, filename: str, key: str) -> None:
    """Publish a marker beside the worker atomically, without opening the destination for writing."""
    _validate_segment(filename)
    temporary = f".retiring-{uuid4().hex}"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=workers_fd)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as marker:
            marker.write(key)
        os.replace(temporary, filename, src_dir_fd=workers_fd, dst_dir_fd=workers_fd)
    finally:
        with suppress(FileNotFoundError):
            os.unlink(temporary, dir_fd=workers_fd)


def _require_current_directory(parent_fd: int, name: str, descriptor: int) -> None:
    try:
        named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError as exc:
        msg = "Worker state root changed during retirement."
        raise ValueError(msg) from exc
    opened = os.fstat(descriptor)
    if not stat.S_ISDIR(named.st_mode) or named.st_dev != opened.st_dev or named.st_ino != opened.st_ino:
        msg = "Worker state root changed during retirement."
        raise ValueError(msg)


@dataclass(slots=True)
class _BoundWorkerStateRoot:
    parent_fd: int | None
    worker_name: str
    worker_fd: int | None
    absent_root: Path | None = None
    absent_name: str | None = None
    workers_fd: int | None = None

    @property
    def exists(self) -> bool:
        """Return whether the validated worker tree is present."""
        return self.worker_fd is not None

    def retirement_key(self) -> str | None:
        """Read the key from the unfinished retirement marker, if any."""
        if self.workers_fd is None:
            return None
        return read_worker_marker(self.workers_fd, self.worker_name + _RETIREMENT_MARKER_SUFFIX)

    @contextmanager
    def retire(self, worker_key: str, path: tuple[str, ...], field_path: tuple[str, ...]) -> Iterator[None]:
        """Validate ownership before backend cleanup, then mark, remove, and unmark the tree."""
        resumed = self.retirement_key() == worker_key
        if not resumed and self.worker_fd is not None:
            require_worker_identity(self.worker_fd, worker_key, identity_path=path, identity_field_path=field_path)
        yield
        marker = self.worker_name + _RETIREMENT_MARKER_SUFFIX
        if not resumed and self.exists and self.workers_fd is not None:
            try:
                write_worker_marker(self.workers_fd, marker, worker_key)
            except OSError as exc:
                if exc.errno not in (errno.ENOSPC, errno.EDQUOT):
                    raise
                # Removing the cache frees space; only recovery from an interruption needs the marker.
                logger.warning("worker_retirement_marker_allocation_failed", extra={"errno": exc.errno})
        self.remove()
        if self.workers_fd is not None:
            with suppress(FileNotFoundError):
                os.unlink(marker, dir_fd=self.workers_fd)

    def remove(self) -> None:
        """Remove the validated worker tree or confirm that it is still absent."""
        if self.worker_fd is None:
            if self.absent_root is not None:
                try:
                    self.absent_root.lstat()
                except FileNotFoundError:
                    return
                msg = "Worker state root changed during retirement."
                raise ValueError(msg)
            assert self.parent_fd is not None
            assert self.absent_name is not None
            try:
                os.stat(self.absent_name, dir_fd=self.parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                return
            msg = "Worker state root changed during retirement."
            raise ValueError(msg)

        assert self.parent_fd is not None
        _require_current_directory(self.parent_fd, self.worker_name, self.worker_fd)
        _remove_tree(self.parent_fd, self.worker_name)


def remove_directory_tree_at(parent_fd: int, name: str) -> None:
    """Remove one descriptor-relative child tree without following symlinks."""
    descriptor = _open_directory_at(parent_fd, name)
    try:
        _require_current_directory(parent_fd, name, descriptor)
        _remove_tree(parent_fd, name)
    finally:
        with suppress(OSError):
            os.close(descriptor)


def _remove_tree(parent_fd: int, name: str) -> None:
    """Remove one child tree, first letting its owner change read-only directories such as Go's module cache."""
    for _path, _dirnames, _filenames, directory_fd in os.fwalk(name, dir_fd=parent_fd, follow_symlinks=False):
        mode = stat.S_IMODE(os.fstat(directory_fd).st_mode)
        if mode & stat.S_IRWXU != stat.S_IRWXU:
            # A directory owned by someone else keeps its mode, and rmtree removes it as before when it is empty.
            with suppress(PermissionError):
                os.fchmod(directory_fd, mode | stat.S_IRWXU)
    shutil.rmtree(name, dir_fd=parent_fd)


@contextmanager
def open_workers_root(trusted_root: Path, workers_subpath: tuple[str, ...]) -> Iterator[int]:
    """Open the directory that contains worker state without following symlinks."""
    descriptors = [os.open(trusted_root.expanduser(), _DIRECTORY_OPEN_FLAGS)]
    try:
        for segment in workers_subpath:
            descriptors.append(_open_directory_at(descriptors[-1], segment))
        yield descriptors[-1]
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def worker_state_directories(trusted_root: Path, workers_subpath: tuple[str, ...]) -> set[str]:
    """List worker trees and remove regular retirement markers whose trees are already gone."""
    try:
        with open_workers_root(trusted_root, workers_subpath) as workers_fd, os.scandir(workers_fd) as entries:
            by_name = {entry.name: entry for entry in entries}
            for name, entry in by_name.items():
                if (
                    name.endswith(_RETIREMENT_MARKER_SUFFIX)
                    and name.removesuffix(_RETIREMENT_MARKER_SUFFIX) not in by_name
                    and entry.is_file(follow_symlinks=False)
                ):
                    with suppress(OSError):
                        os.unlink(name, dir_fd=workers_fd)
            return {name for name, entry in by_name.items() if entry.is_dir(follow_symlinks=False)}
    except (OSError, ValueError):
        return set()


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
    """Open and validate one exact worker tree below a trusted root."""
    _validate_segment(worker_name)
    descriptors: list[int] = []
    try:
        try:
            current_fd = os.open(trusted_root.expanduser(), _DIRECTORY_OPEN_FLAGS)
        except FileNotFoundError:
            yield _BoundWorkerStateRoot(None, worker_name, None, absent_root=trusted_root.expanduser())
            return
        descriptors.append(current_fd)
        for segment in workers_subpath:
            try:
                current_fd = _open_directory_at(current_fd, segment)
            except FileNotFoundError:
                yield _BoundWorkerStateRoot(
                    descriptors[-1],
                    worker_name,
                    None,
                    absent_name=segment,
                )
                return
            descriptors.append(current_fd)
        worker_parent_fd = current_fd
        try:
            worker_fd = _open_directory_at(worker_parent_fd, worker_name)
        except FileNotFoundError:
            yield _BoundWorkerStateRoot(
                worker_parent_fd,
                worker_name,
                None,
                absent_name=worker_name,
                workers_fd=worker_parent_fd,
            )
            return
        descriptors.append(worker_fd)
        if expected_worker_key is not None:
            require_worker_identity(
                worker_fd,
                expected_worker_key,
                identity_path=identity_path,
                identity_field_path=identity_field_path,
            )
        _require_current_directory(worker_parent_fd, worker_name, worker_fd)
        yield _BoundWorkerStateRoot(worker_parent_fd, worker_name, worker_fd, workers_fd=worker_parent_fd)
    finally:
        for descriptor in reversed(descriptors):
            with suppress(OSError):
                os.close(descriptor)
