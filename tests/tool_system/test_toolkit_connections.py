"""An agent's run connects the toolkits that hold a connection for one run, and closes them after it, as Agno's did.

An Agno ``MCPTools`` wrapper registers its functions only once its session is connected, and a toolkit that sets
``requires_connect``, such as Postgres, holds a connection its run must close.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

import pytest
from agno.tools import Toolkit
from agno.tools.mcp import MCPTools
from mcp.types import CallToolResult, ListToolsResult, TextContent, Tool

from mindroom.agent_cli.minimal_agent import prepare_minimal_catalog
from mindroom.agent_loop.spec import RunContext
from mindroom.agents import create_agent
from mindroom.ai import stream_agent_response
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.constants import resolve_runtime_paths
from mindroom.session_storage.types import ConversationSession
from mindroom.tool_system.agent_tool_calls import execute_agent_tool_call
from mindroom.tool_system.runtime_context import build_execution_identity_from_runtime_context, tool_runtime_context
from mindroom.tool_system.tool_access import ToolKey
from tests.conftest import bind_runtime_paths, make_turn_context, runtime_paths_for, test_runtime_paths
from tests.fakes.scripted_model import Await, ScriptedModel, ScriptStep, answer, calls, tool_call
from tests.identity_helpers import persist_entity_accounts
from tests.scripted_agent_helpers import install_agent_model, raising, registered_toolkit, streamed_text
from tests.test_agent_cli_authority import _runtime_context

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence
    from pathlib import Path

# What the toolkits and the model saw, in order: each connect and close, and each model request.
EVENTS: list[str] = []


class _Session:
    """An MCP client session to a server that offers one ``lookup`` tool."""

    async def initialize(self) -> None:
        """Complete the MCP handshake."""

    async def send_ping(self) -> None:
        """Answer a liveness check."""

    async def list_tools(self) -> ListToolsResult:
        """List the server's one tool."""
        schema = {"type": "object", "properties": {"term": {"type": "string"}}, "required": ["term"]}
        return ListToolsResult(tools=[Tool(name="lookup", description="Look up a term.", input_schema=schema)])

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> CallToolResult:
        """Answer one call of the tool."""
        assert arguments is not None
        return CallToolResult(content=[TextContent(type="text", text=f"{name} found {arguments['term']}")])


class _ServerTools(MCPTools):
    """A plugin's wrapper around Agno ``MCPTools`` that records each connect and close of its session."""

    def __init__(self, **kwargs: Any) -> None:  # noqa: ANN401
        super().__init__(session=_Session(), name="server", **kwargs)

    async def connect(self, force: bool = False) -> None:
        """Connect the session, recording it."""
        EVENTS.append("server connect")
        await super().connect(force)

    async def close(self) -> None:
        """Close the session, recording it."""
        EVENTS.append("server close")
        await super().close()


class _ConnectedServerTools(_ServerTools):
    """A wrapper whose plugin connected its session itself, before any run."""

    def __init__(self, **kwargs: Any) -> None:  # noqa: ANN401
        super().__init__(**kwargs)
        asyncio.run(self.initialize())


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
def _registered(server: type[_ServerTools]) -> Iterator[None]:
    """Register the MCP wrapper and the database toolkit for one test."""
    with registered_toolkit("mcp_server", server), registered_toolkit("database", _DatabaseTools):
        yield


def _agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    turns: Sequence[Sequence[ScriptStep]],
) -> tuple[Config, ScriptedModel]:
    """Configure an agent with the MCP wrapper and the database toolkit on a scripted model."""
    config = bind_runtime_paths(
        Config(
            agents={"worker": AgentConfig(display_name="Worker", tools=["mcp_server", "database"])},
            models={"default": ModelConfig(provider="openai", id="test-model", api_key="test-key")},
        ),
        test_runtime_paths(tmp_path),
    )
    config.memory.backend = "none"
    model = ScriptedModel([list(turn) for turn in turns])
    install_agent_model(monkeypatch, model)
    return config, model


async def _reply(config: Config) -> list[str]:
    chunks = [
        chunk
        async for chunk in stream_agent_response(
            make_turn_context("worker", session_id="session", run_id="run", requester_id="@user:localhost"),
            prompt="Look up mindroom.",
            runtime_paths=runtime_paths_for(config),
            config=config,
            show_tool_calls=False,
        )
    ]
    return streamed_text(chunks)


