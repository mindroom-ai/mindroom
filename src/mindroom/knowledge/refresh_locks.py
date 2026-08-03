"""Process-wide serialization and activity bookkeeping for knowledge refreshes.

Refresh work for one source root must not overlap, neither inside this event loop nor
across the refresh subprocesses that share the same checkout. Both exclusions are
required, so ``refresh_source_root_lock`` is the only way to take either one, and this
module owns the tables behind them, which are process-wide singletons.
"""

from __future__ import annotations

import asyncio
import hashlib
import tempfile
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import TYPE_CHECKING

from mindroom.file_locks import async_exclusive_file_lock
from mindroom.knowledge.registry import resolve_refresh_target

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from typing import Literal

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.knowledge.registry import KnowledgeRefreshTarget, KnowledgeSourceRoot
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity


_refresh_locks_guard = Lock()
# Deliberately uncapped, unlike the lock table below and the refresh cooldowns in
# knowledge/utils.py: entries are refcounts that clear when a refresh finishes, so
# evicting a live one would report an in-flight refresh as idle and mask the leak a
# crashed refresh means.
_active_refresh_counts: dict[KnowledgeRefreshTarget, int] = {}
_scheduled_refresh_targets: set[KnowledgeRefreshTarget] = set()
_active_refresh_counts_guard = Lock()
_MAX_REFRESH_LOCKS = 512
_REFRESH_FILE_LOCK_POLL_SECONDS = 0.1


@dataclass
class _RefreshLockEntry:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    borrowers: int = 0


_refresh_locks: dict[KnowledgeSourceRoot, _RefreshLockEntry] = {}


def _borrow_refresh_lock_for_key(key: KnowledgeSourceRoot) -> _RefreshLockEntry:
    with _refresh_locks_guard:
        entry = _refresh_locks.get(key)
        if entry is None:
            _prune_refresh_locks_locked(reserve_slots=1)
            entry = _RefreshLockEntry()
            _refresh_locks[key] = entry
        entry.borrowers += 1
        return entry


def _release_refresh_lock_for_key(key: KnowledgeSourceRoot, entry: _RefreshLockEntry) -> None:
    with _refresh_locks_guard:
        if entry.borrowers <= 0:
            return
        entry.borrowers -= 1
        if _refresh_locks.get(key) is entry:
            _prune_refresh_locks_locked()


def _prune_refresh_locks_locked(*, reserve_slots: int = 0) -> None:
    target_size = max(_MAX_REFRESH_LOCKS - reserve_slots, 0)
    if len(_refresh_locks) <= target_size:
        return
    excess = len(_refresh_locks) - target_size
    for key, entry in tuple(_refresh_locks.items()):
        if excess <= 0:
            break
        if entry.borrowers > 0 or entry.lock.locked():
            continue
        _refresh_locks.pop(key, None)
        excess -= 1


@asynccontextmanager
async def _acquire_refresh_lock(key: KnowledgeSourceRoot) -> AsyncIterator[None]:
    """Serialize source-root refresh and mutation work in this runtime event loop."""
    entry = _borrow_refresh_lock_for_key(key)
    acquired = False
    try:
        await entry.lock.acquire()
        acquired = True
        yield
    finally:
        if acquired:
            entry.lock.release()
        _release_refresh_lock_for_key(key, entry)


def _refresh_file_lock_path(key: KnowledgeSourceRoot) -> Path:
    digest = hashlib.sha256(f"{key.storage_root}\0{key.knowledge_path}".encode()).hexdigest()
    return Path(tempfile.gettempdir()) / "mindroom" / "knowledge_refresh_locks" / f"{digest}.lock"


@asynccontextmanager
async def _acquire_refresh_file_lock(key: KnowledgeSourceRoot) -> AsyncIterator[None]:
    """Serialize source-root refresh and mutation work across processes."""
    async with async_exclusive_file_lock(_refresh_file_lock_path(key), poll_seconds=_REFRESH_FILE_LOCK_POLL_SECONDS):
        yield


@asynccontextmanager
async def refresh_source_root_lock(key: KnowledgeSourceRoot) -> AsyncIterator[None]:
    """Hold every exclusion one source root's refresh and mutation work needs.

    The two halves cover different concurrency domains and neither substitutes for the
    other, so taking them as one unit is what makes the pairing checkable instead of a
    convention every new call site has to remember.

    Dropping the cross-process half corrupts data: a scheduled refresh runs in a child
    interpreter, where the parent's ``asyncio.Lock`` is invisible, so nothing would stop
    it from indexing a source tree while an API upload or delete rewrites it and then
    publishing that half-written corpus as the last-good index.

    Dropping the in-loop half degrades instead: tasks in one event loop would contend on
    ``flock`` alone, polling every ``_REFRESH_FILE_LOCK_POLL_SECONDS`` in arrival-blind
    order rather than queueing, and the borrow counts that keep the lock table from
    evicting a live entry would never be recorded.
    """
    async with _acquire_refresh_lock(key), _acquire_refresh_file_lock(key):
        yield


def mark_refresh_active(key: KnowledgeRefreshTarget) -> None:
    """Record direct refresh activity for one physical target."""
    with _active_refresh_counts_guard:
        _active_refresh_counts[key] = _active_refresh_counts.get(key, 0) + 1


def claim_scheduled_refresh(
    key: KnowledgeRefreshTarget,
) -> Literal["claimed", "scheduled_refresh_active", "direct_refresh_active"]:
    """Atomically claim scheduler ownership or identify the existing owner."""
    with _active_refresh_counts_guard:
        if key in _scheduled_refresh_targets:
            return "scheduled_refresh_active"
        if _active_refresh_counts.get(key, 0) > 0:
            return "direct_refresh_active"
        _active_refresh_counts[key] = 1
        _scheduled_refresh_targets.add(key)
        return "claimed"


def mark_scheduled_refresh_inactive(key: KnowledgeRefreshTarget) -> None:
    """Release one scheduler-owned refresh claim."""
    with _active_refresh_counts_guard:
        if key not in _scheduled_refresh_targets:
            return
        _scheduled_refresh_targets.remove(key)
        _mark_refresh_inactive_locked(key)


def mark_refresh_inactive(key: KnowledgeRefreshTarget) -> None:
    """Release one direct refresh activity record."""
    with _active_refresh_counts_guard:
        _mark_refresh_inactive_locked(key)


def _mark_refresh_inactive_locked(key: KnowledgeRefreshTarget) -> None:
    count = _active_refresh_counts.get(key, 0)
    if count <= 1:
        _active_refresh_counts.pop(key, None)
    else:
        _active_refresh_counts[key] = count - 1


def is_refresh_active(key: KnowledgeRefreshTarget) -> bool:
    """Return whether a refresh is active for one resolved physical binding."""
    with _active_refresh_counts_guard:
        return _active_refresh_counts.get(key, 0) > 0


def is_refresh_active_for_binding(
    base_id: str,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None = None,
) -> bool:
    """Resolve a binding and return whether it has an active refresh."""
    try:
        key = resolve_refresh_target(
            base_id,
            config=config,
            runtime_paths=runtime_paths,
            execution_identity=execution_identity,
            create=False,
        )
    except ValueError:
        return False
    return is_refresh_active(key)
