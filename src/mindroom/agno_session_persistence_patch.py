"""Guarded offload for synchronous Agno session persistence."""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import inspect
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
_PERSISTENCE_LOCK = threading.RLock()
_PERSISTENCE_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="session-persistence")
_REGISTERED_TARGETS: weakref.WeakKeyDictionary[BaseDb, _RegisteredPersistenceTarget] = weakref.WeakKeyDictionary()
_TARGET_TAILS: dict[_RegisteredPersistenceTarget, Future[object]] = {}


@dataclass(frozen=True)
class _RegisteredPersistenceTarget:
    """Opaque identity for one explicitly registered persistence target."""

    digest: bytes = field(repr=False)


@dataclass
class _ReservedPersistenceWrite:
    """Tail placeholder whose write starts only after preparation and its predecessor."""

    target: _RegisteredPersistenceTarget
    completion: Future[object]
    predecessor_finished: bool = False
    context: contextvars.Context | None = None
    write: Callable[[], object] | None = None
    preparation_error: BaseException | None = None
    started: bool = False


@dataclass(frozen=True)
class _DatabaseOwner:
    """Minimal owner accepted by Agno's captured storage helpers."""

    db: BaseDb


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


def _target_digest(*parts: str) -> bytes:
    digest = hashlib.sha256()
    for part in parts:
        encoded = part.encode()
        digest.update(len(encoded).to_bytes(8, byteorder="big"))
        digest.update(encoded)
    return digest.digest()


def _register_sync_session_storage(
    database: BaseDb,
    *,
    db_file: str,
    session_table: str,
) -> None:
    """Opt one application-owned synchronous session database into offloading."""
    target = _RegisteredPersistenceTarget(
        _target_digest("sqlite", str(Path(db_file).resolve()), session_table),
    )
    with _PERSISTENCE_LOCK:
        _REGISTERED_TARGETS[database] = target


def _registered_target(database: BaseDb) -> _RegisteredPersistenceTarget | None:
    with _PERSISTENCE_LOCK:
        return _REGISTERED_TARGETS.get(database)


def _remove_completed_tail(
    target: _RegisteredPersistenceTarget,
    completion: Future[object],
    _finished: Future[object],
) -> None:
    with _PERSISTENCE_LOCK:
        if _TARGET_TAILS.get(target) is completion:
            del _TARGET_TAILS[target]


def _transfer_worker_result(completion: Future[object], worker: Future[object]) -> None:
    try:
        result = worker.result()
    except BaseException as error:
        completion.set_exception(error)
    else:
        completion.set_result(result)


def _start_registered_write(
    completion: Future[object],
    context: contextvars.Context,
    write: Callable[[], object],
) -> None:
    try:
        worker = _PERSISTENCE_EXECUTOR.submit(context.run, write)
    except BaseException as error:
        completion.set_exception(error)
        return
    worker.add_done_callback(partial(_transfer_worker_result, completion))


def _take_ready_registered_write(
    reservation: _ReservedPersistenceWrite,
) -> tuple[Future[object], contextvars.Context, Callable[[], object]] | None:
    if (
        reservation.started
        or not reservation.predecessor_finished
        or reservation.preparation_error is not None
        or reservation.context is None
        or reservation.write is None
    ):
        return None
    reservation.started = True
    return reservation.completion, reservation.context, reservation.write


def _take_ready_registered_failure(reservation: _ReservedPersistenceWrite) -> BaseException | None:
    if reservation.started or not reservation.predecessor_finished or reservation.preparation_error is None:
        return None
    reservation.started = True
    return reservation.preparation_error


def _predecessor_finished(
    reservation: _ReservedPersistenceWrite,
    _predecessor: Future[object],
) -> None:
    with _PERSISTENCE_LOCK:
        reservation.predecessor_finished = True
        ready_error = _take_ready_registered_failure(reservation)
        ready_write = None if ready_error is not None else _take_ready_registered_write(reservation)
    if ready_error is not None:
        reservation.completion.set_exception(ready_error)
    elif ready_write is not None:
        _start_registered_write(*ready_write)


