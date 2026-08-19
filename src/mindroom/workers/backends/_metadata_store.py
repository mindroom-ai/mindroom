"""Shared JSON metadata helpers for local worker backends."""

from __future__ import annotations

import json
import os
import stat
from contextlib import ExitStack, contextmanager, nullcontext, suppress
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
_RETIREMENT_IDENTITY_MARKER = ".retirement-identity."


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


@dataclass(frozen=True, slots=True)
class _IdentityFileBinding:
    device: int
    inode: int


@dataclass(frozen=True, slots=True)
class _WorkerIdentity:
    path: tuple[str, ...]
    binding: _IdentityFileBinding
    worker_key: str
    field_path: tuple[str, ...]


@dataclass(slots=True)
class _RemovalFrame:
    descriptor: int
    binding: _DirectoryBinding | None
    relative_path: tuple[str, ...]
    remaining_names: list[str]


@dataclass(slots=True)
class _BoundWorkerStateRoot:
    """One exact worker-state target pinned beneath an opened trusted root."""

    _trusted_root: _TrustedRootBinding | None
    _bindings: tuple[_DirectoryBinding, ...]
    _worker_parent_fd: int | None
    _worker_name: str
    _worker_fd: int | None
    _identity: _WorkerIdentity | None = None
    _retirement_identity_name: str | None = None
    _retirement_identity: _IdentityFileBinding | None = None
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
                _fsync_and_require_name_absent(self._absent_parent_fd, self._absent_name)
                _validate_trusted_root(self._trusted_root)
                self._remove_retirement_identity(self._absent_parent_fd)
                return
            msg = "Worker state root changed during retirement."
            raise ValueError(msg)

        assert self._worker_parent_fd is not None
        preserved_path = self._identity.path if self._identity is not None and self._retirement_identity is None else ()
        _remove_directory_contents(self._worker_fd, preserved_path=preserved_path)
        _validate_trusted_root(self._trusted_root)
        _validate_bindings(self._bindings)
        if self._retirement_identity is None and self._identity is not None:
            self._retirement_identity_name, self._retirement_identity = _stage_retirement_identity(
                self._worker_fd,
                worker_parent_fd=self._worker_parent_fd,
                worker_binding=self._bindings[-1],
                identity=self._identity,
            )
        self._validate_retirement_identity(self._worker_parent_fd)
        _validate_trusted_root(self._trusted_root)
        _validate_bindings(self._bindings)
        os.rmdir(self._worker_name, dir_fd=self._worker_parent_fd)
        _validate_trusted_root(self._trusted_root)
        _validate_bindings(self._bindings[:-1])
        try:
            os.stat(self._worker_name, dir_fd=self._worker_parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            _validate_trusted_root(self._trusted_root)
            _fsync_and_require_name_absent(self._worker_parent_fd, self._worker_name)
            _validate_trusted_root(self._trusted_root)
            self._remove_retirement_identity(self._worker_parent_fd)
            return
        msg = "Worker state root changed during retirement."
        raise ValueError(msg)

    def _validate_retirement_identity(self, parent_fd: int) -> None:
        if self._retirement_identity_name is None or self._retirement_identity is None:
            return
        _validate_identity_file_binding(
            parent_fd,
            self._retirement_identity_name,
            self._retirement_identity,
        )

    def _remove_retirement_identity(self, parent_fd: int) -> None:
        if self._retirement_identity_name is None or self._retirement_identity is None:
            return
        self._validate_retirement_identity(parent_fd)
        os.unlink(self._retirement_identity_name, dir_fd=parent_fd)
        os.fsync(parent_fd)


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
    with ExitStack() as cleanup:
        cleanup.callback(os.close, descriptor)
        metadata = os.fstat(descriptor)
        if not stat.S_ISDIR(metadata.st_mode):
            msg = f"Worker state path must contain only directories: {name}"
            raise ValueError(msg)
        cleanup.pop_all()
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


def _read_bounded_identity_descriptor(identity_fd: int) -> tuple[bytes, _IdentityFileBinding]:
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
    return raw_payload, _IdentityFileBinding(
        device=metadata.st_dev,
        inode=metadata.st_ino,
    )


def _read_bounded_identity_file(parent_fd: int, filename: str) -> tuple[bytes, _IdentityFileBinding]:
    _validate_segment(filename)
    try:
        identity_fd = os.open(filename, _IDENTITY_FILE_OPEN_FLAGS, dir_fd=parent_fd)
    except FileNotFoundError as exc:
        msg = "Worker identity metadata is missing."
        raise ValueError(msg) from exc
    try:
        return _read_bounded_identity_descriptor(identity_fd)
    finally:
        os.close(identity_fd)


def _decode_identity(raw_payload: bytes, *, description: str) -> dict[str, object]:
    try:
        payload = json.loads(raw_payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        msg = f"{description} must contain a valid JSON object."
        raise ValueError(msg) from exc
    if not isinstance(payload, dict):
        msg = f"{description} must contain a valid JSON object."
        raise TypeError(msg)
    return cast("dict[str, object]", payload)


def _read_identity(
    worker_fd: int,
    *,
    identity_path: tuple[str, ...],
) -> tuple[dict[str, object], _IdentityFileBinding]:
    if not identity_path:
        msg = "Worker identity metadata path is missing."
        raise ValueError(msg)
    with _open_identity_directory(worker_fd, identity_path[:-1]) as identity_parent_fd:
        raw_payload, binding = _read_bounded_identity_file(identity_parent_fd, identity_path[-1])
    return _decode_identity(raw_payload, description="Worker identity metadata"), binding


def _identity_value(payload: dict[str, object], field_path: tuple[str, ...]) -> object:
    value: object = payload
    for field in field_path:
        if not isinstance(value, dict) or field not in value:
            msg = "Worker identity metadata is missing its exact worker key."
            raise ValueError(msg)
        value = cast("dict[str, object]", value)[field]
    return value


def _binding_is_current(binding: _DirectoryBinding) -> bool:
    metadata = os.stat(binding.name, dir_fd=binding.parent_fd, follow_symlinks=False)
    descriptor_metadata = os.fstat(binding.descriptor)
    return (
        stat.S_ISDIR(metadata.st_mode)
        and metadata.st_dev == binding.device
        and metadata.st_ino == binding.inode
        and descriptor_metadata.st_dev == binding.device
        and descriptor_metadata.st_ino == binding.inode
    )


def _path_is_identity_directory(relative_path: tuple[str, ...], identity_path: tuple[str, ...]) -> bool:
    return (
        bool(identity_path)
        and len(relative_path) < len(identity_path)
        and identity_path[: len(relative_path)] == relative_path
    )


def _require_opened_directory_matches(metadata: os.stat_result, descriptor: int) -> None:
    opened = os.fstat(descriptor)
    if opened.st_dev != metadata.st_dev or opened.st_ino != metadata.st_ino:
        msg = "Worker state root changed during retirement."
        raise ValueError(msg)


def _open_removal_frame(
    parent: _RemovalFrame,
    *,
    name: str,
    relative_path: tuple[str, ...],
    metadata: os.stat_result,
) -> _RemovalFrame:
    with ExitStack() as cleanup:
        child_fd = _open_directory_at(parent.descriptor, name)
        cleanup.callback(os.close, child_fd)
        _require_opened_directory_matches(metadata, child_fd)
        frame = _RemovalFrame(
            descriptor=child_fd,
            binding=_binding(parent.descriptor, name, child_fd),
            relative_path=relative_path,
            remaining_names=os.listdir(child_fd),  # noqa: PTH208 -- Path cannot enumerate an fd.
        )
        cleanup.pop_all()
    return frame


def _finish_removal_frame(frame: _RemovalFrame, *, preserved_path: tuple[str, ...]) -> None:
    if frame.binding is None:
        return
    try:
        if _path_is_identity_directory(frame.relative_path, preserved_path):
            return
        if not _binding_is_current(frame.binding):
            msg = "Worker state root changed during retirement."
            raise ValueError(msg)
        os.rmdir(frame.binding.name, dir_fd=frame.binding.parent_fd)
    finally:
        os.close(frame.descriptor)


def _remove_directory_contents(directory_fd: int, *, preserved_path: tuple[str, ...]) -> None:
    stack = [
        _RemovalFrame(
            descriptor=directory_fd,
            binding=None,
            relative_path=(),
            remaining_names=os.listdir(directory_fd),
        ),
    ]
    try:
        while stack:
            frame = stack[-1]
            if frame.remaining_names:
                name = frame.remaining_names.pop()
                relative_path = (*frame.relative_path, name)
                try:
                    metadata = os.stat(name, dir_fd=frame.descriptor, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if not stat.S_ISDIR(metadata.st_mode):
                    if relative_path != preserved_path:
                        os.unlink(name, dir_fd=frame.descriptor)
                    continue
                stack.append(
                    _open_removal_frame(
                        frame,
                        name=name,
                        relative_path=relative_path,
                        metadata=metadata,
                    ),
                )
                continue

            _finish_removal_frame(stack.pop(), preserved_path=preserved_path)
    finally:
        for frame in reversed(stack):
            if frame.binding is not None:
                os.close(frame.descriptor)


def _retirement_identity_prefix(worker_name: str) -> str:
    return f".{worker_name}{_RETIREMENT_IDENTITY_MARKER}"


def _retirement_identity_name(worker_binding: _DirectoryBinding) -> str:
    return f"{_retirement_identity_prefix(worker_binding.name)}{worker_binding.device:x}.{worker_binding.inode:x}"


def _find_retirement_identity_name(parent_fd: int, worker_name: str) -> str | None:
    prefix = _retirement_identity_prefix(worker_name)
    names = [name for name in os.listdir(parent_fd) if name.startswith(prefix)]
    if len(names) > 1:
        msg = "Worker retirement identity metadata is ambiguous."
        raise ValueError(msg)
    if not names:
        return None
    name = names[0]
    suffix = name.removeprefix(prefix).split(".")
    try:
        valid_suffix = len(suffix) == 2 and all(int(value, 16) >= 0 for value in suffix)
    except ValueError:
        valid_suffix = False
    if not valid_suffix:
        msg = "Worker retirement identity metadata path is invalid."
        raise ValueError(msg)
    return name


def _validate_identity_file_binding(
    parent_fd: int,
    filename: str,
    binding: _IdentityFileBinding,
) -> None:
    try:
        metadata = os.stat(filename, dir_fd=parent_fd, follow_symlinks=False)
    except OSError as exc:
        msg = "Worker retirement identity metadata changed during retirement."
        raise ValueError(msg) from exc
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_dev != binding.device or metadata.st_ino != binding.inode:
        msg = "Worker retirement identity metadata changed during retirement."
        raise ValueError(msg)


def _validate_identity_descriptor_binding(identity_fd: int, binding: _IdentityFileBinding) -> None:
    metadata = os.fstat(identity_fd)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_dev != binding.device or metadata.st_ino != binding.inode:
        msg = "Worker retirement identity metadata changed during retirement."
        raise ValueError(msg)


def _sync_retirement_identity(
    parent_fd: int,
    filename: str,
    *,
    expected_worker_key: str,
    identity_field_path: tuple[str, ...],
    expected_binding: _IdentityFileBinding | None = None,
) -> _IdentityFileBinding:
    """Validate and durably publish one retirement identity through one bound descriptor."""
    _validate_segment(filename)
    try:
        identity_fd = os.open(filename, _IDENTITY_FILE_OPEN_FLAGS, dir_fd=parent_fd)
    except OSError as exc:
        msg = "Worker retirement identity metadata changed during retirement."
        raise ValueError(msg) from exc
    try:
        raw_payload, binding = _read_bounded_identity_descriptor(identity_fd)
        if expected_binding is not None and binding != expected_binding:
            msg = "Worker retirement identity metadata changed during retirement."
            raise ValueError(msg)
        payload = _decode_identity(raw_payload, description="Worker retirement identity metadata")
        if _identity_value(payload, identity_field_path) != expected_worker_key:
            msg = f"Worker retirement identity metadata does not match retirement key '{expected_worker_key}'."
            raise ValueError(msg)
        os.fsync(identity_fd)
        _validate_identity_descriptor_binding(identity_fd, binding)
        _validate_identity_file_binding(parent_fd, filename, binding)
        os.fsync(parent_fd)
        _validate_identity_descriptor_binding(identity_fd, binding)
        _validate_identity_file_binding(parent_fd, filename, binding)
        return binding
    finally:
        os.close(identity_fd)


def _fsync_and_require_name_absent(parent_fd: int, name: str) -> None:
    os.fsync(parent_fd)
    try:
        os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    msg = "Worker state root changed during retirement."
    raise ValueError(msg)


def _load_retirement_identity(
    parent_fd: int,
    *,
    worker_name: str,
    expected_worker_key: str | None,
    identity_field_path: tuple[str, ...],
) -> tuple[str | None, _IdentityFileBinding | None]:
    if expected_worker_key is None:
        return None, None
    name = _find_retirement_identity_name(parent_fd, worker_name)
    if name is None:
        return None, None
    return (
        name,
        _sync_retirement_identity(
            parent_fd,
            name,
            expected_worker_key=expected_worker_key,
            identity_field_path=identity_field_path,
        ),
    )


def _load_canonical_identity(
    worker_fd: int,
    *,
    expected_worker_key: str,
    identity_path: tuple[str, ...],
    identity_field_path: tuple[str, ...],
) -> _WorkerIdentity:
    payload, binding = _read_identity(worker_fd, identity_path=identity_path)
    if _identity_value(payload, identity_field_path) != expected_worker_key:
        msg = f"Worker identity metadata does not match retirement key '{expected_worker_key}'."
        raise ValueError(msg)
    return _WorkerIdentity(
        path=identity_path,
        binding=binding,
        worker_key=expected_worker_key,
        field_path=identity_field_path,
    )


def _stage_retirement_identity(
    worker_fd: int,
    *,
    worker_parent_fd: int,
    worker_binding: _DirectoryBinding,
    identity: _WorkerIdentity,
) -> tuple[str, _IdentityFileBinding]:
    descriptors: list[int] = []
    bindings: list[_DirectoryBinding] = []
    current_fd = worker_fd
    retirement_name = _retirement_identity_name(worker_binding)
    try:
        for segment in identity.path[:-1]:
            child_fd = _open_directory_at(current_fd, segment)
            descriptors.append(child_fd)
            bindings.append(_binding(current_fd, segment, child_fd))
            current_fd = child_fd
        identity_name = identity.path[-1]
        _validate_identity_file_binding(current_fd, identity_name, identity.binding)
        with suppress(FileExistsError):
            os.link(
                identity_name,
                retirement_name,
                src_dir_fd=current_fd,
                dst_dir_fd=worker_parent_fd,
                follow_symlinks=False,
            )
        retirement_binding = _sync_retirement_identity(
            worker_parent_fd,
            retirement_name,
            expected_worker_key=identity.worker_key,
            identity_field_path=identity.field_path,
            expected_binding=identity.binding,
        )
        _validate_identity_file_binding(current_fd, identity_name, identity.binding)
        _validate_identity_file_binding(worker_parent_fd, retirement_name, retirement_binding)
        os.unlink(identity_name, dir_fd=current_fd)
        os.fsync(current_fd)
        for binding in reversed(bindings):
            if not _binding_is_current(binding):
                msg = "Worker state root changed during retirement."
                raise ValueError(msg)
            os.rmdir(binding.name, dir_fd=binding.parent_fd)
        return retirement_name, retirement_binding
    finally:
        for descriptor in reversed(descriptors):
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
    try:
        trusted_root_binding = _trusted_root_binding(trusted_root, root_fd)
        _validate_trusted_root(trusted_root_binding)
        current_fd = root_fd
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
        retirement_identity_name, retirement_identity = _load_retirement_identity(
            worker_parent_fd,
            worker_name=worker_name,
            expected_worker_key=expected_worker_key,
            identity_field_path=identity_field_path,
        )
        _validate_trusted_root(trusted_root_binding)
        _validate_bindings(tuple(bindings))
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
                _retirement_identity_name=retirement_identity_name,
                _retirement_identity=retirement_identity,
            )
            yield target
            return
        descriptors.append(worker_fd)
        bindings.append(_binding(worker_parent_fd, worker_name, worker_fd))
        identity = None
        if retirement_identity_name is not None:
            expected_retirement_name = _retirement_identity_name(bindings[-1])
            if retirement_identity_name != expected_retirement_name:
                msg = "Worker state root changed during retirement."
                raise ValueError(msg)
        elif expected_worker_key is not None:
            identity = _load_canonical_identity(
                worker_fd,
                expected_worker_key=expected_worker_key,
                identity_path=identity_path,
                identity_field_path=identity_field_path,
            )
        yield _BoundWorkerStateRoot(
            _trusted_root=trusted_root_binding,
            _bindings=tuple(bindings),
            _worker_parent_fd=worker_parent_fd,
            _worker_name=worker_name,
            _worker_fd=worker_fd,
            _identity=identity,
            _retirement_identity_name=retirement_identity_name,
            _retirement_identity=retirement_identity,
        )
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
