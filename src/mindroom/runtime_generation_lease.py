"""Cross-process ownership leases for streaming runtime generations."""

from __future__ import annotations

import fcntl
import hashlib
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path
    from typing import TextIO

    from mindroom.constants import RuntimePaths

_LEASE_DIRECTORY = "runtime_generation_leases"


def _generation_lease_path(runtime_paths: RuntimePaths, generation: str) -> Path:
    """Return the stable lease path for one opaque runtime generation."""
    generation_digest = hashlib.sha256(generation.encode()).hexdigest()
    return runtime_paths.storage_root / "tracking" / _LEASE_DIRECTORY / f"{generation_digest}.lock"


@dataclass
class RuntimeGenerationLease:
    """Exclusive process-held lease for one runtime generation."""

    generation: str
    _lock_file: TextIO | None

    def release(self) -> None:
        """Release this runtime generation lease while retaining its durable proof file."""
        if self._lock_file is None:
            return
        fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_UN)
        self._lock_file.close()
        self._lock_file = None


def acquire_runtime_generation_lease(
    runtime_paths: RuntimePaths,
    generation: str,
) -> RuntimeGenerationLease:
    """Acquire and durably identify the process-held lease for one generation."""
    lease_path = _generation_lease_path(runtime_paths, generation)
    lease_path.parent.mkdir(parents=True, exist_ok=True)
    lock_file = lease_path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        lock_file.seek(0)
        lock_file.truncate()
        lock_file.write(generation)
        lock_file.flush()
        os.fsync(lock_file.fileno())
    except BaseException:
        lock_file.close()
        raise
    return RuntimeGenerationLease(generation=generation, _lock_file=lock_file)


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
        lock_file.seek(0)
        return lock_file.read() == generation
    finally:
        if acquired:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        lock_file.close()
