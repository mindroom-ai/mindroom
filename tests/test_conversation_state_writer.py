"""Tests for conversation-state persistence scope selection and the ``session:started`` a reply's first write fires."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mindroom.config.agent import AgentConfig, AgentPrivateConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.constants import MATRIX_RESPONSE_EVENT_ID_METADATA_KEY
from mindroom.conversation_state_writer import (
    ConversationStateWriter,
    ConversationStateWriterDeps,
    ReplySession,
    session_started_on_creation,
)
from mindroom.history.session_context import create_scope_session_storage, load_bound_scope_session
from mindroom.history.types import HistoryScope
from mindroom.hooks import EVENT_SESSION_STARTED, HookBindings, HookRegistry, SessionHookContext, hook
from mindroom.llm.messages import Message
from mindroom.llm.runs import RunRecord
from mindroom.llm.usage import HelperUsage, RunMetrics
from mindroom.message_target import MessageTarget
from mindroom.session_storage.agent_storage import create_state_storage
from mindroom.session_storage.lanes import run_session_storage_operation
from mindroom.session_storage.types import ConversationSession
from mindroom.tool_system.worker_routing import ToolExecutionIdentity
from tests.authorization_helpers import make_test_tool_runtime_context
from tests.conftest import (
    bind_runtime_paths,
    make_conversation_reader_mock,
    make_relation_lookup,
    runtime_paths_for,
    seed_session,
    seed_session_row,
    test_runtime_paths,
)
from tests.history_helpers import _plugin

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from mindroom.session_storage.store import ConversationStore

SESSION_ID = "!room:localhost:$thread"


@pytest.mark.asyncio
async def test_persist_response_event_id_keeps_assistant_history_plain(tmp_path: Path) -> None:
    """Response linkage belongs in run metadata, not assistant-role content."""
    runtime_paths = test_runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={"test_agent": AgentConfig(display_name="Test Agent")},
            models={"default": ModelConfig(provider="ollama", id="test-model")},
        ),
        runtime_paths,
    )
    writer = ConversationStateWriter(
        ConversationStateWriterDeps(
            runtime=SimpleNamespace(config=config),
            logger=MagicMock(),
            runtime_paths=runtime_paths,
            agent_name="test_agent",
        ),
    )
    scope = writer.history_scope()
    run = RunRecord(
        run_id="run-1",
        agent_id="test_agent",
        session_id="session-1",
        content="Final answer",
        messages=[
            Message(role="user", content="Question"),
            Message(role="assistant", content="Final answer"),
        ],
    )
    storage = writer.create_storage(None, scope=scope)
    seed_session(
        storage,
        ConversationSession(
            kind="agent",
            session_id="session-1",
            owner_id="test_agent",
            runs=[run],
            created_at=1,
        ),
    )

    await writer.apersist_response_event_id_in_session_run(
        execution_identity=None,
        scope=scope,
        session_id="session-1",
        run_id="run-1",
        response_event_id="$answer",
    )

    persisted = storage.load_session("session-1")
    assert persisted is not None
    assert persisted.runs is not None
    persisted_run = persisted.runs[0]
    assert persisted_run.metadata == {MATRIX_RESPONSE_EVENT_ID_METADATA_KEY: "$answer"}
    assert persisted_run.messages is not None
    assert persisted_run.messages[-1].content == "Final answer"


def test_private_ad_hoc_team_history_scope_is_requester_partitioned(tmp_path: Path) -> None:
    """Private ad hoc team replay must not share one team scope across requesters."""
    runtime_paths = test_runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={
                "shared": AgentConfig(display_name="Shared"),
                "private_worker": AgentConfig(
                    display_name="PrivateWorker",
                    private=AgentPrivateConfig(per="user"),
                ),
            },
            models={"default": ModelConfig(provider="ollama", id="test-model")},
        ),
        runtime_paths,
    )
    runtime_paths = runtime_paths_for(config)
    writer = ConversationStateWriter(
        ConversationStateWriterDeps(
            runtime=SimpleNamespace(config=config),
            logger=MagicMock(),
            runtime_paths=runtime_paths,
            agent_name="shared",
        ),
    )

    alice_scope = writer.team_history_scope(
        ["private_worker", "shared"],
        requester_user_id="@alice:localhost",
    )
    bob_scope = writer.team_history_scope(
        ["private_worker", "shared"],
        requester_user_id="@bob:localhost",
    )

    assert alice_scope.kind == "team"
    assert bob_scope.kind == "team"
    assert alice_scope.scope_id.startswith("team_private_worker+shared_requester_")
    assert bob_scope.scope_id.startswith("team_private_worker+shared_requester_")
    assert alice_scope != bob_scope
    with pytest.raises(ValueError, match=r"^Private ad hoc team history scope requires requester identity$"):
        writer.team_history_scope(["private_worker", "shared"], requester_user_id=None)


def test_private_ad_hoc_team_history_scope_matches_bound_team_storage(tmp_path: Path) -> None:
    """Bookkeeping storage and the team run must use the same private ad hoc scope."""
    runtime_paths = test_runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={
                "shared": AgentConfig(display_name="Shared"),
                "private_worker": AgentConfig(
                    display_name="PrivateWorker",
                    private=AgentPrivateConfig(per="user"),
                ),
            },
            models={"default": ModelConfig(provider="ollama", id="test-model")},
        ),
        runtime_paths,
    )
    runtime_paths = runtime_paths_for(config)
    writer = ConversationStateWriter(
        ConversationStateWriterDeps(
            runtime=SimpleNamespace(config=config),
            logger=MagicMock(),
            runtime_paths=runtime_paths,
            agent_name="shared",
        ),
    )
    execution_identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="shared",
        requester_id="@alice:localhost",
        room_id="!room:localhost",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id="session-1",
    )
    writer_scope = writer.team_history_scope(
        ["private_worker", "shared"],
        requester_user_id="@alice:localhost",
    )
    writer_storage = create_scope_session_storage(
        scope=writer_scope,
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=execution_identity,
    )
    bound_scope_context = load_bound_scope_session(
        member_names=["private_worker", "shared"],
        team_name=None,
        session_id="session-1",
        runtime_paths=runtime_paths,
        config=config,
        execution_identity=execution_identity,
    )
    assert bound_scope_context.scope == writer_scope
    assert bound_scope_context.storage.path == writer_storage.path


def _reply(
    tmp_path: Path,
    seen: list[SessionHookContext | str],
    *,
    scope: HistoryScope | None = None,
    hooks: bool = True,
) -> tuple[ReplySession, ConversationStore]:
    """Return one reply's session, whose ``session:started`` hooks append to ``seen``, and the store it writes."""

    @hook(EVENT_SESSION_STARTED)
    async def started(ctx: SessionHookContext) -> None:
        seen.append(ctx)

    runtime_paths = test_runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={"test_agent": AgentConfig(display_name="Test Agent")},
            models={"default": ModelConfig(provider="ollama", id="test-model")},
        ),
        runtime_paths,
    )
    registry = HookRegistry.from_plugins([_plugin("session-hooks", [started])]) if hooks else HookRegistry.empty()
    # A room message the agent answers in a new thread: the session is the thread the reply started.
    context = make_test_tool_runtime_context(
        agent_name="test_agent",
        target=MessageTarget(
            room_id="!room:localhost",
            source_thread_id=None,
            resolved_thread_id="$thread",
            reply_to_event_id="$thread",
            session_id=SESSION_ID,
        ),
        requester_id="@user:localhost",
        client=AsyncMock(),
        config=config,
        runtime_paths=runtime_paths,
        relations=make_relation_lookup(),
        conversation_reader=make_conversation_reader_mock(),
        hooks=HookBindings(registry=registry),
    )
    reply = ReplySession(
        scope=scope or HistoryScope(kind="agent", scope_id="test_agent"),
        tool_context=context,
    )
    return reply, create_state_storage(reply.scope.scope_id, tmp_path / "sessions", reply.scope)


