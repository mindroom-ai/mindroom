"""Behavioral tests for the guarded Agno session persistence boundary."""

from __future__ import annotations

import asyncio
import gc
import subprocess
import threading
import time
import weakref
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from copy import deepcopy as copy_session
from typing import TYPE_CHECKING, Literal

import pytest
from agno.agent import Agent
from agno.agent import _run as agent_run
from agno.db.postgres import PostgresDb
from agno.db.sqlite import SqliteDb
from agno.db.sqlite.async_sqlite import AsyncSqliteDb
from agno.models.message import Message
from agno.run.agent import RunOutput
from agno.run.team import TeamRunOutput
from agno.session.agent import AgentSession
from agno.session.team import TeamSession
from agno.session.workflow import WorkflowSession
from agno.team import Team
from agno.team import _run as team_run
from agno.workflow import Workflow
from sqlalchemy import create_engine

from mindroom import agno_session_persistence_patch as persistence_patch
from mindroom.agent_storage import create_state_storage, get_agent_session, get_team_session

if TYPE_CHECKING:
    from pathlib import Path

    from agno.db.base import BaseDb

type _Surface = Literal["agent", "team"]


def _storage(tmp_path: Path, name: str = "sessions") -> BaseDb:
    return create_state_storage(
        name,
        tmp_path,
        subdir=name,
        session_table=f"{name}_sessions",
        prompt_roles=frozenset({"system", "developer"}),
    )


def _owner_and_session(
    surface: _Surface,
    storage: BaseDb,
    session_id: str,
) -> tuple[Agent | Team, AgentSession | TeamSession]:
    session_data = {"session_state": {"current_run_id": "run"}}
    messages = [Message(role="user", content=session_id)]
    if surface == "agent":
        return Agent(db=storage, telemetry=False), AgentSession(
            session_id=session_id,
            session_data=session_data,
            created_at=int(time.time()),
            runs=[RunOutput(run_id="run", session_id=session_id, messages=messages)],
        )
    return Team(db=storage, members=[], telemetry=False), TeamSession(
        session_id=session_id,
        session_data=session_data,
        created_at=int(time.time()),
        runs=[TeamRunOutput(run_id="run", session_id=session_id, messages=messages)],
    )


def test_supported_agno_session_sources_are_patched() -> None:
    """Pinned source compatibility must remain visible to CI."""
    assert persistence_patch._apply_patch() is True
    assert persistence_patch._is_applied() is True


