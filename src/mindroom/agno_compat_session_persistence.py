"""Guarded offload for synchronous Agno session persistence."""

from __future__ import annotations

import asyncio
import contextvars
import threading
import weakref
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import asynccontextmanager, contextmanager, nullcontext
from copy import copy, deepcopy
from dataclasses import dataclass, field
from functools import partial
from importlib.metadata import version
from pathlib import Path
from queue import SimpleQueue
from typing import TYPE_CHECKING, Any, cast

from agno.agent import _run as agent_run
from agno.agent import _session as agent_session
from agno.agent import _storage as agent_storage
from agno.db.base import SessionType
from agno.session import AgentSession, TeamSession, WorkflowSession
from agno.team import _session as team_session

from mindroom.background_tasks import run_blocking_until_complete, wait_for_future_until_complete

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Iterator
    from contextlib import AbstractContextManager

    from agno.agent import Agent
    from agno.db.base import BaseDb
    from agno.run import RunContext
    from agno.run.agent import RunOutput
    from agno.run.team import TeamRunOutput
    from agno.team import Team

    type _AgentSession = AgentSession | TeamSession | WorkflowSession

type _PersistenceTarget = tuple[str, str]

# AGNO_COMPAT: Async persistence lacks hooks for synchronous storage owners.
# Reason: Async Agent/Team session paths call owned synchronous storage on the event loop.
# Upstream issue: https://github.com/agno-agi/agno/issues/10149
# Upstream PR: No complete implementation yet; https://github.com/agno-agi/agno/pull/10148
# only routes Agent startup through the existing awaitable read path.
# Remove when: Supported async persistence preserves nonmutating preparation and
# owner-controlled dispatch across the required Agent/Team reads and writes.
# Keep MindRoom's FIFO, snapshot, cancellation, and resource-lifetime guarantees.
# Coverage: tests/test_agno_compat_session_persistence.py::test_registered_writes_run_on_a_dedicated_thread;
# tests/test_agno_compat_session_persistence.py::test_cross_loop_reservation_precedes_snapshot_work;
# tests/test_agno_compat_session_persistence.py::test_async_session_read_shares_save_lane_and_drains_cancellation.

# Agno 3.0 splits one session save into a session-row write (``asave_session``) and
# per-run writes (``asave_run``); both call the synchronous SQLite adapter directly,
# so both are offloaded through the same FIFO lane to keep their order.
# SQLite deletion and ordering have separate upstream tracking and removal
# conditions in agno_compat_sqlite.py; review those when bumping this pin too.
_SUPPORTED_AGNO_VERSION = "3.0.9"
_ORIGINAL_AGENT_AREAD_SESSION = agent_storage.aread_session
_ORIGINAL_AGENT_READ_SESSION = agent_storage.read_session
_ORIGINAL_AGENT_ASAVE_SESSION = agent_session.asave_session
_ORIGINAL_AGENT_SAVE_SESSION = agent_session.save_session
_ORIGINAL_AGENT_ASAVE_RUN = agent_session.asave_run
_ORIGINAL_AGENT_SAVE_RUN = agent_session.save_run
_ORIGINAL_CANCELLED_RUN_PERSIST = agent_run._persist_cancelled_run_in_background
_ORIGINAL_TEAM_AGET_SESSION = team_session.aget_session
_ORIGINAL_TEAM_GET_SESSION = team_session.get_session
_ORIGINAL_TEAM_ASAVE_SESSION = team_session.asave_session
_ORIGINAL_TEAM_SAVE_SESSION = team_session.save_session
_ORIGINAL_TEAM_ASAVE_RUN = team_session.asave_run
_ORIGINAL_TEAM_SAVE_RUN = team_session.save_run

_PATCHED = False
_PATCH_LOCK = threading.Lock()
_LANE_LOCK = threading.Lock()
_REGISTERED_LANES: weakref.WeakKeyDictionary[BaseDb, _PersistenceLane] = weakref.WeakKeyDictionary()
_TARGET_LANES: weakref.WeakValueDictionary[_PersistenceTarget, _PersistenceLane] = weakref.WeakValueDictionary()


