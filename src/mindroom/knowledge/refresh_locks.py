"""Serialization of one knowledge source root's refresh and mutation work, in this event loop and across processes.

Refresh work for one source root must not overlap, neither inside this event loop nor across the refresh subprocesses
that share the same checkout. Both exclusions are required, so ``refresh_source_root_lock`` is the only way to take
either one.
"""

from __future__ import annotations

import asyncio
import hashlib
import tempfile
import weakref
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from mindroom.file_locks import async_exclusive_file_lock, expose_inherited_file_lock

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from mindroom.knowledge.registry import KnowledgeSourceRoot

_REFRESH_FILE_LOCK_POLL_SECONDS = 0.1
#: One in-loop lock per source root while anything holds or waits for it; an idle lock is dropped with its last user.
_refresh_locks: weakref.WeakValueDictionary[KnowledgeSourceRoot, asyncio.Lock] = weakref.WeakValueDictionary()


def _refresh_file_lock_path(key: KnowledgeSourceRoot) -> Path:
    digest = hashlib.sha256(f"{key.storage_root}\0{key.knowledge_path}".encode()).hexdigest()
    return Path(tempfile.gettempdir()) / "mindroom" / "knowledge_refresh_locks" / f"{digest}.lock"


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
    order rather than queueing.
    """
    lock = _refresh_locks.get(key)
    if lock is None:
        lock = _refresh_locks.setdefault(key, asyncio.Lock())
    async with (
        lock,
        async_exclusive_file_lock(
            _refresh_file_lock_path(key),
            poll_seconds=_REFRESH_FILE_LOCK_POLL_SECONDS,
            retain_for_inherited_fds=True,
        ) as lock_file,
    ):
        with expose_inherited_file_lock(lock_file, scope=Path(key.knowledge_path)):
            yield