def _first_session(reply: ReplySession, session_id: str = SESSION_ID) -> ConversationSession:
    return ConversationSession(session_id=session_id, kind=reply.scope.kind, owner_id=reply.scope.scope_id)


def _usage_write(session: ConversationSession) -> Callable[[ConversationStore], None]:
    """Return a helper's usage write into ``session``, which creates the session's row when it has none."""

    def write(opened: ConversationStore) -> None:
        opened.save_independent_usage(
            session_id=session.session_id,
            usage_id="u1",
            kind="compaction_summary",
            requester_id=None,
            usage=HelperUsage(run_id="u1", model="m", provider="P", metrics=RunMetrics(), created_at=1),
            initial_session=session,
        )

    return write


@pytest.mark.asyncio
async def test_session_started_fires_once_after_the_reply_whose_write_created_its_session(tmp_path: Path) -> None:
    """A write the reply makes in a task and a lane creates the row; one ``session:started`` fires once the reply ends."""
    seen: list[SessionHookContext | str] = []
    reply, store = _reply(tmp_path, seen)
    session = _first_session(reply)

    async def helper_call() -> None:
        await run_session_storage_operation(lambda: store, _usage_write(session))

    async with session_started_on_creation(reply, correlation_id="corr-1"):
        await asyncio.create_task(helper_call())
        # The run's own save comes later and finds the row.
        await run_session_storage_operation(
            lambda: store,
            lambda opened: opened.save_run(session, _run(), record_usage=False),
        )
        seen.append("reply ended")

    assert seen[0] == "reply ended"
    [started] = seen[1:]
    assert isinstance(started, SessionHookContext)
    assert (started.scope, started.session_id, started.room_id, started.thread_id) == (
        reply.scope,
        SESSION_ID,
        "!room:localhost",
        "$thread",
    )
    assert (started.agent_name, started.correlation_id) == ("test_agent", "corr-1")
    runtime_context = reply.tool_context
    assert (started.config, started.runtime_paths) == (runtime_context.config, runtime_context.runtime_paths)


