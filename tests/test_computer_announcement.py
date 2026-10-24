"""The agent's worker computer is announced in MindRoom Chat the first time its browser runs there."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest
from agno.tools import Toolkit

import mindroom.custom_tools.chat_ui as chat_ui_module
import mindroom.tools  # noqa: F401
from mindroom.agents import create_agent
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.config.plugin import PluginEntryConfig
from mindroom.constants import resolve_runtime_paths
from mindroom.custom_tools import computer_announcement
from mindroom.custom_tools.browser import BrowserTools
from mindroom.custom_tools.computer_announcement import computer_announcement_middleware
from mindroom.hooks import EVENT_TOOL_BEFORE_CALL, HookRegistry, ToolBeforeCallContext, hook
from mindroom.tool_jobs.execution import agent_tool_middleware
from mindroom.tool_system.agno_toolkits import adapt_toolkit
from mindroom.tool_system.invocation import ToolCallContext, execute_tool_call
from mindroom.tool_system.runtime_context import tool_runtime_context
from mindroom.tool_system.tool_hooks import build_tool_hook_middleware
from tests.chat_ui_contract_fixture import make_chat_ui_context
from tests.conftest import bind_runtime_paths, install_model_providers
from tests.fakes.scripted_model import ScriptedModel

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.invocation import ToolMiddleware


@pytest.fixture
def events() -> list[str]:
    """Record the order of announcements and browser calls."""
    return []


@pytest.fixture
def announce(monkeypatch: pytest.MonkeyPatch, events: list[str]) -> AsyncMock:
    """Stand in for the Chat UI notice, which tests/test_chat_ui_tools.py covers."""
    announce = AsyncMock(side_effect=lambda: events.append("announced"))
    monkeypatch.setattr(computer_announcement, "show_computer_once", announce)
    return announce


def _agent(
    tmp_path: Path,
    tools: list[str],
    *,
    backend: str = "docker",
    computer: bool = True,
    deferred: bool = False,
) -> tuple[Config, RuntimePaths]:
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path,
        process_env={"MINDROOM_WORKER_BACKEND": backend, "MINDROOM_WORKER_COMPUTER_ENABLED": str(computer).lower()},
    )
    browsers = [tool for tool in tools if tool in {"browser", "browser_mcp"}]
    config = Config(
        agents={
            "researcher": AgentConfig(
                display_name="Researcher",
                tools=[{tool: {"defer": True}} if deferred and tool in browsers else tool for tool in tools],
                worker_tools=browsers,
                worker_scope="user_agent",
            ),
        },
        models={"default": ModelConfig(provider="openai", id="gpt-5.6-luna")},
    )
    return config, runtime_paths


def _middleware(toolkit: Toolkit, tool_name: str, config: Config, runtime_paths: RuntimePaths) -> ToolMiddleware | None:
    return computer_announcement_middleware(
        toolkit,
        tool_name,
        agent_name="researcher",
        config=config,
        runtime_paths=runtime_paths,
    )


def _fake_browser(events: list[str]) -> Toolkit:
    async def browser_control(action: str, target: str | None = None) -> str:
        """Drive the browser."""
        events.append(f"{action} on {target or 'default'}")
        return f"{action} done"

    return Toolkit(name="browser", tools=[browser_control])


def _real_browser(runtime_paths: RuntimePaths, events: list[str], *, default_target: str) -> BrowserTools:
    """Keep BrowserTools' own call placement but replace the browser body."""
    toolkit = BrowserTools(
        runtime_paths,
        default_target=default_target,  # type: ignore[arg-type]
        device_user_id="@desktop:example.org",
        device_id="DESKTOP",
        device_ed25519="fingerprint",
    )

    async def body(**arguments: object) -> str:
        events.append(f"{arguments['action']} on {arguments.get('target') or 'default'}")
        return "browsed"

    toolkit.async_functions["browser_control"].entrypoint = body
    return toolkit


async def _call(
    toolkit: Toolkit,
    function_name: str,
    middleware: Sequence[ToolMiddleware | None],
    **arguments: object,
) -> object:
    """Run one call of the toolkit through the loop's tool executor and the given middleware chain."""
    function = next(function for function in adapt_toolkit(toolkit).functions if function.name == function_name)
    outcome = await execute_tool_call(
        function,
        arguments,
        ToolCallContext(run_id="run-1", session_id="session-1", call_id="call-1"),
        middleware=[item for item in middleware if item is not None],
    )
    assert not outcome.is_error, outcome.content
    return outcome.content


