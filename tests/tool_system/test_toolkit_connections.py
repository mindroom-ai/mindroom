"""An agent's run connects the toolkits that hold a connection for one run, and closes them after it.

A toolkit that sets ``requires_connect``, such as Postgres, holds a connection its run must close. An owned toolkit
whose calls start a process resource it holds, as the browser's start Chromium, is released after the run or call.
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
from structlog.testing import capture_logs

from mindroom.agent_cli.minimal_agent import prepare_minimal_catalog
from mindroom.agent_loop.spec import RunContext
from mindroom.agents import create_agent
from mindroom.ai import stream_agent_response
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import DefaultsConfig, ModelConfig
from mindroom.constants import resolve_runtime_paths
from mindroom.custom_tools.browser import BrowserTools
from mindroom.message_target import MessageTarget
from mindroom.run_storage import RunStorage
from mindroom.session_storage.types import ConversationSession
from mindroom.tool_system import authoring
from mindroom.tool_system.agent_tool_calls import execute_agent_tool_call
from mindroom.tool_system.agno_toolkits import configure_toolkit_functions
from mindroom.tool_system.plugins import config_plugins
from mindroom.tool_system.runtime_context import tool_runtime_context
from mindroom.tool_system.tool_access import ToolKey
from mindroom.tool_system.tool_hooks import run_sync_tool_entrypoint
from mindroom.tool_system.toolkit_connections import (
    RetainedToolkitCleanups,
    _shielded_lifecycle,
    release_toolkit,
    run_connection,
    toolkit_call_lifetime,
)
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
    tools: Sequence[str] = ("database", "archive"),
) -> tuple[Config, ScriptedModel]:
    """Configure an agent with ``tools``, by default the database and archive toolkits, on a scripted model."""
    config = bind_runtime_paths(
        Config(
            agents={"worker": AgentConfig(display_name="Worker", tools=list(tools))},
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
    assert await _shielded_lifecycle(lifecycle(asyncio.Event(), released), retain=retained.append) == "connected"
    assert retained == []

    started, release = asyncio.Event(), asyncio.Event()
    caller = asyncio.create_task(_shielded_lifecycle(lifecycle(started, release), retain=retained.append))
    await started.wait()
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    [task] = retained
    assert not task.done()
    release.set()
    assert await task == "connected"


class _FailingLifecycleTools(Toolkit):
    """A toolkit whose connect and close raise what the test sets, recording each step."""

    _requires_connect = True

    def __init__(self, *, connect_error: Exception | None = None, close_error: Exception | None = None) -> None:
        super().__init__(name="database")
        self.connect_error = connect_error
        self.close_error = close_error

    def connect(self) -> None:
        EVENTS.append("database connect")
        if self.connect_error is not None:
            raise self.connect_error

    def close(self) -> None:
        EVENTS.append("database close")
        if self.close_error is not None:
            raise self.close_error


@pytest.mark.asyncio
async def test_a_call_whose_connect_fails_closes_the_toolkit_and_raises_the_connect_s_error() -> None:
    """The connect's error is the call's, with a close error as its cause, and the call's block never runs."""
    connect_error, close_error = RuntimeError("connect failed"), RuntimeError("close failed")
    toolkit = _FailingLifecycleTools(connect_error=connect_error, close_error=close_error)

    with pytest.raises(RuntimeError) as raised:
        async with toolkit_call_lifetime(toolkit, RetainedToolkitCleanups(failure_event="test_cleanup_failed")):
            EVENTS.append("body")

    assert raised.value is connect_error
    assert raised.value.__cause__ is close_error
    assert EVENTS == ["database connect", "database close"]


@pytest.mark.asyncio
async def test_a_retained_close_raises_no_body_failure_and_logs_its_own_by_type() -> None:
    """A body that fails after its call ended already answered it; a failed retained close logs only its type."""
    cleanups = RetainedToolkitCleanups(failure_event="test_cleanup_failed")
    toolkit = _FailingLifecycleTools(close_error=RuntimeError("private close detail"))
    entered, release = threading.Event(), threading.Event()

    def body() -> None:
        entered.set()
        release.wait(5)
        EVENTS.append("body")
        msg = "private body detail"
        raise ValueError(msg)

    running: list[asyncio.Task[None]] = []

    async def failing_call() -> None:
        async with toolkit_call_lifetime(toolkit, cleanups):
            running.append(asyncio.create_task(run_sync_tool_entrypoint(body)))
            assert await asyncio.to_thread(entered.wait, 5)
            raise LookupError

    with capture_logs() as logs:
        with pytest.raises(LookupError):
            await failing_call()
        [cleanup] = cleanups.tasks
        assert EVENTS == ["database connect"]
        release.set()
        assert await cleanups.drain(5)
        with pytest.raises(ValueError, match="private body detail"):
            await running[0]

    assert cleanup.done()
    assert EVENTS == ["database connect", "body", "database close"]
    assert [entry for entry in logs if entry["event"] == "test_cleanup_failed"] == [
        {"error_type": "RuntimeError", "event": "test_cleanup_failed", "log_level": "warning"},
    ]
    assert "private" not in str(logs)


class _CacheTools(Toolkit):
    """A toolkit that holds no connection, though it has a ``close`` of its own."""

    def __init__(self) -> None:
        super().__init__(name="cache", tools=[])

    def close(self) -> None:
        """Record the close."""
        EVENTS.append("cache close")


@pytest.mark.asyncio
async def test_releasing_a_toolkit_leaves_one_that_holds_nothing_alone() -> None:
    """Only a toolkit that holds a connection or its calls' resources is closed or released."""
    await release_toolkit(_CacheTools())
    await release_toolkit(_DatabaseTools())

    assert EVENTS == ["database close"]


