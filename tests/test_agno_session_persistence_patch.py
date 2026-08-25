"""Behavioral tests for the guarded Agno session persistence boundary."""

from __future__ import annotations

import asyncio
import gc
import subprocess
import threading
import time
import weakref
from contextvars import ContextVar
from importlib import import_module
from typing import TYPE_CHECKING, Literal
from urllib.parse import quote

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
    persistence_patch = import_module("mindroom.agno_session_persistence_patch")

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


def test_postgres_handles_use_credential_free_target_identity() -> None:
    """Equivalent PostgreSQL handles coordinate without retaining credentials."""
    persistence_patch = import_module("mindroom.agno_session_persistence_patch")
    first = PostgresDb(
        db_engine=create_engine("postgresql+psycopg://first:secret-one@LOCALHOST:5432/app"),
        db_schema="sessions",
        session_table="records",
    )
    second = PostgresDb(
        db_engine=create_engine("postgresql+psycopg://second:secret-two@localhost:5432/app"),
        db_schema="sessions",
        session_table="records",
    )

    first_identity = persistence_patch._database_coordination_identity(first)
    second_identity = persistence_patch._database_coordination_identity(second)

    assert first_identity == second_identity
    identity_repr = repr(first_identity)
    assert "secret" not in identity_repr
    assert "localhost" not in identity_repr.lower()
    assert "app" not in identity_repr


