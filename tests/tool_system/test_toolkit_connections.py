"""An agent's run connects the toolkits that hold a connection for one run, and closes them after it.

A toolkit that sets ``requires_connect``, such as Postgres, holds a connection its run must close.
"""

from __future__ import annotations

import asyncio
import threading
from contextlib import contextmanager
from dataclasses import replace
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest
from agno.tools import Toolkit

from mindroom.agent_cli.minimal_agent import prepare_minimal_catalog
from mindroom.agent_loop.spec import RunContext
from mindroom.agents import create_agent
from mindroom.ai import stream_agent_response
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import DefaultsConfig, ModelConfig
from mindroom.constants import resolve_runtime_paths
from mindroom.message_target import MessageTarget
from mindroom.run_storage import RunStorage
from mindroom.session_storage.types import ConversationSession
from mindroom.tool_system.agent_tool_calls import execute_agent_tool_call
from mindroom.tool_system.plugins import config_plugins
from mindroom.tool_system.runtime_context import tool_runtime_context
from mindroom.tool_system.tool_access import ToolKey
from mindroom.tool_system.toolkit_connections import run_connection, shielded_lifecycle
from tests.authorization_helpers import make_test_tool_runtime_context
from tests.conftest import (
    bind_runtime_paths,
    install_model_providers,
    make_conversation_reader_mock,
    make_relation_lookup,
    make_turn_context,
    runtime_paths_for,
    test_runtime_paths,
)
from tests.fakes.scripted_model import Await, ScriptedModel, ScriptStep, answer, calls, tool_call
from tests.identity_helpers import persist_entity_accounts
from tests.scripted_agent_helpers import raising, registered_toolkit, streamed_text
from tests.turn_model_helpers import turn_model

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence
    from pathlib import Path

    from mindroom.tool_system.runtime_context import ToolRuntimeContext

# What the toolkits and the model saw, in order: each connect and close, and each model request.
EVENTS: list[str] = []


class _DatabaseTools(Toolkit):
    """A toolkit that holds a database connection for one run, as Agno's Postgres toolkit does."""

    _requires_connect = True

    def __init__(self) -> None:
        super().__init__(name="database", tools=[self.query])

    def connect(self) -> None:
        """Open the connection, recording it."""
        EVENTS.append("database connect")

    def close(self) -> None:
        """Close the connection, recording it."""
        EVENTS.append("database close")

    def query(self, sql: str) -> str:
        """Run one query.

        Args:
            sql: The query.

        """
        return f"1 row for {sql}"


class _ArchiveTools(Toolkit):
    """A second toolkit that holds a connection for one run."""

    _requires_connect = True

    def __init__(self) -> None:
        super().__init__(name="archive", tools=[self.fetch])

    def connect(self) -> None:
        """Open the connection, recording it."""
        EVENTS.append("archive connect")

    def close(self) -> None:
        """Close the connection, recording it."""
        EVENTS.append("archive close")

    def fetch(self, key: str) -> str:
        """Fetch one archived record.

        Args:
            key: The record's key.

        """
        return f"record {key}"


class _BlockingDatabaseTools(_DatabaseTools):
    """A database toolkit whose blocking connect waits until the test lets it finish."""

    def __init__(self) -> None:
        super().__init__()
        self.connecting = threading.Event()
        self.release = threading.Event()

    def connect(self) -> None:
        """Start connecting, then finish once released."""
        self.connecting.set()
        self.release.wait()
        super().connect()


@pytest.fixture(autouse=True)
def _events() -> Iterator[None]:
    EVENTS.clear()
    yield
    EVENTS.clear()


def _request() -> Await:
    """A scripted step that records the model request it belongs to."""

    async def record() -> None:
        EVENTS.append("request")

    return Await(record)


@contextmanager
def _registered() -> Iterator[None]:
    """Register the database and archive toolkits for one test."""
    with registered_toolkit("database", _DatabaseTools), registered_toolkit("archive", _ArchiveTools):
        yield


