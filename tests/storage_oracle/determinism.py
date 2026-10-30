"""Fixed clocks and counted ids for every source of time and randomness the storage paths read.

Inside ``deterministic()``, each patched module reads the clock its ``Determinism`` holds, and each ``uuid4`` it calls
returns the next id of its own counter, which restarts at every step. The driver moves the clock once per action, so
what a store writes depends on the action, never on how many times an implementation asks for the time.

Conversation databases also skip their fsyncs inside the block: a recording writes thousands of transactions, and
durability is not among what it observes.
"""

from __future__ import annotations

import time as real_time
import uuid
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime
from types import ModuleType, SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import patch

from mindroom.history import storage as history_storage
from mindroom.llm import messages, runs
from mindroom.session_storage import (
    agent_storage,
    legacy_agno2_runs,
    legacy_compaction_state,
    preflight,
    store,
    summary_rows,
    types,
)

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Iterator, Sequence
    from pathlib import Path

# The first action's clock; each later action runs ``STEP_SECONDS`` later.
BASE_TIME = 1_750_000_000
STEP_SECONDS = 10


class Determinism:
    """The clock and the id counters one recording runs under."""

    def __init__(self) -> None:
        self.now = BASE_TIME
        self._step = 0
        self._counters: dict[int, int] = {}

    def at(self, step: int) -> None:
        """Move the clock to ``step``'s time and restart every id counter."""
        self._step = step
        self.now = BASE_TIME + step * STEP_SECONDS
        self._counters = {}

    def time(self) -> float:
        """Return the clock, as ``time.time()`` does."""
        return float(self.now)

    @contextmanager
    def ids_repeated(self) -> Iterator[None]:
        """Run a block whose ids the code after it gets again, as a second store doing the same thing would."""
        counters = dict(self._counters)
        try:
            yield
        finally:
            self._counters = counters

    def uuid4(self, tag: int) -> uuid.UUID:
        """Return the next id of counter ``tag`` in this step."""
        number = self._counters.get(tag, 0) + 1
        self._counters[tag] = number
        return uuid.UUID(int=(tag << 120) | (self._step << 32) | number)


def _datetime_class(determinism: Determinism) -> type[datetime]:
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz: object = None) -> FixedDatetime:  # type: ignore[override]
            del tz
            return cls.fromtimestamp(determinism.now, UTC)

    return FixedDatetime


def _time_module(determinism: Determinism) -> SimpleNamespace:
    """Return a stand-in for the ``time`` module whose ``time()`` reads the clock; every other name is the real one."""
    return SimpleNamespace(**{**{name: getattr(real_time, name) for name in dir(real_time)}, "time": determinism.time})


# Modules that call ``uuid4`` by that name, each with its own counter.
_UUID_MODULES: tuple[ModuleType, ...] = (messages, runs, agent_storage, preflight)
# Modules that call ``time()`` imported from ``time``.
_TIME_FUNCTION_MODULES: tuple[ModuleType, ...] = (messages, runs, store, summary_rows, types)
# Modules that call ``time.time()`` on the imported module.
_TIME_MODULE_MODULES: tuple[ModuleType, ...] = (agent_storage, legacy_compaction_state, legacy_agno2_runs)
# Modules that call ``datetime.now``.
_DATETIME_MODULES: tuple[ModuleType, ...] = (history_storage,)


def _without_fsync(connect: Callable[..., sqlite3.Connection]) -> Callable[..., sqlite3.Connection]:
    def connect_without_fsync(path: Path, *, timeout: float = store.BUSY_TIMEOUT_SECONDS) -> sqlite3.Connection:
        connection = connect(path, timeout=timeout)
        connection.execute("PRAGMA synchronous = OFF")
        return connection

    return connect_without_fsync


@contextmanager
def deterministic(*, time_modules: Sequence[ModuleType] = ()) -> Iterator[Determinism]:
    """Patch every clock and id source of the storage paths for the block, and yield their state.

    ``time_modules`` are further modules whose ``time.time()`` reads the clock, such as a test module that builds
    an older database.
    """
    determinism = Determinism()
    fixed_datetime = _datetime_class(determinism)
    time_module = _time_module(determinism)
    with ExitStack() as stack:
        for tag, module in enumerate(_UUID_MODULES, start=1):
            stack.enter_context(patch.object(module, "uuid4", lambda tag=tag: determinism.uuid4(tag)))
        for module in _TIME_FUNCTION_MODULES:
            stack.enter_context(patch.object(module, "time", determinism.time))
        for module in (*_TIME_MODULE_MODULES, *time_modules):
            stack.enter_context(patch.object(module, "time", time_module))
        for module in _DATETIME_MODULES:
            stack.enter_context(patch.object(module, "datetime", fixed_datetime))
        connect = store.connect_conversation_database
        stack.enter_context(patch.object(store, "connect_conversation_database", _without_fsync(connect)))
        yield determinism