def _reserve_registered_write(
    target: _RegisteredPersistenceTarget,
) -> _ReservedPersistenceWrite:
    completion: Future[object] = Future()
    reservation = _ReservedPersistenceWrite(target=target, completion=completion)
    completion.add_done_callback(partial(_remove_completed_tail, target, completion))
    with _PERSISTENCE_LOCK:
        predecessor = _TARGET_TAILS.get(target)
        _TARGET_TAILS[target] = completion
        if predecessor is None:
            reservation.predecessor_finished = True
        else:
            predecessor.add_done_callback(
                partial(_predecessor_finished, reservation),
            )
    return reservation


def _fail_reserved_write(reservation: _ReservedPersistenceWrite, error: BaseException) -> None:
    with _PERSISTENCE_LOCK:
        reservation.preparation_error = error
        ready_error = _take_ready_registered_failure(reservation)
    if ready_error is not None:
        reservation.completion.set_exception(ready_error)


def _attach_registered_write(
    reservation: _ReservedPersistenceWrite,
    write: Callable[[], object],
) -> Future[object]:
    try:
        context = contextvars.copy_context()
    except BaseException as error:
        _fail_reserved_write(reservation, error)
        raise

    with _PERSISTENCE_LOCK:
        reservation.context = context
        reservation.write = write
        ready_write = _take_ready_registered_write(reservation)
    if ready_write is not None:
        _start_registered_write(*ready_write)
    return reservation.completion


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


async def _await_registered_write(worker: Future[object]) -> object:
    wrapped_worker = asyncio.wrap_future(worker)
    cancellation: asyncio.CancelledError | None = None
    while not wrapped_worker.done():
        try:
            await asyncio.shield(wrapped_worker)
        except asyncio.CancelledError as error:
            cancellation = error
    result = wrapped_worker.result()
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
    ):
        await _ORIGINAL_AGENT_ASAVE_SESSION(agent, session)
        return

    target = _registered_target(cast("BaseDb", database))
    if target is None:
        await _ORIGINAL_AGENT_ASAVE_SESSION(agent, session)
        return

    reservation = _reserve_registered_write(target)
    if _is_agno_background_task():
        _attach_registered_write(
            reservation,
            lambda: _ORIGINAL_AGENT_SAVE_SESSION(agent, session),
        ).result()
        return

    try:
        _remove_transient_session_state(session)
        snapshot = deepcopy(session)
        owner = cast("Agent", _DatabaseOwner(cast("BaseDb", database)))
    except BaseException as error:
        _fail_reserved_write(reservation, error)
        raise
    await _await_registered_write(
        _attach_registered_write(
            reservation,
            lambda: _ORIGINAL_AGENT_UPSERT_SESSION(owner, snapshot),
        ),
    )
    agent_session.log_debug(f"Created or updated AgentSession record: {session.session_id}")


async def _team_asave_session(team: Team, session: TeamSession) -> None:
    database = team.db
    if database is None or team.parent_team_id is not None or team.workflow_id is not None or team_has_async_db(team):
        await _ORIGINAL_TEAM_ASAVE_SESSION(team, session)
        return

    target = _registered_target(cast("BaseDb", database))
    if target is None:
        await _ORIGINAL_TEAM_ASAVE_SESSION(team, session)
        return

    reservation = _reserve_registered_write(target)
    if _is_agno_background_task():
        _attach_registered_write(
            reservation,
            lambda: _ORIGINAL_TEAM_SAVE_SESSION(team, session),
        ).result()
        return

    try:
        snapshot = _prepare_team_session_snapshot(team, session)
        owner = cast("Team", _DatabaseOwner(cast("BaseDb", database)))
    except BaseException as error:
        _fail_reserved_write(reservation, error)
        raise
    await _await_registered_write(
        _attach_registered_write(
            reservation,
            lambda: _ORIGINAL_TEAM_UPSERT_SESSION(owner, snapshot),
        ),
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