def test_sqlite_file_uris_share_the_filesystem_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Path spelling, URI query splitting, and encoding must not split FIFO."""
    persistence_patch = import_module("mindroom.agno_session_persistence_patch")
    database_path = tmp_path / "equivalent.db"
    encoded_path = quote(str(database_path), safe="")
    monkeypatch.chdir(tmp_path)
    databases = [
        SqliteDb(db_url=f"sqlite:///{database_path}", session_table="records"),
        SqliteDb(db_url=f"sqlite:///file:{database_path}?mode=rwc&uri=true", session_table="records"),
        SqliteDb(db_url=f"sqlite:///file:{encoded_path}?uri=true&mode=rwc", session_table="records"),
        SqliteDb(db_url="sqlite:///relative.db", session_table="records"),
        SqliteDb(db_url="sqlite:///file:relative.db?mode=rwc&uri=true", session_table="records"),
        SqliteDb(db_url="sqlite:///file:relative.db", session_table="records"),
    ]
    try:
        absolute_identities = {
            persistence_patch._database_coordination_identity(database) for database in databases[:3]
        }
        relative_identities = {
            persistence_patch._database_coordination_identity(database) for database in databases[3:5]
        }
        literal_file_prefix_identity = persistence_patch._database_coordination_identity(databases[5])
    finally:
        for database in databases:
            database.close()

    assert len(absolute_identities) == 1
    assert len(relative_identities) == 1
    assert literal_file_prefix_identity not in relative_identities


def test_sqlite_memory_modes_do_not_collide_with_files_or_private_engines() -> None:
    """Only named shared-memory URI handles may share a cross-engine queue."""
    persistence_patch = import_module("mindroom.agno_session_persistence_patch")
    shared = [
        SqliteDb(db_url="sqlite:///file:shared?mode=memory&cache=shared&uri=true", session_table="records")
        for _ in range(2)
    ]
    private = [
        SqliteDb(db_url="sqlite:///file:shared?mode=memory&cache=private&uri=true", session_table="records")
        for _ in range(2)
    ]
    disk = SqliteDb(db_url="sqlite:///file:shared?mode=rwc&uri=true", session_table="records")
    ordinary_memory = SqliteDb(db_url="sqlite:///:memory:", session_table="records")
    try:
        shared_identities = [persistence_patch._database_coordination_identity(database) for database in shared]
        private_identities = [persistence_patch._database_coordination_identity(database) for database in private]
        disk_identity = persistence_patch._database_coordination_identity(disk)
        ordinary_memory_identity = persistence_patch._database_coordination_identity(ordinary_memory)
        private_queues = [persistence_patch._database_queue(database) for database in private]
    finally:
        for database in [*shared, *private, disk, ordinary_memory]:
            database.close()

    assert shared_identities[0] is not None
    assert shared_identities[0] == shared_identities[1]
    assert private_identities == [None, None]
    assert ordinary_memory_identity is None
    assert disk_identity != shared_identities[0]
    assert private_queues[0] is not private_queues[1]


def test_postgres_effective_socket_target_participates_in_identity() -> None:
    """Query-routed sockets must neither collide nor split equivalent targets."""
    persistence_patch = import_module("mindroom.agno_session_persistence_patch")

    def identity(url: str) -> object:
        database = PostgresDb(
            db_engine=create_engine(url),
            db_schema="sessions",
            session_table="records",
        )
        try:
            return persistence_patch._database_coordination_identity(database)
        finally:
            database.close()

    authority = identity("postgresql+psycopg://user:one@LOCALHOST/app")
    query_network = identity("postgresql+psycopg://other:two@/app?host=localhost&port=5432")
    overridden_socket = identity(
        "postgresql+psycopg://user:one@ignored:6543/app?host=%2Fsocket%2Fone&port=5433",
    )
    direct_socket = identity("postgresql+psycopg://other:two@/app?host=%2Fsocket%2Fone&port=5433")
    other_socket = identity("postgresql+psycopg://user:one@/app?host=%2Fsocket%2Ftwo&port=5433")
    other_port = identity("postgresql+psycopg://user:one@/app?host=%2Fsocket%2Fone&port=5434")

    assert authority == query_network
    assert overridden_socket == direct_socket
    assert len({direct_socket, other_socket, other_port}) == 3
    assert "/socket" not in repr(direct_socket)


def test_target_queue_registry_has_weak_lifecycle(tmp_path: Path) -> None:
    """A durable target identity must not create an unbounded queue registry."""
    persistence_patch = import_module("mindroom.agno_session_persistence_patch")
    storage = _storage(tmp_path, "weak-target")
    identity = persistence_patch._database_coordination_identity(storage)
    queue = persistence_patch._database_queue(storage)
    queue_reference = weakref.ref(queue)

    assert identity in persistence_patch._TARGET_QUEUES
    del queue
    gc.collect()

    assert queue_reference() is None
    assert identity not in persistence_patch._TARGET_QUEUES
    storage.close()


def test_abandoning_an_already_served_ticket_is_a_noop() -> None:
    """Late cleanup must not retain stale tickets or disturb later writes."""
    persistence_patch = import_module("mindroom.agno_session_persistence_patch")
    queue = persistence_patch._DatabaseWriteQueue()
    first_ticket = queue.reserve()

    assert queue.run(first_ticket, lambda: "first") == "first"
    queue.abandon(first_ticket)
    second_ticket = queue.reserve()

    assert queue.run(second_ticket, lambda: "second") == "second"
    assert queue._abandoned_tickets == set()


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
    persistence_patch = import_module("mindroom.agno_session_persistence_patch")
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
    persistence_patch = import_module("mindroom.agno_session_persistence_patch")
    storage = _storage(tmp_path, "submission-failure")
    owner, first_session = _owner_and_session("agent", storage, "first")
    _, second_session = _owner_and_session("agent", storage, "second")
    queue = persistence_patch._database_queue(storage)
    loop = asyncio.get_running_loop()
    original_run_in_executor = loop.run_in_executor

    def fail_submission(*_args: object, **_kwargs: object) -> object:
        error_message = "executor unavailable"
        raise RuntimeError(error_message)

    monkeypatch.setattr(loop, "run_in_executor", fail_submission)
    try:
        with pytest.raises(RuntimeError, match="executor unavailable"):
            await owner.asave_session(first_session)
        assert queue._serving_ticket == 1
        monkeypatch.setattr(loop, "run_in_executor", original_run_in_executor)
        second_save = asyncio.create_task(owner.asave_session(second_session))
        done, _ = await asyncio.wait({second_save}, timeout=5)
        completed_without_repair = second_save in done
        if not completed_without_repair:
            # Repair the pre-fix queue so the RED test does not leak a worker.
            queue.abandon(0)
        await second_save
        assert get_agent_session(storage, "second") is not None
    finally:
        storage.close()

    assert completed_without_repair


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
    persistence_patch = import_module("mindroom.agno_session_persistence_patch")
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
    """Snapshotting must retain Agno's mutation of the live TeamSession."""
    storage = _storage(tmp_path, "team-scrub")
    team = Team(db=storage, members=[], store_member_responses=False, telemetry=False)
    member_response = RunOutput(run_id="member", agent_id="member", content="member")
    run = TeamRunOutput(run_id="run", team_id="team", member_responses=[member_response])
    session = TeamSession(
        session_id="team-scrub",
        team_id="team",
        session_data={"session_state": {"current_run_id": "run"}},
        runs=[run],
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
    assert isinstance(persisted.runs[0], TeamRunOutput)
    assert persisted.runs[0].member_responses == []


@pytest.mark.parametrize("surface", ["agent", "team"])
@pytest.mark.asyncio
async def test_agno_background_tasks_keep_upstream_synchronous_save(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    surface: _Surface,
) -> None:
    """Library-owned detached tasks must not outlive their synchronous DB handle."""
    storage = _storage(tmp_path, f"{surface}-background")
    owner, session = _owner_and_session(surface, storage, "background")
    event_loop_thread = threading.get_ident()
    persistence_threads: list[int] = []
    original_upsert = storage.upsert_session
    background_tasks = agent_run._background_tasks if surface == "agent" else team_run._background_tasks

    def record_thread(
        session: AgentSession | TeamSession,
        deserialize: bool | None = True,
    ) -> object:
        persistence_threads.append(threading.get_ident())
        return original_upsert(session, deserialize=deserialize)

    monkeypatch.setattr(storage, "upsert_session", record_thread)
    task = asyncio.current_task()
    assert task is not None
    background_tasks.add(task)
    try:
        await owner.asave_session(session)
    finally:
        background_tasks.discard(task)
        storage.close()

    assert persistence_threads == [event_loop_thread]


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
) -> None:
    """A ready background save must not deadlock the prior lazy worker ticket."""
    persistence_patch = import_module("mindroom.agno_session_persistence_patch")
    storage = _storage(tmp_path, "background-scheduler-window")
    baseline_owner, baseline_session = _owner_and_session("agent", storage, "baseline")
    first_owner, first_session = _owner_and_session("agent", storage, "first")
    second_owner, second_session = _owner_and_session("agent", storage, "second")
    await baseline_owner.asave_session(baseline_session)
    queue = persistence_patch._database_queue(storage)
    background_finished = threading.Event()
    repair_was_needed = threading.Event()

    def repair_lazy_submission_deadlock() -> None:
        if not background_finished.wait(timeout=5):
            repair_was_needed.set()
            queue.abandon(0)

    repair = threading.Thread(target=repair_lazy_submission_deadlock)
    repair.start()
    foreground = asyncio.create_task(first_owner.asave_session(first_session))
    current = asyncio.current_task()
    assert current is not None
    try:
        # The foreground task reserves and reaches its first await. Do not wait
        # for any signal from the worker before making the background save ready.
        await asyncio.sleep(0)
        agent_run._background_tasks.add(current)
        try:
            await second_owner.asave_session(second_session)
        finally:
            agent_run._background_tasks.discard(current)
            background_finished.set()
        foreground_result = await asyncio.gather(foreground, return_exceptions=True)
    finally:
        background_finished.set()
        repair.join(timeout=1)
        await asyncio.gather(foreground, return_exceptions=True)
        storage.close()

    assert not repair_was_needed.is_set()
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
