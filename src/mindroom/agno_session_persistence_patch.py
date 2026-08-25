"""Guarded offload for synchronous Agno session persistence."""

from __future__ import annotations

import asyncio
import contextvars
import threading
import weakref
from concurrent.futures import Future, ThreadPoolExecutor
from copy import deepcopy
from dataclasses import dataclass, field
from functools import partial
from importlib.metadata import version
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from agno.agent import _init as agent_init
from agno.agent import _session as agent_session
from agno.team import _session as team_session
from agno.team._init import _has_async_db as team_has_async_db

from mindroom.background_tasks import wait_for_future_until_complete

if TYPE_CHECKING:
    from collections.abc import Callable

    from agno.agent import Agent
    from agno.db.base import BaseDb
    from agno.session import AgentSession, TeamSession, WorkflowSession
    from agno.team import Team

    type _AgentSession = AgentSession | TeamSession | WorkflowSession

type _PersistenceTarget = tuple[str, str]

_SUPPORTED_AGNO_VERSION = "2.6.12"
_ORIGINAL_AGENT_ASAVE_SESSION = agent_session.asave_session
_ORIGINAL_AGENT_SAVE_SESSION = agent_session.save_session
_ORIGINAL_TEAM_ASAVE_SESSION = team_session.asave_session
_ORIGINAL_TEAM_SAVE_SESSION = team_session.save_session

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


@dataclass
class _PreparedOperation:
    """A reserved worker slot awaiting caller-thread snapshot preparation."""

    ready: threading.Event = field(default_factory=threading.Event)
    context: contextvars.Context | None = None
    operation: Callable[[], object] | None = None
    error: BaseException | None = None


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
        return _REGISTERED_LANES.get(database)


def _run_prepared_operation(prepared: _PreparedOperation) -> object:
    prepared.ready.wait()
    if prepared.error is not None:
        raise prepared.error
    if prepared.context is None or prepared.operation is None:
        msg = "Session persistence operation was released before preparation"
        raise RuntimeError(msg)
    return prepared.context.run(prepared.operation)


async def _offload_sync_save[Owner, Session](
    lane: _PersistenceLane,
    save: Callable[[Owner, Session], object],
    owner: Owner,
    session: Session,
) -> None:
    prepared = _PreparedOperation()
    worker: Future[object] = lane.executor.submit(_run_prepared_operation, prepared)
    try:
        context = contextvars.copy_context()
        snapshot = deepcopy(session)
        prepared.context = context
        prepared.operation = partial(save, owner, snapshot)
    except BaseException as error:
        prepared.error = error
        raise
    finally:
        prepared.ready.set()

    await wait_for_future_until_complete(asyncio.wrap_future(worker))


async def _agent_asave_session(agent: Agent, session: _AgentSession) -> None:
    database = agent.db
    if (
        database is None
        or agent.team_id is not None
        or agent.workflow_id is not None
        or session.session_data is None
        or agent_init.has_async_db(agent)
    ):
        await _ORIGINAL_AGENT_ASAVE_SESSION(agent, session)
        return

    lane = _registered_lane(cast("BaseDb", database))
    if lane is None:
        await _ORIGINAL_AGENT_ASAVE_SESSION(agent, session)
        return

    await _offload_sync_save(lane, _ORIGINAL_AGENT_SAVE_SESSION, agent, session)


async def _team_asave_session(team: Team, session: TeamSession) -> None:
    database = team.db
    if database is None or team.parent_team_id is not None or team.workflow_id is not None or team_has_async_db(team):
        await _ORIGINAL_TEAM_ASAVE_SESSION(team, session)
        return

    lane = _registered_lane(cast("BaseDb", database))
    if lane is None:
        await _ORIGINAL_TEAM_ASAVE_SESSION(team, session)
        return

    await _offload_sync_save(lane, _ORIGINAL_TEAM_SAVE_SESSION, team, session)


def _is_applied() -> bool:
    """Return whether both guarded async save replacements are installed."""
    return (
        _PATCHED
        and agent_session.asave_session is _agent_asave_session
        and team_session.asave_session is _team_asave_session
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
            or agent_session.asave_session is not _ORIGINAL_AGENT_ASAVE_SESSION
            or team_session.asave_session is not _ORIGINAL_TEAM_ASAVE_SESSION
        ):
            return False
        agent_session.asave_session = cast("Any", _agent_asave_session)
        team_session.asave_session = cast("Any", _team_asave_session)
        _PATCHED = True
        return True


def install_patch() -> None:
    """Install the patch or fail closed on an incompatible Agno version."""
    if not _apply_patch():
        msg = f"Cannot install the synchronous session persistence boundary: expected Agno {_SUPPORTED_AGNO_VERSION}"
        raise RuntimeError(msg)
