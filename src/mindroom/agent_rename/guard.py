"""Refuse to rename while MindRoom runs, and keep it from starting mid-rename."""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from typing import TYPE_CHECKING

from mindroom.agent_rename.steps import RenameRefusedError
from mindroom.constants import tracking_dir
from mindroom.event_journal.opening import event_journal_in_use_lock_path
from mindroom.file_locks import release_file_lock, try_exclusive_file_lock
from mindroom.workers.backend import WorkerBackendError
from mindroom.workers.runtime import primary_worker_backend_name

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path
    from typing import TextIO

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

_SUPPORTED_WORKER_BACKENDS = frozenset({"static_runner", "docker"})


@contextmanager
def stopped_runtime(runtime_paths: RuntimePaths) -> Iterator[None]:
    """Hold the locks a running MindRoom holds, so none runs while the rename edits the disk.

    The orchestrator takes ``tracking/runtime.lock`` and opens the event journal under a
    shared lock on its in-use file.
    Taking both exclusively without waiting refuses when MindRoom is up
    and blocks MindRoom from starting until the context exits.
    """
    with ExitStack() as locks:
        for lock_path in (
            tracking_dir(runtime_paths) / "runtime.lock",
            event_journal_in_use_lock_path(runtime_paths.storage_root),
        ):
            locks.callback(release_file_lock, _take_exclusively(lock_path))
        yield


def _take_exclusively(lock_path: Path) -> TextIO:
    lock_file = try_exclusive_file_lock(lock_path)
    if lock_file is None:
        msg = f"Stop MindRoom first: another process holds {lock_path}."
        raise RenameRefusedError(msg)
    return lock_file


def require_supported_backends(config: Config, runtime_paths: RuntimePaths) -> None:
    """Refuse installs whose journal rows or worker state the rename cannot reach yet."""
    if config.event_journal.backend != "sqlite":
        msg = f"The event_journal.backend {config.event_journal.backend!r} is not supported yet; only 'sqlite' is."
        raise RenameRefusedError(msg)
    try:
        worker_backend = primary_worker_backend_name(runtime_paths)
    except WorkerBackendError as error:
        raise RenameRefusedError(str(error)) from error
    if worker_backend not in _SUPPORTED_WORKER_BACKENDS:
        supported = " or ".join(sorted(_SUPPORTED_WORKER_BACKENDS))
        msg = f"The worker backend {worker_backend!r} is not supported yet; only {supported} are."
        raise RenameRefusedError(msg)