class _ProfileTools(authoring.Toolkit):
    """An owned toolkit whose calls start a process resource it holds until released, as the browser's start Chromium."""

    def __init__(self) -> None:
        super().__init__(name="profile")

    @authoring.tool
    def open_page(self, url: str) -> str:
        """Open one page.

        Args:
            url: The page.

        """
        EVENTS.append("page open")
        return f"opened {url}"

    async def aclose(self) -> None:
        """Release the process resource, recording it."""
        EVENTS.append("profile release")


class _FailingProfileTools(_ProfileTools):
    """A resource-holding toolkit whose release fails."""

    async def aclose(self) -> None:
        """Fail to release, recording the attempt."""
        await super().aclose()
        msg = "release failed"
        raise RuntimeError(msg)


@pytest.mark.asyncio
@pytest.mark.parametrize("fails", [False, True])
async def test_a_run_releases_an_owned_toolkit_holding_process_resources_once_it_ends(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fails: bool,
) -> None:
    """The run releases what the toolkit's calls started, however the run ends, after closing the toolkits after it."""
    turn = [_request(), raising(ValueError("boom"))] if fails else [_request(), *answer("Opened.")]
    turns = [[_request(), *calls(tool_call("open_page", "call_open", url="https://example.com"))], turn]
    with registered_toolkit("profile", _ProfileTools), registered_toolkit("database", _DatabaseTools):
        config, _model = _agent(tmp_path, monkeypatch, turns, tools=("profile", "database"))
        await _reply(config)

    assert EVENTS == ["database connect", "request", "page open", "request", "database close", "profile release"]


@pytest.mark.asyncio
async def test_a_failed_release_is_logged_and_the_run_still_closes_its_other_toolkits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A release that raises is a warning, as a failed close is, and every other toolkit still closes."""
    turns = [[_request(), *answer("Nothing to open.")]]
    with registered_toolkit("database", _DatabaseTools), registered_toolkit("profile", _FailingProfileTools):
        config, _model = _agent(tmp_path, monkeypatch, turns, tools=("database", "profile"))
        with capture_logs() as logs:
            await _reply(config)

    assert EVENTS == ["database connect", "request", "profile release", "database close"]
    assert [(entry["toolkit"], entry["error"]) for entry in logs if entry["event"] == "toolkit_close_failed"] == [
        ("profile", "release failed"),
    ]


@pytest.mark.asyncio
async def test_a_call_lifetime_releases_an_owned_toolkit_holding_process_resources() -> None:
    """A call that builds its own toolkit, as the MCP gateway and script runs do, releases it once the call ends."""
    cleanups = RetainedToolkitCleanups(failure_event="test_cleanup_failed")
    async with toolkit_call_lifetime(_ProfileTools(), cleanups):
        EVENTS.append("body")
    await release_toolkit(_ProfileTools())

    assert EVENTS == ["body", "profile release", "profile release"]


class _SlowProfileTools(_ProfileTools):
    """A resource-holding toolkit whose release waits until the test lets it finish."""

    def __init__(self) -> None:
        super().__init__()
        self.release = asyncio.Event()

    async def aclose(self) -> None:
        """Start releasing, then finish once released."""
        EVENTS.append("profile release started")
        await self.release.wait()
        await super().aclose()


@pytest.mark.asyncio
async def test_a_cancelled_call_hands_the_release_to_its_cleanups() -> None:
    """A cancelled call does not wait for the release; its cleanups finish it, so a shutdown can wait for them."""
    cleanups = RetainedToolkitCleanups(failure_event="test_cleanup_failed")
    toolkit = _SlowProfileTools()
    entered = asyncio.Event()

    async def call() -> None:
        async with toolkit_call_lifetime(toolkit, cleanups):
            entered.set()
            await asyncio.Event().wait()

    calling = asyncio.create_task(call())
    await entered.wait()
    calling.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(calling, timeout=5)

    assert len(cleanups.tasks) == 1
    toolkit.release.set()
    assert await cleanups.drain(5)
    assert EVENTS == ["profile release started", "profile release"]


def test_a_toolkit_routed_to_a_worker_holds_no_process_resources_here() -> None:
    """A worker holds the resources of the calls routed to it, so the primary has nothing to release."""
    toolkit = configure_toolkit_functions(_ProfileTools(), to_worker=lambda _name, entrypoint: entrypoint)

    assert run_connection(toolkit) is None


@pytest.mark.asyncio
async def test_a_primary_browser_is_released_after_the_run_that_used_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A browser whose calls run in the primary closes its profiles, so the next reply's browser can open them."""
    toolkit = BrowserTools(test_runtime_paths(tmp_path))

    async def close_profiles() -> None:
        EVENTS.append("browser profiles closed")

    monkeypatch.setattr(toolkit, "_close_profiles", close_profiles)
    connect = run_connection(toolkit)
    assert connect is not None
    close = await connect()
    assert close is not None
    await close()

    assert EVENTS == ["browser profiles closed"]


class _VendorSessionTools(Toolkit):
    """A vendor toolkit with an ``aclose`` of its own, which MindRoom leaves to the vendor's lifecycle."""

    def __init__(self) -> None:
        super().__init__(name="vendor_session", tools=[])

    async def aclose(self) -> None:
        """Record the close."""
        EVENTS.append("vendor close")


def test_a_vendor_toolkit_with_its_own_aclose_is_not_released() -> None:
    """Only MindRoom's own toolkits declare process resources through ``aclose``."""
    assert run_connection(_VendorSessionTools()) is None