@pytest.mark.asyncio
async def test_browser_call_announces_then_runs(tmp_path: Path, announce: AsyncMock, events: list[str]) -> None:
    """A worker browser call announces the computer first and returns the browser's own result."""
    config, runtime_paths = _agent(tmp_path, ["browser", "chat_ui"])
    toolkit = _fake_browser(events)

    result = await _call(
        toolkit,
        "browser_control",
        [_middleware(toolkit, "browser", config, runtime_paths)],
        action="open",
    )

    assert result == "open done"
    announce.assert_awaited_once_with()
    assert events == ["announced", "open on default"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("default_target", "arguments", "announced"),
    [
        ("host", {"action": "open", "target": "desktop"}, False),
        ("desktop", {"action": "open"}, False),
        ("desktop", {"action": "open", "target": "host"}, True),
    ],
)
async def test_browser_call_announces_only_when_it_runs_on_the_worker(
    tmp_path: Path,
    announce: AsyncMock,
    events: list[str],
    default_target: str,
    arguments: dict[str, object],
    announced: bool,
) -> None:
    """Calls that drive the user's desktop browser, explicitly or by default, do not announce the computer."""
    config, runtime_paths = _agent(tmp_path, ["browser", "chat_ui"])
    toolkit = _real_browser(runtime_paths, events, default_target=default_target)
    middleware = _middleware(toolkit, "browser", config, runtime_paths)

    assert await _call(toolkit, "browser_control", [middleware], **arguments) == "browsed"

    assert announce.await_count == int(announced)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["help", "actions", " Help "])
async def test_reference_actions_do_not_announce(
    tmp_path: Path,
    announce: AsyncMock,
    events: list[str],
    action: str,
) -> None:
    """Help and actions only return reference data, so the first real browser call still announces."""
    config, runtime_paths = _agent(tmp_path, ["browser", "chat_ui"])
    toolkit = _real_browser(runtime_paths, events, default_target="host")
    middleware = _middleware(toolkit, "browser", config, runtime_paths)

    assert await _call(toolkit, "browser_control", [middleware], action=action) == "browsed"
    announce.assert_not_awaited()

    assert await _call(toolkit, "browser_control", [middleware], action="open") == "browsed"
    announce.assert_awaited_once_with()


@pytest.mark.asyncio
@pytest.mark.usefixtures("announce")
async def test_browser_mcp_functions_announce(tmp_path: Path, events: list[str]) -> None:
    """Every browser_mcp function runs on the worker computer, so each call announces it."""
    config, runtime_paths = _agent(tmp_path, ["browser_mcp", "chat_ui"])

    async def browser_navigate(url: str) -> str:
        """Navigate."""
        events.append(f"navigate {url}")
        return "navigated"

    async def browser_snapshot() -> str:
        """Snapshot."""
        events.append("snapshot")
        return "snapshot"

    toolkit = Toolkit(name="browser_mcp", tools=[browser_navigate, browser_snapshot])
    middleware = _middleware(toolkit, "browser_mcp", config, runtime_paths)

    assert await _call(toolkit, "browser_navigate", [middleware], url="https://example.org") == "navigated"
    assert await _call(toolkit, "browser_snapshot", [middleware]) == "snapshot"

    assert events == ["announced", "navigate https://example.org", "announced", "snapshot"]


@pytest.mark.asyncio
async def test_team_member_browser_call_does_not_announce_the_response_owners_computer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    events: list[str],
) -> None:
    """A member's browser runs under the response owner's context, and a notice would name the owner."""
    monkeypatch.setattr(chat_ui_module, "_SHOWN_COMPUTERS", {})
    config, runtime_paths = _agent(tmp_path, ["browser", "chat_ui"])
    toolkit = _fake_browser(events)
    middleware = _middleware(toolkit, "browser", config, runtime_paths)
    owner = make_chat_ui_context(tmp_path / "owner", agent_name="general")
    member = make_chat_ui_context(tmp_path / "member", agent_name="researcher")
    assert owner.client.user_id != member.client.user_id

    with tool_runtime_context(owner):
        assert await _call(toolkit, "browser_control", [middleware], action="open") == "open done"
    owner.client.room_send.assert_not_awaited()

    with tool_runtime_context(member):
        assert await _call(toolkit, "browser_control", [middleware], action="open") == "open done"
    member.client.room_send.assert_awaited_once()
    assert events == ["open on default", "open on default"]


@pytest.mark.asyncio
async def test_announcement_failure_still_runs_the_browser_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    announce: AsyncMock,
    events: list[str],
) -> None:
    """A failed announcement is logged and never changes the browser call's result."""
    announce.side_effect = RuntimeError("Matrix is down")
    logger = MagicMock()
    monkeypatch.setattr(computer_announcement, "logger", logger)
    config, runtime_paths = _agent(tmp_path, ["browser", "chat_ui"])
    toolkit = _fake_browser(events)

    assert (
        await _call(toolkit, "browser_control", [_middleware(toolkit, "browser", config, runtime_paths)], action="open")
        == "open done"
    )

    assert events == ["open on default"]
    logger.warning.assert_called_once()