def _agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    turns: Sequence[Sequence[ScriptStep]],
) -> tuple[Config, ScriptedModel]:
    """Configure an agent with the database and archive toolkits on a scripted model."""
    config = bind_runtime_paths(
        Config(
            agents={"worker": AgentConfig(display_name="Worker", tools=["database", "archive"])},
            models={"default": ModelConfig(provider="openai", id="test-model", api_key="test-key")},
        ),
        test_runtime_paths(tmp_path),
    )
    config.memory.backend = "none"
    model = ScriptedModel([list(turn) for turn in turns])
    install_model_providers(monkeypatch, {"default": model})
    return config, model


async def _reply(config: Config) -> list[str]:
    chunks = [
        chunk
        async for chunk in stream_agent_response(
            replace(
                make_turn_context("worker", session_id="session", run_id="run", requester_id="@user:localhost"),
                prompt="Fetch mindroom.",
                show_tool_calls=False,
            ),
            runtime_paths_for(config),
            config,
        )
    ]
    return streamed_text(chunks)


def _helper_runtime(tmp_path: Path, agent: AgentConfig) -> ToolRuntimeContext:
    """Return the tool runtime of a turn of ``agent``, configured as ``helper`` on a test model."""
    config = bind_runtime_paths(
        Config(
            agents={"helper": agent},
            defaults=DefaultsConfig(tools=[]),
            models={"default": ModelConfig(provider="openai", id="test-model")},
        ),
        test_runtime_paths(tmp_path),
    )
    return make_test_tool_runtime_context(
        agent_name="helper",
        target=MessageTarget("!room:test", "$thread", "$thread", "$event", "session"),
        requester_id="@alice:test",
        client=MagicMock(),
        config=config,
        runtime_paths=runtime_paths_for(config),
        relations=make_relation_lookup(),
        conversation_reader=make_conversation_reader_mock(),
    )


@pytest.mark.asyncio
async def test_a_run_connects_its_toolkits_before_the_first_request_and_closes_them_after(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both toolkits connect before the first request, their functions answer, and both close once the run ends."""
    turns = [
        [_request(), *calls(tool_call("fetch", "call_fetch", key="mindroom"))],
        [_request(), *answer("Found it.")],
    ]
    with _registered():
        config, model = _agent(tmp_path, monkeypatch, turns)
        text = await _reply(config)

    assert "".join(text) == "Found it."
    assert {"fetch", "query"} <= {tool.name for tool in model.requests[0].tools}
    assert model.requests[1].messages[-1].content == "record mindroom"
    assert EVENTS == ["database connect", "archive connect", "request", "request", "archive close", "database close"]


@pytest.mark.asyncio
async def test_a_run_that_fails_still_closes_what_it_connected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A request that raises ends the run, and the run still closes both toolkits."""
    with _registered():
        config, _model = _agent(tmp_path, monkeypatch, [[_request(), raising(ValueError("boom"))]])
        await _reply(config)

    assert EVENTS == ["database connect", "archive connect", "request", "archive close", "database close"]


@pytest.mark.asyncio
async def test_a_minimal_turn_s_catalog_connects_its_toolkits_until_it_closes(tmp_path: Path) -> None:
    """A minimal turn offers only Bash, so its catalog connects the eager toolkits and each deferred one it builds."""
    with _registered():
        runtime = _helper_runtime(
            tmp_path,
            AgentConfig(display_name="Helper", tools=["shell", "database", {"archive": {"defer": True}}]),
        )
        build = create_agent(
            "helper",
            runtime.config,
            runtime.runtime_paths,
            runtime.execution_identity,
            agent_mode="minimal",
            knowledge=None,
            model=turn_model(runtime.config, runtime.runtime_paths, "helper"),
            plugins=config_plugins(runtime.config, runtime.runtime_paths),
        )
        session = ConversationSession(session_id=runtime.session_id, kind="agent", owner_id="helper")
        with tool_runtime_context(runtime):
            catalog = await prepare_minimal_catalog(
                build,
                run=RunContext(
                    run_id="run",
                    user_id=runtime.requester_id,
                    session=session,
                ),
                storage=RunStorage(None, session, None),
                delegation_depth=0,
            )
            try:
                assert EVENTS == ["database connect"]
                binding = await catalog.bind(ToolKey("archive", "fetch"))
                events = [event async for event in execute_agent_tool_call(binding, "call_fetch", {"key": "x"})]
                assert events[-1].call is not None
                assert events[-1].call.result == "record x"
            finally:
                await catalog.close()

    assert EVENTS == ["database connect", "archive connect", "archive close", "database close"]


def test_the_primary_does_not_connect_a_toolkit_a_worker_runs(tmp_path: Path) -> None:
    """A worker builds and connects its own copy of a toolkit routed through it, so the primary leaves it alone."""
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={"MINDROOM_SANDBOX_EXECUTION_MODE": "all", "MINDROOM_SANDBOX_PROXY_URL": "http://sandbox.invalid"},
    )
    with _registered():
        config = Config.validate_with_runtime(
            {
                "models": {"default": {"provider": "ollama", "id": "test-model"}},
                "agents": {
                    "worker": {
                        "display_name": "Worker",
                        "tools": ["database"],
                        "include_default_tools": False,
                        "memory_backend": "none",
                    },
                },
            },
            runtime_paths,
        )
        persist_entity_accounts(config, runtime_paths, usernames={"router": "router", "worker": "worker"})
        build = create_agent(
            "worker",
            config,
            runtime_paths,
            execution_identity=None,
            persist_runtime_state=False,
            knowledge=None,
            model=turn_model(config, runtime_paths, "worker"),
            plugins=config_plugins(config, runtime_paths),
        )

    toolkits = {toolkit.name: toolkit for toolkit in build.spec.toolkits}
    assert toolkits["database"].connect is None
    assert EVENTS == []