@dataclass
class _PersistenceLane:
    """One target's dedicated FIFO executor."""

    executor: ThreadPoolExecutor = field(
        default_factory=lambda: ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="session-persistence",
        ),
    )


# AGNO_COMPAT: Cancelled-run persistence lacks an awaited drain boundary.
# Reason: Agno persists cancelled runs in detached background tasks without a
# public drain boundary before the caller writes canonical history.
# Upstream issue: No matching public cancelled-run persistence drain issue identified.
# Upstream PR: None identified for this extension point.
# Remove when: Agno exposes an awaited cancellation-persistence boundary with exact
# agent/run ownership; preserve caller history ownership and cross-task stream safety.
# Coverage: tests/test_agno_cancellation.py exercises exact-run ownership and drainage.
# Additional coverage: tests/test_ai_cancellation_lifecycle.py and tests/test_delegation_stream_lifecycle.py.
@dataclass
class _CancellationOwner:
    agent: Agent
    run_id: str
    tasks: set[asyncio.Task[Any]] = field(default_factory=set)

    @contextmanager
    def bind(self) -> Iterator[None]:
        """Bind only one call or iterator operation, never a yielded stream chunk."""
        token = _CANCELLATION_OWNER.set(self)
        try:
            yield
        finally:
            _CANCELLATION_OWNER.reset(token)


_CANCELLATION_OWNER: contextvars.ContextVar[_CancellationOwner | None] = contextvars.ContextVar(
    "agent_cancellation_owner",
    default=None,
)


@asynccontextmanager
async def drain_agent_cancellation(
    agent: Agent,
    run_id: str,
) -> AsyncIterator[Callable[[], AbstractContextManager[None]]]:
    """Finish this attempt's detached Agno save before its caller writes canonical history.

    Keep upstream persistence, approval updates, and cleanup intact. Ownership
    matches the exact agent and run, so inherited contexts cannot adopt helper
    runs. The yielded factory binds each call, pull, or close in its own context;
    API streams may move between tasks. Close the iterator before leaving this scope.
    """
    if _agent_lane(agent) is None:
        yield nullcontext
        return
    owner = _CancellationOwner(agent, run_id)
    try:
        yield owner.bind
    finally:
        if owner.tasks:
            await wait_for_future_until_complete(asyncio.gather(*owner.tasks))


def _persist_cancelled_run_in_background(
    agent: Agent,
    run_response: RunOutput,
    session: AgentSession,
    run_context: RunContext | None = None,
    user_id: str | None = None,
) -> None:
    """Retain the exact tasks accepted by the pinned synchronous scheduling helper."""
    owner = _CANCELLATION_OWNER.get()
    if owner is None or owner.agent is not agent or owner.run_id != run_response.run_id:
        _ORIGINAL_CANCELLED_RUN_PERSIST(agent, run_response, session, run_context, user_id)
        return
    before = set(agent_run._background_tasks)
    try:
        _ORIGINAL_CANCELLED_RUN_PERSIST(agent, run_response, session, run_context, user_id)
    finally:
        # The helper registers its task without yielding. Other event loops may
        # share Agno's registry, so only adopt tasks from this loop.
        loop = asyncio.get_running_loop()
        owner.tasks.update(task for task in agent_run._background_tasks - before if task.get_loop() is loop)


def _register_sync_session_storage(
    database: BaseDb,
    *,
    db_file: str,
    session_table: str,
) -> None:
    """Opt one application-owned synchronous session database into offloading."""
    target = (str(Path(db_file).resolve()), session_table)
    with _LANE_LOCK:
        lane = _TARGET_LANES.get(target)
        if lane is None:
            lane = _PersistenceLane()
            _TARGET_LANES[target] = lane
        _REGISTERED_LANES[database] = lane


def _registered_lane(database: BaseDb) -> _PersistenceLane | None:
    with _LANE_LOCK:
        try:
            return _REGISTERED_LANES.get(database)
        except TypeError:
            return None