def test_installation_fails_closed_when_a_pinned_source_drifts() -> None:
    """The application must not silently restore event-loop blocking after drift."""
    code = """
from importlib import import_module

patch = import_module("mindroom.agno_session_persistence_patch")
patch._EXPECTED_SOURCE_HASHES["agent_asave_session"] = "drift"
try:
    import_module("mindroom.agent_storage")
except RuntimeError as error:
    assert "session persistence" in str(error).lower()
else:
    raise AssertionError("source drift did not fail closed")
"""
    result = subprocess.run(
        ["uv", "run", "python", "-c", code],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_sync_saves_are_ordered_across_event_loops(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One shared DB must preserve save invocation order across event loops."""
    storage = _storage(tmp_path)
    agent, agent_session = _owner_and_session("agent", storage, "first")
    team, team_session = _owner_and_session("team", storage, "second")
    first_started = threading.Event()
    release_first = threading.Event()
    started: list[str] = []
    started_guard = threading.Lock()
    errors: list[BaseException] = []
    original_upsert = storage.upsert_session

    def ordered_probe(
        session: AgentSession | TeamSession,
        deserialize: bool | None = True,
    ) -> object:
        with started_guard:
            started.append(session.session_id)
        if session.session_id == "first":
            first_started.set()
            assert release_first.wait(timeout=5)
        return original_upsert(session, deserialize=deserialize)

    def run_save(owner: Agent | Team, session: AgentSession | TeamSession) -> None:
        try:
            asyncio.run(owner.asave_session(session))
        except BaseException as error:  # pragma: no cover - asserted below
            errors.append(error)

    monkeypatch.setattr(storage, "upsert_session", ordered_probe)
    first = threading.Thread(target=run_save, args=(agent, agent_session))
    second = threading.Thread(target=run_save, args=(team, team_session))
    first.start()
    try:
        assert first_started.wait(timeout=5)
        second.start()
        time.sleep(0.1)
        assert started == ["first"]
    finally:
        release_first.set()
        first.join(timeout=5)
        second.join(timeout=5)
        storage.close()

    assert not first.is_alive()
    assert not second.is_alive()
    assert errors == []
    assert started == ["first", "second"]


def test_distinct_sqlite_handles_share_write_order(  # noqa: PLR0915
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Handles for one SQLite table must not commit an older save last."""
    first_storage = _storage(tmp_path, "shared-target")
    second_storage = _storage(tmp_path, "shared-target")
    first_owner, first_session = _owner_and_session("agent", first_storage, "shared")
    second_owner, second_session = _owner_and_session("agent", second_storage, "shared")
    baseline_owner, baseline_session = _owner_and_session("agent", first_storage, "baseline")
    asyncio.run(baseline_owner.asave_session(baseline_session))
    first_session.user_id = "user"
    second_session.user_id = "user"
    first_session.metadata = {"write": "first"}
    second_session.metadata = {"write": "second"}
    first_started = threading.Event()
    second_started = threading.Event()
    release_first = threading.Event()
    errors: list[BaseException] = []
    original_first_upsert = first_storage.upsert_session
    original_second_upsert = second_storage.upsert_session

    def first_upsert(
        session: AgentSession,
        deserialize: bool | None = True,
    ) -> object:
        first_started.set()
        assert release_first.wait(timeout=5)
        return original_first_upsert(session, deserialize=deserialize)

    def second_upsert(
        session: AgentSession,
        deserialize: bool | None = True,
    ) -> object:
        second_started.set()
        return original_second_upsert(session, deserialize=deserialize)

    def run_save(owner: Agent, session: AgentSession) -> None:
        try:
            asyncio.run(owner.asave_session(session))
        except BaseException as error:  # pragma: no cover - asserted below
            errors.append(error)

    monkeypatch.setattr(first_storage, "upsert_session", first_upsert)
    monkeypatch.setattr(second_storage, "upsert_session", second_upsert)
    first = threading.Thread(target=run_save, args=(first_owner, first_session))
    second = threading.Thread(target=run_save, args=(second_owner, second_session))
    first.start()
    try:
        assert first_started.wait(timeout=5)
        second.start()
        assert not second_started.wait(timeout=0.2)
        release_first.set()
        first.join(timeout=5)
        second.join(timeout=5)
        persisted = get_agent_session(first_storage, "shared")
    finally:
        release_first.set()
        first.join(timeout=5)
        second.join(timeout=5)
        first_storage.close()
        second_storage.close()

    assert not first.is_alive()
    assert not second.is_alive()
    assert errors == []
    assert persisted is not None
    assert persisted.metadata == {"write": "second"}


@pytest.mark.parametrize("surface", ["agent", "team"])
def test_snapshot_reservation_preserves_cross_loop_invocation_order(  # noqa: PLR0915
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    surface: _Surface,
) -> None:
    """A later snapshot must remain behind an earlier blocked snapshot."""
    first_storage = _storage(tmp_path, f"{surface}-snapshot-order")
    second_storage = _storage(tmp_path, f"{surface}-snapshot-order")
    baseline_owner, baseline_session = _owner_and_session(surface, first_storage, "baseline")
    asyncio.run(baseline_owner.asave_session(baseline_session))
    first_owner, first_session = _owner_and_session(surface, first_storage, "shared")
    second_owner, second_session = _owner_and_session(surface, second_storage, "shared")
    first_session.metadata = {"write": "first"}
    second_session.metadata = {"write": "second"}
    first_snapshot_started = threading.Event()
    second_snapshot_finished = threading.Event()
    release_first_snapshot = threading.Event()
    first_write_started = threading.Event()
    second_write_started = threading.Event()
    release_first_write = threading.Event()
    first_finished = threading.Event()
    second_finished = threading.Event()
    errors: list[BaseException] = []
    original_first_upsert = first_storage.upsert_session
    original_second_upsert = second_storage.upsert_session

    def controlled_deepcopy(session: AgentSession | TeamSession) -> AgentSession | TeamSession:
        if session.metadata == {"write": "first"}:
            first_snapshot_started.set()
            assert release_first_snapshot.wait(timeout=5)
        else:
            second_snapshot_finished.set()
        return copy_session(session)

    def first_upsert(
        session: AgentSession | TeamSession,
        deserialize: bool | None = True,
    ) -> object:
        first_write_started.set()
        assert release_first_write.wait(timeout=5)
        return original_first_upsert(session, deserialize=deserialize)

    def second_upsert(
        session: AgentSession | TeamSession,
        deserialize: bool | None = True,
    ) -> object:
        second_write_started.set()
        return original_second_upsert(session, deserialize=deserialize)

    def run_save(
        owner: Agent | Team,
        session: AgentSession | TeamSession,
        finished: threading.Event,
    ) -> None:
        try:
            asyncio.run(owner.asave_session(session))
        except BaseException as error:  # pragma: no cover - asserted below
            errors.append(error)
        finally:
            finished.set()

    monkeypatch.setattr(persistence_patch, "deepcopy", controlled_deepcopy)
    monkeypatch.setattr(first_storage, "upsert_session", first_upsert)
    monkeypatch.setattr(second_storage, "upsert_session", second_upsert)
    first = threading.Thread(target=run_save, args=(first_owner, first_session, first_finished))
    second = threading.Thread(target=run_save, args=(second_owner, second_session, second_finished))
    first.start()
    try:
        assert first_snapshot_started.wait(timeout=5)
        second.start()
        assert second_snapshot_finished.wait(timeout=5)
        assert not second_write_started.wait(timeout=0.2)
        assert not second_finished.is_set()
        release_first_snapshot.set()
        assert first_write_started.wait(timeout=5)
        assert not second_write_started.is_set()
        release_first_write.set()
        first.join(timeout=5)
        second.join(timeout=5)
        persisted = (
            get_agent_session(first_storage, "shared")
            if surface == "agent"
            else get_team_session(first_storage, "shared")
        )
    finally:
        release_first_snapshot.set()
        release_first_write.set()
        first.join(timeout=5)
        second.join(timeout=5)
        first_storage.close()
        second_storage.close()

    assert not first.is_alive()
    assert not second.is_alive()
    assert first_finished.is_set()
    assert second_finished.is_set()
    assert second_write_started.is_set()
    assert errors == []
    assert persisted is not None
    assert persisted.metadata == {"write": "second"}


@pytest.mark.parametrize("surface", ["agent", "team"])
def test_snapshot_failure_releases_reservation_and_tail(  # noqa: PLR0915
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    surface: _Surface,
) -> None:
    """Snapshot failure must preserve its error and unblock its successor."""
    first_storage = _storage(tmp_path, f"{surface}-snapshot-failure")
    second_storage = _storage(tmp_path, f"{surface}-snapshot-failure")
    baseline_owner, baseline_session = _owner_and_session(surface, first_storage, "baseline")
    asyncio.run(baseline_owner.asave_session(baseline_session))
    predecessor_owner, predecessor_session = _owner_and_session(surface, first_storage, "shared")
    failing_owner, failing_session = _owner_and_session(surface, second_storage, "shared")
    successor_owner, successor_session = _owner_and_session(surface, second_storage, "shared")
    predecessor_session.metadata = {"write": "predecessor"}
    failing_session.metadata = {"write": "failure"}
    successor_session.metadata = {"write": "successor"}
    target = persistence_patch._registered_target(first_storage)
    predecessor_write_started = threading.Event()
    release_predecessor_write = threading.Event()
    failure_snapshot_started = threading.Event()
    release_failure_snapshot = threading.Event()
    successor_snapshot_finished = threading.Event()
    successor_write_started = threading.Event()
    predecessor_finished = threading.Event()
    failure_finished = threading.Event()
    successor_finished = threading.Event()
    failure = RuntimeError("snapshot failed")
    predecessor_errors: list[BaseException] = []
    failure_errors: list[BaseException] = []
    successor_errors: list[BaseException] = []
    original_first_upsert = first_storage.upsert_session
    original_second_upsert = second_storage.upsert_session

    def failing_deepcopy(session: AgentSession | TeamSession) -> AgentSession | TeamSession:
        if session.metadata == {"write": "failure"}:
            failure_snapshot_started.set()
            assert release_failure_snapshot.wait(timeout=5)
            raise failure
        if session.metadata == {"write": "successor"}:
            successor_snapshot_finished.set()
        return copy_session(session)

    def first_upsert(
        session: AgentSession | TeamSession,
        deserialize: bool | None = True,
    ) -> object:
        predecessor_write_started.set()
        assert release_predecessor_write.wait(timeout=5)
        return original_first_upsert(session, deserialize=deserialize)

    def second_upsert(
        session: AgentSession | TeamSession,
        deserialize: bool | None = True,
    ) -> object:
        successor_write_started.set()
        return original_second_upsert(session, deserialize=deserialize)

    def run_save(
        owner: Agent | Team,
        session: AgentSession | TeamSession,
        finished: threading.Event,
        errors: list[BaseException],
    ) -> None:
        try:
            asyncio.run(owner.asave_session(session))
        except BaseException as error:
            errors.append(error)
        finally:
            finished.set()

    monkeypatch.setattr(persistence_patch, "deepcopy", failing_deepcopy)
    monkeypatch.setattr(first_storage, "upsert_session", first_upsert)
    monkeypatch.setattr(second_storage, "upsert_session", second_upsert)
    predecessor = threading.Thread(
        target=run_save,
        args=(predecessor_owner, predecessor_session, predecessor_finished, predecessor_errors),
    )
    failing = threading.Thread(
        target=run_save,
        args=(failing_owner, failing_session, failure_finished, failure_errors),
    )
    successor = threading.Thread(
        target=run_save,
        args=(successor_owner, successor_session, successor_finished, successor_errors),
    )
    predecessor.start()
    try:
        assert predecessor_write_started.wait(timeout=5)
        predecessor_tail = persistence_patch._TARGET_TAILS.get(target)
        assert predecessor_tail is not None
        failing.start()
        assert failure_snapshot_started.wait(timeout=5)
        assert persistence_patch._TARGET_TAILS.get(target) is not predecessor_tail
        successor.start()
        assert successor_snapshot_finished.wait(timeout=5)
        release_failure_snapshot.set()
        failing.join(timeout=5)
        assert failure_errors == [failure]
        assert not successor_write_started.wait(timeout=0.2)
        assert not successor_finished.is_set()
        release_predecessor_write.set()
        predecessor.join(timeout=5)
        successor.join(timeout=5)
        persisted = (
            get_agent_session(first_storage, "shared")
            if surface == "agent"
            else get_team_session(first_storage, "shared")
        )
    finally:
        release_failure_snapshot.set()
        release_predecessor_write.set()
        for thread in (predecessor, failing, successor):
            if thread.ident is not None:
                thread.join(timeout=5)
        first_storage.close()
        second_storage.close()

    assert not predecessor.is_alive()
    assert not failing.is_alive()
    assert not successor.is_alive()
    assert predecessor_finished.is_set()
    assert failure_finished.is_set()
    assert successor_finished.is_set()
    assert predecessor_errors == []
    assert failure_errors == [failure]
    assert successor_errors == []
    assert persisted is not None
    assert persisted.metadata == {"write": "successor"}
    assert target not in persistence_patch._TARGET_TAILS


def test_application_storage_is_registered_with_an_opaque_target(tmp_path: Path) -> None:
    """Only the application constructor opts a synchronous DB into offloading."""
    storage = _storage(tmp_path, "registered")
    try:
        target = persistence_patch._registered_target(storage)
    finally:
        storage.close()

    assert target is not None
    assert str(tmp_path) not in repr(target)
    assert "registered" not in repr(target)


def test_storage_registration_has_weak_lifecycle(tmp_path: Path) -> None:
    """Registration must not retain a DB after application ownership ends."""
    storage = _storage(tmp_path, "weak-registration")
    storage_reference = weakref.ref(storage)

    assert persistence_patch._registered_target(storage) is not None
    storage.close()
    del storage
    gc.collect()

    assert storage_reference() is None


@pytest.mark.asyncio
async def test_unregistered_postgres_delegates_upstream_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """Arbitrary PostgreSQL engines, including connect args, are not offloaded."""
    database = PostgresDb(
        db_engine=create_engine(
            "postgresql+psycopg://user:credential@localhost/app",
            connect_args={"connect_timeout": 1},
        ),
        db_schema="sessions",
        session_table="records",
    )
    owner = Agent(db=database, telemetry=False)
    session = AgentSession(session_id="postgres", session_data={"session_state": {}})
    event_loop_thread = threading.get_ident()
    observed: list[tuple[int, AgentSession]] = []

    def upstream_probe(session: AgentSession, deserialize: bool | None = True) -> AgentSession:
        _ = deserialize
        observed.append((threading.get_ident(), session))
        return session

    monkeypatch.setattr(database, "upsert_session", upstream_probe)
    try:
        await owner.asave_session(session)
    finally:
        database.close()

    assert persistence_patch._registered_target(database) is None
    assert observed == [(event_loop_thread, session)]


@pytest.mark.parametrize(
    "db_url",
    [
        "sqlite:///:memory:",
        "sqlite:///file:shared?mode=memory&cache=shared&uri=true",
        "sqlite:///file:relative.db?mode=rwc&uri=true",
    ],
)
@pytest.mark.asyncio
async def test_unregistered_sqlite_uri_delegates_upstream_unchanged(
    db_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Arbitrary SQLite URI spellings do not opt into application offloading."""
    database = SqliteDb(db_url=db_url, session_table="records")
    owner = Agent(db=database, telemetry=False)
    session = AgentSession(session_id="sqlite", session_data={"session_state": {}})
    event_loop_thread = threading.get_ident()
    observed: list[int] = []

    def upstream_probe(session: AgentSession, deserialize: bool | None = True) -> AgentSession:
        _ = deserialize
        observed.append(threading.get_ident())
        return session

    monkeypatch.setattr(database, "upsert_session", upstream_probe)
    try:
        await owner.asave_session(session)
    finally:
        database.close()

    assert persistence_patch._registered_target(database) is None
    assert observed == [event_loop_thread]


@pytest.mark.asyncio
async def test_cancellation_drains_write_before_later_save_and_close(  # noqa: PLR0915
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation must not detach a worker or release its ordering ticket."""
    storage = _storage(tmp_path)
    first_owner, first_session = _owner_and_session("agent", storage, "first")
    second_owner, second_session = _owner_and_session("team", storage, "second")
    first_started = threading.Event()
    release_first = threading.Event()
    started: list[str] = []
    active_writes = 0
    original_upsert = storage.upsert_session
    original_close = storage.close

    def blocking_upsert(
        session: AgentSession | TeamSession,
        deserialize: bool | None = True,
    ) -> object:
        nonlocal active_writes
        started.append(session.session_id)
        active_writes += 1
        try:
            if session.session_id == "first":
                first_started.set()
                assert release_first.wait(timeout=5)
            return original_upsert(session, deserialize=deserialize)
        finally:
            active_writes -= 1

    def checked_close() -> None:
        assert active_writes == 0
        original_close()

    def eventual_release() -> None:
        if first_started.wait(timeout=5):
            time.sleep(0.4)
            release_first.set()

    monkeypatch.setattr(storage, "upsert_session", blocking_upsert)
    monkeypatch.setattr(storage, "close", checked_close)
    release_thread = threading.Thread(target=eventual_release)
    release_thread.start()
    first = asyncio.create_task(first_owner.asave_session(first_session))
    second: asyncio.Task[None] | None = None
    try:
        assert await asyncio.to_thread(first_started.wait, 5)
        first.cancel()
        second = asyncio.create_task(second_owner.asave_session(second_session))
        await asyncio.sleep(0.05)
        assert not first.done()
        assert started == ["first"]
        release_first.set()
        with pytest.raises(asyncio.CancelledError):
            await first
        await second
        storage.close()
    finally:
        release_first.set()
        release_thread.join(timeout=1)
        tasks = [first, *([second] if second is not None else [])]
        await asyncio.gather(*tasks, return_exceptions=True)
        if storage.db_engine is not None:
            original_close()

    assert started == ["first", "second"]


@pytest.mark.asyncio
async def test_worker_error_propagates_and_does_not_strand_later_save(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unexpected worker errors must surface and advance the database queue."""
    storage = _storage(tmp_path)
    owner, first_session = _owner_and_session("agent", storage, "first")
    _, second_session = _owner_and_session("agent", storage, "second")
    original_upsert = persistence_patch._ORIGINAL_AGENT_UPSERT_SESSION
    calls = 0

    def fail_once(agent: Agent, session: AgentSession) -> object:
        nonlocal calls
        calls += 1
        if calls == 1:
            error_message = "write failed"
            raise RuntimeError(error_message)
        return original_upsert(agent, session)

    monkeypatch.setattr(persistence_patch, "_ORIGINAL_AGENT_UPSERT_SESSION", fail_once)
    try:
        with pytest.raises(RuntimeError, match="write failed"):
            await owner.asave_session(first_session)
        await owner.asave_session(second_session)
        assert get_agent_session(storage, "second") is not None
    finally:
        storage.close()


@pytest.mark.asyncio
async def test_submission_failure_does_not_strand_later_save(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Executor submission failure must abandon its reserved queue ticket."""
    storage = _storage(tmp_path, "submission-failure")
    owner, first_session = _owner_and_session("agent", storage, "first")
    _, second_session = _owner_and_session("agent", storage, "second")
    target = persistence_patch._registered_target(storage)
    original_submit = persistence_patch._PERSISTENCE_EXECUTOR.submit

    def fail_submission(*_args: object, **_kwargs: object) -> object:
        error_message = "executor unavailable"
        raise RuntimeError(error_message)

    monkeypatch.setattr(persistence_patch._PERSISTENCE_EXECUTOR, "submit", fail_submission)
    try:
        with pytest.raises(RuntimeError, match="executor unavailable"):
            await owner.asave_session(first_session)
        assert target not in persistence_patch._TARGET_TAILS
        monkeypatch.setattr(persistence_patch._PERSISTENCE_EXECUTOR, "submit", original_submit)
        await owner.asave_session(second_session)
        assert get_agent_session(storage, "second") is not None
    finally:
        storage.close()


@pytest.mark.asyncio
async def test_completed_target_tail_is_removed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Completed writes must not retain targets, futures, or write closures."""
    storage = _storage(tmp_path, "tail-lifecycle")
    owner, session = _owner_and_session("agent", storage, "tail")
    target = persistence_patch._registered_target(storage)
    write_started = threading.Event()
    release_write = threading.Event()
    original_upsert = storage.upsert_session

    def blocked_upsert(
        session: AgentSession,
        deserialize: bool | None = True,
    ) -> object:
        write_started.set()
        assert release_write.wait(timeout=5)
        return original_upsert(session, deserialize=deserialize)

    monkeypatch.setattr(storage, "upsert_session", blocked_upsert)
    save = asyncio.create_task(owner.asave_session(session))
    try:
        assert await asyncio.to_thread(write_started.wait, 5)
        assert persistence_patch._TARGET_TAILS.get(target) is not None
        release_write.set()
        await save
    finally:
        release_write.set()
        await asyncio.gather(save, return_exceptions=True)
        storage.close()

    assert target not in persistence_patch._TARGET_TAILS


@pytest.mark.asyncio
async def test_registered_save_ignores_default_executor_starvation(tmp_path: Path) -> None:
    """Registered persistence must use a lane independent of loop-default work."""
    loop = asyncio.get_running_loop()
    await asyncio.to_thread(lambda: None)
    original_default_executor = loop._default_executor
    assert original_default_executor is not None
    starved_executor = ThreadPoolExecutor(max_workers=1)
    loop.set_default_executor(starved_executor)
    default_worker_started = threading.Event()
    release_default_worker = asyncio.Event()
    save_finished = threading.Event()
    watchdog_released_default = threading.Event()

    def block_on_event_loop() -> None:
        default_worker_started.set()
        waiter = asyncio.run_coroutine_threadsafe(release_default_worker.wait(), loop)
        assert waiter.result(timeout=10) is True

    def release_if_save_stalls() -> None:
        if not save_finished.wait(timeout=5):
            watchdog_released_default.set()
            loop.call_soon_threadsafe(release_default_worker.set)

    blocker = loop.run_in_executor(None, block_on_event_loop)
    watchdog = threading.Thread(target=release_if_save_stalls)
    watchdog.start()
    storage = _storage(tmp_path, "dedicated-executor")
    owner, session = _owner_and_session("agent", storage, "dedicated")
    try:
        assert default_worker_started.wait(timeout=5)
        await owner.asave_session(session)
        save_finished.set()
    finally:
        save_finished.set()
        release_default_worker.set()
        await blocker
        loop.set_default_executor(original_default_executor)
        starved_executor.shutdown(wait=True)
        watchdog.join(timeout=1)
        storage.close()

    assert not watchdog_released_default.is_set()


@pytest.mark.asyncio
async def test_worker_receives_the_submission_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Replacing to_thread must retain its context-variable propagation."""
    storage = _storage(tmp_path, "context")
    owner, session = _owner_and_session("agent", storage, "context")
    marker: ContextVar[str] = ContextVar("session_persistence_marker", default="missing")
    observed: list[str] = []
    original_upsert = storage.upsert_session

    def context_probe(
        session: AgentSession,
        deserialize: bool | None = True,
    ) -> object:
        observed.append(marker.get())
        return original_upsert(session, deserialize=deserialize)

    monkeypatch.setattr(storage, "upsert_session", context_probe)
    reset_token = marker.set("present")
    try:
        await owner.asave_session(session)
    finally:
        marker.reset(reset_token)
        storage.close()

    assert observed == ["present"]


@pytest.mark.asyncio
async def test_worker_error_wins_over_cancellation_after_drain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cancelled waiter must still observe a submitted worker failure."""
    storage = _storage(tmp_path, "worker-error")
    owner, session = _owner_and_session("agent", storage, "worker-error")
    write_started = threading.Event()
    release_write = threading.Event()

    def failing_upsert(_agent: Agent, _session: AgentSession) -> None:
        write_started.set()
        assert release_write.wait(timeout=5)
        error_message = "write failed after cancellation"
        raise RuntimeError(error_message)

    monkeypatch.setattr(persistence_patch, "_ORIGINAL_AGENT_UPSERT_SESSION", failing_upsert)
    save = asyncio.create_task(owner.asave_session(session))
    try:
        assert await asyncio.to_thread(write_started.wait, 5)
        save.cancel()
        await asyncio.sleep(0)
        assert not save.done()
        release_write.set()
        with pytest.raises(RuntimeError, match="write failed after cancellation"):
            await save
    finally:
        release_write.set()
        await asyncio.gather(save, return_exceptions=True)
        storage.close()


@pytest.mark.asyncio
async def test_async_database_delegates_to_upstream_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """A native async database must not enter the sync snapshot queue."""
    database = AsyncSqliteDb(db_file=":memory:")
    owner = Agent(db=database, telemetry=False)
    session = AgentSession(
        session_id="async",
        session_data={"session_state": {"current_run_id": "run"}},
    )
    calls: list[AgentSession] = []

    async def async_upsert(session: AgentSession) -> AgentSession:
        calls.append(session)
        return session

    monkeypatch.setattr(database, "upsert_session", async_upsert)
    await owner.asave_session(session)

    assert calls == [session]
    assert session.session_data == {"session_state": {}}


@pytest.mark.asyncio
async def test_team_save_preserves_live_member_response_scrubbing(tmp_path: Path) -> None:
    """Snapshotting must scrub team runs and preserve accepted agent runs."""
    storage = _storage(tmp_path, "team-scrub")
    team = Team(db=storage, members=[], store_member_responses=False, telemetry=False)
    member_response = RunOutput(run_id="member", agent_id="member", content="member")
    agent_run_output = RunOutput(run_id="agent-run", agent_id="member", content="agent")
    run = TeamRunOutput(run_id="run", team_id="team", member_responses=[member_response])
    session = TeamSession(
        session_id="team-scrub",
        team_id="team",
        session_data={"session_state": {"current_run_id": "run"}},
        runs=[agent_run_output, run],
        created_at=int(time.time()),
    )
    try:
        await team.asave_session(session)
        persisted = get_team_session(storage, session.session_id)
    finally:
        storage.close()

    assert run.member_responses == []
    assert persisted is not None
    assert persisted.runs is not None
    assert isinstance(persisted.runs[0], RunOutput)
    assert persisted.runs[0].content == "agent"
    assert isinstance(persisted.runs[1], TeamRunOutput)
    assert persisted.runs[1].member_responses == []


@pytest.mark.parametrize("surface", ["agent", "team"])
@pytest.mark.asyncio
async def test_agno_background_save_completes_before_storage_close(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    surface: _Surface,
) -> None:
    """An unjoined Agno task must finish persistence before its DB can close."""
    storage = _storage(tmp_path, f"{surface}-background")
    owner, session = _owner_and_session(surface, storage, "background")
    persistence_threads: list[int] = []
    write_completed = threading.Event()
    original_upsert = storage.upsert_session
    original_close = storage.close
    storage_closed = False
    background_tasks = agent_run._background_tasks if surface == "agent" else team_run._background_tasks

    def record_thread(
        session: AgentSession | TeamSession,
        deserialize: bool | None = True,
    ) -> object:
        persistence_threads.append(threading.get_ident())
        result = original_upsert(session, deserialize=deserialize)
        write_completed.set()
        return result

    def checked_close() -> None:
        nonlocal storage_closed
        assert write_completed.is_set()
        original_close()
        storage_closed = True

    monkeypatch.setattr(storage, "upsert_session", record_thread)
    monkeypatch.setattr(storage, "close", checked_close)
    task = asyncio.current_task()
    assert task is not None
    background_tasks.add(task)
    try:
        await owner.asave_session(session)
        storage.close()
    finally:
        background_tasks.discard(task)
        if not storage_closed:
            original_close()

    assert storage_closed
    assert len(persistence_threads) == 1
    assert persistence_threads != [threading.get_ident()]


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["agent", "team"])
async def test_agno_background_save_waits_for_earlier_worker(  # noqa: PLR0915
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    surface: _Surface,
) -> None:
    """A detached synchronous save must not overtake a queued worker save."""
    storage = _storage(tmp_path, f"{surface}-background-order")
    first_owner, first_session = _owner_and_session(surface, storage, "shared")
    second_owner, second_session = _owner_and_session(surface, storage, "shared")
    baseline_owner, baseline_session = _owner_and_session(surface, storage, "baseline")
    await baseline_owner.asave_session(baseline_session)
    first_session.user_id = "user"
    second_session.user_id = "user"
    first_session.metadata = {"write": "first"}
    second_session.metadata = {"write": "second"}
    first_started = threading.Event()
    release_first = threading.Event()
    started: list[str] = []
    second_entered_before_release: list[bool] = []
    original_upsert = storage.upsert_session

    def blocking_upsert(
        session: AgentSession | TeamSession,
        deserialize: bool | None = True,
    ) -> object:
        write_marker = str((session.metadata or {}).get("write", ""))
        started.append(write_marker)
        if write_marker == "first":
            first_started.set()
            assert release_first.wait(timeout=5)
        else:
            second_entered_before_release.append(not release_first.is_set())
        return original_upsert(session, deserialize=deserialize)

    def release_later() -> None:
        if first_started.wait(timeout=5):
            time.sleep(0.2)
            release_first.set()

    monkeypatch.setattr(storage, "upsert_session", blocking_upsert)
    release_thread = threading.Thread(target=release_later)
    release_thread.start()
    first = asyncio.create_task(first_owner.asave_session(first_session))
    current = asyncio.current_task()
    assert current is not None
    background_tasks = agent_run._background_tasks if surface == "agent" else team_run._background_tasks
    try:
        assert await asyncio.to_thread(first_started.wait, 5)
        background_tasks.add(current)
        try:
            await second_owner.asave_session(second_session)
        finally:
            background_tasks.discard(current)
        await first
        persisted = get_agent_session(storage, "shared") if surface == "agent" else get_team_session(storage, "shared")
    finally:
        release_first.set()
        release_thread.join(timeout=1)
        await asyncio.gather(first, return_exceptions=True)
        storage.close()

    assert started == ["first", "second"]
    assert second_entered_before_release == [False]
    assert persisted is not None
    assert persisted.metadata == {"write": "second"}


@pytest.mark.asyncio
async def test_background_save_cannot_block_before_prior_worker_submission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ready background save sees the prior dedicated submission first."""
    storage = _storage(tmp_path, "background-scheduler-window")
    baseline_owner, baseline_session = _owner_and_session("agent", storage, "baseline")
    first_owner, first_session = _owner_and_session("agent", storage, "first")
    second_owner, second_session = _owner_and_session("agent", storage, "second")
    await baseline_owner.asave_session(baseline_session)
    submitted = threading.Event()
    original_submit = persistence_patch._PERSISTENCE_EXECUTOR.submit

    def record_submit(*args: object, **kwargs: object) -> object:
        submitted.set()
        return original_submit(*args, **kwargs)

    monkeypatch.setattr(persistence_patch._PERSISTENCE_EXECUTOR, "submit", record_submit)
    foreground = asyncio.create_task(first_owner.asave_session(first_session))
    current = asyncio.current_task()
    assert current is not None
    try:
        # The foreground reaches its first await. Do not wait for a worker-side
        # signal before making the detached save ready.
        await asyncio.sleep(0)
        assert submitted.is_set()
        agent_run._background_tasks.add(current)
        try:
            await second_owner.asave_session(second_session)
        finally:
            agent_run._background_tasks.discard(current)
        foreground_result = await asyncio.gather(foreground, return_exceptions=True)
    finally:
        await asyncio.gather(foreground, return_exceptions=True)
        storage.close()

    assert foreground_result == [None]


@pytest.mark.parametrize(
    ("surface", "owner_attribute"),
    [
        ("agent", "team_id"),
        ("agent", "workflow_id"),
        ("team", "parent_team_id"),
        ("team", "workflow_id"),
    ],
)
@pytest.mark.asyncio
async def test_nested_owners_do_not_delegate_a_sync_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    surface: _Surface,
    owner_attribute: str,
) -> None:
    """Agno's nested-owner branches are no-ops and cannot bypass ordering."""
    storage = _storage(tmp_path, f"nested-{surface}-{owner_attribute}")
    owner, session = _owner_and_session(surface, storage, "nested")
    calls: list[object] = []
    setattr(owner, owner_attribute, "parent")
    monkeypatch.setattr(storage, "upsert_session", lambda saved: calls.append(saved))
    try:
        await owner.asave_session(session)
    finally:
        storage.close()

    assert calls == []


@pytest.mark.asyncio
async def test_workflow_sync_save_remains_upstream_and_responsive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Workflow already owns an upstream thread boundary and stays unpatched."""
    storage = _storage(tmp_path, "workflow")
    workflow = Workflow(db=storage, telemetry=False)
    session = WorkflowSession(
        session_id="workflow",
        session_data={"session_state": {"current_run_id": "run"}},
    )
    loop_progressed = asyncio.Event()
    original_upsert = storage.upsert_session

    def delayed_upsert(session: WorkflowSession, deserialize: bool | None = True) -> object:
        time.sleep(0.1)
        return original_upsert(session, deserialize=deserialize)

    async def mark_progress() -> None:
        await asyncio.sleep(0.01)
        loop_progressed.set()

    monkeypatch.setattr(storage, "upsert_session", delayed_upsert)
    progress = asyncio.create_task(mark_progress())
    try:
        await workflow.asave_session(session)
        assert loop_progressed.is_set()
        assert Workflow.asave_session.__module__.startswith("agno.")
    finally:
        await progress
        storage.close()