@pytest.mark.asyncio
async def test_a_run_connects_its_toolkits_before_the_first_request_and_closes_them_after(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The MCP functions are offered and answer from the run's session; both toolkits close once the run ends."""
    turns = [
        [_request(), *calls(tool_call("lookup", "call_lookup", term="mindroom"))],
        [_request(), *answer("Found it.")],
    ]
    with _registered(_ServerTools):
        config, model = _agent(tmp_path, monkeypatch, turns)
        text = await _reply(config)

    assert "".join(text) == "Found it."
    assert {"lookup", "query"} <= {tool.name for tool in model.requests[0].tools}
    assert model.requests[1].messages[-1].content == "lookup found mindroom"
    assert EVENTS == [
        # The build lists the server's functions through a connection of its own.
        "server connect",
        "server close",
        "server connect",
        "database connect",
        "request",
        "request",
        "database close",
        "server close",
    ]


@pytest.mark.asyncio
async def test_a_run_that_fails_still_closes_what_it_connected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A request that raises ends the run, and the run still closes both toolkits."""
    with _registered(_ServerTools):
        config, _model = _agent(tmp_path, monkeypatch, [[_request(), raising(ValueError("boom"))]])
        await _reply(config)

    assert EVENTS[2:] == ["server connect", "database connect", "request", "database close", "server close"]


@pytest.mark.asyncio
async def test_a_session_connected_before_the_run_is_left_to_its_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A run neither connects nor closes an MCP session its plugin connected already, and still offers its functions."""
    turns = [[_request(), *calls(tool_call("lookup", "call_lookup", term="mindroom"))], [_request(), *answer("Done.")]]
    with _registered(_ConnectedServerTools):
        config, model = _agent(tmp_path, monkeypatch, turns)
        await _reply(config)

    assert model.requests[1].messages[-1].content == "lookup found mindroom"
    assert EVENTS == ["database connect", "request", "request", "database close"]


@pytest.mark.asyncio
async def test_a_minimal_turn_s_catalog_connects_its_toolkits_until_it_closes(tmp_path: Path) -> None:
    """A minimal turn offers only Bash, so its catalog connects the eager toolkits and each deferred one it builds."""
    runtime = _runtime_context(tmp_path)
    runtime.config.agents["helper"] = AgentConfig(
        display_name="Helper",
        tools=["shell", "database", {"mcp_server": {"defer": True}}],
    )
    with _registered(_ServerTools):
        build = create_agent(
            "helper",
            runtime.config,
            runtime.runtime_paths,
            build_execution_identity_from_runtime_context(runtime),
            agent_mode="minimal",
        )
        with tool_runtime_context(runtime):
            catalog = await prepare_minimal_catalog(
                build,
                run=RunContext(run_id="run", session_id=runtime.session_id, user_id=runtime.requester_id),
                session=ConversationSession(session_id=runtime.session_id, kind="agent", owner_id="helper"),
                store=None,
                delegation_depth=0,
            )
            try:
                assert EVENTS == ["database connect"]
                binding = await catalog.bind(ToolKey("mcp_server", "lookup"))
                events = [event async for event in execute_agent_tool_call(binding, "call_lookup", {"term": "x"})]
                assert events[-1].execution is not None
                assert events[-1].execution.result == "lookup found x"
            finally:
                await catalog.close()

    assert EVENTS == [
        "database connect",
        "server connect",
        "server close",
        "server connect",
        "server close",
        "database close",
    ]


def test_the_primary_neither_lists_nor_connects_a_toolkit_a_worker_runs(tmp_path: Path) -> None:
    """A worker builds and connects its own copy of a toolkit routed through it, so the primary leaves it alone."""
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={"MINDROOM_SANDBOX_EXECUTION_MODE": "all", "MINDROOM_SANDBOX_PROXY_URL": "http://sandbox.invalid"},
    )
    with _registered(_ServerTools):
        config = Config.validate_with_runtime(
            {
                "models": {"default": {"provider": "ollama", "id": "test-model"}},
                "agents": {
                    "worker": {
                        "display_name": "Worker",
                        "tools": ["mcp_server", "database"],
                        "include_default_tools": False,
                        "memory_backend": "none",
                    },
                },
            },
            runtime_paths,
        )
        persist_entity_accounts(config, runtime_paths, usernames={"router": "router", "worker": "worker"})
        build = create_agent("worker", config, runtime_paths, execution_identity=None, persist_runtime_state=False)

    toolkits = {toolkit.name: toolkit for toolkit in build.spec.toolkits}
    assert "server" not in toolkits
    assert toolkits["database"].connect is None
    assert EVENTS == []