def _run_registered_storage_operation[Result](
    create_database: Callable[[], BaseDb],
    operation: Callable[[BaseDb], Result],
) -> Result:
    """Create, use, and close one database away from the event-loop thread."""
    database = create_database()
    try:
        lane = _registered_lane(database)
        if lane is None:
            return operation(database)
        context = contextvars.copy_context()
        return cast("Result", lane.executor.submit(context.run, operation, database).result())
    finally:
        database.close()


async def run_registered_storage_operation[Result](
    create_database: Callable[[], BaseDb],
    operation: Callable[[BaseDb], Result],
) -> Result:
    """Run one whole synchronous storage operation in its registered FIFO lane."""
    return await run_blocking_until_complete(
        _run_registered_storage_operation,
        create_database,
        operation,
    )


def _run_prepared_operation(operations: SimpleQueue[Callable[[], object] | None]) -> object | None:
    operation = operations.get()
    return None if operation is None else operation()


async def _offload_sync_save[Owner, Payload](
    lane: _PersistenceLane,
    save: Callable[..., object],
    owner: Owner,
    payload: Payload,
    *save_args: object,
) -> None:
    """Snapshot ``payload`` and run one synchronous save in the lane, in submission order."""
    operations: SimpleQueue[Callable[[], object] | None] = SimpleQueue()
    worker: Future[object | None] = lane.executor.submit(_run_prepared_operation, operations)
    try:
        context = contextvars.copy_context()
        if isinstance(payload, (AgentSession, TeamSession, WorkflowSession)):
            # Agno 3 persists runs separately; snapshot only the session row.
            # Clear history on a shallow copy so the live session stays intact.
            payload = copy(payload)
            payload.runs = None
        snapshot = deepcopy(payload)
        operation = partial(context.run, save, owner, snapshot, *save_args)
    except BaseException:
        operations.put(None)
        raise
    operations.put(operation)

    await wait_for_future_until_complete(asyncio.wrap_future(worker))


def _agent_lane(agent: Agent) -> _PersistenceLane | None:
    """Return the lane for a standalone agent's registered synchronous database."""
    database = agent.db
    if database is None or agent.team_id is not None or agent.workflow_id is not None:
        return None
    return _registered_lane(cast("BaseDb", database))


def _team_lane(team: Team) -> _PersistenceLane | None:
    """Return the lane for a top-level team's registered synchronous database."""
    database = team.db
    if database is None or team.parent_team_id is not None or team.workflow_id is not None:
        return None
    return _registered_lane(cast("BaseDb", database))


async def _agent_aread_session(
    agent: Agent,
    session_id: str,
    session_type: SessionType = SessionType.AGENT,
    user_id: str | None = None,
    runs_limit: int | None = None,
) -> _AgentSession | None:
    """Read registered storage after accepted writes, retaining ownership on cancellation."""
    # Startup preload will use this seam after https://github.com/agno-agi/agno/pull/10148 ships.
    # Keep scheduling here: arbitrary synchronous adapters may be bound to their original thread.
    lane = _agent_lane(agent)
    if lane is None:
        return await _ORIGINAL_AGENT_AREAD_SESSION(agent, session_id, session_type, user_id, runs_limit)
    return await _offload_sync_read(
        lane,
        partial(_ORIGINAL_AGENT_READ_SESSION, agent, session_id, session_type, user_id, runs_limit),
    )


async def _offload_sync_read[Result](lane: _PersistenceLane, read: Callable[[], Result]) -> Result:
    """Read after accepted writes and drain cancellation before releasing ownership."""
    context = contextvars.copy_context()
    worker = lane.executor.submit(context.run, read)
    return cast("Result", await wait_for_future_until_complete(asyncio.wrap_future(worker)))


async def _team_aget_session(
    team: Team,
    session_id: str | None = None,
    user_id: str | None = None,
) -> TeamSession | None:
    """Keep registered team continuation reads in the same FIFO lane as saves."""
    lane = _team_lane(team)
    if lane is None:
        return await _ORIGINAL_TEAM_AGET_SESSION(team, session_id, user_id)
    # Pinned Agno's aget_session bypasses _aread_session for synchronous databases.
    return await _offload_sync_read(lane, partial(_ORIGINAL_TEAM_GET_SESSION, team, session_id, user_id))