@pytest.mark.asyncio
async def test_cancelling_a_blocking_connect_closes_the_connection_once_it_is_made() -> None:
    """The connect's thread goes on after its run stops waiting for it, and what it connects is closed."""
    toolkit = _BlockingDatabaseTools()
    connect = run_connection(toolkit)
    assert connect is not None
    connecting = asyncio.create_task(connect())
    try:
        assert await asyncio.to_thread(toolkit.connecting.wait, 5)
        connecting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await connecting
        cleanup = [task for task in asyncio.all_tasks() if task.get_name() == "toolkit-connect-cleanup"]
        assert len(cleanup) == 1
        assert EVENTS == []
    finally:
        toolkit.release.set()
    await asyncio.wait_for(cleanup[0], timeout=5)

    assert EVENTS == ["database connect", "database close"]


@pytest.mark.asyncio
async def test_a_reconnect_waits_until_a_cancelled_connect_has_closed_its_connection() -> None:
    """A later run connects the toolkit only once an earlier cancelled connect closed what it made, so it stays open."""
    toolkit = _BlockingDatabaseTools()
    connect = run_connection(toolkit)
    assert connect is not None
    cancelled = asyncio.create_task(connect())
    assert await asyncio.to_thread(toolkit.connecting.wait, 5)
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled

    reconnecting = asyncio.create_task(connect())
    await asyncio.sleep(0)
    toolkit.release.set()
    close = await asyncio.wait_for(reconnecting, timeout=5)

    assert close is not None
    assert EVENTS == ["database connect", "database close", "database connect"]
    await close()
    assert EVENTS[-1] == "database close"


@pytest.mark.asyncio
async def test_a_shielded_lifecycle_hands_its_task_to_retain_only_when_the_caller_is_cancelled() -> None:
    """The caller gets the lifecycle's value; cancelled instead, it leaves the still-running task to ``retain``."""
    retained: list[asyncio.Task[str]] = []

    async def lifecycle(started: asyncio.Event, release: asyncio.Event) -> str:
        started.set()
        await release.wait()
        return "connected"

    released = asyncio.Event()
    released.set()
    assert await shielded_lifecycle(lifecycle(asyncio.Event(), released), retain=retained.append) == "connected"
    assert retained == []

    started, release = asyncio.Event(), asyncio.Event()
    caller = asyncio.create_task(shielded_lifecycle(lifecycle(started, release), retain=retained.append))
    await started.wait()
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    [task] = retained
    assert not task.done()
    release.set()
    assert await task == "connected"
