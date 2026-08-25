"""Guarded offload for synchronous Agno session persistence."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import threading
import weakref
from copy import deepcopy
from dataclasses import dataclass
from importlib.metadata import version
from typing import TYPE_CHECKING, Any, cast

from agno.agent import _init as agent_init
from agno.agent import _run as agent_run
from agno.agent import _session as agent_session
from agno.agent import _storage as agent_storage
from agno.team import _run as team_run
from agno.team import _session as team_session
from agno.team import _storage as team_storage
from agno.team._init import _has_async_db as team_has_async_db

if TYPE_CHECKING:
    from collections.abc import Callable

    from agno.agent import Agent
    from agno.db.base import BaseDb
    from agno.run.team import TeamRunOutput
    from agno.session import AgentSession, TeamSession, WorkflowSession
    from agno.team import Team

    type _AgentSession = AgentSession | TeamSession | WorkflowSession


_SUPPORTED_AGNO_VERSION = "2.6.12"
_EXPECTED_SOURCE_HASHES = {
    "agent_asave_session": "fe0e574c43667d54ef958a183473039ad946a51bf95878ae2e7fb50394123fc3",
    "agent_save_session": "7866d1577e338788b88b1ffb456fd153f35a8cfbf9c0b645d7bf234bfb9e9d95",
    "agent_upsert_session": "4615c7e628bf94be93dd11bfa5865adad1c549d379b74e3ae9aea5dbf6e1e452",
    "team_asave_session": "3038fcf9871a3e35c8080e895529ef952bbd5f4d0cd60bff6702e048141088e1",
    "team_save_session": "c64763b434ed048b87a89209685a1c41a13d8531357dd2ed3270d5ae94942c4d",
    "team_upsert_session": "85f5fc1bb4cc8b96b61c907fc3a2b14c2e938bd3c88ad13c112f3a93b4997d23",
    "team_scrub_member_responses": "49321b6de6f2ab951f9fc54c5efb0f46854ca9415d0b09755318a7930bbb7ea0",
}

_ORIGINAL_AGENT_ASAVE_SESSION = agent_session.asave_session
_ORIGINAL_AGENT_SAVE_SESSION = agent_session.save_session
_ORIGINAL_AGENT_UPSERT_SESSION = agent_storage.upsert_session
_ORIGINAL_TEAM_ASAVE_SESSION = team_session.asave_session
_ORIGINAL_TEAM_SAVE_SESSION = team_session.save_session
_ORIGINAL_TEAM_UPSERT_SESSION = team_storage._upsert_session
_ORIGINAL_TEAM_SCRUB_MEMBER_RESPONSES = team_run._scrub_member_responses

_PATCHED = False
_PATCH_LOCK = threading.Lock()
_DATABASE_QUEUES_GUARD = threading.Lock()
_DATABASE_QUEUES: weakref.WeakKeyDictionary[BaseDb, _DatabaseWriteQueue] = weakref.WeakKeyDictionary()


@dataclass(frozen=True)
class _DatabaseOwner:
    """Minimal owner accepted by Agno's captured storage helpers."""

    db: BaseDb


