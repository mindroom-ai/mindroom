"""Behavioral tests for the guarded Agno session persistence boundary."""

from __future__ import annotations

import asyncio
import gc
import subprocess
import threading
import time
import weakref
from contextvars import ContextVar
from copy import deepcopy as copy_session
from typing import TYPE_CHECKING, Literal

import pytest
from agno.agent import Agent
from agno.agent import _session as agent_session_module
from agno.db.sqlite import SqliteDb
from agno.db.sqlite.async_sqlite import AsyncSqliteDb
from agno.models.message import Message
from agno.run.agent import RunOutput
from agno.run.team import TeamRunOutput
from agno.session.agent import AgentSession
from agno.session.team import TeamSession
from agno.session.workflow import WorkflowSession
from agno.team import Team
from agno.team import _session as team_session_module
from agno.workflow import Workflow

from mindroom import agent_storage
from mindroom import agno_session_persistence_patch as persistence_patch
from mindroom.agent_storage import create_state_storage, get_agent_session, get_team_session

if TYPE_CHECKING:
    from collections.abc import Callable
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
    session_data = {
        "session_state": {
            "current_session_id": session_id,
            "current_user_id": "user",
            "current_run_id": "run",
            "durable": "value",
        },
    }
    messages = [
        Message(role="system", content="prompt"),
        Message(role="user", content=session_id),
    ]
    if surface == "agent":
        return Agent(db=storage, telemetry=False), AgentSession(
            session_id=session_id,
            agent_id="agent",
            user_id="before",
            session_data=session_data,
            metadata={"write": session_id},
            created_at=int(time.time()),
            runs=[RunOutput(run_id="run", agent_id="agent", session_id=session_id, messages=messages)],
        )
    return Team(db=storage, members=[], telemetry=False), TeamSession(
        session_id=session_id,
        team_id="team",
        user_id="before",
        session_data=session_data,
        metadata={"write": session_id},
        created_at=int(time.time()),
        runs=[TeamRunOutput(run_id="run", team_id="team", session_id=session_id, messages=messages)],
    )


def _persisted(storage: BaseDb, surface: _Surface, session_id: str) -> AgentSession | TeamSession | None:
    if surface == "agent":
        return get_agent_session(storage, session_id)
    return get_team_session(storage, session_id)


def _run_save_in_thread(
    owner: Agent,
    session: AgentSession,
    errors: list[BaseException],
) -> None:
    try:
        asyncio.run(owner.asave_session(session))
    except BaseException as error:  # pragma: no cover - asserted by the caller
        errors.append(error)


def _recording_upsert(storage: BaseDb, write_order: list[str]) -> Callable[..., object]:
    original_upsert = storage.upsert_session

    def record(session: AgentSession, deserialize: bool | None = True) -> object:
        write_order.append(str((session.metadata or {})["write"]))
        return original_upsert(session, deserialize=deserialize)

    return record