@pytest.mark.parametrize(
    ("tools", "tool_name", "backend", "computer"),
    [
        (["browser"], "browser", "docker", True),
        (["browser", "chat_ui"], "browser", "static", True),
        (["browser", "chat_ui"], "browser", "docker", False),
        (["browser_mcp", "chat_ui"], "browser", "docker", True),
        (["shell", "browser", "chat_ui"], "shell", "docker", True),
    ],
    ids=["without-chat-ui", "without-computer", "computer-disabled", "not-the-computer-browser", "other-tool"],
)
def test_no_middleware_without_chat_ui_or_computer_or_for_other_tools(
    tmp_path: Path,
    events: list[str],
    tools: list[str],
    tool_name: str,
    backend: str,
    computer: bool,
) -> None:
    """Only the browser tool that gives a chat_ui agent its worker computer announces it."""
    config, runtime_paths = _agent(tmp_path, tools, backend=backend, computer=computer)

    assert _middleware(_fake_browser(events), tool_name, config, runtime_paths) is None


@pytest.mark.asyncio
async def test_announcement_runs_inside_the_tool_hook_policy(
    tmp_path: Path,
    announce: AsyncMock,
    events: list[str],
) -> None:
    """The announcement is the innermost policy, so a call a plugin declines announces nothing."""

    @hook(EVENT_TOOL_BEFORE_CALL)
    async def decline(ctx: ToolBeforeCallContext) -> None:
        ctx.decline("browsing is paused")

    plugin = SimpleNamespace(
        name="policy",
        discovered_hooks=(decline,),
        discovered_automations=(),
        entry_config=PluginEntryConfig(path="./plugins/policy", settings={}),
        plugin_order=0,
    )
    bridge = build_tool_hook_middleware(HookRegistry.from_plugins([plugin]), agent_name="researcher")
    config, runtime_paths = _agent(tmp_path, ["browser", "chat_ui"])
    toolkit = _fake_browser(events)

    with tool_runtime_context(make_chat_ui_context(tmp_path / "chat")):
        result = await _call(
            toolkit,
            "browser_control",
            [bridge, _middleware(toolkit, "browser", config, runtime_paths)],
            action="open",
        )

    assert "browsing is paused" in str(result)
    announce.assert_not_awaited()
    assert events == []


@pytest.mark.parametrize(("tools", "announces"), [(["browser", "chat_ui"], True), (["browser"], False)])
def test_agent_factory_attaches_the_announcement_to_the_worker_browser(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tools: list[str],
    announces: bool,
) -> None:
    """create_agent gives a chat_ui agent's worker browser functions the announcement as their own policy, alone."""
    config, runtime_paths = _agent(tmp_path, tools)
    config = bind_runtime_paths(config, runtime_paths)
    install_model_providers(monkeypatch, {"default": ScriptedModel([])})

    build = create_agent("researcher", config, runtime_paths, execution_identity=None)

    assert [middleware.__name__ for middleware in build.tool_policy] == ["bind_live_functions", "middleware"]
    carried = {
        function.name: [middleware.__name__ for middleware in function.middleware]
        for toolkit in build.spec.toolkits
        for function in toolkit.functions
    }
    assert carried["browser_control"] == (["announce_computer"] if announces else [])
    assert sum(names.count("announce_computer") for names in carried.values()) == int(announces)


@pytest.mark.asyncio
async def test_a_deferred_browser_a_minimal_turn_builds_later_announces_the_computer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    announce: AsyncMock,
    events: list[str],
) -> None:
    """A minimal turn builds a deferred browser when the agent first calls it, after the agent's policy was built.

    Its calls still announce the computer, through the agent's policy as the CLI catalog runs them.
    """
    config, runtime_paths = _agent(tmp_path, ["browser", "chat_ui"], deferred=True)
    config = bind_runtime_paths(config, runtime_paths)
    install_model_providers(monkeypatch, {"default": ScriptedModel([])})
    build = create_agent("researcher", config, runtime_paths, execution_identity=None, agent_mode="minimal")
    [deferred] = build.surface.deferred
    assert deferred.name == "browser"

    [function] = (await deferred.materialize()).functions

    async def body(**arguments: object) -> str:
        events.append(f"{arguments['action']} on {arguments.get('target') or 'default'}")
        return "browsed"

    outcome = await execute_tool_call(
        replace(function, entrypoint=body),
        {"action": "open", "target": "host"},
        ToolCallContext(run_id="run-1", session_id="session-1", call_id="call-1"),
        middleware=agent_tool_middleware(build),
    )
    assert not outcome.is_error, outcome.content
    announce.assert_awaited_once_with()
    assert events == ["announced", "open on host"]