class _DatabaseWriteQueue:
    """Cross-loop FIFO for writes sharing one synchronous database."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._next_ticket = 0
        self._serving_ticket = 0
        self._abandoned_tickets: set[int] = set()

    def reserve(self) -> int:
        with self._condition:
            ticket = self._next_ticket
            self._next_ticket += 1
            return ticket

    def abandon(self, ticket: int) -> None:
        """Release a ticket whose snapshot failed before worker submission."""
        with self._condition:
            self._abandoned_tickets.add(ticket)
            self._advance_abandoned()
            self._condition.notify_all()

    def run(self, ticket: int, write: Callable[[], object]) -> object:
        with self._condition:
            self._condition.wait_for(lambda: ticket == self._serving_ticket)
        try:
            return write()
        finally:
            with self._condition:
                self._serving_ticket += 1
                self._advance_abandoned()
                self._condition.notify_all()

    def _advance_abandoned(self) -> None:
        while self._serving_ticket in self._abandoned_tickets:
            self._abandoned_tickets.remove(self._serving_ticket)
            self._serving_ticket += 1


def _source_hash(function: Callable[..., Any]) -> str | None:
    try:
        source = inspect.getsource(function)
    except (OSError, TypeError):
        return None
    return hashlib.sha256(source.encode()).hexdigest()


def _supported_sources_are_loaded() -> bool:
    if version("agno") != _SUPPORTED_AGNO_VERSION:
        return False
    loaded_sources = {
        "agent_asave_session": _ORIGINAL_AGENT_ASAVE_SESSION,
        "agent_save_session": _ORIGINAL_AGENT_SAVE_SESSION,
        "agent_upsert_session": _ORIGINAL_AGENT_UPSERT_SESSION,
        "team_asave_session": _ORIGINAL_TEAM_ASAVE_SESSION,
        "team_save_session": _ORIGINAL_TEAM_SAVE_SESSION,
        "team_upsert_session": _ORIGINAL_TEAM_UPSERT_SESSION,
        "team_scrub_member_responses": _ORIGINAL_TEAM_SCRUB_MEMBER_RESPONSES,
    }
    return all(_source_hash(function) == _EXPECTED_SOURCE_HASHES[name] for name, function in loaded_sources.items())


def _database_queue(database: BaseDb) -> _DatabaseWriteQueue:
    with _DATABASE_QUEUES_GUARD:
        queue = _DATABASE_QUEUES.get(database)
        if queue is None:
            queue = _DatabaseWriteQueue()
            _DATABASE_QUEUES[database] = queue
        return queue


def _is_agno_background_task() -> bool:
    task = asyncio.current_task()
    return task is not None and (task in agent_run._background_tasks or task in team_run._background_tasks)


def _remove_transient_session_state(session: object) -> None:
    session_data = getattr(session, "session_data", None)
    if session_data is None:
        return
    session_state = session_data.get("session_state")
    if not isinstance(session_state, dict):
        return
    session_state.pop("current_session_id", None)
    session_state.pop("current_user_id", None)
    session_state.pop("current_run_id", None)


def _prepare_team_session_snapshot(team: Team, session: TeamSession) -> TeamSession:
    _remove_transient_session_state(session)
    if session.runs is not None:
        for run in session.runs:
            if not hasattr(run, "member_responses"):
                continue
            team_run_output = cast("TeamRunOutput", run)
            if not team.store_member_responses:
                team_run_output.member_responses = []
            else:
                _ORIGINAL_TEAM_SCRUB_MEMBER_RESPONSES(team, team_run_output.member_responses)
    return deepcopy(session)


async def _run_ordered_write(
    queue: _DatabaseWriteQueue,
    ticket: int,
    write: Callable[[], object],
) -> object:
    worker = asyncio.create_task(asyncio.to_thread(queue.run, ticket, write))
    cancellation: asyncio.CancelledError | None = None
    while not worker.done():
        try:
            await asyncio.shield(worker)
        except asyncio.CancelledError as error:
            cancellation = error
    result = worker.result()
    if cancellation is not None:
        raise cancellation
    return result


async def _agent_asave_session(agent: Agent, session: _AgentSession) -> None:
    database = agent.db
    if (
        database is None
        or agent.team_id is not None
        or agent.workflow_id is not None
        or session.session_data is None
        or agent_init.has_async_db(agent)
        or _is_agno_background_task()
    ):
        await _ORIGINAL_AGENT_ASAVE_SESSION(agent, session)
        return

    queue = _database_queue(cast("BaseDb", database))
    ticket = queue.reserve()
    try:
        _remove_transient_session_state(session)
        snapshot = deepcopy(session)
    except BaseException:
        queue.abandon(ticket)
        raise

    owner = cast("Agent", _DatabaseOwner(cast("BaseDb", database)))
    await _run_ordered_write(
        queue,
        ticket,
        lambda: _ORIGINAL_AGENT_UPSERT_SESSION(owner, snapshot),
    )
    agent_session.log_debug(f"Created or updated AgentSession record: {session.session_id}")


async def _team_asave_session(team: Team, session: TeamSession) -> None:
    database = team.db
    if (
        database is None
        or team.parent_team_id is not None
        or team.workflow_id is not None
        or team_has_async_db(team)
        or _is_agno_background_task()
    ):
        await _ORIGINAL_TEAM_ASAVE_SESSION(team, session)
        return

    queue = _database_queue(cast("BaseDb", database))
    ticket = queue.reserve()
    try:
        snapshot = _prepare_team_session_snapshot(team, session)
    except BaseException:
        queue.abandon(ticket)
        raise

    owner = cast("Team", _DatabaseOwner(cast("BaseDb", database)))
    await _run_ordered_write(
        queue,
        ticket,
        lambda: _ORIGINAL_TEAM_UPSERT_SESSION(owner, snapshot),
    )
    team_session.log_debug(f"Created or updated TeamSession record: {session.session_id}")


def _is_applied() -> bool:
    """Return whether both guarded async save replacements are installed."""
    return (
        _PATCHED
        and agent_session.asave_session is _agent_asave_session
        and team_session.asave_session is _team_asave_session
    )


def _apply_patch() -> bool:
    """Install the compatibility boundary when the pinned Agno sources match."""
    global _PATCHED
    if _is_applied():
        return True
    with _PATCH_LOCK:
        if _is_applied():
            return True
        if _PATCHED or not _supported_sources_are_loaded():
            return False
        agent_session.asave_session = cast("Any", _agent_asave_session)
        team_session.asave_session = cast("Any", _team_asave_session)
        _PATCHED = True
        return True


def install_patch() -> None:
    """Install the patch or fail closed on incompatible Agno sources."""
    if not _apply_patch():
        msg = (
            "Cannot install the synchronous session persistence boundary: "
            f"expected unchanged Agno {_SUPPORTED_AGNO_VERSION} session sources"
        )
        raise RuntimeError(msg)