@pytest.mark.asyncio
async def test_session_started_names_the_team_of_a_team_session(tmp_path: Path) -> None:
    """A team reply's hooks see the team, not the bot running it, as the session's agent."""
    seen: list[SessionHookContext | str] = []
    reply, store = _reply(tmp_path, seen, scope=HistoryScope(kind="team", scope_id="crew"))

    async with session_started_on_creation(reply, correlation_id="corr-1"):
        store.update_metadata(_first_session(reply), lambda _metadata: {"k": "v"})

    [started] = seen
    assert isinstance(started, SessionHookContext)
    assert (started.scope.key, started.agent_name) == ("team:crew", "crew")


@pytest.mark.asyncio
async def test_session_started_names_the_running_agent_of_an_agent_session(tmp_path: Path) -> None:
    """An agent reply's hooks see the agent the reply runs as, not the agent scope's ID."""
    seen: list[SessionHookContext | str] = []
    reply, store = _reply(tmp_path, seen, scope=HistoryScope(kind="agent", scope_id="other_agent"))

    async with session_started_on_creation(reply, correlation_id="corr-1"):
        store.update_metadata(_first_session(reply), lambda _metadata: {"k": "v"})

    [started] = seen
    assert isinstance(started, SessionHookContext)
    assert (started.scope.key, started.agent_name) == ("agent:other_agent", "test_agent")


@pytest.mark.asyncio
async def test_session_started_skips_a_session_whose_row_existed(tmp_path: Path) -> None:
    """A reply that only writes into a row an earlier write created fires nothing."""
    seen: list[SessionHookContext | str] = []
    reply, store = _reply(tmp_path, seen)
    seed_session_row(store, _first_session(reply))

    async with session_started_on_creation(reply, correlation_id="corr-1"):
        store.save_run(_first_session(reply), _run(), record_usage=False)

    assert seen == []


@pytest.mark.asyncio
async def test_session_started_skips_rows_of_other_sessions(tmp_path: Path) -> None:
    """A row the reply creates for another session, such as a delegated agent's, is not its own session's."""
    seen: list[SessionHookContext | str] = []
    reply, store = _reply(tmp_path, seen)
    other_agent = ConversationSession(session_id=SESSION_ID, kind="agent", owner_id="helper")

    async with session_started_on_creation(reply, correlation_id="corr-1"):
        store.update_metadata(_first_session(reply, "!room:localhost:$other"), lambda _metadata: {"k": "v"})
        store.update_metadata(other_agent, lambda _metadata: {"k": "v"})

    assert seen == []


@pytest.mark.asyncio
async def test_session_started_skips_a_reply_that_raises(tmp_path: Path) -> None:
    """A reply that raises after creating its row, as one stopped by shutdown does, fires nothing."""
    seen: list[SessionHookContext | str] = []
    reply, store = _reply(tmp_path, seen)

    async def stopped_reply() -> None:
        async with session_started_on_creation(reply, correlation_id="corr-1"):
            store.save_run(_first_session(reply), _run(), record_usage=False)
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await stopped_reply()

    assert seen == []


@pytest.mark.asyncio
@pytest.mark.parametrize("reply_kind", ["no hook", "none"])
async def test_session_started_watches_nothing_without_a_hook_or_a_reply(tmp_path: Path, reply_kind: str) -> None:
    """With no ``session:started`` hook, or without a reply, nothing is watched."""
    seen: list[SessionHookContext | str] = []
    reply, store = _reply(tmp_path, seen, hooks=reply_kind != "no hook")
    watched: ReplySession | None = None if reply_kind == "none" else reply

    with patch("mindroom.conversation_state_writer.watch_session_row", side_effect=AssertionError("watched")):
        async with session_started_on_creation(watched, correlation_id="corr-1"):
            store.save_run(_first_session(reply), _run(), record_usage=False)

    assert seen == []


def _run() -> RunRecord:
    return RunRecord(
        run_id="run-1",
        agent_id="test_agent",
        session_id=SESSION_ID,
        content="Answer",
        messages=[Message(role="user", content="Question"), Message(role="assistant", content="Answer")],
    )