def test_installation_is_exact_version_guarded_and_idempotent() -> None:
    """Only the pinned dependency version may install, and repeat installs are inert."""
    assert persistence_patch.version("agno") == persistence_patch._SUPPORTED_AGNO_VERSION
    persistence_patch.install_patch()
    installed = (agent_session_module.asave_session, team_session_module.asave_session)

    persistence_patch.install_patch()

    assert (agent_session_module.asave_session, team_session_module.asave_session) == installed
    code = """
from importlib import import_module

patch = import_module("mindroom.agno_session_persistence_patch")
patch.version = lambda _distribution: "0.0.0"
try:
    import_module("mindroom.agent_storage")
except RuntimeError as error:
    assert "session persistence" in str(error).lower()
else:
    raise AssertionError("version drift did not fail closed")
"""
    result = subprocess.run(
        ["uv", "run", "python", "-c", code],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_registration_shares_one_lane_for_the_same_path_and_table(tmp_path: Path) -> None:
    """Separate handles for one SQLite table must use the same FIFO lane."""
    first = _storage(tmp_path, "shared")
    second = _storage(tmp_path, "shared")
    try:
        first_lane = persistence_patch._registered_lane(first)
        second_lane = persistence_patch._registered_lane(second)
    finally:
        first.close()
        second.close()

    assert first_lane is not None
    assert second_lane is first_lane


def test_lane_registration_has_a_weak_lifetime(tmp_path: Path) -> None:
    """Historical storage targets must not retain executors after their handles die."""

    def create_references() -> tuple[weakref.ReferenceType[BaseDb], weakref.ReferenceType[object]]:
        storage = _storage(tmp_path, "weak-lane")
        lane = persistence_patch._registered_lane(storage)
        assert lane is not None
        storage_reference = weakref.ref(storage)
        lane_reference = weakref.ref(lane)
        storage.close()
        return storage_reference, lane_reference

    storage_reference, lane_reference = create_references()
    gc.collect()

    assert storage_reference() is None
    assert lane_reference() is None


@pytest.mark.parametrize("surface", ["agent", "team"])
@pytest.mark.asyncio
async def test_registered_writes_run_on_a_dedicated_thread(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    surface: _Surface,
) -> None:
    """Registered synchronous writes must never execute on the event-loop thread."""
    storage = _storage(tmp_path, f"{surface}-thread")
    owner, session = _owner_and_session(surface, storage, surface)
    event_loop_thread = threading.get_ident()
    write_threads: list[int] = []
    original_upsert = storage.upsert_session

    def record_thread(
        session: AgentSession | TeamSession,
        deserialize: bool | None = True,
    ) -> object:
        write_threads.append(threading.get_ident())
        return original_upsert(session, deserialize=deserialize)

    monkeypatch.setattr(storage, "upsert_session", record_thread)
    try:
        await owner.asave_session(session)
    finally:
        storage.close()

    assert len(write_threads) == 1
    assert write_threads != [event_loop_thread]


@pytest.mark.asyncio
async def test_unrelated_target_completes_while_other_lanes_are_blocked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Blocked targets must not consume a shared worker pool needed by another target."""
    release_blockers = threading.Event()
    blocker_started = [threading.Event() for _ in range(4)]
    blocker_storages = [_storage(tmp_path, f"blocked-{index}") for index in range(4)]
    blocker_tasks: list[asyncio.Task[None]] = []

    for index, storage in enumerate(blocker_storages):
        original_upsert = storage.upsert_session

        def blocked_upsert(
            session: AgentSession,
            deserialize: bool | None = True,
            *,
            event: threading.Event = blocker_started[index],
            upsert: object = original_upsert,
        ) -> object:
            event.set()
            assert release_blockers.wait(timeout=5)
            return upsert(session, deserialize=deserialize)  # type: ignore[operator]

        monkeypatch.setattr(storage, "upsert_session", blocked_upsert)
        owner, session = _owner_and_session("agent", storage, f"blocked-{index}")
        blocker_tasks.append(asyncio.create_task(owner.asave_session(session)))

    independent_storage = _storage(tmp_path, "independent")
    independent_owner, independent_session = _owner_and_session("agent", independent_storage, "independent")
    independent_finished = threading.Event()
    original_independent_upsert = independent_storage.upsert_session

    def signal_independent(session: AgentSession, deserialize: bool | None = True) -> object:
        result = original_independent_upsert(session, deserialize=deserialize)
        independent_finished.set()
        return result

    monkeypatch.setattr(independent_storage, "upsert_session", signal_independent)
    independent = asyncio.create_task(independent_owner.asave_session(independent_session))
    try:
        started = await asyncio.gather(
            *(asyncio.to_thread(event.wait, 5) for event in blocker_started),
        )
        assert started == [True, True, True, True]
        assert await asyncio.to_thread(independent_finished.wait, 5)
        await independent
    finally:
        release_blockers.set()
        await asyncio.gather(*blocker_tasks, independent, return_exceptions=True)
        for storage in [*blocker_storages, independent_storage]:
            storage.close()


def test_cross_loop_reservation_precedes_snapshot_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A later completed snapshot must not overtake an earlier blocked snapshot."""
    first_storage = _storage(tmp_path, "cross-loop")
    second_storage = _storage(tmp_path, "cross-loop")
    first_owner, first_session = _owner_and_session("agent", first_storage, "shared")
    second_owner, second_session = _owner_and_session("agent", second_storage, "shared")
    first_session.metadata = {"write": "first"}
    second_session.metadata = {"write": "second"}
    first_snapshot_started = threading.Event()
    release_first_snapshot = threading.Event()
    second_snapshot_finished = threading.Event()
    write_order: list[str] = []
    errors: list[BaseException] = []

    def controlled_deepcopy(saved: AgentSession) -> AgentSession:
        if saved.metadata == {"write": "first"}:
            first_snapshot_started.set()
            assert release_first_snapshot.wait(timeout=5)
        else:
            second_snapshot_finished.set()
        return copy_session(saved)

    monkeypatch.setattr(persistence_patch, "deepcopy", controlled_deepcopy)
    monkeypatch.setattr(first_storage, "upsert_session", _recording_upsert(first_storage, write_order))
    monkeypatch.setattr(second_storage, "upsert_session", _recording_upsert(second_storage, write_order))
    first = threading.Thread(target=_run_save_in_thread, args=(first_owner, first_session, errors))
    second = threading.Thread(target=_run_save_in_thread, args=(second_owner, second_session, errors))
    first.start()
    try:
        assert first_snapshot_started.wait(timeout=5)
        second.start()
        assert second_snapshot_finished.wait(timeout=5)
        assert write_order == []
        release_first_snapshot.set()
        first.join(timeout=5)
        second.join(timeout=5)
        persisted = get_agent_session(first_storage, "shared")
    finally:
        release_first_snapshot.set()
        first.join(timeout=5)
        if second.ident is not None:
            second.join(timeout=5)
        first_storage.close()
        second_storage.close()

    assert not first.is_alive()
    assert not second.is_alive()
    assert errors == []
    assert write_order == ["first", "second"]
    assert persisted is not None
    assert persisted.metadata == {"write": "second"}


@pytest.mark.asyncio
async def test_snapshot_failure_is_immediate_and_does_not_strand_a_successor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Preparation errors consume their queue position without waiting for it."""
    storage = _storage(tmp_path, "snapshot-failure")
    predecessor_owner, predecessor_session = _owner_and_session("agent", storage, "predecessor")
    failing_owner, failing_session = _owner_and_session("agent", storage, "failure")
    successor_owner, successor_session = _owner_and_session("agent", storage, "successor")
    predecessor_started = threading.Event()
    release_predecessor = threading.Event()
    snapshot_error = "snapshot failed"
    original_upsert = storage.upsert_session

    def controlled_deepcopy(saved: AgentSession) -> AgentSession:
        if saved.session_id == "failure":
            raise RuntimeError(snapshot_error)
        return copy_session(saved)

    def blocked_predecessor(session: AgentSession, deserialize: bool | None = True) -> object:
        if session.session_id == "predecessor":
            predecessor_started.set()
            assert release_predecessor.wait(timeout=5)
        return original_upsert(session, deserialize=deserialize)

    monkeypatch.setattr(persistence_patch, "deepcopy", controlled_deepcopy)
    monkeypatch.setattr(storage, "upsert_session", blocked_predecessor)
    predecessor = asyncio.create_task(predecessor_owner.asave_session(predecessor_session))
    successor: asyncio.Task[None] | None = None
    try:
        assert await asyncio.to_thread(predecessor_started.wait, 5)
        with pytest.raises(RuntimeError, match=snapshot_error):
            await failing_owner.asave_session(failing_session)
        successor = asyncio.create_task(successor_owner.asave_session(successor_session))
        await asyncio.sleep(0)
        assert not successor.done()
        release_predecessor.set()
        await asyncio.gather(predecessor, successor)
        assert get_agent_session(storage, "successor") is not None
    finally:
        release_predecessor.set()
        await asyncio.gather(
            predecessor,
            *([successor] if successor is not None else []),
            return_exceptions=True,
        )
        storage.close()


@pytest.mark.asyncio
async def test_cancellation_drains_an_accepted_save_before_raising(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation cannot detach a write already accepted by its lane."""
    storage = _storage(tmp_path, "cancel")
    owner, session = _owner_and_session("agent", storage, "cancel")
    write_started = threading.Event()
    release_write = threading.Event()
    original_upsert = storage.upsert_session

    def blocked_upsert(session: AgentSession, deserialize: bool | None = True) -> object:
        write_started.set()
        assert release_write.wait(timeout=5)
        return original_upsert(session, deserialize=deserialize)

    monkeypatch.setattr(storage, "upsert_session", blocked_upsert)
    save = asyncio.create_task(owner.asave_session(session))
    try:
        assert await asyncio.to_thread(write_started.wait, 5)
        save.cancel()
        await asyncio.sleep(0)
        assert not save.done()
        release_write.set()
        with pytest.raises(asyncio.CancelledError):
            await save
        assert get_agent_session(storage, "cancel") is not None
    finally:
        release_write.set()
        await asyncio.gather(save, return_exceptions=True)
        storage.close()


@pytest.mark.asyncio
async def test_cancellation_chains_a_worker_failure_after_drain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed accepted write remains visible as the cancellation's cause."""
    storage = _storage(tmp_path, "cancel-error")
    owner, session = _owner_and_session("agent", storage, "cancel-error")
    write_started = threading.Event()
    release_write = threading.Event()
    worker_error = "write failed after cancellation"

    def failing_save(_owner: Agent, _session: AgentSession) -> None:
        write_started.set()
        assert release_write.wait(timeout=5)
        raise RuntimeError(worker_error)

    monkeypatch.setattr(persistence_patch, "_ORIGINAL_AGENT_SAVE_SESSION", failing_save)
    save = asyncio.create_task(owner.asave_session(session))
    try:
        assert await asyncio.to_thread(write_started.wait, 5)
        save.cancel()
        await asyncio.sleep(0)
        assert not save.done()
        release_write.set()
        with pytest.raises(asyncio.CancelledError) as cancellation:
            await save
    finally:
        release_write.set()
        await asyncio.gather(save, return_exceptions=True)
        storage.close()

    assert isinstance(cancellation.value.__cause__, RuntimeError)
    assert str(cancellation.value.__cause__) == worker_error


@pytest.mark.asyncio
async def test_worker_receives_the_callers_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The dedicated worker must run in the save caller's context."""
    storage = _storage(tmp_path, "context")
    owner, session = _owner_and_session("agent", storage, "context")
    marker: ContextVar[str] = ContextVar("session_persistence_marker", default="missing")
    observed: list[str] = []
    original_upsert = storage.upsert_session

    def context_probe(session: AgentSession, deserialize: bool | None = True) -> object:
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
async def test_unregistered_synchronous_database_delegates_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A synchronous database not created by the application stays upstream-owned."""
    database = SqliteDb(db_file=str(tmp_path / "unregistered.db"), session_table="sessions")
    owner = Agent(db=database, telemetry=False)
    session = AgentSession(session_id="unregistered", session_data={"session_state": {}})
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

    assert persistence_patch._registered_lane(database) is None
    assert observed == [(event_loop_thread, session)]


@pytest.mark.asyncio
async def test_native_asynchronous_database_delegates_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """A native async database must remain on the captured upstream async path."""
    database = AsyncSqliteDb(db_file=":memory:")
    owner = Agent(db=database, telemetry=False)
    session = AgentSession(session_id="async", session_data={"session_state": {"current_run_id": "run"}})
    calls: list[AgentSession] = []

    async def async_upsert(session: AgentSession) -> AgentSession:
        calls.append(session)
        return session

    monkeypatch.setattr(database, "upsert_session", async_upsert)
    try:
        await owner.asave_session(session)
    finally:
        await database.close()

    assert calls == [session]
    assert session.session_data == {"session_state": {}}


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
async def test_nested_owners_delegate_to_upstream_no_op(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    surface: _Surface,
    owner_attribute: str,
) -> None:
    """Nested agent and team paths preserve the upstream no-write behavior."""
    storage = _storage(tmp_path, f"nested-{surface}-{owner_attribute}")
    owner, session = _owner_and_session(surface, storage, "nested")
    calls: list[AgentSession | TeamSession] = []
    setattr(owner, owner_attribute, "parent")
    monkeypatch.setattr(storage, "upsert_session", lambda saved: calls.append(saved))
    try:
        await owner.asave_session(session)
    finally:
        storage.close()

    assert calls == []


@pytest.mark.asyncio
async def test_workflow_path_remains_upstream_and_responsive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Workflow persistence keeps its own upstream asynchronous boundary."""
    storage = _storage(tmp_path, "workflow")
    workflow = Workflow(db=storage, telemetry=False)
    session = WorkflowSession(session_id="workflow", session_data={"session_state": {"current_run_id": "run"}})
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


@pytest.mark.parametrize("surface", ["agent", "team"])
@pytest.mark.asyncio
async def test_persisted_snapshot_is_canonical_without_mutating_the_live_session(
    tmp_path: Path,
    surface: _Surface,
) -> None:
    """Upstream cleanup belongs to the copied persisted session, never the live one."""
    storage = _storage(tmp_path, f"{surface}-snapshot")
    owner, session = _owner_and_session(surface, storage, surface)
    member_response = RunOutput(run_id="member", agent_id="member", content="member")
    if surface == "team":
        assert isinstance(owner, Team)
        owner.store_member_responses = False
        assert isinstance(session, TeamSession)
        assert session.runs is not None
        assert isinstance(session.runs[0], TeamRunOutput)
        session.runs[0].member_responses = [member_response]

    try:
        await owner.asave_session(session)
        persisted = _persisted(storage, surface, surface)
    finally:
        storage.close()

    assert persisted is not None
    assert session.session_data == {
        "session_state": {
            "current_session_id": surface,
            "current_user_id": "user",
            "current_run_id": "run",
            "durable": "value",
        },
    }
    assert persisted.session_data == {"session_state": {"durable": "value"}}
    assert session.runs is not None
    assert persisted.runs, persisted.to_dict()
    assert [message.role for message in session.runs[0].messages or []] == ["system", "user"]
    assert [message.role for message in persisted.runs[0].messages or []] == ["user"]
    if surface == "team":
        live_run = session.runs[0]
        persisted_run = persisted.runs[0]
        assert isinstance(live_run, TeamRunOutput)
        assert isinstance(persisted_run, TeamRunOutput)
        assert live_run.member_responses == [member_response]
        assert persisted_run.member_responses == []


@pytest.mark.asyncio
async def test_prompt_sanitization_uses_the_snapshot_before_later_live_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Worker-side prompt cleanup must not race later event-loop mutation."""
    storage = _storage(tmp_path, "prompt-snapshot")
    owner, session = _owner_and_session("agent", storage, "prompt-snapshot")
    sanitizer_started = threading.Event()
    release_sanitizer = threading.Event()
    original_sanitizer = agent_storage._session_without_prompt_messages

    def blocked_sanitizer(
        saved: AgentSession | TeamSession,
        prompt_roles: frozenset[str],
    ) -> AgentSession | TeamSession:
        sanitizer_started.set()
        assert release_sanitizer.wait(timeout=5)
        return original_sanitizer(saved, prompt_roles)

    monkeypatch.setattr(agent_storage, "_session_without_prompt_messages", blocked_sanitizer)
    save = asyncio.create_task(owner.asave_session(session))
    try:
        assert await asyncio.to_thread(sanitizer_started.wait, 5)
        session.user_id = "after"
        assert session.runs is not None
        assert session.runs[0].messages is not None
        session.runs[0].messages.append(Message(role="user", content="later"))
        release_sanitizer.set()
        await save
        persisted = get_agent_session(storage, "prompt-snapshot")
    finally:
        release_sanitizer.set()
        await asyncio.gather(save, return_exceptions=True)
        storage.close()

    assert persisted is not None
    assert session.user_id == "after"
    assert persisted.user_id == "before"
    assert session.runs is not None
    assert persisted.runs, persisted.to_dict()
    assert [message.content for message in session.runs[0].messages or []] == ["prompt", "prompt-snapshot", "later"]
    assert [message.content for message in persisted.runs[0].messages or []] == ["prompt-snapshot"]


@pytest.mark.asyncio
async def test_accepted_real_sqlite_save_survives_close_and_keeps_its_lane_alive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An accepted save may reconnect after disposal, and owns its lane until done."""
    storage = _storage(tmp_path, "close-reconnect")
    owner, session = _owner_and_session("agent", storage, "close-reconnect")
    lane = persistence_patch._registered_lane(storage)
    assert lane is not None
    storage_reference = weakref.ref(storage)
    lane_reference = weakref.ref(lane)
    worker_started = threading.Event()
    release_worker = threading.Event()
    original_save = persistence_patch._ORIGINAL_AGENT_SAVE_SESSION

    def delayed_save(saved_owner: Agent, saved: AgentSession) -> None:
        worker_started.set()
        assert release_worker.wait(timeout=5)
        original_save(saved_owner, saved)

    monkeypatch.setattr(persistence_patch, "_ORIGINAL_AGENT_SAVE_SESSION", delayed_save)
    save = asyncio.create_task(owner.asave_session(session))
    assert await asyncio.to_thread(worker_started.wait, 5)
    storage.close()
    del lane, owner, session, storage
    gc.collect()
    assert storage_reference() is not None
    assert lane_reference() is not None
    release_worker.set()
    await save
    del save
    await asyncio.sleep(0)
    gc.collect()

    assert storage_reference() is None
    assert lane_reference() is None
    fresh_storage = _storage(tmp_path, "close-reconnect")
    try:
        assert get_agent_session(fresh_storage, "close-reconnect") is not None
    finally:
        fresh_storage.close()
