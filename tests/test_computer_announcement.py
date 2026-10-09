"""The agent's worker computer is announced in MindRoom Chat the first time its browser runs there."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest
from agno.tools import Toolkit
from agno.tools.function import FunctionCall

import mindroom.tools  # noqa: F401
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.plugin import PluginEntryConfig
from mindroom.constants import resolve_runtime_paths
from mindroom.custom_tools import computer_announcement
from mindroom.custom_tools.browser import BrowserTools
from mindroom.custom_tools.computer_announcement import attach_computer_announcement
from mindroom.hooks import EVENT_TOOL_BEFORE_CALL, HookRegistry, ToolBeforeCallContext, hook
from mindroom.tool_system.runtime_context import tool_runtime_context
from mindroom.tool_system.tool_hooks import build_tool_hook_bridge, prepend_tool_hook_bridge
from tests.chat_ui_contract_fixture import make_chat_ui_context

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths


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
                tools=tools,
                worker_tools=browsers,
                worker_scope="user_agent",
            ),
        },
    )
    return config, runtime_paths


def _attach(toolkit: Toolkit, tool_name: str, config: Config, runtime_paths: RuntimePaths) -> Toolkit:
    return attach_computer_announcement(
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


async def _call(toolkit: Toolkit, function_name: str, **arguments: object) -> object:
    execution = await FunctionCall(
        function=toolkit.get_async_functions()[function_name],
        arguments=arguments,
    ).aexecute()
    assert execution.status == "success", execution.error
    return execution.result


@pytest.mark.asyncio
async def test_browser_call_announces_then_runs(tmp_path: Path, announce: AsyncMock, events: list[str]) -> None:
    """A worker browser call announces the computer first and returns the browser's own result."""
    config, runtime_paths = _agent(tmp_path, ["browser", "chat_ui"])
    toolkit = _attach(_fake_browser(events), "browser", config, runtime_paths)

    result = await _call(toolkit, "browser_control", action="open")

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
async def test_desktop_target_does_not_announce(
    tmp_path: Path,
    announce: AsyncMock,
    events: list[str],
    default_target: str,
    arguments: dict[str, object],
    announced: bool,
) -> None:
    """Calls that drive the user's desktop browser, explicitly or by default, never announce the computer."""
    config, runtime_paths = _agent(tmp_path, ["browser", "chat_ui"])
    toolkit = _attach(
        _real_browser(runtime_paths, events, default_target=default_target),
        "browser",
        config,
        runtime_paths,
    )

    assert await _call(toolkit, "browser_control", **arguments) == "browsed"

    assert announce.await_count == int(announced)


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

    toolkit = _attach(
        Toolkit(name="browser_mcp", tools=[browser_navigate, browser_snapshot]),
        "browser_mcp",
        config,
        runtime_paths,
    )

    assert await _call(toolkit, "browser_navigate", url="https://example.org") == "navigated"
    assert await _call(toolkit, "browser_snapshot") == "snapshot"

    assert events == ["announced", "navigate https://example.org", "announced", "snapshot"]


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
    toolkit = _attach(_fake_browser(events), "browser", config, runtime_paths)

    assert await _call(toolkit, "browser_control", action="open") == "open done"

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
def test_toolkit_unchanged_without_chat_ui_or_computer_or_for_other_tools(
    tmp_path: Path,
    events: list[str],
    tools: list[str],
    tool_name: str,
    backend: str,
    computer: bool,
) -> None:
    """Only the browser tool that gives a chat_ui agent its worker computer announces it."""
    config, runtime_paths = _agent(tmp_path, tools, backend=backend, computer=computer)
    toolkit = _fake_browser(events)
    function = toolkit.async_functions["browser_control"]
    existing_hook = MagicMock()
    function.tool_hooks = [existing_hook]

    assert _attach(toolkit, tool_name, config, runtime_paths) is toolkit

    assert function.tool_hooks == [existing_hook]


@pytest.mark.asyncio
async def test_announcement_hook_runs_inside_the_tool_hook_bridge(
    tmp_path: Path,
    announce: AsyncMock,
    events: list[str],
) -> None:
    """The announcement is the innermost hook, so a call a plugin declines announces nothing."""

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
    bridge = build_tool_hook_bridge(HookRegistry.from_plugins([plugin]), agent_name="researcher")
    config, runtime_paths = _agent(tmp_path, ["browser", "chat_ui"])
    toolkit = prepend_tool_hook_bridge(_fake_browser(events), bridge)
    function = toolkit.async_functions["browser_control"]
    bridge_hooks = list(function.tool_hooks)

    _attach(toolkit, "browser", config, runtime_paths)

    assert len(function.tool_hooks) == len(bridge_hooks) + 1
    assert function.tool_hooks[:-1] == bridge_hooks
    with tool_runtime_context(make_chat_ui_context(tmp_path / "chat")):
        result = await _call(toolkit, "browser_control", action="open")

    assert "browsing is paused" in str(result)
    announce.assert_not_awaited()
    assert events == []
