"""Cross-process ownership leases for streaming runtime generations."""

from __future__ import annotations

import fcntl
import hashlib
import os
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path
    from typing import TextIO

    from mindroom.constants import RuntimePaths

_LEASE_DIRECTORY = "runtime_generation_leases"
_RETIRED_AT_PREFIX = "retired_at_ns="


@dataclass(frozen=True)
class _LeaseRecord:
    """Durable runtime-generation lease state."""

    generation: str
    retired_at_ns: int | None


def _generation_lease_path(runtime_paths: RuntimePaths, generation: str) -> Path:
    """Return the stable lease path for one opaque runtime generation."""
    generation_digest = hashlib.sha256(generation.encode()).hexdigest()
    return runtime_paths.storage_root / "tracking" / _LEASE_DIRECTORY / f"{generation_digest}.lock"


@dataclass
class RuntimeGenerationLease:
    """Exclusive process-held lease for one runtime generation."""

    generation: str
    _lease_path: Path
    _lock_file: TextIO | None

    def release(self) -> None:
        """Release this generation while retaining proof until scan acknowledgement."""
        lock_file = self._lock_file
        if lock_file is None:
            return
        self._lock_file = None
        try:
            if _path_references_lock_file(self._lease_path, lock_file):
                _write_lease_record(
                    lock_file,
                    _LeaseRecord(generation=self.generation, retired_at_ns=time.time_ns()),
                )
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            lock_file.close()


def acquire_runtime_generation_lease(
    runtime_paths: RuntimePaths,
    generation: str,
) -> RuntimeGenerationLease:
    """Acquire and durably identify the process-held lease for one generation."""
    lease_path = _generation_lease_path(runtime_paths, generation)
    lease_path.parent.mkdir(parents=True, exist_ok=True)
    _retire_unlocked_leases(lease_path.parent, now_ns=time.time_ns())
    while True:
        lock_file = _open_generation_lease_file(lease_path)
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        except BaseException:
            lock_file.close()
            raise
        if not _path_references_lock_file(lease_path, lock_file):
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            lock_file.close()
            continue
        try:
            _write_lease_record(lock_file, _LeaseRecord(generation=generation, retired_at_ns=None))
        except BaseException:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            lock_file.close()
            raise
        return RuntimeGenerationLease(generation=generation, _lease_path=lease_path, _lock_file=lock_file)


def _open_generation_lease_file(lease_path: Path) -> TextIO:
    """Open one generation path for the identity-checked acquisition loop."""
    return lease_path.open("a+", encoding="utf-8")


def runtime_generation_owner_stopped(
    runtime_paths: RuntimePaths,
    generation: str,
) -> bool:
    """Return whether a known generation lease exists and has no live process owner."""
    lease_path = _generation_lease_path(runtime_paths, generation)
    try:
        lock_file = lease_path.open("r+", encoding="utf-8")
    except FileNotFoundError:
        return False

    acquired = False
    try:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        acquired = True
        return _retire_or_validate_stopped_owner(
            lease_path,
            lock_file,
            generation=generation,
            now_ns=time.time_ns(),
        )
    finally:
        if acquired:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        lock_file.close()


def _path_references_lock_file(lease_path: Path, lock_file: TextIO) -> bool:
    """Return whether the lease path still names the locked file descriptor."""
    try:
        path_stat = lease_path.stat()
    except FileNotFoundError:
        return False
    file_stat = os.fstat(lock_file.fileno())
    return (path_stat.st_dev, path_stat.st_ino) == (file_stat.st_dev, file_stat.st_ino)


def _retire_or_validate_stopped_owner(
    lease_path: Path,
    lock_file: TextIO,
    *,
    generation: str,
    now_ns: int,
) -> bool:
    """Retire a newly stopped owner or validate its retained proof."""
    if not _path_references_lock_file(lease_path, lock_file):
        return False
    record = _read_lease_record(lock_file)
    if record is None or record.generation != generation:
        return False
    if record.retired_at_ns is None:
        _write_lease_record(
            lock_file,
            _LeaseRecord(generation=generation, retired_at_ns=now_ns),
        )
    return True


def _read_lease_record(lock_file: TextIO) -> _LeaseRecord | None:
    """Read one active or retired lease record."""
    lock_file.seek(0)
    lines = lock_file.read().splitlines()
    if not lines or not lines[0]:
        return None
    if len(lines) == 1:
        return _LeaseRecord(generation=lines[0], retired_at_ns=None)
    if len(lines) != 2 or not lines[1].startswith(_RETIRED_AT_PREFIX):
        return None
    retired_at_value = lines[1].removeprefix(_RETIRED_AT_PREFIX)
    try:
        retired_at_ns = int(retired_at_value)
    except ValueError:
        return None
    return _LeaseRecord(generation=lines[0], retired_at_ns=retired_at_ns)


def _write_lease_record(lock_file: TextIO, record: _LeaseRecord) -> None:
    """Persist one active or retired lease record through its locked descriptor."""
    body = record.generation
    if record.retired_at_ns is not None:
        body = f"{body}\n{_RETIRED_AT_PREFIX}{record.retired_at_ns}"
    lock_file.seek(0)
    lock_file.truncate()
    lock_file.write(body)
    lock_file.flush()
    os.fsync(lock_file.fileno())


def _retire_unlocked_leases(lease_directory: Path, *, now_ns: int) -> None:
    """Retire crashed leases without consuming unacknowledged owner proof."""
    for lease_path in lease_directory.glob("*.lock"):
        _retire_unlocked_lease(lease_path, now_ns=now_ns)


def _retire_unlocked_lease(lease_path: Path, *, now_ns: int) -> str | None:
    """Retire one unlocked crash lease without touching a live owner."""
    try:
        lock_file = lease_path.open("r+", encoding="utf-8")
    except FileNotFoundError:
        return None
    acquired = False
    try:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return None
        acquired = True
        if not _path_references_lock_file(lease_path, lock_file):
            return None
        record = _read_lease_record(lock_file)
        if record is None:
            lease_path.unlink()
            return None
        if record.retired_at_ns is None:
            _write_lease_record(
                lock_file,
                _LeaseRecord(generation=record.generation, retired_at_ns=now_ns),
            )
        return record.generation
    finally:
        if acquired:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        lock_file.close()


def stopped_runtime_generation_proofs(runtime_paths: RuntimePaths) -> set[str]:
    """Return stopped generations eligible for one complete recovery scan."""
    lease_directory = runtime_paths.storage_root / "tracking" / _LEASE_DIRECTORY
    now_ns = time.time_ns()
    generations: set[str] = set()
    for lease_path in lease_directory.glob("*.lock"):
        generation = _retire_unlocked_lease(lease_path, now_ns=now_ns)
        if generation is not None:
            generations.add(generation)
    return generations


def acknowledge_stopped_runtime_generation_proofs(
    runtime_paths: RuntimePaths,
    generations: set[str],
) -> None:
    """Acknowledge stopped-owner proofs after a complete successful Matrix scan."""
    for generation in generations:
        _discard_unlocked_generation_proof(
            _generation_lease_path(runtime_paths, generation),
        )


def _discard_unlocked_generation_proof(lease_path: Path) -> None:
    """Discard one unlocked proof without touching a live or replaced inode."""
    try:
        lock_file = lease_path.open("r+", encoding="utf-8")
    except FileNotFoundError:
        return
    acquired = False
    try:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        acquired = True
        if _path_references_lock_file(lease_path, lock_file):
            lease_path.unlink()
    finally:
        if acquired:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        lock_file.close()
