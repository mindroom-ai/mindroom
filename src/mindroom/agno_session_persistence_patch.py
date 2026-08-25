"""Guarded offload for synchronous Agno session persistence."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import threading
import weakref
from importlib.metadata import version
from typing import TYPE_CHECKING, Any, cast

from agno.agent import _init as agent_init
from agno.agent import _session as agent_session
from agno.team import _session as team_session
from agno.team._init import _has_async_db as team_has_async_db

if TYPE_CHECKING:
    from collections.abc import Callable

    from agno.agent import Agent
    from agno.db.base import BaseDb
    from agno.session import AgentSession, TeamSession, WorkflowSession
    from agno.team import Team

    type _AgentSession = AgentSession | TeamSession | WorkflowSession
    type _SyncSave = Callable[[Any, Any], None]

_SUPPORTED_AGNO_VERSION = "2.6.12"
_EXPECTED_SOURCE_HASHES = {
    "agent_asave_session": "fe0e574c43667d54ef958a183473039ad946a51bf95878ae2e7fb50394123fc3",
    "agent_save_session": "7866d1577e338788b88b1ffb456fd153f35a8cfbf9c0b645d7bf234bfb9e9d95",
    "team_asave_session": "3038fcf9871a3e35c8080e895529ef952bbd5f4d0cd60bff6702e048141088e1",
    "team_save_session": "c64763b434ed048b87a89209685a1c41a13d8531357dd2ed3270d5ae94942c4d",
}

_ORIGINAL_AGENT_ASAVE_SESSION = agent_session.asave_session
_ORIGINAL_AGENT_SAVE_SESSION = agent_session.save_session
_ORIGINAL_TEAM_ASAVE_SESSION = team_session.asave_session
_ORIGINAL_TEAM_SAVE_SESSION = team_session.save_session

_PATCHED = False
_PATCH_LOCK = threading.Lock()
_DATABASE_LOCKS_GUARD = threading.Lock()
_DATABASE_LOCKS: weakref.WeakKeyDictionary[BaseDb, asyncio.Lock] = weakref.WeakKeyDictionary()


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
        "team_asave_session": _ORIGINAL_TEAM_ASAVE_SESSION,
        "team_save_session": _ORIGINAL_TEAM_SAVE_SESSION,
    }
    return all(_source_hash(function) == _EXPECTED_SOURCE_HASHES[name] for name, function in loaded_sources.items())


def _database_lock(database: BaseDb) -> asyncio.Lock:
    with _DATABASE_LOCKS_GUARD:
        lock = _DATABASE_LOCKS.get(database)
        if lock is None:
            lock = asyncio.Lock()
            _DATABASE_LOCKS[database] = lock
        return lock


async def _ordered_thread_save(database: BaseDb, save: _SyncSave, owner: object, session: object) -> None:
    async with _database_lock(database):
        await asyncio.to_thread(save, owner, session)


async def _agent_asave_session(agent: Agent, session: _AgentSession) -> None:
    database = agent.db
    if database is None or agent_init.has_async_db(agent):
        await _ORIGINAL_AGENT_ASAVE_SESSION(agent, session)
        return
    await _ordered_thread_save(cast("BaseDb", database), _ORIGINAL_AGENT_SAVE_SESSION, agent, session)


async def _team_asave_session(team: Team, session: TeamSession) -> None:
    database = team.db
    if database is None or team_has_async_db(team):
        await _ORIGINAL_TEAM_ASAVE_SESSION(team, session)
        return
    await _ordered_thread_save(cast("BaseDb", database), _ORIGINAL_TEAM_SAVE_SESSION, team, session)


def _is_applied() -> bool:
    """Return whether both guarded async save replacements are installed."""
    return (
        _PATCHED
        and agent_session.asave_session is _agent_asave_session
        and team_session.asave_session is _team_asave_session
    )


def apply_patch() -> bool:
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
