"""One FIFO lane per conversation database for storage operations started from async code.

An operation opens its store and runs off the event loop; operations on one database run one at a time, in the
order they reach its lane, so writers in one process never contend for the database's write lock.
"""

from __future__ import annotations

import contextvars
import threading
import weakref
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, cast

from mindroom.background_tasks import run_blocking_until_complete

if TYPE_CHECKING:
    from collections.abc import Callable

    from mindroom.session_storage.store import ConversationStore

_LOCK = threading.Lock()
# A lane lives while an operation holds it; the next operation on an idle database starts a fresh one.
_LANES: weakref.WeakValueDictionary[tuple[str, str], ThreadPoolExecutor] = weakref.WeakValueDictionary()


def _lane(store: ConversationStore) -> ThreadPoolExecutor:
    target = (str(store.path.resolve()), store.names.sessions)
    with _LOCK:
        lane = _LANES.get(target)
        if lane is None:
            lane = ThreadPoolExecutor(max_workers=1, thread_name_prefix="conversation-store")
            _LANES[target] = lane
        return lane


def _run_in_lane[Result](
    open_store: Callable[[], ConversationStore],
    operation: Callable[[ConversationStore], Result],
) -> Result:
    store = open_store()
    lane = _lane(store)
    return cast("Result", lane.submit(contextvars.copy_context().run, operation, store).result())


async def run_session_storage_operation[Result](
    open_store: Callable[[], ConversationStore],
    operation: Callable[[ConversationStore], Result],
) -> Result:
    """Open a store and run one synchronous operation on it in its database's lane.

    Cancelling the caller waits for the operation to finish before the cancellation propagates, so a started
    write always completes.
    """
    return await run_blocking_until_complete(_run_in_lane, open_store, operation)


__all__ = [
    "run_session_storage_operation",
]
