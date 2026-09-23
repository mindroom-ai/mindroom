"""Descriptor-safe deletion of exact worker-owned filesystem trees."""

from __future__ import annotations

import json
import os
import shutil
import stat
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

__all__ = ["open_worker_state_root", "remove_directory_tree_at"]


_DIRECTORY_OPEN_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_IDENTITY_FILE_OPEN_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
_MAX_IDENTITY_METADATA_BYTES = 4 * 1024 * 1024


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


def _read_identity(
    worker_fd: int,
    *,
    identity_path: tuple[str, ...],
    identity_field_path: tuple[str, ...],
) -> object:
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
                raise ValueError(msg) from exc
            descriptors.append(current_fd)
        payload = _decode_identity(_read_bounded_file(current_fd, identity_path[-1]))
        return _identity_value(payload, identity_field_path)
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


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
        shutil.rmtree(self.worker_name, dir_fd=self.parent_fd)


def remove_directory_tree_at(parent_fd: int, name: str) -> None:
    """Remove one descriptor-relative child tree without following symlinks."""
    descriptor = _open_directory_at(parent_fd, name)
    try:
        _require_current_directory(parent_fd, name, descriptor)
        shutil.rmtree(name, dir_fd=parent_fd)
    finally:
        with suppress(OSError):
            os.close(descriptor)


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
            )
            return
        descriptors.append(worker_fd)
        if expected_worker_key is not None:
            actual_worker_key = _read_identity(
                worker_fd,
                identity_path=identity_path,
                identity_field_path=identity_field_path,
            )
            if actual_worker_key != expected_worker_key:
                msg = f"Worker identity metadata does not match retirement key '{expected_worker_key}'."
                raise ValueError(msg)
        _require_current_directory(worker_parent_fd, worker_name, worker_fd)
        yield _BoundWorkerStateRoot(worker_parent_fd, worker_name, worker_fd)
    finally:
        for descriptor in reversed(descriptors):
            with suppress(OSError):
                os.close(descriptor)