async def _agent_asave_session(agent: Agent, session: _AgentSession) -> None:
    lane = _agent_lane(agent) if session.session_data is not None else None
    if lane is None:
        await _ORIGINAL_AGENT_ASAVE_SESSION(agent, session)
        return
    await _offload_sync_save(lane, _ORIGINAL_AGENT_SAVE_SESSION, agent, session)


async def _agent_asave_run(
    agent: Agent,
    run: RunOutput,
    session_id: str,
    user_id: str | None = None,
    run_index: int | None = None,
) -> None:
    lane = _agent_lane(agent)
    if lane is None:
        await _ORIGINAL_AGENT_ASAVE_RUN(agent, run, session_id, user_id, run_index)
        return
    await _offload_sync_save(lane, _ORIGINAL_AGENT_SAVE_RUN, agent, run, session_id, user_id, run_index)


async def _team_asave_session(team: Team, session: TeamSession) -> None:
    lane = _team_lane(team)
    if lane is None:
        await _ORIGINAL_TEAM_ASAVE_SESSION(team, session)
        return
    await _offload_sync_save(lane, _ORIGINAL_TEAM_SAVE_SESSION, team, session)


async def _team_asave_run(
    team: Team,
    run: TeamRunOutput | RunOutput,
    session_id: str,
    user_id: str | None = None,
    run_index: int | None = None,
) -> None:
    lane = _team_lane(team)
    if lane is None:
        await _ORIGINAL_TEAM_ASAVE_RUN(team, run, session_id, user_id, run_index)
        return
    await _offload_sync_save(lane, _ORIGINAL_TEAM_SAVE_RUN, team, run, session_id, user_id, run_index)


def _is_applied() -> bool:
    """Return whether every guarded async read/save replacement is installed."""
    return (
        _PATCHED
        and agent_storage.aread_session is _agent_aread_session
        and agent_session.asave_session is _agent_asave_session
        and agent_session.asave_run is _agent_asave_run
        and agent_run._persist_cancelled_run_in_background is _persist_cancelled_run_in_background
        and team_session.aget_session is _team_aget_session
        and team_session.asave_session is _team_asave_session
        and team_session.asave_run is _team_asave_run
    )


def _apply_patch() -> bool:
    """Install the compatibility boundary for the exact pinned Agno version."""
    global _PATCHED
    if _is_applied():
        return True
    with _PATCH_LOCK:
        if _is_applied():
            return True
        if (
            _PATCHED
            or version("agno") != _SUPPORTED_AGNO_VERSION
            or agent_storage.aread_session is not _ORIGINAL_AGENT_AREAD_SESSION
            or agent_session.asave_session is not _ORIGINAL_AGENT_ASAVE_SESSION
            or agent_session.asave_run is not _ORIGINAL_AGENT_ASAVE_RUN
            or agent_run._persist_cancelled_run_in_background is not _ORIGINAL_CANCELLED_RUN_PERSIST
            or team_session.aget_session is not _ORIGINAL_TEAM_AGET_SESSION
            or team_session.asave_session is not _ORIGINAL_TEAM_ASAVE_SESSION
            or team_session.asave_run is not _ORIGINAL_TEAM_ASAVE_RUN
        ):
            return False
        agent_storage.aread_session = cast("Any", _agent_aread_session)
        agent_session.asave_session = cast("Any", _agent_asave_session)
        agent_session.asave_run = cast("Any", _agent_asave_run)
        agent_run._persist_cancelled_run_in_background = cast("Any", _persist_cancelled_run_in_background)
        team_session.aget_session = cast("Any", _team_aget_session)
        team_session.asave_session = cast("Any", _team_asave_session)
        team_session.asave_run = cast("Any", _team_asave_run)
        _PATCHED = True
        return True


def install_patch() -> None:
    """Install the patch or fail closed on an incompatible Agno version."""
    if not _apply_patch():
        msg = f"Cannot install the synchronous session persistence boundary: expected Agno {_SUPPORTED_AGNO_VERSION}"
        raise RuntimeError(msg)
